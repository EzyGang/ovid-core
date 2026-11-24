# Tools and capabilities

## Tool values

Import from `ovid_core.tools.models`.

### `ToolApproval`

- `required: bool = False`
- `reason: str | None = None`
- `metadata: dict[str, JsonValue] = {}`

The approval value controls the Pydantic AI tool definition.
When `required=True`, the run pauses before the tool runs.
The run returns a `DeferredRunResult` with one `approval` request for the call.
The request carries the call arguments and this `ToolApproval` value, which includes `reason`.
The application approves or denies the exact call in the next run.
[Deferred runs](agents.md#deferred-runs) shows the resume step.

Set `AgentDefinition.tool_approval` to override this value for every Ovid tool in one agent.
For example, `ToolApproval(required=False)` removes all approval pauses for Ovid tools.
The normal workspace and tool checks still apply.

The override does not change tools from a Pydantic AI capability passthrough.

### `ToolResult`

- `content: JsonValue` contains the required result content.
- `metadata: dict[str, JsonValue] = {}` contains non-secret result metadata.

## Deferred tool values

Import from `ovid_core.tools` or `ovid_core.tools.deferred`.
All values are frozen Ovid models that serialize to JSON.

### `DeferredToolRequest`

One tool call that waits for the application.

- `tool_call_id: str`
- `tool_name: str`
- `arguments: dict[str, JsonValue]` contains the arguments that the model sent.
- `kind: DeferredToolKind` is `approval`, `external`, or `question`.
- `approval: ToolApproval | None` is the effective `ToolApproval` when the approval setting of the tool caused the pause.
- `metadata: dict[str, JsonValue] | None` is the metadata of `ToolApprovalRequired` or `ToolDeferred`.

`approval` is `None` for a call that raised `ToolApprovalRequired`.
It is also `None` for a tool from a Pydantic AI capability passthrough.

### `DeferredToolRequests`

- `calls: tuple[DeferredToolRequest, ...]` contains at least one call. The call IDs are unique.

`resume(answers)` takes a mapping from call ID to answer and returns `DeferredToolResume`.
Use this method to build the resume input.

### Answers

`DeferredToolAnswer` is the discriminated union of these models:

| Model | `kind` | Answers a call of kind | Result |
| --- | --- | --- | --- |
| `ToolCallApproved()` | `approved` | `approval` | The tool runs with `ToolExecutionContext.approved=True`. |
| `ToolCallDenied(message='The tool call was denied.')` | `denied` | `approval` | The tool does not run. The model receives `message` with outcome `denied`. |
| `ToolCallReturned(value)` | `returned` | `external`, `question` | The model receives the JSON `value` with outcome `success`. |
| `ToolCallFailed(message)` | `failed` | `external`, `question` | The model receives `message` with outcome `failed`. |

An answer cannot change the call arguments.

### `DeferredToolResume`

- `requests: DeferredToolRequests`
- `answers: dict[str, DeferredToolAnswer]`

The model checks the answers against the requests each time it is built, also from JSON.
It raises `DeferredToolAnswerError` in these conditions:

- A pending call has no answer.
- An answer names a call that is not pending.
- An answer does not match the call kind. For example, a `ToolCallReturned` cannot answer an `approval` call.

## Tool contracts

Import from `ovid_core.tools.base`.

### `ToolExecutionContext[Deps]`

Frozen dataclass with:

- `run: RunContext[Deps]`
- `tool_call_id: str`
- `approved: bool = False`
- `approval_metadata: JsonValue = None`

`approved` is `True` when the application approved this call with `ToolCallApproved` or a Pydantic AI capability approved it.
`approval_metadata` holds the metadata that a Pydantic AI capability supplies with an approval.
An Ovid resume supplies no approval metadata, so the value is `None`.

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

The adapter validates tool input and output. It also applies approval policy, timeouts, hooks, and tool failure results.

### Tool failure results

By default, a tool failure does not stop the run.
The adapter returns the failure to the model as a tool result.
The model can then correct the call or continue with a different step.

| Failure | Result outcome | Result text | Uses the retry budget |
| --- | --- | --- | --- |
| The arguments do not match `args_type`. | `retried` | `Invalid arguments for tool '<name>':` and one `<field>: <message>` line for each error | Yes |
| The tool raises `ToolRetryError`. | `retried` | The error text | Yes |
| The model calls an unknown tool name. | `retried` | The Pydantic AI message that lists the available tools | Yes |
| The tool raises `ToolExecutionError`. | `failed` | The error text | No |
| The tool does not complete in `timeout_seconds`. | `failed` | `Tool '<name>' timed out after <seconds>s` | No |
| The tool returns a value that does not match `result_type`. | `failed` | `Tool '<name>' returned an invalid result` | No |
| The tool or a tool hook raises a different exception. | `failed` | `Tool '<name>' failed: <exception type>: <exception text>` | No |

`<name>` is the tool name that the model called.
This name is `ToolPresentation.wire_name` when the tool sets it, and the tool `id` otherwise.

Raise `ToolExecutionError` when the call cannot succeed, for example when a file does not exist.
Raise `ToolRetryError` when the model can correct the arguments and call the tool again.

```python
from ovid_core.errors import ToolExecutionError, ToolRetryError


async def execute(self, context, arguments):
    if not arguments.path.is_absolute():
        raise ToolRetryError('path must be absolute')
    if not arguments.path.exists():
        raise ToolExecutionError(f'file not found: {arguments.path}')
    ...
```

`AgentRetryPolicy.tools` is the retry budget of each tool.
Each `retried` result of a tool uses one unit of the budget of that tool.
A successful call of the tool restores the budget.
When a tool has no budget left, the next `retried` result stops the run with `AgentRunError`.
A `failed` result does not use the budget.
Thus only the usage limits, such as `AgentUsageLimits.requests`, stop a model that repeats a failed call.

The adapter truncates each result text to `AgentRetryPolicy.error_max_chars` characters.
A truncated text ends with `... [truncated]`, and the limit includes this marker.

**Security:** The exception text goes to the model provider and into the stored conversation history.
Do not put credentials, tokens, or other secret values in an exception that a tool raises.
The text of an unexpected exception can contain file paths or request data.

These conditions always stop the run, because the model cannot correct them:

- Cancellation. `asyncio.CancelledError` propagates unchanged.
- Pydantic AI supplies no tool call ID, no run identity, or invalid run identity.
- The application supplies approval metadata that is not a JSON value.
- Pydantic AI calls a tool through an adapter that did not advertise the tool.

The last three conditions raise `ToolExecutionError`.

Set `AgentRetryPolicy(tool_failures='raise')` to stop the run on each tool failure.
In this mode the adapter raises `ToolValidationError`, `ToolExecutionError`, or `ToolTimeoutError`.
The message of an unexpected exception is then `Tool '<id>' failed`, and the original exception is the cause.
The messages of these errors contain the tool `id`, not the wire name.

These rules apply to Ovid tools.
Pydantic AI owns the failure behavior of MCP tools and of tools from a Pydantic AI capability passthrough.

### Ask for approval or defer a call

A tool can pause the run at run time.
Raise one of these exceptions from `ovid_core.errors` in `execute` or in a tool hook:

- `ToolApprovalRequired(metadata=None)` asks the application to approve the call.
- `ToolDeferred(metadata=None, kind='external')` gives the call to the application, which supplies the result.

`metadata` is an optional JSON object.
The request of the call carries it in `DeferredToolRequest.metadata`.
Set `kind='question'` when the call asks the user for content instead of work outside the agent.

```python
from ovid_core.errors import ToolApprovalRequired


async def execute(self, context, arguments):
    if arguments.amount > 100 and not context.approved:
        raise ToolApprovalRequired(metadata={'amount': arguments.amount})
    ...
```

When the application approves the call, the tool runs again with `context.approved=True`.
When the application answers a `ToolDeferred` call, the tool does not run again.
The model receives the answer as the tool result.

A deferral is not a failure.
Both exceptions pause the run in the `'return'` and the `'raise'` failure modes.
A deferral does not use the retry budget, and `on_tool_error` does not run for it.
The adapter also passes the Pydantic AI exceptions `ApprovalRequired` and `CallDeferred` through in both modes.

The tool hooks run in this order:

- `before_tool` runs each time the tool starts. This includes the start that raises the exception and the approved start.
- `after_tool` runs only when the tool returns a valid result.
- No hook runs for a call that waits because the tool requires approval. That call starts only after approval.

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

The adapter calls `on_tool_error` one time for each failed tool execution, before it returns or raises the failure.
This behavior is the same for each `AgentRetryPolicy.tool_failures` value.
A timeout supplies `ToolTimeoutError`.
An unexpected exception or an invalid result supplies `ToolExecutionError("Tool '<id>' failed")` with the original exception as `__cause__`.
The hook receives the tool `id` in `tool_id` and in these messages, not the wire name.
The adapter does not call `on_tool_error` for invalid arguments, because the tool did not start.

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

A capability with `defer_loading=True` owns every tool that it contributes, directly or through a toolset.
The model cannot see or call these tools before it loads the capability with the `load_capability` tool.
Pydantic AI refuses an earlier call and tells the model to load the capability.
The first refusal of a tool does not use the tool retry budget.
A tool with `defer_loading=True` that no deferred capability owns stays hidden until tool search reveals it.

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


## History processing

`HistoryProcessorCapability[Deps]` accepts an asynchronous `MessageHistoryProcessor`.
The processor receives normalized Ovid messages before each model request and returns the effective history.

The adapter runs history processing after other request capabilities, so provider-native preprocessing remains authoritative.
Processed history becomes `RunResult.history` and can be committed through `ConversationHistoryStore`.

## Todo

`TodoCapability[Deps](service=service)` has the fixed capability ID `todo` and contributes one instruction and one `todo` tool.
The application injects a `TodoService`; capability construction does not create storage or acquire resources. Results contain the
complete committed state, counts, and active task. See [Todo state and capability](todo.md) for the operation, service, and store
contracts, or [Track phased work with Todo](../guides/use-todos.md) for setup.


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
| `OpenAICompactionCapabilityConfig` | optional `stateless`, stateful token threshold, stateless message-count threshold, or stateless estimated-token threshold. Mode validation accepts one matching threshold. |
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

The adapter uses the `pydantic-ai-harness` `Skills` capability.
The adapter reads the libraries from the host filesystem.
The agent does not need a Pydantic AI workspace, and the adapter does not attach one to the run.

Library rules:

- List `directories` from highest to lowest precedence.
- When two libraries contain a skill with the same name, the first library supplies the skill.
- A relative directory resolves from the process working directory at agent build.
- The adapter does not expand `~`.
- Skill names must be unique across the `SkillsCapability` values of one agent.

Validation timing:

- `SkillLibraryConfig` validates its fields at construction.
- Agent build does not read the filesystem.
- Each run reads the libraries at run start, so a changed skill applies to the next run.

A run raises `AgentConstructionError` at run start for these conditions:

- A directory does not exist.
- A directory path is a file or a skill package.
- A directory path starts with `~`.
- An `include` or `exclude` name matches no skill.

The `__cause__` attribute of the error contains the upstream error.
A stream emits `RunFailedEvent` and then raises the same error.

A run does not fail for these conditions.
The integration skips the skill and emits a `UserWarning`:

- A lower-precedence library contains a different skill with a name from a higher-precedence library.
- A `SKILL.md` file has invalid frontmatter, an invalid name, or invalid UTF-8.

A skill load records a `CapabilityLoadReturnPart` with the `message` field.
The skill instructions follow in an `OpaqueRequestPart` with `part_kind='instruction-delta'`.

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
