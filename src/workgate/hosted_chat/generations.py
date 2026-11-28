"""Durable generation admission and replay adapted from final upstream v6.0.3.

Source: webui/generations.py (afc6ca41ea27925af48cc4d7b4af0a5bf1a154fc).
Owner-bound runs, messages and event logs share one private SQLite transaction.
"""

import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any

from .ask_user_tools import ask_user_requested
from .attachment_history import message_file_ids, resolve_message_files
from .content_filters import (
    filters_from_snapshot,
    validate_agent_submission,
    validate_message_submission,
    validate_model_parameters,
)
from .database import SQLiteDatabase
from .generation_actor_lease import ActorLease, ActorLeaseLost
from .generation_protocol import (
    EVENT_LIMIT,
    MAX_EVENT_BYTES,
    job_generation_protocol,
    public_content,
)
from .model_history import (
    completed_model_steps,
    recover_interrupted_model_steps,
)
from .provider_profiles import ProviderRepository
from .providers import selected_provider
from .sampling import validate_sampling
from .storage import WorkspaceRepository, _now

_TERMINAL = frozenset({"complete", "aborted", "error", "interrupted"})
_MAX_PROMPT_BYTES = 128 * 1024
_ROOT_MESSAGE = "00000000-0000-0000-0000-000000000000"


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


@dataclass(frozen=True)
class Generation:
    run_id: str
    conversation_id: str
    generation_created_at: int
    response_message_id: str
    user_message_id: str
    status: str
    agent_id: str | None = None
    protocol_version: int = 2
    sampling: dict = field(default_factory=dict)
    provider: str = ""
    model: str = ""


