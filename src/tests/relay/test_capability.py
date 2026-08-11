from datetime import UTC, datetime
from typing import cast

import pytest
from pydantic import ValidationError
from pytest_mock import MockerFixture

from ovid_core.relay.capability import RelayCapability
from ovid_core.relay.contracts import RelayConnection
from ovid_core.relay.models import RelayAddress, RelayContact, RelayMessage, RelayMessageId, RelayReceipt
from ovid_core.relay.tools import (
    RelayContactsArguments,
    RelayPendingArguments,
    RelaySendArguments,
    RelayToolDescriptions,
    RelayWaitArguments,
)
from ovid_core.tools.base import ToolExecutionContext


def connection_double(mocker: MockerFixture) -> RelayConnection:
    return cast(RelayConnection, mocker.Mock(spec=RelayConnection))


def test_capability_is_fixed_opt_in_and_contributes_four_typed_tools(mocker: MockerFixture) -> None:
    connection = connection_double(mocker)

    capability = RelayCapability[None](connection=connection)

    assert capability.id == 'relay'
    assert capability.defer_loading is False
    assert [tool.id for tool in capability.contributions.tools] == [
        'relay_send',
        'relay_wait',
        'relay_pending',
        'relay_contacts',
    ]
    assert all(tool.description for tool in capability.contributions.tools)
    assert capability.contributions.instructions == ()
    assert capability.contributions.toolsets == ()


def test_capability_overrides_tool_descriptions(mocker: MockerFixture) -> None:
    descriptions = RelayToolDescriptions(
        send='Custom send',
        wait='Custom wait',
        pending='Custom pending',
        contacts='Custom contacts',
    )

    capability = RelayCapability[None](
        connection=connection_double(mocker),
        tool_descriptions=descriptions,
    )

    assert [tool.description for tool in capability.contributions.tools] == [
        'Custom send',
        'Custom wait',
        'Custom pending',
        'Custom contacts',
    ]
    with pytest.raises(ValidationError):
        RelayToolDescriptions(send='')


async def test_relay_tools_delegate_bound_connection_and_return_typed_results(mocker: MockerFixture) -> None:
    connection = connection_double(mocker)
    recipient = RelayAddress('recipient')
    message_id = RelayMessageId.new()
    now = datetime.now(UTC)
    receipt = RelayReceipt(message_id=message_id, recipient=recipient, accepted_at=now)
    message = RelayMessage(
        id=message_id,
        sender=RelayAddress('sender'),
        recipient=recipient,
        content='hello',
        sent_at=now,
    )
    contact = RelayContact(address=RelayAddress('contact'), display_name='Contact')
    connection.send.return_value = receipt
    connection.wait.return_value = message
    connection.pending.return_value = (message,)
    connection.contacts.return_value = (contact,)
    send_tool, wait_tool, pending_tool, contacts_tool = RelayCapability[None](connection=connection).contributions.tools
    context = cast(ToolExecutionContext[None], mocker.Mock())

    send_result = await send_tool.execute(
        context,
        RelaySendArguments(to=recipient, message='hello', reply_to=message_id),
    )
    wait_result = await wait_tool.execute(
        context,
        RelayWaitArguments(sender=RelayAddress('sender'), reply_to=message_id, timeout_seconds=2),
    )
    pending_result = await pending_tool.execute(context, RelayPendingArguments(retain=True))
    contacts_result = await contacts_tool.execute(context, RelayContactsArguments())

    connection.send.assert_awaited_once_with(recipient=recipient, content='hello', reply_to=message_id)
    connection.wait.assert_awaited_once_with(
        sender=RelayAddress('sender'),
        reply_to=message_id,
        timeout_seconds=2,
    )
    connection.pending.assert_awaited_once_with(retain=True)
    connection.contacts.assert_awaited_once_with()
    assert send_result.receipt == receipt
    assert send_result.content == receipt.model_dump(mode='json')
    assert wait_result.message == message
    assert wait_result.content == message.model_dump(mode='json')
    assert pending_result.messages == (message,)
    assert pending_result.content == [message.model_dump(mode='json')]
    assert contacts_result.contacts == (contact,)
    assert contacts_result.content == [contact.model_dump(mode='json')]


async def test_wait_tool_serializes_no_message_result(mocker: MockerFixture) -> None:
    connection = connection_double(mocker)
    connection.wait.return_value = None
    wait_tool = RelayCapability[None](connection=connection).contributions.tools[1]
    context = cast(ToolExecutionContext[None], mocker.Mock())

    result = await wait_tool.execute(context, RelayWaitArguments(timeout_seconds=0))

    assert result.message is None
    assert result.content is None
