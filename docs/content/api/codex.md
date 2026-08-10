# Codex subscription

The Codex integration uses OpenAI's device authorization and the undocumented ChatGPT Codex backend. Keep it isolated behind `CodexSubscriptionModelFactory`. It never silently falls back from subscription access to API-key billing for the same configured provider.

## OAuth values

Import from `ovid_core.codex.models`.

### `CodexOAuthConfig`

| Field | Default |
| --- | --- |
| `issuer` | `https://auth.openai.com` |
| `client_id` | Codex device-flow client ID |
| `backend_url` | `https://chatgpt.com/backend-api/codex` |
| `poll_timeout_seconds` | `900` |
| `refresh_window_seconds` | `300` |

URL values must be non-empty. The poll timeout must be positive. The refresh window must be non-negative.

### `CodexDeviceAuthorization`

The public handoff contains a non-empty `verification_url` and `user_code`.

Serialization and repr output omit `device_auth_id` and `interval_seconds`.

### `CodexTokens`

Contains `id_token`, `access_token`, and rotating `refresh_token` as `SecretStr`. Repr output does not show these values.

Do not put this model in configuration, result metadata, logs, or transport data.

## Device authorization

Import `CodexDeviceAuthClient` from `ovid_core.codex.device`.

```python
client = CodexDeviceAuthClient(
    http_client=http_client,
    token_manager=token_manager,
    config=oauth_config,
)
authorization = await client.start()
print(authorization.verification_url, authorization.user_code)
tokens = await client.complete(authorization)
```

- `start()` requests a device authorization and returns the browser handoff.
- `complete(authorization)` polls for authorization. It exchanges the authorization code and saves the returned tokens.
- Protocol, HTTP, validation, and timeout failures raise `CodexAuthError` without exposing tokens, codes, or response bodies.

The caller owns the injected `httpx.AsyncClient` lifecycle.

## Token storage and refresh

Import from `ovid_core.codex.tokens`.

```python
class CodexTokenStore(Protocol):
    async def load(self) -> CodexTokens | None: ...
    async def save(self, tokens: CodexTokens) -> None: ...
    async def delete(self) -> None: ...
```

`CodexTokenManager(store, http_client, config)` serializes token operations with an async lock:

- `tokens(force_refresh=False)` loads cached or stored tokens, refreshes an access token near expiry or when forced, persists rotating tokens, and returns the current value.
- `save(tokens)` persists and caches an authenticated token set.
- `logout()` deletes storage and clears the cache.
- Missing authentication and refresh failures raise `CodexAuthError`.

`codex_account_id(tokens) -> str` reads the ChatGPT account ID from the identity-token claims. A malformed token or missing account claim raises `CodexAuthError`.

## System keyring

Import `KeyringCodexTokenStore` from `ovid_core.codex.keyring`.

```python
store = KeyringCodexTokenStore(
    service='ovid-core.codex',
    account='default',
)
```

`load`, `save`, and `delete` implement `CodexTokenStore` by moving blocking keyring access to worker threads. Keyring and stored-payload failures become redacted `CodexAuthError` values. Deleting a missing entry is a no-op.

## Instruction catalog

Import from `ovid_core.codex.catalog`.

- `CodexInstructionCatalog.models` contains validated internal catalog entries.
- `instructions_for(model_name)` returns the selected model's base instructions or raises `ModelResolutionError`.
- `load_instruction_catalog(http_client=..., backend_url=...)` requests `/models`, validates the response while ignoring unknown fields, and raises `ModelResolutionError` on HTTP or schema failure.

Most consumers should not call the loader directly. `CodexSubscriptionModelFactory` loads and caches the catalog once per factory.

## Subscription model factory

Import `CodexSubscriptionModelFactory` from `ovid_core.adapters.pydantic_ai.codex`.

```python
factory = CodexSubscriptionModelFactory(
    token_manager=token_manager,
    config=oauth_config,                 # optional
    fallback=DefaultModelFactory(),      # optional
    backend_transport=transport,         # optional
)
handle = await factory.build(model_id='codex', config=model_config)
```

Use `provider='codex-subscription'` in `ModelConfig`. Other providers delegate to `fallback`, which defaults to `DefaultModelFactory`.

For subscription models the factory:

1. Obtains and refreshes OAuth tokens through `CodexTokenManager`.
2. Creates and owns an authenticated OpenAI HTTP client.
3. fetches validated base instructions from the model catalog once.
4. Constructs an `OpenAIResponsesModel` against the ChatGPT Codex backend.
5. Preserves catalog instructions as top-level Responses instructions and maps consumer instructions to developer input.
6. Requires streaming, `store=false`, encrypted reasoning replay, and Codex authentication headers.
7. Closes its HTTP client if model construction fails.

The model owns the HTTP client after successful construction.

The factory rejects stateful Responses API settings with `ModelResolutionError`.

These settings include `openai_store`, background mode, conversation IDs, and previous-response IDs.
