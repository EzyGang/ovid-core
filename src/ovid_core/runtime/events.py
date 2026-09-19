from typing import Annotated, Literal

from pydantic import Field, NonNegativeInt

from ovid_core.messages.models import AgentMessage, ToolCallData, ToolCallPart, ToolReturnData, ToolReturnPart
from ovid_core.models import BaseModel
from ovid_core.runtime.identifiers import ConversationId, RunId
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


class UsageUpdateEvent(EventIdentity):
    kind: Literal['usage_update'] = 'usage_update'
    usage: Usage
    request_usage: RequestUsage | None = None
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


def tool_events_from_messages(
    messages: tuple[AgentMessage, ...],
    *,
    run_id: RunId,
    conversation_id: ConversationId,
) -> tuple[ToolCallEvent | ToolResultEvent, ...]:
    events: list[ToolCallEvent | ToolResultEvent] = []

    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                event = ToolCallEvent(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    sequence=len(events),
                    tool_name=part.tool_name,
                    arguments=part.arguments,
                    tool_call_id=part.tool_call_id,
                )
            elif isinstance(part, ToolReturnPart):
                event = ToolResultEvent(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    sequence=len(events),
                    tool_name=part.tool_name,
                    content=part.content,
                    tool_call_id=part.tool_call_id,
                    outcome=part.outcome,
                )
            else:
                continue

            events.append(event)

    return tuple(events)
