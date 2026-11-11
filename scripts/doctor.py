"""Report source-checkout prerequisites without starting databases or models."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]


def inspect():
    checks = []

    def add(group, name, available):
        checks.append({"group": group, "requirement": name, "available": bool(available)})

    add("campaign", "Linux with fcntl locks", sys.platform.startswith("linux") and importlib.util.find_spec("fcntl"))
    for module in ("neo4j", "falkordb", "redis"):
        add("cypher", f"Python client: {module}", importlib.util.find_spec(module))
    paths = {
        "neo4j": ["runtime/neo4j_2026.09.0_all.deb", "runtime/neo4j-2026.09.0-extracted/usr/share/neo4j/bin/neo4j"],
        "memgraph": ["runtime/jammy-bwrap-3.14.0/memgraph"],
        "falkordb": ["runtime/jammy-bwrap/rootfs", "runtime/falkordb-6.0.3-redis/redis-8.10.2/src/redis-server",
                     "runtime/falkordb-6.0.3-inputs/falkordb-amazonlinux2023-x64.so", "/usr/bin/bwrap"],
        "gremlin": ["scripts/java/GremlinBatchRunner.java", "runtime/janusgraph/classpath.txt"],
    }
    for group, names in paths.items():
        for name in names:
            add(group, name, (ROOT / name).exists())
    for binary in ("java", "javac"):
        add("gremlin", binary + " on PATH", shutil.which(binary))
    add("gremlin", "runtime/tinkerpop-3.8.1/lib/*.jar", any((ROOT / "runtime/tinkerpop-3.8.1/lib").glob("*.jar")))
    cp_file = ROOT / "runtime/janusgraph/classpath.txt"
    if cp_file.is_file():
        entries = cp_file.read_text(encoding="utf-8").strip().split(":" if sys.platform != "win32" else ";")
        add("gremlin", "JanusGraph classpath entries exist", bool(entries) and all(Path(p).is_file() for p in entries))
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require", action="store_true", help="exit with code 1 when any campaign prerequisite is missing")
    args = parser.parse_args()
    checks = inspect()
    print(json.dumps({"scope": "presence checks; no engine startup or version validation",
                      "checks": checks, "database_calls": 0, "model_calls": 0}, indent=2))
    return int(args.require and any(not check["available"] for check in checks))


if __name__ == "__main__":
    raise SystemExit(main())
