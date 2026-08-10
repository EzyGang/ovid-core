import re
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from pydantic import JsonValue

from ovid_core.agents import OvidAgent
from ovid_core.models import BaseModel


type ASGIScope = MutableMapping[str, Any]
type ASGIMessage = MutableMapping[str, Any]
type ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
type ASGISend = Callable[[ASGIMessage], Awaitable[None]]


_AGENT_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]+$')


def _validate_registration(kind: str, registration_id: str, description: str) -> None:
    if not _AGENT_ID_PATTERN.fullmatch(registration_id):
        raise ValueError(f'{kind} registration id must contain only letters, numbers, underscores, or hyphens')

    if not description.strip():
        raise ValueError(f'{kind} registration description must not be empty')


class RequestContext:
    def __init__(
        self,
        *,
        method: str,
        path: str,
        headers: Iterable[tuple[str, str]] = (),
        client_host: str | None = None,
        request_id: str,
    ) -> None:
        self._method = method
        self._path = path
        self._headers = MappingProxyType({name.casefold(): value for name, value in headers})
        self._client_host = client_host
        self._request_id = request_id

    @property
    def method(self) -> str:
        return self._method

    @property
    def path(self) -> str:
        return self._path

    @property
    def client_host(self) -> str | None:
        return self._client_host

    @property
    def request_id(self) -> str:
        return self._request_id

    def header(self, name: str) -> str | None:
        return self._headers.get(name.casefold())

    def __repr__(self) -> str:
        return (
            f'RequestContext(method={self.method!r}, path={self.path!r}, '
            f'client_host={self.client_host!r}, request_id={self.request_id!r})'
        )


class AuthorizationResult(BaseModel):
    allowed: bool
    principal: str | None = None


class AuthorizationCallback(Protocol):
    @abstractmethod
    async def __call__(self, context: RequestContext, resource_id: str, /) -> AuthorizationResult: ...


class DependenciesFactory[Deps](Protocol):
    @abstractmethod
    async def __call__(self, context: RequestContext, authorization: AuthorizationResult) -> Deps: ...


class CommandHandler(Protocol):
    @abstractmethod
    async def __call__(
        self,
        context: RequestContext,
        authorization: AuthorizationResult,
        arguments: JsonValue,
        /,
    ) -> JsonValue: ...


class ReadinessCallback(Protocol):
    @abstractmethod
    async def __call__(self) -> bool: ...


class LifecycleCallback(Protocol):
    @abstractmethod
    async def __call__(self) -> None: ...


class ASGIApplication(Protocol):
    @abstractmethod
    async def __call__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None: ...


@dataclass(frozen=True, slots=True)
class AgentRegistration[Deps, Output]:
    id: str
    description: str
    agent: OvidAgent[Deps, Output]
    dependencies: DependenciesFactory[Deps]

    def __post_init__(self) -> None:
        _validate_registration('agent', self.id, self.description)


@dataclass(frozen=True, slots=True)
class CommandRegistration:
    id: str
    description: str
    handler: CommandHandler

    def __post_init__(self) -> None:
        _validate_registration('command', self.id, self.description)
