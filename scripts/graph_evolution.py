"""Mutation operators for a single evolving fixture graph.

A generation applies a few operators to its parent. Graphs keep the schema of
semantic_suite.fixture (nodes: id, age, score, name, flag; edges: eid, src, dst,
weight) with contiguous ids and eids, so every template generator recomputes its
reference on any generation. One seed drives one trajectory; this is not a
multi-seed design. Size limits keep direct path enumeration tractable.
"""
from __future__ import annotations
import copy
from collections import Counter

LIMITS = {"nodes": 80, "edges": 200, "degree": 10}
AGES = list(range(-6, 11)) + [None, None]
SCORES = [-2.5, -1.5, -0.5, -0.0, 0.0, 0.5, 1.25, 2.5, None]
NAMES = ["", "n0", "n1", "n2", "N1", "é", "a b", "nn1", None]
FLAGS = [True, False, None]
OPERATORS = ("add_node", "add_chain", "add_hub", "add_parallel", "add_self_loop", "loop_plus_incoming",
             "add_cycle", "add_clique", "mutate_value", "mutate_weight", "reorder_edges", "reorder_nodes")


def degrees(g):
    d = Counter()
    for e in g["edges"]:
        d[e["src"]] += 1
        d[e["dst"]] += 1
    return d


def _props(rng, i):
    return {"id": i, "age": rng.choice(AGES), "score": rng.choice(SCORES),
            "name": rng.choice(NAMES), "flag": rng.choice(FLAGS)}


def _node(g, rng):
    g["nodes"].append(_props(rng, len(g["nodes"])))
    return len(g["nodes"]) - 1


def _edge(g, rng, s, t):
    g["edges"].append({"eid": len(g["edges"]), "src": s, "dst": t, "weight": rng.randint(-3, 3)})


def _room(g, needed_nodes=0, needed_edges=0, endpoints=()):
    if len(g["nodes"]) + needed_nodes > LIMITS["nodes"] or len(g["edges"]) + needed_edges > LIMITS["edges"]:
        return False
    d = degrees(g)
    extra = Counter(endpoints)
    return all(d[n] + c <= LIMITS["degree"] for n, c in extra.items())


def _pick(g, rng, slack=1):
    d = degrees(g)
    free = [n["id"] for n in g["nodes"] if d[n["id"]] + slack <= LIMITS["degree"]]
    return rng.choice(free) if free else None


