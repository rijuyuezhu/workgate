"""Resolve, materialize, and launch the optional OpenTUI client."""

import contextlib
import gzip
import hashlib
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .. import __version__
from ..app_paths import app_paths, ensure_private_directory
from ..config.control import ControlSettingsView
from ..config.settings import get_settings
from .contracts import TUI_EXECUTABLE_NAME
from .security import (
    UI_API_PREFIX,
    UI_LOCAL_TOKEN_ENV,
    get_or_create_ui_local_token,
)

MAX_EMBEDDED_TUI_BYTES = 256 * 1024 * 1024
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_TREE_ROOT = _PACKAGE_ROOT.parents[1]


def embedded_tui_payload() -> Path | None:
    """Return the packaged compressed OpenTUI executable for this platform."""
    payload = _PACKAGE_ROOT / "ui_runtime" / f"{TUI_EXECUTABLE_NAME}.gz"
    return payload if payload.is_file() else None


def _copy_bounded_gzip(source: Path, destination: Path) -> None:
    total = 0
    with gzip.open(source, "rb") as reader, destination.open("wb") as writer:
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_EMBEDDED_TUI_BYTES:
                raise ValueError(
                    "Embedded OpenTUI runtime exceeds the extraction limit of "
                    f"{MAX_EMBEDDED_TUI_BYTES} bytes"
                )
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    if total == 0:
        raise ValueError("Embedded OpenTUI runtime is empty")


def _packaged_tui_executable_sha256(payload: Path) -> str | None:
    """Read the build-generated trusted executable digest beside a payload."""
    digest_path = Path(f"{payload}.sha256")
    try:
        info = digest_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65:
            return None
        digest = digest_path.read_text(encoding="ascii").strip()
    except OSError, UnicodeError:
        return None
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        return None
    return digest


def _regular_file_matches_sha256(path: Path, expected_sha256: str) -> bool:
    """Validate one cached executable by type, ownership/mode, and digest."""
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    if os.name != "nt":
        geteuid = getattr(os, "geteuid", None)
        if geteuid is not None and info.st_uid != geteuid():
            return False
        if stat.S_IMODE(info.st_mode) != 0o700:
            return False
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == expected_sha256


def _same_regular_file_content(left: Path, right: Path) -> bool:
    """Compare two regular non-symlink files without trusting cache metadata."""
    try:
        left_info = left.lstat()
        right_info = right.lstat()
    except OSError:
        return False
    if (
        not stat.S_ISREG(left_info.st_mode)
        or not stat.S_ISREG(right_info.st_mode)
        or left_info.st_size != right_info.st_size
    ):
        return False
    with left.open("rb") as left_file, right.open("rb") as right_file:
        while True:
            left_chunk = left_file.read(1024 * 1024)
            right_chunk = right_file.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def materialize_embedded_tui(
    cache_dir: Path,
    *,
    payload: Path | None = None,
) -> Path | None:
    """Atomically unpack the platform OpenTUI payload into regenerable cache."""
    payload = embedded_tui_payload() if payload is None else payload
    if payload is None or not payload.is_file():
        return None

    ensure_private_directory(cache_dir.parent)
    cache_dir = ensure_private_directory(cache_dir)
    target_dir = ensure_private_directory(cache_dir / __version__)
    target = target_dir / payload.name.removesuffix(".gz")
    expected_sha256 = _packaged_tui_executable_sha256(payload)
    if expected_sha256 is not None and _regular_file_matches_sha256(
        target, expected_sha256
    ):
        return target

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target_dir
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _copy_bounded_gzip(payload, temporary)
        if os.name != "nt":
            temporary.chmod(temporary.stat().st_mode | stat.S_IXUSR)
        if expected_sha256 is not None and not _regular_file_matches_sha256(
            temporary, expected_sha256
        ):
            raise ValueError(
                "Embedded OpenTUI runtime digest does not match payload metadata"
            )
        try:
            os.replace(temporary, target)
        except PermissionError:
            if not _same_regular_file_content(temporary, target):
                raise
        return target
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def tui_source_path() -> Path | None:
    """Find the source-mode OpenTUI entry without consulting arbitrary paths."""
    candidates = (
        _SOURCE_TREE_ROOT / "ui-opentui" / "src" / "tui.tsx",
        Path.cwd() / "ui-opentui" / "src" / "tui.tsx",
        Path("/app/ui-opentui/src/tui.tsx"),
    )
    return next((path for path in candidates if path.is_file()), None)


