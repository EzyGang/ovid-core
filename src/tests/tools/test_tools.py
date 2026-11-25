import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic_ai import RunContext as PydanticRunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

import tests.tools.tool_consumer as consumer_module
from ovid_core import (
    AgentDefinition,
    AgentRetryPolicy,
    AgentRunPolicy,
    ExtensionCollisionError,
    ToolExecutionError,
    ToolTimeoutError,
    ToolValidationError,
)
from ovid_core.adapters.pydantic_ai import PydanticAIToolsetAdapter
from ovid_core.adapters.pydantic_ai.extensions import adapt_agent_extensions
from ovid_core.capabilities import BaseCapability, CapabilityContributions
from ovid_core.hooks import BaseToolHook
from ovid_core.routing import ModelRef
from ovid_core.tools import ToolApproval, ToolExecutionContext, ToolGrammar, ToolPresentation, ToolResult
from tests.support.agent_helpers import agent_factory
from tests.tools.tool_consumer import (
    AddArguments,
    AddTool,
    ControlledTool,
    Dependencies,
    FastAddTool,
    RecordingHook,
    TrackingToolset,
    arithmetic_capability,
)


class PinnedAddTool(AddTool):
    id = 'pinned_add'
    presentation = ToolPresentation(wire_name='calculate')

    def __init__(self, value: str) -> None:
        super().__init__()
        self.value = value

    async def execute(
        self,
        context: ToolExecutionContext[Dependencies],
        arguments: AddArguments,
    ) -> ToolResult:
        del context, arguments
        return ToolResult(content=self.value)


class TextAddTool(AddTool):
    presentation = ToolPresentation(
        input_format='text',
        grammar=ToolGrammar(syntax='lark', definition='start: INT "+" INT'),
    )


def upstream_context(
    *,
    approved: bool = False,
    approval_metadata: object = None,
    usage: RunUsage | None = None,
) -> PydanticRunContext[Dependencies]:
    return PydanticRunContext(
        deps=Dependencies(prefix='sum'),
        model=TestModel(),
        usage=usage or RunUsage(),
        tool_call_id='call-1',
        tool_name='add',
        tool_call_approved=approved,
        tool_call_metadata=approval_metadata,
        run_id=str(uuid4()),
        conversation_id=str(uuid4()),
    )


async def test_toolset_adapter_propagates_identity_usage_approval_hooks_and_lifecycle() -> None:
    tool = AddTool()
    tool.defer_loading = True
    hook = RecordingHook()
    source = TrackingToolset((tool,))
    adapter = PydanticAIToolsetAdapter(source=source, hooks=(hook,))
    context = upstream_context(
        approved=True,
        approval_metadata={'reviewer': 'user'},
        usage=RunUsage(requests=2, tool_calls=1, input_tokens=5, output_tokens=3),
    )

    run_adapter = await adapter.for_run(context)
    await run_adapter.__aenter__()
    step_adapter = await run_adapter.for_run_step(context)
    definitions = await step_adapter.get_tools(context)
    result = await step_adapter.call_tool('add', {'left': 2, 'right': 3}, context, definitions['add'])
    await step_adapter.__aexit__(None, None, None)

    assert result == {'content': 5, 'metadata': {'prefix': 'sum'}}
    assert definitions['add'].tool_def.kind == 'unapproved'
    assert definitions['add'].tool_def.defer_loading is True
    assert definitions['add'].tool_def.metadata == {
        'ovid_approval': {'required': True, 'reason': 'Writes a total', 'metadata': {'risk': 'low'}},
        'ovid_effect': {'kind': 'external', 'replay': 'unsafe'},
    }
    assert tool.context is not None
    assert tool.context.approved is True
    assert tool.context.approval_metadata == {'reviewer': 'user'}
    assert tool.context.run.usage.request_count == 2
    assert tool.context.run.usage.total_tokens == 8
    assert hook.events == ['before:add', 'after:add']
    assert (source.entered, source.steps, source.exited) == (1, 1, 1)

    base_hook = BaseToolHook[Dependencies]()
    await base_hook.before_tool(tool.context, 'add', tool.args_type(left=2, right=3))
    await base_hook.after_tool(tool.context, 'add', ToolResult(content=5))
    await base_hook.on_tool_error(tool.context, 'add', ToolExecutionError('failed'))


@pytest.mark.parametrize(
    ('tool_approval', 'expected_kind'),
    (
        (None, 'unapproved'),
        (ToolApproval(required=False, reason='Run without approval'), 'function'),
        (ToolApproval(required=True, reason='Require caller approval'), 'unapproved'),
    ),
)
async def test_toolset_adapter_applies_tool_approval_override(
    tool_approval: ToolApproval | None,
    expected_kind: str,
) -> None:
    tool = AddTool()
    adapter = PydanticAIToolsetAdapter(
        source=TrackingToolset((tool,)),
        tool_approval=tool_approval,
    )

    definitions = await adapter.get_tools(upstream_context())
    definition = definitions['add'].tool_def
    effective_approval = tool.approval if tool_approval is None else tool_approval

    assert definition.kind == expected_kind
    assert definition.metadata == {
        'ovid_approval': effective_approval.model_dump(mode='json'),
        'ovid_effect': {'kind': 'external', 'replay': 'unsafe'},
    }


