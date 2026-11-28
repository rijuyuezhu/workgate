"""Owner-authenticated schedule configuration and explicit run-now admission."""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...hosted_chat.database import SQLiteDatabase
from ...hosted_chat.machine_permissions import require_machine_scope
from ...hosted_chat.schedule_occurrences import ScheduleOccurrences
from ...hosted_chat.schedules import (
    SCHEDULE_LIMITS,
    ScheduleConflict,
    ScheduleRepository,
)
from ...ui.http.routes import UI_API_PREFIX
from ..runtime import ControlRuntime
from .hosted_chat import _HEADERS, _principal

_PREFIX = UI_API_PREFIX + "/chat/schedules"


def hosted_chat_schedule_routes(runtime: ControlRuntime) -> list[Route]:
    async def handle(request: Request):
        try:
            principal = _principal(runtime, request)
            principal.check()
            require_machine_scope(
                set(principal.scopes),
                "read" if request.method == "GET" else "write",
                expires=principal.expires,
            )
            owner = principal.owner
            database = SQLiteDatabase(runtime.config.state_dir)
            repository = ScheduleRepository(database)
            sid = request.path_params.get("schedule_id")
            if request.method == "GET":
                result = (
                    repository.get(owner, sid)
                    if sid is not None
                    else {
                        "schedules": repository.list(owner),
                        "limits": SCHEDULE_LIMITS,
                    }
                )
                return JSONResponse(result, headers=_HEADERS)
            if request.method == "POST" and sid is not None:
                principal.check()
                items = ScheduleOccurrences(database).claim_due(
                    owner=owner, schedule_id=sid, manual=True
                )
                if not items:
                    raise ScheduleConflict(
                        "Schedule already running, recently started or unavailable"
                    )
                item = items[0]
                runtime.generation_supervisor.wake_cleanup()
                return JSONResponse(
                    {
                        "scheduleId": sid,
                        "conversationId": item.conversation_id,
                        "runId": item.run_id,
                        "status": "claimed",
                    },
                    status_code=202,
                    headers=_HEADERS,
                )
            if request.method == "DELETE":
                assert sid is not None
                principal.check()
                repository.delete(owner, sid)
                return JSONResponse({"id": sid}, headers=_HEADERS)
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid schedule body")
            principal.check()
            if sid is None:
                result = repository.create(owner, payload)
                code = 201
            else:
                result = repository.update(owner, sid, payload)
                code = 200
            runtime.generation_supervisor.wake_cleanup()
            return JSONResponse(result, status_code=code, headers=_HEADERS)
        except ScheduleConflict as error:
            return JSONResponse(
                {"error": str(error)}, status_code=409, headers=_HEADERS
            )
        except KeyError:
            return JSONResponse(
                {"error": "Schedule or resource not found"},
                status_code=404,
                headers=_HEADERS,
            )
        except PermissionError:
            return JSONResponse(
                {"error": "Schedule authorization denied"},
                status_code=403,
                headers=_HEADERS,
            )
        except ValueError, TypeError:
            return JSONResponse(
                {"error": "Invalid schedule request"},
                status_code=400,
                headers=_HEADERS,
            )

    return [
        Route(_PREFIX, handle, methods=["GET", "POST"]),
        Route(_PREFIX + "/{schedule_id}/run", handle, methods=["POST"]),
        Route(
            _PREFIX + "/{schedule_id}",
            handle,
            methods=["GET", "PATCH", "DELETE"],
        ),
    ]
