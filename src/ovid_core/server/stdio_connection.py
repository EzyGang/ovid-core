import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from functools import partial
from typing import Any, cast

from pydantic import JsonValue, TypeAdapter, ValidationError

from ovid_core.errors import CredentialError
from ovid_core.runtime.events import AgentEvent
from ovid_core.runtime.identifiers import RunId
from ovid_core.server.active_runs import _ActiveRun, _ConnectionOwner
from ovid_core.server.contracts import AgentRegistration, AuthorizationCallback, CommandRegistration, RequestContext
from ovid_core.server.errors import (
    _AuthorizationDeniedError,
    _CommandExecutionError,
    _server_error_from_exception,
    _UnknownCommandError,
)
from ovid_core.server.models import AgentRunResponse, ServerConfig, ServerErrorResponse
from ovid_core.server.runtime import _AgentServerRuntime
from ovid_core.server.stdio_models import (
    StdioCancelRequest,
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


type _StdioWrite = Callable[[bytes], Awaitable[None]]


_REQUEST_ADAPTER = TypeAdapter(StdioRequest)
_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)


class _StdioConnection:
    def __init__(
        self,
        *,
        agents: Sequence[AgentRegistration[Any, Any]],
        commands: Mapping[str, CommandRegistration],
        authorize: AuthorizationCallback,
        config: ServerConfig,
        runtime: _AgentServerRuntime,
        writer: _StdioWrite,
        tasks: asyncio.TaskGroup,
    ) -> None:
        self._agents = agents
        self._commands = commands
        self._authorize = authorize
        self._config = config
        self._runtime = runtime
        self._writer = writer
        self._tasks = tasks
        self._write_lock = asyncio.Lock()
        self._owner = _ConnectionOwner()
        self._operations: dict[str, RunId] = {}
        self._pending: dict[asyncio.Future[None], None] = {}

    async def receive(self, line: bytes) -> None:
        try:
            request = _REQUEST_ADAPTER.validate_json(line)
        except ValidationError:
            await self.write_error(None, 'invalid_request', 'Request is invalid')
            return

        if isinstance(request, StdioCancelRequest):
            await self._cancel(request)
        elif isinstance(request, StdioInitializeRequest):
            await self._dispatch(request)
        else:
            await self._schedule(request)

    async def _cancel(self, request: StdioCancelRequest) -> None:
        run_id = self._operations.get(request.target_request_id)
        if run_id is None:
            return
        try:
            await self._runtime.cancel_owned(run_id, self._owner)
        except Exception as error:
            await self._write_response(
                StdioErrorResponse(request_id=request.request_id, error=_server_error_from_exception(error))
            )

    async def _schedule(self, request: StdioRunRequest | StdioCommandRequest) -> None:
        if isinstance(request, StdioRunRequest) and request.request_id in self._operations:
            await self.write_error(request.request_id, 'invalid_request', 'Run request id is already active')
            return

        previous = tuple(self._pending)
        done = asyncio.get_running_loop().create_future()
        self._pending[done] = None
        execute = partial(self._dispatch_after, request, previous, done)
        try:
            if isinstance(request, StdioRunRequest):
                operation = self._runtime.start(request.agent_id, self._owner, self._tasks, execute)
                if not done.done():
                    self._operations[request.request_id] = operation.run_id
            else:
                self._tasks.create_task(execute(None), eager_start=True)
        except Exception as error:
            self._pending.pop(done)
            done.set_result(None)
            await self._write_response(
                StdioErrorResponse(request_id=request.request_id, error=_server_error_from_exception(error))
            )

    async def _dispatch_after(
        self,
        request: StdioRunRequest | StdioCommandRequest,
        previous: tuple[asyncio.Future[None], ...],
        done: asyncio.Future[None],
        operation: _ActiveRun | None,
    ) -> None:
        try:
            for predecessor in previous:
                await asyncio.shield(predecessor)
            await self._dispatch(request, operation)
        except asyncio.CancelledError:
            if not isinstance(request, StdioRunRequest):
                raise
            await self.write_error(request.request_id, 'run_cancelled', 'Run cancelled')
        except Exception as error:
            await self._write_response(
                StdioErrorResponse(request_id=request.request_id, error=_server_error_from_exception(error))
            )
        finally:
            if operation is not None:
                self._operations.pop(request.request_id, None)
            self._pending.pop(done)
            done.set_result(None)

    async def _dispatch(
        self,
        request: StdioInitializeRequest | StdioRunRequest | StdioCommandRequest,
        operation: _ActiveRun | None = None,
    ) -> None:
        try:
            if isinstance(request, StdioInitializeRequest):
                await self._initialize(request)
            elif isinstance(request, StdioRunRequest):
                await self._run(request, cast(_ActiveRun, operation))
            else:
                await self._command(request)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._write_response(
                StdioErrorResponse(request_id=request.request_id, error=_server_error_from_exception(error))
            )

    async def _initialize(self, request: StdioInitializeRequest) -> None:
        response = StdioInitializedResponse(
            request_id=request.request_id,
            agents=tuple(StdioDescriptor(id=agent.id, description=agent.description) for agent in self._agents),
            commands=tuple(
                StdioDescriptor(id=command.id, description=command.description) for command in self._commands.values()
            ),
        )
        await self._write_response(response)

    async def _run(self, request: StdioRunRequest, operation: _ActiveRun) -> None:
        context = _request_context(request.request_id, f'/agents/{request.agent_id}')
        await self._runtime.events(
            request.agent_id,
            request.request,
            context,
            send=partial(self._run_frame, request.request_id),
            operation=operation,
        )

    async def _run_frame(
        self,
        request_id: str,
        event: AgentEvent | AgentRunResponse | ServerErrorResponse,
    ) -> None:
        if isinstance(event, AgentRunResponse):
            response = StdioRunResultResponse(request_id=request_id, result=event)
        elif isinstance(event, ServerErrorResponse):
            response = StdioErrorResponse(request_id=request_id, error=event)
        else:
            response = StdioEventResponse(request_id=request_id, event=event)
        await self._write_response(response)

    async def _command(self, request: StdioCommandRequest) -> None:
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
        except asyncio.CancelledError, TimeoutError, CredentialError:
            raise
        except Exception as error:
            raise _CommandExecutionError from error

        await self._write_response(StdioCommandResultResponse(request_id=request.request_id, result=result))

    async def write_error(self, request_id: str | None, code: str, message: str) -> None:
        await self._write_response(
            StdioErrorResponse(request_id=request_id, error=ServerErrorResponse(code=code, message=message))
        )

    async def _write_response(self, response: StdioResponse) -> None:
        payload = f'{response.model_dump_json()}\n'.encode()
        async with self._write_lock:
            frame = asyncio.ensure_future(self._writer(payload))
            try:
                await asyncio.shield(frame)
            except asyncio.CancelledError:
                await frame
                raise


def _request_context(request_id: str, path: str) -> RequestContext:
    return RequestContext(method='STDIO', path=path, request_id=request_id)
