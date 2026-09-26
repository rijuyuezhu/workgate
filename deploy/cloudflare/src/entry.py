# pyright: reportMissingImports=false

import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from workers import DurableObject, Response, WorkerEntrypoint

from workgate.config.settings import Settings
from workgate.hosted import (
    HOSTED_HTTP_PATHS,
    DurableObjectSqlStateStore,
    HostedControlActorCore,
    HostedHttpGateway,
    HostedHttpResponse,
    build_hosted_control_actor_core,
    owner_bearer_matches,
)


class _RequestBodyTooLarge(Exception):
    pass


async def _read_bounded_text(request, limit: int) -> str:
    body = request.body
    if body is None:
        return ""
    chunks: list[bytes] = []
    total = 0
    async for chunk in body:
        to_bytes = getattr(chunk, "to_bytes", None)
        data = (
            cast(bytes, to_bytes())
            if callable(to_bytes)
            else bytes(cast(Any, chunk))
        )
        total += len(data)
        if limit > 0 and total > limit:
            raise _RequestBodyTooLarge
        chunks.append(data)
    return b"".join(chunks).decode("utf-8")


def _response(value: HostedHttpResponse) -> Response:
    headers = {
        "content-type": value.content_type,
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
    }
    if value.content_type.startswith("text/html"):
        headers.update(
            {
                "content-security-policy": (
                    "default-src 'none'; "
                    "script-src 'unsafe-inline'; "
                    "style-src 'unsafe-inline'; "
                    "connect-src 'self'; "
                    "form-action 'none'; "
                    "frame-ancestors 'none'; "
                    "base-uri 'none'"
                ),
                "referrer-policy": "no-referrer",
            }
        )
    if value.body is None:
        body = None
    elif value.content_type.startswith("application/json"):
        body = json.dumps(value.body, ensure_ascii=False, allow_nan=False)
    else:
        body = str(value.body)
    return Response(body, status=value.status, headers=headers)


class WorkgateControl(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self._env = env
        self._sql = DurableObjectSqlStateStore(ctx.storage.sql)
        self._actor: HostedControlActorCore | None = None
        self._gateway: HostedHttpGateway | None = None

    def _ensure_gateway(self, request):
        if self._gateway is not None:
            return self._gateway
        parsed = urlsplit(str(request.url))
        inferred_base_url = f"{parsed.scheme}://{parsed.netloc}"
        configured_base_url = getattr(self._env, "WORKGATE_BASE_URL", None)
        base_url = str(configured_base_url or inferred_base_url).rstrip("/")
        settings = Settings(
            workspace_root=Path("/workspace"),
            state_dir=Path("/state"),
            data_dir=Path("/data"),
            base_url=base_url,
            auth_mode="none",
            ui_enabled=False,
        )
        actor = build_hosted_control_actor_core(settings, state_store=self._sql)
        actor.start()
        owner_token = str(self._env.WORKGATE_OWNER_TOKEN)
        self._actor = actor
        self._gateway = HostedHttpGateway(actor, owner_token=owner_token)
        return self._gateway

    async def fetch(self, request):
        gateway = self._ensure_gateway(request)
        actor = self._actor
        assert actor is not None
        method = str(request.method).upper()
        path = urlsplit(str(request.url)).path
        payload = None
        if method in {"POST", "PUT", "PATCH"}:
            limit = actor.config.max_http_request_bytes
            content_length = request.headers.get("content-length")
            if limit > 0 and content_length is not None:
                try:
                    declared_bytes = int(content_length)
                except ValueError:
                    return Response(
                        "invalid content-length",
                        status=400,
                        headers={"content-type": "text/plain; charset=utf-8"},
                    )
                if declared_bytes < 0:
                    return Response(
                        "invalid content-length",
                        status=400,
                        headers={"content-type": "text/plain; charset=utf-8"},
                    )
                if declared_bytes > limit:
                    return Response(
                        "request body too large",
                        status=413,
                        headers={"content-type": "text/plain; charset=utf-8"},
                    )
            try:
                text = await _read_bounded_text(request, limit)
            except _RequestBodyTooLarge:
                return Response(
                    "request body too large",
                    status=413,
                    headers={"content-type": "text/plain; charset=utf-8"},
                )
            except UnicodeDecodeError:
                return Response(
                    "request body must be UTF-8",
                    status=400,
                    headers={"content-type": "text/plain; charset=utf-8"},
                )
            if text:
                try:
                    payload = json.loads(text)
                except ValueError:
                    return Response(
                        '{"detail":"invalid JSON"}',
                        status=400,
                        headers={"content-type": "application/json"},
                    )
        result = await gateway.dispatch(
            method=method,
            path=path,
            headers=request.headers,
            payload=payload,
        )
        return _response(result)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        parsed = urlsplit(str(request.url))
        method = str(request.method).upper()
        path = parsed.path.rstrip("/") or "/"
        if method == "GET" and path == "/healthz":
            return Response(
                '{"ok":true}',
                headers={
                    "content-type": "application/json",
                    "cache-control": "no-store",
                },
            )
        if path not in HOSTED_HTTP_PATHS:
            return Response(
                '{"detail":"not found"}',
                status=404,
                headers={
                    "content-type": "application/json",
                    "cache-control": "no-store",
                    "x-content-type-options": "nosniff",
                },
            )
        owner_route = (
            path == "/mcp"
            or (path == "/status" and method == "GET")
            or (path == "/pair" and method == "POST")
            or (path == "/pair/lookup" and method == "POST")
        )
        if owner_route and not owner_bearer_matches(
            request.headers,
            str(self.env.WORKGATE_OWNER_TOKEN),
        ):
            return Response(
                '{"detail":"owner bearer required"}',
                status=401,
                headers={
                    "content-type": "application/json",
                    "cache-control": "no-store",
                },
            )
        stub = self.env.WORKGATE_CONTROL.getByName("personal")
        return await stub.fetch(request)
