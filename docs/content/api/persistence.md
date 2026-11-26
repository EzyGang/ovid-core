# Persistence

Import from `ovid_core.persistence`. Applications own storage durability, history selection, retention, and session policy. Core provides normalized messages, a minimal store protocol, and an in-memory implementation.

## `ConversationStore`

```python
class ConversationStore(Protocol):
    async def load(
        self,
        conversation_id: ConversationId,
    ) -> tuple[AgentMessage, ...]: ...

    async def append(
        self,
        conversation_id: ConversationId,
        messages: tuple[AgentMessage, ...],
    ) -> None: ...
```

`load` returns messages in conversation order. `append` adds the supplied messages in order. Implementations should treat an empty append as a no-op and must not persist upstream Pydantic AI message objects.

## `ConversationHistoryStore`

`ConversationHistoryStore` extends `ConversationStore` with one method:

```python
async def commit(
    self,
    conversation_id: ConversationId,
    messages: tuple[AgentMessage, ...],
    history: tuple[AgentMessage, ...],
) -> None: ...
```

`messages` contains the current run delta. `history` contains the effective model history after compaction or history processing.

Servers use `commit` when the store supports this protocol. Other stores continue to receive `append`.

Applications can keep an append-only transcript from `messages` while replacing a separate active context projection with `history`.

## Partial transcripts

