from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.messages import ImageUrl, ModelRequest, ModelResponse, ThinkingPart
from pydantic_ai.messages import SystemPromptPart as PydanticSystemPromptPart
from pydantic_ai.messages import TextPart as PydanticTextPart
from pydantic_ai.messages import ToolCallPart as PydanticToolCallPart
from pydantic_ai.messages import ToolReturnPart as PydanticToolReturnPart
from pydantic_ai.messages import UserPromptPart as PydanticUserPromptPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage as PydanticRequestUsage

from ovid_core.adapters.pydantic_ai.messages import message_from_pydantic, message_to_pydantic
from ovid_core.adapters.pydantic_ai.results import result_from_pydantic
from ovid_core.errors import ProviderError
from ovid_core.messages.models import (
    AgentMessage,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.runtime.results import ResultMetadataEntry, RunResult
from ovid_core.usage.models import RequestUsage, Usage
from tests.helpers import CONVERSATION_ID, RUN_ID, make_request_usage


def test_normalized_message_role_invariants() -> None:
    usage = make_request_usage()
    with pytest.raises(ValidationError, match='opposite role'):
        AgentMessage(role='request', parts=(TextPart(content='wrong'),))
    with pytest.raises(ValidationError, match='only valid on response'):
        AgentMessage(role='request', parts=(UserPromptPart(content='hello'),), request_usage=usage)
    with pytest.raises(ValidationError, match='require request_usage'):
        AgentMessage(role='response', parts=(TextPart(content='hello'),))
    with pytest.raises(ValidationError, match='opposite role'):
        AgentMessage(role='response', parts=(UserPromptPart(content='wrong'),), request_usage=usage)


def test_request_message_adapter_round_trip() -> None:
    upstream = ModelRequest(
        parts=(
            PydanticSystemPromptPart('system'),
            PydanticUserPromptPart('hello'),
            PydanticToolReturnPart('lookup', {'found': True}, 'call-1'),
        ),
        run_id=str(RUN_ID),
        conversation_id=str(CONVERSATION_ID),
        instructions='instruction',
    )

    normalized = message_from_pydantic(upstream)
    restored = message_to_pydantic(normalized)
    normalized_again = message_from_pydantic(restored)

    assert normalized_again == normalized
    assert tuple(type(part) for part in normalized.parts) == (
        SystemPromptPart,
        UserPromptPart,
        ToolReturnPart,
    )


def test_retry_prompt_adapter_normalizes_structured_errors() -> None:
    retry = RetryPromptPart(content='try again', tool_name='lookup', tool_call_id='call-1')
    normalized = AgentMessage(role='request', parts=(retry,))

    restored = message_from_pydantic(message_to_pydantic(normalized))

    assert restored == normalized


def test_response_message_adapter_round_trip_with_usage() -> None:
    upstream = ModelResponse(
        parts=(
            PydanticTextPart('answer'),
            PydanticToolCallPart('lookup', {'key': 1}, 'call-1'),
        ),
        usage=PydanticRequestUsage(input_tokens=8, output_tokens=3, details={'reasoning_tokens': 1}),
        model_name='test-model',
        provider_name='test-provider',
        provider_response_id='response-1',
        finish_reason='tool_call',
        run_id=str(RUN_ID),
        conversation_id=str(CONVERSATION_ID),
    )

    normalized = message_from_pydantic(upstream)
    restored = message_to_pydantic(normalized)
    normalized_again = message_from_pydantic(restored)

    assert normalized_again == normalized
    assert tuple(type(part) for part in normalized.parts) == (TextPart, ToolCallPart)
    assert normalized.request_usage is not None
    assert normalized.request_usage.input_tokens == 8


def test_message_adapter_rejects_unsupported_parts_and_invalid_identifiers() -> None:
    unsupported_request = ModelRequest(parts=(PydanticUserPromptPart([ImageUrl(url='https://example.com/a.png')]),))
    with pytest.raises(ProviderError, match='unsupported') as request_error:
        message_from_pydantic(unsupported_request)
    assert isinstance(request_error.value.__cause__, ValueError)

    with pytest.raises(ProviderError, match='unsupported'):
        message_from_pydantic(ModelResponse(parts=(ThinkingPart('private'),)))

    with pytest.raises(ProviderError, match='invalid message'):
        message_from_pydantic(ModelRequest(parts=(), run_id='invalid'))


def test_message_adapter_rejects_wrong_normalized_role_and_colliding_details() -> None:
    invalid_request = AgentMessage.model_construct(role='request', parts=(TextPart(content='wrong'),))
    with pytest.raises(ProviderError, match='cannot be converted'):
        message_to_pydantic(invalid_request)

    invalid_response = AgentMessage.model_construct(
        role='response',
        parts=(UserPromptPart(content='wrong'),),
        request_usage=RequestUsage(),
        timestamp=datetime.now(UTC),
    )
    with pytest.raises(ProviderError, match='cannot be converted'):
        message_to_pydantic(invalid_response)

    missing_timestamp = AgentMessage(
        role='response',
        parts=(TextPart(content='answer'),),
        request_usage=RequestUsage(),
    )
    with pytest.raises(ProviderError, match='cannot be converted'):
        message_to_pydantic(missing_timestamp)

    duplicate_details = RequestUsage(
        provider_details={
            'one': {'same': 1},
            'two': {'same': 2},
        }
    )
    invalid_details = AgentMessage(
        role='response',
        parts=(TextPart(content='answer'),),
        request_usage=duplicate_details,
        timestamp=datetime.now(UTC),
    )
    with pytest.raises(ProviderError, match='cannot be converted'):
        message_to_pydantic(invalid_details)


def test_real_pydantic_ai_result_maps_to_stable_serializable_values() -> None:
    upstream = Agent(TestModel()).run_sync('hello')

    result = result_from_pydantic(upstream)
    restored = type(result).model_validate_json(result.model_dump_json())

    assert restored == result
    assert result.output == 'success (no tool calls)'
    assert result.usage.request_count == 1
    assert len(result.messages) == 2
    assert result.usage == Usage.from_requests((result.messages[1].request_usage,))


def test_result_adapter_redacts_invalid_metadata_errors() -> None:
    upstream = Agent(TestModel()).run_sync('hello', metadata={'api_key': 'secret'})

    with pytest.raises(ProviderError, match='invalid run result') as error:
        result_from_pydantic(upstream)

    assert isinstance(error.value.__cause__, ValidationError)


def test_run_result_invariants_and_non_secret_metadata() -> None:
    request_usage = make_request_usage()
    response = AgentMessage(
        role='response',
        parts=(TextPart(content='answer'),),
        run_id=RUN_ID,
        conversation_id=CONVERSATION_ID,
        request_usage=request_usage,
    )
    usage = Usage.from_requests((request_usage,))
    metadata = ResultMetadataEntry(key='route', value='default')
    result = RunResult[str](
        output='answer',
        messages=(response,),
        usage=usage,
        run_id=RUN_ID,
        conversation_id=CONVERSATION_ID,
        metadata=(metadata,),
    )

    assert RunResult[str].model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError, match='secret values'):
        ResultMetadataEntry(key='api-key', value='secret')
    with pytest.raises(ValidationError, match='metadata keys must be unique'):
        RunResult[str](
            output='answer',
            messages=(response,),
            usage=usage,
            run_id=RUN_ID,
            conversation_id=CONVERSATION_ID,
            metadata=(metadata, metadata),
        )


