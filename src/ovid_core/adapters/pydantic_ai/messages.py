from pydantic import JsonValue, TypeAdapter, ValidationError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelRequestPart, ModelResponse, ModelResponsePart
from pydantic_ai.messages import RetryPromptPart as PydanticRetryPromptPart
from pydantic_ai.messages import SystemPromptPart as PydanticSystemPromptPart
from pydantic_ai.messages import TextPart as PydanticTextPart
from pydantic_ai.messages import ToolCallPart as PydanticToolCallPart
from pydantic_ai.messages import ToolReturnPart as PydanticToolReturnPart
from pydantic_ai.messages import UserPromptPart as PydanticUserPromptPart
from pydantic_ai.usage import RequestUsage as PydanticRequestUsage

from ovid_core.adapters.pydantic_ai.usage import request_usage_from_pydantic
from ovid_core.errors import ProviderError
from ovid_core.messages.models import (
    AgentMessage,
    MessagePart,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.usage.models import RequestUsage


_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)


def message_from_pydantic(value: ModelMessage) -> AgentMessage:
    try:
        if isinstance(value, ModelRequest):
            return AgentMessage(
                role='request',
                parts=tuple(_request_part_from_pydantic(part) for part in value.parts),
                run_id=RunId(value.run_id) if value.run_id is not None else None,
                conversation_id=ConversationId(value.conversation_id) if value.conversation_id is not None else None,
                timestamp=value.timestamp,
                instructions=value.instructions,
            )

        return AgentMessage(
            role='response',
            parts=tuple(_response_part_from_pydantic(part) for part in value.parts),
            run_id=RunId(value.run_id) if value.run_id is not None else None,
            conversation_id=ConversationId(value.conversation_id) if value.conversation_id is not None else None,
            timestamp=value.timestamp,
            request_usage=request_usage_from_pydantic(
                value.usage,
                provider_namespace=value.provider_name or 'pydantic_ai',
            ),
            model_name=value.model_name,
            provider_name=value.provider_name,
            provider_response_id=value.provider_response_id,
            finish_reason=value.finish_reason,
        )
    except (TypeError, ValidationError, ValueError) as error:
        raise ProviderError('Pydantic AI returned an unsupported or invalid message') from error


def message_to_pydantic(value: AgentMessage) -> ModelMessage:
    try:
        if value.role == 'request':
            return ModelRequest(
                parts=tuple(_request_part_to_pydantic(part) for part in value.parts),
                timestamp=value.timestamp,
                instructions=value.instructions,
                run_id=str(value.run_id) if value.run_id is not None else None,
                conversation_id=str(value.conversation_id) if value.conversation_id is not None else None,
            )
        if value.timestamp is None:
            raise ValueError('response messages require a timestamp for upstream conversion')

        assert value.request_usage is not None

        return ModelResponse(
            parts=tuple(_response_part_to_pydantic(part) for part in value.parts),
            usage=_request_usage_to_pydantic(value.request_usage),
            model_name=value.model_name,
            timestamp=value.timestamp,
            provider_name=value.provider_name,
            provider_response_id=value.provider_response_id,
            finish_reason=value.finish_reason,
            run_id=str(value.run_id) if value.run_id is not None else None,
            conversation_id=str(value.conversation_id) if value.conversation_id is not None else None,
        )
    except (TypeError, ValidationError, ValueError) as error:
        raise ProviderError('Normalized message cannot be converted to Pydantic AI') from error


def _request_part_from_pydantic(value: object) -> MessagePart:
    if isinstance(value, PydanticSystemPromptPart):
        return SystemPromptPart(content=value.content)
    if isinstance(value, PydanticUserPromptPart) and isinstance(value.content, str):
        return UserPromptPart(content=value.content)
    if isinstance(value, PydanticToolReturnPart):
        return ToolReturnPart(
            tool_name=value.tool_name,
            content=_JSON_VALUE_ADAPTER.validate_python(value.content),
            tool_call_id=value.tool_call_id,
            outcome=value.outcome,
        )
    if isinstance(value, PydanticRetryPromptPart):
        content = value.content if isinstance(value.content, str) else value.model_response()

        return RetryPromptPart(content=content, tool_name=value.tool_name, tool_call_id=value.tool_call_id)

    raise ValueError('unsupported Pydantic AI request part')


def _response_part_from_pydantic(value: object) -> MessagePart:
    if isinstance(value, PydanticTextPart):
        return TextPart(content=value.content)
    if isinstance(value, PydanticToolCallPart):
        return ToolCallPart(tool_name=value.tool_name, arguments=value.args, tool_call_id=value.tool_call_id)

    raise ValueError('unsupported Pydantic AI response part')


def _request_part_to_pydantic(value: MessagePart) -> ModelRequestPart:
    if isinstance(value, SystemPromptPart):
        return PydanticSystemPromptPart(value.content)
    if isinstance(value, UserPromptPart):
        return PydanticUserPromptPart(value.content)
    if isinstance(value, ToolReturnPart):
        return PydanticToolReturnPart(value.tool_name, value.content, value.tool_call_id, outcome=value.outcome)
    if isinstance(value, RetryPromptPart):
        return PydanticRetryPromptPart(value.content, tool_name=value.tool_name, tool_call_id=value.tool_call_id)

    raise ValueError('normalized part is not valid in a request')


def _response_part_to_pydantic(value: MessagePart) -> ModelResponsePart:
    if isinstance(value, TextPart):
        return PydanticTextPart(value.content)
    if isinstance(value, ToolCallPart):
        return PydanticToolCallPart(value.tool_name, value.arguments, value.tool_call_id)

    raise ValueError('normalized part is not valid in a response')


def _request_usage_to_pydantic(value: RequestUsage) -> PydanticRequestUsage:
    if len(value.provider_details) > 1:
        raise ValueError('request usage cannot contain details from multiple providers')

    details = next(iter(value.provider_details.values()), {})

    return PydanticRequestUsage(**value.model_dump(exclude={'provider_details'}), details=details)
