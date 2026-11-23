import base64
import json
from collections.abc import AsyncIterator

import pytest
from pydantic import JsonValue, ValidationError
from pydantic_ai.messages import BinaryImage, ModelMessage
from pydantic_ai.messages import ImageUrl as PydanticImageUrl
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pytest_mock import MockerFixture

from ovid_core import InMemoryConversationStore
from ovid_core.messages import BinaryContent, ImageUrl, TextContent, UserPromptPart
from ovid_core.runtime import ConversationId
from ovid_core.server import (
    AgentRunRequest,
    AuthorizationResult,
    RequestContext,
    ServerConfig,
    StdioRunRequest,
    create_agent_app,
    create_stdio_server,
)
from tests.server.server_helpers import allow, build_registration, server_client
from tests.server.test_stdio_server import _frame, _payloads, _serve_frames


_PNG = b'\x89PNG\xfb\xff'
_PROMPT = (
    TextContent(content='Describe these.'),
    ImageUrl(url='https://example.com/no-extension'),
    BinaryContent(data=base64.b64encode(_PNG), media_type='image/png'),
)
_UPSTREAM_PROMPT = [
    'Describe these.',
    PydanticImageUrl('https://example.com/no-extension'),
    BinaryImage(_PNG, media_type='image/png'),
]
_UNSUPPORTED_PROMPT: list[JsonValue] = [{'kind': 'text', 'content': 'Describe.'}, {'kind': 'cache-point'}]
_OVERSIZED_PROMPT = (BinaryContent(data=base64.b64encode(b'x' * 4_000), media_type='image/png'),)


def _recording_model(prompts: list[object]) -> FunctionModel:
    async def respond(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del info
        prompts.append(messages[0].parts[-1].content)
        yield 'Described.'

    return FunctionModel(stream_function=respond, model_name='server')


@pytest.mark.parametrize(
    'prompt',
    (
        '',
        [],
        _UNSUPPORTED_PROMPT,
        [{'kind': 'image_url', 'url': 'https://example.com/a.png', 'force_download': 'allow-local'}],
        [{'kind': 'binary', 'data': 'not base64', 'media_type': 'image/png'}],
    ),
    ids=('empty-text', 'no-items', 'unsupported-kind', 'local-download', 'invalid-base64'),
)
def test_run_request_rejects_prompts_outside_the_typed_content_model(prompt: JsonValue) -> None:
    with pytest.raises(ValidationError):
        AgentRunRequest.model_validate_json(json.dumps({'prompt': prompt}))


async def test_native_http_server_runs_a_multimodal_prompt_and_keeps_its_request_guards() -> None:
    prompts: list[object] = []
    registration = await build_registration(model=_recording_model(prompts))
    store = InMemoryConversationStore()
    app = create_agent_app(
        agents=(registration,),
        authorize=allow,
        config=ServerConfig(max_body_bytes=2_000),
        store=store,
    )
    conversation_id = ConversationId.new()
    headers = {'Authorization': 'Bearer allowed', 'Content-Type': 'application/json'}

    async with server_client(app) as client:
        accepted = await client.post(
            '/agents/writer/events',
            headers=headers,
            content=AgentRunRequest(prompt=_PROMPT, conversation_id=conversation_id).model_dump_json(),
        )
        unsupported = await client.post(
            '/agents/writer/events',
            headers=headers,
            content=json.dumps({'prompt': _UNSUPPORTED_PROMPT}),
        )
        oversized = await client.post(
            '/agents/writer/events',
            headers=headers,
            content=AgentRunRequest(prompt=_OVERSIZED_PROMPT).model_dump_json(),
        )

    persisted = await store.load(conversation_id)

    assert 'event: run_result' in accepted.text
    assert prompts == [_UPSTREAM_PROMPT]
    assert persisted[0].parts[-1] == UserPromptPart(content=_PROMPT)
    assert unsupported.status_code == 422
    assert unsupported.json()['code'] == 'invalid_request'
    assert oversized.status_code == 413


async def test_stdio_server_runs_a_multimodal_prompt_and_keeps_its_request_guards(mocker: MockerFixture) -> None:
    prompts: list[object] = []
    registration = await build_registration(model=_recording_model(prompts))

    async def authorize(_: RequestContext, __: str) -> AuthorizationResult:
        return AuthorizationResult(allowed=True)

    server = create_stdio_server(
        agents=(registration,),
        authorize=authorize,
        config=ServerConfig(max_body_bytes=2_000),
    )
    unsupported = {
        'version': 1,
        'request_id': 'unsupported',
        'type': 'run',
        'agent_id': 'writer',
        'request': {'prompt': _UNSUPPORTED_PROMPT},
    }
    oversized = StdioRunRequest(
        request_id='oversized',
        agent_id='writer',
        request=AgentRunRequest(prompt=_OVERSIZED_PROMPT),
    )

    writer = await _serve_frames(
        server,
        mocker,
        'run',
        (
            f'{json.dumps(unsupported)}\n'.encode(),
            _frame(StdioRunRequest(request_id='run', agent_id='writer', request=AgentRunRequest(prompt=_PROMPT))),
        ),
        ending=_frame(oversized),
    )

    payloads = _payloads(writer)
    errors = [payload['error']['code'] for payload in payloads if payload['type'] == 'error']
    (result,) = (payload for payload in payloads if payload['type'] == 'run_result')

    assert prompts == [_UPSTREAM_PROMPT]
    assert result['result']['messages'][0]['parts'][-1] == UserPromptPart(content=_PROMPT).model_dump(mode='json')
    assert errors == ['invalid_request', 'request_too_large']
