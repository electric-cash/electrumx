from asyncio import CancelledError
from contextlib import asynccontextmanager

from aiorpcx import TaskGroup


async def _check_results(tasks):
    async for task in tasks:
        if not task.cancelled():
            task.result()


@asynccontextmanager
async def task_group(group=None):
    """Propagate worker errors; aiorpcX join() alone only cancels siblings."""
    async with (group if group is not None else TaskGroup()) as tasks:
        try:
            yield tasks
            await _check_results(tasks)
        except CancelledError:
            # A worker may fail while finishing a write during shutdown.
            # Let that error reach the supervisor instead of losing it in join().
            await tasks.cancel_remaining()
            await _check_results(tasks)
            raise
