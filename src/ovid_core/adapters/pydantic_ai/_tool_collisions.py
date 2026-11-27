from asyncio import gather
from dataclasses import replace
from typing import Any

from pydantic_ai import RunContext as PydanticRunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from ovid_core.adapters.pydantic_ai.tools import PydanticAIToolsetAdapter
from ovid_core.errors import ExtensionCollisionError


class _CollisionCheckedToolset[Deps](CombinedToolset[Deps]):
    async def get_tools(self, ctx: PydanticRunContext[Deps]) -> dict[str, ToolsetTool[Deps]]:
        try:
            return await super().get_tools(ctx)
        except UserError as error:
            owners: dict[str, ToolsetTool[Deps]] = {}
            for tools in await _listings(self, ctx):
                for name, tool in tools.items():
                    first = owners.setdefault(name, tool)
                    if first is not tool:
                        message = f'Tool name {name!r} comes from {_tool_source(first)} and {_tool_source(tool)}'
                        raise ExtensionCollisionError(f'{message}. Rename or prefix one of the tools.') from error
            raise


async def _listings[Deps](
    toolset: AbstractToolset[Deps], ctx: PydanticRunContext[Deps]
) -> list[dict[str, ToolsetTool[Deps]]]:
    """List each toolset below the nested combinations, so that a clash between two of them shows its sources."""
    if not isinstance(toolset, CombinedToolset):
        return [await toolset.get_tools(ctx)]

    nested = await gather(*(_listings(child, ctx) for child in toolset.toolsets))
    return [tools for listings in nested for tools in listings]


class OvidToolCollisionsCapability[Deps](AbstractCapability[Deps]):
    """Report a tool name that two toolsets of the agent share as `ExtensionCollisionError`.

    The check covers Ovid tools and the tools of wrapped Pydantic AI capabilities.
    """

    def __init__(self) -> None:
        self.id = 'ovid_core.tool_collisions'

    def get_wrapper_toolset(self, toolset: AbstractToolset[Deps]) -> AbstractToolset[Deps] | None:
        if isinstance(toolset, CombinedToolset):
            return _CollisionCheckedToolset(toolset.toolsets)
        if not isinstance(toolset, WrapperToolset):
            return None

        wrapped = self.get_wrapper_toolset(toolset.wrapped)
        return replace(toolset, wrapped=wrapped) if wrapped is not None else None


def _combine_toolsets[Deps](
    adapters: tuple[PydanticAIToolsetAdapter[Deps], ...],
) -> AbstractToolset[Deps] | None:
    if not adapters:
        return None
    if len(adapters) == 1:
        return adapters[0]

    return _CollisionCheckedToolset(adapters)


def _tool_source(tool: ToolsetTool[Any]) -> str:
    toolset = tool.toolset
    if isinstance(toolset, PydanticAIToolsetAdapter):
        return f'capability {toolset.capability_id!r}' if toolset.capability_id else f'toolset {toolset.id!r}'
    if tool.tool_def.capability_id is not None:
        return f'capability {tool.tool_def.capability_id!r}'

    return toolset.label
