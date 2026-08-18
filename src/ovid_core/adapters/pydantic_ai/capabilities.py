from dataclasses import dataclass, field
from typing import cast

from pydantic.alias_generators import to_snake
from pydantic_ai.capabilities import AbstractCapability

from ovid_core.capabilities.base import BaseCapability


@dataclass(frozen=True, slots=True, kw_only=True)
class _PydanticAICapabilityPort[Deps](BaseCapability[Deps]):
    source: AbstractCapability[Deps] = field(repr=False, compare=False, hash=False)


def pydantic_ai_capability[Deps](capability: AbstractCapability[Deps]) -> BaseCapability[Deps]:
    if not isinstance(capability, AbstractCapability):
        raise TypeError('capability must be a Pydantic AI AbstractCapability')
    if capability.id is not None and (not capability.id or capability.id != capability.id.strip()):
        raise ValueError('Pydantic AI capability ID must be a non-empty trimmed string')
    if capability.defer_loading and capability.id is None:
        raise ValueError('deferred Pydantic AI capabilities require an explicit ID')

    capability_id = capability.id or to_snake(type(capability).__name__)
    if capability.id is None:
        capability.id = capability_id

    return _PydanticAICapabilityPort[Deps](
        id=capability_id,
        description=capability.description,
        defer_loading=capability.defer_loading,
        source=capability,
    )


def _pydantic_ai_capability[Deps](capability: BaseCapability[Deps]) -> AbstractCapability[Deps] | None:
    if not isinstance(capability, _PydanticAICapabilityPort):
        return None

    return cast(AbstractCapability[Deps], capability.source)
