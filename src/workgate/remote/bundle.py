"""Build and serve the trimmed uv-managed runtime used by remote workers."""

import fnmatch
import functools
import gzip
import hashlib
import tarfile
from collections.abc import Iterable
from io import BytesIO
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import __version__
from .constants import REMOTE_WORKER_BUNDLE_PATH

# Keep the worker bundle source-trimmed. These patterns are a worker-runtime
# manifest, not a general workgate package snapshot. Project metadata is added
# separately so uv can install only the locked worker dependency group.
_WORKER_BUNDLE_INCLUDE_PATTERNS = (
    "__init__.py",
    "app_paths.py",
    "audit/*.py",
    "errors.py",
    "version.py",
    "agent_bridge/__init__.py",
    "agent_bridge/models.py",
    "agent_bridge/redaction.py",
    "agent_bridge/skills.py",
    "agent_bridge/sources.py",
    "agent_bridge/state.py",
    "config/*.py",
    "composition/*.py",
    "ops/__init__.py",
    "executor/__init__.py",
    "executor/config.py",
    "executor/runtime.py",
    "executor/search_composition.py",
    "ops/agent.py",
    "ops/files.py",
    "ops/files_service.py",
    "jobs/__init__.py",
    "jobs/lifecycle.py",
    "jobs/managed.py",
    "jobs/reconciliation.py",
    "jobs/persistence.py",
    "jobs/recovery.py",
    "jobs/runner.py",
    "jobs/runner_bootstrap.py",
    "jobs/runtime.py",
    "jobs/shell.py",
    "jobs/state.py",
    "ops/patch/*.py",
    "ops/read.py",
    "ops/todo.py",
    "ops/search/*.py",
    "ops/secret_scan.py",
    "ops/session.py",
    "tool_session/environment.py",
    "ops/bash.py",
    "ops/shell.py",
    "ops/transfer.py",
    "ops/utils/*.py",
    "persistence/*.py",
    "remote/__init__.py",
    "remote/constants.py",
    "remote/tool_specs.py",
    "remote_worker/*.py",
    "remote_worker/**/*.py",
    "schemas/__init__.py",
    "schemas/input_models/__init__.py",
    "schemas/input_models/files.py",
    "schemas/result_models/*.py",
    "telemetry/*.py",
    "terminal/*.py",
    "tool_session/*.py",
    "ui/__init__.py",
    "ui/contracts.py",
    "ui/dashboard.py",
    "utils/*.py",
)
_WORKER_BUNDLE_EXCLUDE_PATTERNS = (
    "**/__pycache__/**",
    "**/*.pyc",
    "**/*.pyo",
    "config/cli.py",
)


def _matches_any(path: str, patterns: Iterable[str]) -> bool:
    """Return whether a POSIX relative path matches any manifest pattern."""
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _should_include_worker_file(path: Path, package_root: Path) -> bool:
    """Return whether one package file belongs in the worker bundle."""
    if path.suffix != ".py" or not path.is_file():
        return False
    relative = path.relative_to(package_root).as_posix()
    if _matches_any(relative, _WORKER_BUNDLE_EXCLUDE_PATTERNS):
        return False
    return _matches_any(relative, _WORKER_BUNDLE_INCLUDE_PATTERNS)


def _worker_bundle_paths(package_root: Path) -> list[Path]:
    """Return Python files selected by the worker-runtime manifest."""
    return sorted(
        path
        for path in package_root.rglob("*.py")
        if _should_include_worker_file(path, package_root)
    )


def _normalized_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Remove host-specific archive metadata for a stable bundle digest."""
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.mode = 0o644
    return info


@functools.lru_cache(maxsize=1)
def worker_bundle_bytes() -> bytes:
    """Return one deterministic worker runtime archive with locked metadata."""
    package_root = Path(__file__).resolve().parents[1]
    project_root = package_root.parents[1]
    buffer = BytesIO()
    with (
        gzip.GzipFile(
            fileobj=buffer, mode="wb", filename="", mtime=0
        ) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as tar,
    ):
        for path in _worker_bundle_paths(package_root):
            tar.add(
                path,
                arcname=path.relative_to(package_root.parent).as_posix(),
                recursive=False,
                filter=_normalized_tar_info,
            )
        for name in ("pyproject.toml", "uv.lock"):
            path = project_root / name
            tar.add(
                path,
                arcname=name,
                recursive=False,
                filter=_normalized_tar_info,
            )
    return buffer.getvalue()


@functools.lru_cache(maxsize=1)
def worker_bundle_manifest() -> dict[str, Any]:
    """Return the authoritative version, digest, size, and cache-busted URL."""
    payload = worker_bundle_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "schema_version": 1,
        "bundle_version": __version__,
        "sha256": digest,
        "size": len(payload),
        "url": f"{REMOTE_WORKER_BUNDLE_PATH}?sha256={digest}",
    }


async def worker_bundle(request: Request) -> Response:
    """Serve the worker manifest or its digest-qualified runtime archive."""
    headers = {"Cache-Control": "no-store"}
    manifest = worker_bundle_manifest()
    if request.query_params.get("manifest") == "1":
        return JSONResponse(manifest, headers=headers)
    requested_digest = request.query_params.get("sha256")
    if requested_digest and requested_digest != manifest["sha256"]:
        return JSONResponse(
            {"error": "worker bundle digest is no longer available"},
            status_code=404,
            headers=headers,
        )
    return Response(
        worker_bundle_bytes(),
        media_type="application/gzip",
        headers=headers,
    )
