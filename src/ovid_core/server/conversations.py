import asyncio
from collections.abc import Mapping
from typing import Any, cast

from pydantic import JsonValue, TypeAdapter
from pydantic_core import to_jsonable_python

from ovid_core.errors import DeferredToolAnswerError
from ovid_core.messages.models import AgentMessage, pending_tool_calls
from ovid_core.persistence import ConversationHistoryStore, ConversationStore, DeferredToolRequestStore
from ovid_core.runtime.identifiers import ConversationId
from ovid_core.runtime.results import DeferredRunResult, PartialRunResult, RunResult, _with_ending
from ovid_core.server.errors import _DeferredToolsUnavailableError, _server_error_from_exception
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
    if store is None:
        return

    await _commit_messages(store, result)
    if not isinstance(store, DeferredToolRequestStore):
        return
    if isinstance(result, DeferredRunResult):
        await store.save_deferred_requests(result.conversation_id, result.requests)
    else:
        await store.clear_deferred_requests(result.conversation_id)


def _stopped_partial(
    partial: PartialRunResult | None,
    error: BaseException,
    deadline: asyncio.Timeout,
) -> PartialRunResult | None:
    """Label the partial with the error that the server reports when its request timeout cancelled the run."""
    if partial is None or not (isinstance(error, asyncio.CancelledError) and deadline.expired()):
        return partial

    failure = _server_error_from_exception(TimeoutError())

    return _with_ending(partial, outcome='timeout', error_type=TimeoutError.__name__, message=failure.message)


def _start_commit(store: ConversationStore | None, partial: PartialRunResult | None) -> asyncio.Future[None] | None:
    """Start the commit of a partial transcript. The pending deferred requests stay unchanged."""
    if store is None or partial is None:
        return None

    return asyncio.ensure_future(_commit_messages(store, partial))


async def _wait_for_commit(commit: asyncio.Future[None] | None, timeout_seconds: float) -> bool:
    """Wait at most the timeout for a partial commit, and return whether it finished without an error.

    A cancellation does not stop the wait. The commit keeps running after the timeout.
    """
    if commit is None:
        return False

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while not commit.done() and loop.time() < deadline:
        try:
            await asyncio.wait((commit,), timeout=deadline - loop.time())
        except asyncio.CancelledError:
            cast(asyncio.Task[Any], asyncio.current_task()).uncancel()

    return commit.done() and commit.exception() is None


async def _commit_messages(
    store: ConversationStore,
    record: RunResult[Any] | DeferredRunResult | PartialRunResult,
) -> None:
    if isinstance(store, ConversationHistoryStore) and record.history:
        await store.commit(record.conversation_id, record.messages, record.history)
    else:
        await store.append(record.conversation_id, record.messages)


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
