"""API integration against the dedicated DB; all credentials are SYNTHETIC."""

from conftest import SYNTHETIC_PASSWORD


def test_health_and_private_routes(client):
    client.cookies.clear()
    assert client.get("/health/live").json()["mode"] == "paper"
    assert client.get("/health/ready").status_code == 200
    for endpoint in [
        "system/status",
        "system/events",
        "reports/contract",
        "settings/defaults",
        "exchanges",
        "auth/me",
    ]:
        assert client.get(f"/api/v1/{endpoint}").status_code == 401
    assert client.get("/openapi.json").status_code == 404


def test_login_cookie_logout_and_audit(authenticated):
    client = authenticated
    cookie = client.cookies.get("arbiq_session")
    assert cookie
    assert client.get("/api/v1/auth/me").json()["username"] == "synthetic-admin"
    status = client.get("/api/v1/system/status").json()
    assert status["services"]["scanner"] == "unavailable"
    assert status["services"]["ai"] == "disabled"
    # Other modules retain explicitly synthetic public evidence in this test DB.
    assert isinstance(status["counts"]["opportunities"], int)
    assert status["counts"]["opportunities"] >= 0
    contract = client.get("/api/v1/reports/contract").json()
    assert contract["export_available"]
    assert len(contract["files"]) == 11
    assert client.get("/api/v1/settings/defaults").json()["persisted"] is False
    events = client.get("/api/v1/system/events").json()
    assert any(row["event_type"] == "auth.login_succeeded" for row in events)
    assert "password" not in str(events)
    assert client.post("/api/v1/auth/logout").status_code == 200
    client.cookies.set("arbiq_session", cookie)
    assert client.get("/api/v1/auth/me").status_code == 401






