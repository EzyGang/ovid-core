from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import JsonValue

from ovid_core.errors import PluginError
from ovid_core.services import AgentServiceBinding, AgentServices


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginActivationContext:
    services: AgentServices


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
