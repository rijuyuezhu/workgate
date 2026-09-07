import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import workgate.remote_worker.lifecycle as lifecycle
import workgate.remote_worker.worker as worker
from workgate.config.settings import clear_settings_cache
from workgate.executor.config import resolve_executor_config
from workgate.remote.manager import RemoteManager, RemoteWorker, _utc


def _managed_poll_report(**extra: object) -> dict[str, object]:
    from workgate.remote.bundle import worker_bundle_manifest

    manifest = worker_bundle_manifest()
    return {
        "protocol_version": 2,
        "runtime_kind": "managed_bundle",
        "worker_version": str(manifest["bundle_version"]),
        "bundle_version": str(manifest["bundle_version"]),
        "bundle_sha256": str(manifest["sha256"]),
        **extra,
    }


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "WORKGATE_WORKER_STATE_DIR", str(tmp_path / "worker-state")
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_ALLOW_FULL_CONTROL", "false")
    monkeypatch.delenv("WORKGATE_WORKER_MANAGED", raising=False)
    monkeypatch.delenv("WORKGATE_WORKER_LOCK_HANDLE", raising=False)
    clear_settings_cache()


def test_worker_run_lock_rejects_duplicate_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)

    with (
        lifecycle.worker_run_lock(),
        pytest.raises(
            lifecycle.WorkerAlreadyRunningError, match="already running"
        ),
        lifecycle.worker_run_lock(),
    ):
        pass

    assert lifecycle.worker_lock_path().is_file()
    with lifecycle.worker_run_lock():
        pass


def test_worker_run_locks_are_isolated_by_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    first = "p_abcdefgh"
    second = "p_ijklmnop"

    with lifecycle.worker_run_lock(first):
        with lifecycle.worker_run_lock(second):
            assert lifecycle.worker_lock_path(first).is_file()
            assert lifecycle.worker_lock_path(second).is_file()
        with (
            pytest.raises(
                lifecycle.WorkerAlreadyRunningError, match="already running"
            ),
            lifecycle.worker_run_lock(first),
        ):
            pass

    with lifecycle.worker_run_lock(first):
        pass


def test_managed_worker_waits_for_lock_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    monkeypatch.setenv("WORKGATE_WORKER_MANAGED", "1")
    attempts = 0
    sleeps: list[float] = []

    def fake_lock(_handle) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BlockingIOError

    monkeypatch.setattr(lifecycle, "_lock_worker_file", fake_lock)
    monkeypatch.setattr(lifecycle, "_unlock_worker_file", lambda _handle: None)
    monkeypatch.setattr(lifecycle.time, "sleep", sleeps.append)

    with lifecycle.worker_run_lock():
        pass

    assert attempts == 2
    assert sleeps == [5.0]


def test_worker_run_lock_propagates_non_contention_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(
        lifecycle,
        "_lock_worker_file",
        lambda _handle: (_ for _ in ()).throw(
            OSError("lock backend unavailable")
        ),
    )

    with (
        pytest.raises(OSError, match="lock backend unavailable"),
        lifecycle.worker_run_lock(),
    ):
        pass


