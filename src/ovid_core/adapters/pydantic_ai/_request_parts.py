from typing import cast

from pydantic import ConfigDict, JsonValue, TypeAdapter, ValidationError
from pydantic_ai._deferred_capabilities import LoadCapabilityReturn as PydanticCapabilityLoadReturn
from pydantic_ai.messages import CompactionPart as PydanticCompactionPart
from pydantic_ai.messages import LoadCapabilityReturnPart as PydanticCapabilityLoadReturnPart
from pydantic_ai.messages import ModelRequestPart
from pydantic_ai.messages import RetryFeedbackPart as PydanticRetryFeedbackPart
from pydantic_ai.messages import RetryInfo as PydanticRetryInfo
from pydantic_ai.messages import SystemPromptPart as PydanticSystemPromptPart
from pydantic_ai.messages import ToolReturnPart as PydanticToolReturnPart
from pydantic_ai.messages import UserPromptPart as PydanticUserPromptPart

from ovid_core.adapters.pydantic_ai._user_content import user_content_from_pydantic, user_content_to_pydantic
from ovid_core.messages.models import (
    CapabilityLoadReturnPart,
    CompactionPart,
    MessagePart,
    OpaqueRequestPart,
    RetryPromptPart,
    SystemPromptPart,
    ToolReturnPart,
    UserPromptPart,
)


_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)
_REQUEST_PART_ADAPTER = TypeAdapter[ModelRequestPart](
    ModelRequestPart,
    config=ConfigDict(defer_build=True, ser_json_bytes='base64', val_json_bytes='base64'),
)


def request_part_from_pydantic(value: ModelRequestPart) -> MessagePart:
    if isinstance(value, PydanticSystemPromptPart) and value == PydanticSystemPromptPart(
        value.content, timestamp=value.timestamp
    ):
        return SystemPromptPart(content=value.content)
    if isinstance(value, PydanticUserPromptPart):
        return _user_prompt_part_from_pydantic(value)
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
        content, content_file_digests = tool_content_from_pydantic(value)

        return ToolReturnPart(
            tool_name=value.tool_name,
            content=content,
            tool_call_id=value.tool_call_id,
            outcome=value.outcome,
            content_file_digests=content_file_digests,
        )

    return opaque_request_part(value)


def tool_content_from_pydantic(value: PydanticToolReturnPart) -> tuple[JsonValue, tuple[str, ...] | None]:
    """Return JSON content, plus the Pydantic AI file record when the content is not plain JSON."""
    try:
        return _JSON_VALUE_ADAPTER.validate_python(value.content), None
    except ValidationError:
        payload = _REQUEST_PART_ADAPTER.dump_python(value, mode='json')

    return payload['content'], tuple(payload.get('content_file_digests', ()))


def request_part_to_pydantic(value: MessagePart) -> ModelRequestPart:
    if isinstance(value, SystemPromptPart):
        return PydanticSystemPromptPart(value.content)
    if isinstance(value, UserPromptPart):
        content = value.content

        return PydanticUserPromptPart(content if isinstance(content, str) else user_content_to_pydantic(content))
    if isinstance(value, CompactionPart):
        return compaction_part_to_pydantic(value)
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
        return _tool_return_part_to_pydantic(value)
    if isinstance(value, RetryPromptPart):
        return _retry_prompt_part_to_pydantic(value)
    if isinstance(value, OpaqueRequestPart):
        return _REQUEST_PART_ADAPTER.validate_python(value.payload)

    raise ValueError('normalized part is not valid in a request')


def compaction_part_to_pydantic(value: CompactionPart) -> PydanticCompactionPart:
    return PydanticCompactionPart(
        content=value.content,
        id=value.id,
        provider_name=value.provider_name,
        provider_details=value.provider_details,
    )


def _user_prompt_part_from_pydantic(value: PydanticUserPromptPart) -> UserPromptPart | OpaqueRequestPart:
    if isinstance(value.content, str):
        return UserPromptPart(content=value.content)

    content = user_content_from_pydantic(value.content)
    if content is None:
        return opaque_request_part(value)

    return UserPromptPart(content=content)


def opaque_request_part(value: ModelRequestPart) -> OpaqueRequestPart:
    return OpaqueRequestPart(part_kind=value.part_kind, payload=_REQUEST_PART_ADAPTER.dump_python(value, mode='json'))


def _tool_return_part_to_pydantic(value: ToolReturnPart) -> ModelRequestPart:
    if value.content_file_digests is None:
        return PydanticToolReturnPart(value.tool_name, value.content, value.tool_call_id, outcome=value.outcome)

    return _REQUEST_PART_ADAPTER.validate_python(
        {
            'part_kind': 'tool-return',
            'tool_name': value.tool_name,
            'content': value.content,
            'tool_call_id': value.tool_call_id,
            'outcome': value.outcome,
            'content_file_digests': list(value.content_file_digests),
        }
    )


def _retry_prompt_part_to_pydantic(value: RetryPromptPart) -> PydanticToolReturnPart | PydanticRetryFeedbackPart:
    if value.tool_name is None:
        return PydanticRetryFeedbackPart(value.content, retry=PydanticRetryInfo(cause='model_retry'))

    return PydanticToolReturnPart(value.tool_name, value.content, value.tool_call_id, outcome='retried')
