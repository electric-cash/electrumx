import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from aiorpcx import TaskTimeout
from websockets.exceptions import ConnectionClosed

from electrumx.lib.coins import ElectricCash
from electrumx.lib.taskgroup import task_group
from electrumx.server.controller import Notifications


@pytest.mark.asyncio
async def test_simple_mempool():
    n = Notifications()
    notified = []
    async def notify(height, touched):
        notified.append((height, touched))
    await n.start(5, notify)

    mtouched = {'a', 'b'}
    btouched = {'b', 'c'}
    await n.on_mempool(mtouched, 6)
    assert notified == [(5, set())]
    await n.on_block(btouched, 6)
    assert notified == [(5, set()), (6, set.union(mtouched, btouched))]


@pytest.mark.asyncio
async def test_enter_mempool_quick_blocks_2():
    n = Notifications()
    notified = []
    async def notify(height, touched):
        notified.append((height, touched))
    await n.start(5, notify)

    # Suppose a gets in block 6 and blocks 7,8 found right after and
    # the block processer processes them together.
    await n.on_mempool({'a'}, 5)
    assert notified == [(5, set()), (5, {'a'})]
    # Mempool refreshes with daemon on block 6
    await n.on_mempool({'a'}, 6)
    assert notified == [(5, set()), (5, {'a'})]
    # Blocks 6, 7 processed together
    await n.on_block({'a', 'b'}, 7)
    assert notified == [(5, set()), (5, {'a'})]
    # Then block 8 processed
    await n.on_block({'c'}, 8)
    assert notified == [(5, set()), (5, {'a'})]
    # Now mempool refreshes
    await n.on_mempool(set(), 8)
    assert notified == [(5, set()), (5, {'a'}), (8, {'a', 'b', 'c'})]


@pytest.fixture
def client():
    session = object.__new__(ElectricCash.SESSIONCLS)
    session.logger = Mock()
    session.subscribe_headers = True
    session.subscribe_headers_result = AsyncMock(return_value={'height': 5})
    session.hashX_subs = {b'a': 'alias-a', b'b': 'alias-b'}
    session.mempool_statuses = {}
    session.subscription_address_status = AsyncMock(return_value='status')
    session.send_notification = AsyncMock()
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [TaskTimeout(1), ConnectionClosed(None, None)])
@pytest.mark.parametrize('height_changed', [True, False], ids=['header', 'scripthash'])
async def test_client_send_failure_is_local(client, error, height_changed):
    client.send_notification.side_effect = error
    async with task_group() as group:
        await group.spawn(client.notify, set(client.hashX_subs), height_changed)
        sibling = await group.spawn(AsyncMock(return_value='delivered'))

    assert sibling.result() == 'delivered'
    client.send_notification.assert_awaited_once()
    method, _ = client.send_notification.await_args.args
    assert method == ('blockchain.headers.subscribe' if height_changed
                      else 'blockchain.scripthash.subscribe')


@pytest.mark.asyncio
@pytest.mark.parametrize(('operation', 'error'), [
    ('send_notification', RuntimeError('unexpected send failure')),
    ('send_notification', asyncio.CancelledError()),
    ('subscription_address_status', TaskTimeout(1)),
])
async def test_notify_preserves_other_failures(client, operation, error):
    getattr(client, operation).side_effect = error
    with pytest.raises(type(error)) as caught:
        await client.notify(set(client.hashX_subs), False)
    assert caught.value is error
