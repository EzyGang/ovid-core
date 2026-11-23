from dataclasses import replace

import pytest
from pydantic import JsonValue
from pydantic_ai.messages import (
    AgentInstructionSource,
    CapabilityInstructionSource,
    CapabilityLoadDeltaPart,
    InstructionBaselinePart,
    InstructionDeltaPart,
    InstructionDigest,
    InstructionId,
    InstructionPart,
    LoadCapabilityReturnPart,
    ModelRequest,
    ModelRequestPart,
    RetryFeedbackPart,
    RetryInfo,
    ToolAvailabilityDeltaPart,
)
from pydantic_ai.messages import CompactionPart as PydanticCompactionPart
from pydantic_ai.messages import SystemPromptPart as PydanticSystemPromptPart
from pydantic_ai.messages import ToolReturnPart as PydanticToolReturnPart

from ovid_core import MessageCodec
from ovid_core.adapters.pydantic_ai import message_from_pydantic, message_to_pydantic
from ovid_core.messages import AgentMessage, CapabilityLoadReturnPart, OpaqueRequestPart


_CAPABILITY_BLOCK = InstructionId(CapabilityInstructionSource('notes'), name='rules')


@pytest.mark.parametrize(
    'part',
    (
        InstructionBaselinePart(
            instructions=[
                InstructionPart('Rules v1.', dynamic=True, on_change='append', name='rules', id=_CAPABILITY_BLOCK),
                InstructionDigest(id=InstructionId(AgentInstructionSource()), content_hash='digest'),
            ]
        ),
        InstructionDeltaPart(id=_CAPABILITY_BLOCK, content='Rules v2.', base_hash='digest'),
        CapabilityLoadDeltaPart(loaded=['notes'], tool_call_id='call-1'),
        ToolAvailabilityDeltaPart(tools_added=['lookup'], tool_call_id='call-1'),
        RetryFeedbackPart(
            content=[{'type': 'missing', 'loc': ('value',), 'msg': 'Field required', 'input': {}}],
            retry=RetryInfo(cause='validation_error', attempt=1, max_retries=2),
        ),
        PydanticSystemPromptPart('Remaining budget: 3.', scope='request'),
        PydanticCompactionPart('Summary.', metadata={'writer': 'application'}),
    ),
)
def test_unmodelled_request_parts_round_trip_losslessly_through_storage(part: ModelRequestPart) -> None:
    codec = MessageCodec()

    normalized = message_from_pydantic(ModelRequest(parts=(part,)))
    stored = codec.decode(codec.encode(normalized))
    (restored,) = message_to_pydantic(stored).parts

    assert [(type(item), item.part_kind) for item in normalized.parts] == [(OpaqueRequestPart, part.part_kind)]
    assert restored == part


@pytest.mark.parametrize(
    ('stored', 'expected'),
    (
        (
            {'kind': 'retry_prompt', 'content': 'Try again.', 'tool_name': 'lookup', 'tool_call_id': 'call-1'},
            PydanticToolReturnPart('lookup', 'Try again.', 'call-1', outcome='retried'),
        ),
        (
            {'kind': 'retry_prompt', 'content': 'Try again.', 'tool_call_id': 'call-1'},
            RetryFeedbackPart('Try again.', retry=RetryInfo(cause='model_retry')),
        ),
    ),
)
def test_stored_retry_prompt_converts_to_the_current_retry_part(
    stored: dict[str, JsonValue],
    expected: PydanticToolReturnPart | RetryFeedbackPart,
) -> None:
    message = AgentMessage.model_validate({'role': 'request', 'parts': [stored]})

    (restored,) = message_to_pydantic(message).parts

    assert replace(restored, timestamp=expected.timestamp) == expected


def test_capability_load_return_maps_message_and_stored_instructions() -> None:
    upstream = ModelRequest(
        parts=(LoadCapabilityReturnPart(content={'message': "Capability 'notes' loaded."}, tool_call_id='load-1'),)
    )
    stored = AgentMessage.model_validate(
        {
            'role': 'request',
            'parts': [{'kind': 'capability_load_return', 'instructions': 'Keep notes.', 'tool_call_id': 'load-0'}],
        }
    )

    normalized = message_from_pydantic(upstream)
    (restored,) = message_to_pydantic(normalized).parts
    (legacy,) = message_to_pydantic(stored).parts

    assert normalized.parts == (CapabilityLoadReturnPart(message="Capability 'notes' loaded.", tool_call_id='load-1'),)
    assert isinstance(restored, LoadCapabilityReturnPart)
    assert restored.content == {'message': "Capability 'notes' loaded."}
    assert isinstance(legacy, LoadCapabilityReturnPart)
    assert legacy.content == {'instructions': 'Keep notes.'}
