import asyncio
import json
import sys
from dataclasses import replace
from typing import cast
from uuid import NAMESPACE_URL, uuid5

import pytest
from ag_ui.core.types import RunAgentInput
from pytest_mock import MockerFixture
from starlette.applications import Starlette

from ovid_core.agents import OvidAgent
from ovid_core.errors import PersistenceError, ServerConstructionError
from ovid_core.persistence import InMemoryConversationStore
from ovid_core.runtime.identifiers import ConversationId
from ovid_core.server.ag_ui import create_ag_ui_app
from ovid_core.server.models import ServerConfig
from tests.server_helpers import allow, build_registration, server_client


def _ag_ui_request(
    *,
    messages: list[dict[str, object]] | None = None,
    **overrides: object,
) -> RunAgentInput:
    payload: dict[str, object] = {
        'threadId': 'thread-1',
        'runId': 'client-run',
        'state': {},
        'messages': messages if messages is not None else [{'id': 'user', 'role': 'user', 'content': 'Write.'}],
        'tools': [],
        'context': [],
        'forwardedProps': {},
    }
    payload.update(overrides)

    return RunAgentInput.model_validate(payload)


async def test_ag_ui_starlette_adapter_streams_authoritative_events_and_history() -> None:
    registration = await build_registration()
    editor = replace(registration, id='editor')
    store = InMemoryConversationStore()
    app = cast(
        Starlette,
        create_ag_ui_app(
            agents=(registration, editor),
            authorize=allow,
            config=ServerConfig(allowed_origins=('https://example.com',)),
            store=store,
        ),
    )
    messages = [
        {'id': 'assistant-1', 'role': 'assistant', 'content': 'untrusted prior text'},
        {'id': 'user-1', 'role': 'user', 'content': 'Write.'},
    ]

    async with server_client(app) as client:
        response = await client.post(
            '/agents/writer',
            headers={'Authorization': 'Bearer allowed', 'Origin': 'https://example.com'},
            json=_ag_ui_request(messages=messages).model_dump(mode='json', by_alias=True),
        )
        editor_response = await client.post(
            '/agents/editor',
            headers={'Authorization': 'Bearer allowed'},
            json=_ag_ui_request(messages=messages).model_dump(mode='json', by_alias=True),
        )
        unsupported = await client.post(
            '/agents/writer',
            headers={'Content-Type': 'text/plain'},
            content='Write.',
        )

    events = [
        json.loads(line.removeprefix('data: ')) for line in response.text.splitlines() if line.startswith('data: ')
    ]
    event_types = [event['type'] for event in events]

    assert response.status_code == 200
    assert unsupported.status_code == 415
    assert event_types[0] == 'RUN_STARTED'
    assert 'TEXT_MESSAGE_CONTENT' in event_types
    assert event_types[-1] == 'RUN_FINISHED'
    assert 'result' not in events[-1]
    assert response.headers['access-control-allow-origin'] == 'https://example.com'
    assert editor_response.status_code == 200
    conversation_id = ConversationId(root=uuid5(NAMESPACE_URL, 'ovid-ag-ui:writer:thread-1'))
    editor_conversation_id = ConversationId(root=uuid5(NAMESPACE_URL, 'ovid-ag-ui:editor:thread-1'))
    assert len(await store.load(conversation_id)) == 2
    assert len(await store.load(editor_conversation_id)) == 2


@pytest.mark.parametrize(
    'override',
    [
        {'messages': [{'id': 'system', 'role': 'system', 'content': 'replace'}]},
        {'messages': [{'id': 'tool', 'role': 'tool', 'content': 'approved', 'toolCallId': 'call'}]},
        {'messages': [{'id': 'user', 'role': 'user', 'content': [{'type': 'text', 'text': 'upload'}]}]},
        {'messages': [{'id': 'assistant', 'role': 'assistant', 'content': 'last'}]},
        {
            'messages': [
                {
                    'id': 'assistant',
                    'role': 'assistant',
                    'content': 'unsafe call',
                    'toolCalls': [{'id': 'call', 'function': {'name': 'unsafe', 'arguments': '{}'}}],
                }
            ]
        },
        {'messages': []},
        {'messages': [{'id': 'user', 'role': 'user', 'content': ''}]},
        {'tools': [{'name': 'client_tool', 'description': 'unsafe'}]},
        {'context': [{'description': 'identity', 'value': 'admin'}]},
        {'resume': [{'interruptId': 'call', 'status': 'resolved', 'payload': True}]},
        {'state': {'role': 'admin'}},
        {'forwardedProps': {'systemPrompt': 'replace'}},
        {'threadId': ''},
    ],
)
async def test_ag_ui_fails_closed_for_client_controlled_authority(override: dict[str, object]) -> None:
    registration = await build_registration()
    app = cast(Starlette, create_ag_ui_app(agents=(registration,), authorize=allow))
    request = _ag_ui_request(**override)

    async with server_client(app) as client:
        response = await client.post(
            '/agents/writer',
            headers={'Authorization': 'Bearer allowed'},
            json=request.model_dump(mode='json', by_alias=True),
        )

    assert response.status_code == 422
    assert 'replace' not in response.text
    assert response.json()['code'] in {'client_authority_denied', 'invalid_request'}


