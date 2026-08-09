from abc import abstractmethod
from typing import Literal, Protocol

from pydantic import ValidationError

from ovid_core.errors import PersistenceError
from ovid_core.messages.models import AgentMessage
from ovid_core.models import BaseModel
from ovid_core.runtime.identifiers import ConversationId


class ConversationStore(Protocol):
    @abstractmethod
    async def load(self, conversation_id: ConversationId) -> tuple[AgentMessage, ...]: ...

    @abstractmethod
    async def append(self, conversation_id: ConversationId, messages: tuple[AgentMessage, ...]) -> None: ...


class _EncodedMessage(BaseModel):
    version: Literal[1] = 1
    message: AgentMessage


class MessageCodec:
    @property
    def version(self) -> int:
        return 1

    def encode(self, message: AgentMessage) -> bytes:
        return _EncodedMessage(message=message).model_dump_json().encode()

    def decode(self, payload: bytes) -> AgentMessage:
        try:
            encoded = _EncodedMessage.model_validate_json(payload)
        except ValidationError as error:
            message = 'Conversation message payload is invalid or uses an unsupported codec version'
            raise PersistenceError(message) from error

        return encoded.message


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._messages: dict[ConversationId, tuple[AgentMessage, ...]] = {}

    async def load(self, conversation_id: ConversationId) -> tuple[AgentMessage, ...]:
        return self._messages.get(conversation_id, ())

    async def append(self, conversation_id: ConversationId, messages: tuple[AgentMessage, ...]) -> None:
        if not messages:
            return

        self._messages[conversation_id] = self._messages.get(conversation_id, ()) + messages
