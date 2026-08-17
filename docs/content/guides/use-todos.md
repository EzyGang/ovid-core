# Track phased work with Todo

Todo is an explicit capability. Installing Ovid Core does not add it to an agent.

## Use process-local state

Construct one service and retain it for both the capability and application integrations:

```python
from ovid_core.todo.capability import TodoCapability
from ovid_core.todo.service import TodoService
from ovid_core.todo.store import InMemoryTodoStore


store = InMemoryTodoStore()
service = TodoService(store=store)
capability = TodoCapability[AppDependencies](service=service)
```

Add `capability` to the application-owned `AgentDefinition.capabilities` tuple. The agent then receives one `todo` tool and its operating instruction.

The in-memory store is suitable for tests and process-local agents. A process restart loses its state.

## Inspect or update state outside the agent

Application code uses the same service:

```python
from ovid_core.todo.models import TodoArgs


state = await service.view(conversation_id)
updated = await service.apply(
    conversation_id,
    TodoArgs(op='block', task='Deploy release', reason='Waiting for approval'),
    expected_revision=state.revision,
)
```

Pass the revision previously displayed by a UI or command. `TodoConflictError` then reports that another actor changed the state instead of overwriting it.

## Supply a durable backend

Implement the async `TodoStore` protocol for a database or remote service and change only service construction:

```python
remote_store = RemoteTodoStore(client=client)
service = TodoService(store=remote_store)
```

The backend must implement compare-and-set revisions, return typed `TodoState` models, preserve order, and raise `TodoStoreError` for safe backend failures. It must not fall back to memory after a failure. The application owns the remote client and closes it during application shutdown.

## Continue and branch conversations

Pass the same `ConversationId` to later runs to retain the same todo list. Use a distinct conversation ID when a branch requires independent work state.

Message replay does not modify todo storage. After context compaction or an external update, tell the model to call `view` or provide state loaded directly from the service.

## Keep product policy outside core

Applications decide whether to:

- force todo use on a first turn
- render a terminal or web view
- inject incomplete-work reminders
- continue an agent loop while actionable tasks remain
- stop when only blocked work remains
- synchronize tasks with a project-management system

Todo exposes authoritative state and mutations; it does not impose those policies.
