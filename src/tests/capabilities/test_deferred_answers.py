from collections.abc import Mapping

import pytest

from ovid_core import InMemoryConversationStore
from ovid_core.capabilities import DeferredToolAnswerCapability
from ovid_core.errors import DeferredToolAnswerError
from ovid_core.runtime import ConversationId, RunId, RunResult
from ovid_core.server import AgentRegistration, AgentRunRequest, StdioRunRequest
from ovid_core.tools import DeferredToolAnswer, DeferredToolRequests, ToolCallApproved, ToolCallDenied, ToolCallReturned
from tests.server.server_helpers import dependencies
from tests.support.agent_consumer import AgentDependencies
from tests.support.deferred_consumer import (
    CheckTool,
    DangerTool,
    ExternalTool,
    RecordingTool,
    ScriptedModel,
    deferred_agent,
    tool_returns,
)
from tests.support.journal_consumer import PlainHistoryStore, TranscriptStore, run_request
from tests.support.partial_consumer import shape, stdio_session


_DEPS = AgentDependencies(prefix='answers')
_CONVERSATION_ID = ConversationId.new()


class FixedAnswers:
    def __init__(self, answers: Mapping[str, DeferredToolAnswer]) -> None:
        self.answers = answers
        self.seen: list[tuple[DeferredToolRequests, ConversationId, RunId]] = []

    async def __call__(
        self,
        requests: DeferredToolRequests,
        *,
        conversation_id: ConversationId,
        run_id: RunId,
    ) -> Mapping[str, DeferredToolAnswer]:
        self.seen.append((requests, conversation_id, run_id))
        return self.answers


def _capability(answerer: FixedAnswers) -> DeferredToolAnswerCapability[AgentDependencies]:
    return DeferredToolAnswerCapability[AgentDependencies](id='answers', answerer=answerer)


async def _registration(
    tools: tuple[RecordingTool, ...],
    answers: Mapping[str, DeferredToolAnswer],
) -> AgentRegistration[AgentDependencies, str]:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'), ('external', {'value': 2}, 'e1'))
    agent = await deferred_agent(model, tools, capabilities=(_capability(FixedAnswers(answers)),))

    return AgentRegistration(id='writer', description='Write with tools.', agent=agent, dependencies=dependencies)


def _run(request_id: str, *, prompt: str | None = None, **answers: DeferredToolAnswer) -> StdioRunRequest:
    request = AgentRunRequest(prompt=prompt, answers=answers or None, conversation_id=_CONVERSATION_ID)

    return run_request(request_id, request)


async def test_answers_for_every_call_complete_the_run() -> None:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'), ('check', {'value': 2}, 'c2'))
    danger = DangerTool()
    check = CheckTool()
    answerer = FixedAnswers({'c1': ToolCallApproved(), 'c2': ToolCallDenied(message='no')})
    agent = await deferred_agent(model, (danger, check), capabilities=(_capability(answerer),))

    result = await agent.run('Go.', deps=_DEPS)

    assert isinstance(result, RunResult)
    assert result.output == 'done'
    assert danger.calls == [(1, True)]
    assert check.calls == [(2, False)]
    assert sorted(tool_returns(result.messages)) == [
        ('c1', 'success', 'danger 1'),
        ('c2', 'denied', 'no'),
    ]
    [(requests, conversation_id, run_id)] = answerer.seen
    assert {call.tool_call_id: (call.kind, call.approval) for call in requests.calls} == {
        'c1': ('approval', DangerTool.approval),
        'c2': ('approval', None),
    }
    assert (conversation_id, run_id) == (result.conversation_id, result.run_id)


async def test_partial_answers_defer_only_unanswered_calls_until_stdio_resume() -> None:
    danger = DangerTool()
    store = InMemoryConversationStore()
    registration = await _registration((danger, ExternalTool()), {'c1': ToolCallApproved()})

    frames = await stdio_session(
        registration,
        store,
        _run('r1', prompt='Go.'),
        'r1',
        _run('r2', e1=ToolCallReturned(value=5)),
        'r2',
    )

    deferred, completed = (frame['result'] for frame in frames if frame['type'] == 'run_result')
    assert isinstance(deferred, dict)
    assert deferred['requests'] == {
        'calls': [
            {
                'tool_call_id': 'e1',
                'tool_name': 'external',
                'arguments': {'value': 2},
                'kind': 'external',
                'approval': None,
                'metadata': {'job': 7},
            }
        ]
    }
    assert isinstance(completed, dict)
    assert completed['output'] == 'done'
    assert danger.calls == [(1, True)]
    assert tool_returns(await store.load(_CONVERSATION_ID)) == [
        ('c1', 'success', 'danger 1'),
        ('e1', 'success', 5),
    ]
    assert await store.load_deferred_requests(_CONVERSATION_ID) is None


@pytest.mark.parametrize(
    ('answers', 'outcome', 'pending'),
    [
        ({'c1': ToolCallApproved(), 'e1': ToolCallReturned(value=5)}, 'completed', ()),
        ({}, 'deferred', ('c1', 'e1')),
    ],
    ids=['all-answered', 'none-answered'],
)
async def test_inline_answers_store_each_message_once_with_journal_and_pending_record(
    answers: Mapping[str, DeferredToolAnswer],
    outcome: str,
    pending: tuple[str, ...],
) -> None:
    store = TranscriptStore()
    plain = PlainHistoryStore()

    for target in (store, plain):
        registration = await _registration((DangerTool(), ExternalTool()), answers)
        await stdio_session(registration, target, _run('r1', prompt='Go.'), 'r1')

    stored = await store.load(_CONVERSATION_ID)
    record = await store.load_deferred_requests(_CONVERSATION_ID)
    recorded = record.calls if record is not None else ()
    assert store.transcript == stored
    assert shape(stored) == shape(plain.messages)
    assert [run.outcome for run in (await store.load_journal(_CONVERSATION_ID)).runs] == [outcome]
    assert tuple(call.tool_call_id for call in recorded) == pending


@pytest.mark.parametrize(
    ('answers', 'message'),
    [
        ({'c1': ToolCallApproved(), 'x9': ToolCallApproved()}, 'not pending'),
        ({'c1': ToolCallReturned(value='forged')}, 'do not match'),
    ],
    ids=['unknown-id', 'wrong-kind'],
)
async def test_invalid_inline_answers_raise_typed_error(
    answers: Mapping[str, DeferredToolAnswer], message: str
) -> None:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'))
    danger = DangerTool()
    agent = await deferred_agent(model, (danger,), capabilities=(_capability(FixedAnswers(answers)),))

    with pytest.raises(DeferredToolAnswerError, match=message):
        await agent.run('Go.', deps=_DEPS)

    assert danger.calls == []
