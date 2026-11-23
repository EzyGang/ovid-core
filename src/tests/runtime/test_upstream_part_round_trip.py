from dataclasses import replace
from typing import get_args

import pytest
from pydantic_ai import messages as upstream
from pydantic_ai.usage import RequestUsage as PydanticRequestUsage

from ovid_core import MessageCodec
from ovid_core.adapters.pydantic_ai import message_from_pydantic, message_to_pydantic
from ovid_core.messages import (
    FilePart,
    NativeToolCallPart,
    NativeToolReturnPart,
    OpaqueRequestPart,
    OpaqueResponsePart,
    UserPromptPart,
)


_IMAGE = upstream.BinaryImage(b'\x89PNG\xfb\xff', media_type='image/png')
_SPEECH = upstream.SpeechPart(
    speaker='assistant',
    transcript='Hello.',
    audio=upstream.BinaryContent(b'\x00\x01', media_type='audio/wav'),
    timestamps=[upstream.SpeechTimestamp(text='Hello.', start_ms=0, duration_ms=400)],
)
_TOOL_SEARCH_CONTENT: upstream.ToolSearchReturnContent = {'discovered_tools': [{'name': 'lookup'}]}
_INSTRUCTION_ID = upstream.InstructionId(upstream.CapabilityInstructionSource('notes'), name='rules')
with pytest.warns(upstream.PydanticAIDeprecationWarning):
    _LEGACY_RETRY = upstream.RetryPromptPart('Try again.', tool_name='lookup')

# Each upstream member maps to one sample and to the Ovid part type that must restore an equal upstream part.
# `None` marks a typed part that Ovid Core maps with documented field loss.
_REQUEST_SAMPLES: dict[type, tuple[upstream.ModelRequestPart, type | None]] = {
    upstream.SystemPromptPart: (upstream.SystemPromptPart('Be brief.'), None),
    upstream.UserPromptPart: (
        upstream.UserPromptPart(
            [
                'Describe these.',
                upstream.ImageUrl('https://example.com/no-extension', identifier='chart'),
                upstream.AudioUrl('https://example.com/a.mp3', force_download=True),
                upstream.VideoUrl('https://example.com/a.mp4', vendor_metadata={'fps': 1}),
                upstream.DocumentUrl('https://example.com/a', media_type='application/pdf'),
                _IMAGE,
            ]
        ),
        UserPromptPart,
    ),
    upstream.SpeechPart: (replace(_SPEECH, speaker='user'), OpaqueRequestPart),
    upstream.ToolSearchReturnPart: (upstream.ToolSearchReturnPart(content=_TOOL_SEARCH_CONTENT), None),
    upstream.LoadCapabilityReturnPart: (upstream.LoadCapabilityReturnPart(content={'message': 'Loaded.'}), None),
    upstream.ToolReturnPart: (upstream.ToolReturnPart('lookup', {'found': True}, 'call-1'), None),
    upstream.RetryPromptPart: (_LEGACY_RETRY, None),
    upstream.RetryFeedbackPart: (
        upstream.RetryFeedbackPart('Try again.', retry=upstream.RetryInfo(cause='model_retry')),
        OpaqueRequestPart,
    ),
    upstream.ToolAvailabilityDeltaPart: (upstream.ToolAvailabilityDeltaPart(tools_added=['lookup']), OpaqueRequestPart),
    upstream.CapabilityLoadDeltaPart: (upstream.CapabilityLoadDeltaPart(loaded=['notes']), OpaqueRequestPart),
    upstream.ModelSettingsDeltaPart: (
        upstream.ModelSettingsDeltaPart(settings={'thinking': 'high'}),
        OpaqueRequestPart,
    ),
    upstream.InstructionDeltaPart: (
        upstream.InstructionDeltaPart(id=_INSTRUCTION_ID, content='Rules v2.'),
        OpaqueRequestPart,
    ),
    upstream.InstructionBaselinePart: (
        upstream.InstructionBaselinePart(
            instructions=[upstream.InstructionDigest(id=_INSTRUCTION_ID, content_hash='digest')]
        ),
        OpaqueRequestPart,
    ),
    upstream.CompactionPart: (upstream.CompactionPart('Summary.'), None),
    upstream.CapabilityPart: (
        upstream.UnknownCapabilityPart(
            source=upstream.OperatorSource(),
            content='Remaining budget: 3.',
            kind='budget',
            data={'remaining': 3},
            rendering=upstream.CapabilityPartRendering(tag='budget'),
        ),
        OpaqueRequestPart,
    ),
}
_RESPONSE_SAMPLES: dict[type, tuple[upstream.ModelResponsePart, type | None]] = {
    upstream.TextPart: (upstream.TextPart('Answer.'), None),
    upstream.ToolSearchCallPart: (upstream.ToolSearchCallPart(args={'queries': ['lookup']}), None),
    upstream.LoadCapabilityCallPart: (upstream.LoadCapabilityCallPart(args={'id': 'notes'}), None),
    upstream.ToolCallPart: (upstream.ToolCallPart('lookup', {'key': 1}, 'call-1'), None),
    upstream.NativeToolSearchCallPart: (
        upstream.NativeToolSearchCallPart(args={'queries': ['lookup']}, provider_name='anthropic'),
        NativeToolCallPart,
    ),
    upstream.NativeToolCallPart: (
        upstream.NativeToolCallPart(
            'web_search',
            {'query': 'ovid'},
            'native-1',
            id='item-1',
            provider_name='openai',
            provider_details={'status': 'completed'},
        ),
        NativeToolCallPart,
    ),
    upstream.NativeToolSearchReturnPart: (
        upstream.NativeToolSearchReturnPart(content=_TOOL_SEARCH_CONTENT, provider_name='anthropic'),
        NativeToolReturnPart,
    ),
    upstream.NativeToolReturnPart: (
        upstream.NativeToolReturnPart(
            'image_generation',
            [{'status': 'completed'}, _IMAGE, upstream.ImageUrl('https://example.com/a.png')],
            'native-1',
            tool_kind='custom',
            metadata={'cost': 1},
            outcome='failed',
            provider_name='openai',
            provider_details={'status': 'completed'},
        ),
        NativeToolReturnPart,
    ),
    upstream.ThinkingPart: (upstream.ThinkingPart('Private.', signature='signature'), None),
    upstream.CompactionPart: (upstream.CompactionPart(id='compact-1', provider_name='openai'), None),
    upstream.FilePart: (
        upstream.FilePart(_IMAGE, id='file-1', provider_name='openai', provider_details={'quality': 'high'}),
        FilePart,
    ),
    upstream.SpeechPart: (_SPEECH, OpaqueResponsePart),
}


