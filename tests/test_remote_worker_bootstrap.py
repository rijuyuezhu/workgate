import asyncio
import builtins
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from email.message import Message
from io import BytesIO
from pathlib import Path

import pytest


def _managed_runtime_report() -> dict[str, object]:
    from workgate.remote.bundle import worker_bundle_manifest
    from workgate.remote.constants import REMOTE_WORKER_RUNTIME_PROTOCOL_VERSION

    manifest = worker_bundle_manifest()
    return {
        "protocol_version": REMOTE_WORKER_RUNTIME_PROTOCOL_VERSION,
        "runtime_kind": "managed_bundle",
        "worker_version": str(manifest["bundle_version"]),
        "bundle_version": str(manifest["bundle_version"]),
        "bundle_sha256": str(manifest["sha256"]),
    }


def _managed_poll_report(**extra: object) -> dict[str, object]:
    report = _managed_runtime_report()
    report["protocol_version"] = 2
    report.update(extra)
    return report


@pytest.mark.parametrize(
    "info",
    [
        {"profile_id": [], "launcher_path": "/state/run"},
        {"profile_id": "p_abcdefgh", "launcher_path": {}},
    ],
)
def test_reconnect_metadata_ignores_non_string_fields(
    info: dict[str, object],
) -> None:
    from workgate.remote.manager import _worker_reconnect_metadata

    assert _worker_reconnect_metadata(info) == (None, None)


