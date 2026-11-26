from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from ovid_core import AgentDefinition
from ovid_core.capabilities import HistoryProcessorCapability
from ovid_core.messages import AgentMessage
from ovid_core.persistence import ConversationStore
from ovid_core.routing import ModelRef
from ovid_core.runtime import ConversationId, RunContext
from ovid_core.server import AgentRegistration, AgentRunRequest
from ovid_core.tools import BaseTool
from tests.server.server_helpers import dependencies
from tests.support.agent_consumer import AgentDependencies, ConsumerToolset
from tests.support.agent_helpers import agent_factory
from tests.support.deferred_consumer import DangerTool, SafeTool
from tests.support.journal_consumer import PlainHistoryStore, TranscriptStore, run_request
from tests.support.partial_consumer import AppendOnlyStore, StreamFunction, boundary, registration, shape, stdio_session


_CONVERSATION_ID = ConversationId.new()


class CountingAppendStore(AppendOnlyStore):
    def __init__(self) -> None:
        super().__init__()
        self.writes = 0

    async def append(self, conversation_id: ConversationId, messages: tuple[AgentMessage, ...]) -> None:
        await super().append(conversation_id, messages)
        self.writes += 1


class BrokenToolset(ConsumerToolset):
    async def get_tools(
        self, context: RunContext[AgentDependencies]
    ) -> Sequence[BaseTool[AgentDependencies, Any, Any]]:
        del context
        raise RuntimeError('listing failed')


def _script(case: str) -> StreamFunction:
    calls = {'deferred': (('safe', 'c1'), ('danger', 'c2'))}.get(case, (('safe', 'c1'),))

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del info
        if not any(isinstance(message, ModelResponse) for message in messages):
            yield {
                index: DeltaToolCall(name=name, json_args='{"value": 1}', tool_call_id=call_id)
                for index, (name, call_id) in enumerate(calls)
            }
            return
        if case == 'partial':
            raise ModelAPIError('partial', 'provider down')
        yield 'done'

    return stream


async def _run(case: str, store: ConversationStore) -> None:
    agent = await registration(_script(case), (SafeTool(), DangerTool()))
    request = AgentRunRequest(prompt='Go.', conversation_id=_CONVERSATION_ID)

    await stdio_session(agent, store, run_request('r1', request), 'r1')


@pytest.mark.parametrize(
    ('case', 'outcome'), [('completed', 'completed'), ('partial', 'failed'), ('deferred', 'deferred')]
)
async def test_step_commits_store_each_run_message_once(case: str, outcome: str) -> None:
    store = TranscriptStore()
    plain = PlainHistoryStore()

    await _run(case, store)
    await _run(case, plain)

    stored = await store.load(_CONVERSATION_ID)
    assert store.transcript == stored
    assert shape(stored) == shape(plain.messages)
    assert store.events[-1] == 'run_ended'
    assert [run.outcome for run in (await store.load_journal(_CONVERSATION_ID)).runs] == [outcome]
    assert (await store.load_deferred_requests(_CONVERSATION_ID) is not None) == (case == 'deferred')


@pytest.mark.parametrize('case', ['completed', 'partial'])
async def test_store_without_journal_gets_one_write_per_run(case: str) -> None:
    plain = PlainHistoryStore()
    append_only = CountingAppendStore()

    await _run(case, plain)
    await _run(case, append_only)

    assert plain.writes == ['commit']
    assert append_only.writes == 1
    assert shape(append_only.messages) == shape(plain.messages)
    assert shape(plain.messages)[-1] == (
        boundary('failed: Provider request failed (unavailable)')
        if case == 'partial'
        else ('response', 'complete', ('text:done',))
    )


async def _journal_run(definition: AgentDefinition[AgentDependencies, str], stream: StreamFunction) -> TranscriptStore:
    model = FunctionModel(stream_function=stream, model_name='journal')
    agent = await agent_factory({'primary': model}).build(definition)
    store = TranscriptStore()
    registered = AgentRegistration(id='writer', description='Write.', agent=agent, dependencies=dependencies)
    request = AgentRunRequest(prompt='Go.', conversation_id=_CONVERSATION_ID)

    await stdio_session(registered, store, run_request('r1', request), 'r1')

    return store


async def test_run_that_writes_no_journal_entry_leaves_no_run_in_the_journal() -> None:
    definition = AgentDefinition[AgentDependencies, str](
        model=ModelRef(name='primary'),
        deps_type=AgentDependencies,
        output_type=str,
        toolsets=(BrokenToolset(()),),
    )

    store = await _journal_run(definition, _script('completed'))

    assert shape(await store.load(_CONVERSATION_ID)) == [
        ('request', 'complete', ('user_prompt:Go.',)),
        boundary('failed: Agent run failed'),
    ]
    assert (await store.load_journal(_CONVERSATION_ID)).runs == ()
    assert store.events == ['commit']


async def test_step_commits_hold_each_message_once_when_a_history_processor_drops_run_messages() -> None:
    responses: list[int] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del messages, info
        responses.append(len(responses))
        if len(responses) > 3:
            yield 'done'
            return
        yield {0: DeltaToolCall(name='safe', json_args='{"value": 1}', tool_call_id=f'c{len(responses)}')}

    async def drop_prompts(messages: tuple[AgentMessage, ...]) -> tuple[AgentMessage, ...]:
        if len(messages) < 4:
            return messages

        return tuple(message for message in messages if not any(part.kind == 'user_prompt' for part in message.parts))

    definition = AgentDefinition[AgentDependencies, str](
        model=ModelRef(name='primary'),
        deps_type=AgentDependencies,
        output_type=str,
        toolsets=(ConsumerToolset((SafeTool(),)),),
        capabilities=(HistoryProcessorCapability[AgentDependencies](id='recent', processor=drop_prompts),),
    )

    store = await _journal_run(definition, stream)

    assert shape(store.transcript) == [
        ('request', 'complete', ('user_prompt:Go.',)),
        *(
            item
            for call_id in ('c1', 'c2', 'c3')
            for item in (
                ('response', 'complete', (f'call:{call_id}',)),
                ('request', 'complete', (f'return:{call_id}:success',)),
            )
        ),
        ('response', 'complete', ('text:done',)),
    ]
    assert [run.outcome for run in (await store.load_journal(_CONVERSATION_ID)).runs] == ['completed']
