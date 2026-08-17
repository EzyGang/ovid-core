import pytest

from ovid_core.todo._reducer_state import required, required_items
from ovid_core.todo.errors import TodoDuplicateError, TodoLimitError, TodoOperationError, TodoTargetNotFoundError
from ovid_core.todo.models import TodoArgs, TodoItem, TodoLimits, TodoPhase, TodoStatus
from ovid_core.todo.reducer import reduce_todos


LIMITS = TodoLimits()


def phase(name: str, *tasks: tuple[str, TodoStatus]) -> TodoPhase:
    return TodoPhase(name=name, tasks=tuple(TodoItem(content=content, status=status) for content, status in tasks))


def statuses(phases: tuple[TodoPhase, ...]) -> list[tuple[str, TodoStatus, str | None]]:
    return [(task.content, task.status, task.blocker) for current in phases for task in current.tasks]


def test_init_replaces_state_promotes_first_and_accepts_empty_list() -> None:
    existing = (phase('Old', ('Old task', TodoStatus.IN_PROGRESS)),)

    initialized = reduce_todos(
        existing,
        TodoArgs(
            op='init',
            list=(
                {'phase': 'One', 'items': ('First', 'Second')},
                {'phase': 'Two', 'items': ('Third',)},
            ),
        ),
        limits=LIMITS,
    )
    cleared = reduce_todos(existing, TodoArgs(op='init', list=()), limits=LIMITS)
    shorthand = reduce_todos((), TodoArgs(op='init', phase='Plan', items=('Task',)), limits=LIMITS)

    assert [current.name for current in initialized] == ['One', 'Two']
    assert statuses(initialized) == [
        ('First', TodoStatus.IN_PROGRESS, None),
        ('Second', TodoStatus.PENDING, None),
        ('Third', TodoStatus.PENDING, None),
    ]
    assert cleared == ()
    assert statuses(shorthand) == [('Task', TodoStatus.IN_PROGRESS, None)]
    assert statuses(existing) == [('Old task', TodoStatus.IN_PROGRESS, None)]


def test_start_switches_active_task_and_is_idempotent() -> None:
    original = (
        phase('One', ('First', TodoStatus.IN_PROGRESS), ('Second', TodoStatus.PENDING)),
        phase('Two', ('Third', TodoStatus.PENDING)),
    )

    started = reduce_todos(original, TodoArgs(op='start', task='Third'), limits=LIMITS)
    repeated = reduce_todos(started, TodoArgs(op='start', task='Third'), limits=LIMITS)

    assert statuses(started) == [
        ('First', TodoStatus.PENDING, None),
        ('Second', TodoStatus.PENDING, None),
        ('Third', TodoStatus.IN_PROGRESS, None),
    ]
    assert repeated == started

    for status in (TodoStatus.COMPLETED, TodoStatus.ABANDONED):
        reopened = reduce_todos((phase('One', ('Task', status)),), TodoArgs(op='start', task='Task'), limits=LIMITS)
        assert statuses(reopened) == [('Task', TodoStatus.IN_PROGRESS, None)]

    with pytest.raises(TodoOperationError, match='blocked todo task'):
        reduce_todos(
            (phase('One', ('Task', TodoStatus.BLOCKED)),),
            TodoArgs(op='start', task='Task'),
            limits=LIMITS,
        )


