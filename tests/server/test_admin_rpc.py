import json
import runpy
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import aiorpcx


def test_cli_sends_named_parameters_with_client_framer(monkeypatch, capsys):
    session = Mock(spec=['send_request'])
    session.send_request = AsyncMock(return_value={'ok': True})
    client = AsyncMock()
    client.__aenter__.return_value = session
    connect = Mock(return_value=client)
    monkeypatch.setattr(aiorpcx, 'connect_rs', connect)
    monkeypatch.setattr(sys, 'argv', ['electrumx_rpc', '-p', '60000', 'reorg', '2'])
    script = Path(__file__).resolve().parents[2] / 'electrumx_rpc'
    if not script.is_file():
        installed = shutil.which('electrumx_rpc')
        assert installed, 'electrumx_rpc not found in the checkout or on PATH'
        script = Path(installed)
    assert runpy.run_path(str(script))['main']() == 0
    assert connect.call_args.args == ('localhost', 60000)
    assert connect.call_args.kwargs['framer'].max_size == 0
    session.send_request.assert_awaited_once_with('reorg', {'count': 2})
    assert json.loads(capsys.readouterr().out) == {'ok': True}
