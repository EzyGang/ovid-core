import pytest
from pydantic import ValidationError

from ovid_core.todo.models import (
    TodoArgs,
    TodoInitPhase,
    TodoItem,
    TodoLimits,
    TodoOperation,
    TodoPhase,
    TodoState,
    TodoStatus,
)


def test_models_normalize_text_and_remain_frozen() -> None:
    item = TodoItem(content='  Keep  authored spacing  ', status=TodoStatus.BLOCKED, blocker=' waiting\n  for input ')
    phase = TodoPhase(name='  Delivery  ', tasks=(item,))
    arguments = TodoArgs(op='block', task='  Keep  authored spacing  ', reason=' waiting\t for input ')

    assert item.content == 'Keep  authored spacing'
    assert item.blocker == 'waiting for input'
    assert phase.name == 'Delivery'
    assert arguments.task == 'Keep  authored spacing'
    assert arguments.reason == 'waiting for input'

    with pytest.raises(ValidationError, match='frozen'):
        item.content = 'changed'
    with pytest.raises(ValidationError, match='Extra inputs'):
        TodoItem.model_validate({'content': 'task', 'unknown': True})


@pytest.mark.parametrize(
    ('model', 'payload'),
    [
        (TodoItem, {'content': ' '}),
        (TodoItem, {'content': 'bad\x00task'}),
        (TodoItem, {'content': 1}),
        (TodoPhase, {'name': '\t', 'tasks': ()}),
        (TodoPhase, {'name': 'bad\x00phase', 'tasks': ()}),
        (TodoArgs, {'op': 'start', 'task': 1}),
        (TodoArgs, {'op': 'append', 'phase': 'phase', 'items': 'task'}),
        (TodoArgs, {'op': 'block', 'task': 'task', 'reason': 1}),
        (TodoArgs, {'op': 'block', 'task': 'task', 'reason': 'bad\x00reason'}),
    ],
)
def test_models_reject_invalid_normalized_values(model: type, payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_blocker_requires_blocked_status_and_blank_reason_becomes_none() -> None:
    assert TodoItem(content='task', status='blocked', blocker='  ').blocker is None
    assert TodoArgs(op='block', task='task', reason=' \n ').reason is None

    with pytest.raises(ValidationError, match='only for blocked'):
        TodoItem(content='task', blocker='waiting')


def test_state_rejects_duplicate_phase_and_global_task_content() -> None:
    with pytest.raises(ValidationError, match='duplicate todo phase'):
        TodoState(phases=(TodoPhase(name='Same'), TodoPhase(name='Same')))

    with pytest.raises(ValidationError, match='duplicate todo task'):
        TodoState(
            phases=(
                TodoPhase(name='One', tasks=(TodoItem(content='Same'),)),
                TodoPhase(name='Two', tasks=(TodoItem(content='Same'),)),
            )
        )

    state = TodoState(
        phases=(
            TodoPhase(name='One', tasks=(TodoItem(content='Task'),)),
            TodoPhase(name='one', tasks=(TodoItem(content='task'),)),
        )
    )
    assert len(state.phases) == 2


def test_limits_require_positive_values() -> None:
    assert TodoLimits().max_tasks == 200

    for field in ('max_phases', 'max_tasks', 'max_tasks_per_phase'):
        with pytest.raises(ValidationError):
            TodoLimits.model_validate({field: 0})


@pytest.mark.parametrize(
    'arguments',
    [
        TodoArgs(op='init', list=()),
        TodoArgs(op='init', list=(TodoInitPhase(phase='Plan', items=['One']),), task='ignored'),
        TodoArgs(op='init', list=(), phase='Ignored', items=['Ignored']),
        TodoArgs(op='init', phase='Plan', items=['One'], reason='ignored'),
        TodoArgs(op='start', task='One', phase='Ignored', reason='ignored'),
        TodoArgs(op='done'),
        TodoArgs(op='done', task='One', phase='Ignored', items=('Ignored',)),
        TodoArgs(op='done', phase='Plan'),
        TodoArgs(op='drop', reason='ignored'),
        TodoArgs(op='block', task='One', phase='Ignored'),
        TodoArgs(op='block', phase='Plan', reason='waiting'),
        TodoArgs(op='unblock', task='One', reason='ignored'),
        TodoArgs(op='unblock', phase='Plan'),
        TodoArgs(op='rm', list=()),
        TodoArgs(op='rm', task='One'),
        TodoArgs(op='append', task='Ignored', phase='Plan', items=('Two',)),
        TodoArgs(op='view', task='Ignored', items=()),
    ],
)
def test_operation_payloads_accept_unrelated_known_fields(arguments: TodoArgs) -> None:
    assert isinstance(arguments.op, TodoOperation)


@pytest.mark.parametrize(
    'payload',
    [
        {'op': 'init'},
        {'op': 'init', 'phase': 'Plan'},
        {'op': 'init', 'items': ['One']},
        {'op': 'start'},
        {'op': 'block'},
        {'op': 'unblock'},
        {'op': 'append', 'phase': 'Plan'},
        {'op': 'append', 'phase': 'Plan', 'items': []},
        {'op': 'append', 'items': ['One']},
    ],
)
def test_operation_payloads_reject_missing_required_fields(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TodoArgs.model_validate(payload)


def test_operation_payloads_still_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError, match='Extra inputs'):
        TodoArgs.model_validate({'op': 'view', 'unknown': True})
