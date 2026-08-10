# Persistence

Import from `ovid_core.persistence`. Applications own storage durability, history selection, retention, and session policy. Core provides normalized messages, a versioned codec, a minimal store protocol, and an in-memory implementation.

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

## `MessageCodec`

```python
codec = MessageCodec()
codec.version  # 2
payload = codec.encode(message)
message = codec.decode(payload)
```

- `version` returns the current integer codec version, `2`.
- `encode(message)` returns UTF-8 JSON bytes containing the codec version and normalized `AgentMessage`.
- `decode(payload)` accepts persisted versions 1 and 2 and returns the normalized message.
- Invalid JSON, an invalid message, or an unsupported version raises `PersistenceError` with a source-safe message.

The version wrapper allows storage migrations without exposing the private encoded-record model as public API.

## `InMemoryConversationStore`

A process-local reference implementation:

```python
store = InMemoryConversationStore()
await store.append(conversation_id, messages)
loaded = await store.load(conversation_id)
```

An unknown conversation loads as `()`. Empty append operations do nothing.

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
