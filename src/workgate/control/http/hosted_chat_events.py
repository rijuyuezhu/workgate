"""External event bearer ingress and native owner-only source administration."""

import json

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...hosted_chat.database import SQLiteDatabase
from ...hosted_chat.event_bindings import EventBindings
from ...hosted_chat.event_delivery_queue import EventDeliveryQueue, _iso
from ...hosted_chat.event_envelope import _TOKEN, EventConflict, _string
from ...hosted_chat.event_source_keys import EventSourceKeys
from ...hosted_chat.machine_permissions import require_machine_scope
from ...ui.http.routes import UI_API_PREFIX
from ..runtime import ControlRuntime
from .hosted_chat import _HEADERS, _principal

_ADMIN = UI_API_PREFIX + "/chat/event-source-keys"
_INGRESS = "/api/agents/v1/events"
_MAX_BODY = 1024 * 1024


class EventBodyTooLarge(ValueError):
    pass


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("Invalid JSON number")


async def _body(request: Request) -> dict:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise ValueError("JSON required")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > _MAX_BODY:
            raise EventBodyTooLarge()
        data.extend(chunk)
    value = json.loads(
        data.decode("utf-8"),
        object_pairs_hook=_unique_pairs,
        parse_constant=_invalid_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value


def _idempotency(request: Request) -> str:
    values = request.headers.getlist("idempotency-key")
    if len(values) != 1 or not _TOKEN.fullmatch(values[0].strip()):
        raise ValueError("Exactly one valid Idempotency-Key is required")
    return values[0].strip()


def _error(status: int, message: str):
    return JSONResponse(
        {"error": message}, status_code=status, headers=_HEADERS
    )


def hosted_chat_event_routes(
    runtime: ControlRuntime,
) -> tuple[list[Route], list[Route]]:
    async def admin(request: Request):
        try:
            principal = _principal(runtime, request)

            def check():
                principal.check()
                require_machine_scope(
                    set(principal.scopes),
                    "read" if request.method == "GET" else "write",
                    expires=principal.expires,
                )

            check()
            repository = EventSourceKeys(
                SQLiteDatabase(runtime.config.state_dir)
            )
            if request.method == "GET":
                return JSONResponse(
                    {"sources": repository.list(principal.owner)},
                    headers=_HEADERS,
                )
            if request.method == "DELETE":
                if not repository.revoke(
                    principal.owner, request.path_params["key_id"]
                ):
                    return _error(404, "Event source not found")
                runtime.generation_supervisor.wake_cleanup()
                return JSONResponse({"revoked": True}, headers=_HEADERS)
            payload = await _body(request)
            if set(payload) - {"label", "permissions", "expiresAt"}:
                raise ValueError("Invalid source fields")
            check()
            result = repository.mint(
                principal.owner,
                label=payload.get("label", "event-source"),
                permissions=payload.get("permissions", ["fire", "continue"]),
                expires_at=payload.get("expiresAt"),
            )
            return JSONResponse(result, status_code=201, headers=_HEADERS)
        except EventBodyTooLarge:
            return _error(413, "Event request too large")
        except PermissionError:
            return _error(403, "Event source administration denied")
        except ValueError, TypeError, RecursionError:
            return _error(400, "Invalid event source request")

    async def ingress(request: Request, *, binding: bool = False):
        # These exact routes bypass only the OAuth middleware; a source key
        # is mandatory even on auth-mode none and localhost.
        authorization = request.headers.getlist("authorization")
        if len(authorization) != 1:
            return _error(401, "Event source bearer required")
        scheme, _, token = authorization[0].partition(" ")
        if scheme.lower() != "bearer" or not token:
            return _error(401, "Event source bearer required")
        database = SQLiteDatabase(runtime.config.state_dir)
        repository = EventSourceKeys(database)
        source = repository.authenticate(token)
        if source is None:
            return _error(401, "Invalid event source bearer")
        owner, source_id = source["owner"], source["id"]
        try:
            queue = EventDeliveryQueue(database)
            if request.method == "GET":
                result = queue.get(
                    owner, source_id, request.path_params["delivery_id"]
                )
                if result is None:
                    return _error(404, "Event delivery not found")
                return JSONResponse(result, headers=_HEADERS)
            identity = _idempotency(request)
            payload = await _body(request)
            # Recheck after reading a potentially slow request; repositories
            # revalidate source permission again in their write transaction.
            if repository.authenticate(token) != source:
                return _error(401, "Invalid event source bearer")
            if binding:
                target = payload.get("target")
                if not isinstance(target, dict):
                    raise ValueError("Target required")
                result = EventBindings(database).register(
                    owner,
                    source_id,
                    identity,
                    actor_id=_string(payload.get("actorId"), "actorId", 256),
                    parent_conversation_id=_string(
                        payload.get("parentConversationId"),
                        "parentConversationId",
                        256,
                    ),
                    parent_message_id=_string(
                        payload.get("parentMessageId"), "parentMessageId", 256
                    ),
                    target_agent_id=_string(
                        target.get("agentId"), "agentId", 256
                    ),
                )
                return JSONResponse(result, status_code=201, headers=_HEADERS)
            receipt = queue.enqueue(owner, source_id, identity, payload)
            runtime.generation_supervisor.wake_cleanup()
            return JSONResponse(
                {**receipt, "availableAt": _iso(receipt["availableAt"])},
                status_code=202,
                headers={
                    **_HEADERS,
                    "Location": _INGRESS + "/" + receipt["id"],
                },
            )
        except EventBodyTooLarge:
            return _error(413, "Event request too large")
        except EventConflict:
            return _error(409, "Event identity conflict or capacity reached")
        except PermissionError:
            return _error(403, "Event source or target authorization denied")
        except KeyError:
            return _error(404, "Event resource not found")
        except ValueError, TypeError, RecursionError:
            return _error(400, "Invalid event request")

    async def bind(request: Request):
        return await ingress(request, binding=True)

    return [
        Route(_ADMIN, admin, methods=["GET", "POST"]),
        Route(_ADMIN + "/{key_id}", admin, methods=["DELETE"]),
    ], [
        Route(_INGRESS + "/bindings", bind, methods=["POST"]),
        Route(_INGRESS, ingress, methods=["POST"]),
        Route(_INGRESS + "/{delivery_id}", ingress, methods=["GET"]),
    ]
