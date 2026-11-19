# Test tooling belongs in a child image, not in the installed server.
ARG RUNTIME_IMAGE=elcash-electrumx:testnet-local
FROM ${RUNTIME_IMAGE}

WORKDIR /checks
COPY docker/requirements-test.txt .
RUN python -m pip install --no-cache-dir --only-binary=:all: -r requirements-test.txt \
    && python -m pip check
COPY tests tests
CMD ["python", "-m", "pytest", "-q", "--asyncio-mode=auto", "-o", "asyncio_default_fixture_loop_scope=function", "--cov=electrumx", "--ignore=tests/integration", "tests"]
