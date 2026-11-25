from typing import Annotated, Literal

from pydantic import Field, JsonValue, NonNegativeInt

from ovid_core.messages.models import ToolCallData, ToolReturnData
from ovid_core.models import BaseModel
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.tools.deferred import DeferredToolKind
from ovid_core.usage.models import RequestUsage, Usage


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


class ToolCallEvent(ToolCallData, EventIdentity):
    kind: Literal['tool_call'] = 'tool_call'


class ToolResultEvent(ToolReturnData, EventIdentity):
    kind: Literal['tool_result'] = 'tool_result'


class ToolDeferredEvent(EventIdentity):
    kind: Literal['tool_deferred'] = 'tool_deferred'
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]
    deferral_kind: DeferredToolKind
    reason: str | None = None
    metadata: dict[str, JsonValue] | None = None


class UsageUpdateEvent(EventIdentity):
    kind: Literal['usage_update'] = 'usage_update'
    usage: Usage
    request_usage: RequestUsage | None = None
    is_final: bool = False


class RunCompletedEvent(EventIdentity):
    kind: Literal['run_completed'] = 'run_completed'
    usage: Usage


class RunDeferredEvent(EventIdentity):
    kind: Literal['run_deferred'] = 'run_deferred'
    usage: Usage


class RunFailedEvent(EventIdentity):
    kind: Literal['run_failed'] = 'run_failed'
    error_type: str = Field(min_length=1)
    message: str = Field(min_length=1)
    persisted: bool = False


AgentEvent = Annotated[
    RunStartedEvent
    | ModelRequestStartedEvent
    | TextDeltaEvent
    | ToolCallEvent
    | ToolResultEvent
    | ToolDeferredEvent
    | UsageUpdateEvent
    | RunCompletedEvent
    | RunDeferredEvent
    | RunFailedEvent,
    Field(discriminator='kind'),
]
