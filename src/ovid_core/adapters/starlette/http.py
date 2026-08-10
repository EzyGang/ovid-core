import asyncio
from collections.abc import Sequence
from http import HTTPStatus
from uuid import uuid4

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response

from ovid_core.server.contracts import RequestContext
from ovid_core.server.models import ServerErrorResponse
from ovid_core.server.runtime import _server_error_from_exception


_SERVER_ERROR_STATUS = {
    'agent_not_found': HTTPStatus.NOT_FOUND,
    'forbidden': HTTPStatus.FORBIDDEN,
    'timeout': HTTPStatus.GATEWAY_TIMEOUT,
    'agent_run_failed': HTTPStatus.BAD_GATEWAY,
}


class BodyTooLargeError(Exception):
    pass


class ClientAuthorityError(Exception):
    pass


class InvalidRequestError(Exception):
    pass


class UnsupportedMediaTypeError(Exception):
    pass


async def read_json_body(request: Request, *, max_body_bytes: int, timeout_seconds: float) -> bytes:
    async with asyncio.timeout(timeout_seconds):
        return await _read_json_body(request, max_body_bytes)


async def _read_json_body(request: Request, max_body_bytes: int) -> bytes:
    if request.headers.get('content-type', '').partition(';')[0].strip().casefold() != 'application/json':
        raise UnsupportedMediaTypeError

    content_length = request.headers.get('content-length')
    if content_length is not None and _content_length(content_length) > max_body_bytes:
        raise BodyTooLargeError

    chunks: list[bytes] = []
    size = 0

    async for chunk in request.stream():
        size += len(chunk)
        if size > max_body_bytes:
            raise BodyTooLargeError

        chunks.append(chunk)

    return b''.join(chunks)


def _apply_cors(app: Starlette, allowed_origins: Sequence[str]) -> None:
    if not allowed_origins:
        return

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=('GET', 'POST'),
        allow_headers=('Authorization', 'Content-Type', 'Traceparent'),
    )


def request_context(request: Request) -> RequestContext:
    return RequestContext(
        method=request.method,
        path=request.url.path,
        headers=request.headers.items(),
        client_host=request.client.host if request.client is not None else None,
        request_id=str(uuid4()),
    )


def _content_length(value: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise InvalidRequestError from error


def _error_response(error: Exception) -> Response:
    payload, status = _error_payload(error)

    return Response(
        content=payload.model_dump_json(),
        status_code=status,
        media_type='application/json',
    )


def _error_payload(error: Exception) -> tuple[ServerErrorResponse, HTTPStatus]:
    if isinstance(error, BodyTooLargeError):
        return ServerErrorResponse(
            code='body_too_large',
            message='Request body exceeds the configured limit',
        ), HTTPStatus.REQUEST_ENTITY_TOO_LARGE

    if isinstance(error, UnsupportedMediaTypeError):
        return ServerErrorResponse(
            code='unsupported_media_type',
            message='Content-Type must be application/json',
        ), HTTPStatus.UNSUPPORTED_MEDIA_TYPE

    if isinstance(error, (ValidationError, InvalidRequestError)):
        return ServerErrorResponse(
            code='invalid_request',
            message='Request body is invalid',
        ), HTTPStatus.UNPROCESSABLE_ENTITY

    if isinstance(error, ClientAuthorityError):
        return ServerErrorResponse(
            code='client_authority_denied',
            message='Client-controlled run state is not accepted',
        ), HTTPStatus.UNPROCESSABLE_ENTITY

    payload = _server_error_from_exception(error)
    status = _SERVER_ERROR_STATUS.get(payload.code, HTTPStatus.INTERNAL_SERVER_ERROR)

    return payload, status
