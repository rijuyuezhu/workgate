"""Owner-scoped, revision-checked management of existing SKILL.md directories.

Only the Executor-managed source is mutable. Other Skill scopes continue to
resolve through the existing scanner; this module adds no Skill database.
"""

import hashlib
import json
import os
import shutil
import stat
import uuid
from contextlib import nullcontext, suppress
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ..app_paths import ensure_private_directory
from ..platform.host import HostOS, host_os
from ..platform.windows.archive import list_tree
from ..utils.private_files import (
    atomic_write_private_text,
    private_file_lock,
    read_private_text,
)
from .models import AgentBridgeManifest
from .skills import validate_skill_name
from .state import load_agent_manifest

MAX_SKILL_CONTENT_BYTES = 128 * 1024
MAX_MANAGED_SKILLS = 256


def _content_bytes(content: str) -> bytes:
    if not isinstance(content, str):
        raise ValueError("Skill content must be UTF-8 text")
    data = content.encode("utf-8")
    if not data or len(data) > MAX_SKILL_CONTENT_BYTES or "\0" in content:
        raise ValueError(
            "SKILL.md must be nonempty, UTF-8, and at most 128 KiB"
        )
    if not content.strip():
        raise ValueError("SKILL.md must not be blank")
    # Keep the existing Markdown format: optional YAML or TOML front matter.
    lines = content.splitlines()
    if lines and lines[0].strip() in {"---", "+++"}:
        marker = lines[0].strip()
        end = next(
            (
                i
                for i, line in enumerate(lines[1:], 1)
                if line.strip()
                in ({"---", "..."} if marker == "---" else {"+++"})
            ),
            None,
        )
        if end is None:
            raise ValueError("SKILL.md front matter is not closed")
        try:
            if marker == "---":
                parsed = yaml.safe_load("\n".join(lines[1:end]))
            else:
                import tomllib

                parsed = tomllib.loads("\n".join(lines[1:end]))
        except ValueError, yaml.YAMLError:
            raise ValueError("Invalid SKILL.md front matter") from None
        if parsed is not None and not isinstance(parsed, dict):
            raise ValueError("SKILL.md front matter must be a mapping")
    return data


def _revision(content: bytes, enabled: bool) -> str:
    return hashlib.sha256(
        content + (b"\x01" if enabled else b"\x00")
    ).hexdigest()


def _safe_root(config_dir: Path, directory: str) -> Path:
    root = Path(config_dir).absolute()
    relative = PurePosixPath(directory)
    if (
        not directory
        or relative.is_absolute()
        or ".." in relative.parts
        or relative == PurePosixPath(".")
        or "\\" in directory
    ):
        raise ValueError("Unsafe managed Skill directory")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or (
            host_os() is HostOS.WINDOWS and current.is_junction()
        ):
            raise ValueError("Symlink or junction in managed Skill directory")
    if root.is_symlink() or (
        host_os() is HostOS.WINDOWS and root.is_junction()
    ):
        raise ValueError("Symlinked Agent configuration directory")
    return current


def _skill_path(root: Path, name: str, *, required: bool) -> Path:
    validate_skill_name(name)
    if len(name.encode("utf-8")) > 255:
        raise ValueError("Skill name exceeds filesystem limit")
    path = root / name
    if (
        path.is_symlink()
        or (host_os() is HostOS.WINDOWS and path.is_junction())
        or (path.exists() and not path.is_dir())
    ):
        raise ValueError("Managed Skill must be a non-symlink directory")
    if required and not path.is_dir():
        raise ValueError("Unknown Executor-managed Skill")
    return path


def _entry(skill_path: Path) -> bytes:
    path = skill_path / "SKILL.md"
    if host_os() is HostOS.WINDOWS:
        from ..platform.windows.security import open_regular_file_no_follow

        fd = open_regular_file_no_follow(path)
    else:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Skill entry must be a regular SKILL.md")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Skill entry must be a regular file")
        if metadata.st_size > MAX_SKILL_CONTENT_BYTES:
            raise ValueError("SKILL.md exceeds 128 KiB")
        content = os.read(fd, MAX_SKILL_CONTENT_BYTES + 1)
    finally:
        os.close(fd)
    _content_bytes(content.decode("utf-8"))
    return content


def _no_links(root: Path) -> None:
    """Reject symlinks and unusual nodes before destructive subtree operations."""
    if host_os() is HostOS.WINDOWS:
        entries, truncated = list_tree(root, recursive=True, max_entries=2049)
        if truncated or len(entries) > 2048:
            raise ValueError(
                "Managed Skill directory is too large to delete safely"
            )
        if any(kind == "link" for _, kind, *_ in entries):
            raise ValueError("Managed Skill contains a symlink or junction")
        return
    checked = 0
    for here, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            checked += 1
            if checked > 2048:
                raise ValueError(
                    "Managed Skill directory is too large to delete safely"
                )
            child = Path(here) / name
            mode = child.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError(
                    "Managed Skill contains a symlink or special file"
                )


