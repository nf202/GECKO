"""Single-fixture semantic exploration; no multi-seed or statistical claim.

The controller freezes queries and independent row references before launching
isolated workers. Engine setup/index creation occurs only in owned databases.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from gqltest.semantic_suite import fixture, specs, reference, bag, canonical

ENGINES = ("neo4j-2026.09.0", "memgraph-3.13.2-jammy", "kuzu-0.11.3", "ladybug-0.21.2", "falkordb-6.0.3",
           "memgraph-3.14.0-jammy")
MEMGRAPH_WRAPPERS = {"memgraph-3.13.2-jammy": "runtime/jammy-bwrap/memgraph",
                     "memgraph-3.14.0-jammy": "runtime/jammy-bwrap-3.14.0/memgraph"}
FORBIDDEN = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|ALTER|INSERT|COPY|LOAD)\b", re.I)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def alias_returns(query):
    """Only frozen top-level RETURNs, including the two UNION arms, are admitted."""
    # Correlated subqueries are provided already aliased by their template.
    if "CALL {" in query or "EXISTS {" in query:
        return query
    chunks = re.split(r"(\bUNION(?: ALL)?\b)", query)
    for j in range(0, len(chunks), 2):
        prefix, projection = chunks[j].rsplit(" RETURN ", 1)
        suffix = ""
        for token in (" ORDER BY ", " LIMIT "):
            if token in projection:
                projection, tail = projection.split(token, 1)
                suffix = token + tail
                break
        distinct = projection.startswith("DISTINCT ")
        if distinct:
            projection = projection[len("DISTINCT "):]
        fields, start, depth, quote = [], 0, 0, None
        for i, ch in enumerate(projection):
            if quote:
                if ch == quote and (i == 0 or projection[i-1] != "\\"):
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0:
                fields.append(projection[start:i].strip())
                start = i + 1
        fields.append(projection[start:].strip())
        fields = [re.sub(r"\s+AS\s+\w+$", "", f, flags=re.I) + f" AS c{i}" for i, f in enumerate(fields)]
        chunks[j] = prefix + " RETURN " + ("DISTINCT " if distinct else "") + ", ".join(fields) + suffix
    # Rebuilding a projection strips its trailing whitespace. Restore token
    # boundaries around UNION instead of concatenating c0UNION.
    return " ".join(chunk.strip() for chunk in chunks)


def catalog(g):
    cases = []

    def add(key, family, query, expected, contract):
        query = alias_returns(query)
        if FORBIDDEN.search(query) or ";" in query:
            raise ValueError(f"non-read template: {key}")
        cases.append({"id": key, "family": family, "query": query,
                      "expected_rows": expected, "contract": contract})

    for spec in specs():
        add("existing/" + spec["id"], spec["family"], spec["query"], reference(g, spec),
            "Inherited finite-reference semantic contract; differences require dialect adjudication.")

    ns, es = g["nodes"], g["edges"]
    by_id = {n["id"]: n for n in ns}
    for t in (-2, -1, 0, 1, 2):
        selected = [n for n in ns if n["age"] is not None and n["age"] > t]
        add(f"alias-filter/{t}", "scope_alias_filter",
            f"MATCH (a:P) WITH a.id AS aid,a.age AS v WHERE v > {t} RETURN aid,v",
            [[n["id"], n["age"]] for n in selected], "WITH alias filtering under three-valued WHERE.")
        add(f"projection-arithmetic/{t}", "numeric_projection",
            f"MATCH (a:P) RETURN a.id,a.age + ({t}),coalesce(a.age,{t})",
            [[n["id"], None if n["age"] is None else n["age"] + t,
              t if n["age"] is None else n["age"]] for n in ns], "NULL-propagating integer arithmetic on small exact integers.")
        groups = collections.defaultdict(list)
        for n in ns:
            groups[n["flag"]].append(n)
        add(f"group-having/{t}", "group_alias_filter",
            f"MATCH (a:P) WITH a.flag AS f,count(*) AS k WHERE k > {t} RETURN f,k",
            [[flag, len(rows)] for flag, rows in groups.items() if len(rows) > t],
            "Nullable Boolean grouping, followed by an aggregate-alias filter.")
        vals = [n["age"] for n in ns if n["age"] is not None and n["age"] > t]
        add(f"collect-unwind/{t}", "collect_unwind_filter",
            f"MATCH (a:P) WITH collect(a.age) AS xs UNWIND xs AS x WITH x WHERE x > {t} RETURN x",
            [[v] for v in vals], "COLLECT excludes NULL; UNWIND preserves duplicates.")
        add(f"optional-count/{t}", "optional_filtered_aggregate",
            f"MATCH (a:P) OPTIONAL MATCH (a)-[r:R]->(b:P) WHERE b.age > {t} RETURN a.id,count(r.eid)",
            [[n["id"], sum(e["src"] == n["id"] and by_id[e["dst"]]["age"] is not None
                            and by_id[e["dst"]]["age"] > t for e in es)] for n in ns],
            "OPTIONAL-local WHERE preserves each left row when no edge qualifies.")
        add(f"exists-correlated/{t}", "correlated_exists",
            "MATCH (a:P) RETURN a.id AS c0,EXISTS { MATCH (a)-[r:R]->(b:P) "
            f"WHERE b.age > {t} " + "} AS c1",
            [[n["id"], any(e["src"] == n["id"] and by_id[e["dst"]]["age"] is not None
                            and by_id[e["dst"]]["age"] > t for e in es)] for n in ns],
            "Existential subquery returns a Boolean; unsupported syntax remains a boundary.")

    add("scope/rebind", "scope_rebinding",
        "MATCH (a:P) WITH a.id AS x,a.age AS v WITH x AS v,v AS x RETURN v,x",
        [[n["id"], n["age"]] for n in ns], "WITH RHS names resolve in the input scope.")
    add("distinct/null", "distinct_null",
        "MATCH (a:P) WITH DISTINCT a.age AS v RETURN v",
        [[v] for v in {n["age"] for n in ns}], "DISTINCT retains one NULL group.")
    add("distinct/group", "distinct_group_aggregate",
        "MATCH (a:P) WITH DISTINCT a.age AS v RETURN count(*),count(v),sum(v)",
        [[len({n["age"] for n in ns}), len({n["age"] for n in ns if n["age"] is not None}),
          sum({n["age"] for n in ns if n["age"] is not None})]], "DISTINCT then NULL-excluding aggregates.")
    for limit in (0, 1, 5):
        add(f"ordered/{limit}", "ordered_limit",
            f"MATCH (a:P) WITH a ORDER BY a.id DESC LIMIT {limit} RETURN a.id,a.age",
            [[n["id"], n["age"]] for n in sorted(ns, key=lambda n: n["id"], reverse=True)[:limit]],
            "Unique ID provides deterministic total ordering before LIMIT.")
    add("union/nullable", "union_nullable",
        "MATCH (a:P) RETURN a.age AS v UNION MATCH (a:P) RETURN a.age AS v",
        [[v] for v in {n["age"] for n in ns}], "UNION deduplicates NULL and equal numeric values.")
    add("union/nullable-all", "union_all_nullable",
        "MATCH (a:P) RETURN a.age AS v UNION ALL MATCH (a:P) RETURN a.age AS v",
        [[n["age"]] for n in ns] * 2, "UNION ALL preserves row multiplicities including NULL.")
    for direction in ("out", "in"):
        pattern = "(a:P)-[r:R]->(b:P)-[s:R]->(c:P)" if direction == "out" else "(a:P)<-[r:R]-(b:P)<-[s:R]-(c:P)"
        rows = []
        for e in es:
            for f in es:
                if e["eid"] == f["eid"]:
                    continue
                if direction == "out" and e["dst"] == f["src"]:
                    rows.append([e["src"], e["eid"], e["dst"], f["eid"], f["dst"]])
                if direction == "in" and e["src"] == f["dst"]:
                    rows.append([e["dst"], e["eid"], e["src"], f["eid"], f["src"]])
        add("twohop/" + direction, "two_hop_distinct_edges",
            f"MATCH {pattern} WHERE r.eid <> s.eid RETURN a.id,r.eid,b.id,s.eid,c.id",
            rows, "Two-edge enumeration explicitly excludes relationship reuse across dialects.")
        add("twohop-count/" + direction, "two_hop_aggregate",
            f"MATCH {pattern} WHERE r.eid <> s.eid RETURN count(*)", [[len(rows)]],
            "COUNT preserves distinct-edge match multiplicity.")
    add("subquery/count", "correlated_count_subquery",
        "MATCH (a:P) CALL { WITH a MATCH (a)-[r:R]->(b:P) RETURN count(*) AS k } RETURN a.id AS c0,k AS c1",
        [[n["id"], sum(e["src"] == n["id"] for e in es)] for n in ns],
        "A no-grouping aggregate produces one row even for empty correlated input.")
    add("empty/collect", "empty_collect_unwind",
        "MATCH (a:P) WHERE a.id < 0 WITH collect(a.id) AS xs UNWIND xs AS x RETURN x", [],
        "Empty collection unwinds to zero rows.")
    add("optional/project-null", "optional_null_projection",
        "MATCH (a:P) OPTIONAL MATCH (a)-[r:R]->(b:P) WHERE b.id < 0 RETURN a.id,b.id,b.age + 1",
        [[n["id"], None, None] for n in ns], "Failed OPTIONAL match null-extends all target properties.")
    add("minimal/empty-aggregates", "empty_aggregate_control",
        "UNWIND [] AS x RETURN sum(x),collect(x),count(x),count(*)", [[0, [], 0, 0]],
        "Cypher-profile SUM of empty input is 0 and COLLECT is []; dialect adjudication is required.")
    add("minimal/null-aggregates", "null_aggregate_control",
        "UNWIND [NULL] AS x RETURN sum(x),collect(x),count(x),count(*)", [[0, [], 0, 1]],
        "Cypher-profile aggregates ignore NULL; COUNT(*) still counts the input row.")
    add("minimal/null-plus-one", "null_aggregate_positive_control",
        "UNWIND [NULL,1] AS x RETURN sum(x),collect(x),count(x),count(*)", [[1, [1], 1, 2]],
        "Normal aggregate control with one non-NULL numeric value.")
    assert len({c["id"] for c in cases}) == len(cases)
    return cases


def named_execute(engine, query):
    started = time.monotonic()
    try:
        if hasattr(engine, "driver"):
            from neo4j import Query
            with engine.driver.session() as session:
                result = session.run(Query(query, timeout=2.0))
                keys = list(result.keys())
                records = list(result)
                aliases = sorted(keys, key=lambda k: int(k[1:]))
                rows = [[r[k] for k in aliases] for r in records]
        else:
            result = engine.conn.execute(query)
            try:
                keys = list(result.get_column_names())
                aliases = sorted(keys, key=lambda k: int(k[1:]))
                positions = [keys.index(k) for k in aliases]
                rows = []
                while result.has_next():
                    row = result.get_next()
                    rows.append([row[i] for i in positions])
            finally:
                result.close()
        return {"status": "ok", "rows": canonical(rows), "returned_keys": keys,
                "elapsed_s": time.monotonic() - started}
    except Exception as exc:
        detail = str(exc)
        return {"status": "error", "error_type": type(exc).__name__,
                "error_code": getattr(exc, "code", None), "error": detail,
                "elapsed_s": time.monotonic() - started}


def state(engine, g):
    nodeq = alias_returns("MATCH (n:P) RETURN n.id,n.age,n.score,n.name,n.flag")
    edgeq = alias_returns("MATCH (a:P)-[r:R]->(b:P) RETURN r.eid,a.id,b.id,r.weight")
    results = [named_execute(engine, q) for q in (nodeq, edgeq)]
    expected = [[[n[k] for k in ("id", "age", "score", "name", "flag")] for n in g["nodes"]],
                [[e[k] for k in ("eid", "src", "dst", "weight")] for e in g["edges"]]]
    return {"executions": results, "matches": all(r["status"] == "ok" and bag(r["rows"]) == bag(e)
                for r, e in zip(results, expected))}


def evaluate(engine, case, phase):
    first = named_execute(engine, case["query"])
    match = first["status"] == "ok" and bag(first["rows"]) == bag(case["expected_rows"])
    executions = [first]
    if not match:
        executions += [named_execute(engine, case["query"]) for _ in range(2)]
    stable = all(r["status"] == first["status"] and
                 (r["status"] != "ok" or bag(r["rows"]) == bag(first["rows"])) for r in executions)
    if match:
        category = "match"
    elif first["status"] == "ok":
        category = "stable_reference_candidate" if stable else "unstable_signal"
    else:
        message = (str(first.get("error_code")) + " " + first.get("error", "")).lower()
        boundary = any(s in message for s in ("syntax", "parser", "parsing error", "not supported", "not implemented", "binder exception"))
        category = "syntax_or_support_boundary" if boundary else "execution_error_candidate"
    return {**case, "phase": phase, "classification": category, "stable": stable,
            "reference_match": match, "executions": executions,
            "confirmed_database_defect": False}


def worker(name, out, protocol):
    from gqltest.engines import KuzuEngine, BoltEngine
    from campaign_broad_semantic import launch_bolt, stop_bolt
    from campaign_current_path_semantics import launch_neo4j, stop_neo4j
    run = out / "engines" / name
    run.mkdir(parents=True, exist_ok=False)
    g, cases = protocol["graph"], protocol["catalog"]
    engine = proc = log = receipt = None
    result = {"engine": name, "seed": protocol["seed"], "cases": [], "phases": [], "complete": False}
    try:
        if name.startswith("kuzu"):
            engine = KuzuEngine(run / "database")
        elif name.startswith("ladybug"):
            import ladybug
            engine = KuzuEngine.__new__(KuzuEngine)
            engine.name, engine.version = "ladybug", ladybug.__version__
            engine.db = ladybug.Database(str(run / "database"), buffer_pool_size=256*1024**2, max_num_threads=2)
            engine.conn = ladybug.Connection(engine.db, num_threads=2)
            engine.conn.set_query_timeout(2000)
        elif name.startswith("falkordb"):
            from falkordb_frontier_adapter import launch
            engine, proc, log, receipt = launch(run)
        elif name.startswith("neo4j"):
            uri, proc, log, receipt = launch_neo4j(run)
            engine = BoltEngine(uri, "neo4j")
        else:
            uri, version, proc, log, receipt = launch_bolt(name, ROOT / MEMGRAPH_WRAPPERS[name], run)
            engine = BoltEngine(uri, "memgraph", version=version)
        result["reported_version"] = str(engine.version)
        engine.setup(g)
        planned_phases = ["baseline", "indexed"] if hasattr(engine, "driver") or name.startswith("falkordb") else ["baseline"]
        result["planned_phases"] = planned_phases
        for phase in planned_phases:
            if phase == "indexed":
                ddl = "CREATE INDEX frontier_age IF NOT EXISTS FOR (n:P) ON (n.age)" if name.startswith("neo4j") else "CREATE INDEX ON :P(age)"
                result["index_setup"] = engine.execute(ddl)
                if result["index_setup"]["status"] != "ok":
                    result["index_phase_skipped"] = True
                    break
                if name.startswith("neo4j"):
                    result["index_ready"] = engine.execute("CALL db.awaitIndexes(20)")
                    if result["index_ready"]["status"] != "ok":
                        raise RuntimeError("index readiness failed")
                if name.startswith("falkordb"):
                    result["index_ready"] = engine.await_index()
                    if not result["index_ready"]["verified"]:
                        raise RuntimeError("FalkorDB index readiness not established")
                    result["index_catalog"] = result["index_ready"]["catalog"]
                else:
                    result["index_catalog"] = engine.execute("SHOW INDEXES" if name.startswith("neo4j") else "SHOW INDEX INFO")
                save(run / "index_setup.json", {k:v for k,v in result.items() if k.startswith("index_")})
            before = state(engine, g)
            if not before["matches"]:
                raise AssertionError("initial graph state does not match fixture")
            for i, case in enumerate(cases):
                row = evaluate(engine, case, phase)
                result["cases"].append(row)
                with (run / "events.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                if (i + 1) % 25 == 0:
                    print(json.dumps({"engine": name, "phase": phase, "cases": i + 1}), flush=True)
            after = state(engine, g)
            result["phases"].append({"phase": phase, "before": before, "after": after})
            if not after["matches"]:
                raise AssertionError("graph state changed during read-only phase")
        result["complete"] = [x["phase"] for x in result["phases"]] == planned_phases
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
                stop(proc, log, receipt, run)
            elif name.startswith("neo4j"):
                stop_neo4j(proc, log, receipt, run)
            else:
                stop_bolt(name, proc, log, receipt, run)
        else:
            save(run / "ownership.json", {"backend": "embedded", "worker_pid": os.getpid(),
                 "engine": name, "closed": engine is not None, "project_owned": True})
        save(run / "results.json", result)
    print(json.dumps({"engine": name, "complete": result["complete"], "cases": len(result["cases"]),
                      "counts": dict(collections.Counter(c["classification"] for c in result["cases"]))}), flush=True)
    return 0 if result["complete"] else 2


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--seed", type=int, default=20261102)
    p.add_argument("--engines", nargs="+", choices=ENGINES, default=list(ENGINES[:5]))
    p.add_argument("--freeze-only", action="store_true")
    p.add_argument("--worker", choices=ENGINES)
    p.add_argument("--worker-timeout", type=int, default=1200)
    p.add_argument("--case-ids", nargs="+", help="Freeze only these catalog IDs; all use the same one fixture.")
    p.add_argument("--amends-protocol", help="Path/ID of retained earlier protocol, if this is a disclosed correction.")
    p.add_argument("--composed-predicates", action="store_true", help="Add 64 typed predicates crossed with seven contexts.")
    p.add_argument("--structural-frontier", action="store_true", help="Add direct-enumeration path, join and pipeline cases.")
    p.add_argument("--list-frontier", action="store_true", help="Add list, quantifier and ordered OPTIONAL aggregation cases.")
    p.add_argument("--function-frontier", action="store_true", help="Add string/numeric/subquery/map/order cases.")
    p.add_argument("--extensions-only", action="store_true", help="Exclude the inherited base catalog for a new frontier batch.")
    p.add_argument("--graph-shape", default="base", help="base (semantic_suite.fixture) or a fixture_shapes.SHAPES key.")
    args = p.parse_args()
    out = args.output.resolve()
    out.relative_to(ROOT / "runs")
    if args.worker:
        def terminate_worker(signum, frame):
            raise SystemExit(f"worker received signal {signum}")
        signal.signal(signal.SIGTERM, terminate_worker)
        return worker(args.worker, out, json.loads((out / "protocol.json").read_text(encoding="utf-8")))
    if args.graph_shape == "base":
        g = fixture(args.seed)
    else:
        from fixture_shapes import SHAPES
        g = SHAPES[args.graph_shape](args.seed)
    out.mkdir(parents=True, exist_ok=False)
    if args.extensions_only and not (args.structural_frontier or args.composed_predicates or args.list_frontier
                                     or args.function_frontier):
        raise ValueError("extensions-only needs an extension catalog")
    cases = [] if args.extensions_only else catalog(g)
    if args.composed_predicates:
        from semantic_frontier_combinations import cases as composed_cases
        cases += composed_cases(g, args.seed)
    if args.structural_frontier:
        from semantic_structural_frontier import cases as structural_cases
        cases += structural_cases(g)
    if args.list_frontier:
        from semantic_list_frontier import cases as list_cases
        cases += list_cases(g)
    if args.function_frontier:
        from semantic_function_frontier import cases as function_cases
        cases += function_cases(g)
    if args.case_ids:
        selected = set(args.case_ids)
        available = {c["id"] for c in cases}
        if not selected <= available:
            raise ValueError(f"unknown case IDs: {selected - available}")
        cases = [c for c in cases if c["id"] in selected]
    sources = ["scripts/campaign_single_seed_frontier.py", "src/gqltest/semantic_suite.py",
               "src/gqltest/engines.py", "scripts/campaign_current_path_semantics.py", "scripts/campaign_broad_semantic.py"]
    if args.composed_predicates:
        sources.append("scripts/semantic_frontier_combinations.py")
    if args.structural_frontier:
        sources.append("scripts/semantic_structural_frontier.py")
    if args.list_frontier:
        sources.append("scripts/semantic_list_frontier.py")
    if args.function_frontier:
        sources.append("scripts/semantic_function_frontier.py")
    if any(name.startswith("falkordb") for name in args.engines):
        sources.append("scripts/falkordb_frontier_adapter.py")
    if args.graph_shape != "base":
        sources.append("scripts/fixture_shapes.py")
    protocol = {"schema": "gqltest-single-seed-frontier/1", "seed": args.seed, "seed_count": 1,
                "study_role": "single-seed exploratory discovery; no statistical superiority claim",
                "engines": args.engines, "graph": g, "catalog": cases,
                "candidate_source": "Codex-authored templates plus inherited frozen semantic suite",
                "generation_calls": 0, "qwen_used": False, "provider_model_attestation": False,
                "measured_queries_read_only": True, "fixture_and_index_setup_writes": True,
                "candidate_total_repeats": 3, "fresh_instance_replays": 0,
                "case_selection": args.case_ids, "amends_protocol": args.amends_protocol,
                "composed_predicates": args.composed_predicates,
                "structural_frontier": args.structural_frontier, "extensions_only": args.extensions_only,
                "list_frontier": args.list_frontier, "graph_shape": args.graph_shape, "function_frontier": args.function_frontier,
                "comparison": "named columns, row bags, inherited nine-decimal float canonicalization",
                "version_policy": "existing pinned installations; not asserted latest as of execution",
                "known_scope_limits": ["Kuzu and Ladybug are related implementations", "no transaction/durability/performance defect campaign", "a candidate is not a confirmed bug"],
                "source_sha256": {s: sha(ROOT / s) for s in sources}, "frozen_unix": time.time()}
    save(out / "protocol.json", protocol)
    save(out / "freeze_receipt.json", {"protocol_sha256": sha(out / "protocol.json"), "before_engine_calls": True})
    for s in sources:
        dest = out / "source_snapshot" / s
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT / s).read_bytes())
    print(json.dumps({"seed_count": 1, "seed": args.seed, "cases_per_phase": len(cases),
                      "families": len({c['family'] for c in cases}), "engines": args.engines}), flush=True)
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
        counts = collections.Counter(c["classification"] for c in d["cases"])
        summaries.append({"engine": name, "complete": d["complete"], "cases": len(d["cases"]),
                          "by_classification": dict(counts), "phases": [x["phase"] for x in d["phases"]],
                          "state_matches": all(x["before"]["matches"] and x["after"]["matches"] for x in d["phases"]),
                          "run_error": d.get("run_error")})
    summary = {"schema": "gqltest-single-seed-frontier-summary/1", "seed_count": 1, "seed": args.seed,
               "confirmed_new_defects": 0, "by_engine": summaries, "workers": workers,
               "protocol_sha256": sha(out / "protocol.json"), "completed_unix": time.time()}
    save(out / "summary.json", summary)
    print(json.dumps(summary), flush=True)
    return 0 if all(x["complete"] for x in summaries) else 2


if __name__ == "__main__":
    raise SystemExit(main())
