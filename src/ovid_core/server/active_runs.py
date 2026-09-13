import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from ovid_core.runtime.identifiers import RunId
from ovid_core.server.contracts import AuthorizationResult


class _ConnectionOwner:
    pass


@dataclass(slots=True)
class _ActiveRun:
    run_id: RunId
    agent_id: str
    done: asyncio.Future[None]
    owner: _ConnectionOwner | None = None
    authorization: AuthorizationResult | None = None
    task: asyncio.Task[Any] | None = None
    cancelled: bool = False
    terminal: bool = False


class _ActiveRuns:
    def __init__(self) -> None:
        self._runs: dict[RunId, _ActiveRun] = {}

    def start(
        self,
        agent_id: str,
        owner: _ConnectionOwner | None,
        tasks: asyncio.TaskGroup,
        execute: Callable[[_ActiveRun], Awaitable[None]],
    ) -> _ActiveRun:
        active = self._create(agent_id, owner)
        coroutine = self._execute(active, execute)

        try:
            task = tasks.create_task(coroutine, eager_start=True)
        except BaseException:
            coroutine.close()
            self._finish(active)
            raise
        task.add_done_callback(lambda _: self._finish(active))

        return active

    async def _execute(self, active: _ActiveRun, execute: Callable[[_ActiveRun], Awaitable[None]]) -> None:
        async with self._bind(active):
            await execute(active)

    @asynccontextmanager
    async def run(self, agent_id: str) -> AsyncIterator[_ActiveRun]:
        active = self._create(agent_id, None)
        async with self._bind(active):
            yield active

    @asynccontextmanager
    async def _bind(self, active: _ActiveRun) -> AsyncIterator[None]:
        active.task = asyncio.current_task()
        try:
            yield
        finally:
            self._finish(active)

    def _create(self, agent_id: str, owner: _ConnectionOwner | None) -> _ActiveRun:
        active = _ActiveRun(RunId.new(), agent_id, asyncio.get_running_loop().create_future(), owner)
        self._runs[active.run_id] = active
        return active

    def _finish(self, active: _ActiveRun) -> None:
        self._runs.pop(active.run_id, None)
        if not active.done.done():
            active.done.set_result(None)

    def cancel(self, run_id: RunId, agent_id: str, principal: str) -> None:
        active = self._runs.get(run_id)
        if (
            active is not None
            and active.agent_id == agent_id
            and active.owner is None
            and active.authorization is not None
            and active.authorization.principal == principal
        ):
            self._cancel(active)

    async def cancel_owned(self, run_id: RunId, owner: _ConnectionOwner) -> None:
        active = self._runs.get(run_id)
        if active is not None and active.owner is owner and not active.terminal:
            self._cancel(active)
            await asyncio.shield(active.done)

    async def disconnect(self, active: _ActiveRun) -> None:
        if self._runs.get(active.run_id) is active:
            self._cancel(active, disconnected=True)
            await asyncio.shield(active.done)

    def _cancel(self, active: _ActiveRun, *, disconnected: bool = False) -> None:
        if disconnected or (not active.cancelled and not active.terminal):
            active.cancelled = True
            cast(asyncio.Task[Any], active.task).cancel()

    async def close(self) -> None:
        active_runs = tuple(self._runs.values())
        for active in active_runs:
            self._cancel(active)
        await asyncio.gather(*(asyncio.shield(active.done) for active in active_runs))
