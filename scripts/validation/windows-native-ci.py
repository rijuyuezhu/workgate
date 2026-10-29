"""Run native Windows contracts and real source-service integration smoke."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run(*args: str) -> None:
    print(f"\n=== {' '.join(args)} ===", flush=True)
    subprocess.run((sys.executable, *args), cwd=ROOT, check=True)


def main() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Native Windows Python required")
    run("-c", "import winpty, uiautomation")
    modules = sorted(
        p
        for folder in ("contracts", "executor")
        for p in (ROOT / "tests" / folder).glob("test_windows_*.py")
    )
    if not modules:
        raise RuntimeError("No native Windows test modules found")
    run("-m", "pytest", "-q", *(str(p) for p in modules))

    # These integration smokes require deliberate ordering and/or setup.
    # Do not run every windows-*-smoke.py by glob: some need special hardware.
    for name, args in (
        ("gui-task", ("--ci",)),
        ("conpty-transport", ()),
        ("user-service", ()),
        ("pairing-approval", ()),
        ("paired-service", ()),
        ("supervisor", ()),
    ):
        run(
            str(ROOT / "scripts" / "validation" / f"windows-{name}-smoke.py"),
            *args,
        )

    run("-m", "pytest", "-q", "tests/e2e/test_deployment_topology.py")


if __name__ == "__main__":
    main()
