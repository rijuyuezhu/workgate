import json
import multiprocessing
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import workgate.audit.payloads as audit_payload_module
from workgate.audit import (
    audit,
    audit_call_context,
    audit_query_snapshot,
    audit_tool_call_end,
    audit_tool_call_start,
    get_audit_entry,
    get_session_audit_entry,
    query_audit,
    query_session_audit,
    summarize_audit_entry,
)
from workgate.audit import core as audit_core
from workgate.audit.core import (
    _AUDIT_BINARY_KEY,
    _AUDIT_CYCLE_KEY,
    _coalesce_audit_records,
)
from workgate.audit.payloads import AUDIT_PAYLOAD_KEY
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.executor.tool_session import get_tool_session_store
from workgate.protocol.ids import new_session_id


def _configure_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_log_bytes: int = 20_000_000,
    max_event_bytes: int = 1_000_000,
    inline_bytes: int = 16 * 1024,
    max_payload_bytes: int = 64 * 1024 * 1024,
    max_payload_store_bytes: int = 256 * 1024 * 1024,
    payload_retention_s: int = 7 * 24 * 60 * 60,
    payloads_enabled: bool = True,
) -> Path:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_MAX_AUDIT_LOG_BYTES", str(max_log_bytes))
    monkeypatch.setenv("WORKGATE_MAX_AUDIT_EVENT_BYTES", str(max_event_bytes))
    monkeypatch.setenv(
        "WORKGATE_AUDIT_PAYLOADS_ENABLED",
        "true" if payloads_enabled else "false",
    )
    monkeypatch.setenv("WORKGATE_AUDIT_INLINE_VALUE_BYTES", str(inline_bytes))
    monkeypatch.setenv(
        "WORKGATE_MAX_AUDIT_PAYLOAD_BYTES", str(max_payload_bytes)
    )
    monkeypatch.setenv(
        "WORKGATE_MAX_AUDIT_PAYLOAD_STORE_BYTES",
        str(max_payload_store_bytes),
    )
    monkeypatch.setenv(
        "WORKGATE_AUDIT_PAYLOAD_RETENTION_S", str(payload_retention_s)
    )
    clear_settings_cache()
    return get_settings().audit_log_path


