from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from ovid_core.models import BaseModel
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.usage.models import RequestUsage


class SystemPromptPart(BaseModel):
    kind: Literal['system_prompt'] = 'system_prompt'
    content: str


class UserPromptPart(BaseModel):
    kind: Literal['user_prompt'] = 'user_prompt'
    content: str


class TextPart(BaseModel):
    kind: Literal['text'] = 'text'
    content: str


type ToolArguments = str | dict[str, JsonValue] | None


class ToolCallPart(BaseModel):
    kind: Literal['tool_call'] = 'tool_call'
    tool_name: str = Field(min_length=1)
    arguments: ToolArguments = None
    tool_call_id: str = Field(min_length=1)


class ToolReturnPart(BaseModel):
    kind: Literal['tool_return'] = 'tool_return'
    tool_name: str = Field(min_length=1)
    content: JsonValue
    tool_call_id: str = Field(min_length=1)
    outcome: Literal['success', 'failed', 'denied', 'interrupted'] = 'success'


class RetryPromptPart(BaseModel):
    kind: Literal['retry_prompt'] = 'retry_prompt'
    content: str
    tool_name: str | None = None
    tool_call_id: str = Field(min_length=1)


MessagePart = Annotated[
    SystemPromptPart | UserPromptPart | TextPart | ToolCallPart | ToolReturnPart | RetryPromptPart,
    Field(discriminator='kind'),
]

_REQUEST_PART_KINDS = frozenset({'system_prompt', 'user_prompt', 'tool_return', 'retry_prompt'})
_RESPONSE_PART_KINDS = frozenset({'text', 'tool_call'})


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
