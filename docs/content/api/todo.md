# Todo state and capability

Ovid Core provides an opt-in todo capability for ordered, phased agent work. Todo state is independent of messages and is keyed by `ConversationId`.

Todo does not provide terminal UI, slash commands, eager first-turn forcing, reminders, automatic continuation, or branch policy. Applications may build those behaviors over `TodoService`.

## Models

Import state and argument models from `ovid_core.todo.models`.

`TodoStatus` values are:

- `pending`
- `in_progress`
- `completed`
- `abandoned`
- `blocked`

`TodoItem` contains exact model-facing `content`, `status`, and an optional `blocker`. `TodoPhase` contains a unique `name` and ordered task tuple. `TodoState` contains ordered phases and an opaque store `revision`.

Phase names and task content are trimmed, bounded, and reject control characters. Task content must be unique across the complete state. Exact content, including case and punctuation, identifies a task; no model-facing task IDs are generated.

`TodoLimits` defaults to 25 phases, 200 total tasks, and 50 tasks per phase. Services validate loaded and proposed state against their configured limits.

## Operations

`TodoArgs` requires an explicit `TodoOperation`:

| Operation | Payload | Behavior |
| --- | --- | --- |
| `init` | `list`, or `phase` and `items` | Replace all state with pending tasks |
| `start` | `task` | Activate one exact pending, completed, or abandoned task |
| `done` | optional `task` or `phase` | Complete the target, or all work |
| `drop` | optional `task` or `phase` | Abandon the target, or all work |
| `block` | `task` or `phase`; optional `reason` | Block open work |
| `unblock` | `task` or `phase` | Return blocked work to pending |
| `rm` | optional `task` or `phase` | Remove the target, or clear all phases |
| `append` | `phase` and non-empty `items` | Append tasks, creating the phase when absent |
| `view` | none | Load state without saving |

After every mutation, at most one task is active. The first existing active task remains active; otherwise the earliest pending task is promoted. Blocked and closed tasks are never promoted.

Bulk completion marks every selected task completed, and bulk abandonment marks every selected task abandoned. Blocking closed work and unblocking open work are successful no-ops. Completing, abandoning, blocking, or removing active work promotes the next pending task.

Known argument fields unused by the selected operation are accepted and ignored. If both `task` and `phase` are supplied, the task target takes precedence. Unknown fields remain invalid.

## Store contract

Import `TodoStore` and `InMemoryTodoStore` from `ovid_core.todo.store`.

A custom `TodoStore` implements:

```python
async def load(conversation_id: ConversationId) -> TodoState | None: ...

async def save(
    conversation_id: ConversationId,
    phases: tuple[TodoPhase, ...],
    *,
    expected_revision: str | None,
) -> TodoState: ...
```

The contract is:

1. An absent record loads as `None`.
2. `expected_revision=None` saves only when no record exists.
3. A non-null expected revision must match the current revision.
4. Every successful save returns persisted phases with a new opaque revision.
5. Clearing persists empty phases rather than deleting the record.
6. Revision mismatches raise `TodoConflictError`.
7. Other backend failures raise `TodoStoreError` with source-safe details.
8. Methods return typed models and preserve phase and task order.
9. A backend never silently falls back to process memory.

`InMemoryTodoStore` uses one async lock and isolates records by `ConversationId`. It is process-local and makes no durability claim. Applications own custom store and remote-client lifecycle.

## Service

Import `TodoService` from `ovid_core.todo.service`:

```python
service = TodoService(store=InMemoryTodoStore())
state = await service.view(conversation_id)
updated = await service.apply(
    conversation_id,
    TodoArgs(op='done', task='Run integration tests'),
    expected_revision=state.revision,
)
```

`view` never writes. `apply` loads once, validates an optional caller revision, reduces the operation, and saves using the loaded revision. It never reloads or retries after a conflict because another writer may have replaced the plan and changed the operation's meaning.

Applications and plugins may retain the service and use the same API for UI, commands, synchronization, and administrative correction.

## Capability

Import `TodoCapability` from `ovid_core.todo.capability`:

```python
service = TodoService(store=InMemoryTodoStore())

definition = AgentDefinition[AppDependencies, str](
    model=route,
    deps_type=AppDependencies,
    output_type=str,
    capabilities=(TodoCapability(service=service),),
)
```

The capability has ID `todo` and contributes one instruction plus one `todo` tool. The tool requires no approval, has a 10-second timeout, and receives `ConversationId` from its run context. Every successful call returns the complete committed state, counts, and active phase and task.

A rejected operation, such as `start` on a task that does not exist, returns a `failed` tool result.
The result text contains the error type and the target, and the run continues.

The capability uses direct service injection. It does not require `AgentServices`, construct stores, or acquire backend resources.

## Conversation behavior

Reusing a `ConversationId` loads its existing todo state. A new ID starts empty. Applications requiring branch isolation provide a distinct conversation ID for each branch. Todo state is authoritative and is not reconstructed from message history.

After compaction or external modification, an application may ask the model to call `view` or inject state obtained from `TodoService.view`. Automatic continuation remains application policy.
