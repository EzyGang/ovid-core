import asyncio
from typing import Literal

import pytest
from pydantic_ai.capabilities import Capability
from pydantic_ai.messages import BinaryContent, ModelRequest
from pydantic_ai.messages import ToolReturnPart as PydanticToolReturnPart
from pydantic_ai.toolsets import FunctionToolset

from ovid_core import AgentRetryPolicy, AgentRunPolicy, ToolExecutionError, ToolTimeoutError
from ovid_core.adapters.pydantic_ai import pydantic_ai_capability
from ovid_core.capabilities import BaseCapability, CapabilityContributions
from ovid_core.messages import BinaryContent as OvidBinaryContent
from ovid_core.messages import ImageUrl
from ovid_core.tools import ToolExecutionContext, ToolResult
from tests.support.agent_consumer import AgentDependencies
from tests.support.deferred_consumer import RecordingTool, ScriptedModel, ValueArguments, deferred_agent, tool_returns
from tests.support.hook_consumer import DecisionHook, RecoveryHook, ResultHook, lookup_capability


class FileTool(RecordingTool):
    id = 'file'

    async def execute(self, context: ToolExecutionContext[AgentDependencies], arguments: ValueArguments) -> ToolResult:
        del context
        return ToolResult(
            content=f'image {arguments.value}',
            metadata={'source': 'camera'},
            files=(ImageUrl(url='https://example.com/a.png'),),
        )


class SlowTool(RecordingTool):
    id = 'slow'
    timeout_seconds = 0.01

    async def execute(self, context: ToolExecutionContext[AgentDependencies], arguments: ValueArguments) -> ToolResult:
        del context, arguments
        await asyncio.Event().wait()
        return ToolResult(content='never')


class SecretTool(RecordingTool):
    id = 'secret'

    async def execute(self, context: ToolExecutionContext[AgentDependencies], arguments: ValueArguments) -> ToolResult:
        del context, arguments
        raise ToolExecutionError('private failure')


def _deps() -> AgentDependencies:
    return AgentDependencies(prefix='hooks')


def _raise(result: ToolResult) -> ToolResult:
    raise RuntimeError(f'cannot rewrite {result.content}')


async def test_after_tool_rewrite_reaches_the_next_request_and_keeps_metadata_out_of_it() -> None:
    calls: list[int] = []
    hook = ResultHook(lambda result: ToolResult(content=str(result.content)[:5], metadata={'full': result.content}))
    model = ScriptedModel(('lookup', {'value': 1}, 'p1'))
    agent = await deferred_agent(model, (), hooks=(hook,), capabilities=(lookup_capability(calls, text='long result'),))

    result = await agent.run('Look up.', deps=_deps())

    request = model.requests[1][-1]
    assert isinstance(request, ModelRequest)
    part = request.parts[-1]
    assert isinstance(part, PydanticToolReturnPart)
    assert (part.content, part.metadata) == ('long ', {'full': 'long result 1'})
    assert 'long result' not in part.model_response_str()
    assert tool_returns(result.messages) == [('p1', 'success', 'long ')]
    assert hook.results == [ToolResult(content='long result 1')]


async def test_after_tool_sees_ovid_and_upstream_files_and_a_none_keeps_the_result() -> None:
    def picture() -> BinaryContent:
        return BinaryContent(data=b'png', media_type='image/png')

    toolset = FunctionToolset[AgentDependencies]()
    toolset.tool_plain(picture)
    hook = ResultHook(lambda result: None)
    model = ScriptedModel(('file', {'value': 1}, 'f1'), ('picture', {}, 'u1'))
    agent = await deferred_agent(
        model,
        (FileTool(),),
        hooks=(hook,),
        capabilities=(pydantic_ai_capability(Capability[AgentDependencies](id='pictures', toolsets=[toolset])),),
    )

    result = await agent.run('Show.', deps=_deps())

    assert sorted(hook.results, key=lambda item: str(item.content)) == [
        ToolResult(content=None, files=(OvidBinaryContent(data=b'cG5n', media_type='image/png'),)),
        ToolResult(
            content='image 1', metadata={'source': 'camera'}, files=(ImageUrl(url='https://example.com/a.png'),)
        ),
    ]
    returns = {call_id: content for call_id, outcome, content in tool_returns(result.messages) if outcome == 'success'}
    assert isinstance(returns['f1'], list)
    assert returns['f1'][0] == 'image 1'
    assert returns.keys() == {'f1', 'u1'}


