from abc import abstractmethod
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol

from ovid_core.relay.models import RelayAddress, RelayContact, RelayIdentity, RelayMessage, RelayMessageId, RelayReceipt


class RelayDisposition(StrEnum):
    ACKNOWLEDGE = 'acknowledge'
    DEFER = 'defer'


type RelayDeliveryHandler = Callable[[RelayMessage], Awaitable[RelayDisposition]]


class RelayConnection(Protocol):
    @property
    @abstractmethod
    def identity(self) -> RelayIdentity: ...

    @abstractmethod
    def set_delivery_handler(self, handler: RelayDeliveryHandler | None) -> None: ...

    @abstractmethod
    async def send(
        self,
        recipient: RelayAddress,
        content: str,
        reply_to: RelayMessageId | None = None,
    ) -> RelayReceipt: ...

    @abstractmethod
    async def wait(
        self,
        sender: RelayAddress | None = None,
        reply_to: RelayMessageId | None = None,
        timeout_seconds: float | None = None,
    ) -> RelayMessage | None: ...

    @abstractmethod
    async def pending(self, retain: bool = False) -> tuple[RelayMessage, ...]: ...

    @abstractmethod
    async def contacts(self) -> tuple[RelayContact, ...]: ...