def test_remote_worker_entrypoint_import_is_dependency_light():
    script = """
import asyncio
import builtins
import json

blocked = {"fastapi", "httpx", "mcp", "starlette", "uvicorn", "pydantic", "pydantic_settings", "yaml", "pathspec"}
real_import = builtins.__import__


def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".")[0] in blocked:
        raise AssertionError(f"unexpected bootstrap import: {name}")
    return real_import(name, globals, locals, fromlist, level)


builtins.__import__ = guarded_import
import workgate.remote_worker
from workgate.remote_worker.dispatch import build_worker_dispatcher
from workgate.remote_worker.worker import worker_capabilities, worker_info

assert "shell" in worker_capabilities()
assert "search" in build_worker_dispatcher().handlers
assert worker_info(".")["workdir"] == "."
assert worker_info(".")["workgate_version"]
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=".",
        env={**os.environ, "PYTHONPATH": "src"},
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr


def test_trimmed_worker_cli_exposes_new_subcommands(tmp_path):
    source_root = Path(__file__).resolve().parents[1] / "src"
    completed = subprocess.run(
        [sys.executable, "-m", "workgate.remote_worker", "--help"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(source_root),
            "PYTHONNOUSERSITE": "1",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    for command in (
        "enroll",
        "connect",
        "run",
        "install-service",
        "uninstall-service",
        "start",
        "stop",
        "restart",
        "status",
        "logs",
        "update",
    ):
        assert command in completed.stdout
    assert "--persist" not in completed.stdout


def test_execute_worker_tool_imports_registry_lazily(monkeypatch):
    import workgate.remote_worker.worker as worker

    real_import = builtins.__import__
    seen_mcp_import = False

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        nonlocal seen_mcp_import
        if name.split(".")[0] == "mcp":
            seen_mcp_import = True
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    assert "shell" in worker.worker_capabilities()
    assert seen_mcp_import is False


@pytest.mark.asyncio
async def test_worker_dispatches_persistent_shell_resize(monkeypatch):
    from workgate.ops import shell as shell_ops
    from workgate.remote_worker.dispatch import execute_worker_tool

    calls = []

    async def fake_resize(shell_id: str, cols: int, rows: int):
        calls.append((shell_id, cols, rows))
        return {
            "shell_id": shell_id,
            "cols": cols,
            "rows": rows,
            "resized": True,
            "backend": "tmux",
        }

    monkeypatch.setattr(
        shell_ops, "resize_persistent_shell_execute", fake_resize
    )

    async def owned_shells(session_id: str):
        assert session_id == "sess_test"
        return ["shell-1"]

    monkeypatch.setattr(
        shell_ops, "list_owned_persistent_shell_ids_execute", owned_shells
    )

    result = await execute_worker_tool(
        "resize_persistent_shell",
        {
            "session_id": "sess_test",
            "shell_id": "shell-1",
            "cols": 132,
            "rows": 38,
        },
    )

    assert result["resized"] is True
    assert calls == [("shell-1", 132, 38)]


@pytest.mark.asyncio
async def test_worker_dispatches_persistent_shell_read_with_ansi(monkeypatch):
    from workgate.ops import shell as shell_ops
    from workgate.remote_worker.dispatch import execute_worker_tool

    calls = []

    async def fake_read(
        shell_id: str,
        lines: int = 200,
        *,
        preserve_ansi: bool = False,
    ):
        calls.append((shell_id, lines, preserve_ansi))
        return {
            "shell_id": shell_id,
            "output": "\x1b[32mready\x1b[0m",
        }

    monkeypatch.setattr(
        shell_ops, "read_persistent_shell_output_execute", fake_read
    )

    async def owned_shells(session_id: str):
        assert session_id == "sess_test"
        return ["shell-1"]

    monkeypatch.setattr(
        shell_ops, "list_owned_persistent_shell_ids_execute", owned_shells
    )

    result = await execute_worker_tool(
        "read_persistent_shell_output",
        {
            "session_id": "sess_test",
            "shell_id": "shell-1",
            "lines": 500,
            "preserve_ansi": True,
        },
    )

    assert result == {
        "shell_id": "shell-1",
        "output": "\x1b[32mready\x1b[0m",
    }
    assert calls == [("shell-1", 500, True)]

    with pytest.raises(ValueError, match="preserve_ansi must be a boolean"):
        await execute_worker_tool(
            "read_persistent_shell_output",
            {
                "session_id": "sess_test",
                "shell_id": "shell-1",
                "preserve_ansi": "true",
            },
        )


@pytest.mark.asyncio
async def test_worker_dispatches_persistent_shell_start(monkeypatch):
    from workgate.ops import shell as shell_ops
    from workgate.remote_worker.dispatch import execute_worker_tool

    calls = []

    async def fake_start(cwd: str, name: str | None, command: str | None):
        calls.append((cwd, name, command))
        return {
            "shell_id": "edge-shell",
            "name": name,
            "cwd": cwd,
            "command": command,
        }

    monkeypatch.setattr(shell_ops, "start_persistent_shell_execute", fake_start)

    result = await execute_worker_tool(
        "start_persistent_shell",
        {"cwd": "/edge", "name": "edge", "command": "bash"},
    )

    assert result["shell_id"] == "edge-shell"
    assert calls == [("/edge", "edge", "bash")]


@pytest.mark.asyncio
async def test_worker_dispatches_terminal_bridge_lifecycle(monkeypatch):
    import workgate.terminal.bridge as bridge_ops
    from workgate.remote_worker.dispatch import execute_worker_tool

    calls = []
    bridge_id = "bridge_capability_1234567890"

    async def fake_open(shell_id, cols, rows):
        calls.append(("open", shell_id, cols, rows))
        return {
            "bridge_id": bridge_id,
            "shell_id": shell_id,
            "cols": cols,
            "rows": rows,
            "backend": "tmux-pty",
        }

    async def fake_read(value, max_bytes, wait_ms):
        calls.append(("read", value, max_bytes, wait_ms))
        return {"bridge_id": value, "data_b64": "", "bytes": 0, "eof": False}

    async def fake_write(value, data_b64):
        calls.append(("write", value, data_b64))
        return {"bridge_id": value, "written_bytes": 3}

    async def fake_resize(value, cols, rows):
        calls.append(("resize", value, cols, rows))
        return {
            "bridge_id": value,
            "cols": cols,
            "rows": rows,
            "resized": True,
            "backend": "tmux-pty",
        }

    async def fake_close(value):
        calls.append(("close", value))
        return {"bridge_id": value, "closed": True}

    monkeypatch.setattr(bridge_ops, "open_terminal_bridge_execute", fake_open)
    monkeypatch.setattr(bridge_ops, "read_terminal_bridge_execute", fake_read)
    monkeypatch.setattr(bridge_ops, "write_terminal_bridge_execute", fake_write)
    monkeypatch.setattr(
        bridge_ops, "resize_terminal_bridge_execute", fake_resize
    )
    monkeypatch.setattr(bridge_ops, "close_terminal_bridge_execute", fake_close)

    opened = await execute_worker_tool(
        "open_terminal_bridge",
        {"shell_id": "edge", "cols": 100, "rows": 30},
    )
    read = await execute_worker_tool(
        "read_terminal_bridge",
        {"bridge_id": bridge_id, "max_bytes": 2048, "wait_ms": 75},
    )
    written = await execute_worker_tool(
        "write_terminal_bridge",
        {"bridge_id": bridge_id, "data_b64": "YWJj"},
    )
    resized = await execute_worker_tool(
        "resize_terminal_bridge",
        {"bridge_id": bridge_id, "cols": 120, "rows": 36},
    )
    closed = await execute_worker_tool(
        "close_terminal_bridge",
        {"bridge_id": bridge_id},
    )

    assert opened["backend"] == "tmux-pty"
    assert read["eof"] is False
    assert written["written_bytes"] == 3
    assert resized["resized"] is True
    assert closed["closed"] is True
    assert calls == [
        ("open", "edge", 100, 30),
        ("read", bridge_id, 2048, 75),
        ("write", bridge_id, "YWJj"),
        ("resize", bridge_id, 120, 36),
        ("close", bridge_id),
    ]


def test_worker_session_start_result_serializes_with_real_dependencies(
    tmp_path,
):
    script = """
