"""Fail-closed checks for versioned source provenance (without native cutover)."""

import json
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from workgate import __version__
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.service import ExecutorServiceManager
from workgate.executor.source_provenance import verified_source_checkout
from workgate.protocol.executor import ExecutorRuntimeOwnership


def _git(path: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True
    ).strip()


@pytest.fixture
def managed_source(tmp_path: Path) -> tuple[Path, Path, Path]:
    # Creates a real local git release; no checkout/network/package install.
    root = tmp_path / "managed source"
    checkout = root / "versions" / ("0" * 40)
    checkout.mkdir(parents=True)
    _git(checkout, "init", "-q")
    _git(checkout, "config", "user.name", "Workgate Test")
    _git(checkout, "config", "user.email", "test@invalid.example")
    _git(
        checkout,
        "remote",
        "add",
        "origin",
        "https://github.com/rijuyuezhu/workgate.git",
    )
    (checkout / "pyproject.toml").write_text(
        f"[project]\nname='workgate'\nversion='{__version__}'\n"
    )
    (checkout / "uv.lock").write_text("version = 1\n")
    (checkout / ".gitignore").write_text(".venv/\n")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", "release")
    _git(checkout, "tag", f"v{__version__}")
    commit = _git(checkout, "rev-parse", "HEAD")
    checkout.rename(checkout.with_name(commit))
    checkout = root / "versions" / commit
    python = (
        checkout
        / ".venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    python.parent.mkdir(parents=True)
    python.write_bytes(b"not executed")

    bin_dir = root / "bin"
    bin_dir.mkdir()
    if os.name == "nt":
        relative = os.path.relpath(python, bin_dir)
        launcher = bin_dir / "workgate.cmd"
        launcher.write_text(
            "@echo off\nrem Managed by Workgate source preparation\n"
            f'"%~dp0{relative}" -m workgate.main %*\n'
        )
    else:
        launcher = bin_dir / "workgate"
        launcher.write_text(
            "#!/bin/sh\n# Managed by Workgate source preparation\n"
            f'exec {shlex.quote(str(python))} -m workgate.main "$@"\n'
        )
    (root / "install.json").write_text(
        json.dumps(
            {
                "role": "executor",
                "tag": f"v{__version__}",
                "fingerprint": "a" * 64,
            }
        )
    )
    return root, checkout, python


def _verify(python: Path) -> Path | None:
    return verified_source_checkout(
        python,
        version=__version__,
        system="Windows" if os.name == "nt" else "Linux",
    )


def test_pinned_managed_release_and_stale_initial_receipt(
    managed_source: tuple[Path, Path, Path],
) -> None:
    root, checkout, python = managed_source
    assert _verify(python) == checkout
    # Explicit source switches leave their original install receipt in place.
    receipt = root / "install.json"
    record = json.loads(receipt.read_text())
    record["tag"] = "v5.0.0a1"
    receipt.write_text(json.dumps(record))
    assert _verify(python) == checkout


@pytest.mark.parametrize(
    "fault",
    [
        "no-receipt",
        "invalid-receipt",
        "wrong-role",
        "malformed-fingerprint",
        "no-launcher",
        "modified-launcher",
        "wrong-origin",
        "dirty-checkout",
        "tag-mismatch",
        "version-mismatch",
        "sha-directory-mismatch",
        "missing-lock",
        "symlink-lock",
        "symlink-project",
        "external-python",
    ],
)
def test_managed_source_rejects_invalid_provenance(
    managed_source: tuple[Path, Path, Path], fault: str
) -> None:
    root, checkout, python = managed_source
    receipt = root / "install.json"
    launcher = (
        root / "bin" / ("workgate.cmd" if os.name == "nt" else "workgate")
    )
    if fault == "no-receipt":
        receipt.unlink()
    elif fault == "invalid-receipt":
        receipt.write_text("{broken")
    elif fault in {"wrong-role", "malformed-fingerprint"}:
        record = json.loads(receipt.read_text())
        record["role" if fault == "wrong-role" else "fingerprint"] = (
            "control" if fault == "wrong-role" else "bad"
        )
        receipt.write_text(json.dumps(record))
    elif fault == "no-launcher":
        launcher.unlink()
    elif fault == "modified-launcher":
        launcher.write_text(
            launcher.read_text().replace("workgate.main", "other.main")
        )
    elif fault == "wrong-origin":
        _git(
            checkout,
            "remote",
            "set-url",
            "origin",
            "https://example.invalid/wg",
        )
    elif fault == "dirty-checkout":
        (checkout / "pyproject.toml").write_text("changed")
    elif fault == "tag-mismatch":
        _git(checkout, "tag", "-d", f"v{__version__}")
    elif fault == "version-mismatch":
        return_value = verified_source_checkout(
            python,
            version="8.8.8",
            system="Windows" if os.name == "nt" else "Linux",
        )
        assert return_value is None
        return
    elif fault == "sha-directory-mismatch":
        python = (
            root
            / "versions"
            / ("f" * 40)
            / ".venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        python.parent.mkdir(parents=True)
        python.write_bytes(b"not executed")
    elif fault == "missing-lock":
        (checkout / "uv.lock").unlink()
    elif fault in {"symlink-lock", "symlink-project"}:
        path = checkout / (
            "uv.lock" if fault == "symlink-lock" else "pyproject.toml"
        )
        destination = root / path.name
        destination.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(destination)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows symlink creation requires developer mode")
            raise
    elif fault == "external-python":
        python = (
            root
            / "user code"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        python.parent.mkdir(parents=True)
        python.write_bytes(b"not executed")
    assert _verify(python) is None


def test_short_executable_path_fails_closed() -> None:
    assert (
        verified_source_checkout(
            Path("/python") if os.name != "nt" else Path("C:/python.exe"),
            version=__version__,
            system="Windows" if os.name == "nt" else "Linux",
        )
        is None
    )


@pytest.mark.parametrize("target", ["install.json", "launcher", "versions"])
def test_managed_source_rejects_symlinks(
    managed_source: tuple[Path, Path, Path], target: str, tmp_path: Path
) -> None:
    root, _checkout, python = managed_source
    if target == "install.json":
        path = root / "install.json"
        destination = tmp_path / "receipt.json"
        destination.write_bytes(path.read_bytes())
    elif target == "launcher":
        path = (
            root / "bin" / ("workgate.cmd" if os.name == "nt" else "workgate")
        )
        destination = tmp_path / path.name
        destination.write_bytes(path.read_bytes())
    else:
        path = root / "versions"
        destination = tmp_path / "moved versions"
        path.rename(destination)
    if target != "versions":
        path.unlink()
    try:
        path.symlink_to(destination, target_is_directory=target == "versions")
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink creation requires developer mode")
        raise
    assert _verify(python) is None


@pytest.mark.parametrize(
    ("running", "current", "ownership", "approved"),
    [
        (True, True, ExecutorRuntimeOwnership.SOURCE, True),
        (False, True, ExecutorRuntimeOwnership.SOURCE, False),
        (True, False, ExecutorRuntimeOwnership.SOURCE, False),
        (True, True, ExecutorRuntimeOwnership.EXTERNAL_PACKAGE, False),
        (True, True, ExecutorRuntimeOwnership.CONTROL_MANAGED, False),
        (True, True, None, False),
    ],
)
def test_native_service_must_be_unchanged_and_owned(
    managed_source: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    running: bool,
    current: bool,
    ownership: ExecutorRuntimeOwnership | None,
    approved: bool,
) -> None:
    _root, checkout, python = managed_source
    config = resolve_executor_config(
        Settings(default_workdir=checkout, state_dir=checkout / "state")
    )
    manager = ExecutorServiceManager(
        config,
        system="Windows" if os.name == "nt" else "Linux",
        executable=python,
    )
    monkeypatch.setattr(
        manager,
        "status",
        lambda: SimpleNamespace(
            running=running,
            runtime_current=current,
            runtime_ownership=ownership,
        ),
    )
    assert (manager.verified_source_checkout() == checkout) is approved
