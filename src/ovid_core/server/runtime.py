import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue, TypeAdapter
from pydantic_core import to_jsonable_python

from ovid_core.agents import AgentStream, OvidAgent
from ovid_core.messages.models import AgentMessage
from ovid_core.persistence import ConversationStore
from ovid_core.runtime.events import AgentEvent, RunCompletedEvent, RunFailedEvent
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.runtime.results import RunResult
from ovid_core.server.active_runs import _ActiveRun, _ActiveRuns, _ConnectionOwner
from ovid_core.server.contracts import AgentRegistration, AuthorizationCallback, LifecycleCallback, RequestContext
from ovid_core.server.errors import _AuthorizationDeniedError, _server_error_from_exception, _UnknownAgentError
from ovid_core.server.models import AgentRunRequest, AgentRunResponse, ServerConfig, ServerErrorResponse
from ovid_core.server.registry import AgentRegistry


_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)


@dataclass(frozen=True, slots=True)
class _AgentServerSession:
    agent: OvidAgent[Any, Any]
    deps: Any
    messages: tuple[AgentMessage, ...]
    conversation_id: ConversationId


class _AgentServerRuntime:
    def __init__(
        self,
        *,
        agents: Sequence[AgentRegistration[Any, Any]],
        authorize: AuthorizationCallback,
        config: ServerConfig,
        store: ConversationStore | None,
    ) -> None:
        self._agents = agents if isinstance(agents, AgentRegistry) else AgentRegistry(agents)
        self._authorize = authorize
        self._config = config
        self._store = store
        self._active_runs = _ActiveRuns()
        self._global_limit = asyncio.Semaphore(config.max_concurrency)
        self._agent_limits: dict[str, asyncio.Semaphore] = {}

    def start(
        self,
        agent_id: str,
        owner: _ConnectionOwner | None,
        tasks: asyncio.TaskGroup,
        execute: Callable[[_ActiveRun], Awaitable[None]],
    ) -> _ActiveRun:
        return self._active_runs.start(agent_id, owner, tasks, execute)

    async def events(
        self,
        agent_id: str,
        request: AgentRunRequest,
        context: RequestContext,
        *,
        send: Callable[[AgentEvent | AgentRunResponse | ServerErrorResponse], Awaitable[None]],
        operation: _ActiveRun,
    ) -> None:
        last_event: AgentEvent | None = None
        completion: RunCompletedEvent | None = None
        try:
            async with self.stream(agent_id, request, context, operation=operation) as stream:
                async for event in stream:
                    if isinstance(event, RunCompletedEvent):
                        completion = event
                    else:
                        last_event = event
                        await send(event)

                result = _response_from_result(stream.result)

        except (Exception, asyncio.CancelledError) as error:
            if isinstance(error, asyncio.CancelledError) and not operation.cancelled:
                raise
            operation.terminal = True
            failure = _server_error_from_exception(error)
            if last_event is not None and not isinstance(last_event, RunFailedEvent):
                await send(
                    RunFailedEvent(
                        run_id=last_event.run_id,
                        conversation_id=last_event.conversation_id,
                        sequence=last_event.sequence + 1,
                        error_type='CancelledError' if failure.code == 'run_cancelled' else type(error).__name__,
                        message=failure.message,
                    )
                )
            await send(failure)
            return
        if completion is not None:
            await send(completion)
        await send(result)

    @asynccontextmanager
    async def stream(
        self,
        agent_id: str,
        request: AgentRunRequest,
        context: RequestContext,
        *,
        operation: _ActiveRun | None = None,
    ) -> AsyncIterator[AgentStream[Any]]:
        lifetime = self._active_runs.run(agent_id) if operation is None else nullcontext(operation)
        async with lifetime as active:
            async with self.session(agent_id, request.conversation_id, context, operation=active) as session:
                async with session.agent.stream(
                    request.prompt,
                    deps=session.deps,
                    messages=session.messages,
                    conversation_id=session.conversation_id,
                    run_id=active.run_id,
                ) as stream:
                    yield stream

                await self.persist(stream.result)
                active.terminal = True

    @asynccontextmanager
    async def session(
        self,
        agent_id: str,
        conversation_id: ConversationId | None,
        context: RequestContext,
        *,
        operation: _ActiveRun | None = None,
    ) -> AsyncIterator[_AgentServerSession]:
        registration = self._registration(agent_id)
        if agent_id not in self._agent_limits:
            self._agent_limits[agent_id] = asyncio.Semaphore(
                _agent_concurrency(registration, self._config.max_concurrency)
            )

        async with self._global_limit, self._agent_limits[agent_id]:
            async with asyncio.timeout(self._timeout(registration)):
                authorization = await self._authorize(context, registration.id)
                if not authorization.allowed:
                    raise _AuthorizationDeniedError
                if operation is not None:
                    operation.authorization = authorization
                conversation_id = conversation_id or ConversationId.new()
                messages = await self._store.load(conversation_id) if self._store is not None else ()
                deps = await registration.dependencies(context, authorization)

                yield _AgentServerSession(
                    agent=registration.agent,
                    deps=deps,
                    messages=messages,
                    conversation_id=conversation_id,
                )

    async def cancel(self, agent_id: str, run_id: RunId, context: RequestContext) -> None:
        registration = self._registration(agent_id)
        async with asyncio.timeout(self._timeout(registration)):
            authorization = await self._authorize(context, registration.id)
            if not authorization.allowed or authorization.principal is None:
                raise _AuthorizationDeniedError
            self._active_runs.cancel(run_id, agent_id, authorization.principal)

    async def cancel_owned(self, run_id: RunId, owner: _ConnectionOwner) -> None:
        await self._active_runs.cancel_owned(run_id, owner)

    async def disconnect(self, operation: _ActiveRun) -> None:
        await self._active_runs.disconnect(operation)

    async def close(self) -> None:
        await self._active_runs.close()

    async def persist(self, result: RunResult[Any]) -> None:
        if self._store is not None:
            await self._store.append(result.conversation_id, result.messages)

    def agent(self, agent_id: str) -> OvidAgent[Any, Any]:
        return self._registration(agent_id).agent

    def _registration(self, agent_id: str) -> AgentRegistration[Any, Any]:
        registration = self._agents.get(agent_id)
        if registration is None:
            raise _UnknownAgentError

        return registration

    def _timeout(self, registration: AgentRegistration[Any, Any]) -> float:
        agent_timeout = registration.agent.diagnostics.policy.timeout_seconds

        if agent_timeout is None:
            return self._config.request_timeout_seconds

        return min(agent_timeout, self._config.request_timeout_seconds)


def _agent_concurrency(registration: AgentRegistration[Any, Any], server_limit: int) -> int:
    agent_limit = registration.agent.diagnostics.policy.max_concurrency

    return min(agent_limit, server_limit) if agent_limit is not None else server_limit


def _response_from_result(result: RunResult[Any]) -> AgentRunResponse:
    return AgentRunResponse(
        output=_JSON_VALUE_ADAPTER.validate_python(to_jsonable_python(result.output)),
        messages=result.messages,
        usage=result.usage,
        run_id=result.run_id,
        conversation_id=result.conversation_id,
    )


@asynccontextmanager
async def _server_lifespan(
    *,
    startup: LifecycleCallback | None,
    shutdown: LifecycleCallback | None,
    shutdown_grace_seconds: int,
    close: LifecycleCallback | None = None,
) -> AsyncIterator[None]:
    if startup is not None:
        await startup()

    try:
        yield
    finally:
        async with asyncio.timeout(shutdown_grace_seconds):
            try:
                if close is not None:
                    await close()
            finally:
                if shutdown is not None:
                    await shutdown()
