from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError
from pydantic_ai.run import AgentRunResult
from pydantic_ai.tools import DeferredToolRequests as PydanticDeferredToolRequests

from ovid_core.adapters.pydantic_ai._deferred_tools import InterruptedCalls, deferred_requests_from_pydantic
from ovid_core.adapters.pydantic_ai.messages import message_from_pydantic
from ovid_core.adapters.pydantic_ai.usage import usage_from_pydantic
from ovid_core.errors import ProviderError
from ovid_core.messages.models import AgentMessage
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.runtime.results import DeferredRunResult, ResultMetadataEntry, RunResult
from ovid_core.tools.models import ToolApproval


def result_from_pydantic[Output](
    value: AgentRunResult[Output] | AgentRunResult[Output | PydanticDeferredToolRequests],
    *,
    approvals: Mapping[str, ToolApproval] = MappingProxyType({}),
    interrupted: InterruptedCalls | None = None,
) -> RunResult[Output] | DeferredRunResult:
    try:
        messages, history = _run_messages(value, interrupted)
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


def _run_messages(
    value: AgentRunResult[Any],
    interrupted: InterruptedCalls | None,
) -> tuple[tuple[AgentMessage, ...], tuple[AgentMessage, ...]]:
    """Return the run delta and the history. The closing request of interrupted calls starts the delta."""
    messages = tuple(message_from_pydantic(message) for message in value.new_messages())
    if interrupted is None:
        return messages, tuple(message_from_pydantic(message) for message in value.all_messages())

    messages = (interrupted.closing, *messages)

    return messages, (*interrupted.history, *messages)
