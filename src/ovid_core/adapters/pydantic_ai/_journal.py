import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, AgentNode, CapabilityOrdering, NodeResult, ValidatedToolArgs
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred, ModelRetry, SkipToolExecution
from pydantic_ai.exceptions import ToolRetryError as PydanticToolRetryError
from pydantic_ai.messages import ModelMessage, ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import ToolDefinition

from ovid_core.adapters.pydantic_ai._usage_tracking import UsageTrackingCapability
from ovid_core.adapters.pydantic_ai.effects import resolve_effect
from ovid_core.adapters.pydantic_ai.messages import message_from_pydantic
from ovid_core.adapters.pydantic_ai.tools import PydanticAIToolsetAdapter
from ovid_core.journal import (
    JournalEntry,
    RunStartedEntry,
    StepCommittedEntry,
    ToolIntentEntry,
    ToolSettledEntry,
    ToolSettledOutcome,
    ToolSettledStatus,
)
from ovid_core.messages.models import AgentMessage
from ovid_core.persistence import RunJournal
from ovid_core.runtime.identifiers import RunId


class TranscriptJournalCapability[Deps](AbstractCapability[Deps]):
    """Write the steps and tool intents of one run to its journal before the run continues.

    The run enters the journal with its first entry, so a run that adds no message leaves no entry.
    """

    def __init__(self, *, journal: RunJournal, run_id: RunId, closing: AgentMessage | None) -> None:
        self.id = 'ovid_core.transcript_journal'
        self._journal = journal
        self._run_id = run_id
        self._closing = () if closing is None else (closing,)
        self._start: RunStartedEntry | None = None
        self._held: dict[int, ModelMessage] = {}

    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(position='outermost', wrapped_by=(UsageTrackingCapability,))

    async def before_run(self, ctx: RunContext[Deps]) -> None:
        del ctx
        self._start = RunStartedEntry(
            run_id=self._run_id, at=datetime.now(UTC), client_request_id=self._journal.client_request_id
        )

    async def prepare_model_request(
        self,
        ctx: RunContext[Deps],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        """Commit the request after Pydantic AI recorded its instruction and tool state, before the model call."""
        await self._commit(ctx.messages)

        return request_context

    async def after_node_run(
        self,
        ctx: RunContext[Deps],
        *,
        node: AgentNode[Deps],
        result: NodeResult[Deps],
    ) -> NodeResult[Deps]:
        del node
        await self._commit(ctx.messages)

        return result

    async def wrap_tool_execute[Result](
        self,
        ctx: RunContext[Deps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        handler: Callable[[ValidatedToolArgs], Awaitable[Result]],
    ) -> Result:
        tools = ctx.tool_manager.tools if ctx.tool_manager is not None else None
        tool = tools.get(call.tool_name) if tools is not None else None
        toolset = tool.toolset if tool is not None else None
        intent = ToolIntentEntry(
            run_id=self._run_id,
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            arguments=call.args_as_dict(),
            effect=resolve_effect(tool_def),
            idempotency_key=(
                toolset.idempotency_key(call.tool_name, args) if isinstance(toolset, PydanticAIToolsetAdapter) else None
            ),
            started_at=datetime.now(UTC),
        )
        await self._append(intent)
        try:
            result = await handler(args)
        except BaseException as error:
            await self._append(self._settled(call, error))
            raise
        await self._append(self._settled(call, None))

        return result

    async def _commit(self, messages: Sequence[ModelMessage]) -> None:
        """Commit the messages of this run after the last committed one. The first step starts with the closing.

        The search runs from the end and stops at a committed message object, or at a copy with the content of the
        last committed message, because a history processor can replace the messages with copies and remove older
        ones.
        """
        last = self._journal.messages[-1] if self._journal.messages else None
        pending: list[tuple[ModelMessage, AgentMessage]] = []
        for upstream in reversed([message for message in messages if message.run_id == str(self._run_id)]):
            if id(upstream) in self._held:
                break
            message = message_from_pydantic(upstream)
            if message == last:
                break
            pending.append((upstream, message))
        if not pending:
            return

        pending.reverse()
        step = (*self._closing, *(message for _, message in pending))
        await self._append(StepCommittedEntry(run_id=self._run_id, messages=step))
        self._held.update((id(upstream), upstream) for upstream, _ in pending)
        self._closing = ()

    async def _append(self, entry: JournalEntry) -> None:
        if self._start is None:
            await self._journal.append(entry)
            return

        await self._journal.append(self._start, entry)
        self._start = None

    def _settled(self, call: ToolCallPart, error: BaseException | None) -> ToolSettledEntry:
        """Classify how a tool call ended. A deferral is not a result, and a skipped call has a result."""
        status: ToolSettledStatus = 'failed'
        outcome: ToolSettledOutcome | None = 'failed'
        match error:
            case None | SkipToolExecution():
                status, outcome = 'completed', 'success'
            case CallDeferred() | ApprovalRequired():
                status, outcome = 'deferred', None
            case PydanticToolRetryError() | ModelRetry():
                outcome = 'retried'
            case asyncio.CancelledError():
                outcome = 'interrupted'

        return ToolSettledEntry(
            run_id=self._run_id,
            tool_call_id=call.tool_call_id,
            status=status,
            outcome=outcome,
            ended_at=datetime.now(UTC),
        )
