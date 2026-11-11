#!/usr/bin/env python3
"""Run a frozen current-version bounded-path semantic campaign.

All measured queries are compiler-emitted reads over fresh project-owned
loopback instances.  A separate finite evaluator supplies expected row bags.
The campaign records stable discrepancies as candidates; it does not label
them as database defects.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from campaign_broad_semantic import launch_bolt, stop_bolt
from gqltest.engines import BoltEngine
from gqltest.generated_graph_features import (
    DIALECT,
    compile_queries,
    expected_partitions,
    validate_feature,
    validate_graph,
)
from gqltest.oracle import bag, partition_holds


SEEDS = tuple(range(83000, 83010))
FEATURE_SEED = 83117
FEATURE_COUNT = 48
ALIASES = ["start_id", "end_id", "edge_ids", "path_length"]
FORBIDDEN = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|ALTER|INSERT|LOAD\s+CSV)\b", re.I)
PACKAGE = ROOT / "runtime/neo4j_2026.09.0_all.deb"
NEO4J_HOME = ROOT / "runtime/neo4j-2026.09.0-extracted/usr/share/neo4j"
EXPECTED_PACKAGE_SHA256 = "583b3b64fde62696d349e0aed6047e374e83695bf25ec3427dfa5eaac7a7f35a"
MEMGRAPH_WRAPPER = ROOT / "runtime/jammy-bwrap/memgraph"


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def save(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def literal(value):
    return {"op": "literal", "value": value}


def prop(endpoint, key):
    return {"op": "property", "endpoint": endpoint, "key": key}


def compare(operator, left, right):
    return {"op": "compare", "operator": operator, "left": left, "right": right}


def predicates():
    age_s, age_e = prop("start", "age"), prop("end", "age")
    id_s, id_e = prop("start", "id"), prop("end", "id")
    name_s, name_e = prop("start", "name"), prop("end", "name")
    flag_s, flag_e = prop("start", "flag"), prop("end", "flag")
    length = {"op": "path_length"}
    atoms = [
        compare("=", age_s, age_e), compare("<>", age_s, age_e),
        compare("<", age_s, literal(0)), compare(">=", age_e, literal(2)),
        compare("=", age_s, literal(None)), compare("=", id_s, id_e),
        compare("<", id_s, id_e), compare(">=", id_e, literal(3)),
        compare("=", name_s, name_e), compare("<>", name_e, literal("Ω")),
        compare("=", name_s, literal("")), compare("=", flag_s, flag_e),
        compare("=", flag_e, literal(True)), compare("<>", flag_s, literal(False)),
        compare("=", length, literal(0)), compare(">", length, literal(1)),
        compare("<=", length, literal(2)), compare("=", length, literal(3)),
        {"op": "is_null", "arg": age_s}, {"op": "is_null", "arg": name_e},
        {"op": "is_null", "arg": flag_s},
    ]
    return atoms + [
        {"op": "not", "arg": atoms[0]},
        {"op": "not", "arg": atoms[18]},
        {"op": "and", "args": [atoms[2], atoms[16]]},
        {"op": "and", "args": [atoms[8], atoms[20]]},
        {"op": "or", "args": [atoms[3], atoms[18]]},
        {"op": "or", "args": [atoms[10], atoms[19]]},
        {"op": "and", "args": [atoms[6], {"op": "not", "arg": atoms[20]}]},
        {"op": "or", "args": [atoms[12], {"op": "not", "arg": atoms[0]}]},
    ]


def frozen_features():
    contexts = [
        {"direction": direction, "min_length": bounds[0], "max_length": bounds[1], "reuse": reuse}
        for direction in ("out", "in")
        for bounds in ((0, 0), (0, 1), (1, 1), (1, 2), (0, 3), (2, 3))
        for reuse in ("walk", "edge_simple")
    ]
    candidates = [
        {"context": context, "predicate": predicate}
        for context in contexts for predicate in predicates()
    ]
    random.Random(FEATURE_SEED).shuffle(candidates)
    selected = candidates[:FEATURE_COUNT]
    for feature in selected:
        validate_feature(feature)
    return selected


def graph(seed: int):
    rng = random.Random(seed)
    ages = [None, -1, 0, 1, 2, 2**63 - 1]
    names = [None, "", "Ada", "Ω", "é", "a'b"]
    flags = [None, False, True]
    nodes = [
        {
            "id": node_id,
            "age": rng.choice(ages),
            "score": float(rng.randint(-2, 3)),
            "name": rng.choice(names),
            "flag": rng.choice(flags),
        }
        for node_id in range(5)
    ]
    endpoints = [(0, 0), (0, 1), (0, 1)]
    endpoints += [(rng.randrange(5), rng.randrange(5)) for _ in range(7)]
    edges = [
        {"eid": index, "src": src, "dst": dst, "weight": rng.randint(-3, 3)}
        for index, (src, dst) in enumerate(endpoints)
    ]
    value = {"nodes": nodes, "edges": edges}
    validate_graph(value)
    return value


def free_port(exclude=()):
    excluded = set(exclude)
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in excluded:
            return port


def wait_ready(uri, process, timeout=60.0):
    from neo4j import GraphDatabase
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Neo4j exited early: {process.returncode}")
        try:
            with GraphDatabase.driver(uri, auth=None) as driver:
                driver.verify_connectivity()
            return
        except Exception as exc:
            last = exc
            time.sleep(0.25)
    raise RuntimeError(f"Neo4j readiness timeout: {last}")


def launch_neo4j(run: Path):
    binary = (NEO4J_HOME / "bin/neo4j").resolve(strict=True)
    binary.relative_to(ROOT)
    bolt = free_port()
    http = free_port((bolt,))
    conf = run / "conf"
    conf.mkdir()
    for name in ("plugins", "data", "logs", "run"):
        (run / name).mkdir()
    (conf / "neo4j.conf").write_text(
        "server.default_listen_address=127.0.0.1\n"
        f"server.bolt.listen_address=127.0.0.1:{bolt}\n"
        f"server.http.listen_address=127.0.0.1:{http}\n"
        "server.https.enabled=false\n"
        f"server.directories.data={run/'data'}\n"
        f"server.directories.logs={run/'logs'}\n"
        f"server.directories.plugins={run/'plugins'}\n"
        f"server.directories.run={run/'run'}\n"
        "server.memory.heap.initial_size=256m\n"
        "server.memory.heap.max_size=512m\n"
        "initial.dbms.default_database=neo4j\n"
        "dbms.security.auth_enabled=false\n"
        "dbms.usage_report.enabled=false\n",
        encoding="utf-8",
    )
    uri = f"bolt://127.0.0.1:{bolt}"
    env = os.environ.copy()
    env.update({"NEO4J_HOME": str(NEO4J_HOME), "NEO4J_CONF": str(conf)})
    command = [str(binary), "console"]
    log = (run / "process.log").open("w", encoding="utf-8")
    process = subprocess.Popen(command, cwd=run, env=env, stdout=log,
                               stderr=subprocess.STDOUT, text=True,
                               start_new_session=True)
    receipt = {
        "schema": "gqltest-owned-current-engine/1",
        "engine": "neo4j-2026.09.0", "pid": process.pid,
        "binary": str(binary), "binary_sha256": sha(binary),
        "uri": uri, "command": command, "terminated": False,
        "project_owned": True,
    }
    save(run / "ownership.json", receipt)
    try:
        wait_ready(uri, process)
        return uri, process, log, receipt
    except Exception:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=15)
        receipt.update(terminated=True, engine_exit=process.returncode)
        save(run / "ownership.json", receipt)
        log.close()
        raise


def stop_neo4j(process, log, receipt, run):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
    receipt.update(terminated=True, engine_exit=process.returncode)
    save(run / "ownership.json", receipt)
    log.close()


def execute_named(engine, query):
    from neo4j import Query
    started = time.monotonic()
    try:
        with engine.driver.session(database="neo4j" if engine.name == "neo4j" else None) as session:
            result = session.run(Query(query, timeout=engine.timeout))
            keys = list(result.keys())
            records = list(result)
        if set(keys) != set(ALIASES) or len(keys) != len(ALIASES):
            raise AssertionError(f"unexpected aliases: {keys!r}")
        rows = [[record.data()[name] for name in ALIASES] for record in records]
        return {"status": "ok", "returned_keys": keys, "rows": rows,
                "elapsed_ms": (time.monotonic() - started) * 1000}
    except Exception as exc:
        return {"status": "error", "error_type": type(exc).__name__,
                "error_code": getattr(exc, "code", None),
                "gql_status": getattr(exc, "gql_status", None),
                "error": str(exc)[:1000],
                "elapsed_ms": (time.monotonic() - started) * 1000}


def inspect_state(engine, fixture):
    queries = [
        "MATCH (n:P) RETURN n.id,n.age,n.score,n.name,n.flag",
        "MATCH (a:P)-[r:R]->(b:P) RETURN r.eid,a.id,b.id,r.weight",
    ]
    expected = [
        [[node[key] for key in ("id", "age", "score", "name", "flag")] for node in fixture["nodes"]],
        [[edge[key] for key in ("eid", "src", "dst", "weight")] for edge in fixture["edges"]],
    ]
    executions = [engine.execute(query) for query in queries]
    matches = all(
        item["status"] == "ok" and bag(item["rows"]) == bag(rows)
        for item, rows in zip(executions, expected)
    )
    return {"queries": queries, "executions": executions,
            "expected_rows": expected, "matches": matches}


def evaluate_case(engine, fixture, feature):
    queries = compile_queries(feature)
    if any(FORBIDDEN.search(query) for query in queries):
        raise AssertionError("compiler emitted a mutation")
    expected = expected_partitions(fixture, feature)
    branches = []
    for label, query, expected_rows in zip(("base", "true", "false", "null"), queries, expected):
        executions = [execute_named(engine, query) for _ in range(2)]
        stable = all(
            item["status"] == executions[0]["status"]
            and (item["status"] != "ok" or bag(item["rows"]) == bag(executions[0]["rows"]))
            for item in executions
        )
        reference_match = stable and executions[0]["status"] == "ok" and bag(executions[0]["rows"]) == bag(expected_rows)
        if not stable or not reference_match:
            executions.append(execute_named(engine, query))
            stable = all(
                item["status"] == executions[0]["status"]
                and (item["status"] != "ok" or bag(item["rows"]) == bag(executions[0]["rows"]))
                for item in executions
            )
            reference_match = stable and executions[0]["status"] == "ok" and bag(executions[0]["rows"]) == bag(expected_rows)
        branches.append({"label": label, "query": query,
                         "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
                         "expected_rows": expected_rows, "executions": executions,
                         "stable": stable, "reference_match": reference_match})
    first = [branch["executions"][0] for branch in branches]
    partition = all(item["status"] == "ok" for item in first) and partition_holds(*[item["rows"] for item in first])
    return {"feature": feature, "branches": branches, "partition_holds": partition}


def run_engine_fixture(engine_name, seed, fixture, features, out):
    run = out / f"{engine_name}-seed{seed}"
    run.mkdir()
    save(run / "graph.json", fixture)
    engine = process = log = receipt = None
    try:
        if engine_name == "memgraph-3.13.2-jammy":
            uri, version, process, log, receipt = launch_bolt(engine_name, MEMGRAPH_WRAPPER, run)
            engine = BoltEngine(uri, "memgraph", version=version)
        else:
            uri, process, log, receipt = launch_neo4j(run)
            engine = BoltEngine(uri, "neo4j")
        engine.setup(fixture)
        before = inspect_state(engine, fixture)
        if not before["matches"]:
            raise AssertionError("initial physical state mismatch")
        cases = [evaluate_case(engine, fixture, feature) for feature in features]
        after = inspect_state(engine, fixture)
        if not after["matches"]:
            raise AssertionError("final physical state mismatch")
        payload = {
            "schema": "gqltest-current-path-semantics-run/1",
            "engine": engine_name, "actual_version": str(engine.version),
            "seed": seed, "before": before, "after": after,
            "cases": cases, "complete": True,
        }
        save(run / "results.json", payload)
        return payload
    finally:
        if engine is not None:
            engine.close()
        if process is not None:
            if engine_name == "neo4j-2026.09.0":
                stop_neo4j(process, log, receipt, run)
            else:
                stop_bolt(engine_name, process, log, receipt, run)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, default=ROOT / "runs/current-path-semantics01")
    ap.add_argument("--preflight-only", action="store_true")
    args = ap.parse_args()
    out = args.output.resolve()
    out.relative_to(ROOT / "runs")
    if out.exists():
        raise SystemExit(f"output exists: {out}")
    if sha(PACKAGE) != EXPECTED_PACKAGE_SHA256:
        raise SystemExit("Neo4j package SHA-256 mismatch")
    for path in (NEO4J_HOME / "bin/neo4j", MEMGRAPH_WRAPPER):
        path.resolve(strict=True).relative_to(ROOT)

    features = frozen_features()
    fixtures = {str(seed): graph(seed) for seed in SEEDS}
    compiled = [query for feature in features for query in compile_queries(feature)]
    if args.preflight_only:
        print(json.dumps({
            "features": len(features), "graphs": len(fixtures),
            "queries_per_graph": len(compiled),
            "unique_features": len({json.dumps(feature, sort_keys=True) for feature in features}),
            "unsafe_queries": sum(bool(FORBIDDEN.search(query)) for query in compiled),
            "neo4j_package_sha256": sha(PACKAGE),
            "memgraph_wrapper": str(MEMGRAPH_WRAPPER),
        }, indent=2))
        return
    source_files = (
        "scripts/campaign_current_path_semantics.py",
        "scripts/campaign_broad_semantic.py",
        "src/gqltest/generated_graph_features.py",
        "src/gqltest/engines.py",
        "src/gqltest/oracle.py",
    )
    out.mkdir()
    protocol = {
        "schema": "gqltest-current-path-semantics-protocol/1",
        "frozen_before_engine_calls": True,
        "seeds": list(SEEDS), "feature_seed": FEATURE_SEED,
        "feature_count": len(features), "features": features,
        "fixtures": fixtures,
        "engines": ["memgraph-3.13.2-jammy", "neo4j-2026.09.0"],
        "branches": ["base", "true", "false", "null"],
        "base_repeats": 2, "candidate_repeats": 3,
        "minimum_query_executions": len(SEEDS) * len(features) * 4 * 2 * 2,
        "physical_inspection_reads": len(SEEDS) * 2 * 4,
        "compiler_contract": DIALECT, "loopback_only": True,
        "measured_queries_read_only": True, "model_calls": 0, "qwen_used": False,
        "neo4j_package_sha256": EXPECTED_PACKAGE_SHA256,
        "memgraph_environment": "project-owned Ubuntu 22.04.5 Base bubblewrap wrapper; same host kernel",
        "source_sha256": {name: sha(ROOT / name) for name in source_files},
        "scope": "held-out current-version bounded-path semantic validation; stable discrepancies are candidates, not defect claims",
    }
    for name in source_files:
        target = out / "source_snapshot" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    save(out / "protocol.json", protocol)
    save(out / "freeze.json", {"before_engine_calls": True, "at_unix": time.time(),
                               "protocol_sha256": sha(out / "protocol.json")})

    summaries = []
    for seed in SEEDS:
        fixture = fixtures[str(seed)]
        for engine_name in protocol["engines"]:
            payload = run_engine_fixture(engine_name, seed, fixture, features, out)
            candidate_branches = sum(
                not branch["reference_match"]
                for case in payload["cases"] for branch in case["branches"]
            )
            row = {"engine": engine_name, "seed": seed,
                   "candidate_branches": candidate_branches,
                   "partition_failures": sum(not case["partition_holds"] for case in payload["cases"])}
            summaries.append(row)
            save(out / "summary.json", {"complete": False, "runs": summaries})
            print(json.dumps(row), flush=True)
    save(out / "summary.json", {"complete": True, "runs": summaries})
    save(out / "complete.json", {"complete": True, "protocol_sha256": sha(out / "protocol.json"),
                                 "summary_sha256": sha(out / "summary.json"),
                                 "completed_at_unix": time.time()})


if __name__ == "__main__":
    main()
