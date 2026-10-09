"""End-to-end retention, query and recovery of canonical cold Audit history."""

import asyncio
import gzip
import json
import random
import string
from pathlib import Path

import pytest

from workgate.audit import (
    audit,
    audit_tool_call_end,
    audit_tool_call_start,
    get_audit_entry,
    get_session_audit_entry,
    query_audit,
    query_session_audit,
)
from workgate.audit.archive import (
    archive_evicted,
    archive_paths,
    prune_archives,
)
from workgate.config.role_config import get_role_config
from workgate.config.settings import clear_settings_cache, get_settings


def _configure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    archive_bytes: int = 1_000_000,
) -> Path:
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_MAX_AUDIT_LOG_BYTES", "5000")
    monkeypatch.setenv("WORKGATE_MAX_AUDIT_EVENT_BYTES", "1000")
    monkeypatch.setenv("WORKGATE_MAX_AUDIT_ARCHIVE_BYTES", str(archive_bytes))
    monkeypatch.setenv("WORKGATE_AUDIT_INLINE_VALUE_BYTES", "256")
    clear_settings_cache()
    return get_settings().audit_log_path


def test_cold_rollover_query_detail_session_and_lazy_payload(
    tmp_path, monkeypatch
):
    path = _configure(tmp_path, monkeypatch)
    session_id = "sess_archived_task"
    audit(
        "archive_payload", session_id=session_id, content="sample-value-" * 1300
    )
    original = query_audit(event="archive_payload")["entries"][0]
    assert "$workgate_audit_payload" in original["content"]
    call_id = "cross-archive-tool"
    audit_tool_call_start(
        call_id=call_id,
        transport="mcp",
        tool="bash",
        input={"session_id": session_id, "command": "echo original"},
    )
    for index in range(48):
        audit(
            "archive_filler",
            session_id=session_id,
            index=index,
            text=f"filler-{index}-" * 19,
        )
    audit_tool_call_end(
        call_id=call_id,
        transport="mcp",
        tool="bash",
        session_ids=(session_id,),
        ok=True,
        duration_ms=4,
        output={"stdout": "done"},
    )
    assert archive_paths(get_role_config())
    assert path.stat().st_size <= get_settings().max_audit_log_bytes
    for listing in (
        lambda: query_audit(event="archive_payload"),
        lambda: query_session_audit(session_id, event="archive_payload"),
    ):
        result = listing()
        assert result["count"] == 1
        assert result["entries"][0]["content"] == original["content"]
    assert (
        get_audit_entry(original["id"], include_full_payloads=True)["content"]
        == "sample-value-" * 1300
    )
    assert (
        get_session_audit_entry(
            session_id, original["id"], include_full_payloads=True
        )["content"]
        == "sample-value-" * 1300
    )
    assert (
        query_session_audit("other-session", event="archive_payload")["count"]
        == 0
    )
    with pytest.raises(ValueError, match="Unknown audit entry"):
        get_session_audit_entry("other-session", original["id"])
    for listing in (
        query_audit,
        lambda **kwargs: query_session_audit(session_id, **kwargs),
    ):
        found = listing(search=call_id)["entries"]
        assert len(found) == 1
        assert found[0]["paired"] is True
        assert found[0]["input"]["command"] == "echo original"
        assert found[0]["output"] == {"stdout": "done"}


