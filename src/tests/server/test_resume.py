from ovid_core import AgentRunPolicy, InMemoryConversationStore
from ovid_core.messages import UserPromptPart
from ovid_core.policy import AgentRetryPolicy, AgentUsageLimits
from ovid_core.runtime import ConversationId
from ovid_core.tools import ToolEffect
from tests.support.deferred_consumer import DangerTool, ScriptedModel, deferred_registration
from tests.support.journal_consumer import PlainHistoryStore, ReadSnapshotTool
from tests.support.partial_consumer import boundary, registration, shape, stdio_session
from tests.support.resume_consumer import (
    UNKNOWN,
    FlakyLookTool,
    LookTool,
    UnsafeLookTool,
    WriteTool,
    crashed_store,
    outcomes,
    prompt_request,
    resume_request,
    returns,
)


_SAFE_READ = ToolEffect(kind='read', replay='safe')


async def test_resume_after_kill_runs_safe_read_once_and_answers_unsafe_write_as_unknown() -> None:
    conversation_id = ConversationId.new()
    store = await crashed_store(conversation_id, ('look', _SAFE_READ), ('write', ToolEffect(kind='workspace_write')))
    look, write = LookTool(), WriteTool()
    model = ScriptedModel()
    agent = await deferred_registration(model, (look, write))

    frames = await stdio_session(agent, store, resume_request('r1', conversation_id), 'r1')

    stored = await store.load(conversation_id)
    assert outcomes(frames) == ['done']
    assert look.calls == [(1, False)]
    assert write.calls == []
    assert sorted(returns(stored)) == [('c1', 'look 1'), ('c2', UNKNOWN.format('write'))]
    assert sum(isinstance(part, UserPromptPart) for message in stored for part in message.parts) == 1
    assert 'UNKNOWN' in str(model.requests[-1])
    assert [run.outcome for run in (await store.load_journal(conversation_id)).runs] == ['lost', 'completed']


async def test_safe_journal_intent_whose_tool_is_now_unsafe_is_not_run_again() -> None:
    conversation_id = ConversationId.new()
    store = await crashed_store(conversation_id, ('look', _SAFE_READ))
    look = UnsafeLookTool()
    agent = await deferred_registration(ScriptedModel(), (look,))

    frames = await stdio_session(agent, store, resume_request('r1', conversation_id), 'r1')

    assert outcomes(frames) == ['done']
    assert look.calls == []
    assert returns(await store.load(conversation_id)) == [('c1', UNKNOWN.format('look'))]


async def test_third_consecutive_lost_run_refuses_resume_and_a_new_prompt_still_works() -> None:
    conversation_id = ConversationId.new()
    store = InMemoryConversationStore()
    tool = ReadSnapshotTool(store)
    agent = await deferred_registration(ScriptedModel(('look', {'value': 1}, 'c1')), (tool,))

    await stdio_session(agent, store, prompt_request('r1', conversation_id, 'Look.'), 'r1')
    disk = tool.snapshot
    for request_id in ('r2', 'r3'):
        assert disk is not None
        tool.store = disk
        await stdio_session(agent, disk, resume_request(request_id, conversation_id), request_id)
        disk = tool.snapshot
    assert disk is not None
    frames = await stdio_session(
        agent, disk, resume_request('r4', conversation_id), 'r4', prompt_request('r5', conversation_id, 'Next.'), 'r5'
    )

    errors = [frame['error'] for frame in frames if frame['type'] == 'error']
    assert outcomes(frames) == ['resume_limit_exceeded', 'done']
    assert errors == [{'code': 'resume_limit_exceeded', 'message': 'Run was interrupted 3 times. Send a new prompt.'}]
    assert len(tool.calls) == 3
    assert [run.outcome for run in (await disk.load_journal(conversation_id)).runs] == [
        'lost',
        'lost',
        'lost',
        'completed',
    ]


async def test_resume_without_an_interrupted_run_is_refused() -> None:
    conversation_id = ConversationId.new()
    agent = await deferred_registration(ScriptedModel(), ())

    frames = await stdio_session(
        agent,
        InMemoryConversationStore(),
        resume_request('r1', conversation_id),
        'r1',
        prompt_request('r2', conversation_id, 'Hello.'),
        'r2',
        resume_request('r3', conversation_id),
        'r3',
    )

    assert outcomes(frames) == ['nothing_to_resume', 'done', 'nothing_to_resume']


async def test_resume_with_pending_deferred_calls_is_refused() -> None:
    conversation_id = ConversationId.new()
    danger = DangerTool()
    agent = await deferred_registration(ScriptedModel(('danger', {'value': 1}, 'c1')), (danger,))

    frames = await stdio_session(
        agent,
        InMemoryConversationStore(),
        prompt_request('r1', conversation_id, 'Delete.'),
        'r1',
        resume_request('r2', conversation_id),
        'r2',
    )

    errors = [frame['error'] for frame in frames if frame['type'] == 'error']
    assert outcomes(frames) == [None, 'nothing_to_resume']
    assert errors == [
        {'code': 'nothing_to_resume', 'message': 'The conversation has pending tool calls. Send answers for them.'}
    ]
    assert danger.calls == []


async def test_resume_after_tool_failure_runs_the_safe_call_again() -> None:
    conversation_id = ConversationId.new()
    store = InMemoryConversationStore()
    look = FlakyLookTool()
    model = ScriptedModel(('look', {'value': 1}, 'c1'))
    policy = AgentRunPolicy(retries=AgentRetryPolicy(tool_failures='raise'))
    agent = await registration(model.stream, (look,), policy=policy)

    frames = await stdio_session(
        agent,
        store,
        prompt_request('r1', conversation_id, 'Look.'),
        'r1',
        resume_request('r2', conversation_id),
        'r2',
    )

    assert outcomes(frames) == ['server_failure', 'done']
    assert look.calls == [(1, False), (1, False)]
    assert shape(await store.load(conversation_id)) == [
        ('request', 'complete', ('user_prompt:Look.',)),
        ('response', 'complete', ('call:c1',)),
        ('request', 'complete', ('return:c1:success',)),
        ('response', 'complete', ('text:done',)),
    ]
    assert [run.outcome for run in (await store.load_journal(conversation_id)).runs] == ['failed', 'completed']


async def test_resume_after_usage_limit_without_journal_drops_boundary_and_adds_no_prompt() -> None:
    conversation_id = ConversationId.new()
    store = PlainHistoryStore()
    look = LookTool()
    model = ScriptedModel(('look', {'value': 1}, 'c1'))
    agent = await registration(model.stream, (look,), policy=AgentRunPolicy(limits=AgentUsageLimits(requests=1)))

    await stdio_session(agent, store, prompt_request('r1', conversation_id, 'Look.'), 'r1')
    stopped = shape(store.messages)
    frames = await stdio_session(agent, store, resume_request('r2', conversation_id), 'r2')

    assert stopped[-1] == boundary('usage_limit: Agent usage limit exceeded')
    assert outcomes(frames) == ['done']
    assert '[Run ended' not in str(model.requests[-1])
    assert look.calls == [(1, False)]
    assert shape(store.messages) == [*stopped[:-1], ('response', 'complete', ('text:done',))]
