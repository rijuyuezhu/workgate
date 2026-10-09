#!/usr/bin/env python3
"""Check universal Python distribution and standalone binary release contracts."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_BINARY_ARTIFACTS = {
    "linux-x86_64",
    "linux-aarch64",
    "macos-x86_64",
    "macos-aarch64",
    "windows-x86_64",
}


def _section(text: str, name: str) -> str:
    match = re.search(
        rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise SystemExit(f"missing workflow job: {name}")
    return match.group(1)


def main() -> int:
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    release = (ROOT / ".github/workflows/release.yml").read_text()
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project_python = tomllib.load(stream)["project"][
            "requires-python"
        ].removeprefix(">=")
    if f'  PYTHON_VERSION: "{project_python}"' not in release:
        raise SystemExit("release Python version must match project minimum")
    package = _section(release, "build-python-package")
    binary = _section(release, "build-binary")
    publish = _section(release, "github-release")
    smoke = _section(ci, "package-smoke")
    expected = EXPECTED_BINARY_ARTIFACTS
    actual = set(
        re.findall(r"^          - artifact: ([\w-]+)$", binary, re.MULTILINE)
    )
    if actual != expected:
        raise SystemExit(
            f"standalone artifact matrix mismatch: {sorted(actual)}"
        )
    required = (
        (package, ("uv build", "check-python-dist.py", "name: python-dist")),
        (
            binary,
            (
                "scripts/release/pyinstaller-entry.py",
                "uv run --extra gui --with pyinstaller",
                "name: binary-",
            ),
        ),
        (
            smoke,
            (
                "uv build",
                "check-python-dist.py",
                "ui-terminal/package-lock.json",
                "uv build --wheel",
            ),
        ),
        (
            publish,
            (
                "- build-python-package",
                "- build-binary",
                "name: python-dist",
                "pattern: binary-*",
                "files: dist/*",
            ),
        ),
    )
    for section, fragments in required:
        for fragment in fragments:
            if fragment not in section:
                raise SystemExit(
                    f"missing universal release contract: {fragment}"
                )
    print(
        "Release matrix: one universal Python wheel + sdist and five standalone binaries"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
