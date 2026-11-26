from abc import abstractmethod
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from ovid_core.errors import PersistenceError
from ovid_core.journal import (
    ConversationJournal,
    JournalEntry,
    RunEndedEntry,
    RunEndOutcome,
    RunStartedEntry,
    StepCommittedEntry,
)
from ovid_core.messages.models import AgentMessage
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.tools.deferred import DeferredToolRequests


class ConversationStore(Protocol):
    @abstractmethod
    async def load(self, conversation_id: ConversationId) -> tuple[AgentMessage, ...]: ...

    @abstractmethod
    async def append(self, conversation_id: ConversationId, messages: tuple[AgentMessage, ...]) -> None: ...


@runtime_checkable
class ConversationHistoryStore(ConversationStore, Protocol):
    @abstractmethod
    async def commit(
        self,
        conversation_id: ConversationId,
        messages: tuple[AgentMessage, ...],
        history: tuple[AgentMessage, ...],
    ) -> None: ...


@runtime_checkable
class DeferredToolRequestStore(ConversationStore, Protocol):
    @abstractmethod
    async def load_deferred_requests(self, conversation_id: ConversationId) -> DeferredToolRequests | None: ...

    @abstractmethod
    async def save_deferred_requests(self, conversation_id: ConversationId, requests: DeferredToolRequests) -> None: ...

    @abstractmethod
    async def clear_deferred_requests(self, conversation_id: ConversationId) -> None: ...


@runtime_checkable
class ConversationJournalStore(ConversationHistoryStore, Protocol):
    @abstractmethod
    async def append_entries(self, conversation_id: ConversationId, entries: Sequence[JournalEntry]) -> None:
        """Store all entries, or none, and return only when they are durable."""

    @abstractmethod
    async def load_journal(self, conversation_id: ConversationId) -> ConversationJournal: ...


class RunJournal:
    """Append the journal entries of one run, and keep the messages that its step commits hold."""

    def __init__(self, *, store: ConversationJournalStore, conversation_id: ConversationId) -> None:
        self._store = store
        self._conversation_id = conversation_id
        self._run_id: RunId | None = None
        self.messages: tuple[AgentMessage, ...] = ()

    async def append(self, *entries: JournalEntry) -> None:
        await self._store.append_entries(self._conversation_id, entries)
        for entry in entries:
            if isinstance(entry, RunStartedEntry):
                self._run_id = entry.run_id
            elif isinstance(entry, StepCommittedEntry):
                self.messages = (*self.messages, *entry.messages)

    async def end(self, outcome: RunEndOutcome) -> None:
        """Close the run. A run that wrote no entry has nothing to close."""
        if self._run_id is None:
            return

        await self.append(RunEndedEntry(run_id=self._run_id, outcome=outcome))


class MessageCodec:
    def encode(self, message: AgentMessage) -> bytes:
        return message.model_dump_json().encode()

    def decode(self, payload: bytes) -> AgentMessage:
        try:
            return AgentMessage.model_validate_json(payload)
        except ValidationError as error:
            message = 'Conversation message payload is invalid'
            raise PersistenceError(message) from error


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._messages: dict[ConversationId, tuple[AgentMessage, ...]] = {}
        self._deferred_requests: dict[ConversationId, DeferredToolRequests] = {}
        self._entries: dict[ConversationId, tuple[JournalEntry, ...]] = {}

    async def load(self, conversation_id: ConversationId) -> tuple[AgentMessage, ...]:
        return self._messages.get(conversation_id, ())

    async def append(self, conversation_id: ConversationId, messages: tuple[AgentMessage, ...]) -> None:
        if not messages:
            return

        self._messages[conversation_id] = self._messages.get(conversation_id, ()) + messages

    async def commit(
        self,
        conversation_id: ConversationId,
        messages: tuple[AgentMessage, ...],
        history: tuple[AgentMessage, ...],
    ) -> None:
        del messages
        self._messages[conversation_id] = history

    async def append_entries(self, conversation_id: ConversationId, entries: Sequence[JournalEntry]) -> None:
        self._entries[conversation_id] = (*self._entries.get(conversation_id, ()), *entries)
        for entry in entries:
            if isinstance(entry, StepCommittedEntry):
                await self.append(conversation_id, entry.messages)

    async def load_journal(self, conversation_id: ConversationId) -> ConversationJournal:
        return ConversationJournal.from_entries(
            self._messages.get(conversation_id, ()),
            self._entries.get(conversation_id, ()),
        )

    async def load_deferred_requests(self, conversation_id: ConversationId) -> DeferredToolRequests | None:
        return self._deferred_requests.get(conversation_id)

    async def save_deferred_requests(self, conversation_id: ConversationId, requests: DeferredToolRequests) -> None:
        self._deferred_requests[conversation_id] = requests

    async def clear_deferred_requests(self, conversation_id: ConversationId) -> None:
        self._deferred_requests.pop(conversation_id, None)
