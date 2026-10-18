"""Bounded authoring of text files inside Executor-owned managed Skill bundles.

Shares the managed manifest lock with Skill CRUD, and never follows symlinks
while opening nested directories or files. No project/global paths are accepted.
"""

import hashlib
import os
import stat
import uuid
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Any

from ..utils.private_files import private_file_lock
from .skill_management import (
    MAX_SKILL_CONTENT_BYTES,
    _content_bytes,
    _safe_root,
    _skill_path,
)
from .skills import IGNORED_RELATED_DIRECTORIES, validate_skill_file_path
from .state import load_agent_manifest

MAX_AUXILIARY_FILES = 1_000


def _file_path(value: str | None) -> Path:
    if value is None:
        raise ValueError("Skill auxiliary file path is required")
    relative = validate_skill_file_path(value)
    if relative.as_posix() == "SKILL.md" or any(
        segment.startswith(".")
        or segment in IGNORED_RELATED_DIRECTORIES
        or len(segment.encode("utf-8")) > 255
        for segment in relative.parts
    ):
        raise ValueError("Not an ordinary Skill auxiliary file path")
    return relative


def _file_bytes(content: str | None) -> bytes:
    if not isinstance(content, str):
        raise ValueError("UTF-8 auxiliary file content required")
    try:
        data = content.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Auxiliary file content must be UTF-8") from exc
    if len(data) > MAX_SKILL_CONTENT_BYTES or "\0" in content:
        raise ValueError("Auxiliary files must be UTF-8 text at most 128 KiB")
    return data


def _opened_file(parent: int, name: str) -> tuple[bytes, int]:
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > MAX_SKILL_CONTENT_BYTES
            ):
                raise ValueError("Auxiliary file must be a small regular file")
            data = bytearray()
            while len(data) <= MAX_SKILL_CONTENT_BYTES:
                chunk = os.read(
                    fd, min(65536, MAX_SKILL_CONTENT_BYTES + 1 - len(data))
                )
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > MAX_SKILL_CONTENT_BYTES:
                raise ValueError("Auxiliary file exceeds 128 KiB")
        finally:
            os.close(fd)
        text = bytes(data).decode("utf-8")
        if "\0" in text:
            raise ValueError("Auxiliary file is not ordinary UTF-8 text")
        return bytes(data), stat.S_IMODE(info.st_mode) & 0o777
    except UnicodeError as exc:
        raise ValueError("Auxiliary file is not UTF-8 text") from exc
    except OSError as exc:
        raise ValueError(
            "Auxiliary file is missing or not a regular file"
        ) from exc


def _revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(
    parent: int,
    staging_dir: int,
    name: str,
    data: bytes,
    *,
    create: bool,
    mode: int = 0o600,
) -> None:
    temporary = ".writing-" + uuid.uuid4().hex
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=staging_dir,
    )
    try:
        try:
            with os.fdopen(fd, "wb", closefd=False) as out:
                out.write(data)
                out.flush()
                os.fchmod(fd, mode)
                os.fsync(fd)
            if create:
                # Hard-link publication cannot replace a concurrently created file.
                os.link(
                    temporary,
                    name,
                    src_dir_fd=staging_dir,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
            else:
                os.replace(
                    temporary, name, src_dir_fd=staging_dir, dst_dir_fd=parent
                )
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=staging_dir)
    finally:
        os.close(fd)


def _list_auxiliary_files(skill_fd: int) -> list[str]:
    """List regular files using the anchored Skill directory descriptor."""
    entries: list[str] = []
    scanned = 0
    for here, dirs, files, current_fd in os.fwalk(
        ".", topdown=True, follow_symlinks=False, dir_fd=skill_fd
    ):
        for item in [*dirs, *files]:
            scanned += 1
            if scanned > 2048:
                raise ValueError(
                    "Managed Skill directory is too large to inspect"
                )
            mode = os.stat(
                item, dir_fd=current_fd, follow_symlinks=False
            ).st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError(
                    "Managed Skill contains a symlink or special file"
                )
        dirs[:] = sorted(
            dirname
            for dirname in dirs
            if not dirname.startswith(".")
            and dirname not in IGNORED_RELATED_DIRECTORIES
        )
        for filename in sorted(files):
            if filename.startswith("."):
                continue
            file_path = (Path(here) / filename).as_posix()
            if file_path == "SKILL.md":
                continue
            try:
                _file_path(file_path)
            except ValueError:
                continue
            entries.append(file_path)
            if len(entries) > MAX_AUXILIARY_FILES:
                raise ValueError("Too many Skill auxiliary files")
    return sorted(entries)


