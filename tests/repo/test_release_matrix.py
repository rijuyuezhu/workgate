"""Zero-install source-release gate checks require only the standard library."""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = (
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    "pyproject.toml",
    "src/workgate/__init__.py",
    "scripts/validation/check-release-matrix.py",
)


def _check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-S", "scripts/validation/check-release-matrix.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _isolated(tmp_path: Path) -> Path:
    for filename in REQUIRED:
        dest = tmp_path / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / filename, dest)
    return tmp_path


def test_source_release_matrix_is_valid():
    result = _check(ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_reject_frozen_release_job(tmp_path):
    root = _isolated(tmp_path)
    path = root / ".github/workflows/release.yml"
    text = path.read_text()
    path.write_text(text + "\n  build-binary:\n    runs-on: ubuntu-latest\n")
    result = _check(root)
    assert result.returncode != 0
    assert "frozen" in result.stderr


def test_reject_missing_windows_source_service_smoke(tmp_path):
    root = _isolated(tmp_path)
    path = root / ".github/workflows/ci.yml"
    text = path.read_text()
    assert "tests/repo/test_source_switch_windows.py" in text
    path.write_text(
        text.replace(
            "tests/repo/test_source_switch_windows.py",
            "tests/repo/test_unknown.py",
        )
    )
    result = _check(root)
    assert result.returncode != 0
    assert "test_source_switch_windows.py" in result.stderr


def test_reject_missing_native_arm64_dependency_wheel(tmp_path):
    root = _isolated(tmp_path)
    path = root / ".github/workflows/release.yml"
    text = path.read_text()
    assert "      - build-arm64-cryptography" in text
    path.write_text(text.replace("      - build-arm64-cryptography\n", "", 1))
    result = _check(root)
    assert result.returncode != 0
    assert "build-arm64-cryptography" in result.stderr


def test_reject_mismatched_package_version(tmp_path):
    root = _isolated(tmp_path)
    (root / "src/workgate/__init__.py").write_text('__version__ = "999.0.0"\n')
    result = _check(root)
    assert result.returncode != 0
    assert "version must match" in result.stderr
