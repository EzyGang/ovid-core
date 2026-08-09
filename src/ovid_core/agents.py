from abc import abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import Field

from ovid_core.capabilities.base import BaseCapability
from ovid_core.hooks.base import BaseToolHook
from ovid_core.messages.models import AgentMessage
from ovid_core.models import BaseModel
from ovid_core.observability import ObservabilityConfig
from ovid_core.policy import AgentRunPolicy
from ovid_core.routing.models import ModelRef, ModelRouteRef, ResolvedModel
from ovid_core.routing.router import ModelRouter
from ovid_core.runtime.events import AgentEvent
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.runtime.results import RunResult
from ovid_core.tools.base import BaseToolset
from ovid_core.usage.tracking import UsageTracker


type AgentModelSelector = ModelRef | ModelRouteRef


@dataclass(frozen=True, slots=True)
class AgentDefinition[Deps, Output]:
    model: AgentModelSelector
    deps_type: type[Deps]
    output_type: type[Output]
    instructions: tuple[str, ...] = ()
    capabilities: tuple[BaseCapability[Deps], ...] = ()
    toolsets: tuple[BaseToolset[Deps], ...] = ()
    hooks: tuple[BaseToolHook[Deps], ...] = ()
    policy: AgentRunPolicy = AgentRunPolicy()
    observability: ObservabilityConfig = ObservabilityConfig()


class AgentExtensionProvenance(BaseModel):
    kind: Literal['capability', 'tool', 'toolset', 'hook', 'instructions']
    id: str = Field(min_length=1)
    source: str = Field(min_length=1)


class AgentConstructionDiagnostics(BaseModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    requested: AgentModelSelector
    selected_model: str = Field(min_length=1)
    fallback_order: tuple[str, ...] = Field(min_length=1)
    policy: AgentRunPolicy
    observability: ObservabilityConfig
    extensions: tuple[AgentExtensionProvenance, ...]


class AgentStream[Output](AsyncIterator[AgentEvent], Protocol):
    @property
    @abstractmethod
    def result(self) -> RunResult[Output]: ...


class AgentRuntime[Deps, Output](Protocol):
    @abstractmethod
    async def run(
        self,
        prompt: str,
        *,
        deps: Deps,
        messages: tuple[AgentMessage, ...],
        run_id: RunId | None,
        conversation_id: ConversationId | None,
        usage_tracker: UsageTracker | None,
    ) -> RunResult[Output]: ...

    @abstractmethod
    def stream(
        self,
        prompt: str,
        *,
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
    def __init__(self, *, runtime: AgentRuntime[Deps, Output], diagnostics: AgentConstructionDiagnostics) -> None:
        self._runtime = runtime
        self.diagnostics = diagnostics

    async def run(
        self,
        prompt: str,
        *,
        deps: Deps,
        messages: tuple[AgentMessage, ...] = (),
        run_id: RunId | None = None,
        conversation_id: ConversationId | None = None,
        usage_tracker: UsageTracker | None = None,
    ) -> RunResult[Output]:
        return await self._runtime.run(
            prompt,
            deps=deps,
            messages=messages,
            run_id=run_id,
            conversation_id=conversation_id,
            usage_tracker=usage_tracker,
        )

    def stream(
        self,
        prompt: str,
        *,
        deps: Deps,
        messages: tuple[AgentMessage, ...] = (),
        run_id: RunId | None = None,
        conversation_id: ConversationId | None = None,
        usage_tracker: UsageTracker | None = None,
    ) -> AbstractAsyncContextManager[AgentStream[Output]]:
        return self._runtime.stream(
            prompt,
            deps=deps,
            messages=messages,
            run_id=run_id,
            conversation_id=conversation_id,
            usage_tracker=usage_tracker,
        )


class AgentFactory:
    def __init__(self, *, router: ModelRouter, compiler: AgentCompiler) -> None:
        self._router = router
        self._compiler = compiler

    async def build[Deps, Output](self, definition: AgentDefinition[Deps, Output]) -> OvidAgent[Deps, Output]:
        resolved = await self._router.resolve(definition.model)
        runtime = self._compiler.compile(definition, resolved)

        return OvidAgent(runtime=runtime, diagnostics=_diagnostics(definition, resolved))


def _diagnostics[Deps, Output](
    definition: AgentDefinition[Deps, Output],
    resolved: ResolvedModel,
) -> AgentConstructionDiagnostics:
    extensions: list[AgentExtensionProvenance] = []
    if definition.instructions:
        extensions.append(AgentExtensionProvenance(kind='instructions', id='caller', source='caller'))

    for capability in definition.capabilities:
        extensions.append(AgentExtensionProvenance(kind='capability', id=capability.id, source='caller'))
        contributions = capability.contributions
        extensions.extend(
            AgentExtensionProvenance(kind='tool', id=tool.id, source=capability.id) for tool in contributions.tools
        )
        extensions.extend(
            AgentExtensionProvenance(kind='toolset', id=toolset.id, source=capability.id)
            for toolset in contributions.toolsets
        )
        extensions.extend(
            AgentExtensionProvenance(kind='hook', id=type(hook).__qualname__, source=capability.id)
            for hook in contributions.hooks
        )
    extensions.extend(
        AgentExtensionProvenance(kind='toolset', id=toolset.id, source='caller') for toolset in definition.toolsets
    )
    extensions.extend(
        AgentExtensionProvenance(kind='hook', id=type(hook).__qualname__, source='caller') for hook in definition.hooks
    )

    return AgentConstructionDiagnostics(
        provider=resolved.provider,
        model=resolved.model,
        requested=definition.model,
        selected_model=resolved.selected_model,
        fallback_order=resolved.fallback_order,
        policy=definition.policy,
        observability=definition.observability,
        extensions=tuple(extensions),
    )
