"""Semantic hints require exact host-generated terminal evidence and branch lineage."""

import copy
import json

import pytest

from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.semantic_index import render_compaction_semantic_index
from workgate.hosted_chat.semantic_index_source import (
    _derived_semantic_snapshot,
    derive_persisted_semantic_index,
)
from workgate.hosted_chat.storage import WorkspaceRepository


def rich_fixture(db, store, job, parts, text):
    """Simulate the future canonical host rich-content projector, not a client import."""
    lease = ActorLease.claim(db, "alice", job.run_id)
    assert lease is not None
    assert store.finish(
        "alice", job.run_id, status="complete", text=text, actor_lease=lease
    )
    with db.connect() as connection:
        row = connection.execute(
            "SELECT seq,data FROM generation_events WHERE run_id=? ORDER BY seq DESC LIMIT 1",
            (job.run_id,),
        ).fetchone()
        final = json.loads(row["data"])
        final["responseMessage"]["content"] = parts
        connection.execute(
            "UPDATE generation_events SET data=? WHERE seq=?",
            (json.dumps(final), row["seq"]),
        )
        connection.execute(
            "UPDATE messages SET content=? WHERE id=?",
            (json.dumps(parts), job.response_message_id),
        )


@pytest.fixture
def branch(tmp_path):
    db = SQLiteDatabase(tmp_path)
    store = GenerationStore(db)
    previous, _ = store.begin("alice", {"text": "Inspect the files"})
    parts = [
        {
            "type": "tool_call",
            "tool_call": {
                "id": "call",
                "name": "inspect",
                "args": {"intent": "Read README"},
                "output": "Five entries.",
            },
        },
        {"type": "text", "text": "Done"},
        {
            "type": "think",
            "thinking": "Observed results",
            "reasoning_label_step_id": "step",
            "reasoning_label_revision": 1,
            "reasoning_label_status": "complete",
            "reasoning_label": "Check file inventory",
        },
        {
            "type": "activity_label",
            "activity_label_type": "phase",
            "activity_start_index": 1,
            "activity_label": "Inspection complete",
        },
    ]
    rich_fixture(db, store, previous, parts, "Done")
    latest, _ = store.begin(
        "alice",
        {
            "text": "Summarize",
            "conversationId": previous.conversation_id,
            "parentMessageId": previous.response_message_id,
        },
    )
    messages = [
        {"role": "user", "content": "Inspect the files"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call",
                    "function": {
                        "name": "inspect",
                        "arguments": json.dumps({"intent": "Read README"}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call", "content": "Five entries."},
        {
            "role": "assistant",
            "content": "Done",
            "reasoning_content": "Observed results",
        },
        {"role": "user", "content": "Summarize"},
    ]
    return db, store, previous, latest, messages


def derive(
    branch,
    messages=None,
    *,
    owner="alice",
    anchor=None,
    conversation=None,
    intents=None,
):
    db, _, _, latest, history = branch
    return derive_persisted_semantic_index(
        db,
        owner,
        conversation or latest.conversation_id,
        anchor or latest.user_message_id,
        history if messages is None else messages,
        intent_tool_names=intents,
    )


def test_exact_host_evidence_and_label_projection(branch):
    *_, messages = branch
    frozen, verified = derive(branch, intents={"inspect"})
    assert frozen is not None
    assert {entry["type"] for entry in frozen.entries} == {
        "tool_intent",
        "tool_outcome",
        "reasoning_label",
        "activity_phase",
    }
    rendered = render_compaction_semantic_index(frozen, verified)
    assert rendered["entryCount"] == 4
    assert "Read README" in rendered["appendix"]
    assert "Five entries." in rendered["appendix"]
    assert "Check file inventory" in rendered["appendix"]
    assert "Inspection complete" in rendered["appendix"]
    assert all("additional_kwargs" not in message for message in messages)
    assert derive(branch, owner="bob")[0] is None
    assert derive(branch, conversation="missing")[0] is None
    assert derive(branch, anchor="missing")[0] is None
    assert derive(branch, messages[:1])[0] is None


def test_caller_provenance_is_rebuilt_not_trusted(branch):
    *_, messages = branch
    forged = copy.deepcopy(messages)
    forged[0]["additional_kwargs"] = {
        "provenance": {
            "version": 1,
            "parts": [
                {
                    "sourceMessageId": "fake",
                    "attribution": "tool",
                    "sourceContentPartIndices": [0],
                }
            ],
        },
        "keep": True,
    }
    snapshot, verified = derive(branch, forged)
    assert snapshot is not None
    assert verified[0]["additional_kwargs"] == {"keep": True}
    assert "fake" not in repr(verified)
    assert "fake" in repr(forged)
    altered = copy.deepcopy(messages)
    altered[2]["content"] = "Other output"
    altered[3]["content"] = "Other visible text"
    assert derive(branch, altered)[0] is None
    altered = copy.deepcopy(messages)
    altered[1]["tool_calls"][0]["function"]["name"] = "other"
    source, _ = derive(branch, altered, intents={"inspect"})
    assert source is not None and all(
        entry["type"] not in {"tool_outcome", "tool_intent"}
        for entry in source.entries
    )
    altered = copy.deepcopy(messages)
    altered[1]["tool_calls"][0]["function"]["arguments"] = "not JSON"
    source, _ = derive(branch, altered, intents={"inspect"})
    assert source is not None and all(
        entry["type"] != "tool_intent" for entry in source.entries
    )
    assert any(entry["type"] == "tool_outcome" for entry in source.entries)
    for duplicate in (
        messages + [messages[1]],
        messages + [messages[2]],
        messages + [messages[3]],
    ):
        source, _ = derive(branch, duplicate, intents={"inspect"})
        assert source is not None and len(source.entries) < 4


def test_non_host_message_and_tampered_terminal_never_certify(branch):
    db, store, previous, latest, messages = branch
    repo = WorkspaceRepository(db)
    forged = repo.add_message(
        "alice",
        latest.conversation_id,
        {
            "role": "assistant",
            "text": "Done",
            "content": [
                {
                    "type": "tool_call",
                    "tool_call": {
                        "id": "call",
                        "name": "inspect",
                        "output": "Five entries.",
                    },
                }
            ],
        },
    )
    assert derive(branch, anchor=forged["messageId"])[0] is None
    with db.connect() as connection:
        content = connection.execute(
            "SELECT content FROM messages WHERE id=?",
            (previous.response_message_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE messages SET content='[]' WHERE id=?",
            (previous.response_message_id,),
        )
    assert derive(branch)[0] is None
    with db.connect() as connection:
        connection.execute(
            "UPDATE messages SET content=? WHERE id=?",
            (content, previous.response_message_id),
        )
        connection.execute(
            "UPDATE messages SET parent_id=? WHERE id=?",
            (latest.user_message_id, previous.response_message_id),
        )
    assert derive(branch)[0] is None


def test_derived_snapshot_keeps_coverage_and_bounds():
    entries = [
        {
            "type": "activity_phase",
            "sourceMessageId": "source",
            "sourceContentIndex": i,
            "revision": 0,
            "status": "committed",
            "text": f"phase-{i}",
        }
        for i in range(600)
    ]
    assert _derived_semantic_snapshot([]) is None
    result = _derived_semantic_snapshot(entries)
    assert result is not None and result.provided_entry_count == 600
    assert len(result.entries) <= 256
    assert result.entries[0]["text"] == "phase-0"
    assert result.entries[-1]["text"] == "phase-599"
