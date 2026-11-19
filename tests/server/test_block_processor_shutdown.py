import asyncio
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from electrumx.lib.taskgroup import task_group
from electrumx.server.block_processor import BlockProcessor


@pytest.mark.asyncio
async def test_cancelled_waiter_never_starts_a_db_write():
    bp = BlockProcessor(Mock(), Mock(), Mock(), Mock())
    write = Mock()
    async with bp.state_lock:
        waiter = asyncio.create_task(bp.run_in_thread_with_lock(write))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    # A subsequent lock acquisition must not race an orphaned writer.
    await bp.run_in_thread_with_lock(lambda: None)
    write.assert_not_called()


@pytest.mark.asyncio
async def test_orderly_processing_completion_flushes_once():
    bp = BlockProcessor(Mock(), Mock(), Mock(), Mock())
    bp._first_open_dbs = AsyncMock()
    bp.prefetcher.main_loop = AsyncMock()
    bp._process_prefetched_blocks = AsyncMock()
    bp.flush = AsyncMock()
    await bp.fetch_and_process_blocks(asyncio.Event())
    bp.flush.assert_awaited_once_with(True)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'writer_error', 'flush_error', 'reorg'])
async def test_shutdown_during_locked_write(outcome):
    bp = BlockProcessor(Mock(), Mock(), Mock(), Mock())
    bp._first_open_dbs = AsyncMock()
    bp.height, bp.tip = 7, b'old'
    loop = asyncio.get_running_loop()
    started, stopping = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    error = ValueError(outcome)
    writes = []

    def write(*args):
        assert bp.state_lock.locked()
        bp.tip = b'partial'
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        if outcome == 'writer_error':
            raise error
        bp.height += -1 if outcome == 'reorg' else 1
        bp.tip = b'complete'

    def flush(data, full, estimate):
        assert bp.state_lock.locked() and full
        if outcome == 'flush_error':
            raise error
        writes.append(('flush', data.height, data.tip))

    async def prefetch(height):
        try:
            await asyncio.Event().wait()
        finally:
            stopping.set()

    bp.db.flush_dbs.side_effect = flush
    bp.prefetcher.main_loop = prefetch
    if outcome == 'reorg':
        bp.reorg_hashes = AsyncMock(return_value=(7, 7, [bytes(32)]))
        bp.backup_blocks = write
        bp.db.flush_backup.side_effect = lambda data, touched: writes.append(
            ('backup', data.height, data.tip))
        bp._process_prefetched_blocks = lambda: bp.reorg_chain(1)
    else:
        bp._process_prefetched_blocks = lambda: bp.run_in_thread_with_lock(write)

    # Use the same parent supervision as Controller.serve.
    async def supervise():
        async with task_group() as group:
            await group.spawn(bp.fetch_and_process_blocks(asyncio.Event()))

    supervisor = asyncio.create_task(supervise())
    try:
        await asyncio.wait_for(started.wait(), 3)
        supervisor.cancel('operator shutdown')
        await asyncio.wait_for(stopping.wait(), 3)
        assert bp.state_lock.locked()
        assert not supervisor.done()
        assert writes == ([('flush', 7, b'old')] if outcome == 'reorg' else [])
        release.set()
        if outcome.endswith('_error'):
            with pytest.raises(ValueError) as caught:
                await supervisor
            assert caught.value is error
            if outcome == 'writer_error':
                bp.db.flush_dbs.assert_not_called()
            else:
                bp.db.flush_dbs.assert_called_once()
        else:
            with pytest.raises(asyncio.CancelledError, match='operator shutdown'):
                await supervisor
            if outcome == 'reorg':
                assert writes == [('flush', 7, b'old'), ('backup', 6, b'complete'),
                                  ('flush', 6, b'complete')]
            else:
                assert writes == [('flush', 8, b'complete')]
    finally:
        release.set()
        if not supervisor.done():
            supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)
