"""Create a reviewed public source snapshot, without private Git history or state.

This is a whitelist export, not a sanitizer for arbitrary unreviewed files. Review
the resulting diff and run the release gates before initializing a public repo.
"""

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    ".gitignore",
    ".gitattributes",
    ".python-version",
    ".env.example",
    "pyproject.toml",
    "uv.lock",
    "hatch_build.py",
    "LICENSE",
    "README.md",
    "README.zh-CN.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "ROADMAP.md",
    "THIRD_PARTY_NOTICES.md",
    "frontend/index.html",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/tsconfig.json",
    "frontend/vite.config.ts",
    "docs/PRODUCT.md",
    "docs/ARCHITECTURE.md",
)
PATTERNS = (
    "src/**/*.py",
    "tests/*.py",
    "scripts/*.py",
    "scripts/*.mjs",
    "frontend/src/*",
    "frontend/public/*.svg",
    ".github/workflows/*.yml",
    ".github/ISSUE_TEMPLATE/*",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "data/knowledge/*.md",
    "data/fixtures/**/*.json",
    "evals/**/*.json",
    "evals/**/*.md",
    "third_party/licenses/*-LICENSE.txt",
    "docs/release/*.json",
    "docs/release/*.md",
    "docs/release/*.png",
)


def selected_files():
    selected = {ROOT / name for name in FILES}
    selected.update(path for pattern in PATTERNS for path in ROOT.glob(pattern) if path.is_file())
    for path in sorted(selected):
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Required reviewed regular file missing: {path.relative_to(ROOT)}")
        if any(
            part.startswith(".")
            and part
            not in {".github", ".gitignore", ".gitattributes", ".python-version", ".env.example"}
            for part in path.relative_to(ROOT).parts
        ):
            raise ValueError("Hidden runtime files are not eligible for public export")
        yield path


def export(destination: Path):
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("Public snapshot target must not already exist")
    paths = list(selected_files())
    from incident_desk.settings import load_settings

    settings = load_settings()
    credential = settings.api_key
    needles = [(str(Path.home()) + os.sep).encode()]
    if credential and credential.get_secret_value():
        needles.append(credential.get_secret_value().encode())
    for path in (settings.local_dir / "model-config").glob("credential-*"):
        if path.is_file() and not path.is_symlink():
            value = path.read_bytes().strip()
            if value:
                needles.append(value)
    # Never print matched content. Known credentials and this operator's private
    # home path are release blockers, including when embedded in binary assets.
    for path in paths:
        raw = path.read_bytes()
        if any(needle in raw for needle in needles):
            raise ValueError(f"Private content detected in {path.relative_to(ROOT)}")
        if b"-----BEGIN PRIVATE KEY-----\n" in raw or b"-----BEGIN RSA PRIVATE KEY-----\n" in raw:
            raise ValueError(f"Private key material detected in {path.relative_to(ROOT)}")
    destination.mkdir(parents=True)
    manifest = {}
    try:
        for path in paths:
            relative = path.relative_to(ROOT)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o644)
            manifest[relative.as_posix()] = hashlib.sha256(target.read_bytes()).hexdigest()
        (destination / "public-source-manifest.json").write_text(
            json.dumps({"schema": 1, "files": manifest}, indent=2, sort_keys=True) + "\n"
        )
    except BaseException:
        shutil.rmtree(destination)
        raise
    return {
        "files": len(paths),
        "bytes": sum(path.stat().st_size for path in paths),
        "private_history_included": False,
        "known_credential_matches": 0,
        "private_home_path_matches": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.destination), indent=2))


if __name__ == "__main__":
    main()
