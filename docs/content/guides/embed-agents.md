# Embed and expose agents

An `OvidAgent[Deps, Output]` is an application component, not a process model. Build it once, then choose how callers reach it. Direct calls and every built-in transport preserve the same model routing, extensions, policy, normalized history, events, and results.

## Pattern 1: in-process service

Put a constructed agent in an application class when business code must not know the construction details:

```python
class ReleaseAdvisor:
    def __init__(self, agent: OvidAgent[AppDeps, Answer]) -> None:
        self._agent = agent

    async def advise(
        self,
        request: ReleaseRequest,
        *,
        deps: AppDeps,
        messages: tuple[AgentMessage, ...] = (),
    ) -> RunResult[Answer]:
        prompt = request.to_prompt()

        return await self._agent.run(
            prompt,
            deps=deps,
            messages=messages,
            conversation_id=request.conversation_id,
        )
```

This is the smallest connection method. The wrapper owns domain prompts. Ovid owns agent runtime values.

This method needs no transport or storage dependency.

Build the router, factory, and agent at application startup. Do not rebuild the agent for every request unless its immutable definition actually changes.

## Pattern 2: CLI or worker

A command or queue worker can load history, invoke the agent, and persist new normalized messages explicitly:

```python
messages = await store.load(job.conversation_id)
result = await agent.run(
    job.prompt,
    deps=deps,
    messages=messages,
    conversation_id=job.conversation_id,
)
await store.append(result.conversation_id, result.messages)
```

Use `MessageCodec` in a durable `ConversationStore`. Keep job acknowledgment and transaction policy in the application.

The application decides if it repeats a completed run after an acknowledgment failure.

Use the stdio server for child-process discovery and commands. Do not create a different line protocol.

## Pattern 3: native HTTP and SSE

Install the server profile:

```bash
uv add 'ovid-core[server]'
```

Register the agent with explicit authorization and dependency construction:

```python
from ovid_core.server.app import create_agent_app
from ovid_core.server.contracts import (
    AgentRegistration,
    AuthorizationResult,
    RequestContext,
)
from ovid_core.server.models import ServerConfig


async def authorize(
    context: RequestContext,
    resource_id: str,
) -> AuthorizationResult:
    principal = await auth_service.authenticate(context.header('authorization'))

    return AuthorizationResult(
        allowed=principal is not None and await principal.can_run(resource_id),
        principal=principal.id if principal is not None else None,
    )


async def dependencies(
    context: RequestContext,
    authorization: AuthorizationResult,
) -> AppDeps:
    if authorization.principal is None:
        raise PermissionError

    return await dependency_factory.for_principal(authorization.principal)


app = create_agent_app(
    agents=(
        AgentRegistration(
            id='release-advisor',
            description='Assess release risk from repository evidence.',
            agent=agent,
            dependencies=dependencies,
        ),
    ),
    authorize=authorize,
    config=ServerConfig(
        host='127.0.0.1',
        port=8000,
        max_concurrency=32,
        allowed_origins=('https://app.example.test',),
    ),
    store=store,
)
```

Run it from an application entry point:

```python
from ovid_core.server.app import serve

serve(app, config=ServerConfig(host='127.0.0.1', port=8000))
```

The app exposes:

- `GET /health` gives process health.
- `GET /ready` gives optional dependency readiness.
- `POST /agents/{agent_id}/runs` gives one JSON result.
- `POST /agents/{agent_id}/events` gives server-sent events and a final result.

### What the server does for you

For every run, the shared runtime uses this sequence:

1. The runtime validates the content type and body size.
2. The runtime applies global and agent concurrency limits.
3. The runtime authorizes the agent ID.
4. The runtime creates or accepts the conversation ID.
5. The runtime loads normalized history from the optional store.
6. The runtime constructs typed dependencies after authorization.
7. The runtime selects the smaller server or agent timeout.
8. The runtime runs or streams the registered agent.
9. The runtime appends new messages after completion.
10. The runtime converts errors to source-safe transport responses.

The server does not supply user authentication, a database, TLS termination, principal rate limits, or deployment configuration.

The application and its infrastructure supply these functions.

