"""Single-seed evolving-graph discovery campaign; no multi-seed or statistical claim.

One trajectory from one seed. Each generation proposes children of the current
graph with graph_evolution.mutate, freezes every child's graph and full template
catalog (references recomputed in Python) before any engine call, runs the
catalog on long-lived owned instances (full reset + state check per child), and
selects the child with the most new unexplained signals, then new known signal
labels, then new result shapes. Operators of a selected child that produced new
signals gain weight. Measured queries are read-only; resets write only owned
instances.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
from pathlib import Path
import random
import sys
import time
from gecko_identity import FeatureIds, IdentityError, unique_index, parse_reserved

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "src/gqltest").is_dir() or not (ROOT / "scripts/java").is_dir():
    raise RuntimeError("Gecko runner must be deployed in the project scripts directory")
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from gecko_runtime_guard import startup_lock
from gqltest.semantic_suite import fixture, bag, canonical
from campaign_single_seed_frontier import save, sha, named_execute, catalog as base_catalog, FORBIDDEN
from campaign_single_seed_writes import reset
from semantic_write_frontier import Model
from graph_evolution import mutate, describe, OPERATORS, LIMITS

ENGINES = {"neo4j-2026.09.0": None, "memgraph-3.14.0-jammy": "runtime/jammy-bwrap-3.14.0/memgraph",
           "falkordb-6.0.3": None}
MAX_ROWS = 3000
BOUNDARY = ("syntax", "parser", "parsing error", "not supported", "not implemented", "unsupported",
            "invalid input", "unbounded variables are not allowed", "unknown function",
            "no signature of method", "missingmethodexception")
TIMEOUT = ("timeout", "timed out", "exceeded", "terminated", "interrupted")
SOURCES = ["scripts/campaign_evolving_graph.py", "scripts/graph_evolution.py", "scripts/campaign_single_seed_frontier.py",
           "scripts/campaign_single_seed_writes.py", "scripts/semantic_write_frontier.py", "src/gqltest/semantic_suite.py",
           "scripts/semantic_frontier_combinations.py", "scripts/semantic_structural_frontier.py",
           "scripts/semantic_list_frontier.py", "scripts/semantic_function_frontier.py", "src/gqltest/engines.py",
           "scripts/falkordb_frontier_adapter.py", "scripts/campaign_broad_semantic.py",
           "scripts/campaign_current_path_semantics.py"]
EXTRA_SOURCES = ["scripts/gecko_runtime_guard.py", "scripts/gecko_identity.py", "scripts/gecko_ast.py", "scripts/gecko_llm.py", "scripts/llm_queue.py", "scripts/fixture_shapes.py",
                 "scripts/gecko_gremlin.py", "scripts/gremlin_engines.py", "scripts/java/GremlinBatchRunner.java"]


def build_catalog(g, seed):
    from semantic_frontier_combinations import cases as composed
    from semantic_structural_frontier import cases as structural
    from semantic_list_frontier import cases as lists
    from semantic_function_frontier import cases as functions
    cases = base_catalog(g) + composed(g, seed) + structural(g) + lists(g) + functions(g, strict=False)
    seen, out, skipped = set(), [], 0
    for c in cases:
        if FORBIDDEN.search(c["query"]):
            raise ValueError("write in measured query: " + c["id"])
        if c["query"] in seen:
            continue
        seen.add(c["query"])
        if len(c["expected_rows"]) > MAX_ROWS:
            skipped += 1
            continue
        out.append(c)
    return out, skipped


def known_label(engine, case):
    """Post-hoc labels for signal families already triaged in earlier batches."""
    q, f, i = case["query"], case["family"], case["id"]
    if engine.startswith("memgraph"):
        if f.startswith("bounded_trail") and "/undirected/" in i:
            return "memgraph#5109-undirected-self-loop"
        if " IN " in q and "NULL" in q.upper():
            return "memgraph#5042-in-null-missing"
        if "toUpper(" in q or "toLower(" in q:
            return "memgraph#4427-ascii-only-case-mapping"
    if engine.startswith("falkordb"):
        if "toInteger" in q:
            return "candidate:falkordb-tointeger-floor"
        if "=~" in q:
            return "falkordb#2603-regex-substring"
        if f.startswith("relationship_scope_split"):
            return "falkordb#2982-consecutive-match-scope"
        if f.startswith("bounded_trail") and "/in/" in i:
            return "falkordb#3451-incoming-path-order"
        if f in ("collect_size", "edge_aggregate", "correlated_count_subquery", "pattern_comprehension"):
            return "falkordb-parallel-edge-multiplicity-family"
        if "[(n)-[:R]" in q or "[(n)<-[:R]" in q:
            return "falkordb-parallel-edge-multiplicity-family"
    if f.startswith("gremlin_") and ".substring(-" in q and engine.startswith(("tinkergraph", "janusgraph")):
        return "candidate:tinkerpop-substring-negative-empty"
    if case.get("zero_sensitive"):
        return "candidate:tinkerpop-signed-zero-equality"
    if engine.startswith("janusgraph") and f == "gremlin_order":
        return "janusgraph#5107-order-keeps-missing-key"
    if engine.startswith("neo4j") and "trim(" in q:
        return "dialect:neo4j-trim-strips-java-whitespace"
    if engine.startswith("neo4j") and "range(" in q:
        return "neo4j#13990-range-step-direction"
    if engine.startswith("neo4j") and "ORDER BY" in q and "score" in q:
        return "neo4j#13985-signed-zero-order"
    return None


def shape(r):
    if r["status"] != "ok":
        return ("err", r.get("error_type"))
    return ("ok", len(r["rows"]), sum(v is None for row in r["rows"] for v in (row if isinstance(row, list) else [row])))


def run_catalog(engine, cases):
    unique_index(cases, context="Cypher execution batch")
    rows = []
    for c in cases:
        first = named_execute(engine, c["query"])
        if c.get("expected_error"):
            match = first["status"] != "ok"
        else:
            match = first["status"] == "ok" and bag(first["rows"]) == bag(c["expected_rows"])
        runs = [first]
        if not match:
            runs += [named_execute(engine, c["query"]) for _ in range(2)]
        # Unordered queries may return rows in any order; compare repeated runs as bags.
        stable = len({json.dumps([r["status"], r.get("rows") is not None and sorted(bag(r["rows"]).items()),
                                  r.get("error")], default=str) for r in runs}) == 1
        rows.append({"id": c["id"], "match": match, "stable": stable, "runs": runs})
    return rows


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def approx_equal(a, b):
    """Values equal up to relative 1e-12 on floats: absorbs summation-order ulps (float sum/avg
    is not associative) and FalkorDB's 15-digit double formatting. Numbers compare by value, as in
    the exact bag comparison (semantic_suite normalizes integral floats to int); bools never
    equal numbers."""
    if isinstance(a, float) or isinstance(b, float):
        if not (_num(a) and _num(b)):
            return False
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=0.0) or a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(approx_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(approx_equal(a[k], b[k]) for k in a)
    return a == b


def _round9(v):
    if isinstance(v, float):
        return round(v, 9)
    if isinstance(v, list):
        return [_round9(x) for x in v]
    if isinstance(v, dict):
        return {k: _round9(x) for k, x in v.items()}
    return v


def approx_bag_equal(xs, ys):
    """Multiset equality of result rows under approx_equal (greedy matching; mismatch path only).
    Floats on both sides are rounded to 9 decimal places as in the suite's canonical form but stay
    floats, so the relative tolerance still applies where canonical() would turn an integral
    float into an int (e.g. 2000002147483655.0 vs FalkorDB's 15-digit 2000002147483660)."""
    if len(xs) != len(ys):
        return False
    if bag(xs) == bag(ys):
        return True
    xs, ys = _round9(xs), _round9(ys)
    # Maximum bipartite matching (Kuhn): greedy pairing fails when neighbouring groups are within
    # tolerance of each other (e.g. -2147483648, -2147483648.00195, -2147483648.0039).
    cand = [[j for j, y in enumerate(ys) if approx_equal(x, y)] for x in xs]
    owner = [-1] * len(ys)

    def augment(i, seen):
        for j in cand[i]:
            if j not in seen:
                seen.add(j)
                if owner[j] < 0 or augment(owner[j], seen):
                    owner[j] = i
                    return True
        return False

    sys.setrecursionlimit(max(10000, 4 * len(xs) + 100))
    return all(cand[i] and augment(i, set()) for i in range(len(xs)))


def classify(child_results, cases):
    """Per (engine, case): match / boundary / timeout / float_precision / reference_suspect /
    known:<label> / unexplained."""
    by_case = unique_index(cases, context="classification catalog")
    out = {}
    for cid, c in by_case.items():
        res = {e: child_results[e][cid] for e in child_results if cid in child_results[e]}
        ok_rows = {e: json.dumps(sorted(bag(r["runs"][0]["rows"]).items())) for e, r in res.items()
                   if r["runs"][0]["status"] == "ok"}
        all_agree_wrong = (len(ok_rows) == len(res) >= 2 and len(set(ok_rows.values())) == 1
                           and not any(r["match"] for r in res.values()))
        for e, r in res.items():
            if r["match"]:
                continue
            first = r["runs"][0]
            msg = (str(first.get("error_code")) + " " + str(first.get("error", ""))).lower()
            if first["status"] != "ok" and any(s in msg for s in TIMEOUT):
                label = "timeout"
            elif first["status"] != "ok" and any(s in msg for s in BOUNDARY):
                label = "boundary"
            elif (first["status"] == "ok" and not c.get("expected_error")
                  and approx_bag_equal(first["rows"], c["expected_rows"])):
                label = "float_precision"
            elif all_agree_wrong and not c.get("zero_sensitive"):
                label = "reference_suspect"
            elif not r["stable"]:
                label = "unstable"
            else:
                k = known_label(e, c)
                label = "known:" + k if k else "unexplained"
            out[(e, cid)] = label
    return out


RECALL_ENGINES = {"neo4j-2025.02.0": "/usr/share/neo4j/bin/neo4j",
                  "memgraph-3.13.2-jammy": "runtime/jammy-bwrap/memgraph"}


def launch_all(out, engines=ENGINES):
    from gqltest.engines import BoltEngine
    from campaign_broad_semantic import launch_bolt
    from campaign_current_path_semantics import launch_neo4j
    from falkordb_frontier_adapter import launch
    handles = {}
    try:
        with startup_lock(ROOT):
            for name, wrapper in engines.items():
                run = out / "engines" / name
                run.mkdir(parents=True)
                if name.startswith("falkordb"):
                    engine, proc, log, receipt = launch(run)
                elif name == "neo4j-2026.09.0":
                    uri, proc, log, receipt = launch_neo4j(run)
                    handles[name] = (None, proc, log, receipt, run)
                    engine = BoltEngine(uri, "neo4j")
                elif name.startswith("neo4j"):  # older release via the shared Bolt launcher (recall benchmark)
                    uri, version, proc, log, receipt = launch_bolt(name, ROOT / wrapper, run)
                    handles[name] = (None, proc, log, receipt, run)
                    engine = BoltEngine(uri, "neo4j", version=version)
                else:
                    uri, version, proc, log, receipt = launch_bolt(name, ROOT / wrapper, run)
                    handles[name] = (None, proc, log, receipt, run)
                    engine = BoltEngine(uri, "memgraph", version=version)
                handles[name] = (engine, proc, log, receipt, run)
    except BaseException:
        stop_all(handles)
        raise
    return handles


def stop_all(handles):
    from campaign_broad_semantic import stop_bolt
    from campaign_current_path_semantics import stop_neo4j
    from falkordb_frontier_adapter import stop
    for name, (engine, proc, log, receipt, run) in handles.items():
        try:
            engine.close()
        except Exception:
            pass
        if name.startswith("falkordb"):
            stop(proc, log, receipt, run)
        elif name.startswith("neo4j"):
            stop_neo4j(proc, log, receipt, run)
        else:
            stop_bolt(name, proc, log, receipt, run)


def build_pool_cases(g, pool):
    """Admit every generated feature on graph g; returns cases and per-feature rejection reasons."""
    from gecko_ast import admit as admit_feature
    unique_index(pool, key="fid", context="feature pool")
    cases, rejected = [], {}
    for f in pool:
        case, why = admit_feature(f, g, MAX_ROWS)
        if case is None:
            rejected[f["fid"]] = why
        else:
            cases.append(case)
    return cases, rejected


def run_gremlin(g, cases, cdir):
    """All Gremlin engines on graph g (fresh embedded graph per batch); mismatches rerun twice."""
    unique_index(cases, context="Gremlin execution batch")
    from gecko_gremlin import load_script
    from gremlin_engines import ENGINES as GREMLIN, run_batch, rows as grows
    results, errors, executions = {}, {}, 0
    script = load_script(g)
    for name in GREMLIN:
        try:
            load, res = run_batch(name, script, [(c["id"], c["query"]) for c in cases], cdir / "gremlin")
            if load["status"] != "ok":
                raise RuntimeError("load failed: " + str(load.get("error")))
            runs = {c["id"]: [res[c["id"]]] for c in cases}
            for r in res.values():
                r["rows"] = grows(r["result"]) if r["status"] == "ok" else None
            bad = [c for c in cases if not (runs[c["id"]][0]["status"] == "ok"
                   and bag(runs[c["id"]][0]["rows"]) == bag(c["expected_rows"]))]
            for rep in (1, 2):
                if not bad:
                    break
                _l, again = run_batch(name, script, [(c["id"], c["query"]) for c in bad], cdir / f"gremlin-rerun{rep}")
                for c in bad:
                    r = again[c["id"]]
                    r["rows"] = grows(r["result"]) if r["status"] == "ok" else None
                    runs[c["id"]].append(r)
            out = []
            for c in cases:
                rs = runs[c["id"]]
                match = rs[0]["status"] == "ok" and bag(rs[0]["rows"]) == bag(c["expected_rows"])
                stable = len({json.dumps([r["status"], r["rows"] is not None and sorted(bag(r["rows"]).items()),
                                          r.get("error")], default=str) for r in rs}) == 1
                out.append({"id": c["id"], "match": match, "stable": stable, "runs": rs})
            executions += sum(len(r["runs"]) for r in out)
            results[name] = unique_index(out, context="Gremlin results", expected=[c["id"] for c in cases])
            save(cdir / f"{name}.results.json", out)
        except IdentityError:
            raise
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
    return results, errors, executions


def disagreements(results, cases):
    """Case ids whose successful engine results differ from each other."""
    out = set()
    for c in cases:
        bags = [rs[c["id"]]["runs"][0]["rows"] for rs in results.values()
                if c["id"] in rs and rs[c["id"]]["runs"][0]["status"] == "ok"]
        if any(not approx_bag_equal(bags[0], b) for b in bags[1:]):
            out.add(c["id"])
    return out


def main():
    import graph_evolution
    from llm_queue import request as llm_request
    from gecko_llm import (MINING_SCHEMA, GEN_SCHEMA, mining_prompt, sanitize_values, generation_prompt,
                           graph_diff, parse_features)
    from gecko_ast import cell as feature_cell, check as check_feature, Invalid
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--seed", type=int, default=20261102)
    p.add_argument("--generations", type=int, default=8)
    p.add_argument("--children", type=int, default=3)
    p.add_argument("--start", choices=("base", "dense30"), default="base")
    p.add_argument("--no-mutation", action="store_true", help="config D: keep the start graph fixed")
    p.add_argument("--generate", action="store_true", help="AI-1: targeted feature generation each generation")
    p.add_argument("--mine-values", action="store_true", help="AI-2: documentation-informed value pools")
    p.add_argument("--selection", choices=("shapes", "disagreement", "random"), default="disagreement")
    p.add_argument("--budget", type=int, default=0, help="stop after this many engine executions (0 = none)")
    p.add_argument("--hours", type=float, default=0, help="stop after this wall-clock time (0 = none)")
    p.add_argument("--max-llm-calls", type=int, default=0,
                   help="model-call cap; after it the search continues with the existing feature pool (0 = none)")
    p.add_argument("--score", choices=("explained", "raw"), default="explained",
                   help="raw = ablation without explanation-aware feedback: every new mismatch counts")
    p.add_argument("--no-discriminative", action="store_true", help="ablation: admit features with delta < 2")
    p.add_argument("--llm-timeout", type=int, default=900)
    p.add_argument("--stop-on-unexplained", action="store_true")
    p.add_argument("--gremlin", action="store_true", help="also test Gremlin engines (Gremlin profile of the features)")
    p.add_argument("--no-cypher", action="store_true", help="Gremlin engines only")
    p.add_argument("--contract", choices=("v1", "v2"), default="v1",
                   help="v2 (Gecko-FE): boolean orderability, substring past end, signed-zero ties, expected errors")
    p.add_argument("--evolve-features", action="store_true", help="Gecko-FE: model-free AST mutation of the pool")
    p.add_argument("--mutants", type=int, default=12, help="mutants proposed per generation")
    p.add_argument("--pool-cap", type=int, default=400, help="evict never-productive features above this size")
    p.add_argument("--repair", action="store_true", help="Gecko-FE: feed rejected proposals back to the model")
    p.add_argument("--engine-set", choices=("current", "recall"), default="current",
                   help="recall = affected releases of the frozen recall benchmark (Neo4j 2025.02.0, Memgraph 3.13.2)")
    a = p.parse_args()
    out = a.output.resolve()
    out.relative_to(ROOT / "runs")
    out.mkdir(parents=True, exist_ok=False)
    rng = random.Random(a.seed)
    if a.start == "dense30":
        from fixture_shapes import dense
        parent = dense(a.seed)
    else:
        parent = fixture(a.seed)
    weights = {o: 1.0 for o in OPERATORS}
    from gremlin_engines import ENGINES as GREMLIN_ENGINES
    from gecko_gremlin import gadmit, seed_features
    cypher_engines = {} if a.no_cypher else (RECALL_ENGINES if a.engine_set == "recall" else ENGINES)
    gremlin_on = a.gremlin or a.no_cypher
    import gecko_ast
    from gecko_evolve import mutate as mutate_feature, FeatureStats
    gecko_ast.V2 = a.contract == "v2"
    fstats, evo_log, last_rejected = FeatureStats(), [], []
    if a.no_discriminative:
        gecko_ast.MIN_DELTA = 0
    deadline = time.monotonic() + a.hours * 3600 if a.hours else None
    config = {"contract": a.contract, "evolve_features": a.evolve_features, "mutants": a.mutants,
              "pool_cap": a.pool_cap, "repair": a.repair, "score_mode": a.score, "discriminative": not a.no_discriminative, "hours": a.hours,
              "cypher": not a.no_cypher, "gremlin": gremlin_on, "generate": a.generate, "mine_values": a.mine_values, "selection": a.selection,
              "mutation": not a.no_mutation, "start": a.start, "budget": a.budget}
    protocol = {"schema": "gecko-trajectory/1", "seed": a.seed, "seed_count": 1,
                "engines": list(cypher_engines) + (list(GREMLIN_ENGINES) if gremlin_on else []),
                "generations": a.generations, "children": 1 if a.no_mutation else a.children, "config": config,
                "limits": LIMITS, "max_expected_rows": MAX_ROWS, "phase": "baseline (no index)",
                "score": {"alpha": 1000, "beta": 10, "gamma": 1,
                          "gamma_term": "new result shapes" if a.selection == "shapes" else "new cross-engine disagreements"},
                "operator_feedback": {"eta": 0.9, "lambda": 1.5},
                "measured_queries_read_only": True, "qwen_used": False,
                "model_policy": "local Codex via file queue; model never receives expected results",
                "source_sha256": {s: sha(ROOT / s) for s in SOURCES + EXTRA_SOURCES}, "started_unix": time.time()}
    save(out / "protocol.json", protocol)
    for s in SOURCES + EXTRA_SOURCES:
        dest = out / "source_snapshot" / s
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT / s).read_bytes())

    if a.mine_values:
        resp = llm_request(out, "mine", mining_prompt(list(ENGINES)), MINING_SCHEMA, a.llm_timeout)
        if resp.get("status") != "ok":
            save(out / "invalid_model_response.json", resp)
            raise RuntimeError("Invalid model mining response: " + str(resp.get("status")))
        mined = sanitize_values(resp.get("output"))
        graph_evolution.AGES[:0] = [v for v in mined["int"] if v not in graph_evolution.AGES]
        graph_evolution.SCORES[:0] = [v for v in mined["float"] if v not in graph_evolution.SCORES]
        graph_evolution.NAMES[:0] = [v for v in mined["str"] if v not in graph_evolution.NAMES]
        save(out / "value_pools.json", {"mined": mined, "status": resp.get("status"),
                                         "pools": {"age": graph_evolution.AGES, "score": graph_evolution.SCORES,
                                                   "name": graph_evolution.NAMES}})

    seen_shapes, seen_signals, seen_labels, seen_dis, seen_raw = set(), set(), set(), set(), set()
    pool, pool_queries, cells = [], [], {}
    feature_ids = FeatureIds(out / "feature_identity.json")
    history, unexplained_all, gen_log = [], [], []
    executions = 0
    prev_graph = parent
    handles = launch_all(out, cypher_engines)
    try:
        for gen in range(1, a.generations + 1):
            gdir = out / f"gen{gen:02d}"
            children = []
            n_children = 1 if a.no_mutation else a.children
            for k in range(n_children):
                explore = k == n_children - 1
                if a.no_mutation:
                    child, ops = json.loads(json.dumps(parent)), []
                else:
                    child, ops = mutate(parent, rng, {o: 1.0 for o in OPERATORS} if explore else weights,
                                        rng.randint(2, 4))
                cases, skipped = build_catalog(child, a.seed) if cypher_engines else ([], 0)
                gen_cases, rejected = build_pool_cases(child, pool) if cypher_engines else ([], {})
                cases += gen_cases
                gcases, grejected = [], {}
                if gremlin_on:
                    for f in seed_features() + pool:
                        gc, why = gadmit(f, child, MAX_ROWS)
                        (gcases.append(gc) if gc else grejected.__setitem__(f["fid"], why))
                unique_index(cases, context="frozen Cypher catalog")
                unique_index(gcases, context="frozen Gremlin catalog")
                unique_index(seed_features() + pool, key="fid", context="seed/generated pool")
                cdir = gdir / f"child{k}"
                save(cdir / "graph.json", child)
                save(cdir / "ops.json", {"explore": explore, "ops": ops, "shape": describe(child)})
                save(cdir / "catalog.json", cases)
                save(cdir / "pool_rejections.json", rejected)
                save(cdir / "gremlin_catalog.json", gcases)
                save(cdir / "gremlin_rejections.json", grejected)
                children.append({"k": k, "graph": child, "ops": ops, "cases": cases, "gcases": gcases, "skipped": skipped,
                                 "dir": cdir, "explore": explore, "generated": len(gen_cases)})
            save(gdir / "freeze_receipt.json", {"before_engine_calls": True,
                 "catalog_sha256": {c["k"]: sha(c["dir"] / "catalog.json") for c in children},
                 "gremlin_catalog_sha256": {c["k"]: sha(c["dir"] / "gremlin_catalog.json") for c in children}})
            for c in children:
                results, errors = {}, {}
                for name, (engine, *_rest) in handles.items():
                    try:
                        reset(engine, c["graph"], Model(c["graph"]).state())
                        rows = run_catalog(engine, c["cases"])
                        executions += sum(len(r["runs"]) for r in rows)
                        results[name] = unique_index(rows, context="Cypher results", expected=[x["id"] for x in c["cases"]])
                        save(c["dir"] / f"{name}.results.json", rows)
                    except IdentityError:
                        raise
                    except Exception as exc:
                        errors[name] = f"{type(exc).__name__}: {exc}"
                labels = classify(results, c["cases"])
                gdis = set()
                if gremlin_on and c["gcases"]:
                    gres, gerr, gexe = run_gremlin(c["graph"], c["gcases"], c["dir"])
                    executions += gexe
                    errors.update(gerr)
                    labels.update(classify(gres, c["gcases"]))
                    gdis = disagreements(gres, c["gcases"])
                    results.update(gres)
                sig = {(e, cid) for (e, cid), lab in labels.items() if lab == "unexplained"}
                shapes = {(e, cid, json.dumps(shape(r["runs"][0]), default=str))
                          for e, rs in results.items() for cid, r in rs.items()}
                dis = disagreements({e: r for e, r in results.items() if e in cypher_engines}, c["cases"]) | gdis
                known = {lab for lab in labels.values() if lab.startswith("known:")}
                third = len(shapes - seen_shapes) if a.selection == "shapes" else len(dis - seen_dis)
                if errors:
                    save(c["dir"] / "invalid_engine_errors.json", errors)
                    raise RuntimeError("Engine batch infrastructure failure: " + str(errors))
                c.update(errors=errors, counts=dict(collections.Counter(labels.values())),
                         new_unexplained=sorted(sig - seen_signals), new_labels=sorted(known - seen_labels),
                         third=third, labels=labels, shapes=shapes, dis=dis)
                raw = {k for k, lab in labels.items() if lab not in ("boundary", "timeout")}
                c["raw"], c["new_raw"] = raw, raw - seen_raw
                if a.score == "raw":
                    c["score"] = 1000 * len(c["new_raw"]) + third
                else:
                    c["score"] = 1000 * len(c["new_unexplained"]) + 10 * len(c["new_labels"]) + third
                save(c["dir"] / "signals.json", {"errors": errors, "counts": c["counts"], "score": c["score"],
                     "new_unexplained": c["new_unexplained"], "new_labels": c["new_labels"], "third_term": third,
                     "disagreements": sorted(dis),
                     "labels": [[e, cid, lab] for (e, cid), lab in sorted(labels.items())]})
            if a.selection == "random":
                best = rng.choice([c for c in children if not c["errors"]] or children)
            else:
                best = max(children, key=lambda c: (not c["errors"], c["score"], -c["k"]))
            for c in children:
                seen_signals |= {(e, cid) for (e, cid), lab in c["labels"].items() if lab == "unexplained"}
                seen_labels |= {lab for lab in c["labels"].values() if lab.startswith("known:")}
                seen_shapes |= c["shapes"]
                seen_dis |= c["dis"]
                seen_raw |= c["raw"]
                for e, cid in c["new_unexplained"]:
                    unexplained_all.append({"generation": gen, "child": c["k"], "engine": e, "id": cid})
            # Cell statistics for generated features of the selected child (Eq. 11).
            by_id = unique_index(best["cases"], context="fitness catalog")
            for cid, cs in by_id.items():
                if not cid.startswith("gen/"):
                    continue
                st = cells.setdefault(cs["cell"], {"n": 0, "u": 0})
                st["n"] += 1
                if any(cid == x for (_e, x), lab in best["labels"].items() if lab == "unexplained") or cid in best["dis"]:
                    st["u"] += 1
            if a.evolve_features:
                hit_ids = {x for (_e, x), lab in best["labels"].items()
                           if lab == "unexplained" or lab.startswith("known:")} | set(best["dis"])
                for cs in best["cases"] + best["gcases"]:
                    kind, _, fid = cs["id"].partition("/")
                    if kind in ("gen", "grem"):
                        fstats.record(fid, cs["id"] in hit_ids, gen)
                from gecko_ast import admit as admit_c
                from gecko_gremlin import gadmit as admit_g, seed_features as gseeds
                pools = {"int": [v for v in graph_evolution.AGES if v is not None],
                         "float": [v for v in graph_evolution.SCORES if v is not None],
                         "str": [v for v in graph_evolution.NAMES if v is not None]}
                made, ops_ok = 0, collections.Counter()
                for par in fstats.pick_parents(pool + gseeds(), 4 * a.mutants, rng):
                    if made >= a.mutants:
                        break
                    m, mop = mutate_feature(par, rng, pools)
                    if m is None:
                        continue
                    m["fid"], m["parent"], m["mutation"] = f"{feature_ids.reserve(1):04d}", par["fid"], mop
                    cc, _ = admit_c(m, best["graph"], MAX_ROWS)
                    gc, _ = admit_g(m, best["graph"], MAX_ROWS)
                    q = (cc or gc)["query"] if (cc or gc) else None
                    if q is None or q in pool_queries:
                        continue
                    pool.append(m); pool_queries.append(q); made += 1; ops_ok[mop] += 1
                evicted = fstats.evictable([f for f in pool if not f["fid"].startswith("s")], a.pool_cap)
                if evicted:
                    ev = set(evicted)
                    drop_q = set()
                    for f in pool:
                        if f["fid"] in ev:
                            for comp in (admit_c, admit_g):
                                cc, _ = comp(f, best["graph"], MAX_ROWS)
                                if cc:
                                    drop_q.add(cc["query"])
                    pool[:] = [f for f in pool if f["fid"] not in ev]
                    pool_queries[:] = [q for q in pool_queries if q not in drop_q]
                evo_log.append({"generation": gen, "mutants_admitted": made, "by_operator": dict(ops_ok),
                                "evicted": len(evicted), "pool": len(pool)})
                save(out / "evolution_log.json", evo_log)
                save(out / "feature_pool.json", pool)
            productive = bool(best["new_raw"]) if a.score == "raw" else bool(best["new_unexplained"] or best["new_labels"])
            if a.selection != "random":
                for o in OPERATORS:
                    weights[o] = 1 + (weights[o] - 1) * 0.9
                if productive:
                    for d in best["ops"]:
                        weights[d["op"]] *= 1.5
            prev_graph, parent = parent, best["graph"]
            entry = {"generation": gen, "selected": best["k"], "selected_explore": best["explore"],
                     "shape": describe(parent), "ops": [d["op"] for d in best["ops"]], "executions": executions,
                     "children": [{"k": c["k"], "score": c["score"], "counts": c["counts"], "errors": c["errors"],
                                   "cases": len(c["cases"]), "gremlin_cases": len(c["gcases"]),
                                   "generated_cases": c["generated"],
                                   "skipped_large": c["skipped"], "new_unexplained": len(c["new_unexplained"]),
                                   "new_labels": c["new_labels"], "third_term": c["third"]}
                                  for c in children],
                     "weights": dict(weights), "pool_size": len(pool), "finished_unix": time.time()}
            # AI-1: targeted generation for the next generation (Eqs. 10, 11).
            calls_used = len(gen_log) + (1 if a.mine_values else 0)
            if a.generate and gen < a.generations and not (a.max_llm_calls and calls_used >= a.max_llm_calls):
                all_cells = {f"{s}:{t}" for s in ("projection", "filter", "group", "order", "total")
                             for t in ("int", "float", "str", "bool")}
                uncovered = sorted(all_cells - set(cells))
                promising = sorted(cells, key=lambda k: -(1 + cells[k]["u"]) / (2 + cells[k]["n"]))[:4]
                prompt = generation_prompt(parent, graph_diff(parent, prev_graph),
                                           {l.split(":", 1)[1] for l in seen_labels}, uncovered + promising,
                                           pool_queries, last_rejected if a.repair else None, a.contract == "v2")
                resp = llm_request(out, "generate", prompt, GEN_SCHEMA, a.llm_timeout)
                if resp.get("status") != "ok":
                    save(out / "invalid_model_response.json", resp)
                    raise RuntimeError("Invalid model generation response: " + str(resp.get("status")))
                feats, parse_errors = parse_reserved(resp.get("output"), parse_features, feature_ids)
                accepted, reasons = [], {}
                for f in feats:
                    try:
                        check_feature(f)
                    except Invalid as err:
                        reasons[f["fid"]] = "invalid: " + str(err)
                        continue
                    from gecko_ast import admit as admit_feature
                    case, why = admit_feature(f, parent, MAX_ROWS)
                    if case is None:
                        reasons[f["fid"]] = why
                        continue
                    if case["query"] in pool_queries:
                        reasons[f["fid"]] = "duplicate"
                        continue
                    pool.append(f)
                    pool_queries.append(case["query"])
                    accepted.append(f["fid"])
                by_fid = unique_index(feats, key="fid", context="repair proposals")
                last_rejected = [{"reason": why, "ast": by_fid[fid]["expr"]} for fid, why in reasons.items()
                                 if fid in by_fid and why not in ("duplicate", "not discriminative")]
                log = {"generation": gen, "request": resp.get("id"), "status": resp.get("status"),
                       "proposed": len(feats), "parse_errors": parse_errors, "accepted": accepted,
                       "rejected": reasons, "usage": resp.get("usage")}
                gen_log.append(log)
                save(out / "generation_log.json", gen_log)
                save(out / "feature_pool.json", pool)
                entry["generation_call"] = {k: log[k] for k in ("status", "proposed")} | {
                    "accepted": len(accepted), "rejected": len(reasons) + len(parse_errors)}
            history.append(entry)
            save(out / "history.json", history)
            save(out / "unexplained.json", unexplained_all)
            save(out / "cells.json", cells)
            print(json.dumps({"generation": gen, "selected": best["k"], "executions": executions,
                              "pool": len(pool), "shape": entry["shape"],
                              "children": [(c["k"], c["score"], c["counts"]) for c in children],
                              "generation_call": entry.get("generation_call")}, default=str), flush=True)
            if a.stop_on_unexplained and unexplained_all:
                break
            if deadline and time.monotonic() >= deadline:
                break
            if a.budget and executions >= a.budget:
                break
    finally:
        stop_all(handles)
        save(out / "summary.json", {"schema": "gecko-trajectory-summary/1", "generations_run": len(history),
             "executions": executions, "unexplained_signal_units": len(unexplained_all),
             "pool_size": len(pool), "model_calls": len(gen_log) + (1 if a.mine_values else 0),
             "confirmed_new_defects": 0, "final_shape": describe(parent), "completed_unix": time.time()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
