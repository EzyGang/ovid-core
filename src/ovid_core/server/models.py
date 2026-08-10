from typing import Literal

from pydantic import Field, JsonValue, PositiveInt

from ovid_core.messages.models import AgentMessage
from ovid_core.models import BaseModel
from ovid_core.runtime.identifiers import ConversationId, RunId
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
    prompt: str = Field(min_length=1)
    conversation_id: ConversationId | None = None


class AgentRunResponse(BaseModel):
    output: JsonValue
    messages: tuple[AgentMessage, ...]
    usage: Usage
    run_id: RunId
    conversation_id: ConversationId


class RunResultSSEEvent(AgentRunResponse):
    kind: Literal['run_result'] = 'run_result'


class HealthResponse(BaseModel):
    status: Literal['ok', 'not_ready']


class ServerErrorResponse(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ServerErrorSSEEvent(ServerErrorResponse):
    kind: Literal['server_error'] = 'server_error'
