"""User data lives outside the installation. Legacy migration never deletes originals."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from contextlib import closing
from pathlib import Path

_lock = threading.Lock()
_prepared: set[Path] = set()
LEGACY_DIR = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    if os.environ.get("QUOTAX_DATA_DIR"):
        return Path(os.environ["QUOTAX_DATA_DIR"]).expanduser().resolve()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "QuotaX"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "QuotaX"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "quotax"


def config_path() -> Path:
    override = os.environ.get("QUOTABOARD_CONFIG")
    return Path(override).expanduser() if override else data_dir() / "config.json"


def prepare_storage(destination: Path, legacy: Path = LEGACY_DIR) -> None:
    """Copy one legacy installation as a unit; SQLite backup includes committed WAL data.

    An explicit QUOTABOARD_CONFIG opts out of migration. No work occurs at import time.
    Existing destinations always win; ambiguous partial destinations fail rather than merge.
    """
    if os.environ.get("QUOTABOARD_CONFIG") or destination != config_path():
        return
    target = destination.parent
    with _lock:
        if target in _prepared:
            return
        if destination.exists() or target.resolve() == legacy.resolve():
            _prepared.add(target)
            return
        if not any((legacy / name).exists() for name in ("config.json", "usage.db", "credentials", "history", "monitor-state.json")):
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            _prepared.add(target)
            return
        # Validate before copying anything. A corrupt legacy config must not become an empty installation.
        raw = (json.loads((legacy / "config.json").read_text(encoding="utf-8"))
               if (legacy / "config.json").exists() else {"channels": []})
        if not isinstance(raw, dict) or not isinstance(raw.get("channels", []), list):
            raise TypeError(f"旧配置结构无效，请先修复 {legacy / 'config.json'}")
        if target.exists() and any(target.iterdir()):
            raise RuntimeError(f"数据目录 {target} 已有文件但缺少 config.json；请先检查，未覆盖任何数据")
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".quotax-migration-", dir=target.parent))
        try:
            for name in ("config.json", "monitor-state.json", "credentials", "history"):
                source = legacy / name
                if not source.exists():
                    continue
                if source.is_symlink() or (source.is_dir() and any(p.is_symlink() for p in source.rglob("*"))):
                    raise ValueError(f"迁移数据中包含符号链接，请手动检查 {source}")
                if source.is_dir():
                    shutil.copytree(source, staging / name)
                else:
                    shutil.copyfile(source, staging / name)
            if not (staging / "config.json").exists():
                (staging / "config.json").write_text(json.dumps(raw), encoding="utf-8")
            source_db = legacy / "usage.db"
            if source_db.is_symlink():
                raise ValueError(f"迁移数据库是符号链接，请手动检查 {source_db}")
            if source_db.exists():
                # Read-only connection: do not checkpoint, move or copy a live WAL file.
                with (
                    closing(sqlite3.connect(source_db.resolve().as_uri() + "?mode=ro", uri=True)) as source,
                    closing(sqlite3.connect(staging / "usage.db")) as dest,
                ):
                    source.backup(dest)
            (staging / "migration.json").write_text(json.dumps({"copied_from": str(legacy)}), encoding="utf-8")
            for path in staging.rglob("*"):
                path.chmod(0o700 if path.is_dir() else 0o600)
            if target.exists():
                if destination.exists():
                    _prepared.add(target)
                    return
                target.rmdir()  # Only an empty directory can be removed.
            try:
                staging.rename(target)
            except OSError:
                # Another process may have completed the same atomic migration first.
                if not destination.exists():
                    raise
            _prepared.add(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
