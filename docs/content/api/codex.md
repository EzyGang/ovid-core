# Codex subscription

The Codex integration uses ChatGPT subscription authentication and the undocumented ChatGPT Codex backend. Keep it behind `CodexSubscriptionModelFactory`.

Ovid does not change a failed subscription request to API-key billing.

System-keyring storage is included with Ovid Core. Use `CodexAuth.ephemeral()` when credentials must remain in memory.

## Authentication service

Import `CodexAuth` from `ovid_core.codex`.

`CodexAuth` owns login, token refresh, logout, and its optional HTTP client. Keep the service open while Codex models can make requests.

```python
async with CodexAuth.persistent() as auth:
    factory = CodexSubscriptionModelFactory(auth=auth)
    handle = await factory.build(model_id='codex', config=model_config)

    async with handle._runtime:
        result = await agent.run('Complete the task')
```

Objects created in the context remain in Python scope after exit. They must not make Codex requests after the authentication service closes.

Pass an `httpx.AsyncClient` when the application owns the client lifecycle:

```python
auth = CodexAuth.persistent(http_client=http_client)
```

The context manager does not close an injected client.

### Persistent authentication

```python
auth = CodexAuth.persistent(
    service='ovid-core.codex',
    account='default',
    config=oauth_config,
)
```

This mode stores ID, access, and refresh tokens in the system keyring. It never falls back to a plaintext file.

### Ephemeral authentication

```python
auth = CodexAuth.ephemeral(config=oauth_config)
```

This mode stores tokens in process memory. Closing the process removes the login.

### Custom storage

Applications can inject a store directly:

```python
auth = CodexAuth(
    store=application_token_store,
    http_client=http_client,
    config=oauth_config,
)
```

A custom store implements `CodexTokenStore`:

```python
class CodexTokenStore(Protocol):
    async def load(self) -> CodexTokens | None: ...
    async def save(self, tokens: CodexTokens) -> None: ...
    async def delete(self) -> None: ...
```

## Browser login

Browser login uses a temporary localhost callback server, OAuth state, and PKCE.

```python
async with CodexAuth.persistent() as auth:
    login = await auth.start_browser_login()
    show_login_url(login.authorization_url)
    await login.wait()
```

The application decides how to display or open `authorization_url`.

`wait()` closes the callback server after success, rejection, timeout, failure, or cancellation. Use `await login.cancel()` when the user abandons login.

The default callback ports are `1455` and `1457`. OpenAI must allow each configured callback port.

## Device-code login

Device login is suitable for terminals and remote hosts:

```python
async with CodexAuth.persistent() as auth:
    login = await auth.start_device_login()
    show_device_code(login.verification_url, login.user_code)
    await login.wait()
```

`wait()` polls for approval, exchanges the authorization code, and stores the tokens. Use `await login.cancel()` to stop polling.

Only one login attempt can run for one `CodexAuth` service.

## OAuth configuration

Import `CodexOAuthConfig` from `ovid_core.codex`.

| Field | Default |
| --- | --- |
| `issuer` | `https://auth.openai.com` |
| `client_id` | Codex OAuth client ID |
| `backend_url` | `https://chatgpt.com/backend-api/codex` |
| `callback_ports` | `(1455, 1457)` |
| `login_timeout_seconds` | `900` |
| `refresh_window_seconds` | `300` |

The login timeout applies to browser and device-code login.

## Token lifecycle

Applications do not need to handle tokens during normal use.

`CodexAuth`:

- loads stored tokens
- refreshes tokens before expiry
- saves rotating tokens
- serializes token changes
- retries one request after a `401`
- deletes stored tokens through `logout()`

```python
await auth.logout()
```

Token, protocol, HTTP, validation, and timeout failures raise redacted `CodexAuthError` values.

## Subscription model factory

Import `CodexSubscriptionModelFactory` from `ovid_core.adapters.pydantic_ai`.

```python
factory = CodexSubscriptionModelFactory(
    auth=auth,
    fallback=DefaultModelFactory(),
    backend_transport=transport,
)
```

Use `provider='codex-subscription'` in `ModelConfig`. Other providers delegate to `fallback`.

For subscription models, the factory:

1. Uses `CodexAuth` for current credentials.
2. Creates an authenticated OpenAI HTTP client.
3. Loads and caches the validated model catalog.
4. Constructs an `OpenAIResponsesModel` for the Codex backend.
5. Preserves catalog instructions as Responses API instructions.
6. Requires stateless Responses API operation.
7. Adds the Codex account and authentication headers.

The model owns its HTTP client after successful construction.

The factory rejects stateful Responses API settings. These settings include `openai_store`, background mode, conversation IDs, and previous-response IDs.
