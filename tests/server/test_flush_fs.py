from array import array
from types import SimpleNamespace

import pytest

from electrumx.lib.util import LogicalFile
from electrumx.server import db as db_module
from electrumx.server.db import DB


def fixture(tmp_path, monkeypatch, dynamic=True):
    db = DB.__new__(DB)
    db.coin = SimpleNamespace(STATIC_BLOCK_HEADERS=not dynamic)
    db.utxo_db = SimpleNamespace(for_sync=False)
    db.fs_height, db.fs_tx_count = 0, 1
    db.tx_counts = array('Q', [1, 2, 2, 3, 4])
    for name in ('headers', 'tx_counts', 'hashes', 'headers_offsets'):
        setattr(db, name + '_file', LogicalFile(str(tmp_path / name), 2, 19))
    db.headers_file.write(0, b'old')
    if dynamic:
        db.header_offset = db.dynamic_header_offset
        db.headers_offsets_file.write(0, bytes(8) + (3).to_bytes(8, 'little'))
    else:
        db.header_offset = lambda height: height * 3
    db.tx_counts_file.write(0, db.tx_counts[:1].tobytes())
    db.hashes_file.write(0, b'O' * 32)
    headers = [b'a' * 7, b'b' * 9, bytes(range(41)), b'd'] if dynamic else [b'abc'] * 4
    data = SimpleNamespace(height=4, tx_count=4, headers=headers,
                           block_tx_hashes=[b'A' * 32, b'', b'B' * 32, b'C' * 32])
    monkeypatch.setattr(db_module, '_FS_WRITE_BATCH_SIZE', 16)
    return db, data


def assert_segments(path, prefix, expected):
    parts = sorted(path.glob(prefix + '[0-9][0-9]'))
    assert [part.read_bytes() for part in parts] == [
        expected[i:i + 19] for i in range(0, len(expected), 19)]


@pytest.mark.parametrize('dynamic', [False, True])
def test_exact_segment_bytes_and_bounded_writes(tmp_path, monkeypatch, dynamic):
    db, data = fixture(tmp_path, monkeypatch, dynamic)
    headers = list(data.headers)
    for file in (db.headers_file, db.headers_offsets_file, db.hashes_file):
        def bounded(offset, chunk, write=file.write):
            assert 0 < len(chunk) <= 16
            write(offset, chunk)
        monkeypatch.setattr(file, 'write', bounded)
    db.flush_fs(data)
    assert_segments(tmp_path, 'headers', b'old' + b''.join(headers))
    ends = [0, 3]
    for header in headers:
        ends.append(ends[-1] + len(header))
    offsets = b''.join(end.to_bytes(8, 'little') for end in ends)
    assert_segments(tmp_path, 'headers_offsets', offsets if dynamic else b'')
    assert_segments(tmp_path, 'tx_counts', db.tx_counts.tobytes())
    assert_segments(tmp_path, 'hashes', b'O' * 32 + b'A' * 32 + b'B' * 32 + b'C' * 32)
    assert (db.fs_height, db.fs_tx_count) == (4, 4)
    assert not data.headers and not data.block_tx_hashes


@pytest.mark.parametrize('name', ['headers', 'headers_offsets', 'tx_counts', 'hashes'])
def test_partial_write_retry(tmp_path, monkeypatch, name):
    db, data = fixture(tmp_path, monkeypatch)
    headers, hashes = list(data.headers), list(data.block_tx_hashes)
    file = getattr(db, name + '_file')
    write = file.write

    def failed(offset, chunk):
        write(offset, chunk[:1])
        raise OSError('incomplete write')

    monkeypatch.setattr(file, 'write', failed)
    with pytest.raises(OSError):
        db.flush_fs(data)
    assert (db.fs_height, db.fs_tx_count) == (0, 1)
    assert data.headers == headers and data.block_tx_hashes == hashes
    monkeypatch.setattr(file, 'write', write)
    db.flush_fs(data)
    assert_segments(tmp_path, 'headers', b'old' + b''.join(headers))
    assert_segments(tmp_path, 'headers_offsets',
                    b''.join(end.to_bytes(8, 'little') for end in [0, 3, 10, 19, 60, 61]))
    assert_segments(tmp_path, 'hashes', b'O' * 32 + b''.join(hashes))
    assert_segments(tmp_path, 'tx_counts', db.tx_counts.tobytes())


def test_invalid_hash_count_does_not_consume_input(tmp_path, monkeypatch):
    db, data = fixture(tmp_path, monkeypatch)
    data.block_tx_hashes[-1] = b'x'
    hashes = list(data.block_tx_hashes)
    with pytest.raises(AssertionError):
        db.flush_fs(data)
    assert data.block_tx_hashes == hashes and len(data.headers) == 4
    assert_segments(tmp_path, 'headers', b'old')