def apply(op, g, rng):
    """Apply one operator in place; return a description dict, or None if infeasible."""
    ids = [n["id"] for n in g["nodes"]]
    if op == "add_node":
        if not _room(g, 1):
            return None
        return {"op": op, "node": _node(g, rng)}
    if op == "add_chain":
        k, start = rng.randint(3, 5), _pick(g, rng)
        if start is None or not _room(g, k, k, [start]):
            return None
        prev, made = start, []
        for _ in range(k):
            n = _node(g, rng)
            _edge(g, rng, prev, n)
            prev = n
            made.append(n)
        return {"op": op, "from": start, "nodes": made}
    if op == "add_hub":
        k = rng.randint(3, 6)
        hub = _pick(g, rng, k)
        targets = [rng.choice(ids) for _ in range(k)]
        if hub is None or not _room(g, 0, k, [hub] * k + targets):
            return None
        out = rng.random() < 0.5
        for t in targets:
            _edge(g, rng, hub, t) if out else _edge(g, rng, t, hub)
        return {"op": op, "hub": hub, "targets": targets, "outgoing": out}
    if op == "add_parallel":
        if not g["edges"]:
            return None
        e = rng.choice(g["edges"])
        if not _room(g, 0, 1, [e["src"], e["dst"]]):
            return None
        _edge(g, rng, e["src"], e["dst"])
        return {"op": op, "copy_of": e["eid"]}
    if op == "add_self_loop":
        n = _pick(g, rng, 2)
        if n is None or not _room(g, 0, 1, [n, n]):
            return None
        _edge(g, rng, n, n)
        return {"op": op, "node": n}
    if op == "loop_plus_incoming":
        n, m = _pick(g, rng, 3), rng.choice(ids)
        if n is None or m == n or not _room(g, 0, 2, [n, n, n, m]):
            return None
        _edge(g, rng, n, n)
        _edge(g, rng, m, n)
        return {"op": op, "node": n, "incoming_from": m}
    if op == "add_cycle":
        k = rng.randint(2, 4)
        cyc = rng.sample(ids, min(k, len(ids)))
        if len(cyc) < 2 or not _room(g, 0, len(cyc), cyc * 2):
            return None
        for a, b in zip(cyc, cyc[1:] + cyc[:1]):
            _edge(g, rng, a, b)
        return {"op": op, "nodes": cyc}
    if op == "add_clique":
        members = rng.sample(ids, min(3, len(ids)))
        pairs = [(a, b) for i, a in enumerate(members) for b in members[i + 1:]]
        if len(members) < 3 or not _room(g, 0, len(pairs), [x for p in pairs for x in p]):
            return None
        for a, b in pairs:
            _edge(g, rng, a, b)
        return {"op": op, "nodes": members}
    if op == "mutate_value":
        n = rng.choice(g["nodes"])
        field = rng.choice(("age", "score", "name", "flag"))
        pool = {"age": AGES, "score": SCORES, "name": NAMES, "flag": FLAGS}[field]
        old, n[field] = n[field], rng.choice(pool)
        return {"op": op, "node": n["id"], "field": field, "old": old, "new": n[field]}
    if op == "mutate_weight":
        if not g["edges"]:
            return None
        e = rng.choice(g["edges"])
        e["weight"] = rng.randint(-3, 3)
        return {"op": op, "eid": e["eid"], "new": e["weight"]}
    if op in ("reorder_edges", "reorder_nodes"):
        key = "edges" if op == "reorder_edges" else "nodes"
        items = g[key]
        if len(items) < 3:
            return None
        i = rng.randrange(len(items) - 2)
        j = rng.randint(i + 2, len(items))
        part = items[i:j]
        rng.shuffle(part)
        items[i:j] = part
        return {"op": op, "slice": [i, j]}
    raise ValueError(op)


def mutate(parent, rng, weights, n_ops):
    """Return (child, applied_ops); operators drawn by weight, infeasible draws retried."""
    g = copy.deepcopy(parent)
    ops, attempts = [], 0
    names = list(OPERATORS)
    while len(ops) < n_ops and attempts < 50:
        attempts += 1
        op = rng.choices(names, [weights[o] for o in names])[0]
        desc = apply(op, g, rng)
        if desc is not None:
            ops.append(desc)
    check(g)
    return g, ops


def check(g):
    assert [n["id"] for n in sorted(g["nodes"], key=lambda n: n["id"])] == list(range(len(g["nodes"])))
    assert [e["eid"] for e in sorted(g["edges"], key=lambda e: e["eid"])] == list(range(len(g["edges"])))
    ids = {n["id"] for n in g["nodes"]}
    assert all(e["src"] in ids and e["dst"] in ids for e in g["edges"])
    assert len(g["nodes"]) <= LIMITS["nodes"] and len(g["edges"]) <= LIMITS["edges"]


def describe(g):
    d = degrees(g)
    pairs = Counter((e["src"], e["dst"]) for e in g["edges"])
    return {"nodes": len(g["nodes"]), "edges": len(g["edges"]), "max_degree": max(d.values(), default=0),
            "self_loops": sum(e["src"] == e["dst"] for e in g["edges"]),
            "parallel_extra": sum(c - 1 for c in pairs.values() if c > 1),
            "null_age": sum(n["age"] is None for n in g["nodes"]),
            "negative_fraction_score": sum(isinstance(n["score"], float) and n["score"] < 0
                                           and n["score"] != int(n["score"]) for n in g["nodes"])}
