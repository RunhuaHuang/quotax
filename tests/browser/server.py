"""Isolated UI test server. Never discover accounts or read the host's logs."""
import os
import sys
from pathlib import Path

data = Path(sys.argv[2])
os.environ.update({
    "QUOTABOARD_CONFIG": str(data / "config.json"),
    "QUOTABOARD_USAGE_DB": str(data / "usage.db"),
    "QUOTABOARD_HISTORY_DIR": str(data / "history"),
    "QUOTAX_ACCESS_PASSWORD": "",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn

from app.main import app, usage_collect

# Collection is explicitly stubbed even if a browser test misses its route mock.
usage_collect.collect_all = lambda: {"sources": [], "total_new": 0}
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), lifespan="off", log_level="warning")
