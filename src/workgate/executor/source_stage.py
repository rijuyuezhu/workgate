"""Stage a trusted, published source release without changing a live Executor."""

import asyncio
import os
import re
import subprocess
from pathlib import Path

import httpx

from ..platform.process import noninteractive_creationflags, run_bounded_capture
from ..version import __version__
from .source_provenance import verified_source_checkout

_REPOSITORY = "https://github.com/rijuyuezhu/workgate.git"
_RELEASES_API = (
    "https://api.github.com/repos/rijuyuezhu/workgate/releases/tags/"
)
_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[abrc][0-9]+)?\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")


async def resolve_published_commit(version: str) -> str:
    """Pin the official published tag to one commit before any source build."""
    tag = f"v{version}"
    if _TAG.fullmatch(tag) is None:
        raise ValueError("invalid Workgate release version")
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.get(_RELEASES_API + tag)
        response.raise_for_status()
        release = response.json()
    if (
        not isinstance(release, dict)
        or release.get("tag_name") != tag
        or release.get("draft") is not False
        or not isinstance(release.get("published_at"), str)
        or not release["published_at"]
    ):
        raise RuntimeError(
            "required Workgate version is not a published release"
        )

    # GitHub Release target_commitish can be a floating branch; resolve the
    # actual official Git tag instead, including peeled annotated tags.
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "git",
            "ls-remote",
            "--tags",
            _REPOSITORY,
            f"refs/tags/{tag}",
            f"refs/tags/{tag}^{{}}",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=25,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        stdin=subprocess.DEVNULL,
        creationflags=noninteractive_creationflags(),
    )
    refs = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2 or _SHA.fullmatch(parts[0]) is None:
            raise RuntimeError("invalid official release tag response")
        if parts[1] in refs:
            raise RuntimeError("duplicate official release tag response")
        refs[parts[1]] = parts[0]
    if not refs or set(refs) - {f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"}:
        raise RuntimeError("official release tag is missing or ambiguous")
    commit = refs.get(f"refs/tags/{tag}^{{}}", refs.get(f"refs/tags/{tag}"))
    if commit is None:
        raise RuntimeError("official release tag is missing")
    return commit


async def prepare_managed_source(
    checkout: Path, *, version: str, system: str
) -> Path:
    """Reuse source prepare.py with a pinned commit; never activate services.

    Only call after the native service manager confirms its live SOURCE
    ownership. The caller remains responsible for draining and explicit
    native cutover in a later phase.
    """
    if version == __version__:
        raise ValueError(
            "source preparation must not modify the running version"
        )
    checkout = checkout.absolute()
    python = (
        checkout
        / ".venv"
        / ("Scripts/python.exe" if system == "Windows" else "bin/python")
    )
    verified = await asyncio.to_thread(
        verified_source_checkout, python, version=__version__, system=system
    )
    if verified != checkout:
        raise RuntimeError("not a verified managed source checkout")
    preparer = checkout / "deploy" / "source" / "prepare.py"
    if preparer.is_symlink() or not preparer.is_file():
        raise RuntimeError("managed release has no trusted source preparer")
    commit = await resolve_published_commit(version)
    root = checkout.parent.parent
    code, _output = await asyncio.to_thread(
        run_bounded_capture,
        [
            str(python),
            str(preparer),
            "--role",
            "executor",
            "--tag",
            f"v{version}",
            "--expected-commit",
            commit,
            "--install-root",
            str(root),
            "--no-activate-launcher",
        ],
        timeout=3600,
        cwd=checkout,
        include_stderr=True,
    )
    if code:
        raise RuntimeError(f"source preparation failed (exit {code})")
    staged = root / "versions" / commit
    if not staged.is_dir() or not (staged / ".venv").is_dir():
        raise RuntimeError(
            "pinned source preparation did not produce a runtime"
        )
    return staged
