import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path

from pydantic import JsonValue, TypeAdapter, ValidationError

from ovid_core.config.errors import ConfigIssue, ConfigValidationError
from ovid_core.config.models import OvidConfig
from ovid_core.errors import ConfigurationError


CURRENT_CONFIG_SCHEMA_VERSION = 1
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])

type ConfigMigration = Callable[[dict[str, JsonValue]], Mapping[str, JsonValue]]


def validate_config(
    data: Mapping[str, JsonValue],
    *,
    source_file: Path | None = None,
    migrations: Mapping[int, ConfigMigration] | None = None,
) -> OvidConfig:
    try:
        migrated = migrate_config(data, migrations=migrations)
        return OvidConfig.model_validate(migrated)
    except ValidationError as error:
        issues = tuple(
            ConfigIssue(path=tuple(item['loc']), message=item['msg'], source_file=source_file)
            for item in error.errors(include_url=False, include_context=False, include_input=False)
        )
        raise ConfigValidationError(issues) from None
    except ConfigurationError as error:
        issue = ConfigIssue(path=('schema_version',), message=str(error), source_file=source_file)
        raise ConfigValidationError((issue,)) from None


def migrate_config(
    data: Mapping[str, JsonValue],
    *,
    migrations: Mapping[int, ConfigMigration] | None = None,
) -> dict[str, JsonValue]:
    migrated = dict(data)
    version = migrated.get('schema_version', CURRENT_CONFIG_SCHEMA_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigurationError('schema version must be an integer')
    if version > CURRENT_CONFIG_SCHEMA_VERSION:
        raise ConfigurationError(f'unsupported schema version {version}')

    available = {} if migrations is None else migrations
    while version < CURRENT_CONFIG_SCHEMA_VERSION:
        migration = available.get(version)
        if migration is None:
            raise ConfigurationError(f'no migration is registered for schema version {version}')
        migrated = dict(migration(dict(migrated)))
        next_version = migrated.get('schema_version')
        if next_version != version + 1:
            raise ConfigurationError(f'migration for schema version {version} did not produce version {version + 1}')
        version += 1

    return migrated


def load_config_file(
    path: Path,
    *,
    migrations: Mapping[int, ConfigMigration] | None = None,
) -> OvidConfig:
    expanded_path = path.expanduser()
    try:
        content = expanded_path.read_bytes()
        if expanded_path.suffix.casefold() == '.json':
            data = _JSON_OBJECT_ADAPTER.validate_json(content)
        elif expanded_path.suffix.casefold() == '.toml':
            data = _JSON_OBJECT_ADAPTER.validate_python(tomllib.loads(content.decode()))
        else:
            raise ConfigurationError('configuration files must use .json or .toml')
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValidationError, ConfigurationError) as error:
        issue = ConfigIssue(path=(), message=str(error), source_file=expanded_path)
        raise ConfigValidationError((issue,)) from None

    return validate_config(data, source_file=expanded_path, migrations=migrations)
