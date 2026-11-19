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


async def _drain_workers(group):
    # Let workers finish cleanup and surface their errors even on repeated cancellation.
    drain = asyncio.create_task(_cancel_and_check(group))
    interrupted = None
    while not drain.done():
        try:
            await asyncio.shield(drain)
        except asyncio.CancelledError as exc:
            interrupted = exc
    drain.result()
    return interrupted


async def cancel_and_join(group):
    """Stop an otherwise unsupervised group and observe worker failures before returning."""
    try:
        interrupted = await _drain_workers(group)
    finally:
        await group.join()
    if interrupted is not None:
        raise interrupted


@asynccontextmanager
async def task_group(group=None, *, on_closing=None):
    """Supervise workers without losing their exceptions in aiorpcX join()."""
    async with (TaskGroup() if group is None else group) as workers:
        try:
            try:
                yield workers
                await _raise_worker_errors(workers)
            finally:
                # Close external admission synchronously, before scheduling the drain.
                if on_closing is not None:
                    on_closing()
        except BaseException:
            await _drain_workers(workers)
            raise
