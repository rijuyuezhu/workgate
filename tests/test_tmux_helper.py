import pytest

import workgate.executor.shell as shell
from workgate.config.settings import Settings
from workgate.executor.config import resolve_executor_config
from workgate.executor.runtime import build_executor_runtime
from workgate.executor.terminal import tmux as tmux_helper


def test_detached_tmux_env_removes_enclosing_client_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setenv("KEEP_ME", "yes")

    env = tmux_helper.detached_tmux_env()

    assert "TMUX" not in env
    assert "TMUX_PANE" not in env
    assert env["KEEP_ME"] == "yes"


def test_resolve_tmux_prefers_system_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        tmux_helper.shutil, "which", lambda value: "/usr/bin/tmux"
    )
    selected = tmux_helper.resolve_tmux("tmux")
    assert selected == tmux_helper.TmuxSelection(
        "/usr/bin/tmux", "system", "tmux"
    )


def test_resolve_tmux_preserves_explicit_configuration(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(tmux_helper.shutil, "which", lambda value: None)
    selected = tmux_helper.resolve_tmux("/missing/custom-tmux")
    assert selected.path is None
    assert selected.source == "configured"
    with pytest.raises(RuntimeError, match="Configured tmux executable"):
        tmux_helper.require_tmux("/missing/custom-tmux")


def test_require_tmux_reports_unavailable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tmux_helper.shutil, "which", lambda value: None)
    with pytest.raises(RuntimeError, match="install tmux"):
        tmux_helper.require_tmux("tmux")


def test_resolve_tmux_uses_explicit_configuration(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        tmux_helper.shutil,
        "which",
        lambda value: "/opt/tmux" if value == "configured-tmux" else None,
    )
    assert tmux_helper.resolve_tmux(
        "configured-tmux"
    ) == tmux_helper.TmuxSelection("/opt/tmux", "configured", "configured-tmux")


@pytest.mark.asyncio
async def test_shell_tmux_uses_resolved_executable(
    monkeypatch: pytest.MonkeyPatch,
):
    config = resolve_executor_config(Settings(tmux_bin="tmux"))
    expected = object()
    calls: list[tuple[list[str], str, int | None, dict[str, str] | None]] = []
    monkeypatch.setattr(
        shell,
        "require_tmux",
        lambda _configured: tmux_helper.TmuxSelection(
            "/opt/tmux", "system", "tmux"
        ),
    )
    monkeypatch.setattr(
        shell, "_resolved_tmux_shell", lambda _config, _cwd=".": "/bin/bash"
    )

    async def fake_run_exec(
        _config,
        argv: list[str],
        *,
        cwd: str = ".",
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
    ):
        calls.append((argv, cwd, timeout_s, env))
        return expected

    monkeypatch.setattr(shell, "_run_exec", fake_run_exec)
    assert await shell.tmux(config, ["list-sessions"], timeout_s=7) is expected
    assert calls == [
        (
            ["/opt/tmux", "list-sessions"],
            ".",
            7,
            {"TMUX": "", "TMUX_PANE": "", "SHELL": "/bin/bash"},
        )
    ]


@pytest.mark.asyncio
async def test_list_persistent_shells_is_empty_when_default_tmux_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    runtime = build_executor_runtime(
        Settings(), enable_control_connection=False
    )
    monkeypatch.setattr(
        shell, "_use_conpty_persistent_shell_backend", lambda: False
    )
    monkeypatch.setattr(
        shell,
        "resolve_tmux",
        lambda _configured: tmux_helper.TmuxSelection(
            None, "unavailable", "tmux"
        ),
    )
    monkeypatch.setattr(
        shell,
        "tmux",
        lambda *args, **kwargs: pytest.fail("tmux must not be invoked"),
    )
    result = await shell.list_persistent_shells_execute(
        runtime.config, runtime.services.tool_session_store
    )
    assert result.shells == []
