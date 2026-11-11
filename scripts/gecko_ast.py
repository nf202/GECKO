"""Closed feature grammar for reference-closed generation (AI-1).

A feature is {"shape": ..., "expr": AST}. This module type-checks an AST,
compiles it to Cypher text and evaluates the expected result directly on a
graph in Python. Anything whose semantics the declared contract does not fix
raises Uncertain and the feature is rejected; nothing is repaired.

AST nodes (JSON):
  {"p": prop}                         property of n (or of m inside "nb")
  {"v": literal}                      int, float, string, bool or null
  {"op": "+|-|*|/|%", "a": E, "b": E}
  {"cmp": "=|<>|<|<=|>|>=", "a": E, "b": E}
  {"and": [E, E]}, {"or": [E, E]}, {"xor": [E, E]}, {"not": E}
  {"isnull": E}, {"notnull": E}
  {"in": E, "list": [literal, ...]}
  {"str": "starts|ends|contains", "a": E, "b": E}
  {"fn": name, "args": [E, ...]}
  {"case": [[cond, value], ...], "else": E or null}
  {"nb": "out|in|both", "agg": "count|sum|min|max", "expr": E, "where": E}
Shapes: projection, filter, group, order (optional "desc": true), total.
"""
from __future__ import annotations
import json
import math

PROPS = {"id": "int", "age": "int", "score": "float", "name": "str", "flag": "bool"}
SHAPES = ("projection", "filter", "group", "order", "total")
FUNCS = {"toInteger", "toFloat", "abs", "sign", "floor", "ceil", "size", "toUpper", "toLower",
         "reverse", "left", "right", "substring", "coalesce", "toString"}
INT64 = 2 ** 63
MAX_DEPTH = 6
MIN_DELTA = 2  # discriminative admission; the no-discriminative ablation sets 0
MAX_NODES = 40
# Gecko-FE contract (off by default): Cypher orderability of booleans, substring past the end is '',
# -0.0 orders equal to 0.0, and integer overflow / integer division by zero expect an engine error.
V2 = False
NEO4J_13985 = False  # offline triage only: evaluate ordering comparisons with -0.0 < 0.0


def ill_conditioned(vals, total):
    """Float sums whose value depends on summation order: the worst-case rounding error
    n * eps * sum|x| exceeds 1e-12 relative to the result (catastrophic cancellation)."""
    if not any(isinstance(v, float) for v in vals):
        return False
    return len(vals) * 2.0 ** -52 * sum(abs(v) for v in vals) > 1e-12 * max(abs(total), 1.0)


class ExpectError(Exception):
    """V2: the declared contract requires the engine to raise (overflow, integer division by zero)."""


class Uncertain(Exception):
    """Semantics outside the declared contract; the feature is rejected."""


class Invalid(Exception):
    """Malformed or ill-typed AST."""


# ------------------------------------------------------------------ typing
def num(t):
    return t in ("int", "float", "num")


def join(a, b):
    if a == "null":
        return b
    if b == "null":
        return a
    if a == b:
        return a
    if num(a) and num(b):
        return "num"
    raise Invalid(f"incompatible types {a}/{b}")


