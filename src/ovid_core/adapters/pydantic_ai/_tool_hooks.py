from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from pydantic import ValidationError
from pydantic_ai import RunContext as PydanticRunContext
from pydantic_ai.capabilities import AbstractCapability, ValidatedToolArgs
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred, ModelRetry, SkipToolExecution, ToolFailed
from pydantic_ai.exceptions import ToolRetryError as PydanticToolRetryError
from pydantic_ai.messages import AudioUrl, BinaryContent, DocumentUrl, ImageUrl, ToolCallPart, ToolReturn, VideoUrl
from pydantic_ai.tools import ToolDefinition, ToolDenied
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_core import to_json, to_jsonable_python

from ovid_core.adapters.pydantic_ai._deferred_tools import HOOK_APPROVAL_KEY
from ovid_core.adapters.pydantic_ai._tool_context import tool_context_from_pydantic
from ovid_core.adapters.pydantic_ai._tool_execution import invalid_arguments_message, tool_result_to_pydantic
from ovid_core.adapters.pydantic_ai._user_content import user_content_item_from_pydantic
from ovid_core.adapters.pydantic_ai.tools import PydanticAIToolsetAdapter
from ovid_core.capabilities.base import BaseCapability
from ovid_core.errors import ToolExecutionError, ToolValidationError
from ovid_core.hooks.base import BaseToolHook
from ovid_core.hooks.models import Deny, Proceed, RequireApproval, ToolCall
from ovid_core.policy import AgentRetryPolicy
from ovid_core.tools.base import BaseTool, ToolExecutionContext
from ovid_core.tools.models import ToolApproval, ToolResult


_FILE_TYPES = (BinaryContent, ImageUrl, AudioUrl, VideoUrl, DocumentUrl)
_RETRY_SIGNALS = (PydanticToolRetryError, ModelRetry, ValidationError, ToolValidationError)


