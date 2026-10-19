"""Filesystem-anchored bootstrap for the bundled durable job runner."""

import contextlib
import os
import sys
import traceback
from pathlib import Path


def main() -> None:
    """Import the runner from the package tree containing this bootstrap."""
    runtime_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(runtime_root))
    try:
        from workgate.executor.jobs.runner import main as runner_main

        runner_main()
    except Exception:
        # Preserve errors even if the WorkGate package failed to import.
        # The attempt directory already exists; do not rely on package helpers.
        argv = sys.argv
        if "--status-file" in argv:
            index = argv.index("--status-file")
            if index + 1 < len(argv):
                path = Path(argv[index + 1]).with_suffix(".runner-stderr.log")
                with contextlib.suppress(OSError):
                    descriptor = os.open(
                        path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
                    )
                    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                        handle.write(traceback.format_exc()[-8192:])
        raise


if __name__ == "__main__":
    main()
