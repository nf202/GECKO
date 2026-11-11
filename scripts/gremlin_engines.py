"""Embedded Gremlin engines for Gecko: TinkerGraph 3.7.3 / 3.8.1 and JanusGraph 1.1.0 (in-memory).

Each batch starts one JVM (scripts/java/GremlinBatchRunner.java) that builds the owned graph from
a generated Groovy load script and runs the measured read-only queries with a per-query timeout.
All jars are project-local under runtime/.
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from gecko_runtime_guard import cache_lock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/java/GremlinBatchRunner.java"


def _janus_cp():
    return (ROOT / "runtime/janusgraph/classpath.txt").read_text().strip()


def _tp381_cp():
    return os.pathsep.join(str(p) for p in sorted((ROOT / "runtime/tinkerpop-3.8.1/lib").glob("*.jar")))


ENGINES = {
    "tinkergraph-3.7.3": ("tinkergraph", _janus_cp),
    "tinkergraph-3.8.1": ("tinkergraph", _tp381_cp),
    "janusgraph-1.1.0-inmemory": ("janusgraph", _janus_cp),
}
MUTATING = re.compile(r"(?:addV|addE|drop|property|sideEffect|mergeV|mergeE|io\s*\(|tx\s*\(|graph\b|;)", re.I)


def read_only(q):
    """True if q (string literals blanked) contains no mutating step or statement separator."""
    return not MUTATING.search(re.sub(r"'[^']*'", "''", q))


def compiled(name):
    """Compile the runner once per classpath; cached by source hash."""
    kind, cp = ENGINES[name][0], ENGINES[name][1]()
    tag = hashlib.sha256(SOURCE.read_bytes() + cp.encode()).hexdigest()[:16]
    classes = ROOT / "runtime/gremlin-runner" / tag
    with cache_lock(ROOT, tag):
        if not (classes / "compile.ready").exists():
            classes.mkdir(parents=True, exist_ok=True)
            subprocess.run(["javac", "-nowarn", "-cp", cp, "-d", str(classes), str(SOURCE)], check=True,
                           capture_output=True, text=True, timeout=300)
            (classes / "compile.ready").write_text(tag)
    return kind, str(classes) + os.pathsep + cp


def _b64(s):
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def run_batch(name, load_script, queries, workdir, timeout_ms=10000):
    """queries: [(case_id, gremlin)]. Returns (load_record, {case_id: record})."""
    from gecko_identity import unique_index
    queries = list(queries)
    unique_index([{"id": cid} for cid, _q in queries], context="Gremlin input")
    if any(cid == "LOAD" for cid, _q in queries):
        raise ValueError("reserved Gremlin case id: LOAD")
    for cid, q in queries:
        if not read_only(q):
            raise ValueError("mutating or multi-statement measured Gremlin query: " + cid)
    kind, cp = compiled(name)
    workdir.mkdir(parents=True, exist_ok=True)
    inp, out = workdir / f"{name}.in.txt", workdir / f"{name}.out.jsonl"
    inp.write_text("LOAD\t" + _b64(load_script) + "\n" + "".join(f"{cid}\t{_b64(q)}\n" for cid, q in queries),
                   encoding="utf-8")
    p = subprocess.run(["java", "-Xms256m", "-Xmx2g", "-cp", cp, "GremlinBatchRunner", kind, str(inp), str(out),
                        str(timeout_ms)], capture_output=True, text=True, timeout=120 + len(queries) * timeout_ms / 1000)
    (workdir / f"{name}.jvm.log").write_text(p.stdout + p.stderr)
    if p.returncode or not out.exists():
        raise RuntimeError(f"{name} runner exited {p.returncode}: {p.stderr[-500:]}")
    recs = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l]
    unique_index(recs, context="Gremlin output", expected=["LOAD"] + [cid for cid, _q in queries])
    if recs[0]["id"] != "LOAD":
        raise ValueError("Gremlin output must begin with LOAD")
    return recs[0], unique_index(recs[1:], context="Gremlin query output")


def rows(result):
    """Gremlin result list -> harness rows: map -> values in key order, entry -> [k, v], else [x]."""
    out = []
    for x in result:
        if isinstance(x, dict) and "__map__" in x:
            out.append([v for _k, v in x["__map__"]])
        elif isinstance(x, dict) and "__entry__" in x:
            out.append(list(x["__entry__"]))
        else:
            out.append([x])
    return out
