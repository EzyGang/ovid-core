import os
from pathlib import Path

import pytest
from pydantic import SecretStr, TypeAdapter, ValidationError

from ovid_core.credentials.models import (
    CallbackCredentialRef,
    CredentialRef,
    EnvironmentCredentialRef,
    FileCredentialRef,
    NamedCredentialRef,
    StoreCredentialRef,
)
from ovid_core.credentials.resolvers import CredentialResolver, EnvironmentCredentialResolver
from ovid_core.errors import CredentialError


_CREDENTIAL_ADAPTER = TypeAdapter(CredentialRef)
_SECRET_ADAPTER = TypeAdapter(SecretStr)


def test_all_credential_references_are_typed_and_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    references = (
        EnvironmentCredentialRef(variable='OVID_TOKEN'),
        NamedCredentialRef(name='shared'),
        FileCredentialRef(path='~/token'),
        CallbackCredentialRef(callback='refresh-token'),
        StoreCredentialRef(store='system', name='account'),
    )

    restored = tuple(_CREDENTIAL_ADAPTER.validate_json(_CREDENTIAL_ADAPTER.dump_json(item)) for item in references)

    assert restored == references
    assert references[2].path == tmp_path / 'token'
    assert all('secret-value' not in repr(item) for item in references)
    with pytest.raises(ValidationError):
        _CREDENTIAL_ADAPTER.validate_python({'kind': 'unknown', 'name': 'value'})


@pytest.mark.asyncio
async def test_environment_resolver_returns_secret_without_disclosing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(os.environ, 'OVID_TEST_TOKEN', 'secret-value')
    resolver: CredentialResolver = EnvironmentCredentialResolver()

    secret = await resolver.resolve(EnvironmentCredentialRef(variable='OVID_TEST_TOKEN'))

    assert secret.get_secret_value() == 'secret-value'
    assert 'secret-value' not in str(secret)
    assert 'secret-value' not in repr(secret)
    assert b'secret-value' not in _SECRET_ADAPTER.dump_json(secret)


@pytest.mark.asyncio
async def test_environment_resolver_reports_missing_and_unsupported_references() -> None:
    resolver = EnvironmentCredentialResolver({})

    with pytest.raises(CredentialError, match='is not set') as missing:
        await resolver.resolve(EnvironmentCredentialRef(variable='MISSING_TOKEN'))
    with pytest.raises(CredentialError, match='does not support') as unsupported:
        await resolver.resolve(NamedCredentialRef(name='shared'))

    assert 'secret-value' not in repr(missing.value)
    assert 'secret-value' not in repr(unsupported.value)
