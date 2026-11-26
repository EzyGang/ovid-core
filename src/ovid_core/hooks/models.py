from typing import Literal

from pydantic import Field, JsonValue

from ovid_core.models import BaseModel


type ToolCallSource = Literal['ovid', 'mcp', 'external']


class ToolCall(BaseModel):
    tool_name: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    arguments: dict[str, JsonValue]
    tool_id: str | None = None
    capability_id: str | None = None
    source: ToolCallSource

    def typed[Args: BaseModel](self, args_type: type[Args]) -> Args:
        return args_type.model_validate(self.arguments)


class Proceed(BaseModel):
    arguments: dict[str, JsonValue]


class Deny(BaseModel):
    message: str = Field(default='The tool call was denied.', min_length=1)


class RequireApproval(BaseModel):
    reason: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


type ToolDecision = Proceed | Deny | RequireApproval
