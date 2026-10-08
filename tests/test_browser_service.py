import asyncio
import os
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import Error as PlaywrightError
from pydantic import TypeAdapter, ValidationError

import workgate.executor.browser as browser_ops
from tests.helpers import (
    build_paired_control_harness,
    build_tool_session_store,
    mcp_structured,
)
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings, clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.control.mcp.app import build_mcp
from workgate.control.runtime import build_control_runtime
from workgate.control.state import ExecutorTrustRecord
from workgate.errors import (
    BrowserUnavailableError,
    SessionTerminationRequestedError,
)
from workgate.executor.browser import (
    BrowserService,
    browser_capability_available,
)
from workgate.executor.connection import ExecutorConnection
from workgate.executor.control_client import ExecutorControlClient
from workgate.executor.hello import build_executor_hello
from workgate.executor.profile import ExecutorProfile
from workgate.executor.runtime import build_executor_runtime
from workgate.executor.sessions import ExecutorSessionService
from workgate.executor.shell_service import ShellService
from workgate.executor.tool_composition import build_executor_tool_dispatcher
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
    return mcp_structured(response)


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
    assert adapter.validate_python([{"action": "click", "target": "#submit"}])
    with pytest.raises(
        ValidationError, match="String should have at most 4096 characters"
    ):
        adapter.validate_python([{"action": "click", "target": "x" * 4097}])
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
        default_workdir=workspace,
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
    <textarea id="notes" aria-label="Notes"></textarea>
    <select id="color" aria-label="Color">
      <option value="red">Red</option>
      <option value="blue">Blue</option>
    </select>
    <select id="multi" aria-label="Multi" multiple>
      <option value="red">Red</option>
      <option value="blue">Blue</option>
    </select>
    <input id="agree" type="checkbox" aria-label="Agree">
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
            if item["type"] == "password"
        )
        textarea_ref = next(
            item["ref"]
            for item in snapshot["interactive_elements"]
            if item["tag"] == "textarea"
        )
        select_refs = [
            item["ref"]
            for item in snapshot["interactive_elements"]
            if item["tag"] == "select"
        ]
        checkbox_ref = next(
            item["ref"]
            for item in snapshot["interactive_elements"]
            if item["type"] == "checkbox"
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
                {"action": "type", "target": textarea_ref, "value": "typed"},
                {"action": "press", "target": textarea_ref, "key": "End"},
                {"action": "select", "target": select_refs[0], "value": "blue"},
                {
                    "action": "select",
                    "target": select_refs[1],
                    "value": ["red", "blue"],
                },
                {"action": "check", "target": checkbox_ref},
                {"action": "uncheck", "target": checkbox_ref},
                {"action": "hover", "target": button_ref},
                {"action": "wait", "ms": 1},
                {"action": "wait_for_text", "text": "ready"},
                {
                    "action": "wait_for_url",
                    "url": f"{base_url}/index.html",
                },
                {"action": "click", "target": button_ref},
            ],
        )
        assert len(acted["results"]) == 12

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
        with pytest.raises(ValueError, match="must not be empty"):
            await service.snapshot(
                session_id,
                browser_id,
                screenshot_path="",
            )
        with pytest.raises(ValueError, match="must end in .png"):
            await service.snapshot(
                session_id,
                browser_id,
                screenshot_path="browser-shot.jpg",
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

        page_actions = await service.act(
            session_id,
            browser_id,
            [
                {"action": "new_page", "url": f"{base_url}/index.html"},
                {"action": "close_page"},
                {
                    "action": "navigate",
                    "url": f"{base_url}/index.html",
                    "wait_until": "load",
                },
            ],
        )
        assert len(page_actions["results"]) == 3

        other_id = "sess_0000000000000000000002"
        store.create_session(session_id=other_id, workdir=workspace)
        with pytest.raises(ValueError, match="unknown browser session"):
            await service.snapshot(other_id, browser_id)

        closed = await service.manage(
            session_id,
            action="close",
            browser_session_id=browser_id,
        )
        assert closed == {"browser_session_id": browser_id, "closed": True}
        await service.aclose()


@pytest.mark.asyncio
async def test_browser_start_failure_cleans_partial_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_chromium(monkeypatch)
    service, _config, _store, session_id, _workspace = _service(tmp_path)

    async def fail_page_start(_state):
        raise RuntimeError("synthetic page start failure")

    monkeypatch.setattr(service, "_ensure_page", fail_page_start)
    with pytest.raises(RuntimeError, match="synthetic page start failure"):
        await service.start(session_id)

    assert service._sessions == {}
    assert service._cleanup_pending == {}
    assert service._starting == 0
    await service.aclose()


@pytest.mark.asyncio
async def test_browser_manage_rejects_invalid_lifecycle_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _config, _store, session_id, _workspace = _service(tmp_path)

    with pytest.raises(ValueError, match="browser_session_id"):
        await service.manage(session_id, action="close")
    with pytest.raises(
        ValueError, match="action must be start, list, or close"
    ):
        await service.manage(session_id, action="invalid")

    monkeypatch.setattr(
        browser_ops, "browser_capability_available", lambda: False
    )
    with pytest.raises(
        BrowserUnavailableError, match="Playwright is not installed"
    ):
        await service.start(session_id)

    monkeypatch.setattr(
        browser_ops, "browser_capability_available", lambda: True
    )
    with pytest.raises(ValueError, match="invalid wait_until"):
        await service.start(session_id, wait_until="invalid")

    await service.aclose()
    with pytest.raises(RuntimeError, match="browser service is closed"):
        await service.start(session_id)


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
    assert attempts == 2
    assert browser_id not in service._cleanup_pending
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
    dispatcher = build_executor_tool_dispatcher(
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
        return {"sessions": []}

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
async def test_browser_lookup_refreshes_idle_deadline_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser, _config, _store, session_id, _workspace = _service(tmp_path)
    monkeypatch.setattr(browser_ops, "_IDLE_TIMEOUT_S", 10)
    now = [100.0]
    monkeypatch.setattr(browser_ops.time, "time", lambda: now[0])

    class FakeBrowser:
        def is_connected(self) -> bool:
            return True

    browser_id = "browser_idle_refresh"
    state = browser_ops.BrowserSessionState(
        browser_session_id=browser_id,
        owner_session_id=session_id,
        playwright=object(),
        browser=FakeBrowser(),
        context=object(),
        created_at=0.0,
        last_used_at=95.0,
    )
    browser._sessions[browser_id] = state
    closed: list[str] = []

    async def record_close(closing_state) -> None:
        closed.append(closing_state.browser_session_id)

    monkeypatch.setattr(browser, "_close_state", record_close)

    assert await browser._get_owned(session_id, browser_id) is state
    assert state.last_used_at == 100.0

    now[0] = 105.0
    await browser._cleanup_idle()
    assert browser_id in browser._sessions
    assert closed == []

    now[0] = 111.0
    await browser._cleanup_idle()
    assert browser_id not in browser._sessions
    assert closed == [browser_id]
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
        default_workdir=workspace,
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
    <button id="css" onclick="document.querySelector('#result').textContent='css-click'">CSS</button>
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

            css_result = (
                await harness.control.session_coordinator.call_session_tool(
                    "browser_act",
                    {
                        "session_id": session_id,
                        "browser_session_id": browser_id,
                        "actions": [{"action": "click", "target": "#css"}],
                    },
                )
            )
            assert isinstance(css_result, dict)
            assert isinstance(css_result["results"], list)
            css_action_result = css_result["results"][0]
            assert isinstance(css_action_result, dict)
            assert css_action_result["target"] == "#css"
            css_after = (
                await harness.control.session_coordinator.call_session_tool(
                    "browser_snapshot",
                    {
                        "session_id": session_id,
                        "browser_session_id": browser_id,
                    },
                )
            )
            assert isinstance(css_after, dict)
            assert "css-click" in str(css_after["text"])

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
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(executor_workspace))
    clear_settings_cache()

    control = build_control_runtime(
        Settings(
            default_workdir=control_workspace,
            state_dir=tmp_path / "protocol-control-state",
            auth_mode="none",
            agent_bridge_enabled=False,
        )
    )
    executor = build_executor_runtime(
        resolve_executor_config(
            Settings(
                default_workdir=executor_workspace,
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


@pytest.mark.asyncio
async def test_browser_storage_state_round_trip_is_private_and_session_relative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_chromium(monkeypatch)
    service, _config, _store, session_id, workspace = _service(tmp_path)
    site = tmp_path / "storage-state-site"
    site.mkdir()
    (site / "index.html").write_text(
        """<!doctype html>
<html><head><title>Auth state</title></head><body>
<div id="state"></div>
<script>
if (new URLSearchParams(location.search).get('seed') === '1') {
  localStorage.setItem('auth-state', 'persisted-login');
  document.cookie = 'auth-cookie=persisted-cookie; path=/';
}
document.querySelector('#state').textContent =
  (localStorage.getItem('auth-state') || 'missing-login') + '|' +
  (document.cookie || 'missing-cookie');
</script>
</body></html>
""",
        encoding="utf-8",
    )

    with _serve_site(site) as base_url:
        first = await service.start(
            session_id,
            url=f"{base_url}/index.html?seed=1",
        )
        first_id = str(first["browser_session_id"])
        before = await service.snapshot(
            session_id, first_id, screenshot_path=None
        )
        assert "persisted-login" in str(before["text"])
        assert "persisted-cookie" in str(before["text"])

        closed = await service.close(
            session_id,
            first_id,
            save_storage_state_path="auth-state.json",
        )
        assert closed["storage_state_path"] == "auth-state.json"
        state_path = workspace / "auth-state.json"
        assert state_path.is_file()
        assert state_path.stat().st_mode & 0o777 == 0o600
        raw_state = state_path.read_text(encoding="utf-8")
        assert "persisted-login" in raw_state

        restored = await service.start(
            session_id,
            url=f"{base_url}/index.html",
            storage_state_path="auth-state.json",
        )
        restored_id = str(restored["browser_session_id"])
        after = await service.snapshot(
            session_id,
            restored_id,
            screenshot_path=None,
        )
        assert "persisted-login" in str(after["text"])
        assert "persisted-cookie" in str(after["text"])
        await service.close(session_id, restored_id)

    await service.aclose()


@pytest.mark.asyncio
async def test_browser_profile_persists_across_workgate_sessions_and_is_exclusive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_chromium(monkeypatch)
    service, config, store, first_session_id, workspace = _service(tmp_path)
    second_session_id = "sess_0000000000000000000002"
    store.create_session(session_id=second_session_id, workdir=workspace)
    site = tmp_path / "profile-site"
    site.mkdir()
    (site / "index.html").write_text(
        """<!doctype html>
<html><head><title>Profile state</title></head><body>
<div id="state"></div>
<script>
if (new URLSearchParams(location.search).get('seed') === '1') {
  localStorage.setItem('profile-auth', 'profile-login');
}
document.querySelector('#state').textContent =
  localStorage.getItem('profile-auth') || 'missing-profile';
</script>
</body></html>
""",
        encoding="utf-8",
    )

    with _serve_site(site) as base_url:
        first = await service.start(
            first_session_id,
            url=f"{base_url}/index.html?seed=1",
            profile_id="login-profile",
        )
        first_id = str(first["browser_session_id"])
        assert first["profile_id"] == "login-profile"
        profile_dir = config.state_dir / "browser-profiles" / "login-profile"
        assert profile_dir.is_dir()
        assert not (
            config.state_dir / "browser-profiles" / "LOGIN-PROFILE"
        ).exists()

        with pytest.raises(ValueError, match="already in use"):
            await service.start(
                second_session_id,
                url=f"{base_url}/index.html",
                profile_id="login-profile",
            )
        with pytest.raises(ValueError, match="already in use"):
            await service.start(
                second_session_id,
                url=f"{base_url}/index.html",
                profile_id="LOGIN-PROFILE",
            )

        closed = await service.close_owned(first_session_id)
        assert first_id in closed
        assert profile_dir.is_dir()

        restored = await service.start(
            second_session_id,
            url=f"{base_url}/index.html",
            profile_id="login-profile",
        )
        restored_id = str(restored["browser_session_id"])
        after = await service.snapshot(
            second_session_id,
            restored_id,
            screenshot_path=None,
        )
        assert "profile-login" in str(after["text"])
        await service.close(second_session_id, restored_id)

    await service.aclose()


@pytest.mark.asyncio
async def test_browser_persistence_validation_and_failed_export_still_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_chromium(monkeypatch)
    service, _config, _store, session_id, workspace = _service(tmp_path)

    for profile_id in (".", "..", "bad profile"):
        with pytest.raises(ValueError, match="profile_id"):
            await service.start(session_id, profile_id=profile_id)

    invalid_state_path = workspace / "invalid-state.json"
    invalid_state_path.write_text(
        '{"cookies":[{"name":"missing-required-cookie-fields"}],"origins":[]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid for Playwright") as raised:
        await service.start(
            session_id,
            storage_state_path="invalid-state.json",
        )
    assert "missing-required-cookie-fields" not in str(raised.value)

    state_path = workspace / "state.json"
    state_path.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be combined"):
        await service.start(
            session_id,
            profile_id="profile",
            storage_state_path="state.json",
        )
    with pytest.raises(ValueError, match="relative"):
        await service.start(
            session_id,
            storage_state_path=str(state_path),
        )
    outside_state = tmp_path / "outside-state.json"
    outside_state.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="stay within"):
        await service.start(
            session_id,
            storage_state_path="../outside-state.json",
        )
    with pytest.raises(ValueError, match="only valid for action=close"):
        await service.manage(
            session_id,
            action="start",
            save_storage_state_path="state.json",
        )
    with pytest.raises(ValueError, match="not valid for action=list"):
        await service.manage(
            session_id,
            action="list",
            storage_state_path="state.json",
        )

    started = await service.start(session_id)
    browser_id = str(started["browser_session_id"])
    with pytest.raises(
        Exception, match="missing-parent|No such file|not found"
    ):
        await service.close(
            session_id,
            browser_id,
            save_storage_state_path="missing-parent/state.json",
        )
    listed = await service.manage(session_id, action="list")
    assert listed["sessions"] == []

    await service.aclose()


@pytest.mark.asyncio
async def test_browser_storage_state_respects_file_byte_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_chromium(monkeypatch)
    service, config, _store, session_id, workspace = _service(tmp_path)

    oversized = workspace / "oversized-state.json"
    oversized.write_text(
        '{"cookies":[],"origins":[],"padding":"' + ("x" * 128) + '"}',
        encoding="utf-8",
    )
    service._config = replace(config, max_file_read_bytes=32)
    with pytest.raises(ValueError, match="file read limit"):
        await service.start(
            session_id,
            storage_state_path="oversized-state.json",
        )

    service._config = replace(config, max_file_write_bytes=1)
    started = await service.start(session_id)
    browser_id = str(started["browser_session_id"])
    with pytest.raises(ValueError, match="file write limit"):
        await service.close(
            session_id,
            browser_id,
            save_storage_state_path="too-large.json",
        )
    assert not (workspace / "too-large.json").exists()
    listed = await service.manage(session_id, action="list")
    assert listed["sessions"] == []

    await service.aclose()


@pytest.mark.asyncio
async def test_browser_profile_launch_failure_is_not_reported_as_missing_chromium(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _config, _store, session_id, _workspace = _service(tmp_path)
    monkeypatch.setattr(
        browser_ops, "browser_capability_available", lambda: True
    )

    class BrokenChromium:
        async def launch_persistent_context(self, **_kwargs):
            raise RuntimeError("backend detail that must stay hidden")

    class FakePlaywright:
        chromium = BrokenChromium()

        async def stop(self) -> None:
            return None

    class Starter:
        async def start(self):
            return FakePlaywright()

    monkeypatch.setattr(
        "playwright.async_api.async_playwright",
        lambda: Starter(),
    )

    with pytest.raises(
        browser_ops.ExecutorOperationFailure,
        match="browser profile 'login-profile' could not be opened",
    ) as raised:
        await service.start(session_id, profile_id="login-profile")

    assert raised.value.code == "browser_profile_unavailable"
    assert "backend detail" not in str(raised.value)
    assert service._profiles_in_use == set()
    await service.aclose()


@pytest.mark.asyncio
async def test_browser_profile_reservation_survives_cleanup_pending(
    tmp_path: Path,
) -> None:
    service, _config, _store, session_id, _workspace = _service(tmp_path)

    class FlakyContext:
        attempts = 0

        async def close(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("synthetic persistent-context close failure")

    class FakePlaywright:
        stops = 0

        async def stop(self) -> None:
            self.stops += 1

    class FakeBrowser:
        pass

    browser_id = "browser_profile_cleanup_pending"
    context = FlakyContext()
    playwright = FakePlaywright()
    state = browser_ops.BrowserSessionState(
        browser_session_id=browser_id,
        owner_session_id=session_id,
        playwright=playwright,
        browser=FakeBrowser(),
        context=context,
        profile_id="login-profile",
        created_at=0.0,
        last_used_at=0.0,
    )
    service._sessions[browser_id] = state
    service._profiles_in_use.add(service._profile_key("login-profile"))

    with pytest.raises(
        RuntimeError, match="synthetic persistent-context close failure"
    ):
        await service.close(session_id, browser_id)

    assert browser_id in service._cleanup_pending
    assert service._profile_key("login-profile") in service._profiles_in_use

    closed = await service.close(session_id, browser_id)
    assert closed == {"browser_session_id": browser_id, "closed": True}
    assert browser_id not in service._cleanup_pending
    assert service._profile_key("login-profile") not in service._profiles_in_use
    assert context.attempts == 2
    assert playwright.stops == 2

    await service.aclose()


@pytest.mark.asyncio
async def test_browser_css_selectors_and_snapshot_refs_use_same_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_chromium(monkeypatch)
    service, _config, _store, session_id, _workspace = _service(tmp_path)
    site = tmp_path / "css-target-site"
    site.mkdir()
    (site / "index.html").write_text(
        """<!doctype html><html><head><title>CSS targets</title></head><body>
<input id="name"><input id="typed"><input id="confirm" type="checkbox">
<select id="choice"><option value="a">A</option><option value="b">B</option></select>
<button id="apply" onclick="document.querySelector('#status').textContent='clicked'">Apply</button>
<button id="extra" title="arrow>>value">Extra</button><span id="status">not clicked</span>
</body></html>""",
        encoding="utf-8",
    )
    try:
        with _serve_site(site) as base_url:
            started = await service.start(
                session_id, url=f"{base_url}/index.html"
            )
            browser_id = str(started["browser_session_id"])
            snap = await service.snapshot(session_id, browser_id)
            button_ref = next(
                item["ref"]
                for item in snap["interactive_elements"]
                if item["text"] == "Apply"
            )
            actions = [
                {"action": "fill", "target": "#name", "value": "private-value"},
                {"action": "type", "target": "input#typed", "value": "typed"},
                {"action": "select", "target": "#choice", "value": "b"},
                {"action": "press", "target": "#name", "key": "End"},
                {"action": "check", "target": "#confirm"},
                {"action": "uncheck", "target": "#confirm"},
                {"action": "hover", "target": "button[title='arrow>>value']"},
                {"action": "click", "target": button_ref},
            ]
            response = await service.act(session_id, browser_id, actions)
            assert [item["target"] for item in response["results"]] == [
                item["target"] for item in actions
            ]
            assert "private-value" not in str(response)
            page = next(iter(service._sessions[browser_id].pages.values())).page
            assert await page.input_value("#name") == "private-value"
            assert await page.input_value("#typed") == "typed"
            assert await page.input_value("#choice") == "b"
            assert not await page.is_checked("#confirm")
            assert await page.text_content("#status") == "clicked"
            with pytest.raises(ValueError, match="stale or unknown"):
                await service.act(
                    session_id,
                    browser_id,
                    [{"action": "click", "target": "e99"}],
                )
            with pytest.raises(PlaywrightError, match="strict mode violation"):
                await service.act(
                    session_id,
                    browser_id,
                    [{"action": "click", "target": "button"}],
                    timeout_ms=500,
                )
            with pytest.raises(ValueError, match="valid CSS selector"):
                await service.act(
                    session_id,
                    browser_id,
                    [{"action": "click", "target": "#bad["}],
                    timeout_ms=500,
                )
            with pytest.raises(PlaywrightError):
                await service.act(
                    session_id,
                    browser_id,
                    [{"action": "click", "target": "#absent"}],
                    timeout_ms=50,
                )
            # Native CSS validation blocks Playwright-specific selector engines,
            # including chains that bypass a simple css= prefix.
            for non_css in (
                "text=Apply",
                "button >> nth=1",
                "body >> xpath=.//button[@id='extra']",
                "body >> text=Apply",
                "button:has-text('Apply')",
            ):
                with pytest.raises(ValueError, match="valid CSS selector"):
                    await service.act(
                        session_id,
                        browser_id,
                        [{"action": "click", "target": non_css}],
                        timeout_ms=500,
                    )
            await service.close(session_id, browser_id)
    finally:
        await service.aclose()
