from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import JsonValue

from ovid_core.config import ConfigMigration, ConfigValidationError, load_config, load_config_file, migrate_config


def test_final_config_validates_without_application_policy() -> None:
    config = load_config(
        {
            'credentials': {'main': {'kind': 'environment', 'variable': 'OPENAI_API_KEY'}},
            'models': {'chat': {'provider': 'openai', 'model': 'gpt-4o'}},
            'routes': {'answer': {'models': ['chat']}},
            'run_policy': {'request_limit': 4},
            'plugins': {'audit': {'config': {'level': 'strict'}}},
        }
    )

    assert config.models['chat'].model == 'gpt-4o'
    assert config.routes['answer'].models == ('chat',)
    assert config.run_policy.request_limit == 4
    assert config.plugins['audit'].config == {'level': 'strict'}


def test_unknown_keys_report_paths_without_input_values() -> None:
    with pytest.raises(ConfigValidationError) as captured:
        load_config(
            {'models': {'primary': {'provider': 'openai', 'model': 'gpt-4o', 'api_key': 'secret-value'}}},
            source_file=Path('config.json'),
        )

    error = captured.value
    assert error.issues[0].path == ('models', 'primary', 'api_key')
    assert error.issues[0].source_file == Path('config.json')
    assert 'secret-value' not in str(error)
    assert 'secret-value' not in repr(error)


def test_migrations_are_explicit_incremental_and_do_not_mutate_inputs() -> None:
    original: dict[str, JsonValue] = {'schema_version': 0, 'legacy_model': 'openai:gpt-4o'}

    def migrate_zero(data: dict[str, JsonValue]) -> Mapping[str, JsonValue]:
        provider, model = str(data.pop('legacy_model')).split(':', maxsplit=1)
        data['schema_version'] = 1
        data['models'] = {'primary': {'provider': provider, 'model': model}}
        return data

    migrations = {0: migrate_zero}
    migrated = migrate_config(original, migrations=migrations)
    config = load_config(original, migrations=migrations)

    assert original == {'schema_version': 0, 'legacy_model': 'openai:gpt-4o'}
    assert migrated['schema_version'] == 1
    assert config.models['primary'].provider == 'openai'
    assert config.models['primary'].model == 'gpt-4o'


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
def test_invalid_schema_versions(
    data: Mapping[str, JsonValue],
    migrations: Mapping[int, ConfigMigration] | None,
    message: str,
) -> None:
    with pytest.raises(ConfigValidationError, match=message) as captured:
        load_config(data, source_file=Path('config.toml'), migrations=migrations)

    assert captured.value.issues[0].path == ('schema_version',)
    assert captured.value.issues[0].source_file == Path('config.toml')


def test_load_config_accepts_parsed_and_serialized_remote_sources() -> None:
    parsed = load_config({'models': {'parsed': {'provider': 'test', 'model': 'test'}}})
    json_config = load_config(b'{"models":{"json":{"provider":"test","model":"test"}}}')
    toml_config = load_config('[models.toml]\nprovider = "test"\nmodel = "test"\n')
    explicit_json = load_config(
        '{"models":{"explicit":{"provider":"test","model":"test"}}}',
        config_format='json',
    )

    assert parsed.models['parsed'].model == 'test'
    assert json_config.models['json'].model == 'test'
    assert toml_config.models['toml'].model == 'test'
    assert explicit_json.models['explicit'].model == 'test'

    with pytest.raises(ConfigValidationError, match='configuration format must be json or toml'):
        load_config('', config_format='yaml')

    with pytest.raises(ConfigValidationError) as captured:
        load_config('{"api_key":"remote-secret",invalid}')

    assert 'remote-secret' not in str(captured.value)
    assert 'remote-secret' not in repr(captured.value)


def test_json_and_toml_files_load_with_safe_errors(tmp_path: Path) -> None:
    json_path = tmp_path / 'config.json'
    json_path.write_text('{"models":{"json":{"provider":"test","model":"test"}}}')
    toml_path = tmp_path / 'config.toml'
    toml_path.write_text(
        '[models.toml]\n'
        'provider = "test"\n'
        'model = "test"\n'
        '\n'
        '[[mcp_servers]]\n'
        'id = "docs"\n'
        '\n'
        '[mcp_servers.transport]\n'
        'kind = "http"\n'
        'url = "https://mcp.example.com"\n'
    )
    invalid_path = tmp_path / 'invalid.json'
    invalid_path.write_text('{invalid')
    unsupported_path = tmp_path / 'config.yaml'
    unsupported_path.write_text('models: {}')

    assert load_config_file(json_path).models['json'].model == 'test'
    toml_config = load_config_file(toml_path)
    assert toml_config.models['toml'].model == 'test'
    assert toml_config.mcp_servers[0].id == 'docs'
    for path in (invalid_path, unsupported_path, tmp_path / 'missing.json'):
        with pytest.raises(ConfigValidationError) as captured:
            load_config_file(path)
        assert captured.value.issues[0].source_file == path
