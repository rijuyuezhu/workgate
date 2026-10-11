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
