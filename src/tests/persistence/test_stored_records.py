import json

from pydantic import JsonValue
from pydantic_ai.messages import ImageUrl as PydanticImageUrl
from pydantic_ai.messages import LoadCapabilityCallPart, LoadCapabilityReturnPart, ModelMessage, ModelResponse
from pydantic_ai.messages import SystemPromptPart as PydanticSystemPromptPart
from pydantic_ai.messages import TextPart as PydanticTextPart
from pydantic_ai.messages import ToolCallPart as PydanticToolCallPart
from pydantic_ai.messages import ToolReturnPart as PydanticToolReturnPart
from pydantic_ai.messages import UserPromptPart as PydanticUserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ovid_core import MessageCodec
from ovid_core.agents import AgentDefinition
from ovid_core.messages import ImageUrl, TextContent, ToolReturnPart, UserPromptPart
from ovid_core.routing import ModelRef
from tests.support.agent_helpers import agent_factory
from tests.support.helpers import CONVERSATION_ID, RUN_ID


_USAGE: dict[str, JsonValue] = {
    'input_tokens': 8,
    'output_tokens': 3,
    'cache_read_tokens': 0,
    'cache_write_tokens': 0,
    'input_audio_tokens': 0,
    'output_audio_tokens': 0,
    'cache_audio_read_tokens': 0,
    'provider_details': {},
}


def _record(role: str, parts: list[JsonValue], **fields: JsonValue) -> bytes:
    """Encode a message in the record shape that Ovid Core wrote before it had typed multimodal content."""
    record: dict[str, JsonValue] = {
        'role': role,
        'parts': parts,
        'run_id': str(RUN_ID),
        'conversation_id': str(CONVERSATION_ID),
        'timestamp': '2026-01-02T03:04:05Z',
        'request_usage': _USAGE if role == 'response' else None,
        'instructions': None,
        'model_name': 'stored-model' if role == 'response' else None,
        'provider_name': None,
        'provider_response_id': None,
        'provider_details': None,
        'finish_reason': None,
        **fields,
    }

    return json.dumps(record).encode()


_STORED_RECORDS = (
    _record(
        'request',
        [{'kind': 'system_prompt', 'content': 'Be brief.'}, {'kind': 'user_prompt', 'content': 'Find it.'}],
        instructions='Use tools.',
    ),
    _record(
        'response',
        [
            {'tool_name': 'lookup', 'arguments': {'key': 1}, 'tool_call_id': 'call-1', 'kind': 'tool_call'},
            {'kind': 'capability_load_call', 'capability_id': 'notes', 'tool_call_id': 'load-1'},
        ],
        finish_reason='tool_call',
    ),
    _record(
        'request',
        [
            {
                'tool_name': 'lookup',
                'content': {'found': True},
                'tool_call_id': 'call-1',
                'outcome': 'success',
                'kind': 'tool_return',
            },
            {
                'kind': 'capability_load_return',
                'message': 'Loaded.',
                'instructions': None,
                'tool_call_id': 'load-1',
                'outcome': 'success',
            },
        ],
    ),
    _record('response', [{'kind': 'text', 'content': 'Found.'}]),
    _record(
        'request',
        [
            {
                'kind': 'opaque_request',
                'part_kind': 'user-prompt',
                'payload': {
                    'content': [
                        'Describe this.',
                        {
                            'url': 'https://example.com/a.png',
                            'force_download': False,
                            'vendor_metadata': None,
                            'kind': 'image-url',
                            'media_type': 'image/png',
                            'identifier': 'b86daf',
                        },
                    ],
                    'timestamp': '2026-01-02T03:04:05Z',
                    'part_kind': 'user-prompt',
                },
            }
        ],
    ),
    _record('response', [{'kind': 'text', 'content': 'A chart.'}]),
)


async def test_records_stored_before_typed_multimodal_content_decode_and_continue_a_run() -> None:
    requests: list[list[ModelMessage]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        requests.append(messages)

        return ModelResponse(parts=(PydanticTextPart('Continued.'),))

    codec = MessageCodec()
    definition = AgentDefinition[None, str](model=ModelRef(name='primary'), deps_type=type(None), output_type=str)
    agent = await agent_factory({'primary': FunctionModel(respond)}).build(definition)

    history = tuple(codec.decode(record) for record in _STORED_RECORDS)
    result = await agent.run('Continue.', deps=None, messages=history)

    (sent,) = requests
    stored_parts = [type(part) for message in sent[:6] for part in message.parts]
    multimodal_prompt = sent[4].parts[0]

    assert result.output == 'Continued.'
    assert len(result.history) == 8
    assert stored_parts == [
        PydanticSystemPromptPart,
        PydanticUserPromptPart,
        PydanticToolCallPart,
        LoadCapabilityCallPart,
        PydanticToolReturnPart,
        LoadCapabilityReturnPart,
        PydanticTextPart,
        PydanticUserPromptPart,
        PydanticTextPart,
    ]
    assert isinstance(multimodal_prompt, PydanticUserPromptPart)
    assert multimodal_prompt.content == ['Describe this.', PydanticImageUrl('https://example.com/a.png')]
    assert result.history[4].parts == (
        UserPromptPart(
            content=(
                TextContent(content='Describe this.'),
                ImageUrl(url='https://example.com/a.png', media_type='image/png', identifier='b86daf'),
            )
        ),
    )


async def test_tool_return_stored_with_the_old_content_and_metadata_envelope_decodes_and_continues_a_run() -> None:
    requests: list[list[ModelMessage]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        requests.append(messages)

        return ModelResponse(parts=(PydanticTextPart('Continued.'),))

    envelope: dict[str, JsonValue] = {'content': 'a\nb', 'metadata': {'k': 1}}
    records = (
        _record('response', [{'tool_name': 'report', 'arguments': {}, 'tool_call_id': 'call-1', 'kind': 'tool_call'}]),
        _record(
            'request',
            [{'tool_name': 'report', 'content': envelope, 'tool_call_id': 'call-1', 'kind': 'tool_return'}],
        ),
        _record('response', [{'kind': 'text', 'content': 'Reported.'}]),
    )
    codec = MessageCodec()
    definition = AgentDefinition[None, str](model=ModelRef(name='primary'), deps_type=type(None), output_type=str)
    agent = await agent_factory({'primary': FunctionModel(respond)}).build(definition)

    history = tuple(codec.decode(record) for record in records)
    result = await agent.run('Continue.', deps=None, messages=history)

    sent = requests[0][1].parts[0]
    assert history[1].parts == (ToolReturnPart(tool_name='report', content=envelope, tool_call_id='call-1'),)
    assert result.output == 'Continued.'
    assert isinstance(sent, PydanticToolReturnPart)
    assert sent.content == envelope
    assert sent.metadata is None
