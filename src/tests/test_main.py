import pytest


@pytest.mark.asyncio
async def test_asyncio_runs() -> None:
    assert bool(1) is True


def test_sync_runs() -> None:
    assert bool(1) is True
