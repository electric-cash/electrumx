import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from electrumx.lib.coins import ElectricCash
from electrumx.server.env import Env
from electrumx.server.peers import PeerManager
from electrumx.server.session import SessionManager


@pytest.fixture
def manager():
    env = SimpleNamespace(coin=ElectricCash, report_services=[], PD_ON='ON',
                          peer_discovery='ON', peer_announce=False, force_proxy=False)
    return PeerManager(env, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_worker', [False, True], ids=['shutdown', 'worker-error'])
async def test_admission_closes_before_peer_workers_are_cancelled(manager, fail_worker):
    ready, finish = asyncio.Event(), asyncio.Event()
    monitors_ready = asyncio.Event()
    monitored, cleaned = [], []
    failure = ValueError('maintenance failed')

    async def maintenance():
        ready.set()
        await finish.wait()
        raise failure

    async def monitor(peer):
        monitored.append(peer)
        if len(monitored) == 2:
            monitors_ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(peer)

    manager._refresh_blacklist = maintenance
    manager._detect_proxy = AsyncMock()
    manager._import_peers = AsyncMock()
    manager._monitor_peer = monitor

    admission_at_cancel = []
    cancel_remaining = manager.group.cancel_remaining

    async def observe_cancel():
        # An empty batch checks admission without creating any worker.
        admission_at_cancel.append(await manager._note_peers([]))
        await cancel_remaining()

    manager.group.cancel_remaining = observe_cancel
    admin = SimpleNamespace(peer_mgr=manager)
    before = 'first.example.com t50001'
    during = 'second.example.com t50002'
    assert await SessionManager.rpc_add_peer(admin, before) == f"peer '{before}' added"
    discovery = asyncio.create_task(manager.discover_peers())
    async with asyncio.timeout(5):
        await ready.wait()
        assert await SessionManager.rpc_add_peer(admin, during) == f"peer '{during}' added"
        await monitors_ready.wait()
        if fail_worker:
            finish.set()
            with pytest.raises(ValueError) as raised:
                await discovery
            assert raised.value is failure
        else:
            discovery.cancel()
            with pytest.raises(asyncio.CancelledError):
                await discovery

    assert admission_at_cancel and not any(admission_at_cancel)
    assert len(monitored) == 2 and set(cleaned) == set(monitored)
    assert manager.group.joined
    manager.group.spawn = AsyncMock()
    response = await SessionManager.rpc_add_peer(admin, 'third.example.com t50003')
    assert 'not added' in response
    manager.group.spawn.assert_not_awaited()
    assert manager.peers == set(monitored)


@pytest.mark.asyncio
async def test_normal_completion_closes_admission_after_startup_workers(manager):
    admission_at_import = []

    async def import_peers():
        admission_at_import.append(await manager._note_peers([]))

    manager._import_peers = import_peers
    manager._detect_proxy = AsyncMock()
    manager._refresh_blacklist = AsyncMock()
    await manager.discover_peers()
    assert admission_at_import == [True]
    assert not await manager._note_peers([])


@pytest.mark.asyncio
async def test_validated_peer_request_returns_admission_result(manager, monkeypatch):
    source = SimpleNamespace(host='192.0.2.1')
    resolve = AsyncMock(return_value=[(None, None, None, None, (source.host, 80))])
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    manager._note_peers = AsyncMock(return_value=False)
    features = {'hosts': {'peer.example.com': {'tcp_port': 50001}}}
    assert not await manager.on_add_peer(features, source)
    manager._note_peers.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('disabled, fail_monitor', [(False, False), (True, False), (False, True)],
                         ids=['before-mempool-ready', 'discovery-off', 'monitor-cleanup-error'])
async def test_serve_drains_unowned_admin_monitors(monkeypatch, tmp_path, disabled, fail_monitor):
    class Coin(ElectricCash):
        class SESSIONCLS(ElectricCash.SESSIONCLS):
            pass

    monkeypatch.setenv('DB_DIRECTORY', str(tmp_path))
    monkeypatch.setenv('DAEMON_URL', 'unused')
    monkeypatch.setenv('SERVICES', 'rpc://127.0.0.1:8000')
    monkeypatch.setenv('PEER_DISCOVERY', 'off' if disabled else 'on')
    env = Env(Coin)
    server = SessionManager(env, SimpleNamespace(db_height=0),
                            SimpleNamespace(backed_up_event=asyncio.Event()),
                            None, None, asyncio.Event())
    peers = server.peer_mgr
    rpc_ready, mempool_ready, monitor_ready = (asyncio.Event() for _ in range(3))
    listener = Mock(wait_closed=AsyncMock())
    failure = ValueError('admin monitor cleanup failed')
    cleanup = []

    async def start(services):
        for service in services:
            server.servers[service] = listener
            rpc_ready.set()

    async def monitor(peer):
        monitor_ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.append(await peers._note_peers([]))
            if fail_monitor:
                raise failure

    server._start_servers = start
    peers._monitor_peer = monitor
    discovery_done = asyncio.Event()
    discover = peers.discover_peers

    async def observe_discovery():
        await discover()
        discovery_done.set()

    peers.discover_peers = observe_discovery
    serving = asyncio.create_task(server.serve(AsyncMock(), mempool_ready))
    try:
        async with asyncio.timeout(5):
            await rpc_ready.wait()
            if disabled:
                mempool_ready.set()
                await discovery_done.wait()
            peer_name = 'first.example.com t50001'
            assert await server.rpc_add_peer(peer_name) == f"peer '{peer_name}' added"
            await monitor_ready.wait()
            serving.cancel()
            with pytest.raises(ValueError if fail_monitor else asyncio.CancelledError) as raised:
                await serving
            if fail_monitor:
                assert raised.value is failure
        assert cleanup == [False]
        assert peers.group.joined
        assert not await peers._note_peers([])
        listener.close.assert_called_once_with()
        listener.wait_closed.assert_awaited_once()
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        if not peers.group.joined:
            await peers.group.cancel_remaining()
            await peers.group.join()


@pytest.mark.asyncio
async def test_shutdown_leaves_discovery_owned_group_to_its_owner(manager):
    ready, finish = asyncio.Event(), asyncio.Event()

    async def maintenance():
        ready.set()
        await finish.wait()

    manager._refresh_blacklist = maintenance
    manager._detect_proxy = AsyncMock()
    manager._import_peers = AsyncMock()
    manager.group.cancel_remaining = AsyncMock(wraps=manager.group.cancel_remaining)
    manager.group.join = AsyncMock(wraps=manager.group.join)
    discovery = asyncio.create_task(manager.discover_peers())
    try:
        async with asyncio.timeout(5):
            await ready.wait()
            await manager.shutdown()
            assert not await manager._note_peers([])
            manager.group.cancel_remaining.assert_not_awaited()
            manager.group.join.assert_not_awaited()
            finish.set()
            await discovery
            await manager.shutdown()
            manager.group.join.assert_awaited_once()
    finally:
        finish.set()
        await discovery


@pytest.mark.asyncio
async def test_shutdown_skips_already_joined_group(manager):
    await manager.group.join()
    manager.group.cancel_remaining = AsyncMock()
    manager.group.join = AsyncMock()
    await manager.shutdown()
    assert not await manager._note_peers([])
    manager.group.cancel_remaining.assert_not_awaited()
    manager.group.join.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelling_unowned_drain_waits_for_monitor_cleanup(manager):
    started, cleaning, release = (asyncio.Event() for _ in range(3))
    cleaned = asyncio.Event()

    async def monitor(peer):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    manager._monitor_peer = monitor
    await manager.add_localRPC_peer('first.example.com t50001')
    async with asyncio.timeout(5):
        await started.wait()
        shutdown = asyncio.create_task(manager.shutdown())
        try:
            await cleaning.wait()
            shutdown.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
    assert cleaned.is_set() and manager.group.joined
