"""Transactional SQLite supporting files for owner-managed chat Skills."""

import uuid
from pathlib import PurePosixPath

from ..agent_bridge.skills import validate_skill_file_path
from .database import SQLiteDatabase
from .managed_skills import SkillConflict, enforce_policy, owned_skill
from .storage import _now, _stamp

_MAX_FILES = 100
_MAX_NODES = 256
_MAX_FILE_BYTES = 10 * 1024 * 1024
_MAX_BUNDLE_BYTES = 32 * 1024 * 1024


def skill_path(path: str) -> str:
    value = validate_skill_file_path(path).as_posix()
    if value.split("/", 1)[0].casefold() == "skill.md":
        raise ValueError("Edit the main Skill record instead")
    return value


def _order(value):
    if type(value) is not int or not 0 <= value <= 100_000:
        raise ValueError("Invalid Skill node order")
    return value


def _nodes(db, owner, sid):
    return {
        row["path"]: row
        for row in db.execute(
            "SELECT id,path,position,created_at,updated_at,'folder' AS kind,NULL AS mime,0 AS size "
            "FROM webui_skill_folders WHERE owner=? AND skill_id=? "
            "UNION ALL SELECT id,path,position,created_at,updated_at,'file',mime,length(data) "
            "FROM webui_skill_files WHERE owner=? AND skill_id=? ORDER BY path",
            (owner, sid, owner, sid),
        )
    }


def _present(row, nodes, version):
    parent = nodes.get(str(PurePosixPath(row["path"]).parent))
    return {
        "id": row["id"],
        "type": row["kind"],
        "name": PurePosixPath(row["path"]).name,
        "relativePath": row["path"],
        "parentId": parent["id"] if parent else None,
        "order": row["position"],
        "mimeType": row["mime"],
        "bytes": row["size"],
        "createdAt": _stamp(row["created_at"] / 1_000_000),
        "updatedAt": _stamp(row["updated_at"] / 1_000_000),
        "skillVersion": version,
    }


def _parent(nodes, path):
    parent = str(PurePosixPath(path).parent)
    if parent != "." and (
        parent not in nodes or nodes[parent]["kind"] != "folder"
    ):
        raise ValueError("Skill parent folder not found")


def _node(nodes, nid):
    for row in nodes.values():
        if row["id"] == nid:
            return row
    raise KeyError("Skill node not found")


def _check_version(db, owner, sid, expected_version):
    row = owned_skill(db, owner, sid)
    if type(expected_version) is not int:
        raise ValueError("Expected Skill version required")
    if row["version"] != expected_version:
        raise SkillConflict("Skill version changed")
    return row["version"]


def _bump(db, owner, sid, version):
    db.execute(
        "UPDATE webui_skills SET version=version+1,updated_at=? WHERE owner=? AND id=?",
        (_now(), owner, sid),
    )
    return version + 1


def _table(row):
    return (
        "webui_skill_folders"
        if row["kind"] == "folder"
        else "webui_skill_files"
    )


