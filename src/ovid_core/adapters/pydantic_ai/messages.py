from collections.abc import Callable

from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse
from pydantic_ai.usage import RequestUsage as PydanticRequestUsage

from ovid_core.adapters.pydantic_ai._request_parts import (
    opaque_request_part,
    request_part_from_pydantic,
    request_part_to_pydantic,
)
from ovid_core.adapters.pydantic_ai._response_parts import (
    opaque_response_part,
    response_part_from_pydantic,
    response_part_to_pydantic,
)
from ovid_core.adapters.pydantic_ai.usage import request_usage_from_pydantic
from ovid_core.errors import ProviderError
from ovid_core.messages.models import AgentMessage, MessagePart
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.usage.models import RequestUsage


def message_from_pydantic(value: ModelMessage) -> AgentMessage:
    try:
        if isinstance(value, ModelRequest):
            return AgentMessage(
                role='request',
                parts=tuple(
                    _part_from_pydantic(part, request_part_from_pydantic, opaque_request_part) for part in value.parts
                ),
                run_id=RunId(value.run_id) if value.run_id is not None else None,
                conversation_id=ConversationId(value.conversation_id) if value.conversation_id is not None else None,
                timestamp=value.timestamp,
                instructions=value.instructions,
            )

        return AgentMessage(
            role='response',
            parts=tuple(
                _part_from_pydantic(part, response_part_from_pydantic, opaque_response_part) for part in value.parts
            ),
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
                parts=tuple(request_part_to_pydantic(part) for part in value.parts),
                timestamp=value.timestamp,
                instructions=value.instructions,
                run_id=str(value.run_id) if value.run_id is not None else None,
                conversation_id=str(value.conversation_id) if value.conversation_id is not None else None,
            )
        if value.timestamp is None:
            raise ValueError('response messages require a timestamp for upstream conversion')

        assert value.request_usage is not None

        return ModelResponse(
            parts=tuple(response_part_to_pydantic(part) for part in value.parts),
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


def _part_from_pydantic[Part](
    value: Part,
    typed: Callable[[Part], MessagePart],
    opaque: Callable[[Part], MessagePart],
) -> MessagePart:
    """Convert to a typed part, or to an opaque part when the typed part cannot hold the value."""
    try:
        return typed(value)
    except ValidationError:
        return opaque(value)


def _request_usage_to_pydantic(value: RequestUsage) -> PydanticRequestUsage:
    if len(value.provider_details) > 1:
        raise ValueError('request usage cannot contain details from multiple providers')

    details = next(iter(value.provider_details.values()), {})

    return PydanticRequestUsage(**value.model_dump(exclude={'provider_details'}), details=details)
