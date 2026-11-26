# Routing and agents

## Routing values

Import from `ovid_core.routing.models`.

### Runtime and capability contracts

```python
class ModelRuntime(Protocol):
    @property
    def model_name(self) -> str: ...
```

`ModelRuntime` is deliberately opaque.
Domain code can identify it but cannot depend on Pydantic AI types.

`ModelCapabilities` contains five required booleans:

- `tools`
- `json_schema_output`
- `json_object_output`
- `image_output`
- `thinking`

`KnownModel(provider, model)` is one catalog entry returned by the default adapter's `known_models()`.

### `ModelHandle`

```text
ModelHandle(
    *,
    model_id: str,
    model_name: str,
    capabilities: ModelCapabilities,
    runtime: ModelRuntime,
    context_window: int | None = None,
    resolve: Callable[[], Awaitable[ModelRuntime]] | None = None,
)
```

Public attributes are:

- `model_id`
- `model_name`
- `capabilities`
- `context_window`
- `resolve`

`context_window` contains discovered model metadata when the provider or `genai-prices` supplies it.
The read-only `runtime` property exposes the initial model snapshot for adapters.
`repr(handle)` includes only the model ID and name.

The optional `resolve` callback returns the current runtime for the same configured model.
`DefaultAgentCompiler` connects this callback to Pydantic AI `SelectModel`.
Pydantic AI selects the model before each new logical request and manages its lifetime.
Existing requests keep their selected model until they finish.
Static handles use `resolve=None`.

### Selectors

| Selector | Fields | Meaning |
| --- | --- | --- |
| `ModelRef` | `kind='model'`, non-empty `name` | Resolve one configured model or alias. |
| `ModelRouteRef` | `kind='route'`, non-empty `name` | Resolve an ordered configured route. |
| `CandidateModelSelector` | `kind='candidates'`, non-empty `models: tuple[ModelRef, ...]` | Resolve an explicit ordered fallback list. |

`ModelSelector` is the discriminated union of all three selectors.
`AgentModelSelector` in `ovid_core.agents` restricts agent definitions to `ModelRef | ModelRouteRef`.

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
4. Builds each handle once per router.
5. Caches each handle.
6. Compiles multiple handles into a Pydantic AI fallback model.

Unknown model or route names raise `ModelResolutionError`.
Provider SDK retries finish inside one candidate.

The fallback model moves to the next candidate only after an applicable final failure.

## Agent definition

Import from `ovid_core.agents`.

```python
@dataclass(frozen=True, slots=True)
class AgentDefinition[Deps, Output]:
    model: AgentModelSelector
    deps_type: type[Deps]
    output_type: type[Output]
    instructions: tuple[InstructionSource[Deps], ...] = ()
    capabilities: tuple[BaseCapability[Deps], ...] = ()
    toolsets: tuple[BaseToolset[Deps], ...] = ()
    tool_approval: ToolApproval | None = None
    hooks: tuple[BaseToolHook[Deps], ...] = ()
    policy: AgentRunPolicy = AgentRunPolicy()
    observability: ObservabilityConfig = ObservabilityConfig()
    services: AgentServices = AgentServices()
```

The definition contains all immutable construction input.

