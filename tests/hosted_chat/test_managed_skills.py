"""Owner-local managed records, revision and live protection checks."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.hosted_chat.test_content_filters import _policy
from workgate.hosted_chat import managed_skills as skills
from workgate.hosted_chat.content_filters import ContentFilterBlocked
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.storage import WorkspaceRepository


@pytest.fixture
def state(tmp_path):
    db = SQLiteDatabase(tmp_path)
    repo = skills.ManagedSkills(db)
    body = {
        "name": "review",
        "description": "Review results.",
        "body": "# Review",
        "frontmatter": {
            "user-invocable": False,
            "disable-model-invocation": True,
            "allowed-tools": ["task"],
        },
        "alwaysApply": True,
    }
    return db, repo, body


def test_owner_catalog_and_revisions(state):
    _, repo, body = state
    item = repo.create("alice", body)
    sid = item["_id"]
    assert item["version"] == 1 and item["source"] == "inline"
    assert (
        item["userInvocable"] is False
        and item["disableModelInvocation"] is True
    )
    assert item["allowedTools"] == ["task"]
    assert repo.get("alice", sid) == item
    assert "body" not in repo.list("alice")[0]
    assert repo.list("bob") == []
    repo.create("bob", body)
    with pytest.raises(skills.SkillConflict):
        repo.create("alice", body)
    for action in (
        lambda: repo.get("bob", sid),
        lambda: repo.update("bob", sid, {"expectedVersion": 1}),
        lambda: repo.delete("bob", sid),
        lambda: repo.update_states("bob", {sid: False}),
    ):
        with pytest.raises(KeyError):
            action()
    updated = repo.update(
        "alice",
        sid,
        {"expectedVersion": 1, "name": "new-review", "body": "Updated"},
    )
    assert updated["version"] == 2 and updated["body"] == "Updated"
    with pytest.raises(skills.SkillConflict):
        repo.update("alice", sid, {"expectedVersion": 1, "body": "Stale"})
    repo.create("alice", body)
    with pytest.raises(skills.SkillConflict):
        repo.update("alice", sid, {"expectedVersion": 2, "name": "review"})
    assert repo.get("alice", sid) == updated
    assert repo.states("alice") == {}
    states = {sid: False, "deployment:installed": True}
    assert repo.update_states("alice", states) == states
    assert repo.states("alice") == states and repo.states("bob") == {}
    repo.delete("alice", sid)
    assert repo.states("alice") == {"deployment:installed": True}
    with pytest.raises(KeyError):
        repo.get("alice", sid)


@pytest.mark.parametrize(
    "patch",
    [
        {"name": None},
        {"name": "../bad"},
        {"name": "Bad"},
        {"description": ""},
        {"description": "x" * 501},
        {"body": None},
        {"body": "x" * (512 * 1024 + 1)},
        {"displayTitle": 1},
        {"category": "x" * 129},
        {"frontmatter": []},
        {"frontmatter": {"user-invocable": "yes"}},
        {"frontmatter": {"allowed-tools": "task"}},
        {"frontmatter": {"large": "x" * 32768}},
        {"alwaysApply": 1},
        {"owner": "bob"},
    ],
)
def test_invalid_record(state, patch):
    _, repo, body = state
    with pytest.raises(ValueError):
        repo.create("alice", {**body, **patch})


def test_live_policy_and_concurrent_edit(state, monkeypatch):
    db, repo, body = state
    item = repo.create("alice", body)
    sid = item["_id"]
    WorkspaceRepository(db).save_setting(
        "alice", "webui.filters.config", _policy("skills")
    )
    with pytest.raises(ContentFilterBlocked):
        repo.create(
            "alice", {**body, "name": "blocked", "body": "Bearer secret"}
        )
    with pytest.raises(ContentFilterBlocked):
        repo.update(
            "alice", sid, {"expectedVersion": 1, "description": "Bearer secret"}
        )
    assert repo.get("alice", sid) == item

    def edit(index):
        try:
            return repo.update(
                "alice", sid, {"expectedVersion": 1, "body": str(index)}
            )
        except skills.SkillConflict:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert (
            sum(result is not None for result in pool.map(edit, range(4))) == 1
        )
    monkeypatch.setattr(skills, "_MAX_SKILLS", 1)
    with pytest.raises(skills.SkillConflict):
        repo.create("alice", {**body, "name": "extra"})
    with pytest.raises(ValueError):
        repo.update("alice", sid, {"body": "missing version"})
    with pytest.raises(ValueError):
        repo.update_states("alice", {sid: 1})
    with pytest.raises(ValueError):
        repo.update_states("alice", {"deployment:../bad": False})
