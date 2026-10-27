"""Launch the existing Linux source switch outside the Executor service cgroup."""

import os
import sys
from pathlib import Path

from ..platform.process import run_bounded_capture
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
    if any(key.startswith("WORKGATE_") for key in os.environ):
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
