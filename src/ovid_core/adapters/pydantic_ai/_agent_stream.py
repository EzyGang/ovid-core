import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, cast

from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.run import AgentRunResult, AgentRunResultEvent

from ovid_core.adapters.pydantic_ai._agent_errors import normalize_run_error
from ovid_core.adapters.pydantic_ai._deferred_tools import InterruptedCalls
from ovid_core.adapters.pydantic_ai._request_parts import tool_content_from_pydantic
from ovid_core.adapters.pydantic_ai.results import partial_from_pydantic, result_from_pydantic
from ovid_core.agent_runtime import AgentStream
from ovid_core.errors import AgentRunError, OvidCoreError
from ovid_core.runtime.events import (
    AgentEvent,
    ModelRequestStartedEvent,
    RunCompletedEvent,
    RunDeferredEvent,
    RunFailedEvent,
    RunStartedEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolDeferredEvent,
    ToolResultEvent,
    UsageUpdateEvent,
)
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.runtime.results import DeferredRunResult, PartialRunResult, RunResult
from ovid_core.tools.models import ToolApproval
from ovid_core.usage.models import Usage


class PydanticAIStream[Output](AgentStream[Output]):
    def __init__(
        self,
        *,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
        run_id: RunId,
        conversation_id: ConversationId,
        approvals: Mapping[str, ToolApproval],
        interrupted: InterruptedCalls | None,
    ) -> None:
        self._events = events
        self._run_id = run_id
        self._conversation_id = conversation_id
        self._approvals = approvals
        self._interrupted = interrupted
        self._pending: deque[AgentEvent] = deque(
            (RunStartedEvent(run_id=run_id, conversation_id=conversation_id, sequence=0),)
        )
        self._result: RunResult[Output] | DeferredRunResult | None = None
        self._error: OvidCoreError | None = None
        self._partial: PartialRunResult | None = None
        self._expect_request = True
        self._request_index = 0
        self._sequence = 1

    @property
    def result(self) -> RunResult[Output] | DeferredRunResult:
        if self._result is None:
            raise AgentRunError('Agent stream has not completed')

        return self._result

    @property
    def complete(self) -> bool:
        return self._result is not None

    def partial(self) -> PartialRunResult | None:
        return self._partial

    def record_failure(self, captured: Sequence[ModelMessage], error: BaseException, usage: Usage) -> None:
        self._partial = partial_from_pydantic(
            captured,
            error=error,
            run_id=self._run_id,
            conversation_id=self._conversation_id,
            usage=usage,
            interrupted=self._interrupted,
        )

    def __aiter__(self) -> PydanticAIStream[Output]:
        return self

    async def __anext__(self) -> AgentEvent:
        if self._pending:
            return self._pending.popleft()
        if self._error is not None:
            raise self._error

        while True:
            try:
                upstream = await anext(self._events)
            except StopAsyncIteration:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._error = normalize_run_error(error)
                return RunFailedEvent(
                    run_id=self._run_id,
                    conversation_id=self._conversation_id,
                    sequence=self._next_sequence(),
                    error_type=type(self._error).__name__,
                    message=str(self._error),
                )

            self._pending.extend(self._translate(upstream))
            if self._pending:
                return self._pending.popleft()

    def _translate(self, event: AgentStreamEvent | AgentRunResultEvent[Any]) -> tuple[AgentEvent, ...]:
        if isinstance(event, PartStartEvent):
            return self._part_start_events(event)
        if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta) and event.delta.content_delta:
            return (self._text_delta(event.delta.content_delta),)
        if isinstance(event, FunctionToolCallEvent) and event.part.tool_name and event.part.tool_call_id:
            return (
                ToolCallEvent(
                    run_id=self._run_id,
                    conversation_id=self._conversation_id,
                    sequence=self._next_sequence(),
                    tool_name=event.part.tool_name,
                    arguments=event.part.args,
                    tool_call_id=event.part.tool_call_id,
                ),
            )
        if isinstance(event, FunctionToolResultEvent):
            return self._tool_result_events(event)
        if isinstance(event, AgentRunResultEvent):
            return self._result_events(cast(AgentRunResult[Output], event.result))

        return ()

    def _part_start_events(self, event: PartStartEvent) -> tuple[AgentEvent, ...]:
        events: list[AgentEvent] = []
        if self._expect_request:
            events.append(
                ModelRequestStartedEvent(
                    run_id=self._run_id,
                    conversation_id=self._conversation_id,
                    sequence=self._next_sequence(),
                    request_index=self._request_index,
                )
            )
            self._request_index += 1
            self._expect_request = False
        if isinstance(event.part, TextPart) and event.part.content:
            events.append(self._text_delta(event.part.content))

        return tuple(events)

    def _text_delta(self, content: str) -> TextDeltaEvent:
        return TextDeltaEvent(
            run_id=self._run_id,
            conversation_id=self._conversation_id,
            sequence=self._next_sequence(),
            content=content,
        )

    def _tool_result_events(self, event: FunctionToolResultEvent) -> tuple[AgentEvent, ...]:
        self._expect_request = True
        if not (event.part.tool_name and event.part.tool_call_id):
            return ()

        return (
            ToolResultEvent(
                run_id=self._run_id,
                conversation_id=self._conversation_id,
                sequence=self._next_sequence(),
                tool_name=event.part.tool_name,
                content=tool_content_from_pydantic(event.part)[0],
                tool_call_id=event.part.tool_call_id,
                outcome=event.part.outcome,
            ),
        )

    def _result_events(self, value: AgentRunResult[Output]) -> tuple[AgentEvent, ...]:
        result = result_from_pydantic(value, approvals=self._approvals, interrupted=self._interrupted)
        self._result = result
        request_usage = next(
            (
                message.request_usage
                for message in reversed(result.history or result.messages)
                if message.role == 'response'
            ),
            None,
        )
        deferred = self._deferred_events(result) if isinstance(result, DeferredRunResult) else ()
        usage_update = UsageUpdateEvent(
            run_id=self._run_id,
            conversation_id=self._conversation_id,
            sequence=self._next_sequence(),
            usage=result.usage,
            request_usage=request_usage,
            is_final=True,
        )
        terminal_type = RunDeferredEvent if isinstance(result, DeferredRunResult) else RunCompletedEvent
        terminal = terminal_type(
            run_id=self._run_id,
            conversation_id=self._conversation_id,
            sequence=self._next_sequence(),
            usage=result.usage,
        )

        return (*deferred, usage_update, terminal)

    def _deferred_events(self, result: DeferredRunResult) -> tuple[ToolDeferredEvent, ...]:
        return tuple(
            ToolDeferredEvent(
                run_id=self._run_id,
                conversation_id=self._conversation_id,
                sequence=self._next_sequence(),
                tool_call_id=request.tool_call_id,
                tool_name=request.tool_name,
                arguments=request.arguments,
                deferral_kind=request.kind,
                reason=request.approval.reason if request.approval is not None else None,
                metadata=request.metadata,
            )
            for request in result.requests.calls
        )

    def _next_sequence(self) -> int:
        sequence = self._sequence
        self._sequence += 1

        return sequence
