import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Never

from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from ovid_core.adapters.pydantic_ai._agent_errors import normalize_run_error
from ovid_core.adapters.pydantic_ai._agent_stream import PydanticAIStream
from ovid_core.adapters.pydantic_ai._usage_tracking import RunUsageRecorder, UsageTrackingCapability
from ovid_core.adapters.pydantic_ai.messages import message_to_pydantic
from ovid_core.adapters.pydantic_ai.results import result_from_pydantic
from ovid_core.agents import AgentRuntime, AgentStream
from ovid_core.messages.models import AgentMessage
from ovid_core.policy import AgentRunPolicy
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.runtime.results import RunResult
from ovid_core.usage.tracking import UsageTracker


class PydanticAIAgentRuntime[Deps, Output](AgentRuntime[Deps, Output]):
    def __init__(self, *, agent: Agent[Deps, Output], policy: AgentRunPolicy) -> None:
        self._agent = agent
        self._policy = policy

    @property
    def upstream_agent(self) -> Agent[Deps, Output]:
        return self._agent

    @property
    def policy(self) -> AgentRunPolicy:
        return self._policy

    async def run(
        self,
        prompt: str,
        *,
        deps: Deps,
        messages: tuple[AgentMessage, ...],
        run_id: RunId | None,
        conversation_id: ConversationId | None,
        usage_tracker: UsageTracker | None,
    ) -> RunResult[Output]:
        run_identity, conversation_identity = _identities(messages, run_id, conversation_id)
        recorder = RunUsageRecorder(tracker=usage_tracker)
        capability = UsageTrackingCapability(tracker=usage_tracker, recorder=recorder)
        try:
            async with asyncio.timeout(self._policy.timeout_seconds):
                result = await self._agent.run(
                    prompt,
                    deps=deps,
                    message_history=tuple(message_to_pydantic(message) for message in messages),
                    run_id=str(run_identity),
                    conversation_id=str(conversation_identity),
                    usage_limits=_usage_limits(self._policy),
                    capabilities=(capability,),
                )
            normalized = result_from_pydantic(result)
            await recorder.record(normalized.usage)
        except Exception as error:
            _raise_normalized(error)

        return normalized

    def stream(
        self,
        prompt: str,
        *,
        deps: Deps,
        messages: tuple[AgentMessage, ...],
        run_id: RunId | None,
        conversation_id: ConversationId | None,
        usage_tracker: UsageTracker | None,
    ) -> AbstractAsyncContextManager[AgentStream[Output]]:
        return self._stream(
            prompt,
            deps=deps,
            messages=messages,
            run_id=run_id,
            conversation_id=conversation_id,
            usage_tracker=usage_tracker,
        )

    @asynccontextmanager
    async def _stream(
        self,
        prompt: str,
        *,
        deps: Deps,
        messages: tuple[AgentMessage, ...],
        run_id: RunId | None,
        conversation_id: ConversationId | None,
        usage_tracker: UsageTracker | None,
    ) -> AsyncIterator[AgentStream[Output]]:
        run_identity, conversation_identity = _identities(messages, run_id, conversation_id)
        recorder = RunUsageRecorder(tracker=usage_tracker)
        capability = UsageTrackingCapability(tracker=usage_tracker, recorder=recorder)
        stream: PydanticAIStream[Output] | None = None
        try:
            async with asyncio.timeout(self._policy.timeout_seconds):
                async with self._agent.run_stream_events(
                    prompt,
                    deps=deps,
                    message_history=tuple(message_to_pydantic(message) for message in messages),
                    run_id=str(run_identity),
                    conversation_id=str(conversation_identity),
                    usage_limits=_usage_limits(self._policy),
                    capabilities=(capability,),
                ) as events:
                    stream = PydanticAIStream(
                        events=events,
                        run_id=run_identity,
                        conversation_id=conversation_identity,
                    )
                    yield stream
        except Exception as error:
            _raise_normalized(error)

        if stream is None or not stream.complete:
            return

        await recorder.record(stream.result.usage)


def _identities(
    messages: tuple[AgentMessage, ...],
    run_id: RunId | None,
    conversation_id: ConversationId | None,
) -> tuple[RunId, ConversationId]:
    inherited_conversation_id = next(
        (message.conversation_id for message in reversed(messages) if message.conversation_id is not None),
        None,
    )

    return run_id or RunId.new(), conversation_id or inherited_conversation_id or ConversationId.new()


def _usage_limits(policy: AgentRunPolicy) -> UsageLimits:
    limits = policy.limits

    return UsageLimits(
        request_limit=limits.requests,
        tool_calls_limit=limits.tool_calls,
        input_tokens_limit=limits.input_tokens,
        output_tokens_limit=limits.output_tokens,
        total_tokens_limit=limits.total_tokens,
        per_request_input_tokens_limit=limits.per_request_input_tokens,
        count_tokens_before_request=limits.count_tokens_before_request,
    )


def _raise_normalized(error: Exception) -> Never:
    normalized = normalize_run_error(error)
    if normalized is error:
        raise error

    raise normalized from error