_MESSAGES: tuple[tuple[upstream.ModelMessage, type | None], ...] = (
    *((upstream.ModelRequest(parts=(part,)), exact_type) for part, exact_type in _REQUEST_SAMPLES.values()),
    *(
        (
            upstream.ModelResponse(parts=(part,), usage=PydanticRequestUsage(input_tokens=1, output_tokens=1)),
            exact_type,
        )
        for part, exact_type in _RESPONSE_SAMPLES.values()
    ),
)


def _part_id(value: object) -> str | None:
    return type(value.parts[0]).__name__ if isinstance(value, upstream.ModelRequest | upstream.ModelResponse) else None


def _union_members(union: object) -> set[type]:
    return {get_args(member)[0] for member in get_args(get_args(union)[0])}


def test_samples_cover_every_upstream_part() -> None:
    assert set(_REQUEST_SAMPLES) == _union_members(upstream.ModelRequestPart)
    assert set(_RESPONSE_SAMPLES) == _union_members(upstream.ModelResponsePart)


@pytest.mark.parametrize(
    'message',
    tuple(message for message, exact_type in _MESSAGES if exact_type is None),
    ids=_part_id,
)
def test_lossy_upstream_parts_convert_to_a_stored_record_and_back(message: upstream.ModelMessage) -> None:
    codec = MessageCodec()

    normalized = message_from_pydantic(message)
    restored = message_to_pydantic(codec.decode(codec.encode(normalized)))

    assert type(restored) is type(message)
    assert len(restored.parts) == 1


@pytest.mark.filterwarnings('ignore:Unknown capability part kind')
@pytest.mark.parametrize(
    ('message', 'exact_type'),
    tuple(case for case in _MESSAGES if case[1] is not None),
    ids=_part_id,
)
def test_lossless_upstream_parts_restore_an_equal_part(message: upstream.ModelMessage, exact_type: type) -> None:
    codec = MessageCodec()
    (part,) = message.parts

    normalized = message_from_pydantic(message)
    (restored,) = message_to_pydantic(codec.decode(codec.encode(normalized))).parts

    assert type(normalized.parts[0]) is exact_type
    assert type(restored) is type(part)
    assert (replace(restored, timestamp=part.timestamp) if hasattr(part, 'timestamp') else restored) == part


@pytest.mark.parametrize(
    'message',
    (
        upstream.ModelResponse(parts=(upstream.LoadCapabilityCallPart(args=None, tool_call_id='load-1'),)),
        upstream.ModelResponse(parts=(upstream.ToolCallPart('', {'key': 1}, 'call-1'),)),
        upstream.ModelResponse(parts=(upstream.NativeToolReturnPart('', 'done', 'native-1'),)),
        upstream.ModelRequest(parts=(upstream.ToolReturnPart('', 'done', 'call-1'),)),
        upstream.ModelRequest(parts=(upstream.UserPromptPart([upstream.BinaryContent(b'data', media_type='')]),)),
    ),
    ids=('load-call-without-id', 'tool-call-without-name', 'native-return-without-name', 'return-without-name', 'file'),
)
def test_part_values_that_no_typed_part_accepts_stay_opaque_and_restore_an_equal_part(
    message: upstream.ModelMessage,
) -> None:
    codec = MessageCodec()

    normalized = message_from_pydantic(message)
    restored = message_to_pydantic(codec.decode(codec.encode(normalized)))

    assert [type(part) for part in normalized.parts] in ([OpaqueRequestPart], [OpaqueResponsePart])
    assert list(restored.parts) == list(message.parts)
