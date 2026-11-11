"""Verify shipped bytes; optionally reject unexpected non-runtime files."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def generated(path):
    parts = path.parts
    ignored = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist", "runs", "relay_runtime", "tmp", "artifacts"}
    return (any(p in ignored or p.endswith(".egg-info") for p in parts)
            or (parts[0] == "runtime" and path.as_posix() != "runtime/README.md")
            or path.suffix in {".pyc", ".log"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "MANIFEST.sha256.json").read_text(encoding="utf-8"))
    errors = []
    for name, expected in manifest.items():
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            errors.append({"path": name, "error": "missing or invalid path"})
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            errors.append({"path": name, "error": "SHA-256 mismatch"})
    actual = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file() and not generated(p.relative_to(ROOT))}
    unexpected = sorted(actual - set(manifest) - {"MANIFEST.sha256.json"})
    print(json.dumps({"verified_files": len(manifest), "errors": errors, "unexpected_files": unexpected}, indent=2))
    return int(bool(errors) or (args.strict and bool(unexpected)))


if __name__ == "__main__":
    raise SystemExit(main())
