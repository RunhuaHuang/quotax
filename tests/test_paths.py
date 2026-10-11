import json
import sqlite3
from pathlib import Path

import pytest

from app import paths


@pytest.fixture
def migration(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    target = tmp_path / "user-data" / "config.json"
    monkeypatch.delenv("QUOTABOARD_CONFIG", raising=False)
    monkeypatch.setenv("QUOTAX_DATA_DIR", str(target.parent))
    (legacy / "config.json").write_text(json.dumps({"channels": [{"id": "test"}]}))
    return legacy, target


def test_migration_keeps_originals_and_credentials_and_is_idempotent(migration):
    legacy, target = migration
    (legacy / "credentials").mkdir()
    (legacy / "credentials" / "codex_test.json").write_text('{"test":true}')
    paths.prepare_storage(target, legacy)
    assert target.read_text() == (legacy / "config.json").read_text()
    assert (target.parent / "credentials" / "codex_test.json").read_text() == '{"test":true}'
    target.write_text('{"channels":[]}')
    paths.prepare_storage(target, legacy)
    assert target.read_text() == '{"channels":[]}'
    assert (legacy / "credentials" / "codex_test.json").exists()


def test_migration_includes_committed_wal_without_mutating_source(migration):
    legacy, target = migration
    connection = sqlite3.connect(legacy / "usage.db")
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE records (value INTEGER)")
        connection.execute("INSERT INTO records VALUES (42)")
        connection.commit()
        paths.prepare_storage(target, legacy)
        copied = sqlite3.connect(target.parent / "usage.db")
        try:
            assert copied.execute("SELECT value FROM records").fetchall() == [(42,)]
        finally:
            copied.close()
        assert connection.execute("SELECT value FROM records").fetchall() == [(42,)]
    finally:
        connection.close()


def test_failed_copy_leaves_originals_and_no_partial_destination(migration, monkeypatch):
    legacy, target = migration

    def broken(*_):
        raise OSError("disk full")

    monkeypatch.setattr(paths.shutil, "copyfile", broken)
    with pytest.raises(OSError, match="disk full"):
        paths.prepare_storage(target, legacy)
    assert (legacy / "config.json").exists()
    assert not target.exists()
    assert not list(target.parent.parent.glob(".quotax-migration-*"))


def test_existing_destination_is_never_overwritten(migration):
    legacy, target = migration
    target.parent.mkdir()
    target.write_text('{"channels":[]}')
    paths.prepare_storage(target, legacy)
    assert target.read_text() == '{"channels":[]}'


def test_partial_destination_is_not_silently_merged(migration):
    legacy, target = migration
    target.parent.mkdir()
    (target.parent / "usage.db").write_bytes(b"existing")
    with pytest.raises(RuntimeError, match="已有文件"):
        paths.prepare_storage(target, legacy)
    assert (target.parent / "usage.db").read_bytes() == b"existing"


def test_explicit_config_path_opts_out(migration, monkeypatch):
    legacy, target = migration
    monkeypatch.setenv("QUOTABOARD_CONFIG", str(target))
    paths.prepare_storage(target, legacy)
    assert not target.exists()


@pytest.mark.parametrize(("platform", "suffix"), [("darwin", "Library/Application Support/QuotaX"),
                                                  ("linux", ".local/share/quotax"), ("win32", "AppData/Local/QuotaX")])
def test_platform_data_directory(tmp_path, monkeypatch, platform, suffix):
    for key in ("QUOTAX_DATA_DIR", "LOCALAPPDATA", "XDG_DATA_HOME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(paths.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert paths.data_dir() == tmp_path / suffix


def test_usage_only_legacy_install_is_also_migrated(migration):
    legacy, target = migration
    (legacy / "config.json").unlink()
    connection = sqlite3.connect(legacy / "usage.db")
    connection.execute("CREATE TABLE records(value INTEGER)")
    connection.commit()
    connection.close()
    paths.prepare_storage(target, legacy)
    assert json.loads(target.read_text()) == {"channels": []}
    assert (target.parent / "usage.db").exists()
