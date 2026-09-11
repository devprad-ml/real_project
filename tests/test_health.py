from unittest.mock import MagicMock
from fastapi import status
from app.db import get_session


# testing healthz
def test_health(client):
    response = client.get("/healthz")
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status":"ok"}

# testing readyz
def test_readiness(client):
    
    response = client.get("/readyz")
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status":"ok"}


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
        client.app.dependency_overrides.clear()  # to prevent test pollution - broken client leaking to success test scenarios.

