from pydantic import JsonValue, TypeAdapter, ValidationError
from pydantic_ai import RunContext as PydanticRunContext

from ovid_core.errors import ToolExecutionError
from ovid_core.runtime.context import RunContext
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.tools.base import ToolExecutionContext
from ovid_core.usage.models import Usage


_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)


def tool_context_from_pydantic[Deps](ctx: PydanticRunContext[Deps]) -> ToolExecutionContext[Deps]:
    if ctx.tool_call_id is None:
        raise ToolExecutionError('Pydantic AI did not supply a tool call ID')

    try:
        metadata = _JSON_VALUE_ADAPTER.validate_python(ctx.tool_call_metadata)
    except ValidationError as error:
        raise ToolExecutionError('Pydantic AI supplied invalid approval metadata') from error

    return ToolExecutionContext(
        run=run_context_from_pydantic(ctx),
        tool_call_id=ctx.tool_call_id,
        approved=ctx.tool_call_approved,
        approval_metadata=metadata,
    )


def run_context_from_pydantic[Deps](ctx: PydanticRunContext[Deps]) -> RunContext[Deps]:
    if ctx.run_id is None or ctx.conversation_id is None:
        raise ToolExecutionError('Pydantic AI did not supply run identity')

    upstream = ctx.usage
    try:
        usage = Usage(
            request_count=upstream.requests,
            tool_calls=upstream.tool_calls,
            input_tokens=upstream.input_tokens,
            output_tokens=upstream.output_tokens,
            cache_read_tokens=upstream.cache_read_tokens,
            cache_write_tokens=upstream.cache_write_tokens,
            input_audio_tokens=upstream.input_audio_tokens,
            output_audio_tokens=upstream.output_audio_tokens,
            cache_audio_read_tokens=upstream.cache_audio_read_tokens,
        )
        return RunContext(
            deps=ctx.deps,
            run_id=RunId.model_validate(ctx.run_id),
            conversation_id=ConversationId.model_validate(ctx.conversation_id),
            usage=usage,
        )
    except ValidationError as error:
        raise ToolExecutionError('Pydantic AI supplied invalid tool execution context') from error
