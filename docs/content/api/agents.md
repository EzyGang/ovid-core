# Routing and agents

## Routing values

Import from `ovid_core.routing.models`.

### Runtime and capability contracts

```python
class ModelRuntime(Protocol):
    @property
    def model_name(self) -> str: ...
```

`ModelRuntime` is deliberately opaque. Domain code can identify it but cannot depend on Pydantic AI types.

`ModelCapabilities` contains five required booleans: `tools`, `json_schema_output`, `json_object_output`, `image_output`, and `thinking`.

`KnownModel(provider, model)` is one catalog entry returned by the default adapter's `known_models()`.

### `ModelHandle`

```python
ModelHandle(
    *,
    model_id: str,
    model_name: str,
    capabilities: ModelCapabilities,
    runtime: ModelRuntime,
)
```

Public attributes are `model_id`, `model_name`, and `capabilities`. The read-only `runtime` property exposes the opaque `ModelRuntime` for adapters. `repr(handle)` includes only the model ID and name.

### Selectors

| Selector | Fields | Meaning |
| --- | --- | --- |
| `ModelRef` | `kind='model'`, non-empty `name` | Resolve one configured model or alias. |
| `ModelRouteRef` | `kind='route'`, non-empty `name` | Resolve an ordered configured route. |
| `CandidateModelSelector` | `kind='candidates'`, non-empty `models: tuple[ModelRef, ...]` | Resolve an explicit ordered fallback list. |

`ModelSelector` is the discriminated union of all three. `AgentModelSelector` in `ovid_core.agents` intentionally narrows agent definitions to `ModelRef | ModelRouteRef`.

### `ResolvedModel`

Frozen dataclass returned by the router:

| Field | Meaning |
| --- | --- |
| `handle` | Primary or compiled fallback handle. |
| `provider`, `model` | Provider and model from the first selected configuration. |
| `requested` | Original `ModelSelector`. |
| `selected_model` | Canonical ID of the first selected model. |
| `fallback_order` | Canonical model IDs in attempt order. |
| `explanation` | Human-readable resolution trace. |

## Factory and router

Import `ModelFactory` from `ovid_core.routing.factory`.

```python
class ModelFactory(Protocol):
    async def build(self, *, model_id: str, config: ModelConfig) -> ModelHandle: ...
```

Import `ModelRouter` from `ovid_core.routing.router`.

```python
router = ModelRouter(config=config, factory=model_factory)
resolved = await router.resolve(ModelRef(name='primary'))
```

The router:

1. Resolves canonical model IDs and aliases.
2. Rejects aliases assigned to multiple models at construction.
3. Resolves route entries and explicit candidates in order.
4. Builds and caches each handle once per router.
5. Compiles multiple handles into a Pydantic AI fallback model.

Unknown model or route names raise `ModelResolutionError`. Provider SDK retries finish inside one candidate.

The fallback model moves to the next candidate only after an applicable final failure.

## Agent definition

Import from `ovid_core.agents`.

```python
@dataclass(frozen=True, slots=True)
class AgentDefinition[Deps, Output]:
    model: AgentModelSelector
    deps_type: type[Deps]
    output_type: type[Output]
    instructions: tuple[str, ...] = ()
    capabilities: tuple[BaseCapability[Deps], ...] = ()
    toolsets: tuple[BaseToolset[Deps], ...] = ()
    hooks: tuple[BaseToolHook[Deps], ...] = ()
    policy: AgentRunPolicy = AgentRunPolicy()
    observability: ObservabilityConfig = ObservabilityConfig()
    services: AgentServices = AgentServices()
```

The definition contains immutable construction input. `services` retains named stateful values by reference in an immutable registry.
Capabilities can add instructions, tools, toolsets, hooks, and model settings. The compiler also adds direct toolsets and hooks.

## Agent services

Import service contracts from `ovid_core.services`.

`AgentServiceKey(id, api_version, value_type=None)` identifies a versioned, globally namespaced contract. Key equality uses only
`id` and `api_version`. `AgentServiceRef(key, name='default')` selects one named instance. An `AgentServiceBinding` stores the
reference, value, provider ID, advertised features, and optional safe identity.

Construct `AgentServices` from a sequence of bindings. Duplicate references and incompatible runtime values fail immediately.
`resolve(ref)` returns the retained value. `binding(ref)` returns its metadata. Missing services raise
`AgentServiceMissingError`.

Capabilities expose `requirements: tuple[AgentServiceRequirement, ...]` before construction. `AgentFactory.build` validates
requirements, calls each capability's `bind(services)` once, and compiles the returned contributions. Run-level model selection
and fallback reuse that bound definition.

## Compiler and runtime protocols

```python
class AgentCompiler(Protocol):
    def compile[Deps, Output](
        self,
        definition: AgentDefinition[Deps, Output],
        resolved: ResolvedModel,
    ) -> AgentRuntime[Deps, Output]: ...
```

`AgentRuntime[Deps, Output]` supplies:

```python
async def run(
    prompt: str,
    *,
    deps: Deps,
    messages: tuple[AgentMessage, ...],
    run_id: RunId | None,
    conversation_id: ConversationId | None,
    usage_tracker: UsageTracker | None,
) -> RunResult[Output]

def stream(
    prompt: str,
    *,
    deps: Deps,
    messages: tuple[AgentMessage, ...],
    run_id: RunId | None,
    conversation_id: ConversationId | None,
    usage_tracker: UsageTracker | None,
) -> AbstractAsyncContextManager[AgentStream[Output]]
```

`AgentStream[Output]` is an `AsyncIterator[AgentEvent]` with a `result: RunResult[Output]` property. The result is available after complete consumption inside the context manager.

## Constructing and running agents

```python
factory = AgentFactory(
    config=config,
    model_factory=None,
    compiler=None,
    provider_api_key=None,
    credential_resolver=None,
)
agent = await factory.build(definition, model=None)
```

The factory creates `DefaultModelFactory`, `ModelRouter`, and `DefaultAgentCompiler` when their arguments are absent.

It also converts `config.mcp_servers` to capabilities. `credential_resolver` resolves credential references inside MCP environment variables and headers.
Service validation and capability binding complete before model resolution and compilation.

The optional `model` argument overrides the definition model for the constructed agent.

`OvidAgent.run` and `OvidAgent.stream` accept the same optional override:

```python
result = await agent.run(
    prompt,
    deps=deps,
    model=ModelRef(name='user-selection'),
)
```

The override applies only to that run or stream. The factory router builds each configured model once and caches its handle.

The public `diagnostics` value describes the model selected when the factory built the agent. A run override does not mutate this value.

## Construction diagnostics

`AgentConstructionDiagnostics` fields:

- `provider`, `model`: selected provider pair.
- `requested`: original agent selector.
- `selected_model`: canonical primary model ID.
- `fallback_order`: canonical attempt order.
- `policy`, `observability`: effective definition values.
- `extensions`: ordered `AgentExtensionProvenance` entries.
- `services`: ordered `AgentServiceDiagnostic` entries with the service key, name, provider, sorted features, safe identity, and consuming capability IDs.

Each extension entry has `kind` (`capability`, `tool`, `toolset`, `hook`, or `instructions`), a non-empty `id`, and a non-empty
`source`. Service diagnostics may contain opaque session identities. They must not contain credentials, absolute workspace roots,
or provider runtime objects.
