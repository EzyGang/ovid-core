# Tools and capabilities

## Tool values

Import from `ovid_core.tools.models`.

### `ToolApproval`

- `required: bool = False`
- `reason: str | None = None`
- `metadata: dict[str, JsonValue] = {}`

The approval value controls the Pydantic AI tool definition.
When `required=True`, Pydantic AI defers the call before tool execution.
The application can approve or reject the exact call.

Set `AgentDefinition.tool_approval` to override this value for every Ovid tool in one agent.
For example, `ToolApproval(required=False)` removes all approval pauses for Ovid tools.
The normal workspace and tool checks still apply.

The override does not change tools from a Pydantic AI capability passthrough.

### `ToolResult`

- `content: JsonValue` contains the required result content.
- `metadata: dict[str, JsonValue] = {}` contains non-secret result metadata.

## Tool contracts

Import from `ovid_core.tools.base`.

### `ToolExecutionContext[Deps]`

Frozen dataclass with:

- `run: RunContext[Deps]`
- `tool_call_id: str`
- `approved: bool = False`
- `approval_metadata: JsonValue = None`

### `BaseTool[Deps, Args, Result]`

Subclass this abstract base and define:

```python
class MyTool(BaseTool[Deps, ArgsModel, ResultModel]):
    id: str
    description: str
    args_type: type[ArgsModel]
    result_type: type[ResultModel]
    approval: ToolApproval = ToolApproval()
    timeout_seconds: float | None = None
    defer_loading: bool = False
    presentation: ToolPresentation = ToolPresentation()

    async def execute(
        self,
        context: ToolExecutionContext[Deps],
        arguments: ArgsModel,
    ) -> ResultModel: ...
```

`Args` must inherit Ovid `BaseModel`. `Result` must inherit `ToolResult`.

`ToolPresentation` optionally supplies an effective `wire_name`, `input_format`, and Lark `ToolGrammar`. `id` remains
the stable Ovid identity. Effective wire-name collisions fail deterministically. Each advertised `ToolsetTool` retains
the exact bound Ovid tool instance, so a call from an earlier model step cannot dispatch to a later dynamic definition.
JSON schema input remains the fallback when the provider or supported Pydantic AI API cannot advertise text grammar.

`descriptor(source=..., approval=...)` returns the effective `AgentToolDescriptor` used during agent preparation.
The descriptor applies the effective wire name and optional agent-wide approval override.

The adapter validates tool input and output. It also applies approval policy, timeouts, hooks, and typed tool errors.

### `BaseToolset[Deps]`

Define `id` and implement `async get_tools(context) -> Sequence[BaseTool[Deps, Any, Any]]`. Lifecycle methods have no-op defaults and may be overridden:

- `for_run(context) -> Self`
- `for_step(context) -> Self`
- `__aenter__() -> Self`
- `__aexit__(exception_type, exception, traceback) -> bool | None`

`descriptor(source=...)` returns the dynamic `AgentToolsetDescriptor` used during agent preparation.

## Tool hooks

Import `BaseToolHook[Deps]` from `ovid_core.hooks.base`. Override any async method:

```python
await before_tool(context, tool_id, arguments)
await after_tool(context, tool_id, result)
await on_tool_error(context, tool_id, error)
```

Defaults do nothing. `arguments` is a validated `BaseModel`, `result` is a `ToolResult`, and `error` is `ToolExecutionError`.

## Capability contracts

Import from `ovid_core.capabilities.base`.

`CapabilityModelSettings(values={})` carries model-setting contributions.

`CapabilityContributions[Deps]` is a frozen dataclass containing:

- `instructions: tuple[str, ...] = ()`
- `tools: tuple[BaseTool[Deps, Any, Any], ...] = ()`
- `toolsets: tuple[BaseToolset[Deps], ...] = ()`
- `hooks: tuple[BaseToolHook[Deps], ...] = ()`
- `model_settings: CapabilityModelSettings = CapabilityModelSettings()`

`BaseCapability[Deps]` is an immutable, keyword-only dataclass. It contains `id`, optional `description`,
`defer_loading`, `contributions`, and inspectable `AgentServiceRequirement` values. Its default `bind(services)`
validates requirements and returns itself. Stateful capabilities return a frozen bound value whose contributions use
the resolved service providers.

`descriptor(source=...)` returns the `AgentCapabilityDescriptor` with the capability instructions and deferred-loading state.

All capability IDs, toolset IDs, and effective tool wire names must be unique. Collisions raise
`ExtensionCollisionError`.

## Pydantic AI capability passthrough

Import `pydantic_ai_capability` from `ovid_core.adapters.pydantic_ai`. It accepts a Pydantic AI
`AbstractCapability` and returns an Ovid `BaseCapability` that the default compiler recognizes:

```python
from pydantic_ai_harness.planning import Planning

from ovid_core.adapters.pydantic_ai import pydantic_ai_capability
from ovid_native.search import SearchCapability


capabilities = (
    SearchCapability[AppDependencies](),
    pydantic_ai_capability(Planning()),
)
```

