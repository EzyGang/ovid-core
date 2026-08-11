from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ovid_core.relay.models import RelayAddress, RelayContact, RelayIdentity, RelayMessage, RelayMessageId, RelayReceipt


def test_relay_values_validate_and_serialize() -> None:
    sender = RelayAddress('agent-a')
    recipient = RelayAddress('agent-b')
    reply_to = RelayMessageId.new()
    message = RelayMessage(
        id=RelayMessageId.new(),
        sender=sender,
        recipient=recipient,
        content='progress',
        sent_at=datetime.now(UTC),
        reply_to=reply_to,
    )
    receipt = RelayReceipt(message_id=message.id, recipient=recipient, accepted_at=datetime.now(UTC))

    assert str(sender) == 'agent-a'
    assert str(message.id) == str(message.id.root)
    assert RelayIdentity(address=sender, display_name='Agent A').model_dump(mode='json') == {
        'address': 'agent-a',
        'display_name': 'Agent A',
    }
    assert RelayContact(address=recipient, display_name='Agent B').model_dump(mode='json') == {
        'address': 'agent-b',
        'display_name': 'Agent B',
    }
    assert message.model_dump(mode='json')['reply_to'] == str(reply_to)
    assert receipt.model_dump(mode='json')['message_id'] == str(message.id)


def test_relay_values_reject_invalid_addresses_times_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        RelayAddress('')
    with pytest.raises(ValidationError, match='timezone'):
        RelayMessage(
            id=RelayMessageId.new(),
            sender=RelayAddress('sender'),
            recipient=RelayAddress('recipient'),
            content='message',
            sent_at=datetime.now(),
        )
    with pytest.raises(ValidationError, match='Extra inputs'):
        RelayIdentity.model_validate({'address': 'sender', 'display_name': 'Sender', 'status': 'running'})
