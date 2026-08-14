from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field, field_validator

from ovid_core.errors import OvidCoreError
from ovid_core.models import BaseModel


class AgentServiceError(OvidCoreError):
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
        if not self.id or '.' not in self.id:
            raise ValueError('Service IDs must be non-empty and globally namespaced')
        if self.api_version <= 0:
            raise ValueError('Service API versions must be positive')


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentServiceRef[T]:
    key: AgentServiceKey[T]
    name: str = 'default'

    def __post_init__(self) -> None:
        if not self.name or not self.name.isidentifier():
            raise ValueError('Service reference names must be non-empty identifiers')


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentServiceBinding[T]:
    ref: AgentServiceRef[T]
    value: T
    provider: str
    features: frozenset[str] = frozenset()
    identity: str | None = None

    def __post_init__(self) -> None:
        if not self.provider:
            raise ValueError('Service providers must be non-empty')
        if any(not feature for feature in self.features):
            raise ValueError('Service features must be non-empty')


class AgentServiceRequirement(BaseModel):
    service_id: str = Field(min_length=1)
    api_version: int = Field(gt=0)
    name: str = 'default'
    required_features: frozenset[str] = frozenset()

    @field_validator('service_id')
    @classmethod
    def validate_service_id(cls, value: str) -> str:
        if '.' not in value:
            raise ValueError('Service IDs must be globally namespaced')

        return value

    @field_validator('name')
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.isidentifier():
            raise ValueError('Service reference names must be identifiers')

        return value

    @field_validator('required_features')
    @classmethod
    def validate_features(cls, value: frozenset[str]) -> frozenset[str]:
        if any(not feature for feature in value):
            raise ValueError('Required service features must be non-empty')

        return value


class AgentServices:
    __slots__ = ('_bindings', '_by_ref')

    def __init__(self, bindings: Sequence[AgentServiceBinding[Any]] = ()) -> None:
        retained = tuple(bindings)
        by_ref: dict[AgentServiceRef[Any], AgentServiceBinding[Any]] = {}

        for binding in retained:
            if binding.ref in by_ref:
                raise AgentServiceCollisionError(f'Duplicate service binding: {_describe_ref(binding.ref)}')

            value_type = binding.ref.key.value_type
            if value_type is not None and not isinstance(binding.value, value_type):
                raise AgentServiceCompatibilityError(
                    f'Incompatible value for service {_describe_ref(binding.ref)}; expected {value_type.__qualname__}'
                )

            by_ref[binding.ref] = binding

        self._bindings = retained
        self._by_ref = by_ref

    def resolve[T](self, ref: AgentServiceRef[T]) -> T:
        return self.binding(ref).value

    def binding[T](self, ref: AgentServiceRef[T]) -> AgentServiceBinding[T]:
        binding = self._by_ref.get(ref)
        if binding is None:
            raise AgentServiceMissingError(f'Missing service binding: {_describe_ref(ref)}')

        return binding

    def contains(self, ref: AgentServiceRef[Any]) -> bool:
        return ref in self._by_ref

    def validate_requirement(self, requirement: AgentServiceRequirement, *, consumer: str) -> None:
        ref = AgentServiceRef(
            key=AgentServiceKey(id=requirement.service_id, api_version=requirement.api_version),
            name=requirement.name,
        )
        binding = self._by_ref.get(ref)
        if binding is None:
            raise AgentServiceMissingError(f'Capability {consumer!r} requires missing service {_describe_ref(ref)}')

        missing = requirement.required_features - binding.features
        if missing:
            required = ', '.join(sorted(requirement.required_features))
            available = ', '.join(sorted(binding.features)) or 'none'
            raise AgentServiceCompatibilityError(
                f'Capability {consumer!r} requires service {_describe_ref(ref)} features [{required}]; '
                f'available features: [{available}]. Select a compatible provider or remove the capability'
            )

    @property
    def bindings(self) -> tuple[AgentServiceBinding[Any], ...]:
        return self._bindings


def _describe_ref(ref: AgentServiceRef[Any]) -> str:
    return f'{ref.key.id}@{ref.key.api_version}:{ref.name}'