import asyncio
import json
import os

from workgate.config.settings import clear_settings_cache
from workgate.remote_worker import worker


async def main():
    workdir = os.environ["REMOTE_WORKDIR"]
    worker._configure_worker_runtime_env(workdir)
    clear_settings_cache()
    result = await worker.execute_worker_tool(
        "session_start",
        {"workdir": workdir, "target": "local", "machine": None, "label": None},
    )
    data = worker.to_jsonable(result)
    assert data["target"] == "local"
    assert data["workdir"] == workdir
    assert "model_fields" not in data
    assert data["git"]["is_repo"] is False
    assert "model_fields" not in data["git"]
    print(json.dumps(data, sort_keys=True))


asyncio.run(main())
"""
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": "src",
            "REMOTE_WORKDIR": str(tmp_path),
            "WORKGATE_WORKSPACE_ROOT": "/workspace",
            "WORKGATE_STATE_DIR": "/workspace/.workgate",
            "WORKGATE_WORKER_STATE_DIR": str(tmp_path / ".worker-state"),
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=".",
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert '"session_id"' in result.stdout


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return self.body


def test_worker_post_json_posts_json_and_returns_object(monkeypatch):
    import workgate.remote_worker.worker as worker

    monkeypatch.setattr(worker.shutil, "which", lambda name: None)
    captured: dict[str, object] = {}

    def fake_urlopen(
        request: urllib.request.Request, timeout: float | None = None
    ) -> _FakeResponse:
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["content_type"] = request.get_header("Content-type")
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return _FakeResponse(b'{"ok": true, "data": {"value": 1}}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = worker._worker_post_json(
        "https://example.test/remote/register",
        {"invite": "abc"},
        headers={"Authorization": "Bearer token"},
        timeout=30,
    )

    assert result == {"ok": True, "data": {"value": 1}}
    assert captured == {
        "url": "https://example.test/remote/register",
        "data": b'{"invite": "abc"}',
        "content_type": "application/json",
        "authorization": "Bearer token",
        "timeout": 30,
    }


def test_worker_post_json_rejects_non_object_response(monkeypatch):
    import workgate.remote_worker.worker as worker

    monkeypatch.setattr(worker.shutil, "which", lambda name: None)

    def fake_urlopen(
        request: urllib.request.Request, timeout: float | None = None
    ) -> _FakeResponse:
        return _FakeResponse(b'["not", "an", "object"]')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(
        RuntimeError, match="returned JSON list, expected object"
    ):
        worker._worker_post_json("https://example.test/remote/poll", {})


def test_worker_post_json_includes_http_error_detail(monkeypatch):
    import workgate.remote_worker.worker as worker

    monkeypatch.setattr(worker.shutil, "which", lambda name: None)

    def fake_urlopen(
        request: urllib.request.Request, timeout: float | None = None
    ) -> _FakeResponse:
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            hdrs=Message(),
            fp=BytesIO(b'{"message": "bad invite"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as exc_info:
        worker._worker_post_json("https://example.test/remote/register", {})

    assert "failed with 400" in str(exc_info.value)
    assert "bad invite" in str(exc_info.value)


def test_worker_post_json_wraps_url_errors(monkeypatch):
    import workgate.remote_worker.worker as worker

    monkeypatch.setattr(worker.shutil, "which", lambda name: None)

    def fake_urlopen(
        request: urllib.request.Request, timeout: float | None = None
    ) -> _FakeResponse:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(
        RuntimeError, match="worker HTTP request failed: connection refused"
    ):
        worker._worker_post_json("https://example.test/remote/poll", {})


def test_worker_post_json_uses_curl_when_available(monkeypatch):
    import workgate.remote_worker.worker as worker

    captured: dict[str, object] = {}

    def fake_run(command, *, input, capture_output, check):
        captured["command"] = command
        captured["input"] = input
        captured["capture_output"] = capture_output
        captured["check"] = check
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=b'{"ok": true}\nWORKGATE_HTTP_STATUS:200',
            stderr=b"",
        )

    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    result = worker._worker_post_json(
        "https://example.test/remote/poll",
        {},
        headers={"X-Test": "value"},
        timeout=12,
    )

    assert result == {"ok": True}
    assert captured["input"] == b"{}"
    command = captured["command"]
    assert isinstance(command, list)
    assert command[:5] == [
        "/usr/bin/curl",
        "--connect-timeout",
        "10",
        "--max-time",
        "12",
    ]
    assert "X-Test: value" in command


def test_worker_retry_delay_is_capped():
    import workgate.remote_worker.worker as worker

    assert [worker._worker_retry_delay(i) for i in range(7)] == [
        1.0,
        2.0,
        4.0,
        8.0,
        16.0,
        30.0,
        30.0,
    ]


@pytest.mark.asyncio
async def test_worker_post_json_forever_retries_until_success(
    monkeypatch, capsys
):
    import workgate.remote_worker.worker as worker

    calls = []
    sleeps = []

    def fake_post(url, payload, headers=None, timeout=None):
        calls.append((url, payload, headers, timeout))
        if len(calls) < 3:
            raise RuntimeError(f"temporary failure {len(calls)}")
        return {"ok": True, "data": {"heartbeat": True}}

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(worker, "_worker_post_json", fake_post)
    monkeypatch.setattr(worker.asyncio, "sleep", fake_sleep)

    result = await worker._worker_post_json_forever(
        "https://example.test/remote/poll",
        {},
        {"X-Test": "value"},
        12,
        "poll",
    )

    assert result == {"ok": True, "data": {"heartbeat": True}}
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]
    assert (
        "Status: poll failed: temporary failure 1. Retrying in 1s..."
        in capsys.readouterr().err
    )


@pytest.mark.asyncio
async def test_worker_result_submission_keeps_heartbeats_while_retrying(
    monkeypatch,
):
    import workgate.remote_worker.worker as worker

    result_attempts = 0
    heartbeat_calls = []
    result = {"job_id": "job-1", "ok": True, "data": {"done": True}}
    headers = {"Authorization": "Bearer token"}

    def fake_post(url, payload, request_headers=None, timeout=None):
        nonlocal result_attempts
        assert request_headers == headers
        assert timeout == 30
        if url.endswith("/result"):
            assert payload == result
            result_attempts += 1
            if result_attempts < 3:
                raise RuntimeError(
                    f"temporary result failure {result_attempts}"
                )
            return {"ok": True, "data": {"accepted": True}}
        assert url.endswith("/heartbeat")
        assert payload == {}
        heartbeat_calls.append(url)
        return {"ok": True, "data": {"accepted": True}}

    monkeypatch.setattr(worker, "_worker_post_json", fake_post)
    monkeypatch.setattr(worker, "_WORKER_RETRY_INITIAL_DELAY_S", 0.02)
    monkeypatch.setattr(worker, "_WORKER_RETRY_MAX_DELAY_S", 0.02)

    response = await worker._submit_worker_result_with_heartbeat(
        result,
        "https://example.test",
        headers,
        0.005,
    )

    assert response == {"ok": True, "data": {"accepted": True}}
    assert result_attempts == 3
    assert heartbeat_calls
    heartbeat_count = len(heartbeat_calls)
    await asyncio.sleep(0.02)
    assert len(heartbeat_calls) == heartbeat_count


def test_worker_runtime_env_explicitly_binds_remote_workspace_and_state(
    tmp_path, monkeypatch
):
    import workgate.remote_worker.worker as worker

    workdir = tmp_path / "remote-workdir"
    worker_state = tmp_path / "worker-state"
    monkeypatch.setenv(
        "WORKGATE_WORKSPACE_ROOT", str(tmp_path / "inherited-workspace")
    )
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / "inherited-state"))
    monkeypatch.setenv("WORKGATE_ALLOW_FULL_CONTROL", "false")
    monkeypatch.setenv("WORKGATE_WORKER_STATE_DIR", str(worker_state))

    worker._configure_worker_runtime_env(str(workdir))

    assert os.environ["WORKGATE_WORKSPACE_ROOT"] == str(workdir)
    assert os.environ["WORKGATE_STATE_DIR"] == str(worker_state / "runtime")
    assert os.environ["WORKGATE_ALLOW_FULL_CONTROL"] == "true"


def test_worker_runtime_env_profile_state_is_authoritative(
    tmp_path, monkeypatch
):
    import workgate.remote_worker.worker as worker

    worker_state = tmp_path / "worker-state"
    profile_id = "p_abcdefgh"
    workdir = tmp_path / "remote-workdir"
    monkeypatch.setenv(
        "WORKGATE_WORKSPACE_ROOT", str(tmp_path / "custom-workspace")
    )
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / "custom-state"))
    monkeypatch.setenv("WORKGATE_ALLOW_FULL_CONTROL", "false")
    monkeypatch.setenv("WORKGATE_WORKER_STATE_DIR", str(worker_state))

    worker._configure_worker_runtime_env(str(workdir), profile_id)

    assert os.environ["WORKGATE_WORKSPACE_ROOT"] == str(workdir)
    assert os.environ["WORKGATE_STATE_DIR"] == str(
        worker_state / "profiles" / profile_id / "state"
    )
    assert os.environ["WORKGATE_ALLOW_FULL_CONTROL"] == "true"


def test_worker_identity_round_trips_and_filters_by_server_name(
    tmp_path, monkeypatch
):
    import workgate.remote_worker.worker as worker

    monkeypatch.setenv("WORKGATE_WORKER_STATE_DIR", str(tmp_path))

    worker._write_worker_identity(
        {
            "server": "https://example.test",
            "name": "machine-a",
            "access": "worker-access",
        }
    )

    assert worker._read_worker_identity(
        "https://example.test", "machine-a"
    ) == {
        "server": "https://example.test",
        "name": "machine-a",
        "access": "worker-access",
    }
    assert (
        worker._read_worker_identity("https://other.test", "machine-a") is None
    )
    assert (
        worker._read_worker_identity("https://example.test", "machine-b")
        is None
    )


def test_worker_cli_keyboard_interrupt_exits_cleanly():
    code = """
