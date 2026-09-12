"""Filesystem-anchored bootstrap for the bundled durable job runner."""

import sys
from pathlib import Path


def main() -> None:
    """Import the runner from the package tree containing this bootstrap."""
    runtime_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(runtime_root))
    from workgate.executor.jobs.runner import main as runner_main

    runner_main()


if __name__ == "__main__":
    main()
