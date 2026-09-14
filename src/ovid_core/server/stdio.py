import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from ovid_core.persistence import ConversationStore
from ovid_core.server.contracts import AgentRegistration, AuthorizationCallback, CommandRegistration, LifecycleCallback
from ovid_core.server.models import ServerConfig
from ovid_core.server.registry import AgentRegistry
from ovid_core.server.runtime import _AgentServerRuntime, _server_lifespan
from ovid_core.server.stdio_connection import _StdioConnection, _StdioWrite


type _StdioRead = Callable[[int], Awaitable[bytes]]


class StdioAgentServer:
    def __init__(
        self,
        *,
        agents: Sequence[AgentRegistration[Any, Any]],
        commands: Sequence[CommandRegistration],
        authorize: AuthorizationCallback,
        config: ServerConfig,
        store: ConversationStore | None,
        startup: LifecycleCallback | None,
        shutdown: LifecycleCallback | None,
    ) -> None:
        self._agents = agents if isinstance(agents, AgentRegistry) else tuple(agents)
        self._commands = _command_map(commands)
        self._authorize = authorize
        self._config = config
        self._runtime = _AgentServerRuntime(agents=agents, authorize=authorize, config=config, store=store)
        self._startup = startup
        self._shutdown = shutdown

    async def run(self) -> None:
        await self._serve(reader=_read_stdin, writer=_write_stdout)

    async def _serve(self, *, reader: _StdioRead, writer: _StdioWrite) -> None:
        async with (
            _server_lifespan(
                startup=self._startup,
                shutdown=self._shutdown,
                shutdown_grace_seconds=self._config.shutdown_grace_seconds,
            ),
            asyncio.TaskGroup() as tasks,
        ):
            connection = _StdioConnection(
                agents=self._agents,
                commands=self._commands,
                authorize=self._authorize,
                config=self._config,
                runtime=self._runtime,
                writer=writer,
                tasks=tasks,
            )
            try:
                while line := await reader(self._config.max_body_bytes + 1):
                    if len(line) > self._config.max_body_bytes:
                        await connection.write_error(None, 'request_too_large', 'Request exceeds limit')
                        return
                    await connection.receive(line)
            finally:
                async with asyncio.timeout(self._config.shutdown_grace_seconds):
                    await self._runtime.close()


def create_stdio_server(
    *,
    agents: Sequence[AgentRegistration[Any, Any]],
    authorize: AuthorizationCallback,
    commands: Sequence[CommandRegistration] = (),
    config: ServerConfig = ServerConfig(),
    store: ConversationStore | None = None,
    startup: LifecycleCallback | None = None,
    shutdown: LifecycleCallback | None = None,
) -> StdioAgentServer:
    return StdioAgentServer(
        agents=agents,
        commands=commands,
        authorize=authorize,
        config=config,
        store=store,
        startup=startup,
        shutdown=shutdown,
    )


def _command_map(commands: Sequence[CommandRegistration]) -> dict[str, CommandRegistration]:
    mapped = {command.id: command for command in commands}

    if len(mapped) != len(commands):
        raise ValueError('command registration ids must be unique')

    return mapped


async def _read_stdin(limit: int) -> bytes:
    return await asyncio.to_thread(sys.stdin.buffer.readline, limit)


async def _write_stdout(payload: bytes) -> None:
    await asyncio.to_thread(_write_stdout_sync, payload)


def _write_stdout_sync(payload: bytes) -> None:
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
