"""Manifest loading and configuration fingerprinting for the agent bridge."""

import hashlib
import json
import os
import stat
from pathlib import Path

from pydantic import ValidationError

from ..app_paths import ensure_private_directory
from ..platform.host import HostOS, host_os
from ..utils.private_files import read_private_bytes, read_private_text
from .models import AgentBridgeManifest, LoadedAgentManifest, SkillSource


def agent_config_fingerprint(config_dir: Path) -> str:
    """Return a stable content fingerprint for the injected agent config tree."""

    root = Path(config_dir)
    digest = hashlib.sha256()

    def update(*parts: object) -> None:
        for part in parts:
            digest.update(str(part).encode("utf-8", errors="replace"))
            digest.update(b"\0")

    def relative_path(path: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return str(path)

    def update_path(path: Path, relative: str) -> None:
        try:
            file_stat = path.lstat()
        except OSError as exc:
            update(relative, "stat_error", type(exc).__name__, exc)
            return

        mode = file_stat.st_mode
        update(
            relative,
            stat.S_IFMT(mode),
            file_stat.st_size,
            file_stat.st_mtime_ns,
        )
        if stat.S_ISLNK(mode):
            try:
                update("link", os.readlink(path))
            except OSError as exc:
                update("link_error", type(exc).__name__, exc)
            return
        if not stat.S_ISREG(mode):
            return

        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            update("read_error", type(exc).__name__, exc)

    if host_os() is HostOS.WINDOWS:
        from ..platform.windows.archive import list_tree
        from ..platform.windows.security import open_regular_file_no_follow

        try:
            native_entries, truncated = list_tree(
                root, recursive=True, max_entries=5000
            )
        except OSError as exc:
            update(".", "native_list_error", type(exc).__name__, exc)
            return digest.hexdigest()
        for path, kind, size, modified in sorted(
            native_entries, key=lambda row: row[0].as_posix()
        ):
            relative = relative_path(path)
            update(relative, kind, size, modified)
            if kind != "file":
                continue
            try:
                with os.fdopen(
                    open_regular_file_no_follow(path), "rb"
                ) as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
            except OSError as exc:
                update(relative, "native_read_error", type(exc).__name__, exc)
        if truncated:
            update("native_list_truncated", len(native_entries))
        return digest.hexdigest()

    update_path(root, ".")
    try:
        root_stat = root.lstat()
    except OSError:
        return digest.hexdigest()
    if not stat.S_ISDIR(root_stat.st_mode):
        return digest.hexdigest()

    walk_errors: list[OSError] = []

    def on_walk_error(exc: OSError) -> None:
        walk_errors.append(exc)

    for current, dirnames, filenames in os.walk(
        root, topdown=True, onerror=on_walk_error
    ):
        dirnames.sort()
        filenames.sort()
        current_path = Path(current)

        for dirname in list(dirnames):
            child = current_path / dirname
            update_path(child, relative_path(child))
            try:
                if child.is_symlink():
                    dirnames.remove(dirname)
            except OSError:
                dirnames.remove(dirname)

        for filename in filenames:
            child = current_path / filename
            update_path(child, relative_path(child))

    for error in walk_errors:
        update("walk_error", type(error).__name__, error)

    return digest.hexdigest()


def agent_registry_fingerprint(
    config_dir: Path,
    sources: tuple[SkillSource, ...],
    private_state_paths: tuple[Path, ...] = (),
) -> str:
    """Fingerprint manifest/Skill roots plus bounded private auth state files."""
    if config_dir.exists():
        ensure_private_directory(config_dir)
    digest = hashlib.sha256()
    digest.update(agent_config_fingerprint(config_dir).encode("ascii"))
    for source in sources:
        digest.update(source.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(source.path).encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(agent_config_fingerprint(source.path).encode("ascii"))
    for path in private_state_paths:
        digest.update(str(path).encode("utf-8", errors="replace"))
        digest.update(b"\0")
        try:
            payload = read_private_bytes(path)
        except FileNotFoundError:
            payload = b"<missing>"
        except OSError as exc:
            payload = f"<error:{type(exc).__name__}>".encode("ascii")
        digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


def load_agent_manifest(
    config_dir: Path, *, read_only: bool = False
) -> LoadedAgentManifest:
    """Read/validate the manifest; diagnostics may not alter file permissions."""
    config_path = config_dir / "config.json"
    if read_only:
        # Never follow user-supplied manifest symlinks or fix file permissions
        # merely because an observer opened a diagnostics subpage.
        if config_dir.is_symlink() or config_path.is_symlink():
            return LoadedAgentManifest(
                config_path=config_path,
                status="invalid_config",
                errors=["symlink"],
            )
    elif config_dir.exists():
        ensure_private_directory(config_dir)
    if not config_path.exists():
        return LoadedAgentManifest(
            config_path=config_path, status="missing_config"
        )
    try:
        raw = json.loads(read_private_text(config_path))
        data = AgentBridgeManifest.model_validate(raw)
    except ValidationError as exc:
        return LoadedAgentManifest(
            config_path=config_path,
            status="invalid_config",
            errors=[str(error) for error in exc.errors(include_input=False)],
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return LoadedAgentManifest(
            config_path=config_path,
            status="invalid_config",
            errors=[str(exc)],
        )
    return LoadedAgentManifest(
        config_path=config_path, status="loaded", data=data
    )
