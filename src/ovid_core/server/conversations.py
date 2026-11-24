from collections.abc import Mapping
from typing import Any

from pydantic import JsonValue, TypeAdapter
from pydantic_core import to_jsonable_python

from ovid_core.errors import DeferredToolAnswerError
from ovid_core.messages.models import AgentMessage, pending_tool_calls
from ovid_core.persistence import ConversationHistoryStore, ConversationStore, DeferredToolRequestStore
from ovid_core.runtime.identifiers import ConversationId
from ovid_core.runtime.results import DeferredRunResult, RunResult
from ovid_core.server.errors import _DeferredToolsUnavailableError
from ovid_core.server.models import AgentRunResponse
from ovid_core.tools.deferred import DeferredToolAnswer, DeferredToolRequests, DeferredToolResume


_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)


async def _stored_resume(
    store: ConversationStore | None,
    conversation_id: ConversationId,
    messages: tuple[AgentMessage, ...],
    answers: Mapping[str, DeferredToolAnswer] | None,
) -> DeferredToolResume | None:
    """Build resume input from the stored pending calls. Client answers never supply call kinds or metadata."""
    if answers is None:
        return None
    if not isinstance(store, DeferredToolRequestStore):
        raise _DeferredToolsUnavailableError

    record = await store.load_deferred_requests(conversation_id)
    pending = {call.tool_call_id for call in pending_tool_calls(messages)}
    calls = tuple(call for call in record.calls if call.tool_call_id in pending) if record is not None else ()
    if not calls or len(calls) != len(pending):
        raise DeferredToolAnswerError('The conversation has no stored pending tool calls to answer')

    return DeferredToolRequests(calls=calls).resume(answers)


async def _persist_result(store: ConversationStore | None, result: RunResult[Any] | DeferredRunResult) -> None:
    """Commit the messages first, then save or clear the pending calls, so a lost second write fails closed."""
    if isinstance(store, ConversationHistoryStore) and result.history:
        await store.commit(result.conversation_id, result.messages, result.history)
    elif store is not None:
        await store.append(result.conversation_id, result.messages)

    if not isinstance(store, DeferredToolRequestStore):
        return
    if isinstance(result, DeferredRunResult):
        await store.save_deferred_requests(result.conversation_id, result.requests)
    else:
        await store.clear_deferred_requests(result.conversation_id)


def _response_from_result(result: RunResult[Any] | DeferredRunResult) -> AgentRunResponse:
    output: JsonValue = None
    requests: DeferredToolRequests | None = None
    if isinstance(result, DeferredRunResult):
        requests = result.requests
    else:
        output = _JSON_VALUE_ADAPTER.validate_python(to_jsonable_python(result.output))

    return AgentRunResponse(
        output=output,
        messages=result.messages,
        usage=result.usage,
        run_id=result.run_id,
        conversation_id=result.conversation_id,
        requests=requests,
    )
