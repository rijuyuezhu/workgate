"""Shell command syntax differs by host; never rewrite Bash as PowerShell."""

import sys

import pytest

import workgate.platform.shell as dialect
from workgate.platform.host import HostOS


def test_shell_argv_preserves_posix_login_and_exec() -> None:
    assert dialect.shell_argv("bash", "printf ok", system="Linux") == [
        "bash",
        "-c",
        "printf ok",
    ]
    assert dialect.shell_argv(
        "/bin/zsh", "echo hello", login=True, system="Darwin"
    ) == ["/bin/zsh", "-lc", "echo hello"]


def test_windows_shell_argv_uses_native_flags() -> None:
    assert dialect.shell_argv(
        "powershell.exe", "Write-Output 'ok'", system="Windows"
    ) == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
        "Write-Output 'ok'; if (-not $?) { "
        "if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }; exit 1 }",
    ]
    assert dialect.shell_argv("cmd.exe", "echo ok", system="Windows") == [
        "cmd.exe",
        "/D",
        "/S",
        "/C",
        "chcp 65001 >NUL & echo ok",
    ]


@pytest.mark.parametrize("shell", ["bash.exe", "unsupported.exe"])
def test_windows_shell_does_not_guess_dialect(shell: str) -> None:
    with pytest.raises(ValueError, match="unsupported Windows shell"):
        dialect.shell_argv(shell, "echo ok", system="Windows")


def test_powershell_quotes_literal_python_argv_and_environment() -> None:
    assert (
        dialect.shell_join_argv(
            ["C:\\Program Files\\Python\\python.exe", "O'Neil script.py"],
            shell="pwsh.exe",
            system="Windows",
        )
        == "& 'C:\\Program Files\\Python\\python.exe' 'O''Neil script.py'"
    )
    assert (
        dialect.shell_env_prefix(
            "powershell.exe",
            "Write-Output $env:SAMPLE",
            {"SAMPLE": "a b'c; &"},
            system="Windows",
        )
        == "$env:SAMPLE = 'a b''c; &'; Write-Output $env:SAMPLE"
    )


def test_cmd_overrides_and_python_quote_explicitly_unsupported() -> None:
    with pytest.raises(ValueError, match="cmd.exe"):
        dialect.shell_env_prefix(
            "cmd.exe", "echo ok", {"FOO": "value"}, system="Windows"
        )
    with pytest.raises(ValueError, match="cmd.exe"):
        dialect.shell_join_argv(
            ["python", "script.py"], shell="cmd", system="Windows"
        )


def test_default_shell_and_python_follow_host(monkeypatch) -> None:
    monkeypatch.setattr(dialect, "host_os", lambda: HostOS.WINDOWS)
    assert dialect.default_shell() == "powershell.exe"
    assert dialect.default_python() == sys.executable
    monkeypatch.setattr(dialect, "host_os", lambda: HostOS.LINUX)
    assert dialect.default_shell() == "/bin/bash"
    assert dialect.default_python() == "python3"


def test_frozen_windows_python_default_does_not_invoke_workgate_executable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(dialect, "host_os", lambda: HostOS.WINDOWS)
    monkeypatch.setattr(dialect.sys, "frozen", True, raising=False)
    assert dialect.default_python() == "python"
