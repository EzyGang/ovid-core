from datetime import UTC, datetime

from ovid_core.journal import ConversationJournal, JournalEntry, JournalToolCall, RunEndedEntry, StepCommittedEntry
from ovid_core.messages.models import AgentMessage, RetryPromptPart, ToolCallPart, ToolReturnPart
from ovid_core.runtime.identifiers import RunId
from ovid_core.tools.deferred import DeferredToolRequests


def close_orphaned_tool_calls(
    journal: ConversationJournal,
    pending: DeferredToolRequests | None = None,
) -> tuple[JournalEntry, ...]:
    """Return the entries that close each run that a stopped process left open.

    An open run gets one request with an `interrupted` result for each tool call that it left without a result.
    Then the run gets the outcome `lost`. A pending deferred call waits for its answer and stays open,
    unless an open run started to execute it.
    """
    open_runs = tuple(run.run_id for run in journal.runs if run.outcome is None)
    intents = {call.tool_call_id: call for call in journal.tool_calls}
    waiting = {call.tool_call_id for call in pending.calls} if pending is not None else set()
    answered = {
        part.tool_call_id
        for message in journal.messages
        for part in message.parts
        if isinstance(part, ToolReturnPart | RetryPromptPart)
    }
    orphans = tuple(
        (owner, _recovery_return(part, intents.get(part.tool_call_id)))
        for message in journal.messages
        for part in message.parts
        if isinstance(part, ToolCallPart) and part.tool_call_id not in answered
        if (owner := _owner(part, message.run_id, intents.get(part.tool_call_id), open_runs, waiting)) is not None
    )

    entries: list[JournalEntry] = []
    for run_id in open_runs:
        returns = tuple(result for owner, result in orphans if owner == run_id)
        if returns:
            request = AgentMessage(role='request', parts=returns, run_id=run_id, timestamp=datetime.now(UTC))
            entries.append(StepCommittedEntry(run_id=run_id, messages=(request,)))
        entries.append(RunEndedEntry(run_id=run_id, outcome='lost'))

    return tuple(entries)


def _owner(
    call: ToolCallPart,
    message_run: RunId | None,
    intent: JournalToolCall | None,
    open_runs: tuple[RunId, ...],
    waiting: set[str],
) -> RunId | None:
    """Return the open run that left the call without a result, or `None` when the call is not an orphan."""
    if intent is not None and intent.run_id in open_runs and intent.status != 'deferred':
        return intent.run_id
    if message_run in open_runs and call.tool_call_id not in waiting:
        return message_run

    return None


def _recovery_return(call: ToolCallPart, intent: JournalToolCall | None) -> ToolReturnPart:
    content = f'[Recovery: {call.tool_name} did not run.]'
    if intent is not None and intent.effect.kind != 'read':
        content = (
            f'[Recovery: {call.tool_name} may have executed before the process stopped; its effect is UNKNOWN. '
            'Inspect current state before retrying.]'
        )

    return ToolReturnPart(
        tool_name=call.tool_name,
        content=content,
        tool_call_id=call.tool_call_id,
        outcome='interrupted',
    )
