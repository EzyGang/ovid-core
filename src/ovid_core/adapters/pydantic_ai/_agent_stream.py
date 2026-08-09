import asyncio
from collections import deque
from collections.abc import AsyncIterator
from typing import cast

from pydantic import JsonValue, TypeAdapter
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolReturnPart,
)
from pydantic_ai.run import AgentRunResult, AgentRunResultEvent

from ovid_core.adapters.pydantic_ai._agent_errors import normalize_run_error
from ovid_core.adapters.pydantic_ai.results import result_from_pydantic
from ovid_core.agents import AgentStream
from ovid_core.errors import AgentRunError, OvidCoreError
from ovid_core.runtime.events import (
    AgentEvent,
    ModelRequestStartedEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunStartedEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
    UsageUpdateEvent,
)
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.runtime.results import RunResult


_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)


class PydanticAIStream[Output](AgentStream[Output]):
    def __init__(
        self,
        *,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Output]],
        run_id: RunId,
        conversation_id: ConversationId,
    ) -> None:
        self._events = events
        self._run_id = run_id
        self._conversation_id = conversation_id
        self._pending: deque[AgentEvent] = deque(
            (RunStartedEvent(run_id=run_id, conversation_id=conversation_id, sequence=0),)
        )
        self._result: RunResult[Output] | None = None
        self._error: OvidCoreError | None = None
        self._expect_request = True
        self._request_index = 0
        self._sequence = 1

    @property
    def result(self) -> RunResult[Output]:
        if self._result is None:
            raise AgentRunError('Agent stream has not completed')

        return self._result

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

    def _translate(self, event: AgentStreamEvent | AgentRunResultEvent[Output]) -> tuple[AgentEvent, ...]:
        if isinstance(event, PartStartEvent):
            return self._part_start_events(event)
        if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta) and event.delta.content_delta:
            return (
                TextDeltaEvent(
                    run_id=self._run_id,
                    conversation_id=self._conversation_id,
                    sequence=self._next_sequence(),
                    content=event.delta.content_delta,
                ),
            )
        if isinstance(event, FunctionToolCallEvent):
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
        if isinstance(event, FunctionToolResultEvent) and isinstance(event.part, ToolReturnPart):
            self._expect_request = True

            return (
                ToolResultEvent(
                    run_id=self._run_id,
                    conversation_id=self._conversation_id,
                    sequence=self._next_sequence(),
                    tool_name=event.part.tool_name,
                    content=_JSON_VALUE_ADAPTER.validate_python(event.part.content),
                    tool_call_id=event.part.tool_call_id,
                    outcome=event.part.outcome,
                ),
            )
        if isinstance(event, AgentRunResultEvent):
            result = result_from_pydantic(cast(AgentRunResult[Output], event.result))
            self._result = result

            return (
                UsageUpdateEvent(
                    run_id=self._run_id,
                    conversation_id=self._conversation_id,
                    sequence=self._next_sequence(),
                    usage=result.usage,
                    is_final=True,
                ),
                RunCompletedEvent(
                    run_id=self._run_id,
                    conversation_id=self._conversation_id,
                    sequence=self._next_sequence(),
                    usage=result.usage,
                ),
            )

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
            events.append(
                TextDeltaEvent(
                    run_id=self._run_id,
                    conversation_id=self._conversation_id,
                    sequence=self._next_sequence(),
                    content=event.part.content,
                )
            )

        return tuple(events)

    def _next_sequence(self) -> int:
        sequence = self._sequence
        self._sequence += 1

        return sequence
