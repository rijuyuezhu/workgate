"""Authenticated event-bound direct children with an atomic read-only guard."""

import hashlib
import json
import re
import uuid

from .database import SQLiteDatabase
from .event_envelope import _TOKEN, EventConflict, _string
from .event_source_keys import live_source
from .storage import _json, _now

_BINDING_ID = re.compile(r"evtbind_[a-f0-9]{48}")
_MAX_BINDINGS = 256


def binding_key(owner: str, source_key_id: str, registration_key: str) -> str:
    return (
        "evtbind_"
        + hashlib.sha256(
            (
                "librechat:agent-event-binding:v1\0"
                + owner
                + "\0\0"
                + source_key_id
                + "\0"
                + registration_key
            ).encode()
        ).hexdigest()[:48]
    )


def _binding_response(row) -> dict:
    return {
        "id": row["id"],
        "actorId": row["actor_id"],
        "agentId": row["agent_id"],
        "threadId": row["conversation_id"],
    }


def live_binding(db, owner: str, source_key_id: str, binding_id: str):
    if not isinstance(binding_id, str) or not _BINDING_ID.fullmatch(binding_id):
        raise ValueError("Invalid event binding")
    if not live_source(db, owner, source_key_id, mode="continue"):
        return None
    row = db.execute(
        "SELECT b.* FROM agent_event_bindings b "
        "JOIN conversations c ON c.id=b.conversation_id AND c.owner=b.owner AND c.agent_id=b.agent_id "
        "JOIN conversations p ON p.id=b.parent_conversation_id AND p.owner=b.owner "
        "JOIN active_subagent_threads a ON a.owner=b.owner AND a.thread_id=c.id AND a.active=1 "
        "JOIN runs r ON r.id=a.parent_run_id AND r.conversation_id=p.id AND r.agent_id=b.parent_agent_id "
        "AND r.status='complete' AND json_extract(r.state,'$.responseMessageId')=b.parent_message_id "
        "WHERE b.id=? AND b.owner=? AND b.source_key_id=?",
        (binding_id, owner, source_key_id),
    ).fetchone()
    if row is None:
        return None
    parent = db.execute(
        "SELECT config FROM agents WHERE id=? AND owner=?",
        (row["parent_agent_id"], owner),
    ).fetchone()
    child = db.execute(
        "SELECT 1 FROM agents WHERE id=? AND owner=?", (row["agent_id"], owner)
    ).fetchone()
    if parent is None or child is None:
        return None
    if not _allowed(
        json.loads(parent["config"]), row["parent_agent_id"], row["agent_id"]
    ):
        return None
    return row


def _allowed(parent: dict, parent_id: str, target: str) -> bool:
    options = parent.get("subagents")
    return (
        isinstance(options, dict)
        and options.get("enabled") is True
        and (
            (parent_id == target and options.get("allowSelf") is not False)
            or target in options.get("agent_ids", [])
        )
    )


