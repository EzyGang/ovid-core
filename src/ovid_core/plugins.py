from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ovid_core.capabilities.base import BaseCapability
from ovid_core.errors import PluginError
from ovid_core.services import AgentServiceBinding, AgentServiceRequirement, AgentServices


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginActivationContext:
    services: AgentServices


class AgentServiceProviderFactory(Protocol):
    id: str

    @abstractmethod
    async def create(self, context: PluginActivationContext) -> AgentServiceBinding[Any]: ...


class AgentServiceConfiguratorFactory(Protocol):
    id: str
    provider_id: str

    @abstractmethod
    async def configure(
        self,
        context: PluginActivationContext,
        binding: AgentServiceBinding[Any],
    ) -> AgentServiceBinding[Any]: ...


class CapabilityFactory(Protocol):
    id: str
    requirements: tuple[AgentServiceRequirement, ...]

    @abstractmethod
    async def create(self, context: PluginActivationContext) -> BaseCapability[Any]: ...


class PluginFactories:
    __slots__ = ('_capabilities', '_configurators', '_providers')

    def __init__(
        self,
        *,
        providers: Sequence[AgentServiceProviderFactory] = (),
        configurators: Sequence[AgentServiceConfiguratorFactory] = (),
        capabilities: Sequence[CapabilityFactory] = (),
    ) -> None:
        self._providers = _index_factories(providers, kind='service provider')
        self._configurators = _index_factories(configurators, kind='service configurator')
        self._capabilities = _index_factories(capabilities, kind='capability factory')

    def provider(self, id: str) -> AgentServiceProviderFactory:
        return _selected_factory(self._providers, id=id, kind='service provider')

    def configurator(self, id: str) -> AgentServiceConfiguratorFactory:
        return _selected_factory(self._configurators, id=id, kind='service configurator')

    def capability(self, id: str) -> CapabilityFactory:
        return _selected_factory(self._capabilities, id=id, kind='capability factory')

    async def binding(
        self,
        *,
        provider_id: str,
        configurator_ids: Sequence[str],
        context: PluginActivationContext,
    ) -> AgentServiceBinding[Any]:
        binding = await self.provider(provider_id).create(context)
        for configurator_id in configurator_ids:
            configurator = self.configurator(configurator_id)
            if configurator.provider_id != provider_id:
                raise PluginError(f'Service configurator {configurator_id!r} does not target provider {provider_id!r}')

            configured = await configurator.configure(context, binding)
            if configured.ref != binding.ref or configured.provider != binding.provider:
                raise PluginError(f'Service configurator {configurator_id!r} cannot replace its provider')

            binding = configured

        return binding


def _index_factories[Factory](factories: Sequence[Factory], *, kind: str) -> dict[str, Factory]:
    indexed: dict[str, Factory] = {}
    for factory in factories:
        id = getattr(factory, 'id', '')
        if not id:
            raise PluginError(f'{kind.capitalize()} IDs must not be empty')
        if id in indexed:
            raise PluginError(f'Duplicate {kind} ID: {id!r}')

        indexed[id] = factory

    return indexed


def _selected_factory[Factory](factories: dict[str, Factory], *, id: str, kind: str) -> Factory:
    factory = factories.get(id)
    if factory is None:
        raise PluginError(f'Unknown {kind} ID: {id!r}')

    return factory
