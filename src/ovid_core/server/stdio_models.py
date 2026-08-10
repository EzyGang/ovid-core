from typing import Annotated, Literal

from pydantic import Field, JsonValue

from ovid_core.models import BaseModel
from ovid_core.runtime.events import AgentEvent
from ovid_core.server.models import AgentRunRequest, AgentRunResponse, ServerErrorResponse


class StdioDescriptor(BaseModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)


class _StdioRequest(BaseModel):
    version: Literal[1] = 1
    request_id: str = Field(min_length=1)


class StdioInitializeRequest(_StdioRequest):
    type: Literal['initialize'] = 'initialize'


class StdioRunRequest(_StdioRequest):
    type: Literal['run'] = 'run'
    agent_id: str = Field(min_length=1)
    request: AgentRunRequest


class StdioCommandRequest(_StdioRequest):
    type: Literal['command'] = 'command'
    command_id: str = Field(min_length=1)
    arguments: JsonValue = None


StdioRequest = Annotated[
    StdioInitializeRequest | StdioRunRequest | StdioCommandRequest,
    Field(discriminator='type'),
]


class _StdioResponse(BaseModel):
    version: Literal[1] = 1
    request_id: str | None


class StdioInitializedResponse(_StdioResponse):
    type: Literal['initialized'] = 'initialized'
    agents: tuple[StdioDescriptor, ...]
    commands: tuple[StdioDescriptor, ...]


class StdioEventResponse(_StdioResponse):
    type: Literal['event'] = 'event'
    event: AgentEvent


class StdioRunResultResponse(_StdioResponse):
    type: Literal['run_result'] = 'run_result'
    result: AgentRunResponse


class StdioCommandResultResponse(_StdioResponse):
    type: Literal['command_result'] = 'command_result'
    result: JsonValue


class StdioErrorResponse(_StdioResponse):
    type: Literal['error'] = 'error'
    error: ServerErrorResponse


StdioResponse = (
    StdioInitializedResponse
    | StdioEventResponse
    | StdioRunResultResponse
    | StdioCommandResultResponse
    | StdioErrorResponse
)
