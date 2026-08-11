from __future__ import annotations

from typing import TYPE_CHECKING

from ovid_core.relay.contracts import RelayConnection, RelayDeliveryHandler
from ovid_core.relay.errors import RelayUnavailableError
from ovid_core.relay.models import RelayAddress, RelayContact, RelayIdentity, RelayMessage, RelayMessageId, RelayReceipt


if TYPE_CHECKING:
    from ovid_core.relay.memory import InMemoryRelay


class InMemoryRelayConnection(RelayConnection):
    def __init__(self, *, relay: InMemoryRelay, identity: RelayIdentity) -> None:
        self._relay = relay
        self._identity = identity
        self._closed = False

    @property
    def identity(self) -> RelayIdentity:
        return self._identity

    def set_delivery_handler(self, handler: RelayDeliveryHandler | None) -> None:
        self._ensure_available()
        self._relay._set_delivery_handler(connection=self, handler=handler)

    async def send(
        self,
        recipient: RelayAddress,
        content: str,
        reply_to: RelayMessageId | None = None,
    ) -> RelayReceipt:
        self._ensure_available()
        return await self._relay._send(
            connection=self,
            recipient=recipient,
            content=content,
            reply_to=reply_to,
        )

    async def wait(
        self,
        sender: RelayAddress | None = None,
        reply_to: RelayMessageId | None = None,
        timeout_seconds: float | None = None,
    ) -> RelayMessage | None:
        self._ensure_available()
        return await self._relay._wait(
            connection=self,
            sender=sender,
            reply_to=reply_to,
            timeout_seconds=timeout_seconds,
        )

    async def pending(self, retain: bool = False) -> tuple[RelayMessage, ...]:
        self._ensure_available()
        return await self._relay._pending(connection=self, retain=retain)

    async def contacts(self) -> tuple[RelayContact, ...]:
        self._ensure_available()
        return await self._relay._contacts(self)

    def close(self) -> None:
        if self._closed:
            return

        self._closed = True
        self._relay._close(self)

    def _ensure_available(self) -> None:
        if self._closed:
            raise RelayUnavailableError(f'Relay connection is unavailable: {self.identity.address}')
