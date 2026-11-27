from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic.alias_generators import to_snake
from pydantic_ai.capabilities import AbstractCapability, WrapperCapability
from pydantic_ai.messages import InstructionPart

from ovid_core.capabilities.base import AgentCapabilityDescriptor, AgentExtensionSource, BaseCapability


@dataclass(frozen=True, slots=True, kw_only=True)
class _PydanticAICapabilityPort[Deps](BaseCapability[Deps]):
    source: AbstractCapability[Deps] = field(repr=False, compare=False, hash=False)

    def descriptor(self, *, source: AgentExtensionSource) -> AgentCapabilityDescriptor:
        instructions = self.source.get_instructions()
        if instructions is None:
            items = ()
        elif isinstance(instructions, str) or not isinstance(instructions, Sequence):
            items = (instructions,)
        else:
            items = tuple(instructions)
        static = tuple(
            item if isinstance(item, str) else item.content for item in items if isinstance(item, str | InstructionPart)
        )

        descriptor = super().descriptor(source=source)
        return descriptor.model_copy(update={'instructions': static, 'dynamic_instructions': len(items) - len(static)})


def pydantic_ai_capability[Deps](capability: AbstractCapability[Deps]) -> BaseCapability[Deps]:
    if not isinstance(capability, AbstractCapability):
        raise TypeError('capability must be a Pydantic AI AbstractCapability')
    if capability.id is not None and (not capability.id or capability.id != capability.id.strip()):
        raise ValueError('Pydantic AI capability ID must be a non-empty trimmed string')
    if capability.defer_loading and capability.id is None:
        raise ValueError('deferred Pydantic AI capabilities require an explicit ID')

    capability_id = capability.id or to_snake(type(capability).__name__)
    source = capability if capability.id is not None else WrapperCapability[Deps](wrapped=capability, id=capability_id)

    return _PydanticAICapabilityPort[Deps](
        id=capability_id,
        description=capability.description,
        defer_loading=capability.defer_loading,
        source=source,
    )


def _pydantic_ai_capability[Deps](capability: BaseCapability[Deps]) -> AbstractCapability[Deps] | None:
    if not isinstance(capability, _PydanticAICapabilityPort):
        return None

    return capability.source
