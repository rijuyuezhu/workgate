"""Reject the obsolete postponed-annotations future import in first-party Python."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEARCH_ROOTS = ("src", "tests", "deploy", "scripts")
TARGET = "from __future__ import " + "annotations"


def main() -> int:
    violations: list[str] = []
    for root_name in SEARCH_ROOTS:
        root = ROOT / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if any(
                part.startswith(".venv")
                or part in {"node_modules", "python_modules", "site-packages"}
                for part in path.parts
            ):
                continue
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if line.strip() == TARGET:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{line_number}: {TARGET}"
                    )
    if violations:
        print(
            "Workgate targets Python 3.14; remove postponed-annotations imports:"
        )
        print("\n".join(violations))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
