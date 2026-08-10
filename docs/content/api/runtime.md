# Messages and runtime

## Normalized message parts

Import from `ovid_core.messages.models`. `MessagePart` is a Pydantic discriminated union on `kind`.

| Part | Fields beyond `kind` | Valid role |
| --- | --- | --- |
| `SystemPromptPart` | `content: str` | request |
| `UserPromptPart` | `content: str` | request |
| `TextPart` | `content: str` | response |
| `ToolCallPart` | non-empty `tool_name`, `arguments`, non-empty `tool_call_id` | response |
| `ToolReturnPart` | non-empty `tool_name`, JSON `content`, non-empty `tool_call_id`, `outcome='success'` | request |
| `CapabilityLoadCallPart` | non-empty `capability_id`, non-empty `tool_call_id` | response |
| `CapabilityLoadReturnPart` | `instructions: str | None`, non-empty `tool_call_id`, `outcome='success'` | request |
| `RetryPromptPart` | `content`, optional `tool_name`, non-empty `tool_call_id` | request |

`ToolArguments = str | dict[str, JsonValue] | None`. Tool and capability outcomes are one of `success`, `failed`, `denied`, or `interrupted`.

### `AgentMessage`

| Field | Type | Default |
| --- | --- | --- |
| `role` | `Literal['request', 'response']` | required |
| `parts` | `tuple[MessagePart, ...]` | required |
| `run_id` | `RunId | None` | `None` |
| `conversation_id` | `ConversationId | None` | `None` |
| `timestamp` | `datetime | None` | `None` |
| `request_usage` | `RequestUsage | None` | `None` |
| `instructions` | `str | None` | `None` |
| `model_name`, `provider_name`, `provider_response_id` | `str | None` | `None` |
| `finish_reason` | `stop | length | content_filter | tool_call | error | None` | `None` |

Validation rejects parts for the other role. Request messages cannot contain `request_usage`. Response messages must contain it.

## Identities and context

Import `RunId` and `ConversationId` from `ovid_core.runtime.identifiers`. Both are frozen UUID root models. Construct from a UUID, parse with Pydantic, or generate a random UUID with `RunId.new()` and `ConversationId.new()`. `str(value)` returns the canonical UUID string.

Import `RunContext[Deps]` from `ovid_core.runtime.context`. It is a frozen dataclass with `deps`, `run_id`, `conversation_id`, and `usage`, which defaults to zero `Usage()`.

## Events

Import from `ovid_core.runtime.events`. Every event extends `EventIdentity` and therefore has `run_id`, `conversation_id`, and a non-negative `sequence`.

| Event | `kind` | Additional fields |
| --- | --- | --- |
| `RunStartedEvent` | `run_started` | none |
| `ModelRequestStartedEvent` | `model_request_started` | non-negative `request_index` |
| `TextDeltaEvent` | `text_delta` | `content` |
| `ToolCallEvent` | `tool_call` | `tool_name`, `arguments`, `tool_call_id` |
| `ToolResultEvent` | `tool_result` | `tool_name`, JSON `content`, `tool_call_id`, `outcome` |
| `UsageUpdateEvent` | `usage_update` | `usage`, `is_final=False` |
| `RunCompletedEvent` | `run_completed` | final `usage` |
| `RunFailedEvent` | `run_failed` | non-empty `error_type`, non-empty `message` |

`AgentEvent` is the discriminated union of these eight event models.

```python
def tool_events_from_messages(
    messages: tuple[AgentMessage, ...],
    *,
    run_id: RunId,
    conversation_id: ConversationId,
) -> tuple[ToolCallEvent | ToolResultEvent, ...]
```

This helper extracts tool calls and returns in message order. It assigns sequence numbers that start at zero.

The helper uses the run and conversation identities from the caller.

## Results

Import from `ovid_core.runtime.results`.

### `ResultMetadataEntry`

The entry contains a non-empty `key` and a JSON `value`.

Ovid Core rejects metadata keys that identify secret data. This check includes normalized names for keys, tokens, authorization, passwords, and credentials.

### `RunResult[Output]`

| Field | Type | Default |
| --- | --- | --- |
| `output` | `Output` | required |
| `messages` | `tuple[AgentMessage, ...]` | required |
| `usage` | `Usage` | required |
| `run_id` | `RunId` | required |
| `conversation_id` | `ConversationId` | required |
| `metadata` | `tuple[ResultMetadataEntry, ...]` | `()` |

Validation enforces three invariants:

1. Any message identity present must equal the result identity.
2. Result usage must equal normalized response-message request usage plus the result's tool-call count.
3. Metadata keys must be unique and must not identify secrets.

A result contains only the current run's usage. A parent `UsageTracker` may separately contain aggregate nested usage.