def _records(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _audit_process(
    workspace: str, state_dir: str, worker: int, events: int
) -> None:
    os.environ["WORKGATE_WORKSPACE_ROOT"] = workspace
    os.environ["WORKGATE_STATE_DIR"] = state_dir
    os.environ["WORKGATE_MAX_AUDIT_LOG_BYTES"] = "5000000"
    os.environ["WORKGATE_MAX_AUDIT_EVENT_BYTES"] = "100000"
    clear_settings_cache()
    for index in range(events):
        audit("process_event", worker=worker, index=index)


def _audit_payload_process(workspace: str, state_dir: str, worker: int) -> None:
    os.environ["WORKGATE_WORKSPACE_ROOT"] = workspace
    os.environ["WORKGATE_STATE_DIR"] = state_dir
    os.environ["WORKGATE_AUDIT_INLINE_VALUE_BYTES"] = "256"
    os.environ["WORKGATE_MAX_AUDIT_PAYLOAD_BYTES"] = "100000"
    os.environ["WORKGATE_MAX_AUDIT_PAYLOAD_STORE_BYTES"] = "200000"
    clear_settings_cache()
    audit(
        "process_payload",
        worker=worker,
        payload={
            "token": "cross-process-secret",
            "body": "shared-process-payload-" * 1_000,
        },
    )


def _join_processes(processes: list[Any], *, timeout_s: float = 90.0) -> None:
    """Wait for one spawned group against a shared deadline and reap stragglers."""
    deadline = time.monotonic() + timeout_s
    failures: list[str] = []
    try:
        for process in processes:
            process.join(timeout=max(0.0, deadline - time.monotonic()))
            if process.is_alive():
                failures.append(
                    f"{process.name} did not exit within shared {timeout_s}s deadline"
                )
            elif process.exitcode != 0:
                failures.append(
                    f"{process.name} exited with {process.exitcode}"
                )
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert failures == []


def test_audit_dual_writes_session_log_but_keeps_global_extras(
    tmp_path, monkeypatch
):
    global_path = _configure_audit(tmp_path, monkeypatch)
    store = get_tool_session_store()
    store.clear()
    session = store.create_session(
        session_id=str(new_session_id()), workdir=tmp_path, label="audit owner"
    )

    audit("global_extra", detail="not owned by a session")
    call_id = "dual-write-call"
    session_ids = audit_tool_call_start(
        call_id=call_id,
        transport="http",
        tool="read",
        input={"session_id": session.session_id, "path": "file.txt"},
    )
    with audit_call_context(call_id, session_ids):
        audit("nested_session_event", detail="owned")
    audit_tool_call_end(
        call_id=call_id,
        transport="http",
        tool="read",
        ok=True,
        duration_ms=2,
        output={"path": "file.txt"},
        session_ids=session_ids,
    )

    session_path = (
        tmp_path / ".state" / "sessions" / session.session_id / "audit.jsonl"
    )
    assert global_path.is_file()
    assert session_path.is_file()
    assert query_audit(event="global_extra")["count"] == 1
    assert (
        query_session_audit(session.session_id, event="global_extra")["count"]
        == 0
    )
    session_result = query_session_audit(session.session_id)
    assert session_result["count"] == 1
    entry = session_result["entries"][0]
    assert entry["id"] == f"call:{call_id}"
    assert entry["session"] == session.session_id
    assert entry["related_events"][0]["event"] == "nested_session_event"
    assert get_session_audit_entry(session.session_id, entry["id"]) == entry


def test_audit_skips_invalid_session_ids_without_leaving_running_calls(
    tmp_path, monkeypatch
):
    _configure_audit(tmp_path, monkeypatch)
    call_id = "invalid-session-id"

    session_ids = audit_tool_call_start(
        call_id=call_id,
        transport="http",
        tool="read",
        input={"session_id": "bad/id", "path": "missing.txt"},
    )
    audit_tool_call_end(
        call_id=call_id,
        transport="http",
        tool="read",
        ok=False,
        duration_ms=1,
        error={"type": "ValueError", "message": "invalid session"},
        session_ids=session_ids,
    )

    entry = get_audit_entry(f"call:{call_id}")
    assert entry["paired"] is True
    assert entry["status"] == "failed"
    assert entry["session"] == "bad/id"
    assert not (tmp_path / ".state" / "sessions" / "bad").exists()


def test_audit_uniformly_redacts_secrets_but_retains_fingerprints(
    tmp_path, monkeypatch
):
    path = _configure_audit(tmp_path, monkeypatch)
    pin = "correct-horse-battery-staple"
    raw_token = "ghp_1234567890abcdef1234567890abcdef"
    fingerprint = "0123456789abcdef"
    monkeypatch.setenv("WORKGATE_OAUTH_ADMIN_PIN", pin)
    clear_settings_cache()

    audit(
        "security_event",
        command=f"echo safe --api-key={raw_token} {pin}",
        headers={"Authorization": f"Bearer {raw_token}"},
        token=raw_token,
        token_sha256=fingerprint,
        token_fingerprint=fingerprint,
        url="https://files.test/download/sensitive-link-token",
        nested={"password": "hidden", "detail": f"Bearer {raw_token}"},
    )

    text = path.read_text(encoding="utf-8")
    record = _records(path)[0]
    assert raw_token not in text
    assert pin not in text
    assert "sensitive-link-token" not in text
    assert "/download/<redacted>" in text
    assert "echo safe" in record["command"]
    assert record["headers"]["Authorization"] == "<redacted>"
    assert record["token"] == "<redacted>"
    assert record["token_sha256"] == fingerprint
    assert record["token_fingerprint"] == fingerprint
    assert record["nested"]["password"] == "<redacted>"


def test_audit_values_are_portable_and_cycle_safe(tmp_path, monkeypatch):
    path = _configure_audit(tmp_path, monkeypatch)
    cycle: dict[str, Any] = {}
    cycle["self"] = cycle

    audit(
        "portable_event",
        payload={
            "cycle": cycle,
            "binary": b"\x00\xffpayload",
            "nan": float("nan"),
            "infinity": float("inf"),
            "path": tmp_path / "artifact.txt",
            "set": {"z", "a"},
            "object": object(),
        },
    )

    payload = _records(path)[0]["payload"]
    assert payload["cycle"]["self"] == {_AUDIT_CYCLE_KEY: "dict"}
    assert payload["binary"][_AUDIT_BINARY_KEY]["bytes"] == 9
    assert len(payload["binary"][_AUDIT_BINARY_KEY]["sha256"]) == 64
    assert payload["nan"] == "nan"
    assert payload["infinity"] == "inf"
    assert payload["path"].endswith("artifact.txt")
    assert payload["set"] == ["a", "z"]
    assert isinstance(payload["object"], str)


def test_audit_event_is_bounded_without_losing_identity(tmp_path, monkeypatch):
    path = _configure_audit(
        tmp_path,
        monkeypatch,
        max_log_bytes=10_000,
        max_event_bytes=1_200,
    )

    audit(
        "large_event",
        call_id="call-large",
        tool="read",
        payload={
            "large": "x" * 100_000,
            "items": list(range(1_000)),
        },
    )

    raw = path.read_bytes()
    record = json.loads(raw)
    assert len(raw) <= 1_200
    assert record["event"] == "large_event"
    assert record["call_id"] == "call-large"
    assert record["tool"] == "read"
    payload_reference = record["payload"]
    assert "$workgate_audit_payload" in payload_reference
    assert payload_reference["$workgate_audit_payload"]["json_bytes"] > len(raw)


def test_audit_preserves_nested_error_details(tmp_path, monkeypatch):
    path = _configure_audit(tmp_path, monkeypatch)

    audit_tool_call_start(
        call_id="nested-error",
        transport="mcp",
        tool="read",
        input={"path": "missing.txt"},
    )
    audit_tool_call_end(
        call_id="nested-error",
        transport="mcp",
        tool="read",
        ok=False,
        duration_ms=5,
        error={
            "type": "OuterError",
            "message": "read failed",
            "cause": {
                "type": "FileNotFoundError",
                "message": "missing.txt",
                "context": {"attempt": 2},
            },
        },
    )

    start, end = _records(path)
    assert start["input"] == {"path": "missing.txt"}
    assert end["error"]["type"] == "OuterError"
    assert end["error"]["cause"] == {
        "type": "FileNotFoundError",
        "message": "missing.txt",
        "context": {"attempt": 2},
    }


def test_audit_threaded_appends_are_complete_and_private(tmp_path, monkeypatch):
    path = _configure_audit(tmp_path, monkeypatch)

    def append(index: int) -> None:
        audit("thread_event", index=index, payload=f"value-{index}")

    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(append, range(200)))

    records = _records(path)
    assert len(records) == 200
    assert {record["index"] for record in records} == set(range(200))
    assert len({record["id"] for record in records}) == 200
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert (
            path.with_name(path.name + ".lock").stat().st_mode & 0o777 == 0o600
        )


