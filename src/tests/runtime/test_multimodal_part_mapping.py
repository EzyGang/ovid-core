import base64
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pydantic_ai.messages import BinaryContent as PydanticBinaryContent
from pydantic_ai.messages import BinaryImage, CachePoint, ModelRequest, UploadedFile, UserContent
from pydantic_ai.messages import DocumentUrl as PydanticDocumentUrl
from pydantic_ai.messages import ImageUrl as PydanticImageUrl
from pydantic_ai.messages import TextContent as PydanticTextContent
from pydantic_ai.messages import ToolReturnPart as PydanticToolReturnPart
from pydantic_ai.messages import UserPromptPart as PydanticUserPromptPart

from ovid_core import MessageCodec
from ovid_core.adapters.pydantic_ai import message_from_pydantic, message_to_pydantic
from ovid_core.messages import (
    BinaryContent,
    DocumentUrl,
    ImageUrl,
    OpaqueRequestPart,
    TextContent,
    ToolReturnPart,
    UserPromptPart,
)


_PNG = b'\x89PNG\xfb\xff'


def test_user_content_keeps_explicit_values_and_stores_no_computed_value() -> None:
    upstream = PydanticUserPromptPart(
        [
            'Describe these.',
            PydanticImageUrl('https://example.com/no-extension', identifier='chart'),
            PydanticDocumentUrl('https://example.com/report', media_type='application/pdf'),
            BinaryImage(_PNG, media_type='image/png'),
        ]
    )

    normalized = message_from_pydantic(ModelRequest(parts=(upstream,)))
    (restored,) = message_to_pydantic(normalized).parts

    assert normalized.parts == (
        UserPromptPart(
            content=(
                TextContent(content='Describe these.'),
                ImageUrl(url='https://example.com/no-extension', identifier='chart'),
                DocumentUrl(url='https://example.com/report', media_type='application/pdf'),
                BinaryContent(data=base64.b64encode(_PNG), media_type='image/png'),
            )
        ),
    )
    assert isinstance(restored, PydanticUserPromptPart)
    assert restored.content == upstream.content
    assert isinstance(restored.content[1], PydanticImageUrl)
    assert restored.content[1].identifier == 'chart'
    assert isinstance(restored.content[2], PydanticDocumentUrl)
    assert restored.content[2].media_type == 'application/pdf'


@pytest.mark.parametrize(
    'content',
    (
        ['Describe this.', PydanticImageUrl('https://example.com/a.png'), CachePoint()],
        [PydanticTextContent('Describe this.')],
        [UploadedFile('file-1', 'openai')],
        [PydanticBinaryContent(_PNG, media_type='image/png')],
        [],
    ),
    ids=('cache-point', 'text-content', 'uploaded-file', 'image-not-narrowed', 'empty'),
)
def test_user_content_without_an_exact_typed_form_stays_opaque(content: list[UserContent]) -> None:
    upstream = PydanticUserPromptPart(content)
    codec = MessageCodec()

    normalized = message_from_pydantic(ModelRequest(parts=(upstream,)))
    (restored,) = message_to_pydantic(codec.decode(codec.encode(normalized))).parts

    assert [type(part) for part in normalized.parts] == [OpaqueRequestPart]
    assert isinstance(restored, PydanticUserPromptPart)
    assert len(restored.content) == len(content)


def test_tool_return_with_files_keeps_json_content_and_restores_the_files() -> None:
    image = BinaryImage(_PNG, media_type='image/png')
    chart = PydanticImageUrl('https://example.com/chart.png')
    upstream = PydanticToolReturnPart('render', [image, {'chart': chart}, 'caption'], 'call-1')
    codec = MessageCodec()

    normalized = message_from_pydantic(ModelRequest(parts=(upstream,)))
    (part,) = normalized.parts
    (restored,) = message_to_pydantic(codec.decode(codec.encode(normalized))).parts

    assert isinstance(part, ToolReturnPart)
    assert part.content == [
        {
            'kind': 'binary',
            'data': base64.urlsafe_b64encode(_PNG).decode(),
            'media_type': 'image/png',
            'identifier': image.identifier,
            'vendor_metadata': None,
        },
        {
            'chart': {
                'kind': 'image-url',
                'url': 'https://example.com/chart.png',
                'media_type': 'image/png',
                'identifier': chart.identifier,
                'force_download': False,
                'vendor_metadata': None,
            }
        },
        'caption',
    ]
    assert part.content_file_digests is not None
    assert len(part.content_file_digests) == 1
    assert replace(restored, timestamp=upstream.timestamp) == upstream


@pytest.mark.parametrize(
    ('content', 'restored_content'),
    (
        ({'kind': 'binary', 'data': 'aGk=', 'media_type': 'text/plain'}, None),
        (({'found': True}, datetime(2026, 1, 2, tzinfo=UTC)), [{'found': True}, '2026-01-02T00:00:00Z']),
    ),
    ids=('json-that-resembles-a-file', 'python-values'),
)
def test_tool_return_without_files_stays_plain_data(content: object, restored_content: object) -> None:
    upstream = PydanticToolReturnPart('lookup', content, 'call-1')

    normalized = message_from_pydantic(ModelRequest(parts=(upstream,)))
    (restored,) = message_to_pydantic(normalized).parts

    assert isinstance(restored, PydanticToolReturnPart)
    assert restored.content == (content if restored_content is None else restored_content)
