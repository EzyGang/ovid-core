from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ovid_core.capabilities.base import BaseCapability, CapabilityContributions
from ovid_core.models import BaseModel


type ThinkingEffort = bool | Literal['minimal', 'low', 'medium', 'high', 'xhigh']


class ThinkingCapabilityConfig(BaseModel):
    kind: Literal['thinking'] = 'thinking'
    effort: ThinkingEffort = True


class WebSearchCapabilityConfig(BaseModel):
    kind: Literal['web_search'] = 'web_search'
    search_context_size: Literal['low', 'medium', 'high'] | None = None
    allowed_domains: tuple[str, ...] | None = None
    blocked_domains: tuple[str, ...] | None = None
    max_uses: int | None = Field(default=None, ge=1)
    external_web_access: bool | None = None


class WebFetchCapabilityConfig(BaseModel):
    kind: Literal['web_fetch'] = 'web_fetch'
    allowed_domains: tuple[str, ...] | None = None
    blocked_domains: tuple[str, ...] | None = None
    max_uses: int | None = Field(default=None, ge=1)
    enable_citations: bool | None = None
    max_content_tokens: int | None = Field(default=None, ge=1)


class ImageGenerationCapabilityConfig(BaseModel):
    kind: Literal['image_generation'] = 'image_generation'
    action: Literal['generate', 'edit', 'auto'] | None = None
    output_format: Literal['png', 'webp', 'jpeg'] | None = None
    quality: Literal['low', 'medium', 'high', 'auto'] | None = None
    size: Literal['auto', '1024x1024', '1024x1536', '1536x1024', '512', '1K', '2K', '4K'] | None = None


class XSearchCapabilityConfig(BaseModel):
    kind: Literal['x_search'] = 'x_search'
    allowed_x_handles: tuple[str, ...] | None = None
    excluded_x_handles: tuple[str, ...] | None = None
    from_date: datetime | None = None
    to_date: datetime | None = None
    enable_image_understanding: bool | None = None
    enable_video_understanding: bool | None = None
    include_output: bool | None = None


class ToolSearchCapabilityConfig(BaseModel):
    kind: Literal['tool_search'] = 'tool_search'
    strategy: Literal['keywords', 'bm25', 'regex'] | None = None
    max_results: int = Field(default=10, ge=1)


class OpenAICompactionCapabilityConfig(BaseModel):
    kind: Literal['openai_compaction'] = 'openai_compaction'
    stateless: bool | None = None
    token_threshold: int | None = Field(default=None, ge=1)
    message_count_threshold: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def validate_mode(self) -> Self:
        if self.stateless is True and self.token_threshold is not None:
            raise ValueError('stateless OpenAI compaction cannot use token_threshold')
        if self.stateless is False and self.message_count_threshold is not None:
            raise ValueError('stateful OpenAI compaction cannot use message_count_threshold')
        if self.stateless is True and self.message_count_threshold is None:
            raise ValueError('stateless OpenAI compaction requires message_count_threshold')

        return self


class AnthropicCompactionCapabilityConfig(BaseModel):
    kind: Literal['anthropic_compaction'] = 'anthropic_compaction'
    token_threshold: int = Field(default=150_000, ge=50_000)
    instructions: str | None = None
    pause_after_compaction: bool = False


type ProviderCapabilityConfig = Annotated[
    ThinkingCapabilityConfig
    | WebSearchCapabilityConfig
    | WebFetchCapabilityConfig
    | ImageGenerationCapabilityConfig
    | XSearchCapabilityConfig
    | ToolSearchCapabilityConfig
    | OpenAICompactionCapabilityConfig
    | AnthropicCompactionCapabilityConfig,
    Field(discriminator='kind'),
]


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderCapability[Deps](BaseCapability[Deps]):
    config: ProviderCapabilityConfig
    contributions: CapabilityContributions[Deps] = field(
        default=CapabilityContributions(),
        init=False,
        repr=False,
    )
