import asyncio
from typing import Any

from pydantic import ValidationError
from pydantic_ai import RunContext as PydanticRunContext
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred, ModelRetry, ToolFailed

from ovid_core.adapters.pydantic_ai._tool_context import tool_context_from_pydantic
from ovid_core.errors import (
    ToolApprovalRequired,
    ToolDeferred,
    ToolExecutionError,
    ToolRetryError,
    ToolTimeoutError,
    ToolValidationError,
)
from ovid_core.hooks.base import BaseToolHook
from ovid_core.models import BaseModel
from ovid_core.policy import AgentRetryPolicy
from ovid_core.tools.base import BaseTool
from ovid_core.tools.models import ToolResult


async def execute_tool[Deps](
    name: str,
    tool: BaseTool[Deps, Any, Any],
    raw_arguments: dict[str, Any],
    ctx: PydanticRunContext[Deps],
    hooks: tuple[BaseToolHook[Deps], ...],
    retries: AgentRetryPolicy,
) -> ToolResult:
    context = tool_context_from_pydantic(ctx)
    arguments = _validated_arguments(name, tool, raw_arguments, retries)
    returning = retries.tool_failures == 'return'
    deadline = asyncio.timeout(tool.timeout_seconds)
    validating_result = False
    try:
        for hook in hooks:
            await hook.before_tool(context, tool.id, arguments)
        async with deadline:
            output = await tool.execute(context, arguments)
        validating_result = True
        result = tool.result_type.model_validate(output)
        validating_result = False
        for hook in hooks:
            await hook.after_tool(context, tool.id, result)
    except ApprovalRequired, CallDeferred:
        raise
    except (ToolApprovalRequired, ToolDeferred) as error:
        raise _upstream_deferral(error) from error
    except Exception as error:
        if returning and isinstance(error, (ModelRetry, ToolFailed)):
            raise
        failure, reply = _classified_failure(name, tool, error, deadline.expired(), validating_result, retries)
    else:
        return result

    for hook in hooks:
        await hook.on_tool_error(context, tool.id, failure)
    if not returning:
        raise failure

    raise reply from failure


def _upstream_deferral(error: ToolApprovalRequired | ToolDeferred) -> ApprovalRequired | CallDeferred:
    if isinstance(error, ToolApprovalRequired):
        return ApprovalRequired(error.metadata)

    return CallDeferred(error.metadata, kind=error.kind)


def _validated_arguments(
    name: str,
    tool: BaseTool[Any, Any, Any],
    raw_arguments: dict[str, Any],
    retries: AgentRetryPolicy,
) -> BaseModel:
    try:
        return tool.args_type.model_validate(raw_arguments)
    except ValidationError as error:
        if retries.tool_failures == 'raise':
            raise ToolValidationError(f'Invalid arguments for tool {tool.id!r}') from error

        lines = '\n'.join(
            f'{".".join(str(item) for item in detail["loc"]) or "arguments"}: {detail["msg"]}'
            for detail in error.errors(include_url=False)
        )
        message = f'Invalid arguments for tool {name!r}:\n{lines}'
        raise ModelRetry(_truncated(message, retries.error_max_chars)) from error


def _classified_failure(
    name: str,
    tool: BaseTool[Any, Any, Any],
    error: Exception,
    deadline_expired: bool,
    invalid_result: bool,
    retries: AgentRetryPolicy,
) -> tuple[ToolExecutionError, ModelRetry | ToolFailed]:
    limit = retries.error_max_chars
    if isinstance(error, ToolExecutionError) and not isinstance(error, TimeoutError):
        reply_type = ModelRetry if isinstance(error, ToolRetryError) else ToolFailed
        return error, reply_type(_truncated(str(error), limit))

    failure = ToolExecutionError(f'Tool {tool.id!r} failed')
    detail = f'failed: {type(error).__name__}: {error}'
    if invalid_result:
        detail = 'returned an invalid result'
    if isinstance(error, TimeoutError):
        failure = ToolTimeoutError(f'Tool {tool.id!r} timed out')
        detail = f'timed out after {tool.timeout_seconds}s' if deadline_expired else detail
    failure.__cause__ = error

    return failure, ToolFailed(_truncated(f'Tool {name!r} {detail}', limit))


def _truncated(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text

    marker = '... [truncated]'
    return f'{text[: max(limit - len(marker), 0)]}{marker}'[:limit]
