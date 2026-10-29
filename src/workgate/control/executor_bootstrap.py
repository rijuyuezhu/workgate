"""Fail-closed compatibility endpoints for retired frozen Executor downloads."""

from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import BaseRoute, Route

_BOOTSTRAP_PATH = "/executor/v1/bootstrap"
_GUIDE = "https://github.com/rijuyuezhu/workgate/blob/main/docs/getting-started/install-options.md"


def executor_bootstrap_routes() -> list[BaseRoute]:
    """Never substitute an unpinned source command for a frozen binary URL."""

    async def retired(_request: Request) -> PlainTextResponse:
        return PlainTextResponse(
            "Frozen Executor bootstrap and binary updates are retired. "
            f"Use the version-pinned source + uv installer: {_GUIDE}\n"
            "Existing frozen services require an explicit identity-preserving "
            "migration before upgrading Control.\n",
            status_code=410,
            headers={"Cache-Control": "no-store"},
        )

    root = _BOOTSTRAP_PATH + "/{target:str}"
    return [
        Route(_BOOTSTRAP_PATH, retired, methods=["GET"]),
        Route(_BOOTSTRAP_PATH + ".ps1", retired, methods=["GET"]),
        Route(root + "/sha256", retired, methods=["GET"]),
        Route(root + "/archive", retired, methods=["GET"]),
    ]
