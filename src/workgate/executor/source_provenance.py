"""Read-only proof that a source runtime uses Workgate's managed release layout."""

import json
import os
import re
import shlex
import subprocess
import tomllib
from pathlib import Path

_RELEASE_COMMIT = re.compile(r"[0-9a-f]{40}")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_RELEASE_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[abrc][0-9]+)?")
_REPOSITORY = "https://github.com/rijuyuezhu/workgate.git"


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def verified_source_checkout(
    executable: Path, *, version: str, system: str
) -> Path | None:
    """Recognize a pinned source release, not an arbitrary source checkout.

    This is only a filesystem/release proof. The caller must *also* verify the
    live native service's owned action before granting update authority.
    """
    if system not in {"Linux", "Windows"} or not executable.is_absolute():
        return None
    windows = system == "Windows"
    path = executable.absolute()
    if len(path.parents) < 3:
        return None
    checkout = path.parents[2]
    versions = checkout.parent
    root = versions.parent
    expected = (
        checkout / ".venv" / ("Scripts/python.exe" if windows else "bin/python")
    )
    if (
        path != expected
        or not _RELEASE_COMMIT.fullmatch(checkout.name)
        or versions.name != "versions"
        or any(
            entry.is_symlink() or not entry.is_dir()
            for entry in (
                root,
                versions,
                checkout,
                checkout / ".git",
                checkout / ".venv",
                path.parent,
            )
        )
        or not path.is_file()
        or (checkout / "uv.lock").is_symlink()
        or not (checkout / "uv.lock").is_file()
        or (checkout / "pyproject.toml").is_symlink()
        or not (checkout / "pyproject.toml").is_file()
    ):
        return None

    launcher = root / "bin" / ("workgate.cmd" if windows else "workgate")
    receipt = root / "install.json"
    if (
        launcher.is_symlink()
        or not launcher.is_file()
        or launcher.parent.is_symlink()
        or receipt.is_symlink()
    ):
        return None
    try:
        if receipt.exists():
            if not receipt.is_file():
                return None
            record = json.loads(receipt.read_text(encoding="utf-8"))
            if (
                not isinstance(record, dict)
                or set(record) != {"role", "tag", "fingerprint"}
                or record["role"] != "executor"
                or not isinstance(record["tag"], str)
                or _RELEASE_TAG.fullmatch(record["tag"]) is None
                or not isinstance(record["fingerprint"], str)
                or _FINGERPRINT.fullmatch(record["fingerprint"]) is None
            ):
                return None
        # Explicit legacy Executor adoption also creates the managed launcher
        # and owned native service, but does not create an install receipt.
        # Any receipt that *is* present must be valid; its initial tag may be
        # older than the current release after an explicit version switch.
        if windows:
            relative = os.path.relpath(path, launcher.parent)
            expected_lines = [
                "@echo off",
                "rem Managed by Workgate source preparation",
                f'"%~dp0{relative}" -m workgate.main %*',
            ]
        else:
            expected_lines = [
                "#!/bin/sh",
                "# Managed by Workgate source preparation",
                f'exec {shlex.quote(str(path))} -m workgate.main "$@"',
            ]
        if launcher.read_text(encoding="utf-8").splitlines() != expected_lines:
            return None
        with (checkout / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]
        if (
            project.get("name") != "workgate"
            or project.get("version") != version
        ):
            return None
        tag = f"v{version}"
        if _RELEASE_TAG.fullmatch(tag) is None:
            return None
        if (
            _git(checkout, "remote", "get-url", "origin") != _REPOSITORY
            or _git(checkout, "rev-parse", "HEAD") != checkout.name
            or _git(
                checkout, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"
            )
            != checkout.name
            or _git(
                checkout, "status", "--porcelain", "--untracked-files=normal"
            )
        ):
            return None
    except OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError:
        return None
    return checkout
