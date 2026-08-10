import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from functools import partial
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from ag_ui.core import (
    AssistantMessage,
    BaseEvent,
    DeveloperMessage,
    RunAgentInput,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from pydantic_ai.run import AgentRunResult
from pydantic_ai.ui import NativeEvent
from pydantic_ai.ui.ag_ui import AGUIAdapter
from starlette.responses import StreamingResponse

from ovid_core.adapters.pydantic_ai._agent_runtime import PydanticAIAgentRuntime, _raise_normalized, _usage_limits
from ovid_core.adapters.pydantic_ai._usage_tracking import RunUsageRecorder, UsageTrackingCapability
from ovid_core.adapters.pydantic_ai.messages import message_from_pydantic, message_to_pydantic
from ovid_core.adapters.pydantic_ai.results import result_from_pydantic
from ovid_core.agents import OvidAgent
from ovid_core.errors import AgentRunError, ServerConstructionError
from ovid_core.messages.models import AgentMessage
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.runtime.results import RunResult


type AGUINativeEvent = NativeEvent
type CompletionCallback[Output] = Callable[[RunResult[Output]], Awaitable[None]]


class AGUIAuthorityError(ValueError):
    pass


class PydanticAIAGUIRun[Deps, Output]:
    def __init__(
        self,
        *,
        agent: OvidAgent[Deps, Output],
        agent_id: str,
        body: bytes,
        accept: str | None,
    ) -> None:
        runtime = agent._runtime_for_adapter()
        if not isinstance(runtime, PydanticAIAgentRuntime):
            raise ServerConstructionError('AG-UI requires a Pydantic AI agent runtime')

        runtime = cast(PydanticAIAgentRuntime[Deps, Output], runtime)

        run_input = _trusted_run_input(AGUIAdapter.build_run_input(body))
        self.conversation_id = ConversationId(root=uuid5(NAMESPACE_URL, f'ovid-ag-ui:{agent_id}:{run_input.thread_id}'))
        self._runtime: PydanticAIAgentRuntime[Deps, Output] = runtime
        self._adapter = AGUIAdapter[Deps, Output](
            agent=runtime.upstream_agent,
            run_input=run_input,
            accept=accept,
            manage_system_prompt='server',
        )
        self._messages = tuple(message_from_pydantic(message) for message in self._adapter.messages)
        self._recorder = RunUsageRecorder(tracker=None)

    def streaming_response(self, events: AsyncIterator[BaseEvent]) -> StreamingResponse:
        return self._adapter.streaming_response(events)

    async def native_stream(
        self,
        *,
        deps: Deps,
        messages: tuple[AgentMessage, ...],
        conversation_id: ConversationId,
    ) -> AsyncIterator[NativeEvent]:
        capability = UsageTrackingCapability(tracker=None, recorder=self._recorder)
        events = self._adapter.run_stream_native(
            message_history=tuple(message_to_pydantic(message) for message in messages),
            conversation_id=str(conversation_id),
            run_id=str(RunId.new()),
            deps=deps,
            usage_limits=_usage_limits(self._runtime.policy),
            capabilities=(capability,),
        )

        try:
            async for event in events:
                yield event
        except Exception as error:
            _raise_normalized(error)

    async def stream(
        self,
        events: AsyncIterator[NativeEvent],
        *,
        on_complete: CompletionCallback[Output],
    ) -> AsyncIterator[BaseEvent]:
        complete = partial(self._complete, on_complete=on_complete)

        async for event in self._adapter.transform_stream(events, on_complete=complete):
            yield event

    async def _complete(
        self,
        result: AgentRunResult[Output],
        *,
        on_complete: CompletionCallback[Output],
    ) -> None:
        try:
            normalized = result_from_pydantic(result)
            await self._recorder.record(normalized.usage)
            normalized = normalized.model_copy(update={'messages': (*self._messages, *normalized.messages)})
            await on_complete(normalized)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise AgentRunError('Agent run failed') from error


def _trusted_run_input(run_input: RunAgentInput) -> RunAgentInput:
    if (
        run_input.tools
        or run_input.context
        or run_input.resume
        or run_input.state not in (None, {})
        or run_input.forwarded_props not in (None, {})
    ):
        raise AGUIAuthorityError('Client-controlled AG-UI run state is not accepted')

    if any(
        isinstance(message, (SystemMessage, DeveloperMessage, ToolMessage))
        or (isinstance(message, AssistantMessage) and bool(message.tool_calls))
        or (isinstance(message, UserMessage) and not isinstance(message.content, str))
        for message in run_input.messages
    ):
        raise AGUIAuthorityError('Client-controlled AG-UI history is not accepted')

    if (
        not run_input.thread_id
        or not run_input.messages
        or not isinstance(run_input.messages[-1], UserMessage)
        or not run_input.messages[-1].content
    ):
        raise AGUIAuthorityError('AG-UI input must end with a non-empty user message')

    return run_input.model_copy(update={'messages': run_input.messages[-1:]})
