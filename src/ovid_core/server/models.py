from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, PositiveInt, field_validator, model_validator

from ovid_core.messages.content import FileUrl, UserContent
from ovid_core.messages.models import AgentMessage
from ovid_core.models import BaseModel
from ovid_core.policy import ProviderFailureKind
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
    resume_restart_limit: PositiveInt = 3


class AgentRunRequest(BaseModel):
    prompt: Annotated[str, Field(min_length=1)] | Annotated[tuple[UserContent, ...], Field(min_length=1)] | None = None
    answers: Annotated[dict[str, DeferredToolAnswer], Field(min_length=1)] | None = None
    conversation_id: ConversationId | None = None
    resume: bool = False
    client_request_id: str | None = Field(default=None, min_length=1, max_length=128)

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
        if self.resume and (self.prompt is not None or self.answers is not None):
            raise ValueError('a resume request cannot carry a prompt or tool answers')
        if self.prompt is None and self.answers is None and not self.resume:
            raise ValueError('a run request requires a prompt, tool answers, or resume')
        if (self.answers is not None or self.resume) and self.conversation_id is None:
            raise ValueError('tool answers and resume require a conversation ID')

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


class ProviderErrorDetails(BaseModel):
    failure_kind: ProviderFailureKind
    status_code: int | None = Field(default=None, ge=100, le=599)
    provider_code: str | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0)


class ServerErrorResponse(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    run_id: RunId | None = Field(default=None, exclude_if=lambda run_id: run_id is None)
    provider: ProviderErrorDetails | None = Field(default=None, exclude_if=lambda provider: provider is None)


class ServerErrorSSEEvent(ServerErrorResponse):
    kind: Literal['server_error'] = 'server_error'
