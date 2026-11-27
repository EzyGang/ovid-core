from collections.abc import Callable

import pytest
from pydantic_ai.exceptions import ModelHTTPError, ModelQuotaExceededError, ModelUnavailableError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pytest_mock import MockerFixture

from ovid_core import AgentDefinition, AgentRunPolicy, ProviderError, ProviderRetryPolicy
from ovid_core.routing import ModelRef, ModelRouteRef
from tests.support.agent_helpers import RETRY_SLEEP, agent_factory


def _unavailable(name: str, calls: list[str]) -> Callable[[list[ModelMessage], AgentInfo], ModelResponse]:
    def request(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        calls.append(name)
        raise ModelHTTPError.for_category(ModelUnavailableError, status_code=503, model_name=name)

    return request


def _answering(calls: list[str]) -> Callable[[list[ModelMessage], AgentInfo], ModelResponse]:
    def request(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        calls.append('second')
        return ModelResponse(parts=[TextPart('second answered')])

    return request


async def _route_run(first: FunctionModel, calls: list[str]) -> str:
    factory = agent_factory(
        {'first': first, 'second': FunctionModel(_answering(calls), model_name='second')},
        route=True,
    )
    agent = await factory.build(
        AgentDefinition(model=ModelRouteRef(name='answer'), deps_type=type(None), output_type=str)
    )

    return (await agent.run('Hello.', deps=None)).output


def test_provider_retry_policy_defaults() -> None:
    assert AgentRunPolicy().provider_retry == ProviderRetryPolicy(
        max_attempts=4,
        initial_delay_seconds=1.0,
        max_delay_seconds=60.0,
    )


async def test_policy_sets_attempts_and_backoff(mocker: MockerFixture) -> None:
    sleep = mocker.patch(RETRY_SLEEP, new=mocker.AsyncMock())
    calls: list[str] = []
    policy = AgentRunPolicy(provider_retry=ProviderRetryPolicy(max_attempts=2, initial_delay_seconds=0.5))
    factory = agent_factory({'primary': FunctionModel(_unavailable('primary', calls), model_name='primary')})
    agent = await factory.build(
        AgentDefinition(model=ModelRef(name='primary'), deps_type=type(None), output_type=str, policy=policy)
    )

    with pytest.raises(ProviderError, match='status 503'):
        await agent.run('Hello.', deps=None)

    assert calls == ['primary', 'primary']
    delay = sleep.await_args.args[0]
    assert 0.25 <= delay <= 0.5


async def test_route_retries_a_transient_failure_on_the_candidate_before_it_falls_back(mocker: MockerFixture) -> None:
    sleep = mocker.patch(RETRY_SLEEP, new=mocker.AsyncMock())
    calls: list[str] = []

    output = await _route_run(FunctionModel(_unavailable('first', calls), model_name='first'), calls)

    assert output == 'second answered'
    assert calls == ['first', 'first', 'first', 'first', 'second']
    assert sleep.await_count == 3


async def test_route_falls_back_from_an_eligible_non_transient_failure_without_retry(mocker: MockerFixture) -> None:
    sleep = mocker.patch(RETRY_SLEEP, new=mocker.AsyncMock())
    calls: list[str] = []

    def quota(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        calls.append('first')
        raise ModelHTTPError.for_category(
            ModelQuotaExceededError,
            status_code=429,
            model_name='first',
            provider_error_code='insufficient_quota',
        )

    output = await _route_run(FunctionModel(quota, model_name='first'), calls)

    assert output == 'second answered'
    assert calls == ['first', 'second']
    sleep.assert_not_awaited()
