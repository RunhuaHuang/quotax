"""Build a source ZIP from an explicit allowlist, never from the personal working folder.

Usage: python scripts/package_release.py --output dist
"""
from __future__ import annotations

import argparse
import hashlib
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = {"AGENTS.md", "README.md", "README.en.md", "pyproject.toml", "uv.lock", ".gitignore",
         "install.sh", "install.ps1", "quotax", "quotax.cmd", "run.sh",
         "package.json", "package-lock.json", "playwright.config.js"}
DIRECTORIES = {"app", "static", "tests", "scripts", ".github", "docs"}
EXCLUDED = {"__pycache__", ".pytest_cache", ".ruff_cache", ".DS_Store"}


def release_files(root: Path):
    candidates = [root / name for name in FILES if (root / name).exists()]
    for name in DIRECTORIES:
        candidates.extend((root / name).rglob("*"))
    for path in sorted(candidates):
        rel = path.relative_to(root)
        if rel.parts[0] not in DIRECTORIES and str(rel) not in FILES:
            continue
        if any(part in EXCLUDED for part in rel.parts) or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise ValueError(f"Release source contains a symlink: {rel}")
        if path.is_file():
            yield path


def build(root: Path, output: Path) -> Path:
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"quotax-{version}-source.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in release_files(root):
            bundle.write(path, f"quotax-{version}/{path.relative_to(root).as_posix()}")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    print(build(ROOT, args.output.resolve()))
