import asyncio

import pytest
from pydantic import JsonValue, TypeAdapter
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse
from pydantic_ai.messages import RetryPromptPart as PydanticRetryPromptPart
from pydantic_ai.messages import ToolReturnPart as PydanticToolReturnPart
from pytest_mock import MockerFixture

from ovid_core import InMemoryConversationStore, PersistenceError
from ovid_core.messages import AgentMessage, RetryPromptPart, ToolCallPart, ToolReturnPart
from ovid_core.persistence import ConversationStore
from ovid_core.runtime import ConversationId
from ovid_core.server import (
    AgentRunRequest,
    AuthorizationResult,
    RequestContext,
    StdioErrorResponse,
    StdioEventResponse,
    StdioRunRequest,
    create_stdio_server,
)
from ovid_core.tools import (
    DeferredToolAnswer,
    DeferredToolRequest,
    DeferredToolRequests,
    ToolCallApproved,
    ToolCallFailed,
    ToolCallReturned,
)
from tests.support.deferred_consumer import (
    DangerTool,
    ExternalTool,
    RecordingTool,
    ScriptedModel,
    deferred_registration,
    tool_returns,
)


_FRAME_ADAPTER = TypeAdapter(dict[str, JsonValue])
_CONVERSATION_ID = ConversationId.new()


class _MessageOnlyStore:
    def __init__(self) -> None:
        self.messages: tuple[AgentMessage, ...] = ()

    async def load(self, conversation_id: ConversationId) -> tuple[AgentMessage, ...]:
        del conversation_id
        return self.messages

    async def append(self, conversation_id: ConversationId, messages: tuple[AgentMessage, ...]) -> None:
        del conversation_id
        self.messages += messages


async def _authorize(_: RequestContext, __: str) -> AuthorizationResult:
    return AuthorizationResult(allowed=True, principal='stdio-user')


def _run(request_id: str, *, prompt: str | None = None, **answers: DeferredToolAnswer) -> StdioRunRequest:
    return StdioRunRequest(
        request_id=request_id,
        agent_id='writer',
        request=AgentRunRequest(prompt=prompt, answers=answers or None, conversation_id=_CONVERSATION_ID),
    )


async def _session(
    model: ScriptedModel,
    tools: tuple[RecordingTool, ...],
    store: ConversationStore | None,
    *requests: StdioRunRequest,
) -> dict[str, list[dict[str, JsonValue]]]:
    server = create_stdio_server(agents=(await deferred_registration(model, tools),), authorize=_authorize, store=store)
    incoming = iter(requests)
    frames: dict[str, list[dict[str, JsonValue]]] = {request.request_id: [] for request in requests}
    finished = asyncio.Event()

    async def read(_: int) -> bytes:
        request = next(incoming, None)
        if request is not None:
            return f'{request.model_dump_json()}\n'.encode()
        await finished.wait()
        return b''

    async def write(payload: bytes) -> None:
        frame = _FRAME_ADAPTER.validate_json(payload)
        frames[str(frame['request_id'])].append(frame)
        if frame['request_id'] == requests[-1].request_id and frame['type'] in ('run_result', 'error'):
            finished.set()

    async with asyncio.timeout(5):
        await server._serve(reader=read, writer=write)
    return frames


def _event_kinds(frames: list[dict[str, JsonValue]]) -> list[str]:
    return [StdioEventResponse.model_validate(frame).event.kind for frame in frames if frame['type'] == 'event']


def _error_code(frames: list[dict[str, JsonValue]]) -> str:
    return StdioErrorResponse.model_validate(frames[-1]).error.code


async def test_stdio_resumes_deferred_run_on_same_conversation() -> None:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'))
    danger = DangerTool()
    store = InMemoryConversationStore()

    frames = await _session(model, (danger,), store, _run('r1', prompt='Delete.'), _run('r2', c1=ToolCallApproved()))

    deferred = frames['r1'][-1]['result']
    completed = frames['r2'][-1]['result']
    assert _event_kinds(frames['r1'])[-3:] == ['tool_deferred', 'usage_update', 'run_deferred']
    assert isinstance(deferred, dict)
    assert deferred['output'] is None
    assert deferred['requests'] == {
        'calls': [
            {
                'tool_call_id': 'c1',
                'tool_name': 'danger',
                'arguments': {'value': 1},
                'kind': 'approval',
                'approval': {'required': True, 'reason': 'Deletes values', 'metadata': {'risk': 'high'}},
                'metadata': None,
            }
        ]
    }
    assert isinstance(completed, dict)
    assert completed['output'] == 'done'
    assert 'requests' not in completed
    assert _event_kinds(frames['r2'])[-2:] == ['usage_update', 'run_completed']
    assert danger.calls == [(1, True)]
    assert tool_returns(await store.load(_CONVERSATION_ID))[0][:2] == ('c1', 'success')
    assert await store.load_deferred_requests(_CONVERSATION_ID) is None


