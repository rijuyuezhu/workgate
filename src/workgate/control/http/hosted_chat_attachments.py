"""Authenticated native private attachment transport."""

import asyncio
import csv
import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from urllib.parse import quote
from xml.etree.ElementTree import ParseError

from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ...hosted_chat.agents import AgentRepository
from ...hosted_chat.attachment_deletion import AttachmentDeletionStore
from ...hosted_chat.attachment_history import file_record
from ...hosted_chat.attachment_uploads import (
    MAX_ATTACHMENT_BYTES,
    _safe_path,
    publish_attachment,
)
from ...hosted_chat.content_filters import ContentFilterBlocked
from ...hosted_chat.context_extraction import (
    ContextExtractionError,
    extract_context_text,
)
from ...hosted_chat.database import SQLiteDatabase
from ...hosted_chat.machine_permissions import require_machine_scope
from ...hosted_chat.office_preview import (
    buffer_to_office_html,
    office_html_bucket,
)
from ...ui.http.routes import UI_API_PREFIX
from ..runtime import ControlRuntime
from .hosted_chat import _HEADERS, _principal

_PREFIX = UI_API_PREFIX + "/chat/files"
_FILE_HEADERS = {**_HEADERS, "X-Content-Type-Options": "nosniff"}
_PREVIEW_HEADERS = {
    **_FILE_HEADERS,
    "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
    "Referrer-Policy": "no-referrer",
}


def _preview_record(database: SQLiteDatabase, owner: str, ident: str):
    with database.read_snapshot() as db:
        row = db.execute(
            "SELECT f.* FROM attachments f JOIN tool_attachment_links l ON l.file_id=f.id "
            "JOIN runs r ON r.id=l.run_id JOIN conversations c ON c.id=r.conversation_id "
            "WHERE f.id=? AND f.owner=? AND c.owner=f.owner "
            "AND l.response_message_id=json_extract(r.state,'$.responseMessageId') "
            "AND NOT EXISTS (SELECT 1 FROM attachment_deletions WHERE file_id=f.id)",
            (ident, owner),
        ).fetchone()
    if (
        row is None
        or office_html_bucket(row["name"], row["content_type"]) is None
    ):
        raise KeyError("Preview not found")
    return row


async def _office_preview(
    database: SQLiteDatabase, owner: str, ident: str, check
):
    row = _preview_record(database, owner, ident)
    metadata = json.loads(row["metadata"])
    try:
        data = await asyncio.to_thread(_read_bytes, database, Path(row["path"]))
    except OSError as error:
        raise KeyError("Preview unavailable") from error
    if (
        metadata.get("bytes") != len(data)
        or metadata.get("sha256") != hashlib.sha256(data).hexdigest()
    ):
        raise KeyError("Preview bytes changed")
    check()
    try:
        rendered = await asyncio.to_thread(
            buffer_to_office_html, data, row["name"], row["content_type"]
        )
    except (
        ValueError,
        ParseError,
        OSError,
        KeyError,
        OverflowError,
        csv.Error,
        RecursionError,
    ):
        rendered = None
    check()
    # Deletion and revocation during rendering must not disclose old bytes.
    if dict(_preview_record(database, owner, ident)) != dict(row):
        raise KeyError("Preview changed")
    if rendered is None:
        return JSONResponse(
            {"error": "Office preview unavailable; original download retained"},
            status_code=422,
            headers=_PREVIEW_HEADERS,
        )
    return Response(rendered, media_type="text/html", headers=_PREVIEW_HEADERS)


def _record(row):
    record = file_record(row)
    return {
        key: record[key]
        for key in (
            "file_id",
            "filename",
            "type",
            "filepath",
            "source",
            "createdAt",
            "conversationId",
            "bytes",
            "processed",
            "extraction",
        )
        if key in record
    }


