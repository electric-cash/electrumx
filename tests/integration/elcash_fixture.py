"""Deterministic Electric Cash mainnet wire fixtures; no ElectrumX imports.

Electric Cash Core 4dd632629de5360c2c2cef21316d0e1c458e60ef, src/chainparams.cpp:
CreateGenesisBlock and CMainParams supply the genesis script, reward and header fields.
All subsequent blocks are synthetic non-AuxPoW indexer inputs, not mined blocks.
Hashes and wallet oracles below are fixed independently of server responses.
"""

from hashlib import sha256
from struct import pack


NET = "mainnet"
TIMESTAMP, NONCE = 1608451200, 3970812118
GENESIS = "00000000a9811adc411f15a9c525d667ca467d83dc5461e2d7fc791d1d3926de"
GENESIS_TXID = "d3133f9c8d4261f44fbd2a8029c56d5d97106adfef11c652d971796cc75dd967"
TIP = "633d6d993d0925f88b534a1042be994726a410cb1e5318f91986e424cf5e704a"
HEIGHT = 101
FUNDING = "c39840ad2f301b74d740d31d68521efa34e24ccec38859367c386117b0a42519"
SPEND = "32ce50fc941f9638639388af871b0f2ab624c1f2d3f3ba21d5c2bcc6edb605e4"
SCRIPTHASH = "acf8ccb25d370ef50b838d10dd7ed37d407ba9803299d9f35bbd199458616b23"
WALLET = bytes.fromhex("76a9143bcd4048e767f561d2cd229c476874f015b544ac88ac")
BALANCE = {"confirmed": 761400000, "unconfirmed": 0}
# (txid, height, vout, sats)
UTXOS = [(FUNDING, 1, 1, 123400000), (SPEND, HEIGHT, 0, 638000000)]


def hash256(data):
    return sha256(sha256(data).digest()).digest()


def vector(data):
    # The fixture only uses one-byte CompactSize lengths.
    assert len(data) < 253
    return bytes([len(data)]) + data


def transaction(prev, index, script_sig, outputs):
    txin = prev + pack("<I", index) + vector(script_sig) + b"\xff" * 4
    txouts = b"".join(pack("<Q", sats) + vector(script) for sats, script in outputs)
    return pack("<IB", 1, 1) + txin + bytes([len(outputs)]) + txouts + bytes(4)


def coinbase(script, outputs):
    return transaction(bytes(32), 0xffffffff, script, outputs)


def chain():
    message = b"We'll aim to make crypto transfers so cheap (& fast) that only the rich will use banks."
    genesis_key = bytes.fromhex("021eeacfd38a03fa3eceea46694a2673f933348fa41a4633b5dffd7799154e26d9")
    genesis_tx = coinbase(bytes.fromhex("04ffff001d4c") + vector(message),
                          [(50000000000, vector(genesis_key) + b"\xac")])
    assert hash256(genesis_tx)[::-1].hex() == GENESIS_TXID
    assert sha256(WALLET).digest()[::-1].hex() == SCRIPTHASH

    blocks = {}
    prev = bytes(32)
    for height in range(HEIGHT + 1):
        if height == 0:
            txs = [genesis_tx]
        else:
            # Output 0 at height 1 is OP_TRUE and matures before it is spent.
            outputs = [(640000000, b"\x51"), (123400000, WALLET)] if height == 1 else [(1, b"\x51")]
            txs = [coinbase(bytes([1, height]), outputs)]
            if height == 1:
                assert hash256(txs[0])[::-1].hex() == FUNDING
            if height == HEIGHT:
                outputs = [(638000000, WALLET)]
                txs.append(transaction(bytes.fromhex(FUNDING)[::-1], 0, b"", outputs))
                assert hash256(txs[-1])[::-1].hex() == SPEND

        hashes = [hash256(tx) for tx in txs]
        merkle = hashes[0] if len(hashes) == 1 else hash256(b"".join(hashes))
        header = pack("<I32s32sIII", 1, prev, merkle, TIMESTAMP + height * 600,
                      0x1d00ffff, NONCE if height == 0 else 0)
        prev = hash256(header)
        blocks[prev[::-1].hex()] = header + bytes([len(txs)]) + b"".join(txs)
    assert next(iter(blocks)) == GENESIS and prev[::-1].hex() == TIP
    return blocks
