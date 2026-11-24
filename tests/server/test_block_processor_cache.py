import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from electrumx.server import block_processor as mod


def block(header):
    return SimpleNamespace(header=header, transactions=())


@pytest.fixture
def bp(monkeypatch):
    clock = SimpleNamespace(time=lambda: 100)
    monkeypatch.setattr(mod, 'time', clock)
    monkeypatch.setattr(mod, '_CACHE_CHECK_BLOCKS', 4)
    proc = mod.BlockProcessor(Mock(cache_MB=5), Mock(), Mock(), Mock())
    proc.coin.GENESIS_ACTIVATION = 0
    proc.coin.header_hash.side_effect = lambda header: header
    proc.daemon.cached_height.return_value = 100
    proc.db.min_undo_height.return_value = 101
    proc.db.fs_height, proc.db.fs_tx_count, proc.db.tx_counts = -1, 0, []
    proc.db.history.unflushed_memsize.return_value = 0
    proc._caught_up_event = asyncio.Event()
    proc.next_cache_check = 113

    def flush(data, full, estimate):
        data.headers.clear()
        data.block_tx_hashes.clear()
        proc.db.fs_height, proc.db.fs_tx_count = data.height, data.tx_count

    proc.db.flush_dbs.side_effect = flush
    return proc


@pytest.mark.asyncio
async def test_fast_batches_check_growth_once_per_interval(bp):
    class NoRescan(list):
        def __iter__(self):
            raise AssertionError('rescanned previously accumulated headers')

    bp.headers = NoRescan()
    size = bp.db.history.unflushed_memsize
    for n in (3, 1, 1, 3):
        bp.advance_blocks([block(b'header')] * n)
        await bp._maybe_flush()
        assert size.call_count == len(bp.headers) // 4
    bp.db.flush_dbs.assert_not_called()
    assert bp.next_cache_check == 113


@pytest.mark.asyncio
@pytest.mark.parametrize('last_size,expected', [(99, None), (100, False)])
async def test_variable_header_bytes_change_flush_decision(bp, last_size, expected):
    # Four blocks at 42 bytes of tx-hash overhead each; threshold is exactly 1 MB.
    bp.db.history.unflushed_memsize.return_value = 1_000_000 - 4 * 42 - 106
    bp.advance_blocks([block(bytes(n)) for n in (1, 2, 3, last_size)])
    await bp._maybe_flush()
    if expected is None:
        bp.db.flush_dbs.assert_not_called()
        assert bp.pending_header_bytes == 105
    else:
        assert bp.db.flush_dbs.call_args.args[1] is expected
        assert not bp.headers and bp.pending_header_bytes == 0
        bp.advance_blocks([block(b'new')])
        await bp._maybe_flush()
        assert bp.pending_header_bytes == 3 and bp.db.flush_dbs.call_count == 1


@pytest.mark.asyncio
async def test_header_bytes_can_trigger_before_block_interval(bp):
    bp.advance_blocks([block(bytes(999_999))])
    await bp._maybe_flush()
    bp.db.history.unflushed_memsize.assert_not_called()
    bp.advance_blocks([block(b'x')])
    await bp._maybe_flush()
    assert bp.db.flush_dbs.call_args.args[1] is False
    assert not bp.headers and bp.pending_header_bytes == 0


@pytest.mark.asyncio
async def test_growth_check_keeps_full_utxo_threshold(bp):
    bp.advance_blocks([block(b'x')] * 4)
    # Existing estimate: 205 bytes per cached entry; 4 MB is 80% of CACHE_MB.
    entries = (4_000_000 + 204) // 205
    bp.db.history.unflushed_memsize.return_value = 1_000_000
    bp.utxo_cache = range(entries - 1)
    assert bp.check_cache_size() is False
    bp.utxo_cache = range(entries)
    await bp._maybe_flush()
    assert bp.db.flush_dbs.call_args.args[1] is True


@pytest.mark.asyncio
async def test_slow_sync_clock_and_caught_up_full_flush(bp):
    bp.advance_blocks([block(b'x')])
    bp.next_cache_check = 99
    await bp._maybe_flush()
    assert bp.next_cache_check == 130
    bp.db.history.unflushed_memsize.assert_called_once()
    bp.db.flush_dbs.assert_not_called()
    bp._caught_up_event.set()
    await bp._maybe_flush()
    assert bp.db.flush_dbs.call_args.args[1] is True
    assert bp.pending_header_bytes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('filesystem_done', [False, True])
async def test_flush_failure_tracks_only_retained_headers(bp, filesystem_done):
    bp.advance_blocks([block(b'abc')])

    def fail(data, full, estimate):
        if filesystem_done:
            data.headers.clear()
        raise OSError('flush failed')

    bp.db.flush_dbs.side_effect = fail
    with pytest.raises(OSError):
        await bp.flush(True)
    assert bp.pending_header_bytes == sum(map(len, bp.headers)) == (0 if filesystem_done else 3)


@pytest.mark.asyncio
async def test_reorg_restarts_growth_from_new_tip(bp):
    bp.advance_blocks([block(b'a'), block(b'bb'), block(b'ccc'), block(b'dddd')])
    await bp._maybe_flush()
    bp.reorg_hashes = AsyncMock(return_value=(3, 3, [bytes(32)]))
    bp.db.read_raw_block.return_value = block(b'dddd')
    bp.coin.block.side_effect = lambda raw, height: raw
    bp.coin.header_prevhash.return_value = b'ccc'
    bp.db.read_undo_info.return_value = b''
    bp.db.assert_flushed = Mock()
    bp.prefetcher.reset_height = AsyncMock()
    bp.db.flush_backup.side_effect = lambda data, touched: setattr(bp.db, 'fs_height', data.height)
    await bp.reorg_chain(1)
    assert bp.height == 2 and bp.pending_header_bytes == 0 and not bp.headers
    bp.advance_blocks([block(b'next')] * 4)
    await bp._maybe_flush()
    assert bp.pending_header_bytes == 16 and bp.db.history.unflushed_memsize.call_count == 2
