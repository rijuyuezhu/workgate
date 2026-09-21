from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Generator
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError

import workgate.executor.browser as browser_ops
from tests.helpers import (
    build_paired_control_harness,
    build_tool_session_store,
    mcp_text,
)
from workgate.config.settings import Settings, clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.control.mcp.app import build_mcp
from workgate.control.runtime import build_control_runtime
from workgate.control.state import ExecutorTrustRecord
from workgate.errors import SessionTerminationRequestedError
from workgate.executor.browser import (
    BrowserService,
    browser_capability_available,
)
from workgate.executor.config import resolve_executor_config
from workgate.executor.connection import ExecutorConnection
from workgate.executor.control_client import ExecutorControlClient
from workgate.executor.hello import build_executor_hello
from workgate.executor.profile import ExecutorProfile
from workgate.executor.runtime import build_executor_runtime
from workgate.executor.search_composition import (
    build_executor_dispatcher_with_search,
)
from workgate.executor.sessions import ExecutorSessionService
from workgate.executor.shell_service import ShellService
from workgate.executor.tool_session.store import UnknownAgentSessionError
from workgate.protocol.credentials import (
    executor_credential_verifier,
    new_executor_credential,
)
from workgate.protocol.ids import new_executor_id
from workgate.schemas.input_models.browser import BrowserActionsArg

pytestmark = pytest.mark.browser

if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
    _ORIGINAL_BROWSER_CACHE = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
else:
    _ORIGINAL_CACHE_ROOT = (
        Path(os.environ["XDG_CACHE_HOME"])
        if os.environ.get("XDG_CACHE_HOME")
        else Path(os.environ.get("HOME", "~")).expanduser() / ".cache"
    )
    _ORIGINAL_BROWSER_CACHE = _ORIGINAL_CACHE_ROOT / "ms-playwright"


def _require_chromium(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(_ORIGINAL_BROWSER_CACHE))
    if not browser_capability_available():
        pytest.skip(
            "Playwright Chromium is not installed in this test environment"
        )


def _mcp_data(response: Any) -> dict[str, Any]:
    data = (
        response[1]
        if isinstance(response, tuple)
        else json.loads(mcp_text(response))
    )
    assert isinstance(data, dict)
    return data


async def _wait_executor_online(control, executor_id: str) -> None:
    async def online() -> None:
        while not await control.executor_transport.is_online(executor_id):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(online(), timeout=2.0)


def test_browser_action_schema_rejects_unknown_or_incomplete_fields() -> None:
    adapter = TypeAdapter(BrowserActionsArg)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        adapter.validate_python(
            [{"action": "click", "target": "e1", "value": "ignored-before"}]
        )
    with pytest.raises(ValidationError, match="Field required"):
        adapter.validate_python([{"action": "fill", "target": "e1"}])
    with pytest.raises(
        ValidationError, match="List should have at most 100 items"
    ):
        adapter.validate_python(
            [
                {
                    "action": "select",
                    "target": "e1",
                    "value": [str(index) for index in range(101)],
                }
            ]
        )