async def test_raise_mode_adapter_raises_typed_errors_and_host_faults_abort_in_return_mode() -> None:
    context = upstream_context()
    hook = RecordingHook()
    raising = AgentRetryPolicy(tools=0, output=0, tool_failures='raise')

    invalid_adapter = PydanticAIToolsetAdapter(source=TrackingToolset((AddTool(),)), hooks=(hook,), retries=raising)
    definitions = await invalid_adapter.get_tools(context)
    with pytest.raises(ToolValidationError, match="Invalid arguments for tool 'add'"):
        await invalid_adapter.call_tool('add', {'left': 'bad'}, context, definitions['add'])
    assert definitions['add'].max_retries == 0

    host_adapter = PydanticAIToolsetAdapter(source=TrackingToolset((AddTool(),)), hooks=(hook,))
    definitions = await host_adapter.get_tools(context)
    with pytest.raises(ToolExecutionError, match="Tool 'missing' is not available"):
        await PydanticAIToolsetAdapter(source=TrackingToolset(())).call_tool('missing', {}, context, definitions['add'])
    with pytest.raises(ToolExecutionError, match='did not supply a tool call ID'):
        await host_adapter.call_tool(
            'add',
            {'left': 1, 'right': 2},
            replace(context, tool_call_id=None),
            definitions['add'],
        )
    with pytest.raises(ToolExecutionError, match='did not supply run identity'):
        await host_adapter.get_tools(replace(context, run_id=None))
    with pytest.raises(ToolExecutionError, match='invalid approval metadata'):
        await host_adapter.call_tool(
            'add',
            {'left': 1, 'right': 2},
            replace(context, tool_call_metadata=object()),
            definitions['add'],
        )
    with pytest.raises(ToolExecutionError, match='invalid tool execution context'):
        await host_adapter.get_tools(replace(context, run_id='invalid'))

    for mode, error_type, message in (
        ('error', ToolExecutionError, "Tool 'controlled' failed"),
        ('typed_error', ToolExecutionError, 'controlled failure'),
        ('invalid_result', ToolExecutionError, "Tool 'controlled' failed"),
    ):
        controlled = ControlledTool(mode=mode)
        adapter = PydanticAIToolsetAdapter(source=TrackingToolset((controlled,)), hooks=(hook,), retries=raising)
        definitions = await adapter.get_tools(context)
        with pytest.raises(error_type, match=message):
            await adapter.call_tool('controlled', {'left': 1, 'right': 2}, context, definitions['controlled'])

    timeout_tool = ControlledTool(mode='wait', timeout_seconds=0.001)
    timeout_adapter = PydanticAIToolsetAdapter(
        source=TrackingToolset((timeout_tool,)),
        hooks=(hook,),
        retries=raising,
    )
    definitions = await timeout_adapter.get_tools(context)
    with pytest.raises(ToolTimeoutError, match="Tool 'controlled' timed out"):
        await timeout_adapter.call_tool('controlled', {'left': 1, 'right': 2}, context, definitions['controlled'])

    cancellation_tool = ControlledTool(mode='wait')
    cancellation_adapter = PydanticAIToolsetAdapter(source=TrackingToolset((cancellation_tool,)), hooks=(hook,))
    definitions = await cancellation_adapter.get_tools(context)
    task = asyncio.create_task(
        cancellation_adapter.call_tool('controlled', {'left': 1, 'right': 2}, context, definitions['controlled'])
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancellation_tool.cancelled is True
    assert hook.events.count('error:controlled') == 4


async def test_capabilities_compile_contributions_and_execute_through_real_agent() -> None:
    tool = FastAddTool()
    hook = RecordingHook()
    toolset = TrackingToolset(())
    capability = arithmetic_capability(tool, toolset, hook)
    requests: list[AgentInfo] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        requests.append(info)
        if len(messages) > 1:
            return ModelResponse(parts=(TextPart('done'),))

        return ModelResponse(parts=(ToolCallPart('fast_add', {'left': 2, 'right': 3}, 'call-1'),))

    definition = AgentDefinition[Dependencies, str](
        model=ModelRef(name='primary'),
        deps_type=Dependencies,
        output_type=str,
        capabilities=(capability,),
    )
    agent = await agent_factory({'primary': FunctionModel(respond)}).build(definition)
    result = await agent.run('Add values', deps=Dependencies(prefix='agent'))

    assert requests[0].instructions == 'Use arithmetic when needed.'
    assert requests[0].model_settings == {'temperature': 0}
    assert tool.context is not None
    assert tool.context.run.run_id == result.run_id
    assert tool.context.run.conversation_id == result.conversation_id
    assert tool.context.run.usage.request_count == 1
    assert result.usage.tool_calls == 1
    assert any(part.kind == 'tool_call' for message in result.messages for part in message.parts)
    assert any(part.kind == 'tool_return' for message in result.messages for part in message.parts)
    assert hook.events == ['before:fast_add', 'after:fast_add']


async def test_agent_retry_policy_reaches_capability_tools_through_the_compiler() -> None:
    hook = RecordingHook()
    capability = BaseCapability(
        id='controlled',
        contributions=CapabilityContributions(tools=(ControlledTool(mode='typed_error'),), hooks=(hook,)),
    )

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=(ToolCallPart('controlled', {'left': 1, 'right': 2}, 'call-1'),))

    definition = AgentDefinition[Dependencies, str](
        model=ModelRef(name='primary'),
        deps_type=Dependencies,
        output_type=str,
        capabilities=(capability,),
        policy=AgentRunPolicy(retries=AgentRetryPolicy(tools=0, tool_failures='raise')),
    )
    agent = await agent_factory({'primary': FunctionModel(respond)}).build(definition)

    with pytest.raises(ToolExecutionError, match='controlled failure'):
        await agent.run('Fail.', deps=Dependencies(prefix='raise'))

    assert hook.events == ['before:controlled', 'error:controlled']


