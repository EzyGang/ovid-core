from uuid import uuid4

import pytest
from pydantic_ai import RunContext as PydanticRunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from ovid_core import ExtensionCollisionError
from ovid_core.adapters.pydantic_ai import PydanticAIToolsetAdapter
from ovid_core.tools import ToolGrammar, ToolPresentation
from tests.tools.tool_consumer import AddTool, Dependencies, FastAddTool, TrackingToolset


def upstream_context() -> PydanticRunContext[Dependencies]:
    return PydanticRunContext(
        deps=Dependencies(prefix='sum'),
        model=TestModel(),
        usage=RunUsage(),
        tool_call_id='call-1',
        tool_name='edit',
        tool_call_approved=False,
        tool_call_metadata=None,
        run_id=str(uuid4()),
        conversation_id=str(uuid4()),
    )


async def test_effective_wire_name_and_text_fallback_are_advertised() -> None:
    tool = AddTool()
    tool.presentation = ToolPresentation(
        wire_name='edit',
        input_format='text',
        grammar=ToolGrammar(syntax='lark', definition='start: /.+/'),
    )
    adapter = PydanticAIToolsetAdapter(source=TrackingToolset((tool,)))

    definitions = await adapter.get_tools(upstream_context())

    assert tuple(definitions) == ('edit',)
    assert definitions['edit'].tool_def.name == 'edit'
    assert definitions['edit'].tool_def.parameters_json_schema == tool.args_type.model_json_schema()
    assert definitions['edit'].tool_def.metadata == {
        'ovid_approval': {'required': True, 'reason': 'Writes a total', 'metadata': {'risk': 'low'}},
        'ovid_input_format': 'text',
        'ovid_grammar': {'syntax': 'lark', 'definition': 'start: /.+/'},
    }


async def test_advertised_call_is_pinned_to_exact_source_instance() -> None:
    first = AddTool()
    second = AddTool()
    first.presentation = ToolPresentation(wire_name='edit')
    second.presentation = ToolPresentation(wire_name='edit')
    adapter = PydanticAIToolsetAdapter(source=TrackingToolset((first,)))
    context = upstream_context()
    first_definitions = await adapter.get_tools(context)
    adapter.source = TrackingToolset((second,))
    await adapter.get_tools(context)

    result = await adapter.call_tool('edit', {'left': 2, 'right': 3}, context, first_definitions['edit'])

    assert result == {'content': 5, 'metadata': {'prefix': 'sum'}}
    assert first.context is not None
    assert second.context is None


async def test_effective_wire_name_collisions_fail_before_advertisement() -> None:
    first = AddTool()
    second = FastAddTool()
    first.presentation = ToolPresentation(wire_name='edit')
    second.presentation = ToolPresentation(wire_name='edit')
    adapter = PydanticAIToolsetAdapter(source=TrackingToolset((first, second)))

    with pytest.raises(ExtensionCollisionError, match="Duplicate or empty tool ID: 'edit'"):
        await adapter.get_tools(upstream_context())
