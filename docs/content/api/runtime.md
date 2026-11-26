# Messages and runtime

## Normalized message parts

Import from `ovid_core.messages`. `MessagePart` is a Pydantic discriminated union on `kind`.

| Part | Fields beyond `kind` | Valid role |
| --- | --- | --- |
| `SystemPromptPart` | `content: str` | request |
| `UserPromptPart` | `content: str | tuple[UserContent, ...]`, where a tuple is not empty | request |
| `TextPart` | `content: str` | response |
| `ThinkingPart` | `content`, optional ID, signature, provider name, and opaque JSON provider details | response |
| `CompactionPart` | optional `content`, ID, provider name, and opaque JSON provider details | request or response |
| `ToolCallPart` | non-empty `tool_name`, `arguments`, non-empty `tool_call_id` | response |
| `NativeToolCallPart` | the `ToolCallPart` fields, and optional `tool_kind`, `id`, `provider_name`, and JSON `provider_details` | response |
| `ToolReturnPart` | non-empty `tool_name`, JSON `content`, non-empty `tool_call_id`, `outcome='success'`, JSON `metadata=None`, optional `content_file_digests` | request |
| `NativeToolReturnPart` | the `ToolReturnPart` fields, and optional `tool_kind`, `provider_name`, and JSON `provider_details` | response |
| `FilePart` | `content: BinaryContent`, and optional `id`, `provider_name`, and JSON `provider_details` | response |
| `CapabilityLoadCallPart` | non-empty `capability_id`, non-empty `tool_call_id` | response |
| `CapabilityLoadReturnPart` | `message: str | None`, `instructions: str | None`, non-empty `tool_call_id`, `outcome='success'` | request |
| `RetryPromptPart` | `content`, optional `tool_name`, non-empty `tool_call_id` | request |
| `OpaqueRequestPart` | non-empty `part_kind`, JSON object `payload` | request |
| `OpaqueResponsePart` | non-empty `part_kind`, JSON object `payload` | response |

`ToolArguments = str | dict[str, JsonValue] | None`.
Tool and capability outcomes are one of `success`, `failed`, `denied`, `interrupted`, or `retried`.
The `retried` outcome tells the model to make the tool call again.
The content of a `retried` return is the feedback for the model.

`metadata` holds the data that a tool returns for the application and not for the model.
The adapter never sends it to the model.
An Ovid tool writes its `ToolResult.metadata` there, or `None` when that value is empty.
Pydantic AI writes other values, for example these:

- `{'pydantic_ai_synthesized_tool_return': True}` on a return that it adds for a tool call without a result
- `{'mcp_app': {...}}` on the return of an MCP tool that names a user interface resource
- any value in the `ToolReturn.metadata` of a Pydantic AI function tool or a deferred call result

The adapter keeps each JSON value exactly, including a value that is not a JSON object.
It stores a value that is not JSON in its JSON form, for example a `datetime` as ISO 8601 text and `bytes` as URL-safe Base64 text.
It stores a value that has no JSON form as `None`.

A provider writes a response `CompactionPart`.
An application or a capability writes a request `CompactionPart`, which contains only `content`.

`CapabilityLoadReturnPart.message` is the load confirmation that the model receives.
`instructions` is `None` for a new load.
Messages that an earlier version stored can contain `instructions`.

`RetryPromptPart` is a legacy part in stored messages.
Ovid Core does not write it for new runs.
The adapter converts it to a `retried` tool return when `tool_name` is set.
The adapter converts it to Pydantic AI retry feedback when `tool_name` is `None`.

### User content items

Import from `ovid_core.messages`. `UserContent` is a Pydantic discriminated union on `kind`.
A user prompt is a string or a sequence of these items.

| Item | `kind` | Fields beyond `kind` |
| --- | --- | --- |
| `TextContent` | `text` | `content: str` |
| `ImageUrl` | `image_url` | the URL fields |
| `AudioUrl` | `audio_url` | the URL fields |
| `VideoUrl` | `video_url` | the URL fields |
| `DocumentUrl` | `document_url` | the URL fields |
| `BinaryContent` | `binary` | `data`, non-empty `media_type`, optional `identifier`, optional JSON `vendor_metadata` |

The URL fields are:

- `url`: A non-empty string.
- `media_type`: An explicit media type. The default is `None`.
- `identifier`: An explicit identifier. The default is `None`.
- `force_download`: `False`, `True`, or `'allow-local'`. The default is `False`.
- `vendor_metadata`: A JSON object for one provider. The default is `None`.

Pydantic AI computes a media type and an identifier when the value is `None`.
Ovid Core does not store a computed value.
A URL with no known file extension is valid without a `media_type`.

