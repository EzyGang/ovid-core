import importlib
import inspect
import pkgutil
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4

import pytest
from mcp.types import ToolAnnotations
from pydantic import JsonValue
from pydantic_ai import RunContext as PydanticRunContext
from pydantic_ai import Tool
from pydantic_ai.capabilities import AbstractCapability, ValidatedToolArgs
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage

import ovid_core
from ovid_core import AgentDefinition
from ovid_core.adapters.pydantic_ai import PydanticAIToolsetAdapter, pydantic_ai_capability, resolve_effect
from ovid_core.capabilities import BaseCapability, CapabilityContributions
from ovid_core.routing import ModelRef
from ovid_core.tools import AgentToolDescriptor, BaseTool, ToolEffect
from tests.support.agent_helpers import agent_factory
from tests.tools.tool_consumer import AddArguments, AddTool, Dependencies, FastAddTool, TrackingToolset


READ_SAFE = ToolEffect(kind='read', replay='safe')


class ReadAddTool(FastAddTool):
    effect = READ_SAFE


class AnnotatedAddTool(AddTool):
    effect = READ_SAFE


@dataclass
class EffectRecorder(AbstractCapability[Dependencies]):
    tool_defs: list[ToolDefinition] = field(default_factory=list)

    async def before_tool_execute(
        self,
        ctx: PydanticRunContext[Dependencies],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
    ) -> ValidatedToolArgs:
        del ctx, call
        self.tool_defs.append(tool_def)
        return args


def upstream_context(*, approved: bool) -> PydanticRunContext[Dependencies]:
    return PydanticRunContext(
        deps=Dependencies(prefix='sum'),
        model=TestModel(),
        usage=RunUsage(),
        tool_call_id='call-1',
        tool_name='add',
        tool_call_approved=approved,
        run_id=str(uuid4()),
        conversation_id=str(uuid4()),
    )


def mcp_definition(annotations: JsonValue) -> ToolDefinition:
    return ToolDefinition(name='remote', metadata={'meta': None, 'annotations': annotations, 'task': False})


def plain_function(value: int) -> int:
    return value


def test_every_ovid_tool_descriptor_carries_its_effect() -> None:
    assert AgentToolDescriptor.model_fields['effect'].is_required()
    assert AddTool().descriptor(source='arithmetic').effect == ToolEffect(kind='external', replay='unsafe')
    assert ReadAddTool().descriptor(source='arithmetic').effect == READ_SAFE
    assert AddTool().idempotency_key(AddArguments(left=1, right=2)) is None


async def test_capability_before_tool_execute_sees_ovid_effect() -> None:
    recorder = EffectRecorder()
    tools = BaseCapability[Dependencies](id='arithmetic', contributions=CapabilityContributions(tools=(ReadAddTool(),)))

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        if len(messages) > 1:
            return ModelResponse(parts=(TextPart('done'),))

        return ModelResponse(parts=(ToolCallPart('fast_add', {'left': 2, 'right': 3}, 'call-1'),))

    definition = AgentDefinition[Dependencies, str](
        model=ModelRef(name='primary'),
        deps_type=Dependencies,
        output_type=str,
        capabilities=(tools, pydantic_ai_capability(recorder)),
    )
    agent = await agent_factory({'primary': FunctionModel(respond)}).build(definition)
    await agent.run('Add values', deps=Dependencies(prefix='agent'))

    [tool_def] = recorder.tool_defs
    assert tool_def.metadata is not None
    assert tool_def.metadata['ovid_effect'] == {'kind': 'read', 'replay': 'safe'}
    assert resolve_effect(tool_def) == READ_SAFE


