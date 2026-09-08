import pytest

import workgate.remote_worker.dispatch as worker_dispatch
from workgate.remote.tool_specs import (
    REMOTE_WORKER_ORIGIN_ARG,
    REMOTE_WORKER_ORIGIN_HUMAN_UI,
    REMOTE_WORKER_TOOL_NAMES,
)
from workgate.remote_worker.dispatch import (
    WORKER_TOOL_NAMES,
    build_worker_dispatcher,
)


def test_worker_dispatcher_membership_matches_shared_capabilities_exactly() -> (
    None
):
    dispatcher = build_worker_dispatcher()

    assert frozenset(dispatcher.handlers) == REMOTE_WORKER_TOOL_NAMES
    assert WORKER_TOOL_NAMES == REMOTE_WORKER_TOOL_NAMES


def test_worker_dispatchers_are_fresh_and_handler_maps_are_immutable() -> None:
    first = build_worker_dispatcher()
    second = build_worker_dispatcher()

    assert first is not second
    assert first.handlers is not second.handlers
    with pytest.raises(TypeError):
        first.handlers["search"] = first.handlers["search"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_worker_dispatcher_override_binds_search_dependency() -> None:
    dependency = object()

    async def bound_search(args):
        return {
            "dependency": dependency,
            "query": args["query"],
        }

    dispatcher = build_worker_dispatcher(
        handler_overrides={"search": bound_search}
    )

    result = await dispatcher.execute(
        "search",
        {
            REMOTE_WORKER_ORIGIN_ARG: REMOTE_WORKER_ORIGIN_HUMAN_UI,
            "query": "needle",
        },
    )

    assert result == {"dependency": dependency, "query": "needle"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"target": "remote", "machine": "edge"},
        {"target": "local", "machine": "edge"},
    ],
)
async def test_worker_dispatcher_rejects_nested_remote_session_start(
    payload: dict[str, str],
) -> None:
    dispatcher = build_worker_dispatcher()

    with pytest.raises(ValueError, match="only local worker sessions"):
        await dispatcher.execute("session_start", payload)


@pytest.mark.asyncio
async def test_worker_dispatcher_local_session_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workgate.ops.session as session_ops

    calls = []

    async def start(workdir, target, machine, label):
        calls.append(("start", workdir, target, machine, label))
        return {"session_id": "SESSION1"}

    async def change(session_id, workdir):
        calls.append(("change", session_id, workdir))
        return {"session_id": session_id}

    async def end(session_id, *, force=False):
        calls.append(("end", session_id, force))
        return {"session_id": session_id}

    monkeypatch.setattr(session_ops, "session_start_execute", start)
    monkeypatch.setattr(session_ops, "session_change_cwd_execute", change)
    monkeypatch.setattr(session_ops, "session_end_execute", end)
    dispatcher = build_worker_dispatcher()

    session = await dispatcher.execute(
        "session_start",
        {
            "workdir": "/worker/work",
            "target": "local",
            "machine": None,
            "label": "worker-local",
        },
    )
    changed = await dispatcher.execute(
        "session_change_cwd",
        {"session_id": session["session_id"], "workdir": "/worker/work/child"},
    )
    ended = await dispatcher.execute(
        "session_end",
        {"session_id": session["session_id"], "force": True},
    )

    assert changed == {"session_id": "SESSION1"}
    assert ended == {"session_id": "SESSION1"}
    assert calls == [
        ("start", "/worker/work", "local", None, "worker-local"),
        ("change", "SESSION1", "/worker/work/child"),
        ("end", "SESSION1", True),
    ]


@pytest.mark.asyncio
async def test_worker_persistent_shell_start_records_shared_session_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workgate.ops.shell as shell_ops

    calls = []

    async def start(cwd, name, command, *, owner_session_id=None):
        calls.append((cwd, name, command, owner_session_id))
        return {"shell_id": "owned-shell"}

    monkeypatch.setattr(shell_ops, "start_persistent_shell_execute", start)

    result = await worker_dispatch._start_persistent_shell(
        {
            "session_id": "sess_shared-owner",
            "cwd": ".",
            "name": "owned",
            "command": "echo hi",
        }
    )

    assert result == {"shell_id": "owned-shell"}
    assert calls == [(".", "owned", "echo hi", "sess_shared-owner")]


