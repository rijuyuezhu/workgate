"""Final upstream resource operator tests adapted to native revision fences."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from workgate.hosted_chat.agent_validation import clean_agent_config
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.attachment_deletion import AttachmentDeletionStore
from workgate.hosted_chat.attachment_uploads import publish_attachment
from workgate.hosted_chat.database import SQLiteDatabase


@pytest.fixture
def repo(tmp_path):
    return AgentRepository(SQLiteDatabase(tmp_path))


def agent(repo, owner="alice", **values):
    return repo.create(owner, {"provider": "openai", "model": None, **values})


def file(repo, identity, owner="alice"):
    with repo.database.connect() as db:
        db.execute(
            "INSERT INTO attachments(id,owner,name,path,content_type,metadata) VALUES(?,?,?,?,?,?)",
            (
                identity,
                owner,
                identity + ".txt",
                "/private/" + identity,
                "text/plain",
                "{}",
            ),
        )


def resources(*ids):
    return {"context": {"file_ids": list(ids)}}


def remove(*ids):
    return [
        {"tool_resource": "context", "file_id": identity} for identity in ids
    ]


def test_versioned_add_and_detach_preserve_membership_and_history(repo):
    aid = agent(repo)["id"]
    file(repo, "owned")
    added = repo.add_resource_file("alice", aid, "context", "owned")
    assert added["tools"] == ["context"]
    assert added["tool_resources"] == resources("owned")
    assert added["version"] == 2
    repeated = repo.add_resource_file("alice", aid, "context", "owned")
    assert repeated["version"] == 3
    assert repeated["tool_resources"] == resources("owned")
    removed = repo.remove_resource_files("alice", aid, remove("owned", "owned"))
    assert removed["version"] == 4
    assert removed["tools"] == ["context"]
    assert removed["tool_resources"] == resources()
    assert repo.remove_resource_files("alice", aid, remove("absent")) == removed
    snapshots = repo.versions("alice", aid)
    assert snapshots is not None
    assert [v["version"] for v in snapshots] == [4, 3, 2, 1]
    assert snapshots[1]["tool_resources"] == resources("owned")


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"ocr": {"file_ids": []}},
        {"context": None},
        {"context": {"file_ids": [""]}},
        {"context": {"file_ids": ["a" * 129]}},
        {"context": {"file_ids": ["a"] * 101}},
        {"context": {"file_ids": [1]}},
        {"context": {"files": []}},
        {"context": {"file_ids": "abc"}},
    ],
)
def test_reject_unsupported_or_invalid_resource_fields(value):
    with pytest.raises(ValueError):
        clean_agent_config({"tool_resources": value})


def test_direct_authoring_clone_restore_and_owner_fences(repo):
    file(repo, "owned")
    file(repo, "foreign", owner="bob")
    with pytest.raises(ValueError):
        agent(repo, tool_resources=resources("foreign"))
    created = agent(repo, tool_resources=resources("owned", "owned"))
    aid = created["id"]
    assert created["tool_resources"] == resources("owned")
    assert repo.get("bob", aid) is None
    for owner, identity in [("bob", aid), ("alice", "missing")]:
        with pytest.raises(KeyError):
            repo.add_resource_file(owner, identity, "context", "owned")
        with pytest.raises(KeyError):
            repo.remove_resource_files(owner, identity, [])
    for identity in ["foreign", "missing"]:
        with pytest.raises(ValueError, match="not available"):
            repo.add_resource_file("alice", aid, "context", identity)
        with pytest.raises(ValueError, match="not available"):
            repo.update("alice", aid, {"tool_resources": resources(identity)})
    clone = repo.duplicate("alice", aid)
    assert clone is not None
    assert clone["agent"]["tool_resources"] == resources("owned")
    removed = repo.remove_resource_files("alice", aid, remove("owned"))
    assert removed["version"] == 2
    restored = repo.revert("alice", aid, 1)
    assert restored is not None
    assert restored["tool_resources"] == resources("owned")
    assert restored["version"] == 3


def test_prepared_deletion_fences_binding_and_transaction_rolls_back(repo):
    aid = agent(repo)["id"]
    file(repo, "owned")
    with repo.database.connect() as db:
        db.execute(
            "INSERT INTO attachment_deletions(file_id,owner,original_path,quarantine_path,phase) VALUES(?,?,?,?,?)",
            (
                "owned",
                "alice",
                "/private/owned",
                "/private/.delete-owned",
                "prepared",
            ),
        )
    with pytest.raises(ValueError, match="not available"):
        repo.add_resource_file("alice", aid, "context", "owned")
    with repo.database.connect() as db:
        db.execute("DELETE FROM attachment_deletions")
    with repo.database.connect() as db:
        with pytest.raises(ValueError, match="active transaction"):
            repo.add_resource_file("alice", aid, "context", "owned", db=db)
        with pytest.raises(ValueError, match="active transaction"):
            repo.remove_resource_files("alice", aid, [], db=db)
    with (
        pytest.raises(RuntimeError, match="rollback"),
        repo.database.connect() as db,
    ):
        db.execute("BEGIN IMMEDIATE")
        repo.add_resource_file("alice", aid, "context", "owned", db=db)
        raise RuntimeError("rollback")
    assert repo.get("alice", aid)["version"] == 1


@pytest.mark.parametrize(
    "resource,identity", [("ocr", "a"), ("context", ""), ("context", None)]
)
def test_invalid_add(repo, resource, identity):
    with pytest.raises(ValueError):
        repo.add_resource_file("alice", agent(repo)["id"], resource, identity)


@pytest.mark.parametrize(
    "files",
    [
        None,
        [None],
        [{"tool_resource": "ocr", "file_id": "x"}],
        remove(""),
        remove("x" * 129),
        remove(*(["x"] * 101)),
    ],
)
def test_invalid_remove(repo, files):
    with pytest.raises(ValueError):
        repo.remove_resource_files("alice", agent(repo)["id"], files)


def test_concurrent_adds_keep_shared_members_and_revisions(repo):
    aid = agent(repo)["id"]
    ids = ["parallel-" + str(i) for i in range(8)]
    for identity in ids:
        file(repo, identity)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda identity: repo.add_resource_file(
                    "alice", aid, "context", identity
                ),
                ids,
            )
        )
    current = repo.get("alice", aid)
    assert current is not None
    assert sorted(current["tool_resources"]["context"]["file_ids"]) == sorted(
        ids
    )
    assert current["version"] == 9


@pytest.mark.asyncio
async def test_shared_blob_preserved_until_all_agents_detach(repo):
    aid = agent(repo)["id"]

    async def chunks():
        yield b"document"

    result = await publish_attachment(
        repo.database,
        "alice",
        "test.txt",
        "text/plain",
        chunks(),
        expected_size=8,
        check_grant=lambda: None,
    )
    ident = result["file_id"]
    with repo.database.connect() as db:
        path = Path(
            db.execute(
                "SELECT path FROM attachments WHERE id=?", (ident,)
            ).fetchone()["path"]
        )
    repo.add_resource_file("alice", aid, "context", ident)
    clone = repo.duplicate("alice", aid)
    assert clone is not None
    other = clone["agent"]["id"]
    store = AttachmentDeletionStore(repo.database)
    assert store.delete("alice", [ident], check_grant=lambda: None)[
        "failedFileIds"
    ] == [ident]
    repo.remove_resource_files("alice", aid, remove(ident))
    assert store.delete("alice", [ident], check_grant=lambda: None)[
        "failedFileIds"
    ] == [ident]
    assert path.read_bytes() == b"document"
    repo.remove_resource_files("alice", other, remove(ident))
    assert store.delete("alice", [ident], check_grant=lambda: None)[
        "deletedFileIds"
    ] == [ident]
    assert not path.exists()
    with pytest.raises(ValueError, match="not available"):
        repo.revert("alice", aid, 1)
    assert repo.get("alice", aid)["tool_resources"] == resources()


def test_empty_resource_configuration(repo):
    assert agent(repo, tool_resources={})["tool_resources"] == {}
    assert (
        agent(repo, tool_resources={"context": {}})["tool_resources"]
        == resources()
    )
