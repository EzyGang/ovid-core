from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from ovid_core.capabilities.base import BaseCapability, CapabilityContributions
from ovid_core.models import BaseModel


class SkillLibraryConfig(BaseModel):
    directories: tuple[Path, ...] = Field(min_length=1)
    include: tuple[str, ...] | None = None
    exclude: tuple[str, ...] | None = None

    @model_validator(mode='after')
    def select_one_filter(self) -> Self:
        if self.include is not None and self.exclude is not None:
            raise ValueError('skill include and exclude selections cannot be combined')

        return self


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillsCapability[Deps](BaseCapability[Deps]):
    description: None = field(default=None, init=False)
    defer_loading: bool = field(default=True, init=False)
    config: SkillLibraryConfig
    contributions: CapabilityContributions[Deps] = field(
        default=CapabilityContributions(),
        init=False,
        repr=False,
    )
