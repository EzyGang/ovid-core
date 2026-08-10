import pytest
from pydantic import ValidationError
from pydantic_ai.usage import RequestUsage as PydanticRequestUsage
from pydantic_ai.usage import RunUsage as PydanticRunUsage

from ovid_core import ProviderError
from ovid_core.adapters.pydantic_ai import (
    PYDANTIC_AI_USAGE_FIELDS,
    request_usage_from_pydantic,
    usage_from_pydantic,
    usage_update_event_from_pydantic,
)
from ovid_core.usage import RequestUsage, Usage
from tests.support.helpers import CONVERSATION_ID, RUN_ID, make_request_usage


def test_zero_usage_serialization_round_trip() -> None:
    usage = Usage()

    assert Usage.model_validate_json(usage.model_dump_json()) == usage
    assert usage.request_count == 0
    assert usage.total_tokens == 0
    assert 'cost' not in Usage.model_fields


def test_request_usage_keeps_namespaced_provider_details() -> None:
    request = RequestUsage(
        input_tokens=10,
        output_tokens=5,
        provider_details={'openai': {'reasoning_tokens': 3}},
    )

    assert request.total_tokens == 15
    assert request.provider_details == {'openai': {'reasoning_tokens': 3}}
    assert RequestUsage.model_validate_json(request.model_dump_json()) == request


def test_usage_aggregates_and_merges_without_request_snapshots() -> None:
    first_request = RequestUsage(
        input_tokens=10,
        output_tokens=2,
        provider_details={'openai': {'reasoning_tokens': 1}},
    )
    second_request = RequestUsage(
        input_tokens=7,
        output_tokens=3,
        provider_details={'openai': {'reasoning_tokens': 2}},
    )
    first = Usage.from_requests((first_request,), tool_calls=1)
    second = Usage.from_requests((second_request,), tool_calls=2)

    merged = first + second

    assert merged.request_count == 2
    assert merged.input_tokens == 17
    assert merged.output_tokens == 5
    assert merged.total_tokens == 22
    assert merged.tool_calls == 3
    assert merged.provider_details == {'openai': {'reasoning_tokens': 3}}
    assert first.request_count == 1
    assert merged.delta_since(first) == second
    with pytest.raises(ValueError, match='usage values cannot decrease'):
        Usage(input_tokens=1).delta_since(Usage(input_tokens=2))
    with pytest.raises(ValueError, match='provider usage values cannot decrease'):
        Usage(provider_details={}).delta_since(Usage(provider_details={'openai': {'reasoning_tokens': 1}}))
    assert (
        Usage(provider_details={'openai': {'reasoning_tokens': 1}}).delta_since(
            Usage(provider_details={'openai': {'reasoning_tokens': 1}})
        )
        == Usage()
    )


def test_usage_rejects_negative_values() -> None:
    with pytest.raises(ValidationError, match='greater than or equal to 0'):
        RequestUsage(input_tokens=-1)
    with pytest.raises(ValidationError, match='greater than or equal to 0'):
        Usage(tool_calls=-1)


def test_pydantic_request_usage_adapter_maps_upstream_fields_directly() -> None:
    upstream = PydanticRequestUsage(
        input_tokens=20,
        output_tokens=8,
        cache_read_tokens=5,
        cache_write_tokens=2,
        input_audio_tokens=3,
        output_audio_tokens=1,
        cache_audio_read_tokens=1,
        details={'reasoning_tokens': 4, 'missing': None},
    )

    request = request_usage_from_pydantic(upstream, provider_namespace='openai')

    assert request.model_dump(exclude={'provider_details'}) == {
        'input_tokens': 20,
        'output_tokens': 8,
        'cache_read_tokens': 5,
        'cache_write_tokens': 2,
        'input_audio_tokens': 3,
        'output_audio_tokens': 1,
        'cache_audio_read_tokens': 1,
    }
    assert request.provider_details == {'openai': {'reasoning_tokens': 4}}


def test_pydantic_request_usage_adapter_handles_missing_details() -> None:
    assert request_usage_from_pydantic(PydanticRequestUsage(), provider_namespace='provider') == RequestUsage()


def test_pydantic_usage_adapter_rejects_invalid_or_inconsistent_values() -> None:
    with pytest.raises(ProviderError, match='invalid usage data') as request_error:
        request_usage_from_pydantic(PydanticRequestUsage(input_tokens=-1), provider_namespace='provider')
    assert isinstance(request_error.value.__cause__, ValidationError)

    request = make_request_usage(input_tokens=10, output_tokens=5)

    with pytest.raises(ProviderError, match='inconsistent'):
        usage_from_pydantic(PydanticRunUsage(requests=1, input_tokens=9, output_tokens=5), (request,))
    with pytest.raises(ProviderError, match='invalid aggregate') as run_error:
        usage_from_pydantic(PydanticRunUsage(tool_calls=-1), ())
    assert isinstance(run_error.value.__cause__, ValidationError)


def test_pydantic_usage_adapter_aggregates_and_streams_snapshots() -> None:
    first = make_request_usage(input_tokens=2, output_tokens=1)
    upstream_request = PydanticRequestUsage(input_tokens=3, output_tokens=4)
    second = request_usage_from_pydantic(upstream_request, provider_namespace='test')
    upstream_run = PydanticRunUsage(requests=2, input_tokens=5, output_tokens=5, tool_calls=1)

    usage = usage_from_pydantic(upstream_run, (first, second))
    event = usage_update_event_from_pydantic(
        upstream_request,
        completed_requests=(first,),
        provider_namespace='test',
        run_id=RUN_ID,
        conversation_id=CONVERSATION_ID,
        sequence=4,
    )

    assert usage == Usage.from_requests((first, second), tool_calls=1)
    assert event.kind == 'usage_update'
    assert event.usage.input_tokens == 5


def test_upstream_usage_inventory_classifies_active_fields() -> None:
    classifications = {item.name: item.classification for item in PYDANTIC_AI_USAGE_FIELDS}

    assert classifications['requests'] == 'stable'
    assert classifications['input_audio_tokens'] == 'optional'
    assert classifications['details'] == 'provider_specific'
    assert classifications['dynamic attributes'] == 'upstream_private'
