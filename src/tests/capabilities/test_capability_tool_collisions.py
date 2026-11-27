from typing import cast

import pytest
from pydantic_ai import RunContext
from pydantic_ai.capabilities import Toolset
from pydantic_ai.exceptions import UserError
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_ai.usage import RunUsage
from starlette.applications import Starlette

from ovid_core.adapters.pydantic_ai import pydantic_ai_capability
from ovid_core.adapters.pydantic_ai._tool_collisions import OvidToolCollisionsCapability, _CollisionCheckedToolset
from ovid_core.agents import AgentDefinition
from ovid_core.capabilities.base import BaseCapability, CapabilityContributions
from ovid_core.errors import ExtensionCollisionError
from ovid_core.routing.models import ModelRef
from ovid_core.server import AgentRegistration, create_agent_app
from ovid_core.tools.base import BaseToolset
from tests.server.server_helpers import allow, dependencies, server_client
from tests.support.agent_consumer import AddTool, AgentDependencies, ConsumerToolset
from tests.support.agent_helpers import agent_factory


def add(left: int, right: int) -> int:
    return left + right


class FlakyToolset(FunctionToolset[None]):
    failed = False

    async def get_tools(self, ctx: RunContext[None]) -> dict[str, ToolsetTool[None]]:
        if not self.failed:
            self.failed = True
            raise UserError('flaky listing')
        return await super().get_tools(ctx)


def _ovid(capability_id: str, *, toolset: bool = False) -> BaseCapability[AgentDependencies]:
    contributions = CapabilityContributions[AgentDependencies](
        tools=() if toolset else (AddTool(),),
        toolsets=(ConsumerToolset((AddTool(),)),) if toolset else (),
    )
    return BaseCapability[AgentDependencies](id=capability_id, contributions=contributions)


def _wrapped(capability_id: str) -> BaseCapability[AgentDependencies]:
    return pydantic_ai_capability(Toolset[AgentDependencies](FunctionToolset([add]), id=capability_id))


def _definition(
    *capabilities: BaseCapability[AgentDependencies],
    toolsets: tuple[BaseToolset[AgentDependencies], ...] = (),
) -> AgentDefinition[AgentDependencies, str]:
    return AgentDefinition[AgentDependencies, str](
        model=ModelRef(name='primary'),
        deps_type=AgentDependencies,
        output_type=str,
        capabilities=capabilities,
        toolsets=toolsets,
    )


@pytest.mark.parametrize(
    ('definition', 'sources'),
    [
        (_definition(_ovid('math'), _wrapped('adder')), "capability 'math' and capability 'adder'"),
        (_definition(_ovid('one'), _ovid('two', toolset=True)), "capability 'one' and capability 'two'"),
        (_definition(_wrapped('left'), _wrapped('right')), "capability 'left' and capability 'right'"),
        (
            _definition(_wrapped('adder'), toolsets=(ConsumerToolset((AddTool(),)),)),
            "toolset 'consumer' and capability 'adder'",
        ),
    ],
)
async def test_tool_name_clash_raises_extension_collision_with_both_sources(
    definition: AgentDefinition[AgentDependencies, str],
    sources: str,
) -> None:
    agent = await agent_factory({'primary': TestModel()}).build(definition)

    with pytest.raises(ExtensionCollisionError) as raised:
        await agent.run('Add.', deps=AgentDependencies(prefix='clash'))

    assert str(raised.value) == f"Tool name 'add' comes from {sources}. Rename or prefix one of the tools."


async def test_plain_toolsets_are_named_by_label_and_other_user_errors_propagate() -> None:
    context = RunContext[None](deps=None, model=TestModel(), usage=RunUsage())
    clashing = _CollisionCheckedToolset[None]((FunctionToolset([add]), FunctionToolset([add])))

    with pytest.raises(ExtensionCollisionError, match='comes from FunctionToolset and FunctionToolset'):
        await clashing.get_tools(context)
    with pytest.raises(UserError, match='flaky listing'):
        await _CollisionCheckedToolset[None]((FlakyToolset([add]),)).get_tools(context)


def test_collision_capability_leaves_toolsets_without_a_combination_unchanged() -> None:
    capability = OvidToolCollisionsCapability[None]()
    leaf = FunctionToolset[None]([add])

    assert capability.get_wrapper_toolset(leaf) is None
    assert capability.get_wrapper_toolset(leaf.prefixed('math')) is None


async def test_server_reports_a_tool_name_clash_with_its_message() -> None:
    agent = await agent_factory({'primary': TestModel()}).build(_definition(_ovid('math'), _wrapped('adder')))
    registration = AgentRegistration(id='writer', description='Add.', agent=agent, dependencies=dependencies)
    app = cast(Starlette, create_agent_app(agents=(registration,), authorize=allow))

    async with server_client(app) as client:
        failed = await client.post(
            '/agents/writer/events', headers={'Authorization': 'Bearer allowed'}, json={'prompt': 'Add.'}
        )

    assert '"code":"extension_collision"' in failed.text
    assert "comes from capability 'math' and capability 'adder'" in failed.text
