"""Import bounded single-Skill bundles without filesystem extraction."""

import base64
import io
import stat
import zipfile

import httpx
import pytest

from tests.hosted_chat.test_content_filters import _policy
from tests.support.hosted_chat_http import runtime_for
from workgate.control.http.app import build_http_app
from workgate.hosted_chat import managed_skill_import as importer
from workgate.hosted_chat.content_filters import ContentFilterBlocked
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.managed_skill_files import ManagedSkillFiles
from workgate.hosted_chat.managed_skills import ManagedSkills, SkillConflict
from workgate.hosted_chat.storage import WorkspaceRepository

MAIN = b"---\r\nname: review\r\ndescription: Review\r\nalways-apply: true\r\nuser-invocable: false\r\n---\r\n# Review\r\n"


def archive(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer, "w", compression=zipfile.ZIP_DEFLATED
    ) as zipped:
        for name, content in entries:
            zipped.writestr(name, content)
    return buffer.getvalue()


def test_import_tree_and_frontmatter(tmp_path):
    db = SQLiteDatabase(tmp_path)
    data = archive(
        [
            ("review/", b""),
            ("review/SKILL.md", MAIN),
            ("review/refs/note.txt", b"notes"),
            ("review/empty/", b""),
            ("review/assets/binary", b"\xff\x00"),
        ]
    )
    result = importer.import_skill(db, "alice", "review.zip", data)
    assert result["name"] == "review" and result["alwaysApply"] is True
    assert result["userInvocable"] is False and result["body"] == "# Review\n"
    assert result["fileCount"] == 2 and result["version"] == 1
    repo = ManagedSkillFiles(db)
    tree = repo.tree("alice", result["_id"])
    assert {node["relativePath"] for node in tree["nodes"]} == {
        "refs",
        "refs/note.txt",
        "empty",
        "assets",
        "assets/binary",
    }
    assert (
        repo.read_file("alice", result["_id"], "refs/note.txt")[1] == b"notes"
    )
    assert (
        repo.read_file("alice", result["_id"], "assets/binary")[1]
        == b"\xff\x00"
    )
    with pytest.raises(SkillConflict):
        importer.import_skill(db, "alice", "review.zip", data)
    assert len(ManagedSkills(db).list("alice")) == 1
    assert (
        importer.import_skill(db, "bob", "other.md", b"Plain text")["name"]
        == "other"
    )


@pytest.mark.parametrize(
    "entries",
    [
        [("no-main.txt", b"data")],
        [("SKILL.md", MAIN), ("second/SKILL.md", MAIN)],
        [("review/SKILL.md", MAIN), ("outside.txt", b"bad")],
        [("SKILL.md", MAIN), ("../escape", b"bad")],
        [("SKILL.md", MAIN), ("refs", b"file"), ("refs/child", b"bad")],
        [("SKILL.md", MAIN), ("skill.md", b"case collision")],
        [("SKILL.md", MAIN), ("a:bad", b"bad")],
    ],
)
def test_invalid_zip(tmp_path, entries):
    db = SQLiteDatabase(tmp_path)
    with pytest.raises(ValueError):
        importer.import_skill(db, "alice", "review.zip", archive(entries))
    assert ManagedSkills(db).list("alice") == []


@pytest.mark.parametrize(
    "text",
    [
        b"---\nname: review\n",
        b"---\nname: review\nname: other\n---\n",
        b"---\nname: &a review\ndescription: *a\n---\n",
        b"---\n- review\n---\n",
        b"\xff",
    ],
)
def test_invalid_frontmatter(tmp_path, text):
    db = SQLiteDatabase(tmp_path)
    with pytest.raises(ValueError):
        importer.import_skill(db, "alice", "review.md", text)
    assert ManagedSkills(db).list("alice") == []


def test_import_bounds_and_atomic_policy(tmp_path, monkeypatch):
    db = SQLiteDatabase(tmp_path)
    WorkspaceRepository(db).save_setting(
        "alice", "webui.filters.config", _policy("skills")
    )
    with pytest.raises(ContentFilterBlocked):
        importer.import_skill(
            db,
            "alice",
            "review.zip",
            archive([("SKILL.md", MAIN), ("note.txt", b"Bearer secret")]),
        )
    with db.read_snapshot() as conn:
        for table in (
            "webui_skills",
            "webui_skill_files",
            "webui_skill_folders",
        ):
            assert (
                conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            )
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError):
        importer.import_skill(
            db,
            "alice",
            "review.zip",
            archive([("SKILL.md", MAIN), (link, b"outside")]),
        )
    monkeypatch.setattr(importer, "_MAX_ENTRY", len(MAIN))
    with pytest.raises(ValueError):
        importer.import_skill(
            db,
            "alice",
            "review.zip",
            archive([("SKILL.md", MAIN), ("big", b"x" * (len(MAIN) + 1))]),
        )
    with pytest.raises(ValueError):
        importer.import_skill(db, "alice", "../review.md", MAIN)


@pytest.mark.asyncio
async def test_import_route(tmp_path):
    runtime = runtime_for(tmp_path)
    async with (
        runtime.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_http_app(runtime=runtime)),
            base_url="http://testserver",
        ) as client,
    ):
        path = "/api/ui/chat/skills/import"
        payload = {
            "filename": "review.md",
            "base64": base64.b64encode(MAIN).decode(),
        }
        result = await client.post(path, json=payload)
        assert result.status_code == 201, result.text
        assert result.json()["name"] == "review"
        assert (await client.post(path, json=payload)).status_code == 409
        assert (
            await client.post(path, json={"filename": "bad", "base64": "!"})
        ).status_code == 400
