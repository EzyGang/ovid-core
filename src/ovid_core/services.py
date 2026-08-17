from dataclasses import dataclass
from typing import Any


class AgentServiceError(Exception):
    pass


class AgentServiceCollisionError(AgentServiceError):
    pass


class AgentServiceNotFoundError(AgentServiceError):
    pass


class AgentServiceCompatibilityError(AgentServiceError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentServiceKey[T]:
    id: str
    api_version: int
    value_type: type[T] | None

    def __post_init__(self) -> None:
        if '.' not in self.id or self.id.startswith('.') or self.id.endswith('.'):
            raise ValueError('service ID must be namespaced')
        if self.api_version < 1:
            raise ValueError('service API version must be positive')


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentServiceRef[T]:
    key: AgentServiceKey[T]
    name: str = 'default'

    def __post_init__(self) -> None:
        if not self.name or self.name.strip() != self.name:
            raise ValueError('service reference name must be non-empty and trimmed')


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentServiceBinding[T]:
    ref: AgentServiceRef[T]
    value: T
    provider: str
    features: frozenset[str] = frozenset()
    identity: str | None = None

    def __post_init__(self) -> None:
        value_type = self.ref.key.value_type
        if value_type is not None and not isinstance(self.value, value_type):
            raise TypeError(f'service value does not implement {value_type.__qualname__}')
        if not self.provider:
            raise ValueError('service provider must be non-empty')
        if any(not feature for feature in self.features):
            raise ValueError('service features must be non-empty')


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentServiceRequirement:
    service_id: str
    name: str = 'default'
    api_version: int = 1
    required_features: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.service_id or not self.name:
            raise ValueError('service requirement ID and name must be non-empty')
        if self.api_version < 1:
            raise ValueError('service API version must be positive')


class AgentServices:
    def __init__(self, bindings: tuple[AgentServiceBinding[Any], ...] = ()) -> None:
        self._bindings = bindings
        self._by_identity: dict[tuple[str, str], AgentServiceBinding[Any]] = {}
        self._consumers: dict[tuple[str, str], list[str]] = {}

        for binding in bindings:
            identity = (binding.ref.key.id, binding.ref.name)
            if identity in self._by_identity:
                service_id, name = identity
                raise AgentServiceCollisionError(f'Duplicate service binding: {service_id}:{name}')
            self._by_identity[identity] = binding

    @property
    def bindings(self) -> tuple[AgentServiceBinding[Any], ...]:
        return self._bindings

    def resolve[T](self, ref: AgentServiceRef[T]) -> T:
        identity = (ref.key.id, ref.name)
        binding = self._by_identity.get(identity)
        if binding is None:
            raise AgentServiceNotFoundError(f'Service is not configured: {ref.key.id}:{ref.name}')
        if binding.ref.key.api_version < ref.key.api_version:
            raise AgentServiceCompatibilityError(
                f'Service API is incompatible: {ref.key.id}:{ref.name} requires version {ref.key.api_version}'
            )

        value_type = ref.key.value_type
        if value_type is not None and not isinstance(binding.value, value_type):
            raise AgentServiceCompatibilityError(f'Service has an incompatible value type: {ref.key.id}:{ref.name}')

        return binding.value

    def validate(self, requirements: tuple[AgentServiceRequirement, ...], *, consumer: str) -> None:
        for requirement in requirements:
            identity = (requirement.service_id, requirement.name)
            binding = self._by_identity.get(identity)
            if binding is None:
                raise AgentServiceNotFoundError(
                    f'Service required by {consumer!r} is not configured: {requirement.service_id}:{requirement.name}'
                )
            if binding.ref.key.api_version < requirement.api_version:
                raise AgentServiceCompatibilityError(
                    f'Service required by {consumer!r} has an incompatible API version: {requirement.service_id}'
                )

            missing = requirement.required_features - binding.features
            if missing:
                features = ', '.join(sorted(missing))
                raise AgentServiceCompatibilityError(
                    f'Service required by {consumer!r} has unavailable operations: {features}'
                )
            consumers = self._consumers.setdefault(identity, [])
            if consumer not in consumers:
                consumers.append(consumer)

    def consumers(self, binding: AgentServiceBinding[Any]) -> tuple[str, ...]:
        identity = (binding.ref.key.id, binding.ref.name)
        return tuple(self._consumers.get(identity, ()))