def test_audit_cross_process_appends_do_not_corrupt_jsonl(
    tmp_path, monkeypatch
):
    path = _configure_audit(tmp_path, monkeypatch)
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_audit_process,
            args=(str(tmp_path), str(tmp_path / ".state"), worker, 25),
        )
        for worker in range(4)
    ]

    for process in processes:
        process.start()
    _join_processes(processes)

    records = _records(path)
    assert len(records) == 100
    assert {(record["worker"], record["index"]) for record in records} == {
        (worker, index) for worker in range(4) for index in range(25)
    }


def test_audit_retention_keeps_completed_tool_calls_paired(
    tmp_path, monkeypatch
):
    path = _configure_audit(
        tmp_path,
        monkeypatch,
        max_log_bytes=3_000,
        max_event_bytes=1_000,
    )

    for index in range(20):
        call_id = f"call-{index}"
        audit_tool_call_start(
            call_id=call_id,
            transport="mcp",
            tool="read",
            input={"path": f"file-{index}.txt", "padding": "s" * 180},
        )
        audit_tool_call_end(
            call_id=call_id,
            transport="mcp",
            tool="read",
            ok=True,
            duration_ms=index,
            output={"content": "e" * 180},
        )

    records = _records(path)
    by_call: dict[str, set[str]] = {}
    for record in records:
        call_id = str(record.get("call_id") or "")
        if call_id:
            by_call.setdefault(call_id, set()).add(record["event"])

    assert path.stat().st_size <= 3_000
    assert by_call
    assert all(
        events == {"tool_call_start", "tool_call_end"}
        for events in by_call.values()
    )