def _read_bytes(database: SQLiteDatabase, path: Path) -> bytes:
    path = _safe_path(database, path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise OSError("Not a regular attachment")
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > MAX_ATTACHMENT_BYTES
        ):
            raise OSError("Invalid attachment bytes")
        data = stream.read(MAX_ATTACHMENT_BYTES + 1)
        if len(data) != info.st_size or len(data) > MAX_ATTACHMENT_BYTES:
            raise OSError("Attachment changed")
    return data


def _agent_files(database: SQLiteDatabase, owner: str, agent_id: str):
    """Read the owned Agent and its live file records from one snapshot."""
    with database.read_snapshot() as db:
        agent = db.execute(
            "SELECT config FROM agents WHERE id=? AND owner=?",
            (agent_id, owner),
        ).fetchone()
        if agent is None:
            raise KeyError("Agent not found")
        identities = (
            json.loads(agent["config"])
            .get("tool_resources", {})
            .get("context", {})
            .get("file_ids", [])
        )
        records = []
        for ident in dict.fromkeys(identities):
            row = db.execute(
                "SELECT * FROM attachments WHERE id=? AND owner=? "
                "AND NOT EXISTS (SELECT 1 FROM attachment_deletions WHERE file_id=?)",
                (ident, owner, ident),
            ).fetchone()
            if row is not None:
                records.append(_record(row))
        return records


async def _upload(request: Request, database: SQLiteDatabase, owner, check):
    if (
        request.headers.get("content-type", "").split(";")[0].strip().lower()
        != "multipart/form-data"
    ):
        raise ValueError("Multipart upload required")

    async def bounded():
        count = 0
        async for chunk in request.stream():
            check()
            count += len(chunk)
            if count > MAX_ATTACHMENT_BYTES + 65536:
                raise MultiPartException("Attachment exceeds upload limit")
            yield chunk

    parser = MultiPartParser(
        request.headers, bounded(), max_files=1, max_fields=0
    )
    form = None
    try:
        form = await parser.parse()
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or upload.size is None:
            raise ValueError("Exactly one file required")

        async def chunks():
            while chunk := await upload.read(65536):
                yield chunk

        agent_id = request.path_params.get("agent_id")
        binder = None
        if agent_id is not None:
            repository = AgentRepository(database)
            if repository.get(owner, agent_id) is None:
                raise KeyError("Agent not found")

            def bind(db, ident):
                return repository.add_resource_file(
                    owner, agent_id, "context", ident, db=db
                )

            binder = bind

        result = await publish_attachment(
            database,
            owner,
            upload.filename or "",
            upload.content_type or "application/octet-stream",
            chunks(),
            expected_size=upload.size,
            check_grant=check,
            process=extract_context_text if agent_id is not None else None,
            bind=binder,
        )
        with database.connect() as db:
            row = db.execute(
                "SELECT * FROM attachments WHERE owner=? AND id=?",
                (owner, result["file_id"]),
            ).fetchone()
        if row is None:
            raise KeyError("Attachment not found")
        return _record(row)
    finally:
        if form is not None:
            await form.close()
        else:
            for handle in parser._files_to_close_on_error:
                handle.close()


