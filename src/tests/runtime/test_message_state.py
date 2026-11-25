from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from pydantic_ai import messages as upstream

from ovid_core import MessageCodec
from ovid_core.adapters.pydantic_ai import message_from_pydantic, message_to_pydantic
from ovid_core.messages import AgentMessage, UserPromptPart
from tests.support.helpers import CONVERSATION_ID, RUN_ID


_TIMESTAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    'message',
    [
        *(upstream.ModelRequest(parts=(), timestamp=_TIMESTAMP, state=state) for state in ('complete', 'interrupted')),
        *(
            upstream.ModelResponse(parts=(upstream.TextPart('Partial'),), timestamp=_TIMESTAMP, state=state)
            for state in ('complete', 'interrupted', 'incomplete', 'suspended')
        ),
    ],
)
def test_message_state_round_trips_through_upstream_messages_and_codec(message: upstream.ModelMessage) -> None:
    codec = MessageCodec()

    normalized = message_from_pydantic(message)
    restored = codec.decode(codec.encode(normalized))

    assert normalized.state == message.state
    assert restored == normalized
    assert message_to_pydantic(restored) == message


def test_request_state_is_limited_and_records_without_state_decode_as_complete() -> None:
    record = (
        f'{{"role":"request","parts":[{{"kind":"user_prompt","content":"Go."}}],'
        f'"run_id":"{RUN_ID}","conversation_id":"{CONVERSATION_ID}","timestamp":"2026-01-02T03:04:05Z"}}'
    )

    decoded = MessageCodec().decode(record.encode())

    assert decoded.state == 'complete'
    assert decoded.parts == (UserPromptPart(content='Go.'),)
    for state in ('incomplete', 'suspended'):
        with pytest.raises(ValidationError, match='request messages can only be complete or interrupted'):
            AgentMessage(role='request', parts=(), state=state)
