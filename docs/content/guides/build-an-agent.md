# Build an agent

This guide makes a reusable typed agent. You can use the agent in a process or with an Ovid transport.

## 1. Define application dependencies

Dependencies are request- or application-owned services available to tools and the agent runtime. They are not a global container.

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class AppDeps:
    tenant_id: str
    knowledge: 'KnowledgeRepository'
```

Use one precise type. A CLI can create it one time. A web server can create it after each authorization.

## 2. Define structured output

Use a Pydantic model when callers need more than text:

```python
from pydantic import Field

from ovid_core.models import BaseModel


class Answer(BaseModel):
    summary: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
```

Ovid's base model prevents mutation and rejects unknown fields.
The compiled Pydantic AI agent validates model output before Ovid creates `RunResult[Answer]`.

Use `str` as `output_type` when plain text is the actual contract.

## 3. Produce the final configuration

Construct `OvidConfig` from the application-produced mapping:

```python
from ovid_core.config import OvidConfig

config = OvidConfig.model_validate(
    {
        'models': {
            'fast': {
                'provider': 'openai',
                'model': 'gpt-5-mini',
                'aliases': ['default'],
                'concurrency_limit': 8,
            },
            'deep': {
                'provider': 'anthropic',
                'model': 'claude-sonnet-4-5',
            },
        },
        'routes': {'answering': {'models': ['deep', 'fast']}},
    }
)
```

The application owns source parsing, file discovery, precedence, merging, and profiles. Core receives only the validated model.

Provider environment authentication works by default. Applications can also pass a `provider_api_key` callback to `AgentFactory`.

## 4. Create the factory

```python
from ovid_core import AgentFactory

factory = AgentFactory(config=config)
```

Create one factory at application startup. It supplies model construction, routing, compilation, and model-handle caching.

Use constructor overrides only when the application has a custom model factory or compiler.

## 5. Define policy and the agent

```python
from ovid_core.agents import AgentDefinition
from ovid_core.policy import AgentRunPolicy, AgentUsageLimits
from ovid_core.routing.models import ModelRef, ModelRouteRef

agent = await factory.build(
    AgentDefinition[AppDeps, Answer](
        model=ModelRouteRef(name='answering'),
        deps_type=AppDeps,
        output_type=Answer,
        instructions=(
            'Answer from the available evidence.',
            'State uncertainty rather than inventing details.',
        ),
        policy=AgentRunPolicy(
            timeout_seconds=120,
            limits=AgentUsageLimits(
                requests=10,
                total_tokens=50_000,
            ),
        ),
    )
)
```

`AgentDefinition` is immutable. It is the complete behavior chosen at construction: model selection, types, instructions, extensions, policy, and observability.

The route tries `deep` first. An applicable final failure can move the request to `fast`.

Authentication errors and invalid requests stop the route. A different model cannot correct these errors.

## 6. Inspect the agent

```python
print(agent.diagnostics.selected_model)
print(agent.diagnostics.fallback_order)
print(agent.diagnostics.extensions)
```

Diagnostics are safe application values. They explain the provider pair, canonical route resolution, effective policy, observability, and extension provenance without exposing provider clients or credentials. Record them at startup when operators need to understand the active agent.

## 7. Run it

```python
deps = AppDeps(tenant_id='tenant-42', knowledge=repository)
result = await agent.run(
    'Summarize the deployment risk.',
    deps=deps,
)

print(result.output.summary)
print(result.output.confidence)
print(result.run_id)
print(result.usage.total_tokens)
```

The adapter runs Pydantic AI underneath. The returned value is an Ovid `RunResult[Answer]` containing normalized messages, usage, identities, and validated metadata.

### Override the model for one call

Use a configured model or route without changing the definition:

```python
result = await agent.run(
    'Use the model selected by this user.',
    deps=deps,
    model=ModelRef(name=user_selected_model),
)
```

The override applies to one run. Pass it to `stream` for the same behavior during a streamed run.

## 8. Continue a conversation

Ovid does not hide history in global agent state. The caller passes it explicitly:

```python
from ovid_core.runtime.identifiers import ConversationId

conversation_id = ConversationId.new()
first = await agent.run(
    'Remember that the release window is Friday.',
    deps=deps,
    conversation_id=conversation_id,
)
second = await agent.run(
    'When is the release window?',
    deps=deps,
    messages=first.messages,
    conversation_id=conversation_id,
)
```

`first.messages` contains messages created by the first run. If a longer history already exists, the application supplies the complete history it wants the model to see. This makes trimming, retention, and session rules explicit.

Use `ConversationStore` when the caller does not manage history. Ovid server transports can manage this sequence with a store.

## 9. Stream stable events

```python
from ovid_core.runtime.events import (
    TextDeltaEvent,
    ToolCallEvent,
    UsageUpdateEvent,
)

async with agent.stream('Explain the migration plan.', deps=deps) as stream:
    async for event in stream:
        match event:
            case TextDeltaEvent(content=content):
                print(content, end='')
            case ToolCallEvent(tool_name=name):
                print(f'\nCalling {name}')
            case UsageUpdateEvent(usage=usage):
                report_tokens(usage.total_tokens)

    result = stream.result
```

Events share run and conversation IDs and carry a monotonically ordered sequence. Consume the stream completely before reading `stream.result`.

## 10. Share a workflow budget

```python
from ovid_core.policy import AgentUsageLimits
from ovid_core.usage.tracking import UsageTracker

tracker = UsageTracker(
    limits=AgentUsageLimits(
        requests=20,
        total_tokens=100_000,
    )
)

parent_result = await agent.run(
    'Plan the work.',
    deps=deps,
    usage_tracker=tracker,
)
```

A subagent can receive `tracker.create_child()`. Its `RunResult` still reports only its local run, while `tracker.aggregate_usage` and its limits cover the whole nested workflow.

## Selecting a single model instead

Use `ModelRef` when fallback is unnecessary:

```python
from ovid_core.routing.models import ModelRef

single_model_definition = AgentDefinition[AppDeps, Answer](
    model=ModelRef(name='default'),
    deps_type=AppDeps,
    output_type=Answer,
)
```

The alias `default` resolves to canonical model `fast`. Diagnostics preserve both the requested selector and selected canonical ID.

## Next steps

- [Extend an agent](extend-an-agent.md) with typed tools, hooks, skills, or MCP.
- [Embed and expose agents](embed-agents.md) in a service, worker, CLI, or transport.
- Read [Architecture](../architecture.md) for the construction and execution flow underneath.
