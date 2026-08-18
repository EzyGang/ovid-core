from dataclasses import dataclass, field
from typing import Self, cast

import pytest
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ovid_core.adapters.pydantic_ai import adapt_capabilities, pydantic_ai_capability
from ovid_core.agents import AgentDefinition
from ovid_core.capabilities.base import BaseCapability
from ovid_core.errors import ExtensionCollisionError
from ovid_core.routing.models import ModelRef
from tests.support.agent_helpers import agent_factory


@dataclass
class RecordingCapability(AbstractCapability[None]):
    events: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return False

    async def for_run(self, ctx: RunContext[None]) -> Self:
        del ctx
        self.events.append('for_run')

        return self

    async def before_run(self, ctx: RunContext[None]) -> None:
        del ctx
        self.events.append('before_run')


def test_pydantic_ai_capability_preserves_source_and_metadata() -> None:
    source = RecordingCapability(id='recording', description='Record the run.')
    capability = pydantic_ai_capability(source)

    assert capability.id == 'recording'
    assert capability.description == 'Record the run.'
    assert capability.defer_loading is False
    assert capability.contributions.tools == ()
    assert adapt_capabilities((capability,)) == (source,)


def test_pydantic_ai_capability_derives_non_deferred_id() -> None:
    source = RecordingCapability()

    assert pydantic_ai_capability(source).id == 'recording_capability'
    assert source.id == 'recording_capability'


def test_pydantic_ai_capability_rejects_invalid_values() -> None:
    invalid = cast(AbstractCapability[None], cast(object, 'invalid'))

    with pytest.raises(TypeError, match='AbstractCapability'):
        pydantic_ai_capability(invalid)
    with pytest.raises(ValueError, match='explicit ID'):
        pydantic_ai_capability(RecordingCapability(defer_loading=True))
    for capability_id in ('', ' spaced '):
        with pytest.raises(ValueError, match='non-empty trimmed'):
            pydantic_ai_capability(RecordingCapability(id=capability_id))


def test_pydantic_ai_capability_id_collides_with_ovid_capability() -> None:
    upstream = pydantic_ai_capability(RecordingCapability(id='shared'))

    with pytest.raises(ExtensionCollisionError, match="Duplicate capability ID: 'shared'"):
        adapt_capabilities((BaseCapability[None](id='shared'), upstream))


async def test_pydantic_ai_capability_runs_with_full_lifecycle() -> None:
    source = RecordingCapability(id='recording')

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        assert source.events == ['for_run', 'before_run']

        return ModelResponse(parts=(TextPart('proxied'),))

    definition = AgentDefinition[None, str](
        model=ModelRef(name='primary'),
        deps_type=type(None),
        output_type=str,
        capabilities=(pydantic_ai_capability(source),),
    )
    agent = await agent_factory({'primary': FunctionModel(respond)}).build(definition)

    assert (await agent.run('Use the capability.', deps=None)).output == 'proxied'
