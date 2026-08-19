from collections.abc import Mapping

import pytest
from pydantic import JsonValue, ValidationError

from ovid_core.config import ConfigMigration, OvidConfig, migrate_config
from ovid_core.errors import ConfigurationError


def test_final_config_validates_without_application_policy() -> None:
    config = OvidConfig.model_validate(
        {
            'credentials': {'main': {'kind': 'environment', 'variable': 'OPENAI_API_KEY'}},
            'models': {'chat': {'provider': 'openai', 'model': 'gpt-4o'}},
            'routes': {'answer': {'models': ['chat']}},
            'run_policy': {'request_limit': 4},
            'plugins': {'audit': {'config': {'level': 'strict'}}},
        }
    )

    assert config.schema_version == 1
    assert config.models['chat'].model == 'gpt-4o'
    assert config.routes['answer'].models == ('chat',)
    assert config.run_policy.request_limit == 4
    assert config.plugins['audit'].config == {'level': 'strict'}


def test_final_config_rejects_unknown_keys_with_structured_paths() -> None:
    with pytest.raises(ValidationError) as captured:
        OvidConfig.model_validate(
            {'models': {'primary': {'provider': 'openai', 'model': 'gpt-4o', 'api_key': 'secret-value'}}}
        )

    errors = captured.value.errors(include_url=False, include_context=False, include_input=False)

    assert errors[0]['loc'] == ('models', 'primary', 'api_key')
    assert errors[0]['type'] == 'extra_forbidden'


def test_final_config_rejects_unsupported_schema_version() -> None:
    with pytest.raises(ValidationError) as captured:
        OvidConfig.model_validate({'schema_version': 2})

    errors = captured.value.errors(include_url=False, include_context=False, include_input=False)

    assert errors[0]['loc'] == ('schema_version',)
    assert errors[0]['type'] == 'literal_error'


def test_migrations_are_explicit_incremental_and_do_not_mutate_inputs() -> None:
    original: dict[str, JsonValue] = {'schema_version': 0, 'legacy_model': 'openai:gpt-4o'}

    def migrate_zero(data: dict[str, JsonValue]) -> Mapping[str, JsonValue]:
        provider, model = str(data.pop('legacy_model')).split(':', maxsplit=1)
        data['schema_version'] = 1
        data['models'] = {'primary': {'provider': provider, 'model': model}}
        return data

    migrated = migrate_config(original, migrations={0: migrate_zero})
    config = OvidConfig.model_validate(migrated)

    assert original == {'schema_version': 0, 'legacy_model': 'openai:gpt-4o'}
    assert migrated['schema_version'] == 1
    assert config.models['primary'].provider == 'openai'
    assert config.models['primary'].model == 'gpt-4o'
    assert migrate_config({}) == {}


@pytest.mark.parametrize(
    ('data', 'migrations', 'message'),
    [
        ({'schema_version': 'one'}, None, 'must be an integer'),
        ({'schema_version': True}, None, 'must be an integer'),
        ({'schema_version': 2}, None, 'unsupported schema version'),
        ({'schema_version': 0}, None, 'no migration is registered'),
        ({'schema_version': 0}, {0: lambda data: data}, 'did not produce version 1'),
    ],
)
def test_invalid_schema_migrations(
    data: Mapping[str, JsonValue],
    migrations: Mapping[int, ConfigMigration] | None,
    message: str,
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        migrate_config(data, migrations=migrations)
