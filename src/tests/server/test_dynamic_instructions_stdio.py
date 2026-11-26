import itertools
from collections.abc import AsyncIterator, Sequence

from pydantic import JsonValue
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCalls, FunctionModel

from ovid_core import AgentDefinition
from ovid_core.capabilities import BaseCapability, CapabilityContributions, InstructionSource
from ovid_core.messages import AgentMessage
from ovid_core.persistence import ConversationStore
from ovid_core.routing import ModelRef
from ovid_core.runtime import ConversationId, RunContext
from ovid_core.server import AgentRegistration, AgentRunRequest
from tests.server.server_helpers import dependencies
from tests.support.agent_consumer import AgentDependencies, ConsumerToolset
from tests.support.agent_helpers import agent_factory
from tests.support.deferred_consumer import SafeTool
from tests.support.journal_consumer import PlainHistoryStore, TranscriptStore, run_request
from tests.support.partial_consumer import boundary, failure, shape, stdio_session, tool_call


_CONVERSATION_ID = ConversationId.new()


def _instructions(messages: Sequence[AgentMessage]) -> list[str | None]:
    return [message.instructions for message in messages]


async def _session(
    store: ConversationStore, source: InstructionSource[AgentDependencies], *prompts: str
) -> tuple[list[str | None], list[dict[str, JsonValue]]]:
    """Run each prompt on one conversation with a model that calls the safe tool once, then answers."""
    seen: list[str | None] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del messages
        seen.append(info.instructions)
        if len(seen) % 2:
            yield tool_call('safe', f'c{len(seen)}')
            return
        yield 'done'

    definition = AgentDefinition[AgentDependencies, str](
        model=ModelRef(name='primary'),
        deps_type=AgentDependencies,
        output_type=str,
        instructions=('Answer briefly.',),
        toolsets=(ConsumerToolset((SafeTool(),)),),
        capabilities=(BaseCapability(id='clock', contributions=CapabilityContributions(instructions=(source,))),),
    )
    model = FunctionModel(stream_function=stream, model_name='dynamic')
    agent = await agent_factory({'primary': model}).build(definition)
    registration = AgentRegistration(id='writer', description='Write.', agent=agent, dependencies=dependencies)
    steps = [
        step
        for index, prompt in enumerate(prompts)
        for step in (
            run_request(f'r{index}', AgentRunRequest(prompt=prompt, conversation_id=_CONVERSATION_ID)),
            f'r{index}',
        )
    ]

    frames = await stdio_session(registration, store, *steps)

    return seen, frames


async def _ticking_session(store: ConversationStore) -> list[str | None]:
    ticks = itertools.count(1)

    def clock(ctx: RunContext[AgentDependencies]) -> str:
        return f'tick={next(ticks)} step={ctx.run_step}'

    seen, _ = await _session(store, clock, 'Go.', 'Next.')
    return seen


async def test_journal_stores_dynamic_instruction_requests_once_and_reevaluates_next_run() -> None:
    store = TranscriptStore()
    plain = PlainHistoryStore()

    journaled_views = await _ticking_session(store)
    plain_views = await _ticking_session(plain)

    stored = await store.load(_CONVERSATION_ID)
    expected = [f'Answer briefly.\n\ntick={tick} step={step}' for tick, step in ((1, 1), (2, 2), (3, 1), (4, 2))]
    assert journaled_views == plain_views == expected
    assert store.transcript == stored
    assert shape(stored) == shape(plain.messages)
    assert _instructions(stored) == _instructions(plain.messages)
    assert _instructions(stored) == [expected[0], None, expected[1], None, expected[2], None, expected[3], None]


async def test_stdio_commits_partial_transcript_when_instruction_source_raises() -> None:
    def broken(ctx: RunContext[AgentDependencies]) -> str:
        if ctx.run_step > 1:
            raise RuntimeError('private source detail')
        return 'Steady.'

    store = TranscriptStore()

    seen, frames = await _session(store, broken, 'Go.')

    stored = await store.load(_CONVERSATION_ID)
    assert shape(stored) == [
        ('request', 'complete', ('user_prompt:Go.',)),
        ('response', 'complete', ('call:c1',)),
        ('request', 'complete', ('return:c1:success',)),
        boundary('failed: Agent run failed'),
    ]
    assert failure(frames, 'r0') == ([True], 'agent_run_failed')
    assert [run.outcome for run in (await store.load_journal(_CONVERSATION_ID)).runs] == ['failed']
    assert seen == ['Answer briefly.\n\nSteady.']
    assert 'private' not in str(stored)