`instructions` accepts static strings and dynamic `InstructionSource` callables, as [Capability contracts](extensions.md#capability-contracts) describes.
The adapter evaluates each callable source before each model request.

Capabilities can add these values:

- Instructions
- Tools
- Toolsets
- Hooks
- Model settings

The compiler also adds direct toolsets and hooks.

### Ovid tool approval

The `tool_approval` value overrides the approval value on every Ovid `BaseTool` in the agent.
The default value, `None`, keeps the value from each tool.
A tool that requires approval pauses the run before the tool runs.
[Deferred runs](#deferred-runs) shows how to answer the pause.

Use this value to let the application run all Ovid tools without an approval pause:

```python
AgentDefinition[AppDeps, Answer](
    model=ModelRef(name='primary'),
    deps_type=AppDeps,
    output_type=Answer,
    capabilities=(WorkspaceFilesCapability(),),
    tool_approval=ToolApproval(required=False),
)
```

The proxy advertises these tools as normal Pydantic AI function tools.
These controls still apply:

- Workspace policy
- Path validation
- Observations
- Timeouts
- Cancellation

Set `required=True` to require approval for all Ovid tools.
This override does not change tools from a Pydantic AI capability passthrough.
Those tools keep the approval behavior of their source capability.

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

```text
async def run(
    prompt: str | Sequence[UserContent] | None,
    *,
    resume: DeferredToolResume | None,
    deps: Deps,
    messages: tuple[AgentMessage, ...],
    run_id: RunId | None,
    conversation_id: ConversationId | None,
    usage_tracker: UsageTracker | None,
) -> RunResult[Output] | DeferredRunResult

def stream(
    prompt: str | Sequence[UserContent] | None,
    *,
    resume: DeferredToolResume | None,
    deps: Deps,
    messages: tuple[AgentMessage, ...],
    run_id: RunId | None,
    conversation_id: ConversationId | None,
    usage_tracker: UsageTracker | None,
    journal: RunJournal | None,
) -> AbstractAsyncContextManager[AgentStream[Output]]
```

`AgentStream[Output]` is an `AsyncIterator[AgentEvent]` with these members:

- A `result: RunResult[Output] | DeferredRunResult` property.
  The result becomes available after complete consumption inside the context manager.
- A `partial() -> PartialRunResult | None` method.
  It returns the transcript of a run that ended with an error or a cancellation.

`partial()` returns `None` while the run continues, after a completed or deferred run, and after a failure before the run added a message.
The stream sets the partial before it raises the normalized error out of the context manager.
Read it after the `async with` block:

```python
from ovid_core import AgentRunError

try:
    async with agent.stream('Fix the tests.', deps=deps) as events:
        async for event in events:
            render(event)
except AgentRunError:
    partial = events.partial()
    if partial is not None:
        await store.append(partial.conversation_id, partial.messages)
    raise
```

[`PartialRunResult`](runtime.md#partialrunresult) describes the fields and the boundary message.
The Ovid servers commit the partial for you, as [Persistence on failure](server.md#persistence-on-failure) describes.

A custom `AgentRuntime` must accept both prompt types and the `resume` value.
It must raise `AgentRunInputError` when `prompt` and `resume` are both `None`.
A custom `AgentStream` that keeps no partial transcript returns `None` from `partial()`.

`stream` also takes `journal`, a `RunJournal` from `ovid_core.persistence`.
`OvidAgent.stream` accepts `journal=None` as the default and passes the value to its runtime.
`PydanticAIAgentRuntime` adds the transcript journal capability to a run that has a journal.
A custom `AgentRuntime` that wraps another `OvidAgent` must pass `journal` to it.
The Ovid servers create the journal, as [Journaled runs](server.md#journaled-runs) describes:

```python
from ovid_core.persistence import RunJournal

journal = RunJournal(store=store, conversation_id=conversation_id)
async with agent.stream('Fix the tests.', deps=deps, conversation_id=conversation_id, journal=journal) as events:
    async for event in events:
        render(event)
```

`journal.messages` holds the run messages that the step commits stored.
`await journal.end(outcome)` appends the `RunEndedEntry` of the run. It does nothing when the run wrote no entry.

### Deferred runs

A run pauses when a tool call waits for the application.
These conditions pause a run:

- The model calls a tool that requires approval.
- A tool raises `ToolApprovalRequired`.
- A tool raises `ToolDeferred`.

A paused run returns `DeferredRunResult` instead of `RunResult[Output]`.
`DeferredRunResult.requests` lists every pending call.
[Results](runtime.md#results) describes the two result models.

To continue the run, build the resume input from the requests and pass it with the paused history:

```python
from ovid_core.runtime import DeferredRunResult
from ovid_core.tools import ToolCallApproved, ToolCallDenied

result = await agent.run('Delete the old notes.', deps=deps)
if isinstance(result, DeferredRunResult):
    answers = {
        call.tool_call_id: ToolCallApproved() if user_approves(call) else ToolCallDenied(message='Not now.')
        for call in result.requests.calls
    }
    result = await agent.run(
        resume=result.requests.resume(answers),
        deps=deps,
        messages=result.history,
    )
```

`DeferredToolRequests.resume` checks the answers before any model call.
It raises `DeferredToolAnswerError` when an answer is missing, names an unknown call, or has the wrong kind.
Every pending call needs an answer in the same resume.
A resume can also contain a new prompt.
The model then receives the prompt after the tool results.

A run with neither a prompt nor resume input raises `AgentRunInputError` before any model call.

A new prompt without resume input closes each pending call in `messages`.
Each call gets a tool result with outcome `interrupted`, and the tool does not run.
The model receives these results before the new prompt.
The first message of `RunResult.messages` is a request with these results.
Thus a store that receives only `messages` also keeps them.
This rule applies when the history ends on the paused response and when it ends on a request with results of other calls.
A resume does not close the pending calls.

### Prompt types

`OvidAgent.run` and `OvidAgent.stream` accept the same `prompt` types as the runtime.
A prompt is a string or a sequence of `UserContent` items from `ovid_core.messages`.
Use a sequence to send text with images, audio, video, or documents.

```python
import base64

from ovid_core.messages import BinaryContent, ImageUrl, TextContent

result = await agent.run(
    (
        TextContent(content='Compare these images.'),
        ImageUrl(url='https://example.com/chart.png'),
        BinaryContent(data=base64.b64encode(image_bytes), media_type='image/png'),
    ),
    deps=deps,
)
```

The run history holds the prompt as a `UserPromptPart` with the same items.
The selected model must accept each media type.
[User content items](runtime.md#user-content-items) lists each item and its fields.

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

The factory supplies these defaults when their arguments are absent:

- `DefaultModelFactory`
- `ModelRouter`
- `DefaultAgentCompiler`

The factory converts `config.mcp_servers` to capabilities.
`credential_resolver` resolves credential references inside MCP environment variables and headers.

The optional `model` argument overrides the definition model for the constructed agent.

Applications that construct prompts from the effective extension surface can split preparation from compilation:

```python
prepared = await factory.prepare(definition, model=None)
extension_context = prepared.context
prepared = prepared.with_instructions((*definition.instructions, render_extensions(extension_context)))
agent = factory.build_prepared(prepared)
```

`prepare` returns a frozen `PreparedAgentDefinition` with the bound definition and an `AgentBuildContext`.
Preparation performs these actions:

- Resolves the model
- Constructs configured capabilities
- Validates service requirements
- Binds each capability once

The context describes these values:

- Selected model
- Capabilities
- Static Ovid tools
- Dynamic Ovid toolsets
- Bound services

Tool descriptors include these fields:

- Effective wire name
- Description
- Argument JSON Schema
- Approval policy
- Effect (`ToolEffect`)
- Timeout
- Input format
- Deferred-loading state

Dynamic toolset descriptors expose identity and ownership because their concrete tools can change between model steps.
Each bound component produces its own descriptor:

- `BaseCapability`
- `BaseTool`
- `BaseToolset`

`with_instructions(instructions: tuple[InstructionSource[Deps], ...])` replaces only the prepared definition instructions.
`build_prepared` compiles the prepared definition.
It does not repeat these actions:

- Capability construction
- Service binding
- Model resolution

`build` calls `prepare` followed by `build_prepared`.

`OvidAgent.run` and `OvidAgent.stream` accept the same optional override:

```python
result = await agent.run(
    prompt,
    deps=deps,
    model=ModelRef(name='user-selection'),
)
```

The override applies only to that run or stream.
The router builds each configured model handle once.
Its resolver can replace the underlying model when credentials change.

The public `diagnostics` value describes the model selected when the factory built the agent.
A run override does not mutate this value.

## Construction diagnostics

Import diagnostic models from `ovid_core.agent_build` or the high-level `ovid_core` package.

`AgentConstructionDiagnostics` fields:

- `provider`, `model`: selected provider pair.
- `requested`: original agent selector.
- `selected_model`: canonical primary model ID.
- `fallback_order`: canonical attempt order.
- `context_window`: discovered maximum context tokens, or `None` when metadata is unavailable.
- `policy`, `observability`: effective definition values.
- `extensions`: ordered `AgentExtensionProvenance` entries.

Each provenance entry contains these values:

- A `kind` identifying the component type
- A non-empty `id`
- A non-empty `source`

The supported component types are:

- `capability`
- `tool`
- `toolset`
- `hook`
- `instructions`

Diagnostics contain configuration and source names.
They do not contain credentials or upstream runtime objects.
