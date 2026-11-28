"""Non-destructive, branch-bound summary checkpoints for foreground chat."""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from .compaction import build_summary_carrier_text
from .content_filters import (
    filters_from_snapshot,
    inspect_content,
    model_message_fragments,
)
from .context_budget import split_recent_turns
from .generation_actor_lease import ActorLease
from .generation_protocol import EVENT_LIMIT
from .generations import GenerationStore, generation_text_history
from .tokenizer import count_tokens

_MAX_SUMMARY_BYTES = 64 * 1024
_MAX_PREFIX_BYTES = 512 * 1024
_MAX_REUSE_CANDIDATES = 32


def _encode(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_encode(value).encode()).hexdigest()


@dataclass(frozen=True)
class CompactionPlan:
    source: list[dict] = field(repr=False)
    fingerprint: str
    scope: str
    retain_turns: int


def _provider_messages(history: list[dict]) -> list[dict]:
    return [
        {"role": message["role"], "content": message["content"]}
        for message in history
    ]


def _validate_content(messages: list[dict], filters: dict | None) -> None:
    blocked = inspect_content(model_message_fragments(messages), filters)
    if blocked is not None:
        raise blocked


def _admitted_history(db: sqlite3.Connection, lease: ActorLease):
    if lease.detached_task_id is not None or lease.wakeup_task_id is not None:
        raise PermissionError("Compaction requires an interactive root")
    lease.assert_current(db)
    row = db.execute(
        "SELECT * FROM runs WHERE id=?", (lease.run_id,)
    ).fetchone()
    state = json.loads(row["state"])
    if row["agent_id"] is not None:
        agent = db.execute(
            "SELECT config FROM agents WHERE owner=? AND id=?",
            (lease.owner, row["agent_id"]),
        ).fetchone()
        if agent is None or json.loads(agent[0]).get(
            "_version", 1
        ) != state.get("agentRevision"):
            raise PermissionError("Compaction Agent revision changed")
    generation = GenerationStore._run(row)
    history = generation_text_history(
        db, lease.owner, generation, include_message_ids=True
    )
    filters = filters_from_snapshot(db, lease.owner)
    settings = db.execute(
        "SELECT value FROM settings WHERE owner=? AND key='endpoints'",
        (lease.owner,),
    ).fetchone()
    endpoints = json.loads(settings[0]) if settings else {}
    summarization = (
        endpoints.get("summarization") if isinstance(endpoints, dict) else None
    )
    scope = _digest(
        [
            lease.owner,
            generation.conversation_id,
            generation.agent_id,
            state.get("agentRevision"),
            generation.provider,
            generation.model,
            filters,
            summarization,
        ]
    )
    return generation, state, history, filters, scope


def _plan(
    db: sqlite3.Connection, lease: ActorLease, retain_turns: int
) -> CompactionPlan | None:
    if type(retain_turns) is not int or not 1 <= retain_turns <= 32:
        raise ValueError("Invalid compaction retained turn count")
    _, _, history, filters, scope = _admitted_history(db, lease)
    source, tail = split_recent_turns(history, turns=retain_turns)
    if (
        not source
        or not tail
        or len(_encode(source).encode()) > _MAX_PREFIX_BYTES
    ):
        return None
    _validate_content(source, filters)
    return CompactionPlan(
        _provider_messages(source), _digest(source), scope, retain_turns
    )


def plan_compaction(
    lease: ActorLease, *, retain_turns: int = 2
) -> CompactionPlan | None:
    with lease.database.read_snapshot() as db:
        return _plan(db, lease, retain_turns)


