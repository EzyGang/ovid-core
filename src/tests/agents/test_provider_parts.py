import base64
from collections.abc import AsyncIterator
from dataclasses import replace

from pydantic_ai.capabilities import Toolset
from pydantic_ai.messages import BinaryImage, ModelMessage, ModelResponse, ToolReturn
from pydantic_ai.messages import FilePart as PydanticFilePart
from pydantic_ai.messages import ImageUrl as PydanticImageUrl
from pydantic_ai.messages import NativeToolCallPart as PydanticNativeToolCallPart
from pydantic_ai.messages import NativeToolReturnPart as PydanticNativeToolReturnPart
from pydantic_ai.messages import TextPart as PydanticTextPart
from pydantic_ai.messages import ToolReturnPart as PydanticToolReturnPart
from pydantic_ai.messages import UserPromptPart as PydanticUserPromptPart
from pydantic_ai.models.function import AgentInfo, BuiltinToolCallsReturns, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.native_tools import WebSearchTool
from pydantic_ai.toolsets import FunctionToolset

from ovid_core.adapters.pydantic_ai import pydantic_ai_capability
from ovid_core.agents import AgentDefinition
from ovid_core.capabilities import (
    BaseCapability,
    HistoryProcessorCapability,
    ProviderCapability,
    WebSearchCapabilityConfig,
)
from ovid_core.messages import (
    AgentMessage,
    BinaryContent,
    FilePart,
    ImageUrl,
    NativeToolCallPart,
    NativeToolReturnPart,
    TextContent,
    TextPart,
    ToolReturnPart,
    UserContent,
    UserPromptPart,
)
from ovid_core.routing import ModelRef
from ovid_core.runtime import ToolCallEvent, ToolResultEvent
from tests.support.agent_helpers import agent_factory


_PNG = b'\x89PNG\xfb\xff'
_IMAGE = BinaryImage(_PNG, media_type='image/png')
_NATIVE_CALL = PydanticNativeToolCallPart('web_search', {'query': 'ovid'}, 'native-1', provider_name='function')
_NATIVE_RETURN = PydanticNativeToolReturnPart(
    'web_search',
    {'results': [{'url': 'https://example.com'}]},
    'native-1',
    provider_name='function',
)
_PROMPT: tuple[UserContent, ...] = (
    TextContent(content='Describe these.'),
    ImageUrl(url='https://example.com/no-extension'),
    BinaryContent(data=base64.b64encode(_PNG), media_type='image/png'),
)


def _definition(*capabilities: BaseCapability[None]) -> AgentDefinition[None, str]:
    return AgentDefinition[None, str](
        model=ModelRef(name='primary'),
        deps_type=type(None),
        output_type=str,
        capabilities=capabilities,
    )