class ManagedSkillFiles:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def tree(self, owner: str, sid: str) -> dict:
        with self.database.read_snapshot() as db:
            version = owned_skill(db, owner, sid)["version"]
            nodes = _nodes(db, owner, sid)
            return {
                "version": version,
                "nodes": [
                    _present(row, nodes, version) for row in nodes.values()
                ],
            }

    def create_folder(
        self,
        owner: str,
        sid: str,
        path: str,
        *,
        expected_version: int,
        position: int = 0,
    ) -> dict:
        path = skill_path(path)
        position = _order(position)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = _check_version(db, owner, sid, expected_version)
            nodes = _nodes(db, owner, sid)
            _parent(nodes, path)
            if path in nodes:
                raise SkillConflict("Skill path already exists")
            if len(nodes) >= _MAX_NODES:
                raise SkillConflict("Skill node limit reached")
            now = _now()
            db.execute(
                "INSERT INTO webui_skill_folders(id,owner,skill_id,path,position,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), owner, sid, path, position, now, now),
            )
            version = _bump(db, owner, sid, version)
            nodes = _nodes(db, owner, sid)
            return _present(nodes[path], nodes, version)

    def put_file(
        self,
        owner: str,
        sid: str,
        path: str,
        data: bytes,
        *,
        expected_version: int,
        mime: str = "application/octet-stream",
        position: int = 0,
        replace: bool = False,
    ) -> dict:
        path = skill_path(path)
        position = _order(position)
        if not isinstance(data, bytes) or len(data) > _MAX_FILE_BYTES:
            raise ValueError("Invalid Skill file bytes")
        if (
            not isinstance(mime, str)
            or not 1 <= len(mime) <= 128
            or any(ord(c) < 32 or ord(c) > 126 for c in mime)
        ):
            raise ValueError("Invalid Skill file MIME")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = _check_version(db, owner, sid, expected_version)
            nodes = _nodes(db, owner, sid)
            _parent(nodes, path)
            prior = nodes.get(path)
            if prior is not None and (prior["kind"] != "file" or not replace):
                raise SkillConflict("Skill path already exists")
            if replace and prior is None:
                raise KeyError("Skill file not found")
            files = [row for row in nodes.values() if row["kind"] == "file"]
            if prior is None and (
                len(nodes) >= _MAX_NODES or len(files) >= _MAX_FILES
            ):
                raise SkillConflict("Skill file limit reached")
            total = (
                sum(row["size"] for row in files)
                - (prior["size"] if prior else 0)
                + len(data)
            )
            if total > _MAX_BUNDLE_BYTES:
                raise ValueError("Skill bundle exceeds byte limit")
            try:
                content = data.decode("utf-8")
            except UnicodeError:
                content = None
            if content is not None:
                enforce_policy(db, owner, references=[content])
            now = _now()
            if prior:
                db.execute(
                    "UPDATE webui_skill_files SET data=?,mime=?,position=?,updated_at=? WHERE id=? AND owner=? AND skill_id=?",
                    (data, mime, position, now, prior["id"], owner, sid),
                )
            else:
                db.execute(
                    "INSERT INTO webui_skill_files(id,owner,skill_id,path,data,mime,position,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        owner,
                        sid,
                        path,
                        data,
                        mime,
                        position,
                        now,
                        now,
                    ),
                )
            version = _bump(db, owner, sid, version)
            nodes = _nodes(db, owner, sid)
            return _present(nodes[path], nodes, version)

    def read_file(self, owner: str, sid: str, path: str) -> tuple[dict, bytes]:
        path = skill_path(path)
        with self.database.read_snapshot() as db:
            version = owned_skill(db, owner, sid)["version"]
            nodes = _nodes(db, owner, sid)
            row = nodes.get(path)
            if row is None or row["kind"] != "file":
                raise KeyError("Skill file not found")
            data = db.execute(
                "SELECT data FROM webui_skill_files WHERE id=? AND owner=? AND skill_id=?",
                (row["id"], owner, sid),
            ).fetchone()[0]
            return _present(row, nodes, version), data

    def move(
        self,
        owner: str,
        sid: str,
        nid: str,
        path: str,
        *,
        expected_version: int,
        position: int | None = None,
    ) -> dict:
        path = skill_path(path)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = _check_version(db, owner, sid, expected_version)
            nodes = _nodes(db, owner, sid)
            row = _node(nodes, nid)
            old = row["path"]
            _parent(nodes, path)
            if path.startswith(old + "/"):
                raise ValueError("Cannot move a Skill node into itself")
            order = row["position"] if position is None else _order(position)
            affected = {
                key: item
                for key, item in nodes.items()
                if key == old
                or (row["kind"] == "folder" and key.startswith(old + "/"))
            }
            targets = {
                key: skill_path(path + key[len(old) :]) for key in affected
            }
            if any(
                target in nodes and target not in affected
                for target in targets.values()
            ):
                raise SkillConflict("Skill destination already exists")
            now = _now()
            # The target is neither an ancestor collision nor a descendant of
            # the moved subtree, so no temporary path namespace is needed.
            for key, item in affected.items():
                db.execute(
                    f"UPDATE {_table(item)} SET path=?,position=?,updated_at=? WHERE id=? AND owner=? AND skill_id=?",
                    (
                        targets[key],
                        order if key == old else item["position"],
                        now,
                        item["id"],
                        owner,
                        sid,
                    ),
                )
            version = _bump(db, owner, sid, version)
            nodes = _nodes(db, owner, sid)
            return _present(nodes[path], nodes, version)

    def delete(
        self, owner: str, sid: str, nid: str, *, expected_version: int
    ) -> int:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = _check_version(db, owner, sid, expected_version)
            row = _node(_nodes(db, owner, sid), nid)
            if row["kind"] == "folder":
                prefix = row["path"] + "/"
                for table in ("webui_skill_files", "webui_skill_folders"):
                    db.execute(
                        f"DELETE FROM {table} WHERE owner=? AND skill_id=? AND (path=? OR substr(path,1,?)=?)",
                        (owner, sid, row["path"], len(prefix), prefix),
                    )
            else:
                db.execute(
                    "DELETE FROM webui_skill_files WHERE id=? AND owner=? AND skill_id=?",
                    (nid, owner, sid),
                )
            return _bump(db, owner, sid, version)
