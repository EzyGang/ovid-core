import asyncio
from abc import abstractmethod
from typing import Protocol
from uuid import uuid4

from ovid_core.runtime.identifiers import ConversationId
from ovid_core.todo.errors import TodoConflictError
from ovid_core.todo.models import TodoPhase, TodoState


class TodoStore(Protocol):
    @abstractmethod
    async def load(self, conversation_id: ConversationId) -> TodoState | None: ...

    @abstractmethod
    async def save(
        self,
        conversation_id: ConversationId,
        phases: tuple[TodoPhase, ...],
        *,
        expected_revision: str | None,
    ) -> TodoState: ...


class InMemoryTodoStore:
    def __init__(self) -> None:
        self._states: dict[ConversationId, TodoState] = {}
        self._lock = asyncio.Lock()

    async def load(self, conversation_id: ConversationId) -> TodoState | None:
        async with self._lock:
            return self._states.get(conversation_id)

    async def save(
        self,
        conversation_id: ConversationId,
        phases: tuple[TodoPhase, ...],
        *,
        expected_revision: str | None,
    ) -> TodoState:
        async with self._lock:
            current = self._states.get(conversation_id)
            current_revision = None if current is None else current.revision

            if current_revision != expected_revision:
                raise TodoConflictError('todo state changed before it could be saved')

            state = TodoState(phases=phases, revision=str(uuid4()))
            self._states[conversation_id] = state
            return state
