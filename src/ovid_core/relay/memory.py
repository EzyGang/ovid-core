import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime

from ovid_core.relay._locking import locked
from ovid_core.relay._state import Mailbox, Waiter
from ovid_core.relay.connection import InMemoryRelayConnection
from ovid_core.relay.contracts import RelayDeliveryHandler, RelayDisposition
from ovid_core.relay.errors import (
    RelayAddressInUseError,
    RelayCapacityError,
    RelayUnavailableError,
    UnknownRelayRecipientError,
)
from ovid_core.relay.models import RelayAddress, RelayContact, RelayIdentity, RelayMessage, RelayMessageId, RelayReceipt


class InMemoryRelay:
    def __init__(self, *, capacity: int = 100) -> None:
        if capacity < 1:
            raise ValueError('Relay mailbox capacity must be positive')

        self._capacity = capacity
        self._mailboxes: dict[RelayAddress, Mailbox] = {}
        self._lock = asyncio.Lock()
        self._delivery_tasks: set[asyncio.Task[None]] = set()

    def connection(
        self,
        identity: RelayIdentity,
        delivery_handler: RelayDeliveryHandler | None = None,
    ) -> InMemoryRelayConnection:
        if identity.address in self._mailboxes:
            raise RelayAddressInUseError(f'Relay address is already registered: {identity.address}')

        connection = InMemoryRelayConnection(relay=self, identity=identity)
        self._mailboxes[identity.address] = Mailbox(
            connection=connection,
            identity=identity,
            delivery_handler=delivery_handler,
        )

        return connection

    async def _send(
        self,
        connection: InMemoryRelayConnection,
        recipient: RelayAddress,
        content: str,
        reply_to: RelayMessageId | None,
    ) -> RelayReceipt:
        message = RelayMessage(
            id=RelayMessageId.new(),
            sender=connection.identity.address,
            recipient=recipient,
            content=content,
            sent_at=datetime.now(UTC),
            reply_to=reply_to,
        )
        mailbox, waiter = await self._accept_message(connection, message)

        if waiter is not None:
            waiter.future.set_result(message)
        elif mailbox.delivery_handler is not None:
            self._schedule_delivery(mailbox=mailbox, message_id=message.id)

        return RelayReceipt(message_id=message.id, recipient=recipient, accepted_at=datetime.now(UTC))

    @locked
    def _accept_message(
        self,
        connection: InMemoryRelayConnection,
        message: RelayMessage,
    ) -> tuple[Mailbox, Waiter | None]:
        self._mailbox_for(connection)
        mailbox = self._recipient_mailbox(message.recipient)
        waiter = self._matching_waiter(mailbox=mailbox, message=message)
        if waiter is None:
            self._accept_pending(mailbox=mailbox, message=message)

        return mailbox, waiter

    async def _wait(
        self,
        connection: InMemoryRelayConnection,
        sender: RelayAddress | None,
        reply_to: RelayMessageId | None,
        timeout_seconds: float | None,
    ) -> RelayMessage | None:
        future: asyncio.Future[RelayMessage] = asyncio.get_running_loop().create_future()
        waiter = Waiter(future=future, sender=sender, reply_to=reply_to)
        message = await self._take_or_register_waiter(
            connection=connection,
            waiter=waiter,
            immediate=timeout_seconds == 0,
        )
        if message is not None or timeout_seconds == 0:
            return message

        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError:
            return None
        finally:
            await self._remove_waiter(connection, waiter)

    @locked
    def _take_or_register_waiter(
        self,
        connection: InMemoryRelayConnection,
        waiter: Waiter,
        immediate: bool,
    ) -> RelayMessage | None:
        mailbox = self._mailbox_for(connection)
        message = self._take_matching_message(mailbox=mailbox, waiter=waiter)
        if message is None and not immediate:
            mailbox.waiters.append(waiter)

        return message

    @locked
    def _pending(
        self,
        connection: InMemoryRelayConnection,
        retain: bool,
    ) -> tuple[RelayMessage, ...]:
        mailbox = self._mailbox_for(connection)
        messages = tuple(message for message in mailbox.messages if message.id not in mailbox.reserved)
        if not retain:
            consumed = {message.id for message in messages}
            mailbox.messages = [message for message in mailbox.messages if message.id not in consumed]

        return messages

    @locked
    def _contacts(self, connection: InMemoryRelayConnection) -> tuple[RelayContact, ...]:
        self._mailbox_for(connection)

        return tuple(
            RelayContact(address=mailbox.identity.address, display_name=mailbox.identity.display_name)
            for mailbox in self._mailboxes.values()
            if mailbox.connection is not connection
        )

    def _set_delivery_handler(
        self,
        connection: InMemoryRelayConnection,
        handler: RelayDeliveryHandler | None,
    ) -> None:
        mailbox = self._mailboxes[connection.identity.address]
        mailbox.delivery_handler = handler
        if handler is not None:
            self._schedule_messages(mailbox, mailbox.messages)

    def _close(self, connection: InMemoryRelayConnection) -> None:
        mailbox = self._mailboxes.pop(connection.identity.address)
        error = RelayUnavailableError(f'Relay connection is unavailable: {connection.identity.address}')
        for waiter in mailbox.waiters:
            waiter.future.set_exception(error)

    def _mailbox_for(self, connection: InMemoryRelayConnection) -> Mailbox:
        return self._mailboxes[connection.identity.address]

    def _recipient_mailbox(self, recipient: RelayAddress) -> Mailbox:
        mailbox = self._mailboxes.get(recipient)
        if mailbox is None:
            raise UnknownRelayRecipientError(f'Unknown Relay recipient: {recipient}')

        return mailbox

    def _accept_pending(self, mailbox: Mailbox, message: RelayMessage) -> None:
        if len(mailbox.messages) >= self._capacity:
            raise RelayCapacityError(f'Relay mailbox is full: {mailbox.identity.address}')

        mailbox.messages.append(message)

    def _matching_waiter(self, mailbox: Mailbox, message: RelayMessage) -> Waiter | None:
        for waiter in mailbox.waiters:
            if waiter.matches(message):
                mailbox.waiters.remove(waiter)
                return waiter

        return None

    def _take_matching_message(self, mailbox: Mailbox, waiter: Waiter) -> RelayMessage | None:
        for index, message in enumerate(mailbox.messages):
            if message.id not in mailbox.reserved and waiter.matches(message):
                return mailbox.messages.pop(index)

        return None

    @locked
    def _remove_waiter(self, connection: InMemoryRelayConnection, waiter: Waiter) -> None:
        mailbox = self._mailboxes.get(connection.identity.address)
        if mailbox is not None and waiter in mailbox.waiters:
            mailbox.waiters.remove(waiter)

    def _schedule_messages(self, mailbox: Mailbox, messages: Iterable[RelayMessage]) -> None:
        for message in messages:
            self._schedule_delivery(mailbox, message.id)

    def _schedule_delivery(self, mailbox: Mailbox, message_id: RelayMessageId) -> None:
        if message_id in mailbox.scheduled:
            return

        task = asyncio.get_running_loop().create_task(self._deliver(mailbox, message_id))
        mailbox.scheduled.add(message_id)
        self._delivery_tasks.add(task)
        task.add_done_callback(self._delivery_tasks.discard)

    async def _deliver(self, mailbox: Mailbox, message_id: RelayMessageId) -> None:
        async with mailbox.delivery_lock:
            prepared = await self._prepare_delivery(mailbox, message_id)
            if prepared is None:
                return
            message, handler = prepared
            try:
                disposition = await handler(message)
            except Exception:
                disposition = RelayDisposition.DEFER

            await self._finish_delivery(mailbox, message_id, disposition)

    @locked
    def _prepare_delivery(
        self,
        mailbox: Mailbox,
        message_id: RelayMessageId,
    ) -> tuple[RelayMessage, RelayDeliveryHandler] | None:
        current = self._mailboxes.get(mailbox.identity.address)
        message = next((item for item in mailbox.messages if item.id == message_id), None)
        if current is not mailbox or message is None or mailbox.delivery_handler is None:
            mailbox.scheduled.discard(message_id)
            return None

        mailbox.reserved.add(message_id)
        return message, mailbox.delivery_handler

    @locked
    def _finish_delivery(
        self,
        mailbox: Mailbox,
        message_id: RelayMessageId,
        disposition: RelayDisposition,
    ) -> None:
        if disposition is RelayDisposition.ACKNOWLEDGE:
            mailbox.messages = [message for message in mailbox.messages if message.id != message_id]

        mailbox.reserved.discard(message_id)
        mailbox.scheduled.discard(message_id)
