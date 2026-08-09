from dataclasses import dataclass
from typing import Any

from pydantic import Field, JsonValue

from ovid_core.hooks.base import BaseToolHook
from ovid_core.models import BaseModel
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


@dataclass(frozen=True, slots=True)
class BaseCapability[Deps]:
    id: str
    contributions: CapabilityContributions[Deps] = CapabilityContributions()
