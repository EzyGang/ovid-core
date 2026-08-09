from pydantic import Field, JsonValue

from ovid_core.models import BaseModel


class ToolApproval(BaseModel):
    required: bool = False
    reason: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ToolResult(BaseModel):
    content: JsonValue
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