def hosted_chat_attachment_routes(runtime: ControlRuntime) -> list[Route]:
    async def handle(request: Request):
        try:
            principal = _principal(runtime, request)

            def check():
                principal.check()
                require_machine_scope(
                    set(principal.scopes),
                    "read" if request.method in {"GET", "HEAD"} else "write",
                    expires=principal.expires,
                )

            check()
            database = SQLiteDatabase(runtime.config.state_dir)
            owner = principal.owner
            ident = request.path_params.get("file_id")
            agent_id = request.path_params.get("agent_id")
            if request.method == "POST":
                result = await _upload(request, database, owner, check)
                if agent_id is not None:
                    runtime.generation_supervisor.wake_cleanup()
                return JSONResponse(
                    result, status_code=201, headers=_FILE_HEADERS
                )
            if request.method == "DELETE":
                payload = await request.json()
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"file_ids"}
                    or not isinstance(payload["file_ids"], list)
                ):
                    raise ValueError("Invalid attachment deletion request")
                if agent_id is not None:
                    with database.connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        check()
                        result = AgentRepository(
                            database
                        ).remove_resource_files(
                            owner,
                            agent_id,
                            [
                                {"tool_resource": "context", "file_id": item}
                                for item in payload["file_ids"]
                            ],
                            db=db,
                        )
                        check()
                    runtime.generation_supervisor.wake_cleanup()
                    return JSONResponse(
                        {"success": True, "version": result["version"]},
                        headers=_FILE_HEADERS,
                    )
                outcome = await asyncio.to_thread(
                    AttachmentDeletionStore(database).delete,
                    owner,
                    payload["file_ids"],
                    check_grant=check,
                )
                return JSONResponse(outcome, headers=_FILE_HEADERS)
            if agent_id is not None:
                records = _agent_files(database, owner, agent_id)
                check()
                return JSONResponse(records, headers=_FILE_HEADERS)
            with database.connect() as db:
                if ident is None:
                    rows = db.execute(
                        "SELECT * FROM attachments WHERE owner=? ORDER BY created_at DESC,id",
                        (owner,),
                    ).fetchall()
                    return JSONResponse(
                        [_record(row) for row in rows], headers=_FILE_HEADERS
                    )
                row = db.execute(
                    "SELECT * FROM attachments WHERE owner=? AND id=?",
                    (owner, ident),
                ).fetchone()
            if row is None:
                raise KeyError("Attachment not found")
            try:
                data = await asyncio.to_thread(
                    _read_bytes, database, Path(row["path"])
                )
            except OSError as exc:
                raise KeyError("Attachment not found") from exc
            check()
            return Response(
                data,
                media_type="application/octet-stream",
                headers={
                    **_FILE_HEADERS,
                    "Content-Disposition": "attachment; filename*=UTF-8''"
                    + quote(row["name"], safe=""),
                },
            )
        except PermissionError:
            return JSONResponse(
                {"error": "Attachment operation denied"},
                status_code=403,
                headers=_FILE_HEADERS,
            )
        except KeyError:
            return JSONResponse(
                {"error": "Attachment not found"},
                status_code=404,
                headers=_FILE_HEADERS,
            )
        except ContextExtractionError as error:
            return JSONResponse(
                {"error": str(error)},
                status_code=error.status_code,
                headers=_FILE_HEADERS,
            )
        except ContentFilterBlocked as error:
            return JSONResponse(
                error.response(), status_code=400, headers=_FILE_HEADERS
            )
        except ValueError, MultiPartException:
            return JSONResponse(
                {"error": "Invalid attachment request"},
                status_code=400,
                headers=_FILE_HEADERS,
            )
        except OSError, sqlite3.DatabaseError:
            return JSONResponse(
                {"error": "Attachment storage unavailable"},
                status_code=503,
                headers=_FILE_HEADERS,
            )

    async def preview(request: Request):
        try:
            principal = _principal(runtime, request)

            def check():
                principal.check()
                require_machine_scope(
                    set(principal.scopes), "read", expires=principal.expires
                )

            check()
            return await _office_preview(
                SQLiteDatabase(runtime.config.state_dir),
                principal.owner,
                request.path_params["file_id"],
                check,
            )
        except PermissionError:
            return JSONResponse(
                {"error": "Attachment operation denied"},
                status_code=403,
                headers=_PREVIEW_HEADERS,
            )
        except KeyError:
            return JSONResponse(
                {"error": "Preview not found"},
                status_code=404,
                headers=_PREVIEW_HEADERS,
            )
        except OSError, sqlite3.DatabaseError:
            return JSONResponse(
                {"error": "Preview unavailable"},
                status_code=503,
                headers=_PREVIEW_HEADERS,
            )

    return [
        Route(_PREFIX, handle, methods=["GET", "POST", "DELETE"]),
        Route(_PREFIX + "/download/{file_id}", handle, methods=["GET"]),
        Route(_PREFIX + "/preview/{file_id}", preview, methods=["GET"]),
        Route(
            _PREFIX + "/agent/{agent_id}",
            handle,
            methods=["GET", "POST", "DELETE"],
        ),
    ]
