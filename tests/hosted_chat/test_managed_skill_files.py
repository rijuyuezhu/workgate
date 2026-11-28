"""Managed Skill tree uses the parent revision as a single mutation fence."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.hosted_chat.test_content_filters import _policy
from workgate.hosted_chat import managed_skill_files as files
from workgate.hosted_chat.content_filters import ContentFilterBlocked
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.managed_skills import ManagedSkills, SkillConflict
from workgate.hosted_chat.storage import WorkspaceRepository


@pytest.fixture
def state(tmp_path):
    db = SQLiteDatabase(tmp_path)
    skills = ManagedSkills(db)
    skill = skills.create(
        "alice",
        {"name": "review", "description": "Review results", "body": "# Review"},
    )
    return db, skills, files.ManagedSkillFiles(db), skill["_id"]


def test_tree_lifecycle_and_literal_prefix(state):
    _, skills, repo, sid = state
    assert repo.tree("alice", sid) == {"version": 1, "nodes": []}
    root = repo.create_folder("alice", sid, "a_%", expected_version=1)
    child = repo.create_folder("alice", sid, "a_%/refs", expected_version=2)
    item = repo.put_file(
        "alice",
        sid,
        "a_%/refs/note.txt",
        b"hello",
        expected_version=3,
        mime="text/plain",
    )
    assert item["parentId"] == child["id"] and item["bytes"] == 5
    other = repo.create_folder("alice", sid, "axZ", expected_version=4)
    repo.put_file("alice", sid, "axZ/keep.bin", b"\xff\x00", expected_version=5)
    assert repo.read_file("alice", sid, "a_%/refs/note.txt")[1] == b"hello"
    assert skills.get("alice", sid)["fileCount"] == 2
    with pytest.raises(SkillConflict):
        repo.put_file(
            "alice", sid, "a_%/refs/note.txt", b"x", expected_version=6
        )
    changed = repo.put_file(
        "alice",
        sid,
        "a_%/refs/note.txt",
        b"updated",
        expected_version=6,
        replace=True,
    )
    assert changed["id"] == item["id"] and changed["skillVersion"] == 7
    with pytest.raises(ValueError):
        repo.move("alice", sid, root["id"], "a_%/refs/self", expected_version=7)
    with pytest.raises(SkillConflict):
        repo.move("alice", sid, root["id"], "axZ", expected_version=7)
    moved = repo.move(
        "alice", sid, root["id"], "renamed", expected_version=7, position=5
    )
    assert moved["order"] == 5
    assert (
        repo.read_file("alice", sid, "renamed/refs/note.txt")[1] == b"updated"
    )
    with pytest.raises(KeyError):
        repo.read_file("alice", sid, "a_%/refs/note.txt")
    assert repo.delete("alice", sid, root["id"], expected_version=8) == 9
    assert {n["relativePath"] for n in repo.tree("alice", sid)["nodes"]} == {
        "axZ",
        "axZ/keep.bin",
    }
    keep = repo.read_file("alice", sid, "axZ/keep.bin")
    assert keep[1] == b"\xff\x00"
    assert repo.delete("alice", sid, keep[0]["id"], expected_version=9) == 10
    assert repo.delete("alice", sid, other["id"], expected_version=10) == 11
    assert repo.tree("alice", sid) == {"version": 11, "nodes": []}


@pytest.mark.parametrize(
    "path",
    ["../outside", "/abs", "a\\b", "a//b", "SKILL.md", "skill.md/x", "a:bad"],
)
def test_invalid_paths(state, path):
    _, _, repo, sid = state
    with pytest.raises(ValueError):
        repo.create_folder("alice", sid, path, expected_version=1)


def test_fences_and_policy(state, monkeypatch):
    db, skills, repo, sid = state
    for call in (
        lambda: repo.tree("bob", sid),
        lambda: repo.create_folder("bob", sid, "x", expected_version=1),
        lambda: repo.read_file("bob", sid, "x"),
    ):
        with pytest.raises(KeyError):
            call()
    with pytest.raises(ValueError):
        repo.create_folder("alice", sid, "missing/child", expected_version=1)
    with pytest.raises(ValueError):
        repo.create_folder("alice", sid, "x", expected_version=1, position=-1)
    with pytest.raises(ValueError):
        repo.put_file(
            "alice", sid, "x", b"x", expected_version=1, mime="text/plain\n"
        )
    with pytest.raises(KeyError):
        repo.put_file("alice", sid, "x", b"x", expected_version=1, replace=True)
    WorkspaceRepository(db).save_setting(
        "alice", "webui.filters.config", _policy("skills")
    )
    with pytest.raises(ContentFilterBlocked):
        repo.put_file(
            "alice", sid, "blocked.txt", b"Bearer secret", expected_version=1
        )
    assert skills.get("alice", sid)["version"] == 1
    monkeypatch.setattr(files, "_MAX_FILE_BYTES", 4)
    with pytest.raises(ValueError):
        repo.put_file("alice", sid, "large.txt", b"12345", expected_version=1)
    monkeypatch.setattr(files, "_MAX_BUNDLE_BYTES", 3)
    with pytest.raises(ValueError):
        repo.put_file("alice", sid, "large.txt", b"1234", expected_version=1)

    def create(i):
        try:
            return repo.put_file(
                "alice", sid, f"{i}.txt", b"ok", expected_version=1
            )
        except SkillConflict:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(item is not None for item in pool.map(create, range(4))) == 1
    with pytest.raises(KeyError):
        repo.delete("alice", sid, "missing", expected_version=2)
    monkeypatch.setattr(files, "_MAX_NODES", 1)
    with pytest.raises(SkillConflict):
        repo.create_folder("alice", sid, "extra", expected_version=2)
    with pytest.raises(SkillConflict):
        repo.put_file("alice", sid, "extra.txt", b"x", expected_version=2)
    skills.delete("alice", sid)
    with db.read_snapshot() as conn:
        assert (
            conn.execute("SELECT count(*) FROM webui_skill_files").fetchone()[0]
            == 0
        )
