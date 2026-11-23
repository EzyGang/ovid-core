from typing import cast

from pydantic import ConfigDict, JsonValue, TypeAdapter, ValidationError
from pydantic_ai._deferred_capabilities import LoadCapabilityReturn as PydanticCapabilityLoadReturn
from pydantic_ai.messages import CompactionPart as PydanticCompactionPart
from pydantic_ai.messages import LoadCapabilityCallPart as PydanticCapabilityLoadCallPart
from pydantic_ai.messages import LoadCapabilityReturnPart as PydanticCapabilityLoadReturnPart
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelRequestPart, ModelResponse, ModelResponsePart
from pydantic_ai.messages import RetryFeedbackPart as PydanticRetryFeedbackPart
from pydantic_ai.messages import RetryInfo as PydanticRetryInfo
from pydantic_ai.messages import SystemPromptPart as PydanticSystemPromptPart
from pydantic_ai.messages import TextPart as PydanticTextPart
from pydantic_ai.messages import ThinkingPart as PydanticThinkingPart
from pydantic_ai.messages import ToolCallPart as PydanticToolCallPart
from pydantic_ai.messages import ToolReturnPart as PydanticToolReturnPart
from pydantic_ai.messages import UserPromptPart as PydanticUserPromptPart
from pydantic_ai.usage import RequestUsage as PydanticRequestUsage
from pydantic_core import to_jsonable_python

from ovid_core.adapters.pydantic_ai.usage import request_usage_from_pydantic
from ovid_core.errors import ProviderError
from ovid_core.messages.models import (
    AgentMessage,
    CapabilityLoadCallPart,
    CapabilityLoadReturnPart,
    CompactionPart,
    MessagePart,
    OpaqueRequestPart,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.usage.models import RequestUsage


_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)
_PROVIDER_DETAILS_ADAPTER = TypeAdapter[dict[str, JsonValue] | None](dict[str, JsonValue] | None)
_REQUEST_PART_ADAPTER = TypeAdapter[ModelRequestPart](
    ModelRequestPart,
    config=ConfigDict(defer_build=True, ser_json_bytes='base64', val_json_bytes='base64'),
)


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
            provider_details=(
                {'compaction': True}
                if value.provider_details and bool(value.provider_details.get('compaction'))
                else None
            ),
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
            provider_details=value.provider_details,
            finish_reason=value.finish_reason,
            run_id=str(value.run_id) if value.run_id is not None else None,
            conversation_id=str(value.conversation_id) if value.conversation_id is not None else None,
        )
    except (TypeError, ValidationError, ValueError) as error:
        raise ProviderError('Normalized message cannot be converted to Pydantic AI') from error


def _request_part_from_pydantic(value: ModelRequestPart) -> MessagePart:
    if isinstance(value, PydanticSystemPromptPart) and value == PydanticSystemPromptPart(
        value.content, timestamp=value.timestamp
    ):
        return SystemPromptPart(content=value.content)
    if isinstance(value, PydanticUserPromptPart) and isinstance(value.content, str):
        return UserPromptPart(content=value.content)
    if isinstance(value, PydanticCompactionPart) and value == PydanticCompactionPart(value.content):
        return CompactionPart(content=value.content)
    if isinstance(value, PydanticCapabilityLoadReturnPart):
        return CapabilityLoadReturnPart(
            message=value.content.get('message'),
            instructions=value.instructions,
            tool_call_id=value.tool_call_id,
            outcome=value.outcome,
        )
    if isinstance(value, PydanticToolReturnPart):
        return ToolReturnPart(
            tool_name=value.tool_name,
            content=_JSON_VALUE_ADAPTER.validate_python(value.content),
            tool_call_id=value.tool_call_id,
            outcome=value.outcome,
        )

    return OpaqueRequestPart(
        part_kind=value.part_kind,
        payload=_REQUEST_PART_ADAPTER.dump_python(value, mode='json'),
    )


