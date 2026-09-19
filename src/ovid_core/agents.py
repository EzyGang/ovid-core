import asyncio
from abc import abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from typing import Any, Protocol, cast

from ovid_core.agent_build import AgentConstructionDiagnostics, build_agent_context, diagnostics
from ovid_core.agent_definition import AgentDefinition as AgentDefinition
from ovid_core.agent_definition import AgentModelSelector as AgentModelSelector
from ovid_core.agent_definition import PreparedAgentDefinition as PreparedAgentDefinition
from ovid_core.capabilities.base import AgentExtensionSource, BaseCapability
from ovid_core.config.models import OvidConfig
from ovid_core.credentials.resolvers import CredentialResolver, ProviderAPIKeyResolver
from ovid_core.errors import ModelResolutionError
from ovid_core.mcp.capability import create_mcp_capability
from ovid_core.messages.models import AgentMessage
from ovid_core.routing.factory import ModelFactory
from ovid_core.routing.models import ResolvedModel
from ovid_core.routing.router import ModelRouter
from ovid_core.runtime.events import AgentEvent
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.runtime.results import RunResult
from ovid_core.usage.tracking import UsageTracker


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

    async def prepare[Deps, Output](
        self,
        definition: AgentDefinition[Deps, Output],
        *,
        model: AgentModelSelector | None = None,
    ) -> PreparedAgentDefinition[Deps, Output]:
        configured_capabilities = await self._configured_capabilities()
        sources: tuple[AgentExtensionSource, ...] = (
            *('configuration' for _ in configured_capabilities),
            *('caller' for _ in definition.capabilities),
        )
        unbound_definition = replace(
            definition,
            model=definition.model if model is None else model,
            capabilities=(*configured_capabilities, *definition.capabilities),
        )
        effective_definition = _bind_definition(unbound_definition)
        resolved = await self._router.resolve(effective_definition.model)
        capability_sources = tuple(zip(effective_definition.capabilities, sources, strict=True))
        context = build_agent_context(
            resolved=resolved,
            capabilities=capability_sources,
            direct_toolsets=effective_definition.toolsets,
            tool_approval=effective_definition.tool_approval,
            services=effective_definition.services,
        )

        return PreparedAgentDefinition(definition=effective_definition, context=context, _resolved=resolved)

    def extend_prepared[Deps, Output](
        self,
        prepared: PreparedAgentDefinition[Deps, Output],
        capabilities: tuple[BaseCapability[Deps], ...],
    ) -> PreparedAgentDefinition[Deps, Output]:
        if not capabilities:
            return prepared

        definition = _bind_definition(
            replace(
                prepared.definition,
                capabilities=(*prepared.definition.capabilities, *capabilities),
            )
        )
        sources: tuple[AgentExtensionSource, ...] = (
            *(descriptor.source for descriptor in prepared.context.capabilities),
            *('caller' for _ in capabilities),
        )
        context = build_agent_context(
            resolved=prepared._resolved,
            capabilities=tuple(zip(definition.capabilities, sources, strict=True)),
            direct_toolsets=definition.toolsets,
            tool_approval=definition.tool_approval,
            services=definition.services,
        )

        return PreparedAgentDefinition(definition=definition, context=context, _resolved=prepared._resolved)

    def build_prepared[Deps, Output](
        self,
        prepared: PreparedAgentDefinition[Deps, Output],
    ) -> OvidAgent[Deps, Output]:
        definition = prepared.definition
        runtime = self._compiler.compile(definition, prepared._resolved)

        return OvidAgent(
            runtime=runtime,
            diagnostics=diagnostics(definition, prepared._resolved, prepared.context),
            runtime_resolver=lambda selector: self._runtime_for_model(definition, selector),
        )

    async def build[Deps, Output](
        self,
        definition: AgentDefinition[Deps, Output],
        *,
        model: AgentModelSelector | None = None,
    ) -> OvidAgent[Deps, Output]:
        prepared = await self.prepare(definition, model=model)
        return self.build_prepared(prepared)

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