def test_query_audit_pairs_calls_folds_children_and_hides_auth(
    tmp_path, monkeypatch
):
    _configure_audit(tmp_path, monkeypatch)
    audit(
        "tool_call_start",
        call_id="call-1",
        transport="mcp",
        tool="write_file",
        session_id="session-a",
        input={"path": "notes.txt", "content": "hello"},
    )
    audit(
        "file_link_created",
        parent_call_id="call-1",
        url="https://files.test/download/private-token",
    )
    audit(
        "tool_call_end",
        call_id="call-1",
        transport="mcp",
        tool="write_file",
        ok=True,
        duration_ms=7,
        output={"path": "notes.txt"},
    )
    audit("auth_ok", subject="browser")

    result = query_audit()

    assert result["count"] == 1
    assert result["total_matched"] == 1
    entry = result["entries"][0]
    assert entry["id"] == "call:call-1"
    assert entry["operation"] == "files"
    assert entry["session"] == "session-a"
    assert entry["status"] == "success"
    assert entry["paired"] is True
    assert entry["input"]["path"] == "notes.txt"
    assert entry["output"] == {"path": "notes.txt"}
    assert entry["related_events"][0]["event"] == "file_link_created"
    assert entry["related_events"][0]["url"].endswith("/download/<redacted>")
    assert "_source_indexes" not in entry
    assert get_audit_entry("call:call-1") == entry


def test_audit_query_snapshot_summarizes_list_and_keeps_selected_preview():
    first = {
        "id": "call:first",
        "ts": 1.0,
        "event": "tool_call",
        "node": "local",
        "operation": "files",
        "tool": "read",
        "input": {"path": "first.txt"},
        "output": {"content": "first"},
        "related_events": [{"event": "child"}],
    }
    second = {
        "id": "call:second",
        "ts": 2.0,
        "event": "tool_call",
        "node": "local",
        "operation": "shell",
        "tool": "bash",
        "input": {"command": "true"},
        "output": {"ok": True},
    }

    summary = summarize_audit_entry(first)
    snapshot = audit_query_snapshot(
        {
            "entries": [first, second],
            "count": 2,
            "total_matched": 2,
            "failed_matched": 0,
        },
        selected_id="call:second",
    )

    assert "input" not in summary
    assert "output" not in summary
    assert summary["related_event_count"] == 1
    assert snapshot["entries"] == [summary, summarize_audit_entry(second)]
    assert snapshot["entry"] == second
    assert snapshot["failed_matched"] == 0


def test_query_audit_filters_sorts_limits_and_skips_malformed_lines(
    tmp_path, monkeypatch
):
    path = _configure_audit(tmp_path, monkeypatch)
    audit(
        "tool_call_start",
        call_id="read-1",
        tool="read",
        transport="http",
        session_id="session-a",
        input={"path": "alpha.txt"},
    )
    audit(
        "tool_call_end",
        call_id="read-1",
        tool="read",
        transport="http",
        ok=True,
        duration_ms=2,
        output={"content": "alpha"},
    )
    audit("job_started", job_id="job-1", command="compile beta")
    with path.open("ab") as handle:
        handle.write(b"not-json\n")

    files = query_audit(operation="files", search="READ", sort="asc")
    jobs = query_audit(event="job", operation="jobs", limit=1)
    session = query_audit(session="SESSION-A")
    payload_only = query_audit(search="ALPHA")

    assert [entry["id"] for entry in files["entries"]] == ["call:read-1"]
    assert jobs["count"] == 1
    assert jobs["total_matched"] == 1
    assert jobs["entries"][0]["event"] == "job_started"
    assert jobs["failed_matched"] == 0
    assert session["count"] == 1
    assert session["entries"][0]["session"] == "session-a"
    assert payload_only["count"] == 0
    with pytest.raises(ValueError, match="sort must be asc or desc"):
        query_audit(sort="sideways")
    with pytest.raises(ValueError, match="start_ts"):
        query_audit(start_ts=2, end_ts=1)
    with pytest.raises(ValueError, match="Unknown audit entry"):
        get_audit_entry("missing")


