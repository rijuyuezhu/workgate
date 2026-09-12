import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from workgate.config.settings import Settings
from workgate.executor import environment as env_ops
from workgate.executor.config import ExecutorConfig, resolve_executor_config
from workgate.schemas.result_models.session import SessionToolProbe


def _config(tmp_path: Path, **updates) -> ExecutorConfig:
    values = {
        "workspace_root": tmp_path,
        "state_dir": tmp_path / "state",
        "shell_executable": "sh",
        "git_bin": "git",
        "rg_bin": "rg",
        "tmux_bin": "tmux",
    }
    values.update(updates)
    return resolve_executor_config(Settings(**values))


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Linux", "linux"), ("Darwin", "macos"), ("Windows", "windows")],
)
def test_normalized_os(value, expected):
    assert env_ops._normalized_os(value) == expected


def test_safe_tokens_and_versions_are_bounded_and_do_not_return_raw_text():
    assert (
        env_ops._safe_token(" Linux secret/path ", "unknown")
        == "Linux_secret_path"
    )
    assert (
        env_ops._extract_version("tool 12.3.4 private/path token") == "12.3.4"
    )
    assert env_ops._extract_version("no version here") is None


def test_version_command_handles_cmd_powershell_and_tmux():
    assert env_ops._version_command(("cmd.exe",), "shell")[-3:] == (
        "/d",
        "/c",
        "ver",
    )
    assert "$PSVersionTable" in " ".join(
        env_ops._version_command(("pwsh",), "shell")
    )
    assert env_ops._version_command(("tmux",), "tmux") == ("tmux", "-V")
    assert env_ops._version_command(("git",), "git") == ("git", "--version")


def test_probe_command_reports_missing_timeout_error_and_version(monkeypatch):
    assert (
        env_ops._probe_command(
            env_ops._CommandProbe("git", None, "configured")
        ).status
        == "missing"
    )

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["tool"], 1)

    monkeypatch.setattr(env_ops.subprocess, "run", timeout)
    timed = env_ops._probe_command(
        env_ops._CommandProbe("git", ("git",), "configured")
    )
    assert timed.available is False and timed.status == "timeout"

    monkeypatch.setattr(
        env_ops.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()),
    )
    failed = env_ops._probe_command(
        env_ops._CommandProbe("git", ("git",), "configured")
    )
    assert failed.available is False and failed.status == "error"

    monkeypatch.setattr(
        env_ops.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="git version 2.51.0\nprivate/path", stderr=""
        ),
    )
    available = env_ops._probe_command(
        env_ops._CommandProbe("git", ("git",), "configured")
    )
    assert available.model_dump() == {
        "available": True,
        "status": "available",
        "version": "2.51.0",
        "source": "configured",
    }

    monkeypatch.setattr(
        env_ops.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="tool 1.2.3", stderr="failure secret"
        ),
    )
    errored = env_ops._probe_command(
        env_ops._CommandProbe("git", ("git",), "configured")
    )
    assert errored.status == "error"
    assert "secret" not in json.dumps(errored.model_dump())


def test_tool_collection_is_cached_and_probes_concurrently(
    monkeypatch, tmp_path
):
    config = _config(tmp_path)
    with env_ops._CACHE_LOCK:
        env_ops._TOOL_CACHE.clear()
    probes = tuple(
        env_ops._CommandProbe(name, (name,), "configured")
        for name in ("shell", "git", "ripgrep", "tmux")
    )
    monkeypatch.setattr(
        env_ops,
        "_tool_probe_context",
        lambda _config: (("cache-key",), probes),
    )
    calls = []
    barrier = threading.Barrier(len(probes))

    def probe(spec):
        calls.append(spec.name)
        barrier.wait(timeout=1)
        return SessionToolProbe(
            available=True,
            status="available",
            version="1.2.3",
            source=spec.source,
        )

    monkeypatch.setattr(env_ops, "_probe_command", probe)
    first = env_ops._collect_tools(config)
    second = env_ops._collect_tools(config)

    assert sorted(calls) == sorted(probe.name for probe in probes)
    assert first.git.version == "1.2.3"
    assert second == first
    assert second is not first
    assert set(first.model_dump()) == {"shell", "git", "ripgrep", "tmux"}


def test_runtime_environment_normalizes_source_and_frozen(monkeypatch):
    monkeypatch.setattr(env_ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(env_ops.platform, "release", lambda: "24.1 secret/path")
    monkeypatch.setattr(env_ops.platform, "machine", lambda: "arm64 private")
    monkeypatch.setattr(
        env_ops.platform, "python_implementation", lambda: "CPython"
    )
    monkeypatch.setattr(env_ops.platform, "python_version", lambda: "3.14.0")
    monkeypatch.setattr(env_ops.sys, "frozen", True, raising=False)

    runtime = env_ops._runtime_environment()
    assert runtime.runtime_kind == "frozen"
    assert runtime.os == "macos"
    assert runtime.release == "24.1_secret_path"
    assert runtime.architecture == "arm64_private"
    assert runtime.process_bits in {32, 64}


def test_collect_executor_environment_is_executor_owned_and_allowlisted(
    monkeypatch, tmp_path
):
    config = _config(
        tmp_path,
        command_denylist=["private-command"],
        path_denylist=["private/path"],
        max_jobs=17,
        max_grep_results=23,
    )
    tool = SessionToolProbe(available=True, status="available", version="1.0")
    tools = env_ops.SessionToolsEnvironment(
        shell=tool,
        git=tool,
        ripgrep=tool,
        tmux=tool,
    )
    monkeypatch.setattr(env_ops, "_collect_tools", lambda _config: tools)
    monkeypatch.setattr(env_ops, "conpty_available", lambda: False)

    environment = env_ops.collect_executor_session_environment(
        config, workdir=str(tmp_path)
    )
    payload = environment.model_dump(mode="json")
    encoded = json.dumps(payload)

    assert environment.workspace.workspace_root == str(tmp_path)
    assert environment.capabilities.raw_pty is True
    assert environment.policy.max_jobs == 17
    assert environment.policy.max_search_results == 23
    assert set(payload["capabilities"]) == {"raw_pty", "conpty"}
    assert "authentication_mode" not in payload["policy"]
    assert "max_agent_sessions" not in payload["policy"]
    for control_only in (
        "browser_ui",
        "browser_console",
        "agent_bridge",
        "oauth_mcp_clients",
        "oauth_configured_mcp_servers",
        "http_transfer",
        "audit_full_payload",
    ):
        assert control_only not in payload["capabilities"]
    for forbidden in ("private-command", "private/path"):
        assert forbidden not in encoded

    monkeypatch.setattr(
        env_ops,
        "_collect_tools",
        lambda _config: (_ for _ in ()).throw(
            RuntimeError("probe-secret-fixture")
        ),
    )
    fallback = env_ops.collect_executor_session_environment(
        config, workdir=str(tmp_path)
    )
    assert fallback.tools.git.status == "error"
    assert "probe-secret-fixture" not in json.dumps(
        fallback.model_dump(mode="json")
    )


def test_resolve_command_rejects_invalid_or_missing(monkeypatch):
    assert env_ops._resolve_command("'") is None
    monkeypatch.setattr(env_ops.shutil, "which", lambda _value: None)
    assert env_ops._resolve_command("missing-tool") is None
