from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import yaml

import workgate.standalone.supervisor as standalone_supervisor
from workgate.config.settings import Settings
from workgate.control.standalone_bootstrap import (
    maybe_write_standalone_bootstrap,
    prepare_standalone_control_settings,
)
from workgate.control.state import ControlState
from workgate.executor.config import resolve_executor_config
from workgate.executor.profile import ExecutorProfileStore
from workgate.executor.runtime import build_executor_runtime
from workgate.executor.standalone_bootstrap import (
    apply_standalone_executor_paths,
    mark_standalone_executor_owner_action,
    maybe_import_standalone_bootstrap,
)
from workgate.persistence import FileStateStore
from workgate.protocol.credentials import executor_credential_is_trusted
from workgate.protocol.standalone import (
    STANDALONE_BOOTSTRAP_ENV,
    STANDALONE_CONTROL_CHILD_ENV,
    STANDALONE_CONTROL_READY_HEADER,
    STANDALONE_CONTROL_READY_NONCE_ENV,
    STANDALONE_CONTROL_URL_ENV,
    STANDALONE_EXECUTOR_CHILD_ENV,
    STANDALONE_EXECUTOR_CONFIG_DIR_ENV,
    STANDALONE_EXECUTOR_OWNER_ACTION_FILE_ENV,
    STANDALONE_EXECUTOR_RUNTIME_DIR_ENV,
)
from workgate.standalone.config import resolve_standalone_child_config
from workgate.standalone.supervisor import (
    StandaloneSupervisor,
    cleanup_standalone_runtime_files,
    prepare_standalone,
    standalone_child_env,
)


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "workspace_root": tmp_path / "workspace",
        "state_dir": tmp_path / "state",
        "data_dir": tmp_path / "data",
        "port": 18765,
    }
    values.update(overrides)
    return Settings(**values)


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(
    base_url: str, process: subprocess.Popen[str], *, timeout_s: float = 10
) -> None:
    deadline = time.monotonic() + timeout_s
    with httpx.Client(timeout=0.5, trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(
                    f"standalone exited early with code {process.returncode}"
                )
            try:
                response = client.get(f"{base_url}/healthz")
                if response.status_code == 200 and response.json() == {
                    "ok": True
                }:
                    return
            except httpx.HTTPError, ValueError:
                pass
            time.sleep(0.05)
    raise AssertionError(f"standalone did not become healthy at {base_url}")


def test_standalone_resolves_distinct_role_authority(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        mode="stdio",
        host="0.0.0.0",
        base_url="https://public.example",
        auth_mode="none",
        auth_bypass_localhost=True,
        oauth_issuer="https://public.example",
        oauth_resource="https://public.example/mcp",
        command_denylist=["shutdown"],
    )

    resolved = resolve_standalone_child_config(settings)

    assert resolved.control["mode"] == "mcp"
    assert resolved.control["host"] == "127.0.0.1"
    assert resolved.control["base_url"] is None
    assert resolved.control["auth_mode"] == "oauth"
    assert resolved.control["auth_bypass_localhost"] is False
    assert resolved.control["oauth_issuer"] is None
    assert resolved.control["oauth_resource"] is None
    assert resolved.control["state_dir"] == str(
        tmp_path / "state" / "standalone" / "control"
    )
    assert "workspace_root" not in resolved.control
    assert "allow_full_control" not in resolved.control
    assert "command_denylist" not in resolved.control

    assert resolved.executor["workspace_root"] == str(
        (tmp_path / "workspace").resolve()
    )
    assert resolved.executor["allow_full_control"] is False
    assert resolved.executor["command_denylist"] == ["shutdown"]
    assert resolved.executor["state_dir"] == str(
        tmp_path / "state" / "standalone" / "executor"
    )
    assert "host" not in resolved.executor
    assert "port" not in resolved.executor
    assert "auth_mode" not in resolved.executor
    assert resolved.control_state_dir != resolved.executor_state_dir
    assert resolved.control_url == "http://127.0.0.1:18765"


@pytest.mark.parametrize("port", [-1, 0, 65536])
def test_standalone_requires_stable_tcp_port(tmp_path: Path, port: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 65535"):
        resolve_standalone_child_config(_settings(tmp_path, port=port))


def test_private_launcher_bootstrap_becomes_normal_executor_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_path = tmp_path / "runtime" / "executor-bootstrap.json"
    bootstrap_path.parent.mkdir(mode=0o700)
    monkeypatch.setenv(STANDALONE_BOOTSTRAP_ENV, str(bootstrap_path))

    control_settings = Settings(
        state_dir=tmp_path / "control-state",
        data_dir=tmp_path / "control-data",
        host="127.0.0.1",
        port=18765,
    )
    assert maybe_write_standalone_bootstrap(control_settings)
    assert bootstrap_path.is_file()

    control = ControlState(FileStateStore(lambda: control_settings.state_dir))
    control.start()
    try:
        records = control.snapshot_executors()
        assert len(records) == 1
        trust = next(iter(records.values()))
        assert trust.revoked_at is None
    finally:
        control.close()

    executor_store = FileStateStore(lambda: tmp_path / "executor-state")
    profile_store = ExecutorProfileStore(executor_store)
    assert maybe_import_standalone_bootstrap(profile_store)
    assert not bootstrap_path.exists()
    profile = profile_store.load()
    assert profile is not None
    assert profile.executor_id == trust.executor_id
    assert profile.control_url == "http://127.0.0.1:18765"
    assert executor_credential_is_trusted(
        profile.credential,
        trust.credential_verifier,
        revoked_at=trust.revoked_at,
    )
    assert maybe_import_standalone_bootstrap(profile_store) is False
    if os.name != "nt":
        assert (
            stat.S_IMODE(
                executor_store.layout.executor_profile_path.stat().st_mode
            )
            == 0o600
        )


def test_standalone_control_child_generates_and_reuses_private_oauth_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "control-state"
    monkeypatch.setenv(STANDALONE_CONTROL_CHILD_ENV, "1")
    settings = Settings(state_dir=state_dir)

    first = prepare_standalone_control_settings(settings)
    second = prepare_standalone_control_settings(settings)

    assert first.oauth_admin_pin
    assert second.oauth_admin_pin == first.oauth_admin_pin
    pin_path = state_dir / "oauth-admin-pin"
    assert pin_path.read_text(encoding="utf-8").strip() == first.oauth_admin_pin
    if os.name != "nt":
        assert stat.S_IMODE(pin_path.stat().st_mode) == 0o600


def test_control_bootstrap_reuses_existing_launcher_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_path = tmp_path / "runtime" / "executor-bootstrap.json"
    bootstrap_path.parent.mkdir(mode=0o700)
    monkeypatch.setenv(STANDALONE_BOOTSTRAP_ENV, str(bootstrap_path))
    settings = Settings(
        state_dir=tmp_path / "control-state",
        host="127.0.0.1",
        port=18765,
    )

    assert maybe_write_standalone_bootstrap(settings)
    first = bootstrap_path.read_bytes()
    assert maybe_write_standalone_bootstrap(settings)
    assert bootstrap_path.read_bytes() == first

    control = ControlState(FileStateStore(lambda: settings.state_dir))
    control.start()
    try:
        assert len(control.snapshot_executors()) == 1
    finally:
        control.close()


def test_control_bootstrap_recovers_payload_written_before_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_path = tmp_path / "runtime" / "executor-bootstrap.json"
    bootstrap_path.parent.mkdir(mode=0o700)
    monkeypatch.setenv(STANDALONE_BOOTSTRAP_ENV, str(bootstrap_path))
    settings = Settings(
        state_dir=tmp_path / "control-state",
        host="127.0.0.1",
        port=18765,
    )
    assert maybe_write_standalone_bootstrap(settings)
    payload = json.loads(bootstrap_path.read_text(encoding="utf-8"))
    FileStateStore(lambda: settings.state_dir).remove(
        FileStateStore(lambda: settings.state_dir).layout.control_executors_path
    )

    assert maybe_write_standalone_bootstrap(settings)
    control = ControlState(FileStateStore(lambda: settings.state_dir))
    control.start()
    try:
        assert list(control.snapshot_executors()) == [payload["executor_id"]]
    finally:
        control.close()


def test_control_bootstrap_refuses_missing_profile_after_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_path = tmp_path / "runtime" / "executor-bootstrap.json"
    bootstrap_path.parent.mkdir(mode=0o700)
    monkeypatch.setenv(STANDALONE_BOOTSTRAP_ENV, str(bootstrap_path))
    settings = Settings(
        state_dir=tmp_path / "control-state",
        host="127.0.0.1",
        port=18765,
    )
    assert maybe_write_standalone_bootstrap(settings)
    bootstrap_path.unlink()

    with pytest.raises(RuntimeError, match="owner recovery is required"):
        maybe_write_standalone_bootstrap(settings)


def test_executor_reuses_identity_when_standalone_port_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_path = tmp_path / "runtime" / "executor-bootstrap.json"
    bootstrap_path.parent.mkdir(mode=0o700)
    monkeypatch.setenv(STANDALONE_BOOTSTRAP_ENV, str(bootstrap_path))
    settings = Settings(
        state_dir=tmp_path / "control-state",
        host="127.0.0.1",
        port=18765,
    )
    assert maybe_write_standalone_bootstrap(settings)

    profile_store = ExecutorProfileStore(
        FileStateStore(lambda: tmp_path / "executor-state")
    )
    assert maybe_import_standalone_bootstrap(profile_store)
    before = profile_store.load()
    assert before is not None

    monkeypatch.setenv(STANDALONE_CONTROL_URL_ENV, "http://127.0.0.1:28765")
    assert maybe_import_standalone_bootstrap(profile_store) is False
    after = profile_store.load()
    assert after is not None
    assert after.executor_id == before.executor_id
    assert after.credential == before.credential
    assert after.control_url == "http://127.0.0.1:28765"


def test_prepare_standalone_writes_private_role_configs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    settings = _settings(tmp_path)

    prepared = prepare_standalone(settings)

    control = yaml.safe_load(
        prepared.control_config_path.read_text(encoding="utf-8")
    )
    executor = yaml.safe_load(
        prepared.executor_config_path.read_text(encoding="utf-8")
    )
    assert control["oauth_admin_pin"] is None
    assert not prepared.oauth_admin_pin_path.exists()
    assert prepared.generated_oauth_admin_pin is True
    assert "workspace_root" not in control
    assert executor["workspace_root"] == str(settings.workspace_root.resolve())
    assert "auth_mode" not in executor
    assert not prepared.bootstrap_path.exists()
    assert prepared.executor_profile_path.parent != (
        prepared.child_config.control_state_dir / "executor"
    )
    if os.name != "nt":
        assert (
            stat.S_IMODE(prepared.control_config_path.stat().st_mode) == 0o600
        )
        assert (
            stat.S_IMODE(prepared.executor_config_path.stat().st_mode) == 0o600
        )


def test_prepare_standalone_removes_stale_bootstrap_when_profile_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    settings = _settings(tmp_path)
    first = prepare_standalone(settings)
    first.bootstrap_path.write_text("stale", encoding="utf-8")
    first.executor_profile_path.parent.mkdir(parents=True, exist_ok=True)
    first.executor_profile_path.write_text("fixture", encoding="utf-8")

    second = prepare_standalone(settings)

    assert second.bootstrap_path == first.bootstrap_path
    assert not second.bootstrap_path.exists()


def test_standalone_runtime_files_are_namespaced_by_state_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    first = prepare_standalone(_settings(tmp_path / "first", port=18765))
    second = prepare_standalone(_settings(tmp_path / "second", port=28765))

    assert first.control_config_path != second.control_config_path
    assert first.executor_config_path != second.executor_config_path
    assert first.bootstrap_path != second.bootstrap_path
    assert first.executor_agent_config_dir != second.executor_agent_config_dir
    assert not first.executor_agent_config_dir.exists()
    assert not second.executor_agent_config_dir.exists()
    assert first.control_config_path.parent.parent == (
        runtime / "workgate" / "standalone"
    )
    assert second.control_config_path.parent.parent == (
        runtime / "workgate" / "standalone"
    )
    assert first.executor_agent_config_dir.parent.parent.parent == (
        tmp_path / "config" / "workgate" / "standalone"
    )


def test_standalone_child_env_uses_process_env_when_source_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/fixture/bin")
    monkeypatch.setenv("WORKGATE_PORT", "9999")

    env = standalone_child_env()

    assert env["PATH"] == "/fixture/bin"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert "WORKGATE_PORT" not in env
    assert STANDALONE_BOOTSTRAP_ENV not in env
    assert STANDALONE_CONTROL_CHILD_ENV not in env
    assert STANDALONE_CONTROL_READY_NONCE_ENV not in env
    assert STANDALONE_CONTROL_URL_ENV not in env
    assert STANDALONE_EXECUTOR_CHILD_ENV not in env
    assert STANDALONE_EXECUTOR_CONFIG_DIR_ENV not in env
    assert STANDALONE_EXECUTOR_OWNER_ACTION_FILE_ENV not in env
    assert STANDALONE_EXECUTOR_RUNTIME_DIR_ENV not in env


def test_workgate_child_argv_supports_source_and_frozen_launches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(standalone_supervisor.sys, "frozen", raising=False)
    source = standalone_supervisor.workgate_child_argv("control")
    assert source == [
        sys.executable,
        "-m",
        "workgate.main",
        "control",
    ]

    monkeypatch.setattr(
        standalone_supervisor.sys, "frozen", True, raising=False
    )
    assert standalone_supervisor.workgate_child_argv("control") == [
        sys.executable,
        "control",
    ]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(503), False),
        (httpx.Response(200, text="not-json"), False),
        (httpx.Response(200, json=[]), False),
        (httpx.Response(200, json={"ok": False}), False),
        (httpx.Response(200, json={"ok": True}), False),
        (
            httpx.Response(
                200,
                json={"ok": True},
                headers={STANDALONE_CONTROL_READY_HEADER: "instance-marker"},
            ),
            True,
        ),
    ],
)
def test_control_ready_probe_validates_workgate_health(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
    expected: bool,
) -> None:
    monkeypatch.setattr(
        standalone_supervisor.httpx,
        "get",
        lambda *_args, **_kwargs: response,
    )

    assert (
        standalone_supervisor._control_is_listening(
            "http://127.0.0.1:8765", "instance-marker"
        )
        is expected
    )


