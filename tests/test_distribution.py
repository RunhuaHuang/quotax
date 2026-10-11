import json
import zipfile

from scripts.package_release import build


def test_source_package_excludes_personal_files_and_keeps_required_modules(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nversion="1.3.0"\ndescription="统一查看额度"\n', encoding="utf-8"
    )
    (source / "app").mkdir()
    (source / "app" / "main.py").write_text('app = None')
    (source / "static").mkdir()
    (source / "static" / "refresh-controller.js").write_text('export const test = true;')
    (source / "config.json").write_text(json.dumps({"api_key": "secret-test-value"}))
    (source / "usage.db").write_bytes(b"private")
    (source / "credentials").mkdir()
    (source / "credentials" / "auth.json").write_text('private')
    (source / "personal-notes.txt").write_text('private')
    archive = build(source, tmp_path / "output")
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert "quotax-1.3.0/app/main.py" in names
        assert "quotax-1.3.0/static/refresh-controller.js" in names
        assert not any("config.json" in name or "usage.db" in name or "credentials" in name or "personal" in name for name in names)
        assert all(b"secret-test-value" not in bundle.read(name) for name in names)
    assert archive.with_suffix(".zip.sha256").exists()


def test_source_launcher_preserves_saved_access_settings(tmp_path):
    import os
    import shutil
    import subprocess
    from pathlib import Path

    if os.name == "nt" or not shutil.which("bash"):
        import pytest
        pytest.skip("bash unavailable")
    root = Path(__file__).resolve().parent.parent
    shutil.copyfile(root / "run.sh", tmp_path / "run.sh")
    (tmp_path / "quotax.env").write_text("QUOTAX_ACCESS_PASSWORD=test-only-password\nQUOTAX_HOST='[::1]'\nQUOTAX_PORT=9123\n")
    binary = tmp_path / "uv"
    binary.write_text('#!/bin/sh\nprintf "%s\\n" "$QUOTAX_ACCESS_PASSWORD" "$@"\n')
    binary.chmod(0o755)
    env = {"PATH": str(tmp_path) + os.pathsep + os.defpath}
    result = subprocess.run(["bash", str(tmp_path / "run.sh")], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["test-only-password", "run", "--locked", "--no-dev", "uvicorn", "app.main:app", "--host", "::1", "--port", "9123"]


def test_upgrade_reads_persisted_settings_before_validation(tmp_path):
    import os
    import shutil
    import subprocess
    from pathlib import Path

    if os.name == "nt" or not shutil.which("bash"):
        import pytest
        pytest.skip("bash unavailable")
    # An invalid saved port must stop installation before downloads or mutations.
    (tmp_path / "quotax.env").write_text("QUOTAX_PORT=invalid\n")
    env = {"PATH": os.defpath, "QUOTAX_HOME": str(tmp_path), "TMPDIR": str(tmp_path)}
    script = Path(__file__).resolve().parent.parent / "install.sh"
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "QUOTAX_PORT" in result.stderr
    assert "Downloading" not in result.stdout
