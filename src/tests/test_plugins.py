import pytest

from ovid_core.capabilities.base import BaseCapability
from ovid_core.errors import PluginError
from ovid_core.models import BaseModel
from ovid_core.plugins import PluginActivationContext, PluginRegistrar
from ovid_core.services import (
    AgentServiceBinding,
    AgentServiceKey,
    AgentServiceRef,
    AgentServiceRequirement,
    AgentServices,
)


class PluginConfiguration(BaseModel):
    value: str


def create_service(
    *,
    context: PluginActivationContext,
    config: dict[str, str],
) -> AgentServiceBinding[str]:
    del context, config
    return AgentServiceBinding[str](
        ref=AgentServiceRef(
            key=AgentServiceKey(id='test.service', api_version=1, value_type=str),
        ),
        value='service',
        provider='test.provider',
    )


def configure_service(
    binding: AgentServiceBinding[str],
    *,
    context: PluginActivationContext,
    config: dict[str, str],
) -> AgentServiceBinding[str]:
    del context, config
    return binding


def create_capability(*, context: PluginActivationContext) -> BaseCapability[None]:
    configuration = context.configuration
    assert isinstance(configuration, PluginConfiguration)
    return BaseCapability(id=f'test.capability.{configuration.value}')


def test_plugin_registrar_collects_and_selects_explicit_factories() -> None:
    registrar = PluginRegistrar()
    requirement = AgentServiceRequirement(
        service_id='test.service',
        api_version=1,
    )
    registrar.register_service_provider_factory(id='test.provider', factory=create_service)
    registrar.register_service_configurator_factory(
        id='test.configure',
        provider_id='test.provider',
        factory=configure_service,
    )
    registrar.register_capability_factory(
        id='test.capability',
        requirements=(requirement,),
        factory=create_capability,
    )
    assert registrar.service_factories.providers[0].id == 'test.provider'
    assert registrar.service_factories.configurators[0].id == 'test.configure'
    assert registrar.capability_factories[0].id == 'test.capability'
    services = registrar.select_service_factories(
        providers=('test.provider',),
        configurators=('test.configure',),
    )
    capability = registrar.select_capability_factories(('test.capability',))[0]
    context = PluginActivationContext(
        services=AgentServices(),
        configuration=PluginConfiguration(value='configured'),
    )
    binding = services.providers[0].create(context=context, config={})

    assert services.configurators[0].configure(binding, context=context, config={}) is binding
    assert capability.requirements == (requirement,)
    assert capability.create(context=context).id == 'test.capability.configured'


def test_plugin_registrar_rejects_duplicate_empty_and_unknown_ids() -> None:
    registrar = PluginRegistrar()
    registrar.register_service_provider_factory(id='test.provider', factory=create_service)

    with pytest.raises(PluginError, match='Duplicate or empty service provider ID'):
        registrar.register_service_provider_factory(id='test.provider', factory=create_service)
    with pytest.raises(PluginError, match='Duplicate or empty capability ID'):
        registrar.register_capability_factory(id='', requirements=(), factory=create_capability)
    with pytest.raises(PluginError, match='Unknown selected service provider ID'):
        registrar.select_service_factories(providers=('missing',))
    with pytest.raises(PluginError, match='Duplicate selected service provider ID'):
        registrar.select_service_factories(providers=('test.provider', 'test.provider'))
