"""Zero-install distribution/release check remains usable without project deps."""

import subprocess
import sys
from pathlib import Path


def test_release_matrix_requires_universal_package_and_binary_matrix():
    completed = subprocess.run(
        [sys.executable, "-S", "scripts/validation/check-release-matrix.py"],
        cwd=Path(__file__).parents[2],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_release_matrix_rejects_stale_binary_packaging(tmp_path):
    """Fail the build when Release references a deleted matrix key or binary."""
    import shutil

    root = Path(__file__).parents[2]
    paths = (
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
        "pyproject.toml",
        "scripts/release/build-binary.sh",
        "scripts/release/build-binary.ps1",
        "scripts/release/package-windows.ps1",
        "scripts/validation/check-release-matrix.py",
    )
    for relative in paths:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, destination)

    release_path = tmp_path / ".github/workflows/release.yml"
    original = release_path.read_text()
    correct = "cp dist/workgate release/workgate-${{ matrix.artifact }}/"
    for broken in (
        "cp dist/${{ matrix.binary }} release/workgate-${{ matrix.artifact }}/",
        "cp dist/unknown release/workgate-${{ matrix.artifact }}/",
    ):
        release_path.write_text(original.replace(correct, broken))
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                "scripts/validation/check-release-matrix.py",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode != 0
        assert "release" in completed.stderr


def test_release_matrix_rejects_mismatched_package_version(tmp_path):
    """A release must not silently differ from the importable package version."""
    import shutil

    root = Path(__file__).parents[2]
    paths = (
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
        "pyproject.toml",
        "src/workgate/__init__.py",
        "scripts/release/build-binary.ps1",
        "scripts/release/package-windows.ps1",
        "scripts/validation/check-release-matrix.py",
    )
    for relative in paths:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, destination)
    runtime = tmp_path / "src/workgate/__init__.py"
    runtime.write_text('__version__ = "999.0.0"\n')
    completed = subprocess.run(
        [sys.executable, "-S", "scripts/validation/check-release-matrix.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "version must match" in completed.stderr


def test_release_matrix_rejects_missing_windows_binary(tmp_path):
    """Do not publish a release without the native Windows x86-64 asset."""
    import shutil

    root = Path(__file__).parents[2]
    for relative in (
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
        "pyproject.toml",
        "src/workgate/__init__.py",
        "scripts/release/build-binary.sh",
        "scripts/release/build-binary.ps1",
        "scripts/release/package-windows.ps1",
        "scripts/validation/check-release-matrix.py",
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, destination)
    workflow = tmp_path / ".github/workflows/release.yml"
    workflow.write_text(
        workflow.read_text().replace(
            "          - artifact: windows-x86_64\n            runner: windows-2022\n",
            "",
        )
    )
    completed = subprocess.run(
        [sys.executable, "-S", "scripts/validation/check-release-matrix.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "matrix mismatch" in completed.stderr


def test_release_includes_native_arm64_wheel_in_existing_asset_bundle():
    workflow = (
        Path(__file__).parents[2] / ".github/workflows/release.yml"
    ).read_text()
    producer = workflow.split("\n  build-arm64-cryptography:\n", 1)[1].split(
        "\n  github-release:\n", 1
    )[0]
    publisher = workflow.split("\n  github-release:\n", 1)[1]
    assert "runs-on: windows-11-arm" in producer
    assert "./scripts/release/build_arm64_crypto_wheel.ps1" in producer
    assert "name: arm64-cryptography" in producer
    assert "      - build-arm64-cryptography" in publisher
    assert "name: arm64-cryptography" in publisher
    assert "sha256sum > SHA256SUMS.txt" in publisher
    assert "files: dist/*" in publisher
