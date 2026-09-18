# Credentials

Configuration stores serializable references, never resolved secrets.
Resolver implementations return Pydantic `SecretStr` values.

## Credential references

Import credential reference types from `ovid_core.credentials.models`.
`CredentialRef` is a Pydantic discriminated union on `kind`.

| Model | Fields | Serialized example |
| --- | --- | --- |
| `EnvironmentCredentialRef` | `kind='environment'`, non-empty `variable` | `{'kind': 'environment', 'variable': 'OPENAI_API_KEY'}` |
| `NamedCredentialRef` | `kind='named'`, non-empty `name` | `{'kind': 'named', 'name': 'production-openai'}` |
| `FileCredentialRef` | `kind='file'`, `path: Path` | `{'kind': 'file', 'path': '~/.secrets/token'}` |
| `CallbackCredentialRef` | `kind='callback'`, non-empty `callback` | `{'kind': 'callback', 'callback': 'application-resolver'}` |
| `StoreCredentialRef` | `kind='store'`, non-empty `store` and `name` | `{'kind': 'store', 'store': 'vault', 'name': 'openai'}` |

`FileCredentialRef` expands `~` during validation.
The reference does not read the file.

The application defines behavior for these references:

- named references
- callback references
- file references
- external-store references

```python
from ovid_core.credentials.models import CredentialRef
from pydantic import TypeAdapter

reference = TypeAdapter(CredentialRef).validate_python(
    {'kind': 'environment', 'variable': 'OPENAI_API_KEY'}
)
```

## Resolver protocol

Import from `ovid_core.credentials.resolvers`.

```python
class CredentialResolver(Protocol):
    async def resolve(self, reference: CredentialRef) -> SecretStr: ...
```

A resolver raises `CredentialError` when it cannot resolve a supported reference.

Do not put secret values in these locations:

- exceptions
- configuration
- logs
- serialized DTOs

## Provider API-key callback

`ProviderAPIKeyResolver` is an async callable:

```python
async def provider_api_key(model_id: str, provider: str) -> SecretStr | None: ...
```

Pass this callable to `AgentFactory(provider_api_key=...)`.

The default model factory calls `provider_api_key` during construction.
Compiled agents use Pydantic AI `SelectModel` to resolve credentials before each new logical model request.
This applies to normal and streamed agent runs.

Return a `SecretStr` to inject the current key into the provider.

Changed keys apply to subsequent requests without restarting the agent turn.
Active requests and streams keep their original provider until they finish.
The factory reuses the model when the resolved key is unchanged.
Configured settings and concurrency limits remain effective after rotation.
Resolver failures stop the request instead of reusing a stale key.

Pydantic AI owns the selected models and their lifetimes.
Same-step continuations keep their selected model.
Direct calls to `handle.runtime` use the initial model snapshot and do not resolve credentials again.

Return `None` to use the provider environment or native authentication.

This callback supports application-owned storage.
It does not put the key in `OvidConfig` or modify process environment variables.


## Provider authentication registry

Import provider authentication contracts from `ovid_core.authentication`.

`ProviderRegistry` supplies:

- enabled providers
- authentication flows
- connection state
- authenticated model options

Each `ProviderDefinition` supplies:

- a stable provider ID and label
- one or more `AuthenticationFlow` implementations
- a `ProviderCredentialBinding`
- an async model-option loader

`AuthenticationFlowSession` returns transport-neutral interactions for:

- URL display
- secret input
- progress
- completion
- failure

Interactions contain only:

- semantic input kinds
- progress stages
- failure reasons

They do not contain:

- UI messages
- prompts
- placeholders
- instructions
- completion text

Applications render these interactions and provide concrete credential stores.

`default_provider_registry` automatically includes:

- every Pydantic AI model provider whose installed adapter accepts an API key
- Codex browser and device authentication
- caller-supplied custom definitions

Ovid Core installs all Pydantic AI model-provider extras, so the default registry exposes the complete upstream provider catalog.

Pass `excluded_provider_ids` to remove providers.

Pass `custom_definitions` to add providers or replace a generated definition with the same ID.

Provider modules own provider-specific behavior.

The registry and application surfaces contain no provider-specific dispatch.


## Environment resolver

```python
class EnvironmentCredentialResolver:
    def __init__(self, environment: Mapping[str, str] | None = None) -> None: ...
    async def resolve(self, reference: CredentialRef) -> SecretStr: ...
```

With no mapping, the constructor copies `os.environ`.
A supplied mapping gives deterministic behavior in tests and applications.

`resolve` accepts only `EnvironmentCredentialRef`.
It raises `CredentialError` for unsupported kinds or missing variables.

```python
from ovid_core.credentials.models import EnvironmentCredentialRef
from ovid_core.credentials.resolvers import EnvironmentCredentialResolver

resolver = EnvironmentCredentialResolver({'TOKEN': 'secret'})
secret = await resolver.resolve(EnvironmentCredentialRef(variable='TOKEN'))
```
