from collections.abc import Sequence
from typing import Any

from ovid_core.errors import ServerConstructionError
from ovid_core.persistence import ConversationStore
from ovid_core.server.contracts import (
    AgentRegistration,
    ASGIApplication,
    AuthorizationCallback,
    LifecycleCallback,
    ReadinessCallback,
)
from ovid_core.server.models import ServerConfig


def create_agent_app(
    *,
    agents: Sequence[AgentRegistration[Any, Any]],
    authorize: AuthorizationCallback,
    config: ServerConfig = ServerConfig(),
    store: ConversationStore | None = None,
    readiness: ReadinessCallback | None = None,
    startup: LifecycleCallback | None = None,
    shutdown: LifecycleCallback | None = None,
) -> ASGIApplication:
    try:
        from ovid_core.adapters.starlette.app import create_starlette_app
    except ModuleNotFoundError as error:
        raise ServerConstructionError('Native server support requires the ovid-core server extra') from error

    return create_starlette_app(
        agents=agents,
        authorize=authorize,
        config=config,
        store=store,
        readiness=readiness,
        startup=startup,
        shutdown=shutdown,
    )


def serve(app: ASGIApplication, *, config: ServerConfig = ServerConfig()) -> None:
    try:
        import uvicorn
    except ModuleNotFoundError as error:
        raise ServerConstructionError('Native server launcher requires the ovid-core server extra') from error

    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        timeout_graceful_shutdown=config.shutdown_grace_seconds,
    )
