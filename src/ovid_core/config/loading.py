import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from pydantic import JsonValue, TypeAdapter, ValidationError

from ovid_core.config.errors import ConfigIssue, ConfigValidationError
from ovid_core.config.models import OvidConfig
from ovid_core.errors import ConfigurationError


CURRENT_CONFIG_SCHEMA_VERSION = 1
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])

type ConfigMigration = Callable[[dict[str, JsonValue]], Mapping[str, JsonValue]]
type ConfigFormat = Literal['json', 'toml']
type ConfigSource = Mapping[str, JsonValue] | str | bytes


def load_config(
    source: ConfigSource,
    *,
    config_format: ConfigFormat | None = None,
    source_file: Path | None = None,
    migrations: Mapping[int, ConfigMigration] | None = None,
) -> OvidConfig:
    if not isinstance(source, (str, bytes)):
        data = source
    else:
        try:
            data = _parse_config(source, config_format=config_format)
        except ValidationError as error:
            detail = error.errors(include_url=False, include_context=False, include_input=False)[0]
            issue = ConfigIssue(path=(), message=detail['msg'], source_file=source_file)
            raise ConfigValidationError((issue,)) from None
        except (UnicodeError, tomllib.TOMLDecodeError, ConfigurationError) as error:
            issue = ConfigIssue(path=(), message=str(error), source_file=source_file)
            raise ConfigValidationError((issue,)) from None

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


def _parse_config(content: str | bytes, *, config_format: ConfigFormat | None) -> dict[str, JsonValue]:
    selected_format = config_format or _detect_config_format(content)

    if selected_format == 'json':
        return _JSON_OBJECT_ADAPTER.validate_json(content)
    if selected_format == 'toml':
        text = content.decode() if isinstance(content, bytes) else content

        return _JSON_OBJECT_ADAPTER.validate_python(tomllib.loads(text))

    raise ConfigurationError('configuration format must be json or toml')


def _detect_config_format(content: str | bytes) -> ConfigFormat:
    if isinstance(content, bytes):
        return 'json' if content.lstrip().startswith(b'{') else 'toml'

    return 'json' if content.lstrip().startswith('{') else 'toml'


def load_config_file(
    path: Path,
    *,
    migrations: Mapping[int, ConfigMigration] | None = None,
) -> OvidConfig:
    expanded_path = path.expanduser()
    suffix = expanded_path.suffix.casefold()
    if suffix not in {'.json', '.toml'}:
        issue = ConfigIssue(path=(), message='configuration files must use .json or .toml', source_file=expanded_path)
        raise ConfigValidationError((issue,))

    try:
        content = expanded_path.read_bytes()
    except OSError as error:
        issue = ConfigIssue(path=(), message=str(error), source_file=expanded_path)
        raise ConfigValidationError((issue,)) from None

    return load_config(
        content,
        config_format='json' if suffix == '.json' else 'toml',
        source_file=expanded_path,
        migrations=migrations,
    )
