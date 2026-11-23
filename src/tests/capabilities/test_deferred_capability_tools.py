from pydantic_ai.messages import LoadCapabilityCallPart, ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ovid_core import AgentDefinition, MessageCodec
from ovid_core.capabilities import BaseCapability, CapabilityContributions
from ovid_core.messages import ToolReturnPart
from ovid_core.routing import ModelRef
from tests.support.agent_consumer import AddTool, AgentDependencies
from tests.support.agent_helpers import agent_factory


async def test_deferred_capability_tool_is_refused_until_the_model_loads_the_capability() -> None:
    visible_tools: list[set[str]] = []
    responses = iter(
        (
            ModelResponse(parts=(ToolCallPart('add', {'left': 1, 'right': 2}, 'before-load'),)),
            ModelResponse(parts=(LoadCapabilityCallPart(args={'id': 'arithmetic'}, tool_call_id='load'),)),
            ModelResponse(parts=(ToolCallPart('add', {'left': 1, 'right': 2}, 'after-load'),)),
            ModelResponse(parts=(TextPart('loaded'),)),
            ModelResponse(parts=(ToolCallPart('add', {'left': 2, 'right': 2}, 'continued'),)),
            ModelResponse(parts=(TextPart('continued'),)),
        )
    )

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        visible_tools.append({tool.name for tool in info.function_tools})
        return next(responses)

    capability = BaseCapability[AgentDependencies](
        id='arithmetic',
        description='Integer arithmetic',
        defer_loading=True,
        contributions=CapabilityContributions(instructions=('Add with the add tool.',), tools=(AddTool(),)),
    )
    definition = AgentDefinition[AgentDependencies, str](
        model=ModelRef(name='primary'),
        deps_type=AgentDependencies,
        output_type=str,
        capabilities=(capability,),
    )
    agent = await agent_factory({'primary': FunctionModel(respond)}).build(definition)
    deps = AgentDependencies(prefix='deferred')

    first = await agent.run('Add.', deps=deps)
    codec = MessageCodec()
    stored_history = tuple(codec.decode(codec.encode(message)) for message in first.history)
    second = await agent.run('Add again.', deps=deps, messages=stored_history)

    returns = [part for message in first.messages for part in message.parts if isinstance(part, ToolReturnPart)]
    assert (first.output, second.output) == ('loaded', 'continued')
    assert visible_tools == [
        {'load_capability'},
        {'load_capability'},
        {'load_capability', 'add'},
        {'load_capability', 'add'},
        {'load_capability', 'add'},
        {'load_capability', 'add'},
    ]
    assert [(part.tool_call_id, part.outcome) for part in returns] == [
        ('before-load', 'retried'),
        ('after-load', 'success'),
    ]
    assert "belongs to capability 'arithmetic'" in str(returns[0].content)
    assert deps.events == ['deferred:add', 'deferred:add']
