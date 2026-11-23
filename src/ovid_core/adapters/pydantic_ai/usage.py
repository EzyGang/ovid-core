from pydantic import ValidationError
from pydantic_ai.usage import RequestUsage as PydanticRequestUsage
from pydantic_ai.usage import RunUsage as PydanticRunUsage

from ovid_core.errors import ProviderError
from ovid_core.usage.models import RequestUsage, Usage


_TOKEN_FIELDS = (
    'input_tokens',
    'output_tokens',
    'cache_read_tokens',
    'cache_write_tokens',
    'input_audio_tokens',
    'output_audio_tokens',
    'cache_audio_read_tokens',
)


def request_usage_from_pydantic(value: PydanticRequestUsage, *, provider_namespace: str) -> RequestUsage:
    details = {key: item for key, item in value.details.items() if item is not None}
    payload = {field: getattr(value, field) for field in _TOKEN_FIELDS}
    payload['provider_details'] = {provider_namespace: details} if details else {}

    try:
        return RequestUsage.model_validate(payload)
    except ValidationError as error:
        raise ProviderError('Provider returned invalid usage data') from error


def usage_from_pydantic(value: PydanticRunUsage, requests: tuple[RequestUsage, ...]) -> Usage:
    try:
        usage = Usage.from_requests(requests, tool_calls=value.tool_calls)
    except ValidationError as error:
        raise ProviderError('Provider returned invalid aggregate usage data') from error

    upstream = (value.requests, *(getattr(value, field) for field in _TOKEN_FIELDS))
    normalized = (usage.request_count, *(getattr(usage, field) for field in _TOKEN_FIELDS))

    if upstream != normalized:
        raise ProviderError('Provider aggregate usage is inconsistent with request usage')

    return usage


def aggregate_usage_from_pydantic(value: PydanticRunUsage) -> Usage:
    try:
        return Usage.model_validate(
            {
                'request_count': value.requests,
                'tool_calls': value.tool_calls,
                **{field: getattr(value, field) for field in _TOKEN_FIELDS},
            }
        )
    except ValidationError as error:
        raise ProviderError('Provider returned invalid aggregate usage data') from error