def test_tool_call_session_is_derived_before_oversized_input_is_externalized(
    tmp_path, monkeypatch
):
    path = _configure_audit(tmp_path, monkeypatch, inline_bytes=256)
    audit_tool_call_start(
        call_id="session-input",
        transport="mcp",
        tool="read",
        input={
            "session_id": "SESSION1",
            "path": "file.txt",
            "content": "x" * 5_000,
        },
    )
    audit_tool_call_end(
        call_id="session-input",
        transport="mcp",
        tool="read",
        ok=True,
        duration_ms=1,
        output={"path": "file.txt"},
    )

    start = _records(path)[0]
    result = query_audit(session="SESSION1")

    assert start["session"] == "SESSION1"
    assert AUDIT_PAYLOAD_KEY in start["input"]
    assert result["count"] == 1
    assert result["entries"][0]["session"] == "SESSION1"


def test_query_audit_reports_failures_across_all_matches(tmp_path, monkeypatch):
    _configure_audit(tmp_path, monkeypatch)
    for index, ok in enumerate((False, True, False)):
        call_id = f"aggregate-{index}"
        audit_tool_call_start(
            call_id=call_id,
            transport="mcp",
            tool="read",
            input={"path": f"file-{index}.txt"},
        )
        audit_tool_call_end(
            call_id=call_id,
            transport="mcp",
            tool="read",
            ok=ok,
            duration_ms=index,
            output={"content": "ok"} if ok else None,
            error=None if ok else {"message": "failed"},
        )

    result = query_audit(limit=1)

    assert result["count"] == 1
    assert result["total_matched"] == 3
    assert result["failed_matched"] == 2


def test_coalesce_audit_records_supports_legacy_calls_without_ids():
    rows = _coalesce_audit_records(
        [
            {
                "ts": 1.0,
                "event": "tool_call_start",
                "tool": "read",
                "session_id": "legacy-session",
                "input": {"path": "one.txt"},
            },
            {
                "ts": 2.0,
                "event": "tool_call_end",
                "tool": "read",
                "session_id": "legacy-session",
                "ok": False,
                "duration_ms": 3,
                "error": {"message": "missing"},
            },
        ]
    )

    assert len(rows) == 1
    assert rows[0]["id"] == "legacy-call:1.0:0"
    assert rows[0]["status"] == "failed"
    assert rows[0]["paired"] is True
    assert rows[0]["error"] == {"message": "missing"}


def test_audit_payload_round_trip_is_deduplicated_private_and_redacted(
    tmp_path, monkeypatch
):
    _configure_audit(
        tmp_path,
        monkeypatch,
        inline_bytes=256,
        max_payload_bytes=100_000,
        max_payload_store_bytes=200_000,
    )
    payload = {
        "token": "top-secret-token",
        "body": "recoverable-" * 1_000,
    }

    audit("large_payload", payload=payload)
    audit("large_payload", payload=payload)

    listing = query_audit(event="large_payload", sort="asc")
    assert listing["count"] == 2
    reference = listing["entries"][0]["payload"]
    assert AUDIT_PAYLOAD_KEY in reference
    assert "top-secret-token" not in json.dumps(reference)
    full = get_audit_entry(
        listing["entries"][0]["id"], include_full_payloads=True
    )
    assert full["payload"] == {
        "token": "<redacted>",
        "body": payload["body"],
    }

    files = list(get_settings().audit_payload_dir.glob("*.json.gz"))
    assert len(files) == 1
    if os.name != "nt":
        assert files[0].stat().st_mode & 0o777 == 0o600


