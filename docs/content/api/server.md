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

`AgentRunRequest` contains a non-empty `prompt` and optional `conversation_id`.

`AgentRunResponse` contains JSON-compatible `output`, normalized `messages`, `usage`, `run_id`, and `conversation_id`. `RunResultSSEEvent` adds `kind='run_result'`.

`HealthResponse.status` is `ok` or `not_ready`.

`ServerErrorResponse` has non-empty `code` and `message`. `ServerErrorSSEEvent` adds `kind='server_error'`.

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

The runtime emits `run_completed` only after persistence succeeds.
The runtime emits `run_result` after `run_completed`.
Cancellation or failure before durable completion produces at most one `run_failed` event before `server_error`.
The runtime does not emit `run_completed` for cancellation or failure before durable completion.
The runtime emits only `server_error` for failures before streaming starts.

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
2. The stdio server waits for the run tasks.
3. The stdio server invokes the shutdown callback.
