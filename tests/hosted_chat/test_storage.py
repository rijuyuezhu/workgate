"""Contract tests for the Python LibreChat-compatible persistence layer."""

from pathlib import Path

import pytest

from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.storage import WorkspaceRepository


@pytest.fixture
def repo(tmp_path: Path) -> WorkspaceRepository:
    return WorkspaceRepository(SQLiteDatabase(tmp_path))


def test_project_and_conversation_roundtrip(repo: WorkspaceRepository) -> None:
    project = repo.create_project(
        "user-a", "Research", "Experiments", color="blue"
    )
    assert len(project["_id"]) == 24
    assert project["color"] == "blue"
    convo = repo.create_conversation(
        "user-a",
        {
            "title": "Example",
            "projectId": project["_id"],
            "endpoint": "agents",
            "memory": True,
        },
    )
    assert convo["title"] == "Example"
    assert convo["memory"] is True
    assert convo["projectId"] == project["_id"]
    assert repo.conversation("user-b", convo["conversationId"]) is None
    assert repo.projects("user-a")["projects"][0]["_id"] == project["_id"]
    updated = repo.update_conversation(
        "user-a",
        convo["conversationId"],
        {"isArchived": True, "title": "Updated"},
    )
    assert updated is not None
    assert updated["isArchived"] is True
    assert updated["title"] == "Updated"
    assert (
        repo.conversations("user-a", archived=True)["conversations"][0]["title"]
        == "Updated"
    )
    assert repo.conversations("user-a")["conversations"] == []
    assert (
        repo.update_conversation(
            "user-b", convo["conversationId"], {"title": "Bypass"}
        )
        is None
    )
    assert repo.delete_project("user-b", project["_id"]) is False
    assert repo.delete_project("user-a", project["_id"]) is True
    current = repo.conversation("user-a", convo["conversationId"])
    assert current is not None and current["projectId"] is None


def test_messages_pagination_and_search(repo: WorkspaceRepository) -> None:
    cid = repo.create_conversation("owner", {"title": "南京大学"})[
        "conversationId"
    ]
    for i in range(8):
        repo.add_message(
            "owner",
            cid,
            {"role": "user", "text": f"南京大学问题 {i}", "messageId": f"m{i}"},
        )
    page1 = repo.messages("owner", cid, limit=3)
    page2 = repo.messages("owner", cid, limit=3, cursor=page1["nextCursor"])
    page3 = repo.messages("owner", cid, limit=3, cursor=page2["nextCursor"])
    assert len(page1["messages"]) == 3
    assert len(page2["messages"]) == 3
    assert len(page3["messages"]) == 2
    assert not set(m["messageId"] for m in page1["messages"]) & set(
        m["messageId"] for m in page2["messages"]
    )
    assert len(repo.search_messages("owner", "南京大")) == 8
    assert len(repo.search_messages("owner", "大学")) == 8
    assert repo.search_messages("another", "南京大") == []
    assert (
        len(repo.conversations("owner", search="南京大学")["conversations"])
        == 1
    )
    assert repo.delete_conversation("owner", cid) is True
    assert repo.search_messages("owner", "南京大") == []
    with pytest.raises(KeyError):
        repo.messages("owner", cid)


def test_conversation_owner_isolation(repo: WorkspaceRepository) -> None:
    cid = repo.create_conversation("alice", {"title": "Private"})[
        "conversationId"
    ]
    with pytest.raises(KeyError):
        repo.add_message("bob", cid, {"text": "attack"})
    assert repo.delete_conversation("bob", cid) is False
    current = repo.conversation("alice", cid)
    assert current is not None and current["title"] == "Private"


