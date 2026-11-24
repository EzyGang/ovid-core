import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from ovid_core.server.contracts import LifecycleCallback


@asynccontextmanager
async def _server_lifespan(
    *,
    startup: LifecycleCallback | None,
    shutdown: LifecycleCallback | None,
    shutdown_grace_seconds: int,
    close: LifecycleCallback | None = None,
) -> AsyncGenerator[None]:
    if startup is not None:
        await startup()

    try:
        yield
    finally:
        async with asyncio.timeout(shutdown_grace_seconds):
            try:
                if close is not None:
                    await close()
            finally:
                if shutdown is not None:
                    await shutdown()
