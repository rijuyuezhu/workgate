"""Transactional editing of an owned visible artifact and canonical replay.

Adapted from pinned v6.0.3 webui/artifact_routes.py, with WorkGate's native
terminal record and child/checkpoint fences. No code is evaluated.
"""

import json
import re
from collections.abc import Callable

from .artifacts import ARTIFACT_START, _units, edit_artifact, find_all_artifacts
from .content_filters import filters_from_snapshot, validate_message_submission
from .database import SQLiteDatabase
from .generation_protocol import MAX_EVENT_BYTES
from .generations import _TERMINAL, _saved_model_steps
from .model_history import completed_model_steps
from .storage import WorkspaceRepository, _json, _now


class ArtifactConflict(ValueError):
    """An immutable transcript or inconsistent canonical response."""


def _artifact_text(artifact: dict) -> str:
    return _units(artifact["text"])[artifact["start"] : artifact["end"]]


def _text_parts(value: dict) -> str:
    return "".join(
        part["text"]
        for part in value.get("content", [])
        if isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    )


def _bounded_artifacts(value: dict) -> list[dict]:
    # Bound source scanning independently of the replacement size. A retained
    # artifact entry references its entire original text for UTF-16 offsets.
    text = value.get("text", "")
    parts = _text_parts(value)
    if (
        len(text.encode()) + len(parts.encode()) > 2 * MAX_EVENT_BYTES
        or max(text.count(ARTIFACT_START), parts.count(ARTIFACT_START)) > 128
    ):
        raise ValueError("Artifact source exceeds edit limits")
    return find_all_artifacts(value)


def _canonical_artifacts(steps: list[dict]) -> list[tuple[int, dict, int, str]]:
    sources = []
    for step_index, step in enumerate(steps):
        if step.get("role") != "assistant":
            continue
        content = step.get("content")
        if isinstance(content, str):
            source = {"text": content}
        elif isinstance(content, list):
            source = {"text": "", "content": content}
        else:
            continue
        for local_index, artifact in enumerate(_bounded_artifacts(source)):
            sources.append(
                (step_index, source, local_index, _artifact_text(artifact))
            )
    return sources


def _edit_steps(
    steps: list[dict],
    visible: dict,
    value: dict,
    index: int,
    original: str,
    updated: str,
) -> list[dict]:
    """Prove the selected visible artifact matches the ordered canonical sequence."""
    sources = _canonical_artifacts(steps)
    artifacts = _bounded_artifacts(visible)
    if [_artifact_text(item) for item in artifacts] != [
        item[3] for item in sources
    ]:
        raise ArtifactConflict("Visible and canonical artifacts do not match")
    step_index, source, local_index, _ = sources[index]
    try:
        edited, _ = edit_artifact(source, local_index, original, updated)
    except ValueError as error:
        raise ArtifactConflict("Canonical artifact changed") from error
    step = steps[step_index]
    step["content"] = (
        edited["text"]
        if isinstance(step["content"], str)
        else edited["content"]
    )
    if [_artifact_text(item) for item in _bounded_artifacts(value)] != [
        item[3] for item in _canonical_artifacts(steps)
    ]:
        raise ArtifactConflict("Edited canonical artifact projection diverges")
    return completed_model_steps([], steps)


