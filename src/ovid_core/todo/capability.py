from dataclasses import dataclass, field

from ovid_core.capabilities.base import BaseCapability, CapabilityContributions
from ovid_core.todo.models import (
    TodoArgs,
    TodoCounts,
    TodoOperation,
    TodoState,
    TodoStatus,
    TodoToolContent,
    TodoToolResult,
)
from ovid_core.todo.service import TodoService
from ovid_core.tools.base import BaseTool, ToolExecutionContext
from ovid_core.tools.models import ToolApproval, ToolEffect


TODO_INSTRUCTIONS = (
    'Use todo for multi-step implementation work, when the user requests a plan, or when the user supplies a '
    'checklist. Preserve every user-specified work item. Keep phase names and task labels concise and unique. Mark '
    'work complete immediately after it finishes. Block tasks only while waiting for external input. Continue '
    'actionable work after updating the list. Use view when persisted state may have changed.'
)

_TODO_DESCRIPTION = (
    'Track phased todo work. Use init with list, or phase plus items; start, done, drop, block, unblock, and rm '
    'target exact task content or a phase where supported; append adds items; view reads without saving. done, drop, '
    'and rm without a target apply to all work. Every mutation keeps at most one active task and promotes the '
    'earliest pending task. Known fields unused by the selected operation are ignored; task wins over phase.'
)


class TodoTool[Deps](BaseTool[Deps, TodoArgs, TodoToolResult]):
    id = 'todo'
    description = _TODO_DESCRIPTION
    args_type = TodoArgs
    result_type = TodoToolResult
    approval = ToolApproval()
    effect = ToolEffect(kind='workspace_write', replay='safe')
    timeout_seconds = 10.0
    defer_loading = False

    def __init__(self, service: TodoService) -> None:
        self._service = service

    async def execute(
        self,
        context: ToolExecutionContext[Deps],
        arguments: TodoArgs,
    ) -> TodoToolResult:
        if arguments.op == TodoOperation.VIEW:
            state = await self._service.view(context.run.conversation_id)
        else:
            state = await self._service.apply(context.run.conversation_id, arguments)

        content = _tool_content(arguments.op, state)
        return TodoToolResult(content=content.model_dump(mode='json'))


@dataclass(frozen=True, slots=True, kw_only=True)
class TodoCapability[Deps](BaseCapability[Deps]):
    id: str = field(default='todo', init=False)
    description: str = field(default='Phased task tracking for multi-step agent work', init=False)
    defer_loading: bool = field(default=False, init=False)
    service: TodoService
    contributions: CapabilityContributions[Deps] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        contributions = CapabilityContributions(
            instructions=(TODO_INSTRUCTIONS,),
            tools=(TodoTool[Deps](self.service),),
        )
        object.__setattr__(self, 'contributions', contributions)


def _tool_content(operation: TodoOperation, state: TodoState) -> TodoToolContent:
    tasks = tuple(task for phase in state.phases for task in phase.tasks)
    active = next(
        (
            (phase.name, task.content)
            for phase in state.phases
            for task in phase.tasks
            if task.status == TodoStatus.IN_PROGRESS
        ),
        None,
    )
    counts = TodoCounts(
        total=len(tasks),
        actionable=sum(task.status in {TodoStatus.PENDING, TodoStatus.IN_PROGRESS} for task in tasks),
        blocked=sum(task.status == TodoStatus.BLOCKED for task in tasks),
        closed=sum(task.status in {TodoStatus.COMPLETED, TodoStatus.ABANDONED} for task in tasks),
    )

    return TodoToolContent(
        operation=operation,
        state=state,
        counts=counts,
        active_phase=None if active is None else active[0],
        active_task=None if active is None else active[1],
    )
