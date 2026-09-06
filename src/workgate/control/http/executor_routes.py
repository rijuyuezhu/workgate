"""Authenticated executor protocol v1 HTTP routes owned by control."""

from __future__ import annotations

import json

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from ...protocol.errors import ProtocolErrorCode, ProtocolErrorResponse
from ...protocol.executor import ExecutorHelloRequest, ExecutorResult
from ..executor_transport import ExecutorTransport, ExecutorTransportError


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return ""
    return value


def _error_response(exc: ExecutorTransportError) -> JSONResponse:
    code = exc.error.code
    if code is ProtocolErrorCode.UNAUTHORIZED_EXECUTOR:
        status = 401
    elif code is ProtocolErrorCode.EXECUTOR_REVOKED:
        status = 403
    elif code is ProtocolErrorCode.UNKNOWN_COMMAND:
        status = 404
    elif code is ProtocolErrorCode.EXECUTOR_OVERLOADED:
        status = 409
    else:
        status = 400
    payload = ProtocolErrorResponse(error=exc.error)
    return JSONResponse(payload.model_dump(mode="json"), status_code=status)


async def _json_model(request: Request, model_type):
    try:
        payload = await request.json()
        return model_type.model_validate(payload)
    except json.JSONDecodeError, ValidationError, TypeError, ValueError:
        return None


def executor_routes(transport: ExecutorTransport) -> list[BaseRoute]:
    """Return bearer-authenticated executor routes outside owner OAuth auth."""

    async def hello(request: Request) -> Response:
        message = await _json_model(request, ExecutorHelloRequest)
        if message is None:
            return JSONResponse(
                {"detail": "invalid executor hello request"}, status_code=422
            )
        try:
            response = await transport.hello(_bearer(request), message)
        except ExecutorTransportError as exc:
            return _error_response(exc)
        return JSONResponse(response.model_dump(mode="json"))

    async def heartbeat(request: Request) -> Response:
        try:
            await transport.heartbeat(_bearer(request))
        except ExecutorTransportError as exc:
            return _error_response(exc)
        return Response(status_code=204)

    async def poll(request: Request) -> Response:
        try:
            command = await transport.poll(_bearer(request))
        except ExecutorTransportError as exc:
            return _error_response(exc)
        if command is None:
            return Response(status_code=204)
        return JSONResponse(command.model_dump(mode="json"))

    async def result(request: Request) -> Response:
        message = await _json_model(request, ExecutorResult)
        if message is None:
            return JSONResponse(
                {"detail": "invalid executor result request"}, status_code=422
            )
        try:
            await transport.submit_result(_bearer(request), message)
        except ExecutorTransportError as exc:
            return _error_response(exc)
        return Response(status_code=204)

    return [
        Route("/executor/v1/hello", hello, methods=["POST"]),
        Route("/executor/v1/heartbeat", heartbeat, methods=["POST"]),
        Route("/executor/v1/poll", poll, methods=["POST"]),
        Route("/executor/v1/result", result, methods=["POST"]),
    ]
