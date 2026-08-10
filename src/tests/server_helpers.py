from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from starlette.applications import Starlette

from ovid_core.policy import AgentRunPolicy
from ovid_core.server.contracts import AgentRegistration, AuthorizationResult, RequestContext
from tests.agent_consumer import AgentDependencies, text_definition
from tests.agent_helpers import agent_factory


@asynccontextmanager
async def server_client(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        yield client


async def text_stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
    del messages, info
    yield 'Hello'
    yield ' server'


def text_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    del messages, info

    return ModelResponse(parts=[TextPart(content='Hello server')])


async def dependencies(context: RequestContext, authorization: AuthorizationResult) -> AgentDependencies:
    return AgentDependencies(prefix=f'{authorization.principal}:{context.method}')


async def allow(context: RequestContext, agent_id: str) -> AuthorizationResult:
    del agent_id

    return AuthorizationResult(allowed=context.header('authorization') == 'Bearer allowed', principal='user-1')


async def build_registration(*, policy: AgentRunPolicy = AgentRunPolicy()) -> AgentRegistration[AgentDependencies, str]:
    model = FunctionModel(function=text_response, stream_function=text_stream, model_name='server')
    definition = replace(text_definition(), policy=policy)
    agent = await agent_factory({'primary': model}).build(definition)

    return AgentRegistration(
        id='writer',
        description='Write a short response.',
        agent=agent,
        dependencies=dependencies,
    )
