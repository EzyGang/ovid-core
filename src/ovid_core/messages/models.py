from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from ovid_core.identifiers import ConversationId, RunId
from ovid_core.messages.content import BinaryContent, UserContent
from ovid_core.models import BaseModel
from ovid_core.usage.models import RequestUsage


class SystemPromptPart(BaseModel):
    kind: Literal['system_prompt'] = 'system_prompt'
    content: str


class UserPromptPart(BaseModel):
    kind: Literal['user_prompt'] = 'user_prompt'
    content: str | Annotated[tuple[UserContent, ...], Field(min_length=1)]


class TextPart(BaseModel):
    kind: Literal['text'] = 'text'
    content: str


class ProviderPartData(BaseModel):
    id: str | None = None
    provider_name: str | None = None
    provider_details: dict[str, JsonValue] | None = None


class ThinkingPart(ProviderPartData):
    kind: Literal['thinking'] = 'thinking'
    content: str
    signature: str | None = None


class CompactionPart(ProviderPartData):
    kind: Literal['compaction'] = 'compaction'
    content: str | None = None


type ToolArguments = str | dict[str, JsonValue] | None


class ToolCallData(BaseModel):
    tool_name: str = Field(min_length=1)
    arguments: ToolArguments = None
    tool_call_id: str = Field(min_length=1)


class ToolCallPart(ToolCallData):
    kind: Literal['tool_call'] = 'tool_call'


class NativeToolCallPart(ToolCallData, ProviderPartData):
    kind: Literal['native_tool_call'] = 'native_tool_call'
    tool_kind: str | None = None


class ToolReturnData(BaseModel):
    tool_name: str = Field(min_length=1)
    content: JsonValue
    tool_call_id: str = Field(min_length=1)
    outcome: Literal['success', 'failed', 'denied', 'interrupted', 'retried'] = 'success'


class ToolReturnPart(ToolReturnData):
    kind: Literal['tool_return'] = 'tool_return'
    content_file_digests: tuple[str, ...] | None = None


class NativeToolReturnPart(ToolReturnData):
    kind: Literal['native_tool_return'] = 'native_tool_return'
    content_file_digests: tuple[str, ...] | None = None
    tool_kind: str | None = None
    metadata: JsonValue = None
    provider_name: str | None = None
    provider_details: dict[str, JsonValue] | None = None


class FilePart(ProviderPartData):
    kind: Literal['file'] = 'file'
    content: BinaryContent


class CapabilityLoadCallPart(BaseModel):
    kind: Literal['capability_load_call'] = 'capability_load_call'
    capability_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)


class CapabilityLoadReturnPart(BaseModel):
    kind: Literal['capability_load_return'] = 'capability_load_return'
    message: str | None = None
    instructions: str | None = None
    tool_call_id: str = Field(min_length=1)
    outcome: Literal['success', 'failed', 'denied', 'interrupted', 'retried'] = 'success'


class RetryPromptPart(BaseModel):
    kind: Literal['retry_prompt'] = 'retry_prompt'
    content: str
    tool_name: str | None = None
    tool_call_id: str = Field(min_length=1)


class OpaqueRequestPart(BaseModel):
    kind: Literal['opaque_request'] = 'opaque_request'
    part_kind: str = Field(min_length=1)
    payload: dict[str, JsonValue]


class OpaqueResponsePart(BaseModel):
    kind: Literal['opaque_response'] = 'opaque_response'
    part_kind: str = Field(min_length=1)
    payload: dict[str, JsonValue]


MessagePart = Annotated[
    SystemPromptPart
    | UserPromptPart
    | TextPart
    | CompactionPart
    | ThinkingPart
    | ToolCallPart
    | NativeToolCallPart
    | ToolReturnPart
    | NativeToolReturnPart
    | FilePart
    | CapabilityLoadCallPart
    | CapabilityLoadReturnPart
    | RetryPromptPart
    | OpaqueRequestPart
    | OpaqueResponsePart,
    Field(discriminator='kind'),
]

_REQUEST_PART_KINDS = frozenset(
    {
        'system_prompt',
        'user_prompt',
        'compaction',
        'tool_return',
        'capability_load_return',
        'retry_prompt',
        'opaque_request',
    }
)
_RESPONSE_PART_KINDS = frozenset(
    {
        'text',
        'thinking',
        'compaction',
        'tool_call',
        'native_tool_call',
        'native_tool_return',
        'file',
        'capability_load_call',
        'opaque_response',
    }
)


class AgentMessage(BaseModel):
    role: Literal['request', 'response']
    parts: tuple[MessagePart, ...]
    run_id: RunId | None = None
    conversation_id: ConversationId | None = None
    timestamp: datetime | None = None
    request_usage: RequestUsage | None = None
    instructions: str | None = None
    model_name: str | None = None
    provider_name: str | None = None
    provider_response_id: str | None = None
    provider_details: dict[str, JsonValue] | None = None
    finish_reason: Literal['stop', 'length', 'content_filter', 'tool_call', 'error'] | None = None

    @model_validator(mode='after')
    def validate_role_fields(self) -> Self:
        allowed_kinds = _REQUEST_PART_KINDS if self.role == 'request' else _RESPONSE_PART_KINDS

        if any(part.kind not in allowed_kinds for part in self.parts):
            raise ValueError(f'{self.role} message contains a part for the opposite role')
        if self.role == 'request' and self.request_usage is not None:
            raise ValueError('request_usage is only valid on response messages')
        if self.role == 'response' and self.request_usage is None:
            raise ValueError('response messages require request_usage')

        return self


def pending_tool_calls(messages: Sequence[AgentMessage]) -> tuple[ToolCallPart, ...]:
    """Return the tool calls of the last response that have no result in a later request."""
    responses = [index for index, message in enumerate(messages) if message.role == 'response']
    if not responses:
        return ()

    answered = {
        part.tool_call_id
        for message in messages[responses[-1] + 1 :]
        for part in message.parts
        if isinstance(part, ToolReturnPart | RetryPromptPart)
    }

    return tuple(
        part
        for part in messages[responses[-1]].parts
        if isinstance(part, ToolCallPart) and part.tool_call_id not in answered
    )
