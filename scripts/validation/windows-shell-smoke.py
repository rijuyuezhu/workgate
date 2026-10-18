"""Native Windows shell argv and quoting smoke; bounded process containment is #385."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("This smoke needs native Windows Python")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workgate.platform.shell import (  # noqa: E402
    default_python,
    default_shell,
    shell_argv,
    shell_env_prefix,
    shell_join_argv,
)


def run() -> None:
    assert default_shell() == "powershell.exe"
    assert default_python() == sys.executable
    with tempfile.TemporaryDirectory(prefix="workgate-ps-") as tmp:
        folder = Path(tmp) / "directory with spaces"
        folder.mkdir()
        command = shell_env_prefix(
            default_shell(),
            "Write-Output $env:TEST_VALUE",
            {"TEST_VALUE": "a b'c; &"},
        )
        result = subprocess.run(
            shell_argv(default_shell(), command),
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        assert result.stdout.strip() == "a b'c; &", result

        python_script = folder / "script with spaces.py"
        python_script.write_text(
            "import os, sys\n"
            "print('SCRIPT_OK')\n"
            "print(os.path.basename(os.getcwd()))\n"
            "sys.exit(7)\n",
            encoding="utf-8",
        )
        invocation = shell_join_argv(
            [sys.executable, str(python_script)], shell=default_shell()
        )
        result = subprocess.run(
            shell_argv(default_shell(), invocation),
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Native child executable failures must be surfaced as shell failures.
        assert result.returncode == 7, result
        assert "SCRIPT_OK" in result.stdout, result
        assert "directory with spaces" in result.stdout, result

        unicode_command = shell_argv(
            default_shell(), "Write-Output '你好，Windows — é'"
        )
        raw = subprocess.run(
            unicode_command,
            cwd=folder,
            capture_output=True,
            timeout=30,
            check=True,
        )
        assert raw.stdout.decode("utf-8").strip() == "你好，Windows — é"

        unicode_error = subprocess.run(
            shell_argv(default_shell(), "Write-Error '你好，stderr — é'"),
            cwd=folder,
            capture_output=True,
            timeout=30,
        )
        assert unicode_error.returncode != 0, unicode_error
        assert "你好，stderr — é" in unicode_error.stderr.decode("utf-8")

        native = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os; print('EXE_OK'); print(os.path.basename(os.getcwd()))",
            ],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        assert native.stdout.splitlines() == [
            "EXE_OK",
            "directory with spaces",
        ]

        # Windows batch files are executed by Windows' standard command
        # processor. These are trusted script paths and arguments.
        for extension in (".cmd", ".bat"):
            batch = folder / f"batch with spaces{extension}"
            batch.write_bytes(
                b"@echo off\r\necho BATCH_OK:%~1\r\nexit /b 7\r\n"
            )
            result = subprocess.run(
                [str(batch), "two words"],
                cwd=folder,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 7, result
            assert "BATCH_OK:two words" in result.stdout, result

        cmd_unicode = subprocess.run(
            shell_argv("cmd.exe", "echo 你好，cmd — é"),
            cwd=folder,
            capture_output=True,
            timeout=30,
            check=True,
        )
        assert cmd_unicode.stdout.decode("utf-8").strip() == "你好，cmd — é"

        cmd = subprocess.run(
            shell_argv("cmd.exe", "echo CMD_OK"),
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        assert "CMD_OK" in cmd.stdout, cmd

    print(
        "Windows native PowerShell/cmd dialect, quoting and exit status: PASS"
    )


if __name__ == "__main__":
    run()
