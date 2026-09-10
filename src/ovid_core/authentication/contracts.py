from abc import abstractmethod
from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import SecretStr

from ovid_core.authentication.models import AuthenticationInteraction
from ovid_core.routing import ModelProviderOption


class CredentialStore[Credential](Protocol):
    @abstractmethod
    async def load(self) -> Credential | None: ...

    @abstractmethod
    async def save(self, credential: Credential, /) -> None: ...

    @abstractmethod
    async def delete(self) -> None: ...


class ProviderCredentialBinding(Protocol):
    @abstractmethod
    async def connected(self) -> bool: ...

    @abstractmethod
    async def disconnect(self) -> None: ...


class APIKeyCredentialBinding(ProviderCredentialBinding, Protocol):
    @abstractmethod
    async def save_api_key(self, api_key: SecretStr) -> None: ...


class AuthenticationFlowSession(Protocol):
    @abstractmethod
    async def start(self) -> AuthenticationInteraction: ...

    @abstractmethod
    async def poll(self) -> AuthenticationInteraction | None: ...

    @abstractmethod
    async def submit(self, value: SecretStr) -> AuthenticationInteraction: ...

    @abstractmethod
    async def cancel(self) -> None: ...


class AuthenticationFlow(Protocol):
    id: str
    label: str

    @abstractmethod
    async def create_session(self) -> AuthenticationFlowSession: ...


type ProviderModelLoader = Callable[[], Awaitable[ModelProviderOption]]