def typeof(e, scope="n", depth=0, count=None):
    count = count if count is not None else [0]
    count[0] += 1
    if depth > MAX_DEPTH or count[0] > MAX_NODES:
        raise Invalid("expression too large")
    if not isinstance(e, dict) or len(e) == 0:
        raise Invalid("node must be an object")
    t = lambda x, s=scope: typeof(x, s, depth + 1, count)
    if "p" in e:
        if e["p"] not in PROPS:
            raise Invalid("unknown property")
        if e.get("of", "n") != scope:
            raise Invalid("property of unbound variable")
        return PROPS[e["p"]]
    if "v" in e:
        v = e["v"]
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, int):
            if abs(v) >= 2 ** 53:
                raise Invalid("literal too large")
            return "int"
        if isinstance(v, float):
            if not math.isfinite(v):
                raise Invalid("non-finite literal")
            return "float"
        if isinstance(v, str):
            if len(v) > 40 or any(ord(c) > 0xFFFF or c in "'\\\n\r" for c in v):
                raise Invalid("unsupported string literal")
            return "str"
        raise Invalid("literal type")
    if "op" in e:
        a, b = t(e["a"]), t(e["b"])
        if e["op"] not in "+-*/%" or len(e["op"]) != 1:
            raise Invalid("operator")
        for x in (a, b):
            if not (num(x) or x == "null"):
                raise Invalid("arithmetic needs numbers")
        if a == "int" and b == "int":
            return "int"
        return "float" if "float" in (a, b) else "num"
    if "cmp" in e:
        if e["cmp"] not in ("=", "<>", "<", "<=", ">", ">="):
            raise Invalid("comparison")
        join(t(e["a"]), t(e["b"]))
        return "bool"
    for k in ("and", "or", "xor"):
        if k in e:
            if not isinstance(e[k], list) or len(e[k]) != 2:
                raise Invalid(k)
            for x in e[k]:
                if t(x) not in ("bool", "null"):
                    raise Invalid("boolean operand")
            return "bool"
    if "not" in e:
        if t(e["not"]) not in ("bool", "null"):
            raise Invalid("boolean operand")
        return "bool"
    if "isnull" in e or "notnull" in e:
        t(e.get("isnull", e.get("notnull")))
        return "bool"
    if "in" in e:
        lt = t(e["in"])
        lst = e.get("list")
        if not isinstance(lst, list) or len(lst) > 8:
            raise Invalid("in-list")
        for x in lst:
            join(lt, t({"v": x}))
        return "bool"
    if "str" in e:
        if e["str"] not in ("starts", "ends", "contains"):
            raise Invalid("string predicate")
        for x in (t(e["a"]), t(e["b"])):
            if x not in ("str", "null"):
                raise Invalid("string predicate needs strings")
        return "bool"
    if "fn" in e:
        f, args = e["fn"], e.get("args", [])
        if f not in FUNCS or not isinstance(args, list):
            raise Invalid("function")
        ts = [t(a) for a in args]
        if f in ("toInteger", "toFloat", "abs", "sign", "floor", "ceil"):
            if len(ts) != 1 or not (num(ts[0]) or ts[0] == "null"):
                raise Invalid(f + " needs one number")
            return {"toInteger": "int", "toFloat": "float", "sign": "int", "floor": "float",
                    "ceil": "float"}.get(f, ts[0])
        if f in ("size", "toUpper", "toLower", "reverse"):
            if len(ts) != 1 or ts[0] not in ("str", "null"):
                raise Invalid(f + " needs one string")
            return "int" if f == "size" else "str"
        if f in ("left", "right"):
            if len(args) != 2 or ts[0] not in ("str", "null") or not _small_lit(args[1]):
                raise Invalid(f + "(string, literal)")
            return "str"
        if f == "substring":
            if len(args) != 3 or ts[0] not in ("str", "null") or not all(_small_lit(a) for a in args[1:]):
                raise Invalid("substring(string, literal, literal)")
            return "str"
        if f == "coalesce":
            if not 2 <= len(ts) <= 3:
                raise Invalid("coalesce arity")
            r = "null"
            for x in ts:
                r = join(r, x)
            return r
        if f == "toString":
            if len(ts) != 1 or ts[0] not in ("int", "bool", "null"):
                raise Invalid("toString only for integers and booleans")
            return "str"
    if "case" in e:
        arms = e["case"]
        if not isinstance(arms, list) or not 1 <= len(arms) <= 4:
            raise Invalid("case arms")
        r = "null"
        for arm in arms:
            if not isinstance(arm, list) or len(arm) != 2 or t(arm[0]) not in ("bool", "null"):
                raise Invalid("case arm")
            r = join(r, t(arm[1]))
        if e.get("else") is not None:
            r = join(r, t(e["else"]))
        return r
    if "nb" in e:
        if scope != "n" or e["nb"] not in ("out", "in", "both") or e.get("agg") not in ("count", "sum", "min", "max"):
            raise Invalid("neighbor aggregate")
        if e.get("where") is not None and typeof(e["where"], "m", depth + 1, count) not in ("bool", "null"):
            raise Invalid("neighbor filter")
        if e["agg"] == "count":
            return "int"
        et = typeof(e["expr"], "m", depth + 1, count)
        if e["agg"] == "sum" and not num(et):
            raise Invalid("sum needs numbers")
        if et == "bool" and not V2:
            raise Invalid("min/max over booleans")
        return et
    raise Invalid("unknown node")


