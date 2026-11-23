import base64
from collections.abc import Sequence

from pydantic_ai.messages import AudioUrl as PydanticAudioUrl
from pydantic_ai.messages import BinaryContent as PydanticBinaryContent
from pydantic_ai.messages import BinaryImage as PydanticBinaryImage
from pydantic_ai.messages import DocumentUrl as PydanticDocumentUrl
from pydantic_ai.messages import ImageUrl as PydanticImageUrl
from pydantic_ai.messages import UserContent as PydanticUserContent
from pydantic_ai.messages import VideoUrl as PydanticVideoUrl

from ovid_core.messages.content import (
    AudioUrl,
    BinaryContent,
    DocumentUrl,
    ImageUrl,
    TextContent,
    UserContent,
    VideoUrl,
)


_URL_TYPES = {
    'image-url': ImageUrl,
    'audio-url': AudioUrl,
    'video-url': VideoUrl,
    'document-url': DocumentUrl,
}
_PYDANTIC_URL_TYPES = {
    'image_url': PydanticImageUrl,
    'audio_url': PydanticAudioUrl,
    'video_url': PydanticVideoUrl,
    'document_url': PydanticDocumentUrl,
}


def user_content_from_pydantic(content: Sequence[PydanticUserContent]) -> tuple[UserContent, ...] | None:
    """Return typed items only when they convert back to items equal to the source."""
    items = tuple(item for item in map(_item_from_pydantic, content) if item is not None)
    if not items or user_content_to_pydantic(items) != list(content):
        return None

    return items


def user_content_to_pydantic(content: Sequence[UserContent]) -> list[PydanticUserContent]:
    return [_item_to_pydantic(item) for item in content]


def binary_content_from_pydantic(value: PydanticBinaryContent) -> BinaryContent:
    return BinaryContent(
        data=base64.b64encode(value.data),
        media_type=value.media_type,
        identifier=value._identifier,
        vendor_metadata=value.vendor_metadata,
    )


def binary_content_to_pydantic(value: BinaryContent) -> PydanticBinaryContent:
    """Build the class that Pydantic AI narrows to, without its computed identifier."""
    binary_type = PydanticBinaryImage if value.media_type.startswith('image/') else PydanticBinaryContent

    return binary_type(
        data=value.data,
        media_type=value.media_type,
        identifier=value.identifier,
        vendor_metadata=value.vendor_metadata,
    )


def _item_from_pydantic(value: PydanticUserContent) -> UserContent | None:
    if isinstance(value, str):
        return TextContent(content=value)
    if isinstance(value, PydanticBinaryContent):
        return binary_content_from_pydantic(value)
    if not isinstance(value, PydanticImageUrl | PydanticAudioUrl | PydanticVideoUrl | PydanticDocumentUrl):
        return None

    return _URL_TYPES[value.kind](
        url=value.url,
        media_type=value._media_type,
        identifier=value._identifier,
        force_download=value.force_download,
        vendor_metadata=value.vendor_metadata,
    )


def _item_to_pydantic(value: UserContent) -> PydanticUserContent:
    if isinstance(value, TextContent):
        return value.content
    if isinstance(value, BinaryContent):
        return binary_content_to_pydantic(value)

    return _PYDANTIC_URL_TYPES[value.kind](
        value.url,
        media_type=value.media_type,
        identifier=value.identifier,
        force_download=value.force_download,
        vendor_metadata=value.vendor_metadata,
    )
