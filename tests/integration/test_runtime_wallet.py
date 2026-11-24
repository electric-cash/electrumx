"""Offline test of the installed Electric Cash mainnet server, from this worktree:

docker run --rm --pull=never --network none --mount "type=bind,source=$PWD/tests/integration,target=/smoke,readonly" --entrypoint python3 elcash-electrumx:mainnet-local -B /smoke/test_runtime_wallet.py

For qualification substitute the local image ID for the tag. No published ports,
source overlays, external Core, credentials, persistent DB or Docker socket needed.
The image must provide electrumx_server and openssl (for a temporary TLS identity).
"""

import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import elcash_fixture as fx


HOST = "127.0.0.1"


class CoreRPC(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, req):
        method, params = req["method"], req.get("params", [])
        core = self.server
        error = result = None
        if method == "getblockcount" and not params:
            result = fx.HEIGHT
        elif method == "getblockhash" and len(params) == 1 and 0 <= params[0] <= fx.HEIGHT:
            result = core.hashes[params[0]]
        elif (method == "getblock" and len(params) == 2 and params[1] is False
              and params[0] in core.blocks):
            core.fetched.append(params[0])
            result = core.blocks[params[0]].hex()
        elif method == "getrawmempool" and not params:
            result = []
        else:
            core.unexpected.append((method, params))
            error = {"code": -32601, "message": "unexpected fixture request"}
        return {"id": req["id"], "result": result, "error": error}

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        reply = [self.reply(item) for item in req] if isinstance(req, list) else self.reply(req)
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@contextmanager
def core_stub():
    with ThreadingHTTPServer((HOST, 0), CoreRPC) as core:
        core.blocks = fx.chain()
        core.hashes = list(core.blocks)
        core.fetched, core.unexpected = [], []
        thread = threading.Thread(target=core.serve_forever, daemon=True)
        thread.start()
        try:
            yield core
        finally:
            core.shutdown()
            thread.join()


def wallet_ports():
    with socket.socket() as tcp, socket.socket() as tls:
        tcp.bind((HOST, 0))
        tls.bind((HOST, 0))
        return tcp.getsockname()[1], tls.getsockname()[1]


def connect(proc, port):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"ElectrumX exited with status {proc.returncode} before serving")
        try:
            return socket.create_connection((HOST, port), timeout=5)
        except ConnectionRefusedError:
            time.sleep(0.1)
    raise TimeoutError(f"wallet port {port} did not become ready")


def tls_identity(tmp):
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(cert)],
                   capture_output=True, check=True, timeout=15)
    return cert, key


@unittest.skipUnless(__name__ == "__main__" or os.getenv("ELCASH_WALLET_SMOKE") == "1",
                     "run explicitly inside the candidate image")
class WalletSmoke(unittest.TestCase):
    def check_wallet(self, stream, header):
        seq = 0

        def rpc(method, *params):
            nonlocal seq
            seq += 1
            data = {"jsonrpc": "2.0", "id": seq, "method": method, "params": params}
            stream.write(json.dumps(data).encode() + b"\n")
            stream.flush()
            reply = json.loads(stream.readline())
            self.assertEqual(reply["id"], seq, method)
            self.assertIsNone(reply.get("error"), (method, reply))
            return reply["result"]

        software, protocol = rpc("server.version", "elcash-offline", "1.4.2")
        self.assertRegex(software, r"^ElectrumX \S+")
        self.assertEqual(protocol, "1.4.2")
        self.assertEqual(rpc("server.features")["genesis_hash"], fx.GENESIS)
        self.assertEqual(rpc("blockchain.headers.subscribe"), {"height": fx.HEIGHT, "hex": header})
        self.assertEqual(rpc("blockchain.scripthash.get_history", fx.SCRIPTHASH),
                         [{"height": 1, "tx_hash": fx.FUNDING}, {"height": fx.HEIGHT, "tx_hash": fx.SPEND}])
        self.assertEqual(rpc("blockchain.scripthash.get_balance", fx.SCRIPTHASH), fx.BALANCE)
        fields = ("tx_hash", "height", "tx_pos", "value")
        expected = [dict(zip(fields, row)) for row in fx.UTXOS]
        self.assertEqual(rpc("blockchain.scripthash.listunspent", fx.SCRIPTHASH), expected)

    def boot(self, core, db, cert, key):
        tcp, tls = wallet_ports()
        env = dict(os.environ, COIN="ElectricCash", NET=fx.NET, DB_DIRECTORY=str(db),
                   DB_ENGINE="leveldb", ALLOW_ROOT="1", PEER_DISCOVERY="off", REPORT_SERVICES="",
                   DAEMON_URL=f"http://fixture:fixture@{HOST}:{core.server_port}/",
                   SERVICES=f"tcp://{HOST}:{tcp},ssl://{HOST}:{tls}",
                   SSL_CERTFILE=str(cert), SSL_KEYFILE=str(key), PYTHONUNBUFFERED="1")
        header = core.blocks[fx.TIP][:80].hex()
        ctx = ssl.create_default_context(cafile=str(cert))
        with tempfile.TemporaryFile() as log:
            proc = subprocess.Popen(["electrumx_server"], cwd=str(db), env=env,
                                    stdout=log, stderr=subprocess.STDOUT)
            try:
                with connect(proc, tcp) as sock, sock.makefile("rwb") as stream:
                    self.check_wallet(stream, header)
                with connect(proc, tls) as sock, ctx.wrap_socket(sock, server_hostname=HOST) as secure:
                    with secure.makefile("rwb") as stream:
                        self.check_wallet(stream, header)
                self.assertIsNone(proc.poll())
                proc.terminate()
                self.assertEqual(proc.wait(timeout=15), 0)
                log.seek(0)
                output = log.read().decode(errors="replace")
                self.assertIn("shutdown complete", output)
                self.assertIn("ElectrumX server terminated normally", output)
                self.assertNotIn("Traceback (most recent call last)", output)
            except BaseException:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                log.seek(0)
                print(f"Core raw fetches: {len(core.fetched)}; unexpected: {core.unexpected}",
                      log.read().decode(errors="replace")[-8000:], sep="\n", file=sys.stderr)
                raise

    def test_funded_tcp_and_tls_before_and_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp, core_stub() as core:
            db = Path(tmp) / "db"
            db.mkdir()
            cert, key = tls_identity(Path(tmp))
            self.boot(core, db, cert, key)
            self.assertCountEqual(core.fetched, core.hashes)
            for store in ("hist", "utxo"):
                self.assertTrue((db / store / "CURRENT").is_file(), store)
            core.fetched.clear()
            self.boot(core, db, cert, key)
            self.assertEqual(core.fetched, [], "persisted index should not refetch any raw blocks")
            self.assertEqual(core.unexpected, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
