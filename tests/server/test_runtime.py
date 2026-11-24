import asyncio
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiorpcx import Service, connect_rs, connect_ws

from electrumx import Controller, Env
from electrumx.lib.coins import ElectricCash
from electrumx.lib.server_base import ServerBase
from electrumx.server.block_processor import BlockProcessor
from electrumx.server.mempool import MemPool
from electrumx.server.session import SessionManager


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_DIRECTORY', str(tmp_path))
    monkeypatch.setenv('DAEMON_URL', 'http://user:pass@127.0.0.1:1/')
    monkeypatch.setenv('PEER_DISCOVERY', 'off')
    return Env(ElectricCash)


def test_startup_error_reaches_caller_without_waiting_for_shutdown():
    watchdog_fired = []

    class FailingServer(ServerBase):
        async def serve(self, shutdown_event):
            loop = asyncio.get_running_loop()

            def watchdog():
                watchdog_fired.append(True)
                shutdown_event.set()

            loop.call_later(0.5, watchdog)
            raise ValueError('startup failed')

    server = FailingServer(SimpleNamespace(loop_policy=None, allow_root=True))
    with pytest.raises(ValueError, match='startup failed'):
        asyncio.run(server.run())
    assert not watchdog_fired


def test_requested_shutdown_waits_for_cleanup():
    class StoppingServer(ServerBase):
        async def serve(self, shutdown_event):
            shutdown_event.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                self.cleaned_up = True

    server = StoppingServer(SimpleNamespace(loop_policy=None, allow_root=True))
    asyncio.run(server.run())
    assert server.cleaned_up


@pytest.mark.asyncio
async def test_controller_reaches_daemon_on_supported_runtime(env):
    class OfflineCoin(ElectricCash):
        DAEMON = Mock(side_effect=LookupError('daemon constructed'))

    env.coin = OfflineCoin
    controller = object.__new__(Controller)
    controller.env = env
    controller.logger = Mock()
    with pytest.raises(LookupError, match='daemon constructed'):
        await controller.serve(asyncio.Event())


@pytest.mark.asyncio
async def test_worker_failure_after_first_completion():
    async def fail_later():
        await asyncio.sleep(0)
        raise ValueError('worker failed')

    bp = SimpleNamespace(_first_open_dbs=AsyncMock(), height=0, logger=Mock(),
                         prefetcher=SimpleNamespace(main_loop=AsyncMock()),
                         _process_prefetched_blocks=fail_later, flush=AsyncMock())
    with pytest.raises(ValueError, match='worker failed'):
        await BlockProcessor.fetch_and_process_blocks(bp, asyncio.Event())
    bp.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_worker_does_not_hide_later_failure():
    async def fail_later(_):
        await asyncio.sleep(0)
        raise ValueError('refresh failed')

    cleaned = asyncio.Event()

    async def log_until_cancelled(_):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    pool = SimpleNamespace(_refresh_hashes=fail_later,
                           _refresh_histogram=AsyncMock(side_effect=asyncio.CancelledError),
                           _logging=log_until_cancelled)
    with pytest.raises(ValueError, match='refresh failed'):
        await MemPool.keep_synchronized(pool, asyncio.Event())
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_daemon_owns_session_on_running_loop(env):
    daemon = ElectricCash.DAEMON(ElectricCash, env.daemon_url)
    with warnings.catch_warnings():
        warnings.simplefilter('error', DeprecationWarning)
        async with daemon:
            session = daemon.session
            assert not session.closed
    assert session.closed and daemon.session is None


@pytest.mark.asyncio
async def test_daemon_accepts_json_with_charset(unused_tcp_port):
    async def reply(request):
        assert (await request.json())['method'] == 'getblockcount'
        assert request.headers['Authorization'].startswith('Basic ')
        return web.json_response({'result': 42, 'error': None})

    app = web.Application()
    app.router.add_post('/', reply)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, '127.0.0.1', unused_tcp_port).start()
        url = f'http://user:pass@127.0.0.1:{unused_tcp_port}/'
        async with ElectricCash.DAEMON(ElectricCash, url) as daemon:
            async with asyncio.timeout(1):
                assert await daemon.height() == 42
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['tcp', 'ws'])
@pytest.mark.parametrize('pause', [False, True])
async def test_listener_shutdown_with_active_client(env, unused_tcp_port, protocol, pause):
    service = Service.from_string(f'{protocol}://127.0.0.1:{unused_tcp_port}')
    env.services = [service]
    env.max_sessions = 1 if pause else 10
    header = bytes(range(ElectricCash.TRUNCATED_HEADER_SIZE))
    auxpow = b'auxpow fixture'
    db = Mock(db_height=0, raw_header=AsyncMock(return_value=header + auxpow))
    db.dynamic_header_len.return_value = len(header + auxpow)
    manager = SessionManager(env, db, Mock(backed_up_event=asyncio.Event()),
                             Mock(), Mock(), asyncio.Event())
    ready = asyncio.Event()
    ready.set()
    task = asyncio.create_task(manager.serve(AsyncMock(), ready))
    try:
        async with asyncio.timeout(5):
            await manager.server_listening.wait()
            connect = (connect_ws(f'ws://127.0.0.1:{service.port}', proxy=None)
                       if protocol == 'ws' else connect_rs('127.0.0.1', service.port))
            async with connect as client:
                version = await client.send_request('server.version', ['unit-test', '1.4'])
                assert version[1] == '1.4'
                features = await client.send_request('server.features')
                assert (features['protocol_min'], features['protocol_max']) == ('1.4', '1.4.2')
                assert features['genesis_hash'] == ElectricCash.GENESIS_HASH
                assert await client.send_request('blockchain.block.header', [0]) == header.hex()
                assert await client.send_request('blockchain.merged-mining.aux-pow-header', [0]) == {
                    'aux_pow_header': auxpow.hex()}
                assert await client.send_request('server.ping') is None
                assert len(manager.sessions) == 1
                assert (service not in manager.servers) == pause
                task.cancel()
                done, _ = await asyncio.wait([task], timeout=1)
                assert task in done, 'shutdown blocked on the active client'
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.shield(task)
                assert not manager.sessions
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
