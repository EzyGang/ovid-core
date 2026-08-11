import asyncio

import pytest

from ovid_core.relay.errors import (
    RelayAddressInUseError,
    RelayCapacityError,
    RelayUnavailableError,
    UnknownRelayRecipientError,
)
from ovid_core.relay.memory import InMemoryRelay
from ovid_core.relay.models import RelayAddress, RelayIdentity, RelayMessageId


def identity(address: str) -> RelayIdentity:
    return RelayIdentity(address=RelayAddress(address), display_name=address.title())


async def test_connections_are_explicit_unique_live_contacts() -> None:
    relay = InMemoryRelay()
    alpha = relay.connection(identity('alpha'))
    beta = relay.connection(identity('beta'))

    assert alpha.identity == identity('alpha')
    assert [contact.model_dump() for contact in await alpha.contacts()] == [{'address': 'beta', 'display_name': 'Beta'}]
    with pytest.raises(RelayAddressInUseError, match='already registered'):
        relay.connection(identity('alpha'))

    beta.close()
    beta.close()
    assert await alpha.contacts() == ()
    with pytest.raises(UnknownRelayRecipientError, match='Unknown Relay recipient'):
        await alpha.send(RelayAddress('beta'), 'hello')
    with pytest.raises(RelayUnavailableError, match='unavailable'):
        await beta.pending()
    with pytest.raises(RelayUnavailableError, match='unavailable'):
        beta.set_delivery_handler(None)


def test_relay_requires_positive_bounded_capacity() -> None:
    with pytest.raises(ValueError, match='capacity must be positive'):
        InMemoryRelay(capacity=0)


async def test_capacity_rejects_without_dropping_and_networks_are_isolated() -> None:
    relay = InMemoryRelay(capacity=1)
    sender = relay.connection(identity('sender'))
    recipient = relay.connection(identity('recipient'))
    isolated = InMemoryRelay().connection(identity('isolated'))

    first = await sender.send(RelayAddress('recipient'), 'first')
    with pytest.raises(RelayCapacityError, match='mailbox is full'):
        await sender.send(RelayAddress('recipient'), 'second')
    with pytest.raises(UnknownRelayRecipientError):
        await isolated.send(RelayAddress('recipient'), 'separate network')

    pending = await recipient.pending(retain=True)
    assert [message.id for message in pending] == [first.message_id]
    assert [message.content for message in pending] == ['first']


async def test_pending_retains_or_atomically_consumes_fifo_messages() -> None:
    relay = InMemoryRelay()
    sender = relay.connection(identity('sender'))
    recipient = relay.connection(identity('recipient'))
    await sender.send(RelayAddress('recipient'), 'one')
    await sender.send(RelayAddress('recipient'), 'two')

    retained = await recipient.pending(retain=True)
    consumed = await recipient.pending()

    assert [message.content for message in retained] == ['one', 'two']
    assert consumed == retained
    assert await recipient.pending() == ()


async def test_wait_filters_pending_by_exact_sender_and_reply_correlation() -> None:
    relay = InMemoryRelay()
    alpha = relay.connection(identity('alpha'))
    beta = relay.connection(identity('beta'))
    gamma = relay.connection(identity('gamma'))
    correlation = RelayMessageId.new()
    await beta.send(RelayAddress('alpha'), 'uncorrelated')
    await gamma.send(RelayAddress('alpha'), 'correlated', reply_to=correlation)

    correlated = await alpha.wait(sender=RelayAddress('gamma'), reply_to=correlation, timeout_seconds=0)
    uncorrelated = await alpha.wait(sender=RelayAddress('beta'), timeout_seconds=0)

    assert correlated is not None and correlated.content == 'correlated'
    assert correlated.reply_to == correlation
    assert uncorrelated is not None and uncorrelated.content == 'uncorrelated'
    assert await alpha.wait(timeout_seconds=0) is None
    assert await alpha.wait(timeout_seconds=0.001) is None


async def test_oldest_matching_waiter_wins_and_cancellation_propagates() -> None:
    relay = InMemoryRelay(capacity=1)
    alpha = relay.connection(identity('alpha'))
    beta = relay.connection(identity('beta'))
    gamma = relay.connection(identity('gamma'))
    await beta.send(RelayAddress('alpha'), 'fills mailbox')

    nonmatching_waiter = asyncio.create_task(alpha.wait(sender=RelayAddress('delta')))
    first_waiter = asyncio.create_task(alpha.wait(sender=RelayAddress('gamma')))
    second_waiter = asyncio.create_task(alpha.wait(sender=RelayAddress('gamma')))
    await asyncio.sleep(0)
    receipt = await gamma.send(RelayAddress('alpha'), 'direct to waiter')

    first = await first_waiter
    assert first is not None and first.id == receipt.message_id
    assert not second_waiter.done()
    assert not nonmatching_waiter.done()

    nonmatching_waiter.cancel()
    second_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await nonmatching_waiter
    with pytest.raises(asyncio.CancelledError):
        await second_waiter
    assert [message.content for message in await alpha.pending()] == ['fills mailbox']
    await gamma.send(RelayAddress('alpha'), 'retained after cancellation')
    assert [message.content for message in await alpha.pending()] == ['retained after cancellation']


async def test_close_wakes_an_active_waiter_and_makes_all_operations_unavailable() -> None:
    relay = InMemoryRelay()
    connection = relay.connection(identity('agent'))
    waiter = asyncio.create_task(connection.wait())
    await asyncio.sleep(0)

    connection.close()

    with pytest.raises(RelayUnavailableError, match='unavailable'):
        await waiter
    with pytest.raises(RelayUnavailableError):
        await connection.send(RelayAddress('other'), 'message')
    with pytest.raises(RelayUnavailableError):
        await connection.wait(timeout_seconds=0)
    with pytest.raises(RelayUnavailableError):
        await connection.contacts()