def test_control_ready_probe_handles_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args, **_kwargs):
        raise httpx.HTTPError("offline")

    monkeypatch.setattr(standalone_supervisor.httpx, "get", fail)

    assert (
        standalone_supervisor._control_is_listening(
            "http://127.0.0.1:8765", "instance-marker"
        )
        is False
    )


def test_standalone_child_env_removes_ambient_workgate_settings(
    tmp_path: Path,
) -> None:
    bootstrap = tmp_path / "bootstrap.json"
    env = standalone_child_env(
        {
            "PATH": "/bin",
            "WORKGATE_HOST": "0.0.0.0",
            "WORKGATE_ALLOW_FULL_CONTROL": "true",
        },
        bootstrap_path=bootstrap,
        standalone_control=True,
        standalone_executor=True,
        executor_config_dir=tmp_path / "executor-config",
        executor_owner_action_path=tmp_path / "owner-action",
        executor_runtime_dir=tmp_path / "executor-runtime",
        control_ready_nonce="instance-marker",
    )

    assert env["PATH"] == "/bin"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env[STANDALONE_BOOTSTRAP_ENV] == str(bootstrap)
    assert env[STANDALONE_CONTROL_CHILD_ENV] == "1"
    assert env[STANDALONE_CONTROL_READY_NONCE_ENV] == "instance-marker"
    assert env[STANDALONE_EXECUTOR_CHILD_ENV] == "1"
    assert env[STANDALONE_EXECUTOR_CONFIG_DIR_ENV] == str(
        tmp_path / "executor-config"
    )
    assert env[STANDALONE_EXECUTOR_OWNER_ACTION_FILE_ENV] == str(
        tmp_path / "owner-action"
    )
    assert env[STANDALONE_EXECUTOR_RUNTIME_DIR_ENV] == str(
        tmp_path / "executor-runtime"
    )
    assert STANDALONE_CONTROL_URL_ENV not in env
    assert "WORKGATE_HOST" not in env
    assert "WORKGATE_ALLOW_FULL_CONTROL" not in env