def test_cold_directory_recovers_valid_orphans_and_skips_corruption(
    tmp_path, monkeypatch
):
    _configure(tmp_path, monkeypatch)
    settings = get_role_config()
    archive_evicted(
        [
            (
                json.dumps(
                    {"id": "orphan-marker", "event": "recovered", "ts": 1}
                )
                + "\n"
            ).encode()
        ],
        settings,
    )
    root = archive_paths(settings)[0].parent
    (root / "00000000000000000000-000000000000.jsonl.gz").write_bytes(
        b"broken gzip"
    )
    (root / "00000000000000000002-000000000000.jsonl.gz").write_bytes(
        gzip.compress(b"x" * 1_100_000, mtime=0)
    )
    (root / "00000000000000000001-000000000000.jsonl.gz").symlink_to(
        root / "does-not-exist"
    )
    assert query_audit(event="recovered")["entries"][0]["id"] == "orphan-marker"
    assert len(archive_paths(settings)) == 3
    prune_archives(settings)
    assert len(archive_paths(settings)) == 1
    # Querying a duplicated, still-hot record reports it only once after crash recovery.
    audit("new-event", marker="same-id")
    record = json.loads(
        get_settings().audit_log_path.read_text().splitlines()[-1]
    )
    archive_evicted([(json.dumps(record) + "\n").encode()], settings)
    assert query_audit(event="new-event")["count"] == 1


def test_cold_archive_prunes_oldest_when_compressed_budget_exhausted(
    tmp_path, monkeypatch
):
    _configure(tmp_path, monkeypatch, archive_bytes=1000)
    settings = get_role_config()
    for i in range(6):
        archive_evicted(
            [
                (
                    json.dumps(
                        {
                            "id": f"record-{i}",
                            "event": "archive_budget",
                            "text": "z" * 250,
                        }
                    )
                    + "\n"
                ).encode()
            ],
            settings,
        )
        prune_archives(settings)
    files = archive_paths(settings)
    assert 0 < len(files) < 6
    assert (
        sum(path.stat().st_size for path in files)
        <= settings.max_audit_archive_bytes
    )
    result = query_audit(event="archive_budget")
    assert result["total_matched"] == len(files)
    assert any(entry["id"] == "record-5" for entry in result["entries"])
    assert not any(entry["id"] == "record-0" for entry in result["entries"])


def test_failed_hot_replace_leaves_log_and_reconciles_duplicate_archive(
    tmp_path, monkeypatch
):
    from workgate.audit import core as audit_core

    path = _configure(tmp_path, monkeypatch)
    for index in range(12):
        audit("crash_record", index=index, text="persist-" * 15)
    before = path.read_bytes()
    expected_ids = {
        row["id"] for row in query_audit(event="crash_record")["entries"]
    }
    original_write = audit_core.atomic_write_private_bytes

    def fail_hot_replace(target: Path, data: bytes) -> None:
        if target == path:
            raise OSError("simulated hot-log replacement failure")
        original_write(target, data)

    monkeypatch.setattr(
        audit_core, "atomic_write_private_bytes", fail_hot_replace
    )
    with (
        pytest.raises(OSError, match="simulated"),
        audit_core._audit_transaction(path),
    ):
        audit_core._enforce_audit_log_limit(
            path, 1400, settings=get_role_config()
        )
    assert path.read_bytes() == before
    assert archive_paths(get_role_config())
    # New archive may temporarily duplicate hot records after an interrupted
    # transaction, but canonical query exposes each logical event only once.
    assert {
        row["id"] for row in query_audit(event="crash_record")["entries"]
    } == expected_ids
    monkeypatch.setattr(
        audit_core, "atomic_write_private_bytes", original_write
    )
    with audit_core._audit_transaction(path):
        audit_core._enforce_audit_log_limit(
            path, 1400, settings=get_role_config()
        )
    assert {
        row["id"] for row in query_audit(event="crash_record")["entries"]
    } == expected_ids


def test_cold_history_disabled_keeps_only_hot_log(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, archive_bytes=0)
    for index in range(50):
        audit("disabled_archive", index=index, text="y" * 300)
    assert archive_paths(get_role_config()) == []
    assert query_audit(event="disabled_archive")["total_matched"] < 50


