# Extend an agent

Your application owns Ovid extensions. The compiler adapts the extensions when it makes the runtime.

Use these extension types:

- Use `BaseTool` for one operation.
- Use `BaseToolset` for dynamic tools or lifecycle.
- Use `BaseToolHook` for common tool behavior.
- Use `BaseCapability` to combine instructions, tools, toolsets, hooks, and model settings.
- Use built-in capabilities for provider features, Agent Skills, or MCP.

## Create a typed tool

```python
from pathlib import Path

from pydantic import Field

from ovid_core.models import BaseModel
from ovid_core.tools.base import BaseTool, ToolExecutionContext
from ovid_core.tools.models import ToolApproval, ToolResult


class ReadTextArgs(BaseModel):
    path: Path
    max_characters: int = Field(default=20_000, ge=1, le=100_000)


class ReadTextTool(BaseTool['AppDeps', ReadTextArgs, ToolResult]):
    id = 'read_text'
    description = 'Read a UTF-8 text file from the workspace.'
    args_type = ReadTextArgs
    result_type = ToolResult
    approval = ToolApproval(
        required=True,
        reason='Reading a workspace file requires caller approval.',
    )
    timeout_seconds = 10

    async def execute(
        self,
        context: ToolExecutionContext['AppDeps'],
        arguments: ReadTextArgs,
    ) -> ToolResult:
        text = await context.run.deps.files.read_text(arguments.path)

        return ToolResult(
            content=text[:arguments.max_characters],
            metadata={'path': str(arguments.path)},
        )
```

The Pydantic AI adapter:

1. The adapter publishes the argument schema to the model.
2. The adapter validates model arguments as `ReadTextArgs`.
3. The adapter sends run dependencies and identities through `ToolExecutionContext`.
4. The adapter applies approval and timeout policy.
5. The adapter calls the hooks.
6. The adapter validates the returned `ToolResult`.
7. The adapter converts the result to upstream tool-return content.
8. The adapter converts validation, execution, and timeout failures to Ovid errors.

The adapter does not convert cancellation to a tool error.

Each tool declares its default approval value.
The application controls approval and supplies approval data in the execution context.
Set `AgentDefinition.tool_approval` to replace every Ovid tool default for one agent.
For example, `ToolApproval(required=False)` removes approval pauses for all Ovid tools.
Other workspace and tool checks still apply.

## Bundle it in a capability

```python
from ovid_core.capabilities.base import BaseCapability, CapabilityContributions

files_capability = BaseCapability['AppDeps'](
    id='workspace-files',
    description='Read approved text files from the current workspace.',
    contributions=CapabilityContributions(
        instructions=(
            'Use workspace file tools only when the answer depends on local content.',
        ),
        tools=(ReadTextTool(),),
    ),
)
```

Add it to the definition:

```python
AgentDefinition[AppDeps, Answer](
    model=ModelRef(name='primary'),
    deps_type=AppDeps,
    output_type=Answer,
    capabilities=(files_capability,),
)
```

A capability is an explicit composition unit. It does not discover plugins or mutate a global registry. Diagnostics record its ID and the tools, hooks, and instructions it contributes.

## Add explicit Relay messaging
For complete setup, automatic delivery, delegation, custom descriptions, and alternative connections, see
[Use Relay between agents](use-relay.md).

Relay is structurally opt-in. Create one application-owned network, bind one connection to each agent identity, and attach the
capability only to agents that should receive the four Relay tools:

```python
from ovid_core.relay import (
    InMemoryRelay,
    RelayAddress,
    RelayCapability,
    RelayDisposition,
    RelayIdentity,
    RelayToolDescriptions,
    RelayMessage,
)

relay = InMemoryRelay(capacity=100)


async def deliver_to_application(message: RelayMessage) -> RelayDisposition:
    accepted = await application_inbox.offer(message)
    return RelayDisposition.ACKNOWLEDGE if accepted else RelayDisposition.DEFER


worker_connection = relay.connection(
    RelayIdentity(
        address=RelayAddress('worker'),
        display_name='Worker',
    ),
    delivery_handler=deliver_to_application,
)
worker_relay = RelayCapability[AppDeps](
    connection=worker_connection,
    tool_descriptions=RelayToolDescriptions(
        send='Send messages and progress updates to a known collaborator.',
        wait='Wait for a reply from a collaborator.',
    ),
)

definition = AgentDefinition[AppDeps, Answer](
    model=ModelRef(name='primary'),
    deps_type=AppDeps,
    output_type=Answer,
    capabilities=(worker_relay,),
)
```

