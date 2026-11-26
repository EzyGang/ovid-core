import asyncio
from typing import Any, cast

from pydantic import JsonValue, TypeAdapter
from pydantic_core import to_jsonable_python

from ovid_core.errors import DeferredToolAnswerError, NothingToResumeError, ResumeLimitError
from ovid_core.messages.models import AgentMessage, pending_tool_calls
from ovid_core.persistence import (
    ConversationHistoryStore,
    ConversationJournalStore,
    ConversationStore,
    DeferredToolRequestStore,
    RunJournal,
)
from ovid_core.recovery import close_orphaned_tool_calls, restart_count
from ovid_core.runtime.identifiers import ConversationId
from ovid_core.runtime.results import DeferredRunResult, PartialRunResult, RunResult, _with_ending
from ovid_core.server.errors import _DeferredToolsUnavailableError, _server_error_from_exception
from ovid_core.server.models import AgentRunRequest, AgentRunResponse
from ovid_core.tools.deferred import DeferredToolRequests, DeferredToolResume, InterruptedRunResume


_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)


async def _stored_resume(
    store: ConversationStore | None,
    conversation_id: ConversationId,
    messages: tuple[AgentMessage, ...],
    request: AgentRunRequest,
) -> DeferredToolResume | InterruptedRunResume | None:
    """Build resume input from the request and the stored pending calls.

    Client answers never supply call kinds or metadata.
    """
    if request.resume:
        return InterruptedRunResume()
    if request.answers is None:
        return None
    if not isinstance(store, DeferredToolRequestStore):
        raise _DeferredToolsUnavailableError

    record = await store.load_deferred_requests(conversation_id)
    pending = {call.tool_call_id for call in pending_tool_calls(messages)}
    calls = tuple(call for call in record.calls if call.tool_call_id in pending) if record is not None else ()
    if not calls or len(calls) != len(pending):
        raise DeferredToolAnswerError('The conversation has no stored pending tool calls to answer')

    return DeferredToolRequests(calls=calls).resume(request.answers)


async def _open_history(
    store: ConversationStore | None,
    conversation_id: ConversationId,
    *,
    admitted: bool,
    resume_limit: int | None,
) -> tuple[tuple[AgentMessage, ...], RunJournal | None]:
    """Load the history, and create the journal of an admitted run when the store has a journal.

    For an admitted run, the store first closes the runs that a stopped process left open.
    A run that is not admitted must not do this, because the open run of an active run is not lost.
    A resume request gives its restart limit. Its checks run before the recovery, which then keeps safe calls open.
    """
    if store is None:
        return (), None
    if not (admitted and isinstance(store, ConversationJournalStore)):
        messages = await store.load(conversation_id)
        if resume_limit is not None:
            await _check_resume(store, conversation_id, messages, restarts=0, limit=resume_limit)
        return messages, None

    journal = RunJournal(store=store, conversation_id=conversation_id)
    loaded = await store.load_journal(conversation_id)
    if resume_limit is not None:
        await _check_resume(store, conversation_id, loaded.messages, restarts=restart_count(loaded), limit=resume_limit)
    if all(run.outcome is not None for run in loaded.runs):
        return loaded.messages, journal

    pending = (
        await store.load_deferred_requests(conversation_id) if isinstance(store, DeferredToolRequestStore) else None
    )
    entries = close_orphaned_tool_calls(loaded, pending, replay_safe=resume_limit is not None)
    await store.append_entries(conversation_id, entries)

    return await store.load(conversation_id), journal


async def _check_resume(
    store: ConversationStore,
    conversation_id: ConversationId,
    messages: tuple[AgentMessage, ...],
    *,
    restarts: int,
    limit: int,
) -> None:
    """Refuse a resume when its runs stopped too many times in a row, or when stored pending calls need answers."""
    if restarts >= limit:
        raise ResumeLimitError(restarts)

    record = (
        await store.load_deferred_requests(conversation_id) if isinstance(store, DeferredToolRequestStore) else None
    )
    pending = {call.tool_call_id for call in pending_tool_calls(messages)}
    if record is not None and any(call.tool_call_id in pending for call in record.calls):
        raise NothingToResumeError('The conversation has pending tool calls. Send answers for them.')


async def _persist_result(
    store: ConversationStore | None,
    result: RunResult[Any] | DeferredRunResult,
    *,
    journal: RunJournal | None,
) -> None:
    """Commit the messages first, then save or clear the pending calls, so a lost second write fails closed.

    The journal entry that ends the run is the last write.
    """
    if store is None:
        return

    await _commit_messages(store, result, journal)
    if isinstance(store, DeferredToolRequestStore) and isinstance(result, DeferredRunResult):
        await store.save_deferred_requests(result.conversation_id, result.requests)
    elif isinstance(store, DeferredToolRequestStore):
        await store.clear_deferred_requests(result.conversation_id)
    if journal is not None:
        await journal.end('deferred' if isinstance(result, DeferredRunResult) else 'completed')


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


def _start_commit(
    store: ConversationStore | None,
    partial: PartialRunResult | None,
    *,
    journal: RunJournal | None,
) -> asyncio.Future[None] | None:
    """Start the commit of a partial transcript. The pending deferred requests stay unchanged."""
    if store is None or partial is None:
        return None

    return asyncio.ensure_future(_commit_partial(store, partial, journal))


async def _commit_partial(store: ConversationStore, partial: PartialRunResult, journal: RunJournal | None) -> None:
    await _commit_messages(store, partial, journal)
    if journal is not None:
        await journal.end(partial.outcome)


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
    journal: RunJournal | None,
) -> None:
    """Write the messages of the record that the step commits of the journal do not hold."""
    held = journal.messages if journal is not None else ()
    messages = tuple(message for message in record.messages if message not in held)
    if isinstance(store, ConversationHistoryStore) and record.history:
        await store.commit(record.conversation_id, messages, record.history)
    else:
        await store.append(record.conversation_id, messages)


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