def test_run_result_rejects_identity_and_usage_mismatches() -> None:
    request_usage = make_request_usage()
    other_run = RunId.new()
    other_conversation = ConversationId.new()
    response = AgentMessage(
        role='response',
        parts=(TextPart(content='answer'),),
        run_id=other_run,
        conversation_id=CONVERSATION_ID,
        request_usage=request_usage,
    )
    with pytest.raises(ValidationError, match='message run_id'):
        RunResult[str](
            output='answer',
            messages=(response,),
            usage=Usage.from_requests((request_usage,)),
            run_id=RUN_ID,
            conversation_id=CONVERSATION_ID,
        )

    response = response.model_copy(update={'run_id': RUN_ID, 'conversation_id': other_conversation})
    with pytest.raises(ValidationError, match='message conversation_id'):
        RunResult[str](
            output='answer',
            messages=(response,),
            usage=Usage.from_requests((request_usage,)),
            run_id=RUN_ID,
            conversation_id=CONVERSATION_ID,
        )

    response = response.model_copy(update={'conversation_id': CONVERSATION_ID})
    with pytest.raises(ValidationError, match='result usage'):
        RunResult[str](
            output='answer',
            messages=(response,),
            usage=Usage(),
            run_id=RUN_ID,
            conversation_id=CONVERSATION_ID,
        )