def _small_lit(e):
    return isinstance(e, dict) and set(e) == {"v"} and isinstance(e["v"], int) and not isinstance(e["v"], bool) \
        and 0 <= e["v"] <= 20


def check(feature):
    if not isinstance(feature, dict) or feature.get("shape") not in SHAPES:
        raise Invalid("shape")
    t = typeof(feature["expr"])
    shape = feature["shape"]
    if shape == "filter" and t not in ("bool", "null"):
        raise Invalid("filter needs a boolean")
    if shape == "order" and t in (("null", "num") if V2 else ("null", "num", "bool")):
        raise Invalid("order key must be int, float or string")
    if shape == "total" and t == "bool" and not V2:
        raise Invalid("total over booleans")
    return t


def props_used(e, acc=None):
    acc = set() if acc is None else acc
    if isinstance(e, dict):
        if "p" in e:
            acc.add(e["p"])
        for v in e.values():
            props_used(v, acc)
    elif isinstance(e, list):
        for v in e:
            props_used(v, acc)
    return acc


def cell(feature):
    used = sorted(PROPS[p] for p in props_used(feature["expr"]) if p != "id") or ["none"]
    return feature["shape"] + ":" + "+".join(sorted(set(used)))


# ------------------------------------------------------------------ compile
def lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v) if "e" not in repr(v) else "%.17g" % v
    return "'" + v + "'"


def cy(e, var="n"):
    if "p" in e:
        return f"{e.get('of', var)}.{e['p']}"
    if "v" in e:
        return lit(e["v"])
    if "op" in e:
        return f"({cy(e['a'], var)} {e['op']} {cy(e['b'], var)})"
    if "cmp" in e:
        return f"({cy(e['a'], var)} {e['cmp']} {cy(e['b'], var)})"
    for k in ("and", "or", "xor"):
        if k in e:
            return f"({cy(e[k][0], var)} {k.upper()} {cy(e[k][1], var)})"
    if "not" in e:
        return f"(NOT {cy(e['not'], var)})"
    if "isnull" in e:
        return f"({cy(e['isnull'], var)} IS NULL)"
    if "notnull" in e:
        return f"({cy(e['notnull'], var)} IS NOT NULL)"
    if "in" in e:
        return f"({cy(e['in'], var)} IN [{', '.join(lit(x) for x in e['list'])}])"
    if "str" in e:
        op = {"starts": "STARTS WITH", "ends": "ENDS WITH", "contains": "CONTAINS"}[e["str"]]
        return f"({cy(e['a'], var)} {op} {cy(e['b'], var)})"
    if "fn" in e:
        return f"{e['fn']}({', '.join(cy(a, var) for a in e['args'])})"
    if "case" in e:
        arms = " ".join(f"WHEN {cy(c, var)} THEN {cy(v, var)}" for c, v in e["case"])
        els = f" ELSE {cy(e['else'], var)}" if e.get("else") is not None else ""
        return f"(CASE {arms}{els} END)"
    if "nb" in e:
        pat = {"out": "(n)-[:R]->(m)", "in": "(n)<-[:R]-(m)", "both": "(n)-[:R]-(m)"}[e["nb"]]
        where = cy(e["where"], "m") if e.get("where") is not None else None
        if e["agg"] == "count":
            w = f" WHERE {where}" if where else ""
            return f"size([{pat}{w} | 1])"
        x = cy(e["expr"], "m")
        cond = f"{where} AND {x} IS NOT NULL" if where else f"{x} IS NOT NULL"
        lst = f"[{pat} WHERE {cond} | {x}]"
        if e["agg"] == "sum":
            return f"reduce(s = 0, x IN {lst} | s + x)"
        cmp_ = "<" if e["agg"] == "min" else ">"
        return f"reduce(r = NULL, x IN {lst} | CASE WHEN r IS NULL OR x {cmp_} r THEN x ELSE r END)"
    raise Invalid("compile")


