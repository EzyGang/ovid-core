from typing import TYPE_CHECKING, Literal

from pydantic import Field, PositiveInt

from ovid_core.capabilities.base import AgentCapabilityDescriptor, AgentExtensionSource, BaseCapability
from ovid_core.models import BaseModel
from ovid_core.observability import ObservabilityConfig
from ovid_core.policy import AgentRunPolicy
from ovid_core.routing.models import ModelRef, ModelRouteRef, ResolvedModel
from ovid_core.services import AgentServices
from ovid_core.tools.base import BaseToolset
from ovid_core.tools.models import AgentToolDescriptor, AgentToolsetDescriptor, ToolApproval


if TYPE_CHECKING:
    from ovid_core.agents import AgentDefinition


class AgentServiceDiagnostic(BaseModel):
    id: str = Field(min_length=1)
    api_version: int = Field(ge=1)
    name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    features: tuple[str, ...]
    identity: str | None = None
    consumers: tuple[str, ...]


class AgentBuildContext(BaseModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    selected_model: str = Field(min_length=1)
    capabilities: tuple[AgentCapabilityDescriptor, ...]
    tools: tuple[AgentToolDescriptor, ...]
    toolsets: tuple[AgentToolsetDescriptor, ...]
    services: tuple[AgentServiceDiagnostic, ...]


class AgentExtensionProvenance(BaseModel):
    kind: Literal['capability', 'tool', 'toolset', 'hook', 'instructions']
    id: str = Field(min_length=1)
    source: str = Field(min_length=1)


class AgentConstructionDiagnostics(BaseModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    requested: ModelRef | ModelRouteRef
    selected_model: str = Field(min_length=1)
    fallback_order: tuple[str, ...] = Field(min_length=1)
    context_window: PositiveInt | None = None
    policy: AgentRunPolicy
    observability: ObservabilityConfig
    tool_approval: ToolApproval | None = None
    extensions: tuple[AgentExtensionProvenance, ...]
    services: tuple[AgentServiceDiagnostic, ...] = ()


def build_agent_context[Deps](
    *,
    resolved: ResolvedModel,
    capabilities: tuple[tuple[BaseCapability[Deps], AgentExtensionSource], ...],
    direct_toolsets: tuple[BaseToolset[Deps], ...],
    tool_approval: ToolApproval | None,
    services: AgentServices,
) -> AgentBuildContext:
    capability_descriptors = tuple(capability.descriptor(source=source) for capability, source in capabilities)
    tools = tuple(
        tool.descriptor(source=capability.id, approval=tool_approval)
        for capability, _ in capabilities
        for tool in capability.contributions.tools
    )
    toolsets = tuple(
        toolset.descriptor(source=capability.id)
        for capability, _ in capabilities
        for toolset in capability.contributions.toolsets
    ) + tuple(toolset.descriptor(source='caller') for toolset in direct_toolsets)

    return AgentBuildContext(
        provider=resolved.provider,
        model=resolved.model,
        selected_model=resolved.selected_model,
        capabilities=capability_descriptors,
        tools=tools,
        toolsets=toolsets,
        services=_service_diagnostics(capabilities, services),
    )


def _service_diagnostics[Deps](
    capabilities: tuple[tuple[BaseCapability[Deps], AgentExtensionSource], ...],
    services: AgentServices,
) -> tuple[AgentServiceDiagnostic, ...]:
    consumers: dict[tuple[str, int, str], list[str]] = {}
    for capability, _ in capabilities:
        for requirement in capability.requirements:
            ref = requirement.ref()
            key = (ref.key.id, ref.key.api_version, ref.name)
            consumers.setdefault(key, []).append(capability.id)

    diagnostics: list[AgentServiceDiagnostic] = []
    for binding in services.bindings:
        ref = binding.ref
        diagnostics.append(
            AgentServiceDiagnostic(
                id=ref.key.id,
                api_version=ref.key.api_version,
                name=ref.name,
                provider=binding.provider,
                features=tuple(sorted(binding.features)),
                identity=binding.identity,
                consumers=tuple(consumers.get((ref.key.id, ref.key.api_version, ref.name), ())),
            )
        )

    return tuple(diagnostics)


def diagnostics[Deps, Output](
    definition: AgentDefinition[Deps, Output],
    resolved: ResolvedModel,
    context: AgentBuildContext,
) -> AgentConstructionDiagnostics:
    extensions: list[AgentExtensionProvenance] = []
    if definition.instructions:
        extensions.append(AgentExtensionProvenance(kind='instructions', id='caller', source='caller'))

    capability_sources = {descriptor.id: descriptor.source for descriptor in context.capabilities}
    for capability in definition.capabilities:
        extensions.append(
            AgentExtensionProvenance(
                kind='capability',
                id=capability.id,
                source=capability_sources[capability.id],
            )
        )
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
        context_window=resolved.handle.context_window,
        policy=definition.policy,
        observability=definition.observability,
        tool_approval=definition.tool_approval,
        extensions=tuple(extensions),
        services=context.services,
    )
