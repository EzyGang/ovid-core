from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from ovid_core.messages.models import AgentMessage, TextPart
from ovid_core.models import BaseModel
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.tools.deferred import DeferredToolRequests
from ovid_core.usage.models import RequestUsage, Usage


class ResultMetadataEntry(BaseModel):
    key: str = Field(min_length=1)
    value: JsonValue

    @model_validator(mode='after')
    def reject_secret_keys(self) -> Self:
        normalized_key = self.key.casefold().replace('-', '_')

        if any(secret_key in normalized_key for secret_key in _SECRET_METADATA_KEYS):
            raise ValueError('result metadata keys cannot identify secret values')

        return self


type RunOutcome = Literal['failed', 'cancelled', 'usage_limit', 'context_overflow', 'timeout']


class _RunTranscript(BaseModel):
    messages: tuple[AgentMessage, ...]
    history: tuple[AgentMessage, ...] = ()
    usage: Usage
    run_id: RunId
    conversation_id: ConversationId

    @model_validator(mode='after')
    def validate_identity(self) -> Self:
        for message in self.messages:
            if message.run_id is not None and message.run_id != self.run_id:
                raise ValueError('message run_id must match result run_id')
            if message.conversation_id is not None and message.conversation_id != self.conversation_id:
                raise ValueError('message conversation_id must match result conversation_id')

        if self.history and self.messages and self.history[-len(self.messages) :] != self.messages:
            raise ValueError('result messages must end the active history')

        return self


class _RunRecord(_RunTranscript):
    @model_validator(mode='after')
    def validate_usage(self) -> Self:
        request_usage = (
            message.request_usage
            for message in self.messages
            if message.role == 'response' and message.request_usage is not None
        )
        expected_usage = Usage.from_requests(request_usage, tool_calls=self.usage.tool_calls)

        if expected_usage != self.usage:
            raise ValueError('result usage must equal the aggregate normalized request usage')

        return self


class RunResult[Output](_RunRecord):
    output: Output
    metadata: tuple[ResultMetadataEntry, ...] = ()

    @model_validator(mode='after')
    def validate_unique_metadata(self) -> Self:
        metadata_keys = tuple(item.key for item in self.metadata)

        if len(metadata_keys) != len(set(metadata_keys)):
            raise ValueError('result metadata keys must be unique')

        return self


class DeferredRunResult(_RunRecord):
    requests: DeferredToolRequests


class PartialRunResult(_RunTranscript):
    """The transcript of a run that ended without a result, closed by a boundary message that states the outcome."""

    outcome: RunOutcome
    error_type: str = Field(min_length=1)
    message: str = Field(min_length=1)


def _boundary(run_id: RunId, conversation_id: ConversationId, outcome: RunOutcome, message: str) -> AgentMessage:
    """Build the response that closes a partial transcript and states why the run ended."""
    return AgentMessage(
        role='response',
        parts=(TextPart(content=f'[Run ended: {outcome}: {message}]'),),
        run_id=run_id,
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
        request_usage=RequestUsage(),
        provider_name='ovid',
        finish_reason='error',
        state='interrupted',
    )


def is_boundary(message: AgentMessage) -> bool:
    """Identify the response that closes a partial transcript by the fields that `_boundary` writes."""
    return (
        message.role == 'response'
        and message.provider_name == 'ovid'
        and message.finish_reason == 'error'
        and message.state == 'interrupted'
    )


def _with_ending(partial: PartialRunResult, *, outcome: RunOutcome, error_type: str, message: str) -> PartialRunResult:
    """Replace the ending of a partial transcript, including its boundary response."""
    boundary = _boundary(partial.run_id, partial.conversation_id, outcome, message)

    return partial.model_copy(
        update={
            'messages': (*partial.messages[:-1], boundary),
            'history': (*partial.history[:-1], boundary),
            'outcome': outcome,
            'error_type': error_type,
            'message': message,
        }
    )


_SECRET_METADATA_KEYS = (
    'api_key',
    'access_token',
    'refresh_token',
    'api_token',
    'authorization',
    'password',
    'credential',
)