`BinaryContent.data` holds bytes.
Pydantic reads and writes `data` as text in the standard Base64 alphabet, with `+` and `/`.
Do not supply text in the URL-safe Base64 alphabet.
Pydantic decodes such text to incorrect bytes and does not raise an error.

```python
import base64

from ovid_core.messages import BinaryContent, ImageUrl, TextContent

prompt = (
    TextContent(content='Compare these images.'),
    ImageUrl(url='https://example.com/chart.png'),
    BinaryContent(data=base64.b64encode(image_bytes), media_type='image/png'),
)
```

The adapter keeps a Pydantic AI user prompt as a typed `UserPromptPart` only when the conversion is exact.
A plain string item converts to `TextContent`, and `TextContent` converts to a plain string item.
The adapter stores the full prompt as an `OpaqueRequestPart` when the prompt contains one of these items:

- A Pydantic AI `TextContent`, `StructuredContent`, `CachePoint`, or `UploadedFile`.
- A Pydantic AI `BinaryContent` with an image media type that is not a `BinaryImage`.
- An item of a subclass that Ovid Core does not model.

### Provider-run tools and generated files

A provider can run a tool, such as web search, and return the call and the result in one response.
`NativeToolCallPart` and `NativeToolReturnPart` hold that call and that result.
`tool_kind` identifies the tool independently of `tool_name`.
Ovid Core runs no code for these parts, and a stream emits no tool event for them.

`FilePart` holds one file that the model generated, such as an image.

The adapter restores an equal Pydantic AI part from each of these parts, except for the timestamp of a return.

### Tool return content with files

A tool from a Pydantic AI capability or an MCP server can return files.
`ToolReturnPart.content` and `NativeToolReturnPart.content` stay JSON.
Each file is a JSON object in the serialized form of Pydantic AI:

| File | `kind` | Other keys |
| --- | --- | --- |
| Inline bytes | `binary` | `data` in the URL-safe Base64 alphabet, `media_type`, `identifier`, `vendor_metadata` |
| URL | `image-url`, `audio-url`, `video-url`, or `document-url` | `url`, `media_type`, `identifier`, `force_download`, `vendor_metadata` |
| Uploaded file | `uploaded-file` | `file_id`, `provider_name`, `media_type`, `identifier`, `vendor_metadata` |

These objects are not Ovid content items.
A content item uses a `kind` such as `image_url` and standard Base64 data.
A file object in tool return content uses a `kind` such as `image-url` and URL-safe Base64 data.

`content_file_digests` is the record that Pydantic AI uses to restore the files.
The value is `None` when `ToolReturnPart.content` is plain JSON, and the adapter then restores the content as data.
The value is a tuple when Pydantic AI serialized the content, and the adapter then restores each recorded file.
Do not change `content` or `content_file_digests` in stored history.

