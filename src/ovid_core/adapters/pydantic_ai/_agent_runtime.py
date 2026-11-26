import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Never

from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import UserContent as PydanticUserContent
from pydantic_ai.tools import DeferredToolRequests as PydanticDeferredToolRequests
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai.usage import UsageLimits

from ovid_core.adapters.pydantic_ai._agent_errors import normalize_run_error
from ovid_core.adapters.pydantic_ai._agent_stream import PydanticAIStream
from ovid_core.adapters.pydantic_ai._deferred_tools import (
    InterruptedCalls,
    StaticApprovalRecorder,
    deferred_results_to_pydantic,
    interrupted_calls,
)
from ovid_core.adapters.pydantic_ai._journal import TranscriptJournalCapability
from ovid_core.adapters.pydantic_ai._usage_tracking import RunUsageRecorder, UsageTrackingCapability
from ovid_core.adapters.pydantic_ai._user_content import user_content_to_pydantic
from ovid_core.adapters.pydantic_ai.messages import message_to_pydantic
from ovid_core.adapters.pydantic_ai.results import result_from_pydantic
from ovid_core.agent_runtime import AgentRuntime, AgentStream
from ovid_core.errors import AgentRunInputError, CredentialError
from ovid_core.messages.content import UserContent
from ovid_core.messages.models import AgentMessage
from ovid_core.persistence import RunJournal
from ovid_core.policy import AgentRunPolicy
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.runtime.results import DeferredRunResult, RunResult
from ovid_core.tools.deferred import DeferredToolResume
from ovid_core.usage.tracking import UsageTracker


@dataclass(frozen=True, slots=True)
class _RunInput[Deps]:
    prompt: str | Sequence[PydanticUserContent] | None
    history: tuple[ModelMessage, ...]
    results: DeferredToolResults | None
    run_id: RunId
    conversation_id: ConversationId
    recorder: RunUsageRecorder
    approvals: StaticApprovalRecorder[Deps]
    capabilities: tuple[AbstractCapability[Deps], ...]
    interrupted: InterruptedCalls | None


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
        prompt: str | Sequence[UserContent] | None,
        *,
        resume: DeferredToolResume | None,
        deps: Deps,
        messages: tuple[AgentMessage, ...],
        run_id: RunId | None,
        conversation_id: ConversationId | None,
        usage_tracker: UsageTracker | None,
    ) -> RunResult[Output] | DeferredRunResult:
        run = self._run_input(prompt, resume, messages, run_id, conversation_id, usage_tracker, journal=None)
        try:
            async with asyncio.timeout(self._policy.timeout_seconds):
                result = await self._agent.run(
                    run.prompt,
                    output_type=[self._agent.output_type, PydanticDeferredToolRequests],
                    deps=deps,
                    message_history=run.history,
                    deferred_tool_results=run.results,
                    run_id=str(run.run_id),
                    conversation_id=str(run.conversation_id),
                    usage_limits=_usage_limits(self._policy),
                    capabilities=run.capabilities,
                )
            normalized = result_from_pydantic(result, approvals=run.approvals.approvals, interrupted=run.interrupted)
            await run.recorder.record(normalized.usage)
        except Exception as error:
            _raise_normalized(error)

        return normalized

    def stream(
        self,
        prompt: str | Sequence[UserContent] | None,
        *,
        resume: DeferredToolResume | None,
        deps: Deps,
        messages: tuple[AgentMessage, ...],
        run_id: RunId | None,
        conversation_id: ConversationId | None,
        usage_tracker: UsageTracker | None,
        journal: RunJournal | None,
    ) -> AbstractAsyncContextManager[AgentStream[Output]]:
        return self._stream(
            prompt,
            resume=resume,
            deps=deps,
            messages=messages,
            run_id=run_id,
            conversation_id=conversation_id,
            usage_tracker=usage_tracker,
            journal=journal,
        )

    @asynccontextmanager
    async def _stream(
        self,
        prompt: str | Sequence[UserContent] | None,
        *,
        resume: DeferredToolResume | None,
        deps: Deps,
        messages: tuple[AgentMessage, ...],
        run_id: RunId | None,
        conversation_id: ConversationId | None,
        usage_tracker: UsageTracker | None,
        journal: RunJournal | None,
    ) -> AsyncGenerator[AgentStream[Output]]:
        run = self._run_input(prompt, resume, messages, run_id, conversation_id, usage_tracker, journal=journal)
        stream: PydanticAIStream[Output] | None = None
        with capture_run_messages() as captured:
            try:
                async with (
                    asyncio.timeout(self._policy.timeout_seconds),
                    self._agent.run_stream_events(
                        run.prompt,
                        output_type=[self._agent.output_type, PydanticDeferredToolRequests],
                        deps=deps,
                        message_history=run.history,
                        deferred_tool_results=run.results,
                        run_id=str(run.run_id),
                        conversation_id=str(run.conversation_id),
                        usage_limits=_usage_limits(self._policy),
                        capabilities=run.capabilities,
                    ) as events,
                ):
                    stream = PydanticAIStream(
                        events=events,
                        run_id=run.run_id,
                        conversation_id=run.conversation_id,
                        approvals=run.approvals.approvals,
                        interrupted=run.interrupted,
                    )
                    yield stream
            except (Exception, asyncio.CancelledError) as error:
                normalized = error if isinstance(error, asyncio.CancelledError) else normalize_run_error(error)
                if stream is not None:
                    stream.record_failure(captured, normalized, run.recorder.usage)
                _raise_as(error, normalized)

        if stream is None or not stream.complete:
            return

        await run.recorder.record(stream.result.usage)

    def _run_input(
        self,
        prompt: str | Sequence[UserContent] | None,
        resume: DeferredToolResume | None,
        messages: tuple[AgentMessage, ...],
        run_id: RunId | None,
        conversation_id: ConversationId | None,
        usage_tracker: UsageTracker | None,
        *,
        journal: RunJournal | None,
    ) -> _RunInput[Deps]:
        if prompt is None and resume is None:
            raise AgentRunInputError('Agent run requires a prompt or resume input')

        run_identity, conversation_identity = _identities(messages, run_id, conversation_id)
        interrupted = interrupted_calls(messages, conversation_identity) if resume is None else None
        history = messages if interrupted is None else (*messages, interrupted.closing)
        recorder = RunUsageRecorder(tracker=usage_tracker)
        approvals = StaticApprovalRecorder[Deps]()
        usage = UsageTrackingCapability[Deps](tracker=usage_tracker, recorder=recorder)
        closing = interrupted.closing if interrupted is not None else None
        journaling = (
            ()
            if journal is None
            else (TranscriptJournalCapability[Deps](journal=journal, run_id=run_identity, closing=closing),)
        )

        return _RunInput(
            prompt=prompt if prompt is None or isinstance(prompt, str) else user_content_to_pydantic(prompt),
            history=tuple(message_to_pydantic(message) for message in history),
            results=deferred_results_to_pydantic(resume.answers) if resume is not None else None,
            run_id=run_identity,
            conversation_id=conversation_identity,
            recorder=recorder,
            approvals=approvals,
            capabilities=(usage, approvals, *journaling),
            interrupted=interrupted,
        )


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
    _raise_as(error, normalize_run_error(error))


def _raise_as(error: BaseException, normalized: BaseException) -> Never:
    if normalized is error:
        raise error
    if isinstance(normalized, CredentialError):
        raise normalized from None

    raise normalized from error