def test_runtime_cleanup_preserves_unconsumed_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    prepared = prepare_standalone(_settings(tmp_path))
    prepared.bootstrap_path.write_text("fixture", encoding="utf-8")

    cleanup_standalone_runtime_files(prepared)

    assert not prepared.control_config_path.exists()
    assert not prepared.executor_config_path.exists()
    assert prepared.bootstrap_path.is_file()


def test_runtime_cleanup_removes_consumed_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    prepared = prepare_standalone(_settings(tmp_path))
    prepared.bootstrap_path.write_text("fixture", encoding="utf-8")
    prepared.executor_profile_path.parent.mkdir(parents=True, exist_ok=True)
    prepared.executor_profile_path.write_text("fixture", encoding="utf-8")

    cleanup_standalone_runtime_files(prepared)

    assert not prepared.control_config_path.exists()
    assert not prepared.executor_config_path.exists()
    assert not prepared.bootstrap_path.exists()


class _FakeProcess:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(
                self.argv, 0.0 if timeout is None else timeout
            )
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _prepared_for_supervisor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return prepare_standalone(_settings(tmp_path))


@pytest.mark.parametrize("failed_role", ["control", "executor"])
def test_supervisor_restarts_only_the_exited_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_role: str,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    prepared = prepare_standalone(_settings(tmp_path))
    spawned: list[tuple[_FakeProcess, dict[str, str]]] = []

    def factory(argv, *, env):
        process = _FakeProcess(list(argv))
        spawned.append((process, dict(env)))
        if "control" in process.argv and not prepared.bootstrap_path.exists():
            prepared.bootstrap_path.write_text("{}", encoding="utf-8")
        return process

    supervisor = StandaloneSupervisor(
        prepared,
        process_factory=factory,  # type: ignore[arg-type]
        control_ready_probe=lambda _url, _token: True,
    )
    supervisor.start()
    original_control = supervisor.children["control"]
    original_executor = supervisor.children["executor"]
    original = {
        "control": original_control,
        "executor": original_executor,
    }
    surviving_role = "executor" if failed_role == "control" else "control"
    survivor = original[surviving_role]

    original[failed_role].returncode = 1  # type: ignore[attr-defined]
    assert supervisor.restart_exited_children() == (failed_role,)

    assert supervisor.children[failed_role] is not original[failed_role]
    assert supervisor.children[surviving_role] is survivor
    assert len(spawned) == 3
    assert "control" in spawned[0][0].argv
    assert "executor" in spawned[1][0].argv
    assert spawned[0][1][STANDALONE_BOOTSTRAP_ENV] == str(
        prepared.bootstrap_path
    )
    assert spawned[0][1][STANDALONE_CONTROL_CHILD_ENV] == "1"
    assert (
        spawned[0][1][STANDALONE_CONTROL_READY_NONCE_ENV]
        == prepared.control_ready_nonce
    )
    assert spawned[1][1][STANDALONE_BOOTSTRAP_ENV] == str(
        prepared.bootstrap_path
    )
    assert STANDALONE_CONTROL_CHILD_ENV not in spawned[1][1]
    assert spawned[1][1][STANDALONE_EXECUTOR_CHILD_ENV] == "1"
    assert spawned[1][1][STANDALONE_EXECUTOR_CONFIG_DIR_ENV] == str(
        prepared.executor_agent_config_dir.parent
    )
    assert spawned[1][1][STANDALONE_EXECUTOR_OWNER_ACTION_FILE_ENV] == str(
        prepared.executor_owner_action_path
    )
    assert spawned[1][1][STANDALONE_EXECUTOR_RUNTIME_DIR_ENV] == str(
        prepared.control_config_path.parent / "executor"
    )

    supervisor.shutdown()
    assert survivor.terminated  # type: ignore[attr-defined]