Share that `InMemoryRelay` instance with every connection that must exchange messages.
Connections from separate instances remain isolated.
`relay_send` returns when the recipient connection accepts the message.
It does not wait for the handler.
The connection serializes automatic handler calls.

`ACKNOWLEDGE` consumes the message.
`DEFER` or an exception leaves it pending.
Calling `set_delivery_handler()` later makes pending messages eligible.
Setting it to `None` leaves future messages pending.

The capability contributes four tools:

- `relay_send`
- `relay_wait`
- `relay_pending`
- `relay_contacts`

Use the received message ID as `reply_to` when answering.
Filter `relay_wait` by `reply_to` for exact correlation.
A receipt confirms only backend acceptance.
It does not confirm that an agent read or answered a message.


Use `RelayToolDescriptions` to override model-visible tool descriptions.
Descriptions that you omit keep their core defaults.
Use `AgentDefinition.instructions` for application-wide collaboration policy.


A distributed transport can implement the same structural seam without changing factory configuration:

```python
from ovid_core.relay import RelayCapability, RelayConnection

connection: RelayConnection = application_relay.connection_for('worker')
worker_relay = RelayCapability[AppDeps](connection=connection)
```

Core owns the Relay values, protocol, capability, tools, and process-local implementation.
The application owns:

- network selection
- identity assignment
- delivery into its agent loop
- handler policy
- startup and shutdown

`AgentFactory` remains unaware of Relay.
`RelayConnection` requires no context manager or lifecycle method.

## Use a toolset for dynamic tools

A toolset can derive available tools from run dependencies and owns optional lifecycle transitions:

```python
from collections.abc import Sequence
from typing import Any

from ovid_core.runtime.context import RunContext
from ovid_core.tools.base import BaseTool, BaseToolset


class TenantToolset(BaseToolset[AppDeps]):
    id = 'tenant-tools'

    async def for_run(self, context: RunContext[AppDeps]) -> 'TenantToolset':
        await context.deps.operations.open(context.deps.tenant_id)

        return self

    async def get_tools(
        self,
        context: RunContext[AppDeps],
    ) -> Sequence[BaseTool[AppDeps, Any, Any]]:
        return await tools_for_tenant(context.deps.tenant_id)

    async def __aexit__(self, exception_type, exception, traceback) -> None:
        await close_operations()
```

The toolset interface uses fully parameterized `Any` arguments because one toolset can contain different tools.

Each tool keeps its precise argument and result models. `for_step` can return a different toolset for an agent-loop step.

Pass application toolsets through `AgentDefinition.toolsets`, or contribute them from a capability.

## Add common hooks

```python
from ovid_core.hooks.base import BaseToolHook


class ToolAuditHook(BaseToolHook[AppDeps]):
    async def before_tool(self, context, tool_id, arguments) -> None:
        await context.run.deps.audit.started(
            run_id=str(context.run.run_id),
            tool_id=tool_id,
        )

    async def after_tool(self, context, tool_id, result) -> None:
        await context.run.deps.audit.completed(
            run_id=str(context.run.run_id),
            tool_id=tool_id,
        )
```

Pass hooks directly through `AgentDefinition.hooks` when they apply to the entire agent. Contribute them from one capability when they apply only to that capability's toolsets.

`on_tool_error` receives normalized `ToolExecutionError`. Hooks should avoid logging raw arguments or results when those values may contain user or secret content.

## Add provider-native capabilities

`ProviderCapability` represents model features that should remain provider-native rather than becoming fake local tools:

```python
from ovid_core.capabilities.integrations import (
    ProviderCapability,
    ThinkingCapabilityConfig,
    WebSearchCapabilityConfig,
)

thinking = ProviderCapability[AppDeps](
    id='reasoning',
    config=ThinkingCapabilityConfig(effort='high'),
)
web_search = ProviderCapability[AppDeps](
    id='web-search',
    config=WebSearchCapabilityConfig(
        search_context_size='medium',
        allowed_domains=('docs.python.org',),
        max_uses=5,
    ),
)
```

The adapter maps supported configurations to Pydantic AI capabilities. Availability still depends on the selected provider and model. Unsupported integration construction fails at agent build time instead of silently degrading.

Available configuration covers thinking, web search, web fetch, image generation, X search, tool search, OpenAI compaction, and Anthropic compaction. See the [extension API](../api/extensions.md#provider-capabilities) for every field.

## Load Agent Skills

```python
from pathlib import Path

from ovid_core.skills import SkillLibraryConfig, SkillsCapability

skills = SkillsCapability[AppDeps](
    id='team-skills',
    config=SkillLibraryConfig(
        directories=(Path('.agents/skills'),),
        include=('incident-response', 'release-checklist'),
    ),
)
```

Skills default to deferred loading. The model initially sees capability metadata rather than every skill's complete instructions and tools. It loads a selected capability when needed, which reduces the initial prompt and tool surface.

Set `include` or `exclude`, never both. Filesystem discovery happens through the installed Agent Skills integration at runtime, not during module import.

## Connect MCP servers from configuration

Add the server to the final `OvidConfig`:

```python
from ovid_core.config import OvidConfig
from ovid_core.mcp import MCPServerConfig

issue_tracker = MCPServerConfig.model_validate(
    {
        'id': 'issue-tracker',
        'include_tools': ['search_issues', 'get_issue'],
        'namespace': 'issues',
        'defer_loading': True,
        'description': 'Read issue tracker state.',
        'transport': {
            'kind': 'http',
            'url': 'https://mcp.example.test',
            'headers': {
                'plain': {'X-Client': 'ovid-app'},
                'credentials': {
                    'Authorization': {'kind': 'callback', 'callback': 'issue-tracker-token'},
                },
            },
        },
    }
)
config = OvidConfig(models=models, mcp_servers=(issue_tracker,))
```

Pass the application credential resolver when you create the factory:

```python
factory = AgentFactory(
    config=config,
    credential_resolver=credential_resolver,
)
agent = await factory.build(definition)
```

The factory constructs each configured MCP capability and adds it to the agent. It resolves credential references before capability construction.

For stdio MCP, set `kind = "stdio"` with `command`, `args`, `cwd`, and `environment`.

Use `include_tools` to keep the exposed surface narrow. Use `namespace` to prevent collisions with local tools.

Call `create_mcp_capability` directly only for session-specific or application-generated definitions.

## Defer large capabilities

Set `defer_loading=True` when a capability is useful only for some requests and has substantial instructions or tools. Deferred loading improves initial context size but adds a capability-load step when selected. Keep always-needed, small tools eager.

Provider capabilities may have their own upstream loading behavior. Skills and MCP are the common deferred cases.

## Collision rules

Capability IDs, direct toolset IDs, and effective tool IDs must be unique. The compiler raises `ExtensionCollisionError` during construction or tool discovery rather than allowing order-dependent shadowing.

Choose stable namespaced IDs when independently developed components may meet in one agent:

```text
workspace.read_text
issues.search
release.check_status
```

## Test the contract, not the adapter internals

For an application tool, test:

- Argument limits and validation.
- Dependency use and the returned `ToolResult`.
- Approval behavior.
- Timeout and cancellation behavior.
- Hook results.
- Capability composition and collision behavior.

A direct tool test can construct `ToolExecutionContext` with an Ovid `RunContext`. This test does not need a provider.

Add one agent integration test when the contract depends on adapter execution or schema publication.

## Next steps

- [Embed and expose agents](embed-agents.md) in your application.
- Review [Components](../components.md) when choosing between local tools, Skills, and MCP.
- Use the [public extension API](../api/extensions.md) for exact signatures and fields.