## Pattern 4: stdio host

Use stdio when a parent process manages the agent host and wants a versioned request/response protocol:

```python
from ovid_core.server.stdio import create_stdio_server

server = create_stdio_server(
    agents=registrations,
    authorize=authorize,
    commands=commands,
    config=server_config,
    store=store,
    startup=startup,
    shutdown=shutdown,
)
await server.run()
```

The client first sends an `initialize` request to discover agents and commands. A `run` request yields zero or more event responses followed by one run result. Registered commands execute application handlers outside the agent loop but through the same authorization boundary.

Use this for desktop applications, local harness workers, or language-neutral process supervision. Use direct Python calls when no process boundary exists.

## Pattern 5: AG-UI frontend

Install the AG-UI profile:

```bash
uv add 'ovid-core[server-ag-ui]'
```

```python
from ovid_core.server.ag_ui import create_ag_ui_app

app = create_ag_ui_app(
    agents=registrations,
    authorize=authorize,
    config=server_config,
    store=store,
)
```

The app exposes `POST /agents/{agent_id}` with AG-UI streaming.

The server controls system prompts, tools, context, resume state, forwarded properties, run IDs, and stored history.

The server rejects client control of these values. A client thread ID maps to an Ovid conversation ID.

AG-UI requires agents from `DefaultAgentCompiler`. Use native HTTP/SSE for a runtime-independent Ovid transport.

## Persistence choices

`ConversationStore` intentionally has only `load` and `append`. That makes SQLite, PostgreSQL, object storage, event logs, and encrypted stores application choices.

A durable store should decide:

- Transaction and duplicate-write behavior.
- Ordering guarantees.
- Retention and truncation.
- Tenant separation.
- Storage encryption.
- Authorization outside the server load and append operations.
- Codec-version migration.

Store the output from `MessageCodec.encode(message)`. Do not make a second normalized schema.

Do not store the Pydantic AI message object.

## Request-scoped dependencies

The registered `DependenciesFactory` is the bridge from transport identity to agent dependencies. It runs after authorization and may create tenant-scoped repositories, API clients, feature flags, or audit sinks.

Keep secrets inside those services rather than placing them in prompts, `RunResult.metadata`, or `AuthorizationResult.principal`. The request context's repr deliberately excludes headers.

## Parent and subagent workflows

For an orchestrator that calls several Ovid agents, create one root tracker:

```python
tracker = UsageTracker(limits=workflow_limits)

plan = await planner.run(prompt, deps=deps, usage_tracker=tracker.create_child())
review = await reviewer.run(
    render_review_prompt(plan.output),
    deps=deps,
    usage_tracker=tracker.create_child(),
)
```

Each result reports its own messages and usage. The root tracker applies workflow limits and reports combined usage.

Pass normalized output or application DTOs between agents. Do not pass upstream runtime objects.

## Observability

Enable `ObservabilityConfig(enabled=True)` after the application configures Pydantic AI or OpenTelemetry export.

Core maps the instrumentation settings. Core does not install a global exporter.

By default, observability excludes content.

Set `include_content=True` only when the configured telemetry destination is safe for model request and response content.

## Composition-root example

A maintainable application keeps construction in one place:

```python
@dataclass(frozen=True)
class Runtime:
    advisor: OvidAgent[AppDeps, Answer]
    store: ConversationStore


async def build_runtime(settings: ApplicationSettings) -> Runtime:
    config = settings.to_ovid_config()
    agent_factory = AgentFactory(
        config=config,
        provider_api_key=settings.provider_api_key,
        credential_resolver=settings.credential_resolver,
    )
    advisor = await agent_factory.build(release_advisor_definition())

    return Runtime(advisor=advisor, store=build_store(settings.database))
```

You do not need a global agent registry.

Tests can supply a different model factory, compiler, dependency value, store, or constructed agent.

## Choosing the boundary

- Start in process.
- Add persistence when history outlives the call.
- Add stdio or HTTP only when another process needs access.
- Add AG-UI only for an AG-UI client.
- Keep application authorization, dependency construction, and deployment policy outside the agent definition.

The agent stays the same as the application grows around it.
