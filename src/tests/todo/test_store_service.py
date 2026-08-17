import asyncio
from typing import cast

import pytest
from pytest_mock import MockerFixture

from ovid_core.runtime.identifiers import ConversationId
from ovid_core.todo.errors import TodoConflictError, TodoLimitError, TodoStoreError
from ovid_core.todo.models import TodoArgs, TodoItem, TodoLimits, TodoPhase, TodoState, TodoStatus
from ovid_core.todo.service import TodoService
from ovid_core.todo.store import InMemoryTodoStore, TodoStore


async def test_in_memory_store_revisions_clear_and_conversation_isolation() -> None:
    store = InMemoryTodoStore()
    first_conversation = ConversationId.new()
    second_conversation = ConversationId.new()

    assert await store.load(first_conversation) is None

    initial = await store.save(
        first_conversation,
        (TodoPhase(name='Plan', tasks=(TodoItem(content='Task'),)),),
        expected_revision=None,
    )
    replacement = await store.save(first_conversation, (), expected_revision=initial.revision)

    assert initial.revision is not None
    assert replacement.revision is not None
    assert replacement.revision != initial.revision
    assert replacement.phases == ()
    assert await store.load(first_conversation) == replacement
    assert await store.load(second_conversation) is None

    second = await store.save(second_conversation, (), expected_revision=None)
    assert second.revision not in {initial.revision, replacement.revision}

    with pytest.raises(TodoConflictError, match='changed'):
        await store.save(first_conversation, (), expected_revision=None)
    with pytest.raises(TodoConflictError, match='changed'):
        await store.save(first_conversation, (), expected_revision=initial.revision)


async def test_in_memory_store_allows_only_one_competing_save() -> None:
    store = InMemoryTodoStore()
    conversation_id = ConversationId.new()
    initial = await store.save(conversation_id, (), expected_revision=None)

    results = await asyncio.gather(
        store.save(
            conversation_id,
            (TodoPhase(name='One'),),
            expected_revision=initial.revision,
        ),
        store.save(
            conversation_id,
            (TodoPhase(name='Two'),),
            expected_revision=initial.revision,
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(result, TodoState) for result in results) == 1
    assert sum(isinstance(result, TodoConflictError) for result in results) == 1


def store_double(mocker: MockerFixture) -> TodoStore:
    return cast(TodoStore, mocker.Mock(spec=TodoStore))


async def test_service_view_returns_empty_without_save_and_validates_loaded_limits(mocker: MockerFixture) -> None:
    store = store_double(mocker)
    store.load.return_value = None
    service = TodoService(store=store, limits=TodoLimits(max_tasks=1))
    conversation_id = ConversationId.new()

    empty = await service.view(conversation_id)

    assert empty == TodoState()
    store.load.assert_awaited_once_with(conversation_id)
    store.save.assert_not_called()

    store.load.reset_mock()
    store.load.return_value = TodoState(
        phases=(TodoPhase(name='Plan', tasks=(TodoItem(content='One'), TodoItem(content='Two'))),),
        revision='revision',
    )
    with pytest.raises(TodoLimitError, match='task limit'):
        await service.view(conversation_id)


async def test_service_applies_with_loaded_revision_and_returns_committed_state(mocker: MockerFixture) -> None:
    store = store_double(mocker)
    conversation_id = ConversationId.new()
    committed = TodoState(
        phases=(TodoPhase(name='Plan', tasks=(TodoItem(content='One', status=TodoStatus.IN_PROGRESS),)),),
        revision='new',
    )
    store.load.return_value = None
    store.save.return_value = committed
    service = TodoService(store=store)

    result = await service.apply(
        conversation_id,
        TodoArgs(op='init', phase='Plan', items=('One',)),
    )

    assert result is committed
    store.save.assert_awaited_once_with(
        conversation_id,
        committed.phases,
        expected_revision=None,
    )


async def test_service_checks_explicit_revision_and_never_retries_conflicts(mocker: MockerFixture) -> None:
    store = store_double(mocker)
    conversation_id = ConversationId.new()
    loaded = TodoState(phases=(TodoPhase(name='Plan'),), revision='current')
    store.load.return_value = loaded
    service = TodoService(store=store)

    with pytest.raises(TodoConflictError, match='operation was applied'):
        await service.apply(conversation_id, TodoArgs(op='rm'), expected_revision='stale')
    store.save.assert_not_called()

    store.save.side_effect = TodoConflictError('backend conflict')
    with pytest.raises(TodoConflictError, match='backend conflict'):
        await service.apply(conversation_id, TodoArgs(op='rm'), expected_revision='current')
    store.save.assert_awaited_once()
    assert store.load.await_count == 2


async def test_service_view_operation_never_saves_and_store_errors_propagate(mocker: MockerFixture) -> None:
    store = store_double(mocker)
    conversation_id = ConversationId.new()
    state = TodoState(revision='current')
    store.load.return_value = state
    service = TodoService(store=store)

    assert await service.apply(conversation_id, TodoArgs(op='view')) == state
    store.save.assert_not_called()

    store.load.side_effect = TodoStoreError('remote store unavailable')
    with pytest.raises(TodoStoreError, match='remote store unavailable'):
        await service.view(conversation_id)