The default Pydantic AI compiler passes the exact source capability instance to Pydantic AI. Its instructions, toolsets,
native tools, model settings, deferred loading, ordering, `for_agent`, `for_run`, and lifecycle hooks keep their upstream
behavior. This also lets Pydantic AI Harness capabilities run beside Ovid Native capabilities.

An always-available capability without an explicit ID receives a stable snake-case class ID on both the port and source.
Deferred capabilities require an explicit upstream ID because message history must refer to a stable value. Explicit IDs
must be non-empty and trimmed. Ovid rejects collisions between passthrough and Ovid capability IDs.

Passthrough does not convert upstream tools into Ovid `BaseTool` values. Ovid approval metadata, `BaseToolHook` hooks,
tool-result validation, service requirements, and Ovid tool timeout policy do not apply to those upstream tools. The
source capability keeps its own Pydantic AI behavior. Applications must install its optional dependencies and own any
resources it opens.

Capabilities that replace model selection, add message or event values outside Ovid's normalized unions, or impose
durable-execution rules may conflict with Ovid routing, diagnostics, persistence, streaming, or per-run usage tracking.
Test those combinations through the public Ovid run and stream APIs. A custom `AgentCompiler` must implement this
adapter-specific port itself.

## Agent service contracts

Import these values from `ovid_core.services`:

- `AgentServiceKey`
- `AgentServiceRef`
- `AgentServiceBinding`
- `AgentServiceRequirement`
- `AgentServices`

A namespaced ID and positive API version identify each key.
References add an identifier name.
The immutable registry rejects duplicate bindings.
It validates declared runtime value types.
Requirements can also name mandatory provider features.

`AgentDefinition.services` defaults to an empty registry. `AgentFactory` validates and binds capabilities once before
compilation. Missing or incompatible services raise narrow `AgentServiceError` subclasses during construction.

## Explicit plugin factories

`PluginRegistrar` records namespaced factories for service providers, service configurators, and capabilities.
Registration does not activate a factory.
Applications select IDs with `select_service_factories(...)` and `select_capability_factories(...)`.
Invalid selections raise `PluginError`.
This includes unknown, empty, duplicate, and incompatible selections.

Provider factories receive `PluginActivationContext` and application-selected JSON configuration.
They return one `AgentServiceBinding`.
Configurators target one selected provider ID and return a configured binding.
They cannot replace an unselected provider.

Capability factories declare `AgentServiceRequirement` values before activation.
They resolve the assembled `AgentServices` from their activation context.
The registry preserves requested order.
Applications can start selected providers deterministically.
They close owned services in reverse order.

Plugin code is trusted executable code. Installing or discovering it does not register a provider, configure a
service, contribute a capability, or change an agent.

## Relay

Import Relay contracts and implementations from `ovid_core.relay`.
Relay is off by default.
Only `RelayCapability(connection=connection)` contributes Relay tools.
`AgentFactory` does not create or configure a connection.
For a task-oriented walkthrough, see [Use Relay between agents](../guides/use-relay.md).

### Values and connection

- `RelayAddress` is a validated opaque non-empty string root.
- `RelayIdentity` contains `address` and `display_name`.
- `RelayMessageId` is a UUID root with `new()`.
- `RelayMessage` contains `id`, `sender`, `recipient`, `content`, timezone-aware `sent_at`, and optional `reply_to`.
- `RelayReceipt` contains `message_id`, `recipient`, and timezone-aware `accepted_at`. It records backend acceptance, not reading or delivery.
- `RelayContact` contains `address` and `display_name`. Relay does not expose presence or lifecycle status.

`RelayConnection` is structurally typed and bound to one `identity`. It defines:

```text
identity: RelayIdentity
set_delivery_handler(handler: RelayDeliveryHandler | None) -> None
await send(recipient, content, reply_to=None) -> RelayReceipt
await wait(sender=None, reply_to=None, timeout_seconds=None) -> RelayMessage | None
await pending(retain=False) -> tuple[RelayMessage, ...]
await contacts() -> tuple[RelayContact, ...]
```

`RelayDeliveryHandler` is an async callable from `RelayMessage` to `RelayDisposition.ACKNOWLEDGE` or
`RelayDisposition.DEFER`. Acknowledgement consumes the message. Defer, a handler exception, or no handler retains it.
An active matching `wait` takes precedence. Handler work is asynchronous to sender acceptance.

`InMemoryRelay(capacity=100)` is an explicit process-local network.
Call synchronous `connection(identity, delivery_handler=None)` for each unique identity.
Connections from the same instance can communicate.
Connections from separate instances cannot communicate.
Each mailbox has a fixed capacity.
It rejects overflow with `RelayCapacityError`.

Relay operations use narrow errors:

- Unknown recipients raise `UnknownRelayRecipientError`.
- Duplicate live addresses raise `RelayAddressInUseError`.
- Full mailboxes raise `RelayCapacityError`.
- Closed connections raise `RelayUnavailableError`.

### Automatic tools

`RelayCapability[Deps](connection=connection)` has the fixed capability ID `relay` and contributes:

