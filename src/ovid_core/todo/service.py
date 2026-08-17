from ovid_core.runtime.identifiers import ConversationId
from ovid_core.todo._reducer_state import validate_todos
from ovid_core.todo.errors import TodoConflictError
from ovid_core.todo.models import TodoArgs, TodoLimits, TodoOperation, TodoState
from ovid_core.todo.reducer import reduce_todos
from ovid_core.todo.store import TodoStore


class TodoService:
    def __init__(
        self,
        *,
        store: TodoStore,
        limits: TodoLimits = TodoLimits(),
    ) -> None:
        self._store = store
        self._limits = limits

    async def view(self, conversation_id: ConversationId) -> TodoState:
        state = await self._store.load(conversation_id)
        if state is None:
            return TodoState()

        validate_todos(state.phases, self._limits)
        return state

    async def apply(
        self,
        conversation_id: ConversationId,
        arguments: TodoArgs,
        *,
        expected_revision: str | None = None,
    ) -> TodoState:
        if arguments.op == TodoOperation.VIEW:
            return await self.view(conversation_id)

        loaded = await self._store.load(conversation_id)
        state = TodoState() if loaded is None else loaded
        validate_todos(state.phases, self._limits)

        if expected_revision is not None and expected_revision != state.revision:
            raise TodoConflictError('todo state changed before the operation was applied')

        phases = reduce_todos(state.phases, arguments, limits=self._limits)
        return await self._store.save(
            conversation_id,
            phases,
            expected_revision=state.revision,
        )
