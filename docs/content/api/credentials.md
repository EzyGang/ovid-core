# Credentials

Configuration stores serializable references, never resolved secrets. Resolver implementations return Pydantic `SecretStr` values.

## Credential references

Import from `ovid_core.credentials.models`. `CredentialRef` is a Pydantic discriminated union on `kind`.

| Model | Fields | Serialized example |
| --- | --- | --- |
| `EnvironmentCredentialRef` | `kind='environment'`, non-empty `variable` | `{'kind': 'environment', 'variable': 'OPENAI_API_KEY'}` |
| `NamedCredentialRef` | `kind='named'`, non-empty `name` | `{'kind': 'named', 'name': 'production-openai'}` |
| `FileCredentialRef` | `kind='file'`, `path: Path` | `{'kind': 'file', 'path': '~/.secrets/token'}` |
| `CallbackCredentialRef` | `kind='callback'`, non-empty `callback` | `{'kind': 'callback', 'callback': 'application-resolver'}` |
| `StoreCredentialRef` | `kind='store'`, non-empty `store` and `name` | `{'kind': 'store', 'store': 'vault', 'name': 'openai'}` |

`FileCredentialRef` expands `~` during validation. The reference does not read the file.

The application defines named, callback, file-reference, and external-store behavior.

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

Do not put secret values in exceptions, configuration, logs, or serialized DTOs.

## Provider API-key callback

`ProviderAPIKeyResolver` is an async callable:

```python
async def provider_api_key(model_id: str, provider: str) -> SecretStr | None: ...
```

Pass this callable to `AgentFactory(provider_api_key=...)`.

The default model factory calls it when it constructs a configured model. Return a `SecretStr` to inject the key into the provider.

Return `None` to use the provider environment or native authentication.

This callback supports application-owned storage. It does not put the key in `OvidConfig` or modify process environment variables.


## Provider authentication registry

Import provider authentication contracts from `ovid_core.authentication`.

`ProviderRegistry` is the source of enabled providers, authentication flows, connection state, and authenticated model options.

Each `ProviderDefinition` supplies:

- a stable provider ID and label
- one or more `AuthenticationFlow` implementations
- a `ProviderCredentialBinding`
- an async model-option loader

`AuthenticationFlowSession` returns transport-neutral interactions for URL display, secret input, progress, completion, and failure.

Interactions contain semantic input kinds, progress stages, and failure reasons only.

They do not contain UI messages, prompts, placeholders, instructions, or completion text.

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

With no mapping, the constructor copies `os.environ`. A supplied mapping gives deterministic behavior in tests and applications.

`resolve` accepts only `EnvironmentCredentialRef`. Unsupported kinds and missing variables raise `CredentialError`.

```python
from ovid_core.credentials.models import EnvironmentCredentialRef
from ovid_core.credentials.resolvers import EnvironmentCredentialResolver

resolver = EnvironmentCredentialResolver({'TOKEN': 'secret'})
secret = await resolver.resolve(EnvironmentCredentialRef(variable='TOKEN'))
```