| Tool | Arguments | Result |
| --- | --- | --- |
| `relay_send` | `to`, `message`, optional `reply_to` | accepted `receipt` |
| `relay_wait` | optional `sender`, `reply_to`, `timeout_seconds` | one consumed `message` or `None` |
| `relay_pending` | `retain=False` | FIFO `messages`, consumed unless retained |
| `relay_contacts` | none | registered `contacts` other than self |

When supplied, `reply_to` requires an exact match.
A wait timeout returns `None`.
This includes an immediate check with zero.
Cancellation continues to the caller.
Applications can replace each built-in model-visible description.
They do not need to replace the implementation.

```python
from ovid_core.relay import RelayCapability, RelayToolDescriptions

relay_capability = RelayCapability[AppDeps](
    connection=connection,
    tool_descriptions=RelayToolDescriptions(
        send='Send delegation updates to a known contact.',
        wait='Wait for a correlated delegation response.',
        pending='Read delegation messages not delivered automatically.',
        contacts='List Relay contacts visible to this agent.',
    ),
)
```

These values change only the tool definitions presented to the model. Put broader workflow instructions in
`AgentDefinition.instructions`.


## Provider capabilities

Import from `ovid_core.capabilities.integrations`.

`ThinkingEffort = bool | Literal['minimal', 'low', 'medium', 'high', 'xhigh']`.

`ProviderCapability[Deps]` extends `BaseCapability`, requires a `config: ProviderCapabilityConfig`, and has adapter-owned empty contributions. `ProviderCapabilityConfig` is a discriminated union on `kind`:

| Config | Fields and defaults |
| --- | --- |
| `ThinkingCapabilityConfig` | `effort=True`. Accepts `bool`, `minimal`, `low`, `medium`, `high`, or `xhigh`. |
| `WebSearchCapabilityConfig` | optional `search_context_size`, allowed/blocked domains, positive `max_uses`, and `external_web_access`. |
| `WebFetchCapabilityConfig` | optional allowed/blocked domains, positive `max_uses`, `enable_citations`, and positive `max_content_tokens`. |
| `ImageGenerationCapabilityConfig` | optional `action` (`generate`, `edit`, `auto`), `output_format`, `quality`, and supported `size`. |
| `XSearchCapabilityConfig` | optional allowed/excluded handles, date range, image/video understanding, and output inclusion. |
| `ToolSearchCapabilityConfig` | optional `strategy` (`keywords`, `bm25`, `regex`) and `max_results=10`. |
| `OpenAICompactionCapabilityConfig` | optional `stateless`, positive token threshold, and positive message-count threshold. Mode validation enforces the matching threshold. |
| `AnthropicCompactionCapabilityConfig` | `token_threshold=150000` with minimum 50000, optional instructions, and `pause_after_compaction=False`. |

The adapter rejects a provider capability unsupported by the selected upstream provider.

## Agent Skills

Import from `ovid_core.skills`.

`SkillLibraryConfig` requires at least one `directories: tuple[Path, ...]`. Set either `include` or `exclude`, never both. `SkillsCapability[Deps]` is keyword-only, requires `id` and `config`, fixes `description=None`, and defaults to deferred loading.

```python
from pathlib import Path
from ovid_core.skills import SkillLibraryConfig, SkillsCapability

skills = SkillsCapability[None](
    id='project-skills',
    config=SkillLibraryConfig(directories=(Path('.agents/skills'),)),
)
```

## MCP

Import transport values from `ovid_core.mcp.models`.

### Values and transports

`MCPValues` separates `plain: dict[str, str]` from `credentials: dict[str, CredentialRef]`. The same name cannot appear in both mappings.

- `MCPStdioTransportConfig`: `kind='stdio'`, non-empty `command`, `args=()`, optional `cwd`, and `environment=MCPValues()`.
- `MCPHTTPTransportConfig`: `kind='http'`, validated HTTP `url`, and `headers=MCPValues()`.
- `MCPTransportConfig`: discriminated union of the two.

### `MCPServerConfig`

| Field | Default |
| --- | --- |
| `id` | Required non-empty string |
| `transport` | Required `MCPTransportConfig` |
| `include_tools` | `None` |
| `namespace` | `None`. The value must be non-empty when present. |
| `include_instructions` | `True` |
| `defer_loading` | `False` |
| `description` | `None` |

### Configuration construction

Add server definitions to `OvidConfig.mcp_servers`. `AgentFactory` creates the corresponding capabilities and adds them to every built agent.

Pass `credential_resolver` to `AgentFactory` when configured MCP values contain credential references.

Use `create_mcp_capability` directly only for an agent-specific or application-generated server definition.

Import `MCPServerCapability` and `create_mcp_capability` from `ovid_core.mcp.capability`.

```text
async def create_mcp_capability[Deps](
    config: MCPServerConfig,
    *,
    resolver: CredentialResolver | None = None,
) -> MCPServerCapability[Deps]
```

The factory resolves the referenced environment variables or HTTP headers concurrently.

The factory does not serialize or show the resolved `SecretStr` values. A resolver is optional when there are no credential references.

Missing resolver support raises `CredentialError`.
