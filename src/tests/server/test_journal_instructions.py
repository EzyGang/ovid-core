from collections.abc import AsyncIterator, Callable, Sequence

import pytest
from pydantic_ai import RunContext as PydanticRunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import InstructionPart, ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from ovid_core import AgentDefinition
from ovid_core.adapters.pydantic_ai import pydantic_ai_capability
from ovid_core.capabilities import BaseCapability
from ovid_core.messages import AgentMessage, OpaqueRequestPart
from ovid_core.persistence import ConversationStore
from ovid_core.routing import ModelRef
from ovid_core.runtime import ConversationId
from ovid_core.server import AgentRegistration, AgentRunRequest
from tests.server.server_helpers import dependencies
from tests.support.agent_consumer import AgentDependencies, ConsumerToolset
from tests.support.agent_helpers import agent_factory
from tests.support.deferred_consumer import DangerTool, SafeTool
from tests.support.journal_consumer import PlainHistoryStore, TranscriptStore, run_request
from tests.support.partial_consumer import shape, stdio_session


_CONVERSATION_ID = ConversationId.new()

type ModelView = tuple[str | None, tuple[str, ...]]


class NotesCapability(AbstractCapability[AgentDependencies]):
    """Add an instruction block that appends its changes, so each new window records an instruction baseline."""

    def __init__(self) -> None:
        self.id = 'notes'

    def get_instructions(self) -> Callable[[PydanticRunContext[AgentDependencies]], InstructionPart]:
        def notes(ctx: PydanticRunContext[AgentDependencies]) -> InstructionPart:
            return InstructionPart(content=f'Keep notes for {ctx.deps.prefix}.', name='notes', on_change='append')

        return notes


def _view(messages: Sequence[ModelMessage]) -> tuple[str, ...]:
    return tuple(part.part_kind for message in messages for part in message.parts)


def _recorded(messages: Sequence[AgentMessage]) -> list[tuple[str, ...]]:
    return [
        tuple(part.part_kind for part in message.parts if isinstance(part, OpaqueRequestPart)) for message in messages
    ]


async def _run(
    case: str, capabilities: tuple[BaseCapability[AgentDependencies], ...], store: ConversationStore
) -> list[ModelView]:
    views: list[ModelView] = []
    calls = (('safe', 'c1'), ('danger', 'c2')) if case == 'deferred' else (('safe', 'c1'),)

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        views.append((info.instructions, _view(messages)))
        if len(views) == 1:
            yield {
                index: DeltaToolCall(name=name, json_args='{"value": 1}', tool_call_id=call_id)
                for index, (name, call_id) in enumerate(calls)
            }
            return
        if len(views) == 2 and case == 'partial':
            raise ModelAPIError('partial', 'provider down')
        yield 'done'

    definition = AgentDefinition[AgentDependencies, str](
        model=ModelRef(name='primary'),
        deps_type=AgentDependencies,
        output_type=str,
        instructions=('Answer briefly.',),
        toolsets=(ConsumerToolset((SafeTool(), DangerTool())),),
        capabilities=capabilities,
    )
    model = FunctionModel(stream_function=stream, model_name='instructions')
    agent = await agent_factory({'primary': model}).build(definition)
    registration = AgentRegistration(id='writer', description='Write.', agent=agent, dependencies=dependencies)
    first = AgentRunRequest(prompt='Go.', conversation_id=_CONVERSATION_ID)
    second = AgentRunRequest(prompt='Next.', conversation_id=_CONVERSATION_ID)

    await stdio_session(registration, store, run_request('r1', first), 'r1', run_request('r2', second), 'r2')

    return views


@pytest.mark.parametrize('case', ['completed', 'partial', 'deferred'])
@pytest.mark.parametrize(
    'capabilities', [(), (pydantic_ai_capability(NotesCapability()),)], ids=['agent', 'capability']
)
async def test_journal_stores_requests_with_their_instruction_records_once(
    case: str,
    capabilities: tuple[BaseCapability[AgentDependencies], ...],
) -> None:
    store = TranscriptStore()
    plain = PlainHistoryStore()

    journaled_views = await _run(case, capabilities, store)
    plain_views = await _run(case, capabilities, plain)

    stored = await store.load(_CONVERSATION_ID)
    assert store.transcript == stored
    assert shape(stored) == shape(plain.messages)
    assert _recorded(stored) == _recorded(plain.messages)
    assert journaled_views == plain_views
    assert any('instruction-baseline' in kinds for kinds in _recorded(stored)) == bool(capabilities)
    assert [run.outcome for run in (await store.load_journal(_CONVERSATION_ID)).runs] == [
        {'completed': 'completed', 'partial': 'failed', 'deferred': 'deferred'}[case],
        'completed',
    ]
