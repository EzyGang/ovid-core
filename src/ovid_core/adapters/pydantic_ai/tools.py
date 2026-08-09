import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, cast

from pydantic import JsonValue, ValidationError
from pydantic_ai import RunContext as PydanticRunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.models import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_core import SchemaValidator, core_schema

from ovid_core.adapters.pydantic_ai._tool_context import run_context_from_pydantic, tool_context_from_pydantic
from ovid_core.capabilities.base import BaseCapability
from ovid_core.errors import ExtensionCollisionError, ToolExecutionError, ToolTimeoutError, ToolValidationError
from ovid_core.hooks.base import BaseToolHook
from ovid_core.runtime.context import RunContext
from ovid_core.tools.base import BaseTool, BaseToolset, ToolExecutionContext


_ANY_VALIDATOR = SchemaValidator(core_schema.any_schema())


@dataclass(kw_only=True)
class PydanticAIToolsetAdapter[Deps](AbstractToolset[Deps]):
    source: BaseToolset[Deps]
    hooks: tuple[BaseToolHook[Deps], ...] = ()
    _tools: dict[str, BaseTool[Deps, Any, Any]] | None = None

    @property
    def id(self) -> str:
        return self.source.id

    async def for_run(self, ctx: PydanticRunContext[Deps]) -> AbstractToolset[Deps]:
        source = await self.source.for_run(run_context_from_pydantic(ctx))
        return type(self)(source=source, hooks=self.hooks)

    async def for_run_step(self, ctx: PydanticRunContext[Deps]) -> AbstractToolset[Deps]:
        source = await self.source.for_step(run_context_from_pydantic(ctx))
        if source is self.source:
            return self

        return type(self)(source=source, hooks=self.hooks)

    async def __aenter__(self) -> PydanticAIToolsetAdapter[Deps]:
        await self.source.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> bool | None:
        exception_type, exception, traceback = args
        return await self.source.__aexit__(
            cast(type[BaseException] | None, exception_type),
            cast(BaseException | None, exception),
            cast(TracebackType | None, traceback),
        )

    async def get_tools(self, ctx: PydanticRunContext[Deps]) -> dict[str, ToolsetTool[Deps]]:
        discovered = await self.source.get_tools(run_context_from_pydantic(ctx))
        tools = _unique_tools(discovered)
        self._tools = tools

        return {
            name: ToolsetTool(
                toolset=self,
                tool_def=_tool_definition(tool),
                max_retries=0,
                args_validator=_ANY_VALIDATOR,
            )
            for name, tool in tools.items()
        }

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: PydanticRunContext[Deps],
        tool: ToolsetTool[Deps],
    ) -> JsonValue:
        del tool
        source_tool = self._tools[name] if self._tools is not None else None
        if source_tool is None:
            raise ToolExecutionError(f'Tool {name!r} is not available')

        return await _execute_tool(source_tool, tool_args, ctx, self.hooks)


class _StaticToolset[Deps](BaseToolset[Deps]):
    def __init__(self, *, id: str, tools: Sequence[BaseTool[Deps, Any, Any]]) -> None:
        self.id = id
        self._tools = tuple(tools)

    async def get_tools(self, context: RunContext[Deps]) -> Sequence[BaseTool[Deps, Any, Any]]:
        del context
        return self._tools


class _CollisionCheckedToolset[Deps](CombinedToolset[Deps]):
    async def get_tools(self, ctx: PydanticRunContext[Deps]) -> dict[str, ToolsetTool[Deps]]:
        try:
            return await super().get_tools(ctx)
        except UserError as error:
            raise ExtensionCollisionError('Capabilities contribute conflicting tool IDs') from error


class PydanticAICapabilityAdapter[Deps](AbstractCapability[Deps]):
    def __init__(self, source: BaseCapability[Deps]) -> None:
        self.id = source.id
        self.description = None
        self.defer_loading = False
        self._source = source

    def get_instructions(self) -> list[str] | None:
        instructions = self._source.contributions.instructions
        return list(instructions) if instructions else None

    def get_model_settings(self) -> ModelSettings | None:
        values = self._source.contributions.model_settings.values
        return cast(ModelSettings, dict(values)) if values else None

    def get_toolset(self) -> AbstractToolset[Deps] | None:
        contributions = self._source.contributions
        sources = list(contributions.toolsets)
        if contributions.tools:
            sources.insert(0, _StaticToolset(id=self._source.id, tools=contributions.tools))

        adapters = [PydanticAIToolsetAdapter(source=source, hooks=contributions.hooks) for source in sources]
        if not adapters:
            return None
        if len(adapters) == 1:
            return adapters[0]

        return _CollisionCheckedToolset(adapters)


