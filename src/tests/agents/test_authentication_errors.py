from collections.abc import AsyncIterator
from traceback import format_exception

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

import tests.support.agent_consumer as consumer
from ovid_core.errors import AuthenticationError, CredentialError
from ovid_core.runtime.events import AgentEvent
from tests.support.agent_consumer import AgentDependencies, WaitTool
from tests.support.agent_helpers import agent_factory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('failure', 'expected_type'),
    [
        (ModelHTTPError(403, 'model', {'error': 'provider-secret'}), AuthenticationError),
        (CredentialError('credential-secret'), CredentialError),
    ],
)
async def test_authentication_failure_is_redacted_in_run_and_stream(
    failure: Exception,
    expected_type: type[CredentialError],
) -> None:
    async def request(_: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        raise failure

    async def rejected_stream(_: list[ModelMessage], __: AgentInfo) -> AsyncIterator[str]:
        yield 'Started'
        raise failure

    factory = agent_factory({'primary': FunctionModel(request, stream_function=rejected_stream, model_name='rejected')})
    agent = await factory.build(consumer.waiting_definition(tool=WaitTool()))
    with pytest.raises(expected_type) as captured:
        await agent.run('Run.', deps=AgentDependencies(prefix='failure'))
    assert '-secret' not in ''.join(format_exception(captured.value))
    events: list[AgentEvent] = []
    with pytest.raises(expected_type) as streamed:
        async with agent.stream('Run.', deps=AgentDependencies(prefix='failure')) as stream:
            async for event in stream:
                events.append(event)
    failed = events[-1]
    assert failed.kind == 'run_failed'
    assert failed.error_type == expected_type.__name__
    assert '-secret' not in failed.message
    assert '-secret' not in ''.join(format_exception(streamed.value))
