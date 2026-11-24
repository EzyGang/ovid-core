from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import INTERRUPTED_TOOL_RETURN_CONTENT
from pydantic_ai.tools import DeferredToolRequests as PydanticDeferredToolRequests
from pydantic_ai.tools import DeferredToolResults, ToolApproved, ToolDenied

from ovid_core.messages.models import AgentMessage, ToolReturnPart, pending_tool_calls
from ovid_core.runtime.identifiers import ConversationId
from ovid_core.tools.deferred import (
    DeferredToolRequest,
    DeferredToolRequests,
    DeferredToolResume,
    ToolCallApproved,
    ToolCallDenied,
    ToolCallReturned,
)
from ovid_core.tools.models import ToolApproval


@dataclass(frozen=True, slots=True)
class InterruptedCalls:
    """The history before a new prompt and the request that closes its pending calls as interrupted.

    The request has no run ID because Pydantic AI rejects a history that contains the ID of the current run.
    """

    history: tuple[AgentMessage, ...]
    closing: AgentMessage


def interrupted_calls(messages: tuple[AgentMessage, ...], conversation_id: ConversationId) -> InterruptedCalls | None:
    pending = pending_tool_calls(messages)
    if not pending:
        return None

    closing = AgentMessage(
        role='request',
        parts=tuple(
            ToolReturnPart(
                tool_name=call.tool_name,
                content=INTERRUPTED_TOOL_RETURN_CONTENT,
                tool_call_id=call.tool_call_id,
                outcome='interrupted',
            )
            for call in pending
        ),
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
    )

    return InterruptedCalls(history=messages, closing=closing)


class StaticApprovalRecorder[Deps](AbstractCapability[Deps]):
    """Record the Ovid approval of each call that waits because its tool definition requires approval."""

    def __init__(self) -> None:
        self.id = 'ovid_core.static_approvals'
        self.approvals: dict[str, ToolApproval] = {}

    async def handle_deferred_tool_calls(
        self,
        ctx: RunContext[Deps],
        *,
        requests: PydanticDeferredToolRequests,
    ) -> DeferredToolResults | None:
        for call in requests.approvals:
            tool_def = ctx.tool_manager.get_tool_def(call.tool_name) if ctx.tool_manager is not None else None
            metadata = tool_def.metadata if tool_def is not None and tool_def.kind == 'unapproved' else None
            if metadata is not None and 'ovid_approval' in metadata:
                self.approvals[call.tool_call_id] = ToolApproval.model_validate(metadata['ovid_approval'])

        return None


def deferred_requests_from_pydantic(
    requests: PydanticDeferredToolRequests,
    approvals: Mapping[str, ToolApproval],
) -> DeferredToolRequests:
    return DeferredToolRequests(
        calls=tuple(
            DeferredToolRequest(
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                arguments=call.args_as_dict(),
                kind=requests.kind(call.tool_call_id),
                approval=approvals.get(call.tool_call_id),
                metadata=requests.metadata.get(call.tool_call_id),
            )
            for call in (*requests.approvals, *requests.calls)
        )
    )


def deferred_results_to_pydantic(resume: DeferredToolResume) -> DeferredToolResults:
    results = DeferredToolResults()
    for call_id, answer in resume.answers.items():
        if isinstance(answer, ToolCallApproved):
            results.approvals[call_id] = ToolApproved()
        elif isinstance(answer, ToolCallDenied):
            results.approvals[call_id] = ToolDenied(answer.message)
        elif isinstance(answer, ToolCallReturned):
            results.calls[call_id] = answer.value
        else:
            results.calls[call_id] = ToolFailed(answer.message)

    return results