@pytest.mark.parametrize('tool_failures', ['return', 'raise'])
async def test_on_tool_error_recovers_a_timeout_with_a_success_result(
    tool_failures: Literal['return', 'raise'],
) -> None:
    hook = RecoveryHook(lambda error: ToolResult(content='cached') if isinstance(error, ToolTimeoutError) else None)
    policy = AgentRunPolicy(retries=AgentRetryPolicy(tool_failures=tool_failures))
    model = ScriptedModel(('slow', {'value': 1}, 't1'))
    agent = await deferred_agent(model, (SlowTool(),), hooks=(hook,), policy=policy)

    result = await agent.run('Wait.', deps=_deps())

    assert tool_returns(result.messages) == [('t1', 'success', 'cached')]
    assert [type(error) for error in hook.errors] == [ToolTimeoutError]


async def test_failing_after_hook_fails_the_call_without_the_original_result() -> None:
    calls: list[int] = []
    model = ScriptedModel(('lookup', {'value': 1}, 'p1'))
    capabilities = (lookup_capability(calls, text='private'),)
    agent = await deferred_agent(model, (), hooks=(ResultHook(_raise),), capabilities=capabilities)
    raising = await deferred_agent(
        ScriptedModel(('lookup', {'value': 1}, 'p1')),
        (),
        hooks=(ResultHook(_raise),),
        capabilities=capabilities,
        policy=AgentRunPolicy(retries=AgentRetryPolicy(tool_failures='raise')),
    )

    result = await agent.run('Look up.', deps=_deps())
    with pytest.raises(ToolExecutionError, match=r'^Tool call failed: hook ResultHook failed\.$'):
        await raising.run('Look up.', deps=_deps())

    assert tool_returns(result.messages) == [('p1', 'failed', 'Tool call failed: hook ResultHook failed.')]
    assert 'private' not in result.model_dump_json()
    assert calls == [1, 1]


async def test_failing_error_hook_fails_the_call_with_a_generic_message() -> None:
    def fail(error: ToolExecutionError) -> ToolResult:
        raise RuntimeError(str(error))

    model = ScriptedModel(('secret', {'value': 1}, 'e1'))
    agent = await deferred_agent(model, (SecretTool(),), hooks=(DecisionHook(), RecoveryHook(fail)))

    result = await agent.run('Fail.', deps=_deps())

    assert tool_returns(result.messages) == [('e1', 'failed', 'Tool call failed: hook RecoveryHook failed.')]
    assert 'private failure' not in result.model_dump_json()


async def test_capability_hooks_apply_only_to_the_tools_of_their_capability_after_agent_hooks() -> None:
    class AgentHook(DecisionHook):
        pass

    class FirstHook(DecisionHook):
        pass

    log: list[str] = []
    agent_hook = AgentHook(log=log)
    first_hook = FirstHook(log=log)
    second_hook = DecisionHook(log=log)
    capabilities = (
        BaseCapability[AgentDependencies](
            id='first',
            contributions=CapabilityContributions(tools=(FileTool(),), hooks=(first_hook,)),
        ),
        BaseCapability[AgentDependencies](
            id='second',
            contributions=CapabilityContributions(tools=(SlowTool(),), hooks=(second_hook,)),
        ),
    )
    model = ScriptedModel(('file', {'value': 1}, 'f1'), ('secret', {'value': 1}, 'e1'))
    agent = await deferred_agent(model, (SecretTool(),), hooks=(agent_hook,), capabilities=capabilities)

    await agent.run('Use the tools.', deps=_deps())

    assert sorted((call.tool_id, call.capability_id) for call in agent_hook.calls) == [
        ('file', 'first'),
        ('secret', None),
    ]
    assert [call.tool_id for call in first_hook.calls] == ['file']
    assert second_hook.calls == []
    assert log.index('AgentHook:file') < log.index('FirstHook:file')
