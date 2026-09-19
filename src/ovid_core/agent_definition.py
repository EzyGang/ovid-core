from dataclasses import dataclass, replace
from typing import Self

from ovid_core.agent_build import AgentBuildContext
from ovid_core.capabilities.base import BaseCapability
from ovid_core.hooks.base import BaseToolHook
from ovid_core.observability import ObservabilityConfig
from ovid_core.policy import AgentRunPolicy
from ovid_core.routing.models import ModelRef, ModelRouteRef, ResolvedModel
from ovid_core.services import AgentServices
from ovid_core.tools.base import BaseToolset
from ovid_core.tools.models import ToolApproval


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


@dataclass(frozen=True, slots=True)
class PreparedAgentDefinition[Deps, Output]:
    definition: AgentDefinition[Deps, Output]
    context: AgentBuildContext
    _resolved: ResolvedModel

    def with_instructions(self, instructions: tuple[str, ...]) -> Self:
        return replace(self, definition=replace(self.definition, instructions=instructions))

    def with_policy(self, policy: AgentRunPolicy) -> Self:
        return replace(self, definition=replace(self.definition, policy=policy))