def test_supervisor_leaves_owner_action_executor_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared = _prepared_for_supervisor(tmp_path, monkeypatch)
    control = _FakeProcess(["control"])
    executor = _FakeProcess(["executor"])
    executor.returncode = 1
    prepared.executor_owner_action_path.write_text(
        "owner-action\n", encoding="utf-8"
    )
    supervisor = StandaloneSupervisor(prepared)
    cast(Any, supervisor)._children.update(
        {"control": control, "executor": executor}
    )

    assert supervisor.restart_exited_children() == ()
    assert supervisor.children["control"] is control
    assert supervisor.children["executor"] is executor
    assert "requires owner action" in capsys.readouterr().err

    assert supervisor.restart_exited_children() == ()
    assert capsys.readouterr().err == ""


def test_owner_action_marker_is_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime" / "owner-action"
    path.parent.mkdir(mode=0o700)
    monkeypatch.setenv(STANDALONE_EXECUTOR_OWNER_ACTION_FILE_ENV, str(path))

    mark_standalone_executor_owner_action()

    assert path.read_text(encoding="utf-8") == "owner-action\n"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_invalid_standalone_profile_marks_owner_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "runtime" / "owner-action"
    marker.parent.mkdir(mode=0o700)
    monkeypatch.setenv(STANDALONE_EXECUTOR_OWNER_ACTION_FILE_ENV, str(marker))
    config = resolve_executor_config(_settings(tmp_path))
    runtime = build_executor_runtime(config)
    profile_path = runtime.services.state_store.layout.executor_profile_path
    runtime.services.state_store.write_json(profile_path, {"version": 1})

    with pytest.raises(RuntimeError, match="Invalid executor profile"):
        await runtime.start()

    assert marker.is_file()
    await runtime.aclose()


