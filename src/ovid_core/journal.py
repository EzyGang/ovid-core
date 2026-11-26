from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import Field, JsonValue

from ovid_core.messages.models import AgentMessage
from ovid_core.models import BaseModel
from ovid_core.runtime.identifiers import RunId
from ovid_core.runtime.results import RunOutcome
from ovid_core.tools.models import ToolEffect


type ToolSettledStatus = Literal['completed', 'failed', 'deferred']
type ToolSettledOutcome = Literal['success', 'failed', 'retried', 'interrupted']
type RunEndOutcome = RunOutcome | Literal['completed', 'deferred', 'lost']


class RunStartedEntry(BaseModel):
    kind: Literal['run_started'] = 'run_started'
    run_id: RunId
    at: datetime


class StepCommittedEntry(BaseModel):
    kind: Literal['step_committed'] = 'step_committed'
    run_id: RunId
    messages: tuple[AgentMessage, ...] = Field(min_length=1)


class _ToolCallIntent(BaseModel):
    run_id: RunId
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]
    effect: ToolEffect
    idempotency_key: str | None = None
    started_at: datetime


class ToolIntentEntry(_ToolCallIntent):
    kind: Literal['tool_intent'] = 'tool_intent'


class ToolSettledEntry(BaseModel):
    """The end of a tool call. A deferred call has no outcome because the model receives no result for it yet."""

    kind: Literal['tool_settled'] = 'tool_settled'
    run_id: RunId
    tool_call_id: str = Field(min_length=1)
    status: ToolSettledStatus
    outcome: ToolSettledOutcome | None
    ended_at: datetime


class RunEndedEntry(BaseModel):
    kind: Literal['run_ended'] = 'run_ended'
    run_id: RunId
    outcome: RunEndOutcome


JournalEntry = Annotated[
    RunStartedEntry | StepCommittedEntry | ToolIntentEntry | ToolSettledEntry | RunEndedEntry,
    Field(discriminator='kind'),
]


class JournalRun(BaseModel):
    """A run in the journal. A run without an outcome is open."""

    run_id: RunId
    started_at: datetime
    outcome: RunEndOutcome | None = None


class JournalToolCall(_ToolCallIntent):
    """A tool intent and its settle record. The status `started` means that no settle record exists."""

    status: Literal['started'] | ToolSettledStatus = 'started'
    outcome: ToolSettledOutcome | None = None
    ended_at: datetime | None = None


class ConversationJournal(BaseModel):
    messages: tuple[AgentMessage, ...]
    runs: tuple[JournalRun, ...] = ()
    tool_calls: tuple[JournalToolCall, ...] = ()

    @classmethod
    def from_entries(cls, messages: tuple[AgentMessage, ...], entries: Sequence[JournalEntry]) -> Self:
        """Build the journal from the stored history and the entries in append order.

        The history must already contain the messages of every step commit.
        """
        starts: list[RunStartedEntry] = []
        intents: list[ToolIntentEntry] = []
        outcomes: dict[RunId, RunEndOutcome] = {}
        settles: dict[tuple[RunId, str], dict[str, Any]] = {}
        for entry in entries:
            match entry:
                case RunStartedEntry():
                    starts.append(entry)
                case ToolIntentEntry():
                    intents.append(entry)
                case ToolSettledEntry():
                    settles[entry.run_id, entry.tool_call_id] = entry.model_dump(
                        include={'status', 'outcome', 'ended_at'}
                    )
                case RunEndedEntry():
                    outcomes[entry.run_id] = entry.outcome

        return cls(
            messages=messages,
            runs=tuple(
                JournalRun(run_id=start.run_id, started_at=start.at, outcome=outcomes.get(start.run_id))
                for start in starts
            ),
            tool_calls=tuple(
                JournalToolCall.model_validate(
                    {**intent.model_dump(exclude={'kind'}), **settles.get((intent.run_id, intent.tool_call_id), {})}
                )
                for intent in intents
            ),
        )
