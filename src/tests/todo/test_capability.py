from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ovid_core import AgentDefinition
from ovid_core.routing.models import ModelRef
from ovid_core.runtime.context import RunContext
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.todo.capability import TODO_INSTRUCTIONS, TodoCapability
from ovid_core.todo.models import TodoArgs, TodoOperation, TodoStatus, TodoToolContent
from ovid_core.todo.service import TodoService
from ovid_core.todo.store import InMemoryTodoStore
from ovid_core.tools.base import ToolExecutionContext
from tests.support.agent_helpers import agent_factory


async def test_capability_contributes_one_fixed_tool_with_complete_results() -> None:
    service = TodoService(store=InMemoryTodoStore())
    capability = TodoCapability[None](service=service)
    tool = capability.contributions.tools[0]
    conversation_id = ConversationId.new()
    context = ToolExecutionContext(
        run=RunContext(deps=None, run_id=RunId.new(), conversation_id=conversation_id),
        tool_call_id='todo-1',
    )

    initialized = TodoToolContent.model_validate(
        (
            await tool.execute(
                context,
                TodoArgs(op='init', phase='Plan', items=('First', 'Second')),
            )
        ).content
    )
    blocked = TodoToolContent.model_validate(
        (await tool.execute(context, TodoArgs(op='block', task='First', reason='waiting'))).content
    )
    viewed = TodoToolContent.model_validate((await tool.execute(context, TodoArgs(op='view'))).content)

    assert capability.id == 'todo'
    assert capability.defer_loading is False
    assert capability.contributions.instructions == (TODO_INSTRUCTIONS,)
    assert capability.contributions.toolsets == ()
    assert len(capability.contributions.tools) == 1
    assert tool.id == 'todo'
    assert tool.approval.required is False
    assert tool.timeout_seconds == 10.0
    assert tool.defer_loading is False
    assert initialized.counts.model_dump() == {'total': 2, 'actionable': 2, 'blocked': 0, 'closed': 0}
    assert initialized.active_phase == 'Plan'
    assert initialized.active_task == 'First'
    assert blocked.counts.model_dump() == {'total': 2, 'actionable': 1, 'blocked': 1, 'closed': 0}
    assert blocked.active_task == 'Second'
    assert viewed.operation == TodoOperation.VIEW
    assert viewed.state == blocked.state


async def test_tool_reports_empty_and_blocked_only_state_without_active_task() -> None:
    service = TodoService(store=InMemoryTodoStore())
    tool = TodoCapability[None](service=service).contributions.tools[0]
    context = ToolExecutionContext(
        run=RunContext(deps=None, run_id=RunId.new(), conversation_id=ConversationId.new()),
        tool_call_id='todo-1',
    )

    empty = TodoToolContent.model_validate((await tool.execute(context, TodoArgs(op='view'))).content)
    await tool.execute(context, TodoArgs(op='init', phase='Plan', items=('Only',)))
    blocked = TodoToolContent.model_validate((await tool.execute(context, TodoArgs(op='block', task='Only'))).content)

    assert empty.counts.total == 0
    assert empty.active_phase is None
    assert empty.active_task is None
    assert blocked.counts.blocked == 1
    assert blocked.counts.actionable == 0
    assert blocked.active_phase is None
    assert blocked.active_task is None


async def test_real_agent_runs_full_todo_flow_and_isolates_conversations() -> None:
    service = TodoService(store=InMemoryTodoStore())
    capability = TodoCapability[None](service=service)
    operations = (
        {
            'op': 'init',
            'list': [
                {'phase': 'Implementation', 'items': ['First', 'Second']},
                {'phase': 'Verification', 'items': ['Third']},
            ],
        },
        {'op': 'done', 'task': 'First'},
        {'op': 'block', 'task': 'Second', 'reason': 'waiting'},
        {'op': 'view'},
    )
    observed_contents: list[TodoToolContent] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if returns:
            observed_contents.append(TodoToolContent.model_validate(returns[-1].content))

        if len(returns) < len(operations):
            assert [tool.name for tool in info.function_tools] == ['todo']
            return ModelResponse(
                parts=(ToolCallPart('todo', operations[len(returns)], f'todo-{len(returns)}'),),
            )

        return ModelResponse(parts=(TextPart('todo complete'),))

    definition = AgentDefinition[None, str](
        model=ModelRef(name='primary'),
        deps_type=type(None),
        output_type=str,
        capabilities=(capability,),
    )
    agent = await agent_factory({'primary': FunctionModel(respond, model_name='todo')}).build(definition)
    conversation_id = ConversationId.new()

    result = await agent.run('Track this work.', deps=None, conversation_id=conversation_id)
    state = await service.view(conversation_id)
    isolated = await service.view(ConversationId.new())

    assert result.output == 'todo complete'
    assert len(observed_contents) == 4
    assert observed_contents[1].active_task == 'Second'
    assert observed_contents[2].active_task == 'Third'
    assert observed_contents[3].operation == TodoOperation.VIEW
    assert [(task.content, task.status) for phase in state.phases for task in phase.tasks] == [
        ('First', TodoStatus.COMPLETED),
        ('Second', TodoStatus.BLOCKED),
        ('Third', TodoStatus.IN_PROGRESS),
    ]
    assert isolated.phases == ()


async def test_real_agent_continues_after_todo_target_is_not_found() -> None:
    capability = TodoCapability[None](service=TodoService(store=InMemoryTodoStore()))
    responses = iter(
        (
            ModelResponse(parts=(ToolCallPart('todo', {'op': 'start', 'task': 'Absent task'}, 'todo-0'),)),
            ModelResponse(parts=(TextPart('recovered'),)),
        )
    )

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return next(responses)

    definition = AgentDefinition[None, str](
        model=ModelRef(name='primary'),
        deps_type=type(None),
        output_type=str,
        capabilities=(capability,),
    )
    agent = await agent_factory({'primary': FunctionModel(respond, model_name='todo')}).build(definition)

    result = await agent.run('Track this work.', deps=None)
    returns = [part for message in result.messages for part in message.parts if part.kind == 'tool_return']

    assert result.output == 'recovered'
    assert [(part.outcome, part.content) for part in returns] == [
        ('failed', "Tool 'todo' failed: TodoTargetNotFoundError: todo task not found: 'Absent task'")
    ]
