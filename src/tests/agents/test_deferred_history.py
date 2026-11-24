from pydantic_ai import RunContext
from pydantic_ai.capabilities import HandleDeferredToolCalls, Toolset
from pydantic_ai.tools import DeferredToolRequests as PydanticDeferredToolRequests
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai.toolsets import FunctionToolset

from ovid_core import InMemoryConversationStore
from ovid_core.adapters.pydantic_ai import pydantic_ai_capability
from ovid_core.runtime import ConversationId, DeferredRunResult, RunResult
from ovid_core.tools import ToolCallApproved
from tests.support.agent_consumer import AgentDependencies
from tests.support.deferred_consumer import DangerTool, SafeTool, ScriptedModel, deferred_agent, tool_returns


_INTERRUPTED = 'The tool call was interrupted before a result was produced.'


def _deps() -> AgentDependencies:
    return AgentDependencies(prefix='history')


async def test_new_prompt_interrupts_calls_pending_at_end_of_history() -> None:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'))
    danger = DangerTool()
    agent = await deferred_agent(model, (danger,))
    paused = await agent.run('Delete.', deps=_deps())
    assert isinstance(paused, DeferredRunResult)
    assert paused.history[-1].role == 'response'

    result = await agent.run('Never mind.', deps=_deps(), messages=paused.history)

    assert isinstance(result, RunResult)
    assert result.output == 'done'
    assert tool_returns(result.messages) == [('c1', 'interrupted', _INTERRUPTED)]
    assert result.history[: len(paused.history)] == paused.history
    assert danger.calls == []


async def test_new_prompt_interrupts_mixed_calls_after_store_round_trip() -> None:
    model = ScriptedModel(('safe', {'value': 1}, 's1'), ('danger', {'value': 2}, 'c1'))
    safe = SafeTool()
    danger = DangerTool()
    agent = await deferred_agent(model, (safe, danger))
    store = InMemoryConversationStore()
    paused = await agent.run('Mix.', deps=_deps())
    assert isinstance(paused, DeferredRunResult)
    conversation_id = ConversationId.new()
    await store.commit(conversation_id, paused.messages, paused.history)
    stored = await store.load(conversation_id)

    resumed = await agent.run(
        resume=paused.requests.resume({'c1': ToolCallApproved()}),
        deps=_deps(),
        messages=stored,
    )
    interrupted = await agent.run('Never mind.', deps=_deps(), messages=stored)

    assert isinstance(resumed, RunResult)
    assert isinstance(interrupted, RunResult)
    assert interrupted.output == 'done'
    assert [(call_id, outcome) for call_id, outcome, _ in tool_returns(interrupted.history)] == [
        ('s1', 'success'),
        ('c1', 'interrupted'),
    ]
    assert [(call_id, outcome) for call_id, outcome, _ in tool_returns(interrupted.messages)] == [('c1', 'interrupted')]
    assert safe.calls == [(1, False)]
    assert danger.calls == [(2, True)]


async def test_inline_deferred_handler_completes_run() -> None:
    async def approve_all(
        ctx: RunContext[AgentDependencies],
        requests: PydanticDeferredToolRequests,
    ) -> DeferredToolResults:
        del ctx
        return requests.build_results(approve_all=True)

    model = ScriptedModel(('danger', {'value': 1}, 'c1'))
    danger = DangerTool()
    handler = pydantic_ai_capability(HandleDeferredToolCalls[AgentDependencies](handler=approve_all))
    agent = await deferred_agent(model, (danger,), capabilities=(handler,))

    result = await agent.run('Delete.', deps=_deps())

    assert isinstance(result, RunResult)
    assert result.output == 'done'
    assert danger.calls == [(1, True)]


async def test_non_ovid_approval_tool_has_no_ovid_approval() -> None:
    def remove(value: int) -> str:
        return f'removed {value}'

    toolset = FunctionToolset[AgentDependencies]()
    toolset.add_function(remove, requires_approval=True)
    model = ScriptedModel(('remove', {'value': 1}, 'r1'))
    agent = await deferred_agent(model, (), capabilities=(pydantic_ai_capability(Toolset(toolset)),))

    result = await agent.run('Remove.', deps=_deps())

    assert isinstance(result, DeferredRunResult)
    assert [(call.kind, call.approval) for call in result.requests.calls] == [('approval', None)]
