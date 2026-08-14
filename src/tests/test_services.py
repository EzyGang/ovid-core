import pytest
from pydantic import ValidationError

from ovid_core.services import (
    AgentServiceBinding,
    AgentServiceCollisionError,
    AgentServiceCompatibilityError,
    AgentServiceKey,
    AgentServiceMissingError,
    AgentServiceRef,
    AgentServiceRequirement,
    AgentServices,
)


class ServiceValue:
    pass


def service_key(*, value_type: type[ServiceValue] | None = ServiceValue) -> AgentServiceKey[ServiceValue]:
    return AgentServiceKey(id='tests.workspace', api_version=1, value_type=value_type)


def service_binding(*, features: frozenset[str] = frozenset({'read'})) -> AgentServiceBinding[ServiceValue]:
    return AgentServiceBinding(
        ref=AgentServiceRef(key=service_key()),
        value=ServiceValue(),
        provider='tests.ServiceValue',
        features=features,
        identity='opaque',
    )


def test_service_key_identity_excludes_runtime_type() -> None:
    typed = service_key()
    untyped = service_key(value_type=None)

    assert typed == untyped
    assert hash(typed) == hash(untyped)
    assert len({typed, untyped}) == 1


@pytest.mark.parametrize(
    ('arguments', 'message'),
    [
        ({'id': '', 'api_version': 1}, 'globally namespaced'),
        ({'id': 'workspace', 'api_version': 1}, 'globally namespaced'),
        ({'id': 'tests.workspace', 'api_version': 0}, 'positive'),
    ],
)
def test_service_key_rejects_invalid_identity(arguments: dict[str, str | int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        AgentServiceKey(**arguments)


@pytest.mark.parametrize('name', ['', 'two words', 'two-workspaces'])
def test_service_reference_rejects_invalid_names(name: str) -> None:
    with pytest.raises(ValueError, match='identifiers'):
        AgentServiceRef(key=service_key(), name=name)


def test_service_binding_validates_safe_metadata() -> None:
    reference = AgentServiceRef(key=service_key())

    with pytest.raises(ValueError, match='providers'):
        AgentServiceBinding(ref=reference, value=ServiceValue(), provider='')
    with pytest.raises(ValueError, match='features'):
        AgentServiceBinding(ref=reference, value=ServiceValue(), provider='tests', features=frozenset({''}))


@pytest.mark.parametrize(
    'value',
    [
        {'service_id': 'workspace', 'api_version': 1},
        {'service_id': 'tests.workspace', 'api_version': 0},
        {'service_id': 'tests.workspace', 'api_version': 1, 'name': 'two words'},
        {'service_id': 'tests.workspace', 'api_version': 1, 'required_features': ['']},
    ],
)
def test_service_requirement_rejects_invalid_values(value: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AgentServiceRequirement.model_validate(value)


def test_registry_retains_values_and_resolves_named_bindings() -> None:
    binding = service_binding()
    services = AgentServices((binding,))

    assert services.bindings == (binding,)
    assert services.contains(binding.ref)
    assert services.binding(binding.ref) is binding
    assert services.resolve(binding.ref) is binding.value


def test_registry_rejects_duplicate_and_incompatible_bindings() -> None:
    binding = service_binding()

    with pytest.raises(AgentServiceCollisionError, match='tests.workspace@1:default'):
        AgentServices((binding, binding))

    incompatible = AgentServiceBinding(ref=binding.ref, value='wrong', provider='tests')
    with pytest.raises(AgentServiceCompatibilityError, match='ServiceValue'):
        AgentServices((incompatible,))


def test_registry_reports_missing_services_and_features_safely() -> None:
    binding = service_binding(features=frozenset({'search'}))
    services = AgentServices((binding,))
    missing_ref = AgentServiceRef(key=AgentServiceKey(id='tests.other', api_version=1))

    assert not services.contains(missing_ref)
    with pytest.raises(AgentServiceMissingError, match='tests.other@1:default'):
        services.resolve(missing_ref)

    missing_requirement = AgentServiceRequirement(service_id='tests.other', api_version=1)
    with pytest.raises(AgentServiceMissingError, match="Capability 'consumer'.*tests.other@1:default"):
        services.validate_requirement(missing_requirement, consumer='consumer')

    feature_requirement = AgentServiceRequirement(
        service_id='tests.workspace',
        api_version=1,
        required_features=frozenset({'ast', 'search'}),
    )
    with pytest.raises(AgentServiceCompatibilityError, match=r'\[ast, search\].*\[search\]'):
        services.validate_requirement(feature_requirement, consumer='consumer')


def test_registry_accepts_satisfied_requirements() -> None:
    services = AgentServices((service_binding(features=frozenset({'ast', 'search'})),))
    requirement = AgentServiceRequirement(
        service_id='tests.workspace',
        api_version=1,
        required_features=frozenset({'ast'}),
    )

    services.validate_requirement(requirement, consumer='consumer')
