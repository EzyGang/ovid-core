import asyncio
from collections.abc import Awaitable, Callable

from ovid_core.errors import UsageLimitError
from ovid_core.policy import AgentUsageLimits
from ovid_core.usage.models import Usage


type UsageUpdateCallback = Callable[[UsageTracker, Usage], Awaitable[None]]


class UsageTracker:
    def __init__(
        self,
        *,
        limits: AgentUsageLimits | None = None,
        on_update: UsageUpdateCallback | None = None,
        _parent: UsageTracker | None = None,
    ) -> None:
        self._usage = Usage()
        self._limits = limits
        self._on_update = on_update
        self._parent = _parent
        self._lock = asyncio.Lock()

    @property
    def usage(self) -> Usage:
        return self._usage

    @property
    def aggregate_usage(self) -> Usage:
        return self._parent.aggregate_usage if self._parent is not None else self._usage

    @property
    def limits(self) -> AgentUsageLimits | None:
        return self._parent.limits if self._parent is not None else self._limits

    def create_child(self, *, on_update: UsageUpdateCallback | None = None) -> UsageTracker:
        return UsageTracker(on_update=on_update, _parent=self)

    async def add(self, delta: Usage) -> None:
        if delta.is_zero:
            return

        async with self._lock:
            self._usage = self._usage + delta
            current = self._usage

        if self._parent is not None:
            await self._parent.add(delta)
        else:
            _enforce_aggregate_limits(current, self._limits)

        if self._on_update is not None:
            await self._on_update(self, delta)

    def check_before_request(self) -> None:
        limits = self.limits
        if limits is None:
            return

        usage = self.aggregate_usage
        if limits.requests is not None and usage.request_count >= limits.requests:
            raise UsageLimitError('Aggregate request limit reached')
        if limits.input_tokens is not None and usage.input_tokens >= limits.input_tokens:
            raise UsageLimitError('Aggregate input token limit reached')
        if limits.output_tokens is not None and usage.output_tokens >= limits.output_tokens:
            raise UsageLimitError('Aggregate output token limit reached')
        if limits.total_tokens is not None and usage.total_tokens >= limits.total_tokens:
            raise UsageLimitError('Aggregate total token limit reached')

    def check_before_tool_call(self) -> None:
        limits = self.limits
        if limits is None or limits.tool_calls is None:
            return
        if self.aggregate_usage.tool_calls >= limits.tool_calls:
            raise UsageLimitError('Aggregate tool-call limit reached')


def _enforce_aggregate_limits(usage: Usage, limits: AgentUsageLimits | None) -> None:
    if limits is None:
        return

    exceeded = (
        (limits.requests is not None and usage.request_count > limits.requests)
        or (limits.tool_calls is not None and usage.tool_calls > limits.tool_calls)
        or (limits.input_tokens is not None and usage.input_tokens > limits.input_tokens)
        or (limits.output_tokens is not None and usage.output_tokens > limits.output_tokens)
        or (limits.total_tokens is not None and usage.total_tokens > limits.total_tokens)
    )
    if exceeded:
        raise UsageLimitError('Aggregate usage limit exceeded')