def query(feature):
    e, shape = cy(feature["expr"]), feature["shape"]
    if shape == "projection":
        return f"MATCH (n:P) RETURN n.id AS c0, {e} AS c1"
    if shape == "filter":
        return f"MATCH (n:P) WHERE {e} RETURN n.id AS c0"
    if shape == "group":
        return f"MATCH (n:P) RETURN {e} AS c0, count(*) AS c1"
    if shape == "order":
        d = " DESC" if feature.get("desc") else ""
        return f"MATCH (n:P) WITH n ORDER BY {e}{d}, n.id RETURN collect(n.id) AS c0"
    if feature["expr"] and typeof(feature["expr"]) in ("str", "bool"):
        return f"MATCH (n:P) RETURN count({e}) AS c0, min({e}) AS c1, max({e}) AS c2"
    return f"MATCH (n:P) RETURN count({e}) AS c0, sum({e}) AS c1, min({e}) AS c2, max({e}) AS c3"


# ------------------------------------------------------------------ evaluate
def tdiv(a, b):
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def _int(v):
    if isinstance(v, int) and not isinstance(v, bool) and not -INT64 <= v < INT64:
        if V2:
            raise ExpectError("integer overflow")
        raise Uncertain("integer overflow")
    return v


def _bmp(s):
    if any(ord(c) > 0xFFFF for c in s):
        raise Uncertain("non-BMP string")
    return s


def and3(a, b):
    if a is False or b is False:
        return False
    return None if a is None or b is None else True


def or3(a, b):
    if a is True or b is True:
        return True
    return None if a is None or b is None else False