def test_audit_payload_resolution_reports_corrupt_missing_and_expired(
    tmp_path, monkeypatch
):
    _configure_audit(
        tmp_path,
        monkeypatch,
        inline_bytes=256,
        max_payload_bytes=100_000,
        max_payload_store_bytes=200_000,
        payload_retention_s=60,
    )
    audit("payload_state", payload={"body": "state-" * 1_000})
    preview = query_audit(event="payload_state")["entries"][0]
    reference = preview["payload"][AUDIT_PAYLOAD_KEY]
    path = get_settings().audit_payload_dir / f"{reference['sha256']}.json.gz"

    path.write_bytes(b"not-gzip")
    corrupt = get_audit_entry(preview["id"], include_full_payloads=True)
    assert corrupt["payload"][AUDIT_PAYLOAD_KEY]["status"] == "corrupt"

    path.unlink()
    missing = get_audit_entry(preview["id"], include_full_payloads=True)
    assert missing["payload"][AUDIT_PAYLOAD_KEY]["status"] == "missing"

    monkeypatch.setattr(
        audit_payload_module.time,
        "time",
        lambda: float(reference["created_at"]) + 61,
    )
    expired = get_audit_entry(preview["id"], include_full_payloads=True)
    assert expired["payload"][AUDIT_PAYLOAD_KEY]["status"] == "expired"


def test_audit_payload_above_per_value_limit_is_explicitly_omitted(
    tmp_path, monkeypatch
):
    path = _configure_audit(
        tmp_path,
        monkeypatch,
        inline_bytes=256,
        max_payload_bytes=1_024,
        max_payload_store_bytes=4_096,
    )

    audit("oversized_payload", payload={"body": "z" * 5_000})

    record = _records(path)[0]
    omitted = record["payload"]["$workgate_audit_truncated"]
    assert omitted["reason"] == "payload_byte_limit"
    assert omitted["original_bytes"] > 1_024
    assert list(get_settings().audit_payload_dir.glob("*.json.gz")) == []


def test_audit_payload_store_quota_drops_complete_lifecycle_units(
    tmp_path, monkeypatch
):
    path = _configure_audit(
        tmp_path,
        monkeypatch,
        max_log_bytes=100_000,
        inline_bytes=256,
        max_payload_bytes=8_000,
        max_payload_store_bytes=10_000,
    )
    for index in range(6):
        payload = random.Random(index).randbytes(4_000).hex()
        audit_tool_call_start(
            call_id=f"quota-{index}",
            transport="mcp",
            tool="read",
            input={"payload": payload},
        )
        audit_tool_call_end(
            call_id=f"quota-{index}",
            transport="mcp",
            tool="read",
            ok=True,
            duration_ms=index,
            output={"ok": True},
        )

    records = _records(path)
    by_call: dict[str, set[str]] = {}
    for record in records:
        call_id = str(record.get("call_id") or "")
        if call_id:
            by_call.setdefault(call_id, set()).add(str(record["event"]))
    assert by_call
    assert all(
        events == {"tool_call_start", "tool_call_end"}
        for events in by_call.values()
    )
    assert (
        sum(
            item.stat().st_size
            for item in get_settings().audit_payload_dir.glob("*.json.gz")
        )
        <= 10_000
    )


