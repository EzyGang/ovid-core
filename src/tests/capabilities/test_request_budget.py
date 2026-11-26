import inspect
from typing import Any, get_type_hints

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ovid_core import AgentDefinition, AgentRunPolicy, AgentUsageLimits
from ovid_core.capabilities import REQUEST_BUDGET_WRAP_UP_PROMPT, RequestBudgetCapability
from ovid_core.routing import ModelRef
from tests.support.agent_consumer import AddTool, AgentDependencies, ConsumerToolset
from tests.support.agent_helpers import agent_factory


WARNING = 'Configured run limits are approaching.'


async def test_request_budget_warns_near_limit_and_wraps_up_last_request() -> None:
    requests: list[tuple[str, int, str | None]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        text = '\n'.join(str(part.content) for part in request.parts if hasattr(part, 'content'))
        tool_choice = (info.model_settings or {}).get('tool_choice')
        requests.append((text, len(info.function_tools), tool_choice))
        if tool_choice == 'none':
            return ModelResponse(parts=(TextPart('Done: two additions. Remaining: none. Next: report.'),))

        return ModelResponse(parts=(ToolCallPart('add', {'left': 1, 'right': 2}),))

    definition = AgentDefinition[AgentDependencies, str](
        model=ModelRef(name='primary'),
        deps_type=AgentDependencies,
        output_type=str,
        toolsets=(ConsumerToolset((AddTool(),)),),
        capabilities=(RequestBudgetCapability[AgentDependencies](requests=3, warning_threshold=0.3),),
        policy=AgentRunPolicy(limits=AgentUsageLimits(requests=3)),
    )
    agent = await agent_factory({'primary': FunctionModel(respond)}).build(definition)

    result = await agent.run('Add numbers.', deps=AgentDependencies(prefix='budget'))

    assert result.output == 'Done: two additions. Remaining: none. Next: report.'
    assert result.usage.request_count == 3
    first, second, last = requests
    assert WARNING not in first[0]
    assert REQUEST_BUDGET_WRAP_UP_PROMPT not in first[0]
    assert first[2] is None
    assert WARNING in second[0]
    assert REQUEST_BUDGET_WRAP_UP_PROMPT not in second[0]
    assert second[2] is None
    assert REQUEST_BUDGET_WRAP_UP_PROMPT in last[0]
    assert last[1] == 1
    assert last[2] == 'none'


@pytest.mark.parametrize(
    'values',
    [
        {'requests': 0},
        {'requests': 3, 'warning_threshold': 0},
        {'requests': 3, 'warning_threshold': 1.5},
        {'requests': 3, 'wrap_up_prompt': '  '},
    ],
)
def test_request_budget_rejects_invalid_values(values: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match='request budget'):
        RequestBudgetCapability[None](**values)


def test_request_budget_defaults_and_public_signature_use_no_pydantic_ai_type() -> None:
    capability = RequestBudgetCapability[None](requests=50)
    hints = get_type_hints(RequestBudgetCapability)
    parameters = inspect.signature(RequestBudgetCapability).parameters

    assert capability.id == 'request_budget'
    assert capability.warning_threshold == 0.9
    assert capability.wrap_up_prompt == REQUEST_BUDGET_WRAP_UP_PROMPT
    assert list(parameters) == ['description', 'requirements', 'requests', 'warning_threshold', 'wrap_up_prompt']
    assert all('pydantic_ai' not in str(hint) for hint in hints.values())
