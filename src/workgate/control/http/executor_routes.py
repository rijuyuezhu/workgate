"""Authenticated executor protocol v1 HTTP routes owned by control."""

from __future__ import annotations

import json

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from ...protocol.errors import ProtocolErrorCode, ProtocolErrorResponse
from ...protocol.executor import (
    EXECUTOR_HEARTBEAT_PATH,
    EXECUTOR_HELLO_PATH,
    EXECUTOR_PAIR_POLL_PATH,
    EXECUTOR_PAIR_START_PATH,
    EXECUTOR_POLL_PATH,
    EXECUTOR_RESULT_PATH,
    ExecutorHelloRequest,
    ExecutorResult,
)
from ...protocol.pairing import PairPollRequest, PairStartRequest
from ..executor_transport import ExecutorTransport, ExecutorTransportError
from ..pairing import ExecutorPairingError, ExecutorPairingService


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


def _pairing_error_response(exc: ExecutorPairingError) -> JSONResponse:
    code = exc.error.code
    if code is ProtocolErrorCode.PAIRING_PENDING:
        status = 202
    elif code is ProtocolErrorCode.PAIRING_DENIED:
        status = 403
    elif code is ProtocolErrorCode.PAIRING_EXPIRED:
        status = 410
    elif code is ProtocolErrorCode.PAIRING_CAPACITY_EXHAUSTED:
        status = 429
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


def executor_routes(
    transport: ExecutorTransport,
    pairing: ExecutorPairingService | None = None,
) -> list[BaseRoute]:
    """Return bearer-authenticated executor routes outside owner OAuth auth."""

    async def pair_start(request: Request) -> Response:
        if pairing is None:
            return Response(status_code=404)
        message = await _json_model(request, PairStartRequest)
        if message is None:
            return JSONResponse(
                {"detail": "invalid executor pair-start request"},
                status_code=422,
            )
        try:
            response = await pairing.start_pairing(message)
        except ExecutorPairingError as exc:
            return _pairing_error_response(exc)
        return JSONResponse(response.model_dump(mode="json"))

    async def pair_poll(request: Request) -> Response:
        if pairing is None:
            return Response(status_code=404)
        message = await _json_model(request, PairPollRequest)
        if message is None:
            return JSONResponse(
                {"detail": "invalid executor pair-poll request"},
                status_code=422,
            )
        try:
            response = await pairing.poll(message.device_code)
        except ExecutorPairingError as exc:
            return _pairing_error_response(exc)
        return JSONResponse(response.model_dump(mode="json"))

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
        *(
            [
                Route(EXECUTOR_PAIR_START_PATH, pair_start, methods=["POST"]),
                Route(EXECUTOR_PAIR_POLL_PATH, pair_poll, methods=["POST"]),
            ]
            if pairing is not None
            else []
        ),
        Route(EXECUTOR_HELLO_PATH, hello, methods=["POST"]),
        Route(EXECUTOR_HEARTBEAT_PATH, heartbeat, methods=["POST"]),
        Route(EXECUTOR_POLL_PATH, poll, methods=["POST"]),
        Route(EXECUTOR_RESULT_PATH, result, methods=["POST"]),
    ]
