import pytest
from pydantic_ai.exceptions import ApprovalRequired

from ovid_core import AgentRetryPolicy, AgentRunPolicy
from ovid_core.errors import AgentRunInputError
from ovid_core.runtime import AgentEvent, DeferredRunResult, RunResult, ToolDeferredEvent
from ovid_core.tools import (
    DeferredToolRequest,
    ToolApproval,
    ToolCallApproved,
    ToolCallDenied,
    ToolCallFailed,
    ToolCallReturned,
    ToolExecutionContext,
)
from tests.support.agent_consumer import AgentDependencies
from tests.support.deferred_consumer import (
    CheckTool,
    DangerTool,
    EventHook,
    ExternalTool,
    QuestionTool,
    RecordingTool,
    SafeTool,
    ScriptedModel,
    deferred_agent,
    tool_returns,
)


def _deps() -> AgentDependencies:
    return AgentDependencies(prefix='deferred')


async def test_static_approval_defers_with_request_and_stream_events() -> None:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'))
    danger = DangerTool()
    agent = await deferred_agent(model, (danger,))

    result = await agent.run('Delete.', deps=_deps())
    async with agent.stream('Delete.', deps=_deps()) as stream:
        events: list[AgentEvent] = [event async for event in stream]

    assert isinstance(result, DeferredRunResult)
    assert result.requests.calls == (
        DeferredToolRequest(
            tool_call_id='c1',
            tool_name='danger',
            arguments={'value': 1},
            kind='approval',
            approval=ToolApproval(required=True, reason='Deletes values', metadata={'risk': 'high'}),
        ),
    )
    assert result.messages[-1].role == 'response'
    assert DeferredRunResult.model_validate_json(result.model_dump_json()) == result
    assert danger.calls == []
    assert [event.kind for event in events][-4:] == ['tool_call', 'tool_deferred', 'usage_update', 'run_deferred']
    deferred = next(event for event in events if isinstance(event, ToolDeferredEvent))
    assert (deferred.tool_call_id, deferred.deferral_kind, deferred.reason) == ('c1', 'approval', 'Deletes values')
    assert stream.result.usage == events[-1].usage


async def test_approve_runs_tool_once_and_deny_returns_message() -> None:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'))
    danger = DangerTool()
    agent = await deferred_agent(model, (danger,))
    paused = await agent.run('Delete.', deps=_deps())
    assert isinstance(paused, DeferredRunResult)

    approved = await agent.run(
        resume=paused.requests.resume({'c1': ToolCallApproved()}),
        deps=_deps(),
        messages=paused.history,
    )
    denied = await agent.run(
        resume=paused.requests.resume({'c1': ToolCallDenied(message='no')}),
        deps=_deps(),
        messages=paused.history,
    )

    assert isinstance(approved, RunResult)
    assert approved.output == 'done'
    assert tool_returns(approved.messages) == [('c1', 'success', {'content': 'danger 1', 'metadata': {}})]
    assert approved.usage.tool_calls == 1
    assert isinstance(denied, RunResult)
    assert denied.output == 'done'
    assert tool_returns(denied.messages) == [('c1', 'denied', 'no')]
    assert danger.calls == [(1, True)]


async def test_resumed_stream_repeats_tool_call_before_answered_result() -> None:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'))
    agent = await deferred_agent(model, (DangerTool(),))
    paused = await agent.run('Delete.', deps=_deps())
    assert isinstance(paused, DeferredRunResult)

    async with agent.stream(
        resume=paused.requests.resume({'c1': ToolCallApproved()}),
        deps=_deps(),
        messages=paused.history,
    ) as stream:
        kinds = [event.kind async for event in stream]

    assert kinds[:3] == ['run_started', 'tool_call', 'tool_result']
    assert kinds[-2:] == ['usage_update', 'run_completed']
    assert isinstance(stream.result, RunResult)


async def test_tool_deferred_creates_external_and_question_requests() -> None:
    model = ScriptedModel(('external', {'value': 2}, 'e1'), ('ask', {'value': 3}, 'q1'))
    external = ExternalTool()
    agent = await deferred_agent(model, (external, QuestionTool()))
    paused = await agent.run('Fetch.', deps=_deps())
    assert isinstance(paused, DeferredRunResult)

    returned = await agent.run(
        resume=paused.requests.resume({'e1': ToolCallReturned(value={'rows': 4}), 'q1': ToolCallReturned(value='Bob')}),
        deps=_deps(),
        messages=paused.history,
    )
    failed = await agent.run(
        resume=paused.requests.resume({'e1': ToolCallFailed(message='offline'), 'q1': ToolCallReturned(value=None)}),
        deps=_deps(),
        messages=paused.history,
    )

    assert [(call.tool_call_id, call.kind, call.approval, call.metadata) for call in paused.requests.calls] == [
        ('e1', 'external', None, {'job': 7}),
        ('q1', 'question', None, None),
    ]
    assert isinstance(returned, RunResult)
    assert tool_returns(returned.messages) == [('e1', 'success', {'rows': 4}), ('q1', 'success', 'Bob')]
    assert isinstance(failed, RunResult)
    assert tool_returns(failed.messages)[0] == ('e1', 'failed', 'offline')
    assert external.calls == [(2, False)]