def test_audit_payload_orphan_gc_and_symlink_replacement(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("symlink creation is not reliably available on Windows")
    _configure_audit(
        tmp_path,
        monkeypatch,
        inline_bytes=256,
        max_payload_bytes=100_000,
        max_payload_store_bytes=200_000,
        payload_retention_s=60,
    )
    settings = get_settings()
    audit_core._AUDIT_PAYLOAD_SWEEP_TIMES.clear()
    monkeypatch.setattr(audit_core.time, "monotonic", lambda: 1.0)
    settings.audit_payload_dir.mkdir(parents=True, exist_ok=True)
    orphan = settings.audit_payload_dir / f"{'a' * 64}.json.gz"
    orphan.write_bytes(b"orphan")
    os.utime(orphan, (0, 0))

    audit("gc-trigger", value="small")
    assert not orphan.exists()

    value = {"body": "safe-" * 2_000}
    from workgate.audit.payloads import canonical_json_bytes

    digest = (
        __import__("hashlib").sha256(canonical_json_bytes(value)).hexdigest()
    )
    target = tmp_path / "symlink-target"
    target.write_bytes(b"must-not-change")
    payload_path = settings.audit_payload_dir / f"{digest}.json.gz"
    payload_path.symlink_to(target)

    audit("symlink-replacement", payload=value)

    assert not payload_path.is_symlink()
    assert target.read_bytes() == b"must-not-change"
    entry = query_audit(event="symlink-replacement")["entries"][0]
    assert (
        get_audit_entry(entry["id"], include_full_payloads=True)["payload"]
        == value
    )


def test_audit_payload_cross_process_deduplicates_and_recovers(
    tmp_path, monkeypatch
):
    _configure_audit(
        tmp_path,
        monkeypatch,
        inline_bytes=256,
        max_payload_bytes=100_000,
        max_payload_store_bytes=200_000,
    )
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_audit_payload_process,
            args=(str(tmp_path), str(tmp_path / ".state"), worker),
        )
        for worker in range(4)
    ]
    for process in processes:
        process.start()
    _join_processes(processes)

    listing = query_audit(event="process_payload", sort="asc")
    assert listing["count"] == 4
    assert len(list(get_settings().audit_payload_dir.glob("*.json.gz"))) == 1
    for entry in listing["entries"]:
        full = get_audit_entry(entry["id"], include_full_payloads=True)
        assert full["payload"] == {
            "token": "<redacted>",
            "body": "shared-process-payload-" * 1_000,
        }


def test_disabling_audit_payloads_falls_back_to_bounded_jsonl(
    tmp_path, monkeypatch
):
    path = _configure_audit(
        tmp_path,
        monkeypatch,
        max_event_bytes=1_200,
        inline_bytes=256,
        max_payload_bytes=100_000,
        max_payload_store_bytes=200_000,
        payloads_enabled=False,
    )

    audit("disabled_payload_store", payload={"body": "x" * 20_000})

    record = _records(path)[0]
    assert record["$workgate_audit_truncated"]["reason"] == ("event_byte_limit")
    assert not get_settings().audit_payload_dir.exists()


def test_audit_payload_rejects_symlinked_store_root(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("symlink creation is not reliably available on Windows")
    _configure_audit(
        tmp_path,
        monkeypatch,
        inline_bytes=256,
        max_payload_bytes=100_000,
        max_payload_store_bytes=200_000,
    )
    settings = get_settings()
    target = tmp_path / "outside-payload-root"
    target.mkdir()
    settings.audit_payload_dir.parent.mkdir(parents=True, exist_ok=True)
    settings.audit_payload_dir.symlink_to(target, target_is_directory=True)

    audit("symlinked-payload-root", payload={"body": "x" * 10_000})

    entry = query_audit(event="symlinked-payload-root")["entries"][0]
    assert entry["payload"]["$workgate_audit_truncated"]["reason"] == (
        "payload_store_error"
    )
    assert list(target.iterdir()) == []


def test_audit_payload_decompression_bomb_is_bounded(tmp_path, monkeypatch):
    import gzip

    _configure_audit(
        tmp_path,
        monkeypatch,
        inline_bytes=256,
        max_payload_bytes=4_096,
        max_payload_store_bytes=8_192,
    )
    audit("decompression-bomb", payload={"body": "small-" * 300})
    preview = query_audit(event="decompression-bomb")["entries"][0]
    reference = preview["payload"][AUDIT_PAYLOAD_KEY]
    path = get_settings().audit_payload_dir / f"{reference['sha256']}.json.gz"
    path.write_bytes(gzip.compress(b'"' + b"z" * 20_000 + b'"', mtime=0))

    full = get_audit_entry(preview["id"], include_full_payloads=True)

    assert full["payload"][AUDIT_PAYLOAD_KEY]["status"] == "corrupt"
