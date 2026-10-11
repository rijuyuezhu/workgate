import argparse
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the real Chromium Human UI end-to-end scenario."
    )
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=Path("browser-artifacts"),
        help="Directory for traces, screenshots, video, browser logs, and process logs.",
    )
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["WORKGATE_BROWSER_E2E"] = "1"
    env["WORKGATE_BROWSER_ARTIFACTS"] = str(artifacts)
    if "PLAYWRIGHT_BROWSERS_PATH" not in env:
        cache_home = Path(
            env.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
        ).expanduser()
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(cache_home / "ms-playwright")
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-m",
        "browser",
        "tests/e2e/browser/test_human_ui_e2e.py",
    ]
    return subprocess.run(command, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
