import os
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from pydantic import SecretStr

from ovid_core.credentials.models import CredentialRef, EnvironmentCredentialRef
from ovid_core.errors import CredentialError


type ProviderAPIKeyResolver = Callable[[str, str], Awaitable[SecretStr | None]]


class CredentialResolver(Protocol):
    @abstractmethod
    async def resolve(self, reference: CredentialRef) -> SecretStr: ...


class EnvironmentCredentialResolver:
    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = dict(os.environ if environment is None else environment)

    async def resolve(self, reference: CredentialRef) -> SecretStr:
        if not isinstance(reference, EnvironmentCredentialRef):
            raise CredentialError(f'environment resolver does not support {reference.kind!r} credential references')

        value = self._environment.get(reference.variable)
        if value is None:
            raise CredentialError(f'environment variable {reference.variable!r} is not set')

        return SecretStr(value)
