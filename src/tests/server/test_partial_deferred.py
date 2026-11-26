from collections.abc import AsyncIterator

from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCalls

from ovid_core import InMemoryConversationStore
from ovid_core.runtime import ConversationId
from ovid_core.server import AgentRunRequest, StdioRunRequest
from ovid_core.tools import ToolCallApproved
from tests.support.deferred_consumer import DangerTool, ScriptedModel
from tests.support.partial_consumer import registration, shape, stdio_session


_CONVERSATION_ID = ConversationId.new()


def _run(request_id: str, request: AgentRunRequest) -> StdioRunRequest:
    return StdioRunRequest(request_id=request_id, agent_id='writer', request=request)


async def test_deferred_run_storage_is_unchanged_and_partial_commit_keeps_pending_record() -> None:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'))

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        if any(isinstance(message, ModelResponse) for message in messages):
            raise ModelAPIError('partial', 'provider down')
        async for item in model.stream(messages, info):
            yield item

    store = InMemoryConversationStore()
    agent = await registration(stream, (DangerTool(),))
    prompt = AgentRunRequest(prompt='Delete.', conversation_id=_CONVERSATION_ID)

    deferred = await stdio_session(agent, store, _run('r1', prompt), 'r1')
    deferred_messages = await store.load(_CONVERSATION_ID)
    record = await store.load_deferred_requests(_CONVERSATION_ID)
    failed = await stdio_session(
        agent,
        store,
        _run('r2', AgentRunRequest(prompt='Next.', conversation_id=_CONVERSATION_ID)),
        'r2',
        _run('r3', AgentRunRequest(answers={'c1': ToolCallApproved()}, conversation_id=_CONVERSATION_ID)),
        'r3',
    )

    result = deferred[-1]['result']
    assert isinstance(result, dict)
    assert [message.model_dump(mode='json') for message in deferred_messages] == result['messages']
    assert record is not None
    assert record.model_dump(mode='json') == result['requests']
    assert await store.load_deferred_requests(_CONVERSATION_ID) == record
    stored = await store.load(_CONVERSATION_ID)
    assert stored[: len(deferred_messages)] == deferred_messages
    assert shape(stored[len(deferred_messages) :]) == [
        ('request', 'complete', ('return:c1:interrupted',)),
        ('request', 'complete', ('user_prompt:Next.',)),
        ('response', 'interrupted', ('text:[Run ended: failed: Provider request failed (unavailable)]',)),
    ]
    errors = [frame['error'] for frame in failed if frame['type'] == 'error']
    assert [error['code'] for error in errors if isinstance(error, dict)] == [
        'provider_error',
        'invalid_tool_answers',
    ]
