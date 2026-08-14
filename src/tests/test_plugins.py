from dataclasses import dataclass, replace

import pytest

from ovid_core.capabilities import BaseCapability
from ovid_core.errors import PluginError
from ovid_core.plugins import PluginActivationContext, PluginFactories
from ovid_core.services import AgentServiceBinding, AgentServiceKey, AgentServiceRef, AgentServices


SERVICE_REF = AgentServiceRef(key=AgentServiceKey[str](id='tests.workspace', api_version=1, value_type=str))


@dataclass
class ProviderFactory:
    id: str = 'native'

    async def create(self, context: PluginActivationContext) -> AgentServiceBinding[str]:
        assert context.services.bindings == ()
        return AgentServiceBinding(ref=SERVICE_REF, value='provider', provider='tests.Native')


@dataclass
class ConfiguratorFactory:
    id: str = 'limits'
    provider_id: str = 'native'
    replace_provider: bool = False

    async def configure(
        self,
        context: PluginActivationContext,
        binding: AgentServiceBinding[str],
    ) -> AgentServiceBinding[str]:
        assert context.services.bindings == ()
        if self.replace_provider:
            return replace(binding, provider='tests.Replacement')

        return replace(binding, features=frozenset({'search'}))


@dataclass
class CapabilityFactoryStub:
    id: str = 'search'
    requirements: tuple[object, ...] = ()

    async def create(self, context: PluginActivationContext) -> BaseCapability[None]:
        assert context.services.bindings == ()
        return BaseCapability(id='search')


@pytest.mark.asyncio
async def test_plugin_factories_require_explicit_selection_and_preserve_provider() -> None:
    provider = ProviderFactory()
    configurator = ConfiguratorFactory()
    capability = CapabilityFactoryStub()
    factories = PluginFactories(
        providers=(provider,),
        configurators=(configurator,),
        capabilities=(capability,),
    )
    context = PluginActivationContext(services=AgentServices())

    binding = await factories.binding(provider_id='native', configurator_ids=('limits',), context=context)

    assert factories.provider('native') is provider
    assert factories.configurator('limits') is configurator
    assert factories.capability('search') is capability
    assert binding.provider == 'tests.Native'
    assert binding.features == frozenset({'search'})
    assert (await capability.create(context)).id == 'search'


@pytest.mark.parametrize(
    ('keyword', 'factory', 'kind'),
    [
        ('providers', ProviderFactory(), 'service provider'),
        ('configurators', ConfiguratorFactory(), 'service configurator'),
        ('capabilities', CapabilityFactoryStub(), 'capability factory'),
    ],
)
def test_plugin_factory_ids_reject_duplicates(keyword: str, factory: object, kind: str) -> None:
    with pytest.raises(PluginError, match=f'Duplicate {kind} ID'):
        PluginFactories(**{keyword: (factory, factory)})


@pytest.mark.parametrize('keyword', ['providers', 'configurators', 'capabilities'])
def test_plugin_factory_ids_reject_empty_values(keyword: str) -> None:
    with pytest.raises(PluginError, match='IDs must not be empty'):
        PluginFactories(**{keyword: (ProviderFactory(id=''),)})


@pytest.mark.parametrize('selector', ['provider', 'configurator', 'capability'])
def test_plugin_factory_selection_rejects_unknown_ids(selector: str) -> None:
    factories = PluginFactories()

    with pytest.raises(PluginError, match='Unknown'):
        getattr(factories, selector)('missing')


@pytest.mark.asyncio
async def test_configurators_cannot_target_or_replace_another_provider() -> None:
    context = PluginActivationContext(services=AgentServices())
    wrong_target = PluginFactories(
        providers=(ProviderFactory(),),
        configurators=(ConfiguratorFactory(provider_id='other'),),
    )
    with pytest.raises(PluginError, match='does not target provider'):
        await wrong_target.binding(provider_id='native', configurator_ids=('limits',), context=context)

    replacement = PluginFactories(
        providers=(ProviderFactory(),),
        configurators=(ConfiguratorFactory(replace_provider=True),),
    )
    with pytest.raises(PluginError, match='cannot replace its provider'):
        await replacement.binding(provider_id='native', configurator_ids=('limits',), context=context)
