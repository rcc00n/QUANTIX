"""All fixture users, assets, funds and prices are SYNTHETIC. Never use real data.

Integration tests require a dedicated database with an _test suffix. No fixture
truncates application records; database lifecycle is owned by the test runner.
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from arbiq.auth.security import hash_password
from arbiq.core.config import Settings
from arbiq.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

SYNTHETIC_PASSWORD = "synthetic-fixture-password-only"


@pytest.fixture(scope="session")
def database_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "Integration tests require TEST_DATABASE_URL pointing to a dedicated _test DB"
        )
    if not make_url(url).database.endswith("_test"):
        pytest.fail("Refusing to test against a database without an _test suffix")
    directory = Path(__file__).resolve().parents[1] / "backend"
    if not directory.exists():
        directory = Path("/app")
    os.environ["DATABASE_URL"] = url
    config = Config(str(directory / "alembic.ini"))
    command.upgrade(config, "head")
    return url


@pytest.fixture(scope="session")
def app_settings(database_url):
    return Settings(
        _env_file=None,
        database_url=database_url,
        admin_username="synthetic-admin",
        admin_password_hash=hash_password(SYNTHETIC_PASSWORD),
        allowed_origins=["http://testserver"],
        cookie_secure=False,
    )


@pytest.fixture(scope="session")
def client(app_settings):
    with TestClient(create_app(app_settings)) as test_client:
        yield test_client


@pytest.fixture
def authenticated(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "synthetic-admin", "password": SYNTHETIC_PASSWORD},
    )
    assert response.status_code == 200
    yield client
    client.cookies.clear()