def test_done_and_drop_handle_task_phase_and_global_scopes() -> None:
    original = (
        phase(
            'One',
            ('Active', TodoStatus.IN_PROGRESS),
            ('Blocked', TodoStatus.BLOCKED),
            ('Abandoned', TodoStatus.ABANDONED),
        ),
        phase('Two', ('Completed', TodoStatus.COMPLETED), ('Pending', TodoStatus.PENDING)),
    )

    completed_task = reduce_todos(
        original,
        TodoArgs(op='done', task='Abandoned', phase='Two', reason='ignored'),
        limits=LIMITS,
    )
    completed_phase = reduce_todos(original, TodoArgs(op='done', phase='One'), limits=LIMITS)
    completed_all = reduce_todos(original, TodoArgs(op='done'), limits=LIMITS)
    dropped_task = reduce_todos(original, TodoArgs(op='drop', task='Completed'), limits=LIMITS)
    dropped_phase = reduce_todos(original, TodoArgs(op='drop', phase='Two'), limits=LIMITS)
    dropped_all = reduce_todos(original, TodoArgs(op='drop'), limits=LIMITS)

    assert statuses(completed_task)[2][1] == TodoStatus.COMPLETED
    assert statuses(completed_task)[4][1] == TodoStatus.PENDING
    assert statuses(completed_phase)[:3] == [
        ('Active', TodoStatus.COMPLETED, None),
        ('Blocked', TodoStatus.COMPLETED, None),
        ('Abandoned', TodoStatus.COMPLETED, None),
    ]
    assert all(status == TodoStatus.COMPLETED for _, status, _ in statuses(completed_all))
    assert statuses(dropped_task)[3][1] == TodoStatus.ABANDONED
    assert [status for _, status, _ in statuses(dropped_phase)[3:]] == [
        TodoStatus.ABANDONED,
        TodoStatus.ABANDONED,
    ]
    assert all(status == TodoStatus.ABANDONED for _, status, _ in statuses(dropped_all))


def test_block_and_unblock_preserve_closed_work_and_promote_pending() -> None:
    original = (
        phase(
            'One',
            ('Active', TodoStatus.IN_PROGRESS),
            ('Pending', TodoStatus.PENDING),
            ('Completed', TodoStatus.COMPLETED),
        ),
    )

    blocked = reduce_todos(original, TodoArgs(op='block', task='Active', reason='waiting'), limits=LIMITS)
    blocked_phase = reduce_todos(original, TodoArgs(op='block', phase='One', reason='all waiting'), limits=LIMITS)
    unblocked = reduce_todos(blocked_phase, TodoArgs(op='unblock', task='Pending'), limits=LIMITS)
    unblocked_phase = reduce_todos(blocked_phase, TodoArgs(op='unblock', phase='One'), limits=LIMITS)

    assert statuses(blocked) == [
        ('Active', TodoStatus.BLOCKED, 'waiting'),
        ('Pending', TodoStatus.IN_PROGRESS, None),
        ('Completed', TodoStatus.COMPLETED, None),
    ]
    assert statuses(blocked_phase) == [
        ('Active', TodoStatus.BLOCKED, 'all waiting'),
        ('Pending', TodoStatus.BLOCKED, 'all waiting'),
        ('Completed', TodoStatus.COMPLETED, None),
    ]
    assert statuses(unblocked)[1] == ('Pending', TodoStatus.IN_PROGRESS, None)
    assert statuses(unblocked_phase)[:2] == [
        ('Active', TodoStatus.IN_PROGRESS, None),
        ('Pending', TodoStatus.PENDING, None),
    ]

    assert reduce_todos(original, TodoArgs(op='block', task='Completed'), limits=LIMITS) == original
    assert reduce_todos(original, TodoArgs(op='unblock', task='Pending'), limits=LIMITS) == original


def test_remove_and_append_preserve_order_and_promote() -> None:
    original = (
        phase('One', ('Active', TodoStatus.IN_PROGRESS), ('Next', TodoStatus.PENDING)),
        phase('Two', ('Last', TodoStatus.PENDING)),
    )

    removed_task = reduce_todos(original, TodoArgs(op='rm', task='Active'), limits=LIMITS)
    removed_phase = reduce_todos(original, TodoArgs(op='rm', phase='One'), limits=LIMITS)
    removed_all = reduce_todos(original, TodoArgs(op='rm'), limits=LIMITS)
    appended = reduce_todos(original, TodoArgs(op='append', phase='One', items=('Added',)), limits=LIMITS)
    new_phase = reduce_todos(original, TodoArgs(op='append', phase='Three', items=('Added',)), limits=LIMITS)

    assert statuses(removed_task)[0] == ('Next', TodoStatus.IN_PROGRESS, None)
    assert statuses(removed_phase)[0] == ('Last', TodoStatus.IN_PROGRESS, None)
    assert removed_all == ()
    assert [task.content for task in appended[0].tasks] == ['Active', 'Next', 'Added']
    assert [current.name for current in new_phase] == ['One', 'Two', 'Three']


