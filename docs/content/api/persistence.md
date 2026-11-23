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

Upgrade each reader of a shared store before a writer.

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
