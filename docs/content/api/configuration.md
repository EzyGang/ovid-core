# Models and configuration

## Shared model bases

Import from `ovid_core.models`.

| Symbol | Contract |
| --- | --- |
| `BaseModel` | Pydantic model base with `extra='forbid'` and `frozen=True`. All structured Ovid DTOs inherit it. |
| `BaseRootModel[Root]` | Frozen Pydantic `RootModel` base used for scalar domain values such as run and conversation IDs. |

## Configuration models

Import from `ovid_core.config.models`.

### `ConfigName`

`Annotated[str, StringConstraints(min_length=1)]`. Used as the key type for named model, route, credential, and plugin mappings.

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

A domain-neutral configuration section for application configuration files.

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

Ovid Core validates and carries plugin configuration but does not discover or load plugins.

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

Core accepts one final `OvidConfig`. File discovery, environment mapping, merging, profile selection, and source precedence belong to the consuming application.

Use TOML for the standard application configuration file:

```toml
[models.primary]
provider = "openai"
model = "gpt-5"

[[mcp_servers]]
id = "project-tools"

[mcp_servers.transport]
kind = "http"
url = "https://mcp.example.com"
```

JSON remains supported for applications that already use JSON configuration.

## Validation and loading

Import from `ovid_core.config.loading`.

### Constants and aliases

- `CURRENT_CONFIG_SCHEMA_VERSION = 1`
- `ConfigFormat = Literal['json', 'toml']`
- `ConfigSource = Mapping[str, JsonValue] | str | bytes`
- `ConfigMigration = Callable[[dict[str, JsonValue]], Mapping[str, JsonValue]]`

A migration receives its own mutable copy and must return data whose `schema_version` is exactly one greater than the input version.

### `load_config`

```text
def load_config(
    source: ConfigSource,
    *,
    config_format: ConfigFormat | None = None,
    source_file: Path | None = None,
    migrations: Mapping[int, ConfigMigration] | None = None,
) -> OvidConfig
```

Loads an already parsed mapping or serialized TOML/JSON held in `str` or `bytes`. This is the source-independent entry point for HTTP responses, object stores, databases, package resources, and virtual file systems.

Serialized input that starts with a JSON object delimiter uses JSON.
All other serialized input uses TOML.
Pass `config_format` when the source format must be explicit.

```python
content = await get_remote('configs/ovid.toml')
config = load_config(content, config_format='toml')
```

The function runs explicit migrations and validates `OvidConfig`. Parse, validation, and migration failures become source-safe `ConfigValidationError` values.

`source_file` is optional issue metadata. Core never opens it.

### `migrate_config`

```text
def migrate_config(
    data: Mapping[str, JsonValue],
    *,
    migrations: Mapping[int, ConfigMigration] | None = None,
) -> dict[str, JsonValue]
```

Copies the source mapping before migration. A future schema version, a non-integer version, a missing migration, or a migration that does not advance exactly one version raises `ConfigurationError`.

### `load_config_file`

```text
def load_config_file(
    path: Path,
    *,
    migrations: Mapping[int, ConfigMigration] | None = None,
) -> OvidConfig
```

Expands `~`, selects TOML or JSON from the file suffix, reads the file, and delegates to `load_config`.

Unsupported suffixes, I/O failures, parse errors, and validation errors become source-safe `ConfigValidationError` values.

## Validation errors

Import from `ovid_core.config.errors`.

- `ConfigPath = tuple[str | int, ...]`
- `ConfigIssue(path, message, source_file=None)` stores one source-safe issue.
- `str(issue)` shows the dotted path, optional file, and message. The root path appears as `<root>`.
- `ConfigValidationError(issues)` extends `ConfigurationError`. Its `issues` attribute contains the ordered issues.
