import asyncio

import pytest
from pydantic import ValidationError

from ovid_core.capabilities import BaseCapability, CapabilityContributions
from ovid_core.hooks import Deny, Proceed, RequireApproval, ToolCall
from ovid_core.runtime import AgentEvent, DeferredRunResult, RunResult, ToolDeferredEvent
from ovid_core.tools import DeferredToolRequest, ToolApproval, ToolCallApproved
from tests.support.agent_consumer import AgentDependencies
from tests.support.deferred_consumer import SafeTool, ScriptedModel, ValueArguments, deferred_agent, tool_returns
from tests.support.hook_consumer import DecisionHook, lookup_capability


def _deps() -> AgentDependencies:
    return AgentDependencies(prefix='hooks')


async def test_deny_on_passthrough_function_tool_returns_denied_result_and_stops_the_chain() -> None:
    calls: list[int] = []
    denying = DecisionHook(Deny(message='blocked'))
    later = DecisionHook()
    model = ScriptedModel(('lookup', {'value': 1}, 'p1'))
    agent = await deferred_agent(model, (), hooks=(denying, later), capabilities=(lookup_capability(calls),))

    result = await agent.run('Look up.', deps=_deps())

    assert isinstance(result, RunResult)
    assert result.output == 'done'
    assert tool_returns(result.messages) == [('p1', 'denied', 'blocked')]
    assert calls == []
    assert later.calls == []
    assert denying.calls == [
        ToolCall(
            tool_name='lookup',
            tool_call_id='p1',
            arguments={'value': 1},
            capability_id='lookup',
            source='external',
        )
    ]


async def test_require_approval_defers_once_and_the_approved_resume_runs_the_tool_once() -> None:
    model = ScriptedModel(('safe', {'value': 1}, 's1'))
    tool = SafeTool()
    hook = DecisionHook(
        RequireApproval(reason='Needs review', metadata={'risk': 'low'}),
        RequireApproval(reason='Needs review'),
        RequireApproval(reason='Needs review'),
    )
    agent = await deferred_agent(model, (tool,), hooks=(hook,))

    paused = await agent.run('Use the tool.', deps=_deps())
    assert isinstance(paused, DeferredRunResult)
    resumed = await agent.run(
        resume=paused.requests.resume({'s1': ToolCallApproved()}),
        deps=_deps(),
        messages=paused.history,
    )
    async with agent.stream('Use the tool.', deps=_deps()) as stream:
        events: list[AgentEvent] = [event async for event in stream]

    assert paused.requests.calls == (
        DeferredToolRequest(
            tool_call_id='s1',
            tool_name='safe',
            arguments={'value': 1},
            kind='approval',
            approval=ToolApproval(required=True, reason='Needs review', metadata={'risk': 'low'}),
            metadata={'risk': 'low'},
        ),
    )
    assert isinstance(resumed, RunResult)
    assert tool_returns(resumed.messages) == [('s1', 'success', 'safe 1')]
    assert tool.calls == [(1, True)]
    deferred = [event for event in events if isinstance(event, ToolDeferredEvent)]
    assert [(event.deferral_kind, event.reason, event.metadata) for event in deferred] == [
        ('approval', 'Needs review', None)
    ]


async def test_proceed_replaces_the_arguments_for_later_hooks_and_the_tool_in_registration_order() -> None:
    tool = SafeTool()
    replacing = DecisionHook(Proceed(arguments={'value': 5}))
    recording = DecisionHook()
    capability = BaseCapability[AgentDependencies](
        id='values',
        contributions=CapabilityContributions(tools=(tool,), hooks=(recording,)),
    )
    model = ScriptedModel(('safe', {'value': 1}, 's1'))
    agent = await deferred_agent(model, (), hooks=(replacing,), capabilities=(capability,))

    result = await agent.run('Use the tool.', deps=_deps())

    assert tool_returns(result.messages) == [('s1', 'success', 'safe 5')]
    assert tool.calls == [(5, False)]
    assert replacing.calls[0].arguments == {'value': 1}
    assert recording.calls == [
        ToolCall(
            tool_name='safe',
            tool_call_id='s1',
            arguments={'value': 5},
            tool_id='safe',
            capability_id='values',
            source='ovid',
        )
    ]