@pytest.mark.asyncio
async def test_browser_service_enforces_action_schema_without_echoing_values(
    tmp_path: Path,
) -> None:
    browser, _config, _store, session_id, _workspace = _service(tmp_path)
    secret = "must-not-appear-in-validation-error"

    with pytest.raises(ValueError, match="invalid browser actions") as raised:
        await browser.act(
            session_id,
            "browser_not_reached",
            [{"action": "click", "target": "e1", "value": secret}],
        )

    assert secret not in str(raised.value)
    await browser.aclose()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def _serve_site(directory: Path) -> Generator[str]:
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(_QuietHandler, directory=str(directory)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host = str(server.server_address[0])
        port = int(server.server_address[1])
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _service(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        workspace_root=workspace,
        state_dir=tmp_path / "state",
        agent_bridge_enabled=False,
    )
    config = resolve_executor_config(settings)
    store = build_tool_session_store(settings)
    session_id = "sess_0000000000000000000001"
    store.create_session(session_id=session_id, workdir=workspace)
    return BrowserService(config, store), config, store, session_id, workspace


@pytest.mark.asyncio
async def test_browser_snapshot_actions_ownership_and_screenshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_chromium(monkeypatch)
    service, _config, store, session_id, workspace = _service(tmp_path)
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        """<!doctype html>
<html>
  <head><title>Browser test</title></head>
  <body>
    <input id="secret" type="password" aria-label="Password">
    <button id="go" onclick="document.querySelector('#result').textContent='clicked'">Go</button>
    <div id="result">ready</div>
  </body>
</html>
""",
        encoding="utf-8",
    )

    with _serve_site(site) as base_url:
        started = await service.start(session_id, url=f"{base_url}/index.html")
        browser_id = started["browser_session_id"]

        snapshot = await service.snapshot(session_id, browser_id)
        assert snapshot["title"] == "Browser test"
        assert "ready" in snapshot["text"]
        input_ref = next(
            item["ref"]
            for item in snapshot["interactive_elements"]
            if item["tag"] == "input"
        )
        button_ref = next(
            item["ref"]
            for item in snapshot["interactive_elements"]
            if item["tag"] == "button"
        )

        secret = "browser-entered-secret"
        acted = await service.act(
            session_id,
            browser_id,
            [
                {"action": "fill", "target": input_ref, "value": secret},
                {"action": "click", "target": button_ref},
            ],
        )
        assert len(acted["results"]) == 2

        after = await service.snapshot(
            session_id,
            browser_id,
            screenshot_path="browser-shot.png",
        )
        assert "clicked" in after["text"]
        assert secret not in str(after)
        assert (workspace / "browser-shot.png").is_file()

        with pytest.raises(FileExistsError, match="never overwrite"):
            await service.snapshot(
                session_id,
                browser_id,
                screenshot_path="browser-shot.png",
            )
        with pytest.raises(ValueError, match="only permits"):
            await service.act(
                session_id,
                browser_id,
                [{"action": "navigate", "url": "file:///etc/passwd"}],
            )
        with pytest.raises(ValueError, match="URL credentials"):
            await service.act(
                session_id,
                browser_id,
                [
                    {
                        "action": "navigate",
                        "url": "https://user:pass@example.test/",
                    }
                ],
            )

        other_id = "sess_0000000000000000000002"
        store.create_session(session_id=other_id, workdir=workspace)
        with pytest.raises(ValueError, match="unknown browser session"):
            await service.snapshot(other_id, browser_id)

        closed = await service.close(session_id, browser_id)
        assert closed == {"browser_session_id": browser_id, "closed": True}
        await service.aclose()


@pytest.mark.asyncio
async def test_browser_close_failure_remains_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_chromium(monkeypatch)
    service, _config, _store, session_id, _workspace = _service(tmp_path)
    started = await service.start(session_id)
    browser_id = started["browser_session_id"]
    original_close = service._close_state
    attempts = 0

    async def fail_once(state):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic close failure")
        await original_close(state)

    monkeypatch.setattr(service, "_close_state", fail_once)
    with pytest.raises(RuntimeError, match="synthetic close failure"):
        await service.close(session_id, browser_id)
    assert browser_id in service._cleanup_pending

    listed = await service.manage(session_id, action="list")
    assert listed["sessions"] == []
    assert listed["cleanup_pending"] == []
    assert attempts == 2
    await service.aclose()


@pytest.mark.asyncio
async def test_session_termination_closes_owned_browsers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_chromium(monkeypatch)
    browser, config, store, session_id, _workspace = _service(tmp_path)
    shell = ShellService(config, store)
    sessions = ExecutorSessionService(config, store, shell, browser)
    started = await browser.start(session_id)
    browser_id = started["browser_session_id"]

    result = await sessions.terminate(session_id)

    assert result["absent"] is True
    assert result["stopped_browsers"] == [browser_id]
    with pytest.raises(UnknownAgentSessionError):
        store.require_session(session_id)
    assert browser._sessions == {}
    assert browser._cleanup_pending == {}
    await browser.aclose()


@pytest.mark.asyncio
async def test_browser_operations_reject_cleanup_only_session(
    tmp_path: Path,
) -> None:
    browser, _config, store, session_id, _workspace = _service(tmp_path)
    store.prepare_session_termination(session_id)

    with pytest.raises(SessionTerminationRequestedError):
        await browser.manage(session_id, action="list")

    await browser.aclose()