def manage_skill_file(
    config_dir: Path,
    action: str,
    *,
    name: str,
    path: str | None = None,
    content: str | None = None,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """List/read/create/edit/delete small text files in one *managed* Skill."""
    if action not in {"list", "get", "create", "update", "delete"}:
        raise ValueError("Unsupported Skill auxiliary file operation")
    relative = None if action == "list" else _file_path(path)
    if action == "list" and path is not None:
        raise ValueError("List does not accept a file path")
    data = _file_bytes(content) if action in {"create", "update"} else None
    if data is None and content is not None:
        raise ValueError("Content is only valid for create or update")
    if action in {"update", "delete"}:
        if not expected_revision or len(expected_revision) != 64:
            raise ValueError("Expected file revision required")
    elif expected_revision is not None:
        raise ValueError("File revision is not valid for this action")

    if name.startswith("."):
        raise ValueError("Hidden managed Skill names cannot be edited")
    config_dir = Path(config_dir)
    if config_dir.is_symlink():
        raise ValueError("Symlinked Agent configuration directory")
    writing = action in {"create", "update", "delete"}
    with (
        private_file_lock(config_dir / "config.lock")
        if writing
        else ExitStack()
    ):
        loaded = load_agent_manifest(config_dir)
        if loaded.status == "invalid_config":
            raise ValueError("Invalid Agent Bridge manifest")
        root = _safe_root(config_dir, loaded.data.skills.directory)
        _skill_path(root, name, required=True)
        with ExitStack() as stack:
            # Descend by dirfd, including the configured managed Skills root.
            # A symlink swap in *any* component cannot redirect a file write.
            fd = os.open(
                config_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            stack.callback(os.close, fd)
            for segment in Path(loaded.data.skills.directory).parts:
                opened = os.open(
                    segment,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
                stack.callback(os.close, opened)
                fd = opened
            managed_fd = fd  # Stage outside the Skill bundle so its scanner never sees half-written files.
            skill_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=fd,
            )
            stack.callback(os.close, skill_fd)
            # Validate SKILL.md through the same anchored directory.
            _content_bytes(
                _opened_file(skill_fd, "SKILL.md")[0].decode("utf-8")
            )
            if action == "list":
                return {"files": _list_auxiliary_files(skill_fd)}
            if (
                action == "create"
                and len(_list_auxiliary_files(skill_fd)) >= MAX_AUXILIARY_FILES
            ):
                raise ValueError("Too many Skill auxiliary files")
            assert relative is not None
            fd = skill_fd
            ancestors: list[tuple[int, str]] = []
            for segment in relative.parts[:-1]:
                if action == "create":
                    with suppress(FileExistsError):
                        os.mkdir(segment, 0o700, dir_fd=fd)
                opened = os.open(
                    segment,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
                stack.callback(os.close, opened)
                ancestors.append((fd, segment))
                fd = opened
            filename = relative.parts[-1]
            if action == "get":
                text, _mode = _opened_file(fd, filename)
                return {
                    "path": relative.as_posix(),
                    "content": text.decode("utf-8"),
                    "revision": _revision(text),
                }
            if action == "create":
                try:
                    os.stat(filename, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError("Auxiliary file already exists")
                assert data is not None
                _write_atomic(fd, managed_fd, filename, data, create=True)
                return {
                    "path": relative.as_posix(),
                    "revision": _revision(data),
                }
            previous, mode = _opened_file(fd, filename)
            if _revision(previous) != expected_revision:
                raise ValueError(
                    "Auxiliary file changed; reload before editing"
                )
            if action == "update":
                assert data is not None
                _write_atomic(
                    fd, managed_fd, filename, data, create=False, mode=mode
                )
                return {
                    "path": relative.as_posix(),
                    "revision": _revision(data),
                }
            os.unlink(filename, dir_fd=fd)
            for parent_fd, segment in reversed(ancestors):
                try:
                    os.rmdir(segment, dir_fd=parent_fd)
                except OSError:
                    break
            return {"deleted": relative.as_posix()}
