import asyncio
from abc import abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, cast

from pydantic import Field

from ovid_core.capabilities.base import BaseCapability
from ovid_core.config.models import OvidConfig
from ovid_core.credentials.resolvers import CredentialResolver, ProviderAPIKeyResolver
from ovid_core.errors import ModelResolutionError
from ovid_core.hooks.base import BaseToolHook
from ovid_core.mcp.capability import create_mcp_capability
from ovid_core.messages.models import AgentMessage
from ovid_core.models import BaseModel
from ovid_core.observability import ObservabilityConfig
from ovid_core.policy import AgentRunPolicy
from ovid_core.routing.factory import ModelFactory
from ovid_core.routing.models import ModelRef, ModelRouteRef, ResolvedModel
from ovid_core.routing.router import ModelRouter
from ovid_core.runtime.events import AgentEvent
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.runtime.results import RunResult
from ovid_core.services import AgentServices
from ovid_core.tools.base import BaseToolset
from ovid_core.tools.models import ToolApproval
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
    tool_approval: ToolApproval | None = None
    hooks: tuple[BaseToolHook[Deps], ...] = ()
    policy: AgentRunPolicy = AgentRunPolicy()
    observability: ObservabilityConfig = ObservabilityConfig()
    services: AgentServices = AgentServices()


class AgentServiceDiagnostic(BaseModel):
    id: str = Field(min_length=1)
    api_version: int = Field(ge=1)
    name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    features: tuple[str, ...]
    identity: str | None = None
    consumers: tuple[str, ...]


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
    tool_approval: ToolApproval | None = None
    extensions: tuple[AgentExtensionProvenance, ...]
    services: tuple[AgentServiceDiagnostic, ...] = ()


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
        prompt: str,
        *,
        deps: Deps,
        messages: tuple[AgentMessage, ...] = (),
        run_id: RunId | None = None,
        conversation_id: ConversationId | None = None,
        usage_tracker: UsageTracker | None = None,
        model: AgentModelSelector | None = None,
    ) -> RunResult[Output]:
        runtime = await self._resolve_runtime(model)

        return await runtime.run(
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
        model: AgentModelSelector | None = None,
    ) -> AbstractAsyncContextManager[AgentStream[Output]]:
        return self._stream(
            prompt,
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
        prompt: str,
        *,
        deps: Deps,
        messages: tuple[AgentMessage, ...],
        run_id: RunId | None,
        conversation_id: ConversationId | None,
        usage_tracker: UsageTracker | None,
        model: AgentModelSelector | None,
    ) -> AsyncIterator[AgentStream[Output]]:
        runtime = await self._resolve_runtime(model)

        async with runtime.stream(
            prompt,
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


class AgentFactory:
    def __init__(
        self,
        *,
        config: OvidConfig,
        model_factory: ModelFactory | None = None,
        compiler: AgentCompiler | None = None,
        provider_api_key: ProviderAPIKeyResolver | None = None,
        credential_resolver: CredentialResolver | None = None,
    ) -> None:
        if model_factory is None:
            from ovid_core.adapters.pydantic_ai.models import DefaultModelFactory

            model_factory = DefaultModelFactory(provider_api_key=provider_api_key)
        elif provider_api_key is not None:
            raise ValueError('provider_api_key is only valid with the default model factory')

        if compiler is None:
            from ovid_core.adapters.pydantic_ai.agents import DefaultAgentCompiler

            compiler = DefaultAgentCompiler()

        self._router = ModelRouter(config=config, factory=model_factory)
        self._compiler = compiler
        self._mcp_configs = config.mcp_servers
        self._credential_resolver = credential_resolver
        self._mcp_capabilities: tuple[BaseCapability[Any], ...] | None = None
        self._mcp_lock = asyncio.Lock()

    async def build[Deps, Output](
        self,
        definition: AgentDefinition[Deps, Output],
        *,
        model: AgentModelSelector | None = None,
    ) -> OvidAgent[Deps, Output]:
        configured_capabilities = await self._configured_capabilities()
        unbound_definition = replace(
            definition,
            model=definition.model if model is None else model,
            capabilities=(*configured_capabilities, *definition.capabilities),
        )
        effective_definition = _bind_definition(unbound_definition)
        resolved = await self._router.resolve(effective_definition.model)
        runtime = self._compiler.compile(effective_definition, resolved)

        return OvidAgent(
            runtime=runtime,
            diagnostics=_diagnostics(effective_definition, resolved),
            runtime_resolver=lambda selector: self._runtime_for_model(effective_definition, selector),
        )

    async def _runtime_for_model[Deps, Output](
        self,
        definition: AgentDefinition[Deps, Output],
        model: AgentModelSelector,
    ) -> AgentRuntime[Deps, Output]:
        effective_definition = replace(definition, model=model)
        resolved = await self._router.resolve(model)

        return self._compiler.compile(effective_definition, resolved)

    async def _configured_capabilities[Deps](self) -> tuple[BaseCapability[Deps], ...]:
        async with self._mcp_lock:
            if self._mcp_capabilities is None:
                capabilities = await asyncio.gather(
                    *(create_mcp_capability(config, resolver=self._credential_resolver) for config in self._mcp_configs)
                )
                self._mcp_capabilities = cast(tuple[BaseCapability[Any], ...], capabilities)

        return cast(tuple[BaseCapability[Deps], ...], self._mcp_capabilities)


def _bind_definition[Deps, Output](definition: AgentDefinition[Deps, Output]) -> AgentDefinition[Deps, Output]:
    capabilities = tuple(capability.bind(definition.services) for capability in definition.capabilities)
    return replace(definition, capabilities=capabilities)


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
        tool_approval=definition.tool_approval,
        extensions=tuple(extensions),
        services=_service_diagnostics(definition),
    )


def _service_diagnostics[Deps, Output](
    definition: AgentDefinition[Deps, Output],
) -> tuple[AgentServiceDiagnostic, ...]:
    diagnostics: list[AgentServiceDiagnostic] = []

    for binding in definition.services.bindings:
        ref = binding.ref
        consumers = tuple(
            capability.id
            for capability in definition.capabilities
            if any(
                requirement.service_id == ref.key.id
                and requirement.api_version == ref.key.api_version
                and requirement.name == ref.name
                for requirement in capability.requirements
            )
        )
        diagnostics.append(
            AgentServiceDiagnostic(
                id=ref.key.id,
                api_version=ref.key.api_version,
                name=ref.name,
                provider=binding.provider,
                features=tuple(sorted(binding.features)),
                identity=binding.identity,
                consumers=consumers,
            )
        )

    return tuple(diagnostics)