@pytest.mark.asyncio
async def test_worker_audit_query_covers_snapshot_and_summary_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workgate.audit as audit

    calls = []

    def query_session(log_session_id, **filters):
        calls.append(("session", log_session_id, filters))
        return {"entries": [{"id": "one"}], "count": 1}

    def query_global(**filters):
        calls.append(("global", filters))
        return {"entries": [{"id": "two"}, "ignored"], "count": 2}

    monkeypatch.setattr(audit, "query_session_audit", query_session)
    monkeypatch.setattr(audit, "query_audit", query_global)
    monkeypatch.setattr(
        audit,
        "audit_query_snapshot",
        lambda result, selected_id: {
            "selected_id": selected_id,
            "count": result["count"],
        },
    )
    monkeypatch.setattr(
        audit,
        "summarize_audit_entry",
        lambda entry: {"summary": entry["id"]},
    )

    snapshot = await worker_dispatch._query_audit(
        {
            "log_session_id": "LOG1",
            "snapshot": True,
            "selected_id": "one",
            "limit": 7,
            "sort": "asc",
        }
    )
    summary = await worker_dispatch._query_audit({"summary_only": True})

    assert snapshot == {"selected_id": "one", "count": 1}
    assert summary["entries"] == [{"summary": "two"}]
    assert calls[0][0:2] == ("session", "LOG1")
    assert calls[0][2]["limit"] == 7
    assert calls[0][2]["sort"] == "asc"
    assert calls[1][0] == "global"


@pytest.mark.asyncio
async def test_worker_audit_entry_covers_session_and_global_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workgate.audit as audit

    calls = []

    def get_session(log_session_id, entry_id, *, include_full_payloads=False):
        calls.append(
            ("session", log_session_id, entry_id, include_full_payloads)
        )
        return {"id": entry_id, "scope": "session"}

    def get_global(entry_id, *, include_full_payloads=False):
        calls.append(("global", entry_id, include_full_payloads))
        return {"id": entry_id, "scope": "global"}

    monkeypatch.setattr(audit, "get_session_audit_entry", get_session)
    monkeypatch.setattr(audit, "get_audit_entry", get_global)

    session_entry = await worker_dispatch._get_audit_entry(
        {
            "log_session_id": "LOG1",
            "id": "ENTRY1",
            "include_full_payloads": True,
        }
    )
    global_entry = await worker_dispatch._get_audit_entry({"id": "ENTRY2"})

    assert session_entry == {"id": "ENTRY1", "scope": "session"}
    assert global_entry == {"id": "ENTRY2", "scope": "global"}
    assert calls == [
        ("session", "LOG1", "ENTRY1", True),
        ("global", "ENTRY2", False),
    ]


@pytest.mark.asyncio
async def test_worker_ui_session_snapshot_combines_todos_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workgate.audit as audit
    import workgate.ops.todo as todo_ops

    class Todos:
        def model_dump(self, *, mode):
            assert mode == "json"
            return {"items": ["one"]}

    calls = []

    def read_todos(session_id, *, touch_session=True):
        calls.append(("todos", session_id, touch_session))
        return Todos()

    def query_session(session_id, **filters):
        calls.append(("audit", session_id, filters))
        return {"entries": [{"id": "ENTRY1"}], "count": 1}

    monkeypatch.setattr(todo_ops, "read_todos_execute", read_todos)
    monkeypatch.setattr(audit, "query_session_audit", query_session)
    monkeypatch.setattr(
        audit,
        "audit_query_snapshot",
        lambda result, selected_id: {
            "selected_id": selected_id,
            "count": result["count"],
        },
    )

    result = await worker_dispatch._ui_session_snapshot(
        {
            "session_id": "SESSION1",
            "selected_id": "ENTRY1",
            "session": "ignored-filter",
            "limit": 9,
        }
    )

    assert result == {
        "todos": {"items": ["one"]},
        "audit": {"selected_id": "ENTRY1", "count": 1},
    }
    assert calls[0] == ("todos", "SESSION1", False)
    assert calls[1][0:2] == ("audit", "SESSION1")
    assert calls[1][2]["limit"] == 9
    assert "session" not in calls[1][2]


@pytest.mark.asyncio
async def test_worker_bash_adapter_maps_wire_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workgate.ops.bash as bash_ops

    calls = []

    async def execute(*args):
        calls.append(args)
        return {"ok": True}

    monkeypatch.setattr(bash_ops, "bash_execute", execute)

    result = await worker_dispatch._bash(
        {
            "session_id": "SESSION1",
            "command": "printf ok",
            "cwd": "src",
            "timeout_s": 12,
            "max_output_bytes": 4096,
            "env": {"KEY": "value"},
            "async_": True,
            "pty": False,
            "name": "probe",
        }
    )

    assert result == {"ok": True}
    assert calls == [
        (
            "SESSION1",
            "printf ok",
            "src",
            12,
            4096,
            {"KEY": "value"},
            True,
            False,
            "probe",
        )
    ]


def test_worker_dispatcher_rejects_unknown_override() -> None:
    async def handler(_args):
        return None

    with pytest.raises(
        ValueError, match="unknown remote worker handler override"
    ):
        build_worker_dispatcher(handler_overrides={"plugin": handler})