def update_artifact(
    database: SQLiteDatabase,
    owner: str,
    message_id: str,
    index: int,
    original: str,
    updated: str,
    *,
    check_grant: Callable[[], None],
) -> dict:
    if (
        type(index) is not int
        or index < 0
        or not isinstance(original, str)
        or not isinstance(updated, str)
    ):
        raise ValueError("Invalid artifact edit parameters")
    if any(
        len(value.encode()) > MAX_EVENT_BYTES for value in (original, updated)
    ):
        raise ValueError("Artifact edit exceeds byte limit")
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        check_grant()
        row = db.execute(
            "SELECT m.*,c.metadata AS conversation_metadata FROM messages m JOIN conversations c ON c.id=m.conversation_id "
            "WHERE m.id=? AND c.owner=?",
            (message_id, owner),
        ).fetchone()
        if row is None:
            raise KeyError("Message not found")
        cid = row["conversation_id"]
        if (
            db.execute(
                "SELECT 1 FROM active_subagent_threads WHERE thread_id=?",
                (cid,),
            ).fetchone()
            or json.loads(row["conversation_metadata"]).get("subagentThread")
            is not None
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-8[0-9a-f]{3}-b[0-9a-f]{3}-[0-9a-f]{12}",
                cid,
                re.IGNORECASE,
            )
        ):
            raise ArtifactConflict("Child conversation is read-only")
        if (
            db.execute(
                "SELECT 1 FROM runs WHERE conversation_id=? AND status IN ('running','requires_action')",
                (cid,),
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM subagent_tasks WHERE owner=? AND parent_conversation_id=? AND status='running'",
                (owner, cid),
            ).fetchone()
        ):
            raise ArtifactConflict("Conversation has active work")
        visible = WorkspaceRepository._message(row)
        _bounded_artifacts(visible)
        value, path = edit_artifact(visible, index, original, updated)
        if path.startswith("/content/"):
            # Native history reads top-level text as its legacy fallback.
            # Refuse an ambiguous imported DTO rather than silently editing
            # only the display projection.
            if _text_parts(visible) != visible["text"]:
                raise ArtifactConflict("Visible and replay text do not match")
            value["text"] = _text_parts(value)
        elif _text_parts(visible):
            raise ArtifactConflict(
                "Artifact cannot be edited across content parts"
            )
        if len(value["text"].encode()) > MAX_EVENT_BYTES:
            raise ValueError("Artifact edit exceeds byte limit")
        validate_message_submission(
            value["text"], filters_from_snapshot(db, owner)
        )
        metadata = json.loads(row["metadata"])
        metadata["userSubmittedPaths"] = value["userSubmittedPaths"]
        runs = db.execute(
            "SELECT id,status,state FROM runs WHERE conversation_id=? AND json_extract(state,'$.responseMessageId')=? LIMIT 2",
            (cid, message_id),
        ).fetchall()
        if len(runs) > 1:
            raise ArtifactConflict("Canonical response identity is ambiguous")
        if runs:
            run = runs[0]
            state = json.loads(run["state"])
            if (
                run["status"] not in _TERMINAL
                or row["role"] != "assistant"
                or state.get("manualCompaction")
            ):
                raise ArtifactConflict(
                    "Artifact response is not an editable terminal message"
                )
            terminal = db.execute(
                "SELECT seq,data FROM generation_events WHERE run_id=? AND json_extract(data,'$.final')=1 LIMIT 2",
                (run["id"],),
            ).fetchall()
            if len(terminal) != 1:
                raise ArtifactConflict("Terminal response is unavailable")
            event = json.loads(terminal[0]["data"])
            response = event.get("responseMessage")
            if not isinstance(response, dict) or any(
                response.get(key) != visible.get(key)
                for key in (
                    "messageId",
                    "conversationId",
                    "parentMessageId",
                    "text",
                    "content",
                )
            ):
                raise ArtifactConflict(
                    "Terminal response does not match visible history"
                )
            if "modelSteps" in state:
                try:
                    steps = _saved_model_steps(db, cid, message_id, row)
                    if steps is None:
                        raise ArtifactConflict(
                            "Canonical model history missing"
                        )
                    state["modelSteps"] = _edit_steps(
                        steps, visible, value, index, original, updated
                    )
                except PermissionError as error:
                    raise ArtifactConflict(
                        "Canonical model history changed"
                    ) from error
                db.execute(
                    "UPDATE runs SET state=?,updated_at=? WHERE id=?",
                    (_json(state), _now(), run["id"]),
                )
            # Preserve original streamed deltas. The final DTO is the current
            # user-edited response, explicitly marked rather than passed off
            # as new model output.
            response.update(
                text=value["text"],
                content=value.get("content", []),
                userSubmittedPaths=value["userSubmittedPaths"],
            )
            encoded = _json(event)
            if len(encoded.encode()) > MAX_EVENT_BYTES:
                raise ValueError("Edited terminal response exceeds byte limit")
            db.execute(
                "UPDATE generation_events SET data=? WHERE seq=?",
                (encoded, terminal[0]["seq"]),
            )
        db.execute(
            "UPDATE messages SET text=?,content=?,metadata=?,updated_at=? WHERE id=?",
            (
                value["text"],
                _json(value.get("content") or {}),
                _json(metadata),
                _now(),
                message_id,
            ),
        )
        check_grant()
        return {
            "conversationId": cid,
            "text": value["text"],
            **({"content": value["content"]} if "content" in value else {}),
        }
