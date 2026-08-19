# Getting started

This guide builds one agent from a final `OvidConfig`. The example has no storage, tools, server, or observability.

## Before you start

Install the package:

```bash
uv add ovid-core
```

Use Python 3.14 or newer.

## 1. Configure the model

Construct `OvidConfig` from the application-produced data:

```python
from ovid_core.config import OvidConfig

config = OvidConfig.model_validate(
    {'models': {'primary': {'provider': 'openai', 'model': 'gpt-5'}}}
)
```

`primary` is an application model ID. Agent code uses this ID instead of a provider model string.

The application owns parsing, file discovery, source precedence, environment mapping, and profiles. Core accepts one final model.

Applications can parse TOML, JSON, YAML, remote content, or another source. Validate the resulting Ovid mapping after applying application policy.

## 2. Create the factory

```python
from ovid_core import AgentFactory

factory = AgentFactory(config=config)
```

`AgentFactory` supplies the default model factory, router, and compiler. It caches constructed model handles for reuse.

Advanced applications can replace the model factory or compiler through constructor arguments.

## 3. Define the agent

```python
from ovid_core import AgentDefinition
from ovid_core.routing import ModelRef

definition = AgentDefinition[None, str](
    model=ModelRef(name='primary'),
    deps_type=type(None),
    output_type=str,
    instructions=(
        'Answer concisely.',
        'Do not invent facts.',
    ),
)
agent = await factory.build(definition)
```

The immutable definition sets:

- The default configured model.
- The dependency type for each run.
- The output type for each caller.
- The instructions, extensions, policy, and observability settings.

This example has no dependencies and returns text. Larger applications can use a dependency dataclass and a Pydantic output model.

`factory.build` resolves `primary` and returns `OvidAgent[None, str]`.

## 4. Run it

```python
result = await agent.run(
    'What is 2 + 2?',
    deps=None,
)

print(result.output)
print(result.usage.total_tokens)
print(result.run_id)
print(result.conversation_id)
```

Pydantic AI performs the provider request and agent loop. Ovid Core normalizes the outcome into `RunResult[str]`:

- `output` contains the validated output.
- `messages` contain stable Ovid conversation values.
- `usage` contains all reported model requests in this run.
- `run_id` and `conversation_id` identify the run and conversation.
- `metadata` contains JSON-compatible values. Ovid Core rejects secret-related metadata keys.

Application code does not need to handle upstream `AgentRunResult`, provider message, or usage types.

## Complete program

```python
import asyncio

from ovid_core import AgentDefinition, AgentFactory
from ovid_core.config import OvidConfig
from ovid_core.routing import ModelRef


async def main() -> None:
    config = OvidConfig.model_validate(
        {'models': {'primary': {'provider': 'openai', 'model': 'gpt-5'}}}
    )
    factory = AgentFactory(config=config)
    agent = await factory.build(
        AgentDefinition[None, str](
            model=ModelRef(name='primary'),
            deps_type=type(None),
            output_type=str,
            instructions=('Answer concisely and do not invent facts.',),
        )
    )
    result = await agent.run('What is 2 + 2?', deps=None)

    print(result.output)
    print(result.usage)


asyncio.run(main())
```

## Supply provider API keys

Provider environment variables continue to work. For OpenAI:

```bash
export OPENAI_API_KEY='...'
```

An application can provide a saved key without modifying environment variables:

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

The callback receives the configured model ID and provider name. It can read from a database, OS keyring, encrypted file, or application vault.

Return `None` when the provider must use its normal environment or cloud authentication.

Ovid Core does not serialize the returned `SecretStr` or add it to `OvidConfig`.

## Continue a conversation

An `OvidAgent` does not hide mutable history. Reuse a conversation identity and pass the messages the next run should see:

```python
from ovid_core.runtime.identifiers import ConversationId

conversation_id = ConversationId.new()
first = await agent.run(
    'Remember the number 17.',
    deps=None,
    conversation_id=conversation_id,
)
second = await agent.run(
    'What number did I give you?',
    deps=None,
    messages=first.messages,
    conversation_id=conversation_id,
)
```

This is enough for an in-memory caller. Add a `ConversationStore` only when history must cross requests or process restarts.

## Override the model for one run

Add every selectable model to the final configuration before factory construction:

```python
config = OvidConfig.model_validate(
    {
        'models': {
            'primary': {'provider': 'openai', 'model': 'gpt-5'},
            'fast': {'provider': 'openai', 'model': 'gpt-5-mini'},
        }
    }
)
```

Pass the user selection to `run` or `stream`:

```python
selected_model = ModelRef(name=user_selected_model)
reply = await agent.run(
    'Answer with the selected model.',
    deps=None,
    messages=second.messages,
    conversation_id=conversation_id,
    model=selected_model,
)
```

The override applies only to this run. The agent definition and default model do not change.

The same conversation can use a different configured model on its next run. The router caches each constructed model handle.

## Stream a run

```python
from ovid_core.runtime.events import TextDeltaEvent

async with agent.stream('Explain the result.', deps=None) as stream:
    async for event in stream:
        if isinstance(event, TextDeltaEvent):
            print(event.content, end='')

    result = stream.result
```

The adapter converts upstream stream parts to the stable `AgentEvent` union. Consume the stream before reading its final result. Cancellation propagates rather than becoming a normal failure event.

## Add a fallback route

Define the ordered route in the final configuration:

```python
config = OvidConfig.model_validate(
    {
        'models': {
            'primary': {'provider': 'openai', 'model': 'gpt-5'},
            'backup': {'provider': 'anthropic', 'model': 'claude-sonnet-4-5'},
        },
        'routes': {'resilient': {'models': ['primary', 'backup']}},
    }
)
```

Select the route in the definition or as a run override:

```python
from ovid_core.routing import ModelRouteRef

result = await agent.run(
    'Use the resilient route.',
    deps=None,
    model=ModelRouteRef(name='resilient'),
)
```

Provider retries complete within one candidate. An applicable final failure can advance to the next configured model.

Authentication and invalid requests stop the route.

## Configure MCP servers

Include MCP definitions in the final configuration:

```python
config = OvidConfig.model_validate(
    {
        'models': {'primary': {'provider': 'openai', 'model': 'gpt-5'}},
        'mcp_servers': [
            {
                'id': 'project-tools',
                'include_tools': ['search', 'read'],
                'transport': {'kind': 'http', 'url': 'https://mcp.example.com'},
            }
        ],
    }
)
```

`AgentFactory(config=config)` constructs these capabilities and adds them to each agent.

MCP environment variables and headers can contain credential references. Pass a `CredentialResolver` to `AgentFactory` when those references need resolution.

## What to add next

Do not add every component by default:

- Need structured output, typed dependencies, routes, policy, or shared budgets? Continue with [Build an agent](guides/build-an-agent.md).
- Need local tools, hooks, Skills, MCP, web search, or reasoning controls? Read [Extend an agent](guides/extend-an-agent.md).
- Need durable history, a worker, HTTP/SSE, stdio, or AG-UI? Read [Embed and expose agents](guides/embed-agents.md).
- Unsure which parts are optional? Use the [Components](components.md) matrix.
- Want the implementation flow underneath? Read [Architecture](architecture.md).
