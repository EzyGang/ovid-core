import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse
from pydantic_ai.messages import TextPart as PydanticTextPart
from pydantic_ai.messages import UserPromptPart as PydanticUserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage as PydanticRequestUsage

import tests.support.agent_consumer as consumer
from ovid_core import ConversationStore, InMemoryConversationStore, MessageCodec, PersistenceError
from ovid_core.adapters.pydantic_ai import message_from_pydantic, message_to_pydantic
from ovid_core.messages import AgentMessage
from ovid_core.runtime import ConversationId
from tests.support.agent_consumer import AgentDependencies
from tests.support.agent_helpers import agent_factory
from tests.support.helpers import CONVERSATION_ID


def test_message_codec_round_trips_normalized_adapter_values() -> None:
    codec = MessageCodec()
    upstream = (
        ModelRequest(parts=(PydanticUserPromptPart('hello'),), conversation_id=str(CONVERSATION_ID)),
        ModelResponse(
            parts=(PydanticTextPart('answer'),),
            usage=PydanticRequestUsage(input_tokens=4, output_tokens=2),
            model_name='test-model',
            conversation_id=str(CONVERSATION_ID),
        ),
    )

    for message in upstream:
        normalized = message_from_pydantic(message)
        payload = codec.encode(normalized)
        restored = message_from_pydantic(message_to_pydantic(codec.decode(payload)))

        assert restored == normalized
        assert b'"version":2' in payload
        assert codec.decode(payload.replace(b'"version":2', b'"version":1')) == normalized

    assert codec.version == 2


def test_message_codec_rejects_invalid_and_unsupported_payloads_safely() -> None:
    codec = MessageCodec()
    message = message_from_pydantic(ModelRequest(parts=(PydanticUserPromptPart('secret-value'),)))
    unsupported = codec.encode(message).replace(b'"version":2', b'"version":3')

    for payload in (b'{"content":"secret-value"}', unsupported):
        with pytest.raises(PersistenceError, match='invalid or uses an unsupported') as error:
            codec.decode(payload)

        assert isinstance(error.value.__cause__, ValidationError)
        assert 'secret-value' not in str(error.value)


@pytest.mark.asyncio
async def test_in_memory_store_loads_snapshots_and_appends_ordered_batches() -> None:
    store: ConversationStore = InMemoryConversationStore()
    other_conversation_id = ConversationId.new()
    first = message_from_pydantic(
        ModelRequest(parts=(PydanticUserPromptPart('first'),), conversation_id=str(CONVERSATION_ID))
    )
    second = message_from_pydantic(
        ModelRequest(parts=(PydanticUserPromptPart('second'),), conversation_id=str(CONVERSATION_ID))
    )

    assert await store.load(CONVERSATION_ID) == ()
    await store.append(CONVERSATION_ID, ())
    await store.append(CONVERSATION_ID, (first,))
    snapshot = await store.load(CONVERSATION_ID)
    await store.append(CONVERSATION_ID, (second,))

    assert snapshot == (first,)
    assert await store.load(CONVERSATION_ID) == (first, second)
    assert await store.load(other_conversation_id) == ()


async def continuation_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    del info
    prompts = sum(
        isinstance(part, PydanticUserPromptPart)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
    )

    return ModelResponse(
        parts=(PydanticTextPart(f'prompt {prompts}'),),
        usage=PydanticRequestUsage(input_tokens=prompts, output_tokens=1),
    )


@pytest.mark.asyncio
async def test_persisted_normalized_messages_reload_and_continue_conversation() -> None:
    model = FunctionModel(continuation_response, model_name='continuation')
    agent = await agent_factory({'primary': model}).build(consumer.text_definition())
    store = InMemoryConversationStore()
    deps = AgentDependencies(prefix='persistence')

    first = await agent.run('first', deps=deps, conversation_id=CONVERSATION_ID)
    await store.append(CONVERSATION_ID, first.messages)
    history = await store.load(CONVERSATION_ID)
    second = await agent.run('second', deps=deps, messages=history)
    await store.append(CONVERSATION_ID, second.messages)
    persisted = await store.load(CONVERSATION_ID)

    assert first.output == 'prompt 1'
    assert second.output == 'prompt 2'
    assert second.conversation_id == first.conversation_id == CONVERSATION_ID
    assert second.run_id != first.run_id
    assert persisted == first.messages + second.messages
    assert all(isinstance(message, AgentMessage) for message in persisted)
    assert not any(type(message).__module__.startswith('pydantic_ai') for message in persisted)
