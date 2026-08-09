import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic_ai import Agent
from pydantic_ai import RunContext as PydanticRunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

import tests.tool_consumer as consumer_module
from ovid_core.adapters.pydantic_ai.results import result_from_pydantic
from ovid_core.adapters.pydantic_ai.tools import PydanticAIToolsetAdapter, adapt_capabilities
from ovid_core.capabilities.base import BaseCapability, CapabilityContributions
from ovid_core.errors import ExtensionCollisionError, ToolExecutionError, ToolTimeoutError, ToolValidationError
from ovid_core.hooks.base import BaseToolHook
from ovid_core.runtime.events import tool_events_from_messages
from ovid_core.tools.models import ToolApproval, ToolResult
from tests.tool_consumer import (
    AddTool,
    ControlledTool,
    Dependencies,
    FastAddTool,
    RecordingHook,
    TrackingToolset,
    arithmetic_capability,
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
    assert definitions['add'].tool_def.metadata == {
        'ovid_approval': {'required': True, 'reason': 'Writes a total', 'metadata': {'risk': 'low'}}
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


async def test_adapter_preserves_typed_validation_execution_timeout_and_cancellation_errors() -> None:
    context = upstream_context()
    hook = RecordingHook()

    invalid_adapter = PydanticAIToolsetAdapter(source=TrackingToolset((AddTool(),)), hooks=(hook,))
    definitions = await invalid_adapter.get_tools(context)
    with pytest.raises(ToolValidationError, match="Invalid arguments for tool 'add'"):
        await invalid_adapter.call_tool('add', {'left': 'bad'}, context, definitions['add'])
    with pytest.raises(ToolExecutionError, match="Tool 'missing' is not available"):
        await PydanticAIToolsetAdapter(source=TrackingToolset(())).call_tool('missing', {}, context, definitions['add'])

    with pytest.raises(ToolExecutionError, match='did not supply a tool call ID'):
        await invalid_adapter.call_tool(
            'add',
            {'left': 1, 'right': 2},
            replace(context, tool_call_id=None),
            definitions['add'],
        )
    with pytest.raises(ToolExecutionError, match='did not supply run identity'):
        await invalid_adapter.get_tools(replace(context, run_id=None))
    with pytest.raises(ToolExecutionError, match='invalid approval metadata'):
        await invalid_adapter.call_tool(
            'add',
            {'left': 1, 'right': 2},
            replace(context, tool_call_metadata=object()),
            definitions['add'],
        )
    with pytest.raises(ToolExecutionError, match='invalid tool execution context'):
        await invalid_adapter.get_tools(replace(context, run_id='invalid'))

    for mode, error_type, message in (
        ('error', ToolExecutionError, "Tool 'controlled' failed"),
        ('typed_error', ToolExecutionError, 'controlled failure'),
        ('invalid_result', ToolExecutionError, "Tool 'controlled' failed"),
    ):
        controlled = ControlledTool(mode=mode)
        adapter = PydanticAIToolsetAdapter(source=TrackingToolset((controlled,)), hooks=(hook,))
        definitions = await adapter.get_tools(context)
        with pytest.raises(error_type, match=message):
            await adapter.call_tool('controlled', {'left': 1, 'right': 2}, context, definitions['controlled'])

    timeout_tool = ControlledTool(mode='wait', timeout_seconds=0.001)
    timeout_adapter = PydanticAIToolsetAdapter(source=TrackingToolset((timeout_tool,)), hooks=(hook,))
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
    adapted = adapt_capabilities((capability,))

    assert adapted[0].get_instructions() == ['Use arithmetic when needed.']
    assert adapted[0].get_model_settings() == {'temperature': 0}
    assert adapted[0].get_toolset() is not None

    agent = Agent(
        TestModel(call_tools=['fast_add']),
        deps_type=Dependencies,
        capabilities=adapted,
    )
    upstream_result = await agent.run('Add values', deps=Dependencies(prefix='agent'))
    result = result_from_pydantic(upstream_result)
    events = tool_events_from_messages(
        result.messages,
        run_id=result.run_id,
        conversation_id=result.conversation_id,
    )

    assert [event.kind for event in events] == ['tool_call', 'tool_result']
    assert [event.sequence for event in events] == [0, 1]

    assert tool.context is not None
    assert tool.context.run.run_id == result.run_id
    assert tool.context.run.conversation_id == result.conversation_id
    assert tool.context.run.usage.request_count == 1
    assert result.usage.tool_calls == 1
    assert any(part.kind == 'tool_call' for message in result.messages for part in message.parts)
    assert any(part.kind == 'tool_return' for message in result.messages for part in message.parts)
    assert hook.events == ['before:fast_add', 'after:fast_add']


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
            adapt_capabilities(capabilities)


async def test_dynamic_tool_collisions_and_empty_capabilities_are_stable() -> None:
    context = upstream_context()
    duplicate_adapter = PydanticAIToolsetAdapter(source=TrackingToolset((AddTool(), AddTool())))
    with pytest.raises(ExtensionCollisionError, match="Duplicate or empty tool ID: 'add'"):
        await duplicate_adapter.get_tools(context)

    replacement = PydanticAIToolsetAdapter(source=TrackingToolset((AddTool(),), replace_on_step=True))
    assert await replacement.for_run_step(context) is not replacement

    one_toolset = adapt_capabilities(
        (BaseCapability(id='one', contributions=CapabilityContributions(toolsets=(TrackingToolset(()),))),)
    )[0].get_toolset()
    assert isinstance(one_toolset, PydanticAIToolsetAdapter)

    conflicting = adapt_capabilities(
        (
            BaseCapability(
                id='conflicting',
                contributions=CapabilityContributions(
                    tools=(AddTool(),),
                    toolsets=(TrackingToolset((AddTool(),)),),
                ),
            ),
        )
    )[0].get_toolset()
    assert conflicting is not None
    with pytest.raises(ExtensionCollisionError, match='conflicting tool IDs'):
        await conflicting.get_tools(context)

    empty = adapt_capabilities((BaseCapability(id='empty'),))[0]
    assert empty.get_instructions() is None
    assert empty.get_model_settings() is None
    assert empty.get_toolset() is None


def test_consumer_module_has_no_pydantic_ai_dependency() -> None:
    assert all(
        not getattr(value, '__module__', '').startswith('pydantic_ai')
        for name, value in vars(consumer_module).items()
        if not name.startswith('_')
    )
    assert ToolApproval().model_dump() == {'required': False, 'reason': None, 'metadata': {}}
