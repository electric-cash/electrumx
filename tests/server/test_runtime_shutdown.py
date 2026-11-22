import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from electrumx.lib.server_base import ServerBase
from electrumx.server.block_processor import BlockProcessor


@pytest.mark.asyncio
async def test_server_failure_reaches_runner():
    server = object.__new__(ServerBase)
    server.logger = Mock()
    server.serve = AsyncMock(side_effect=RuntimeError("startup failed"))
    with pytest.raises(RuntimeError, match="startup failed"):
        await asyncio.wait_for(server.run(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("write_fails", [False, True])
@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_shutdown_waits_for_index_write(write_fails, repeat_cancel):
    bp = object.__new__(BlockProcessor)
    bp.height = 11
    bp.state_lock = asyncio.Lock()
    bp.logger = Mock()
    bp._first_open_dbs = AsyncMock()
    stopping = asyncio.Event()

    async def prefetch(height):
        try:
            await asyncio.Event().wait()
        finally:
            stopping.set()

    bp.prefetcher = SimpleNamespace(main_loop=prefetch)
    started = asyncio.Event()
    finished = threading.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    flushed = []

    def write():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        if write_fails:
            raise OSError("index write failed")
        finished.set()

    async def flush(utxos):
        assert finished.is_set() and not bp.state_lock.locked()
        flushed.append(utxos)

    bp._process_prefetched_blocks = lambda: bp.run_in_thread_with_lock(write)
    bp.flush = flush
    supervisor = asyncio.create_task(bp.fetch_and_process_blocks(asyncio.Event()))
    try:
        await asyncio.wait_for(started.wait(), 2)
        supervisor.cancel()
        await asyncio.wait_for(stopping.wait(), 2)
        if repeat_cancel:
            supervisor.cancel()
            await asyncio.sleep(0)
        assert bp.state_lock.locked() and not supervisor.done()
    finally:
        release.set()
        with pytest.raises(OSError if write_fails else asyncio.CancelledError):
            await asyncio.wait_for(supervisor, 2)
    assert flushed == ([] if write_fails else [True])


@pytest.mark.asyncio
async def test_worker_failure_after_prefetch_returns_does_not_flush():
    prefetched = asyncio.Event()

    async def process():
        await prefetched.wait()
        raise ValueError("failed indexing")

    bp = SimpleNamespace(height=0, _first_open_dbs=AsyncMock(), flush=AsyncMock(),
                         prefetcher=SimpleNamespace(main_loop=AsyncMock(
                             side_effect=lambda _: prefetched.set())),
                         _process_prefetched_blocks=process)
    with pytest.raises(ValueError, match="failed indexing"):
        await BlockProcessor.fetch_and_process_blocks(bp, asyncio.Event())
    bp.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_reorg_flushes_each_backup_before_releasing_lock():
    bp = object.__new__(BlockProcessor)
    bp.logger = Mock()
    bp.state_lock = asyncio.Lock()
    bp.touched = {None, b'address'}
    bp.flush = AsyncMock()
    bp.reorg_hashes = AsyncMock(return_value=(12, 12, [bytes(32)]))
    bp.height = 12
    bp.prefetcher = SimpleNamespace(reset_height=AsyncMock())
    bp.backed_up_event = Mock()
    bp.flush_data = lambda: bp.height
    bp.db = Mock()
    bp.db.read_raw_block.return_value = b'block'
    phases = []

    def backup(blocks):
        assert blocks == [b'block']
        bp.height -= 1
        phases.append('backed up')

    def persist(height, touched):
        assert height == 11 and touched == {b'address'}
        phases.append('persisted')

    bp.backup_blocks = backup
    bp.db.flush_backup = persist
    locked = bp.run_in_thread_with_lock

    async def run_batch(func, *args):
        await locked(func, *args)
        phases.append('released')

    bp.run_in_thread_with_lock = run_batch
    await bp.reorg_chain(1)
    assert phases == ['backed up', 'persisted', 'released']
    bp.prefetcher.reset_height.assert_awaited_once_with(11)
    bp.backed_up_event.set.assert_called_once_with()