def eq(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b if isinstance(a, bool) and isinstance(b, bool) else False
    return a == b


class Graph:
    def __init__(self, g):
        self.nodes = {n["id"]: n for n in g["nodes"]}
        self.out, self.inn = {}, {}
        for e in g["edges"]:
            self.out.setdefault(e["src"], []).append(e["dst"])
            self.inn.setdefault(e["dst"], []).append(e["src"])

    def neighbors(self, nid, d):
        o, i = self.out.get(nid, []), self.inn.get(nid, [])
        if d == "out":
            return list(o)
        if d == "in":
            return list(i)
        return list(o) + [s for s in i if s != nid]


def ev(e, G, n, m=None):
    r = lambda x: ev(x, G, n, m)
    if "p" in e:
        node = m if e.get("of", "n") == "m" else n
        return node.get(e["p"])
    if "v" in e:
        return e["v"]
    if "op" in e:
        a, b = r(e["a"]), r(e["b"])
        if a is None or b is None:
            return None
        op = e["op"]
        both_int = isinstance(a, int) and isinstance(b, int)
        if op in "/%" and b == 0:
            if V2 and both_int:
                raise ExpectError("integer division by zero")
            raise Uncertain("division by zero")
        if op == "+":
            v = a + b
        elif op == "-":
            v = a - b
        elif op == "*":
            v = a * b
        elif op == "/":
            v = tdiv(a, b) if both_int else a / b
        else:
            v = a - b * tdiv(a, b) if both_int else math.fmod(a, b)
        if isinstance(v, float) and not math.isfinite(v):
            raise Uncertain("non-finite float")
        return _int(v)
    if "cmp" in e:
        a, b = r(e["a"]), r(e["b"])
        if a is None or b is None:
            return None
        c = e["cmp"]
        if c == "=":
            return eq(a, b)
        if c == "<>":
            return not eq(a, b)
        if isinstance(a, bool) != isinstance(b, bool):
            raise Uncertain("bool/number ordering")
        if (NEO4J_13985 and a == 0 and b == 0 and not isinstance(a, bool) and not isinstance(b, bool)
                and (isinstance(a, float) or isinstance(b, float))):
            # triage-only model of Neo4j #13985: -0.0 = 0.0 but -0.0 < 0.0 under ordering comparisons
            a, b = math.copysign(1, a), math.copysign(1, b)
        return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[c]
    if "and" in e:
        return and3(r(e["and"][0]), r(e["and"][1]))
    if "or" in e:
        return or3(r(e["or"][0]), r(e["or"][1]))
    if "xor" in e:
        a, b = r(e["xor"][0]), r(e["xor"][1])
        return None if a is None or b is None else a != b
    if "not" in e:
        a = r(e["not"])
        return None if a is None else not a
    if "isnull" in e:
        return r(e["isnull"]) is None
    if "notnull" in e:
        return r(e["notnull"]) is not None
    if "in" in e:
        a, lst = r(e["in"]), e["list"]
        if not lst:
            return False
        if a is None:
            return None
        if any(x is not None and eq(a, x) for x in lst):
            return True
        return None if any(x is None for x in lst) else False
    if "str" in e:
        a, b = r(e["a"]), r(e["b"])
        if a is None or b is None:
            return None
        _bmp(a), _bmp(b)
        return {"starts": a.startswith(b), "ends": a.endswith(b), "contains": b in a}[e["str"]]
    if "fn" in e:
        f = e["fn"]
        args = [r(x) for x in e["args"]]
        if f == "coalesce":
            return next((x for x in args if x is not None), None)
        if args[0] is None:
            return None
        x = args[0]
        if f == "toInteger":
            return _int(int(x)) if isinstance(x, float) else x
        if f == "toFloat":
            return float(x)
        if f == "abs":
            return _int(abs(x))
        if f == "sign":
            return (x > 0) - (x < 0)
        if f in ("floor", "ceil"):
            v = float(math.floor(x) if f == "floor" else math.ceil(x))
            return math.copysign(0.0, x) if v == 0 else v  # IEEE: ceil(-0.5) = -0.0, floor(-0.0) = -0.0
        if f == "toString":
            return ("true" if x else "false") if isinstance(x, bool) else str(x)
        s = _bmp(x)
        if f == "size":
            return len(s)
        if f == "toUpper":
            return s.upper()
        if f == "toLower":
            return s.lower()
        if f == "reverse":
            return s[::-1]
        if f == "left":
            return s[:args[1]]
        if f == "right":
            return s[len(s) - args[1]:] if args[1] else ""
        if f == "substring":
            if args[1] > len(s) and not V2:
                raise Uncertain("substring start beyond length")
            return s[args[1]:args[1] + args[2]]
    if "case" in e:
        for c, v in e["case"]:
            if r(c) is True:
                return r(v)
        return r(e["else"]) if e.get("else") is not None else None
    if "nb" in e:
        rows = []
        for mid in G.neighbors(n["id"], e["nb"]):
            mm = G.nodes[mid]
            if e.get("where") is not None and ev(e["where"], G, n, mm) is not True:
                continue
            rows.append(mm)
        if e["agg"] == "count":
            return len(rows)
        vals = [v for v in (ev(e["expr"], G, n, mm) for mm in rows) if v is not None]
        if e["agg"] == "sum":
            total = 0
            for v in vals:
                total += v
            if ill_conditioned(vals, total):
                raise Uncertain("ill-conditioned float sum")
            return _int(total)
        if not vals:
            return None
        if any(isinstance(v, str) for v in vals) and any(not isinstance(v, str) for v in vals):
            raise Uncertain("mixed min/max")
        return min(vals) if e["agg"] == "min" else max(vals)
    raise Invalid("evaluate")


def _key(v):
    return json.dumps(0.0 if v == 0 and isinstance(v, float) else v, sort_keys=True)


def _gkey(v):
    """Cypher grouping key: numbers group by value (1 = 1.0, -0.0 = 0.0); integral floats in the exact
    double range join the integer group, larger integers stay exact; bools never equal numbers."""
    if isinstance(v, float) and v.is_integer() and abs(v) <= 2 ** 53:
        v = int(v)
    return _key(v)


def reference(feature, g):
    """Expected rows and discriminative power delta, or raise Uncertain."""
    G = Graph(g)
    nodes = sorted(g["nodes"], key=lambda n: n["id"])
    vals = [(n, ev(feature["expr"], G, n)) for n in nodes]
    shape = feature["shape"]
    if shape == "projection":
        return [[n["id"], v] for n, v in vals], len({_key(v) for _, v in vals})
    if shape == "filter":
        return [[n["id"]] for n, v in vals if v is True], len({v is True for _, v in vals})
    if shape == "group":
        groups = {}
        for _, v in vals:
            k = _gkey(v)
            groups.setdefault(k, [v, 0])[1] += 1
        return [[v, c] for v, c in groups.values()], len(groups)
    if shape == "order":
        ks = [v for _, v in vals if v is not None]
        if any(isinstance(v, str) for v in ks) and any(not isinstance(v, str) for v in ks):
            raise Uncertain("mixed order key")
        if any(isinstance(v, bool) for v in ks) and any(not isinstance(v, bool) for v in ks):
            raise Uncertain("mixed order key")
        if not V2 and any(isinstance(v, float) and v == 0 and math.copysign(1, v) < 0 for v in ks):
            raise Uncertain("signed zero ordering")
        present = [n for n, v in vals if v is not None]
        missing = [n for n, v in vals if v is None]
        key = {n["id"]: v for n, v in vals}
        # Stable two-pass sort: ties keep n.id ascending in both directions.
        present.sort(key=lambda n: n["id"])
        present.sort(key=lambda n: key[n["id"]], reverse=bool(feature.get("desc")))
        ordered = (missing + present) if feature.get("desc") else (present + missing)
        return [[[n["id"] for n in ordered]]], len({_key(v) for _, v in vals})
    present = [v for _, v in vals if v is not None]
    if typeof(feature["expr"]) in ("str", "bool"):
        return [[len(present), min(present) if present else None, max(present) if present else None]], \
            len({_key(v) for _, v in vals})
    total = 0
    for v in present:
        total += v
    if isinstance(total, float) and not math.isfinite(total):
        raise Uncertain("non-finite sum")
    if ill_conditioned(present, total):
        raise Uncertain("ill-conditioned float sum")
    return [[len(present), _int(total), min(present) if present else None, max(present) if present else None]], \
        len({_key(v) for _, v in vals})


def _conditional(e):
    if isinstance(e, dict):
        if any(k in e for k in ("and", "or", "xor", "case", "nb")) or e.get("fn") == "coalesce":
            return True
        return any(_conditional(v) for v in e.values())
    if isinstance(e, list):
        return any(_conditional(v) for v in e)
    return False


def admit(feature, g, row_budget=3000):
    """(case dict, None) if admitted for g, else (None, reason)."""
    try:
        check(feature)
        q = query(feature)
        rows, delta = reference(feature, g)
    except Invalid as err:
        return None, "invalid: " + str(err)
    except Uncertain as err:
        return None, "outside contract: " + str(err)
    except ExpectError as err:
        # An error is only a reliable expectation when the failing sub-expression is evaluated on
        # every row; under AND/OR/XOR/CASE/coalesce or a neighbour aggregate engines may legally skip it.
        if _conditional(feature["expr"]):
            return None, "outside contract: error under conditional evaluation"
        return {"id": "gen/" + feature["fid"], "family": "gen_" + feature["shape"], "query": q,
                "expected_rows": [], "expected_error": str(err), "contract": "closed generated feature (expected error)",
                "cell": cell(feature)}, None
    if len(rows) > row_budget:
        return None, "row budget"
    if delta < MIN_DELTA:
        return None, "not discriminative"
    return {"id": "gen/" + feature["fid"], "family": "gen_" + feature["shape"], "query": q,
            "expected_rows": rows, "contract": "closed generated feature", "cell": cell(feature)}, None