def _remove_manifest(path: Path) -> None:
    if host_os() is HostOS.WINDOWS:
        from ..platform.windows.security import delete_private_file

        with suppress(FileNotFoundError):
            delete_private_file(path)
    else:
        path.unlink(missing_ok=True)


def _move_skill_directory(source: Path, destination: Path) -> None:
    if host_os() is HostOS.WINDOWS:
        from ..platform.windows.archive import move_directory

        move_directory(source, destination, require_private_source=True)
    else:
        source.rename(destination)


def _remove_skill_directory(path: Path) -> None:
    if host_os() is HostOS.WINDOWS:
        from ..platform.windows.archive import remove_directory_tree

        remove_directory_tree(path, require_private_root=True)
    else:
        shutil.rmtree(path)


def manage_skill(
    config_dir: Path,
    action: str,
    *,
    name: str | None = None,
    content: str | None = None,
    expected_revision: str | None = None,
    entry_only: bool = False,
) -> dict[str, Any]:
    """Inspect or mutate only the canonical managed source, never global/project."""
    if action not in {
        "list",
        "get",
        "create",
        "stage",
        "update",
        "enable",
        "disable",
        "delete",
        "validate",
    }:
        raise ValueError("Unsupported Skill management action")
    if action != "list" and not name:
        raise ValueError("Skill name required")
    if action in {"create", "stage", "update", "validate"} and content is None:
        raise ValueError("SKILL.md content required")
    if (
        action not in {"create", "stage", "update", "validate"}
        and content is not None
    ):
        raise ValueError("SKILL.md content is not valid for this action")
    if (
        action in {"update", "enable", "disable", "delete"}
        and not expected_revision
    ):
        raise ValueError("Expected revision required for Skill mutation")
    if (
        action in {"list", "create", "stage", "get", "validate"}
        and expected_revision is not None
    ):
        raise ValueError("Expected revision is not valid for this action")
    if entry_only and action != "delete":
        raise ValueError("Entry-only removal applies only to delete")
    if name:
        validate_skill_name(name)
        if name.startswith("."):
            raise ValueError("Managed Skill names cannot begin with a dot")
    if content is not None:
        _content_bytes(content)
    if action == "validate":
        return {
            "valid": True,
            "name": name,
            "max_bytes": MAX_SKILL_CONTENT_BYTES,
        }
    config_dir = Path(config_dir)
    if config_dir.is_symlink() or (
        host_os() is HostOS.WINDOWS and config_dir.is_junction()
    ):
        raise ValueError("Symlinked Agent configuration directory")
    writing = action in {
        "create",
        "stage",
        "update",
        "enable",
        "disable",
        "delete",
    }
    if writing:
        if host_os() is not HostOS.WINDOWS:
            config_dir.mkdir(parents=True, exist_ok=True)
        ensure_private_directory(config_dir)
    # Use the same private manifest lock as MCP management, so editing one
    # manifest cannot silently erase concurrent Skill toggle updates.
    with (
        private_file_lock(config_dir / "config.lock")
        if writing
        else nullcontext()
    ):
        loaded = load_agent_manifest(config_dir)
        if loaded.status == "invalid_config":
            raise ValueError("Invalid Agent Bridge manifest")
        root = _safe_root(config_dir, loaded.data.skills.directory)
        disabled = set(loaded.data.skills.disabled)

        def row(skill_name: str) -> dict[str, Any]:
            folder = _skill_path(root, skill_name, required=True)
            data = _entry(folder)
            configured = skill_name not in disabled
            enabled = loaded.data.skills.enabled and configured
            return {
                "name": skill_name,
                "source": "managed",
                "enabled": enabled,
                "configured_enabled": configured,
                "revision": _revision(data, configured),
                "content": data.decode("utf-8"),
                "has_assets": (
                    any(
                        child.name != "SKILL.md"
                        for child, *_ in list_tree(
                            folder, recursive=False, max_entries=2
                        )[0]
                    )
                    if host_os() is HostOS.WINDOWS
                    else any(
                        child.name != "SKILL.md" for child in folder.iterdir()
                    )
                ),
            }

        if action == "list":
            if not root.is_dir():
                return {"skills": []}
            native_truncated = False
            if host_os() is HostOS.WINDOWS:
                listed, native_truncated = list_tree(
                    root, recursive=False, max_entries=MAX_MANAGED_SKILLS + 1
                )
                entries = sorted(
                    p.name
                    for p, kind, *_ in listed
                    if kind == "dir" and not p.name.startswith(".")
                )
            else:
                entries = sorted(
                    p.name
                    for p in root.iterdir()
                    if p.is_dir()
                    and not p.is_symlink()
                    and not p.name.startswith(".")
                )
            selected = []
            invalid = 0
            for skill_name in entries[:MAX_MANAGED_SKILLS]:
                try:
                    selected.append(
                        {
                            k: v
                            for k, v in row(skill_name).items()
                            if k != "content"
                        }
                    )
                except OSError, ValueError:
                    invalid += 1
            return {
                "skills": selected,
                "invalid_entries": invalid,
                "truncated": (
                    native_truncated or len(entries) > MAX_MANAGED_SKILLS
                ),
            }
        assert name is not None
        folder = _skill_path(
            root, name, required=action not in {"create", "stage"}
        )
        if action == "get":
            return {"skill": row(name)}
        if action in {"create", "stage"}:
            if folder.exists():
                raise ValueError("Managed Skill already exists")
            if action == "stage" and name in disabled:
                raise ValueError(
                    "Target Skill has an existing disabled setting"
                )
            if root.is_dir():
                if host_os() is HostOS.WINDOWS:
                    items, truncated = list_tree(
                        root,
                        recursive=False,
                        max_entries=MAX_MANAGED_SKILLS + 1,
                    )
                    capacity_reached = (
                        truncated
                        or sum(1 for _, kind, *_ in items if kind == "dir")
                        >= MAX_MANAGED_SKILLS
                    )
                else:
                    capacity_reached = (
                        sum(1 for p in root.iterdir() if p.is_dir())
                        >= MAX_MANAGED_SKILLS
                    )
                if capacity_reached:
                    raise ValueError("Managed Skill limit reached")
            if host_os() is HostOS.WINDOWS:
                ensure_private_directory(root)
            else:
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
            # A staged Skill is disabled *before* its directory appears. A
            # concurrent scanner cannot activate half of a cross-owner move.
            previous_manifest: str | None = None
            if action == "stage":
                previous_manifest = (
                    read_private_text(loaded.config_path)
                    if loaded.config_path.exists()
                    else None
                )
                raw = (
                    json.loads(previous_manifest)
                    if previous_manifest
                    else {"version": 1}
                )
                raw.setdefault("skills", {})["disabled"] = sorted(
                    disabled | {name}
                )
                AgentBridgeManifest.model_validate(raw)
                atomic_write_private_text(
                    loaded.config_path,
                    json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                )
                disabled.add(name)
            staged = root / (".creating-" + uuid.uuid4().hex)
            try:
                if host_os() is HostOS.WINDOWS:
                    ensure_private_directory(staged)
                else:
                    staged.mkdir(mode=0o700)
                atomic_write_private_text(staged / "SKILL.md", content or "")
                _move_skill_directory(staged, folder)
            except BaseException:
                # If staging never became visible, roll the disabled marker
                # back. Never enable a directory which may be left behind.
                if not folder.exists():
                    if previous_manifest is None:
                        _remove_manifest(loaded.config_path)
                    else:
                        atomic_write_private_text(
                            loaded.config_path, previous_manifest
                        )
                raise
            finally:
                if staged.exists():
                    _remove_skill_directory(staged)
            return {"action": action, "skill": row(name)}
        previous = row(name)
        if previous["revision"] != expected_revision:
            raise ValueError("Skill changed; reload before editing")
        if action == "update":
            _no_links(folder)
            atomic_write_private_text(folder / "SKILL.md", content or "")
        elif action in {"enable", "disable"}:
            if action == "disable":
                disabled.add(name)
            else:
                disabled.discard(name)
            raw: dict[str, Any] = (
                json.loads(read_private_text(loaded.config_path))
                if loaded.config_path.exists()
                else {"version": 1}
            )
            raw.setdefault("skills", {})["disabled"] = sorted(disabled)
            AgentBridgeManifest.model_validate(raw)
            atomic_write_private_text(
                loaded.config_path,
                json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
            )
        else:
            _no_links(folder)
            if entry_only:
                if host_os() is HostOS.WINDOWS:
                    assets_exist = any(
                        child.name != "SKILL.md"
                        for child, *_ in list_tree(
                            folder, recursive=False, max_entries=2
                        )[0]
                    )
                else:
                    assets_exist = any(
                        child.name != "SKILL.md" for child in folder.iterdir()
                    )
                if assets_exist:
                    raise ValueError(
                        "Skill contains auxiliary files; move the complete bundle separately"
                    )
            tombstone = root / (".deleting-" + uuid.uuid4().hex)
            _move_skill_directory(folder, tombstone)
            try:
                if name in disabled:
                    disabled.remove(name)
                    raw = json.loads(read_private_text(loaded.config_path))
                    raw.setdefault("skills", {})["disabled"] = sorted(disabled)
                    AgentBridgeManifest.model_validate(raw)
                    atomic_write_private_text(
                        loaded.config_path,
                        json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                    )
            except BaseException:
                _move_skill_directory(tombstone, folder)
                raise
            _remove_skill_directory(tombstone)
            return {"action": action, "deleted": name}
        return {"action": action, "skill": row(name)}
