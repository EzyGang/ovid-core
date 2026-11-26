import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from pydantic_ai.capabilities import AbstractCapability, CombinedCapability, Toolset
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from ovid_core import ToolExecutionError, ToolRetryError
from ovid_core.adapters.pydantic_ai import PydanticAIToolsetAdapter, pydantic_ai_capability
from ovid_core.adapters.pydantic_ai._deferred_tools import StaticApprovalRecorder
from ovid_core.adapters.pydantic_ai._journal import TranscriptJournalCapability
from ovid_core.adapters.pydantic_ai._usage_tracking import RunUsageRecorder, UsageTrackingCapability
from ovid_core.errors import ToolApprovalRequired
from ovid_core.mcp import MCPServerConfig, MCPStdioTransportConfig, MCPValues, create_mcp_capability
from ovid_core.persistence import RunJournal
from ovid_core.runtime import ConversationId, RunId
from ovid_core.tools import ToolEffect, ToolExecutionContext
from tests.support.agent_consumer import AgentDependencies, ConsumerToolset
from tests.support.agent_helpers import agent_factory
from tests.support.deferred_consumer import RecordingTool, SafeTool, ScriptedModel, ValueArguments, deferred_agent
from tests.support.integration_consumer import integration_definition
from tests.support.journal_consumer import TranscriptStore


_CONVERSATION_ID = ConversationId.new()
_MCP_SERVER = Path(__file__).parents[1] / 'mcp' / 'mcp_server.py'


class KeyedTool(SafeTool):
    def __init__(self, store: TranscriptStore) -> None:
        super().__init__()
        self.store = store

    def idempotency_key(self, arguments: ValueArguments) -> str | None:
        return f'value-{arguments.value}'

    def defer(self, context: ToolExecutionContext[AgentDependencies]) -> None:
        del context
        self.store.events.append('execute')


class FailingTool(RecordingTool):
    id = 'fail'

    def defer(self, context: ToolExecutionContext[AgentDependencies]) -> None:
        del context
        raise ToolExecutionError('disk full')


class RetryingTool(RecordingTool):
    id = 'retry'

    def defer(self, context: ToolExecutionContext[AgentDependencies]) -> None:
        del context
        raise ToolRetryError('try again')


class CheckTool(RecordingTool):
    id = 'check'

    def defer(self, context: ToolExecutionContext[AgentDependencies]) -> None:
        del context
        raise ToolApprovalRequired


def echo(text: str) -> str:
    return text


async def test_intent_append_returns_before_the_tool_starts() -> None:
    store = TranscriptStore()
    agent = await deferred_agent(ScriptedModel(('safe', {'value': 3}, 'c1')), (KeyedTool(store),))
    journal = RunJournal(store=store, conversation_id=_CONVERSATION_ID)

    async with agent.stream(
        'Go.', deps=AgentDependencies(prefix='p'), conversation_id=_CONVERSATION_ID, journal=journal
    ) as stream:
        async for _ in stream:
            pass

    [call] = (await store.load_journal(_CONVERSATION_ID)).tool_calls
    assert store.events == [
        'run_started',
        'step_committed',
        'step_committed',
        'tool_intent',
        'execute',
        'tool_settled',
        'step_committed',
        'step_committed',
    ]
    assert store.transcript == journal.messages == stream.result.messages
    assert (call.tool_name, call.arguments, call.effect, call.idempotency_key) == (
        'safe',
        {'value': 3},
        ToolEffect(),
        'value-3',
    )
    assert (call.status, call.outcome) == ('completed', 'success')


async def test_tool_failure_retry_and_deferral_produce_settle_entries() -> None:
    store = TranscriptStore()
    model = ScriptedModel(
        ('fail', {'value': 1}, 'c1'),
        ('retry', {'value': 1}, 'c2'),
        ('safe', {'value': 'not a number'}, 'c3'),
        ('check', {'value': 1}, 'c4'),
    )
    agent = await deferred_agent(model, (FailingTool(), RetryingTool(), SafeTool(), CheckTool()))
    journal = RunJournal(store=store, conversation_id=_CONVERSATION_ID)

    async with agent.stream(
        'Go.', deps=AgentDependencies(prefix='p'), conversation_id=_CONVERSATION_ID, journal=journal
    ) as stream:
        async for _ in stream:
            pass

    calls = (await store.load_journal(_CONVERSATION_ID)).tool_calls
    assert sorted((call.tool_call_id, call.status, call.outcome, call.idempotency_key) for call in calls) == [
        ('c1', 'failed', 'failed', None),
        ('c2', 'failed', 'retried', None),
        ('c3', 'failed', 'retried', None),
        ('c4', 'deferred', None, None),
    ]
    assert all(call.ended_at is not None for call in calls)


async def test_passthrough_and_mcp_tool_calls_get_intent_entries() -> None:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del info
        if any(isinstance(message, ModelResponse) for message in messages):
            yield 'done'
            return
        yield {
            0: DeltaToolCall(name='echo', json_args=json.dumps({'text': 'hi'}), tool_call_id='p1'),
            1: DeltaToolCall(name='server_credential_loaded', json_args='{}', tool_call_id='m1'),
        }

    mcp = await create_mcp_capability(
        MCPServerConfig(
            id='test-mcp',
            transport=MCPStdioTransportConfig(
                command=sys.executable, args=(str(_MCP_SERVER),), environment=MCPValues()
            ),
            include_tools=('credential_loaded',),
            namespace='server',
        )
    )
    passthrough = pydantic_ai_capability(Toolset(FunctionToolset([echo]), id='echo'))
    definition = integration_definition((passthrough, mcp))
    agent = await agent_factory({'primary': FunctionModel(stream_function=stream)}).build(definition)
    store = TranscriptStore()

    async with agent.stream(
        'Go.',
        deps=None,
        conversation_id=_CONVERSATION_ID,
        journal=RunJournal(store=store, conversation_id=_CONVERSATION_ID),
    ) as run:
        async for _ in run:
            pass

    calls = (await store.load_journal(_CONVERSATION_ID)).tool_calls
    assert sorted((call.tool_name, call.tool_call_id, call.status, call.idempotency_key) for call in calls) == [
        ('echo', 'p1', 'completed', None),
        ('server_credential_loaded', 'm1', 'completed', None),
    ]
    assert [call.arguments for call in sorted(calls, key=lambda call: call.tool_name)] == [{'text': 'hi'}, {}]


def test_toolset_adapter_has_no_idempotency_key_for_an_unlisted_tool() -> None:
    adapter = PydanticAIToolsetAdapter[AgentDependencies](source=ConsumerToolset(()))

    assert adapter.idempotency_key('missing', {'value': 1}) is None


def test_journal_capability_runs_inside_usage_tracking_and_outside_other_capabilities() -> None:
    journal = TranscriptJournalCapability[None](
        journal=RunJournal(store=TranscriptStore(), conversation_id=_CONVERSATION_ID),
        run_id=RunId.new(),
        closing=None,
    )
    usage = UsageTrackingCapability[None](tracker=None, recorder=RunUsageRecorder(tracker=None))
    other = AbstractCapability[None]()

    combined = CombinedCapability[None]([other, journal, StaticApprovalRecorder[None](), usage])

    assert [type(capability) for capability in combined.capabilities] == [
        UsageTrackingCapability,
        TranscriptJournalCapability,
        AbstractCapability,
        StaticApprovalRecorder,
    ]
