from ovid_core.todo._reducer_state import (
    find_phase,
    find_task,
    map_tasks,
    normalize_active,
    replace_task,
    required,
    required_items,
    validate_todos,
    with_status,
)
from ovid_core.todo.errors import TodoOperationError
from ovid_core.todo.models import TodoArgs, TodoInitPhase, TodoItem, TodoLimits, TodoOperation, TodoPhase, TodoStatus


def reduce_todos(
    phases: tuple[TodoPhase, ...],
    arguments: TodoArgs,
    *,
    limits: TodoLimits,
) -> tuple[TodoPhase, ...]:
    validate_todos(phases, limits)

    match arguments.op:
        case TodoOperation.INIT:
            updated = _initialize(arguments)
        case TodoOperation.START:
            updated = _start(phases, required(arguments.task))
        case TodoOperation.DONE:
            updated = _close(phases, arguments, TodoStatus.COMPLETED)
        case TodoOperation.DROP:
            updated = _close(phases, arguments, TodoStatus.ABANDONED)
        case TodoOperation.BLOCK:
            updated = _block(phases, arguments)
        case TodoOperation.UNBLOCK:
            updated = _unblock(phases, arguments)
        case TodoOperation.REMOVE:
            updated = _remove(phases, arguments)
        case TodoOperation.APPEND:
            updated = _append(phases, required(arguments.phase), required_items(arguments.items))
        case TodoOperation.VIEW:
            raise TodoOperationError('view does not mutate todo state')
        case _:
            raise TodoOperationError('unsupported todo operation')

    normalized = normalize_active(updated)
    validate_todos(normalized, limits)
    return normalized


def _initialize(arguments: TodoArgs) -> tuple[TodoPhase, ...]:
    definitions = arguments.list
    if definitions is None:
        definitions = (TodoInitPhase(phase=required(arguments.phase), items=required_items(arguments.items)),)

    return tuple(
        TodoPhase(name=definition.phase, tasks=tuple(TodoItem(content=item) for item in definition.items))
        for definition in definitions
    )


def _start(phases: tuple[TodoPhase, ...], content: str) -> tuple[TodoPhase, ...]:
    phase_index, task_index = find_task(phases, content)
    target = phases[phase_index].tasks[task_index]
    if target.status == TodoStatus.IN_PROGRESS:
        return phases
    if target.status == TodoStatus.BLOCKED:
        raise TodoOperationError(f'blocked todo task cannot be started: {content!r}')

    def transition(current_phase: int, current_task: int, task: TodoItem) -> TodoItem:
        if (current_phase, current_task) == (phase_index, task_index):
            return with_status(task, TodoStatus.IN_PROGRESS)
        if task.status == TodoStatus.IN_PROGRESS:
            return with_status(task, TodoStatus.PENDING)
        return task

    return map_tasks(phases, transition)


def _close(
    phases: tuple[TodoPhase, ...],
    arguments: TodoArgs,
    status: TodoStatus,
) -> tuple[TodoPhase, ...]:
    if arguments.task is not None:
        phase_index, task_index = find_task(phases, arguments.task)
        target = with_status(phases[phase_index].tasks[task_index], status)
        return replace_task(phases, phase_index, task_index, target)

    selected_phase = find_phase(phases, arguments.phase) if arguments.phase is not None else None

    def transition(current_phase: int, _current_task: int, task: TodoItem) -> TodoItem:
        if selected_phase is not None and current_phase != selected_phase:
            return task
        return with_status(task, status)

    return map_tasks(phases, transition)


def _block(phases: tuple[TodoPhase, ...], arguments: TodoArgs) -> tuple[TodoPhase, ...]:
    if arguments.task is not None:
        phase_index, task_index = find_task(phases, arguments.task)
        target = phases[phase_index].tasks[task_index]
        if target.status in {TodoStatus.COMPLETED, TodoStatus.ABANDONED}:
            return phases

        blocked = with_status(target, TodoStatus.BLOCKED, blocker=arguments.reason)
        return replace_task(phases, phase_index, task_index, blocked)

    phase_index = find_phase(phases, required(arguments.phase))

    def transition(current_phase: int, _current_task: int, task: TodoItem) -> TodoItem:
        if current_phase != phase_index or task.status in {TodoStatus.COMPLETED, TodoStatus.ABANDONED}:
            return task
        return with_status(task, TodoStatus.BLOCKED, blocker=arguments.reason)

    return map_tasks(phases, transition)


def _unblock(phases: tuple[TodoPhase, ...], arguments: TodoArgs) -> tuple[TodoPhase, ...]:
    if arguments.task is not None:
        phase_index, task_index = find_task(phases, arguments.task)
        target = phases[phase_index].tasks[task_index]
        if target.status != TodoStatus.BLOCKED:
            return phases

        return replace_task(phases, phase_index, task_index, with_status(target, TodoStatus.PENDING))

    phase_index = find_phase(phases, required(arguments.phase))

    def transition(current_phase: int, _current_task: int, task: TodoItem) -> TodoItem:
        if current_phase == phase_index and task.status == TodoStatus.BLOCKED:
            return with_status(task, TodoStatus.PENDING)
        return task

    return map_tasks(phases, transition)


def _remove(phases: tuple[TodoPhase, ...], arguments: TodoArgs) -> tuple[TodoPhase, ...]:
    if arguments.task is not None:
        phase_index, task_index = find_task(phases, arguments.task)
        phase = phases[phase_index]
        replacement = TodoPhase(name=phase.name, tasks=phase.tasks[:task_index] + phase.tasks[task_index + 1 :])
        return phases[:phase_index] + (replacement,) + phases[phase_index + 1 :]
    if arguments.phase is not None:
        phase_index = find_phase(phases, arguments.phase)
        return phases[:phase_index] + phases[phase_index + 1 :]
    return ()


def _append(phases: tuple[TodoPhase, ...], phase_name: str, items: tuple[str, ...]) -> tuple[TodoPhase, ...]:
    new_tasks = tuple(TodoItem(content=item) for item in items)
    for phase_index, phase in enumerate(phases):
        if phase.name == phase_name:
            replacement = TodoPhase(name=phase.name, tasks=phase.tasks + new_tasks)
            return phases[:phase_index] + (replacement,) + phases[phase_index + 1 :]

    return phases + (TodoPhase(name=phase_name, tasks=new_tasks),)
