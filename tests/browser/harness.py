import base64
import contextlib
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    expect,
)

from tests.e2e_helpers import (
    PROJECT_ROOT,
    free_tcp_port,
    provision_executor_pair,
    server_env,
)
from workgate.oauth.core.scopes import default_scope
from workgate.ui.contracts import POSIX_TUI_EXECUTABLE_NAME
from workgate.ui.session import (
    UI_CSRF_HEADER,
    UI_SESSION_BINDING_HEADER,
    UI_SESSION_BINDING_STORAGE_KEY,
    UI_SESSION_ESTABLISHED_STORAGE_KEY,
    ui_csrf_cookie_name,
    ui_session_cookie_name,
)

LEGACY_TOKEN_STORAGE_KEY = "workgate-ui-access-token"


def _start_logged_process(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
) -> subprocess.Popen[bytes]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        return subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdout=stdout,
            stderr=stderr,
        )


def _terminate_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _wait_for_http_ready(base_url: str, process: subprocess.Popen[Any]) -> None:
    deadline = time.monotonic() + 15
    with httpx.Client(timeout=1) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(
                    f"server exited early with code {process.returncode}"
                )
            try:
                response = client.get(f"{base_url}/healthz")
                if (
                    response.status_code == 200
                    and response.json().get("ok") is True
                ):
                    return
            except httpx.HTTPError, json.JSONDecodeError:
                pass
            time.sleep(0.05)
    raise AssertionError(f"server did not become ready at {base_url}")


