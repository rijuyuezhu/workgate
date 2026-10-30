"""Schedule native Executor cutovers outside their current managed service."""

import os
import plistlib
import sys
from pathlib import Path

from ..app_paths import ensure_private_directory
from ..platform.host import HostOS, host_os
from ..platform.process import run_bounded_capture
from ..platform.windows_task import RunCommand, WindowsUserTask
from ..supervisor import PARENT_FD_ENV
from ..utils.private_files import write_private_bytes
from .source_stage import _TAG

# One well-known, automatically collected transient service. systemd refuses
# another unit with the same name while a previous cutover is in progress.
_TRANSIENT_UNIT = "workgate-executor-source-cutover"


def schedule_linux_source_cutover(
    checkout: Path, *, version: str, config: Path
) -> None:
    """Schedule existing switch.py under the user systemd manager.

    This only establishes an external handoff. The caller must first freeze
    admission and verify resource idleness; a later coordinator must confirm
    the resulting native service state. Scheduling is not upgrade success.
    """
    if sys.platform != "linux":
        raise RuntimeError("native source handoff requires Linux systemd")
    tag = f"v{version}"
    if _TAG.fullmatch(tag) is None:
        raise ValueError("invalid source release version")
    if any(
        key.startswith("WORKGATE_") and key != PARENT_FD_ENV
        for key in os.environ
    ):
        raise RuntimeError(
            "source cutover requires durable config, not overrides"
        )

    script = checkout / "deploy" / "source" / "switch.py"
    python = checkout / ".venv" / "bin" / "python"
    if (
        not script.is_file()
        or script.is_symlink()
        or not python.is_file()
        or config.is_symlink()
        or not config.is_file()
        or not config.is_absolute()
    ):
        raise RuntimeError("managed source cutover inputs are unavailable")

    code, _ = run_bounded_capture(
        [
            "systemd-run",
            "--user",
            "--unit=" + _TRANSIENT_UNIT,
            "--collect",
            "--no-block",
            "--service-type=exec",
            str(python),
            str(script),
            "--role",
            "executor",
            "--tag",
            tag,
            "--install-root",
            str(checkout.parent.parent),
            "--executor-config",
            str(config),
        ],
        timeout=30,
    )
    if code:
        raise RuntimeError("could not schedule native source cutover")


def schedule_windows_source_cutover(
    checkout: Path, *, version: str, config: Path, run: RunCommand
) -> None:
    """Hand off an existing prepared Windows switch to an expiring owned task.

    The Task Scheduler owns this new one-shot task independently from the
    running Executor task. Accepted scheduling is not proof of a cutover.
    """
    if host_os() is not HostOS.WINDOWS:
        raise RuntimeError(
            "native source handoff requires Windows Task Scheduler"
        )
    tag = f"v{version}"
    if _TAG.fullmatch(tag) is None:
        raise ValueError("invalid source release version")
    if any(
        key.startswith("WORKGATE_") and key != PARENT_FD_ENV
        for key in os.environ
    ):
        raise RuntimeError(
            "source cutover requires durable config, not overrides"
        )

    script = checkout / "deploy" / "source" / "switch_windows.py"
    python = checkout / ".venv" / "Scripts" / "pythonw.exe"
    if (
        not script.is_file()
        or script.is_symlink()
        or not python.is_file()
        or python.is_symlink()
        or config.is_symlink()
        or not config.is_file()
        or not config.is_absolute()
    ):
        raise RuntimeError(
            "managed Windows source cutover inputs are unavailable"
        )

    task = WindowsUserTask(
        "Workgate Executor Source Cutover",
        description="Managed by Workgate: executor source cutover",
        run=run,
    )
    task.schedule_once(
        [
            str(python),
            str(script),
            "--role",
            "executor",
            "--tag",
            tag,
            "--install-root",
            str(checkout.parent.parent),
            "--executor-config",
            str(config),
        ],
        cwd=str(checkout),
    )


def schedule_macos_source_cutover(
    checkout: Path, *, version: str, config: Path
) -> None:
    """Submit a one-shot launchd job outside the running Executor LaunchAgent."""
    if host_os() is not HostOS.MACOS:
        raise RuntimeError("native source handoff requires macOS launchd")
    tag = f"v{version}"
    if _TAG.fullmatch(tag) is None:
        raise ValueError("invalid source release version")
    if any(
        key.startswith("WORKGATE_") and key != PARENT_FD_ENV
        for key in os.environ
    ):
        raise RuntimeError(
            "source cutover requires durable config, not overrides"
        )
    script = checkout / "deploy/source/switch_macos.py"
    python = checkout / ".venv/bin/python"
    if (
        any(
            path.is_symlink() or not path.is_file()
            for path in (script, python, config)
        )
        or not config.is_absolute()
    ):
        raise RuntimeError(
            "managed macOS source cutover inputs are unavailable"
        )

    # launchctl submit infers KeepAlive; use a one-shot plist that does not
    # relaunch the completed switch and restart the Executor in a loop.
    directory = ensure_private_directory(
        checkout.parent.parent / "source-cutover"
    )
    plist = directory / (version + ".plist")
    label = "com.workgate.executor.source-cutover." + version
    payload = {
        "Label": label,
        "ProgramArguments": [
            str(python),
            str(script),
            "--tag",
            tag,
            "--install-root",
            str(checkout.parent.parent),
            "--executor-config",
            str(config),
        ],
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": str(directory / "cutover.log"),
        "StandardErrorPath": str(directory / "cutover.log"),
    }
    # A fixed per-release filename is an additional fail-closed duplicate
    # guard. Do not replace a previous handoff or follow a foreign symlink.
    write_private_bytes(plist, plistlib.dumps(payload), exclusive=True)
    code, _ = run_bounded_capture(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
        timeout=30,
    )
    if code:
        # A failed/uncertain launchd handoff must not automatically retry.
        raise RuntimeError("could not schedule macOS source cutover")
