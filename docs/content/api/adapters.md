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
- `PydanticAICapabilityAdapter(source, hooks=(), include_toolset=True, tool_approval=None, retries=AgentRetryPolicy())` adapts Ovid capability contributions.
- `adapt_capabilities(capabilities, tool_approval=None, retries=AgentRetryPolicy())` adapts a sequence after collision validation.

`retries` supplies the tool retry budget, the tool failure mode, and the maximum failure text length to each adapted Ovid tool.
`DefaultAgentCompiler` passes `AgentDefinition.policy.retries`.
In `'return'` mode, the adapter passes a Pydantic AI `ModelRetry`, `ToolFailed`, `ApprovalRequired`, or `CallDeferred` exception from a tool to Pydantic AI unchanged.
In `'raise'` mode, the adapter wraps these exceptions in `ToolExecutionError`.

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
- maps `ToolResult` to JSON
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
    value: AgentRunResult[Output],
) -> RunResult[Output]
```

`result_from_pydantic`:

- converts the new messages and their usage
- converts UUIDs
- sorts metadata keys
- validates the complete Ovid result

Invalid upstream state raises `ProviderError`.

## Usage conversion

Import from `ovid_core.adapters.pydantic_ai.usage`.

### Upstream field classification

`UpstreamUsageField(name, classification)` is a frozen dataclass.
The classification is one of:

- `stable`
- `optional`
- `provider_specific`
- `upstream_private`

`PYDANTIC_AI_USAGE_FIELDS` describes the exact upstream fields that the adapter normalizes.

### Functions

| Function | Contract |
| --- | --- |
| `request_usage_from_pydantic(value, provider_namespace=...)` | Normalizes one request, preserving provider details under the supplied namespace. |
| `usage_from_pydantic(value, requests)` | Aggregates normalized requests and rejects disagreement with upstream run totals. |
| `aggregate_usage_from_pydantic(value)` | Converts aggregate counters when request detail is unavailable. |
| `usage_update_event_from_pydantic(value, *, completed_requests, provider_namespace, run_id, conversation_id, sequence)` | Creates a stable incremental `UsageUpdateEvent`. |

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
- `CompletionCallback[Output]` is an async callback receiving normalized `RunResult[Output]`.
- `AGUIAuthorityError` reports client attempts to control server-authoritative run state.
- `PydanticAIAGUIRun(agent, agent_id, body, accept)` parses trusted AG-UI input and requires a Pydantic AI runtime.

`PydanticAIAGUIRun` exposes:

- the deterministic `conversation_id`
- `native_stream(deps, messages, conversation_id)`
- `stream(events, on_complete=...)`
- `streaming_response(events)`

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
