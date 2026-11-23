from collections.abc import Sequence
from dataclasses import dataclass, replace
from types import TracebackType
from typing import Any, cast

from pydantic import JsonValue
from pydantic_ai import RunContext as PydanticRunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.models import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_core import SchemaValidator, core_schema

from ovid_core.adapters.pydantic_ai._extension_validation import validate_extension_ids
from ovid_core.adapters.pydantic_ai._tool_context import run_context_from_pydantic
from ovid_core.adapters.pydantic_ai._tool_execution import execute_tool
from ovid_core.adapters.pydantic_ai.integrations import adapt_integration_capability
from ovid_core.capabilities.base import BaseCapability
from ovid_core.errors import ExtensionCollisionError, ToolExecutionError
from ovid_core.hooks.base import BaseToolHook
from ovid_core.policy import AgentRetryPolicy
from ovid_core.runtime.context import RunContext
from ovid_core.tools.base import BaseTool, BaseToolset
from ovid_core.tools.models import ToolApproval


_ANY_VALIDATOR = SchemaValidator(core_schema.any_schema())


@dataclass(kw_only=True)
class _BoundToolsetTool[Deps](ToolsetTool[Deps]):
    source_tool: BaseTool[Deps, Any, Any]


@dataclass(kw_only=True)
class PydanticAIToolsetAdapter[Deps](AbstractToolset[Deps]):
    source: BaseToolset[Deps]
    hooks: tuple[BaseToolHook[Deps], ...] = ()
    tool_approval: ToolApproval | None = None
    deferred_capability_id: str | None = None
    retries: AgentRetryPolicy = AgentRetryPolicy()

    @property
    def id(self) -> str:
        return self.source.id

    async def for_run(self, ctx: PydanticRunContext[Deps]) -> AbstractToolset[Deps]:
        return replace(self, source=await self.source.for_run(run_context_from_pydantic(ctx)))

    async def for_run_step(self, ctx: PydanticRunContext[Deps]) -> AbstractToolset[Deps]:
        source = await self.source.for_step(run_context_from_pydantic(ctx))
        if source is self.source:
            return self

        return replace(self, source=source)

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

        return {
            name: _BoundToolsetTool[Deps](
                toolset=self,
                tool_def=_tool_definition(
                    source_tool,
                    tool_approval=self.tool_approval,
                    deferred_capability_id=self.deferred_capability_id,
                ),
                max_retries=self.retries.tools,
                args_validator=_ANY_VALIDATOR,
                source_tool=source_tool,
            )
            for name, source_tool in _unique_tools(discovered).items()
        }

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: PydanticRunContext[Deps],
        tool: ToolsetTool[Deps],
    ) -> JsonValue:
        if not isinstance(tool, _BoundToolsetTool) or tool.toolset is not self:
            raise ToolExecutionError(f'Tool {name!r} is not available for this model step')

        return await execute_tool(tool.source_tool, tool_args, ctx, self.hooks, self.retries)


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
    def __init__(
        self,
        source: BaseCapability[Deps],
        *,
        hooks: tuple[BaseToolHook[Deps], ...] = (),
        include_toolset: bool = True,
        tool_approval: ToolApproval | None = None,
        retries: AgentRetryPolicy = AgentRetryPolicy(),
    ) -> None:
        self.id = source.id
        self.description = source.description
        self.defer_loading = source.defer_loading
        self._source = source
        self._hooks = hooks
        self._include_toolset = include_toolset
        self._tool_approval = tool_approval
        self._retries = retries

    def get_instructions(self) -> list[str] | None:
        instructions = self._source.contributions.instructions
        return list(instructions) if instructions else None

    def get_model_settings(self) -> ModelSettings | None:
        values = self._source.contributions.model_settings.values
        return cast(ModelSettings, dict(values)) if values else None

    def get_toolset(self) -> AbstractToolset[Deps] | None:
        if not self._include_toolset:
            return None

        contributions = self._source.contributions
        hooks = (*self._hooks, *contributions.hooks)
        sources = list(contributions.toolsets)
        if contributions.tools:
            sources.insert(0, _StaticToolset(id=self._source.id, tools=contributions.tools))

        return _combine_toolsets(
            tuple(
                PydanticAIToolsetAdapter(
                    source=source,
                    hooks=hooks,
                    tool_approval=self._tool_approval,
                    retries=self._retries,
                )
                for source in sources
            )
        )


def adapt_capabilities[Deps](
    capabilities: Sequence[BaseCapability[Deps]],
    *,
    tool_approval: ToolApproval | None = None,
    retries: AgentRetryPolicy = AgentRetryPolicy(),
) -> tuple[AbstractCapability[Deps], ...]:
    validate_extension_ids(capabilities)

    return tuple(
        integration
        if (integration := adapt_integration_capability(capability)) is not None
        else PydanticAICapabilityAdapter(capability, tool_approval=tool_approval, retries=retries)
        for capability in capabilities
    )


def _unique_tools[Deps](tools: Sequence[BaseTool[Deps, Any, Any]]) -> dict[str, BaseTool[Deps, Any, Any]]:
    result: dict[str, BaseTool[Deps, Any, Any]] = {}
    for tool in tools:
        name = _wire_name(tool)
        if not name or name in result:
            raise ExtensionCollisionError(f'Duplicate or empty effective tool name: {name!r}')
        result[name] = tool

    return result


def _combine_toolsets[Deps](
    adapters: tuple[PydanticAIToolsetAdapter[Deps], ...],
) -> AbstractToolset[Deps] | None:
    if not adapters:
        return None
    if len(adapters) == 1:
        return adapters[0]

    return _CollisionCheckedToolset(adapters)


def _tool_definition(
    tool: BaseTool[Any, Any, Any],
    *,
    tool_approval: ToolApproval | None,
    deferred_capability_id: str | None,
) -> ToolDefinition:
    presentation = tool.presentation
    approval = tool_approval if tool_approval is not None else tool.approval
    metadata: dict[str, JsonValue] = {'ovid_approval': approval.model_dump(mode='json')}
    if presentation.input_format != 'json' or presentation.grammar is not None:
        metadata['ovid_input_format'] = presentation.input_format
    if presentation.grammar is not None:
        metadata['ovid_grammar'] = {
            'syntax': presentation.grammar.syntax,
            'definition': presentation.grammar.definition,
        }

    return ToolDefinition(
        name=_wire_name(tool),
        description=tool.description,
        parameters_json_schema=tool.args_type.model_json_schema(),
        kind='unapproved' if approval.required else 'function',
        metadata=metadata,
        timeout=tool.timeout_seconds,
        defer_loading=tool.defer_loading or deferred_capability_id is not None,
        capability_id=deferred_capability_id,
    )


def _wire_name(tool: BaseTool[Any, Any, Any]) -> str:
    return tool.presentation.wire_name or tool.id