def split_tui_command(value: str, *, windows: bool | None = None) -> list[str]:
    """Parse one administrator-configured command while preserving Windows paths."""
    windows = os.name == "nt" if windows is None else windows
    parts = shlex.split(value, posix=not windows)
    if windows:
        parts = [
            part[1:-1]
            if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"}
            else part
            for part in parts
        ]
    if not parts:
        raise ValueError("ui_tui_command is empty")
    return parts


def _tui_sidecar_candidates() -> tuple[Path, ...]:
    """Return trusted platform sidecar locations in resolution order."""
    candidates = [
        Path(sys.executable).resolve().parent / TUI_EXECUTABLE_NAME,
        Path(sys.argv[0]).resolve().parent / TUI_EXECUTABLE_NAME,
        _SOURCE_TREE_ROOT / "ui-opentui" / "dist" / TUI_EXECUTABLE_NAME,
        Path.cwd() / "ui-opentui" / "dist" / TUI_EXECUTABLE_NAME,
        Path("/app/ui-opentui/dist") / TUI_EXECUTABLE_NAME,
    ]
    path_sidecar = shutil.which(TUI_EXECUTABLE_NAME)
    if path_sidecar:
        candidates.append(Path(path_sidecar).resolve())
    return tuple(dict.fromkeys(candidates))


def tui_runtime_available(settings: ControlSettingsView | None = None) -> bool:
    """Return whether OpenTUI can be started without materializing a payload."""
    active = settings or get_settings()
    if active.ui_tui_command:
        return True
    return (
        any(candidate.is_file() for candidate in _tui_sidecar_candidates())
        or embedded_tui_payload() is not None
        or (tui_source_path() is not None and shutil.which("bun") is not None)
    )


def resolve_tui_command(
    settings: ControlSettingsView | None = None,
) -> list[str]:
    """Resolve a configured, released, embedded, or Bun source OpenTUI runtime."""
    active = settings or get_settings()
    if active.ui_tui_command:
        return split_tui_command(active.ui_tui_command)

    for candidate in _tui_sidecar_candidates():
        if candidate.is_file():
            return [str(candidate)]

    try:
        embedded = materialize_embedded_tui(app_paths().ui_runtime_dir)
    except (EOFError, OSError, ValueError) as exc:
        raise RuntimeError(
            f"Unable to prepare embedded OpenTUI runtime: {exc}"
        ) from exc
    if embedded is not None:
        return [str(embedded)]

    source = tui_source_path()
    bun = shutil.which("bun")
    if source is not None and bun:
        return [bun, str(source)]
    if source is not None:
        raise RuntimeError(
            "OpenTUI source is installed, but Bun is not available in PATH"
        )
    raise RuntimeError(
        "OpenTUI runtime not found; install a release bundle or run "
        "`cd ui-opentui && bun run build:tui`"
    )


def validate_tui_api_base(value: str) -> str:
    """Accept only a credential-free loopback HTTP(S) Human UI API URL."""
    normalized = str(value).strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Native TUI --api-base must use a credential-free loopback HTTP(S) URL"
        )
    if parsed.path.rstrip("/") != UI_API_PREFIX:
        raise ValueError(f"Native TUI --api-base path must be {UI_API_PREFIX}")
    return normalized


def run_tui(
    api_base: str,
    *,
    settings: ControlSettingsView | None = None,
) -> int:
    """Launch OpenTUI with its loopback API credential confined to the environment."""
    active = settings or get_settings()
    env = os.environ.copy()
    env.update(
        {
            "WORKGATE_UI_API_BASE": validate_tui_api_base(api_base),
            "WORKGATE_UI_MODE": "tui",
            UI_LOCAL_TOKEN_ENV: get_or_create_ui_local_token(),
        }
    )
    try:
        completed = subprocess.run(
            resolve_tui_command(active),
            env=env,
            check=False,
        )
    except KeyboardInterrupt:
        return 130
    return int(completed.returncode)
