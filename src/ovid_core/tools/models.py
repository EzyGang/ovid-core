from typing import Literal

from pydantic import Field, JsonValue

from ovid_core.models import BaseModel


class ToolApproval(BaseModel):
    required: bool = False
    reason: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class AgentToolDescriptor(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str
    parameters_json_schema: dict[str, JsonValue]
    approval: ToolApproval
    timeout_seconds: float | None = None
    defer_loading: bool
    input_format: Literal['json', 'text']
    source: str = Field(min_length=1)


class AgentToolsetDescriptor(BaseModel):
    id: str = Field(min_length=1)
    description: str | None = None
    dynamic: bool
    source: str = Field(min_length=1)


class ToolResult(BaseModel):
    content: JsonValue
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
