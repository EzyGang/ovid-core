from collections.abc import Mapping
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from ovid_core.errors import DeferredToolAnswerError
from ovid_core.models import BaseModel
from ovid_core.tools.models import ToolApproval


type DeferredToolKind = Literal['approval', 'external', 'question']


class DeferredToolRequest(BaseModel):
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]
    kind: DeferredToolKind
    approval: ToolApproval | None = None
    metadata: dict[str, JsonValue] | None = None


class ToolCallApproved(BaseModel):
    kind: Literal['approved'] = 'approved'


class ToolCallDenied(BaseModel):
    kind: Literal['denied'] = 'denied'
    message: str = Field(default='The tool call was denied.', min_length=1)


class ToolCallReturned(BaseModel):
    kind: Literal['returned'] = 'returned'
    value: JsonValue


class ToolCallFailed(BaseModel):
    kind: Literal['failed'] = 'failed'
    message: str = Field(min_length=1)


DeferredToolAnswer = Annotated[
    ToolCallApproved | ToolCallDenied | ToolCallReturned | ToolCallFailed,
    Field(discriminator='kind'),
]


class DeferredToolRequests(BaseModel):
    calls: tuple[DeferredToolRequest, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def validate_unique_calls(self) -> Self:
        call_ids = [call.tool_call_id for call in self.calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError('deferred tool call IDs must be unique')

        return self

    def resume(self, answers: Mapping[str, DeferredToolAnswer]) -> DeferredToolResume:
        return DeferredToolResume(requests=self, answers=dict(answers))

    def check_answers(self, answers: Mapping[str, DeferredToolAnswer]) -> None:
        """Raise `DeferredToolAnswerError` when an answer names a call that is not pending or of another kind."""
        kinds = {call.tool_call_id: call.kind for call in self.calls}
        if unknown := sorted(answers.keys() - kinds.keys()):
            raise DeferredToolAnswerError(f'Answers name tool calls that are not pending: {unknown}')

        mismatched = sorted(
            call_id
            for call_id, answer in answers.items()
            if (kinds[call_id] == 'approval') != isinstance(answer, ToolCallApproved | ToolCallDenied)
        )
        if mismatched:
            raise DeferredToolAnswerError(f'Answers do not match the kind of pending tool calls: {mismatched}')


class DeferredToolResume(BaseModel):
    """Answers for every pending call of one deferred run, checked against the calls when the model is built."""

    requests: DeferredToolRequests
    answers: dict[str, DeferredToolAnswer]

    @model_validator(mode='after')
    def validate_answers(self) -> Self:
        pending = {call.tool_call_id for call in self.requests.calls}
        if missing := sorted(pending - self.answers.keys()):
            raise DeferredToolAnswerError(f'Answers are missing for pending tool calls: {missing}')
        self.requests.check_answers(self.answers)

        return self


class InterruptedRunResume(BaseModel):
    """Continue the interrupted run of the given history without a new prompt."""

    kind: Literal['interrupted'] = 'interrupted'
