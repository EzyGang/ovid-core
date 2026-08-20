from pydantic import Field

from ovid_core.capabilities.base import AgentCapabilityDescriptor, AgentExtensionSource, BaseCapability
from ovid_core.models import BaseModel
from ovid_core.routing.models import ResolvedModel
from ovid_core.services import AgentServices
from ovid_core.tools.base import BaseToolset
from ovid_core.tools.models import AgentToolDescriptor, AgentToolsetDescriptor, ToolApproval


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
