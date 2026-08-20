from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Self, cast

from pydantic import JsonValue

from ovid_core.models import BaseModel
from ovid_core.runtime.context import RunContext
from ovid_core.tools.models import AgentToolDescriptor, AgentToolsetDescriptor, ToolApproval, ToolResult


class ToolGrammar(BaseModel):
    syntax: Literal['lark']
    definition: str


class ToolPresentation(BaseModel):
    wire_name: str | None = None
    input_format: Literal['json', 'text'] = 'json'
    grammar: ToolGrammar | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionContext[Deps]:
    run: RunContext[Deps]
    tool_call_id: str
    approved: bool = False
    approval_metadata: JsonValue = None


class BaseTool[Deps, Args: BaseModel, Result: ToolResult](ABC):
    id: str
    description: str
    args_type: type[Args]
    result_type: type[Result]
    approval: ToolApproval = ToolApproval()
    timeout_seconds: float | None = None
    defer_loading: bool = False
    presentation: ToolPresentation = ToolPresentation()

    def descriptor(
        self,
        *,
        source: str,
        approval: ToolApproval | None = None,
    ) -> AgentToolDescriptor:
        presentation = self.presentation

        return AgentToolDescriptor(
            id=self.id,
            name=presentation.wire_name or self.id,
            description=self.description,
            parameters_json_schema=cast(dict[str, JsonValue], self.args_type.model_json_schema()),
            approval=approval if approval is not None else self.approval,
            timeout_seconds=self.timeout_seconds,
            defer_loading=self.defer_loading,
            input_format=presentation.input_format,
            source=source,
        )

    @abstractmethod
    async def execute(self, context: ToolExecutionContext[Deps], arguments: Args) -> Result: ...


class BaseToolset[Deps](ABC):
    id: str
    description: str | None = None

    def descriptor(self, *, source: str) -> AgentToolsetDescriptor:
        return AgentToolsetDescriptor(
            id=self.id,
            description=self.description,
            dynamic=True,
            source=source,
        )

    async def for_run(self, context: RunContext[Deps]) -> Self:
        return self

    async def for_step(self, context: RunContext[Deps]) -> Self:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return None

    @abstractmethod
    async def get_tools(self, context: RunContext[Deps]) -> Sequence[BaseTool[Deps, Any, Any]]: ...
