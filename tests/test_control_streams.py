import asyncio
from typing import Any

import pytest

from workgate.control.streams import ControlStreamHub
from workgate.ui.http.live_state import UiTerminalConnectionRegistry


class _FakeSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []
        self.closed: list[tuple[int, str | None]] = []

    async def receive(self) -> dict[str, Any]:
        return await self.incoming.get()

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(bytes(data))

    async def send_text(self, data: str) -> None:
        self.sent_text.append(str(data))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed.append((code, reason))


class _BlockingSendSocket(_FakeSocket):
    def __init__(self) -> None:
        super().__init__()
        self.send_started = asyncio.Event()
        self.release_first_send = asyncio.Event()
        self._blocked_once = False

    async def send_bytes(self, data: bytes) -> None:
        if not self._blocked_once:
            self._blocked_once = True
            self.send_started.set()
            await self.release_first_send.wait()
        await super().send_bytes(data)


@pytest.mark.asyncio
async def test_stream_hub_pairs_one_use_browser_token_and_expected_executor():
    hub = ControlStreamHub(max_streams=2, idle_timeout_s=0)
    grant = await hub.create("exec-1")
    browser = _FakeSocket()
    executor = _FakeSocket()

    assert await hub.claim_browser(grant.stream_id, "wrong", browser) is False
    assert (
        await hub.claim_executor(grant.stream_id, "exec-wrong", executor)
        is False
    )
    assert (
        await hub.claim_browser(grant.stream_id, grant.browser_token, browser)
        is True
    )
    assert (
        await hub.claim_browser(
            grant.stream_id, grant.browser_token, _FakeSocket()
        )
        is False
    )
    assert await hub.claim_executor(grant.stream_id, "exec-1", executor) is True
    assert await hub.activate_executor(grant.stream_id) is True

    await browser.incoming.put({"type": "websocket.receive", "bytes": b"input"})
    await executor.incoming.put(
        {"type": "websocket.receive", "text": '{"type":"ready"}'}
    )
    for _ in range(10):
        if executor.sent_bytes and browser.sent_text:
            break
        await asyncio.sleep(0)
    assert executor.sent_bytes == [b"input"]
    assert browser.sent_text == ['{"type":"ready"}']

    await browser.incoming.put({"type": "websocket.disconnect"})
    await asyncio.wait_for(hub.wait_closed(grant.stream_id), timeout=0.5)
    assert hub.active_count() == 0


@pytest.mark.asyncio
async def test_stream_hub_bounds_pending_streams_and_expires_unused_grant():
    hub = ControlStreamHub(
        max_streams=1,
        idle_timeout_s=0,
        browser_token_ttl_s=0.02,
    )
    first = await hub.create("exec-1")
    with pytest.raises(RuntimeError, match="Too many pending"):
        await hub.create("exec-2")

    await asyncio.sleep(0.04)

    assert hub.expected_executor(first.stream_id) is None
    second = await hub.create("exec-2")
    assert second.stream_id != first.stream_id
    await hub.aclose()


@pytest.mark.asyncio
async def test_executor_claim_refreshes_full_browser_token_ttl() -> None:
    hub = ControlStreamHub(
        max_streams=1,
        idle_timeout_s=0,
        browser_token_ttl_s=0.04,
    )
    grant = await hub.create("exec-1")
    initial_expiry = grant.expires_at
    await asyncio.sleep(0.025)

    executor = _FakeSocket()
    assert await hub.claim_executor(grant.stream_id, "exec-1", executor)
    assert await hub.activate_executor(grant.stream_id)
    refreshed_expiry = await hub.browser_expires_at(grant.stream_id)
    assert refreshed_expiry is not None
    assert refreshed_expiry > initial_expiry

    await asyncio.sleep(0.025)
    browser = _FakeSocket()
    assert await hub.claim_browser(
        grant.stream_id, grant.browser_token, browser
    )
    await browser.incoming.put({"type": "websocket.disconnect"})
    await asyncio.wait_for(hub.wait_closed(grant.stream_id), timeout=0.5)


@pytest.mark.asyncio
async def test_stream_hub_closes_executor_streams_on_trust_invalidation():
    hub = ControlStreamHub(max_streams=2, idle_timeout_s=0)
    grant = await hub.create("exec-1")
    browser = _FakeSocket()
    executor = _FakeSocket()
    assert await hub.claim_browser(
        grant.stream_id, grant.browser_token, browser
    )
    assert await hub.claim_executor(grant.stream_id, "exec-1", executor)
    assert await hub.activate_executor(grant.stream_id)

    await hub.close_executor("exec-1")

    assert hub.active_count() == 0
    assert browser.closed[-1][0] == 4403
    assert executor.closed[-1][0] == 4403


@pytest.mark.asyncio
async def test_stream_hub_idle_timeout_tracks_activity_across_both_directions():
    hub = ControlStreamHub(max_streams=1, idle_timeout_s=0.04)
    grant = await hub.create("exec-1")
    browser = _FakeSocket()
    executor = _FakeSocket()
    assert await hub.claim_browser(
        grant.stream_id, grant.browser_token, browser
    )
    assert await hub.claim_executor(grant.stream_id, "exec-1", executor)
    assert await hub.activate_executor(grant.stream_id)

    for payload in (b"one", b"two", b"three"):
        await executor.incoming.put(
            {"type": "websocket.receive", "bytes": payload}
        )
        await asyncio.sleep(0.025)
        assert hub.active_count() == 1

    assert browser.sent_bytes == [b"one", b"two", b"three"]
    await asyncio.sleep(0.06)
    assert hub.active_count() == 0


@pytest.mark.asyncio
async def test_stream_hub_backpressure_does_not_buffer_extra_source_frames() -> (
    None
):
    hub = ControlStreamHub(max_streams=1, idle_timeout_s=0)
    grant = await hub.create("exec-1")
    browser = _BlockingSendSocket()
    executor = _FakeSocket()
    assert await hub.claim_browser(
        grant.stream_id, grant.browser_token, browser
    )
    assert await hub.claim_executor(grant.stream_id, "exec-1", executor)
    assert await hub.activate_executor(grant.stream_id)

    await executor.incoming.put(
        {"type": "websocket.receive", "bytes": b"first"}
    )
    await executor.incoming.put(
        {"type": "websocket.receive", "bytes": b"second"}
    )
    await asyncio.wait_for(browser.send_started.wait(), timeout=0.5)

    assert executor.incoming.qsize() == 1
    assert browser.sent_bytes == []

    browser.release_first_send.set()
    for _ in range(20):
        if browser.sent_bytes == [b"first", b"second"]:
            break
        await asyncio.sleep(0)
    assert browser.sent_bytes == [b"first", b"second"]

    await executor.incoming.put({"type": "websocket.disconnect"})
    await asyncio.wait_for(hub.wait_closed(grant.stream_id), timeout=0.5)


@pytest.mark.asyncio
async def test_stream_hub_shares_human_ui_terminal_admission() -> None:
    registry = UiTerminalConnectionRegistry()
    await registry.start()
    snapshot_marker = registry.reserve(1)
    assert snapshot_marker is not None
    hub = ControlStreamHub(
        max_streams=1,
        idle_timeout_s=0,
        reserve_slot=lambda: registry.reserve(1),
        release_slot=registry.release,
    )

    with pytest.raises(
        RuntimeError, match="Too many Human UI terminal connections"
    ):
        await hub.create("exec-1")

    registry.release(snapshot_marker)
    grant = await hub.create("exec-1")
    assert registry.active_count() == 1
    await hub.cancel(grant.stream_id)
    assert registry.active_count() == 0
    await registry.aclose()
