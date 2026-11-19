import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from aiorpcx import Service

from electrumx import Env
from electrumx.lib.coins import ElectricCash
from electrumx.server.session import SessionManager


@pytest.fixture
def manager(monkeypatch, tmp_path):
    monkeypatch.setenv('DB_DIRECTORY', str(tmp_path))
    monkeypatch.setenv('DAEMON_URL', 'http://fixture:fixture@127.0.0.1:1/')
    monkeypatch.setenv('PEER_DISCOVERY', 'off')
    env = Env(ElectricCash)
    return SessionManager(env, Mock(db_height=0), Mock(backed_up_event=asyncio.Event()),
                          Mock(), Mock(), asyncio.Event())


def register_client(manager, release=None):
    client = Mock(start_time=manager.start_time, cost=0)
    client.closing = asyncio.Event()

    async def close(*, force_after):
        client.closing.set()
        if release is not None:
            await release.wait()
        manager.remove_session(client)

    client.close = AsyncMock(side_effect=close)
    manager.add_session(client)
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['tcp', 'ws'])
@pytest.mark.parametrize('paused', [False, True])
async def test_late_registration_drained_only_on_shutdown(manager, protocol, paused):
    service = Service.from_string(f'{protocol}://127.0.0.1:8001')
    manager.env.services = [service]
    manager.env.max_sessions = 1 if paused else 10
    listener_closed = asyncio.Event()
    transport_drained = asyncio.Event()
    release_close = asyncio.Event()
    listener = Mock()
    listener.close.side_effect = lambda **kwargs: listener_closed.set()
    listener.wait_closed = AsyncMock(side_effect=transport_drained.wait)

    async def start(services):
        for item in services:
            assert item == service
            manager.servers[item] = listener

    manager._start_servers = start
    ready = asyncio.Event()
    ready.set()
    server = asyncio.create_task(manager.serve(AsyncMock(), ready))
    try:
        async with asyncio.timeout(3):
            await manager.server_listening.wait()
            existing = register_client(manager)
            if paused:
                await listener_closed.wait()
                listener.close.assert_called_once_with(
                    **({'close_connections': False} if protocol == 'ws' else {}))
                # A previously accepted client is still usable during a capacity pause.
                before_shutdown = register_client(manager)
                await asyncio.sleep(0)
                before_shutdown.close.assert_not_called()
                existing.close.assert_not_called()

            server.cancel()
            await existing.closing.wait()  # the initial session snapshot has been taken
            late = register_client(manager, release_close)
            await late.closing.wait()
            transport_drained.set()
            await asyncio.sleep(0)
            assert not server.done()  # must also join the late client's close coroutine
            release_close.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.shield(server)
            late.close.assert_awaited_once_with(force_after=1)
            existing.close.assert_awaited_once_with(force_after=1)
            if paused:
                before_shutdown.close.assert_awaited_once_with(force_after=1)
            assert not manager.sessions
    finally:
        transport_drained.set()
        release_close.set()
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)


@pytest.mark.asyncio
async def test_resume_does_not_wait_for_paused_listener_to_drain(manager):
    service = Service.from_string('ws://127.0.0.1:8001')
    manager.env.max_sessions = 2
    old = Mock()
    draining, drained, resumed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def wait_closed():
        draining.set()
        await drained.wait()

    old.wait_closed = wait_closed
    manager.servers[service] = old
    manager._start_external_servers = AsyncMock(side_effect=resumed.set)
    first, second = register_client(manager), register_client(manager)
    try:
        async with manager._task_group as group:
            worker = await group.spawn(manager._manage_servers)
            try:
                async with asyncio.timeout(3):
                    await draining.wait()
                    manager.remove_session(first)
                    await resumed.wait()
                    assert not drained.is_set()
                    second.close.assert_not_called()
            finally:
                drained.set()
                worker.cancel()
    finally:
        manager.remove_session(second)
    assert not manager._paused_listeners


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['tcp', 'ssl', 'ws', 'wss'])
async def test_only_tls_listeners_bound_pending_handshakes(manager, monkeypatch, protocol):
    service = Service.from_string(f'{protocol}://127.0.0.1:8001')
    start = AsyncMock()
    serve = 'serve_ws' if protocol in ('ws', 'wss') else 'serve_rs'
    monkeypatch.setattr('electrumx.server.session.' + serve, start)
    monkeypatch.setattr(manager, '_ssl_context', Mock(return_value=object()))
    await manager._start_servers([service])
    options = start.await_args.kwargs
    if protocol in ('ssl', 'wss'):
        assert 0 < options['ssl_handshake_timeout'] <= 10
    else:
        assert 'ssl_handshake_timeout' not in options