def test_conversation_cursor_is_stable(repo: WorkspaceRepository) -> None:
    for i in range(6):
        repo.create_conversation("owner", {"title": f"Chat {i}"})
    seen: list[str] = []
    cursor = None
    while True:
        page = repo.conversations("owner", limit=2, cursor=cursor)
        seen.extend(item["conversationId"] for item in page["conversations"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 6


def test_user_settings_are_namespaced(repo: WorkspaceRepository) -> None:
    repo.save_setting("alice", "chat", {"model": "gpt-4.1"})
    assert repo.setting("alice", "chat")["model"] == "gpt-4.1"
    assert repo.setting("bob", "chat") is None


def test_owner_scoped_references(repo: WorkspaceRepository) -> None:
    project = repo.create_project("alice", "private")
    with repo.database.connect() as db:
        db.execute(
            "INSERT INTO agents(id,owner,name) VALUES ('agent-a','alice','private')"
        )
    for field, ident in (("projectId", project["id"]), ("agent_id", "agent-a")):
        with pytest.raises(KeyError):
            repo.create_conversation("bob", {field: ident})
        convo = repo.create_conversation("bob", {})
        with pytest.raises(KeyError):
            repo.update_conversation(
                "bob", convo["conversationId"], {field: ident}
            )
        assert repo.create_conversation("alice", {field: ident})[field] == ident
    assert repo.project("bob", project["id"]) is None
    assert repo.update_project("bob", project["id"], {"name": "attack"}) is None
    with pytest.raises(ValueError):
        repo.create_project("alice", " ")
    with pytest.raises(ValueError):
        repo.update_project("alice", project["id"], {"name": " "})
    updated = repo.update_project(
        "alice", project["id"], {"name": "new", "color": "red"}
    )
    assert updated is not None and updated["color"] == "red"
    assert repo.project("alice", project["id"]) == updated


def test_literal_search_and_bookmarks(repo: WorkspaceRepository) -> None:
    for text, tags in (("100%_done", ["a%b"]), ("100xxdone", ["axb"])):
        convo = repo.create_conversation(
            "owner", {"title": text, "tags": tags, "pinned": True}
        )
        repo.add_message("owner", convo["conversationId"], {"text": text})
        repo.create_project("owner", text)
    assert len(repo.conversations("owner", search="%_")["conversations"]) == 1
    assert (
        len(
            repo.conversations("owner", tags=["a%b"], pinned=True)[
                "conversations"
            ]
        )
        == 1
    )
    assert (
        len(
            repo.conversations("owner", project_id="unassigned")[
                "conversations"
            ]
        )
        == 2
    )
    assert len(repo.projects("owner", search="%_")["projects"]) == 1
    assert len(repo.search_messages("owner", "%_")) == 1
    assert len(repo.search_messages("owner", "100%_done")) == 1
    assert repo.search_messages("owner", " ") == []
    assert repo.search_messages("other", "%_") == []
    repo.database.fts_enabled = False
    assert len(repo.search_messages("owner", "100%_done")) == 1
    assert len(repo.search_messages("owner", "%_", limit=-1)) == 1


def test_settings_atomic_merge_and_cursor_errors(
    repo: WorkspaceRepository,
) -> None:
    assert repo.merge_setting("alice", "prefs", {"x": 1}, max_bytes=100) == {
        "x": 1
    }
    assert repo.merge_setting("alice", "prefs", {"y": 2}, max_bytes=100) == {
        "x": 1,
        "y": 2,
    }
    with pytest.raises(ValueError):
        repo.merge_setting("alice", "prefs", {"large": "x" * 100}, max_bytes=20)
    assert repo.setting("alice", "prefs") == {"x": 1, "y": 2}
    cid = repo.create_conversation("alice", {"conversationId": "id"})[
        "conversationId"
    ]
    with pytest.raises(ValueError):
        repo.create_conversation("bob", {"conversationId": cid})
    for cursor in ("bad!", "", "a" * 4097):
        with pytest.raises(ValueError):
            repo.messages("alice", cid, cursor=cursor)
        with pytest.raises(ValueError):
            repo.conversations("alice", cursor=cursor)


def test_concurrent_metadata_updates_are_serialized(
    repo: WorkspaceRepository,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    cid = repo.create_conversation("owner", {})["conversationId"]
    barrier = Barrier(4)

    def update(index: int) -> None:
        barrier.wait(timeout=5)
        repo.update_conversation("owner", cid, {f"key-{index}": index})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(update, range(4)))
    current = repo.conversation("owner", cid)
    assert current is not None
    assert [current[f"key-{index}"] for index in range(4)] == list(range(4))
