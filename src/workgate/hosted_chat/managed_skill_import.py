"""Bounded, in-memory import of one managed Skill; no archive extraction."""

import io
import mimetypes
import stat
import uuid
import zipfile
from pathlib import PurePosixPath

import yaml

from ..agent_bridge.skills import validate_skill_file_path
from .database import SQLiteDatabase
from .managed_skill_files import _MAX_FILES, _MAX_NODES, skill_path
from .managed_skills import (
    ManagedSkills,
    owned_skill,
    present_skill,
    validate_record,
)
from .storage import _now

_MAX_IMPORT = 10 * 1024 * 1024
_MAX_ENTRY = 512 * 1024
_MAX_FRONT = 32768


class _FrontLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in mapping:
                raise ValueError("Invalid or duplicate Skill metadata key")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _parse_main(raw: bytes, filename: str) -> dict:
    text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    front = {}
    body = text
    if text.startswith("---\n"):
        lines = text.splitlines(keepends=True)
        end = next(
            (i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"),
            None,
        )
        if end is None:
            raise ValueError("Unclosed Skill frontmatter")
        metadata = "".join(lines[1:end])
        if len(metadata.encode("utf-8")) > _MAX_FRONT:
            raise ValueError("Skill frontmatter too large")
        if any(
            isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
            for token in yaml.scan(metadata)
        ):
            raise ValueError("Skill frontmatter aliases are unsupported")
        parsed = yaml.load(metadata, Loader=_FrontLoader)
        if parsed is not None and not isinstance(parsed, dict):
            raise ValueError("Skill frontmatter must be a mapping")
        front = parsed or {}
        body = "".join(lines[end + 1 :])
    fallback = PurePosixPath(filename).stem.lower().replace("_", "-")
    name = front.pop("name", fallback)
    description = front.pop("description", "Imported Skill")
    always = front.pop("always-apply", front.pop("alwaysApply", False))
    return validate_record(
        {
            "name": name,
            "description": description,
            "body": body,
            "frontmatter": front,
            "alwaysApply": always,
        }
    )


def _bundle(raw: bytes) -> tuple[bytes, dict[str, bytes], set[str]]:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > _MAX_NODES + 1:
            raise ValueError("Skill archive has too many entries")
        names = set()
        files = []
        directories = []
        total = 0
        for entry in entries:
            name = (
                entry.filename.removesuffix("/")
                if entry.is_dir()
                else entry.filename
            )
            validate_skill_file_path(name)
            if name in names or entry.flag_bits & 1:
                raise ValueError("Duplicate or encrypted Skill archive entry")
            names.add(name)
            mode = (entry.external_attr >> 16) & 0xFFFF
            kind = stat.S_IFMT(mode)
            if kind not in (
                0,
                stat.S_IFDIR if entry.is_dir() else stat.S_IFREG,
            ):
                raise ValueError("Non-regular Skill archive entry")
            if entry.is_dir():
                directories.append(name)
                continue
            total += entry.file_size
            if entry.file_size > _MAX_ENTRY or total > _MAX_IMPORT:
                raise ValueError("Skill archive exceeds expanded size limits")
            files.append(entry)
        roots = [
            entry
            for entry in files
            if PurePosixPath(entry.filename).name == "SKILL.md"
        ]
        if len(roots) != 1 or len(files) > _MAX_FILES + 1:
            raise ValueError("Archive must contain exactly one SKILL.md")
        root = roots[0]
        prefix = root.filename.removesuffix("SKILL.md")
        parts = {}
        folders = set()
        for directory in directories:
            if prefix and (
                directory == prefix.rstrip("/")
                or prefix.startswith(directory + "/")
            ):
                continue
            if not directory.startswith(prefix):
                raise ValueError("Archive must contain one Skill directory")
            folders.add(skill_path(directory[len(prefix) :]))
        for entry in files:
            if not entry.filename.startswith(prefix):
                raise ValueError("Archive must contain one Skill directory")
            path = entry.filename[len(prefix) :]
            if entry is not root:
                path = skill_path(path)
            with archive.open(entry) as stream:
                content = stream.read(_MAX_ENTRY + 1)
            if len(content) != entry.file_size or len(content) > _MAX_ENTRY:
                raise ValueError("Skill archive entry size changed")
            parts[path] = content
        main = parts.pop("SKILL.md")
        for path in (*parts, *folders):
            for parent in PurePosixPath(path).parents:
                if parent != PurePosixPath("."):
                    folders.add(skill_path(parent.as_posix()))
        if (
            folders.intersection(parts)
            or len(folders) + len(parts) > _MAX_NODES
        ):
            raise ValueError("Invalid Skill archive tree")
        return main, parts, folders


def import_skill(
    database: SQLiteDatabase, owner: str, filename: str, raw: bytes
) -> dict:
    if (
        not isinstance(filename, str)
        or not filename
        or len(filename) > 256
        or "/" in filename
        or "\\" in filename
    ):
        raise ValueError("Invalid Skill filename")
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_IMPORT:
        raise ValueError("Invalid Skill import size")
    try:
        if zipfile.is_zipfile(io.BytesIO(raw)):
            main, parts, folders = _bundle(raw)
        else:
            if len(raw) > _MAX_ENTRY:
                raise ValueError("Skill main document too large")
            main, parts, folders = raw, {}, set()
        record = _parse_main(main, filename)
    except (
        yaml.YAMLError,
        zipfile.BadZipFile,
        NotImplementedError,
        RuntimeError,
        UnicodeError,
        RecursionError,
    ) as error:
        raise ValueError("Invalid Skill import") from error
    references = []
    for content in parts.values():
        try:
            references.append(content.decode("utf-8"))
        except UnicodeError:
            continue
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        sid = ManagedSkills(database)._insert_locked(
            db, owner, record, references=references
        )
        now = _now()
        for folder in sorted(folders):
            db.execute(
                "INSERT INTO webui_skill_folders(id,owner,skill_id,path,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), owner, sid, folder, now, now),
            )
        for path, data in sorted(parts.items()):
            db.execute(
                "INSERT INTO webui_skill_files(id,owner,skill_id,path,data,mime,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    owner,
                    sid,
                    path,
                    data,
                    mimetypes.guess_type(path)[0] or "application/octet-stream",
                    now,
                    now,
                ),
            )
        return present_skill(
            owned_skill(db, owner, sid), detail=True, file_count=len(parts)
        )
