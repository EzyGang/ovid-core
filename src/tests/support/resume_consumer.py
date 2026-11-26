from datetime import UTC, datetime

from pydantic import JsonValue

from ovid_core import InMemoryConversationStore
from ovid_core.journal import RunStartedEntry, StepCommittedEntry, ToolIntentEntry
from ovid_core.messages import AgentMessage, ToolCallPart, ToolReturnPart, UserPromptPart
from ovid_core.runtime import ConversationId, RunId
from ovid_core.server import AgentRunRequest, StdioRunRequest
from ovid_core.tools import ToolEffect, ToolExecutionContext
from ovid_core.usage import RequestUsage
from tests.support.agent_consumer import AgentDependencies
from tests.support.deferred_consumer import RecordingTool


UNKNOWN = (
    '[Recovery: {} may have executed before the process stopped; its effect is UNKNOWN. '
    'Inspect current state before retrying.]'
)


class LookTool(RecordingTool):
    id = 'look'
    effect = ToolEffect(kind='read', replay='safe')


class UnsafeLookTool(RecordingTool):
    id = 'look'
    effect = ToolEffect(kind='read')


class WriteTool(RecordingTool):
    id = 'write'
    effect = ToolEffect(kind='workspace_write')


class FlakyLookTool(LookTool):
    """Fail the first call, then succeed."""

    def defer(self, context: ToolExecutionContext[AgentDependencies]) -> None:
        del context
        if len(self.calls) == 1:
            raise RuntimeError('look failed')


async def crashed_store(conversation_id: ConversationId, *calls: tuple[str, ToolEffect]) -> InMemoryConversationStore:
    """Store a run that stopped while each given call ran: the response and the intents exist, no settle record."""
    store = InMemoryConversationStore()
    run_id = RunId.new()
    now = datetime.now(UTC)
    request = AgentMessage(
        role='request',
        parts=(UserPromptPart(content='Go.'),),
        run_id=run_id,
        conversation_id=conversation_id,
        timestamp=now,
    )
    response = AgentMessage(
        role='response',
        parts=tuple(
            ToolCallPart(tool_name=name, arguments={'value': index}, tool_call_id=f'c{index}')
            for index, (name, _) in enumerate(calls, start=1)
        ),
        run_id=run_id,
        conversation_id=conversation_id,
        timestamp=now,
        request_usage=RequestUsage(),
    )
    intents = tuple(
        ToolIntentEntry(
            run_id=run_id,
            tool_call_id=f'c{index}',
            tool_name=name,
            arguments={'value': index},
            effect=effect,
            started_at=now,
        )
        for index, (name, effect) in enumerate(calls, start=1)
    )
    entries = (RunStartedEntry(run_id=run_id, at=now), StepCommittedEntry(run_id=run_id, messages=(request, response)))
    await store.append_entries(conversation_id, (*entries, *intents))

    return store


def resume_request(request_id: str, conversation_id: ConversationId) -> StdioRunRequest:
    return StdioRunRequest(
        request_id=request_id,
        agent_id='writer',
        request=AgentRunRequest(resume=True, conversation_id=conversation_id),
    )


def prompt_request(request_id: str, conversation_id: ConversationId, prompt: str) -> StdioRunRequest:
    return StdioRunRequest(
        request_id=request_id,
        agent_id='writer',
        request=AgentRunRequest(prompt=prompt, conversation_id=conversation_id),
    )


def outcomes(frames: list[dict[str, JsonValue]]) -> list[JsonValue]:
    """Return the output of each run result and the code of each error, in order."""
    return [
        payload.get('output', payload.get('code'))
        for frame in frames
        if isinstance(payload := frame.get('result', frame.get('error')), dict)
    ]


def returns(messages: tuple[AgentMessage, ...]) -> list[tuple[str, JsonValue]]:
    return [
        (part.tool_call_id, part.content)
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
