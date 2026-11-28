"""Authenticated native private attachment transport."""

import asyncio
import os
import sqlite3
import stat
from pathlib import Path
from urllib.parse import quote

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
from ...ui.http.routes import UI_API_PREFIX
from ..runtime import ControlRuntime
from .hosted_chat import _HEADERS, _principal

_PREFIX = UI_API_PREFIX + "/chat/files"
_FILE_HEADERS = {**_HEADERS, "X-Content-Type-Options": "nosniff"}


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
            if request.method == "POST":
                result = await _upload(request, database, owner, check)
                if request.path_params.get("agent_id") is not None:
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
                outcome = await asyncio.to_thread(
                    AttachmentDeletionStore(database).delete,
                    owner,
                    payload["file_ids"],
                    check_grant=check,
                )
                return JSONResponse(outcome, headers=_FILE_HEADERS)
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

    return [
        Route(_PREFIX, handle, methods=["GET", "POST", "DELETE"]),
        Route(_PREFIX + "/download/{file_id}", handle, methods=["GET"]),
        Route(_PREFIX + "/agent/{agent_id}", handle, methods=["POST"]),
    ]
