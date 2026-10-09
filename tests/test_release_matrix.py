"""Zero-install distribution/release check remains usable without project deps."""

import subprocess
import sys
from pathlib import Path


def test_release_matrix_requires_universal_package_and_binary_matrix():
    completed = subprocess.run(
        [sys.executable, "-S", "scripts/validation/check-release-matrix.py"],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
