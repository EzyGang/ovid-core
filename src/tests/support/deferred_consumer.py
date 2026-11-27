import json
from collections.abc import AsyncIterator, Sequence

from pydantic import JsonValue
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from ovid_core import AgentDefinition, AgentRunPolicy, BaseModel, OvidAgent, ToolExecutionError
from ovid_core.capabilities import BaseCapability
from ovid_core.errors import ToolApprovalRequired, ToolDeferred
from ovid_core.hooks import BaseToolHook, ToolCall
from ovid_core.messages import AgentMessage, ToolReturnPart
from ovid_core.routing import ModelRef
from ovid_core.server import AgentRegistration
from ovid_core.tools import BaseTool, ToolApproval, ToolExecutionContext, ToolResult
from tests.server.server_helpers import dependencies
from tests.support.agent_consumer import AgentDependencies, ConsumerToolset
from tests.support.agent_helpers import agent_factory


class ValueArguments(BaseModel):
    value: int


class RecordingTool(BaseTool[AgentDependencies, ValueArguments, ToolResult]):
    description = 'Handle a value.'
    args_type = ValueArguments
    result_type = ToolResult

    def __init__(self) -> None:
        self.calls: list[tuple[int, bool]] = []

    async def execute(
        self,
        context: ToolExecutionContext[AgentDependencies],
        arguments: ValueArguments,
    ) -> ToolResult:
        self.calls.append((arguments.value, context.approved))
        self.defer(context)

        return ToolResult(content=f'{self.id} {arguments.value}')

    def defer(self, context: ToolExecutionContext[AgentDependencies]) -> None:
        del context


class SafeTool(RecordingTool):
    id = 'safe'


class DangerTool(RecordingTool):
    id = 'danger'
    approval = ToolApproval(required=True, reason='Deletes values', metadata={'risk': 'high'})


class CheckTool(RecordingTool):
    id = 'check'

    def defer(self, context: ToolExecutionContext[AgentDependencies]) -> None:
        if not context.approved:
            raise ToolApprovalRequired(metadata={'limit': 10})


class ExternalTool(RecordingTool):
    id = 'external'

    def defer(self, context: ToolExecutionContext[AgentDependencies]) -> None:
        del context
        raise ToolDeferred(metadata={'job': 7})


class QuestionTool(RecordingTool):
    id = 'ask'

    def defer(self, context: ToolExecutionContext[AgentDependencies]) -> None:
        del context
        raise ToolDeferred(kind='question')


class EventHook(BaseToolHook[AgentDependencies]):
    def __init__(self) -> None:
        self.events: list[str] = []

    async def before_tool(self, context: ToolExecutionContext[AgentDependencies], call: ToolCall) -> None:
        self.events.append(f'before:{call.tool_id}:{context.approved}')

    async def after_tool(
        self,
        context: ToolExecutionContext[AgentDependencies],
        call: ToolCall,
        result: ToolResult,
    ) -> None:
        del result
        self.events.append(f'after:{call.tool_id}:{context.approved}')

    async def on_tool_error(
        self,
        context: ToolExecutionContext[AgentDependencies],
        call: ToolCall,
        error: ToolExecutionError,
    ) -> None:
        del context, error
        self.events.append(f'error:{call.tool_id}')


class ScriptedModel:
    """Call the given tools when the conversation has no model response yet, then answer with text."""

    def __init__(self, *calls: tuple[str, dict[str, JsonValue], str]) -> None:
        self.calls = calls
        self.requests: list[list[ModelMessage]] = []

    def _calls_tools(self, messages: list[ModelMessage]) -> bool:
        self.requests.append(messages)

        return bool(self.calls) and not any(isinstance(message, ModelResponse) for message in messages)

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        if self._calls_tools(messages):
            return ModelResponse(parts=[ToolCallPart(name, args, call_id) for name, args, call_id in self.calls])

        return ModelResponse(parts=[TextPart('done')])

    async def stream(self, messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del info
        if self._calls_tools(messages):
            yield {
                index: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=call_id)
                for index, (name, args, call_id) in enumerate(self.calls)
            }
            return

        yield 'done'

    def model(self) -> FunctionModel:
        return FunctionModel(function=self.respond, stream_function=self.stream, model_name='scripted')


async def deferred_agent(
    model: ScriptedModel,
    tools: Sequence[RecordingTool],
    *,
    hooks: tuple[BaseToolHook[AgentDependencies], ...] = (),
    policy: AgentRunPolicy = AgentRunPolicy(),
    tool_approval: ToolApproval | None = None,
    capabilities: tuple[BaseCapability[AgentDependencies], ...] = (),
) -> OvidAgent[AgentDependencies, str]:
    definition = AgentDefinition[AgentDependencies, str](
        model=ModelRef(name='primary'),
        deps_type=AgentDependencies,
        output_type=str,
        toolsets=(ConsumerToolset(tools),),
        capabilities=capabilities,
        hooks=hooks,
        policy=policy,
        tool_approval=tool_approval,
    )

    return await agent_factory({'primary': model.model()}).build(definition)


async def deferred_registration(
    model: ScriptedModel,
    tools: Sequence[RecordingTool],
) -> AgentRegistration[AgentDependencies, str]:
    return AgentRegistration(
        id='writer',
        description='Write with tools.',
        agent=await deferred_agent(model, tools),
        dependencies=dependencies,
    )


def tool_returns(messages: Sequence[AgentMessage]) -> list[tuple[str, str, JsonValue]]:
    return [
        (part.tool_call_id, part.outcome, part.content)
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
