import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx2
import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from starlette.applications import Starlette

from ovid_core import InMemoryConversationStore
from ovid_core.runtime import ConversationId, RunId
from ovid_core.server import AgentRegistration, AgentRunRequest, AgentRunResponse, create_ag_ui_app, create_agent_app
from ovid_core.tools import ToolCallApproved
from ovid_core.usage import Usage
from tests.server.server_helpers import allow, build_registration, server_client
from tests.support.deferred_consumer import DangerTool, ScriptedModel, deferred_registration


_HEADERS = {'Authorization': 'Bearer allowed'}


def _sse(response: httpx2.Response) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


async def _post(client: httpx2.AsyncClient, request: AgentRunRequest) -> list[dict[str, Any]]:
    response = await client.post(
        '/agents/writer/events',
        headers={**_HEADERS, 'Content-Type': 'application/json'},
        content=request.model_dump_json(exclude_none=True),
    )
    return _sse(response)


async def test_native_http_resumes_deferred_run() -> None:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'))
    danger = DangerTool()
    store = InMemoryConversationStore()
    app = create_agent_app(agents=(await deferred_registration(model, (danger,)),), authorize=allow, store=store)
    conversation_id = ConversationId.new()

    async with server_client(app) as client:
        deferred = await _post(client, AgentRunRequest(prompt='Delete.', conversation_id=conversation_id))
        completed = await _post(
            client,
            AgentRunRequest(answers={'c1': ToolCallApproved()}, conversation_id=conversation_id),
        )

    assert [event['kind'] for event in deferred][-4:] == ['tool_deferred', 'usage_update', 'run_deferred', 'run_result']
    assert deferred[-1]['output'] is None
    assert [call['tool_call_id'] for call in deferred[-1]['requests']['calls']] == ['c1']
    assert completed[-1]['kind'] == 'run_result'
    assert set(completed[-1]) == {'kind', 'output', 'messages', 'usage', 'run_id', 'conversation_id'}
    assert danger.calls == [(1, True)]
    assert await store.load_deferred_requests(conversation_id) is None


async def test_conversation_admits_one_active_run() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del info
        first = messages[0]
        if isinstance(first, ModelRequest) and any(
            isinstance(part, UserPromptPart) and part.content == 'Wait.' for part in first.parts
        ):
            entered.set()
            await release.wait()
        yield 'done'

    registration = await build_registration(model=FunctionModel(stream_function=stream, model_name='blocking'))
    store = InMemoryConversationStore()
    app = create_agent_app(agents=(registration,), authorize=allow, store=store)
    busy_id = ConversationId.new()

    async with server_client(app) as client:
        first = asyncio.create_task(_post(client, AgentRunRequest(prompt='Wait.', conversation_id=busy_id)))
        await entered.wait()
        busy_prompt = await _post(client, AgentRunRequest(prompt='Again.', conversation_id=busy_id))
        busy_resume = await _post(
            client,
            AgentRunRequest(answers={'c1': ToolCallApproved()}, conversation_id=busy_id),
        )
        other = await _post(client, AgentRunRequest(prompt='Go.', conversation_id=ConversationId.new()))
        release.set()
        completed = await first

    assert busy_prompt[-1] == {
        'kind': 'server_error',
        'code': 'conversation_busy',
        'message': busy_prompt[-1]['message'],
        'run_id': completed[-1]['run_id'],
    }
    assert busy_resume[-1]['code'] == 'conversation_busy'
    assert other[-1]['kind'] == 'run_result'
    assert completed[-1]['kind'] == 'run_result'
    assert len(await store.load(busy_id)) == 2


async def test_ag_ui_deferral_fails_with_typed_error() -> None:
    model = ScriptedModel(('danger', {'value': 1}, 'c1'))
    danger = DangerTool()
    registration: AgentRegistration[Any, str] = await deferred_registration(model, (danger,))
    app = cast(Starlette, create_ag_ui_app(agents=(registration,), authorize=allow))
    body = {
        'threadId': 'thread-1',
        'runId': 'client-run',
        'state': {},
        'messages': [{'id': 'user', 'role': 'user', 'content': 'Delete.'}],
        'tools': [],
        'context': [],
        'forwardedProps': {},
    }

    async with server_client(app) as client:
        response = await client.post('/agents/writer', headers=_HEADERS, json=body)

    assert '"type":"RUN_ERROR"' in response.text
    assert 'The AG-UI bridge does not support deferred tool calls' in response.text
    assert danger.calls == []


def test_run_request_requires_prompt_or_answers_and_completed_response_is_unchanged() -> None:
    conversation_id = ConversationId.new()
    response = AgentRunResponse(
        output='done',
        messages=(),
        usage=Usage(),
        run_id=RunId.new(),
        conversation_id=conversation_id,
    )

    assert AgentRunRequest(answers={'c1': ToolCallApproved()}, conversation_id=conversation_id).prompt is None
    with pytest.raises(ValidationError, match='requires a prompt, tool answers, or resume'):
        AgentRunRequest(conversation_id=conversation_id)
    with pytest.raises(ValidationError, match='conversation ID'):
        AgentRunRequest(answers={'c1': ToolCallApproved()})
    with pytest.raises(ValidationError):
        AgentRunRequest.model_validate({'answers': {'c1': {'kind': 'approved', 'override_args': {}}}})
    assert set(json.loads(response.model_dump_json())) == {'output', 'messages', 'usage', 'run_id', 'conversation_id'}