def commit_compaction(
    lease: ActorLease,
    plan: CompactionPlan,
    summary: str,
    *,
    check_grant: Callable[[], None],
) -> dict:
    """Store a summary and replay event atomically without editing any message."""
    check_grant()
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or len(summary.encode()) > _MAX_SUMMARY_BYTES
    ):
        raise ValueError("Invalid compaction summary")
    summary = summary.strip()
    carrier = build_summary_carrier_text(summary)
    tokens = count_tokens(carrier, exact=False)
    # Replacing history with a larger summary would defeat compaction.
    if tokens >= sum(
        count_tokens(message["content"], exact=False) for message in plan.source
    ):
        raise ValueError("Compaction summary does not reduce context")
    with lease.database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        check_grant()
        actual = _plan(db, lease, plan.retain_turns)
        if actual != plan:
            raise PermissionError("Compaction source or policy changed")
        generation, state, _, filters, _ = _admitted_history(db, lease)
        _validate_content([{"role": "system", "content": carrier}], filters)
        record = {
            "fingerprint": plan.fingerprint,
            "scope": plan.scope,
            "boundary": len(plan.source),
            "summary": summary,
            "tokenCount": tokens,
        }
        prior = state.get("compaction")
        if prior == record:
            return record
        if (
            prior is not None
            and prior.get("scope") == plan.scope
            and prior["boundary"] >= record["boundary"]
        ):
            raise ValueError(
                "Compaction checkpoint cannot replace the same or a newer prefix"
            )
        count = db.execute(
            "SELECT count(*) FROM generation_events WHERE run_id=?",
            (lease.run_id,),
        ).fetchone()[0]
        if count >= EVENT_LIMIT:
            raise ValueError("Generation event retention limit reached")
        state["compaction"] = record
        db.execute(
            "UPDATE runs SET state=? WHERE id=?", (_encode(state), lease.run_id)
        )
        # The text remains in private state; replay only bounded checkpoint
        # metadata, so a later policy change cannot redisclose the summary.
        event = {
            "event": "on_compaction",
            "data": {
                "runId": lease.run_id,
                "conversationId": generation.conversation_id,
                "boundary": record["boundary"],
                "tokenCount": tokens,
            },
        }
        db.execute(
            "INSERT INTO generation_events(run_id,data) VALUES(?,?)",
            (lease.run_id, _encode(event)),
        )
        check_grant()
        lease.assert_current(db)
        return record


def project_compaction(lease: ActorLease, messages: list[dict]) -> list[dict]:
    """Reuse the longest exact prefix on this branch, preserving the input list."""
    with lease.database.read_snapshot() as db:
        generation, state, history, filters, scope = _admitted_history(
            db, lease
        )
        prior_runs = db.execute(
            "SELECT json_extract(state,'$.compaction') FROM runs "
            "WHERE conversation_id=? AND status='complete' AND id!=? "
            "AND json_extract(state,'$.compaction') IS NOT NULL "
            "ORDER BY created_at DESC,id DESC LIMIT ?",
            (generation.conversation_id, lease.run_id, _MAX_REUSE_CANDIDATES),
        ).fetchall()
        candidates = [
            state.get("compaction"),
            *(json.loads(row[0]) for row in prior_runs),
        ]
        leading = 0
        while (
            leading < len(messages)
            and messages[leading].get("role") == "system"
        ):
            leading += 1
        best = None
        for candidate in candidates:
            if (
                not isinstance(candidate, dict)
                or candidate.get("scope") != scope
            ):
                continue
            boundary = candidate["boundary"]
            if not 0 < boundary < len(history) or (
                best is not None and boundary <= best["boundary"]
            ):
                continue
            prefix = history[:boundary]
            if _digest(prefix) != candidate["fingerprint"] or messages[
                leading : leading + boundary
            ] != _provider_messages(prefix):
                continue
            carrier = build_summary_carrier_text(candidate["summary"])
            # Validate both the hidden original source and the derived
            # summary against the current assembled-context policy.
            _validate_content(prefix, filters)
            _validate_content([{"role": "system", "content": carrier}], filters)
            best = candidate
        if best is None:
            return messages
        return [
            *messages[:leading],
            {
                "role": "system",
                "content": build_summary_carrier_text(best["summary"]),
            },
            *messages[leading + best["boundary"] :],
        ]
