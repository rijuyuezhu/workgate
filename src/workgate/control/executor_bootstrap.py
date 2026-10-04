"""Public fresh-machine executor bootstrap routes."""

import asyncio
import re
import shlex
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route

from .. import __version__

EXECUTOR_BOOTSTRAP_PATH = "/executor/v1/bootstrap"
EXECUTOR_BOOTSTRAP_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_CHECKSUM_MAX_BYTES = 4 * 1024
_RELEASE_REPOSITORY = "rijuyuezhu/workgate"
_SUPPORTED_TARGETS = frozenset(
    {
        "linux-x86_64",
        "linux-aarch64",
        "macos-x86_64",
        "macos-aarch64",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ExecutorBootstrapUnavailable(RuntimeError):
    """Raised when this runtime cannot safely distribute a matching executor."""


def _release_tag() -> str:
    return f"v{__version__}"


def _archive_name(target: str) -> str:
    if target not in _SUPPORTED_TARGETS:
        raise ValueError(f"unsupported executor bootstrap target: {target}")
    return f"workgate-{target}.tar.gz"


def _release_asset_url(target: str, *, checksum: bool = False) -> str:
    archive_name = _archive_name(target)
    name = f"workgate-{target}.sha256" if checksum else archive_name
    return (
        f"https://github.com/{_RELEASE_REPOSITORY}/releases/download/"
        f"{_release_tag()}/{name}"
    )


def _source_checkout_root() -> Path | None:
    """Return the Workgate source checkout containing this module, if any."""
    module = Path(__file__).resolve()
    relative = Path("src/workgate/control/executor_bootstrap.py")
    for parent in module.parents:
        candidate = parent / relative
        if (parent / ".git").exists() and candidate.resolve() == module:
            return parent
    return None


def _ensure_release_matches_runtime() -> None:
    """Fail closed when a source checkout is not the release it would serve."""
    root = _source_checkout_root()
    if root is None:
        return
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        release = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                f"{_release_tag()}^{{commit}}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExecutorBootstrapUnavailable(
            "executor bootstrap is unavailable because this source checkout "
            "cannot verify its matching release tag"
        ) from exc
    if head != release:
        raise ExecutorBootstrapUnavailable(
            "executor bootstrap is unavailable from an untagged development "
            f"checkout; expected {_release_tag()}"
        )
    try:
        source_status = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                "src/workgate",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExecutorBootstrapUnavailable(
            "executor bootstrap is unavailable because this source checkout "
            "cannot verify its release contents"
        ) from exc
    if source_status:
        raise ExecutorBootstrapUnavailable(
            "executor bootstrap is unavailable from a modified release checkout"
        )


def _parse_checksum(payload: str, target: str) -> str:
    archive_name = _archive_name(target)
    fields = payload.strip().split()
    if len(fields) != 2:
        raise ExecutorBootstrapUnavailable(
            f"executor bootstrap checksum is invalid for {target}"
        )
    digest, name = fields
    digest = digest.lower()
    if name.lstrip("*") != archive_name or _SHA256_RE.fullmatch(digest) is None:
        raise ExecutorBootstrapUnavailable(
            f"executor bootstrap checksum is invalid for {target}"
        )
    return digest


async def _fetch_checksum(target: str) -> str:
    payload = bytearray()
    async with (
        httpx.AsyncClient(follow_redirects=True) as client,
        client.stream(
            "GET",
            _release_asset_url(target, checksum=True),
            timeout=30.0,
        ) as response,
    ):
        if response.status_code != 200:
            raise ExecutorBootstrapUnavailable(
                f"executor bootstrap checksum is unavailable for {target}"
            )
        async for chunk in response.aiter_bytes():
            payload.extend(chunk)
            if len(payload) > _CHECKSUM_MAX_BYTES:
                raise ExecutorBootstrapUnavailable(
                    f"executor bootstrap checksum is too large for {target}"
                )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExecutorBootstrapUnavailable(
            f"executor bootstrap checksum is invalid for {target}"
        ) from exc
    return _parse_checksum(text, target)


async def _archive_stream(target: str) -> AsyncIterator[bytes]:
    total = 0
    async with (
        httpx.AsyncClient(follow_redirects=True) as client,
        client.stream(
            "GET",
            _release_asset_url(target),
            timeout=httpx.Timeout(60.0, read=120.0),
        ) as response,
    ):
        if response.status_code != 200:
            raise ExecutorBootstrapUnavailable(
                f"executor bootstrap runtime is unavailable for {target}"
            )
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > EXECUTOR_BOOTSTRAP_MAX_ARCHIVE_BYTES:
                raise ExecutorBootstrapUnavailable(
                    "executor bootstrap runtime exceeds the size limit "
                    f"for {target}"
                )
            yield chunk


def bootstrap_script(base_url: str) -> str:
    """Return the bootstrap script that composes the existing executor CLI."""
    control = base_url.rstrip("/")
    artifact_root = control + EXECUTOR_BOOTSTRAP_PATH
    template = r"""#!/usr/bin/env bash
set -euo pipefail
umask 077

CONTROL=__CONTROL__
ARTIFACT_ROOT=__ARTIFACT_ROOT__
VERSION=__VERSION__
MAX_ARCHIVE_BYTES=__MAX_ARCHIVE_BYTES__
NAME=""
DEFAULT_WORKDIR=""
PERSIST=0
RUNTIME_PUBLISHED=0
RUNTIME_HAD_PREVIOUS=0
RUNTIME_DIR=""
PREVIOUS=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --name) NAME="${2:-}"; shift 2 ;;
    --default-workdir) DEFAULT_WORKDIR="${2:-}"; shift 2 ;;
    --persist) PERSIST=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required" >&2
  exit 2
fi
if ! command -v tar >/dev/null 2>&1; then
  echo "tar is required" >&2
  exit 2
fi

case "$(uname -s):$(uname -m)" in
  Linux:x86_64|Linux:amd64) TARGET=linux-x86_64 ;;
  Linux:aarch64|Linux:arm64) TARGET=linux-aarch64 ;;
  Darwin:x86_64|Darwin:amd64) TARGET=macos-x86_64 ;;
  Darwin:arm64|Darwin:aarch64) TARGET=macos-aarch64 ;;
  *) echo "unsupported executor platform: $(uname -s) $(uname -m)" >&2; exit 2 ;;
esac

CHECKSUM_URL="$ARTIFACT_ROOT/$TARGET/sha256"
ARCHIVE_URL="$ARTIFACT_ROOT/$TARGET/archive"

TMPDIR="$(mktemp -d)"
cleanup() {
  status=$?
  if [ "$status" -ne 0 ] && [ "$RUNTIME_PUBLISHED" = "1" ]; then
    rm -f "$RUNTIME_DIR/workgate"
    if [ "$RUNTIME_HAD_PREVIOUS" = "1" ] && [ -f "$PREVIOUS" ]; then
      mv -f "$PREVIOUS" "$RUNTIME_DIR/workgate"
    fi
  fi
  rm -f "$PREVIOUS"
  rm -rf "$TMPDIR"
  exit "$status"
}
trap cleanup EXIT

CHECKSUM_TEXT="$(curl -fsSL --max-filesize 4096 "$CHECKSUM_URL")"
EXPECTED_SHA256="$(printf '%s\n' "$CHECKSUM_TEXT" | awk '{print $1}')"
EXPECTED_NAME="$(printf '%s\n' "$CHECKSUM_TEXT" | awk '{print $2}')"
EXPECTED_NAME="${EXPECTED_NAME#\*}"
if [[ ! "$EXPECTED_SHA256" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "invalid executor runtime checksum" >&2
  exit 1
fi
EXPECTED_SHA256="$(printf '%s' "$EXPECTED_SHA256" | tr 'A-F' 'a-f')"
if [ "$EXPECTED_NAME" != "workgate-$TARGET.tar.gz" ]; then
  echo "executor runtime checksum names the wrong archive" >&2
  exit 1
fi

ARCHIVE="$TMPDIR/workgate.tar.gz"
curl -fL --progress-bar --max-filesize "$MAX_ARCHIVE_BYTES" \
  "$ARCHIVE_URL" -o "$ARCHIVE"
ARCHIVE_BYTES="$(wc -c < "$ARCHIVE" | tr -d '[:space:]')"
if [ "$ARCHIVE_BYTES" -gt "$MAX_ARCHIVE_BYTES" ]; then
  echo "executor runtime archive exceeds the size limit" >&2
  exit 1
fi
if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL_SHA256="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
  ACTUAL_SHA256="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
else
  echo "sha256sum or shasum is required" >&2
  exit 2
fi
if [ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]; then
  echo "executor runtime checksum mismatch" >&2
  exit 1
fi

MEMBER="workgate-$TARGET/workgate"
STAGED="$TMPDIR/workgate"
tar -xOzf "$ARCHIVE" "$MEMBER" > "$STAGED"
chmod 700 "$STAGED"
if [ "$("$STAGED" --version)" != "workgate $VERSION" ]; then
  echo "executor runtime version mismatch" >&2
  exit 1
fi

RUNTIME="$STAGED"
if [ "$PERSIST" = "1" ]; then
  if [ "$(uname -s)" = "Darwin" ]; then
    RUNTIME_DIR="$HOME/Library/Application Support/workgate/executor-bootstrap"
  else
    RUNTIME_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/workgate/executor-bootstrap"
  fi
  mkdir -p "$RUNTIME_DIR"
  chmod 700 "$RUNTIME_DIR"
  NEXT="$RUNTIME_DIR/.workgate.next.$$"
  PREVIOUS="$RUNTIME_DIR/.workgate.previous.$$"
  rm -f "$NEXT" "$PREVIOUS"
  cp "$STAGED" "$NEXT"
  chmod 700 "$NEXT"
  if [ -f "$RUNTIME_DIR/workgate" ]; then
    mv "$RUNTIME_DIR/workgate" "$PREVIOUS"
    RUNTIME_HAD_PREVIOUS=1
  fi
  mv "$NEXT" "$RUNTIME_DIR/workgate"
  RUNTIME_PUBLISHED=1
  RUNTIME="$RUNTIME_DIR/workgate"
fi

CONNECT_ARGS=(executor connect "$CONTROL")
RUN_ARGS=(executor run)
INSTALL_ARGS=(executor install-service)
if [ -n "$NAME" ]; then CONNECT_ARGS+=(--name "$NAME"); fi
if [ -n "$DEFAULT_WORKDIR" ]; then
  CONNECT_ARGS+=(--default-workdir "$DEFAULT_WORKDIR")
  RUN_ARGS+=(--default-workdir "$DEFAULT_WORKDIR")
  INSTALL_ARGS+=(--default-workdir "$DEFAULT_WORKDIR")
fi

"$RUNTIME" "${CONNECT_ARGS[@]}"
if [ "$PERSIST" = "1" ]; then
  "$RUNTIME" "${INSTALL_ARGS[@]}"
  RUNTIME_PUBLISHED=0
  rm -f "$PREVIOUS"
  echo "Workgate executor installed and started."
  echo "Management: $RUNTIME executor status"
  exit 0
fi
"$RUNTIME" "${RUN_ARGS[@]}"
"""
    return (
        template.replace("__CONTROL__", shlex.quote(control))
        .replace("__ARTIFACT_ROOT__", shlex.quote(artifact_root))
        .replace("__VERSION__", shlex.quote(__version__))
        .replace(
            "__MAX_ARCHIVE_BYTES__",
            str(EXECUTOR_BOOTSTRAP_MAX_ARCHIVE_BYTES),
        )
    )


def executor_bootstrap_routes(base_url: str) -> list[BaseRoute]:
    """Return public routes for release-backed executor bootstrap."""

    async def script(_request: Request) -> Response:
        return PlainTextResponse(
            bootstrap_script(base_url),
            media_type="text/x-shellscript",
            headers={"Cache-Control": "no-store"},
        )

    async def checksum(request: Request) -> Response:
        target = request.path_params["target"]
        try:
            name = _archive_name(target)
            await asyncio.to_thread(_ensure_release_matches_runtime)
            digest = await _fetch_checksum(target)
        except ValueError:
            return Response(status_code=404)
        except (ExecutorBootstrapUnavailable, httpx.HTTPError) as exc:
            return PlainTextResponse(str(exc), status_code=503)
        return PlainTextResponse(
            f"{digest}  {name}\n",
            headers={"Cache-Control": "no-store"},
        )

    async def archive(request: Request) -> Response:
        target = request.path_params["target"]
        try:
            name = _archive_name(target)
            await asyncio.to_thread(_ensure_release_matches_runtime)
        except ValueError:
            return Response(status_code=404)
        except (ExecutorBootstrapUnavailable, httpx.HTTPError) as exc:
            return PlainTextResponse(str(exc), status_code=503)
        headers = {
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{name}"',
        }
        return StreamingResponse(
            _archive_stream(target),
            media_type="application/gzip",
            headers=headers,
        )

    root = EXECUTOR_BOOTSTRAP_PATH + "/{target:str}"
    return [
        Route(EXECUTOR_BOOTSTRAP_PATH, script, methods=["GET"]),
        Route(root + "/sha256", checksum, methods=["GET"]),
        Route(root + "/archive", archive, methods=["GET"]),
    ]
