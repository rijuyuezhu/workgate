"""Controller-owned channel administration and credential-safe discovery."""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...hosted_chat.credential_cipher import CredentialCipher
from ...hosted_chat.database import SQLiteDatabase
from ...hosted_chat.model_discovery import (
    CatalogUnavailable,
    discover_models,
    refresh_model_channel,
    save_discovered,
)
from ...hosted_chat.provider_profiles import ChannelConflict, ProviderRepository
from ...oauth.core.scopes import SCOPE_SHELL_WRITE
from ...ui.http.routes import UI_API_PREFIX
from ..runtime import ControlRuntime
from .hosted_chat import _HEADERS, _principal

_PREFIX = UI_API_PREFIX + "/chat/providers"


def hosted_chat_provider_routes(runtime: ControlRuntime) -> list[Route]:
    async def handle(request: Request):
        try:
            payload = await request.json() if request.method != "GET" else None
            principal = _principal(runtime, request)
            principal.check()
            database = SQLiteDatabase(runtime.config.state_dir)
            repo = ProviderRepository(database, CredentialCipher(database))
            if request.method == "GET":
                return JSONResponse(repo.public_profiles(), headers=_HEADERS)

            def check():
                principal.check()
                if (
                    principal.owner != "local-user"
                    or SCOPE_SHELL_WRITE not in principal.scopes
                ):
                    raise PermissionError("Controller administration required")

            check()
            if not isinstance(payload, dict):
                raise ValueError("Invalid provider request")
            operation = request.path_params.get("operation")
            if operation == "discover":
                result = await discover_models(repo, payload)
                check()
            elif operation == "refresh":
                if set(payload) != {"channel"} or not isinstance(
                    payload["channel"], str
                ):
                    raise ValueError("Invalid channel")
                result = await refresh_model_channel(
                    repo, payload["channel"], check=check
                )
            elif operation is None and request.method == "PUT":
                result = await save_discovered(repo, payload, check=check)
            elif operation is None and request.method == "DELETE":
                if not set(payload) <= {"name", "legacy_provider", "force"}:
                    raise ValueError("Invalid channel deletion")
                result = repo.delete(**payload)
                runtime.generation_supervisor.wake_cleanup()
            else:
                raise KeyError("Unknown provider operation")
            return JSONResponse(result, headers=_HEADERS)
        except ChannelConflict as error:
            return JSONResponse(
                {
                    "error": "channel_conflict",
                    "message": str(error),
                    "can_force": error.can_force,
                },
                status_code=409,
                headers=_HEADERS,
            )
        except CatalogUnavailable as error:
            return JSONResponse(
                {"error": "catalog_unavailable", "message": str(error)},
                status_code=502,
                headers=_HEADERS,
            )
        except PermissionError:
            return JSONResponse(
                {"error": "Provider operation denied"},
                status_code=403,
                headers=_HEADERS,
            )
        except KeyError:
            return JSONResponse(
                {"error": "Provider resource not found"},
                status_code=404,
                headers=_HEADERS,
            )
        except ValueError:
            return JSONResponse(
                {"error": "Invalid provider request"},
                status_code=400,
                headers=_HEADERS,
            )

    return [
        Route(_PREFIX, handle, methods=["GET", "PUT", "DELETE"]),
        Route(_PREFIX + "/{operation}", handle, methods=["POST"]),
    ]
