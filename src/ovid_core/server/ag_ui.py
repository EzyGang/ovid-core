from collections.abc import Sequence
from typing import Any

from ovid_core.errors import ServerConstructionError
from ovid_core.persistence import ConversationStore
from ovid_core.server.contracts import AgentRegistration, ASGIApplication, AuthorizationCallback
from ovid_core.server.models import ServerConfig


def create_ag_ui_app(
    *,
    agents: Sequence[AgentRegistration[Any, Any]],
    authorize: AuthorizationCallback,
    config: ServerConfig = ServerConfig(),
    store: ConversationStore | None = None,
) -> ASGIApplication:
    try:
        from ovid_core.adapters.starlette.ag_ui import create_starlette_ag_ui_app
    except ModuleNotFoundError as error:
        raise ServerConstructionError('AG-UI support requires the ovid-core server-ag-ui extra') from error

    return create_starlette_ag_ui_app(agents=agents, authorize=authorize, config=config, store=store)
