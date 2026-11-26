# Pydantic AI adapters

Third-party runtime objects live under `ovid_core.adapters`.
Domain code should depend on Ovid-owned values.
Applications should use these APIs only at composition boundaries.

## Model construction

Import from `ovid_core.adapters.pydantic_ai.models`.

### `DefaultModelFactory`

`DefaultModelFactory` implements `ModelFactory`:

```python
factory = DefaultModelFactory(provider_api_key=None)
handle = await factory.build(model_id=model_id, config=model_config)
```

The factory passes `provider:model` to upstream model inference.
The built-in test model maps to `test`.

The factory:

- merges model settings
- applies model concurrency
- normalizes capabilities
- returns an opaque handle

The factory resolves `provider_api_key` during construction.
The agent compiler uses Pydantic AI `SelectModel` to resolve it before each new logical model request.
When the key changes, the factory builds a replacement model.
Active requests keep their original provider until they finish.
The factory reuses the current model when the key does not change.

Pydantic AI manages model lifetimes.
Credential versions share the configured concurrency limiter.

The factory preserves `CredentialError` exceptions.
It converts other construction failures to `ModelResolutionError`.
Each converted exception omits the original message and cause.

Gateway-prefixed providers use the upstream gateway factory, so the application key remains scoped to the gateway endpoint.

### `known_models`

```text
def known_models() -> tuple[KnownModel, ...]
```

`known_models()` delegates to the Pydantic AI model catalog.
It divides each identifier into a typed provider and model pair.

The catalog gives information only.
An unknown future pair remains valid until model construction cannot resolve it.

### `available_api_key_models`

```text
def available_api_key_models() -> tuple[KnownModel, ...]
```

`available_api_key_models()` returns catalog models whose provider adapter is installed and accepts an explicit `api_key` argument.

`available_api_key_model_options()` returns the same installed subset as grouped `ModelSelectionOptions`.

Availability does not validate remote credentials or model access.
Provider-specific environment requirements still apply, including AWS region configuration for Bedrock and Bedrock Mantle.

## Agent compilation

`DefaultAgentCompiler.compile(definition, resolved)` implements `AgentCompiler`.
It returns an Ovid `AgentRuntime`.

Compilation maps:

- types
- instructions
- retries
- extensions
- policy
- concurrency
- observability

Invalid runtime values or construction failures raise `AgentConstructionError`.

## Extension adaptation

Import from `ovid_core.adapters.pydantic_ai.extensions`.

- `PydanticAIExtensions[Deps]` is a frozen dataclass containing adapted `capabilities` and `toolsets`.
- `adapt_agent_extensions(capabilities, toolsets, hooks, tool_approval=None, retries=AgentRetryPolicy())` returns the adapter bundle.

`adapt_agent_extensions`:

- validates IDs
- adapts provider integrations
- adapts skills integrations
- adapts MCP integrations
- combines capability and direct toolsets

Import from `ovid_core.adapters.pydantic_ai.tools`:

- `PydanticAIToolsetAdapter(source=..., hooks=(), tool_approval=None, deferred_capability_id=None, retries=AgentRetryPolicy())` adapts a `BaseToolset` to Pydantic AI lifecycle and execution.
  `deferred_capability_id` names the deferred capability that owns the tools.
- `PydanticAICapabilityAdapter(source)` adapts the instructions and model settings of an Ovid capability.
  It keeps static instruction strings and wraps each dynamic source in a Pydantic AI instruction function or model-settings callable that receives the Ovid `RunContext`.
  `adapt_agent_extensions` adapts the tools, toolsets, and hooks of the capability.

