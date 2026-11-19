import asyncio
from contextlib import asynccontextmanager

from aiorpcx import TaskGroup


async def _raise_worker_errors(group):
    async for worker in group:
        if not worker.cancelled():
            worker.result()


async def _cancel_and_check(group):
    await group.cancel_remaining()
    await _raise_worker_errors(group)


@asynccontextmanager
async def task_group(group=None):
    """Supervise workers without losing their exceptions in aiorpcX join()."""
    async with (TaskGroup() if group is None else group) as workers:
        try:
            yield workers
            await _raise_worker_errors(workers)
        except BaseException:
            # Repeated shutdown requests must not mask an indexing failure or
            # interrupt observation of a write already running in an executor.
            drain = asyncio.create_task(_cancel_and_check(workers))
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    pass
            drain.result()
            raise
