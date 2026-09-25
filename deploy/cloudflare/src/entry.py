# pyright: reportMissingImports=false

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from workers import DurableObject, Response, WorkerEntrypoint

from workgate.config.settings import Settings
from workgate.hosted import (
    DurableObjectSqlStateStore,
    HostedControlActorCore,
    HostedHttpGateway,
    HostedHttpResponse,
    build_hosted_control_actor_core,
)


def _response(value: HostedHttpResponse) -> Response:
    headers = {"content-type": value.content_type}
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
            text = await request.text()
            if len(text.encode("utf-8")) > actor.config.max_http_request_bytes:
                return Response(
                    "request body too large",
                    status=413,
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
        if str(request.method).upper() == "GET" and parsed.path == "/healthz":
            return Response(
                '{"ok":true}',
                headers={"content-type": "application/json"},
            )
        stub = self.env.WORKGATE_CONTROL.getByName("personal")
        return await stub.fetch(request)