class EventBindings:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def register(
        self,
        owner: str,
        source_key_id: str,
        registration_key: str,
        *,
        actor_id: str,
        parent_conversation_id: str,
        parent_message_id: str,
        target_agent_id: str,
    ) -> dict:
        for value, label in (
            (actor_id, "actorId"),
            (parent_conversation_id, "parentConversationId"),
            (parent_message_id, "parentMessageId"),
            (target_agent_id, "agentId"),
        ):
            _string(value, label, 256)
        if not isinstance(registration_key, str) or not _TOKEN.fullmatch(
            registration_key
        ):
            raise ValueError("Invalid registration key")
        identifier = binding_key(owner, source_key_id, registration_key)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not live_source(db, owner, source_key_id, mode="continue"):
                raise PermissionError("Event source authorization denied")
            parent = db.execute(
                "SELECT * FROM conversations WHERE owner=? AND id=?",
                (owner, parent_conversation_id),
            ).fetchone()
            if parent is None:
                raise KeyError("Parent conversation not found")
            if (
                json.loads(parent["metadata"]).get("subagentThread")
                or db.execute(
                    "SELECT 1 FROM active_subagent_threads WHERE owner=? AND thread_id=?",
                    (owner, parent_conversation_id),
                ).fetchone()
            ):
                raise PermissionError(
                    "Event parent must be an ordinary conversation"
                )
            message = db.execute(
                "SELECT 1 FROM messages WHERE conversation_id=? AND id=? AND role='assistant'",
                (parent_conversation_id, parent_message_id),
            ).fetchone()
            rows = db.execute(
                "SELECT id,agent_id FROM runs WHERE conversation_id=? AND status='complete' "
                "AND json_extract(state,'$.responseMessageId')=? LIMIT 2",
                (parent_conversation_id, parent_message_id),
            ).fetchall()
            if not message or len(rows) != 1 or not rows[0]["agent_id"]:
                raise PermissionError(
                    "Parent reply is not an authoritative completed Agent run"
                )
            run = rows[0]
            parent_id = run["agent_id"]
            config = db.execute(
                "SELECT config FROM agents WHERE owner=? AND id=?",
                (owner, parent_id),
            ).fetchone()
            child = db.execute(
                "SELECT 1 FROM agents WHERE owner=? AND id=?",
                (owner, target_agent_id),
            ).fetchone()
            if (
                config is None
                or child is None
                or not _allowed(
                    json.loads(config["config"]), parent_id, target_agent_id
                )
            ):
                raise PermissionError("Target is not a live direct child")
            prior = db.execute(
                "SELECT * FROM agent_event_bindings WHERE id=?", (identifier,)
            ).fetchone()
            if prior is not None:
                fields = (
                    prior["owner"],
                    prior["source_key_id"],
                    prior["actor_id"],
                    prior["parent_conversation_id"],
                    prior["parent_message_id"],
                    prior["parent_agent_id"],
                    prior["agent_id"],
                )
                if fields != (
                    owner,
                    source_key_id,
                    actor_id,
                    parent_conversation_id,
                    parent_message_id,
                    parent_id,
                    target_agent_id,
                ):
                    raise EventConflict(
                        "Idempotency-Key was already used for a different event binding"
                    )
                if live_binding(db, owner, source_key_id, identifier) is None:
                    raise PermissionError("Event binding is no longer valid")
                return _binding_response(prior)
            if (
                db.execute(
                    "SELECT count(*) FROM agent_event_bindings WHERE owner=?",
                    (owner,),
                ).fetchone()[0]
                >= _MAX_BINDINGS
            ):
                raise EventConflict("Event binding capacity reached")
            thread = str(uuid.uuid5(uuid.NAMESPACE_URL, identifier))
            now = _now()
            metadata = {
                "agentEventBinding": {
                    "bindingId": identifier,
                    "sourceKeyId": source_key_id,
                    "actorId": actor_id,
                },
                "subagentThread": {
                    "rootConversationId": parent_conversation_id,
                    "parentConversationId": parent_conversation_id,
                    "parentMessageId": parent_message_id,
                    "parentToolCallId": "event-binding:" + identifier,
                    "parentAgentId": parent_id,
                    "subagentType": target_agent_id,
                    "subagentKind": "agent",
                    "depth": 1,
                },
            }
            db.execute(
                "INSERT INTO conversations(id,owner,title,agent_id,endpoint,archived,metadata,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?,?)",
                (
                    thread,
                    owner,
                    ("Agent actor: " + actor_id)[:120],
                    target_agent_id,
                    "agents",
                    _json(metadata),
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO active_subagent_threads(owner,thread_id,parent_run_id,active) VALUES(?,?,?,1)",
                (owner, thread, run["id"]),
            )
            db.execute(
                "INSERT INTO agent_event_bindings(id,owner,source_key_id,actor_id,parent_conversation_id,parent_message_id,parent_agent_id,agent_id,conversation_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    owner,
                    source_key_id,
                    actor_id,
                    parent_conversation_id,
                    parent_message_id,
                    parent_id,
                    target_agent_id,
                    thread,
                    now,
                ),
            )
            return {
                "id": identifier,
                "actorId": actor_id,
                "agentId": target_agent_id,
                "threadId": thread,
            }
