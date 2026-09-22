"""Skip, rather than fail, when the backing services are not running.

Most of `test_api.py` talks to a real Postgres and a real Redis. Those are
worth testing against the real thing, but it means a fresh clone cannot run
the whole suite without bringing `k8s/` up first.

A failure and an unmet prerequisite are different results, and reporting the
second as the first makes a working project look broken. These tests skip
when nothing is listening, so the suite reports what it actually checked.

Bring the dependencies up with the manifests in `k8s/`, then port-forward:

    kubectl -n agentops port-forward svc/postgres-db 5432:5432
    kubectl -n agentops port-forward svc/redis 6379:6379
    kubectl -n agentops port-forward svc/temporal 7233:7233
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import pytest


CONNECT_TIMEOUT_SECONDS = 1.0


def _listening(host: str, port: int) -> bool:
    connection = socket.socket()
    connection.settimeout(CONNECT_TIMEOUT_SECONDS)
    try:
        connection.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        connection.close()


def _postgres_endpoint() -> tuple[str, int]:
    return (
        os.getenv("POSTGRES_HOST", "127.0.0.1"),
        int(os.getenv("POSTGRES_PORT", "5432")),
    )


def _redis_endpoint() -> tuple[str, int]:
    parsed = urlparse(os.getenv("REDIS_URL", "redis://127.0.0.1:6379"))
    return parsed.hostname or "127.0.0.1", parsed.port or 6379


# Checked once per session: a port that is down at collection time is not
# going to come up midway through, and one probe per test would cost more
# than the tests do.
POSTGRES_UP = _listening(*_postgres_endpoint())
REDIS_UP = _listening(*_redis_endpoint())


def pytest_collection_modifyitems(config, items) -> None:
    """Skip the tests whose dependencies are not reachable.

    `test_api.py` is the only module that needs them. The manifest, Temporal
    and recovery tests run against files and fakes, so they are left alone and
    a fresh clone still gets meaningful coverage.
    """
    if POSTGRES_UP and REDIS_UP:
        return

    missing = ", ".join(
        name
        for name, up in (("Postgres", POSTGRES_UP), ("Redis", REDIS_UP))
        if not up
    )
    skip = pytest.mark.skip(
        reason=f"{missing} not reachable; see tests/conftest.py to start it"
    )
    for item in items:
        if item.fspath.basename == "test_api.py":
            item.add_marker(skip)
