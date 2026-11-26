import json
import time
from typing import cast

import httpx2
import pytest
from pydantic_ai.models import Model
from pytest_mock import MockerFixture

import tests.support.agent_consumer as consumer
from ovid_core import ContextWindowError, ProviderError, ProviderFailureKind
from ovid_core.adapters.pydantic_ai import CodexSubscriptionModelFactory
from ovid_core.codex import CodexAuth
from ovid_core.config import ModelConfig
from tests.support.agent_consumer import AgentDependencies
from tests.support.agent_helpers import RETRY_SLEEP, agent_factory
from tests.support.helpers import MemoryTokenStore, make_codex_tokens, make_jwt, oauth_client


_CATALOG = {'models': [{'slug': 'gpt-5-codex', 'base_instructions': 'codex-instructions'}]}
_TOKENS = make_codex_tokens()


def _completed() -> httpx2.Response:
    response = {
        'id': 'resp_1',
        'created_at': time.time(),
        'model': 'gpt-5-codex',
        'object': 'response',
        'output': [],
        'parallel_tool_calls': True,
        'tool_choice': 'auto',
        'tools': [],
        'status': 'completed',
    }
    events = (
        {'type': 'response.created', 'sequence_number': 0, 'response': response},
        {
            'type': 'response.output_text.delta',
            'sequence_number': 1,
            'item_id': 'message_1',
            'output_index': 0,
            'content_index': 0,
            'delta': 'retried',
            'logprobs': [],
        },
        {'type': 'response.completed', 'sequence_number': 2, 'response': response},
    )
    content = ''.join(f'data: {json.dumps(event)}\n\n' for event in events)

    return httpx2.Response(200, text=content, headers={'content-type': 'text/event-stream'})


def _error(status_code: int, *, code: str | None = None, message: str = 'failed', **headers: str) -> httpx2.Response:
    return httpx2.Response(
        status_code,
        json={'error': {'code': code, 'type': 'invalid_request_error', 'message': message}},
        headers={key.replace('_', '-'): value for key, value in headers.items()},
    )


async def _codex_run(responses: list[httpx2.Response], requests: list[httpx2.Request]) -> str:
    def backend(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/models'):
            return httpx2.Response(200, json=_CATALOG)
        requests.append(request)
        return responses.pop(0)

    config = ModelConfig(provider='codex-subscription', model='gpt-5-codex')
    async with (
        oauth_client(lambda _request: httpx2.Response(500)) as client,
        CodexAuth(store=MemoryTokenStore(_TOKENS), http_client=client) as auth,
    ):
        factory = CodexSubscriptionModelFactory(auth=auth, backend_transport=httpx2.MockTransport(backend))
        handle = await factory.build(model_id='primary', config=config)
        async with cast(Model, handle.runtime):
            agent = await agent_factory({'primary': handle.runtime}).build(consumer.text_definition())
            result = await agent.run('Hello.', deps=AgentDependencies(prefix='codex'))

    return result.output


async def test_rate_limit_waits_for_retry_after_and_sends_one_retry(mocker: MockerFixture) -> None:
    sleep = mocker.patch(RETRY_SLEEP, new=mocker.AsyncMock())
    requests: list[httpx2.Request] = []

    output = await _codex_run([_error(429, code='rate_limit_exceeded', retry_after='2'), _completed()], requests)

    assert output == 'retried'
    assert len(requests) == 2
    sleep.assert_awaited_once_with(2.0)


async def test_unavailable_backend_fails_after_max_attempts(mocker: MockerFixture) -> None:
    sleep = mocker.patch(RETRY_SLEEP, new=mocker.AsyncMock())
    requests: list[httpx2.Request] = []

    with pytest.raises(ProviderError, match=r'^Provider request failed \(unavailable, status 503\)$') as raised:
        await _codex_run([_error(503) for _ in range(4)], requests)

    assert (raised.value.failure_kind, raised.value.status_code) == (ProviderFailureKind.UNAVAILABLE, 503)
    assert len(requests) == 4
    assert [call.args[0] <= 4 for call in sleep.await_args_list] == [True, True, True]


async def test_context_overflow_raises_context_window_error_without_retry(mocker: MockerFixture) -> None:
    sleep = mocker.patch(RETRY_SLEEP, new=mocker.AsyncMock())
    requests: list[httpx2.Request] = []

    with pytest.raises(ContextWindowError):
        await _codex_run([_error(400, code='context_length_exceeded', message='Input is too large')], requests)

    assert len(requests) == 1
    sleep.assert_not_awaited()


async def test_quota_rate_limit_is_not_retried(mocker: MockerFixture) -> None:
    sleep = mocker.patch(RETRY_SLEEP, new=mocker.AsyncMock())
    requests: list[httpx2.Request] = []

    with pytest.raises(ProviderError) as raised:
        await _codex_run([_error(429, code='insufficient_quota', retry_after='1')], requests)

    assert raised.value.failure_kind is ProviderFailureKind.RATE_LIMIT
    assert (raised.value.provider_code, raised.value.retry_after_seconds) == ('insufficient_quota', 1.0)
    assert len(requests) == 1
    sleep.assert_not_awaited()


async def test_retry_after_above_max_delay_fails_at_once_and_states_the_delay(mocker: MockerFixture) -> None:
    sleep = mocker.patch(RETRY_SLEEP, new=mocker.AsyncMock())
    requests: list[httpx2.Request] = []

    with pytest.raises(ProviderError, match='wait of 120 seconds') as raised:
        await _codex_run([_error(429, code='rate_limit_exceeded', retry_after='120')], requests)

    assert raised.value.retry_after_seconds == 120.0
    assert len(requests) == 1
    sleep.assert_not_awaited()


async def test_provider_secrets_are_absent_from_raised_errors(mocker: MockerFixture) -> None:
    mocker.patch(RETRY_SLEEP, new=mocker.AsyncMock())
    access_token = _TOKENS.access_token.get_secret_value()
    other_jwt = make_jwt({'sub': 'leaked'})
    message = f'Rejected Bearer {access_token} with {other_jwt} and sk-proj-secret123'
    requests: list[httpx2.Request] = []

    with pytest.raises(ProviderError) as raised:
        await _codex_run([_error(400, code='invalid_value', message=message)], requests)

    chain: list[BaseException] = []
    error: BaseException | None = raised.value
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    text = ' '.join(f'{error!s} {error!r}' for error in chain)
    assert raised.value.failure_kind is ProviderFailureKind.INVALID_REQUEST
    assert requests[0].headers['authorization'].endswith(access_token)
    assert '[redacted]' in text
    assert all(secret not in text for secret in (access_token, other_jwt, 'sk-proj-secret123'))
