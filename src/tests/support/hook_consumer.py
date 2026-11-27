from collections.abc import Callable

from pydantic_ai.capabilities import Capability
from pydantic_ai.toolsets import FunctionToolset

from ovid_core.adapters.pydantic_ai import pydantic_ai_capability
from ovid_core.capabilities import BaseCapability
from ovid_core.errors import ToolExecutionError
from ovid_core.hooks import BaseToolHook, ToolCall, ToolDecision
from ovid_core.tools import ToolExecutionContext, ToolResult
from tests.support.agent_consumer import AgentDependencies


class DecisionHook[Deps](BaseToolHook[Deps]):
    """Return the given decisions in order, one for each call, and record each call that the hook receives."""

    def __init__(self, *decisions: ToolDecision | BaseException | None, log: list[str] | None = None) -> None:
        self.decisions = list(decisions)
        self.calls: list[ToolCall] = []
        self.log = log

    async def before_tool(self, context: ToolExecutionContext[Deps], call: ToolCall) -> ToolDecision | None:
        del context
        self.calls.append(call)
        if self.log is not None:
            self.log.append(f'{type(self).__name__}:{call.tool_name}')
        decision = self.decisions.pop(0) if self.decisions else None
        if isinstance(decision, BaseException):
            raise decision

        return decision


class ResultHook(BaseToolHook[AgentDependencies]):
    """Give each result to `rewrite`, and record each result that the hook receives."""

    def __init__(self, rewrite: Callable[[ToolResult], ToolResult | None]) -> None:
        self.rewrite = rewrite
        self.results: list[ToolResult] = []

    async def after_tool(
        self,
        context: ToolExecutionContext[AgentDependencies],
        call: ToolCall,
        result: ToolResult,
    ) -> ToolResult | None:
        del context, call
        self.results.append(result)

        return self.rewrite(result)


class RecoveryHook(BaseToolHook[AgentDependencies]):
    """Give each failure to `recover`, and record each failure that the hook receives."""

    def __init__(self, recover: Callable[[ToolExecutionError], ToolResult | None]) -> None:
        self.recover = recover
        self.errors: list[ToolExecutionError] = []

    async def on_tool_error(
        self,
        context: ToolExecutionContext[AgentDependencies],
        call: ToolCall,
        error: ToolExecutionError,
    ) -> ToolResult | None:
        del context, call
        self.errors.append(error)

        return self.recover(error)


def lookup_capability(calls: list[int], *, text: str = 'found') -> BaseCapability[AgentDependencies]:
    """Return a passthrough capability with the upstream function tool `lookup`."""
    toolset = FunctionToolset[AgentDependencies]()

    @toolset.tool_plain
    def lookup(value: int) -> str:
        calls.append(value)
        return f'{text} {value}'

    return pydantic_ai_capability(Capability[AgentDependencies](id='lookup', toolsets=[toolset]))
