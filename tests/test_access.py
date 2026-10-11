import pytest
from fastapi.testclient import TestClient

from app import main as m


@pytest.fixture
def client(monkeypatch, isolated_config):
    monkeypatch.delenv("QUOTAX_ACCESS_PASSWORD", raising=False)
    monkeypatch.setattr(m, "_ALLOWED_HOSTS", {"testserver", "localhost", "127.0.0.1", "quota.example"})
    return TestClient(m.app)


def test_local_ui_keeps_working_without_password(client):
    assert client.get("/").status_code == 200


def test_allowed_remote_host_still_needs_password(client):
    assert client.get("/", headers={"host": "quota.example"}).status_code == 403


def test_remote_socket_cannot_spoof_local_host(client):
    remote = TestClient(m.app, client=("192.0.2.1", 1234))
    assert remote.get("/", headers={"host": "localhost"}).status_code == 403


def test_password_protects_ui_and_secret_apis(client, monkeypatch):
    monkeypatch.setenv("QUOTAX_ACCESS_PASSWORD", "test-access-password")
    for path in ("/", "/api/config/export?include_secrets=true", "/api/channels/x/secret", "/api/health"):
        res = client.get(path)
        assert res.status_code == 401
        assert "Basic" in res.headers["www-authenticate"]
    assert client.get("/", auth=("quotax", "wrong")).status_code == 401
    assert client.get("/", auth=("wrong", "test-access-password")).status_code == 401
    assert client.get("/", auth=("quotax", "test-access-password")).status_code == 200
    assert client.get("/healthz").json() == {"ok": True, "service": "quotax"}


def test_remote_basic_login_and_no_store(client, monkeypatch):
    monkeypatch.setenv("QUOTAX_ACCESS_PASSWORD", "test-access-password")
    result = client.get("/api/channels", headers={"host": "quota.example"}, auth=("quotax", "test-access-password"))
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("headers", [{"origin": "https://evil.example"}, {"origin": "null"}, {"sec-fetch-site": "cross-site"}])
def test_cross_site_mutations_are_blocked(client, headers):
    result = client.post("/api/config/import", json={"channels": []}, headers=headers)
    assert result.status_code == 403
