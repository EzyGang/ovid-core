from collections.abc import Callable

from ovid_core.todo.errors import TodoDuplicateError, TodoLimitError, TodoOperationError, TodoTargetNotFoundError
from ovid_core.todo.models import TodoItem, TodoLimits, TodoPhase, TodoStatus


def validate_todos(phases: tuple[TodoPhase, ...], limits: TodoLimits) -> None:
    if len(phases) > limits.max_phases:
        raise TodoLimitError(f'todo list exceeds the phase limit of {limits.max_phases}')

    phase_names: set[str] = set()
    task_contents: set[str] = set()
    task_count = 0

    for phase in phases:
        if phase.name in phase_names:
            raise TodoDuplicateError(f'duplicate todo phase: {phase.name!r}')
        phase_names.add(phase.name)

        if len(phase.tasks) > limits.max_tasks_per_phase:
            raise TodoLimitError(f'todo phase {phase.name!r} exceeds the task limit of {limits.max_tasks_per_phase}')

        for task in phase.tasks:
            if task.content in task_contents:
                raise TodoDuplicateError(f'duplicate todo task: {task.content!r}')
            task_contents.add(task.content)
            task_count += 1

    if task_count > limits.max_tasks:
        raise TodoLimitError(f'todo list exceeds the task limit of {limits.max_tasks}')


def normalize_active(phases: tuple[TodoPhase, ...]) -> tuple[TodoPhase, ...]:
    active_seen = False

    def retain_first(_phase_index: int, _task_index: int, task: TodoItem) -> TodoItem:
        nonlocal active_seen
        if task.status != TodoStatus.IN_PROGRESS:
            return task
        if not active_seen:
            active_seen = True
            return task
        return with_status(task, TodoStatus.PENDING)

    normalized = map_tasks(phases, retain_first)
    if active_seen:
        return normalized

    for phase_index, phase in enumerate(normalized):
        for task_index, task in enumerate(phase.tasks):
            if task.status == TodoStatus.PENDING:
                return replace_task(normalized, phase_index, task_index, with_status(task, TodoStatus.IN_PROGRESS))

    return normalized


def map_tasks(
    phases: tuple[TodoPhase, ...],
    transition: Callable[[int, int, TodoItem], TodoItem],
) -> tuple[TodoPhase, ...]:
    return tuple(
        TodoPhase(
            name=phase.name,
            tasks=tuple(transition(phase_index, task_index, task) for task_index, task in enumerate(phase.tasks)),
        )
        for phase_index, phase in enumerate(phases)
    )


def replace_task(
    phases: tuple[TodoPhase, ...],
    phase_index: int,
    task_index: int,
    task: TodoItem,
) -> tuple[TodoPhase, ...]:
    phase = phases[phase_index]
    tasks = phase.tasks[:task_index] + (task,) + phase.tasks[task_index + 1 :]
    replacement = TodoPhase(name=phase.name, tasks=tasks)
    return phases[:phase_index] + (replacement,) + phases[phase_index + 1 :]


def with_status(task: TodoItem, status: TodoStatus, *, blocker: str | None = None) -> TodoItem:
    return TodoItem(content=task.content, status=status, blocker=blocker)


def find_task(phases: tuple[TodoPhase, ...], content: str) -> tuple[int, int]:
    for phase_index, phase in enumerate(phases):
        for task_index, task in enumerate(phase.tasks):
            if task.content == content:
                return phase_index, task_index

    raise TodoTargetNotFoundError(f'todo task not found: {content!r}')


def find_phase(phases: tuple[TodoPhase, ...], name: str) -> int:
    for phase_index, phase in enumerate(phases):
        if phase.name == name:
            return phase_index

    raise TodoTargetNotFoundError(f'todo phase not found: {name!r}')


def required(value: str | None) -> str:
    if value is None:
        raise TodoOperationError('todo operation is missing its required target')
    return value


def required_items(value: tuple[str, ...] | None) -> tuple[str, ...]:
    if not value:
        raise TodoOperationError('todo operation requires non-empty items')
    return value
