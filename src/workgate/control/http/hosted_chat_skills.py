"""Owner-managed chat Skills; installed Control Skills remain read-only."""

import base64
import binascii
import json

import yaml
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ...hosted_chat.content_filters import ContentFilterBlocked
from ...hosted_chat.database import SQLiteDatabase
from ...hosted_chat.machine_permissions import require_machine_scope
from ...hosted_chat.managed_skill_files import ManagedSkillFiles, skill_path
from ...hosted_chat.managed_skill_import import import_skill
from ...hosted_chat.managed_skills import ManagedSkills, SkillConflict
from ...ui.http.routes import UI_API_PREFIX
from ..runtime import ControlRuntime
from .hosted_chat import _HEADERS, _principal

_PREFIX = UI_API_PREFIX + "/chat/skills"
_MAX_BODY = 15 * 1024 * 1024
_RAW_HEADERS = {
    **_HEADERS,
    "X-Content-Type-Options": "nosniff",
    "Content-Disposition": "attachment",
    "Content-Security-Policy": "sandbox; default-src 'none'",
}


async def _body(request: Request, check):
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise ValueError("JSON required")
    raw = bytearray()
    async for chunk in request.stream():
        check()
        if len(raw) + len(chunk) > _MAX_BODY:
            raise ValueError("Skill request too large")
        raw.extend(chunk)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Invalid Skill request")
    check()
    return data


def _text(value):
    if not isinstance(value, str):
        raise ValueError("Expected text")
    return value


def _version(payload):
    version = payload.get("expectedVersion")
    if type(version) is not int:
        raise ValueError("Expected Skill version required")
    return version


def _file_bytes(payload):
    if "base64" in payload:
        if "content" in payload:
            raise ValueError("Select one file encoding")
        return base64.b64decode(_text(payload["base64"]), validate=True)
    return _text(payload.get("content", "")).encode("utf-8")


def _deployment(record):
    name = record["name"]
    return {
        "_id": "deployment:" + name,
        "name": name,
        "displayTitle": name,
        "description": record.get("description") or name,
        "source": "deployment",
        "author": "deployment",
        "version": 1,
        "fileCount": len(record.get("related_files") or []),
        "alwaysApply": False,
        "userInvocable": True,
        "disableModelInvocation": False,
        "allowedTools": [],
    }


async def _installed(runtime):
    if not runtime.config.agent_bridge_enabled:
        return []
    return (await runtime.agent_bridge.list_skills(None)).skills


def _main_document(record):
    front = {
        **record.get("frontmatter", {}),
        "name": record["name"],
        "description": record["description"],
    }
    if "alwaysApply" in front:
        del front["alwaysApply"]
    front["always-apply"] = record["alwaysApply"]
    return (
        "---\n"
        + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
        + "---\n"
        + record["body"]
    )


def _failure(error):
    if isinstance(error, ContentFilterBlocked):
        return JSONResponse(error.response(), status_code=422, headers=_HEADERS)
    if isinstance(error, SkillConflict):
        return JSONResponse(
            {"error": "skill_version_or_path_conflict"},
            status_code=409,
            headers=_HEADERS,
        )
    if isinstance(error, KeyError):
        return JSONResponse(
            {"error": "Skill or file not found"},
            status_code=404,
            headers=_HEADERS,
        )
    if isinstance(error, PermissionError):
        return JSONResponse(
            {"error": "Skill access denied"}, status_code=403, headers=_HEADERS
        )
    return JSONResponse(
        {"error": "Invalid Skill request"}, status_code=400, headers=_HEADERS
    )


