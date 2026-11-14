import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="crew-health-test-"))
# Set TEST_DATABASE_URL to run the suite against PostgreSQL (use an empty database).
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL", f"sqlite:///{_TMP / 'test.db'}")
os.environ["INGEST_SOURCE"] = "bundled"
os.environ["RAW_CACHE_DIR"] = str(_TMP / "raw")
os.environ.pop("ADMIN_API_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c
