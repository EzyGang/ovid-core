# Messages and runtime

## Normalized message parts

Import from `ovid_core.messages.models`. `MessagePart` is a Pydantic discriminated union on `kind`.

| Part | Fields beyond `kind` | Valid role |
| --- | --- | --- |
| `SystemPromptPart` | `content: str` | request |
| `UserPromptPart` | `content: str` | request |
| `TextPart` | `content: str` | response |
| `ThinkingPart` | `content`, optional ID, signature, provider name, and opaque JSON provider details | response |
| `CompactionPart` | optional `content`, ID, provider name, and opaque JSON provider details | request or response |
| `ToolCallPart` | non-empty `tool_name`, `arguments`, non-empty `tool_call_id` | response |
| `ToolReturnPart` | non-empty `tool_name`, JSON `content`, non-empty `tool_call_id`, `outcome='success'` | request |
| `CapabilityLoadCallPart` | non-empty `capability_id`, non-empty `tool_call_id` | response |
| `CapabilityLoadReturnPart` | `message: str | None`, `instructions: str | None`, non-empty `tool_call_id`, `outcome='success'` | request |
| `RetryPromptPart` | `content`, optional `tool_name`, non-empty `tool_call_id` | request |
| `OpaqueRequestPart` | non-empty `part_kind`, JSON object `payload` | request |

`ToolArguments = str | dict[str, JsonValue] | None`.
Tool and capability outcomes are one of `success`, `failed`, `denied`, `interrupted`, or `retried`.
The `retried` outcome tells the model to make the tool call again.
The content of a `retried` return is the feedback for the model.

A provider writes a response `CompactionPart`.
An application or a capability writes a request `CompactionPart`, which contains only `content`.

`CapabilityLoadReturnPart.message` is the load confirmation that the model receives.
`instructions` is `None` for a new load.
Messages that an earlier version stored can contain `instructions`.

`RetryPromptPart` is a legacy part in stored messages.
Ovid Core does not write it for new runs.
The adapter converts it to a `retried` tool return when `tool_name` is set.
The adapter converts it to Pydantic AI retry feedback when `tool_name` is `None`.

### `OpaqueRequestPart`

`OpaqueRequestPart` stores a Pydantic AI request part that the typed Ovid parts cannot hold without data loss.
`part_kind` is the Pydantic AI part kind.
`payload` is the JSON-compatible serialized part.
The adapter restores the same Pydantic AI part from `payload`.

Pydantic AI uses these parts to continue a conversation.
Keep them in stored history and do not change `payload`.

| `part_kind` | Recorded state |
| --- | --- |
| `instruction-baseline` | The instruction blocks that a request sent. |
| `instruction-delta` | A replacement or a withdrawal of one instruction block, such as the instructions of a loaded capability. |
| `capability-load-delta` | Capabilities that code loaded without a `load_capability` call. |
| `tool-availability-delta` | Tools that became available to the model. |
| `model-settings-delta` | Model settings that changed during the conversation. |
| `retry-feedback` | Feedback about a response that Pydantic AI could not use as output. |
| `capability`, `speech` | Content that a capability or a realtime session recorded. |
| `system-prompt`, `user-prompt`, `compaction` | A part with data that the typed Ovid part does not hold, such as a request scope, file content, or metadata. |

`ThinkingPart` preserves provider reasoning state for replay. User-facing event projections omit it.

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
| `provider_details` | `dict[str, JsonValue] | None` | `None` |
| `finish_reason` | `stop | length | content_filter | tool_call | error | None` | `None` |

Validation rejects parts for the other role. Request messages cannot contain `request_usage`. Response messages must contain it.

## Identities and context

Import `RunId` and `ConversationId` from `ovid_core.runtime.identifiers`.
Both use frozen UUID root models.
Construct them from UUID values or parse them with Pydantic.
Use `RunId.new()` or `ConversationId.new()` to generate random UUIDs.
`str(value)` returns the canonical UUID string.

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
| `UsageUpdateEvent` | `usage_update` | aggregate `usage`, optional latest `request_usage`, `is_final=False` |
| `RunCompletedEvent` | `run_completed` | final `usage` |
| `RunFailedEvent` | `run_failed` | non-empty `error_type`, non-empty `message` |

`AgentEvent` is the discriminated union of these eight event models.

A `ToolResultEvent` has the same `outcome` values as a `ToolReturnPart`.
A stream emits a `ToolResultEvent` with `outcome='retried'` for each retried tool call.

```text
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
| `history` | `tuple[AgentMessage, ...]` | `()` |
| `usage` | `Usage` | required |
| `run_id` | `RunId` | required |
| `conversation_id` | `ConversationId` | required |
| `metadata` | `tuple[ResultMetadataEntry, ...]` | `()` |

Validation enforces three invariants:

1. Any current-run message identity present must equal the result identity.
2. Current-run messages must end a non-empty active history.
3. Result usage must equal normalized current-run response usage plus the result's tool-call count.
4. Metadata keys must be unique and must not identify secrets.

`messages` is the current run delta. `history` is the effective model history after provider compaction or history processing.

A result contains only the current run's usage. A parent `UsageTracker` may separately contain aggregate nested usage.
