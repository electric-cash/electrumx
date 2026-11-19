from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiorpcx import RPCError

from electrumx.lib.coins import ElectricCash


@pytest.fixture
def session():
    return SimpleNamespace(bump_cost=Mock(), daemon_request=AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize(('verbose', 'response'), [
    (False, 'cafebabe'), (True, 'cafebabe'),
    (True, {'hex': '00', 'blockhash': 'ff' * 32}),
    (True, {'hex': '00', 'blockhash': None}),
])
async def test_transaction_get(session, verbose, response):
    session.daemon_request.return_value = response
    txid = 'ff' * 32
    assert await ElectricCash.SESSIONCLS.transaction_get(session, txid, verbose) == response
    session.daemon_request.assert_awaited_once_with('getrawtransaction', txid, verbose)


@pytest.mark.asyncio
async def test_transaction_get_daemon_failure(session):
    session.daemon_request.side_effect = RPCError(1, 'backend error')
    with pytest.raises(RPCError, match='backend error'):
        await ElectricCash.SESSIONCLS.transaction_get(session, 'ff' * 32, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(('txid', 'verbose'), [('cafe', True), ('ff' * 32, 'true')])
async def test_transaction_get_invalid_params(session, txid, verbose):
    with pytest.raises(RPCError):
        await ElectricCash.SESSIONCLS.transaction_get(session, txid, verbose)
    session.daemon_request.assert_not_awaited()
