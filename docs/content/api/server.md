# Servers and transports

Install `ovid-core[server]` for native HTTP/SSE and stdio server support. Install `ovid-core[server-ag-ui]` for AG-UI.

## Shared server models

Import from `ovid_core.server.models`.

### `ServerConfig`

| Field | Default | Constraint |
| --- | --- | --- |
| `host` | `127.0.0.1` | non-empty |
| `port` | `8000` | 1–65535 |
| `max_body_bytes` | `1048576` | positive |
| `request_timeout_seconds` | `60.0` | positive |
| `max_concurrency` | `32` | positive |
| `allowed_origins` | `()` | exact CORS origins |
| `shutdown_grace_seconds` | `10` | positive |
| `resume_restart_limit` | `3` | positive |

`AgentRunRequest` contains these fields:

- `prompt`: a non-empty string, a non-empty array of content items, or `null`.
- `answers`: a non-empty object that maps a pending tool call ID to an answer, or `null`.
- `conversation_id`: optional.
- `resume`: `true` to continue an interrupted run. The default is `false`.
- `client_request_id`: an optional key from 1 to 128 characters. A client sends the same key again when it retries the same request. [Client request IDs](#client-request-ids) describes the check.

A request needs a `prompt`, `answers`, or both, unless it sets `resume`.
A request with `resume` cannot contain a `prompt` or `answers`.
A request with `answers` or `resume` needs a `conversation_id`.
[Deferred tool calls](#deferred-tool-calls) describes the answers.
[Resume](#resume) describes `resume`.
[User content items](runtime.md#user-content-items) lists each item and its JSON fields.

```json
{
  "prompt": [
    {"kind": "text", "content": "Describe this image."},
    {"kind": "image_url", "url": "https://example.com/chart.png"},
    {"kind": "binary", "data": "iVBORw0KGgo=", "media_type": "image/png"}
  ]
}
```

Validation rejects these prompts:

- An item with a `kind` that the content items do not include, such as a cache point.
- A URL item with `force_download='allow-local'`, because a client must not make the server read a private address.
- A `binary` item whose `data` is not Base64 text.

`max_body_bytes` is the only size limit for a prompt.
Base64 text is approximately 4/3 the size of its bytes, so increase `max_body_bytes` for large inline files.

`AgentRunResponse` contains JSON-compatible `output`, normalized `messages`, `usage`, `run_id`, and `conversation_id`. `RunResultSSEEvent` adds `kind='run_result'`.

A paused run also sets `requests`, a `DeferredToolRequests` object, and `output` is `null`.
A completed run has no `requests` key.
Thus a client that never pauses a run receives the same completed result as before.

`HealthResponse.status` is `ok` or `not_ready`.

`ServerErrorResponse` has non-empty `code` and `message`. `ServerErrorSSEEvent` adds `kind='server_error'`.
The errors `conversation_busy` and `duplicate_request` also have `run_id`. Other errors do not have this key.

A `ProviderError` uses the code `provider_error` and the HTTP status 502.
Its response also has a `provider` object, a `ProviderErrorDetails` value with these fields:

- `failure_kind`: the `ProviderFailureKind` value.
- `status_code`: the provider HTTP status, or `null`.
- `provider_code`: the error code that the provider sent, or `null`.
- `retry_after_seconds`: the wait that the provider asked for, or `null`.

Other error responses have no `provider` key.

### Deferred tool calls

A paused run ends with `tool_deferred` events, the final `usage_update`, `run_deferred`, and a result with `requests`.
To continue, send a request with the same `conversation_id` and one answer for each call in `requests`:

```json
{
  "conversation_id": "7d2c9b0e-4f5a-4bd8-9a39-3b6f0c1e2d4a",
  "answers": {
    "call-1": {"kind": "approved"},
    "call-2": {"kind": "denied", "message": "Do not delete that file."},
    "call-3": {"kind": "returned", "value": {"rows": 4}},
    "call-4": {"kind": "failed", "message": "The job did not finish."}
  }
}
```

Use `approved` or `denied` for an `approval` call.
Use `returned` or `failed` for an `external` or `question` call.
An answer cannot change the call arguments or supply approval metadata.

The server applies these authority rules before any model call:

- The store must implement `DeferredToolRequestStore`. Otherwise the server returns `deferred_tools_unavailable`.
- The server builds the resume input from the stored record. It does not use call kinds or metadata from the client.
- A call is answerable only when it is in the record and has no result in the stored messages.
- Each pending call needs exactly one answer of the matching kind.

A request that breaks the last two rules, or a request for a conversation without a record, returns `invalid_tool_answers`.
A new `prompt` without `answers` closes the pending calls as `interrupted`, and the completed run clears the record.
[DeferredToolRequestStore](persistence.md#deferredtoolrequeststore) describes the write order.

An agent with a [`DeferredToolAnswerCapability`](extensions.md#inline-deferred-answers) answers some calls during the run.
The server does not apply these rules to those answers, because the application supplies them in process.
The stored messages contain the results of the answered calls.
The `requests` of the result and the stored record contain only the calls that stay pending.
The client answers only these calls when it continues the run.

The server admits one active run for each conversation.
A second run on a conversation with an active run returns `conversation_busy` with the `run_id` of the active run.
While a partial commit keeps the conversation admitted, `run_id` is the run of that commit.
This rule applies to a new prompt and to a resume.
Runs on different conversations run at the same time.

### Persistence on failure

The native HTTP, stdio, and AG-UI servers commit the partial transcript of a run that does not complete.
This applies to these terminal paths:

- A provider, tool, or run error.
- A usage limit or a full context window.
- A timeout.
- A cancellation request.
- A client disconnect, or stdin EOF for the stdio server.

The server commits [`PartialRunResult`](runtime.md#partialrunresult) with the same store call as a completed run, then it sends the failure.
The commit does not save or clear the record of pending deferred tool calls.
The native HTTP and stdio servers keep the conversation admitted until the commit ends, even when the run already ended.
Thus a following run on the same conversation gets `conversation_busy` or loads the committed transcript.

The commit has these limits:

- A cancellation that arrives during the commit does not stop it.
- The server waits at most `shutdown_grace_seconds` for the commit before it sends the failure.
- The server never cancels the commit. A commit that is still running after the wait keeps the conversation admitted until it ends.
- A commit error never replaces the run error.

`run_failed` carries `persisted=true` when the commit finished before the server sent the event.
It carries `persisted=false` when the commit failed or did not finish in time, when there is no store, or when the run added no message.
On the next run, Pydantic AI closes each tool call of the partial that has no result, and the model sees the boundary text.

The stored outcome agrees with the error that the server sends for each stop that the server starts:

| Stop | Error code | Boundary text |
| --- | --- | --- |
| `request_timeout_seconds` | `timeout` | `[Run ended: timeout: Request timed out]` |
| Shutdown or stdin EOF | `run_cancelled` | `[Run ended: cancelled: Run cancelled]` |
| Client cancellation request | `run_cancelled` | `[Run ended: cancelled: Run cancelled]` |
| HTTP client disconnect | `run_cancelled`, not delivered | `[Run ended: cancelled: Run cancelled]` |

For a request timeout, the partial also has `error_type='TimeoutError'`, the same value as the `run_failed` event.

### Journaled runs

When the store implements [`ConversationJournalStore`](persistence.md#conversationjournalstore), the native HTTP and stdio servers journal each admitted run.
The server creates a `RunJournal` for the conversation and passes it to `OvidAgent.stream(..., journal=...)`.
The run then commits each step and records each tool intent before the tool runs.
At the end of the run, the server writes only the messages that the step commits do not hold, then `RunEndedEntry`.
For a partial transcript, `RunEndedEntry.outcome` is the outcome of the partial, for example `cancelled` or `timeout`.
A partial commit that fails leaves the run open, so the next load recovers it.
AG-UI runs are not admitted, so the server does not journal them.

### Recovery on load

A process can stop without a commit, for example after a kill, an out-of-memory error, or a power loss.
The native HTTP and stdio servers recover the conversation when they load it for an admitted run:

1. The server admits the run, then it calls `load_journal`.
2. When the journal has an open run, the server reads the pending deferred calls, if the store implements `DeferredToolRequestStore`.
3. The server appends the entries of [`close_orphaned_tool_calls`](persistence.md#recovery) in one `append_entries` call.
4. The server loads the history again and starts the run with it.

Recovery runs before the server closes pending calls for a new prompt.
Thus the model sees the recovery text, not the generic interrupted text.
A run that a server closed with a partial commit has a `RunEndedEntry`, so recovery does not change it.
Pydantic AI closes the tool calls of that partial on the next run, as for a store without the journal.
A second load finds no open run and adds nothing.
A run that is not admitted, such as an AG-UI run, does not recover, because an open run can belong to an active run.

Recovery does not answer a pending deferred call.
A resume of that call works after recovery.

### Resume

A run that stopped without a result can continue without a new prompt.
Send a request with `resume` and the same `conversation_id`:

```json
{"conversation_id": "7d2c9b0e-4f5a-4bd8-9a39-3b6f0c1e2d4a", "resume": true}
```

The server refuses the resume before recovery changes the conversation in these conditions:

| Condition | Error code |
| --- | --- |
| The store holds pending deferred calls that have no result in the stored messages. These calls need answers. | `nothing_to_resume` |
| The journal shows `resume_restart_limit` or more stopped runs in a row. | `resume_limit_exceeded` |

[`restart_count`](persistence.md#restart-count) gives the number of stopped runs in a row.
The `resume_limit_exceeded` message is `Run was interrupted <count> times. Send a new prompt.`
A new prompt does not have this limit, and a completed run starts a new count.
A store without `ConversationJournalStore` has no run outcomes, so the server does not apply the limit.

Then the server recovers the conversation with `replay_safe=True`, as [Recovery](persistence.md#recovery) describes.
Recovery keeps open each started call of the last response whose journal intent is safe to replay.
The server starts the run with `InterruptedRunResume`, and [Interrupted runs](agents.md#interrupted-runs) describes the run.
The run checks the effect of the current tool again before a call runs.
Thus a call runs again only when its journal intent and its current tool both have `replay='safe'`.

A conversation without an interrupted run returns `nothing_to_resume`.
For example, an empty conversation or a conversation whose last run completed has no interrupted run.
A resume that stops again gets its own partial commit and journal outcome.

### Client request IDs

A client can retry a request after it lost the response.
Send the same `client_request_id` with the retry, so that the server does not run the request again.
The server reads the key only when the store implements [`ConversationJournalStore`](persistence.md#conversationjournalstore).
A store without the journal cannot find the key, so the server ignores it.

The server stores the key in the `RunStartedEntry` of the run.
After it admits a run, the server looks for the key in the runs of the same conversation.
This check comes before the resume checks and before recovery, so nothing is written for a duplicate request.

| Earlier run with the same key | Response |
| --- | --- |
| Completed or deferred | `duplicate_request` with the `run_id` of that run. The model is not called. |
| Lost, or stopped with a partial commit | `duplicate_request` with the `run_id` of that run. The message tells the client to send a `resume` request. |
| Active | `conversation_busy` with the `run_id` of the active run. |

A key in a different conversation does not count.
A run that wrote no journal entry did not store its key.

## Registration and callbacks

Import from `ovid_core.server.contracts`.

### `RequestContext`

```text
RequestContext(
    *,
    method: str,
    path: str,
    headers: Iterable[tuple[str, str]] = (),
    client_host: str | None = None,
    request_id: str,
)
```

Read-only properties: `method`, `path`, `client_host`, and `request_id`. `header(name)` performs a case-insensitive lookup. `repr` excludes headers so authorization material is not exposed.

### Authorization and dependencies

```python
class AuthorizationCallback(Protocol):
    async def __call__(
        self,
        context: RequestContext,
        resource_id: str,
        /,
    ) -> AuthorizationResult: ...

class DependenciesFactory[Deps](Protocol):
    async def __call__(
        self,
        context: RequestContext,
        authorization: AuthorizationResult,
    ) -> Deps: ...
```

`AuthorizationResult(allowed, principal=None)` is immutable.

Agent transports pass the agent ID as `resource_id`. Stdio commands pass `command:<id>`.

The server constructs dependencies only after successful authorization.

### Other callbacks

- `CommandHandler(context, authorization, arguments) -> JsonValue` handles one stdio command.
- `ReadinessCallback() -> bool` determines `/ready` state.
- `LifecycleCallback() -> None` runs at startup or shutdown.
- `ASGIApplication(scope, receive, send) -> None` is the returned native app protocol.
- `ASGIScope`, `ASGIMessage`, `ASGIReceive`, and `ASGISend` are ASGI type aliases.

### Registrations

`AgentRegistration` and `CommandRegistration` are immutable dataclasses.

IDs accept only letters, digits, `_`, and `-`. Descriptions must contain non-space characters.

The server rejects duplicate registration IDs during construction.

### Deferred registration

Import `AgentRegistry` from `ovid_core.server`.
Pass the same registry as `agents` to `create_agent_app` or `create_stdio_server`.
Both native transports resolve agents added after server construction.

Use a configured `factory` with a `primary` model, as shown in [Getting started](../getting-started.md).
Supply the application's `authorize` callback.
`AgentRegistration` associates a constructed agent with its server ID, description, and per-request dependency callback.

```python
from ovid_core.agents import AgentDefinition
from ovid_core.routing import ModelRef
from ovid_core.server import (
    AgentRegistration,
    AgentRegistry,
    AuthorizationResult,
    RequestContext,
    create_agent_app,
    create_stdio_server,
)


async def dependencies(
    context: RequestContext,
    authorization: AuthorizationResult,
) -> None:
    return None


registry = AgentRegistry()
app = create_agent_app(agents=registry, authorize=authorize)
stdio = create_stdio_server(agents=registry, authorize=authorize)

agent = await factory.build(
    AgentDefinition[None, str](
        model=ModelRef(name='primary'),
        deps_type=type(None),
        output_type=str,
        instructions=('Answer writing requests concisely.',),
    )
)
registration = AgentRegistration(
    id='writer',
    description='Answer writing requests.',
    agent=agent,
    dependencies=dependencies,
)
registry.register((registration,))
```

Register on the server event loop.
Each batch is atomic and rejects duplicate IDs, including IDs that already exist.
Registrations cannot be replaced or removed.
The registry implements `Sequence`, so existing static registration sequences remain supported.
`registry.get(agent_id)` returns the matching registration or `None` through indexed lookup.
An empty registry advertises no agents and rejects agent runs until registration.
Stdio initialization returns the current registry contents.
Authorization, agent diagnostics, concurrency limits, cancellation, and persistence apply to newly registered agents without special cases.

## Native HTTP and SSE

Import from `ovid_core.server.app`.

```python
app = create_agent_app(
    agents=registrations,
    authorize=authorize,
    config=ServerConfig(),
    store=None,
    readiness=None,
    startup=None,
    shutdown=None,
)
serve(app, config=ServerConfig())
```

`create_agent_app` returns an ASGI application. Missing server dependencies raise `ServerConstructionError`.

`serve` starts Uvicorn with the configured host, port, and shutdown timeout. This function blocks the current thread.

### Routes

| Route | Contract |
| --- | --- |
| `GET /health` | Returns `HealthResponse(status='ok')`. |
| `GET /ready` | Returns `ok`, or `not_ready` with HTTP 503 when the readiness callback returns false. |
| `POST /agents/{agent_id}/events` | Accepts `AgentRunRequest` JSON and returns an SSE stream. |
| `DELETE /agents/{agent_id}/runs/{run_id}` | Requests cancellation of an active run that belongs to the authorized principal. |

Requests require `Content-Type: application/json`. Body size must not exceed `max_body_bytes`.

The server applies `request_timeout_seconds`. It enables CORS only when `allowed_origins` is not empty.

The runtime applies global and agent concurrency limits. It authorizes the agent before history access or dependency construction.

The runtime creates a conversation ID when necessary. It loads history and appends new messages through the optional store.

The effective timeout is the smaller server or agent timeout.

Native HTTP runs use an SSE stream.
Native HTTP has no synchronous run endpoint.
The SSE stream carries `AgentEvent` records and a final `run_result` with the `AgentRunResponse` fields.
The `run_started` event contains a server-generated run ID of type `RunId`.

Use the run ID with `DELETE /agents/{agent_id}/runs/{run_id}` to send a cancellation request.

A cancellation request requires a stable, non-`None` authorized principal.
The server returns a bodyless HTTP 204 response to an authorized cancellation request.
The original SSE stream delivers the terminal cancellation result asynchronously.
Unauthorized callers and callers without a principal receive HTTP 403.

The server also returns HTTP 204 for these targets without changing another run:

- A run that belongs to another authorized principal.
- A run for another agent.
- A stale run ID.
- A completed run.

The runtime emits `run_completed` or `run_deferred` only after persistence succeeds.
The runtime emits `run_result` after `run_completed` or `run_deferred`.
Cancellation or failure before durable completion produces at most one `run_failed` event before `server_error`.
The runtime sends this `run_failed` event after the partial commit ends.
The runtime does not emit `run_completed` or `run_deferred` for cancellation or failure before durable completion.
The runtime emits only `server_error` for failures before streaming starts.
A client disconnect cancels the run, and the server commits the partial transcript before it releases the conversation.

## AG-UI

Import from `ovid_core.server.ag_ui`.

```python
app = create_ag_ui_app(
    agents=registrations,
    authorize=authorize,
    config=ServerConfig(),
    store=None,
)
```

The returned ASGI app exposes `POST /agents/{agent_id}` with an AG-UI stream.

This app requires agents from `DefaultAgentCompiler`.

The server controls system prompts, tools, context, resume state, forwarded properties, run identity, and history.

The server rejects client control of these values. A client thread ID maps to an Ovid `ConversationId`.

The server rejects a user message whose content is not a string.
Use the native HTTP server or the stdio server to send a prompt with content items.

The AG-UI bridge does not support deferred tool calls.
A run that pauses for a tool call ends with a `RUN_ERROR` event.
Its message is `The AG-UI bridge does not support deferred tool calls`.
The bridge also rejects a client `resume` value.
Use the native HTTP server or the stdio server for tools that need approval, or set `AgentDefinition.tool_approval`.

The AG-UI app commits the partial transcript of a failed or cancelled run before it sends `RUN_ERROR`.
AG-UI has no `persisted` flag.
A client disconnect commits the partial only when the cancellation reaches the run while it waits for the model or a tool.
When the cancellation reaches the response while it writes an event, the stream closes without a commit.

## Stdio server

Import from `ovid_core.server.stdio`.

```python
server = create_stdio_server(
    agents=registrations,
    authorize=authorize,
    commands=(),
    config=ServerConfig(),
    store=None,
    startup=None,
    shutdown=None,
)
await server.run()
```

`StdioAgentServer.run()` reads one UTF-8 JSON request per line from stdin and writes one JSON response per line to stdout. It shares agent authorization, limits, persistence, and lifecycle behavior with the native server. An oversized line writes `request_too_large` and ends the server.

One request line, with its prompt, must not exceed `max_body_bytes`.

### Stdio request models

Import from `ovid_core.server.stdio_models`. Every request has `version=1` and non-empty `request_id`.

| Model | Additional fields |
| --- | --- |
| `StdioInitializeRequest` | `type='initialize'` |
| `StdioRunRequest` | `type='run'`, non-empty `agent_id`, `request: AgentRunRequest` |
| `StdioCommandRequest` | `type='command'`, non-empty `command_id`, `arguments: JsonValue=None` |
| `StdioCancelRequest` | `type='cancel'`, non-empty `target_request_id` |

`StdioRequest` is the discriminated union on `type`.

### Stdio response models

Every response has `version=1` and `request_id: str | None`.

| Model | Additional fields |
| --- | --- |
| `StdioInitializedResponse` | `type='initialized'`, agent and command `StdioDescriptor` tuples |
| `StdioEventResponse` | `type='event'`, `event: AgentEvent` |
| `StdioRunResultResponse` | `type='run_result'`, `result: AgentRunResponse` |
| `StdioCommandResultResponse` | `type='command_result'`, JSON `result` |
| `StdioErrorResponse` | `type='error'`, `error: ServerErrorResponse` |

`StdioResponse` is their union. Initialization enumerates registrations. A run may emit multiple event responses and exactly one final result or error response. Commands return one command result or error.

A paused run uses the same frames.
Its last event response carries `run_deferred`, and its `run_result` frame carries a result with `requests`.
A `StdioRunRequest` with `request.answers` resumes the paused run on the same `conversation_id`.
Because runs on one connection execute in FIFO order, a resume that follows a paused run starts after the server stores that run.

Runs and commands execute in FIFO order.
Initialization and cancellation requests remain available while a run executes.
A cancellation request targets only a run request from the same stdio connection.
The target can be a queued run or a run before streaming starts.
The stdio server does not repeat authorization or compare principals for cancellation requests.

The stdio server does not acknowledge successful or stale cancellation requests.
The stdio server sends terminal frames for cancellation on the original run request.
An internal cancellation failure produces an error that contains the cancellation request ID instead.

The stdio server uses the same durable completion ordering as the native HTTP SSE stream.
The stdio server starts cleanup when any of these conditions occurs:

- The input reaches EOF.
- An input line exceeds the size limit.
- The server shuts down.

Cleanup follows this sequence:

1. The stdio server cancels active runs.
2. Each cancelled run starts its partial commit, waits for it for at most `shutdown_grace_seconds`, and writes its `run_failed` and error frames.
3. The stdio server waits for the run tasks for at most `shutdown_grace_seconds`.
4. The stdio server invokes the shutdown callback, which has its own `shutdown_grace_seconds` limit.

The wait in step 3 also bounds the commit waits of step 2, so the server adds no other wait for the commits.
A commit that does not finish in that time is not cancelled, and the process can exit before it ends.
