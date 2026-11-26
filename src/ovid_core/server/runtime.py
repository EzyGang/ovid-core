import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from typing import Any

from ovid_core.agent_runtime import AgentStream, OvidAgent
from ovid_core.messages.models import AgentMessage
from ovid_core.persistence import ConversationStore, RunJournal
from ovid_core.runtime.events import AgentEvent, RunCompletedEvent, RunDeferredEvent, RunFailedEvent
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.server.active_runs import _ActiveRun, _ActiveRuns, _ConnectionOwner
from ovid_core.server.contracts import AgentRegistration, AuthorizationCallback, RequestContext
from ovid_core.server.conversations import (
    _open_history,
    _persist_result,
    _response_from_result,
    _start_commit,
    _stopped_partial,
    _stored_resume,
    _wait_for_commit,
)
from ovid_core.server.errors import _AuthorizationDeniedError, _server_error_from_exception, _UnknownAgentError
from ovid_core.server.models import AgentRunRequest, AgentRunResponse, ServerConfig, ServerErrorResponse
from ovid_core.server.registry import AgentRegistry


@dataclass(frozen=True, slots=True)
class _AgentServerSession:
    agent: OvidAgent[Any, Any]
    deps: Any
    messages: tuple[AgentMessage, ...]
    conversation_id: ConversationId
    deadline: asyncio.Timeout
    journal: RunJournal | None


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
        completion: RunCompletedEvent | RunDeferredEvent | None = None
        try:
            async with self.stream(agent_id, request, context, operation=operation) as stream:
                async for event in stream:
                    if isinstance(event, RunCompletedEvent | RunDeferredEvent):
                        completion = event
                        continue
                    last_event = event
                    if not isinstance(event, RunFailedEvent):
                        await send(event)

                result = _response_from_result(stream.result)

        except asyncio.CancelledError as error:
            if not operation.cancelled:
                raise
            await self._send_failure(error, last_event=last_event, operation=operation, send=send)
            return
        except Exception as error:
            await self._send_failure(error, last_event=last_event, operation=operation, send=send)
            return
        if completion is not None:
            await send(completion)
        await send(result)

    async def _send_failure(
        self,
        error: BaseException,
        *,
        last_event: AgentEvent | None,
        operation: _ActiveRun,
        send: Callable[[AgentEvent | AgentRunResponse | ServerErrorResponse], Awaitable[None]],
    ) -> None:
        operation.terminal = True
        failure = _server_error_from_exception(error)
        if isinstance(last_event, RunFailedEvent):
            await send(last_event.model_copy(update={'persisted': operation.persisted}))
        elif last_event is not None:
            await send(
                RunFailedEvent(
                    run_id=last_event.run_id,
                    conversation_id=last_event.conversation_id,
                    sequence=last_event.sequence + 1,
                    error_type='CancelledError' if failure.code == 'run_cancelled' else type(error).__name__,
                    message=failure.message,
                    persisted=operation.persisted,
                )
            )
        await send(failure)

    @asynccontextmanager
    async def stream(
        self,
        agent_id: str,
        request: AgentRunRequest,
        context: RequestContext,
        *,
        operation: _ActiveRun | None = None,
    ) -> AsyncGenerator[AgentStream[Any]]:
        lifetime = self._active_runs.run(agent_id) if operation is None else nullcontext(operation)
        async with lifetime as active:
            async with self.session(agent_id, request.conversation_id, context, operation=active) as session:
                resume = await _stored_resume(self._store, session.conversation_id, session.messages, request.answers)
                stream: AgentStream[Any] | None = None
                try:
                    async with session.agent.stream(
                        request.prompt,
                        resume=resume,
                        deps=session.deps,
                        messages=session.messages,
                        conversation_id=session.conversation_id,
                        run_id=active.run_id,
                        journal=session.journal,
                    ) as stream:
                        yield stream
                except (Exception, asyncio.CancelledError) as error:
                    partial = _stopped_partial(
                        stream.partial() if stream is not None else None, error, session.deadline
                    )
                    active.commit = _start_commit(self._store, partial, journal=session.journal)
                    active.persisted = await _wait_for_commit(active.commit, self._config.shutdown_grace_seconds)
                    raise

                await _persist_result(self._store, stream.result, journal=session.journal)
                active.terminal = True

    @asynccontextmanager
    async def session(
        self,
        agent_id: str,
        conversation_id: ConversationId | None,
        context: RequestContext,
        *,
        operation: _ActiveRun | None = None,
    ) -> AsyncGenerator[_AgentServerSession]:
        registration = self._registration(agent_id)
        if agent_id not in self._agent_limits:
            self._agent_limits[agent_id] = asyncio.Semaphore(
                _agent_concurrency(registration, self._config.max_concurrency)
            )

        async with self._global_limit, self._agent_limits[agent_id]:
            async with asyncio.timeout(self._timeout(registration)) as deadline:
                authorization = await self._authorize(context, registration.id)
                if not authorization.allowed:
                    raise _AuthorizationDeniedError
                conversation_id = conversation_id or ConversationId.new()
                if operation is not None:
                    operation.authorization = authorization
                    self._active_runs.admit(operation, conversation_id)
                messages, journal = await _open_history(self._store, conversation_id, admitted=operation is not None)
                deps = await registration.dependencies(context, authorization)

                yield _AgentServerSession(
                    agent=registration.agent,
                    deps=deps,
                    messages=messages,
                    conversation_id=conversation_id,
                    deadline=deadline,
                    journal=journal,
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
