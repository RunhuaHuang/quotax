"""Check the installed wheel from outside the checkout; never read local credentials."""
import os
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="quotax-smoke-") as tmp:
    os.environ["QUOTABOARD_CONFIG"] = str(Path(tmp) / "config.json")
    os.environ["QUOTABOARD_USAGE_DB"] = str(Path(tmp) / "usage.db")
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)  # No lifespan: do not discover accounts or scan the host.
    assert client.get("/healthz").json()["service"] == "quotax"
    assert client.get("/").status_code == 200
    for asset in ("app.js", "usage.js", "refresh-controller.js", "i18n.js", "styles.css"):
        assert client.get(f"/static/{asset}").status_code == 200, asset
    assert client.get("/api/quotas").json()["channels"] == []
    print("Installed wheel: endpoints and static modules OK")