def _response_part_from_pydantic(value: object) -> MessagePart:
    if isinstance(value, PydanticTextPart):
        return TextPart(content=value.content)
    if isinstance(value, PydanticThinkingPart):
        return ThinkingPart(
            content=value.content,
            id=value.id,
            signature=value.signature,
            provider_name=value.provider_name,
            provider_details=_PROVIDER_DETAILS_ADAPTER.validate_python(to_jsonable_python(value.provider_details)),
        )
    if isinstance(value, PydanticCompactionPart):
        return CompactionPart(
            content=value.content,
            id=value.id,
            provider_name=value.provider_name,
            provider_details=_PROVIDER_DETAILS_ADAPTER.validate_python(to_jsonable_python(value.provider_details)),
        )
    if isinstance(value, PydanticCapabilityLoadCallPart):
        if value.capability_id is None:
            raise ValueError('capability load call does not contain an ID')

        return CapabilityLoadCallPart(capability_id=value.capability_id, tool_call_id=value.tool_call_id)
    if isinstance(value, PydanticToolCallPart):
        return ToolCallPart(tool_name=value.tool_name, arguments=value.args, tool_call_id=value.tool_call_id)

    raise ValueError('unsupported Pydantic AI response part')


def _request_part_to_pydantic(value: MessagePart) -> ModelRequestPart:
    if isinstance(value, SystemPromptPart):
        return PydanticSystemPromptPart(value.content)
    if isinstance(value, UserPromptPart):
        return PydanticUserPromptPart(value.content)
    if isinstance(value, CompactionPart):
        return _compaction_part_to_pydantic(value)
    if isinstance(value, CapabilityLoadReturnPart):
        return PydanticCapabilityLoadReturnPart(
            content=cast(
                PydanticCapabilityLoadReturn,
                value.model_dump(include={'message', 'instructions'}, exclude_none=True),
            ),
            tool_call_id=value.tool_call_id,
            outcome=value.outcome,
        )
    if isinstance(value, ToolReturnPart):
        return PydanticToolReturnPart(value.tool_name, value.content, value.tool_call_id, outcome=value.outcome)
    if isinstance(value, RetryPromptPart):
        return _retry_prompt_part_to_pydantic(value)
    if isinstance(value, OpaqueRequestPart):
        return _REQUEST_PART_ADAPTER.validate_python(value.payload)

    raise ValueError('normalized part is not valid in a request')


def _retry_prompt_part_to_pydantic(value: RetryPromptPart) -> PydanticToolReturnPart | PydanticRetryFeedbackPart:
    if value.tool_name is None:
        return PydanticRetryFeedbackPart(value.content, retry=PydanticRetryInfo(cause='model_retry'))

    return PydanticToolReturnPart(value.tool_name, value.content, value.tool_call_id, outcome='retried')


def _compaction_part_to_pydantic(value: CompactionPart) -> PydanticCompactionPart:
    return PydanticCompactionPart(
        content=value.content,
        id=value.id,
        provider_name=value.provider_name,
        provider_details=value.provider_details,
    )


def _response_part_to_pydantic(value: MessagePart) -> ModelResponsePart:
    if isinstance(value, TextPart):
        return PydanticTextPart(value.content)
    if isinstance(value, ThinkingPart):
        return PydanticThinkingPart(
            content=value.content,
            id=value.id,
            signature=value.signature,
            provider_name=value.provider_name,
            provider_details=value.provider_details,
        )
    if isinstance(value, CompactionPart):
        return _compaction_part_to_pydantic(value)
    if isinstance(value, CapabilityLoadCallPart):
        return PydanticCapabilityLoadCallPart(
            args={'id': value.capability_id},
            tool_call_id=value.tool_call_id,
        )
    if isinstance(value, ToolCallPart):
        return PydanticToolCallPart(value.tool_name, value.arguments, value.tool_call_id)

    raise ValueError('normalized part is not valid in a response')


def _request_usage_to_pydantic(value: RequestUsage) -> PydanticRequestUsage:
    if len(value.provider_details) > 1:
        raise ValueError('request usage cannot contain details from multiple providers')

    details = next(iter(value.provider_details.values()), {})

    return PydanticRequestUsage(**value.model_dump(exclude={'provider_details'}), details=details)
