from abc import abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol

from ovid_core.agent_build import AgentConstructionDiagnostics
from ovid_core.agent_definition import AgentDefinition, AgentModelSelector
from ovid_core.errors import ModelResolutionError
from ovid_core.messages.content import UserContent
from ovid_core.messages.models import AgentMessage
from ovid_core.routing.models import ResolvedModel
from ovid_core.runtime.events import AgentEvent
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.runtime.results import DeferredRunResult, RunResult
from ovid_core.tools.deferred import DeferredToolResume
from ovid_core.usage.tracking import UsageTracker


class AgentStream[Output](AsyncIterator[AgentEvent], Protocol):
    @property
    @abstractmethod
    def result(self) -> RunResult[Output] | DeferredRunResult: ...


class AgentRuntime[Deps, Output](Protocol):
    @abstractmethod
    async def run(
        self,
        prompt: str | Sequence[UserContent] | None,
        *,
        resume: DeferredToolResume | None,
        deps: Deps,
        messages: tuple[AgentMessage, ...],
        run_id: RunId | None,
        conversation_id: ConversationId | None,
        usage_tracker: UsageTracker | None,
    ) -> RunResult[Output] | DeferredRunResult: ...

    @abstractmethod
    def stream(
        self,
        prompt: str | Sequence[UserContent] | None,
        *,
        resume: DeferredToolResume | None,
        deps: Deps,
        messages: tuple[AgentMessage, ...],
        run_id: RunId | None,
        conversation_id: ConversationId | None,
        usage_tracker: UsageTracker | None,
    ) -> AbstractAsyncContextManager[AgentStream[Output]]: ...


class AgentCompiler(Protocol):
    @abstractmethod
    def compile[Deps, Output](
        self,
        definition: AgentDefinition[Deps, Output],
        resolved: ResolvedModel,
    ) -> AgentRuntime[Deps, Output]: ...


class OvidAgent[Deps, Output]:
    def __init__(
        self,
        *,
        runtime: AgentRuntime[Deps, Output],
        diagnostics: AgentConstructionDiagnostics,
        runtime_resolver: Callable[[AgentModelSelector], Awaitable[AgentRuntime[Deps, Output]]] | None = None,
    ) -> None:
        self._runtime = runtime
        self._runtime_resolver = runtime_resolver
        self.diagnostics = diagnostics

    async def run(
        self,
        prompt: str | Sequence[UserContent] | None = None,
        *,
        resume: DeferredToolResume | None = None,
        deps: Deps,
        messages: tuple[AgentMessage, ...] = (),
        run_id: RunId | None = None,
        conversation_id: ConversationId | None = None,
        usage_tracker: UsageTracker | None = None,
        model: AgentModelSelector | None = None,
    ) -> RunResult[Output] | DeferredRunResult:
        runtime = await self._resolve_runtime(model)

        return await runtime.run(
            prompt,
            resume=resume,
            deps=deps,
            messages=messages,
            run_id=run_id,
            conversation_id=conversation_id,
            usage_tracker=usage_tracker,
        )

    def stream(
        self,
        prompt: str | Sequence[UserContent] | None = None,
        *,
        resume: DeferredToolResume | None = None,
        deps: Deps,
        messages: tuple[AgentMessage, ...] = (),
        run_id: RunId | None = None,
        conversation_id: ConversationId | None = None,
        usage_tracker: UsageTracker | None = None,
        model: AgentModelSelector | None = None,
    ) -> AbstractAsyncContextManager[AgentStream[Output]]:
        return self._stream(
            prompt,
            resume=resume,
            deps=deps,
            messages=messages,
            run_id=run_id,
            conversation_id=conversation_id,
            usage_tracker=usage_tracker,
            model=model,
        )

    @asynccontextmanager
    async def _stream(
        self,
        prompt: str | Sequence[UserContent] | None,
        *,
        resume: DeferredToolResume | None,
        deps: Deps,
        messages: tuple[AgentMessage, ...],
        run_id: RunId | None,
        conversation_id: ConversationId | None,
        usage_tracker: UsageTracker | None,
        model: AgentModelSelector | None,
    ) -> AsyncGenerator[AgentStream[Output]]:
        runtime = await self._resolve_runtime(model)

        async with runtime.stream(
            prompt,
            resume=resume,
            deps=deps,
            messages=messages,
            run_id=run_id,
            conversation_id=conversation_id,
            usage_tracker=usage_tracker,
        ) as stream:
            yield stream

    async def _resolve_runtime(self, model: AgentModelSelector | None) -> AgentRuntime[Deps, Output]:
        if model is None:
            return self._runtime
        if self._runtime_resolver is None:
            raise ModelResolutionError('this agent does not support model overrides')

        return await self._runtime_resolver(model)

    def _runtime_for_adapter(self) -> AgentRuntime[Deps, Output]:
        return self._runtime
