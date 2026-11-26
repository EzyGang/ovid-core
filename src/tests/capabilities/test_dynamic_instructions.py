import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ovid_core import AgentDefinition
from ovid_core.capabilities import (
    AgentCapabilityDescriptor,
    BaseCapability,
    CapabilityContributions,
    CapabilityModelSettings,
    InstructionSource,
)
from ovid_core.errors import AgentRunError
from ovid_core.routing import ModelRef
from ovid_core.runtime import RunContext
from tests.support.agent_consumer import AgentDependencies, ConsumerToolset
from tests.support.agent_helpers import agent_factory
from tests.support.deferred_consumer import SafeTool


type Request = tuple[str | None, float | None]
type Instructions = tuple[InstructionSource[AgentDependencies], ...]


async def _later(ctx: RunContext[AgentDependencies]) -> str | None:
    return 'Report now.' if ctx.run_step > 1 else None


def _definition(
    instructions: Instructions, contributions: CapabilityContributions[AgentDependencies]
) -> AgentDefinition[AgentDependencies, str]:
    return AgentDefinition[AgentDependencies, str](
        model=ModelRef(name='primary'),
        deps_type=AgentDependencies,
        output_type=str,
        instructions=instructions,
        toolsets=(ConsumerToolset((SafeTool(),)),),
        capabilities=(BaseCapability(id='runtime', contributions=contributions),),
    )


def _two_steps(requests: list[Request]) -> FunctionModel:
    """Call the safe tool on the first request and answer on the second."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        requests.append((info.instructions, (info.model_settings or {}).get('temperature')))
        if len(requests) == 1:
            return ModelResponse(parts=[ToolCallPart('safe', {'value': 1}, tool_call_id='c1')])

        return ModelResponse(parts=[TextPart('done')])

    return FunctionModel(respond)


async def _run(
    *,
    instructions: Instructions = (),
    contributions: CapabilityContributions[AgentDependencies] = CapabilityContributions(),
    prepared_instructions: Instructions | None = None,
) -> list[Request]:
    requests: list[Request] = []
    factory = agent_factory({'primary': _two_steps(requests)})
    prepared = await factory.prepare(_definition(instructions, contributions))
    if prepared_instructions is not None:
        prepared = prepared.with_instructions(prepared_instructions)
    agent = factory.build_prepared(prepared)

    result = await agent.run('Go.', deps=AgentDependencies(prefix='dynamic'))

    assert result.output == 'done'
    return requests


async def test_capability_instruction_source_sees_each_run_step() -> None:
    contributions = CapabilityContributions[AgentDependencies](instructions=(lambda ctx: f'step={ctx.run_step}',))

    requests = await _run(contributions=contributions)

    assert requests == [('step=1', None), ('step=2', None)]


async def test_async_instruction_source_is_awaited_and_none_contributes_nothing() -> None:
    async def owner(ctx: RunContext[AgentDependencies]) -> str:
        return f'Work for {ctx.deps.prefix}.'

    contributions = CapabilityContributions[AgentDependencies](
        instructions=('Static.', owner, lambda ctx: None, _later),
    )

    requests = await _run(contributions=contributions)

    assert [text for text, _ in requests] == [
        'Static.\n\nWork for dynamic.',
        'Static.\n\nWork for dynamic.\n\nReport now.',
    ]


async def test_model_settings_source_applies_only_on_targeted_steps() -> None:
    def later_steps(ctx: RunContext[AgentDependencies]) -> CapabilityModelSettings | None:
        return CapabilityModelSettings(values={'temperature': 0.2}) if ctx.run_step > 1 else None

    requests = await _run(contributions=CapabilityContributions(model_settings=later_steps))

    assert [temperature for _, temperature in requests] == [None, 0.2]


async def test_static_capability_instructions_and_settings_apply_on_every_step() -> None:
    contributions = CapabilityContributions[AgentDependencies](
        instructions=('Static.',),
        model_settings=CapabilityModelSettings(values={'temperature': 0.5}),
    )

    requests = await _run(contributions=contributions)

    assert requests == [('Static.', 0.5), ('Static.', 0.5)]


async def test_agent_instruction_source_sees_each_run_step() -> None:
    requests = await _run(instructions=('Agent.', lambda ctx: f'agent step={ctx.run_step}'))

    assert [text for text, _ in requests] == ['Agent.\n\nagent step=1', 'Agent.\n\nagent step=2']


async def test_prepared_definition_accepts_static_and_dynamic_instructions() -> None:
    requests = await _run(instructions=('Replaced.',), prepared_instructions=('Rendered.', _later))

    assert [text for text, _ in requests] == ['Rendered.', 'Rendered.\n\nReport now.']


async def test_failing_instruction_source_ends_run_with_normalized_error() -> None:
    def broken(ctx: RunContext[AgentDependencies]) -> str:
        raise RuntimeError(f'private source detail {ctx.run_step}')

    with pytest.raises(AgentRunError, match='^Agent run failed$') as raised:
        await _run(contributions=CapabilityContributions(instructions=(broken,)))

    assert 'private' not in str(raised.value)


def test_descriptor_counts_dynamic_sources_and_keeps_static_text() -> None:
    dynamic = BaseCapability[AgentDependencies](
        id='runtime',
        contributions=CapabilityContributions(instructions=('Static.', lambda ctx: None, _later)),
    )
    static = BaseCapability[AgentDependencies](
        id='plain',
        contributions=CapabilityContributions(instructions=('Only.',)),
    )

    described = dynamic.descriptor(source='caller')

    assert (described.instructions, described.dynamic_instructions) == (('Static.',), 2)
    assert static.descriptor(source='caller').dynamic_instructions == 0
    assert AgentCapabilityDescriptor(id='manual', defer_loading=False, instructions=(), source='caller') == (
        BaseCapability[None](id='manual').descriptor(source='caller')
    )
