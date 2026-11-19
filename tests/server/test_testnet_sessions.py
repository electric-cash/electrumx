import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiorpcx import NetAddress, Request, Service, SessionKind
from websockets.exceptions import ConnectionClosedOK

from electrumx.lib.coins import ElectricCashTestnet
from electrumx.server.session import session_manager
from electrumx.server.session.session_manager import SessionManager


@pytest.fixture
def manager():
    env = SimpleNamespace(coin=ElectricCashTestnet, max_send=400000,
                          anon_logs=False, report_services=[])
    return SessionManager(env, None, None, None, None, asyncio.Event())


def connect(manager, kind='TCP'):
    transport = Mock(kind=SessionKind.SERVER)
    transport.remote_address.return_value = NetAddress('127.0.0.1', 60001)
    return ElectricCashTestnet.SESSIONCLS(manager, None, None, manager.peer_mgr,
                                          kind, transport)


@pytest.mark.asyncio
async def test_notification_ending_with_client_does_not_fail_supervisor(manager):
    session = connect(manager, kind='WS')
    session.subscribe_headers = True
    manager.hsub_results = {'height': 12, 'hex': '00' * 80}
    session.send_notification = AsyncMock(side_effect=ConnectionClosedOK(None, None))
    await session.notify(set(), True)
    session.send_notification.assert_awaited_once()

    session.send_notification.side_effect = ValueError('unexpected error')
    with pytest.raises(ValueError, match='unexpected error'):
        await session.notify(set(), True)


@pytest.mark.asyncio
async def test_testnet_staking_and_account_rpc(manager):
    manager.daemon = ElectricCashTestnet.DAEMON(ElectricCashTestnet, 'user:pass@localhost')
    rpc = manager.daemon._send_single = AsyncMock(side_effect=[{'remaining': 3}, {'power': 21}])
    session = connect(manager)
    send = session.handle_request
    # Preserve the existing combined response key used on this branch.
    result = await send(Request('blockchain.staking.calc_gp_and_free_tx_limit', [3, 15]))
    assert result == {'gp': 200, 'free_tx_imit': 600}
    assert await send(Request('blockchain.get_free_tx_info', ['account'])) == {'remaining': 3}
    assert await send(Request('blockchain.get_gov_power', ['account'])) == {'power': 21}
    assert [call.args for call in rpc.await_args_list] == [
        ('getfreetxinfo', ('account',)), ('getgovpower', ('account',))]
    await session.connection_lost()
    assert not manager.sessions


@pytest.mark.asyncio
async def test_drain_closes_existing_and_late_sessions(manager, monkeypatch):
    service = Service.from_string('ssl://127.0.0.1:50002')
    listener = manager.servers[service] = Mock()
    sessions = []
    closed = []

    async def close(session, *, force_after):
        assert force_after == 1
        closed.append(session)
        await session.connection_lost()

    monkeypatch.setattr(ElectricCashTestnet.SESSIONCLS, 'close', close)

    async def wait_closed():
        listener.close.assert_called_once_with()
        sessions.append(connect(manager))  # Handshake completes during drain.
        while manager.sessions:
            await asyncio.sleep(0)

    listener.wait_closed = wait_closed
    sessions.append(connect(manager))
    await asyncio.wait_for(manager._drain_on_shutdown(), 2)
    assert len(sessions) == 2
    assert set(closed) == set(sessions)
    assert not manager.servers and not manager.sessions


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['ssl', 'wss', 'tcp', 'ws', 'rpc'])
async def test_handshake_deadline_only_on_tls(manager, monkeypatch, protocol):
    manager.env.SSL_PROTOCOLS = {'ssl', 'wss'}
    tls = object()
    monkeypatch.setattr(manager, '_ssl_context', lambda: tls)
    serve = AsyncMock()
    name = 'serve_ws' if protocol in ('ws', 'wss') else 'serve_rs'
    monkeypatch.setattr(session_manager, name, serve)
    service = Service.from_string(f'{protocol}://127.0.0.1:60001')
    await manager._start_servers([service])
    assert manager.servers[service] is serve.return_value
    options = serve.await_args.kwargs
    if protocol in manager.env.SSL_PROTOCOLS:
        assert options == {'ssl': tls, 'ssl_handshake_timeout': 10}
    else:
        assert options == {'ssl': None}


@pytest.mark.asyncio
async def test_max_sessions_can_resume_before_paused_websocket_drains(manager):
    ws = Service.from_string('ws://127.0.0.1:60002')
    rpc = Service.from_string('rpc://127.0.0.1:60003')
    manager.env.max_sessions = 2
    listener = manager.servers[ws] = Mock()
    admin = manager.servers[rpc] = Mock()
    retiring, drained, resumed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def wait_closed():
        retiring.set()
        await drained.wait()

    listener.wait_closed = wait_closed
    manager._start_external_servers = AsyncMock(side_effect=resumed.set)
    first = connect(manager)
    second = connect(manager)
    async with asyncio.timeout(2), manager._task_group as group:
        supervisor = await group.spawn(manager._manage_servers)
        try:
            await retiring.wait()
            listener.close.assert_called_once_with(close_connections=False)
            assert set(manager.sessions) == {first, second}
            assert manager.servers == {rpc: admin}
            await first.connection_lost()
            await resumed.wait()
            assert manager._paused_listeners == {listener}
            admin.close.assert_not_called()
        finally:
            supervisor.cancel()
            drained.set()
    assert not manager._paused_listeners
    await second.connection_lost()
