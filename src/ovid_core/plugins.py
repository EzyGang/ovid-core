from abc import abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from pydantic import JsonValue

from ovid_core.capabilities.base import BaseCapability
from ovid_core.errors import PluginError
from ovid_core.models import BaseModel
from ovid_core.services import AgentServiceBinding, AgentServiceRequirement, AgentServices


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginActivationContext:
    services: AgentServices
    configuration: BaseModel | None = None


class AgentServiceProviderFactory(Protocol):
    @property
    @abstractmethod
    def id(self) -> str: ...

    @abstractmethod
    def create(
        self,
        *,
        context: PluginActivationContext,
        config: dict[str, JsonValue],
    ) -> AgentServiceBinding[Any]: ...


class AgentServiceConfiguratorFactory(Protocol):
    @property
    @abstractmethod
    def id(self) -> str: ...

    @property
    @abstractmethod
    def provider_id(self) -> str: ...

    @abstractmethod
    def configure(
        self,
        binding: AgentServiceBinding[Any],
        *,
        context: PluginActivationContext,
        config: dict[str, JsonValue],
    ) -> AgentServiceBinding[Any]: ...


class CapabilityFactory(Protocol):
    @property
    @abstractmethod
    def id(self) -> str: ...

    @property
    @abstractmethod
    def requirements(self) -> tuple[AgentServiceRequirement, ...]: ...

    @abstractmethod
    def create(self, *, context: PluginActivationContext) -> BaseCapability[Any]: ...


type ServiceProviderBuilder = Callable[..., AgentServiceBinding[Any]]
type ServiceConfiguratorBuilder = Callable[..., AgentServiceBinding[Any]]
type CapabilityBuilder = Callable[..., BaseCapability[Any]]


@dataclass(frozen=True, slots=True)
class _RegisteredServiceProviderFactory:
    id: str
    factory: ServiceProviderBuilder

    def create(
        self,
        *,
        context: PluginActivationContext,
        config: dict[str, JsonValue],
    ) -> AgentServiceBinding[Any]:
        return self.factory(context=context, config=config)


@dataclass(frozen=True, slots=True)
class _RegisteredServiceConfiguratorFactory:
    id: str
    provider_id: str
    factory: ServiceConfiguratorBuilder

    def configure(
        self,
        binding: AgentServiceBinding[Any],
        *,
        context: PluginActivationContext,
        config: dict[str, JsonValue],
    ) -> AgentServiceBinding[Any]:
        return self.factory(binding, context=context, config=config)


@dataclass(frozen=True, slots=True)
class _RegisteredCapabilityFactory:
    id: str
    requirements: tuple[AgentServiceRequirement, ...]
    factory: CapabilityBuilder

    def create(self, *, context: PluginActivationContext) -> BaseCapability[Any]:
        return self.factory(context=context)


class PluginRegistrar:
    def __init__(self) -> None:
        self._providers: dict[str, AgentServiceProviderFactory] = {}
        self._configurators: dict[str, AgentServiceConfiguratorFactory] = {}
        self._capabilities: dict[str, CapabilityFactory] = {}

    @property
    def service_factories(self) -> PluginServiceFactories:
        return PluginServiceFactories(
            providers=tuple(self._providers.values()),
            configurators=tuple(self._configurators.values()),
        )

    @property
    def capability_factories(self) -> tuple[CapabilityFactory, ...]:
        return tuple(self._capabilities.values())

    def register_service_provider_factory(self, *, id: str, factory: ServiceProviderBuilder) -> None:
        registered = _RegisteredServiceProviderFactory(id=id, factory=factory)
        self._register(self._providers, registered, kind='service provider')

    def register_service_configurator_factory(
        self,
        *,
        id: str,
        provider_id: str,
        factory: ServiceConfiguratorBuilder,
    ) -> None:
        registered = _RegisteredServiceConfiguratorFactory(id=id, provider_id=provider_id, factory=factory)
        self._register(self._configurators, registered, kind='service configurator')

    def register_capability_factory(
        self,
        *,
        id: str,
        requirements: Sequence[AgentServiceRequirement],
        factory: CapabilityBuilder,
    ) -> None:
        registered = _RegisteredCapabilityFactory(id=id, requirements=tuple(requirements), factory=factory)
        self._register(self._capabilities, registered, kind='capability')

    def select_service_factories(
        self,
        *,
        providers: Sequence[str],
        configurators: Sequence[str] = (),
    ) -> PluginServiceFactories:
        return PluginServiceFactories(
            providers=self._select(self._providers, providers, kind='service provider'),
            configurators=self._select(self._configurators, configurators, kind='service configurator'),
        )

    def select_capability_factories(self, ids: Sequence[str]) -> tuple[CapabilityFactory, ...]:
        return self._select(self._capabilities, ids, kind='capability')

    @staticmethod
    def _select[Factory](
        registry: dict[str, Factory],
        identifiers: Sequence[str],
        *,
        kind: str,
    ) -> tuple[Factory, ...]:
        selected: list[Factory] = []
        seen: set[str] = set()
        for identifier in identifiers:
            if identifier in seen:
                raise PluginError(f'Duplicate selected {kind} ID: {identifier!r}')
            try:
                selected.append(registry[identifier])
            except KeyError as error:
                raise PluginError(f'Unknown selected {kind} ID: {identifier!r}') from error
            seen.add(identifier)
        return tuple(selected)

    @staticmethod
    def _register[Factory](
        registry: dict[str, Factory],
        factory: Factory,
        *,
        kind: str,
    ) -> None:
        identifier = cast(Any, factory).id
        if not identifier or identifier in registry:
            raise PluginError(f'Duplicate or empty {kind} ID: {identifier!r}')
        registry[identifier] = factory


@dataclass(frozen=True, slots=True)
class PluginServiceFactories:
    providers: tuple[AgentServiceProviderFactory, ...] = ()
    configurators: tuple[AgentServiceConfiguratorFactory, ...] = ()

    def __init__(
        self,
        *,
        providers: Sequence[AgentServiceProviderFactory] = (),
        configurators: Sequence[AgentServiceConfiguratorFactory] = (),
    ) -> None:
        _validate_unique_ids(providers, kind='service provider')
        _validate_unique_ids(configurators, kind='service configurator')

        provider_ids = frozenset(provider.id for provider in providers)
        for configurator in configurators:
            if configurator.provider_id in provider_ids:
                continue

            message = f'Configurator {configurator.id!r} targets unavailable provider {configurator.provider_id!r}'
            raise PluginError(message)

        object.__setattr__(self, 'providers', tuple(providers))
        object.__setattr__(self, 'configurators', tuple(configurators))


def _validate_unique_ids(
    factories: Sequence[AgentServiceProviderFactory] | Sequence[AgentServiceConfiguratorFactory],
    *,
    kind: str,
) -> None:
    identifiers: set[str] = set()

    for factory in factories:
        if not factory.id or factory.id in identifiers:
            raise PluginError(f'Duplicate or empty {kind} ID: {factory.id!r}')
        identifiers.add(factory.id)