async def test_proceed_validates_replacement_arguments_against_the_tool_schema() -> None:
    calls: list[int] = []
    tool = SafeTool()
    model = ScriptedModel(('safe', {'value': 1}, 's1'), ('lookup', {'value': 1}, 'p1'))
    invalid = DecisionHook(Proceed(arguments={'value': 'bad'}), Proceed(arguments={'value': 'bad'}))
    agent = await deferred_agent(model, (tool,), hooks=(invalid,), capabilities=(lookup_capability(calls),))
    valid_model = ScriptedModel(('lookup', {'value': 1}, 'p2'))
    valid = DecisionHook(Proceed(arguments={'value': 7}))
    valid_agent = await deferred_agent(valid_model, (), hooks=(valid,), capabilities=(lookup_capability(calls),))

    result = await agent.run('Use the tools.', deps=_deps())
    valid_result = await valid_agent.run('Look up.', deps=_deps())

    detail = 'value: Input should be a valid integer, unable to parse string as an integer'
    assert sorted(tool_returns(result.messages)) == [
        ('p1', 'denied', f"Invalid arguments for tool 'lookup':\n{detail}"),
        ('s1', 'denied', f"Invalid arguments for tool 'safe':\n{detail}"),
    ]
    assert tool.calls == []
    assert tool_returns(valid_result.messages) == [('p2', 'success', 'found 7')]
    assert calls == [7]


async def test_invalid_model_arguments_of_an_ovid_tool_skip_the_hooks_and_ask_for_a_retry() -> None:
    tool = SafeTool()
    hook = DecisionHook(Deny(message='blocked'))
    model = ScriptedModel(('safe', {'value': 'bad'}, 's1'))
    agent = await deferred_agent(model, (tool,), hooks=(hook,))

    result = await agent.run('Use the tool.', deps=_deps())

    detail = 'value: Input should be a valid integer, unable to parse string as an integer'
    assert tool_returns(result.messages) == [('s1', 'retried', f"Invalid arguments for tool 'safe':\n{detail}")]
    assert hook.calls == []
    assert tool.calls == []


async def test_raising_before_hook_denies_with_the_hook_name_and_stops_the_chain() -> None:
    tool = SafeTool()
    failing = DecisionHook(RuntimeError('secret detail'))
    later = DecisionHook()
    model = ScriptedModel(('safe', {'value': 1}, 's1'))
    agent = await deferred_agent(model, (tool,), hooks=(failing, later))

    result = await agent.run('Use the tool.', deps=_deps())

    assert tool_returns(result.messages) == [('s1', 'denied', 'Tool call denied: hook DecisionHook failed.')]
    assert 'secret detail' not in result.model_dump_json()
    assert tool.calls == []
    assert later.calls == []


async def test_cancellation_in_a_before_hook_is_not_caught() -> None:
    tool = SafeTool()
    model = ScriptedModel(('safe', {'value': 1}, 's1'))
    agent = await deferred_agent(model, (tool,), hooks=(DecisionHook(asyncio.CancelledError()),))

    with pytest.raises(asyncio.CancelledError):
        await agent.run('Use the tool.', deps=_deps())

    assert tool.calls == []


def test_tool_call_typed_validates_arguments_with_the_given_model() -> None:
    call = ToolCall(tool_name='safe', tool_call_id='s1', arguments={'value': 3}, source='ovid')

    assert call.typed(ValueArguments) == ValueArguments(value=3)
    with pytest.raises(ValidationError):
        call.model_copy(update={'arguments': {'value': 'bad'}}).typed(ValueArguments)
