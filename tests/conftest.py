import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="crew-health-test-"))
# Set TEST_DATABASE_URL to run the suite against PostgreSQL (use an empty database).
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL", f"sqlite:///{_TMP / 'test.db'}")
os.environ["INGEST_SOURCE"] = "bundled"
os.environ["RAW_CACHE_DIR"] = str(_TMP / "raw")
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ.pop("ADMIN_API_KEY", None)
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["JWT_SECRET"] = "test-secret-" + "x" * 40
os.environ["JWT_EXPIRE_MINUTES"] = "30"
os.environ["DEMO_PASSWORD"] = "test-password"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def login(client, username: str) -> dict:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": "test-password"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


@pytest.fixture(scope="session")
def client():
    """Logged in as the flight surgeon unless a test passes other headers."""
    with TestClient(app) as c:
        c.headers.update(login(c, "surgeon"))
        yield c


@pytest.fixture(scope="session")
def auth(client):
    cache: dict[str, dict] = {}

    def headers(username: str) -> dict:
        if username not in cache:
            cache[username] = login(client, username)
        return cache[username]

    return headers
