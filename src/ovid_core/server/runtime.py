import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue, TypeAdapter
from pydantic_core import to_jsonable_python

from ovid_core.agents import AgentStream, OvidAgent
from ovid_core.errors import AgentRunError, OvidCoreError, TransportError
from ovid_core.messages.models import AgentMessage
from ovid_core.persistence import ConversationStore
from ovid_core.runtime.identifiers import ConversationId
from ovid_core.runtime.results import RunResult
from ovid_core.server.contracts import AgentRegistration, AuthorizationCallback, LifecycleCallback, RequestContext
from ovid_core.server.models import AgentRunRequest, AgentRunResponse, ServerConfig, ServerErrorResponse


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
        self._agents = _agent_map(agents)
        self._authorize = authorize
        self._config = config
        self._store = store
        self._global_limit = asyncio.Semaphore(config.max_concurrency)
        self._agent_limits = {
            agent.id: asyncio.Semaphore(_agent_concurrency(agent, config.max_concurrency)) for agent in agents
        }

    async def run(
        self,
        agent_id: str,
        request: AgentRunRequest,
        context: RequestContext,
    ) -> AgentRunResponse:
        async with self.session(agent_id, request.conversation_id, context) as session:
            result = await session.agent.run(
                request.prompt,
                deps=session.deps,
                messages=session.messages,
                conversation_id=session.conversation_id,
            )
            await self.persist(result)

        return _response_from_result(result)

    @asynccontextmanager
    async def stream(
        self,
        agent_id: str,
        request: AgentRunRequest,
        context: RequestContext,
    ) -> AsyncIterator[AgentStream[Any]]:
        async with self.session(agent_id, request.conversation_id, context) as session:
            async with session.agent.stream(
                request.prompt,
                deps=session.deps,
                messages=session.messages,
                conversation_id=session.conversation_id,
            ) as stream:
                yield stream

            await self.persist(stream.result)

    @asynccontextmanager
    async def session(
        self,
        agent_id: str,
        conversation_id: ConversationId | None,
        context: RequestContext,
    ) -> AsyncIterator[_AgentServerSession]:
        registration = self._registration(agent_id)

        async with self._global_limit, self._agent_limits[agent_id]:
            async with asyncio.timeout(self._timeout(registration)):
                authorization = await self._authorize(context, registration.id)

                if not authorization.allowed:
                    raise _AuthorizationDeniedError

                conversation_id = conversation_id or ConversationId.new()
                messages = await self._store.load(conversation_id) if self._store is not None else ()
                deps = await registration.dependencies(context, authorization)

                yield _AgentServerSession(
                    agent=registration.agent,
                    deps=deps,
                    messages=messages,
                    conversation_id=conversation_id,
                )

    async def persist(self, result: RunResult[Any]) -> None:
        if self._store is not None:
            await self._store.append(result.conversation_id, result.messages)

    def agent(self, agent_id: str) -> OvidAgent[Any, Any]:
        return self._registration(agent_id).agent

    def _registration(self, agent_id: str) -> AgentRegistration[Any, Any]:
        try:
            return self._agents[agent_id]
        except KeyError as error:
            raise _UnknownAgentError from error

    def _timeout(self, registration: AgentRegistration[Any, Any]) -> float:
        agent_timeout = registration.agent.diagnostics.policy.timeout_seconds

        if agent_timeout is None:
            return self._config.request_timeout_seconds

        return min(agent_timeout, self._config.request_timeout_seconds)


class _UnknownAgentError(TransportError):
    pass


class _AuthorizationDeniedError(TransportError):
    pass


class _UnknownCommandError(TransportError):
    pass


class _CommandExecutionError(TransportError):
    pass


def _server_error_from_exception(error: Exception) -> ServerErrorResponse:
    if isinstance(error, _UnknownAgentError):
        return ServerErrorResponse(code='agent_not_found', message='Agent was not found')

    if isinstance(error, _UnknownCommandError):
        return ServerErrorResponse(code='command_not_found', message='Command was not found')

    if isinstance(error, _AuthorizationDeniedError):
        return ServerErrorResponse(code='forbidden', message='Request is not authorized')

    if isinstance(error, TimeoutError):
        return ServerErrorResponse(code='timeout', message='Request timed out')

    if isinstance(error, _CommandExecutionError):
        return ServerErrorResponse(code='command_failed', message='Command execution failed')

    if isinstance(error, AgentRunError):
        return ServerErrorResponse(code='agent_run_failed', message=str(error))

    if isinstance(error, OvidCoreError):
        return ServerErrorResponse(code='server_failure', message='Server operation failed')

    return ServerErrorResponse(code='internal_error', message='Internal server error')


def _agent_map(
    agents: Sequence[AgentRegistration[Any, Any]],
) -> dict[str, AgentRegistration[Any, Any]]:
    mapped = {agent.id: agent for agent in agents}

    if not mapped:
        raise ValueError('at least one agent registration is required')

    if len(mapped) != len(agents):
        raise ValueError('agent registration ids must be unique')

    return mapped


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
) -> AsyncIterator[None]:
    if startup is not None:
        await startup()

    try:
        yield
    finally:
        if shutdown is not None:
            async with asyncio.timeout(shutdown_grace_seconds):
                await shutdown()