@pytest.mark.parametrize(
    'answers',
    [
        {'c1': ToolCallApproved(), 'e1': ToolCallFailed(message='down'), 'x9': ToolCallApproved()},
        {'c1': ToolCallReturned(value='forged'), 'e1': ToolCallFailed(message='down')},
        {'c1': ToolCallApproved(), 'e1': ToolCallApproved()},
        {'c1': ToolCallApproved()},
    ],
    ids=['not-pending', 'approval-as-result', 'external-as-approval', 'partial'],
)
async def test_stdio_rejects_invalid_answers_before_model_call(answers: dict[str, DeferredToolAnswer]) -> None:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'), ('external', {'value': 2}, 'e1'))
    danger = DangerTool()
    store = InMemoryConversationStore()

    frames = await _session(model, (danger, ExternalTool()), store, _run('r1', prompt='Go.'), _run('r2', **answers))

    assert [frame['type'] for frame in frames['r2']] == ['error']
    assert _error_code(frames['r2']) == 'invalid_tool_answers'
    assert len(model.requests) == 1
    assert danger.calls == []
    assert await store.load_deferred_requests(_CONVERSATION_ID) is not None


@pytest.mark.parametrize(
    ('store', 'code'),
    [
        (InMemoryConversationStore(), 'invalid_tool_answers'),
        (_MessageOnlyStore(), 'deferred_tools_unavailable'),
        (None, 'deferred_tools_unavailable'),
    ],
    ids=['no-record', 'store-without-protocol', 'no-store'],
)
async def test_stdio_resume_fails_closed_without_stored_record(store: ConversationStore | None, code: str) -> None:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'))

    frames = await _session(model, (DangerTool(),), store, _run('r1', c1=ToolCallApproved()))

    assert _error_code(frames['r1']) == code
    assert model.requests == []


async def test_stdio_new_prompt_interrupts_pending_calls_and_clears_record() -> None:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'))
    danger = DangerTool()
    store = InMemoryConversationStore()

    frames = await _session(model, (danger,), store, _run('r1', prompt='Delete.'), _run('r2', prompt='Stop.'))

    assert frames['r2'][-1]['type'] == 'run_result'
    assert tool_returns(await store.load(_CONVERSATION_ID))[0][:2] == ('c1', 'interrupted')
    assert await store.load_deferred_requests(_CONVERSATION_ID) is None
    assert danger.calls == []


async def test_stdio_lost_record_write_rejects_resume_and_accepts_new_prompt(mocker: MockerFixture) -> None:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'))
    danger = DangerTool()
    store = InMemoryConversationStore()
    mocker.patch.object(store, 'save_deferred_requests', side_effect=PersistenceError('disk full'))

    frames = await _session(
        model,
        (danger,),
        store,
        _run('r1', prompt='Delete.'),
        _run('r2', c1=ToolCallApproved()),
        _run('r3', prompt='Stop.'),
    )

    assert frames['r1'][-1]['type'] == 'error'
    assert _error_code(frames['r2']) == 'invalid_tool_answers'
    assert frames['r3'][-1]['type'] == 'run_result'
    assert danger.calls == []


async def test_stdio_record_without_messages_rejects_resume_and_new_prompt_clears_it() -> None:
    model = ScriptedModel()
    store = InMemoryConversationStore()
    stale = DeferredToolRequests(
        calls=(DeferredToolRequest(tool_call_id='c1', tool_name='danger', arguments={'value': 1}, kind='approval'),)
    )
    await store.save_deferred_requests(_CONVERSATION_ID, stale)

    frames = await _session(model, (DangerTool(),), store, _run('r1', c1=ToolCallApproved()), _run('r2', prompt='Hi.'))

    assert _error_code(frames['r1']) == 'invalid_tool_answers'
    assert frames['r2'][-1]['type'] == 'run_result'
    assert len(model.requests) == 1
    assert await store.load_deferred_requests(_CONVERSATION_ID) is None


def _unanswered_model_calls(messages: list[ModelMessage]) -> list[str]:
    return [
        call.tool_call_id
        for index, message in enumerate(messages)
        if isinstance(message, ModelResponse)
        for call in message.tool_calls
        if not any(
            isinstance(part, PydanticToolReturnPart | PydanticRetryPromptPart)
            and part.tool_call_id == call.tool_call_id
            for later in messages[index + 1 :]
            if isinstance(later, ModelRequest)
            for part in later.parts
        )
    ]


def _unanswered_stored_calls(messages: tuple[AgentMessage, ...]) -> list[str]:
    return [
        call.tool_call_id
        for index, message in enumerate(messages)
        for call in message.parts
        if isinstance(call, ToolCallPart)
        and not any(
            isinstance(part, ToolReturnPart | RetryPromptPart) and part.tool_call_id == call.tool_call_id
            for later in messages[index + 1 :]
            for part in later.parts
        )
    ]


@pytest.mark.parametrize(
    'store',
    [_MessageOnlyStore(), InMemoryConversationStore()],
    ids=['append-only', 'in-memory'],
)
async def test_stdio_interrupted_calls_persist_for_every_store(
    store: _MessageOnlyStore | InMemoryConversationStore,
) -> None:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'))
    danger = DangerTool()

    frames = await _session(
        model,
        (danger,),
        store,
        _run('r1', prompt='Delete.'),
        _run('r2', prompt='Stop.'),
        _run('r3', prompt='Continue.'),
    )
    stored = await store.load(_CONVERSATION_ID)

    assert [frames[request_id][-1]['type'] for request_id in ('r1', 'r2', 'r3')] == ['run_result'] * 3
    assert _unanswered_model_calls(model.requests[-1]) == []
    assert _unanswered_stored_calls(stored) == []
    assert [(call_id, outcome) for call_id, outcome, _ in tool_returns(stored)] == [('c1', 'interrupted')]
    assert danger.calls == []
