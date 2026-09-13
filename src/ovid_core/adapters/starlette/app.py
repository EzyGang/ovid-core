import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from functools import partial
from http import HTTPStatus
from typing import Any

from pydantic import BaseModel as PydanticModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from ovid_core.adapters.starlette.http import _apply_cors, _error_response, read_json_body, request_context
from ovid_core.persistence import ConversationStore
from ovid_core.runtime.events import AgentEvent
from ovid_core.runtime.identifiers import RunId
from ovid_core.server.active_runs import _ActiveRun
from ovid_core.server.contracts import (
    AgentRegistration,
    AuthorizationCallback,
    LifecycleCallback,
    ReadinessCallback,
    RequestContext,
)
from ovid_core.server.models import (
    AgentRunRequest,
    AgentRunResponse,
    HealthResponse,
    RunResultSSEEvent,
    ServerConfig,
    ServerErrorResponse,
    ServerErrorSSEEvent,
)
from ovid_core.server.runtime import _AgentServerRuntime, _server_lifespan


def create_starlette_app(
    *,
    agents: Sequence[AgentRegistration[Any, Any]],
    authorize: AuthorizationCallback,
    config: ServerConfig,
    store: ConversationStore | None,
    readiness: ReadinessCallback | None,
    startup: LifecycleCallback | None,
    shutdown: LifecycleCallback | None,
) -> Starlette:
    runtime = _AgentServerRuntime(agents=agents, authorize=authorize, config=config, store=store)
    app = Starlette(
        routes=[
            Route('/health', _health, methods=['GET']),
            Route('/ready', partial(_ready, readiness=readiness), methods=['GET']),
            Route('/agents/{agent_id:str}/events', partial(_stream, runtime=runtime, config=config), methods=['POST']),
            Route('/agents/{agent_id:str}/runs/{run_id:str}', partial(_cancel, runtime=runtime), methods=['DELETE']),
        ],
        lifespan=partial(
            _lifespan,
            runtime=runtime,
            startup=startup,
            shutdown=shutdown,
            shutdown_grace_seconds=config.shutdown_grace_seconds,
        ),
    )
    _apply_cors(app, config.allowed_origins)

    return app


async def _health(_: Request) -> Response:
    return _model_response(HealthResponse(status='ok'))


async def _ready(_: Request, *, readiness: ReadinessCallback | None) -> Response:
    is_ready = await readiness() if readiness is not None else True
    status = HTTPStatus.OK if is_ready else HTTPStatus.SERVICE_UNAVAILABLE
    payload = HealthResponse(status='ok' if is_ready else 'not_ready')

    return _model_response(payload, status=status)


async def _cancel(request: Request, *, runtime: _AgentServerRuntime) -> Response:
    try:
        await runtime.cancel(
            request.path_params['agent_id'],
            RunId.model_validate(request.path_params['run_id']),
            request_context(request),
        )
    except Exception as error:
        return _error_response(error)
    return Response(status_code=HTTPStatus.NO_CONTENT)


async def _stream(request: Request, *, runtime: _AgentServerRuntime, config: ServerConfig) -> Response:
    try:
        payload = await _request_payload(request, config)
    except Exception as error:
        return _error_response(error)

    events = _event_stream(
        runtime=runtime,
        agent_id=request.path_params['agent_id'],
        payload=payload,
        context=request_context(request),
    )

    return StreamingResponse(
        events,
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@asynccontextmanager
async def _lifespan(
    _: Starlette,
    *,
    runtime: _AgentServerRuntime,
    startup: LifecycleCallback | None,
    shutdown: LifecycleCallback | None,
    shutdown_grace_seconds: int,
) -> AsyncIterator[None]:
    async with _server_lifespan(
        startup=startup,
        shutdown=shutdown,
        shutdown_grace_seconds=shutdown_grace_seconds,
        close=runtime.close,
    ):
        yield


async def _request_payload(request: Request, config: ServerConfig) -> AgentRunRequest:
    body = await read_json_body(
        request,
        max_body_bytes=config.max_body_bytes,
        timeout_seconds=config.request_timeout_seconds,
    )

    return AgentRunRequest.model_validate_json(body)


async def _event_stream(
    *,
    runtime: _AgentServerRuntime,
    agent_id: str,
    payload: AgentRunRequest,
    context: RequestContext,
) -> AsyncIterator[str]:
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1)
    disconnected = asyncio.Event()

    async def produce(operation: _ActiveRun) -> None:
        try:
            await runtime.events(
                agent_id,
                payload,
                context,
                send=partial(_run_frame, queue, disconnected),
                operation=operation,
            )
        finally:
            if not disconnected.is_set():
                await queue.put(None)

    async with asyncio.TaskGroup() as tasks:
        operation = runtime.start(agent_id, None, tasks, produce)
        try:
            while (frame := await queue.get()) is not None:
                yield frame
        finally:
            disconnected.set()
            await runtime.disconnect(operation)


async def _run_frame(
    queue: asyncio.Queue[str | None],
    disconnected: asyncio.Event,
    event: AgentEvent | AgentRunResponse | ServerErrorResponse,
) -> None:
    if disconnected.is_set():
        return
    if isinstance(event, AgentRunResponse):
        payload = RunResultSSEEvent.model_validate(event, from_attributes=True)
    elif isinstance(event, ServerErrorResponse):
        payload = ServerErrorSSEEvent.model_validate(event, from_attributes=True)
    else:
        payload = event
    await queue.put(_encode_sse(payload.kind, payload))


def _encode_sse(kind: str, payload: PydanticModel) -> str:
    return f'event: {kind}\ndata: {payload.model_dump_json()}\n\n'


def _model_response(payload: PydanticModel, *, status: HTTPStatus = HTTPStatus.OK) -> Response:
    return Response(content=payload.model_dump_json(), status_code=status, media_type='application/json')
