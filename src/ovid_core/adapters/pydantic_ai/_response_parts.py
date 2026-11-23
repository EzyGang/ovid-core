from typing import Any

from pydantic import ConfigDict, JsonValue, TypeAdapter
from pydantic_ai.messages import CompactionPart as PydanticCompactionPart
from pydantic_ai.messages import FilePart as PydanticFilePart
from pydantic_ai.messages import LoadCapabilityCallPart as PydanticCapabilityLoadCallPart
from pydantic_ai.messages import ModelResponsePart
from pydantic_ai.messages import NativeToolCallPart as PydanticNativeToolCallPart
from pydantic_ai.messages import NativeToolReturnPart as PydanticNativeToolReturnPart
from pydantic_ai.messages import TextPart as PydanticTextPart
from pydantic_ai.messages import ThinkingPart as PydanticThinkingPart
from pydantic_ai.messages import ToolCallPart as PydanticToolCallPart
from pydantic_core import to_jsonable_python

from ovid_core.adapters.pydantic_ai._request_parts import compaction_part_to_pydantic
from ovid_core.adapters.pydantic_ai._user_content import binary_content_from_pydantic, binary_content_to_pydantic
from ovid_core.messages.models import (
    CapabilityLoadCallPart,
    CompactionPart,
    FilePart,
    MessagePart,
    NativeToolCallPart,
    NativeToolReturnPart,
    OpaqueResponsePart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
)


_PROVIDER_DETAILS_ADAPTER = TypeAdapter[dict[str, JsonValue] | None](dict[str, JsonValue] | None)
_RESPONSE_PART_ADAPTER = TypeAdapter[ModelResponsePart](
    ModelResponsePart,
    config=ConfigDict(defer_build=True, ser_json_bytes='base64', val_json_bytes='base64'),
)


def response_part_from_pydantic(value: ModelResponsePart) -> MessagePart:
    if isinstance(value, PydanticTextPart):
        return TextPart(content=value.content)
    if isinstance(value, PydanticThinkingPart):
        return ThinkingPart(
            content=value.content,
            id=value.id,
            signature=value.signature,
            provider_name=value.provider_name,
            provider_details=_provider_details(value.provider_details),
        )
    if isinstance(value, PydanticCompactionPart):
        return CompactionPart(
            content=value.content,
            id=value.id,
            provider_name=value.provider_name,
            provider_details=_provider_details(value.provider_details),
        )
    if isinstance(value, PydanticCapabilityLoadCallPart):
        if value.capability_id is None:
            return opaque_response_part(value)

        return CapabilityLoadCallPart(capability_id=value.capability_id, tool_call_id=value.tool_call_id)
    if isinstance(value, PydanticToolCallPart):
        return ToolCallPart(tool_name=value.tool_name, arguments=value.args, tool_call_id=value.tool_call_id)

    return _provider_part_from_pydantic(value)


def response_part_to_pydantic(value: MessagePart) -> ModelResponsePart:
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
        return compaction_part_to_pydantic(value)
    if isinstance(value, CapabilityLoadCallPart):
        return PydanticCapabilityLoadCallPart(
            args={'id': value.capability_id},
            tool_call_id=value.tool_call_id,
        )
    if isinstance(value, ToolCallPart):
        return PydanticToolCallPart(value.tool_name, value.arguments, value.tool_call_id)

    return _provider_part_to_pydantic(value)


def _provider_part_from_pydantic(value: ModelResponsePart) -> MessagePart:
    if isinstance(value, PydanticNativeToolCallPart):
        return NativeToolCallPart(
            tool_name=value.tool_name,
            arguments=value.args,
            tool_call_id=value.tool_call_id,
            tool_kind=value.tool_kind,
            id=value.id,
            provider_name=value.provider_name,
            provider_details=_provider_details(value.provider_details),
        )
    if isinstance(value, PydanticFilePart):
        return FilePart(
            content=binary_content_from_pydantic(value.content),
            id=value.id,
            provider_name=value.provider_name,
            provider_details=_provider_details(value.provider_details),
        )

    if not isinstance(value, PydanticNativeToolReturnPart):
        return opaque_response_part(value)

    payload = _RESPONSE_PART_ADAPTER.dump_python(value, mode='json')

    return NativeToolReturnPart(
        tool_name=value.tool_name,
        content=payload['content'],
        tool_call_id=value.tool_call_id,
        outcome=value.outcome,
        content_file_digests=payload.get('content_file_digests'),
        tool_kind=value.tool_kind,
        metadata=payload['metadata'],
        provider_name=value.provider_name,
        provider_details=payload['provider_details'],
    )


def opaque_response_part(value: ModelResponsePart) -> OpaqueResponsePart:
    return OpaqueResponsePart(part_kind=value.part_kind, payload=_RESPONSE_PART_ADAPTER.dump_python(value, mode='json'))


def _provider_part_to_pydantic(value: MessagePart) -> ModelResponsePart:
    if isinstance(value, NativeToolCallPart):
        return _RESPONSE_PART_ADAPTER.validate_python(
            {
                'part_kind': 'builtin-tool-call',
                'tool_name': value.tool_name,
                'args': value.arguments,
                'tool_call_id': value.tool_call_id,
                'tool_kind': value.tool_kind,
                'id': value.id,
                'provider_name': value.provider_name,
                'provider_details': value.provider_details,
            }
        )
    if isinstance(value, NativeToolReturnPart):
        return _RESPONSE_PART_ADAPTER.validate_python(
            {
                'part_kind': 'builtin-tool-return',
                **value.model_dump(mode='json', exclude={'kind'}),
            }
        )
    if isinstance(value, FilePart):
        return PydanticFilePart(
            content=binary_content_to_pydantic(value.content),
            id=value.id,
            provider_name=value.provider_name,
            provider_details=value.provider_details,
        )
    if isinstance(value, OpaqueResponsePart):
        return _RESPONSE_PART_ADAPTER.validate_python(value.payload)

    raise ValueError('normalized part is not valid in a response')


def _provider_details(value: dict[str, Any] | None) -> dict[str, JsonValue] | None:
    return _PROVIDER_DETAILS_ADAPTER.validate_python(to_jsonable_python(value))
