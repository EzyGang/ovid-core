import pytest

from ovid_core.services import (
    AgentServiceBinding,
    AgentServiceCollisionError,
    AgentServiceCompatibilityError,
    AgentServiceKey,
    AgentServiceNotFoundError,
    AgentServiceRef,
    AgentServiceRequirement,
    AgentServices,
)


def key(*, api_version: int = 1, value_type: type[str] | None = str) -> AgentServiceKey[str]:
    return AgentServiceKey(id='test.service', api_version=api_version, value_type=value_type)


def binding(*, api_version: int = 1, name: str = 'default') -> AgentServiceBinding[str]:
    return AgentServiceBinding(
        ref=AgentServiceRef(key=key(api_version=api_version), name=name),
        value='service',
        provider='test-provider',
        features=frozenset(('read', 'write')),
        identity='shared-service',
    )


def test_service_key_reference_binding_and_requirement_validation() -> None:
    for service_id in ('service', '.service', 'service.'):
        with pytest.raises(ValueError, match='namespaced'):
            AgentServiceKey(id=service_id, api_version=1, value_type=str)
    with pytest.raises(ValueError, match='positive'):
        key(api_version=0)
    with pytest.raises(ValueError, match='non-empty and trimmed'):
        AgentServiceRef(key=key(), name=' invalid ')
    with pytest.raises(ValueError, match='non-empty and trimmed'):
        AgentServiceRef(key=key(), name='')
    with pytest.raises(TypeError, match='does not implement str'):
        AgentServiceBinding(ref=AgentServiceRef(key=key()), value=1, provider='test')
    with pytest.raises(ValueError, match='provider'):
        AgentServiceBinding(ref=AgentServiceRef(key=key()), value='service', provider='')
    with pytest.raises(ValueError, match='features'):
        AgentServiceBinding(
            ref=AgentServiceRef(key=key()),
            value='service',
            provider='test',
            features=frozenset(('',)),
        )
    AgentServiceBinding(
        ref=AgentServiceRef(key=key(value_type=None)),
        value='untyped',
        provider='test',
    )

    for service_id, name in (('', 'default'), ('test.service', '')):
        with pytest.raises(ValueError, match='ID and name'):
            AgentServiceRequirement(service_id=service_id, name=name)
    with pytest.raises(ValueError, match='positive'):
        AgentServiceRequirement(service_id='test.service', api_version=0)


def test_service_registry_resolves_typed_versioned_bindings() -> None:
    configured = binding(api_version=2)
    services = AgentServices((configured,))

    assert services.bindings == (configured,)
    assert services.resolve(AgentServiceRef(key=key(api_version=1))) == 'service'
    with pytest.raises(AgentServiceNotFoundError, match='not configured'):
        services.resolve(AgentServiceRef(key=key(), name='missing'))
    with pytest.raises(AgentServiceCompatibilityError, match='requires version 3'):
        services.resolve(AgentServiceRef(key=key(api_version=3)))

    untyped_key: AgentServiceKey[object] = AgentServiceKey(
        id='test.untyped',
        api_version=1,
        value_type=None,
    )
    untyped = AgentServiceBinding(
        ref=AgentServiceRef(key=untyped_key),
        value='value',
        provider='test',
    )
    incompatible_ref = AgentServiceRef(key=AgentServiceKey(id='test.untyped', api_version=1, value_type=int))
    with pytest.raises(AgentServiceCompatibilityError, match='value type'):
        AgentServices((untyped,)).resolve(incompatible_ref)

    with pytest.raises(AgentServiceCollisionError, match='Duplicate'):
        AgentServices((configured, configured))


def test_service_requirements_validate_features_and_track_consumers_once() -> None:
    configured = binding()
    services = AgentServices((configured,))
    valid = AgentServiceRequirement(
        service_id='test.service',
        required_features=frozenset(('read',)),
    )

    services.validate((valid,), consumer='first')
    services.validate((valid,), consumer='first')
    services.validate((valid,), consumer='second')
    assert services.consumers(configured) == ('first', 'second')

    unknown = AgentServiceRequirement(service_id='test.missing')
    with pytest.raises(AgentServiceNotFoundError, match="required by 'consumer'"):
        services.validate((unknown,), consumer='consumer')
    incompatible = AgentServiceRequirement(service_id='test.service', api_version=2)
    with pytest.raises(AgentServiceCompatibilityError, match='API version'):
        services.validate((incompatible,), consumer='consumer')
    missing_feature = AgentServiceRequirement(
        service_id='test.service',
        required_features=frozenset(('execute', 'delete')),
    )
    with pytest.raises(AgentServiceCompatibilityError, match='delete, execute'):
        services.validate((missing_feature,), consumer='consumer')

    other = binding(name='other')
    assert services.consumers(other) == ()