def test_standalone_executor_paths_namespace_temp_and_integration_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = resolve_executor_config(_settings(tmp_path))
    runtime_root = tmp_path / "runtime-instance"
    config_root = tmp_path / "config-instance"
    monkeypatch.setenv(STANDALONE_EXECUTOR_RUNTIME_DIR_ENV, str(runtime_root))
    monkeypatch.setenv(STANDALONE_EXECUTOR_CONFIG_DIR_ENV, str(config_root))

    adjusted = apply_standalone_executor_paths(base)

    assert adjusted.temp_dir == runtime_root / "tmp"
    assert adjusted.agent_config_dir == config_root / "agent"
    assert adjusted.agent_auth_dir == base.agent_auth_dir
    assert adjusted.state_dir == base.state_dir
    assert adjusted.workspace_root == base.workspace_root


def test_supervisor_rejects_unknown_role_and_duplicate_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared_for_supervisor(tmp_path, monkeypatch)
    spawned: list[_FakeProcess] = []

    def factory(argv, *, env):
        _ = env
        process = _FakeProcess(list(argv))
        spawned.append(process)
        if "control" in process.argv:
            prepared.bootstrap_path.write_text("{}", encoding="utf-8")
        return process

    supervisor = StandaloneSupervisor(
        prepared,
        process_factory=factory,  # type: ignore[arg-type]
        control_ready_probe=lambda _url, _token: True,
    )
    with pytest.raises(ValueError, match="unknown standalone child role"):
        supervisor._argv("unknown")

    supervisor.start()
    with pytest.raises(RuntimeError, match="already started"):
        supervisor.start()
    supervisor.shutdown()
    assert len(spawned) == 2