@pytest.mark.parametrize(
    ('tool_def', 'expected'),
    (
        pytest.param(Tool(plain_function).tool_def, ToolEffect(), id='plain-function-tool'),
        pytest.param(
            mcp_definition(ToolAnnotations(readOnlyHint=True, idempotentHint=True).model_dump(by_alias=True)),
            READ_SAFE,
            id='mcp-read-only-idempotent',
        ),
        pytest.param(
            mcp_definition(ToolAnnotations(readOnlyHint=True).model_dump(by_alias=True)),
            ToolEffect(kind='read', replay='unsafe'),
            id='mcp-read-only',
        ),
        pytest.param(mcp_definition(ToolAnnotations().model_dump(by_alias=True)), ToolEffect(), id='mcp-no-hints'),
        pytest.param(mcp_definition(None), ToolEffect(), id='mcp-no-annotations'),
        pytest.param(
            ToolDefinition(
                name='declared',
                metadata={'ovid_effect': {'kind': 'workspace_write', 'replay': 'safe'}, 'annotations': None},
            ),
            ToolEffect(kind='workspace_write', replay='safe'),
            id='ovid-effect',
        ),
    ),
)
def test_resolve_effect_reads_ovid_mcp_and_default_effects(tool_def: ToolDefinition, expected: ToolEffect) -> None:
    assert resolve_effect(tool_def) == expected


@pytest.mark.parametrize(
    'declared',
    (
        'read',
        {'kind': 'read'},
        {'kind': 'delete', 'replay': 'safe'},
        {'kind': 'read', 'replay': True},
        {'kind': 'read', 'replay': 'safe', 'extra': 1},
    ),
)
def test_resolve_effect_ignores_invalid_ovid_effect(declared: JsonValue) -> None:
    tool_def = ToolDefinition(
        name='invalid',
        metadata={'ovid_effect': declared, 'annotations': {'readOnlyHint': True, 'idempotentHint': True}},
    )

    assert resolve_effect(tool_def) == ToolEffect()


@pytest.mark.parametrize(
    'annotations',
    (
        'read-only',
        {'readOnlyHint': 'true', 'idempotentHint': True},
        {'readOnlyHint': 1, 'idempotentHint': True},
        {'readOnlyHint': True, 'idempotentHint': 'yes'},
    ),
)
def test_resolve_effect_ignores_wrongly_typed_annotations(annotations: JsonValue) -> None:
    assert resolve_effect(mcp_definition(annotations)) == ToolEffect()


async def test_effect_does_not_change_approval_or_execution() -> None:
    plain = PydanticAIToolsetAdapter(source=TrackingToolset((AddTool(),)))
    annotated = PydanticAIToolsetAdapter(source=TrackingToolset((AnnotatedAddTool(),)))
    plain_tool = (await plain.get_tools(upstream_context(approved=False)))['add']
    annotated_tool = (await annotated.get_tools(upstream_context(approved=False)))['add']
    assert annotated_tool.tool_def.metadata is not None
    assert plain_tool.tool_def.metadata is not None
    annotated_metadata = dict(annotated_tool.tool_def.metadata)
    plain_metadata = dict(plain_tool.tool_def.metadata)

    assert annotated_metadata.pop('ovid_effect') == {'kind': 'read', 'replay': 'safe'}
    assert plain_metadata.pop('ovid_effect') == {'kind': 'external', 'replay': 'unsafe'}
    assert replace(annotated_tool.tool_def, metadata=annotated_metadata) == replace(
        plain_tool.tool_def, metadata=plain_metadata
    )
    assert annotated_tool.tool_def.kind == 'unapproved'

    context = upstream_context(approved=True)
    arguments = {'left': 2, 'right': 3}
    assert await annotated.call_tool('add', arguments, context, annotated_tool) == await plain.call_tool(
        'add', arguments, context, plain_tool
    )


def concrete_ovid_tools(tool_type: type[BaseTool[Any, Any, Any]]) -> dict[str, ToolEffect]:
    found: dict[str, ToolEffect] = {}
    for subclass in tool_type.__subclasses__():
        found |= concrete_ovid_tools(subclass)
        if subclass.__module__.startswith('ovid_core.') and not inspect.isabstract(subclass):
            found[subclass.id] = subclass.effect

    return found


def test_every_concrete_ovid_core_tool_declares_its_effect() -> None:
    for module in pkgutil.walk_packages(ovid_core.__path__, 'ovid_core.'):
        importlib.import_module(module.name)

    assert concrete_ovid_tools(BaseTool) == {
        'relay_send': ToolEffect(kind='external', replay='unsafe'),
        'relay_wait': ToolEffect(kind='external', replay='unsafe'),
        'relay_pending': ToolEffect(kind='external', replay='unsafe'),
        'relay_contacts': READ_SAFE,
        'todo': ToolEffect(kind='workspace_write', replay='safe'),
    }
