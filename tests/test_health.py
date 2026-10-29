from unittest.mock import MagicMock

from fastapi import status

from app.db import get_session


# testing healthz
def test_health(client):
    response = client.get("/healthz")
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status":"ok"}

# testing readyz
def test_readiness(client, vectors):
    response = client.get("/readyz")
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status":"ok"}


def test_readiness_ignores_the_vector_store(client, vectors):
    """qdrant is shared: failing readiness on it would pull every pod at once and turn
    a search outage into a total one."""
    vectors.down = True
    response = client.get("/readyz")
    assert response.status_code == status.HTTP_200_OK


# testing /readyz failure 
def test_readiness_failure(client):
    def broken_session():
        mock_db = MagicMock()
        mock_db.execute.side_effect = Exception("Database service not available")
        return mock_db
    # simulate the failure using mock db
    client.app.dependency_overrides[get_session] = broken_session

    try:
        response = client.get("/readyz")
        
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE

    finally:
        # prevent test pollution: broken client leaking into success test scenarios
        client.app.dependency_overrides.clear()



def test_every_route_thread_can_get_a_database_connection():
    """The deadlock found by the /search load test: more threads than connections means
    requests holding connections wait for threads, and threads wait for connections."""
    from app.config import get_settings
    from app.db import engine

    capacity = engine.pool.size() + engine.pool._max_overflow
    # Exactly: more only spends Postgres connections that admission control never uses.
    assert capacity == get_settings().threadpool_size


class _Limiter:
    """Stands in for the anyio limiter: `busy` never grants a slot."""

    def __init__(self, busy):
        self.busy, self.acquired, self.released = busy, 0, 0

    async def acquire(self):
        import anyio

        if self.busy:
            await anyio.sleep(30)
        self.acquired += 1

    def release(self):
        self.released += 1


def test_a_request_over_capacity_gets_a_503_not_a_deadlock(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "queue_timeout_seconds", 0.05)
    client.app.state.inflight = _Limiter(busy=True)

    r = client.get("/readyz")

    assert r.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert r.json()["detail"] == "server busy, retry shortly"
    assert r.headers["Retry-After"] == "5"


def test_liveness_answers_even_when_every_slot_is_taken(client):
    client.app.state.inflight = _Limiter(busy=True)
    assert client.get("/healthz").status_code == status.HTTP_200_OK


def test_the_slot_is_released_after_each_request(client, vectors):
    limiter = client.app.state.inflight = _Limiter(busy=False)
    client.get("/readyz")
    client.get("/readyz")
    assert (limiter.acquired, limiter.released) == (2, 2)