A `ToolResultEvent` contains the same JSON content.
An Ovid `BaseTool` returns files in `ToolResult.files`, and its tool return takes this form.
[`ToolResult`](extensions.md#toolresult) shows an example.

### `OpaqueRequestPart` and `OpaqueResponsePart`

An opaque part stores a Pydantic AI part that the typed Ovid parts cannot hold without data loss.
`OpaqueRequestPart` stores a request part, and `OpaqueResponsePart` stores a response part.
`part_kind` is the Pydantic AI part kind.
`payload` is the JSON-compatible serialized part.
The adapter restores the same Pydantic AI part from `payload`.

Pydantic AI uses these parts to continue a conversation.
Keep them in stored history and do not change `payload`.
The payload shape belongs to Pydantic AI and can change with a Pydantic AI release.

| Request `part_kind` | Recorded state |
| --- | --- |
| `instruction-baseline` | The instruction blocks that a request sent. |
| `instruction-delta` | A replacement or a withdrawal of one instruction block, such as the instructions of a loaded capability. |
| `capability-load-delta` | Capabilities that code loaded without a `load_capability` call. |
| `tool-availability-delta` | Tools that became available to the model. |
| `model-settings-delta` | Model settings that changed during the conversation. |
| `retry-feedback` | Feedback about a response that Pydantic AI could not use as output. |
| `capability`, `speech` | Content that a capability or a realtime session recorded. |
| `system-prompt`, `user-prompt`, `compaction` | A part with data that the typed Ovid part does not hold, such as a request scope, a cache point, or metadata. |

A response part becomes an `OpaqueResponsePart` when Ovid Core has no typed part for its kind.
In Pydantic AI 2.71, that kind is `speech`.
A response part kind from a later Pydantic AI release also becomes an `OpaqueResponsePart`.

A part also becomes an opaque part when the typed part does not accept its value.
Examples are a capability load call without a capability ID and a tool call with an empty tool name.
The `part_kind` of such a part is the Pydantic AI kind, such as `tool-call` or `tool-return`.

`ThinkingPart` preserves provider reasoning state for replay. User-facing event projections omit it.

### Pydantic AI fields that typed parts do not keep

No typed part keeps a part timestamp.
The adapter sets a new timestamp when it restores a part.

| Ovid part | Pydantic AI fields that the part does not keep |
| --- | --- |
| `TextPart` | `id`, `provider_name`, `provider_details`, `citations` |
| `ToolCallPart` | `tool_kind`, `id`, `provider_name`, `provider_details` |
| `ToolReturnPart` | `tool_kind`, `retry` |
| `CapabilityLoadCallPart` | `id`, `provider_name`, `provider_details` |
| `CapabilityLoadReturnPart` | `metadata`, `retry` |
| `CompactionPart` in a response | `metadata` |

A local tool-search call or return converts to a `ToolCallPart` or a `ToolReturnPart`.
The adapter restores a base Pydantic AI tool part without `tool_kind='tool-search'`.

`ThinkingPart`, `NativeToolCallPart`, `NativeToolReturnPart`, `FilePart`, and a typed `UserPromptPart` keep all other fields.
A request `SystemPromptPart` or `CompactionPart` with a field that the typed part does not hold becomes an `OpaqueRequestPart`.

`AgentMessage.provider_details` keeps only the compaction marker of a Pydantic AI response.
`AgentMessage` does not keep the `metadata` of a Pydantic AI message.
Thus a stored history does not keep the Pydantic AI marker of a deferred pause.
[Deferred runs](agents.md#deferred-runs) gives the rule that Ovid applies to a new prompt instead.

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
| `state` | `complete | interrupted | incomplete | suspended` | `complete` |

Validation rejects parts for the other role. Request messages cannot contain `request_usage`. Response messages must contain it.

### Message state

`state` has the same values as the Pydantic AI `ModelRequest.state` and `ModelResponse.state`, and it converts in both directions without change.

- `complete`: the message is whole.
- `interrupted`: the run stopped while it made this message, or before it acted on this message.
- `incomplete`: a response that is still in a stream.
- `suspended`: a provider paused the response and expects a continuation.

A request message can only be `complete` or `interrupted`, and validation rejects the other values.
A record that has no `state` field decodes as `complete`.
Pydantic AI uses an `interrupted` message at the end of a history to close the unanswered tool calls on the next run.

## Identities and context

Import `RunId` and `ConversationId` from `ovid_core.runtime.identifiers`.
Both use frozen UUID root models.
Construct them from UUID values or parse them with Pydantic.
Use `RunId.new()` or `ConversationId.new()` to generate random UUIDs.
`str(value)` returns the canonical UUID string.

Import `RunContext[Deps]` from `ovid_core.runtime.context`. It is a frozen dataclass with `deps`, `run_id`, `conversation_id`, `usage` and `run_step`.
`usage` defaults to zero `Usage()`.
`run_step: int = 0` is the number of the current model request in the run, from Pydantic AI `RunContext.run_step`.
The first model request of a run has step `1`, and each run starts again at step `1`.

## Events

Import from `ovid_core.runtime.events`. Every event extends `EventIdentity` and therefore has `run_id`, `conversation_id`, and a non-negative `sequence`.

| Event | `kind` | Additional fields |
| --- | --- | --- |
| `RunStartedEvent` | `run_started` | none |
| `ModelRequestStartedEvent` | `model_request_started` | non-negative `request_index` |
| `TextDeltaEvent` | `text_delta` | `content` |
| `ToolCallEvent` | `tool_call` | `tool_name`, `arguments`, `tool_call_id` |
| `ToolResultEvent` | `tool_result` | `tool_name`, JSON `content`, `tool_call_id`, `outcome`, JSON `metadata=None` |
| `ToolDeferredEvent` | `tool_deferred` | `tool_call_id`, `tool_name`, JSON object `arguments`, `deferral_kind`, optional `reason`, optional JSON object `metadata` |
| `UsageUpdateEvent` | `usage_update` | aggregate `usage`, optional latest `request_usage`, `is_final=False` |
| `RunCompletedEvent` | `run_completed` | final `usage` |
| `RunDeferredEvent` | `run_deferred` | final `usage` |
| `RunFailedEvent` | `run_failed` | non-empty `error_type`, non-empty `message`, `persisted=False` |

`AgentEvent` is the discriminated union of these ten event models.

A server sets `RunFailedEvent.persisted` to `true` when the commit of the partial transcript finished before the server sent the event.
The value is `false` when the commit failed or did not finish in time, when the server has no store, or when the run added no message.
A stream that runs without a server always emits `persisted=False`.

A `ToolResultEvent` has the same `outcome` values as a `ToolReturnPart`.
A stream emits a `ToolResultEvent` with `outcome='retried'` for each retried tool call.
A stream emits no tool event for a tool call with an empty `tool_name` or an empty `tool_call_id`, and the run continues.
`ToolResultEvent.metadata` is the `metadata` of the `ToolReturnPart`.
The model does not receive it.
An event from an earlier release has no `metadata` key and decodes with `metadata=None`.

A paused run ends with these events:

1. One `ToolDeferredEvent` for each call in `DeferredRunResult.requests`, in the same order.
2. The final `UsageUpdateEvent`.
3. `RunDeferredEvent`, in place of `RunCompletedEvent`.

`deferral_kind` is `approval`, `external`, or `question`.
`reason` is the `ToolApproval.reason` of a tool whose approval setting caused the pause.
`metadata` is the metadata of `ToolApprovalRequired` or `ToolDeferred`.

A resumed run emits `ToolCallEvent` again for each answered call, before the `ToolResultEvent` of that call.
No `ModelRequestStartedEvent` comes before these events, because the model did not make the calls in this run.
Thus the events of each run contain each call that the run completes.

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

Validation enforces four invariants:

1. Any current-run message identity present must equal the result identity.
2. Current-run messages must end a non-empty active history.
3. Result usage must equal normalized current-run response usage plus the result's tool-call count.
4. Metadata keys must be unique and must not identify secrets.

`messages` is the current run delta. `history` is the effective model history after provider compaction or history processing.

A result contains only the current run's usage. A parent `UsageTracker` may separately contain aggregate nested usage.

### `DeferredRunResult`

A run that pauses for deferred tool calls returns `DeferredRunResult`.
It has no `output`.

| Field | Type | Default |
| --- | --- | --- |
| `requests` | `DeferredToolRequests` | required |
| `messages` | `tuple[AgentMessage, ...]` | required |
| `history` | `tuple[AgentMessage, ...]` | `()` |
| `usage` | `Usage` | required |
| `run_id` | `RunId` | required |
| `conversation_id` | `ConversationId` | required |

Validation enforces the first three `RunResult` invariants.
The last message of `messages` is the response with the pending calls, or a request with the results of other calls from the same response.
Pass `history` to the next run together with the resume input.
[Deferred tool values](extensions.md#deferred-tool-values) describes `DeferredToolRequests`.

### `PartialRunResult`

A run that fails, is cancelled, reaches a usage limit, fills the context window, or times out has no result.
`AgentStream.partial()` then returns a `PartialRunResult` with the messages that the run made.

| Field | Type | Default |
| --- | --- | --- |
| `messages` | `tuple[AgentMessage, ...]` | required |
| `history` | `tuple[AgentMessage, ...]` | `()` |
| `usage` | `Usage` | required |
| `run_id` | `RunId` | required |
| `conversation_id` | `ConversationId` | required |
| `outcome` | `RunOutcome` | required |
| `error_type` | non-empty `str` | required |
| `message` | non-empty `str` | required |

`RunOutcome` is one of these values:

| Outcome | Cause |
| --- | --- |
| `failed` | Any other run error, for example a provider error or a tool error in `tool_failures='raise'` mode. |
| `cancelled` | The run task was cancelled. |
| `usage_limit` | `UsageLimitError` |
| `context_overflow` | `ContextWindowError` |
| `timeout` | `AgentTimeoutError`, from `AgentRunPolicy.timeout_seconds` |

`error_type` is the class name of the normalized Ovid error, or `CancelledError`.
`message` is the text of the normalized Ovid error, or `Run cancelled`.
It never contains the text of a provider exception or of an exception that is not an Ovid error.

`messages` are the messages of this run, which are the history messages with the run ID.
When the run closed pending calls of the stored history, `messages` starts with that closing request, as in `RunResult`.
`history` is the full history of the run.
Both end with one synthesized boundary response:

- `parts` is one `TextPart` with the text `[Run ended: {outcome}: {message}]`.
- `finish_reason` is `error`, `state` is `interrupted`, and `provider_name` is `ovid`.
- `request_usage` is a zero `RequestUsage`.

The partial keeps the interrupted response or request that Pydantic AI recorded.
A tool call in an interrupted response whose arguments are not valid JSON is removed.
On the next run, Pydantic AI closes each tool call that has no result with an `interrupted` result before it sends the request.
The model thus sees the closed calls and the boundary text.

Validation enforces the first two `RunResult` invariants.
`usage` is the aggregate usage that Pydantic AI reported for the run, so it can differ from the sum of the response usage.