def adapt_capabilities[Deps](capabilities: Sequence[BaseCapability[Deps]]) -> tuple[AbstractCapability[Deps], ...]:
    _validate_extension_ids(capabilities)
    return tuple(PydanticAICapabilityAdapter(capability) for capability in capabilities)


def _validate_extension_ids[Deps](capabilities: Sequence[BaseCapability[Deps]]) -> None:
    capability_ids: set[str] = set()
    tool_ids: set[str] = set()
    toolset_ids: set[str] = set()

    for capability in capabilities:
        _add_unique(capability.id, capability_ids, 'capability')
        for tool in capability.contributions.tools:
            _add_unique(tool.id, tool_ids, 'tool')
        for toolset in capability.contributions.toolsets:
            _add_unique(toolset.id, toolset_ids, 'toolset')


def _add_unique(value: str, seen: set[str], kind: str) -> None:
    if not value:
        raise ExtensionCollisionError(f'{kind.capitalize()} IDs must not be empty')
    if value in seen:
        raise ExtensionCollisionError(f'Duplicate {kind} ID: {value!r}')

    seen.add(value)


def _unique_tools[Deps](tools: Sequence[BaseTool[Deps, Any, Any]]) -> dict[str, BaseTool[Deps, Any, Any]]:
    result: dict[str, BaseTool[Deps, Any, Any]] = {}
    for tool in tools:
        if not tool.id or tool.id in result:
            raise ExtensionCollisionError(f'Duplicate or empty tool ID: {tool.id!r}')
        result[tool.id] = tool

    return result


def _tool_definition(tool: BaseTool[Any, Any, Any]) -> ToolDefinition:
    metadata = {'ovid_approval': tool.approval.model_dump(mode='json')}
    return ToolDefinition(
        name=tool.id,
        description=tool.description,
        parameters_json_schema=tool.args_type.model_json_schema(),
        kind='unapproved' if tool.approval.required else 'function',
        metadata=metadata,
        timeout=tool.timeout_seconds,
    )


async def _execute_tool[Deps](
    tool: BaseTool[Deps, Any, Any],
    raw_arguments: dict[str, Any],
    ctx: PydanticRunContext[Deps],
    hooks: tuple[BaseToolHook[Deps], ...],
) -> dict[str, JsonValue]:
    context = tool_context_from_pydantic(ctx)
    try:
        arguments = tool.args_type.model_validate(raw_arguments)
    except ValidationError as error:
        raise ToolValidationError(f'Invalid arguments for tool {tool.id!r}') from error

    try:
        for hook in hooks:
            await hook.before_tool(context, tool.id, arguments)
        async with asyncio.timeout(tool.timeout_seconds):
            result = tool.result_type.model_validate(await tool.execute(context, arguments))
        for hook in hooks:
            await hook.after_tool(context, tool.id, result)
    except asyncio.CancelledError:
        raise
    except TimeoutError as error:
        failure = ToolTimeoutError(f'Tool {tool.id!r} timed out')
        await _notify_error(hooks, context, tool.id, failure)
        raise failure from error
    except ToolExecutionError as error:
        await _notify_error(hooks, context, tool.id, error)
        raise
    except Exception as error:
        failure = ToolExecutionError(f'Tool {tool.id!r} failed')
        await _notify_error(hooks, context, tool.id, failure)
        raise failure from error

    return result.model_dump(mode='json')


async def _notify_error[Deps](
    hooks: tuple[BaseToolHook[Deps], ...],
    context: ToolExecutionContext[Deps],
    tool_id: str,
    error: ToolExecutionError,
) -> None:
    for hook in hooks:
        await hook.on_tool_error(context, tool_id, error)
