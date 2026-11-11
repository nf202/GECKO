"""Single-fixture write-semantics campaign; no multi-seed or statistical claim.

Every case runs on a freshly reset copy of the fixture inside an owned engine:
reset -> verify fixture state -> one write statement -> read full state (and
W7 probes) -> compare with the frozen Python reference. Measured statements
write by design; they only touch project-owned instances. Templates, the
reference states and sources are frozen before any engine call.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from gqltest.semantic_suite import fixture, bag, canonical
from campaign_single_seed_frontier import save, sha
from semantic_write_frontier import (cases as write_cases, validate, strict_cases, validate_strict,
                                     NODE_STATE, EDGE_STATE)

ENGINES = ("neo4j-2026.09.0", "memgraph-3.13.2-jammy", "falkordb-6.0.3")
BOUNDARY = ("syntax", "parser", "parsing error", "not supported", "not implemented", "unsupported", "unknown function")


def run(engine, query, params=None):
    started = time.monotonic()
    try:
        if hasattr(engine, "driver"):
            from neo4j import Query
            with engine.driver.session() as session:
                result = session.run(Query(query, timeout=5.0), params or {})
                keys = list(result.keys())
                rows = [list(r.values()) for r in result]
                result.consume()
        else:
            r = engine.graph.query(query, params=params, timeout=5000)
            keys = [str(h[1]) for h in (r.header or [])]
            rows = r.result_set
        return {"status": "ok", "rows": canonical(rows), "keys": keys, "elapsed_s": time.monotonic() - started}
    except Exception as exc:
        return {"status": "error", "error_type": type(exc).__name__,
                "error_code": getattr(exc, "code", None), "error": str(exc),
                "elapsed_s": time.monotonic() - started}


def read_state(engine):
    nodes, edges = run(engine, NODE_STATE), run(engine, EDGE_STATE)
    if nodes["status"] != "ok" or edges["status"] != "ok":
        return None, [nodes, edges]
    for row in nodes["rows"]:
        row[6] = sorted(row[6] or [])
    return {"nodes": nodes["rows"], "edges": edges["rows"]}, [nodes, edges]


def same_state(a, b):
    return a is not None and bag(a["nodes"]) == bag(b["nodes"]) and bag(a["edges"]) == bag(b["edges"])


def reset(engine, g, fixture_state):
    log = [run(engine, "MATCH (n) DETACH DELETE n")]
    for n in g["nodes"]:
        log.append(run(engine, "CREATE (:P {id:$id, age:$age, score:$score, name:$name, flag:$flag})", n))
    for e in g["edges"]:
        log.append(run(engine, "MATCH (a:P), (b:P) WHERE a.id=$src AND b.id=$dst "
                               "CREATE (a)-[:R {eid:$eid, weight:$weight}]->(b)", e))
    failed = [x for x in log if x["status"] != "ok"]
    observed, _ = read_state(engine)
    if failed or not same_state(observed, fixture_state):
        raise RuntimeError(f"reset failed: {failed[:1] or 'state mismatch'}")


def run_transaction(engine, spec):
    """Explicit Bolt transaction; stops at the first failed step, then commits or rolls back."""
    steps, ended = [], None
    with engine.driver.session() as session:
        tx = session.begin_transaction()
        try:
            for step in spec["steps"]:
                started = time.monotonic()
                try:
                    result = tx.run(step["query"])
                    rows = [list(r.values()) for r in result]
                    result.consume()
                    steps.append({"status": "ok", "rows": canonical(rows), "elapsed_s": time.monotonic() - started})
                except Exception as exc:
                    steps.append({"status": "error", "error_type": type(exc).__name__,
                                  "error_code": getattr(exc, "code", None), "error": str(exc),
                                  "elapsed_s": time.monotonic() - started})
                    break
            failed = any(s["status"] != "ok" for s in steps)
            if spec["end"] == "commit" and not failed:
                tx.commit()
                ended = "committed"
            else:
                if not tx.closed():
                    tx.rollback()
                ended = "rolled_back"
        except Exception as exc:
            ended = f"end_error: {type(exc).__name__}: {exc}"
        finally:
            if not tx.closed():
                tx.close()
    return {"steps": steps, "ended": ended}


def attempt(engine, g, fixture_state, case):
    reset(engine, g, fixture_state)
    if "transaction" in case:
        write = run_transaction(engine, case["transaction"])
    else:
        write = run(engine, case["query"])
    observed, state_reads = read_state(engine)
    probes = [{"name": p["name"], **run(engine, p["query"])}
              for p in case.get("probes", case.get("after_probes", []))]
    return {"write": write, "state": observed, "state_reads": state_reads if observed is None else None,
            "probes": probes}


def classify_transaction(case, a):
    steps, spec = a["write"]["steps"], case["transaction"]["steps"]
    for got, want in zip(steps, spec):
        if want["expect"] == "error" and got["status"] == "ok":
            return "no_error_raised_boundary"
        if want["expect"] == "ok" and got["status"] != "ok":
            message = (str(got.get("error_code")) + " " + got.get("error", "")).lower()
            return "syntax_or_support_boundary" if any(s in message for s in BOUNDARY) else "execution_error_candidate"
    if len(steps) != len(spec) or a["write"]["ended"].startswith("end_error"):
        return "transaction_control_candidate"
    if not same_state(a["state"], case["expected_state"]):
        return "transaction_state_candidate"
    if any(w["expected_rows"] is not None and bag(g["rows"]) != bag(w["expected_rows"]) for g, w in zip(steps, spec)):
        return "in_transaction_read_candidate"
    if any(p["status"] != "ok" or bag(p["rows"]) != bag(r["expected_rows"])
           for p, r in zip(a["probes"], case["after_probes"])):
        return "probe_reference_candidate"
    return "match"


def classify(case, a):
    write = a["write"]
    if a["state"] is None:
        return "state_read_error"
    if "transaction" in case:
        return classify_transaction(case, a)
    state_ok = same_state(a["state"], case["expected_state"])
    if case["expected_error"]:
        if write["status"] == "ok":
            return "no_error_raised_boundary"
        return "match" if state_ok else "atomicity_candidate"
    if write["status"] != "ok":
        message = (str(write.get("error_code")) + " " + write.get("error", "")).lower()
        return "syntax_or_support_boundary" if any(s in message for s in BOUNDARY) else "execution_error_candidate"
    rows_ok = case["expected_rows"] is None or bag(write["rows"]) == bag(case["expected_rows"])
    probe_ok = all(p["status"] == "ok" and bag(p["rows"]) == bag(ref["expected_rows"])
                   for p, ref in zip(a["probes"], case.get("probes", [])))
    if state_ok and rows_ok and probe_ok:
        return "match"
    if case["contract_uncertain"]:
        for name, alt in case.get("alternative_states", {}).items():
            if same_state(a["state"], alt) and probe_ok:
                return "contract_variant:" + name
        return "contract_uncertain_difference"
    if state_ok and rows_ok:
        return "probe_reference_candidate"
    return "write_reference_candidate"


def signature(a):
    w = a["write"]
    if "steps" in w:
        w = {"status": w["ended"] + "|" + ",".join(s["status"] for s in w["steps"]),
             "rows": [r for s in w["steps"] for r in (s.get("rows") or [])]}
    return json.dumps({"status": w["status"], "rows": w.get("rows") and sorted(bag(w["rows"]).items()),
                       "state": a["state"] and [sorted(bag(a["state"][k]).items()) for k in ("nodes", "edges")],
                       "probes": [(p["status"], p.get("rows") and sorted(bag(p["rows"]).items())) for p in a["probes"]]},
                      sort_keys=True, default=str)


def evaluate(engine, g, fixture_state, case, phase):
    if "transaction" in case and not hasattr(engine, "driver"):
        return {"id": case["id"], "family": case["family"], "phase": phase,
                "classification": "not_run_no_multistatement_transactions", "stable": True, "attempts": [],
                "confirmed_database_defect": False}
    attempts = [attempt(engine, g, fixture_state, case)]
    category = classify(case, attempts[0])
    if category != "match":
        attempts += [attempt(engine, g, fixture_state, case) for _ in range(2)]
    stable = len({signature(a) for a in attempts}) == 1 and len({classify(case, a) for a in attempts}) == 1
    return {"id": case["id"], "family": case["family"], "phase": phase, "classification": category,
            "stable": stable, "attempts": attempts, "confirmed_database_defect": False}


def worker(name, out, protocol):
    from gqltest.engines import BoltEngine
    from campaign_broad_semantic import launch_bolt, stop_bolt
    from campaign_current_path_semantics import launch_neo4j, stop_neo4j
    run_dir = out / "engines" / name
    run_dir.mkdir(parents=True, exist_ok=False)
    g, catalog, fixture_state = protocol["graph"], protocol["catalog"], protocol["fixture_state"]
    engine = proc = log = receipt = None
    result = {"engine": name, "seed": protocol["seed"], "cases": [], "phases": [], "complete": False}
    try:
        if name.startswith("falkordb"):
            from falkordb_frontier_adapter import launch
            engine, proc, log, receipt = launch(run_dir)
        elif name.startswith("neo4j"):
            uri, proc, log, receipt = launch_neo4j(run_dir)
            engine = BoltEngine(uri, "neo4j")
        else:
            uri, version, proc, log, receipt = launch_bolt(name, ROOT / "runtime/jammy-bwrap/memgraph", run_dir)
            engine = BoltEngine(uri, "memgraph", version=version)
        result["reported_version"] = str(engine.version)
        engine.setup(g)  # refuses a nonempty instance: proves the instance is owned and fresh
        for phase in ("baseline", "indexed"):
            if phase == "indexed":
                ddl = ("CREATE INDEX frontier_age IF NOT EXISTS FOR (n:P) ON (n.age)" if name.startswith("neo4j")
                       else "CREATE INDEX ON :P(age)")
                result["index_setup"] = run(engine, ddl)
                if result["index_setup"]["status"] != "ok":
                    raise RuntimeError("index creation failed")
                if name.startswith("neo4j"):
                    result["index_ready"] = run(engine, "CALL db.awaitIndexes(20)")
                    result["index_catalog"] = run(engine, "SHOW INDEXES")
                elif name.startswith("falkordb"):
                    ready = engine.await_index()
                    if not ready["verified"]:
                        raise RuntimeError("FalkorDB index readiness not established")
                    result["index_catalog"] = ready["catalog"]
                else:
                    result["index_catalog"] = run(engine, "SHOW INDEX INFO")
                save(run_dir / "index_setup.json", {k: v for k, v in result.items() if k.startswith("index_")})
            for i, case in enumerate(catalog):
                row = evaluate(engine, g, fixture_state, case, phase)
                result["cases"].append(row)
                with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                if (i + 1) % 10 == 0:
                    print(json.dumps({"engine": name, "phase": phase, "cases": i + 1}), flush=True)
            reset(engine, g, fixture_state)
            result["phases"].append({"phase": phase, "final_reset_verified": True})
        result["complete"] = [x["phase"] for x in result["phases"]] == ["baseline", "indexed"]
    except BaseException as exc:
        result["run_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if engine:
            try:
                engine.close()
            except Exception as exc:
                result["close_error"] = str(exc)
        if proc:
            if name.startswith("falkordb"):
                from falkordb_frontier_adapter import stop
                stop(proc, log, receipt, run_dir)
            elif name.startswith("neo4j"):
                stop_neo4j(proc, log, receipt, run_dir)
            else:
                stop_bolt(name, proc, log, receipt, run_dir)
        save(run_dir / "results.json", result)
    print(json.dumps({"engine": name, "complete": result["complete"], "cases": len(result["cases"]),
                      "counts": dict(collections.Counter(c["classification"] for c in result["cases"]))}), flush=True)
    return 0 if result["complete"] else 2


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--seed", type=int, default=20261102)
    p.add_argument("--engines", nargs="+", choices=ENGINES, default=list(ENGINES))
    p.add_argument("--freeze-only", action="store_true")
    p.add_argument("--worker", choices=ENGINES)
    p.add_argument("--worker-timeout", type=int, default=2400)
    p.add_argument("--catalog", choices=("base", "strict"), default="base",
                   help="base: W1/W6/W7 batch; strict: runtime-only errors, controls and explicit transactions")
    args = p.parse_args()
    out = args.output.resolve()
    out.relative_to(ROOT / "runs")
    if args.worker:
        def terminate_worker(signum, frame):
            raise SystemExit(f"worker received signal {signum}")
        signal.signal(signal.SIGTERM, terminate_worker)
        return worker(args.worker, out, json.loads((out / "protocol.json").read_text(encoding="utf-8")))
    out.mkdir(parents=True, exist_ok=False)
    g = fixture(args.seed)
    catalog = write_cases(g) if args.catalog == "base" else strict_cases(g)
    power = (validate if args.catalog == "base" else validate_strict)(catalog, g)
    from semantic_write_frontier import Model
    sources = ["scripts/campaign_single_seed_writes.py", "scripts/semantic_write_frontier.py",
               "scripts/campaign_single_seed_frontier.py", "src/gqltest/semantic_suite.py", "src/gqltest/engines.py",
               "scripts/campaign_current_path_semantics.py", "scripts/campaign_broad_semantic.py",
               "scripts/falkordb_frontier_adapter.py", "tests/test_semantic_write_frontier.py"]
    protocol = {"schema": "gqltest-single-seed-writes/1", "seed": args.seed, "seed_count": 1,
                "study_role": "single-seed exploratory write-semantics discovery; no statistical claim",
                "engines": args.engines, "graph": g, "fixture_state": Model(g).state(),
                "catalog_name": args.catalog, "catalog": catalog,
                "pre_freeze_power_checks": power,
                "candidate_source": "Claude-authored templates with independent Python reference model",
                "generation_calls": 0, "qwen_used": False,
                "measured_statements_write": True, "writes_only_in_owned_instances": True,
                "per_case_reset": "DETACH DELETE all, reload fixture, verify full state",
                "phases": ["baseline", "indexed :P(age)"], "candidate_total_repeats": 3,
                "classification_rules": {
                    "atomicity_candidate": "expected runtime error raised but state differs from fixture",
                    "no_error_raised_boundary": "engine did not raise the expected error; not an atomicity candidate",
                    "contract_variant": "uncertain template matched a declared alternative",
                    "write_reference_candidate": "write succeeded but state or returned rows differ from reference",
                    "probe_reference_candidate": "state matches but a follow-up read differs",
                    "transaction_state_candidate": "explicit transaction ended but final state differs from reference",
                    "in_transaction_read_candidate": "a read inside the open transaction differs from reference",
                    "transaction_control_candidate": "commit/rollback failed or a step was skipped unexpectedly",
                    "not_run_no_multistatement_transactions": "FalkorDB exposes no explicit multi-statement transaction"},
                "known_scope_limits": ["single host/kernel", "no concurrency or durability", "a candidate is not a confirmed bug",
                                       "public-issue correspondence is checked after execution"],
                "source_sha256": {s: sha(ROOT / s) for s in sources}, "frozen_unix": time.time()}
    save(out / "protocol.json", protocol)
    save(out / "freeze_receipt.json", {"protocol_sha256": sha(out / "protocol.json"), "before_engine_calls": True})
    for s in sources:
        dest = out / "source_snapshot" / s
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT / s).read_bytes())
    print(json.dumps({"seed": args.seed, "cases_per_phase": len(catalog), "power": power, "engines": args.engines}), flush=True)
    if args.freeze_only:
        return 0
    workers = []
    for name in args.engines:
        with (out / (name + ".worker.log")).open("w", encoding="utf-8") as stream:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--output", str(out),
                                        "--worker", name], cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
            save(out / (name + ".worker.json"), {"pid": process.pid, "started_unix": time.time()})
            try:
                code = process.wait(timeout=args.worker_timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    code = process.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    process.kill()
                    code = process.wait(timeout=10)
            workers.append({"engine": name, "exit_code": code})
            save(out / "workers.json", workers)
            print(json.dumps(workers[-1]), flush=True)
    summaries = []
    for name in args.engines:
        path = out / "engines" / name / "results.json"
        if not path.exists():
            summaries.append({"engine": name, "complete": False, "failure": "worker did not produce results"})
            continue
        d = json.loads(path.read_text(encoding="utf-8"))
        summaries.append({"engine": name, "complete": d["complete"], "cases": len(d["cases"]),
                          "by_classification": dict(collections.Counter(c["classification"] for c in d["cases"])),
                          "unstable": sum(not c["stable"] for c in d["cases"]), "run_error": d.get("run_error")})
    summary = {"schema": "gqltest-single-seed-writes-summary/1", "seed_count": 1, "seed": args.seed,
               "confirmed_new_defects": 0, "by_engine": summaries, "workers": workers,
               "protocol_sha256": sha(out / "protocol.json"), "completed_unix": time.time()}
    save(out / "summary.json", summary)
    print(json.dumps(summary), flush=True)
    return 0 if all(x.get("complete") for x in summaries) else 2


if __name__ == "__main__":
    raise SystemExit(main())
