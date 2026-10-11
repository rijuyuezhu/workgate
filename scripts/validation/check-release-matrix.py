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
    validation = _section(release, "validate-release")
    package = _section(release, "build-python-package")
    binary = _section(release, "build-binary")
    publish = _section(release, "github-release")
    smoke = _section(ci, "package-smoke")
    binary_smoke = _section(ci, "standalone-binary-smoke")
    expected = EXPECTED_BINARY_ARTIFACTS
    actual = set(
        re.findall(r"^          - artifact: ([\w-]+)$", binary, re.MULTILINE)
    )
    if actual != expected:
        raise SystemExit(
            f"standalone release artifact matrix mismatch: {sorted(actual)}"
        )
    ci_artifacts = set(
        re.findall(
            r"^          - artifact: ([\w-]+)$", binary_smoke, re.MULTILINE
        )
    )
    if ci_artifacts != expected:
        raise SystemExit(
            f"standalone CI artifact matrix mismatch: {sorted(ci_artifacts)}"
        )
    if "windows-" in binary or "windows-" in binary_smoke:
        raise SystemExit("standalone binary jobs must not include Windows")
    for job, section in (("release", binary), ("CI", binary_smoke)):
        fields = set(
            re.findall(r"^          - ([a-z_]+):", section, re.MULTILINE)
        )
        fields.update(
            re.findall(r"^            ([a-z_]+):", section, re.MULTILINE)
        )
        references = set(re.findall(r"matrix\.([a-z_]+)", section))
        if undefined := references - fields:
            raise SystemExit(
                f"undefined standalone {job} matrix fields: {sorted(undefined)}"
            )
    if (
        "cp dist/workgate release/workgate-${{ matrix.artifact }}/"
        not in binary
    ):
        raise SystemExit(
            "release must package the built dist/workgate executable"
        )
    required = (
        (
            validation,
            ("python scripts/validation/check-release-provenance.py",),
        ),
        (package, ("uv build", "check-python-dist.py", "name: python-dist")),
        (
            binary,
            (
                "bash scripts/release/build-binary.sh",
                "python scripts/release/smoke-binary.py dist/workgate",
                "name: binary-",
            ),
        ),
        (
            binary_smoke,
            (
                "bash scripts/release/build-binary.sh",
                "python scripts/release/smoke-binary.py dist/workgate",
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
                "actions/checkout@v6",
                "python scripts/validation/check-release-provenance.py",
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
    builder = (ROOT / "scripts/release/build-binary.sh").read_text()
    for fragment in (
        "--clean --noconfirm --onefile --name workgate",
        "--collect-all workgate",
        "scripts/release/pyinstaller-entry.py",
    ):
        if fragment not in builder:
            raise SystemExit(
                f"missing shared PyInstaller build contract: {fragment}"
            )
    print(
        "Release matrix: universal Python wheel + sdist and four standalone binaries"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