Each Ovid tool definition has `metadata['ovid_approval']` and `metadata['ovid_effect']` as JSON.
Import `resolve_effect(tool_def)` from `ovid_core.adapters.pydantic_ai.effects` or `ovid_core.adapters.pydantic_ai`.
It returns the `ToolEffect` of an Ovid tool, an MCP tool, or another Pydantic AI tool.
[Declaring tool effects](extensions.md#declaring-tool-effects) gives the rules.

`retries` supplies the tool retry budget, the tool failure mode, and the maximum failure text length to each adapted Ovid tool.
`DefaultAgentCompiler` passes `AgentDefinition.policy.retries`.
The adapter passes a Pydantic AI `ApprovalRequired` or `CallDeferred` exception from a tool to Pydantic AI unchanged in both failure modes.
It converts `ToolApprovalRequired` to `ApprovalRequired` and `ToolDeferred` to `CallDeferred`, with the same metadata and kind.
In `'return'` mode, the adapter also passes a Pydantic AI `ModelRetry` or `ToolFailed` exception unchanged.
In `'raise'` mode, the adapter wraps these two exceptions in `ToolExecutionError`.

`PydanticAIAgentRuntime` adds `DeferredToolRequests` to the output types of each run, not to the compiled agent.
`upstream_agent.output_type` stays the definition output type.
The runtime converts resume input to Pydantic AI `DeferredToolResults`:

| Ovid answer | Pydantic AI value |
| --- | --- |
| `ToolCallApproved()` | `ToolApproved()` in `approvals` |
| `ToolCallDenied(message)` | `ToolDenied(message)` in `approvals` |
| `ToolCallReturned(value)` | `value` in `calls` |
| `ToolCallFailed(message)` | `ToolFailed(message)` in `calls` |

For a run with a prompt and no resume input, the runtime closes each pending call of the last response as `interrupted`.
It adds one request with a tool result for each call, with the Pydantic AI text `INTERRUPTED_TOOL_RETURN_CONTENT`.
The request has no run ID, because Pydantic AI rejects a history that contains the ID of the current run.
The request is the first message of `result.messages`, and `result.history` is the input history followed by `result.messages`.

For `InterruptedRunResume`, the runtime gives no `DeferredToolResults` and passes `user_prompt=None` to Pydantic AI.
It also adds a capability whose `wrap_tool_execute` checks each call of the last response before the tool runs.
A call with a stored answer never runs again.
The capability gives back a success result as `SkipToolExecution(ToolReturn(...))` with its metadata.
It raises `ModelRetry` for a retry, and `ToolFailed` for a failed result.
It gives back a denied result as `SkipToolExecution(ToolDenied(...))`.
An open call that must not run raises `SkipToolExecution` with the UNKNOWN recovery text.
[Interrupted runs](agents.md#interrupted-runs) describes the rules.

Import `adapt_integration_capability` from `ovid_core.adapters.pydantic_ai.integrations`.

`adapt_integration_capability` adapts these capability types:

- `ProviderCapability`
- `SkillsCapability`
- `MCPServerCapability`

It returns `None` for other capability types.

During execution, the adapter:

- validates argument and result models
- applies approval policy
- applies timeouts
- applies hooks
- maps `ToolResult` to a Pydantic AI `ToolReturn`: `content` and `files` go to the model, and `metadata` stays out of band
- preserves cancellation

Duplicate tool or extension IDs raise `ExtensionCollisionError`.

## Message conversion

Import from `ovid_core.adapters.pydantic_ai.messages`.

```text
def message_from_pydantic(value: ModelMessage) -> AgentMessage
def message_to_pydantic(value: AgentMessage) -> ModelMessage
```

Both functions convert:

- message parts
- identities
- timestamps
- usage
- provider metadata
- finish reasons
- message state

The state conversion is exact in both directions.
`ModelRequest.state` maps to `AgentMessage.state` with the values `complete` and `interrupted`.
`ModelResponse.state` maps with the values `complete`, `interrupted`, `incomplete`, and `suspended`.
An `interrupted` message keeps its state after a store round trip, so Pydantic AI can close the unanswered tool calls on the next run.

`message_from_pydantic` does not raise for a part kind that Ovid Core does not model.
It converts that part to an `OpaqueRequestPart` or an `OpaqueResponsePart`.
It also converts a part to an opaque part when the typed Ovid part cannot hold its value.
Examples are a request part with more data than the typed part holds, a capability load call without an ID, and a tool call with an empty tool name.
`message_to_pydantic` restores the same upstream part from the payload of an opaque part.

The conversion of these parts is exact, except for part timestamps:

- `OpaqueRequestPart` and `OpaqueResponsePart`
- `NativeToolCallPart`, `NativeToolReturnPart`, and `FilePart`
- A `UserPromptPart` with a tuple of content items
- The content of a `ToolReturnPart` that holds files
- The `metadata` of a `ToolReturnPart` that holds a JSON value

`message_to_pydantic` selects the Pydantic AI subclass for a provider-run tool part from `tool_kind`.
For example, `tool_kind='tool-search'` restores a `NativeToolSearchCallPart`.

Other typed parts do not keep each Pydantic AI field.
[Pydantic AI fields that typed parts do not keep](runtime.md#pydantic-ai-fields-that-typed-parts-do-not-keep) lists those fields.

`message_from_pydantic` raises `ProviderError` only for these conditions:

- A run ID or a conversation ID that is not a UUID.
- Usage data that Ovid Core cannot normalize.
- A part with a value that Pydantic AI cannot serialize to JSON.

`message_to_pydantic` raises `ProviderError` for an invalid normalized conversion, such as a changed opaque payload.

## Result conversion

Import `result_from_pydantic` from `ovid_core.adapters.pydantic_ai.results`.

```text
def result_from_pydantic[Output](
    value: AgentRunResult[Output] | AgentRunResult[Output | DeferredToolRequests],
    *,
    approvals: Mapping[str, ToolApproval] = {},
) -> RunResult[Output] | DeferredRunResult
```

`result_from_pydantic`:

- converts the new messages and their usage
- converts UUIDs
- sorts metadata keys
- returns `DeferredRunResult` when the output is a Pydantic AI `DeferredToolRequests`
- validates the complete Ovid result

`approvals` maps a call ID to the Ovid approval of a tool that requires approval.
The runtime records these values when the run pauses and sets `DeferredToolRequest.approval` from them.

Invalid upstream state raises `ProviderError`.

### Partial transcripts

`PydanticAIAgentRuntime.stream` wraps the upstream run in Pydantic AI `capture_run_messages()`.
When the run raises an error or is cancelled, the stream builds a `PartialRunResult` from the captured messages before it raises the normalized error.
`PydanticAIStream.partial()` returns it.

Pydantic AI marks the message that the run stopped in:

- A stream that stops during a response gives a response with `state='interrupted'`.
- A cancellation during tool execution gives a request with `state='interrupted'`, which holds the tool results that finished.
- A response that crossed a token or cost limit gets `state='interrupted'`.

The adapter normalizes the error first, and the boundary response uses only the normalized message.
A context-window error from a provider normalizes to `ContextWindowError` in a stream, as in `run`.

### Transcript journal

`PydanticAIAgentRuntime.stream` adds an internal per-run capability when the call has a `RunJournal`.
The capability uses the run ID of the run and the request that closes pending calls on a new prompt, when there is one.
It has the ID `ovid_core.transcript_journal`.

The capability takes the `outermost` position, inside the per-run usage tracking capability.
Thus the usage check runs before the journal records a tool call, and the journal wraps every other capability.

The capability uses these Pydantic AI hooks:

| Hook | Journal write |
| --- | --- |
| `before_run` | Keeps the `RunStartedEntry` for the first append of the run. |
| `prepare_model_request` | `StepCommittedEntry` with the request and each run message that no step holds yet. Pydantic AI runs this hook after it records the instruction baseline or changes, model settings changes, tool changes, and capability loads on the request, and before the model call. |
| `after_node_run` | `StepCommittedEntry` with the new run messages, for example the model response. |
| `wrap_tool_execute` | `ToolIntentEntry` before the handler, and `ToolSettledEntry` after it. |

The intent append completes before the tool runs.
The settle entry comes from `wrap_tool_execute` because Pydantic AI does not call `after_tool_execute` or `on_tool_execute_error` for `ToolFailed`, `ModelRetry`, `SkipToolExecution`, `ApprovalRequired`, or `CallDeferred`.
The capability hooks into Pydantic AI tool execution, so it records Ovid tools, passthrough Pydantic AI tools, and MCP tools.
The run messages are the messages with the run ID of the run, converted with `message_from_pydantic`.
[Step commits](persistence.md#step-commits) describes the entries and the end-of-run writes.

`PydanticAIToolsetAdapter.idempotency_key(name, arguments)` returns the idempotency key of a call to an Ovid tool of the latest tool listing.
It returns `None` for a tool that the listing does not contain and for arguments that are not valid.

## Usage conversion

Import from `ovid_core.adapters.pydantic_ai.usage`.

| Function | Contract |
| --- | --- |
| `request_usage_from_pydantic(value, provider_namespace=...)` | Normalizes one request, preserving provider details under the supplied namespace. |
| `usage_from_pydantic(value, requests)` | Aggregates normalized requests and rejects disagreement with upstream run totals. |
| `aggregate_usage_from_pydantic(value)` | Converts aggregate counters when request detail is unavailable. |

Invalid or inconsistent provider usage raises `ProviderError`.

## Fallback compilation

Import `compile_fallback_model` from `ovid_core.adapters.pydantic_ai.routing`.

```text
def compile_fallback_model(
    *,
    model_id: str,
    handles: Sequence[ModelHandle],
) -> ModelHandle
```

The function returns one handle without a change.

For multiple handles, the function creates a Pydantic AI `FallbackModel`.
The new handle reports capabilities that all candidates support.
For handles with resolvers, the compiler selects current candidate models before each new logical request.
It creates a replacement `FallbackModel` only when a candidate changes.
The candidate order and fallback policy remain unchanged.

Its context window is the smallest candidate window when every candidate supplies metadata.
It is unknown when any candidate window is unknown.

Authentication and invalid-request errors stop the route.
Other applicable final errors can move to the next model.

## AG-UI bridge

Import from `ovid_core.adapters.pydantic_ai.ag_ui` when building a custom transport.

- `AGUINativeEvent` aliases Pydantic AI's `NativeEvent`.
- `CompletionCallback[Output]` is an async callback receiving normalized `RunResult[Output] | DeferredRunResult`.
- `AGUIDeferredToolsError` is the `AgentRunError` that ends a run that pauses for a deferred tool call.
- `AGUIAuthorityError` reports client attempts to control server-authoritative run state.
- `PydanticAIAGUIRun(agent, agent_id, body, accept)` parses trusted AG-UI input and requires a Pydantic AI runtime.

`PydanticAIAGUIRun` exposes:

- the deterministic `conversation_id`
- `native_stream(deps, messages, conversation_id)`
- `stream(events, on_complete=...)`
- `streaming_response(events)`
- `partial()`, which returns the `PartialRunResult` of a failed or cancelled `native_stream`, or `None`

The bridge uses `upstream_agent` without `DeferredToolRequests` in its output types.
A pause therefore ends the native stream with `AGUIDeferredToolsError`.

Most applications should use `create_ag_ui_app` instead.

## Starlette boundary

Low-level Starlette factories are available for applications that require a concrete `Starlette` return type:

- `ovid_core.adapters.starlette.app.create_starlette_app(...)`
- `ovid_core.adapters.starlette.ag_ui.create_starlette_ag_ui_app(...)`

These factories use the same arguments as the high-level server factories.
They do not check optional dependencies.

Use the high-level factories unless your application needs a concrete Starlette object.

`ovid_core.adapters.starlette.http` also exposes transport helpers for custom Starlette adapters:

- `read_json_body(request, *, max_body_bytes, timeout_seconds) -> bytes`
- `request_context(request) -> RequestContext`
- `BodyTooLargeError`
- `ClientAuthorityError`
- `InvalidRequestError`
- `UnsupportedMediaTypeError`

These helpers enforce the same request-body and authority rules as the built-in transports.

## Codex adapter

See [Codex subscription](codex.md) for `CodexSubscriptionModelFactory`.