def test_supervisor_fails_fast_when_control_exits_before_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared_for_supervisor(tmp_path, monkeypatch)
    spawned: list[_FakeProcess] = []

    def factory(argv, *, env):
        _ = env
        process = _FakeProcess(list(argv))
        if "control" in process.argv:
            prepared.bootstrap_path.write_text("{}", encoding="utf-8")
            if not spawned:
                process.returncode = 1
        spawned.append(process)
        return process

    supervisor = StandaloneSupervisor(
        prepared,
        process_factory=factory,  # type: ignore[arg-type]
        control_ready_probe=lambda _url, _token: True,
    )

    with pytest.raises(RuntimeError, match="exited before becoming ready"):
        supervisor.start()

    assert len(spawned) == 1
    assert supervisor.children == {}


def test_supervisor_times_out_when_control_never_becomes_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared_for_supervisor(tmp_path, monkeypatch)
    spawned: list[_FakeProcess] = []

    def factory(argv, *, env):
        _ = env
        process = _FakeProcess(list(argv))
        prepared.bootstrap_path.write_text("{}", encoding="utf-8")
        spawned.append(process)
        return process

    supervisor = StandaloneSupervisor(
        prepared,
        process_factory=factory,  # type: ignore[arg-type]
        control_ready_probe=lambda _url, _token: False,
        poll_interval_s=0.01,
        bootstrap_wait_timeout_s=0.1,
    )

    with pytest.raises(RuntimeError, match="control readiness"):
        supervisor.start()

    assert spawned[0].terminated
    assert supervisor.children == {}


