from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import Field, field_validator

from ovid_core.models import BaseModel


class AgentServiceError(Exception):
    pass


class AgentServiceCollisionError(AgentServiceError):
    pass


class AgentServiceMissingError(AgentServiceError):
    pass


class AgentServiceCompatibilityError(AgentServiceError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentServiceKey[T]:
    id: str
    api_version: int
    value_type: type[T] | None = field(default=None, compare=False, hash=False, repr=False)

    def __post_init__(self) -> None:
        _validate_service_id(self.id)

        if self.api_version < 1:
            raise ValueError('Service API version must be positive')


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentServiceRef[T]:
    key: AgentServiceKey[T]
    name: str = 'default'

    def __post_init__(self) -> None:
        _validate_reference_name(self.name)


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentServiceBinding[T]:
    ref: AgentServiceRef[T]
    value: T
    provider: str
    features: frozenset[str] = frozenset()
    identity: str | None = None

    def __post_init__(self) -> None:
        if not self.provider:
            raise ValueError('Service provider must be non-empty')
        if any(not feature for feature in self.features):
            raise ValueError('Service features must be non-empty')


class AgentServiceRequirement(BaseModel):
    service_id: str
    api_version: int = Field(ge=1)
    name: str = 'default'
    required_features: frozenset[str] = frozenset()

    @field_validator('service_id')
    @classmethod
    def validate_service_id(cls, value: str) -> str:
        _validate_service_id(value)
        return value

    @field_validator('name')
    @classmethod
    def validate_name(cls, value: str) -> str:
        _validate_reference_name(value)
        return value

    @field_validator('required_features')
    @classmethod
    def validate_features(cls, value: frozenset[str]) -> frozenset[str]:
        if any(not feature for feature in value):
            raise ValueError('Required service features must be non-empty')

        return value

    def ref(self) -> AgentServiceRef[Any]:
        return AgentServiceRef(
            key=AgentServiceKey(id=self.service_id, api_version=self.api_version),
            name=self.name,
        )


class AgentServices:
    def __init__(self, bindings: Sequence[AgentServiceBinding[Any]] = ()) -> None:
        indexed: dict[tuple[str, int, str], AgentServiceBinding[Any]] = {}

        for binding in bindings:
            identity = _binding_identity(binding.ref)
            if identity in indexed:
                service_id, api_version, name = identity
                raise AgentServiceCollisionError(
                    f'Duplicate service binding: {service_id!r} API {api_version}, name {name!r}'
                )

            value_type = binding.ref.key.value_type
            if value_type is not None and not isinstance(binding.value, value_type):
                raise AgentServiceCompatibilityError(
                    f'Service {binding.ref.key.id!r} API {binding.ref.key.api_version} has an incompatible value type'
                )

            indexed[identity] = binding

        self._bindings = tuple(bindings)
        self._indexed = indexed

    def resolve[T](self, ref: AgentServiceRef[T]) -> T:
        return self.binding(ref).value

    def binding[T](self, ref: AgentServiceRef[T]) -> AgentServiceBinding[T]:
        binding = self._indexed.get(_binding_identity(ref))
        if binding is None:
            raise AgentServiceMissingError(
                f'Service {ref.key.id!r} API {ref.key.api_version}, name {ref.name!r} is not bound'
            )

        return cast(AgentServiceBinding[T], binding)

    def contains(self, ref: AgentServiceRef[Any]) -> bool:
        return _binding_identity(ref) in self._indexed

    def validate(self, requirement: AgentServiceRequirement, *, consumer: str) -> AgentServiceBinding[Any]:
        try:
            binding = self.binding(requirement.ref())
        except AgentServiceMissingError as error:
            raise AgentServiceMissingError(f'Capability {consumer!r} requires {error}') from error

        missing = requirement.required_features - binding.features
        if missing:
            available = ', '.join(sorted(binding.features)) or 'none'
            required = ', '.join(sorted(missing))
            raise AgentServiceCompatibilityError(
                f'Capability {consumer!r} requires unavailable operations [{required}] from service '
                f'{requirement.service_id!r}; available operations: [{available}]'
            )

        return binding

    @property
    def bindings(self) -> tuple[AgentServiceBinding[Any], ...]:
        return self._bindings


def _binding_identity(ref: AgentServiceRef[Any]) -> tuple[str, int, str]:
    return ref.key.id, ref.key.api_version, ref.name


def _validate_service_id(value: str) -> None:
    if not value or '.' not in value or any(part == '' for part in value.split('.')):
        raise ValueError('Service ID must be a non-empty namespaced identifier')
    if any(not part.replace('_', '').replace('-', '').isalnum() for part in value.split('.')):
        raise ValueError('Service ID must contain only alphanumeric characters, underscores, hyphens, and dots')


def _validate_reference_name(value: str) -> None:
    if not value or not value.isidentifier():
        raise ValueError('Service reference name must be a non-empty identifier')