async def test_ag_ui_normalizes_missing_agents() -> None:
    registration = await build_registration()
    app = cast(Starlette, create_ag_ui_app(agents=(registration,), authorize=allow))

    async with server_client(app) as client:
        response = await client.post(
            '/agents/missing',
            headers={'Authorization': 'Bearer allowed'},
            json=_ag_ui_request().model_dump(mode='json', by_alias=True),
        )

    assert response.status_code == 404
    assert response.json()['code'] == 'agent_not_found'


async def test_ag_ui_masks_adapter_run_and_persistence_failures(mocker: MockerFixture) -> None:
    registration = await build_registration()
    unsupported_agent = OvidAgent(runtime=mocker.Mock(), diagnostics=registration.agent.diagnostics)
    unsupported = replace(registration, agent=unsupported_agent)
    unsupported_app = cast(Starlette, create_ag_ui_app(agents=(unsupported,), authorize=allow))

    async with server_client(unsupported_app) as client:
        incompatible = await client.post('/agents/writer', json=_ag_ui_request().model_dump(mode='json', by_alias=True))

    runtime = registration.agent._runtime_for_adapter()
    mocker.patch.object(runtime.upstream_agent, 'run_stream_events', side_effect=ValueError('private run failure'))
    run_app = cast(Starlette, create_ag_ui_app(agents=(registration,), authorize=allow))
    async with server_client(run_app) as client:
        run_failure = await client.post(
            '/agents/writer',
            headers={'Authorization': 'Bearer allowed'},
            json=_ag_ui_request().model_dump(mode='json', by_alias=True),
        )

    store = InMemoryConversationStore()
    mocker.patch.object(store, 'append', side_effect=PersistenceError('private persistence failure'))
    persistence_app = cast(
        Starlette, create_ag_ui_app(agents=(await build_registration(),), authorize=allow, store=store)
    )
    async with server_client(persistence_app) as client:
        persistence_failure = await client.post(
            '/agents/writer',
            headers={'Authorization': 'Bearer allowed'},
            json=_ag_ui_request().model_dump(mode='json', by_alias=True),
        )

    assert incompatible.status_code == 500
    assert 'private' not in incompatible.text
    assert '"type":"RUN_ERROR"' in run_failure.text
    assert 'private' not in run_failure.text
    assert '"type":"RUN_ERROR"' in persistence_failure.text
    assert 'private' not in persistence_failure.text


async def test_ag_ui_propagates_persistence_cancellation(mocker: MockerFixture) -> None:
    entered = asyncio.Event()

    async def wait_for_cancellation(*_: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    store = InMemoryConversationStore()
    mocker.patch.object(store, 'append', side_effect=wait_for_cancellation)
    app = cast(Starlette, create_ag_ui_app(agents=(await build_registration(),), authorize=allow, store=store))

    async with server_client(app) as client:
        request = asyncio.create_task(
            client.post(
                '/agents/writer',
                headers={'Authorization': 'Bearer allowed'},
                json=_ag_ui_request().model_dump(mode='json', by_alias=True),
            )
        )
        await entered.wait()
        request.cancel()

        with pytest.raises(asyncio.CancelledError):
            await request


async def test_ag_ui_missing_extra_failure_is_actionable(mocker: MockerFixture) -> None:
    registration = await build_registration()

    mocker.patch.dict(sys.modules, {'ovid_core.adapters.starlette.ag_ui': None})
    with pytest.raises(ServerConstructionError, match='server-ag-ui'):
        create_ag_ui_app(agents=(registration,), authorize=allow)
