"""Pinned source reducer semantics and owned LangGraph persistence."""

from typing import cast

import pytest
from typing_extensions import TypedDict

from workgate.hosted_chat.checkpoints import EVENT_ACTOR_KEY
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import (
    ActorLease,
    ActorLeaseLost,
)
from workgate.hosted_chat.message_reducers import (
    REMOVE_ALL_MESSAGES,
    agent_messages_reducer,
    create_remove_all_message,
    messages_state_reducer,
)


def test_message_ids_types_clear_and_replacements():
    def mk(ident):
        return {"role": "user", "id": ident, "content": str(ident)}

    assert messages_state_reducer([mk(True)], [mk(1)]) == [mk(True), mk(1)]
    assert messages_state_reducer([mk(1)], [mk(1.0)]) == [mk(1.0)]
    assert messages_state_reducer([mk(False)], [mk(False)]) == [mk(False)]
    ident = object()
    assert messages_state_reducer([mk(ident)], [mk(ident)])[0]["id"] is ident
    assert messages_state_reducer([mk("a")], [None, mk("b")]) == [
        mk("a"),
        mk("b"),
    ]
    assert messages_state_reducer([mk("x")], create_remove_all_message()) == []
    assert create_remove_all_message() == {
        "type": "remove",
        "role": "remove",
        "id": REMOVE_ALL_MESSAGES,
    }
    assert agent_messages_reducer([mk("old")], [mk("replacement")]) == [
        mk("replacement")
    ]
    with pytest.raises(TypeError):
        messages_state_reducer([mk("a")], [3])


def test_message_remove_restore_clear_and_generated_ids():
    a = {"id": "a", "content": "old"}
    b = {"id": "b", "content": "new"}
    removal = {"type": "remove", "id": "a"}
    assert messages_state_reducer([a, b], [removal]) == [b]
    replacement = {"id": "a", "content": "restored"}
    assert messages_state_reducer([a, b], [removal, replacement]) == [
        replacement,
        b,
    ]
    assert messages_state_reducer(
        [a], [create_remove_all_message(), removal]
    ) == [removal]
    with pytest.raises(ValueError, match="doesn't exist"):
        messages_state_reducer([], [removal])
    bare = {"role": "user"}
    assert messages_state_reducer([], bare, id_factory=lambda: "generated") == [
        {"role": "user", "id": "generated"}
    ]
    assert bare["id"] == "generated"


@pytest.fixture
def owned(tmp_path):
    pytest.importorskip("langgraph")
    from langchain_core.runnables import RunnableConfig

    from workgate.hosted_chat.graph_checkpoint import OwnedGraphCheckpointer

    db = SQLiteDatabase(tmp_path)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO conversations(id,owner) VALUES('chat','alice')"
        )
        conn.execute(
            "INSERT INTO runs(id,conversation_id,status) VALUES('run','chat','running')"
        )
    lease = ActorLease.claim(db, "alice", "run")
    assert lease is not None
    saver = OwnedGraphCheckpointer(db, "alice", actor_lease=lease)
    config = cast(
        RunnableConfig,
        saver.storage.config("chat", "run:graph:", event_actor=True),
    )
    return db, lease, saver, config


def present(value):
    assert value is not None
    return value


@pytest.mark.asyncio
async def test_typed_tuples_async_writes_filters_and_owner_fence(owned):
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.types import Send

    from workgate.hosted_chat.graph_checkpoint import OwnedGraphCheckpointer

    db, lease, saver, config = owned
    assert saver.get_tuple(config) is None
    with pytest.raises(ValueError, match="thread config"):
        list(saver.list(None))
    state = empty_checkpoint()
    state["id"] = "c1"
    first = saver.put(config, state, {"source": "input", "step": 0}, {})
    assert present(saver.get_tuple(config)).checkpoint == state
    send = Send("child", {"messages": [{"role": "user", "content": "go"}]})
    await saver.aput_writes(first, [("tasks", send)], "task-1")
    second_state = state.copy()
    second_state["id"] = "c2"
    second = await saver.aput(
        first, second_state, {"source": "loop", "step": 1}, {}
    )
    assert [row.checkpoint["id"] for row in saver.list(config)] == ["c2", "c1"]
    assert [
        row.checkpoint["id"]
        for row in saver.list(config, limit=1, filter={"step": 0})
    ] == ["c1"]
    assert [
        row.checkpoint["id"] for row in saver.list(config, before=second)
    ] == ["c1"]
    assert list(saver.list(config, limit=0)) == []
    with pytest.raises(ValueError, match="limit"):
        list(saver.list(config, limit=-1))
    rows = [row async for row in saver.alist(config)]
    assert present(rows[-1].pending_writes)[0][2] == send
    assert present(await saver.aget_tuple(second)).metadata["step"] == 1
    with pytest.raises(PermissionError):
        OwnedGraphCheckpointer(db, "bob", actor_lease=lease)
    for wrong in (
        saver.storage.config("other", "run:graph:", event_actor=True),
        saver.storage.config("chat", "another:graph:", event_actor=True),
        saver.storage.config("chat", "run", event_actor=True),
        saver.storage.config("chat", "run:graph:"),
    ):
        with pytest.raises(ActorLeaseLost):
            saver.get_tuple(cast(RunnableConfig, wrong))
    with db.connect() as conn:
        conn.execute(
            "UPDATE generation_actor_leases SET expires_at=0 WHERE run_id='run'"
        )
    replacement = ActorLease.claim(db, "alice", "run")
    assert replacement is not None
    with pytest.raises(ActorLeaseLost):
        saver.get_tuple(second)
    with pytest.raises(ActorLeaseLost):
        await saver.aput_writes(second, [("tasks", send)], "stale")
    fresh = OwnedGraphCheckpointer(db, "alice", actor_lease=replacement)
    assert present(fresh.get_tuple(second)).checkpoint["id"] == "c2"


class GraphState(TypedDict):
    value: int


@pytest.mark.asyncio
async def test_real_langgraph_resume_does_not_repeat_completed_node(owned):
    from langgraph.graph import END, START, StateGraph

    from workgate.hosted_chat.graph_checkpoint import OwnedGraphCheckpointer

    db, lease, saver, config = owned
    calls = []

    def first(state: GraphState):
        calls.append("first")
        return {"value": state["value"] + 1}

    def second(state: GraphState):
        calls.append("second")
        return {"value": state["value"] + 1}

    graph = StateGraph(GraphState)
    graph.add_node("first", first)
    graph.add_node("second", second)
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    paused = graph.compile(checkpointer=saver, interrupt_before=["second"])
    assert (await paused.ainvoke({"value": 0}, config))["value"] == 1
    assert calls == ["first"]
    lease.release()
    fresh = ActorLease.claim(db, "alice", "run")
    assert fresh is not None
    restarted = OwnedGraphCheckpointer(db, "alice", actor_lease=fresh)
    resumed = graph.compile(checkpointer=restarted)
    assert (await resumed.ainvoke(None, config))["value"] == 2
    assert calls == ["first", "second"]
    assert (
        present(restarted.get_tuple(config)).config.get("configurable", {})[
            EVENT_ACTOR_KEY
        ]
        == "run:graph:"
    )