def _saved_model_steps(
    db: sqlite3.Connection, conversation_id: str, message_id: str, message
) -> list[dict] | None:
    """Read only canonical steps of the unique terminal host response."""
    rows = db.execute(
        "SELECT id,status,state FROM runs WHERE conversation_id=? "
        "AND json_extract(state,'$.responseMessageId')=? "
        "AND json_type(state,'$.modelSteps') IS NOT NULL LIMIT 2",
        (conversation_id, message_id),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1 or rows[0]["status"] not in _TERMINAL:
        raise PermissionError("Canonical model response identity changed")
    run = rows[0]
    state = json.loads(run["state"])
    if state.get("modelStepsStatus", "complete") != run["status"]:
        raise PermissionError("Canonical model response identity changed")
    if state.get("userMessageId") != message["parent_id"]:
        raise PermissionError("Canonical model response parent changed")
    terminal = db.execute(
        "SELECT data FROM generation_events WHERE run_id=? "
        "AND json_extract(data,'$.final')=1 LIMIT 2",
        (run["id"],),
    ).fetchall()
    if len(terminal) != 1:
        raise PermissionError("Canonical model response is not terminal")
    response = json.loads(terminal[0]["data"]).get("responseMessage")
    if not isinstance(response, dict) or (
        response.get("messageId") != message_id
        or response.get("conversationId") != conversation_id
        or response.get("parentMessageId") != message["parent_id"]
        or response.get("text") != message["text"]
        or response.get("content") != json.loads(message["content"])
    ):
        raise PermissionError(
            "Canonical model response does not match terminal record"
        )
    steps = state["modelSteps"]
    if not isinstance(steps, list) or not all(
        isinstance(item, dict) for item in steps
    ):
        raise PermissionError("Invalid canonical model steps")
    try:
        return completed_model_steps([], steps)
    except ValueError as error:
        raise PermissionError("Incomplete canonical model steps") from error


def _generation_history(
    db: sqlite3.Connection,
    owner: str,
    generation: Generation,
    *,
    include_message_ids: bool,
    rich: bool,
) -> list[dict]:
    """Project the admitted user-message branch, not the latest conversation."""
    row = db.execute(
        "SELECT r.* FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE r.id=? AND c.owner=?",
        (generation.run_id, owner),
    ).fetchone()
    if row is None or GenerationStore._run(row) != generation:
        raise PermissionError("Generation history identity changed")
    message_id = generation.user_message_id
    groups = []
    seen = set()
    while message_id is not None:
        if message_id in seen:
            raise PermissionError("Generation history has a cyclic branch")
        seen.add(message_id)
        message = db.execute(
            "SELECT m.parent_id,m.role,m.text,m.content,EXISTS("
            "SELECT 1 FROM runs r WHERE r.conversation_id=m.conversation_id "
            "AND json_extract(r.state,'$.responseMessageId')=m.id "
            "AND json_extract(r.state,'$.manualCompaction')=1) AS manual_summary "
            "FROM messages m WHERE m.id=? AND m.conversation_id=?",
            (message_id, generation.conversation_id),
        ).fetchone()
        if message is None or message["role"] not in {"user", "assistant"}:
            raise PermissionError("Generation history branch is unavailable")
        if not (message["role"] == "assistant" and message["manual_summary"]):
            steps = (
                _saved_model_steps(
                    db, generation.conversation_id, message_id, message
                )
                if rich and message["role"] == "assistant"
                else None
            )
            if steps is None:
                # Legacy display history remains text-only: never promote
                # UI tool previews into authoritative provider evidence.
                steps = [{"role": message["role"], "content": message["text"]}]
                if rich and message["role"] == "user":
                    admitted = db.execute(
                        "SELECT state FROM runs WHERE conversation_id=? "
                        "AND json_extract(state,'$.userMessageId')=? "
                        "AND json_type(state,'$.messageFileIds') IS NOT NULL LIMIT 2",
                        (generation.conversation_id, message_id),
                    ).fetchall()
                    if len(admitted) > 1:
                        raise PermissionError(
                            "Attachment admission identity changed"
                        )
                    if admitted:
                        ids = json.loads(admitted[0]["state"])["messageFileIds"]
                        steps[0]["files"] = [
                            {"file_id": ident} for ident in ids
                        ]

            groups.append(
                [
                    {
                        **step,
                        **(
                            {"messageId": message_id}
                            if include_message_ids
                            else {}
                        ),
                    }
                    for step in steps
                ]
            )
        message_id = message["parent_id"]
    history = [step for group in reversed(groups) for step in group]
    if not history or (
        not json.loads(row["state"]).get("manualCompaction")
        and history[-1]["role"] != "user"
    ):
        raise PermissionError("Generation user message is unavailable")
    return history


def generation_text_history(
    db: sqlite3.Connection,
    owner: str,
    generation: Generation,
    *,
    include_message_ids: bool = False,
) -> list[dict]:
    """Keep the existing bounded-text consumer contract."""
    return _generation_history(
        db,
        owner,
        generation,
        include_message_ids=include_message_ids,
        rich=False,
    )


def generation_model_history(
    db: sqlite3.Connection,
    owner: str,
    generation: Generation,
    *,
    include_message_ids: bool = False,
) -> list[dict]:
    """Replay exact ordered host-owned model steps on the admitted branch."""
    return _generation_history(
        db,
        owner,
        generation,
        include_message_ids=include_message_ids,
        rich=True,
    )


class GenerationConflict(Exception):
    def __init__(self, existing: Generation):
        self.existing = existing
        super().__init__("Conversation already has an active generation")


class GenerationStore:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    @staticmethod
    def _run(row: Any) -> Generation:
        state = json.loads(row["state"])
        return Generation(
            run_id=row["id"],
            conversation_id=row["conversation_id"],
            generation_created_at=state["generationCreatedAt"],
            response_message_id=state["responseMessageId"],
            user_message_id=state["userMessageId"],
            status=row["status"],
            agent_id=row["agent_id"],
            protocol_version=job_generation_protocol(state),
            provider=state["provider"],
            model=state["model"],
            sampling=state.get("sampling", {}),
        )

    def latest(self, owner: str, conversation_id: str) -> Generation | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT r.* FROM runs r JOIN conversations c ON c.id=r.conversation_id "
                "WHERE c.owner=? AND r.conversation_id=? ORDER BY r.created_at DESC,r.id DESC LIMIT 1",
                (owner, conversation_id),
            ).fetchone()
            return self._run(row) if row else None

    def get(self, owner: str, run_id: str) -> Generation | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT r.* FROM runs r JOIN conversations c ON c.id=r.conversation_id "
                "WHERE c.owner=? AND r.id=?",
                (owner, run_id),
            ).fetchone()
            return self._run(row) if row else None

    def native_admission(self, owner: str, run_id: str) -> dict | None:
        """Public root admission metadata; never a persisted credential."""
        with self.database.read_snapshot() as db:
            row = db.execute(
                "SELECT r.state FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE r.id=? AND c.owner=?",
                (run_id, owner),
            ).fetchone()
        if row is None:
            return None
        state = json.loads(row["state"])
        admission = state.get("nativeAdmission")
        if (
            not isinstance(admission, dict)
            or state.get("detachedTaskId")
            or state.get("completionWakeupTaskId")
        ):
            return None
        return admission

    def begin(
        self,
        owner: str,
        payload: dict,
        *,
        protocol_version: int = 2,
        native_admission: dict | None = None,
    ) -> tuple[Generation, bool]:
        """Admit a single ordinary turn in its own transaction."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._begin_locked(
                db,
                owner,
                payload,
                protocol_version=protocol_version,
                native_admission=native_admission,
            )

    def _begin_locked(
        self,
        db: sqlite3.Connection,
        owner: str,
        payload: dict,
        *,
        protocol_version: int = 2,
        wakeup_task_id: str | None = None,
        native_admission: dict | None = None,
    ) -> tuple[Generation, bool]:
        """Internal admission seam for the atomic completion transaction."""
        if type(protocol_version) is not int or protocol_version not in (1, 2):
            raise ValueError("Invalid generation protocol")
        client_request_id = payload.get("clientRequestId")
        if client_request_id is not None:
            if (
                not isinstance(client_request_id, str)
                or not 1 <= len(client_request_id) <= 160
            ):
                raise ValueError("Invalid client request id")
            if client_request_id.startswith(
                ("subagent:", "subagent-wakeup:", "trigger_")
            ) and not (
                wakeup_task_id is not None
                and client_request_id.startswith(
                    "subagent-wakeup:" + wakeup_task_id + ":"
                )
            ):
                raise ValueError(
                    "Internal generation request id cannot be submitted"
                )
        raw_id = payload.get("conversationId")
        if raw_id is not None and not isinstance(raw_id, str):
            raise ValueError("Invalid conversation id")
        new_conversation = raw_id in (None, "", "new", "new_conversation")
        conversation_id = str(uuid.uuid4()) if new_conversation else raw_id
        assert conversation_id is not None
        manual = payload.get("compact") is True
        if "compact" in payload and type(payload["compact"]) is not bool:
            raise ValueError("Invalid compaction flag")
        if manual and (
            new_conversation
            or wakeup_task_id is not None
            or not isinstance(payload.get("parentMessageId"), str)
            or not payload["parentMessageId"]
            or payload["parentMessageId"] == _ROOT_MESSAGE
        ):
            raise ValueError(
                "Compaction requires an existing conversation and anchor"
            )
        prompt = "" if manual else payload.get("text")
        if not manual and (
            not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES
        ):
            raise ValueError(
                "A non-empty text message is required (max 128 KiB)"
            )
        if any(
            payload.get(key)
            for key in (
                "isContinued",
                "isRegenerate",
                "editedContent",
                "responseMessageId",
                "manualSkills",
                "content",
            )
        ):
            raise ValueError("Unsupported generation request")
        file_ids = message_file_ids(payload.get("files"))
        if file_ids and (manual or wakeup_task_id is not None):
            raise ValueError("Attachments require an interactive turn")
        if (
            file_ids
            and native_admission is not None
            and "shell:read" not in native_admission["scopes"]
        ):
            raise PermissionError("Attachments require read scope")
        assert isinstance(prompt, str)
        user_message_id = (
            payload.get("parentMessageId")
            if manual
            else payload.get("messageId")
        )
        if user_message_id is None:
            user_message_id = str(uuid.uuid4())
        if (
            not isinstance(user_message_id, str)
            or not user_message_id
            or len(user_message_id) > 128
            or user_message_id == _ROOT_MESSAGE
        ):
            raise ValueError("Invalid message id")
        response_message_id = str(uuid.uuid4())
        run_id = "run_" + secrets.token_hex(16)
        if (
            not new_conversation
            and db.execute(
                "SELECT 1 FROM active_subagent_threads WHERE thread_id=?",
                (conversation_id,),
            ).fetchone()
        ):
            raise PermissionError("Child conversation is read-only")
        if client_request_id:
            prior = db.execute(
                "SELECT r.* FROM generation_requests gr JOIN runs r ON r.id=gr.run_id "
                "JOIN conversations c ON c.id=r.conversation_id "
                "WHERE gr.owner=? AND gr.client_request_id=? AND c.owner=?",
                (owner, client_request_id, owner),
            ).fetchone()
            if prior:
                if (
                    not new_conversation
                    and prior["conversation_id"] != conversation_id
                ):
                    raise ValueError(
                        "Request id belongs to another conversation"
                    )
                previous = json.loads(prior["state"])
                if bool(previous.get("manualCompaction")) != manual or (
                    manual and previous["userMessageId"] != user_message_id
                ):
                    raise ValueError("Request id belongs to another operation")
                if previous.get("messageFileIds", []) != file_ids:
                    raise ValueError(
                        "Request id belongs to different attachments"
                    )
                return self._run(prior), False
        now = _now()
        convo = None
        if not new_conversation:
            convo = db.execute(
                "SELECT * FROM conversations WHERE id=? AND owner=?",
                (conversation_id, owner),
            ).fetchone()
            if convo is None:
                raise KeyError("Conversation not found")
            active = db.execute(
                "SELECT * FROM runs WHERE conversation_id=? AND status IN ('running','requires_action') ORDER BY created_at DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if active:
                raise GenerationConflict(self._run(active))
        project_id = (
            payload.get("projectId", payload.get("chatProjectId"))
            if convo is None
            else convo["project_id"]
        )
        if project_id is not None and (
            not isinstance(project_id, str) or not project_id
        ):
            raise ValueError("Invalid project id")
        agent_id = payload.get("agent_id", convo["agent_id"] if convo else None)
        WorkspaceRepository._validate_references(
            db, owner, {"projectId": project_id, "agent_id": agent_id}
        )
        agent = None
        if agent_id is not None:
            agent = json.loads(
                db.execute(
                    "SELECT config FROM agents WHERE id=? AND owner=?",
                    (agent_id, owner),
                ).fetchone()[0]
            )
        filters = filters_from_snapshot(db, owner)
        if not manual:
            validate_message_submission(prompt, filters)
        if agent is not None:
            validate_agent_submission(agent, filters)
        else:
            validate_model_parameters(payload, filters)
        selection = dict(payload)
        if convo:
            selection.setdefault("endpoint", convo["endpoint"])
        for key in ("provider", "endpoint", "model"):
            value = selection.get(key)
            if value is not None and (
                not isinstance(value, str)
                or len(value) > 200
                or any(ord(c) < 32 for c in value)
            ):
                raise ValueError("Invalid model selection")
        provider = selected_provider(selection, agent)
        profile = ProviderRepository._profile_record(db, provider)
        if convo and provider == convo["endpoint"]:
            selection.setdefault("model", convo["model"])
        model = (
            (agent or {}).get("model")
            or selection.get("model")
            or next(iter(profile["models"]), None)
        )
        if (
            not isinstance(model, str)
            or not model.strip()
            or len(model) > 200
            or any(ord(c) < 32 for c in model)
        ):
            raise ValueError("Invalid model identifier")
        metadata = json.loads(convo["metadata"]) if convo else {}
        sampling = (
            {}
            if agent is not None
            else validate_sampling(
                payload.get("lsmSampling", metadata.get("lsmSampling", {})),
                provider=profile["provider"],
            )
        )
        if agent is None and "lsmSampling" in payload:
            metadata["lsmSampling"] = sampling
        state = {
            "userMessageId": user_message_id,
            "responseMessageId": response_message_id,
            "generationCreatedAt": now // 1000,
            "clientRequestId": client_request_id,
            "generationProtocolVersion": protocol_version,
            "provider": provider,
            "model": model,
            "sampling": sampling,
        }
        if file_ids:
            state["messageFileIds"] = file_ids
        if manual:
            state["manualCompaction"] = True
        if native_admission is not None:
            state["nativeAdmission"] = {**native_admission}
            if ask_user_requested(agent, payload):
                state["nativeAdmission"]["ask_user"] = True
        if agent is not None:
            state["agentRevision"] = agent.get("_version", 1)
        if wakeup_task_id is not None:
            state["completionWakeupTaskId"] = wakeup_task_id
        parent_id = payload.get("parentMessageId")
        if parent_id in (None, _ROOT_MESSAGE):
            parent_id = None
        if "parentMessageId" not in payload and convo:
            parent = db.execute(
                "SELECT id FROM messages WHERE conversation_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            parent_id = parent["id"] if parent else None
        if parent_id is not None and (
            not isinstance(parent_id, str)
            or not db.execute(
                "SELECT 1 FROM messages WHERE id=? AND conversation_id=?",
                (parent_id, conversation_id),
            ).fetchone()
        ):
            raise KeyError("Parent message not found")
        if not manual and (
            db.execute(
                "SELECT 1 FROM messages WHERE id=?", (user_message_id,)
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM runs WHERE json_extract(state,'$.responseMessageId')=?",
                (user_message_id,),
            ).fetchone()
        ):
            raise ValueError("Message identifier is already in use")
        title = prompt.strip()[:80] if convo is None else convo["title"]
        if new_conversation:
            db.execute(
                "INSERT INTO conversations(id,owner,title,model,endpoint,project_id,agent_id,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    conversation_id,
                    owner,
                    title,
                    model,
                    provider,
                    project_id,
                    agent_id,
                    _json(metadata),
                    now,
                    now,
                ),
            )
        else:
            db.execute(
                "UPDATE conversations SET model=?,endpoint=?,agent_id=?,metadata=?,updated_at=? WHERE id=? AND owner=?",
                (
                    model,
                    provider,
                    agent_id,
                    _json(metadata),
                    now,
                    conversation_id,
                    owner,
                ),
            )
        user_metadata = {
            "sender": "User",
            "isCreatedByUser": True,
            "endpoint": provider,
            "files": resolve_message_files(db, owner, file_ids),
        }
        if not manual:
            db.execute(
                "INSERT INTO messages(id,conversation_id,parent_id,role,text,content,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    user_message_id,
                    conversation_id,
                    parent_id,
                    "user",
                    prompt,
                    "{}",
                    _json(user_metadata),
                    now,
                    now,
                ),
            )
        db.execute(
            "INSERT INTO runs(id,conversation_id,agent_id,status,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (
                run_id,
                conversation_id,
                agent_id,
                "running",
                _json(state),
                now,
                now,
            ),
        )
        if client_request_id:
            db.execute(
                "INSERT INTO generation_requests(owner,client_request_id,run_id) VALUES(?,?,?)",
                (owner, client_request_id, run_id),
            )
        created = {
            "created": True,
            "conversation": {
                "conversationId": conversation_id,
                "title": title,
            },
            "message": {
                "messageId": user_message_id,
                "conversationId": conversation_id,
                "parentMessageId": parent_id,
                "text": prompt,
                **user_metadata,
            },
        }
        if manual:
            created["message"] = {
                "messageId": user_message_id,
                "conversationId": conversation_id,
                "text": "",
                "compact": True,
            }
        db.execute(
            "INSERT INTO generation_events(run_id,data) VALUES(?,?)",
            (run_id, _json(created)),
        )
        return self._run(
            db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        ), True

    def _assert_actor(
        self, db, owner: str, run_id: str, actor_lease: ActorLease
    ) -> None:
        if (
            actor_lease.owner != owner
            or actor_lease.run_id != run_id
            or actor_lease.database.path != self.database.path
        ):
            raise ActorLeaseLost(
                "Generation actor lease does not match the run"
            )
        actor_lease.assert_current(db)

    def append(
        self, owner: str, run_id: str, event: dict, *, actor_lease: ActorLease
    ) -> int:
        if actor_lease.detached_task_id is not None:
            raise PermissionError(
                "Detached generation cannot publish model deltas"
            )
        encoded = _json(event)
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("Generation event exceeds 256 KiB")
        # Tool completion, attachments, steering and lifecycle events require
        # their own transactional evidence and cannot enter this generic path.
        if (
            not isinstance(event, dict)
            or set(event) != {"event", "data"}
            or not isinstance(event["event"], str)
            or event["event"]
            not in {
                "on_message_delta",
                "on_reasoning_delta",
                "on_run_step",
                "on_run_step_closed",
            }
            or not isinstance(event["data"], dict)
        ):
            raise ValueError("Unsupported generation event")
        if event["event"] in {"on_message_delta", "on_reasoning_delta"}:
            kind = "text" if event["event"] == "on_message_delta" else "think"
            delta = event["data"].get("delta")
            part = delta.get("content") if isinstance(delta, dict) else None
            if (
                not isinstance(part, dict)
                or part.get("type") != kind
                or not isinstance(part.get(kind), str)
            ):
                raise ValueError("Invalid generation delta")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_actor(db, owner, run_id, actor_lease)
            count = db.execute(
                "SELECT count(*) FROM generation_events WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            if count >= EVENT_LIMIT:
                raise ValueError("Generation event retention limit reached")
            cursor = db.execute(
                "INSERT INTO generation_events(run_id,data) VALUES(?,?)",
                (run_id, encoded),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def events(
        self, owner: str, run_id: str, after: int = 0, limit: int = 200
    ) -> list[tuple[int, dict]]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT ge.seq,ge.data FROM generation_events ge JOIN runs r ON r.id=ge.run_id "
                "JOIN conversations c ON c.id=r.conversation_id "
                "WHERE c.owner=? AND ge.run_id=? AND ge.seq>? ORDER BY ge.seq LIMIT ?",
                (owner, run_id, after, min(200, max(1, limit))),
            ).fetchall()
            return [(int(row[0]), json.loads(row[1])) for row in rows]

    def public_delta_cursor(
        self, actor_lease: ActorLease, *, activation: str | None = None
    ) -> int:
        """Latest observable delta, optionally for a single graph activation."""
        with self.database.read_snapshot() as db:
            self._assert_actor(
                db, actor_lease.owner, actor_lease.run_id, actor_lease
            )
            return int(
                db.execute(
                    "SELECT coalesce(max(seq),0) FROM generation_events WHERE run_id=? "
                    "AND json_extract(data,'$.event') IN ('on_message_delta','on_reasoning_delta') "
                    "AND (? IS NULL OR json_extract(data,'$.data.graphActivationId')=?)",
                    (actor_lease.run_id, activation, activation),
                ).fetchone()[0]
            )

    def finish(
        self,
        owner: str,
        run_id: str,
        *,
        status: str,
        text: str = "",
        error: str | None = None,
        actor_lease: ActorLease,
        model_steps: list[dict] | None = None,
    ) -> bool:
        """Persist the final message, unread revision and terminal event atomically."""
        if actor_lease.detached_task_id is not None:
            raise PermissionError("Detached generation must settle its task")
        if status not in _TERMINAL:
            raise ValueError("Only terminal statuses can finish a generation")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT r.* FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE r.id=? AND c.owner=?",
                (run_id, owner),
            ).fetchone()
            if not row or row["status"] in _TERMINAL:
                return False
            self._assert_actor(db, owner, run_id, actor_lease)
            return self._finish_locked(
                db,
                row,
                status=status,
                text=text,
                error=error,
                model_steps=model_steps,
            )

    def _finish_locked(
        self,
        db,
        row,
        *,
        status: str,
        text: str = "",
        error: str | None = None,
        model_steps: list[dict] | None = None,
    ) -> bool:
        """Finalize after the caller has checked the exact transition authority."""
        if status not in _TERMINAL:
            raise ValueError("Only terminal statuses can finish a generation")
        run_id = row["id"]
        generation = self._run(row)
        now = _now()
        content = public_content(
            json.loads(event["data"])
            for event in db.execute(
                "SELECT data FROM generation_events WHERE run_id=? ORDER BY seq",
                (run_id,),
            )
        )
        streamed = "".join(
            part["text"] for part in content if part["type"] == "text"
        )
        if streamed:
            text = streamed
        elif text:
            content.append(
                {"type": "text", "text": text, "phase": "final_answer"}
            )
        if status in {"error", "interrupted"}:
            content.append(
                {"type": "error", "error": error or "Generation failed"}
            )
        metadata = {
            "sender": "Assistant",
            "isCreatedByUser": False,
            "unfinished": status != "complete",
        }
        run_state = json.loads(
            db.execute(
                "SELECT state FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
        )
        if status != "complete":
            if model_steps is not None:
                raise ValueError(
                    "Only completed model turns can save canonical steps"
                )
            model_steps = recover_interrupted_model_steps(db, row, run_state)
        usage = run_state.get("contextUsage")
        if run_state.get("manualCompaction"):
            metadata["manualCompaction"] = True
        if run_state.get("manualCompaction") and status == "complete":
            record = run_state.get("compaction")
            if not isinstance(record, dict):
                raise ValueError("Manual compaction result is missing")
            content.append(
                {
                    "type": "summary",
                    "boundary": {"messageIndex": record["boundary"]},
                    "tokenCount": record["tokenCount"],
                    "initiatedBy": "user",
                    "content": [{"type": "text", "text": record["summary"]}],
                }
            )
        if isinstance(usage, dict):
            metadata["metadata"] = {"contextUsage": usage}
        response_message = {
            "messageId": generation.response_message_id,
            "conversationId": generation.conversation_id,
            "parentMessageId": generation.user_message_id,
            "text": text,
            "content": content,
            **metadata,
        }
        if content or model_steps:
            db.execute(
                "INSERT INTO messages(id,conversation_id,parent_id,role,text,content,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    generation.response_message_id,
                    generation.conversation_id,
                    generation.user_message_id,
                    "assistant",
                    text,
                    _json(content),
                    _json(metadata),
                    now,
                    now,
                ),
            )
        from .steering import SteeringStore

        # Human-action terminal transitions may have updated state in this
        # transaction already; never resurrect a consumed pending claim.
        state = json.loads(
            db.execute(
                "SELECT state FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
        )
        if model_steps is not None:
            if state.get("manualCompaction"):
                raise ValueError(
                    "Only completed model turns can save canonical steps"
                )
            state["modelSteps"] = model_steps
            if status != "complete":
                state["modelStepsStatus"] = status
        pending_steers = SteeringStore.settle_leftovers(state)
        db.execute(
            "UPDATE runs SET status=?,state=?,updated_at=? WHERE id=?",
            (status, _json(state), now, run_id),
        )
        unread = db.execute(
            "UPDATE conversations SET updated_at=?,unread_marker=CASE WHEN unread_marker LIKE 'manual:%' THEN ? ELSE ? END WHERE id=? RETURNING unread_marker",
            (
                now,
                "manual:" + run_id,
                "run:" + run_id,
                generation.conversation_id,
            ),
        ).fetchone()["unread_marker"]
        final = {
            "final": True,
            "unreadMarker": unread,
            "generationProtocolVersion": generation.protocol_version,
            "conversation": {"conversationId": generation.conversation_id},
            "responseMessage": response_message,
            **({"pendingSteers": pending_steers} if pending_steers else {}),
            **({"aborted": True} if status == "aborted" else {}),
            **(
                {"error": error or "Generation failed"}
                if status in {"error", "interrupted"}
                else {}
            ),
        }
        db.execute(
            "INSERT INTO generation_events(run_id,data) VALUES(?,?)",
            (run_id, _json(final)),
        )
        return True