def test_normalization_retains_first_active_and_ignores_blocked_only_lists() -> None:
    multiple = (
        phase('One', ('First', TodoStatus.IN_PROGRESS)),
        phase('Two', ('Second', TodoStatus.IN_PROGRESS), ('Third', TodoStatus.PENDING)),
    )
    normalized = reduce_todos(multiple, TodoArgs(op='append', phase='Two', items=('Fourth',)), limits=LIMITS)
    blocked = reduce_todos(
        (phase('One', ('Blocked', TodoStatus.BLOCKED), ('Done', TodoStatus.COMPLETED)),),
        TodoArgs(op='append', phase='One', items=('Pending',)),
        limits=LIMITS,
    )

    assert [status for _, status, _ in statuses(normalized)] == [
        TodoStatus.IN_PROGRESS,
        TodoStatus.PENDING,
        TodoStatus.PENDING,
        TodoStatus.PENDING,
    ]
    assert statuses(blocked)[2][1] == TodoStatus.IN_PROGRESS

    closed = reduce_todos(
        (phase('One', ('Blocked', TodoStatus.BLOCKED), ('Done', TodoStatus.COMPLETED)),),
        TodoArgs(op='done', task='Done'),
        limits=LIMITS,
    )
    assert all(status != TodoStatus.IN_PROGRESS for _, status, _ in statuses(closed))


def test_reducer_rejects_missing_targets_duplicates_limits_and_view() -> None:
    original = (phase('One', ('Task', TodoStatus.IN_PROGRESS)),)

    for arguments in (
        TodoArgs(op='start', task='Missing'),
        TodoArgs(op='done', phase='Missing'),
        TodoArgs(op='block', phase='Missing'),
        TodoArgs(op='unblock', phase='Missing'),
        TodoArgs(op='rm', task='Missing'),
    ):
        with pytest.raises(TodoTargetNotFoundError):
            reduce_todos(original, arguments, limits=LIMITS)

    with pytest.raises(TodoDuplicateError, match='phase'):
        reduce_todos((TodoPhase(name='Same'), TodoPhase(name='Same')), TodoArgs(op='rm'), limits=LIMITS)
    with pytest.raises(TodoDuplicateError, match='task'):
        reduce_todos(
            (TodoPhase(name='One', tasks=(TodoItem(content='Same'), TodoItem(content='Same'))),),
            TodoArgs(op='rm'),
            limits=LIMITS,
        )
    with pytest.raises(TodoDuplicateError, match='task'):
        reduce_todos(original, TodoArgs(op='append', phase='One', items=('Task',)), limits=LIMITS)
    with pytest.raises(TodoOperationError, match='view'):
        reduce_todos(original, TodoArgs(op='view'), limits=LIMITS)

    with pytest.raises(TodoLimitError, match='phase limit'):
        reduce_todos((TodoPhase(name='One'), TodoPhase(name='Two')), TodoArgs(op='rm'), limits=TodoLimits(max_phases=1))
    with pytest.raises(TodoLimitError, match='phase .* task limit'):
        reduce_todos(
            original, TodoArgs(op='append', phase='One', items=('Two',)), limits=TodoLimits(max_tasks_per_phase=1)
        )
    with pytest.raises(TodoLimitError, match='list exceeds the task limit'):
        reduce_todos(original, TodoArgs(op='append', phase='Two', items=('Two',)), limits=TodoLimits(max_tasks=1))


def test_internal_required_value_guards() -> None:
    assert required('value') == 'value'
    assert required_items(('value',)) == ('value',)

    with pytest.raises(TodoOperationError, match='required target'):
        required(None)
    with pytest.raises(TodoOperationError, match='non-empty items'):
        required_items(None)


def test_corrupted_operation_values_fail_closed() -> None:
    arguments = TodoArgs.model_construct(op='unsupported')

    with pytest.raises(ValueError, match='unsupported todo operation'):
        arguments.validate_operation_payload()
    with pytest.raises(TodoOperationError, match='unsupported todo operation'):
        reduce_todos((), arguments, limits=LIMITS)
