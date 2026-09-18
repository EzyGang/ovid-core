# Codex subscription

The Codex integration uses ChatGPT subscription authentication and the undocumented ChatGPT Codex backend.
Keep it behind `CodexSubscriptionModelFactory`.

Ovid does not change a failed subscription request to API-key billing.

## Authentication service

Import `CodexAuth` from `ovid_core.codex`.

`CodexAuth` owns:

- login
- token refresh
- logout
- the HTTP client that it creates

Keep the service open while Codex models can make requests.

```python
async with CodexAuth.persistent() as auth:
    factory = CodexSubscriptionModelFactory(auth=auth)
    handle = await factory.build(model_id='codex', config=model_config)

    async with handle._runtime:
        result = await agent.run('Complete the task')
```

Objects created in the context remain in Python scope after exit.
They must not make Codex requests after the authentication service closes.

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

Persistent authentication stores these tokens in the system keyring:

- ID tokens
- access tokens
- refresh tokens

It never falls back to a plaintext file.

### Ephemeral authentication

```python
auth = CodexAuth.ephemeral(config=oauth_config)
```

Ephemeral authentication stores tokens in process memory.
Closing the process removes the login.

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
    async def snapshot(self) -> CodexTokenSnapshot: ...
    async def compare_and_swap(self, expected_revision: int, tokens: CodexTokens) -> bool: ...
```

Import `CodexTokenSnapshot` from `ovid_core.codex`.
`CodexTokenSnapshot` contains a nonnegative `revision` and optional `tokens`.
Its representation excludes `tokens`.
`snapshot()` reads both values atomically, including the retained revision after deletion.

Every committed `save()` or `delete()` advances the revision, including deletion of an empty store.
`compare_and_swap()` replaces tokens and advances the revision only when `expected_revision` matches.
A conflict returns `False` without changing stored state.
Custom stores must enforce these guarantees across all writers, not only one service instance.

The memory store updates state atomically within the event loop.
The keyring store holds a process-shared lock for the same service and account during every read or mutation.
It stores revision and tokens in one record and retains a token-free record after deletion.
The keyring store assigns revision zero when it reads a flat token record.
Keyring lock acquisition has a five-second timeout and never falls back to unlocked access.

## Browser login

Browser login uses:

- a temporary localhost callback server
- OAuth state
- PKCE

```python
async with CodexAuth.persistent() as auth:
    login = await auth.start_browser_login()
    show_login_url(login.authorization_url)
    await login.wait()
```

The application decides how to display or open `authorization_url`.

`wait()` closes the callback server after:

- success
- rejection
- timeout
- failure
- cancellation

Use `await login.cancel()` when the user abandons login.

The default callback ports are `1455` and `1457`.
OpenAI must allow each configured callback port.

## Device-code login

Device login is suitable for terminals and remote hosts:

```python
async with CodexAuth.persistent() as auth:
    login = await auth.start_device_login()
    show_device_code(login.verification_url, login.user_code)
    await login.wait()
```

`wait()`:

- polls for approval
- exchanges the authorization code
- stores the tokens

Use `await login.cancel()` to stop polling.

Only one login attempt can run for one `CodexAuth` service.

Each login captures the stored revision before starting authorization.
A replacement or deletion supersedes that login, so its late result cannot overwrite the new state.
Cancellation stops pending authorization before releasing the login.
A storage commit that has already started completes before cancellation returns.

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

- reads authoritative stored tokens before each token request
- refreshes tokens before expiry
- saves rotating tokens only if their original revision remains current
- discards obsolete refresh results and honors replacement or deletion
- retries one request after a `401`
- deletes stored tokens through `logout()`

```python
await auth.logout()
```

These failures raise redacted `CodexAuthError` values:

- token failures
- protocol failures
- HTTP failures
- validation failures
- timeout failures

Provider or network failure does not delete stored credentials.
After a refresh conflict, an already-expired replacement fails without another refresh attempt.

Server responses use `authentication_error` with `Provider authentication failed` for known authentication rejection.
Unavailable credentials use `credential_error` with `Provider credentials are unavailable`.
These errors use the error envelope and do not close the stdio connection.

## Subscription model factory

Import `CodexSubscriptionModelFactory` from `ovid_core.adapters.pydantic_ai`.

```python
factory = CodexSubscriptionModelFactory(
    auth=auth,
    fallback=DefaultModelFactory(),
    backend_transport=transport,
)
```

`await factory.provider_options()` returns one `ModelProviderOption` for Codex subscription models from the authenticated backend catalog.
The provider registry uses it directly as the Codex model loader.
It does not enumerate fallback providers or assemble global reasoning-effort options.

Use `provider='codex-subscription'` in `ModelConfig`.
The factory delegates other providers to `fallback`.

For subscription models, the factory:

1. Uses `CodexAuth` for current credentials.
2. Creates an authenticated OpenAI HTTP client.
3. Loads the model catalog.
4. Caches the catalog for the credential revision observed before loading.
5. Constructs an `OpenAIResponsesModel` for the Codex backend.
6. Preserves catalog instructions as Responses API instructions.
7. Requires stateless Responses API operation.
8. Adds the Codex account and authentication headers.

The model owns its HTTP client after successful construction.

The factory rejects stateful Responses API settings, including:

- `openai_store`
- background mode
- conversation IDs
- previous-response IDs
