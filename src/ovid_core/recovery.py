from datetime import UTC, datetime
from itertools import takewhile

from ovid_core.journal import ConversationJournal, JournalEntry, JournalToolCall, RunEndedEntry, StepCommittedEntry
from ovid_core.messages.models import AgentMessage, RetryPromptPart, ToolCallPart, ToolReturnPart, pending_tool_calls
from ovid_core.runtime.identifiers import RunId
from ovid_core.tools.deferred import DeferredToolRequests


def close_orphaned_tool_calls(
    journal: ConversationJournal,
    pending: DeferredToolRequests | None = None,
    *,
    replay_safe: bool = False,
) -> tuple[JournalEntry, ...]:
    """Return the entries that close each run that a stopped process left open.

    An open run gets an `interrupted` result for each tool call that it left without a result, then the outcome `lost`.
    A pending deferred call stays open for its answer, unless an open run started to execute it.
    With `replay_safe`, a started call of the last response stays open when its intent is safe to replay.
    """
    open_runs = tuple(run.run_id for run in journal.runs if run.outcome is None)
    intents = {call.tool_call_id: call for call in journal.tool_calls}
    waiting = {call.tool_call_id for call in pending.calls} if pending is not None else set()
    replayable = _replayable_calls(journal) if replay_safe else set()
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
        if isinstance(part, ToolCallPart) and part.tool_call_id not in answered and part.tool_call_id not in replayable
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


def restart_count(journal: ConversationJournal) -> int:
    """Count the runs that stopped in a row after the last run that ended normally.

    An open run counts, because the next admitted load closes it as `lost`.
    A cancelled run does not count and does not end the series.
    """
    runs = (run for run in reversed(journal.runs) if run.outcome != 'cancelled')

    return sum(1 for _ in takewhile(lambda run: run.outcome not in ('completed', 'deferred'), runs))


def unknown_effect_text(tool_name: str) -> str:
    """Return the result of a call that may have executed before the process stopped."""
    return (
        f'[Recovery: {tool_name} may have executed before the process stopped; its effect is UNKNOWN. '
        'Inspect current state before retrying.]'
    )


def _replayable_calls(journal: ConversationJournal) -> set[str]:
    """Return the started calls of the last response whose intent is safe to replay."""
    tail = {call.tool_call_id for call in pending_tool_calls(journal.messages)}

    return {
        call.tool_call_id
        for call in journal.tool_calls
        if call.tool_call_id in tail and call.status == 'started' and call.effect.replay == 'safe'
    }


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
        content = unknown_effect_text(call.tool_name)

    return ToolReturnPart(
        tool_name=call.tool_name,
        content=content,
        tool_call_id=call.tool_call_id,
        outcome='interrupted',
    )
