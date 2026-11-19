FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS base
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade zlib1g \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --only-binary=:all: pip==26.3

FROM base AS builder
WORKDIR /build
COPY setup.py setup.cfg LICENCE electrumx_server electrumx_rpc electrumx_compact_history ./
COPY electrumx ./electrumx
RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM base
# The ELCASH deployment uses LevelDB. Its Linux wheel includes the native library.
COPY docker/requirements-runtime.txt /tmp/requirements-runtime.txt
WORKDIR /source
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements-runtime.txt /wheels/*.whl \
    && python -m pip check \
    && python -c "import plyvel" \
    && ln -s /usr/local/bin/electrumx_rpc /source/electrumx_rpc \
    && ln -s /usr/local/bin/electrumx_server /source/electrumx_server \
    && ln -s /usr/local/bin/electrumx_compact_history /source/electrumx_compact_history \
    && mkdir -p /db

VOLUME /db
ENV ALLOW_ROOT=1 \
    DB_DIRECTORY=/db \
    COIN=ElectricCash \
    NET=mainnet \
    DB_ENGINE=leveldb \
    HOST="" \
    SSL_CERTFILE="" \
    SSL_KEYFILE="" \
    SERVICES="ssl://:50002,tcp://:50001" \
    COST_HARD_LIMIT=500000 \
    COST_LIMIT_SOFT=100000 \
    INITIAL_CONCURRENT=500 \
    BANDWIDTH_UNIT_COST=100000 \
    MAX_SEND=10000000 \
    CACHE_MB=2000 \
    MAX_SESSIONS=999999

CMD ["/usr/local/bin/electrumx_server"]
