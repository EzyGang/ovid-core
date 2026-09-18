from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, cast

from ovid_core.errors import CodexAuthError


if TYPE_CHECKING:
    from ovid_core.codex.auth import CodexAuth


class _CodexLogin(ABC):
    def __init__(self, *, auth: CodexAuth, expected_revision: int) -> None:
        self._auth = auth
        self._expected_revision = expected_revision
        self._wait_task: asyncio.Task[None] | None = None
        self._cancelled = False
        self._finished = False

    async def cancel(self) -> None:
        if self._finished:
            return

        self._cancelled = True
        task = self._wait_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError, CodexAuthError:
                pass
        await self._finish()

    def _begin_wait(self) -> None:
        if self._finished or self._wait_task is not None:
            raise CodexAuthError('Codex login is not pending')

        self._wait_task = cast(asyncio.Task[None], asyncio.current_task())

    @abstractmethod
    async def _finish(self) -> None: ...
