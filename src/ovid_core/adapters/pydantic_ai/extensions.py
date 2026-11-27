from collections.abc import Sequence
from dataclasses import dataclass

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AbstractToolset

from ovid_core.adapters.pydantic_ai._extension_validation import validate_extension_ids
from ovid_core.adapters.pydantic_ai._tool_collisions import _combine_toolsets
from ovid_core.adapters.pydantic_ai.integrations import adapt_integration_capability
from ovid_core.adapters.pydantic_ai.tools import PydanticAICapabilityAdapter, PydanticAIToolsetAdapter, _StaticToolset
from ovid_core.capabilities.base import BaseCapability
from ovid_core.policy import AgentRetryPolicy
from ovid_core.tools.base import BaseToolset
from ovid_core.tools.models import ToolApproval


@dataclass(frozen=True, slots=True)
class PydanticAIExtensions[Deps]:
    capabilities: tuple[AbstractCapability[Deps], ...]
    toolsets: tuple[AbstractToolset[Deps], ...]


def adapt_agent_extensions[Deps](
    capabilities: Sequence[BaseCapability[Deps]],
    toolsets: Sequence[BaseToolset[Deps]],
    *,
    tool_approval: ToolApproval | None = None,
    retries: AgentRetryPolicy = AgentRetryPolicy(),
) -> PydanticAIExtensions[Deps]:
    validate_extension_ids(capabilities, toolsets)
    adapted_capabilities = tuple(_adapt_capability(capability) for capability in capabilities)
    adapted_toolsets = [
        PydanticAIToolsetAdapter(
            source=source,
            tool_approval=tool_approval,
            capability_id=capability.id,
            deferred_capability_id=capability.id if capability.defer_loading else None,
            retries=retries,
        )
        for capability in capabilities
        for source in _capability_toolsets(capability)
    ]
    adapted_toolsets.extend(
        PydanticAIToolsetAdapter(source=source, tool_approval=tool_approval, retries=retries) for source in toolsets
    )
    combined = _combine_toolsets(tuple(adapted_toolsets))

    return PydanticAIExtensions(
        capabilities=adapted_capabilities,
        toolsets=(combined,) if combined is not None else (),
    )


def _adapt_capability[Deps](capability: BaseCapability[Deps]) -> AbstractCapability[Deps]:
    integration = adapt_integration_capability(capability)
    if integration is not None:
        return integration

    return PydanticAICapabilityAdapter(capability)


def _capability_toolsets[Deps](capability: BaseCapability[Deps]) -> tuple[BaseToolset[Deps], ...]:
    contributions = capability.contributions
    if not contributions.tools:
        return contributions.toolsets

    return (_StaticToolset(id=capability.id, tools=contributions.tools), *contributions.toolsets)