def hosted_chat_skill_routes(runtime: ControlRuntime) -> list[Route]:
    def context(request):
        principal = _principal(runtime, request)

        def check():
            principal.check()
            require_machine_scope(
                set(principal.scopes),
                "read" if request.method == "GET" else "write",
                expires=principal.expires,
            )

        check()
        return principal.owner, SQLiteDatabase(runtime.config.state_dir), check

    async def import_bundle(request: Request):
        try:
            owner, db, check = context(request)
            payload = await _body(request, check)
            if set(payload) != {"filename", "base64"}:
                raise ValueError("Invalid Skill import request")
            result = import_skill(
                db, owner, _text(payload["filename"]), _file_bytes(payload)
            )
            return JSONResponse(result, status_code=201, headers=_HEADERS)
        except (
            KeyError,
            PermissionError,
            ValueError,
            TypeError,
            RecursionError,
            OSError,
            binascii.Error,
        ) as error:
            return _failure(error)

    async def catalog(request: Request):
        try:
            owner, db, check = context(request)
            repo = ManagedSkills(db)
            sid = request.path_params.get("skill_id")
            if request.method == "GET":
                if sid is None:
                    items = repo.list(owner) + [
                        _deployment(row) for row in await _installed(runtime)
                    ]
                    query = request.query_params.get("q", "").casefold()
                    if query:
                        items = [
                            row
                            for row in items
                            if query
                            in (
                                row["name"] + " " + row["description"]
                            ).casefold()
                        ]
                    offset = int(request.query_params.get("after", "0"))
                    limit = int(request.query_params.get("limit", "100"))
                    if offset < 0 or not 1 <= limit <= 256:
                        raise ValueError("Invalid Skill page")
                    after = (
                        str(offset + limit)
                        if offset + limit < len(items)
                        else None
                    )
                    result = {
                        "skills": items[offset : offset + limit],
                        "has_more": after is not None,
                        "after": after,
                    }
                elif sid.startswith("deployment:"):
                    name = sid.removeprefix("deployment:")
                    records = await _installed(runtime)
                    record = next(
                        (row for row in records if row["name"] == name), None
                    )
                    if record is None:
                        raise KeyError("Skill not found")
                    loaded = await runtime.agent_bridge.activate_skill(
                        None, name
                    )
                    result = {
                        **_deployment(record),
                        "body": loaded.content,
                        "frontmatter": {},
                    }
                else:
                    result = repo.get(owner, sid)
                check()
                return JSONResponse(result, headers=_HEADERS)
            if sid is not None and sid.startswith("deployment:"):
                raise PermissionError("Installed Skill is read-only")
            if request.method == "DELETE":
                assert sid is not None
                repo.delete(owner, sid)
                return JSONResponse(
                    {"id": sid, "deleted": True, "cleanupComplete": True},
                    headers=_HEADERS,
                )
            payload = await _body(request, check)
            result = (
                repo.create(owner, payload)
                if sid is None
                else repo.update(owner, sid, payload)
            )
            return JSONResponse(
                result,
                status_code=201 if sid is None else 200,
                headers=_HEADERS,
            )
        except (
            KeyError,
            PermissionError,
            ValueError,
            TypeError,
            RecursionError,
            OSError,
        ) as error:
            return _failure(error)

    async def states(request: Request):
        try:
            owner, db, check = context(request)
            repo = ManagedSkills(db)
            if request.method == "GET":
                result = repo.states(owner)
            else:
                values = (await _body(request, check)).get("skillStates")
                if not isinstance(values, dict):
                    raise ValueError("Invalid Skill states")
                result = repo.update_states(owner, values)
            return JSONResponse(result, headers=_HEADERS)
        except (
            KeyError,
            PermissionError,
            ValueError,
            TypeError,
            RecursionError,
        ) as error:
            return _failure(error)

    async def tree(request: Request):
        try:
            owner, db, check = context(request)
            sid = request.path_params["skill_id"]
            if sid.startswith("deployment:"):
                raise PermissionError("Installed Skill is read-only")
            repo = ManagedSkillFiles(db)
            nid = request.path_params.get("node_id")
            if request.method == "GET":
                return JSONResponse(repo.tree(owner, sid), headers=_HEADERS)
            payload = await _body(request, check)
            version = _version(payload)
            if request.method == "DELETE":
                assert nid is not None
                result = {
                    "deleted": True,
                    "version": repo.delete(
                        owner, sid, nid, expected_version=version
                    ),
                }
            elif request.method == "PATCH":
                assert nid is not None
                result = repo.move(
                    owner,
                    sid,
                    nid,
                    _text(payload.get("path")),
                    expected_version=version,
                    position=payload.get("order"),
                )
            else:
                path = _text(payload.get("path"))
                kind = payload.get("type")
                if kind == "folder":
                    result = repo.create_folder(
                        owner,
                        sid,
                        path,
                        expected_version=version,
                        position=payload.get("order", 0),
                    )
                elif kind == "file":
                    result = repo.put_file(
                        owner,
                        sid,
                        path,
                        _file_bytes(payload),
                        expected_version=version,
                        mime=payload.get("mimeType", "text/plain"),
                        position=payload.get("order", 0),
                    )
                else:
                    raise ValueError("Invalid Skill node kind")
            return JSONResponse(
                result,
                status_code=201 if request.method == "POST" else 200,
                headers=_HEADERS,
            )
        except (
            KeyError,
            PermissionError,
            ValueError,
            TypeError,
            RecursionError,
            binascii.Error,
        ) as error:
            return _failure(error)

    async def file(request: Request):
        try:
            owner, db, check = context(request)
            sid = request.path_params["skill_id"]
            path = request.path_params["path"]
            repo = ManagedSkillFiles(db)
            if request.method == "PUT":
                if sid.startswith("deployment:"):
                    raise PermissionError("Installed Skill is read-only")
                payload = await _body(request, check)
                result = repo.put_file(
                    owner,
                    sid,
                    path,
                    _file_bytes(payload),
                    expected_version=_version(payload),
                    mime=payload.get("mimeType", "text/plain"),
                    position=payload.get("order", 0),
                    replace=True,
                )
                return JSONResponse(result, headers=_HEADERS)
            if sid.startswith("deployment:"):
                name = sid.removeprefix("deployment:")
                if not runtime.config.agent_bridge_enabled:
                    raise KeyError("Skill not found")
                loaded = (
                    await runtime.agent_bridge.activate_skill(None, name)
                    if path == "SKILL.md"
                    else await runtime.agent_bridge.read_skill_file(
                        None, name, skill_path(path)
                    )
                )
                meta = {"mimeType": "text/plain"}
                data = loaded.content.encode("utf-8")
            elif path == "SKILL.md":
                record = ManagedSkills(db).get(owner, sid)
                data = _main_document(record).encode("utf-8")
                meta = {
                    "mimeType": "text/markdown",
                    "skillVersion": record["version"],
                }
            else:
                meta, data = repo.read_file(owner, sid, path)
            check()
            if request.query_params.get("raw") in {"1", "true"}:
                return Response(
                    data,
                    media_type="application/octet-stream",
                    headers=_RAW_HEADERS,
                )
            try:
                result = {**meta, "content": data.decode("utf-8")}
            except UnicodeError:
                result = {**meta, "isBinary": True}
            return JSONResponse(result, headers=_HEADERS)
        except (
            KeyError,
            PermissionError,
            ValueError,
            TypeError,
            RecursionError,
            OSError,
            binascii.Error,
        ) as error:
            return _failure(error)

    return [
        Route(_PREFIX, catalog, methods=["GET", "POST"]),
        Route(_PREFIX + "/import", import_bundle, methods=["POST"]),
        Route(_PREFIX + "/states", states, methods=["GET", "POST"]),
        Route(
            _PREFIX + "/{skill_id}", catalog, methods=["GET", "PATCH", "DELETE"]
        ),
        Route(_PREFIX + "/{skill_id}/tree", tree, methods=["GET", "POST"]),
        Route(
            _PREFIX + "/{skill_id}/tree/{node_id}",
            tree,
            methods=["PATCH", "DELETE"],
        ),
        Route(
            _PREFIX + "/{skill_id}/files/{path:path}",
            file,
            methods=["GET", "PUT"],
        ),
    ]
