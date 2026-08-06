from typing import Annotated, Literal

from pydantic import Field, JsonValue, NonNegativeInt

from ovid_core.messages.models import ToolArguments
from ovid_core.models import BaseModel
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.usage.models import Usage


class EventIdentity(BaseModel):
    run_id: RunId
    conversation_id: ConversationId
    sequence: NonNegativeInt


class RunStartedEvent(EventIdentity):
    kind: Literal['run_started'] = 'run_started'


class ModelRequestStartedEvent(EventIdentity):
    kind: Literal['model_request_started'] = 'model_request_started'
    request_index: NonNegativeInt


class TextDeltaEvent(EventIdentity):
    kind: Literal['text_delta'] = 'text_delta'
    content: str


class ToolCallEvent(EventIdentity):
    kind: Literal['tool_call'] = 'tool_call'
    tool_name: str = Field(min_length=1)
    arguments: ToolArguments = None
    tool_call_id: str = Field(min_length=1)


class ToolResultEvent(EventIdentity):
    kind: Literal['tool_result'] = 'tool_result'
    tool_name: str = Field(min_length=1)
    content: JsonValue
    tool_call_id: str = Field(min_length=1)
    outcome: Literal['success', 'failed', 'denied', 'interrupted'] = 'success'


class UsageUpdateEvent(EventIdentity):
    kind: Literal['usage_update'] = 'usage_update'
    usage: Usage
    is_final: bool = False


class RunCompletedEvent(EventIdentity):
    kind: Literal['run_completed'] = 'run_completed'
    usage: Usage


class RunFailedEvent(EventIdentity):
    kind: Literal['run_failed'] = 'run_failed'
    error_type: str = Field(min_length=1)
    message: str = Field(min_length=1)


AgentEvent = Annotated[
    RunStartedEvent
    | ModelRequestStartedEvent
    | TextDeltaEvent
    | ToolCallEvent
    | ToolResultEvent
    | UsageUpdateEvent
    | RunCompletedEvent
    | RunFailedEvent,
    Field(discriminator='kind'),
]
