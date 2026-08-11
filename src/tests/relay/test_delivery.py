import asyncio

from pytest_mock import MockerFixture

from ovid_core.relay.contracts import RelayDisposition
from ovid_core.relay.memory import InMemoryRelay
from ovid_core.relay.models import RelayAddress, RelayIdentity, RelayMessage


def identity(address: str) -> RelayIdentity:
    return RelayIdentity(address=RelayAddress(address), display_name=address.title())


async def settle_delivery() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def test_setting_handler_delivers_pending_and_ack_defer_or_error_controls_consumption(
    mocker: MockerFixture,
) -> None:
    relay = InMemoryRelay()
    sender = relay.connection(identity('sender'))
    recipient = relay.connection(identity('recipient'))
    await sender.send(RelayAddress('recipient'), 'ack')
    await sender.send(RelayAddress('recipient'), 'defer')
    await sender.send(RelayAddress('recipient'), 'error')
    handler = mocker.AsyncMock(
        side_effect=(RelayDisposition.ACKNOWLEDGE, RelayDisposition.DEFER, RuntimeError('application failure'))
    )

    recipient.set_delivery_handler(handler)
    await settle_delivery()

    assert [call.args[0].content for call in handler.await_args_list] == ['ack', 'defer', 'error']
    assert [message.content for message in await recipient.pending(retain=True)] == ['defer', 'error']

    recipient.set_delivery_handler(None)
    await sender.send(RelayAddress('recipient'), 'future pending')
    await settle_delivery()
    assert handler.await_count == 3
    assert [message.content for message in await recipient.pending()] == ['defer', 'error', 'future pending']


async def test_waiter_precedes_automatic_handler(mocker: MockerFixture) -> None:
    relay = InMemoryRelay()
    sender = relay.connection(identity('sender'))
    handler = mocker.AsyncMock(return_value=RelayDisposition.ACKNOWLEDGE)
    recipient = relay.connection(identity('recipient'), delivery_handler=handler)
    waiter = asyncio.create_task(recipient.wait(sender=RelayAddress('sender')))
    await asyncio.sleep(0)

    receipt = await sender.send(RelayAddress('recipient'), 'waited')
    message = await waiter
    await settle_delivery()

    assert message is not None and message.id == receipt.message_id
    handler.assert_not_awaited()
    assert await recipient.pending() == ()


async def test_sender_acceptance_does_not_wait_for_handler_and_inflight_message_is_not_duplicated(
    mocker: MockerFixture,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def block_delivery(_: RelayMessage) -> RelayDisposition:
        entered.set()
        await release.wait()
        return RelayDisposition.ACKNOWLEDGE

    handler = mocker.AsyncMock(side_effect=block_delivery)
    relay = InMemoryRelay()
    sender = relay.connection(identity('sender'))
    recipient = relay.connection(identity('recipient'), delivery_handler=handler)

    receipt = await sender.send(RelayAddress('recipient'), 'automatic')
    assert receipt.recipient == RelayAddress('recipient')
    await entered.wait()
    assert await recipient.pending(retain=True) == ()
    assert await recipient.wait(timeout_seconds=0) is None

    release.set()
    await settle_delivery()
    assert await recipient.pending() == ()


async def test_automatic_deliveries_are_serialized_per_recipient(mocker: MockerFixture) -> None:
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    maximum_active = 0
    contents: list[str] = []

    async def record_delivery(message: RelayMessage) -> RelayDisposition:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        contents.append(message.content)
        if len(contents) == 1:
            first_entered.set()
            await release_first.wait()
        active -= 1
        return RelayDisposition.ACKNOWLEDGE

    handler = mocker.AsyncMock(side_effect=record_delivery)
    relay = InMemoryRelay()
    sender = relay.connection(identity('sender'))
    recipient = relay.connection(identity('recipient'), delivery_handler=handler)

    await sender.send(RelayAddress('recipient'), 'one')
    await sender.send(RelayAddress('recipient'), 'two')
    await first_entered.wait()
    await asyncio.sleep(0)
    assert contents == ['one']

    release_first.set()
    await settle_delivery()

    assert contents == ['one', 'two']
    assert maximum_active == 1
    assert await recipient.pending() == ()


async def test_pending_consumption_or_handler_removal_prevents_scheduled_duplicate_delivery(
    mocker: MockerFixture,
) -> None:
    handler = mocker.AsyncMock(return_value=RelayDisposition.ACKNOWLEDGE)
    relay = InMemoryRelay()
    sender = relay.connection(identity('sender'))
    recipient = relay.connection(identity('recipient'), delivery_handler=handler)

    await sender.send(RelayAddress('recipient'), 'claimed by pending')
    recipient.set_delivery_handler(handler)
    assert [message.content for message in await recipient.pending()] == ['claimed by pending']
    await settle_delivery()
    handler.assert_not_awaited()

    recipient.set_delivery_handler(handler)
    await sender.send(RelayAddress('recipient'), 'retained after removal')
    recipient.set_delivery_handler(None)
    await settle_delivery()
    handler.assert_not_awaited()
    assert [message.content for message in await recipient.pending()] == ['retained after removal']


async def test_closing_recipient_before_scheduled_delivery_skips_handler(mocker: MockerFixture) -> None:
    handler = mocker.AsyncMock(return_value=RelayDisposition.ACKNOWLEDGE)
    relay = InMemoryRelay()
    sender = relay.connection(identity('sender'))
    recipient = relay.connection(identity('recipient'), delivery_handler=handler)

    await sender.send(RelayAddress('recipient'), 'closing')
    recipient.close()
    await settle_delivery()

    handler.assert_not_awaited()
