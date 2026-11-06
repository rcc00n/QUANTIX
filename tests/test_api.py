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








