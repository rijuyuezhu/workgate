#!/usr/bin/env python3
"""Check source-only release artifacts and native installer CI contracts."""

import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _section(text: str, name: str) -> str:
    match = re.search(
        rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise SystemExit(f"missing workflow job: {name}")
    return match.group(1)


def _require(section: str, *fragments: str) -> None:
    for fragment in fragments:
        if fragment not in section:
            raise SystemExit(f"missing source release contract: {fragment}")


def main() -> int:
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    release = (ROOT / ".github/workflows/release.yml").read_text()
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    project_python = project["requires-python"].removeprefix(">=")
    version = project["version"]
    runtime = ast.parse((ROOT / "src/workgate/__init__.py").read_text())
    versions = [
        assignment.value.value
        for assignment in runtime.body
        if isinstance(assignment, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in assignment.targets
        )
        and isinstance(assignment.value, ast.Constant)
        and isinstance(assignment.value.value, str)
    ]
    if versions != [version]:
        raise SystemExit("pyproject version must match workgate.__version__")
    if f'  PYTHON_VERSION: "{project_python}"' not in release:
        raise SystemExit("release Python version must match project minimum")

    validation = _section(release, "validate-release")
    package = _section(release, "build-python-package")
    arm64 = _section(release, "build-arm64-cryptography")
    publish = _section(release, "github-release")
    windows = _section(ci, "windows-native")
    topology = _section(ci, "topology")
    smoke = _section(ci, "package-smoke")
    gate = _section(ci, "ci-gate")

    if re.search(r"^  (?:build-binary|binary-smoke):", release + ci, re.M):
        raise SystemExit("frozen executable build jobs must not be published")
    for fragment in (
        "scripts/release/build-binary.",
        "scripts/release/package-windows.ps1",
        "scripts/release/smoke-binary.py",
        "pattern: binary-*",
        "- build-binary",
        "- binary-smoke",
        "self-contained executable",
        "workgate-${{ matrix.artifact }}",
    ):
        if fragment in release + ci:
            raise SystemExit(f"obsolete frozen release reference: {fragment}")

    _require(
        validation, "python scripts/validation/check-release-provenance.py"
    )
    _require(package, "uv build", "check-python-dist.py", "name: python-dist")
    _require(
        arm64,
        "runs-on: windows-11-arm",
        "build_arm64_crypto_wheel.ps1",
        "name: arm64-cryptography",
    )
    _require(
        publish,
        "- build-python-package",
        "- build-arm64-cryptography",
        "name: python-dist",
        "name: arm64-cryptography",
        "sha256sum > SHA256SUMS.txt",
        "python scripts/validation/check-release-provenance.py",
        "files: dist/*",
    )
    _require(
        windows,
        "runs-on: windows-2022",
        "python-version: '3.14'",
        "scripts/validation/windows-native-ci.py",
        "tests/repo/test_source_bootstrap.py",
        "tests/repo/test_source_switch_windows.py",
    )
    _require(
        topology,
        "pytest -q -m topology",
        "test_source_control_native_service.py",
        "test_source_switch.py",
    )
    _require(
        smoke,
        "uv build",
        "check-python-dist.py",
        "uv build --wheel",
        "ui-terminal/package-lock.json",
    )
    _require(
        gate,
        "- windows-native",
        "- topology",
        "- package-smoke",
        "- release-matrix",
    )
    print(
        "Release matrix: uv-managed source + universal wheel/sdist + ARM64 dependency wheel"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
