import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import cast

import pytest
from pydantic import JsonValue, ValidationError
from pydantic_ai.exceptions import ModelRetry, ToolFailed
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

import tests.support.agent_consumer as consumer
from ovid_core import (
    AgentDefinition,
    AgentRetryPolicy,
    AgentRunError,
    AgentRunPolicy,
    AgentUsageLimits,
    BaseModel,
    OvidAgent,
    ToolExecutionError,
    ToolRetryError,
    ToolTimeoutError,
    ToolValidationError,
    UsageLimitError,
)
from ovid_core.hooks import BaseToolHook
from ovid_core.messages import ToolReturnPart
from ovid_core.routing import ModelRef
from ovid_core.runtime import AgentEvent, RunResult
from ovid_core.tools import BaseTool, ToolExecutionContext, ToolResult
from tests.support.agent_consumer import AgentDependencies, ConsumerToolset, WaitTool
from tests.support.agent_helpers import agent_factory


class FailArguments(BaseModel):
    kind: str


class FailTool(BaseTool[AgentDependencies, FailArguments, ToolResult]):
    id = 'fail'
    description = 'Fail in the requested way.'
    args_type = FailArguments
    result_type = ToolResult
    timeout_seconds = 0.05

    async def execute(
        self,
        context: ToolExecutionContext[AgentDependencies],
        arguments: FailArguments,
    ) -> ToolResult:
        del context
        if arguments.kind == 'slow':
            await asyncio.Event().wait()
        if arguments.kind == 'invalid':
            return cast(ToolResult, 'private result')

        failures: dict[str, Exception] = {
            'execution': ToolExecutionError('missing file'),
            'retry': ToolRetryError('use an absolute path'),
            'generic': ValueError('boom'),
            'own_timeout': TimeoutError('upstream is slow'),
            'long': ToolExecutionError('x' * 100),
            'model_retry': ModelRetry('direct retry'),
            'tool_failed': ToolFailed('direct failure'),
        }
        raise failures[arguments.kind]


class ErrorHook(BaseToolHook[AgentDependencies]):
    def __init__(self) -> None:
        self.errors: list[ToolExecutionError] = []

    async def on_tool_error(
        self,
        context: ToolExecutionContext[AgentDependencies],
        tool_id: str,
        error: ToolExecutionError,
    ) -> None:
        del context, tool_id
        self.errors.append(error)


class CallingModel:
    def __init__(self, *calls: dict[str, JsonValue], repeat: bool = False) -> None:
        self.calls = calls
        self.repeat = repeat
        self.requests = 0

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        index = self.requests
        self.requests += 1
        if self.repeat or index < len(self.calls):
            return ModelResponse(parts=[ToolCallPart('fail', self.calls[index % len(self.calls)], f'call-{index}')])

        return ModelResponse(parts=[TextPart('done')])


async def failing_agent(
    model: FunctionModel,
    *,
    retries: AgentRetryPolicy = AgentRetryPolicy(),
    limits: AgentUsageLimits = AgentUsageLimits(),
    hook: ErrorHook | None = None,
) -> OvidAgent[AgentDependencies, str]:
    definition = AgentDefinition[AgentDependencies, str](
        model=ModelRef(name='primary'),
        deps_type=AgentDependencies,
        output_type=str,
        toolsets=(ConsumerToolset((FailTool(),)),),
        hooks=(hook,) if hook is not None else (),
        policy=AgentRunPolicy(retries=retries, limits=limits),
    )

    return await agent_factory({'primary': model}).build(definition)


