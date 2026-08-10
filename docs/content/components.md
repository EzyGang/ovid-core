# Components

You can add Ovid Core components gradually.

A basic agent needs a final configuration, an agent definition, and an `AgentFactory`.

Optional components solve additional application problems. You can add them without a change to the run result format.

## Required components

A basic agent uses these components:

| Component | Function |
| --- | --- |
| `OvidConfig` | Defines models, routes, MCP servers, run defaults, and credential references |
| `AgentDefinition` | Defines types, instructions, extensions, and agent policy |
| `AgentFactory` | Builds agents from the final configuration |
| `OvidAgent` | Supplies `run` and `stream` |

Create one factory during application startup:

```python
factory = AgentFactory(config=config)
agent = await factory.build(definition)
```

The factory supplies `DefaultModelFactory`, `ModelRouter`, and `DefaultAgentCompiler`. You only provide these lower-level components for custom integration work.

## Optional components

| Component | Use it for | Do not use it when |
| --- | --- | --- |
| Typed tools | Model access to application operations | The agent only makes output |
| Toolsets | Dynamic tools or tool lifecycle | Static tools are sufficient |
| Hooks | Approval, audit, or common tool policy | No tool interception is necessary |
| Provider capabilities | Reasoning, search, image generation, or compaction | Plain model requests are sufficient |
| Agent Skills | Instructions and tools from skill directories | Instructions stay in code |
| MCP | Tools or instructions from an MCP server | All extensions are local |
| Credential references | MCP environment variables and headers | No configured extension needs a referenced secret |
| `UsageTracker` | One budget for multiple related runs | Each result needs only local usage |
| Conversation storage | History after a request or process ends | The caller keeps history in memory |
| Codex subscription | Explicit ChatGPT subscription authentication | The application uses normal provider authentication |
| HTTP and SSE | Agent access across an HTTP interface | The application calls the agent directly |
| Stdio server | Agent access across a child-process interface | Direct calls or HTTP are better |
| AG-UI | An AG-UI user interface | The application does not use AG-UI |
| Observability | Pydantic AI OpenTelemetry spans | The application does not configure telemetry |

## Installation options

Install the base package:

```bash
uv add ovid-core
```

The base package includes these functions:

- Agent construction.
- Provider model support through Pydantic AI.
- Tools and capabilities.
- Agent Skills and MCP.
- Storage interfaces.
- Codex authentication.
- Pydantic AI adapters.

Install the optional HTTP and SSE server:

```bash
uv add 'ovid-core[server]'
```

This option adds Starlette and Uvicorn.

Install the optional AG-UI server:

```bash
uv add 'ovid-core[server-ag-ui]'
```

This option also installs the HTTP server dependencies.

Some providers need an additional provider SDK. Add the applicable Pydantic AI provider option to your application.

## Model factory options

### Default provider authentication

`AgentFactory` uses `DefaultModelFactory`. By default, provider SDKs read environment variables, cloud credential chains, ADC, or provider profiles.

An application can provide API keys without environment changes:

```python
from pydantic import SecretStr


async def provider_api_key(model_id: str, provider: str) -> SecretStr | None:
    value = await application_secrets.load(model_id, provider)

    return None if value is None else SecretStr(value)


factory = AgentFactory(
    config=config,
    provider_api_key=provider_api_key,
)
```

The callback receives the configured model ID and provider name. Return `None` to use the provider default authentication.

The key remains outside `OvidConfig`. The default factory passes it directly to the provider constructor.

### Codex subscription authentication

Use `CodexSubscriptionModelFactory` only for explicit ChatGPT Codex subscription access:

```python
model_factory = CodexSubscriptionModelFactory(
    token_manager=token_manager,
    fallback=DefaultModelFactory(),
)
factory = AgentFactory(config=config, model_factory=model_factory)
```

The fallback factory constructs models for other providers.

The Codex integration uses an undocumented backend. It requires stateless Responses API operation.

The integration does not change a subscription request to API-key billing.

### Custom model factories

Implement `ModelFactory` when your application has different model construction requirements.

Pass it through `AgentFactory(config=config, model_factory=...)`.

The selected compiler must support the runtime in the returned `ModelHandle`.

## Extension options

Select the smallest extension type that satisfies the requirement:

1. Use `BaseTool` for one static operation.
2. Use one capability for multiple static operations.
3. Use `BaseToolset` for dynamic tools or lifecycle.
4. Use `BaseToolHook` for common tool behavior.
5. Use `ProviderCapability` for provider model features.
6. Use `SkillsCapability` for Agent Skills directories.
7. Define MCP servers in `OvidConfig.mcp_servers`.

`AgentFactory` constructs configured MCP capabilities. Direct capabilities remain useful for session-specific or application-generated extensions.

## Conversation options

### Caller-owned history

Pass `RunResult.messages` to the next run.

Use this option for a command, notebook, or process that keeps history in memory.

### Application storage

Implement `ConversationStore` when history must continue after the caller ends.

Use `MessageCodec` for stored messages. Your application selects the database, transaction rules, and retention policy.

### Server storage

Give a store to an Ovid server factory.

The server loads history before a run. It appends the new messages after a successful run.

## Transport options

| Application type | Recommended interface |
| --- | --- |
| Python service or worker | Direct `OvidAgent` calls |
| Desktop child process | Stdio server |
| General service integration | HTTP and SSE server |
| AG-UI user interface | AG-UI server |
| Existing web framework | Direct calls or a low-level adapter factory |

All transports use the same registered agent. A transport does not change the model, extensions, policy, or output type.

## Policy options

The default policy permits 50 requests. It also uses a 30-second tool timeout.

The default policy has no run timeout, concurrency limit, tool retry, or output retry.

Change only the values that your application requires.

Use `UsageTracker` when one budget applies to multiple runs. Use a child tracker for each subagent.

## Recommended adoption sequence

Use this sequence:

1. Configure one model.
2. Make one typed agent definition.
3. Add typed dependencies and structured output.
4. Add only the tools that the agent needs.
5. Add a fallback route when you have a fallback requirement.
6. Add storage when conversations continue across calls.
7. Add a transport when another process needs access.
8. Add common usage tracking when runs share a budget.
9. Enable observability after you configure an exporter.

This sequence keeps the first configuration small. It also gives you a clear path to a larger application.
