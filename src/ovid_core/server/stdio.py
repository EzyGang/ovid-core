import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from pydantic import JsonValue, TypeAdapter, ValidationError

from ovid_core.persistence import ConversationStore
from ovid_core.server.contracts import (
    AgentRegistration,
    AuthorizationCallback,
    CommandRegistration,
    LifecycleCallback,
    RequestContext,
)
from ovid_core.server.models import ServerConfig, ServerErrorResponse
from ovid_core.server.runtime import (
    _AgentServerRuntime,
    _AuthorizationDeniedError,
    _CommandExecutionError,
    _response_from_result,
    _server_error_from_exception,
    _server_lifespan,
    _UnknownCommandError,
)
from ovid_core.server.stdio_models import (
    StdioCommandRequest,
    StdioCommandResultResponse,
    StdioDescriptor,
    StdioErrorResponse,
    StdioEventResponse,
    StdioInitializedResponse,
    StdioInitializeRequest,
    StdioRequest,
    StdioResponse,
    StdioRunRequest,
    StdioRunResultResponse,
)


type _StdioRead = Callable[[int], Awaitable[bytes]]
type _StdioWrite = Callable[[bytes], Awaitable[None]]


_REQUEST_ADAPTER = TypeAdapter(StdioRequest)
_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)


class StdioAgentServer:
    def __init__(
        self,
        *,
        agents: Sequence[AgentRegistration[Any, Any]],
        commands: Sequence[CommandRegistration],
        authorize: AuthorizationCallback,
        config: ServerConfig,
        store: ConversationStore | None,
        startup: LifecycleCallback | None,
        shutdown: LifecycleCallback | None,
    ) -> None:
        self._agents = tuple(agents)
        self._commands = _command_map(commands)
        self._authorize = authorize
        self._config = config
        self._runtime = _AgentServerRuntime(agents=agents, authorize=authorize, config=config, store=store)
        self._startup = startup
        self._shutdown = shutdown

    async def run(self) -> None:
        await self._serve(reader=_read_stdin, writer=_write_stdout)

    async def _serve(self, *, reader: _StdioRead, writer: _StdioWrite) -> None:
        async with _server_lifespan(
            startup=self._startup,
            shutdown=self._shutdown,
            shutdown_grace_seconds=self._config.shutdown_grace_seconds,
        ):
            while line := await reader(self._config.max_body_bytes + 1):
                if len(line) > self._config.max_body_bytes:
                    await _write_response(writer, _error_response(None, 'request_too_large', 'Request exceeds limit'))
                    return

                await self._dispatch(line, writer)

    async def _dispatch(self, line: bytes, writer: _StdioWrite) -> None:
        try:
            request = _REQUEST_ADAPTER.validate_json(line)
        except ValidationError:
            await _write_response(writer, _error_response(None, 'invalid_request', 'Request is invalid'))
            return

        try:
            if isinstance(request, StdioInitializeRequest):
                await self._initialize(request, writer)
            elif isinstance(request, StdioRunRequest):
                await self._run(request, writer)
            else:
                await self._command(request, writer)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await _write_response(writer, _error_from_exception(request.request_id, error))

    async def _initialize(self, request: StdioInitializeRequest, writer: _StdioWrite) -> None:
        response = StdioInitializedResponse(
            request_id=request.request_id,
            agents=tuple(StdioDescriptor(id=agent.id, description=agent.description) for agent in self._agents),
            commands=tuple(
                StdioDescriptor(id=command.id, description=command.description) for command in self._commands.values()
            ),
        )
        await _write_response(writer, response)

    async def _run(self, request: StdioRunRequest, writer: _StdioWrite) -> None:
        context = _request_context(request.request_id, f'/agents/{request.agent_id}')

        async with self._runtime.stream(request.agent_id, request.request, context) as stream:
            async for event in stream:
                await _write_response(writer, StdioEventResponse(request_id=request.request_id, event=event))

            result = _response_from_result(stream.result)

        await _write_response(writer, StdioRunResultResponse(request_id=request.request_id, result=result))

    async def _command(self, request: StdioCommandRequest, writer: _StdioWrite) -> None:
        try:
            command = self._commands[request.command_id]
        except KeyError as error:
            raise _UnknownCommandError from error

        context = _request_context(request.request_id, f'/commands/{request.command_id}')
        authorization = await self._authorize(context, f'command:{request.command_id}')

        if not authorization.allowed:
            raise _AuthorizationDeniedError

        try:
            async with asyncio.timeout(self._config.request_timeout_seconds):
                value = await command.handler(context, authorization, request.arguments)
                result = _JSON_VALUE_ADAPTER.validate_python(value)
        except asyncio.CancelledError, TimeoutError:
            raise
        except Exception as error:
            raise _CommandExecutionError from error

        await _write_response(writer, StdioCommandResultResponse(request_id=request.request_id, result=result))


def create_stdio_server(
    *,
    agents: Sequence[AgentRegistration[Any, Any]],
    authorize: AuthorizationCallback,
    commands: Sequence[CommandRegistration] = (),
    config: ServerConfig = ServerConfig(),
    store: ConversationStore | None = None,
    startup: LifecycleCallback | None = None,
    shutdown: LifecycleCallback | None = None,
) -> StdioAgentServer:
    return StdioAgentServer(
        agents=agents,
        commands=commands,
        authorize=authorize,
        config=config,
        store=store,
        startup=startup,
        shutdown=shutdown,
    )


def _command_map(commands: Sequence[CommandRegistration]) -> dict[str, CommandRegistration]:
    mapped = {command.id: command for command in commands}

    if len(mapped) != len(commands):
        raise ValueError('command registration ids must be unique')

    return mapped


def _request_context(request_id: str, path: str) -> RequestContext:
    return RequestContext(method='STDIO', path=path, request_id=request_id)


async def _write_response(writer: _StdioWrite, response: StdioResponse) -> None:
    await writer(f'{response.model_dump_json()}\n'.encode())


def _error_from_exception(request_id: str, error: Exception) -> StdioErrorResponse:
    return StdioErrorResponse(request_id=request_id, error=_server_error_from_exception(error))


def _error_response(request_id: str | None, code: str, message: str) -> StdioErrorResponse:
    return StdioErrorResponse(request_id=request_id, error=ServerErrorResponse(code=code, message=message))


async def _read_stdin(limit: int) -> bytes:
    return await asyncio.to_thread(sys.stdin.buffer.readline, limit)


async def _write_stdout(payload: bytes) -> None:
    await asyncio.to_thread(_write_stdout_sync, payload)


def _write_stdout_sync(payload: bytes) -> None:
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
