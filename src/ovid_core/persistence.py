from abc import abstractmethod
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from ovid_core.errors import PersistenceError
from ovid_core.messages.models import AgentMessage
from ovid_core.runtime.identifiers import ConversationId


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
