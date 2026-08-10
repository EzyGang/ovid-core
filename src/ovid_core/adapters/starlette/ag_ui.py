from collections.abc import AsyncIterator, Sequence
from functools import partial
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ovid_core.adapters.pydantic_ai.ag_ui import AGUIAuthorityError, AGUINativeEvent, PydanticAIAGUIRun
from ovid_core.adapters.starlette.http import (
    ClientAuthorityError,
    _apply_cors,
    _error_response,
    read_json_body,
    request_context,
)
from ovid_core.persistence import ConversationStore
from ovid_core.server.contracts import AgentRegistration, AuthorizationCallback, RequestContext
from ovid_core.server.models import ServerConfig
from ovid_core.server.runtime import _AgentServerRuntime


def create_starlette_ag_ui_app(
    *,
    agents: Sequence[AgentRegistration[Any, Any]],
    authorize: AuthorizationCallback,
    config: ServerConfig,
    store: ConversationStore | None,
) -> Starlette:
    runtime = _AgentServerRuntime(agents=agents, authorize=authorize, config=config, store=store)
    app = Starlette(
        routes=[Route('/agents/{agent_id:str}', partial(_run, runtime=runtime, config=config), methods=['POST'])]
    )
    _apply_cors(app, config.allowed_origins)

    return app


async def _run(request: Request, *, runtime: _AgentServerRuntime, config: ServerConfig) -> Response:
    agent_id = request.path_params['agent_id']

    try:
        body = await read_json_body(
            request,
            max_body_bytes=config.max_body_bytes,
            timeout_seconds=config.request_timeout_seconds,
        )
        integration = PydanticAIAGUIRun(
            agent=runtime.agent(agent_id),
            agent_id=agent_id,
            body=body,
            accept=request.headers.get('accept'),
        )
    except AGUIAuthorityError:
        return _error_response(ClientAuthorityError())
    except Exception as error:
        return _error_response(error)

    native_events = _native_events(
        runtime=runtime,
        integration=integration,
        agent_id=agent_id,
        context=request_context(request),
    )

    return integration.streaming_response(integration.stream(native_events, on_complete=runtime.persist))


async def _native_events[Deps, Output](
    *,
    runtime: _AgentServerRuntime,
    integration: PydanticAIAGUIRun[Deps, Output],
    agent_id: str,
    context: RequestContext,
) -> AsyncIterator[AGUINativeEvent]:
    async with runtime.session(agent_id, integration.conversation_id, context) as session:
        async for event in integration.native_stream(
            deps=session.deps,
            messages=session.messages,
            conversation_id=session.conversation_id,
        ):
            yield event
