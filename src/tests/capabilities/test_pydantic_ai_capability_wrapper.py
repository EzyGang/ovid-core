from collections.abc import Callable
from dataclasses import dataclass

import pytest
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, Toolset
from pydantic_ai.messages import (
    InstructionPart,
    LoadCapabilityCallPart,
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness.planning import Planning

from ovid_core.adapters.pydantic_ai import adapt_integration_capability, pydantic_ai_capability
from ovid_core.agents import AgentDefinition
from ovid_core.capabilities.base import BaseCapability
from ovid_core.hooks.base import BaseToolHook
from ovid_core.hooks.models import ToolCall
from ovid_core.routing.models import ModelRef
from ovid_core.tools.base import ToolExecutionContext
from tests.support.agent_helpers import agent_factory


type _Instruction = str | InstructionPart | Callable[[RunContext[None]], str]


@dataclass
class InstructedCapability(AbstractCapability[None]):
    instructions: _Instruction | list[_Instruction] | None = None

    def get_instructions(self) -> _Instruction | list[_Instruction] | None:
        return self.instructions


class OwnerHook(BaseToolHook[None]):
    def __init__(self) -> None:
        self.owners: list[str | None] = []

    async def before_tool(self, context: ToolExecutionContext[None], call: ToolCall) -> None:
        del context
        self.owners.append(call.capability_id)


def greet() -> str:
    return 'hello'


def dynamic(ctx: RunContext[None]) -> str:
    del ctx
    return 'dynamic'


def _definition(capability: BaseCapability[None], *, hook: OwnerHook | None = None) -> AgentDefinition[None, str]:
    return AgentDefinition[None, str](
        model=ModelRef(name='primary'),
        deps_type=type(None),
        output_type=str,
        capabilities=(capability,),
        hooks=(hook,) if hook is not None else (),
    )


def _scripted(*responses: ModelResponse) -> tuple[FunctionModel, list[set[tuple[str, str | None]]]]:
    remaining = iter(responses)
    visible: list[set[tuple[str, str | None]]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        visible.append({(tool.name, tool.capability_id) for tool in info.function_tools})
        return next(remaining)

    return FunctionModel(respond), visible


@pytest.mark.parametrize(
    ('instructions', 'static', 'dynamic_count'),
    [
        (None, (), 0),
        ('Plan first.', ('Plan first.',), 0),
        (dynamic, (), 1),
        (['Plan first.', InstructionPart('Keep it short.'), dynamic], ('Plan first.', 'Keep it short.'), 1),
    ],
)
def test_wrapped_capability_descriptor_lists_static_and_counts_dynamic_instructions(
    instructions: _Instruction | list[_Instruction] | None,
    static: tuple[str, ...],
    dynamic_count: int,
) -> None:
    capability = pydantic_ai_capability(InstructedCapability(id='instructed', instructions=instructions))

    descriptor = capability.descriptor(source='caller')

    assert (descriptor.instructions, descriptor.dynamic_instructions) == (static, dynamic_count)


async def test_wrapped_planning_exposes_its_tools_and_descriptor_instructions() -> None:
    planning = Planning[None]()
    model, visible = _scripted(ModelResponse(parts=(TextPart('planned'),)))
    factory = agent_factory({'primary': model})

    prepared = await factory.prepare(_definition(pydantic_ai_capability(planning)))
    result = await factory.build_prepared(prepared).run('Plan.', deps=None)

    descriptor = prepared.context.capabilities[0]
    assert (descriptor.id, descriptor.instructions) == ('planning', (planning.get_instructions(),))
    assert result.output == 'planned'
    assert {('write_plan', 'planning'), ('read_plan', 'planning')} <= visible[0]


async def test_wrapped_capability_tools_carry_the_wrapper_id_for_hooks() -> None:
    source = Toolset[None](FunctionToolset([greet]))
    hook = OwnerHook()
    model, visible = _scripted(
        ModelResponse(parts=(ToolCallPart('greet', {}, 'greet-1'),)),
        ModelResponse(parts=(TextPart('greeted'),)),
    )

    agent = await agent_factory({'primary': model}).build(_definition(pydantic_ai_capability(source), hook=hook))
    result = await agent.run('Greet.', deps=None)

    assert result.output == 'greeted'
    assert visible[0] == {('greet', 'toolset')}
    assert hook.owners == ['toolset']
    assert source.id is None


async def test_deferred_wrapped_capability_keeps_its_explicit_id_and_loads() -> None:
    source = Toolset[None](FunctionToolset([greet]), id='greeting', defer_loading=True)
    capability = pydantic_ai_capability(source)
    model, visible = _scripted(
        ModelResponse(parts=(LoadCapabilityCallPart(args={'id': 'greeting'}, tool_call_id='load'),)),
        ModelResponse(parts=(TextPart('loaded'),)),
    )

    agent = await agent_factory({'primary': model}).build(_definition(capability))
    result = await agent.run('Load.', deps=None)

    assert adapt_integration_capability(capability) is source
    assert result.output == 'loaded'
    assert ('greet', 'greeting') not in visible[0]
    assert ('greet', 'greeting') in visible[1]