from workgate.remote_worker import cli


def fake_asyncio_run(coro):
    coro.close()
    raise KeyboardInterrupt


cli.asyncio.run = fake_asyncio_run
cli.run_worker_cli(
    [
        "connect",
        "--server",
        "https://example.test",
        "--invite",
        "workgate_inv_test",
    ]
)
"""

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=".",
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 130
    assert "Status: disconnected by user." in completed.stderr
    assert "Traceback" not in completed.stderr


@pytest.mark.asyncio
async def test_remote_manager_persists_workers_and_resumes(
    tmp_path, monkeypatch
):
    from workgate.config.settings import clear_settings_cache
    from workgate.remote.manager import RemoteManager

    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()

    manager = RemoteManager()
    invite = await manager.create_invite(name="worker-a")
    registered = await manager.register_worker(
        {
            "invite": invite.code,
            "workdir": str(tmp_path),
            "capabilities": ["shell"],
            "info": {"hostname": "remote-host"},
            "runtime": _managed_runtime_report(),
        }
    )
    assert registered["poll_timeout_s"] == 25

    reloaded = RemoteManager()
    inventory = reloaded.list_machines()
    assert inventory.counts == {"online": 0, "offline": 1, "total": 1}
    assert inventory.machines[0].name == "worker-a"
    assert inventory.machines[0].queue_depth == 0

    resumed = await reloaded.resume_worker(
        registered["token"],
        {
            "name": "worker-a",
            "workdir": str(tmp_path / "remote"),
            "runtime": _managed_runtime_report(),
        },
    )
    assert resumed["name"] == "worker-a"
    assert resumed["token"] == registered["token"]
    assert resumed["poll_timeout_s"] == 25
    assert reloaded.list_machines().counts == {
        "online": 1,
        "offline": 0,
        "total": 1,
    }


@pytest.mark.asyncio
async def test_remote_manager_resume_uses_token_name_after_rename(
    tmp_path, monkeypatch
):
    from workgate.config.settings import clear_settings_cache
    from workgate.remote.manager import RemoteManager

    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()

    manager = RemoteManager()
    invite = await manager.create_invite(name="worker-old")
    registered = await manager.register_worker(
        {
            "invite": invite.code,
            "workdir": str(tmp_path),
            "capabilities": ["shell"],
            "info": {"hostname": "remote-host"},
            "runtime": _managed_runtime_report(),
        }
    )
    manager.rename("worker-old", "worker-renamed")

    resumed = await manager.resume_worker(
        registered["token"],
        {
            "name": "worker-old",
            "workdir": str(tmp_path / "remote"),
            "runtime": _managed_runtime_report(),
        },
    )

    assert resumed["name"] == "worker-renamed"
    inventory = manager.list_machines()
    assert [machine.name for machine in inventory.machines] == [
        "worker-renamed"
    ]
    assert inventory.counts == {"online": 1, "offline": 0, "total": 1}


@pytest.mark.asyncio
async def test_remote_manager_list_machines_reports_counts_and_details(
    tmp_path, monkeypatch
):
    from workgate.config.settings import clear_settings_cache
    from workgate.remote.manager import RemoteManager, RemoteWorker

    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()

    manager = RemoteManager()
    manager._load_registry_unlocked()
    now = 1_000_000.0
    monkeypatch.setattr("workgate.remote.manager._utc", lambda: now)

    recent = RemoteWorker(
        name="recent-worker", token="recent", last_seen=now - 5
    )
    stale = RemoteWorker(
        name="stale-worker",
        token="stale",
        last_seen=now - 500,
        info={"profile_id": [], "launcher_path": {}},
    )
    manager.workers = {recent.name: recent, stale.name: stale}
    manager.tokens = {recent.token: recent.name, stale.token: stale.name}
    recent.queue.put_nowait({"id": "job-1"})

    result = manager.list_machines()

    assert result.counts == {"online": 1, "offline": 1, "total": 2}
    assert [machine.name for machine in result.machines] == [
        "recent-worker",
        "stale-worker",
    ]
    assert result.machines[0].last_seen_age_s == 5
    assert result.machines[0].queue_depth == 1
    assert result.machines[0].offline_after_s == 60
    assert result.machines[1].profile_id is None
    assert result.machines[1].reconnect_command is None


def _configure_remote_state(tmp_path, monkeypatch, **overrides):
    from workgate.config.settings import clear_settings_cache

    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    for name, value in overrides.items():
        monkeypatch.setenv(f"WORKGATE_{name.upper()}", str(value))
    clear_settings_cache()


def test_worker_post_rejects_non_http_server_url(monkeypatch):
    import workgate.remote_worker.worker as worker

    monkeypatch.setattr(worker.shutil, "which", lambda _name: None)

    with pytest.raises(ValueError, match="absolute HTTP"):
        worker._worker_post_json("file:///tmp/control", {})


@pytest.mark.asyncio
async def test_worker_post_forever_stops_on_permanent_http_error(monkeypatch):
    import workgate.remote_worker.worker as worker

    calls = 0

    def reject(url, payload, headers=None, timeout=None):
        nonlocal calls
        calls += 1
        raise worker.WorkerHttpError(url, 400, "invalid registration")

    async def unexpected_sleep(_delay):
        raise AssertionError("permanent HTTP errors must not be retried")

    monkeypatch.setattr(worker, "_worker_post_json", reject)
    monkeypatch.setattr(worker.asyncio, "sleep", unexpected_sleep)

    with pytest.raises(worker.WorkerHttpError) as error:
        await worker._worker_post_json_forever(
            "https://example.test/remote/register", {}, operation="register"
        )

    assert error.value.status_code == 400
    assert calls == 1


@pytest.mark.asyncio
async def test_worker_post_forever_retries_transient_http_error(monkeypatch):
    import workgate.remote_worker.worker as worker

    calls = 0
    sleeps = []

    def post(url, payload, headers=None, timeout=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise worker.WorkerHttpError(url, 503, "temporarily unavailable")
        return {"ok": True}

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(worker, "_worker_post_json", post)
    monkeypatch.setattr(worker.asyncio, "sleep", record_sleep)

    result = await worker._worker_post_json_forever(
        "https://example.test/remote/poll", {}, operation="poll"
    )

    assert result == {"ok": True}
    assert calls == 2
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_long_worker_job_sends_heartbeats(monkeypatch):
    import workgate.remote_worker.worker as worker

    heartbeat_urls = []

    async def slow_tool(tool, args):
        assert tool == "bash"
        assert args == {"command": "sleep"}
        await asyncio.sleep(0.04)
        return {"done": True}

    def post(url, payload, headers=None, timeout=None):
        heartbeat_urls.append(url)
        return {"ok": True}

    monkeypatch.setattr(worker, "execute_worker_tool", slow_tool)
    monkeypatch.setattr(worker, "_worker_post_json", post)

    result = await worker._execute_worker_job_with_heartbeat(
        {"tool": "bash", "args": {"command": "sleep"}},
        "https://example.test",
        {"Authorization": "Bearer token"},
        0.01,
    )

    assert result == {"done": True}
    assert heartbeat_urls
    assert set(heartbeat_urls) == {"https://example.test/remote/heartbeat"}


@pytest.mark.asyncio
async def test_remote_registry_recovers_from_backup(tmp_path, monkeypatch):
    from workgate.remote.manager import RemoteManager

    _configure_remote_state(tmp_path, monkeypatch)
    manager = RemoteManager()
    invite = await manager.create_invite(name="backup-worker")
    registered = await manager.register_worker(
        {
            "invite": invite.code,
            "workdir": str(tmp_path),
            "runtime": _managed_runtime_report(),
        }
    )
    manager._registry_path().write_text("{broken", encoding="utf-8")

    recovered = RemoteManager()
    inventory = recovered.list_machines()

    assert inventory.counts == {"online": 0, "offline": 1, "total": 1}
    assert inventory.machines[0].name == "backup-worker"
    assert (
        json.loads(recovered._registry_path().read_text(encoding="utf-8"))[
            "workers"
        ][0]["access"]
        == registered["token"]
    )


def test_remote_registry_refuses_silent_reset_when_both_copies_are_corrupt(
    tmp_path, monkeypatch
):
    from workgate.remote.manager import RemoteManager

    _configure_remote_state(tmp_path, monkeypatch)
    manager = RemoteManager()
    manager._registry_path().parent.mkdir(parents=True, exist_ok=True)
    manager._registry_path().write_text("{broken", encoding="utf-8")
    manager._registry_backup_path().write_text("[]", encoding="utf-8")

    with pytest.raises(RuntimeError, match="refusing to reset"):
        manager.list_machines()


@pytest.mark.asyncio
async def test_remote_queue_limit_counts_inflight_jobs(tmp_path, monkeypatch):
    from workgate.remote.manager import RemoteManager, RemoteWorker, _utc

    _configure_remote_state(tmp_path, monkeypatch, remote_max_pending_jobs=1)
    manager = RemoteManager()
    manager._load_registry_unlocked()
    worker = RemoteWorker(name="worker", token="token", last_seen=_utc())
    manager.workers[worker.name] = worker
    manager.tokens[worker.token] = worker.name

    first = asyncio.create_task(
        manager.call("worker", "read", {"path": "a"}, timeout_s=10)
    )
    while worker.queue.qsize() == 0:
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="queue is full"):
        await manager.call("worker", "read", {"path": "b"}, timeout_s=10)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert manager.pending == {}
    assert manager.pending_machines == {}

    second = asyncio.create_task(
        manager.call("worker", "read", {"path": "c"}, timeout_s=10)
    )
    while not manager.pending:
        await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second


@pytest.mark.asyncio
async def test_remote_poll_skips_cancelled_job_and_delivers_next(
    tmp_path, monkeypatch
):
    from workgate.remote.manager import RemoteManager, RemoteWorker, _utc

    _configure_remote_state(tmp_path, monkeypatch)
    manager = RemoteManager()
    manager._load_registry_unlocked()
    worker = RemoteWorker(name="worker", token="token", last_seen=_utc())
    manager.workers[worker.name] = worker
    manager.tokens[worker.token] = worker.name
    manager.cancelled_jobs["cancelled"] = _utc()
    worker.queue.put_nowait({"id": "cancelled", "tool": "read", "args": {}})
    worker.queue.put_nowait({"id": "next", "tool": "read", "args": {}})

    result = await manager.poll("token", _managed_poll_report())

    assert result["job"]["id"] == "next"
    assert "cancelled" not in manager.cancelled_jobs


@pytest.mark.asyncio
async def test_remote_result_is_rejected_from_wrong_worker(
    tmp_path, monkeypatch
):
    from workgate.remote.manager import RemoteManager, RemoteWorker, _utc

    _configure_remote_state(tmp_path, monkeypatch)
    manager = RemoteManager()
    manager._load_registry_unlocked()
    worker_a = RemoteWorker(name="a", token="token-a", last_seen=_utc())
    worker_b = RemoteWorker(name="b", token="token-b", last_seen=_utc())
    manager.workers = {"a": worker_a, "b": worker_b}
    manager.tokens = {"token-a": "a", "token-b": "b"}
    future = asyncio.get_running_loop().create_future()
    manager.pending["job"] = future
    manager.pending_machines["job"] = "a"

    with pytest.raises(PermissionError, match="another worker"):
        await manager.submit_result(
            "token-b", {"job_id": "job", "ok": True, "data": {}}
        )

    assert not future.done()
    future.cancel()


def test_remote_machine_names_are_portably_validated(tmp_path, monkeypatch):
    from workgate.remote.manager import RemoteManager

    _configure_remote_state(tmp_path, monkeypatch)
    manager = RemoteManager()

    with pytest.raises(ValueError, match="unsupported characters"):
        manager.rename("missing", "bad/name")
    with pytest.raises(ValueError, match="128 characters"):
        manager.rename("missing", "x" * 129)


@pytest.mark.asyncio
async def test_worker_job_heartbeat_stops_when_job_finishes_during_sleep(
    monkeypatch,
):
    import workgate.remote_worker.worker as worker

    class FinishingTask:
        def __init__(self):
            self.checks = 0

        def done(self):
            self.checks += 1
            return self.checks >= 2

    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(worker.asyncio, "sleep", sleep)
    monkeypatch.setattr(
        worker,
        "_worker_post_json",
        lambda *args, **kwargs: pytest.fail("heartbeat must not be sent"),
    )
    task = FinishingTask()
    await worker._worker_job_heartbeat_loop(
        task,  # type: ignore[arg-type]
        "https://example.test",
        {"authorization": "Bearer token"},
        0,
    )
    assert task.checks == 2
    assert sleeps == [0.01]
