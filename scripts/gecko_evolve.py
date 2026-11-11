"""Model-free evolution of the closed-AST feature pool (Gecko-FE).

Mutation operators rewrite a copy of a feature's AST; every mutant is re-admitted through the reference
interpreter (gecko_ast.admit / gecko_gremlin.gadmit), so the oracle stays model- and mutation-free.
Parents are drawn in proportion to (1 + hits) / (1 + runs): features whose cases produced signals
(unexplained, known-issue or cross-engine disagreement) are varied more often.
"""
from __future__ import annotations
import copy
import random

AST_KEYS = ("p", "v", "op", "cmp", "and", "or", "xor", "not", "isnull", "notnull", "in", "str", "fn", "case", "nb")
FN_SWAPS = [("floor", "ceil"), ("toUpper", "toLower"), ("abs", "sign"), ("left", "right"), ("toInteger", "toFloat")]
CMPS = ["=", "<>", "<", "<=", ">", ">="]
OPS = ["+", "-", "*", "/", "%"]
SHAPES = ["projection", "filter", "group", "order", "total"]
OPERATORS = ("constant", "function_swap", "comparison_swap", "arithmetic_swap", "neighbour_direction",
             "neighbour_aggregate", "wrap", "shape", "direction")


def _slots(e, out):
    """All (container, key) places holding an AST node, depth-first."""
    if isinstance(e, dict):
        for k, v in e.items():
            if isinstance(v, dict) and any(x in v for x in AST_KEYS):
                out.append((e, k)); _slots(v, out)
            elif isinstance(v, list):
                _slots(v, out)
    elif isinstance(e, list):
        for i, v in enumerate(e):
            if isinstance(v, dict) and any(x in v for x in AST_KEYS):
                out.append((e, i)); _slots(v, out)
            elif isinstance(v, list):
                _slots(v, out)
    return out


def _nodes(expr):
    root = {"root": expr}
    return root, [(root, "root")] + _slots(expr, [])


def _literal_for(v, rng, pools):
    if isinstance(v, bool):
        return not v
    if isinstance(v, int):
        return rng.choice(pools["int"])
    if isinstance(v, float):
        return rng.choice(pools["float"])
    if isinstance(v, str):
        return rng.choice(pools["str"])
    return v


def mutate(feature, rng: random.Random, pools, op=None):
    """Return (mutant, operator) or (None, operator) if the operator does not apply."""
    f = copy.deepcopy(feature)
    op = op or rng.choice(OPERATORS)
    root, places = _nodes(f["expr"])
    rng.shuffle(places)
    def node(pl):
        c, k = pl
        return c[k]
    if op == "shape":
        f["shape"] = rng.choice([s for s in SHAPES if s != f["shape"]])
        return f, op
    if op == "direction":
        if f["shape"] != "order":
            return None, op
        f["desc"] = not f.get("desc")
        return f, op
    for pl in places:
        n = node(pl)
        if op == "constant" and "v" in n and n["v"] is not None:
            nv = _literal_for(n["v"], rng, pools)
            if nv != n["v"] or type(nv) is not type(n["v"]):
                n["v"] = nv
                break
        elif op == "function_swap" and "fn" in n:
            for a, b in FN_SWAPS:
                if n["fn"] in (a, b):
                    n["fn"] = b if n["fn"] == a else a
                    break
            else:
                continue
            break
        elif op == "comparison_swap" and "cmp" in n:
            n["cmp"] = rng.choice([c for c in CMPS if c != n["cmp"]])
            break
        elif op == "arithmetic_swap" and "op" in n:
            n["op"] = rng.choice([o for o in OPS if o != n["op"]])
            break
        elif op == "neighbour_direction" and "nb" in n:
            n["nb"] = rng.choice([d for d in ("out", "in", "both") if d != n["nb"]])
            break
        elif op == "neighbour_aggregate" and "nb" in n:
            n["agg"] = rng.choice([a for a in ("count", "sum", "min", "max") if a != n["agg"]])
            break
        elif op == "wrap" and ("p" in n or "op" in n or "fn" in n or "nb" in n):
            c, k = pl
            c[k] = rng.choice([{"fn": "abs", "args": [n]}, {"op": "*", "a": n, "b": {"v": -1}},
                               {"op": "+", "a": n, "b": {"v": 0.0}}, {"fn": "coalesce", "args": [n, {"v": 0}]},
                               {"isnull": n}])
            break
    else:
        return None, op
    f["expr"] = root["root"]
    return f, op


class FeatureStats:
    """Per-feature execution and signal counts (by fid), used for parent selection and eviction."""

    def __init__(self):
        self.n, self.hits, self.born = {}, {}, {}

    def record(self, fid, hit, gen):
        self.born.setdefault(fid, gen)
        self.n[fid] = self.n.get(fid, 0) + 1
        self.hits[fid] = self.hits.get(fid, 0) + (1 if hit else 0)

    def fitness(self, fid):
        return (1 + self.hits.get(fid, 0)) / (1 + self.n.get(fid, 0))

    def pick_parents(self, feats, k, rng):
        if not feats:
            return []
        w = [self.fitness(f["fid"]) for f in feats]
        return rng.choices(feats, weights=w, k=k)

    def evictable(self, feats, cap, min_runs=6):
        """Feature fids to drop so that len(feats) <= cap: never-productive ones first, oldest first."""
        extra = len(feats) - cap
        if extra <= 0:
            return []
        cand = [f for f in feats if self.n.get(f["fid"], 0) >= min_runs and self.hits.get(f["fid"], 0) == 0]
        cand.sort(key=lambda f: (self.fitness(f["fid"]), self.born.get(f["fid"], 0)))
        return [f["fid"] for f in cand[:extra]]