A server also writes the [`PartialRunResult`](runtime.md#partialrunresult) of a run that does not complete.
It uses the same call as for a completed run:

- A `ConversationHistoryStore` receives `commit(conversation_id, partial.messages, partial.history)`.
- Any other store receives `append(conversation_id, partial.messages)`.

`partial.messages` contains the prompt request, every response and tool result of the run, the interrupted response or request, and the boundary response.
The boundary response is the last message, with `finish_reason='error'` and `state='interrupted'`.

A partial commit never saves or clears the record of pending deferred tool calls.
After a partial commit, the stored messages end with the boundary response, so they show no pending call.
Thus the server rejects a resume, and a new prompt clears the record when it completes.

On the next run, Pydantic AI can add a synthesized `interrupted` result to the interrupted request.
A `ConversationHistoryStore` then receives that result in `history`.
A [`ConversationJournalStore`](#conversationjournalstore) receives only the part of `partial.messages` that its step commits do not hold.
[Step commits](#step-commits) describes this split.

## `DeferredToolRequestStore`

`DeferredToolRequestStore` extends `ConversationStore`.
It keeps the pending tool calls of a paused run for each conversation:

```python
async def load_deferred_requests(self, conversation_id: ConversationId) -> DeferredToolRequests | None: ...

async def save_deferred_requests(self, conversation_id: ConversationId, requests: DeferredToolRequests) -> None: ...

async def clear_deferred_requests(self, conversation_id: ConversationId) -> None: ...
```

The protocol is optional and runtime-checkable.
A server resumes a paused run only when its store supports this protocol.
The stored record is the authority for the kind and the metadata of each pending call.
Client answers do not supply these values.

A server writes in this order:

1. After a paused run, it stores the messages, and then it saves the record.
2. After a completed run, it stores the messages, and then it clears the record.

A completed run can be a resume or a new prompt that closed the pending calls as `interrupted`.

A server accepts an answer only for a call that is in the record and has no result in the stored messages.
The record must contain every pending call, and a resume needs an answer for each pending call.
Thus a failure between the two writes cannot make a call run twice:

- Messages without a record: the server rejects the resume.
- A record without the messages of its run: the stored messages do not show the calls as pending, so the server rejects the resume.

A new prompt works in both conditions, and the completed run clears the record.
A durable store can keep `requests.model_dump_json()` and read it with `DeferredToolRequests.model_validate_json`.

## `ConversationJournalStore`

`ConversationJournalStore` extends `ConversationHistoryStore`.
A server that has a journal store records each step of a run and the intent of each tool call before the tool runs.
After a process stops without a commit, the next load closes the tool calls that the process left open.

```python
async def append_entries(self, conversation_id: ConversationId, entries: Sequence[JournalEntry]) -> None: ...

async def load_journal(self, conversation_id: ConversationId) -> ConversationJournal: ...
```

The protocol is optional and runtime-checkable.
A store without it keeps the behavior that the sections above describe: one commit at the end of a run, or one partial commit when the run stops.

### Durability rule

`append_entries` must store all entries or none.
It must return only when the entries are durable.
A file store must flush and sync the file before it returns.
The in-memory store returns when it holds the entries.

A `StepCommittedEntry` adds its messages to the history that `load` returns.
A later `commit` replaces that history, as for any `ConversationHistoryStore`.

### Journal entries

Import the entry models from `ovid_core.journal`.
`JournalEntry` is a union of these models, discriminated by `kind`:

| Entry | `kind` | Fields |
| --- | --- | --- |
| `RunStartedEntry` | `run_started` | `run_id`, `at` |
| `StepCommittedEntry` | `step_committed` | `run_id`, `messages` (one or more) |
| `ToolIntentEntry` | `tool_intent` | `run_id`, `tool_call_id`, `tool_name`, `arguments`, `effect`, `idempotency_key`, `started_at` |
| `ToolSettledEntry` | `tool_settled` | `run_id`, `tool_call_id`, `status`, `outcome`, `ended_at` |
| `RunEndedEntry` | `run_ended` | `run_id`, `outcome` |

A durable store can keep `entry.model_dump_json()` and read it with `TypeAdapter(JournalEntry).validate_json`.

`ToolIntentEntry` records a tool call before the tool runs:

- `arguments` holds the arguments that the model sent, as a JSON object.
- `effect` is the [`ToolEffect`](extensions.md) that `resolve_effect` gives for the tool definition. This includes passthrough Pydantic AI tools and MCP tools.
- `idempotency_key` comes from `BaseTool.idempotency_key` for an Ovid tool. It is `None` for other tools and for arguments that are not valid.

**Warning:** The journal stores tool arguments without redaction, the same as transcripts. Arguments can contain secrets.

`ToolSettledEntry` records how the call ended:

| End of the call | `status` | `outcome` |
| --- | --- | --- |
| The tool returned, or a capability skipped the call with a result | `completed` | `success` |
| `ToolFailed`, or any other tool error | `failed` | `failed` |
| `ModelRetry`, which includes arguments that are not valid | `failed` | `retried` |
| Cancellation during the call | `failed` | `interrupted` |
| `ApprovalRequired` or `CallDeferred` | `deferred` | `None` |

A deferred call has no outcome because the model receives no result for it yet.

`RunEndedEntry.outcome` is `completed`, `deferred`, a [`RunOutcome`](runtime.md#partialrunresult) of a partial transcript, or `lost`.

### Step commits

A journaled run writes its messages while it runs:

1. Before each model request, the run commits the request and each earlier message that no step holds yet. Pydantic AI has then recorded the instruction baseline or changes, model settings changes, and tool changes on the request, so the step holds the request that the model receives.
2. After each step of the run graph, the run commits the new messages, for example the model response.
3. Before each tool call, the run appends a `ToolIntentEntry` and waits until the store returns.
4. After each tool call, the run appends a `ToolSettledEntry`.

The first entry of a run is `RunStartedEntry`, in the same append as the first step.
A run that adds no message writes no entry.
The request that closes pending calls on a new prompt starts the first step.

The run finds its new messages from the end of the history.
It stops at a message object that a step committed, or at a copy with the content of the last committed message.
Thus a history processor that replaces messages with copies, or removes older messages, does not cause a second commit of a message.

In these cases, Pydantic AI changes a request after its step commit:

- An attempt at another model than the step model, for example after `RetryModelRequest`, records the instructions and settings of that model again. Ovid route fallback runs inside one attempt, so it does not cause this.
- A `prepare_model_request` hook of an inner capability edits the persistent history in place.
- A step that ends without a response replaces the trailing requests with copies without their request-scoped system prompts.

The step then holds the earlier version of the request.
The `history` of the end-of-run commit holds the version that Pydantic AI recorded, and it is the authoritative history.
The end-of-run `messages` compare by content, so they can hold the recorded version again, and an append-only transcript can hold both versions.

At the end of the run, the server writes only the messages of the result that the step commits do not hold, compared by content:

- A completed or deferred run: `commit(conversation_id, (), result.history)`. This keeps the history that compaction or history processing gave.
- A partial run: `commit(conversation_id, rest, partial.history)`. `rest` holds the interrupted response or request, when there is one, and the boundary response.

Thus an append-only transcript built from step messages and `commit` messages holds each message once.
The `RunEndedEntry` is the last write of a run, after the record of pending deferred calls.

### `ConversationJournal`

`load_journal` returns the history and the state of each run and tool call:

```python
class ConversationJournal(BaseModel):
    messages: tuple[AgentMessage, ...]
    runs: tuple[JournalRun, ...] = ()
    tool_calls: tuple[JournalToolCall, ...] = ()

    @classmethod
    def from_entries(cls, messages: tuple[AgentMessage, ...], entries: Sequence[JournalEntry]) -> Self: ...
```

- `messages` is the history that `load` returns.
- A `JournalRun` has `run_id`, `started_at`, and `outcome`. A run with `outcome=None` is open.
- A `JournalToolCall` has the fields of its intent, and `status`, `outcome`, and `ended_at` from its settle entry.

A tool call without a settle entry has `status='started'`.
After a restart, its effect is unknown, as the Pydantic AI Harness tool-effect record defines.
`from_entries` builds the journal from a history that already contains the step messages and from the entries in append order.

### Recovery

Import `close_orphaned_tool_calls` from `ovid_core.recovery`:

```python
def close_orphaned_tool_calls(
    journal: ConversationJournal,
    pending: DeferredToolRequests | None = None,
    *,
    replay_safe: bool = False,
) -> tuple[JournalEntry, ...]
```

The function returns the entries that close each open run.
For each open run, it returns a `StepCommittedEntry` with one request, then `RunEndedEntry(outcome='lost')`.
The request holds a `ToolReturnPart` with `outcome='interrupted'` for each tool call that the run left without a result:

- A call with an intent for a tool whose effect is not `read`: `[Recovery: <tool> may have executed before the process stopped; its effect is UNKNOWN. Inspect current state before retrying.]`
- A call with an intent for a `read` tool, or a call without an intent: `[Recovery: <tool> did not run.]`

A call in `pending` waits for a deferred answer and stays open.
The exception is a call that an open run started to execute after an approval or an answer. Recovery closes that call.
A run without a tool call to close gets only its `RunEndedEntry`.

With `replay_safe=True`, recovery keeps a call open when all these conditions are true:

- The call is in the last response and has no result.
- Its intent has `status='started'`.
- Its intent effect has `replay='safe'`.

The run still gets `RunEndedEntry(outcome='lost')`.
A server passes `replay_safe=True` only for a [resume](server.md#resume), which then runs the open call again.

`unknown_effect_text(tool_name)` returns the UNKNOWN recovery text for a tool.

[Recovery on load](server.md#recovery-on-load) describes when a server applies these entries.

### Restart count

Import `restart_count` from `ovid_core.recovery`:

```python
def restart_count(journal: ConversationJournal) -> int
```

The function counts the runs that stopped in a row after the last run that ended normally.
A run with the outcome `completed` or `deferred` ends normally.
These runs count:

- A run with the outcome `lost`, `failed`, `usage_limit`, `context_overflow`, or `timeout`.
- An open run, because the next admitted load closes it as `lost`.

A `cancelled` run does not count and does not end the series, because a user stops it deliberately.
The server compares the count with `ServerConfig.resume_restart_limit`, as [Resume](server.md#resume) describes.

## `MessageCodec`

```python
codec = MessageCodec()
payload = codec.encode(message)
message = codec.decode(payload)
```

- `encode(message)` returns UTF-8 JSON bytes for the normalized `AgentMessage`.
- `decode(payload)` returns the normalized message.
- Invalid JSON or an invalid message raises `PersistenceError` with a source-safe message.

The codec stores the normalized message directly.

### Record compatibility

A newer Ovid Core release reads records that an older release wrote.
An older release cannot read a record that contains a part kind, a field, or a content item from a newer release.
`decode` raises `PersistenceError` for such a record.

These values appear only in records from a release with typed multimodal content:

- The part kinds `native_tool_call`, `native_tool_return`, `file`, and `opaque_response`
- A `user_prompt` part whose `content` is an array of content items
- The `content_file_digests` field, which each `tool_return` part contains

A record from a release with out-of-band tool metadata contains a `metadata` field in each `tool_return` part.
A record without this field decodes with `metadata=None`.
A `tool_return` part from an earlier release can hold a JSON object with `content` and `metadata` as its `content`.
That part stays valid, and the model receives that object again in a later run.

Upgrade each reader of a shared store before a writer.

A record from a release with message state can contain a `state` field.
A record without `state` decodes as `complete`.

A record can contain inline file bytes as Base64 text, which makes the record large.
An opaque part and a tool return with files contain data in the serialized form of Pydantic AI.
A later Pydantic AI release can change that form.

## `InMemoryConversationStore`

A process-local reference implementation:

```python
store = InMemoryConversationStore()
await store.append(conversation_id, messages)
loaded = await store.load(conversation_id)
```

An unknown conversation loads as `()`. Empty append operations do nothing. `commit` replaces the active in-memory history.
The store also implements `DeferredToolRequestStore` and keeps pending tool calls in memory.
It also implements `ConversationJournalStore`. A step commit appends to the in-memory history.

Use this store for tests and temporary applications. It has no durability or external concurrency control.

## Durable implementation pattern

Persist the codec bytes rather than a model dump chosen by the application:

```python
from ovid_core.persistence import ConversationStore, MessageCodec


class DatabaseConversationStore(ConversationStore):
    def __init__(self) -> None:
        self._codec = MessageCodec()

    async def load(self, conversation_id: ConversationId) -> tuple[AgentMessage, ...]:
        rows = await load_rows(str(conversation_id))
        return tuple(self._codec.decode(row.payload) for row in rows)

    async def append(
        self,
        conversation_id: ConversationId,
        messages: tuple[AgentMessage, ...],
    ) -> None:
        await append_rows(
            str(conversation_id),
            tuple(self._codec.encode(message) for message in messages),
        )
```

The application controls transaction boundaries. It must also prevent duplicate append operations during transport retries.
