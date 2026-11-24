import asyncio
import json
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.run import AgentRunResult
from pydantic_ai.tools import DeferredToolRequests as PydanticDeferredToolRequests

from ovid_core.adapters.pydantic_ai._deferred_tools import InterruptedCalls, deferred_requests_from_pydantic
from ovid_core.adapters.pydantic_ai.messages import message_from_pydantic
from ovid_core.adapters.pydantic_ai.usage import usage_from_pydantic
from ovid_core.errors import ContextWindowError, ProviderError, UsageLimitError
from ovid_core.messages.models import AgentMessage, MessagePart, ToolCallPart
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.runtime.results import (
    DeferredRunResult,
    PartialRunResult,
    ResultMetadataEntry,
    RunOutcome,
    RunResult,
    _boundary,
)
from ovid_core.tools.models import ToolApproval
from ovid_core.usage.models import Usage


def result_from_pydantic[Output](
    value: AgentRunResult[Output] | AgentRunResult[Output | PydanticDeferredToolRequests],
    *,
    approvals: Mapping[str, ToolApproval] = MappingProxyType({}),
    interrupted: InterruptedCalls | None = None,
) -> RunResult[Output] | DeferredRunResult:
    try:
        messages, history = _run_messages(value.new_messages(), value.all_messages(), interrupted)
        request_usage = tuple(
            message.request_usage
            for message in messages
            if message.role == 'response' and message.request_usage is not None
        )
        usage = usage_from_pydantic(value.usage, request_usage)
        run_id = RunId(value.run_id)
        conversation_id = ConversationId(value.conversation_id)
        output = value.output
        if isinstance(output, PydanticDeferredToolRequests):
            return DeferredRunResult(
                requests=deferred_requests_from_pydantic(output, approvals),
                messages=messages,
                history=history,
                usage=usage,
                run_id=run_id,
                conversation_id=conversation_id,
            )

        return RunResult[Output](
            output=output,
            messages=messages,
            history=history,
            usage=usage,
            run_id=run_id,
            conversation_id=conversation_id,
            metadata=tuple(
                ResultMetadataEntry(key=key, value=item) for key, item in sorted((value.metadata or {}).items())
            ),
        )
    except (TypeError, ValidationError, ValueError) as error:
        raise ProviderError('Pydantic AI returned an invalid run result') from error


def partial_from_pydantic(
    captured: Sequence[ModelMessage],
    *,
    error: BaseException,
    run_id: RunId,
    conversation_id: ConversationId,
    usage: Usage,
    interrupted: InterruptedCalls | None,
) -> PartialRunResult | None:
    """Build the transcript of a failed run from the messages that `capture_run_messages` kept.

    Returns `None` when the run added no message. Interrupted responses lose tool calls whose arguments are not JSON.
    """
    run_messages = [message for message in captured if message.run_id == str(run_id)]
    if not run_messages:
        return None

    messages, history = _run_messages(run_messages, captured, interrupted)
    outcome, error_type, text = _ending(error)
    boundary = _boundary(run_id, conversation_id, outcome, text)

    return PartialRunResult(
        messages=(*map(_without_unparsable_calls, messages), boundary),
        history=(*map(_without_unparsable_calls, history), boundary),
        usage=usage,
        run_id=run_id,
        conversation_id=conversation_id,
        outcome=outcome,
        error_type=error_type,
        message=text,
    )


def _run_messages(
    new_messages: Sequence[ModelMessage],
    all_messages: Sequence[ModelMessage],
    interrupted: InterruptedCalls | None,
) -> tuple[tuple[AgentMessage, ...], tuple[AgentMessage, ...]]:
    """Return the run delta and the history. The closing request of interrupted calls starts the delta."""
    messages = tuple(message_from_pydantic(message) for message in new_messages)
    if interrupted is None:
        return messages, tuple(message_from_pydantic(message) for message in all_messages)

    messages = (interrupted.closing, *messages)

    return messages, (*interrupted.history, *messages)


def _ending(error: BaseException) -> tuple[RunOutcome, str, str]:
    """Classify a normalized run error as an outcome, an error type and a message that is safe to store."""
    if isinstance(error, asyncio.CancelledError):
        return 'cancelled', 'CancelledError', 'Run cancelled'

    outcome: RunOutcome = 'failed'
    if isinstance(error, ContextWindowError):
        outcome = 'context_overflow'
    elif isinstance(error, UsageLimitError):
        outcome = 'usage_limit'
    elif isinstance(error, TimeoutError):
        outcome = 'timeout'

    return outcome, type(error).__name__, str(error) or 'Agent run failed'


def _without_unparsable_calls(message: AgentMessage) -> AgentMessage:
    if message.state != 'interrupted':
        return message

    return message.model_copy(
        update={'parts': tuple(part for part in message.parts if not _unparsable_call(part))},
    )


def _unparsable_call(part: MessagePart) -> bool:
    if not isinstance(part, ToolCallPart) or not isinstance(part.arguments, str) or not part.arguments:
        return False
    try:
        json.loads(part.arguments)
    except ValueError:
        return True

    return False