def test_cold_metadata_survives_payload_quota_eviction(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    monkeypatch.setenv("WORKGATE_MAX_AUDIT_PAYLOAD_STORE_BYTES", "2400")
    monkeypatch.setenv("WORKGATE_MAX_AUDIT_PAYLOAD_BYTES", "1024")
    clear_settings_cache()
    randomizer = random.Random(1)
    for index in range(12):
        value = "".join(randomizer.choices(string.ascii_letters, k=730))
        audit("quota_cold_event", index=index, content=value)
    assert archive_paths(get_role_config())
    matched = query_audit(event="quota_cold_event")
    assert matched["total_matched"] == 12
    assert matched["count"] == 12
    assert any(row["index"] == 0 for row in matched["entries"])
    # Payload objects have an independent quota; loss of a full value may not
    # erase the reference/preview and its owning historical event.
    first = next(row for row in matched["entries"] if row["index"] == 0)
    assert "$workgate_audit_payload" in first["content"]
    full = get_audit_entry(first["id"], include_full_payloads=True)
    assert full["id"] == first["id"]


def test_cold_query_enforces_oauth_read_and_full_scope(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.helpers import build_paired_http_app
    from workgate.oauth.core.scopes import SCOPE_AUDIT_FULL, SCOPE_AUDIT_READ
    from workgate.oauth.protocol.token_codec import issue_access_token

    _configure(tmp_path, monkeypatch)
    base_url = "https://cold-audit.example"
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "oauth")
    monkeypatch.setenv("WORKGATE_BASE_URL", base_url)
    clear_settings_cache()
    app, harness = build_paired_http_app(get_settings())
    task = asyncio.run(
        harness.control.task_service.create_task(label="cold scope")
    )
    audit(
        "cold_oauth_marker",
        task_id=task.task_id,
        payload="oauth-cold-content-" * 900,
    )
    for index in range(40):
        audit("rollover", index=index, text="z" * 210)

    def headers(scope: str) -> dict[str, str]:
        token = issue_access_token(
            client_id="cold-audit-test", scope=scope, resource=f"{base_url}/mcp"
        )
        return {"Authorization": f"Bearer {token}"}

    client = TestClient(app, base_url=base_url)
    params = {"event": "cold_oauth_marker", "task_id": task.task_id}
    assert (
        client.get(
            "/tools/audit_tail", params=params, headers=headers("shell:read")
        ).status_code
        == 403
    )
    listing = client.get(
        "/tools/audit_tail", params=params, headers=headers(SCOPE_AUDIT_READ)
    )
    assert listing.status_code == 200, listing.text
    entry = listing.json()["entries"][0]
    assert "$workgate_audit_payload" in entry["payload"]
    detail_params = {
        "task_id": task.task_id,
        "entry_id": entry["id"],
        "include_full_payloads": "true",
    }
    assert (
        client.get(
            "/tools/audit_tail",
            params=detail_params,
            headers=headers(SCOPE_AUDIT_READ),
        ).status_code
        == 403
    )
    full = client.get(
        "/tools/audit_tail",
        params=detail_params,
        headers=headers(f"{SCOPE_AUDIT_READ} {SCOPE_AUDIT_FULL}"),
    )
    assert full.status_code == 200
    assert full.json()["entries"][0]["payload"] == "oauth-cold-content-" * 900


def test_cold_segments_coalesce_out_of_order_nested_events(
    tmp_path, monkeypatch
):
    path = _configure(tmp_path, monkeypatch)
    settings = get_role_config()
    # Recovery sorts logical timestamps before coalescing even if different
    # retention units reached the archive directory out of original order.
    rows = [
        {
            "id": "child",
            "ts": 20,
            "event": "child_detail",
            "parent_call_id": "abc",
            "detail": "nested",
        },
        {
            "id": "start",
            "ts": 10,
            "event": "tool_call_start",
            "tool": "bash",
            "call_id": "abc",
            "input": {"command": "echo"},
        },
    ]
    for record in rows:
        archive_evicted([(json.dumps(record) + "\n").encode()], settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "id": "end",
                "ts": 30,
                "event": "tool_call_end",
                "tool": "bash",
                "call_id": "abc",
                "ok": True,
            }
        )
        + "\n"
    )
    matches = query_audit(search="abc")["entries"]
    assert len(matches) == 1
    assert matches[0]["paired"] is True
    assert matches[0]["related_events"][0]["detail"] == "nested"