def test_supervisor_failed_control_restart_preserves_executor_and_stops_retrying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared = _prepared_for_supervisor(tmp_path, monkeypatch)
    prepared.bootstrap_path.write_text("{}", encoding="utf-8")
    old_control = _FakeProcess(["control"])
    old_control.returncode = 1
    executor = _FakeProcess(["executor"])
    spawned: list[_FakeProcess] = []

    def factory(argv, *, env):
        _ = env
        process = _FakeProcess(list(argv))
        spawned.append(process)
        return process

    supervisor = StandaloneSupervisor(
        prepared,
        process_factory=factory,  # type: ignore[arg-type]
        control_ready_probe=lambda _url, _token: False,
        poll_interval_s=0.01,
        bootstrap_wait_timeout_s=0.1,
    )
    cast(Any, supervisor)._children.update(
        {"control": old_control, "executor": executor}
    )

    assert supervisor.restart_exited_children() == ()

    assert len(spawned) == 1
    assert spawned[0].terminated
    assert supervisor.children["executor"] is executor
    assert not executor.terminated
    assert "failed to become ready" in capsys.readouterr().err

    assert supervisor.restart_exited_children() == ()
    assert len(spawned) == 1


def test_failed_control_recovery_does_not_restart_exited_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared_for_supervisor(tmp_path, monkeypatch)
    prepared.bootstrap_path.write_text("{}", encoding="utf-8")
    old_control = _FakeProcess(["control"])
    old_control.returncode = 1
    old_executor = _FakeProcess(["executor"])
    old_executor.returncode = 1
    spawned: list[_FakeProcess] = []

    def factory(argv, *, env):
        _ = env
        process = _FakeProcess(list(argv))
        spawned.append(process)
        return process

    supervisor = StandaloneSupervisor(
        prepared,
        process_factory=factory,  # type: ignore[arg-type]
        control_ready_probe=lambda _url, _token: False,
        poll_interval_s=0.01,
        bootstrap_wait_timeout_s=0.1,
    )
    cast(Any, supervisor)._children.update(
        {"control": old_control, "executor": old_executor}
    )

    assert supervisor.restart_exited_children() == ()

    assert len(spawned) == 1
    assert "control" in spawned[0].argv
    assert supervisor.children["executor"] is old_executor


def test_supervisor_does_not_restart_children_after_stop_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared_for_supervisor(tmp_path, monkeypatch)
    process = _FakeProcess(["control"])
    process.returncode = 1
    supervisor = StandaloneSupervisor(
        prepared,
        process_factory=lambda *_args, **_kwargs: process,  # type: ignore[arg-type]
    )
    cast(Any, supervisor)._children["control"] = process

    supervisor.request_stop()

    assert supervisor.restart_exited_children() == ()


def test_supervisor_shutdown_kills_child_that_ignores_terminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared_for_supervisor(tmp_path, monkeypatch)

    class HangingProcess(_FakeProcess):
        def terminate(self) -> None:
            self.terminated = True

    process = HangingProcess(["control"])
    supervisor = StandaloneSupervisor(prepared)
    cast(Any, supervisor)._children["control"] = process

    supervisor.shutdown()

    assert process.terminated
    assert process.killed
    assert supervisor.children == {}


def test_supervisor_signal_context_requests_stop_and_restores_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared_for_supervisor(tmp_path, monkeypatch)
    supervisor = StandaloneSupervisor(prepared)
    before = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }

    with supervisor._signal_handlers():
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert supervisor._stop.is_set()

    assert {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    } == before


def test_run_standalone_maps_lock_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def busy_lock(*_args, **_kwargs):
        raise TimeoutError("busy")

    monkeypatch.setattr(standalone_supervisor, "private_file_lock", busy_lock)

    with pytest.raises(
        standalone_supervisor.StandaloneAlreadyRunningError,
        match="already active",
    ):
        standalone_supervisor.run_standalone(_settings(tmp_path))


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX process-group cleanup is used for the real standalone smoke",
)
def test_real_standalone_bootstraps_offline_and_protects_loopback(
    tmp_path: Path,
) -> None:
    port = _free_tcp_port()
    base_url = f"http://127.0.0.1:{port}"
    workspace = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    runtime_base = tmp_path / "runtime"
    runtime_base.mkdir(mode=0o700)
    log_path = tmp_path / "standalone.log"
    env = os.environ.copy()
    env.update(
        {
            "XDG_RUNTIME_DIR": str(runtime_base),
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1",
        }
    )
    argv = [
        sys.executable,
        "-m",
        "workgate.main",
        "standalone",
        "--workspace-root",
        str(workspace),
        "--state-dir",
        str(state_dir),
        "--data-dir",
        str(data_dir),
        "--port",
        str(port),
    ]

    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
    try:
        _wait_for_health(base_url, process)
        profile_path = (
            state_dir / "standalone" / "executor" / "executor" / "profile.json"
        )
        trust_path = (
            state_dir / "standalone" / "control" / "control" / "executors.json"
        )
        deadline = time.monotonic() + 10
        while (
            not profile_path.is_file() or not trust_path.is_file()
        ) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert profile_path.is_file()
        assert trust_path.is_file()

        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        trust = json.loads(trust_path.read_text(encoding="utf-8"))
        assert [row["executor_id"] for row in trust["executors"]] == [
            profile["executor_id"]
        ]
        assert profile["credential"] not in trust_path.read_text(
            encoding="utf-8"
        )
        runtime_root = runtime_base / "workgate" / "standalone"
        assert list(runtime_root.glob("*/executor-bootstrap.json")) == []
        pin_path = state_dir / "standalone" / "control" / "oauth-admin-pin"
        assert pin_path.is_file()
        pin = pin_path.read_text(encoding="utf-8").strip()
        assert pin
        assert pin not in log_path.read_text(encoding="utf-8", errors="replace")

        with httpx.Client(timeout=1, trust_env=False) as client:
            response = client.post(
                f"{base_url}/mcp",
                json={},
                headers={"Content-Type": "application/json"},
            )
        assert response.status_code == 401
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


