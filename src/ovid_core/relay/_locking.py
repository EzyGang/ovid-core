import asyncio
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Concatenate, Protocol


class _LockOwner(Protocol):
    @property
    @abstractmethod
    def _lock(self) -> asyncio.Lock: ...


def locked[Owner: _LockOwner, **P, Result](
    function: Callable[Concatenate[Owner, P], Result],
) -> Callable[Concatenate[Owner, P], Awaitable[Result]]:
    @wraps(function)
    async def wrapped(self: Owner, *args: P.args, **kwargs: P.kwargs) -> Result:
        async with self._lock:
            return function(self, *args, **kwargs)

    return wrapped
