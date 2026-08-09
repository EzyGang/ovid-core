from collections.abc import Callable

import pytest
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.usage import RunUsage as PydanticRunUsage

from ovid_core.adapters.pydantic_ai._provider_errors import provider_failure_kind, should_fallback
from ovid_core.adapters.pydantic_ai.usage import aggregate_usage_from_pydantic
from ovid_core.errors import ProviderError, UsageLimitError
from ovid_core.policy import AgentUsageLimits, ProviderFailureKind
from ovid_core.usage.models import Usage
from ovid_core.usage.tracking import UsageTracker


@pytest.mark.parametrize(
    ('status', 'kind', 'fallback'),
    (
        (401, ProviderFailureKind.AUTHENTICATION, False),
        (429, ProviderFailureKind.RATE_LIMIT, True),
        (408, ProviderFailureKind.TIMEOUT, True),
        (500, ProviderFailureKind.UNAVAILABLE, True),
        (400, ProviderFailureKind.INVALID_REQUEST, False),
    ),
)
def test_provider_http_failures_have_stable_fallback_classification(
    status: int,
    kind: ProviderFailureKind,
    fallback: bool,
) -> None:
    error = ModelHTTPError(status, 'model', 'provider-secret')

    assert provider_failure_kind(error) is kind
    assert should_fallback(error) is fallback


def test_non_http_provider_failure_classification_is_stable() -> None:
    assert provider_failure_kind(TimeoutError()) is ProviderFailureKind.TIMEOUT
    assert provider_failure_kind(ModelAPIError('model', 'secret')) is ProviderFailureKind.UNAVAILABLE
    assert provider_failure_kind(RuntimeError('secret')) is ProviderFailureKind.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('limits', 'usage'),
    (
        (AgentUsageLimits(requests=1), Usage(request_count=2)),
        (AgentUsageLimits(tool_calls=1), Usage(tool_calls=2)),
        (AgentUsageLimits(input_tokens=1), Usage(input_tokens=2)),
        (AgentUsageLimits(output_tokens=1), Usage(output_tokens=2)),
        (AgentUsageLimits(total_tokens=1), Usage(input_tokens=2)),
    ),
)
async def test_tracker_rejects_each_aggregate_limit(
    limits: AgentUsageLimits,
    usage: Usage,
) -> None:
    tracker = UsageTracker(limits=limits)

    with pytest.raises(UsageLimitError, match='Aggregate usage limit exceeded'):
        await tracker.add(usage)

    assert tracker.usage == usage


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('limits', 'usage', 'check', 'message'),
    (
        (
            AgentUsageLimits(input_tokens=1),
            Usage(input_tokens=1),
            UsageTracker.check_before_request,
            'input token',
        ),
        (
            AgentUsageLimits(output_tokens=1),
            Usage(output_tokens=1),
            UsageTracker.check_before_request,
            'output token',
        ),
        (
            AgentUsageLimits(total_tokens=1),
            Usage(input_tokens=1),
            UsageTracker.check_before_request,
            'total token',
        ),
        (
            AgentUsageLimits(tool_calls=1),
            Usage(tool_calls=1),
            UsageTracker.check_before_tool_call,
            'tool-call',
        ),
    ),
)
async def test_tracker_checks_boundaries_before_work(
    limits: AgentUsageLimits,
    usage: Usage,
    check: Callable[[UsageTracker], None],
    message: str,
) -> None:
    tracker = UsageTracker(limits=limits)
    await tracker.add(usage)

    with pytest.raises(UsageLimitError, match=message):
        check(tracker)


@pytest.mark.asyncio
async def test_tracker_allows_work_below_optional_limits() -> None:
    tracker = UsageTracker(limits=AgentUsageLimits(tool_calls=2))
    await tracker.add(Usage(tool_calls=1))

    tracker.check_before_request()
    tracker.check_before_tool_call()


def test_live_usage_adapter_rejects_negative_aggregates() -> None:
    with pytest.raises(ProviderError, match='invalid aggregate usage'):
        aggregate_usage_from_pydantic(PydanticRunUsage(input_tokens=-1))