async def test_web_search_run_keeps_native_parts_and_a_second_run_continues_from_them() -> None:
    requests: list[tuple[list[ModelMessage], AgentInfo]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        requests.append((messages, info))
        if len(messages) > 1:
            return ModelResponse(parts=(PydanticTextPart('Continued.'),))

        return ModelResponse(parts=(_NATIVE_CALL, _NATIVE_RETURN, PydanticTextPart('Found.')))

    async def respond_stream(
        messages: list[ModelMessage],
        info: AgentInfo,
    ) -> AsyncIterator[str | BuiltinToolCallsReturns]:
        del messages, info
        yield {0: _NATIVE_CALL, 1: _NATIVE_RETURN}
        yield 'Found.'

    definition = _definition(ProviderCapability(id='web-search', config=WebSearchCapabilityConfig()))
    model = FunctionModel(respond, stream_function=respond_stream)
    agent = await agent_factory({'primary': model}).build(definition)

    first = await agent.run('Search.', deps=None)
    second = await agent.run('Continue.', deps=None, messages=first.history)
    async with agent.stream('Search.', deps=None) as stream:
        events = [event async for event in stream]

    native_parts = (
        NativeToolCallPart(
            tool_name='web_search',
            arguments={'query': 'ovid'},
            tool_call_id='native-1',
            provider_name='function',
        ),
        NativeToolReturnPart(
            tool_name='web_search',
            content={'results': [{'url': 'https://example.com'}]},
            tool_call_id='native-1',
            content_file_digests=(),
            provider_name='function',
        ),
        TextPart(content='Found.'),
    )
    continued_messages, _ = requests[1]
    continued_call, continued_return, _ = continued_messages[1].parts

    assert [type(tool) for tool in requests[0][1].model_request_parameters.native_tools] == [WebSearchTool]
    assert first.output == 'Found.'
    assert first.messages[-1].parts == native_parts
    assert first.history[-1].parts == native_parts
    assert second.output == 'Continued.'
    assert second.history[1].parts == native_parts
    assert continued_call == _NATIVE_CALL
    assert replace(continued_return, timestamp=_NATIVE_RETURN.timestamp) == _NATIVE_RETURN
    assert stream.result.output == 'Found.'
    assert stream.result.messages[-1].parts == native_parts
    assert [event for event in events if isinstance(event, ToolCallEvent | ToolResultEvent)] == []
    assert events[-1].kind == 'run_completed'


async def test_history_processor_receives_native_parts_and_returns_them_unchanged() -> None:
    received: list[tuple[AgentMessage, ...]] = []
    sent: list[list[ModelMessage]] = []

    async def record(messages: tuple[AgentMessage, ...]) -> tuple[AgentMessage, ...]:
        received.append(messages)

        return messages

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        sent.append(messages)
        if len(messages) > 1:
            return ModelResponse(parts=(PydanticTextPart('Continued.'),))

        return ModelResponse(parts=(_NATIVE_CALL, _NATIVE_RETURN))

    definition = _definition(HistoryProcessorCapability(id='history', processor=record))
    agent = await agent_factory({'primary': FunctionModel(respond)}).build(definition)

    first = await agent.run('Search.', deps=None)
    await agent.run('Continue.', deps=None, messages=first.history)

    processed_call, processed_return = sent[1][1].parts

    assert [type(part) for part in received[1][1].parts] == [NativeToolCallPart, NativeToolReturnPart]
    assert received[1][1].parts == first.history[1].parts
    assert processed_call == _NATIVE_CALL
    assert replace(processed_return, timestamp=_NATIVE_RETURN.timestamp) == _NATIVE_RETURN


async def test_response_with_a_generated_image_completes_with_a_file_part() -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info

        return ModelResponse(
            parts=(PydanticFilePart(_IMAGE, id='file-1', provider_name='function'), PydanticTextPart('Drawn.'))
        )

    agent = await agent_factory({'primary': FunctionModel(respond)}).build(_definition())

    result = await agent.run('Draw.', deps=None)

    assert result.output == 'Drawn.'
    assert result.messages[-1].parts == (
        FilePart(
            content=BinaryContent(data=base64.b64encode(_PNG), media_type='image/png'),
            id='file-1',
            provider_name='function',
        ),
        TextPart(content='Drawn.'),
    )


async def test_function_tool_that_returns_an_image_completes_and_streams_one_tool_result() -> None:
    requests: list[list[ModelMessage]] = []

    async def picture() -> ToolReturn:
        """Return a picture."""
        return ToolReturn(return_value=_IMAGE, content=['Look.', PydanticImageUrl('https://example.com/no-extension')])

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del info
        requests.append(messages)
        if len(messages) > 1:
            yield 'Seen.'
            return

        yield {0: DeltaToolCall(name='picture', json_args='{}', tool_call_id='call-1')}

    capability = pydantic_ai_capability(Toolset(FunctionToolset([picture]), id='pictures'))
    agent = await agent_factory({'primary': FunctionModel(stream_function=respond)}).build(_definition(capability))

    async with agent.stream('Show.', deps=None) as stream:
        events = [event async for event in stream]

    image_content = {
        'kind': 'binary',
        'data': base64.urlsafe_b64encode(_PNG).decode(),
        'media_type': 'image/png',
        'identifier': _IMAGE.identifier,
        'vendor_metadata': None,
    }
    tool_return, follow_up = stream.result.messages[2].parts
    sent_return, sent_follow_up = requests[1][2].parts

    assert stream.result.output == 'Seen.'
    assert [event.content for event in events if isinstance(event, ToolResultEvent)] == [image_content]
    assert tool_return == ToolReturnPart(
        tool_name='picture',
        content=image_content,
        tool_call_id='call-1',
        content_file_digests=(),
    )
    assert follow_up == UserPromptPart(
        content=(TextContent(content='Look.'), ImageUrl(url='https://example.com/no-extension'))
    )
    assert isinstance(sent_return, PydanticToolReturnPart)
    assert sent_return.content == _IMAGE
    assert isinstance(sent_follow_up, PydanticUserPromptPart)


async def test_multimodal_prompt_reaches_the_model_as_list_content_in_run_and_stream() -> None:
    prompts: list[object] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        prompts.append(messages[0].parts[0].content)

        return ModelResponse(parts=(PydanticTextPart('Described.'),))

    async def respond_stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del info
        prompts.append(messages[0].parts[0].content)
        yield 'Described.'

    model = FunctionModel(respond, stream_function=respond_stream)
    agent = await agent_factory({'primary': model}).build(_definition())

    result = await agent.run(_PROMPT, deps=None)
    async with agent.stream(list(_PROMPT), deps=None) as stream:
        async for _ in stream:
            pass

    expected = ['Describe these.', PydanticImageUrl('https://example.com/no-extension'), _IMAGE]

    assert prompts == [expected, expected]
    assert result.messages[0].parts == (UserPromptPart(content=_PROMPT),)
    assert stream.result.messages[0].parts == (UserPromptPart(content=_PROMPT),)
