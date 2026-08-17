from enum import StrEnum
from typing import Self, cast
from unicodedata import category

from pydantic import Field, JsonValue, field_validator, model_validator

from ovid_core.models import BaseModel
from ovid_core.tools.models import ToolResult


type _RawValue = JsonValue | tuple[JsonValue, ...]


class TodoStatus(StrEnum):
    PENDING = 'pending'
    IN_PROGRESS = 'in_progress'
    COMPLETED = 'completed'
    ABANDONED = 'abandoned'
    BLOCKED = 'blocked'


class TodoItem(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    status: TodoStatus = TodoStatus.PENDING
    blocker: str | None = Field(default=None, max_length=1_000)

    @field_validator('content', mode='before')
    @classmethod
    def normalize_content(cls, value: _RawValue) -> _RawValue:
        return _normalize_label(value)

    @field_validator('blocker', mode='before')
    @classmethod
    def normalize_blocker(cls, value: _RawValue) -> _RawValue:
        return _normalize_reason(value)

    @model_validator(mode='after')
    def validate_blocker_status(self) -> Self:
        if self.blocker is not None and self.status != TodoStatus.BLOCKED:
            raise ValueError('blocker is permitted only for blocked tasks')

        return self


class TodoPhase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tasks: tuple[TodoItem, ...] = ()

    @field_validator('name', mode='before')
    @classmethod
    def normalize_name(cls, value: _RawValue) -> _RawValue:
        return _normalize_label(value)


class TodoState(BaseModel):
    phases: tuple[TodoPhase, ...] = ()
    revision: str | None = None

    @model_validator(mode='after')
    def reject_duplicates(self) -> Self:
        phase_names: set[str] = set()
        task_contents: set[str] = set()

        for phase in self.phases:
            if phase.name in phase_names:
                raise ValueError(f'duplicate todo phase: {phase.name!r}')
            phase_names.add(phase.name)

            for task in phase.tasks:
                if task.content in task_contents:
                    raise ValueError(f'duplicate todo task: {task.content!r}')
                task_contents.add(task.content)

        return self


class TodoLimits(BaseModel):
    max_phases: int = Field(default=25, ge=1)
    max_tasks: int = Field(default=200, ge=1)
    max_tasks_per_phase: int = Field(default=50, ge=1)


class TodoOperation(StrEnum):
    INIT = 'init'
    START = 'start'
    DONE = 'done'
    DROP = 'drop'
    BLOCK = 'block'
    UNBLOCK = 'unblock'
    REMOVE = 'rm'
    APPEND = 'append'
    VIEW = 'view'


class TodoInitPhase(BaseModel):
    phase: str = Field(min_length=1, max_length=200)
    items: tuple[str, ...] = Field(min_length=1)

    @field_validator('phase', mode='before')
    @classmethod
    def normalize_phase(cls, value: _RawValue) -> _RawValue:
        return _normalize_label(value)

    @field_validator('items', mode='before')
    @classmethod
    def normalize_items(cls, value: _RawValue) -> _RawValue:
        return _normalize_items(value)


class TodoArgs(BaseModel):
    op: TodoOperation
    list: tuple[TodoInitPhase, ...] | None = None
    task: str | None = Field(default=None, min_length=1, max_length=500)
    phase: str | None = Field(default=None, min_length=1, max_length=200)
    items: tuple[str, ...] | None = None
    reason: str | None = Field(default=None, max_length=1_000)

    @field_validator('task', 'phase', mode='before')
    @classmethod
    def normalize_selector(cls, value: _RawValue) -> _RawValue:
        return None if value is None else _normalize_label(value)

    @field_validator('items', mode='before')
    @classmethod
    def normalize_item_list(cls, value: _RawValue) -> _RawValue:
        return None if value is None else _normalize_items(value)

    @field_validator('reason', mode='before')
    @classmethod
    def normalize_reason(cls, value: _RawValue) -> _RawValue:
        return _normalize_reason(value)

    @model_validator(mode='after')
    def validate_operation_payload(self) -> Self:
        _validate_operation_payload(self)
        return self


class TodoCounts(BaseModel):
    total: int = Field(ge=0)
    actionable: int = Field(ge=0)
    blocked: int = Field(ge=0)
    closed: int = Field(ge=0)


class TodoToolContent(BaseModel):
    operation: TodoOperation
    state: TodoState
    counts: TodoCounts
    active_phase: str | None = None
    active_task: str | None = None


class TodoToolResult(ToolResult):
    pass


def _normalize_label(value: _RawValue) -> _RawValue:
    if not isinstance(value, str):
        return value

    normalized = value.strip()
    _reject_control_characters(normalized)
    return normalized


def _normalize_reason(value: _RawValue) -> _RawValue:
    if value is None or not isinstance(value, str):
        return value

    normalized = ' '.join(value.split())
    if not normalized:
        return None

    _reject_control_characters(normalized)
    return normalized


def _normalize_items(value: _RawValue) -> _RawValue:
    if not isinstance(value, (list, tuple)):
        return value

    return tuple(cast(JsonValue, _normalize_label(item)) for item in value)


def _reject_control_characters(value: str) -> None:
    if any(category(character) == 'Cc' for character in value):
        raise ValueError('todo text cannot contain control characters')


def _validate_operation_payload(arguments: TodoArgs) -> None:
    match arguments.op:
        case TodoOperation.INIT:
            if arguments.list is None and (arguments.phase is None or not arguments.items):
                raise ValueError('init requires list, or phase with non-empty items')
        case TodoOperation.START:
            if arguments.task is None:
                raise ValueError('start requires a task target')
        case TodoOperation.BLOCK | TodoOperation.UNBLOCK:
            if arguments.task is None and arguments.phase is None:
                raise ValueError(f'{arguments.op.value} requires a task or phase target')
        case TodoOperation.APPEND:
            if arguments.phase is None or not arguments.items:
                raise ValueError('append requires phase with non-empty items')
        case TodoOperation.DONE | TodoOperation.DROP | TodoOperation.REMOVE | TodoOperation.VIEW:
            pass
        case _:
            raise ValueError('unsupported todo operation')