@pytest.mark.asyncio
async def test_browser_dispatch_is_fenced_from_session_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser, config, store, session_id, _workspace = _service(tmp_path)
    shell = ShellService(config, store)
    sessions = ExecutorSessionService(config, store, shell, browser)
    dispatcher = build_executor_dispatcher_with_search(
        config,
        store,
        shell_service=shell,
        browser_service=browser,
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_manage(owner_session_id: str, **_kwargs):
        assert owner_session_id == session_id
        entered.set()
        await release.wait()
        return {"sessions": [], "cleanup_pending": []}

    monkeypatch.setattr(browser, "manage", blocked_manage)
    operation = asyncio.create_task(
        dispatcher.execute(
            "browser_session",
            {"session_id": session_id, "action": "list"},
        )
    )
    await entered.wait()
    termination = asyncio.create_task(sessions.terminate(session_id))
    await asyncio.sleep(0.02)
    assert termination.done() is False

    release.set()
    await operation
    ended = await termination
    assert ended["absent"] is True
    await browser.aclose()


@pytest.mark.asyncio
async def test_session_termination_waits_for_idle_cleanup_in_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser, config, store, session_id, _workspace = _service(tmp_path)
    shell = ShellService(config, store)
    sessions = ExecutorSessionService(config, store, shell, browser)
    monkeypatch.setattr(browser_ops, "_IDLE_TIMEOUT_S", 0)
    browser_id = "browser_cleanup_race"
    state = browser_ops.BrowserSessionState(
        browser_session_id=browser_id,
        owner_session_id=session_id,
        playwright=object(),
        browser=object(),
        context=object(),
        created_at=0.0,
        last_used_at=0.0,
    )
    browser._sessions[browser_id] = state
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_close(_state) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(browser, "_close_state", blocked_close)
    cleanup = asyncio.create_task(browser._cleanup_idle())
    await entered.wait()
    termination = asyncio.create_task(sessions.terminate(session_id))
    await asyncio.sleep(0.02)
    assert termination.done() is False

    release.set()
    await cleanup
    ended = await termination
    assert ended["absent"] is True
    assert browser_id not in browser._closing
    await browser.aclose()


@pytest.mark.asyncio
async def test_idle_reaper_skips_browser_with_active_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser, _config, _store, session_id, _workspace = _service(tmp_path)
    monkeypatch.setattr(browser_ops, "_IDLE_TIMEOUT_S", 0)
    browser_id = "browser_active_operation"
    state = browser_ops.BrowserSessionState(
        browser_session_id=browser_id,
        owner_session_id=session_id,
        playwright=object(),
        browser=object(),
        context=object(),
        created_at=0.0,
        last_used_at=0.0,
    )
    closed: list[str] = []

    async def record_close(closing_state) -> None:
        closed.append(closing_state.browser_session_id)

    monkeypatch.setattr(browser, "_close_state", record_close)
    browser._sessions[browser_id] = state
    await state.lock.acquire()
    try:
        await browser._cleanup_idle()
        assert browser_id in browser._sessions
        assert closed == []
    finally:
        state.lock.release()

    await browser._cleanup_idle()
    assert browser_id not in browser._sessions
    assert closed == [browser_id]
    await browser.aclose()


@pytest.mark.asyncio
async def test_full_page_screenshot_rejects_oversized_page_before_capture(
    tmp_path: Path,
) -> None:
    browser, _config, _store, session_id, workspace = _service(tmp_path)

    class OversizedPage:
        async def evaluate(self, _script):
            return {"width": 1200, "height": 50_000}

        async def screenshot(self, **_kwargs):
            raise AssertionError(
                "oversized page must be rejected before capture"
            )

    with pytest.raises(ValueError, match="dimension limit"):
        await browser._screenshot(
            session_id,
            OversizedPage(),
            "oversized.png",
            full_page=True,
        )
    assert not (workspace / "oversized.png").exists()
    await browser.aclose()


@pytest.mark.asyncio
async def test_browser_page_budget_closes_excess_popup_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser, _config, _store, session_id, _workspace = _service(tmp_path)
    monkeypatch.setattr(browser_ops, "_MAX_PAGES_PER_BROWSER", 2)

    class FakePage:
        def __init__(self) -> None:
            self.closed = False

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

        def on(self, _event, _handler) -> None:
            return None

    first = FakePage()
    second = FakePage()
    overflow = FakePage()

    class FakeContext:
        pages = [first, second, overflow]

    state = browser_ops.BrowserSessionState(
        browser_session_id="browser_page_budget",
        owner_session_id=session_id,
        playwright=object(),
        browser=object(),
        context=FakeContext(),
        created_at=0.0,
        last_used_at=0.0,
    )

    await browser._sync_pages(state)

    assert overflow.closed is True
    assert len(state.pages) == 2
    assert {id(item.page) for item in state.pages.values()} == {
        id(first),
        id(second),
    }


@pytest.mark.asyncio
async def test_browser_page_budget_cleanup_failure_is_not_silently_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser, _config, _store, session_id, _workspace = _service(tmp_path)
    monkeypatch.setattr(browser_ops, "_MAX_PAGES_PER_BROWSER", 1)

    class FakePage:
        def __init__(self, *, fail_close: bool = False) -> None:
            self.closed = False
            self.fail_close = fail_close

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            if self.fail_close:
                raise RuntimeError("synthetic page close failure")
            self.closed = True

        def on(self, _event, _handler) -> None:
            return None

    first = FakePage()
    overflow = FakePage(fail_close=True)

    class FakeContext:
        pages = [first, overflow]

    state = browser_ops.BrowserSessionState(
        browser_session_id="browser_page_budget_failure",
        owner_session_id=session_id,
        playwright=object(),
        browser=object(),
        context=FakeContext(),
        created_at=0.0,
        last_used_at=0.0,
    )

    with pytest.raises(
        browser_ops.ExecutorOperationFailure,
        match="failed to close a browser page",
    ):
        await browser._sync_pages(state)

    assert overflow.closed is False
    assert len(FakeContext.pages) == 2


@pytest.mark.asyncio
async def test_browser_capability_probe_is_safe_inside_event_loop() -> None:
    assert isinstance(browser_capability_available(), bool)


@pytest.mark.asyncio
async def test_abandoned_browser_is_reaped_without_followup_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _config, _store, session_id, _workspace = _service(tmp_path)
    monkeypatch.setattr(browser_ops, "_IDLE_TIMEOUT_S", 0.02)
    monkeypatch.setattr(browser_ops, "_IDLE_REAP_INTERVAL_S", 0.01)

    class FakeContext:
        closed = False

        async def close(self) -> None:
            self.closed = True

    class FakeBrowser:
        closed = False

        async def close(self) -> None:
            self.closed = True

    class FakePlaywright:
        stopped = False

        async def stop(self) -> None:
            self.stopped = True

    context = FakeContext()
    browser = FakeBrowser()
    playwright = FakePlaywright()
    browser_id = "browser_test_idle_reaper"
    state = browser_ops.BrowserSessionState(
        browser_session_id=browser_id,
        owner_session_id=session_id,
        playwright=playwright,
        browser=browser,
        context=context,
        created_at=0.0,
        last_used_at=0.0,
    )
    service._sessions[browser_id] = state
    service._ensure_reaper()
    try:
        for _ in range(100):
            if (
                browser_id not in service._sessions
                and browser_id not in service._closing
                and context.closed
                and browser.closed
                and playwright.stopped
            ):
                break
            await asyncio.sleep(0.01)

        assert browser_id not in service._sessions
        assert browser_id not in service._closing
        assert browser_id not in service._cleanup_pending
        assert context.closed is True
        assert browser.closed is True
        assert playwright.stopped is True
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_browser_routes_through_control_to_bound_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_chromium(monkeypatch)
    workspace = tmp_path / "routed-workspace"
    workspace.mkdir()
    settings = Settings(
        workspace_root=workspace,
        state_dir=tmp_path / "control-state",
        agent_bridge_enabled=False,
    )
    harness = build_paired_control_harness(settings)
    site = tmp_path / "routed-site"
    site.mkdir()
    (site / "index.html").write_text(
        """<!doctype html>
<html>
  <head><title>Routed browser</title></head>
  <body>
    <button id="go" onclick="document.querySelector('#result').textContent='routed-click'">Go</button>
    <div id="result">ready</div>
  </body>
</html>
""",
        encoding="utf-8",
    )

    try:
        with _serve_site(site) as base_url:
            started = await harness.control.session_coordinator.start_session(
                workdir=".", executor_id=harness.executor_id
            )
            assert isinstance(started, dict)
            session_id = str(started["session_id"])
            browser = (
                await harness.control.session_coordinator.call_session_tool(
                    "browser_session",
                    {
                        "session_id": session_id,
                        "action": "start",
                        "url": f"{base_url}/index.html",
                    },
                )
            )
            assert isinstance(browser, dict)
            browser_id = str(browser["browser_session_id"])

            snapshot = (
                await harness.control.session_coordinator.call_session_tool(
                    "browser_snapshot",
                    {
                        "session_id": session_id,
                        "browser_session_id": browser_id,
                    },
                )
            )
            assert isinstance(snapshot, dict)
            elements = snapshot["interactive_elements"]
            assert isinstance(elements, list)
            button_ref = next(
                str(item["ref"])
                for item in elements
                if isinstance(item, dict)
                if item["tag"] == "button"
            )
            await harness.control.session_coordinator.call_session_tool(
                "browser_act",
                {
                    "session_id": session_id,
                    "browser_session_id": browser_id,
                    "actions": [{"action": "click", "target": button_ref}],
                },
            )
            after = await harness.control.session_coordinator.call_session_tool(
                "browser_snapshot",
                {
                    "session_id": session_id,
                    "browser_session_id": browser_id,
                },
            )
            assert isinstance(after, dict)
            assert "routed-click" in str(after["text"])

            ended = await harness.control.session_coordinator.end_session(
                session_id
            )
            assert browser_id in ended["stopped_browsers"]
    finally:
        await harness.executor.aclose()
        await harness.control.aclose()


@pytest.mark.asyncio
async def test_browser_routes_over_executor_http_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_chromium(monkeypatch)
    control_workspace = tmp_path / "protocol-control-workspace"
    executor_workspace = tmp_path / "protocol-executor-workspace"
    control_workspace.mkdir()
    executor_workspace.mkdir()
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(executor_workspace))
    clear_settings_cache()

    control = build_control_runtime(
        Settings(
            workspace_root=control_workspace,
            state_dir=tmp_path / "protocol-control-state",
            auth_mode="none",
            agent_bridge_enabled=False,
        )
    )
    executor = build_executor_runtime(
        resolve_executor_config(
            Settings(
                workspace_root=executor_workspace,
                state_dir=tmp_path / "protocol-executor-state",
                agent_bridge_enabled=False,
            )
        ),
        enable_control_connection=False,
    )
    executor_id = new_executor_id()
    credential = new_executor_credential()
    site = tmp_path / "protocol-site"
    site.mkdir()
    (site / "index.html").write_text(
        """<!doctype html>
<html>
  <head><title>Protocol browser</title></head>
  <body>
    <button id="go" onclick="document.querySelector('#result').textContent='protocol-click'">Go</button>
    <div id="result">ready</div>
  </body>
</html>
""",
        encoding="utf-8",
    )

    http_client: httpx.AsyncClient | None = None
    connection: ExecutorConnection | None = None
    await control.start()
    await executor.start()
    try:
        control.control_state.put_executor(
            ExecutorTrustRecord(
                executor_id=executor_id,
                name="browser-protocol-executor",
                credential_verifier=executor_credential_verifier(credential),
                created_at=1,
            )
        )
        app = build_http_app(runtime=control)
        profile = ExecutorProfile(
            control_url="http://127.0.0.1",
            executor_id=executor_id,
            credential=credential,
        )
        http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=profile.control_url,
            headers={"Authorization": f"Bearer {credential}"},
        )
        client = ExecutorControlClient(profile, client=http_client)
        connection = ExecutorConnection.from_client(
            client,
            hello_factory=lambda: build_executor_hello(
                executor.config, sessions=executor.sessions.inventory()
            ),
            execute=executor._execute_protocol_command,
            max_concurrent_commands=executor.config.max_concurrent_commands,
        )
        connection.start()
        await _wait_executor_online(control, executor_id)
        mcp = build_mcp(runtime=control)

        with _serve_site(site) as base_url:
            started = _mcp_data(
                await mcp.call_tool("session_start", {"workdir": "."})
            )
            session_id = str(started["session_id"])
            assert started["executor_id"] == executor_id

            browser = _mcp_data(
                await mcp.call_tool(
                    "browser_session",
                    {
                        "session_id": session_id,
                        "action": "start",
                        "url": f"{base_url}/index.html",
                    },
                )
            )
            browser_id = str(browser["browser_session_id"])
            snapshot = _mcp_data(
                await mcp.call_tool(
                    "browser_snapshot",
                    {
                        "session_id": session_id,
                        "browser_session_id": browser_id,
                    },
                )
            )
            elements = snapshot["interactive_elements"]
            assert isinstance(elements, list)
            button_ref = next(
                str(item["ref"])
                for item in elements
                if isinstance(item, dict) and item.get("tag") == "button"
            )
            _mcp_data(
                await mcp.call_tool(
                    "browser_act",
                    {
                        "session_id": session_id,
                        "browser_session_id": browser_id,
                        "actions": [{"action": "click", "target": button_ref}],
                    },
                )
            )
            after = _mcp_data(
                await mcp.call_tool(
                    "browser_snapshot",
                    {
                        "session_id": session_id,
                        "browser_session_id": browser_id,
                    },
                )
            )
            assert "protocol-click" in str(after["text"])

            ended = _mcp_data(
                await mcp.call_tool("session_end", {"session_id": session_id})
            )
            assert browser_id in ended["stopped_browsers"]
    finally:
        if connection is not None:
            await connection.aclose()
        if http_client is not None:
            await http_client.aclose()
        await executor.aclose()
        await control.aclose()
        clear_settings_cache()
