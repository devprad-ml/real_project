""" testing code"""

from pathlib import Path
from dotenv import load_dotenv
env = Path(__file__).resolve().parent.parent / ".env.test"
load_dotenv(dotenv_path=env, override=True)

# import pytest now
import pytest
from fastapi.testclient import TestClient

# import app factory
from app.main import create_app


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client

