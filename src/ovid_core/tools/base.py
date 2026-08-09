from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

from pydantic import JsonValue

from ovid_core.models import BaseModel
from ovid_core.runtime.context import RunContext
from ovid_core.tools.models import ToolApproval, ToolResult


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

    @abstractmethod
    async def execute(self, context: ToolExecutionContext[Deps], arguments: Args) -> Result: ...


class BaseToolset[Deps](ABC):
    id: str

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
