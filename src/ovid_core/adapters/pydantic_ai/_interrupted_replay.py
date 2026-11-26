from collections.abc import Awaitable, Callable

from pydantic_ai import RunContext, ToolReturn
from pydantic_ai.capabilities import AbstractCapability, ValidatedToolArgs
from pydantic_ai.exceptions import ModelRetry, SkipToolExecution, ToolFailed
from pydantic_ai.messages import ToolCallPart as PydanticToolCallPart
from pydantic_ai.tools import ToolDefinition, ToolDenied

from ovid_core.adapters.pydantic_ai.effects import resolve_effect
from ovid_core.errors import NothingToResumeError
from ovid_core.messages.models import AgentMessage, RetryPromptPart, ToolReturnPart, pending_tool_calls
from ovid_core.recovery import unknown_effect_text
from ovid_core.runtime.results import is_boundary


class InterruptedRunReplay[Deps](AbstractCapability[Deps]):
    """Continue an interrupted run from its stored history, and guard the calls that the run executes again.

    The history loses one trailing boundary response, because Pydantic AI returns a final text response as the output.
    When the last response has open calls, the history ends at that response, so the run executes all its calls again.
    A call with a stored answer never runs again. It gives back that answer with its kind when Pydantic AI can
    reproduce it, and the UNKNOWN result otherwise. An open call runs only when its current tool is safe to replay.
    Another open call returns the UNKNOWN result.
    """

    def __init__(self, messages: tuple[AgentMessage, ...]) -> None:
        self.id = 'ovid_core.interrupted_run_replay'
        history = messages[:-1] if messages and is_boundary(messages[-1]) else messages
        pending = pending_tool_calls(history)
        if not history or (history[-1].role == 'response' and not pending):
            raise NothingToResumeError('The conversation has no interrupted run to resume')

        self._open = {call.tool_call_id for call in pending}
        self.history = history
        self._stored: dict[str, ToolReturnPart | RetryPromptPart] = {}
        if not self._open:
            return

        last = max(index for index, message in enumerate(history) if message.role == 'response')
        self.history = history[: last + 1]
        self._stored = {
            part.tool_call_id: part
            for message in history[last + 1 :]
            for part in message.parts
            if isinstance(part, ToolReturnPart | RetryPromptPart)
        }

    async def wrap_tool_execute[Result](
        self,
        ctx: RunContext[Deps],
        *,
        call: PydanticToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        handler: Callable[[ValidatedToolArgs], Awaitable[Result]],
    ) -> Result:
        del ctx
        stored = self._stored.get(call.tool_call_id)
        if stored is not None:
            raise _stored_answer(stored)
        if call.tool_call_id in self._open and resolve_effect(tool_def).replay != 'safe':
            raise SkipToolExecution(unknown_effect_text(call.tool_name))

        return await handler(args)


def _stored_answer(part: ToolReturnPart | RetryPromptPart) -> Exception:
    """Return the exception that gives back a stored answer with its kind, outcome, and metadata.

    Pydantic AI cannot give a skipped call the outcome `interrupted`, file content, or metadata on a non-success result.
    Such an answer becomes the UNKNOWN result.
    """
    if isinstance(part, RetryPromptPart):
        return ModelRetry(part.content)

    exact = part.content_file_digests is None and (part.outcome == 'success' or part.metadata is None)
    text = part.content if isinstance(part.content, str) else None
    match part.outcome:
        case 'success' if exact:
            return SkipToolExecution(ToolReturn(return_value=part.content, metadata=part.metadata))
        case 'retried' if exact and text is not None:
            return ModelRetry(text)
        case 'failed' if exact and text is not None:
            return ToolFailed(text)
        case 'denied' if exact and text is not None:
            return SkipToolExecution(ToolDenied(text))

    return SkipToolExecution(unknown_effect_text(part.tool_name))