def test_extension_ids_collide_deterministically() -> None:
    tool = FastAddTool()
    duplicate_tools = BaseCapability(
        id='one',
        contributions=CapabilityContributions(tools=(tool, tool)),
    )
    duplicate_toolsets = BaseCapability(
        id='one',
        contributions=CapabilityContributions(toolsets=(TrackingToolset(()), TrackingToolset(()))),
    )

    for capabilities, message in (
        ((BaseCapability(id='same'), BaseCapability(id='same')), "Duplicate capability ID: 'same'"),
        ((BaseCapability(id=''),), 'Capability IDs must not be empty'),
        ((duplicate_tools,), "Duplicate tool ID: 'fast_add'"),
        ((duplicate_toolsets,), "Duplicate toolset ID: 'arithmetic'"),
    ):
        with pytest.raises(ExtensionCollisionError, match=message):
            adapt_agent_extensions(capabilities, (), ())


async def test_dynamic_tool_collisions_and_empty_capabilities_are_stable() -> None:
    context = upstream_context()
    duplicate_adapter = PydanticAIToolsetAdapter(source=TrackingToolset((AddTool(), AddTool())))
    with pytest.raises(ExtensionCollisionError, match="Duplicate or empty effective tool name: 'add'"):
        await duplicate_adapter.get_tools(context)

    replacement = PydanticAIToolsetAdapter(source=TrackingToolset((AddTool(),), replace_on_step=True))
    assert await replacement.for_run_step(context) is not replacement

    source = TrackingToolset((PinnedAddTool('old'),))
    pinned = PydanticAIToolsetAdapter(source=source)
    old_definitions = await pinned.get_tools(context)
    source.tools = (PinnedAddTool('new'),)
    new_definitions = await pinned.get_tools(context)
    old_copy = replace(old_definitions['calculate'], tool_def=new_definitions['calculate'].tool_def)
    old_result = await pinned.call_tool('calculate', {'left': 1, 'right': 2}, context, old_copy)
    new_result = await pinned.call_tool('calculate', {'left': 1, 'right': 2}, context, new_definitions['calculate'])
    assert old_result['content'] == 'old'
    assert new_result['content'] == 'new'

    text_adapter = PydanticAIToolsetAdapter(source=TrackingToolset((TextAddTool(),)))
    text_definitions = await text_adapter.get_tools(context)
    assert text_definitions['add'].tool_def.parameters_json_schema['properties'].keys() == {'left', 'right'}
    one_toolset = adapt_agent_extensions(
        (BaseCapability(id='one', contributions=CapabilityContributions(toolsets=(TrackingToolset(()),))),),
        (),
        (),
        retries=AgentRetryPolicy(tools=5),
    ).toolsets[0]
    assert isinstance(one_toolset, PydanticAIToolsetAdapter)
    assert one_toolset.retries.tools == 5

    conflicting = adapt_agent_extensions(
        (
            BaseCapability(
                id='conflicting',
                contributions=CapabilityContributions(
                    tools=(AddTool(),),
                    toolsets=(TrackingToolset((AddTool(),)),),
                ),
            ),
        ),
        (),
        (),
    ).toolsets[0]
    with pytest.raises(ExtensionCollisionError, match='conflicting tool IDs'):
        await conflicting.get_tools(context)

    empty = adapt_agent_extensions((BaseCapability(id='empty'),), (), ())
    assert empty.capabilities[0].get_instructions() is None
    assert empty.capabilities[0].get_model_settings() is None
    assert empty.toolsets == ()


def test_consumer_module_has_no_pydantic_ai_dependency() -> None:
    assert all(
        not getattr(value, '__module__', '').startswith('pydantic_ai')
        for name, value in vars(consumer_module).items()
        if not name.startswith('_')
    )
    assert ToolApproval().model_dump() == {'required': False, 'reason': None, 'metadata': {}}
