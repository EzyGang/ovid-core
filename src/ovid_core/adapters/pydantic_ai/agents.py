import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import cast

from pydantic_ai import Agent
from pydantic_ai.agent import AgentRetries
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from ovid_core.adapters.pydantic_ai._agent_errors import normalize_run_error
from ovid_core.adapters.pydantic_ai._agent_stream import PydanticAIStream
from ovid_core.adapters.pydantic_ai.extensions import adapt_agent_extensions
from ovid_core.adapters.pydantic_ai.messages import message_to_pydantic
from ovid_core.adapters.pydantic_ai.results import result_from_pydantic
from ovid_core.agents import AgentDefinition, AgentRunPolicy, AgentRuntime, AgentStream
from ovid_core.errors import AgentConstructionError, OvidCoreError
from ovid_core.messages.models import AgentMessage
from ovid_core.routing.models import ResolvedModel
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.runtime.results import RunResult


class PydanticAIAgentCompiler:
    def compile[Deps, Output](
        self,
        definition: AgentDefinition[Deps, Output],
        resolved: ResolvedModel,
    ) -> AgentRuntime[Deps, Output]:
        runtime = resolved.handle.runtime
        if not isinstance(runtime, Model):
            raise AgentConstructionError('Resolved model is not compatible with the Pydantic AI adapter')

        try:
            extensions = adapt_agent_extensions(definition.capabilities, definition.toolsets, definition.hooks)
            policy = definition.policy
            agent = Agent[Deps, Output](
                runtime,
                output_type=definition.output_type,
                instructions=definition.instructions,
                deps_type=definition.deps_type,
                retries=cast(AgentRetries, policy.retries.model_dump()),
                toolsets=extensions.toolsets,
                capabilities=extensions.capabilities,
                end_strategy=policy.end_strategy,
                tool_timeout=policy.tool_timeout_seconds,
                max_concurrency=policy.max_concurrency,
            )
            agent.instrument = policy.instrumentation
        except OvidCoreError:
            raise
        except Exception as error:
            raise AgentConstructionError('Pydantic AI agent construction failed') from error

        return _PydanticAIAgentRuntime(agent=agent, policy=policy)


class _PydanticAIAgentRuntime[Deps, Output]:
    def __init__(self, *, agent: Agent[Deps, Output], policy: AgentRunPolicy) -> None:
        self._agent = agent
        self._policy = policy

    async def run(
        self,
        prompt: str,
        *,
        deps: Deps,
        messages: tuple[AgentMessage, ...],
        run_id: RunId | None,
        conversation_id: ConversationId | None,
    ) -> RunResult[Output]:
        run_identity, conversation_identity = _identities(messages, run_id, conversation_id)
        try:
            async with asyncio.timeout(self._policy.timeout_seconds):
                result = await self._agent.run(
                    prompt,
                    deps=deps,
                    message_history=tuple(message_to_pydantic(message) for message in messages),
                    run_id=str(run_identity),
                    conversation_id=str(conversation_identity),
                    usage_limits=_usage_limits(self._policy),
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise normalize_run_error(error) from error

        return result_from_pydantic(result)

    def stream(
        self,
        prompt: str,
        *,
        deps: Deps,
        messages: tuple[AgentMessage, ...],
        run_id: RunId | None,
        conversation_id: ConversationId | None,
    ) -> AbstractAsyncContextManager[AgentStream[Output]]:
        return self._stream(
            prompt,
            deps=deps,
            messages=messages,
            run_id=run_id,
            conversation_id=conversation_id,
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
    ) -> AsyncIterator[AgentStream[Output]]:
        run_identity, conversation_identity = _identities(messages, run_id, conversation_id)
        try:
            async with asyncio.timeout(self._policy.timeout_seconds):
                async with self._agent.run_stream_events(
                    prompt,
                    deps=deps,
                    message_history=tuple(message_to_pydantic(message) for message in messages),
                    run_id=str(run_identity),
                    conversation_id=str(conversation_identity),
                    usage_limits=_usage_limits(self._policy),
                ) as events:
                    yield PydanticAIStream(
                        events=events,
                        run_id=run_identity,
                        conversation_id=conversation_identity,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise normalize_run_error(error) from error


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
    )
