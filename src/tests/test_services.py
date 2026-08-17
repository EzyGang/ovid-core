from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Self, cast

import pytest
from pydantic import JsonValue, ValidationError
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from ovid_core import AgentDefinition
from ovid_core.capabilities import BaseCapability, CapabilityContributions
from ovid_core.errors import PluginError
from ovid_core.plugins import PluginActivationContext, PluginServiceFactories
from ovid_core.routing import ModelRouteRef
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
from tests.support.agent_helpers import agent_factory, failing_request
from tests.tools.tool_consumer import TrackingToolset


@dataclass(slots=True)
class ServiceValue:
    marker: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BindingCapability(BaseCapability[None]):
    tracker: list[str] = field(compare=False)
    id: str = field(default='binding', init=False)
    requirements: tuple[AgentServiceRequirement, ...] = (
        AgentServiceRequirement(
            service_id='test.workspace',
            api_version=1,
            required_features=frozenset(('search',)),
        ),
    )

    def bind(self, services: AgentServices) -> Self:
        super().bind(services)
        self.tracker.append('bound')
        return replace(self, contributions=CapabilityContributions(toolsets=(TrackingToolset(()),)))


@dataclass(frozen=True, slots=True)
class ProviderFactory:
    id: str

    def create(
        self,
        *,
        context: PluginActivationContext,
        config: dict[str, JsonValue],
    ) -> AgentServiceBinding[object]:
        del context, config
        return AgentServiceBinding(
            ref=AgentServiceRef(key=AgentServiceKey(id='test.plugin', api_version=1)),
            value=object(),
            provider=self.id,
        )


@dataclass(frozen=True, slots=True)
class ConfiguratorFactory:
    id: str
    provider_id: str

    def configure(
        self,
        binding: AgentServiceBinding[object],
        *,
        context: PluginActivationContext,
        config: dict[str, JsonValue],
    ) -> AgentServiceBinding[object]:
        del context, config
        return binding


def binding(
    *,
    value: ServiceValue | None = None,
    features: frozenset[str] | None = None,
) -> AgentServiceBinding[ServiceValue]:
    key = AgentServiceKey(id='test.workspace', api_version=1, value_type=ServiceValue)
    return AgentServiceBinding(
        ref=AgentServiceRef(key=key),
        value=value or ServiceValue(marker='retained'),
        provider='tests.ServiceValue',
        features=features if features is not None else frozenset(('search',)),
        identity='opaque',
    )


def test_service_keys_named_references_and_registry_identity() -> None:
    first_key = AgentServiceKey(id='test.workspace', api_version=1, value_type=ServiceValue)
    second_key = AgentServiceKey[object](id='test.workspace', api_version=1)
    value = ServiceValue(marker='retained')
    service_binding = binding(value=value)
    services = AgentServices((service_binding,))

    assert first_key == second_key
    assert hash(first_key) == hash(second_key)
    assert services.resolve(AgentServiceRef(key=first_key)) is value
    assert services.binding(AgentServiceRef(key=first_key)) is service_binding
    assert services.contains(AgentServiceRef(key=first_key))
    assert services.bindings == (service_binding,)
    requirement = AgentServiceRequirement(service_id='test.workspace', api_version=1, name='project')
    assert requirement.ref().name == 'project'


@pytest.mark.parametrize(
    ('factory', 'message'),
    (
        (lambda: AgentServiceKey(id='workspace', api_version=1), 'namespaced'),
        (lambda: AgentServiceKey(id='test.workspace', api_version=0), 'positive'),
        (lambda: AgentServiceKey(id='test.bad/value', api_version=1), 'alphanumeric'),
        (
            lambda: AgentServiceRef(key=AgentServiceKey(id='test.workspace', api_version=1), name='not valid'),
            'identifier',
        ),
        (
            lambda: AgentServiceBinding(
                ref=AgentServiceRef(key=AgentServiceKey(id='test.workspace', api_version=1)), value=1, provider=''
            ),
            'provider',
        ),
        (
            lambda: AgentServiceBinding(
                ref=AgentServiceRef(key=AgentServiceKey(id='test.workspace', api_version=1)),
                value=1,
                provider='test',
                features=frozenset(('',)),
            ),
            'features',
        ),
    ),
)
def test_service_values_reject_invalid_identity(factory: Callable[[], object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()

    with pytest.raises(ValidationError):
        AgentServiceRequirement(service_id='workspace', api_version=1)
    with pytest.raises(ValidationError):
        AgentServiceRequirement(service_id='test.workspace', api_version=1, name='not valid')
    with pytest.raises(ValidationError):
        AgentServiceRequirement(
            service_id='test.workspace',
            api_version=1,
            required_features=frozenset(('',)),
        )


def test_registry_rejects_collisions_types_missing_services_and_features() -> None:
    service_binding = binding()

    with pytest.raises(AgentServiceCollisionError):
        AgentServices((service_binding, service_binding))
    with pytest.raises(AgentServiceCompatibilityError, match='value type'):
        AgentServices((binding(value=cast(ServiceValue, 'wrong')),))

    services = AgentServices((service_binding,))
    missing_ref = AgentServiceRef(key=AgentServiceKey(id='test.missing', api_version=1))
    with pytest.raises(AgentServiceMissingError):
        services.resolve(missing_ref)
    with pytest.raises(AgentServiceMissingError, match='Capability'):
        services.validate(
            AgentServiceRequirement(service_id='test.missing', api_version=1),
            consumer='missing',
        )
    with pytest.raises(AgentServiceCompatibilityError, match='unavailable operations'):
        services.validate(
            AgentServiceRequirement(
                service_id='test.workspace',
                api_version=1,
                required_features=frozenset(('ast',)),
            ),
            consumer='ast',
        )


@pytest.mark.asyncio
async def test_agent_factory_binds_once_reuses_bound_definition_and_reports_services() -> None:
    tracker: list[str] = []
    services = AgentServices((binding(),))
    factory = agent_factory(
        {
            'failing': FunctionModel(failing_request, model_name='failing'),
            'working': TestModel(custom_output_text='done', model_name='working'),
        },
        route=True,
    )
    definition = AgentDefinition[None, str](
        model=ModelRouteRef(name='answer'),
        deps_type=type(None),
        output_type=str,
        capabilities=(BindingCapability(tracker=tracker),),
        services=services,
    )

    agent = await factory.build(definition)
    result = await agent.run('Use fallback.', deps=None)

    assert result.output == 'done'
    assert tracker == ['bound']
    assert agent.diagnostics.services[0].identity == 'opaque'
    assert agent.diagnostics.services[0].consumers == ('binding',)
    assert ('toolset', 'arithmetic') in ((extension.kind, extension.id) for extension in agent.diagnostics.extensions)


def test_plugin_service_factory_contract_rejects_collisions_and_unknown_targets() -> None:
    provider = ProviderFactory(id='native')
    configurator = ConfiguratorFactory(id='limits', provider_id='native')

    factories = PluginServiceFactories(providers=(provider,), configurators=(configurator,))
    assert factories.providers == (provider,)
    assert factories.configurators == (configurator,)

    with pytest.raises(PluginError, match='Duplicate'):
        PluginServiceFactories(providers=(provider, provider))
    with pytest.raises(PluginError, match='unavailable provider'):
        PluginServiceFactories(configurators=(configurator,))