async def test_tool_approval_required_reruns_tool_as_approved_with_coherent_hooks() -> None:
    model = ScriptedModel(('check', {'value': 5}, 'k1'))
    check = CheckTool()
    hook = EventHook()
    agent = await deferred_agent(model, (check,), hooks=(hook,))
    paused = await agent.run('Check.', deps=_deps())
    assert isinstance(paused, DeferredRunResult)

    result = await agent.run(
        resume=paused.requests.resume({'k1': ToolCallApproved()}),
        deps=_deps(),
        messages=paused.history,
    )

    assert paused.requests.calls[0].kind == 'approval'
    assert paused.requests.calls[0].approval is None
    assert paused.requests.calls[0].metadata == {'limit': 10}
    assert isinstance(result, RunResult)
    assert tool_returns(result.messages) == [('k1', 'success', {'content': 'check 5', 'metadata': {}})]
    assert check.calls == [(5, False), (5, True)]
    assert hook.events == ['before:check:False', 'before:check:True', 'after:check:True']


@pytest.mark.parametrize('tool', [ExternalTool(), CheckTool()], ids=['tool-deferred', 'tool-approval-required'])
async def test_raise_failure_mode_passes_deferrals_through(tool: RecordingTool) -> None:
    model = ScriptedModel((tool.id, {'value': 1}, 'call-1'))
    hook = EventHook()
    policy = AgentRunPolicy(retries=AgentRetryPolicy(tool_failures='raise'))
    agent = await deferred_agent(model, (tool,), hooks=(hook,), policy=policy)

    result = await agent.run('Defer.', deps=_deps())

    assert isinstance(result, DeferredRunResult)
    assert [call.tool_call_id for call in result.requests.calls] == ['call-1']
    assert hook.events == [f'before:{tool.id}:False']


async def test_raise_failure_mode_passes_upstream_deferral_through() -> None:
    class UpstreamTool(RecordingTool):
        id = 'upstream'

        def defer(self, context: ToolExecutionContext[AgentDependencies]) -> None:
            del context
            raise ApprovalRequired(metadata={'source': 'upstream'})

    model = ScriptedModel(('upstream', {'value': 1}, 'u1'))
    policy = AgentRunPolicy(retries=AgentRetryPolicy(tool_failures='raise'))
    agent = await deferred_agent(model, (UpstreamTool(),), policy=policy)

    result = await agent.run('Defer.', deps=_deps())

    assert isinstance(result, DeferredRunResult)
    assert result.requests.calls[0].metadata == {'source': 'upstream'}


async def test_mixed_turn_runs_normal_tool_once() -> None:
    model = ScriptedModel(('safe', {'value': 1}, 's1'), ('danger', {'value': 2}, 'c1'))
    safe = SafeTool()
    danger = DangerTool()
    agent = await deferred_agent(model, (safe, danger))
    paused = await agent.run('Mix.', deps=_deps())
    assert isinstance(paused, DeferredRunResult)

    result = await agent.run(
        resume=paused.requests.resume({'c1': ToolCallApproved()}),
        deps=_deps(),
        messages=paused.history,
    )

    assert [call.tool_call_id for call in paused.requests.calls] == ['c1']
    assert paused.usage.tool_calls == 1
    assert isinstance(result, RunResult)
    assert safe.calls == [(1, False)]
    assert danger.calls == [(2, True)]


async def test_agent_override_still_disables_approval() -> None:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'))
    danger = DangerTool()
    agent = await deferred_agent(model, (danger,), tool_approval=ToolApproval(required=False))

    result = await agent.run('Delete.', deps=_deps())

    assert isinstance(result, RunResult)
    assert result.output == 'done'
    assert danger.calls == [(1, False)]


async def test_run_without_prompt_or_resume_fails_before_model_call() -> None:
    model = ScriptedModel()
    agent = await deferred_agent(model, ())

    with pytest.raises(AgentRunInputError, match='prompt or resume'):
        await agent.run(deps=_deps())
    with pytest.raises(AgentRunInputError, match='prompt or resume'):
        async with agent.stream(deps=_deps()):
            pass

    assert model.requests == []
