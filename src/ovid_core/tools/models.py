from typing import Literal

from pydantic import Field, JsonValue

from ovid_core.models import BaseModel


class ToolGrammar(BaseModel):
    syntax: Literal['lark']
    definition: str = Field(min_length=1)


class ToolPresentation(BaseModel):
    wire_name: str | None = Field(default=None, min_length=1)
    input_format: Literal['json', 'text'] = 'json'
    grammar: ToolGrammar | None = None


class ToolApproval(BaseModel):
    required: bool = False
    reason: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ToolResult(BaseModel):
    content: JsonValue
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
