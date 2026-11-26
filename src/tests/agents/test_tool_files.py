import base64

from pydantic_ai.messages import BinaryContent as PydanticBinaryContent
from pydantic_ai.messages import DocumentUrl as PydanticDocumentUrl
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart
from pydantic_ai.messages import ToolReturnPart as PydanticToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile

from ovid_core import MessageCodec
from ovid_core.adapters.pydantic_ai import message_to_pydantic
from ovid_core.agents import AgentDefinition
from ovid_core.messages import BinaryContent, DocumentUrl
from ovid_core.routing import ModelRef
from ovid_core.tools import ToolResult
from tests.support.agent_helpers import agent_factory
from tests.support.tool_result_consumer import run_tool


_PNG = b'\x89PNG\r\n\x1a\n\xfb\xff'
_IMAGE = BinaryContent(data=base64.b64encode(_PNG), media_type='image/png')
_DOCUMENT = DocumentUrl(url='https://example.com/spec.pdf', media_type='application/pdf')
_RESULT = ToolResult(content='ui.png', metadata={'k': 1}, files=(_IMAGE, _DOCUMENT))


def _files(part: PydanticToolReturnPart) -> list[tuple[str, bytes | str, str]]:
    """Return the kind, the bytes or URL, and the media type of each file in a tool return part."""
    return [
        ('binary', file.data, file.media_type)
        if isinstance(file, PydanticBinaryContent)
        else (file.kind, file.url, file.media_type)
        for file in part.files
    ]


async def test_tool_files_reach_the_model_in_the_tool_return_and_stream_in_the_upstream_shape() -> None:
    run = await run_tool(_RESULT)

    content = run.stored_return.content
    assert run.result.output == 'Done.'
    assert run.sent_return.model_response_str() == 'ui.png'
    assert run.sent_return.metadata == {'k': 1}
    assert isinstance(run.sent_return.files[1], PydanticDocumentUrl)
    assert _files(run.sent_return) == [
        ('binary', _PNG, 'image/png'),
        ('document-url', 'https://example.com/spec.pdf', 'application/pdf'),
    ]
    assert isinstance(content, list)
    assert content[0] == 'ui.png'
    assert content[1] == {
        'data': base64.urlsafe_b64encode(_PNG).decode(),
        'media_type': 'image/png',
        'vendor_metadata': None,
        'kind': 'binary',
        'identifier': run.sent_return.files[0].identifier,
    }
    assert isinstance(content[2], dict)
    assert content[2]['kind'] == 'document-url'
    assert run.stored_return.content_file_digests is not None
    assert run.stored_return.metadata == {'k': 1}
    assert run.result_event.content == content
    assert run.result_event.metadata == {'k': 1}


async def test_stored_tool_files_survive_a_store_round_trip_and_a_second_run() -> None:
    requests: list[list[ModelMessage]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        requests.append(messages)

        return ModelResponse(parts=(TextPart('Continued.'),))

    run = await run_tool(_RESULT)
    codec = MessageCodec()
    history = tuple(codec.decode(codec.encode(message)) for message in run.result.messages)
    restored = message_to_pydantic(history[2])
    definition = AgentDefinition[None, str](model=ModelRef(name='primary'), deps_type=type(None), output_type=str)
    agent = await agent_factory({'primary': FunctionModel(respond)}).build(definition)

    result = await agent.run('Continue.', deps=None, messages=history)

    sent = requests[0][2]
    expected = [
        ('binary', _PNG, 'image/png'),
        ('document-url', 'https://example.com/spec.pdf', 'application/pdf'),
    ]
    assert history == run.result.messages
    assert isinstance(restored, ModelRequest)
    assert isinstance(restored.parts[0], PydanticToolReturnPart)
    assert _files(restored.parts[0]) == expected
    assert result.output == 'Continued.'
    assert isinstance(sent, ModelRequest)
    assert isinstance(sent.parts[0], PydanticToolReturnPart)
    assert sent.parts[0].model_response_str() == 'ui.png'
    assert sent.parts[0].metadata == {'k': 1}
    assert _files(sent.parts[0]) == expected


async def test_model_without_image_input_gets_the_upstream_note_and_the_run_completes() -> None:
    run = await run_tool(
        ToolResult(content='ui.png', files=(_IMAGE,)), profile=ModelProfile(supports_image_input=False)
    )

    identifier = PydanticBinaryContent(_PNG, media_type='image/png').identifier
    assert run.result.output == 'Done.'
    assert run.sent_return.content == [
        'ui.png',
        f'File {identifier} (image/png, 10 bytes) was not sent: this model cannot read images.',
    ]
    assert run.sent_return.files == []
    assert run.result_event.content == run.stored_return.content
