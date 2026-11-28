"""Pinned external event envelope and bounded ordered delivery admission."""

import hashlib
import hmac
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from .content_filters import filters_from_snapshot, validate_message_submission
from .database import SQLiteDatabase
from .event_bindings import live_binding
from .event_envelope import _TOKEN, EventConflict, _string
from .event_source_keys import _now, live_source

_MAX_PROMPT_BYTES = 128 * 1024
_MAX_PENDING = 256
_NO_PARENT = "00000000-0000-0000-0000-000000000000"


def _iso(value: int) -> str:
    return (
        datetime.fromtimestamp(value / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _js(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def delivery_key(envelope: dict) -> str:
    """Direct port of original envelope.ts:getAgentTriggerIdempotencyKey."""
    target = envelope["target"]
    principal = envelope["principal"]
    source = envelope["event"]["source"]
    values = [
        envelope["version"],
        principal.get("tenantId", ""),
        principal["userId"],
        source["type"],
        source["id"],
        envelope["event"]["type"],
        envelope["event"]["id"],
        envelope["deliveryId"],
        envelope["mode"],
        target["agentId"],
        "" if envelope["mode"] == "fire" else target["conversationId"],
        target["parentMessageId"] if envelope["mode"] == "continue" else "",
        target.get("bindingId", "") if envelope["mode"] == "continue" else "",
        target.get("sourceKeyId", "") if envelope["mode"] == "continue" else "",
    ]
    return "trigger_" + hashlib.sha256(_js(values).encode()).hexdigest()


def _parse_expected_action(value: Any, mode: str, target: dict) -> dict | None:
    """Original envelope.ts:createExpectedAction + delivery.ts bound-child only."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("expectedAction must be an object")
    tool_name = _string(value.get("toolName"), "expectedAction.toolName", 256)
    if len(tool_name) > 256:
        raise ValueError("expectedAction.toolName exceeds 256 characters")
    if (
        mode != "continue"
        or not target.get("bindingId")
        or not target.get("sourceKeyId")
    ):
        raise ValueError(
            "Expected actions require authenticated bound-child continuation"
        )
    result = {"toolName": tool_name}
    subset = value.get("argumentSubset")
    if subset is not None:
        if not isinstance(subset, dict):
            raise ValueError("expectedAction.argumentSubset must be an object")
        # Enforce exactly the JSON-value shape allowed by the source clone.
        result["argumentSubset"] = json.loads(_json(subset))
    return result


_COALESCE_WINDOW_MS = 750
_MAX_BATCH_SIZE = 8
_MAX_BATCH_BYTES = 512 * 1024


def _coalescing_key(envelope: dict, options: Any) -> str | None:
    """Original delivery.ts:coalescingIdentity, observational bound children only."""
    if options is None:
        return None
    if not isinstance(options, dict) or not isinstance(options.get("key"), str):
        raise ValueError("coalesce.key must be a string")
    key = options["key"].strip()
    if not 1 <= len(key) <= 128:
        raise ValueError(
            "coalesce.key must contain between 1 and 128 characters"
        )
    target = envelope["target"]
    if (
        envelope["mode"] != "continue"
        or not target.get("bindingId")
        or not target.get("sourceKeyId")
    ):
        raise ValueError(
            "Coalescing requires authenticated bound-child continue"
        )
    if envelope.get("expectedAction") is not None:
        raise ValueError("Expected actions cannot be coalesced")
    source = envelope["event"]["source"]
    fields = [
        envelope["principal"].get("tenantId", ""),
        envelope["principal"]["userId"],
        source["type"],
        source["id"],
        target["agentId"],
        target["conversationId"],
        target["bindingId"],
        target["sourceKeyId"],
        key,
    ]
    return "trigger_batch_" + _digest(fields)


def _batch_input(root: dict, members: list[dict]) -> str:
    """Original batch.ts:createAgentTriggerBatchEnvelope event order/content."""
    deliveries = [(root["id"], json.loads(root["envelope"]))]
    deliveries.extend(
        (member["id"], json.loads(member["envelope"])) for member in members
    )
    deliveries.sort(
        key=lambda item: (
            item[1]["event"]["occurredAt"],
            item[1]["receivedAt"],
            item[1]["event"]["id"],
            item[0],
        )
    )
    events = [
        {
            "deliveryId": envelope["deliveryId"],
            "event": envelope["event"],
            "input": envelope["input"],
        }
        for _, envelope in deliveries
    ]
    counts: dict[str, int] = {}
    for entry in events:
        kind = entry["event"]["type"]
        counts[kind] = counts.get(kind, 0) + 1
    batch = {
        "kind": "librechat.agent_event_batch",
        "version": 1,
        "count": len(events),
        "summary": {
            "eventTypes": [
                {"type": kind, "count": counts[kind]} for kind in sorted(counts)
            ]
        },
        "events": events,
    }
    return _js(batch)


def _lane(envelope: dict, ordering_key: str | None) -> str:
    source = envelope["event"]["source"]
    if ordering_key is not None:
        if (
            not isinstance(ordering_key, str)
            or not 1 <= len(ordering_key.strip()) <= 256
        ):
            raise ValueError("orderingKey must contain 1-256 characters")
        lane = ["explicit", ordering_key.strip()]
    else:
        lane = [
            "default",
            source["type"],
            source["id"],
            envelope["mode"],
            envelope["target"]["agentId"],
            ""
            if envelope["mode"] == "fire"
            else envelope["target"]["conversationId"],
        ]
    return "trigger_lane_" + _digest(
        [
            envelope["principal"].get("tenantId", ""),
            envelope["principal"]["userId"],
            lane,
        ]
    )


def prepare_source_event(
    owner: str,
    source_key_id: str,
    identity: str,
    body: dict,
    *,
    received_at: int | None = None,
) -> tuple[dict, str]:
    """Source ingress.ts overwrites caller-controlled source, principal and receive time."""
    if (
        not isinstance(body, dict)
        or not isinstance(identity, str)
        or not _TOKEN.fullmatch(identity)
    ):
        raise ValueError("Exactly one valid Idempotency-Key header is required")
    mode = body.get("mode")
    if not isinstance(mode, str) or mode not in {"fire", "continue", "steer"}:
        raise ValueError("Unsupported original Agent Trigger mode")
    event = body.get("event")
    target = body.get("target")
    if not isinstance(event, dict) or not isinstance(target, dict):
        raise ValueError("An event and target object are required")
    moment = event.get("occurredAt")
    if type(moment) is not int or moment < 0 or moment > 2**53 - 1:
        raise ValueError("event.occurredAt must be a nonnegative safe integer")
    agent_id = _string(target.get("agentId"), "target.agentId", 256)
    resolved: dict[str, Any] = {"agentId": agent_id}
    if mode != "fire":
        resolved["conversationId"] = _string(
            target.get("conversationId"), "target.conversationId", 256
        )
    if mode == "continue":
        resolved["parentMessageId"] = _string(
            target.get("parentMessageId"), "target.parentMessageId", 256
        )
        resolved["bindingId"] = _string(
            target.get("bindingId"), "target.bindingId", 256
        )
        resolved["sourceKeyId"] = source_key_id
    if mode == "steer":
        stamp = target.get("generationCreatedAt")
        if type(stamp) is not int or stamp < 0:
            raise ValueError(
                "target.generationCreatedAt must be a nonnegative integer"
            )
        resolved["generationCreatedAt"] = stamp
        if target.get("preempt") is not None:
            if type(target["preempt"]) is not bool:
                raise ValueError("target.preempt must be a boolean")
            resolved["preempt"] = target["preempt"]
    expected_action = _parse_expected_action(
        body.get("expectedAction"), mode, resolved
    )
    received_at = _now() if received_at is None else received_at
    envelope = {
        "version": 1,
        "mode": mode,
        "requestId": str(uuid.uuid4()),
        "deliveryId": identity,
        "receivedAt": received_at,
        "principal": {"userId": owner},
        "event": {
            "id": _string(event.get("id"), "event.id", 256),
            "type": _string(event.get("type"), "event.type", 256),
            "occurredAt": moment,
            "source": {"id": source_key_id, "type": "remote_api_key"},
        },
        "target": resolved,
        "input": _string(body.get("input"), "input"),
    }
    if expected_action is not None:
        envelope["expectedAction"] = expected_action
    if "payload" in event:
        envelope["event"]["payload"] = event["payload"]
    if len(_json(envelope).encode()) > 1024 * 1024:
        raise ValueError("Agent Trigger envelope exceeds 1048576 bytes")
    return envelope, _lane(envelope, body.get("orderingKey"))


class EventDeliveryQueue:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def enqueue(
        self, owner: str, source_key_id: str, identity: str, body: dict
    ) -> dict:
        if not isinstance(body, dict):
            raise ValueError("Event body must be an object")
        try:
            if len(_json(body).encode()) > 1024 * 1024:
                raise ValueError("Event body too large")
        except RecursionError, UnicodeError, TypeError:
            raise ValueError("Invalid event JSON") from None
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            mode = body.get("mode")
            if not isinstance(mode, str) or mode not in {
                "fire",
                "continue",
                "steer",
            }:
                raise ValueError("Unsupported event mode")
            if not live_source(db, owner, source_key_id, mode=mode):
                raise PermissionError("Event source authorization denied")
            if mode == "continue":
                binding_id = body.get("bindingId")
                if not isinstance(binding_id, str):
                    raise ValueError("Continue requires an event binding")
                binding = live_binding(db, owner, source_key_id, binding_id)
                if binding is None:
                    raise PermissionError("Event binding unavailable")
                body = {
                    **body,
                    "orderingKey": binding_id,
                    "target": {
                        "agentId": binding["agent_id"],
                        "conversationId": binding["conversation_id"],
                        "parentMessageId": _NO_PARENT,
                        "bindingId": binding_id,
                    },
                }
            elif body.get("bindingId") is not None:
                raise ValueError("Binding is valid only for continue")
            envelope, lane = prepare_source_event(
                owner, source_key_id, identity, body
            )
            if envelope.get("expectedAction") is not None:
                raise PermissionError(
                    "External events have no unattended action grant"
                )
            target = envelope["target"]
            if (
                db.execute(
                    "SELECT 1 FROM agents WHERE id=? AND owner=?",
                    (target["agentId"], owner),
                ).fetchone()
                is None
            ):
                raise PermissionError("Event target Agent is unavailable")
            if mode == "steer":
                # Never feed remote input into a foreground run with native
                # grants. Later dispatch must also recheck the exact epoch.
                row = db.execute(
                    "SELECT r.state,r.agent_id,r.status FROM runs r JOIN conversations c ON c.id=r.conversation_id "
                    "WHERE c.id=? AND c.owner=? ORDER BY r.created_at DESC,r.id DESC LIMIT 1",
                    (target["conversationId"], owner),
                ).fetchone()
                if (
                    row is None
                    or row["agent_id"] != target["agentId"]
                    or row["status"] != "running"
                ):
                    raise PermissionError("Event steer target unavailable")
                state = json.loads(row["state"])
                if (
                    state.get("generationCreatedAt")
                    != target["generationCreatedAt"]
                    or state.get("eventSourceKeyId") != source_key_id
                    or state.get("nativeAdmission") is not None
                ):
                    raise PermissionError(
                        "Event steer requires a source-owned model-only run"
                    )
            filters = filters_from_snapshot(db, owner)
            validate_message_submission(envelope["input"], filters)
            coalesce_key = _coalescing_key(envelope, body.get("coalesce"))
            encoded = _json(envelope)
            encoded_bytes = len(encoded.encode())
            if coalesce_key and encoded_bytes > _MAX_BATCH_BYTES:
                raise ValueError("Coalesced envelope exceeds batch limit")
            key = delivery_key(envelope)
            if (
                coalesce_key is not None
                and len(
                    _batch_input({"id": key, "envelope": encoded}, []).encode()
                )
                > _MAX_PROMPT_BYTES
            ):
                raise ValueError("Event batch exceeds model input budget")
            stable = {
                k: v
                for k, v in envelope.items()
                if k not in {"requestId", "receivedAt"}
            }
            fingerprint = _digest([stable, coalesce_key, lane])
            prior = db.execute(
                "SELECT fingerprint,status,available_at FROM agent_event_deliveries WHERE id=? AND owner=? AND source_key_id=?",
                (key, owner, source_key_id),
            ).fetchone()
            if prior:
                if not hmac.compare_digest(prior["fingerprint"], fingerprint):
                    raise EventConflict(
                        "Idempotency-Key was already used for different event content"
                    )
                return {
                    "id": key,
                    "status": prior["status"],
                    "availableAt": prior["available_at"],
                    "replayed": True,
                }
            count = db.execute(
                "SELECT count(*) FROM agent_event_deliveries WHERE owner=? AND status IN ('pending','leased')",
                (owner,),
            ).fetchone()[0]
            if count >= _MAX_PENDING:
                raise EventConflict("Event queue capacity reached")
            now = _now()
            parent = None
            available_at = now
            if coalesce_key is not None:
                candidates = db.execute(
                    "SELECT id,available_at,envelope FROM agent_event_deliveries WHERE owner=? AND source_key_id=? "
                    "AND coalesce_key=? AND ordering_key=? AND batch_root_id IS NULL AND status='pending' "
                    "AND run_id IS NULL AND coalesce_until>? AND batch_count<? AND batch_bytes+?<=? ORDER BY seq DESC",
                    (
                        owner,
                        source_key_id,
                        coalesce_key,
                        lane,
                        now,
                        _MAX_BATCH_SIZE,
                        encoded_bytes,
                        _MAX_BATCH_BYTES,
                    ),
                ).fetchall()
                for candidate in candidates:
                    members = db.execute(
                        "SELECT id,envelope FROM agent_event_deliveries WHERE batch_root_id=? ORDER BY seq",
                        (candidate["id"],),
                    ).fetchall()
                    if (
                        len(
                            _batch_input(
                                candidate,
                                [*members, {"id": key, "envelope": encoded}],
                            ).encode()
                        )
                        <= _MAX_PROMPT_BYTES
                    ):
                        parent = candidate
                        break
                available_at = (
                    now + _COALESCE_WINDOW_MS
                    if parent is None
                    else parent["available_at"]
                )
            db.execute(
                "INSERT INTO agent_event_deliveries(id,owner,source_key_id,fingerprint,envelope,mode,ordering_key,status,available_at,coalesce_key,coalesce_until,batch_root_id,batch_count,batch_bytes,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?)",
                (
                    key,
                    owner,
                    source_key_id,
                    fingerprint,
                    encoded,
                    mode,
                    lane,
                    available_at,
                    coalesce_key,
                    available_at
                    if coalesce_key is not None and parent is None
                    else None,
                    parent["id"] if parent else None,
                    1,
                    encoded_bytes,
                    now,
                    now,
                ),
            )
            if parent is not None:
                db.execute(
                    "UPDATE agent_event_deliveries SET batch_count=batch_count+1,batch_bytes=batch_bytes+?,updated_at=? WHERE id=? AND status='pending'",
                    (encoded_bytes, now, parent["id"]),
                )
            return {
                "id": key,
                "status": "pending",
                "availableAt": available_at,
                "replayed": False,
            }

    def get(
        self, owner: str, source_key_id: str, delivery_id: str
    ) -> dict | None:
        if not isinstance(delivery_id, str) or not re.fullmatch(
            r"trigger_[a-f0-9]{64}", delivery_id
        ):
            raise ValueError("Invalid event delivery id")
        with self.database.read_snapshot() as db:
            if not live_source(db, owner, source_key_id):
                raise PermissionError("Event source authorization denied")
            row = db.execute(
                "SELECT id,status,attempts,available_at,created_at,updated_at,error_code,error_type,result FROM agent_event_deliveries "
                "WHERE id=? AND owner=? AND source_key_id=?",
                (delivery_id, owner, source_key_id),
            ).fetchone()
        if row is None:
            return None
        result = {
            "id": row["id"],
            "status": row["status"],
            "attempts": row["attempts"],
            "availableAt": _iso(row["available_at"]),
            "createdAt": _iso(row["created_at"]),
        }
        if row["status"] in {"succeeded", "dead"}:
            result["settledAt"] = _iso(row["updated_at"])
        if row["result"]:
            result["result"] = json.loads(row["result"])
        if row["error_code"]:
            result["error"] = {
                "code": row["error_code"],
                "message": row["error_type"],
            }
        return result
