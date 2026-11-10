"""Filesystem-anchored bootstrap for the bundled durable job runner."""

import contextlib
import json
import os
import sys
import traceback
from pathlib import Path


def main() -> None:
    """Import the runner from the package tree containing this bootstrap."""
    runtime_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(runtime_root))
    try:
        from workgate.executor.jobs.runner import main as runner_main

        runner_main()
    except Exception as exc:
        # Keep import/runner failure evidence independent of package imports.
        # Exception messages and source lines may contain user secrets.
        with contextlib.suppress(OSError, ValueError, IndexError):
            index = sys.argv.index("--status-file")
            status = Path(sys.argv[index + 1])
            if status.suffix == ".json" and not status.exists():
                evidence = {
                    "exception_type": type(exc).__name__[:128],
                    "frames": [
                        {
                            "module": Path(frame.filename).name[:128],
                            "function": frame.name[:128],
                            "line": frame.lineno,
                        }
                        for frame in traceback.extract_tb(
                            exc.__traceback__, limit=-20
                        )
                    ],
                }
                path = status.with_suffix(".runner-error.json")
                descriptor = os.open(
                    path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(evidence, handle)
        raise


if __name__ == "__main__":
    main()
