import asyncio

from ovid_core.relay.connection import InMemoryRelayConnection
from ovid_core.relay.contracts import RelayDeliveryHandler
from ovid_core.relay.models import RelayAddress, RelayIdentity, RelayMessage, RelayMessageId


class Waiter:
    def __init__(
        self,
        future: asyncio.Future[RelayMessage],
        sender: RelayAddress | None,
        reply_to: RelayMessageId | None,
    ) -> None:
        self.future = future
        self.sender = sender
        self.reply_to = reply_to

    def matches(self, message: RelayMessage) -> bool:
        sender_matches = self.sender is None or message.sender == self.sender
        reply_matches = self.reply_to is None or message.reply_to == self.reply_to

        return sender_matches and reply_matches


class Mailbox:
    def __init__(
        self,
        connection: InMemoryRelayConnection,
        identity: RelayIdentity,
        delivery_handler: RelayDeliveryHandler | None,
    ) -> None:
        self.connection = connection
        self.identity = identity
        self.delivery_handler = delivery_handler
        self.messages: list[RelayMessage] = []
        self.waiters: list[Waiter] = []
        self.reserved: set[RelayMessageId] = set()
        self.scheduled: set[RelayMessageId] = set()
        self.delivery_lock = asyncio.Lock()
