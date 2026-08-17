from dataclasses import dataclass
from typing import Any, Self

from pydantic import Field, JsonValue

from ovid_core.hooks.base import BaseToolHook
from ovid_core.models import BaseModel
from ovid_core.services import AgentServiceRequirement, AgentServices
from ovid_core.tools.base import BaseTool, BaseToolset


class CapabilityModelSettings(BaseModel):
    values: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CapabilityContributions[Deps]:
    instructions: tuple[str, ...] = ()
    tools: tuple[BaseTool[Deps, Any, Any], ...] = ()
    toolsets: tuple[BaseToolset[Deps], ...] = ()
    hooks: tuple[BaseToolHook[Deps], ...] = ()
    model_settings: CapabilityModelSettings = CapabilityModelSettings()


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseCapability[Deps]:
    id: str
    description: str | None = None
    defer_loading: bool = False
    contributions: CapabilityContributions[Deps] = CapabilityContributions()
    requirements: tuple[AgentServiceRequirement, ...] = ()

    def bind(self, services: AgentServices) -> Self:
        for requirement in self.requirements:
            services.validate(requirement, consumer=self.id)

        return self
