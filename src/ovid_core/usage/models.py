from collections.abc import Iterable
from typing import Self

from pydantic import Field, NonNegativeInt

from ovid_core.models import BaseModel


type ProviderUsageDetails = dict[str, dict[str, NonNegativeInt]]


class RequestUsage(BaseModel):
    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    cache_read_tokens: NonNegativeInt = 0
    cache_write_tokens: NonNegativeInt = 0
    input_audio_tokens: NonNegativeInt = 0
    output_audio_tokens: NonNegativeInt = 0
    cache_audio_read_tokens: NonNegativeInt = 0
    provider_details: ProviderUsageDetails = Field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class Usage(BaseModel):
    request_count: NonNegativeInt = 0
    tool_calls: NonNegativeInt = 0
    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    cache_read_tokens: NonNegativeInt = 0
    cache_write_tokens: NonNegativeInt = 0
    input_audio_tokens: NonNegativeInt = 0
    output_audio_tokens: NonNegativeInt = 0
    cache_audio_read_tokens: NonNegativeInt = 0
    provider_details: ProviderUsageDetails = Field(default_factory=dict)

    @classmethod
    def from_requests(cls, requests: Iterable[RequestUsage] = (), *, tool_calls: int = 0) -> Self:
        request_values = tuple(requests)

        return cls(
            request_count=len(request_values),
            tool_calls=tool_calls,
            provider_details=_merge_provider_details(request.provider_details for request in request_values),
            **{field: sum(getattr(request, field) for request in request_values) for field in _TOKEN_FIELDS},
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Self:
        return type(self)(
            request_count=self.request_count + other.request_count,
            tool_calls=self.tool_calls + other.tool_calls,
            provider_details=_merge_provider_details((self.provider_details, other.provider_details)),
            **{field: getattr(self, field) + getattr(other, field) for field in _TOKEN_FIELDS},
        )


def _merge_provider_details(values: Iterable[ProviderUsageDetails]) -> ProviderUsageDetails:
    merged: ProviderUsageDetails = {}

    for namespaces in values:
        for namespace, details in namespaces.items():
            target = merged.setdefault(namespace, {})

            for key, value in details.items():
                target[key] = target.get(key, 0) + value

    return merged


_TOKEN_FIELDS = (
    'input_tokens',
    'output_tokens',
    'cache_read_tokens',
    'cache_write_tokens',
    'input_audio_tokens',
    'output_audio_tokens',
    'cache_audio_read_tokens',
)