@pytest.mark.integration
@pytest.mark.skipif(
    os.name == "nt",
    reason="real standalone process restart coverage is exercised on POSIX",
)
def test_real_supervisor_restarts_control_and_executor_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    port = _free_tcp_port()
    base_url = f"http://127.0.0.1:{port}"
    settings = _settings(tmp_path, port=port)
    prepared = prepare_standalone(settings)
    supervisor = StandaloneSupervisor(prepared)

    try:
        supervisor.start()
        original_control = supervisor.children["control"]
        original_executor = supervisor.children["executor"]
        _wait_for_health(base_url, original_control)  # type: ignore[arg-type]

        deadline = time.monotonic() + 10
        while (
            not prepared.executor_profile_path.is_file()
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert prepared.executor_profile_path.is_file()
        profile_before = prepared.executor_profile_path.read_bytes()

        original_executor.kill()
        original_executor.wait(timeout=5)
        assert supervisor.restart_exited_children() == ("executor",)
        restarted_executor = supervisor.children["executor"]
        assert restarted_executor is not original_executor
        assert supervisor.children["control"] is original_control
        assert prepared.executor_profile_path.read_bytes() == profile_before

        original_control.kill()
        original_control.wait(timeout=5)
        assert supervisor.restart_exited_children() == ("control",)
        restarted_control = supervisor.children["control"]
        assert restarted_control is not original_control
        assert supervisor.children["executor"] is restarted_executor
        _wait_for_health(base_url, restarted_control)  # type: ignore[arg-type]
        assert prepared.executor_profile_path.read_bytes() == profile_before
    finally:
        supervisor.shutdown()
        cleanup_standalone_runtime_files(prepared)


@pytest.mark.integration
@pytest.mark.skipif(
    os.name == "nt",
    reason="real standalone owner-action process exit is exercised on POSIX",
)
def test_real_standalone_leaves_executor_offline_when_control_trust_is_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    settings = _settings(tmp_path, port=_free_tcp_port())
    first = prepare_standalone(settings)
    first_supervisor = StandaloneSupervisor(first)

    try:
        first_supervisor.start()
        deadline = time.monotonic() + 10
        while (
            not first.executor_profile_path.is_file()
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert first.executor_profile_path.is_file()
    finally:
        first_supervisor.shutdown()
        cleanup_standalone_runtime_files(first)

    control_store = FileStateStore(lambda: first.child_config.control_state_dir)
    control_store.remove(control_store.layout.control_executors_path)

    second = prepare_standalone(settings)
    second_supervisor = StandaloneSupervisor(second)
    try:
        second_supervisor.start()
        executor = second_supervisor.children["executor"]
        deadline = time.monotonic() + 10
        while executor.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)

        assert executor.returncode == 1
        assert second.executor_owner_action_path.is_file()
        control = second_supervisor.children["control"]
        assert control.poll() is None
        assert second_supervisor.restart_exited_children() == ()
        assert second_supervisor.children["control"] is control
        assert second_supervisor.children["executor"] is executor
    finally:
        second_supervisor.shutdown()
        cleanup_standalone_runtime_files(second)