def tool_returns(result: RunResult[str]) -> list[tuple[str, JsonValue]]:
    return [
        (part.outcome, part.content)
        for message in result.messages
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


async def test_tool_execution_error_returns_failed_result_in_history_and_stream() -> None:
    async def call_failing_tool_once(
        messages: list[ModelMessage],
        info: AgentInfo,
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        del info
        if len(messages) == 1:
            yield {0: DeltaToolCall(name='fail', json_args='{"kind": "execution"}', tool_call_id='call-0')}
        else:
            yield 'done'

    agent = await failing_agent(FunctionModel(stream_function=call_failing_tool_once))

    async with agent.stream('Read.', deps=AgentDependencies(prefix='failed')) as stream:
        streamed = [event for event in [event async for event in stream] if event.kind == 'tool_result']
        result = stream.result

    assert result.output == 'done'
    assert [(event.outcome, event.content) for event in streamed] == [('failed', 'missing file')]
    assert tool_returns(result) == [('failed', 'missing file')]
    assert (result.usage.request_count, result.usage.tool_calls) == (2, 0)


async def test_each_failure_kind_returns_its_documented_tool_result() -> None:
    kinds = ('execution', 'retry', 'generic', 'own_timeout', 'slow', 'invalid', 'model_retry', 'tool_failed')
    hook = ErrorHook()
    agent = await failing_agent(FunctionModel(CallingModel(*({'kind': kind} for kind in kinds))), hook=hook)

    result = await agent.run('Fail.', deps=AgentDependencies(prefix='kinds'))

    assert result.output == 'done'
    assert tool_returns(result) == [
        ('failed', 'missing file'),
        ('retried', 'use an absolute path'),
        ('failed', "Tool 'fail' failed: ValueError: boom"),
        ('failed', "Tool 'fail' failed: TimeoutError: upstream is slow"),
        ('failed', "Tool 'fail' timed out after 0.05s"),
        ('failed', "Tool 'fail' returned an invalid result"),
        ('retried', 'direct retry'),
        ('failed', 'direct failure'),
    ]
    assert [(type(error), str(error), type(error.__cause__)) for error in hook.errors] == [
        (ToolExecutionError, 'missing file', type(None)),
        (ToolRetryError, 'use an absolute path', type(None)),
        (ToolExecutionError, "Tool 'fail' failed", ValueError),
        (ToolTimeoutError, "Tool 'fail' timed out", TimeoutError),
        (ToolTimeoutError, "Tool 'fail' timed out", TimeoutError),
        (ToolExecutionError, "Tool 'fail' failed", ValidationError),
    ]
    assert 'private result' not in str(tool_returns(result))


async def test_invalid_arguments_are_retried_until_the_tool_budget_is_exhausted() -> None:
    recovering = await failing_agent(FunctionModel(CallingModel({'kindd': 'generic'}, {}, {'kind': 'execution'})))
    exhausting_model = CallingModel({}, repeat=True)
    exhausting = await failing_agent(FunctionModel(exhausting_model))

    result = await recovering.run('Fail.', deps=AgentDependencies(prefix='invalid'))
    with pytest.raises(AgentRunError, match='Agent run failed'):
        await exhausting.run('Fail.', deps=AgentDependencies(prefix='invalid'))

    assert tool_returns(result) == [
        ('retried', "Invalid arguments for tool 'fail':\nkind: Field required\nkindd: Extra inputs are not permitted"),
        ('retried', "Invalid arguments for tool 'fail':\nkind: Field required"),
        ('failed', 'missing file'),
    ]
    assert exhausting_model.requests == AgentRetryPolicy().tools + 1


async def test_tool_retry_error_consumes_the_budget_and_execution_error_does_not() -> None:
    retries = AgentRetryPolicy(tools=1)
    retrying_model = CallingModel({'kind': 'retry'}, repeat=True)
    retrying = await failing_agent(FunctionModel(retrying_model), retries=retries)
    failing = await failing_agent(FunctionModel(CallingModel(*({'kind': 'execution'},) * 3)), retries=retries)

    with pytest.raises(AgentRunError, match='Agent run failed'):
        await retrying.run('Fail.', deps=AgentDependencies(prefix='retry'))
    result = await failing.run('Fail.', deps=AgentDependencies(prefix='retry'))

    assert retrying_model.requests == 2
    assert tool_returns(result) == [('failed', 'missing file')] * 3


async def test_endless_tool_failures_stop_at_the_request_limit() -> None:
    model = CallingModel({'kind': 'execution'}, repeat=True)
    agent = await failing_agent(FunctionModel(model), limits=AgentUsageLimits(requests=3))

    with pytest.raises(UsageLimitError, match='Agent usage limit exceeded'):
        await agent.run('Fail.', deps=AgentDependencies(prefix='limit'))

    assert model.requests == 3


@pytest.mark.parametrize(
    ('arguments', 'error_type', 'message'),
    (
        ({'kind': 'execution'}, ToolExecutionError, 'missing file'),
        ({'kind': 'retry'}, ToolRetryError, 'use an absolute path'),
        ({'kind': 'model_retry'}, ToolExecutionError, "Tool 'fail' failed"),
        ({}, ToolValidationError, "Invalid arguments for tool 'fail'"),
    ),
)
async def test_raise_mode_aborts_the_run_with_the_ovid_tool_error(
    arguments: dict[str, JsonValue],
    error_type: type[Exception],
    message: str,
) -> None:
    model = CallingModel(arguments)
    agent = await failing_agent(FunctionModel(model), retries=AgentRetryPolicy(tool_failures='raise'))

    with pytest.raises(error_type, match=message) as raised:
        await agent.run('Fail.', deps=AgentDependencies(prefix='raise'))

    assert type(raised.value) is error_type
    assert model.requests == 1


async def test_on_tool_error_fires_once_per_failed_execution_in_both_modes() -> None:
    returning_hook = ErrorHook()
    raising_hook = ErrorHook()
    returning = await failing_agent(FunctionModel(CallingModel({'kind': 'generic'})), hook=returning_hook)
    raising = await failing_agent(
        FunctionModel(CallingModel({'kind': 'generic'})),
        retries=AgentRetryPolicy(tool_failures='raise'),
        hook=raising_hook,
    )

    result = await returning.run('Fail.', deps=AgentDependencies(prefix='hook'))
    with pytest.raises(ToolExecutionError) as raised:
        await raising.run('Fail.', deps=AgentDependencies(prefix='hook'))

    assert tool_returns(result) == [('failed', "Tool 'fail' failed: ValueError: boom")]
    assert raising_hook.errors == [raised.value]
    for hook in (returning_hook, raising_hook):
        assert [(type(error), str(error)) for error in hook.errors] == [(ToolExecutionError, "Tool 'fail' failed")]
        assert isinstance(hook.errors[0].__cause__, ValueError)


async def test_long_error_is_truncated_to_error_max_chars() -> None:
    retries = AgentRetryPolicy(error_max_chars=40)
    agent = await failing_agent(FunctionModel(CallingModel({'kind': 'long'})), retries=retries)

    result = await agent.run('Fail.', deps=AgentDependencies(prefix='long'))

    assert tool_returns(result) == [('failed', f'{"x" * 25}... [truncated]')]
    assert len(str(tool_returns(result)[0][1])) == 40


async def test_cancelled_stream_raises_cancelled_error_without_a_failed_result() -> None:
    tool = WaitTool()
    hook = ErrorHook()
    definition = replace(consumer.waiting_definition(tool=tool), hooks=(hook,))
    agent = await agent_factory({'primary': TestModel(call_tools=['wait'])}).build(definition)
    deps = AgentDependencies(prefix='cancel')
    events: list[AgentEvent] = []

    async def consume() -> None:
        async with agent.stream('Wait.', deps=deps) as stream:
            async for event in stream:
                events.append(event)

    task = asyncio.create_task(consume())
    await deps.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert tool.cancelled is True
    assert hook.errors == []
    assert [event.kind for event in events if event.kind == 'tool_result'] == []
