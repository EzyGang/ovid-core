from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, PositiveInt, field_validator, model_validator

from ovid_core.messages.content import FileUrl, UserContent
from ovid_core.messages.models import AgentMessage
from ovid_core.models import BaseModel
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.tools.deferred import DeferredToolAnswer, DeferredToolRequests
from ovid_core.usage.models import Usage


class ServerConfig(BaseModel):
    host: str = Field(default='127.0.0.1', min_length=1)
    port: int = Field(default=8000, ge=1, le=65535)
    max_body_bytes: PositiveInt = 1_048_576
    request_timeout_seconds: float = Field(default=60.0, gt=0)
    max_concurrency: PositiveInt = 32
    allowed_origins: tuple[str, ...] = ()
    shutdown_grace_seconds: PositiveInt = 10


class AgentRunRequest(BaseModel):
    prompt: Annotated[str, Field(min_length=1)] | Annotated[tuple[UserContent, ...], Field(min_length=1)] | None = None
    answers: Annotated[dict[str, DeferredToolAnswer], Field(min_length=1)] | None = None
    conversation_id: ConversationId | None = None

    @field_validator('prompt')
    @classmethod
    def reject_local_downloads(
        cls,
        value: str | tuple[UserContent, ...] | None,
    ) -> str | tuple[UserContent, ...] | None:
        if value is None or isinstance(value, str):
            return value
        if any(isinstance(item, FileUrl) and item.force_download == 'allow-local' for item in value):
            raise ValueError('a request prompt cannot allow downloads from local addresses')

        return value

    @model_validator(mode='after')
    def require_run_input(self) -> Self:
        if self.prompt is None and self.answers is None:
            raise ValueError('a run request requires a prompt or tool answers')
        if self.answers is not None and self.conversation_id is None:
            raise ValueError('tool answers require a conversation ID')

        return self


class AgentRunResponse(BaseModel):
    output: JsonValue
    messages: tuple[AgentMessage, ...]
    usage: Usage
    run_id: RunId
    conversation_id: ConversationId
    requests: DeferredToolRequests | None = Field(default=None, exclude_if=lambda requests: requests is None)


class RunResultSSEEvent(AgentRunResponse):
    kind: Literal['run_result'] = 'run_result'


class HealthResponse(BaseModel):
    status: Literal['ok', 'not_ready']


class ServerErrorResponse(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ServerErrorSSEEvent(ServerErrorResponse):
    kind: Literal['server_error'] = 'server_error'
