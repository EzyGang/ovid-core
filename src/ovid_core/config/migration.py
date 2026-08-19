from collections.abc import Callable, Mapping

from pydantic import JsonValue

from ovid_core.errors import ConfigurationError


CURRENT_CONFIG_SCHEMA_VERSION = 1

type ConfigMigration = Callable[[dict[str, JsonValue]], Mapping[str, JsonValue]]


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
