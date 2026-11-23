from typing import Annotated, Literal

from pydantic import Base64Bytes, Field, JsonValue

from ovid_core.models import BaseModel


class TextContent(BaseModel):
    kind: Literal['text'] = 'text'
    content: str


class FileUrl(BaseModel):
    url: str = Field(min_length=1)
    media_type: str | None = None
    identifier: str | None = None
    force_download: bool | Literal['allow-local'] = False
    vendor_metadata: dict[str, JsonValue] | None = None


class ImageUrl(FileUrl):
    kind: Literal['image_url'] = 'image_url'


class AudioUrl(FileUrl):
    kind: Literal['audio_url'] = 'audio_url'


class VideoUrl(FileUrl):
    kind: Literal['video_url'] = 'video_url'


class DocumentUrl(FileUrl):
    kind: Literal['document_url'] = 'document_url'


class BinaryContent(BaseModel):
    kind: Literal['binary'] = 'binary'
    data: Base64Bytes
    media_type: str = Field(min_length=1)
    identifier: str | None = None
    vendor_metadata: dict[str, JsonValue] | None = None


UserContent = Annotated[
    TextContent | ImageUrl | AudioUrl | VideoUrl | DocumentUrl | BinaryContent,
    Field(discriminator='kind'),
]
