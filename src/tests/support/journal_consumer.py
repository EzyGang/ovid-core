import asyncio
import copy
from collections.abc import Sequence

from ovid_core import InMemoryConversationStore
from ovid_core.journal import JournalEntry, StepCommittedEntry
from ovid_core.messages import AgentMessage, ToolReturnPart
from ovid_core.runtime import ConversationId
from ovid_core.server import AgentRunRequest, StdioRunRequest
from ovid_core.tools import DeferredToolRequests, ToolEffect, ToolExecutionContext
from tests.support.agent_consumer import AgentDependencies
from tests.support.deferred_consumer import DangerTool, RecordingTool


class TranscriptStore(InMemoryConversationStore):
    """A journal store that keeps an append-only transcript and the order of its writes, like an application store.

    Each journal append yields to the event loop first, so a caller that does not await it would run ahead.
    """

    def __init__(self) -> None:
        super().__init__()
        self.transcript: tuple[AgentMessage, ...] = ()
        self.events: list[str] = []

    async def append_entries(self, conversation_id: ConversationId, entries: Sequence[JournalEntry]) -> None:
        await asyncio.sleep(0)
        await super().append_entries(conversation_id, entries)
        self.events.extend(entry.kind for entry in entries)
        self.transcript += tuple(
            message for entry in entries if isinstance(entry, StepCommittedEntry) for message in entry.messages
        )

    async def commit(
        self,
        conversation_id: ConversationId,
        messages: tuple[AgentMessage, ...],
        history: tuple[AgentMessage, ...],
    ) -> None:
        await super().commit(conversation_id, messages, history)
        self.events.append('commit')
        self.transcript += messages


class PlainHistoryStore:
    """A history store without the journal. It records each write."""

    def __init__(self) -> None:
        self.messages: tuple[AgentMessage, ...] = ()
        self.writes: list[str] = []

    async def load(self, conversation_id: ConversationId) -> tuple[AgentMessage, ...]:
        del conversation_id
        return self.messages

    async def append(self, conversation_id: ConversationId, messages: tuple[AgentMessage, ...]) -> None:
        del conversation_id
        self.writes.append('append')
        self.messages += messages

    async def commit(
        self,
        conversation_id: ConversationId,
        messages: tuple[AgentMessage, ...],
        history: tuple[AgentMessage, ...],
    ) -> None:
        del conversation_id, messages
        self.writes.append('commit')
        self.messages = history


class StopAfterSaveStore(InMemoryConversationStore):
    """Keep a copy of the store as a process leaves it when it stops right after it saves the pending calls."""

    def __init__(self) -> None:
        super().__init__()
        self.snapshot: InMemoryConversationStore | None = None

    async def save_deferred_requests(self, conversation_id: ConversationId, requests: DeferredToolRequests) -> None:
        await super().save_deferred_requests(conversation_id, requests)
        self.snapshot = copy.deepcopy(self)


class SnapshotTool(RecordingTool):
    """Keep a copy of the store when the tool starts, as a process leaves it when it stops during the tool."""

    id = 'write'
    effect = ToolEffect(kind='workspace_write')

    def __init__(self, store: InMemoryConversationStore) -> None:
        super().__init__()
        self.store = store
        self.snapshot: InMemoryConversationStore | None = None

    def defer(self, context: ToolExecutionContext[AgentDependencies]) -> None:
        del context
        self.snapshot = copy.deepcopy(self.store)


class ReadSnapshotTool(SnapshotTool):
    id = 'look'
    effect = ToolEffect(kind='read', replay='safe')


class ApprovedSnapshotTool(DangerTool):
    """Keep a copy of the store when the approved call starts."""

    def __init__(self, store: InMemoryConversationStore) -> None:
        super().__init__()
        self.store = store
        self.snapshot: InMemoryConversationStore | None = None

    def defer(self, context: ToolExecutionContext[AgentDependencies]) -> None:
        if context.approved:
            self.snapshot = copy.deepcopy(self.store)


def run_request(request_id: str, request: AgentRunRequest) -> StdioRunRequest:
    return StdioRunRequest(request_id=request_id, agent_id='writer', request=request)


def recovery_returns(messages: Sequence[AgentMessage]) -> list[tuple[str, str]]:
    return [
        (part.tool_call_id, str(part.content))
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart) and str(part.content).startswith('[Recovery:')
    ]
