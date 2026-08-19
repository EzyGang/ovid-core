# Models and configuration

## Shared model bases

Import from `ovid_core.models`.

| Symbol | Contract |
| --- | --- |
| `BaseModel` | Pydantic model base with `extra='forbid'` and `frozen=True`. All structured Ovid DTOs inherit it. |
| `BaseRootModel[Root]` | Frozen Pydantic `RootModel` base used for scalar domain values such as run and conversation IDs. |

## Configuration models

Import from `ovid_core.config`.

### `ConfigName`

`Annotated[str, StringConstraints(min_length=1)]`. It is the key type for named model, route, credential, and plugin mappings.

### `ModelConfig`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `provider` | `str` | required | Non-empty Pydantic AI provider name. |
| `model` | `str` | required | Non-empty provider model name. |
| `aliases` | `tuple[str, ...]` | `()` | Additional names accepted by `ModelRouter`. |
| `concurrency_limit` | `int \| None` | `None` | Per-model request concurrency. The minimum value is 1. |
| `settings` | `dict[str, JsonValue]` | `{}` | Model settings that the adapter sends to Pydantic AI. |

Provider and model remain separate in configuration. `DefaultModelFactory` joins them only during model construction.

### `RouteConfig`

`models: tuple[str, ...]` is a non-empty ordered list of configured model names or aliases. The order is the fallback order.

### `RunPolicyConfig`

A domain-neutral configuration section for final Ovid configuration.

| Field | Type | Default |
| --- | --- | --- |
| `request_limit` | `int \| None` (at least 1) | `None` |
| `tool_call_limit` | `int \| None` (at least 1) | `None` |
| `timeout_seconds` | positive `float \| None` | `None` |

This configuration value is distinct from the richer runtime `AgentRunPolicy` used by `AgentDefinition`.

### `PluginConfig`

| Field | Type | Default |
| --- | --- | --- |
| `enabled` | `bool` | `True` |
| `config` | `dict[str, JsonValue]` | `{}` |

Ovid Core validates and carries plugin configuration. It does not discover or load plugins.

### `OvidConfig`

The final configuration accepted by core.

| Field | Type | Default |
| --- | --- | --- |
| `schema_version` | `Literal[1]` | `1` |
| `models` | `dict[ConfigName, ModelConfig]` | `{}` |
| `routes` | `dict[ConfigName, RouteConfig]` | `{}` |
| `credentials` | `dict[ConfigName, CredentialRef]` | `{}` |
| `mcp_servers` | `tuple[MCPServerConfig, ...]` | `()` |
| `run_policy` | `RunPolicyConfig` | `RunPolicyConfig()` |
| `plugins` | `dict[ConfigName, PluginConfig]` | `{}` |

Core accepts one final `OvidConfig`. The consuming application owns:

- file and remote-source I/O
- TOML, JSON, YAML, or other parsing
- environment mapping
- merging and source precedence
- profile selection
- source provenance and error presentation.

Validate the application-produced mapping with Pydantic:

```python
from ovid_core.config import OvidConfig

config_data = load_application_config()
config = OvidConfig.model_validate(config_data)
```

An application can embed the contract in its own settings model:

```python
from pydantic import BaseModel

from ovid_core.config import OvidConfig


class ApplicationSettings(BaseModel):
    ovid: OvidConfig
    database_url: str
```

Parse and merge all application sources before model validation. Pass `settings.ovid` to `AgentFactory`.

## Schema migration

Import from `ovid_core.config`.

- `CURRENT_CONFIG_SCHEMA_VERSION = 1`
- `ConfigMigration = Callable[[dict[str, JsonValue]], Mapping[str, JsonValue]]`

A migration receives its own mutable copy. It must return data whose `schema_version` is exactly one greater than the input version.

```text
def migrate_config(
    data: Mapping[str, JsonValue],
    *,
    migrations: Mapping[int, ConfigMigration] | None = None,
) -> dict[str, JsonValue]
```

Call `migrate_config` after application parsing and before `OvidConfig.model_validate`:

```python
from ovid_core.config import OvidConfig, migrate_config

parsed = load_application_config()
migrated = migrate_config(parsed, migrations=application_migrations)
config = OvidConfig.model_validate(migrated)
```

The function copies the source mapping before migration. It raises `ConfigurationError` for:

- a non-integer schema version
- a future schema version
- a missing migration step
- a migration that does not advance exactly one version.

Applications provide migrations explicitly. Core does not discover migration functions or configuration sources.

## Validation errors

`OvidConfig.model_validate` raises Pydantic `ValidationError` for invalid final data. The application owns source labels and user-facing error envelopes.

Pydantic structured errors include rejected input unless the caller excludes it. Omit input and context before logging untrusted configuration:

```python
issues = error.errors(
    include_url=False,
    include_context=False,
    include_input=False,
)
```
