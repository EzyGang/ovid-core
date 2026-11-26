from collections.abc import AsyncIterator

from pydantic_ai.messages import INTERRUPTED_TOOL_RETURN_CONTENT, ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCalls

from ovid_core import InMemoryConversationStore
from ovid_core.runtime import ConversationId
from ovid_core.server import AgentRunRequest, StdioCancelRequest
from ovid_core.tools import ToolCallApproved
from tests.support.deferred_consumer import DangerTool, ScriptedModel, deferred_registration, tool_returns
from tests.support.journal_consumer import (
    ApprovedSnapshotTool,
    ReadSnapshotTool,
    SnapshotTool,
    StopAfterSaveStore,
    recovery_returns,
    run_request,
)
from tests.support.partial_consumer import WaitingTool, registration, shape, stdio_session, tool_call


_CONVERSATION_ID = ConversationId.new()
_UNKNOWN = (
    '[Recovery: {} may have executed before the process stopped; its effect is UNKNOWN. '
    'Inspect current state before retrying.]'
)


def _prompt(prompt: str) -> AgentRunRequest:
    return AgentRunRequest(prompt=prompt, conversation_id=_CONVERSATION_ID)


async def test_write_intent_without_settle_gets_one_unknown_result_on_first_load_only() -> None:
    store = InMemoryConversationStore()
    tool = SnapshotTool(store)
    model = ScriptedModel(('write', {'value': 1}, 'c1'), ('danger', {'value': 2}, 'c2'))
    agent = await deferred_registration(model, (tool, DangerTool()))

    await stdio_session(agent, store, run_request('r1', _prompt('Write.')), 'r1')
    disk = tool.snapshot
    assert disk is not None
    stopped = await disk.load_journal(_CONVERSATION_ID)
    await stdio_session(agent, disk, run_request('r2', _prompt('Again.')), 'r2')
    recovered = await disk.load(_CONVERSATION_ID)
    await stdio_session(agent, disk, run_request('r3', _prompt('More.')), 'r3')

    journal = await disk.load_journal(_CONVERSATION_ID)
    assert [(call.tool_call_id, call.status) for call in stopped.tool_calls] == [('c1', 'started')]
    assert [run.outcome for run in stopped.runs] == [None]
    assert shape(recovered) == [
        ('request', 'complete', ('user_prompt:Write.',)),
        ('response', 'complete', ('call:c1', 'call:c2')),
        ('request', 'complete', ('return:c1:interrupted', 'return:c2:interrupted')),
        ('request', 'complete', ('user_prompt:Again.',)),
        ('response', 'complete', ('text:done',)),
    ]
    expected = [('c1', _UNKNOWN.format('write')), ('c2', '[Recovery: danger did not run.]')]
    assert recovery_returns(recovered) == recovery_returns(await disk.load(_CONVERSATION_ID)) == expected
    assert [run.outcome for run in journal.runs] == ['lost', 'completed', 'completed']
    assert 'UNKNOWN' in str(model.requests[1])
    assert INTERRUPTED_TOOL_RETURN_CONTENT not in str(model.requests[1])


async def test_read_only_intent_without_settle_is_closed_as_not_run() -> None:
    store = InMemoryConversationStore()
    tool = ReadSnapshotTool(store)
    agent = await deferred_registration(ScriptedModel(('look', {'value': 1}, 'c1')), (tool,))

    await stdio_session(agent, store, run_request('r1', _prompt('Look.')), 'r1')
    disk = tool.snapshot
    assert disk is not None
    await stdio_session(agent, disk, run_request('r2', _prompt('Again.')), 'r2')

    assert recovery_returns(await disk.load(_CONVERSATION_ID)) == [('c1', '[Recovery: look did not run.]')]
    assert [run.outcome for run in (await disk.load_journal(_CONVERSATION_ID)).runs] == ['lost', 'completed']


async def test_pending_deferred_call_is_not_closed_as_orphan_and_resumes() -> None:
    store = StopAfterSaveStore()
    danger = DangerTool()
    agent = await deferred_registration(ScriptedModel(('danger', {'value': 1}, 'c1')), (danger,))

    await stdio_session(agent, store, run_request('r1', _prompt('Delete.')), 'r1')
    disk = store.snapshot
    assert disk is not None
    stopped = await disk.load_journal(_CONVERSATION_ID)
    answers = AgentRunRequest(answers={'c1': ToolCallApproved()}, conversation_id=_CONVERSATION_ID)
    frames = await stdio_session(agent, disk, run_request('r2', answers), 'r2')

    result = frames[-1]['result']
    assert isinstance(result, dict)
    assert result['output'] == 'done'
    assert [run.outcome for run in stopped.runs] == [None]
    assert danger.calls == [(1, True)]
    stored = await disk.load(_CONVERSATION_ID)
    assert recovery_returns(stored) == []
    assert tool_returns(stored) == [('c1', 'success', {'content': 'danger 1', 'metadata': {}})]
    assert [run.outcome for run in (await disk.load_journal(_CONVERSATION_ID)).runs] == ['lost', 'completed']


async def test_call_lost_while_its_approved_resume_runs_is_closed_as_unknown() -> None:
    store = InMemoryConversationStore()
    tool = ApprovedSnapshotTool(store)
    agent = await deferred_registration(ScriptedModel(('danger', {'value': 1}, 'c1')), (tool,))
    answers = AgentRunRequest(answers={'c1': ToolCallApproved()}, conversation_id=_CONVERSATION_ID)

    await stdio_session(agent, store, run_request('r1', _prompt('Delete.')), 'r1', run_request('r2', answers), 'r2')
    disk = tool.snapshot
    assert disk is not None
    frames = await stdio_session(
        agent, disk, run_request('r3', answers), 'r3', run_request('r4', _prompt('Next.')), 'r4'
    )

    errors = [frame['error'] for frame in frames if frame['type'] == 'error']
    assert [error['code'] for error in errors if isinstance(error, dict)] == ['invalid_tool_answers']
    assert recovery_returns(await disk.load(_CONVERSATION_ID)) == [('c1', _UNKNOWN.format('danger'))]
    assert await disk.load_deferred_requests(_CONVERSATION_ID) is None
    assert [run.outcome for run in (await disk.load_journal(_CONVERSATION_ID)).runs] == [
        'deferred',
        'lost',
        'completed',
    ]


async def test_run_closed_by_partial_commit_is_not_recovered_again() -> None:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del info
        if any(isinstance(message, ModelResponse) for message in messages):
            yield 'done'
            return
        yield tool_call('wait', 'c1')

    store = InMemoryConversationStore()
    tool = WaitingTool()
    agent = await registration(stream, (tool,))
    cancel = StdioCancelRequest(request_id='cancel', target_request_id='r1')

    await stdio_session(agent, store, run_request('r1', _prompt('Wait.')), tool.started, cancel, 'r1')
    closed = await store.load_journal(_CONVERSATION_ID)
    await stdio_session(agent, store, run_request('r2', _prompt('Again.')), 'r2')

    journal = await store.load_journal(_CONVERSATION_ID)
    assert [(call.tool_call_id, call.status, call.outcome) for call in closed.tool_calls] == [
        ('c1', 'failed', 'interrupted')
    ]
    assert [run.outcome for run in journal.runs] == ['cancelled', 'completed']
    assert recovery_returns(await store.load(_CONVERSATION_ID)) == []
