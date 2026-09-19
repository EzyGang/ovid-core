from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ovid_core.agents import AgentDefinition
from ovid_core.capabilities import HistoryProcessorCapability
from ovid_core.messages import AgentMessage, UserPromptPart
from ovid_core.routing import ModelRef
from tests.support.agent_helpers import agent_factory


async def test_history_processor_replaces_active_history_between_model_requests() -> None:
    async def compact(messages: tuple[AgentMessage, ...]) -> tuple[AgentMessage, ...]:
        if len(messages) < 3:
            return messages
        return (
            AgentMessage(role='request', parts=(UserPromptPart(content='<context-summary>summary</context-summary>'),)),
            messages[-1],
        )

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        return ModelResponse(parts=(TextPart(f'{len(messages)} messages'),))

    definition = AgentDefinition[None, str](
        model=ModelRef(name='primary'),
        deps_type=type(None),
        output_type=str,
        capabilities=(HistoryProcessorCapability(id='history', processor=compact),),
    )
    agent = await agent_factory({'primary': FunctionModel(respond)}).build(definition)
    first = await agent.run('First.', deps=None)
    second = await agent.run('Second.', deps=None, messages=first.history)

    assert first.output == '1 messages'
    assert second.output == '1 messages'
    assert len(second.messages) == 2
    assert len(second.history) == 3
    summary = second.history[0].parts[0]
    assert isinstance(summary, UserPromptPart)
    assert summary.content.startswith('<context-summary>')
