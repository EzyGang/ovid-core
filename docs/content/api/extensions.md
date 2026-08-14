# Tools and capabilities

## Tool values

Import from `ovid_core.tools.models`.

### `ToolApproval`

- `required: bool = False`
- `reason: str | None = None`
- `metadata: dict[str, JsonValue] = {}`

### `ToolResult`

- `content: JsonValue` contains the required result content.
- `metadata: dict[str, JsonValue] = {}` contains non-secret result metadata.

### Tool presentation

`ToolPresentation` separates stable Ovid identity from model-visible syntax:

- `wire_name: str | None = None` overrides the name advertised to the model.
- `input_format: Literal['json', 'text'] = 'json'` selects structured input or one complete string.
- `grammar: ToolGrammar | None = None` carries a Lark grammar for adapters that support constrained text tools.

`ToolGrammar` has `syntax='lark'` and a non-empty `definition`.

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
    presentation: ToolPresentation = ToolPresentation()
    timeout_seconds: float | None = None
    defer_loading: bool = False

    async def execute(
        self,
        context: ToolExecutionContext[Deps],
        arguments: ArgsModel,
    ) -> ResultModel: ...
```

`Args` must inherit Ovid `BaseModel`. `Result` must inherit `ToolResult`. Text-input tools use a root model that validates one
complete string. The Pydantic AI adapter exposes those tools as `{"input": "..."}` JSON when the provider API has no public
grammar-constrained custom-tool contract.

The adapter validates tool input and output. It also applies approval policy, timeouts, hooks, and typed tool errors. Dispatch is
pinned to the `ToolsetTool` definition from the advertising model step, so a later dynamic schema cannot redirect an earlier call.

### `BaseToolset[Deps]`

Define `id` and implement `async get_tools(context) -> Sequence[BaseTool[Deps, Any, Any]]`. Lifecycle methods have no-op defaults and may be overridden:

- `for_run(context) -> Self`
- `for_step(context) -> Self`
- `__aenter__() -> Self`
- `__aexit__(exception_type, exception, traceback) -> bool | None`

`for_step` may return another toolset with different definitions. Effective wire-name collisions fail during each discovery step.

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

`BaseCapability[Deps]` is an immutable, keyword-only dataclass. It contains `id`, optional `description`, inspectable service
`requirements`, `defer_loading`, and `contributions`. Its default `bind(services)` validates every requirement and returns the
same capability. Stateful capabilities return a frozen bound capability with provider-backed contributions.

Capability IDs, tool IDs, effective tool wire names, and toolset IDs must be unique in their namespaces. Collisions raise
`ExtensionCollisionError`.

## Plugin factory contracts

Import generic plugin contracts from `ovid_core.plugins`. `PluginActivationContext.services` exposes the explicitly constructed
service registry. `AgentServiceProviderFactory`, `AgentServiceConfiguratorFactory`, and `CapabilityFactory` register factories,
never process-global service instances.

`PluginFactories` rejects empty or duplicate contribution IDs. Applications select provider, configurator, and capability IDs
explicitly. `binding(...)` applies selected configurators in order and rejects a configurator that targets or replaces another
provider. Discovery and installation do not call factories or alter an agent definition.

## Relay

Import Relay contracts and implementations from `ovid_core.relay`. Relay is off by default: only
`RelayCapability(connection=connection)` contributes Relay tools, and `AgentFactory` does not create or configure a connection.
For a task-oriented walkthrough, see [Use Relay between agents](../guides/use-relay.md).

### Values and connection

- `RelayAddress` is a validated opaque non-empty string root.
- `RelayIdentity` contains `address` and `display_name`.
- `RelayMessageId` is a UUID root with `new()`.
- `RelayMessage` contains `id`, `sender`, `recipient`, `content`, timezone-aware `sent_at`, and optional `reply_to`.
- `RelayReceipt` contains `message_id`, `recipient`, and timezone-aware `accepted_at`. It records backend acceptance, not reading or delivery.
- `RelayContact` contains `address` and `display_name`; Relay does not expose presence or lifecycle status.

`RelayConnection` is structurally typed and bound to one `identity`. It defines:

```python
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

`InMemoryRelay(capacity=100)` is an explicit process-local network. Call synchronous
`connection(identity, delivery_handler=None)` for each unique identity. Connections from the same instance can communicate;
connections from separate instances cannot. Each mailbox is bounded and rejects overflow with `RelayCapacityError`.
Unknown recipients raise `UnknownRelayRecipientError`; duplicate live addresses raise `RelayAddressInUseError`; full mailboxes raise
`RelayCapacityError`; closed connections raise `RelayUnavailableError`.

### Automatic tools

`RelayCapability[Deps](connection=connection)` has the fixed capability ID `relay` and contributes:

| Tool | Arguments | Result |
| --- | --- | --- |
| `relay_send` | `to`, `message`, optional `reply_to` | accepted `receipt` |
| `relay_wait` | optional `sender`, `reply_to`, `timeout_seconds` | one consumed `message` or `None` |
| `relay_pending` | `retain=False` | FIFO `messages`; consumed unless retained |
| `relay_contacts` | none | registered `contacts` other than self |

When supplied, `reply_to` is matched exactly. A wait timeout returns `None`, including an immediate check with zero; cancellation
continues to the caller.
Consumer applications can replace the model-visible description of each built-in tool without replacing its implementation:

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

```python
async def create_mcp_capability[Deps](
    config: MCPServerConfig,
    *,
    resolver: CredentialResolver | None = None,
) -> MCPServerCapability[Deps]
```

The factory resolves the referenced environment variables or HTTP headers concurrently.

The factory does not serialize or show the resolved `SecretStr` values. A resolver is optional when there are no credential references.

Missing resolver support raises `CredentialError`.