class OvidToolHooksCapability[Deps](AbstractCapability[Deps]):
    """Run Ovid tool hooks around the execution of every tool: Ovid, MCP, and upstream tools.

    Agent hooks apply to every tool. Capability hooks apply only to the tools that the capability owns.
    """

    def __init__(
        self,
        *,
        hooks: tuple[BaseToolHook[Deps], ...],
        capabilities: Sequence[BaseCapability[Deps]],
        retries: AgentRetryPolicy,
    ) -> None:
        self.id = 'ovid_core.tool_hooks'
        self._hooks = hooks
        self._capability_hooks: dict[str | None, tuple[BaseToolHook[Deps], ...]] = {
            capability.id: capability.contributions.hooks for capability in capabilities
        }
        self._retries = retries

    async def wrap_tool_execute[Result](
        self,
        ctx: PydanticRunContext[Deps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        handler: Callable[[ValidatedToolArgs], Awaitable[Result]],
    ) -> Result | ToolReturn[Any]:
        metadata = tool_def.metadata or {}
        owner = metadata.get('ovid_capability_id', tool_def.capability_id)
        capability_id = owner if isinstance(owner, str) else None
        hooks = (*self._hooks, *self._capability_hooks.get(capability_id, ()))
        hooked = _HookedCall.start(ctx, call, tool_def, args, hooks, capability_id, self._retries) if hooks else None
        if hooked is None:
            return await handler(args)

        hooked, args = await hooked.before(ctx, args)

        try:
            value = await handler(args)
        except SkipToolExecution, CallDeferred, ApprovalRequired:
            raise
        except Exception as error:
            return await hooked.recovered(error)

        return await hooked.after(value)


@dataclass(frozen=True, slots=True)
class _HookedCall[Deps]:
    hooks: tuple[BaseToolHook[Deps], ...]
    context: ToolExecutionContext[Deps]
    call: ToolCall
    tool: ToolsetTool[Deps]
    ovid_tool: BaseTool[Deps, Any, Any] | None
    retries: AgentRetryPolicy

    @classmethod
    def start(
        cls,
        ctx: PydanticRunContext[Deps],
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        hooks: tuple[BaseToolHook[Deps], ...],
        capability_id: str | None,
        retries: AgentRetryPolicy,
    ) -> _HookedCall[Deps] | None:
        """Return `None` for an Ovid tool call with invalid arguments, which the tool reports without hooks."""
        manager = ctx.tool_manager
        assert manager is not None and manager.tools is not None
        tool = manager.tools[call.tool_name]
        toolset = tool.toolset
        ovid_tool = toolset.listed_tool(call.tool_name) if isinstance(toolset, PydanticAIToolsetAdapter) else None
        try:
            if ovid_tool is not None:
                ovid_tool.args_type.model_validate(args)
        except ValidationError:
            return None

        mcp = {'annotations', 'meta'} <= (tool_def.metadata or {}).keys()
        tool_call = ToolCall(
            tool_name=call.tool_name,
            tool_call_id=call.tool_call_id,
            arguments=to_jsonable_python(args, fallback=str),
            tool_id=ovid_tool.id if ovid_tool is not None else None,
            capability_id=capability_id,
            source='ovid' if ovid_tool is not None else 'mcp' if mcp else 'external',
        )

        return cls(hooks, tool_context_from_pydantic(ctx), tool_call, tool, ovid_tool, retries)

    async def before(
        self,
        ctx: PydanticRunContext[Deps],
        args: ValidatedToolArgs,
    ) -> tuple[_HookedCall[Deps], ValidatedToolArgs]:
        """Chain `before_tool` in order, and stop at the first denial or approval request."""
        hooked = self
        for hook in self.hooks:
            try:
                decision = await hook.before_tool(hooked.context, hooked.call)
            except Exception as error:
                raise SkipToolExecution(ToolDenied(f'Tool call denied: hook {type(hook).__name__} failed.')) from error
            if isinstance(decision, Deny):
                raise SkipToolExecution(ToolDenied(decision.message))
            if isinstance(decision, RequireApproval) and not self.context.approved:
                approval = ToolApproval(required=True, reason=decision.reason, metadata=decision.metadata)
                raise ApprovalRequired({HOOK_APPROVAL_KEY: approval.model_dump(mode='json')})
            if isinstance(decision, Proceed):
                try:
                    validator = hooked.tool.args_validator
                    args = validator.validate_json(to_json(decision.arguments), context=ctx.validation_context)
                    if hooked.ovid_tool is not None:
                        hooked.ovid_tool.args_type.model_validate(args)
                except ValidationError as error:
                    message = invalid_arguments_message(hooked.call.tool_name, error, hooked.retries.error_max_chars)
                    raise SkipToolExecution(ToolDenied(message)) from error
                hooked = replace(hooked, call=hooked.call.model_copy(update={'arguments': decision.arguments}))

        return hooked, args

    async def after[Result](self, value: Result) -> Result | ToolReturn[Any]:
        """Give each `after_tool` the current result. A rewrite replaces the whole result."""
        returned = value if isinstance(value, ToolReturn) else ToolReturn(return_value=value)
        items = returned.return_value if isinstance(returned.return_value, list) else [returned.return_value]
        files = [item for item in items if isinstance(item, _FILE_TYPES)]
        data = [item for item in items if not isinstance(item, _FILE_TYPES)]
        current = ToolResult.model_validate(
            {
                'content': to_jsonable_python(
                    (data[0] if len(data) == 1 else data or None) if files else returned.return_value,
                    fallback=str,
                ),
                'metadata': to_jsonable_python(returned.metadata or {}, fallback=str),
                'files': [user_content_item_from_pydantic(item) for item in files],
            }
        )
        rewritten = False
        for hook in self.hooks:
            try:
                result = await hook.after_tool(self.context, self.call, current)
            except Exception as error:
                raise self._hook_failure(hook) from error
            if result is not None:
                current, rewritten = result, True

        return tool_result_to_pydantic(current) if rewritten else value

    async def recovered(self, error: Exception) -> ToolReturn[Any]:
        """Give the failure to each `on_tool_error`. The first result recovers the call. A retry is not a failure."""
        cause: BaseException | None = error
        while cause is not None and not isinstance(cause, ToolExecutionError):
            cause = cause.__cause__
        if isinstance(cause, ToolExecutionError):
            failure = cause
        elif isinstance(error, _RETRY_SIGNALS):
            raise error
        else:
            failure = ToolExecutionError(f'Tool {self.call.tool_id or self.call.tool_name!r} failed')
            failure.__cause__ = error
        for hook in self.hooks:
            try:
                result = await hook.on_tool_error(self.context, self.call, failure)
            except Exception as hook_error:
                raise self._hook_failure(hook) from hook_error
            if result is not None:
                return tool_result_to_pydantic(result)

        raise error

    def _hook_failure(self, hook: BaseToolHook[Deps]) -> ToolFailed | ToolExecutionError:
        message = f'Tool call failed: hook {type(hook).__name__} failed.'

        return ToolExecutionError(message) if self.retries.tool_failures == 'raise' else ToolFailed(message)
