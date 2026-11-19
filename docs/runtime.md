# Electric Cash mainnet runtime

Build from this worktree. The root Dockerfile installs ElectrumX as a wheel
and takes its runtime dependencies from `docker/requirements-runtime.txt`.
The qualification target is Linux amd64, CPython 3.12 and LevelDB. The local
tag identifies the source tree; it does not select the chain. Use
`COIN=ElectricCash` and `NET=mainnet` explicitly for a configured server.

## Reproduce the PR checks

Run from the repository root with Docker set to Linux containers:

```sh
docker build -t elcash-electrumx:mainnet-local .
docker run --rm --pull=never --network none --mount "type=bind,source=${PWD}/tests/integration,target=/smoke,readonly" --entrypoint python3 elcash-electrumx:mainnet-local -B /smoke/test_runtime_wallet.py
docker build -f docker/unit.Dockerfile --build-arg RUNTIME_IMAGE=elcash-electrumx:mainnet-local -t elcash-electrumx:mainnet-unit .
docker run --rm --pull=never --network none --ulimit nofile=65536:65536 elcash-electrumx:mainnet-unit
```

The wallet test starts the installed server against a loopback Core RPC stub
and a fresh LevelDB. It checks the mainnet genesis, header, history, balance
and unspent outputs over TCP and verified TLS, then restarts the process and
reads the same index without fetching the raw blocks again.
As a separate check, the PR job starts the image with its default command and
no `DAEMON_URL`; only exit code 1 with `required envvar DAEMON_URL` passes.

The child image adds the tools in `docker/requirements-test.txt` and copies
the tests, not application source. Pytest discovers the unit suite, including
the Electric Cash address vectors, block and transaction JSON fixtures, and
server tests. Coverage is measured on the installed package. The file limit
makes the existing maximum-session test independent of the Docker host.
Only `tests/integration` is excluded from pytest: the wallet harness runs
directly in the unmodified runtime above. Flake8 remains informational.

The fixture is not a consensus validator or a live node. These checks do not
qualify historical database upgrades, public endpoints, optional RocksDB or
other coin backends. They neither publish an image nor deploy a release.

## Historical build and CI files

[`contrib/Dockerfile`](../contrib/Dockerfile) is the historical upstream
BitcoinSV/RocksDB example, not the supported Electric Cash image. Use the
[root Dockerfile](../Dockerfile) for ELCASH.
[`.travis.yml`](../.travis.yml) describes the obsolete Python 3.7-era
pipeline with LevelDB, RocksDB and optional coin libraries; it is not a
qualification run for Python 3.12.

The [storage tests](../tests/server/test_storage.py) still run against each
installed backend. The ELCASH PR image installs only LevelDB, so no RocksDB
cases are collected there. No backend tests are removed or filtered; a
RocksDB qualification would need a separate image with that binding.
