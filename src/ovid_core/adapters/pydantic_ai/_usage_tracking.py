from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering, WrapRunHandler
from pydantic_ai.capabilities.abstract import ValidatedToolArgs
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.run import AgentRunResult
from pydantic_ai.tools import ToolDefinition

from ovid_core.adapters.pydantic_ai.usage import aggregate_usage_from_pydantic
from ovid_core.usage.models import Usage
from ovid_core.usage.tracking import UsageTracker


class RunUsageRecorder:
    def __init__(self, *, tracker: UsageTracker | None) -> None:
        self._tracker = tracker
        self._previous = Usage()

    async def record(self, current: Usage) -> None:
        delta = current.delta_since(self._previous)
        self._previous = current
        if self._tracker is not None:
            await self._tracker.add(delta)


class UsageTrackingCapability[Deps](AbstractCapability[Deps]):
    def __init__(self, *, tracker: UsageTracker | None, recorder: RunUsageRecorder) -> None:
        self.id = 'ovid_core.usage_tracking'
        self._tracker = tracker
        self._recorder = recorder

    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(position='outermost')

    async def before_model_request(
        self,
        ctx: RunContext[Deps],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        del ctx
        if self._tracker is not None:
            self._tracker.check_before_request()

        return request_context

    async def after_model_request(
        self,
        ctx: RunContext[Deps],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        del request_context
        await self._recorder.record(aggregate_usage_from_pydantic(ctx.usage))

        return response

    async def before_tool_execute(
        self,
        ctx: RunContext[Deps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
    ) -> ValidatedToolArgs:
        del ctx, call, tool_def
        if self._tracker is not None:
            self._tracker.check_before_tool_call()

        return args

    async def wrap_run(
        self,
        ctx: RunContext[Deps],
        *,
        handler: WrapRunHandler,
    ) -> AgentRunResult[Any]:
        try:
            return await handler()
        finally:
            await self._recorder.record(aggregate_usage_from_pydantic(ctx.usage))