@pytest.mark.parametrize(
    ("system", "output"),
    [
        ("Linux", "123\n"),
        ("Darwin", "service = {\n    pid = 123\n}\n"),
    ],
)
def test_legacy_managed_worker_is_detected_by_service_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    output: str,
) -> None:
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(lifecycle.platform, "system", lambda: system)
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 123)
    monkeypatch.setattr(lifecycle.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(
        lifecycle.shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(
        lifecycle,
        "_run",
        lambda command: subprocess.CompletedProcess(
            command, 0, stdout=output, stderr=""
        ),
    )
    if system == "Linux":
        path = lifecycle._systemd_unit_path()
    else:
        path = lifecycle._launchd_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()

    assert lifecycle._current_worker_is_managed() is True


def test_manual_worker_is_not_confused_with_managed_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(lifecycle.platform, "system", lambda: "Linux")
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 456)
    monkeypatch.setattr(
        lifecycle.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )
    monkeypatch.setattr(
        lifecycle,
        "_run",
        lambda command: subprocess.CompletedProcess(
            command, 0, stdout="123\n", stderr=""
        ),
    )
    path = lifecycle._systemd_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()

    assert lifecycle._current_worker_is_managed() is False


def test_worker_lock_survives_reexec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    script = tmp_path / "lock-reexec.py"
    script.write_text(
        """
import os
import subprocess
import sys
from workgate.remote_worker.lifecycle import (
    WorkerAlreadyRunningError,
    prepare_worker_lock_reexec,
    worker_run_lock,
)

if len(sys.argv) > 1 and sys.argv[1] == "probe":
    try:
        with worker_run_lock():
            raise SystemExit(0)
    except WorkerAlreadyRunningError:
        raise SystemExit(2)

if os.environ.get("WORKGATE_LOCK_STAGE") == "2":
    with worker_run_lock():
        probe = subprocess.run([sys.executable, __file__, "probe"], check=False)
        if probe.returncode != 2:
            raise SystemExit(f"competing lock result: {probe.returncode}")
        print("lock inherited", flush=True)
else:
    with worker_run_lock():
        os.environ["WORKGATE_LOCK_STAGE"] = "2"
        prepare_worker_lock_reexec()
        os.execv(sys.executable, [sys.executable, __file__])
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        value
        for value in (source_root, environment.get("PYTHONPATH", ""))
        if value
    )

    completed = subprocess.run(
        [sys.executable, str(script)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "lock inherited"


@pytest.mark.asyncio
async def test_run_worker_locks_before_enrollment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    bundle_runtime = tmp_path / "bundle-runtime"
    workspace = tmp_path / "executor-workspace"
    bundle_runtime.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(bundle_runtime)
    monkeypatch.delenv("WORKGATE_WORKSPACE_ROOT", raising=False)
    clear_settings_cache()
    events: list[str] = []
    executor_workspace_roots: list[Path] = []
    control_connection_flags: list[bool] = []

    @contextmanager
    def fake_lock():
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    async def fake_locked(*_args, **_kwargs) -> None:
        events.append("network")

    fake_runtime = SimpleNamespace(dispatcher=SimpleNamespace(execute=None))

    class RuntimeScope:
        async def __aenter__(self):
            events.append("runtime-enter")
            return fake_runtime

        async def __aexit__(self, *_args):
            events.append("runtime-exit")

    fake_runtime.lifespan = lambda: RuntimeScope()

    def fake_build(settings, *, enable_control_connection=True):
        executor_workspace_roots.append(
            resolve_executor_config(settings).workspace_root
        )
        control_connection_flags.append(enable_control_connection)
        return fake_runtime

    monkeypatch.setattr(lifecycle, "worker_run_lock", fake_lock)
    monkeypatch.setattr(worker, "_run_worker_locked", fake_locked)
    monkeypatch.setattr(
        "workgate.executor.runtime.build_executor_runtime", fake_build
    )

    await worker.run_worker(
        "https://controller.test", "invite", workdir=str(workspace)
    )

    assert executor_workspace_roots == [workspace.resolve()]
    assert control_connection_flags == [False]
    assert events == [
        "lock-enter",
        "runtime-enter",
        "network",
        "runtime-exit",
        "lock-exit",
    ]


def test_poll_timeout_helpers_bound_and_advertise_deadline() -> None:
    assert worker._worker_poll_request_timeout_s({"poll_timeout_s": 30}) == 40
    for value in (None, 0, -1, float("inf"), "invalid"):
        assert (
            worker._worker_poll_request_timeout_s({"poll_timeout_s": value})
            is None
        )

    payload = worker._worker_poll_payload("3.9.1", {}, 40)
    assert payload["poll_timeout_s"] == 30
    assert payload["protocol_version"] == 2
    assert payload["runtime_kind"] == "unmanaged_source"


def test_bounded_poll_requires_curl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="curl is required"):
        worker._worker_post_json(
            "https://controller.test/remote/poll", {}, timeout=30
        )


@pytest.mark.asyncio
async def test_controller_uses_shorter_worker_poll_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    monkeypatch.setenv("WORKGATE_REMOTE_POLL_TIMEOUT_S", "30")
    clear_settings_cache()
    manager = RemoteManager()
    monkeypatch.setattr(manager, "_load_registry_unlocked", lambda: None)
    managed = RemoteWorker(name="worker", token="token", last_seen=_utc())
    manager.workers[managed.name] = managed
    manager.tokens[managed.token] = managed.name
    observed: list[float] = []

    async def fake_wait_for(awaitable, timeout: float):
        observed.append(timeout)
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(
        "workgate.remote.manager.asyncio.wait_for", fake_wait_for
    )

    result = await manager.poll(
        "token", _managed_poll_report(poll_timeout_s=0.25)
    )

    assert observed == [pytest.approx(0.25, abs=0.001)]
    assert result == {
        "job": None,
        "heartbeat": True,
        "poll_timeout_s": 30.0,
        "upgrade": {
            "required": False,
            "version": _managed_poll_report()["bundle_version"],
            "sha256": _managed_poll_report()["bundle_sha256"],
            "manifest_path": "/remote/worker-bundle.tgz?manifest=1",
        },
    }


@pytest.mark.asyncio
async def test_worker_continuously_updates_negotiated_poll_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(
        worker, "_configure_worker_runtime_env", lambda _path: None
    )
    monkeypatch.setattr(
        worker,
        "_read_worker_identity",
        lambda server, name: {
            "server": server,
            "name": name or "worker",
            "access": "access",
        },
    )
    monkeypatch.setattr(worker, "_write_worker_identity", lambda _data: None)
    monkeypatch.setattr(
        worker.worker_runtime, "current_runtime_identity", lambda: {}
    )

    async def resume(*_args, **_kwargs):
        return {
            "ok": True,
            "data": {
                "name": "worker",
                "heartbeat_interval_s": 15,
                "poll_timeout_s": 30,
            },
        }

    calls: list[tuple[float | None, float | None]] = []
    responses = iter(
        [
            {"job": None, "poll_timeout_s": 5},
            {"job": None, "upgrade": {"required": True}},
        ]
    )

    async def post(
        _url,
        payload,
        _headers=None,
        timeout=None,
        _operation="request",
    ):
        calls.append((timeout, payload.get("poll_timeout_s")))
        return {"ok": True, "data": next(responses)}

    async def upgrade(*_args, **_kwargs):
        raise SystemExit(0)

    monkeypatch.setattr(worker, "_worker_resume_or_none", resume)
    monkeypatch.setattr(worker, "_worker_post_json_forever", post)
    monkeypatch.setattr(worker, "_install_and_reexec_worker", upgrade)

    with pytest.raises(SystemExit):
        await worker._run_worker_locked(
            "https://controller.test", "", "worker", str(tmp_path)
        )

    assert calls == [(40.0, 30.0), (15.0, 5.0)]


def test_windows_reexec_preserves_lock_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workgate.remote_worker import runtime

    calls: list[object] = []
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(
        lifecycle,
        "prepare_worker_lock_reexec",
        lambda: calls.append("prepare") or 99,
    )
    monkeypatch.setattr(
        lifecycle,
        "cancel_worker_lock_reexec",
        lambda value: calls.append(("cancel", value)),
    )
    monkeypatch.setattr(
        runtime,
        "reexec_environment",
        lambda: {"WORKGATE_WORKER_LOCK_HANDLE": "99"},
    )
    monkeypatch.setattr(
        runtime,
        "worker_reexec_argv",
        lambda: ["worker-python", "-m", "workgate.remote_worker", "run"],
    )
    captured = SimpleNamespace()

    def fake_popen(argv, **kwargs):
        captured.argv = argv
        captured.kwargs = kwargs
        return SimpleNamespace(pid=1)

    monkeypatch.setattr(runtime.subprocess, "Popen", fake_popen)

    with pytest.raises(SystemExit):
        runtime.reexec_worker()

    assert calls == ["prepare", ("cancel", 99)]
    assert captured.kwargs["close_fds"] is False
    assert captured.kwargs["env"]["WORKGATE_WORKER_LOCK_HANDLE"] == "99"


def test_managed_service_pid_probe_failure_is_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(lifecycle.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        lifecycle.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )
    path = lifecycle._systemd_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    monkeypatch.setattr(
        lifecycle,
        "_run",
        lambda _command: (_ for _ in ()).throw(OSError("manager unavailable")),
    )

    assert lifecycle._managed_service_pid() is None
    assert lifecycle._current_worker_is_managed() is False
