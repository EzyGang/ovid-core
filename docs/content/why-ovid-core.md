# Why Ovid Core

Pydantic AI operates models, agent loops, structured output, tools, streams, and instrumentation.

Ovid Core does not replace these functions. Ovid Core supplies stable interfaces for the application around the Pydantic AI runtime.

This separation becomes important when more than one application part uses an agent.

A worker, service, database, and user interface must use the same definitions. They need common message, event, result, tool, and usage formats.

Ovid Core supplies these formats. It also keeps provider runtime objects inside the adapter.

## When to use Pydantic AI directly

Use Pydantic AI directly for a small agent in one process.

Direct use is a good selection when:

- One process owns the agent.
- Upstream types do not cross an API.
- Upstream types do not enter persistent storage.
- The code uses one fixed model.
- The workflow does not have subagent usage limits.
- The application does not need an Ovid transport.

This direct configuration has few parts:

```python
from pydantic_ai import Agent

agent = Agent('openai:gpt-5', output_type=str)
result = await agent.run('Summarize this change')
```

Do not add Ovid Core if your application does not need its interfaces.

## When to use Ovid Core

Use Ovid Core when the agent is a shared application component.

Ovid Core helps you do these tasks:

- Define an agent one time for different callers.
- Select models from application configuration.
- Use ordered fallback routes.
- Store conversations without upstream runtime objects.
- Give messages, events, results, and usage to other application parts.
- Combine tools, toolsets, hooks, Agent Skills, MCP, and provider capabilities.
- Apply one usage budget to parent agents and subagents.
- Use HTTP, SSE, stdio, or AG-UI without a new agent definition.
- Keep Pydantic AI compatibility code in one adapter package.

Ovid Core uses an explicit configuration and typed agent definition:

```python
factory = AgentFactory(config=config)
agent = await factory.build(definition)
result = await agent.run('Summarize this change', deps=deps)
```

The factory contains the default model construction, routing, and compilation components. Applications can replace these parts when they need custom integration behavior.

The returned Ovid agent supplies stable run and stream methods.

## Comparison

| Function | Direct Pydantic AI | Ovid Core |
| --- | --- | --- |
| Model selection | Use a model string or upstream model object | Use a configured model, alias, or route |
| Fallback information | Use upstream fallback behavior | Use an ordered route and construction diagnostics |
| Messages and results | Use Pydantic AI runtime values | Use frozen Ovid data models |
| Stream data | Use upstream event types | Use the stable `AgentEvent` union |
| Tools | Use Pydantic AI tools and toolsets | Use typed Ovid tools, approval data, timeouts, and hooks |
| Usage limits | Apply limits to an upstream run | Apply limits to a run or a nested workflow |
| Compatibility code | Put upstream imports in application code | Put upstream imports in adapter code |
| Conversation storage | Select an upstream serialization method | Use normalized messages and a versioned codec |
| Transports | Make an application transport | Use optional HTTP, SSE, stdio, or AG-UI |
| Provider credentials | Use provider rules | Use environment, native provider authentication, or an application API-key callback |

## Required parts

A basic Ovid agent needs:

1. A final `OvidConfig`.
2. An `AgentDefinition`.

Create one `AgentFactory` from the configuration. The factory supplies the default model factory, router, and compiler.

```python
factory = AgentFactory(config=config)
agent = await factory.build(definition)
result = await agent.run('Summarize this change', deps=deps)
```

The application can override the configured model for one run. Storage, servers, custom credentials, Codex, Agent Skills, and hooks remain optional.

MCP is also optional. When `OvidConfig.mcp_servers` contains definitions, the factory adds those servers automatically.

See [Getting started](getting-started.md) for a complete basic configuration.

## Ovid Core responsibilities

Ovid Core owns data that crosses application interfaces:

- Final configuration and schema migration.
- Model selectors, handles, routes, and diagnostics.
- Agent definitions and run interfaces.
- Normalized messages, events, identities, and results.
- Request, run, and nested usage data.
- Retry, timeout, concurrency, fallback, and limit policies.
- Tool, toolset, capability, and hook interfaces.
- Credential references and resolver interfaces.
- Conversation storage and server interfaces.
- Pydantic AI and Starlette adapters.

These types contain data and protocols. Application code can validate, serialize, and test them without a provider client.

## Application responsibilities

Ovid Core does not control these application functions:

- Configuration file locations.
- Source precedence and profiles.
- User authentication and authorization.
- Durable storage and retention.
- Request dependency construction.
- Telemetry export.
- Process deployment.
- Permitted provider settings.

The application controls these functions because each application has different requirements.

Ovid Core uses explicit parameters and protocols. It does not use a global service container.

## Compatibility cost

The Ovid interface does not expose all Pydantic AI runtime features.

Ovid Core can add an upstream feature in these ways:

1. Add a stable field to an Ovid type.
2. Add a provider capability.
3. Use a public adapter interface at the composition point.

This design adds a translation step. It also isolates upstream changes from the rest of the application.
