import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Sequence

from pydantic import JsonValue, TypeAdapter
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from ovid_core import AgentDefinition, AgentRunPolicy
from ovid_core.messages import AgentMessage, MessagePart, TextPart, ToolCallPart, UserPromptPart
from ovid_core.messages import ToolReturnPart as OvidToolReturnPart
from ovid_core.persistence import ConversationStore
from ovid_core.routing import ModelRef
from ovid_core.runtime import ConversationId
from ovid_core.server import (
    AgentRegistration,
    AuthorizationResult,
    RequestContext,
    ServerConfig,
    StdioCancelRequest,
    StdioRunRequest,
    create_stdio_server,
)
from ovid_core.tools import ToolExecutionContext, ToolResult
from tests.server.server_helpers import dependencies
from tests.support.agent_consumer import AgentDependencies, ConsumerToolset
from tests.support.agent_helpers import agent_factory
from tests.support.deferred_consumer import RecordingTool, ValueArguments


type StreamFunction = Callable[[list[ModelMessage], AgentInfo], AsyncIterator[str | DeltaToolCalls]]
type StdioStep = StdioRunRequest | StdioCancelRequest | asyncio.Event | str | Callable[[], None]

_FRAME_ADAPTER = TypeAdapter(dict[str, JsonValue])


class WaitingTool(RecordingTool):
    id = 'wait'

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def execute(self, context: ToolExecutionContext[AgentDependencies], arguments: ValueArguments) -> ToolResult:
        del context, arguments
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError('unreachable')


class AppendOnlyStore:
    def __init__(self) -> None:
        self.messages: tuple[AgentMessage, ...] = ()

    async def load(self, conversation_id: ConversationId) -> tuple[AgentMessage, ...]:
        del conversation_id
        return self.messages

    async def append(self, conversation_id: ConversationId, messages: tuple[AgentMessage, ...]) -> None:
        del conversation_id
        self.messages += messages


def tool_call(name: str, call_id: str) -> DeltaToolCalls:
    return {0: DeltaToolCall(name=name, json_args=json.dumps({'value': 1}), tool_call_id=call_id)}


def returned_calls(messages: Sequence[ModelMessage]) -> int:
    return sum(isinstance(part, ToolReturnPart) for message in messages for part in message.parts)


def unanswered_calls(messages: Sequence[ModelMessage]) -> set[str]:
    calls = {
        call.tool_call_id for message in messages if isinstance(message, ModelResponse) for call in message.tool_calls
    }
    answered = {
        part.tool_call_id
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }

    return calls - answered


async def registration(
    stream: StreamFunction,
    tools: Sequence[RecordingTool] = (),
    *,
    policy: AgentRunPolicy = AgentRunPolicy(),
) -> AgentRegistration[AgentDependencies, str]:
    definition = AgentDefinition[AgentDependencies, str](
        model=ModelRef(name='primary'),
        deps_type=AgentDependencies,
        output_type=str,
        toolsets=(ConsumerToolset(tools),),
        policy=policy,
    )
    agent = await agent_factory({'primary': FunctionModel(stream_function=stream, model_name='partial')}).build(
        definition
    )

    return AgentRegistration(id='writer', description='Write with tools.', agent=agent, dependencies=dependencies)


def shape(messages: Sequence[AgentMessage]) -> list[tuple[str, str, tuple[str, ...]]]:
    """Summarize stored messages as role, state and part labels."""
    return [(message.role, message.state, tuple(_label(part) for part in message.parts)) for message in messages]


def boundary(text: str) -> tuple[str, str, tuple[str, ...]]:
    return ('response', 'interrupted', (f'text:[Run ended: {text}]',))


def failure(frames: list[dict[str, JsonValue]], request_id: str) -> tuple[list[JsonValue], JsonValue]:
    """Return the `persisted` values of the run_failed events and the error code of the request."""
    own = [frame for frame in frames if frame['request_id'] == request_id]
    persisted = [
        event.get('persisted')
        for frame in own
        if isinstance(event := frame.get('event'), dict) and event['kind'] == 'run_failed'
    ]
    error = own[-1]['error']
    assert isinstance(error, dict)

    return persisted, error['code']


def _label(part: MessagePart) -> str:
    if isinstance(part, ToolCallPart):
        return f'call:{part.tool_call_id}'
    if isinstance(part, OvidToolReturnPart):
        return f'return:{part.tool_call_id}:{part.outcome}'
    if isinstance(part, TextPart | UserPromptPart):
        return f'{part.kind}:{part.content}'

    return type(part).__name__


async def stdio_session(
    agent: AgentRegistration[AgentDependencies, str],
    store: ConversationStore | None,
    *steps: StdioStep,
    config: ServerConfig = ServerConfig(),
) -> list[dict[str, JsonValue]]:
    """Feed the steps to a stdio server, then close stdin.

    An event step waits for the event. A string step waits for the terminal frame of that request.
    A callable step runs before the next step.
    """

    async def authorize(_: RequestContext, __: str) -> AuthorizationResult:
        return AuthorizationResult(allowed=True, principal='stdio-user')

    server = create_stdio_server(agents=(agent,), authorize=authorize, store=store, config=config)
    pending = iter(steps)
    terminal: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
    frames: list[dict[str, JsonValue]] = []

    async def read(_: int) -> bytes:
        for step in pending:
            if isinstance(step, StdioRunRequest | StdioCancelRequest):
                return f'{step.model_dump_json()}\n'.encode()
            if isinstance(step, str):
                await terminal[step].wait()
            elif isinstance(step, asyncio.Event):
                await step.wait()
            else:
                step()
        return b''

    async def write(payload: bytes) -> None:
        frame = _FRAME_ADAPTER.validate_json(payload)
        frames.append(frame)
        if frame['type'] in ('run_result', 'error'):
            terminal[str(frame['request_id'])].set()

    async with asyncio.timeout(5):
        await server._serve(reader=read, writer=write)

    return frames