@dataclass
class BrowserHarness:
    root: Path
    artifacts: Path
    control_workspace: Path
    executor_workspace: Path
    control_tmux_tmpdir: Path
    executor_tmux_tmpdir: Path
    base_url: str
    admin_pin: str
    executor_id: str
    opentui_crash_marker: Path
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    server: subprocess.Popen[Any]
    executor: subprocess.Popen[Any]
    api_token: str | None = None
    terminal_sessions: list[tuple[str, str]] = field(default_factory=list)
    console_messages: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    request_failures: list[str] = field(default_factory=list)
    websocket_events: list[str] = field(default_factory=list)

    @classmethod
    def start(
        cls,
        root: Path,
        artifacts: Path,
        playwright: Playwright,
    ) -> BrowserHarness:
        artifacts.mkdir(parents=True, exist_ok=True)
        control_workspace = root / "workspace-control"
        control_workspace.mkdir(parents=True)
        executor_workspace = root / "workspace-executor"
        executor_workspace.mkdir(parents=True)
        control_tmux_tmpdir = Path(tempfile.mkdtemp(prefix="workgate-b-ctl-"))
        executor_tmux_tmpdir = Path(tempfile.mkdtemp(prefix="workgate-b-exe-"))
        (executor_workspace / "notes.txt").write_text(
            "executor browser fixture\n", encoding="utf-8"
        )
        (executor_workspace / "stale-executor.txt").write_text(
            "stale executor preview\n", encoding="utf-8"
        )
        (executor_workspace / "copy-source.txt").write_text(
            "copy source\n", encoding="utf-8"
        )
        opentui_crash_marker = root / "opentui-crash-next"
        opentui_wrapper = root / "opentui-wrapper.py"
        opentui_binary = (
            PROJECT_ROOT / "ui-opentui" / "dist" / POSIX_TUI_EXECUTABLE_NAME
        )
        opentui_wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os\n"
            "from pathlib import Path\n"
            f"marker = Path({str(opentui_crash_marker)!r})\n"
            f"binary = {str(opentui_binary)!r}\n"
            "if marker.exists():\n"
            "    marker.unlink()\n"
            "    print('intentional browser e2e OpenTUI crash', flush=True)\n"
            "    raise SystemExit(17)\n"
            "os.execv(binary, [binary])\n",
            encoding="utf-8",
        )
        opentui_wrapper.chmod(0o755)

        port = free_tcp_port()
        base_url = f"http://127.0.0.1:{port}"
        admin_pin = "browser-e2e-pin-924681"
        control_state_dir = root / "state-control"
        executor_state_dir = root / "state-executor"
        executor_id = provision_executor_pair(
            control_state_dir=control_state_dir,
            executor_state_dir=executor_state_dir,
            control_url=base_url,
            name="browser-loopback",
        )
        env = server_env(
            control_workspace,
            mode="http",
            port=port,
            state_dir=control_state_dir,
        )
        env.update(
            {
                "TMUX_TMPDIR": str(control_tmux_tmpdir),
                "WORKGATE_AUTH_MODE": "oauth",
                "WORKGATE_BASE_URL": base_url,
                "WORKGATE_OAUTH_ADMIN_PIN": admin_pin,
                "WORKGATE_REMOTE_ENABLED": "true",
                "WORKGATE_REMOTE_POLL_TIMEOUT_S": "1",
                "WORKGATE_REMOTE_JOB_TIMEOUT_S": "20",
                "WORKGATE_AGENT_BRIDGE_ENABLED": "false",
                "WORKGATE_UI_TERMINAL_IDLE_TIMEOUT_S": "120",
                "WORKGATE_UI_TUI_COMMAND": str(opentui_wrapper),
            }
        )
        server = _start_logged_process(
            [
                sys.executable,
                "-m",
                "workgate.main",
                "server",
                "--mode",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--auth-mode",
                "oauth",
                "--workspace-root",
                str(control_workspace),
                "--agent-bridge-enabled",
                "false",
                "--remote-enabled",
                "true",
                "--remote-poll-timeout-s",
                "1",
                "--remote-job-timeout-s",
                "20",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            stdout_path=artifacts / "server.stdout.log",
            stderr_path=artifacts / "server.stderr.log",
        )
        executor: subprocess.Popen[Any] | None = None
        try:
            _wait_for_http_ready(base_url, server)
            executor_env = server_env(
                executor_workspace,
                mode="http",
                state_dir=executor_state_dir,
            )
            executor_env["TMUX_TMPDIR"] = str(executor_tmux_tmpdir)
            executor = _start_logged_process(
                [sys.executable, "-m", "workgate.main", "executor", "run"],
                cwd=PROJECT_ROOT,
                env=executor_env,
                stdout_path=artifacts / "executor.stdout.log",
                stderr_path=artifacts / "executor.stderr.log",
            )
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                record_video_dir=str(artifacts / "video"),
            )
            context.set_default_timeout(15_000)
            context.tracing.start(
                screenshots=True, snapshots=True, sources=True
            )
            page = context.new_page()
            harness = cls(
                root=root,
                artifacts=artifacts,
                control_workspace=control_workspace,
                executor_workspace=executor_workspace,
                control_tmux_tmpdir=control_tmux_tmpdir,
                executor_tmux_tmpdir=executor_tmux_tmpdir,
                base_url=base_url,
                admin_pin=admin_pin,
                executor_id=executor_id,
                opentui_crash_marker=opentui_crash_marker,
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
                server=server,
                executor=executor,
            )
            harness._attach_diagnostics()
            return harness
        except Exception:
            _terminate_process(executor)
            _terminate_process(server)
            shutil.rmtree(control_tmux_tmpdir, ignore_errors=True)
            shutil.rmtree(executor_tmux_tmpdir, ignore_errors=True)
            raise

    def _attach_diagnostics(self) -> None:
        def on_console(message: Any) -> None:
            line = f"{message.type}: {message.text}"
            self.console_messages.append(line)
            if message.type == "error":
                self.console_errors.append(line)

        def on_websocket(socket: Any) -> None:
            self.websocket_events.append(f"open {socket.url}")

            def record_frame(direction: str, frame: Any) -> None:
                payload = frame
                if isinstance(frame, dict):
                    payload = frame.get("payload", frame)
                if isinstance(payload, bytes):
                    display = payload[:256].hex()
                else:
                    display = str(payload)[:2048]
                self.websocket_events.append(
                    f"{direction} {socket.url} {display}"
                )

            socket.on("framesent", lambda frame: record_frame("sent", frame))
            socket.on(
                "framereceived", lambda frame: record_frame("received", frame)
            )
            socket.on(
                "close",
                lambda: self.websocket_events.append(f"close {socket.url}"),
            )

        self.page.on("console", on_console)
        self.page.on("websocket", on_websocket)
        self.page.on(
            "pageerror", lambda error: self.page_errors.append(str(error))
        )
        self.page.on(
            "requestfailed",
            lambda request: self.request_failures.append(
                f"{request.method} {request.url}: {request.failure}"
            ),
        )

    def stop(self, *, failed: bool) -> None:
        try:
            if failed and not self.page.is_closed():
                self.page.screenshot(
                    path=str(self.artifacts / "failure.png"), full_page=True
                )
            if not self.page.is_closed():
                for executor_id, shell_id in reversed(self.terminal_sessions):
                    with contextlib.suppress(Exception):
                        self.api(
                            "POST",
                            "/api/ui/terminals/kill",
                            body={
                                "executor_id": executor_id,
                                "shell_id": shell_id,
                            },
                        )
        finally:
            (self.artifacts / "browser-console.log").write_text(
                "\n".join(self.console_messages), encoding="utf-8"
            )
            (self.artifacts / "page-errors.log").write_text(
                "\n".join(self.page_errors), encoding="utf-8"
            )
            (self.artifacts / "request-failures.log").write_text(
                "\n".join(self.request_failures), encoding="utf-8"
            )
            (self.artifacts / "websockets.log").write_text(
                "\n".join(self.websocket_events), encoding="utf-8"
            )
            with contextlib.suppress(Exception):
                self.context.tracing.stop(
                    path=str(self.artifacts / "trace.zip")
                )
            with contextlib.suppress(Exception):
                self.context.close()
            with contextlib.suppress(Exception):
                self.browser.close()
            _terminate_process(self.executor)
            _terminate_process(self.server)
            shutil.rmtree(self.control_tmux_tmpdir, ignore_errors=True)
            shutil.rmtree(self.executor_tmux_tmpdir, ignore_errors=True)

    def wait_executor_online(self) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.executor.poll() is not None:
                raise AssertionError(
                    f"executor exited early with code {self.executor.returncode}; "
                    f"see {self.artifacts / 'executor.stderr.log'}"
                )
            result = self.api("GET", "/api/ui/executors")
            if result["status"] == 200:
                rows = result["payload"]["data"]["executors"]
                if any(
                    row.get("executor_id") == self.executor_id
                    and row.get("online") is True
                    for row in rows
                ):
                    return
            time.sleep(0.05)
        raise AssertionError(
            f"executor {self.executor_id} did not become online; "
            f"see {self.artifacts / 'executor.stderr.log'}"
        )

    def track_terminal(self, executor_id: str, shell_id: str) -> None:
        self.terminal_sessions.append((executor_id, shell_id))

    def api(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = self.page.evaluate(
            """
            async ({
              method,
              path,
              body,
              token,
              csrfCookieName,
              csrfHeaderName,
              bindingHeaderName,
              bindingStorageKey,
            }) => {
              const headers = {};
              const uiRequest = path.startsWith("/api/ui/");
              const unsafe = !["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase());
              if (uiRequest) {
                const binding = localStorage.getItem(bindingStorageKey) || "";
                if (binding) headers[bindingHeaderName] = binding;
              }
              if (uiRequest && unsafe) {
                const prefix = `${encodeURIComponent(csrfCookieName)}=`;
                const csrf = document.cookie
                  .split(";")
                  .map((value) => value.trim())
                  .find((value) => value.startsWith(prefix));
                if (csrf) headers[csrfHeaderName] = decodeURIComponent(csrf.slice(prefix.length));
              } else if (!uiRequest && token) {
                headers.Authorization = `Bearer ${token}`;
              }
              if (body !== null) headers["Content-Type"] = "application/json";
              const response = await fetch(path, {
                method,
                headers,
                body: body === null ? undefined : JSON.stringify(body),
                credentials: "same-origin",
              });
              let payload = null;
              try { payload = await response.json(); } catch (_) {}
              return {status: response.status, payload};
            }
            """,
            {
                "method": method,
                "path": path,
                "body": body,
                "token": self.api_token,
                "csrfCookieName": ui_csrf_cookie_name(self.base_url),
                "csrfHeaderName": UI_CSRF_HEADER,
                "bindingHeaderName": UI_SESSION_BINDING_HEADER,
                "bindingStorageKey": UI_SESSION_BINDING_STORAGE_KEY,
            },
        )
        assert isinstance(result, dict)
        return result

    def issue_token(self, scope: str) -> str:
        callback = f"{self.base_url}/ui/callback"
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode("ascii")).digest()
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        state = secrets.token_urlsafe(24)
        resource = f"{self.base_url}/mcp"
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            registration = client.post(
                f"{self.base_url}/oauth/register",
                json={
                    "client_name": "browser-e2e-scope-client",
                    "redirect_uris": [callback],
                },
            )
            registration.raise_for_status()
            client_id = registration.json()["client_id"]
            authorization = {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": callback,
                "scope": scope,
                "resource": resource,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
            approved = client.post(
                f"{self.base_url}/oauth/authorize?{urlencode(authorization)}",
                data={**authorization, "pin": self.admin_pin},
            )
            assert approved.status_code == 302
            query = parse_qs(urlparse(approved.headers["location"]).query)
            assert query["state"] == [state]
            exchange = client.post(
                f"{self.base_url}/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": query["code"][0],
                    "client_id": client_id,
                    "redirect_uri": callback,
                    "resource": resource,
                    "code_verifier": verifier,
                },
            )
            exchange.raise_for_status()
            return str(exchange.json()["access_token"])

    def set_token(self, token: str) -> None:
        result = self.page.evaluate(
            """
            async ({token, bindingHeaderName, bindingStorageKey}) => {
              const binding = localStorage.getItem(bindingStorageKey) || "";
              const response = await fetch("/api/ui/session/token", {
                method: "POST",
                headers: {
                  Accept: "application/json",
                  Authorization: `Bearer ${token}`,
                  [bindingHeaderName]: binding,
                },
                credentials: "same-origin",
              });
              return {status: response.status, payload: await response.json()};
            }
            """,
            {
                "token": token,
                "bindingHeaderName": UI_SESSION_BINDING_HEADER,
                "bindingStorageKey": UI_SESSION_BINDING_STORAGE_KEY,
            },
        )
        assert result["status"] == 200, result
        self.api_token = token

    def login(self) -> None:
        response = self.page.goto(
            f"{self.base_url}/ui", wait_until="domcontentloaded"
        )
        assert response is not None and response.status == 200
        expect(self.page.locator("#auth-panel")).to_be_visible()
        unauthenticated = self.api("GET", "/api/ui/bootstrap")
        assert unauthenticated["status"] == 401

        waiting_page = self.context.new_page()
        waiting = waiting_page.goto(
            f"{self.base_url}/ui", wait_until="domcontentloaded"
        )
        assert waiting is not None and waiting.status == 200
        expect(waiting_page.locator("#auth-panel")).to_be_visible()

        self.page.locator("#oauth-login").click()
        self.page.wait_for_url(re.compile(r"/oauth/authorize\?"))
        self.page.locator('input[name="pin"]').fill(self.admin_pin)
        self.page.get_by_role("button", name="Approve").click()
        self.page.wait_for_url(re.compile(r"/ui/callback"))
        expect(self.page.locator("#connection-state")).to_have_text("Connected")
        expect(self.page.locator("#auth-panel")).to_be_hidden()
        expect(waiting_page.locator("#connection-state")).to_have_text(
            "Connected"
        )
        expect(waiting_page.locator("#auth-panel")).to_be_hidden()
        established_signal = self.page.evaluate(
            "key => localStorage.getItem(key) || ''",
            UI_SESSION_ESTABLISHED_STORAGE_KEY,
        )
        assert isinstance(established_signal, str) and established_signal
        assert (
            waiting_page.evaluate(
                "key => localStorage.getItem(key) || ''",
                UI_SESSION_ESTABLISHED_STORAGE_KEY,
            )
            == established_signal
        )
        waiting_page.close()
        assert not self.page.evaluate(
            "key => Boolean(sessionStorage.getItem(key))",
            LEGACY_TOKEN_STORAGE_KEY,
        )
        binding = self.page.evaluate(
            "key => localStorage.getItem(key) || ''",
            UI_SESSION_BINDING_STORAGE_KEY,
        )
        assert isinstance(binding, str) and re.fullmatch(
            r"[A-Za-z0-9_-]{43,128}", binding
        )
        cookies = self.context.cookies()
        session_cookie = next(
            (
                cookie
                for cookie in cookies
                if cookie.get("name") == ui_session_cookie_name(self.base_url)
            ),
            None,
        )
        csrf_cookie = next(
            (
                cookie
                for cookie in cookies
                if cookie.get("name") == ui_csrf_cookie_name(self.base_url)
            ),
            None,
        )
        assert session_cookie is not None
        assert csrf_cookie is not None
        assert session_cookie.get("httpOnly") is True
        assert csrf_cookie.get("httpOnly") is False
        assert session_cookie.get("sameSite") == "Strict"
        assert csrf_cookie.get("sameSite") == "Strict"
        session_expires = session_cookie.get("expires")
        csrf_expires = csrf_cookie.get("expires")
        assert isinstance(session_expires, int | float)
        assert isinstance(csrf_expires, int | float)
        assert session_expires > time.time() + 300
        assert csrf_expires > time.time() + 300

        restored_page = self.context.new_page()
        restored = restored_page.goto(
            f"{self.base_url}/ui", wait_until="domcontentloaded"
        )
        assert restored is not None and restored.status == 200
        expect(restored_page.locator("#connection-state")).to_have_text(
            "Connected"
        )
        expect(restored_page.locator("#auth-panel")).to_be_hidden()
        assert not restored_page.evaluate(
            "key => Boolean(sessionStorage.getItem(key))",
            LEGACY_TOKEN_STORAGE_KEY,
        )
        assert (
            restored_page.evaluate(
                "key => localStorage.getItem(key) || ''",
                UI_SESSION_BINDING_STORAGE_KEY,
            )
            == binding
        )
        self.page.close()
        self.page = restored_page
        self._attach_diagnostics()

        self.api_token = self.issue_token(default_scope())
        self.wait_executor_online()
        self.console_errors = [
            line
            for line in self.console_errors
            if "401 (Unauthorized)" not in line
        ]

    def navigate(self, view: str) -> None:
        item = self.page.locator(f'.nav-item[data-view="{view}"]')
        expect(item).to_be_visible()
        item.click()
        expect(item).to_have_attribute("aria-current", "page")
        expect(self.page.locator("#page-title")).to_have_text(
            {
                "overview": "Overview",
                "executors": "Executors",
                "sessions": "Sessions",
                "terminals": "Terminals",
                "files": "Files",
                "audit": "Audit",
                "console": "OpenTUI",
            }[view]
        )

    def assert_clean_browser(self) -> None:
        assert not self.console_errors, "\n".join(self.console_errors)
        assert not self.page_errors, "\n".join(self.page_errors)
        unexpected = [
            line
            for line in self.request_failures
            if "favicon.ico" not in line and "ERR_ABORTED" not in line
        ]
        assert not unexpected, "\n".join(unexpected)
