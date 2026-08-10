# Pydantic AI adapters

Third-party runtime objects live under `ovid_core.adapters`. Domain code should depend on Ovid-owned values and use these APIs only at composition boundaries.

## Model construction

Import from `ovid_core.adapters.pydantic_ai.models`.

### `DefaultModelFactory`

Implements `ModelFactory`:

```python
factory = DefaultModelFactory(provider_api_key=None)
handle = await factory.build(model_id=model_id, config=model_config)
```

The factory passes `provider:model` to upstream model inference. The built-in test model maps to `test`.

It merges model settings, applies model concurrency, normalizes capabilities, and returns an opaque handle.

When `provider_api_key` returns a key, the factory passes that key to the inferred provider constructor. Construction failures become source-safe `ModelResolutionError` values.

### `known_models`

```python
def known_models() -> tuple[KnownModel, ...]
```

Delegates to the Pydantic AI model catalog. It divides each identifier into a typed provider and model pair.

The catalog gives information only. An unknown future pair remains valid until model construction cannot resolve it.

## Agent compilation

`DefaultAgentCompiler.compile(definition, resolved)` implements `AgentCompiler`. It returns an Ovid `AgentRuntime`.

Compilation maps types, instructions, retries, extensions, policy, concurrency, and observability. Invalid runtime values or construction failures raise `AgentConstructionError`.

## Extension adaptation

Import from `ovid_core.adapters.pydantic_ai.extensions`.

- `PydanticAIExtensions[Deps]` is a frozen dataclass containing adapted `capabilities` and `toolsets`.
- `adapt_agent_extensions(capabilities, toolsets, hooks)` validates IDs, adapts provider/skills/MCP integrations, combines capability and direct toolsets, and returns the adapter bundle.

Import from `ovid_core.adapters.pydantic_ai.tools`:

- `PydanticAIToolsetAdapter(source=..., hooks=())` adapts a `BaseToolset` to Pydantic AI lifecycle and execution.
- `PydanticAICapabilityAdapter(source, hooks=(), include_toolset=True)` adapts Ovid capability contributions.
- `adapt_capabilities(capabilities)` adapts a sequence after collision validation.

Import `adapt_integration_capability` from `ovid_core.adapters.pydantic_ai.integrations`.

This function adapts `ProviderCapability`, `SkillsCapability`, and `MCPServerCapability`. It returns `None` for other capability types.

Adapter execution validates argument and result models. It applies approval policy, timeouts, and hooks.

The adapter maps `ToolResult` to JSON and preserves cancellation. Duplicate tool or extension IDs raise `ExtensionCollisionError`.

## Message conversion

Import from `ovid_core.adapters.pydantic_ai.messages`.

```python
def message_from_pydantic(value: ModelMessage) -> AgentMessage
def message_to_pydantic(value: AgentMessage) -> ModelMessage
```

Both functions convert message parts, identities, timestamps, usage, provider metadata, and finish reasons.

Unsupported or invalid upstream messages raise `ProviderError`. Invalid normalized conversions also raise `ProviderError`.

## Result conversion

Import `result_from_pydantic` from `ovid_core.adapters.pydantic_ai.results`.

```python
def result_from_pydantic[Output](
    value: AgentRunResult[Output],
) -> RunResult[Output]
```

Converts the new messages and their usage. It also converts UUIDs, sorts metadata keys, and validates the complete Ovid result.

Invalid upstream state raises `ProviderError`.

## Usage conversion

Import from `ovid_core.adapters.pydantic_ai.usage`.

### Upstream field classification

`UpstreamUsageField(name, classification)` is a frozen dataclass. Classification is `stable`, `optional`, `provider_specific`, or `upstream_private`. `PYDANTIC_AI_USAGE_FIELDS` describes the exact upstream fields currently normalized.

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

```python
def compile_fallback_model(
    *,
    model_id: str,
    handles: Sequence[ModelHandle],
) -> ModelHandle
```

The function returns one handle without a change.

For multiple handles, it makes a Pydantic AI `FallbackModel`. The new handle reports capabilities that all candidates support.

Authentication and invalid-request errors stop the route. Other applicable final errors can move to the next model.

## AG-UI bridge

Import from `ovid_core.adapters.pydantic_ai.ag_ui` when building a custom transport.

- `AGUINativeEvent` aliases Pydantic AI's `NativeEvent`.
- `CompletionCallback[Output]` is an async callback receiving normalized `RunResult[Output]`.
- `AGUIAuthorityError` reports client attempts to control server-authoritative run state.
- `PydanticAIAGUIRun(agent, agent_id, body, accept)` parses trusted AG-UI input and requires a Pydantic AI runtime.

`PydanticAIAGUIRun` exposes the deterministic `conversation_id`, `native_stream(deps, messages, conversation_id)`, `stream(events, on_complete=...)`, and `streaming_response(events)`. Most applications should use `create_ag_ui_app` instead.

## Starlette boundary

Low-level Starlette factories are available for applications that require a concrete `Starlette` return type:

- `ovid_core.adapters.starlette.app.create_starlette_app(...)`
- `ovid_core.adapters.starlette.ag_ui.create_starlette_ag_ui_app(...)`

These factories use the same arguments as the high-level server factories. They do not check optional dependencies.

Use the high-level factories unless your application needs a concrete Starlette object.

`ovid_core.adapters.starlette.http` also exposes transport helpers for custom Starlette adapters:

- `read_json_body(request, *, max_body_bytes, timeout_seconds) -> bytes`
- `request_context(request) -> RequestContext`
- `BodyTooLargeError`, `ClientAuthorityError`, `InvalidRequestError`, and `UnsupportedMediaTypeError`

These helpers enforce the same request-body and authority rules as the built-in transports.

## Codex adapter

See [Codex subscription](codex.md) for `CodexSubscriptionModelFactory`.
