from datetime import UTC, datetime

import pytest

from ovid_core import AgentRunInputError, NothingToResumeError
from ovid_core.messages import AgentMessage, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from ovid_core.tools import InterruptedRunResume, ToolResult
from ovid_core.usage import RequestUsage
from tests.support.agent_consumer import AgentDependencies
from tests.support.deferred_consumer import ScriptedModel, deferred_agent
from tests.support.hook_consumer import DecisionHook, RecoveryHook, ResultHook
from tests.support.resume_consumer import UNKNOWN, LookTool, WriteTool


_DEPS = AgentDependencies(prefix='resume')


def _history(provider_name: str) -> tuple[AgentMessage, ...]:
    now = datetime.now(UTC)
    request = AgentMessage(role='request', parts=(UserPromptPart(content='Go.'),), timestamp=now)
    ending = AgentMessage(
        role='response',
        parts=(TextPart(content='[Run ended: failed: Agent run failed]'),),
        timestamp=now,
        request_usage=RequestUsage(),
        provider_name=provider_name,
        finish_reason='error',
        state='interrupted',
    )

    return request, ending


async def test_resume_drops_boundary_and_sends_the_stored_request_without_a_new_prompt() -> None:
    model = ScriptedModel()
    agent = await deferred_agent(model, ())

    result = await agent.run(resume=InterruptedRunResume(), deps=_DEPS, messages=_history('ovid'))

    assert result.output == 'done'
    assert '[Run ended' not in str(model.requests[-1])
    assert sum(isinstance(part, UserPromptPart) for message in result.history for part in message.parts) == 1


async def test_resume_rejects_a_prompt_and_a_history_without_an_interrupted_run() -> None:
    agent = await deferred_agent(ScriptedModel(), ())

    with pytest.raises(AgentRunInputError, match='not both for an interrupted run'):
        await agent.run('Go.', resume=InterruptedRunResume(), deps=_DEPS, messages=_history('ovid'))
    with pytest.raises(NothingToResumeError, match='no interrupted run'):
        await agent.run(resume=InterruptedRunResume(), deps=_DEPS)
    with pytest.raises(NothingToResumeError, match='no interrupted run'):
        await agent.run(resume=InterruptedRunResume(), deps=_DEPS, messages=_history('provider'))


async def test_stored_answers_never_run_again_and_keep_their_kind_when_upstream_can_reproduce_it() -> None:
    now = datetime.now(UTC)
    stored = (
        RetryPromptPart(content='Try again.', tool_name='write', tool_call_id='c2'),
        ToolReturnPart(tool_name='write', content='Try later.', tool_call_id='c3', outcome='retried'),
        ToolReturnPart(tool_name='write', content='Write failed.', tool_call_id='c4', outcome='failed'),
        ToolReturnPart(tool_name='write', content='Not allowed.', tool_call_id='c5', outcome='denied'),
        ToolReturnPart(tool_name='write', content='written', tool_call_id='c6', metadata={'lines': 3}),
        ToolReturnPart(tool_name='write', content='Stopped.', tool_call_id='c7', outcome='interrupted'),
        ToolReturnPart(tool_name='write', content='Failed.', tool_call_id='c8', outcome='failed', metadata={'a': 1}),
    )
    calls = (ToolCallPart(tool_name='look', arguments={'value': 1}, tool_call_id='c1'),) + tuple(
        ToolCallPart(tool_name='write', arguments={'value': 2}, tool_call_id=part.tool_call_id) for part in stored
    )
    history = (
        AgentMessage(role='request', parts=(UserPromptPart(content='Go.'),), timestamp=now),
        AgentMessage(role='response', parts=calls, timestamp=now, request_usage=RequestUsage()),
        AgentMessage(role='request', parts=stored, timestamp=now),
    )
    look, write = LookTool(), WriteTool()
    agent = await deferred_agent(ScriptedModel(), (look, write))

    result = await agent.run(resume=InterruptedRunResume(), deps=_DEPS, messages=history)

    answers = {
        part.tool_call_id: (part.outcome, part.content, part.metadata)
        for part in result.history[-2].parts
        if isinstance(part, ToolReturnPart)
    }
    assert result.output == 'done'
    assert look.calls == [(1, False)]
    assert write.calls == []
    assert answers == {
        'c1': ('success', 'look 1', None),
        'c2': ('retried', 'Try again.', None),
        'c3': ('retried', 'Try later.', None),
        'c4': ('failed', 'Write failed.', None),
        'c5': ('denied', 'Not allowed.', None),
        'c6': ('success', 'written', {'lines': 3}),
        'c7': ('success', UNKNOWN.format('write'), None),
        'c8': ('success', UNKNOWN.format('write'), None),
    }


async def test_only_a_replayed_call_that_runs_reaches_the_agent_hooks() -> None:
    now = datetime.now(UTC)
    stored = (
        ToolReturnPart(tool_name='write', content='Write failed.', tool_call_id='c1', outcome='failed'),
        ToolReturnPart(tool_name='write', content='written', tool_call_id='c2'),
    )
    calls = (
        ToolCallPart(tool_name='write', arguments={'value': 1}, tool_call_id='c1'),
        ToolCallPart(tool_name='write', arguments={'value': 2}, tool_call_id='c2'),
        ToolCallPart(tool_name='look', arguments={'value': 3}, tool_call_id='c3'),
        ToolCallPart(tool_name='write', arguments={'value': 4}, tool_call_id='c4'),
    )
    history = (
        AgentMessage(role='request', parts=(UserPromptPart(content='Go.'),), timestamp=now),
        AgentMessage(role='response', parts=calls, timestamp=now, request_usage=RequestUsage()),
        AgentMessage(role='request', parts=stored, timestamp=now),
    )
    look, write = LookTool(), WriteTool()
    decisions = DecisionHook[AgentDependencies]()
    results = ResultHook(lambda result: None)
    recovery = RecoveryHook(lambda error: ToolResult(content='recovered'))
    agent = await deferred_agent(ScriptedModel(), (look, write), hooks=(decisions, results, recovery))

    result = await agent.run(resume=InterruptedRunResume(), deps=_DEPS, messages=history)

    answers = {
        part.tool_call_id: (part.outcome, part.content)
        for part in result.history[-2].parts
        if isinstance(part, ToolReturnPart)
    }
    assert answers == {
        'c1': ('failed', 'Write failed.'),
        'c2': ('success', 'written'),
        'c3': ('success', 'look 3'),
        'c4': ('success', UNKNOWN.format('write')),
    }
    assert [call.tool_call_id for call in decisions.calls] == ['c3']
    assert results.results == [ToolResult(content='look 3')]
    assert recovery.errors == []
    assert (look.calls, write.calls) == ([(3, False)], [])
