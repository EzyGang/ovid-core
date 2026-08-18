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
| `POST /agents/{agent_id}/runs` | Accepts `AgentRunRequest` JSON and returns `AgentRunResponse` JSON. |
| `POST /agents/{agent_id}/events` | Accepts the same request. Returns `AgentEvent` records, then `run_result`. Transport failures become `server_error`. |

Requests require `Content-Type: application/json`. Body size must not exceed `max_body_bytes`.

The server applies `request_timeout_seconds`. It enables CORS only when `allowed_origins` is not empty.

The runtime applies global and agent concurrency limits. It authorizes the agent before history access or dependency construction.

The runtime creates a conversation ID when necessary. It loads history and appends new messages through the optional store.

The effective timeout is the smaller server or agent timeout.

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
