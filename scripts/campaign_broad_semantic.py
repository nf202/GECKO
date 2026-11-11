"""Run an exhaustive held-out semantic workload across isolated graph engines.

This protocol executes the frozen manual14 feature catalog over every supported
context and all four read-only partition branches. It is descriptive evidence:
rejections and reference discrepancies are retained as dialect observations,
not vendor defects.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gqltest.engines import BoltEngine, KuzuEngine
from gqltest.model import CONTEXTS, FEATURES, graph, queries
from gqltest.oracle import bag, partition_holds
from gqltest.path_reference import EDGE_SIMPLE, KUZU_WALK, finite_expected_path
from gqltest.predicate import PredicateError, compile_predicate
from gqltest.reference import expected

SEEDS = (42, 314, 2718)
SPECS = (
    ("kuzu", None),
    ("memgraph-2.10.1", ROOT / "runtime/memgraph-2.10.1/usr/lib/memgraph/memgraph"),
    ("memgraph-3.13.2", ROOT / "runtime/memgraph-3.13.2/usr/lib/memgraph/memgraph"),
    ("neo4j-2025.02.0", Path("/usr/share/neo4j/bin/neo4j")),
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ready(port: int, process: subprocess.Popen, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"engine exited {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError("engine did not become ready")


def launch_bolt(name: str, binary: Path, run: Path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    uri = f"bolt://127.0.0.1:{port}"
    process = None
    env = os.environ.copy()
    if name.startswith("memgraph"):
        if "3.13.2" in name:
            preload = os.environ.get("GECKO_LIBPYTHON_PRELOAD")
            if preload:
                env["LD_PRELOAD"] = preload
        version = subprocess.check_output([str(binary), "--version"], text=True,
                                          stderr=subprocess.STDOUT, env=env).strip()
        command = [
            str(binary), "--bolt-address=127.0.0.1", f"--bolt-port={port}",
            "--bolt-num-workers=2", "--bolt-server-name-for-init=Neo4j/5.11.0",
            "--memory-limit=1024", "--query-execution-timeout-sec=2",
            "--telemetry-enabled=false", "--storage-properties-on-edges=true",
            "--storage-wal-enabled=false", f"--data-directory={run/'data'}",
            f"--log-file={run/'engine.log'}",
        ]
    else:
        conf = run / "conf"
        conf.mkdir(parents=True)
        (run / "plugins").mkdir()
        (conf / "neo4j.conf").write_text(
            f"server.default_listen_address=127.0.0.1\n"
            f"server.bolt.listen_address=127.0.0.1:{port}\n"
            f"server.http.listen_address=127.0.0.1:{port+1}\n"
            "server.https.enabled=false\n"
            f"server.directories.data={run/'data'}\n"
            f"server.directories.logs={run/'logs'}\n"
            f"server.directories.plugins={run/'plugins'}\n"
            f"server.directories.run={run/'run'}\n"
            "server.memory.heap.initial_size=256m\n"
            "server.memory.heap.max_size=512m\n"
            "initial.dbms.default_database=neo4j\n"
            "dbms.security.auth_enabled=false\n"
            "dbms.usage_report.enabled=false\n"
        )
        env.update({"NEO4J_HOME": "/usr/share/neo4j", "NEO4J_CONF": str(conf)})
        version = "neo4j-2025.02.0"
        command = [str(binary), "console"]
    receipt = {
        "project_root": str(ROOT), "engine": name, "uri": uri,
        "version": version, "binary": str(binary),
        "binary_sha256": sha(binary), "command": command,
    }
    log_path = run / "process.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w")
    process = subprocess.Popen(command, cwd=run, env=env, stdout=log,
                               stderr=subprocess.STDOUT,
                               start_new_session=(name.startswith("neo4j")))
    receipt["pid"] = process.pid
    (run / "ownership.json").write_text(json.dumps(receipt, indent=2) + "\n")
    try:
        ready(port, process)
        return uri, version, process, log, receipt
    except BaseException:
        stop_bolt(name, process, log, receipt, run)
        raise


def stop_bolt(name, process, log, receipt, run):
    if process is not None and process.poll() is None:
        if name.startswith("neo4j"):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            if name.startswith("neo4j"):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.wait(timeout=5)
    receipt["engine_exit"] = process.returncode if process else None
    receipt["terminated"] = True
    (run / "ownership.json").write_text(json.dumps(receipt, indent=2) + "\n")
    log.close()


def canonical_rows(rows):
    return [json.loads(x) for x in sorted(
        json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        for row in rows
    )]


def reference_rows(g, feature, context, contract):
    if context.name in ("undirected", "two_edge", "bounded_path"):
        expr = compile_predicate(feature.predicate)
        return finite_expected_path(g, expr, context.name, contract)
    return expected(g, feature.name, context.name)


def execute_case(engine, g, feature, context, contract):
    executions = [engine.execute(q) for q in queries(feature, context)]
    statuses = [item["status"] for item in executions]
    rowbags = [bag(item.get("rows", [])) if item["status"] == "ok" else None
               for item in executions]
    partition = None
    reference = None
    reference_status = "unavailable"
    if all(status == "ok" for status in statuses):
        partition = partition_holds(*[item["rows"] for item in executions])
        try:
            reference = reference_rows(g, feature, context, contract)
            reference_status = "match" if all(
                bag(item["rows"]) == bag(exp)
                for item, exp in zip(executions, reference)
            ) else "dialect_discrepancy"
        except (PredicateError, ValueError) as exc:
            reference_status = f"reference_unavailable:{exc}"
    return {
        "feature": feature.name, "predicate": feature.predicate,
        "context": context.name, "statuses": statuses,
        "partition_holds": partition, "reference_status": reference_status,
        "rows": [len(item.get("rows", [])) for item in executions],
        "rowbags": [sorted(x.elements()) if x is not None else None for x in rowbags],
        "errors": [item.get("error") for item in executions
                   if item["status"] != "ok"],
    }


def run_one(name, binary, seed, out):
    out.mkdir(parents=True, exist_ok=False)
    g = graph(seed)
    (out / "graph.json").write_text(json.dumps(g, indent=2) + "\n")
    contract = KUZU_WALK if name == "kuzu" else EDGE_SIMPLE
    process = log = receipt = None
    engine = None
    try:
        if name == "kuzu":
            version = __import__("kuzu").__version__
            engine = KuzuEngine(out / "database")
            receipt = {"project_root": str(ROOT), "engine": name,
                       "version": version, "terminated": True}
        else:
            uri, version, process, log, receipt = launch_bolt(name, binary, out)
            engine = BoltEngine(uri, "memgraph" if name.startswith("memgraph") else "neo4j",
                                 version=version if name.startswith("memgraph") else None)
        engine.setup(g)
        cases = []
        for feature in FEATURES:
            for context in CONTEXTS:
                cases.append(execute_case(engine, g, feature, context, contract))
        payload = {
            "schema": "gqltest-broad-semantic-run/1", "engine": name,
            "version": str(engine.version), "seed": seed,
            "feature_count": len(FEATURES), "context_count": len(CONTEXTS),
            "case_count": len(cases), "path_contract": contract.name,
            "llm_calls": 0, "qwen_used": False,
            "cases": cases,
        }
        (out / "results.json").write_text(json.dumps(payload, indent=2) + "\n")
        return payload
    finally:
        if engine is not None:
            engine.close()
        if process is not None:
            stop_bolt(name, process, log, receipt, out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root_out = args.output.resolve()
    root_out.mkdir(parents=True, exist_ok=False)
    protocol = {
        "schema": "gqltest-broad-semantic-protocol/1",
        "seeds": list(SEEDS), "features": [f.name for f in FEATURES],
        "contexts": [c.name for c in CONTEXTS],
        "engines": [name for name, _ in SPECS],
        "branches_per_case": 4,
        "interpretation": "held-out descriptive semantic workload; no defect claim",
        "llm_calls": 0, "qwen_used": False,
    }
    (root_out / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
    all_runs = []
    for name, binary in SPECS:
        for seed in SEEDS:
            run = root_out / f"{name}-seed{seed}"
            payload = run_one(name, binary, seed, run)
            all_runs.append(payload)
            print(json.dumps({"engine": name, "seed": seed,
                              "cases": payload["case_count"]}), flush=True)
    flat = []
    for payload in all_runs:
        for case in payload["cases"]:
            flat.append({"engine": payload["engine"], "version": payload["version"],
                         "seed": payload["seed"], **case})
    summary = {
        "schema": "gqltest-broad-semantic-summary/1",
        "runs": len(all_runs), "cases": len(flat),
        "successful_cases": sum(all(x == "ok" for x in c["statuses"]) for c in flat),
        "partition_failures": sum(c["partition_holds"] is False for c in flat),
        "reference_discrepancy_cases": sum(c["reference_status"] == "dialect_discrepancy"
                                           for c in flat),
        "context_rejection_cases": sum("context_rejection" in c["statuses"] for c in flat),
        "unknown_error_cases": sum("unknown_error" in c["statuses"] for c in flat),
        "timeout_cases": sum("timeout" in c["statuses"] for c in flat),
        "qwen_used": False, "llm_calls": 0,
    }
    signature_groups = defaultdict(list)
    for c in flat:
        if all(x == "ok" for x in c["statuses"]):
            signature_groups[(c["seed"], c["feature"], c["context"],
                              tuple(c["rowbags"]))].append(c["engine"])
    cross_engine = []
    for (seed, feature, context, signature), engines in signature_groups.items():
        if len(engines) >= 2:
            cross_engine.append({"seed": seed, "feature": feature,
                                 "context": context, "engines": sorted(engines)})
    summary["successful_signature_groups"] = len(signature_groups)
    summary["cross_engine_agreement_groups"] = len(cross_engine)
    summary["cross_engine_agreement_only"] = all(
        len(item["engines"]) == len(SPECS) for item in cross_engine
    )
    (root_out / "flat_cases.json").write_text(json.dumps(flat, indent=2) + "\n")
    (root_out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    if summary["partition_failures"] or summary["unknown_error_cases"] or summary["timeout_cases"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
