"""Gremlin profile of the Gecko feature grammar: render a closed AST to Gremlin and compute its
expected result under declared Gremlin semantics, without consulting any engine.

Same AST as gecko_ast (the model is unchanged); only the compiler and the reference differ.
Declared Gremlin semantics (pinned by scripts/probe_gremlin_semantics.py on TinkerGraph 3.7.3,
3.8.1 and JanusGraph 1.1.0):
  * a missing property or empty reduction is "nothing" (no traverser), never a NULL value;
  * predicates are two-valued: a comparison over nothing is false, so not() of it is true;
  * math() is double arithmetic (5/2 = 2.5, % is fmod). Bare math()/where() over a missing operand
    raises, so the compiler guards non-literal operands with filter(): arithmetic over nothing is
    nothing and a comparison over nothing is false;
  * project().by(nothing) omits the key; order().by(nothing) and groupCount().by(nothing) drop the
    vertex; sum/min/max over nothing produce nothing; count produces 0;
  * both() returns a self-loop neighbour twice; substring() clamps its indices;
  * group keys use Java equality: 1L != 1.0d. The reference follows the specification for signed
    zero (-0.0 == 0.0 in comparisons, grouping and ordering) and flags each case that depends on it
    (zero_sensitive), because the engines treat -0.0 as distinct and smaller (TinkerPop candidate).
Profile restrictions: no null literals; string predicates need a literal right operand;
right(s, 0) is rejected (substring(-0) is the whole string).
"""
from __future__ import annotations
import math

import gecko_ast
from gecko_ast import check, typeof, Invalid, Uncertain, _key, _bmp, cell, ill_conditioned

PRED = ("cmp", "and", "or", "xor", "not", "isnull", "notnull", "in", "str")
P_OP = {"=": "eq", "<>": "neq", "<": "lt", "<=": "lte", ">": "gt", ">=": "gte"}
FLIP = {"=": "=", "<>": "<>", "<": ">", "<=": ">=", ">": "<", ">=": "<="}
EXACT = 2 ** 53


def is_pred(e):
    return any(k in e for k in PRED)


def _lit(e):
    return "v" in e and e["v"] is not None


# ------------------------------------------------------------------ profile check
def _restrict(e):
    if isinstance(e, list):
        for x in e:
            _restrict(x)
        return
    if not isinstance(e, dict):
        return
    if "v" in e and e["v"] is None:
        raise Invalid("null literal outside the Gremlin profile")
    if "in" in e and any(x is None for x in e["list"]):
        raise Invalid("null in IN-list outside the Gremlin profile")
    if "str" in e and not (_lit(e["b"]) and isinstance(e["b"]["v"], str)):
        raise Invalid("string predicate needs a literal pattern in the Gremlin profile")
    if e.get("fn") == "right" and e["args"][1]["v"] == 0:
        raise Invalid("right(s, 0) outside the Gremlin profile")
    for k, v in e.items():
        if k not in ("v", "list"):
            _restrict(v)


def _bool_ordering(e):
    """Booleans as order keys or min/max operands stay outside the Gremlin profile (Java Comparable
    semantics of min()/max() over Boolean are not pinned by the probes)."""
    if isinstance(e, dict):
        if "nb" in e and e.get("agg") in ("min", "max"):
            try:
                if typeof(e["expr"], "m") == "bool":
                    return True
            except Invalid:
                pass
        return any(_bool_ordering(v) for v in e.values())
    if isinstance(e, list):
        return any(_bool_ordering(v) for v in e)
    return False


def gcheck(feature):
    t = check(feature)
    if t == "bool" and feature["shape"] in ("order", "total"):
        raise Invalid("boolean order key / total outside the Gremlin profile")
    if _bool_ordering(feature["expr"]):
        raise Invalid("min/max over booleans outside the Gremlin profile")
    _restrict(feature["expr"])
    return t


# ------------------------------------------------------------------ render
def glit(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v}L"
    if isinstance(v, float):
        return repr(v) + "d"
    return "'" + v + "'"


EMPTY = "__.not(__.identity())"


def _guard(operands, t):
    """Prefix presence filters for non-literal operands (a missing operand makes t nothing)."""
    pre = "".join(f".filter({gv(x)})" for x in operands if "v" not in x)
    return "__.identity()" + pre + t[2:] if pre else t


def gv(e):
    """Value traversal from the current vertex (n, or m inside a neighbour aggregate)."""
    if is_pred(e):
        return f"__.choose({gp(e)}, __.constant(true), __.constant(false))"
    if "p" in e:
        return f"__.values('{e['p']}')"
    if "v" in e:
        return f"__.constant({glit(e['v'])})"
    if "op" in e:
        return _guard((e["a"], e["b"]), f"__.project('x', 'y').by({gv(e['a'])}).by({gv(e['b'])}).math('x {e['op']} y')")
    if "fn" in e:
        f, args = e["fn"], e["args"]
        if f == "coalesce":
            return "__.coalesce(" + ", ".join(gv(a) for a in args) + ")"
        x = gv(args[0])
        return x + {
            "toInteger": ".math('signum(_) * floor(abs(_))')", "toFloat": ".math('_')",
            "abs": ".math('abs(_)')", "sign": ".math('signum(_)')", "floor": ".math('floor(_)')",
            "ceil": ".math('ceil(_)')", "size": ".length().math('_')", "toUpper": ".toUpper()",
            "toLower": ".toLower()", "reverse": ".reverse()", "toString": ".asString()",
            "left": f".substring(0, {args[1]['v'] if f == 'left' else 0})",
            "right": f".substring({-args[1]['v'] if f == 'right' else 0})",
            "substring": (f".substring({args[1]['v']}, {args[1]['v'] + args[2]['v']})" if f == "substring" else ""),
        }[f]
    if "case" in e:
        out = gv(e["else"]) if e.get("else") is not None else EMPTY
        for c, v in reversed(e["case"]):
            out = f"__.choose({gp(c)}, {gv(v)}, {out})"
        return out
    if "nb" in e:
        t = f"__.{e['nb']}('R')"
        if e.get("where") is not None:
            t += f".filter({gp(e['where'])})"
        if e["agg"] == "count":
            return t + ".count()"
        return t + f".flatMap({gv(e['expr'])}).{e['agg']}()"
    raise Invalid("render")


def gp(e):
    """Filter traversal: passes the current vertex iff the predicate holds (two-valued)."""
    if "cmp" in e:
        a, b, c = e["a"], e["b"], e["cmp"]
        if _lit(a) and not _lit(b):
            a, b, c = b, a, FLIP[c]
        if _lit(b):
            return f"__.filter({gv(a)}.is(P.{P_OP[c]}({glit(b['v'])})))"
        return "__.filter(" + _guard((a, b), f"__.project('x', 'y').by({gv(a)}).by({gv(b)}).where('x', P.{P_OP[c]}('y'))") + ")"
    if "and" in e:
        return f"__.and({gp(e['and'][0])}, {gp(e['and'][1])})"
    if "or" in e:
        return f"__.or({gp(e['or'][0])}, {gp(e['or'][1])})"
    if "xor" in e:
        p, q = gp(e["xor"][0]), gp(e["xor"][1])
        return f"__.or(__.and({p}, __.not({q})), __.and(__.not({p}), {q}))"
    if "not" in e:
        return f"__.not({gp(e['not'])})"
    if "isnull" in e:
        return f"__.not({gv(e['isnull'])})"
    if "notnull" in e:
        return f"__.filter({gv(e['notnull'])})"
    if "in" in e:
        return f"__.filter({gv(e['in'])}.is(P.within({', '.join(glit(x) for x in e['list'])})))"
    if "str" in e:
        fn = {"starts": "startingWith", "ends": "endingWith", "contains": "containing"}[e["str"]]
        return f"__.filter({gv(e['a'])}.is(TextP.{fn}({glit(e['b']['v'])})))"
    return f"__.filter({gv(e)}.is(true))"


def gquery(feature):
    e, shape = feature["expr"], feature["shape"]
    head = "g.V().hasLabel('P')"
    if shape == "projection":
        return f"{head}.project('c0', 'c1').by(__.values('id')).by({gv(e)}).toList()"
    if shape == "filter":
        return f"{head}.filter({gp(e)}).values('id').toList()"
    if shape == "group":
        return f"{head}.groupCount().by({gv(e)}).unfold().toList()"
    if shape == "order":
        d = "desc" if feature.get("desc") else "asc"
        return f"{head}.order().by({gv(e)}, Order.{d}).by(__.values('id'), Order.asc).values('id').fold().toList()"
    cols = ["__.count(Scope.local)"] + ([] if typeof(e) == "str" else ["__.sum(Scope.local)"]) + \
        ["__.min(Scope.local)", "__.max(Scope.local)"]
    keys = ", ".join(f"'c{i}'" for i in range(len(cols)))
    return f"{head}.flatMap({gv(e)}).fold().project({keys})" + "".join(f".by({c})" for c in cols) + ".toList()"


# ------------------------------------------------------------------ reference
class GGraph:
    def __init__(self, g):
        self.nodes = {n["id"]: n for n in g["nodes"]}
        self.out, self.inn = {}, {}
        for e in g["edges"]:
            self.out.setdefault(e["src"], []).append(e["dst"])
            self.inn.setdefault(e["dst"], []).append(e["src"])

    def neighbors(self, nid, d):
        o, i = self.out.get(nid, []), self.inn.get(nid, [])
        return list(o) if d == "out" else list(i) if d == "in" else list(o) + list(i)


def _signum(x):
    return x if x == 0 else math.copysign(1.0, x)


def _ieee(v, x):
    """Java Math.floor/ceil: a zero result keeps the sign of the argument (ceil(-0.5) = -0.0)."""
    return math.copysign(0.0, x) if v == 0 else float(v)


def _f(x):
    if isinstance(x, int) and abs(x) > EXACT:
        raise Uncertain("integer beyond double precision in math()")
    return float(x)


def _checked(v):
    if isinstance(v, float) and (not math.isfinite(v) or abs(v) > EXACT):
        raise Uncertain("math() result outside the exact double range")
    if isinstance(v, int) and abs(v) > EXACT:
        raise Uncertain("long beyond 2^53")
    return v


def _java_double_str(x):
    if x == 0 or not 1e-3 <= abs(x) < 1e7:
        raise Uncertain("Double.toString notation")
    return repr(x)


ZERO = [False]  # set when a result depends on -0.0 == 0 (spec: equal; see greference)


def _note_zero(a, b):
    if a == 0 and b == 0 and not isinstance(a, bool) and not isinstance(b, bool) and             math.copysign(1, a) != math.copysign(1, b):
        ZERO[0] = True


def _eq(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    _note_zero(a, b)
    return a == b


def _cmp(a, b, c):
    if c == "=":
        return _eq(a, b)
    if c == "<>":
        return not _eq(a, b)
    if isinstance(a, bool) or isinstance(b, bool):
        raise Uncertain("boolean ordering")
    _note_zero(a, b)
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[c]


def gtruth(e, G, n):
    """Two-valued predicate value (Gremlin filter semantics)."""
    r = lambda x: gev(x, G, n)
    if "cmp" in e:
        a, b, c = e["a"], e["b"], e["cmp"]
        if _lit(a) and not _lit(b):
            a, b, c = b, a, FLIP[c]
        if _lit(b):
            v = r(a)
            return False if v is None else _cmp(v, b["v"], c)
        va, vb = r(a), r(b)
        return False if va is None or vb is None else _cmp(va, vb, c)
    if "and" in e:
        return gtruth(e["and"][0], G, n) and gtruth(e["and"][1], G, n)
    if "or" in e:
        return gtruth(e["or"][0], G, n) or gtruth(e["or"][1], G, n)
    if "xor" in e:
        return gtruth(e["xor"][0], G, n) != gtruth(e["xor"][1], G, n)
    if "not" in e:
        return not gtruth(e["not"], G, n)
    if "isnull" in e:
        return r(e["isnull"]) is None
    if "notnull" in e:
        return r(e["notnull"]) is not None
    if "in" in e:
        v = r(e["in"])
        return v is not None and any(_eq(v, x) for x in e["list"])
    if "str" in e:
        a, b = r(e["a"]), e["b"]["v"]
        if a is None:
            return False
        _bmp(a), _bmp(b)
        return {"starts": a.startswith(b), "ends": a.endswith(b), "contains": b in a}[e["str"]]
    return r(e) is True


def gev(e, G, n):
    """Value of a value traversal; None means nothing (no traverser)."""
    r = lambda x: gev(x, G, n)
    if is_pred(e):
        return gtruth(e, G, n)
    if "p" in e:
        return n.get(e["p"])
    if "v" in e:
        return e["v"]
    if "op" in e:
        a, b = r(e["a"]), r(e["b"])
        if a is None or b is None:
            return None
        a, b, op = _f(a), _f(b), e["op"]
        if op in "/%" and b == 0:
            raise Uncertain("division by zero")
        v = {"+": lambda: a + b, "-": lambda: a - b, "*": lambda: a * b, "/": lambda: a / b,
             "%": lambda: math.fmod(a, b)}[op]()
        return _checked(v)
    if "fn" in e:
        f, args = e["fn"], e["args"]
        if f == "coalesce":
            return next((v for v in (r(a) for a in args) if v is not None), None)
        x = r(args[0])
        if x is None:
            return None
        if f in ("toInteger", "toFloat", "abs", "sign", "floor", "ceil"):
            x = _f(x)
            return {"toInteger": lambda: _signum(x) * math.floor(abs(x)), "toFloat": lambda: x,
                    "abs": lambda: abs(x), "sign": lambda: _signum(x), "floor": lambda: _ieee(math.floor(x), x),
                    "ceil": lambda: _ieee(math.ceil(x), x)}[f]()
        if f == "toString":
            if isinstance(x, bool):
                return "true" if x else "false"
            return str(x) if isinstance(x, int) else _java_double_str(x)
        s = _bmp(x)
        if f == "size":
            return float(len(s))
        if f == "toUpper":
            return s.upper()
        if f == "toLower":
            return s.lower()
        if f == "reverse":
            return s[::-1]
        if f == "left":
            return s[:args[1]["v"]]
        if f == "right":
            return s[-args[1]["v"]:]
        if f == "substring":
            a, l = args[1]["v"], args[2]["v"]
            if a > len(s):
                raise Uncertain("substring start beyond length")
            return s[a:a + l]
    if "case" in e:
        for c, v in e["case"]:
            if gtruth(c, G, n):
                return r(v)
        return r(e["else"]) if e.get("else") is not None else None
    if "nb" in e:
        ms = [G.nodes[mid] for mid in G.neighbors(n["id"], e["nb"])]
        if e.get("where") is not None:
            ms = [m for m in ms if gtruth(e["where"], G, m)]
        if e["agg"] == "count":
            return len(ms)
        vals = [v for v in (gev(e["expr"], G, m) for m in ms) if v is not None]
        if not vals:
            return None
        if any(isinstance(v, str) for v in vals) and any(not isinstance(v, str) for v in vals):
            raise Uncertain("mixed min/max")
        if e["agg"] == "sum":
            total = 0
            for v in vals:
                total = _checked(total + v)
            if ill_conditioned(vals, total):
                raise Uncertain("ill-conditioned float sum")
            return total
        _zero_groups(vals)  # min/max between -0.0 and 0.0 depends on signed-zero semantics
        return min(vals) if e["agg"] == "min" else max(vals)
    raise Invalid("evaluate")


def _zero_groups(vs):
    """Spec: -0.0 == 0.0, so they form one group (_key merges them); flag the dependence."""
    zs = {math.copysign(1, v) for v in vs if isinstance(v, float) and v == 0}
    if len(zs) > 1:
        ZERO[0] = True


def greference(feature, g):
    """Expected rows and delta under the specification (Gremlin semantics: numbers compare by
    value after promotion and -0.0 == 0.0). ZERO[0] records whether the rows depend on that rule."""
    ZERO[0] = False
    G = GGraph(g)
    nodes = sorted(g["nodes"], key=lambda n: n["id"])
    shape, e = feature["shape"], feature["expr"]
    if shape == "filter":
        truth = [(n, gtruth(e, G, n)) for n in nodes]
        return [[n["id"]] for n, t in truth if t], len({t for _, t in truth})
    vals = [(n, gev(e, G, n)) for n in nodes]
    delta = len({_key(v) for _, v in vals})
    present = [v for _, v in vals if v is not None]
    if shape == "projection":
        return [[n["id"], v] if v is not None else [n["id"]] for n, v in vals], delta
    if shape == "group":
        _zero_groups(present)
        groups = {}
        for v in present:
            groups.setdefault(_key(v), [v, 0])[1] += 1
        return [[v, c] for v, c in groups.values()], delta
    if shape == "order":
        if any(isinstance(v, str) for v in present) and any(not isinstance(v, str) for v in present):
            raise Uncertain("mixed order key")
        if any(isinstance(v, float) and v == 0 and math.copysign(1, v) < 0 for v in present) and                 any(v == 0 and not (isinstance(v, float) and math.copysign(1, v) < 0) for v in present):
            ZERO[0] = True  # spec: -0.0 and 0 tie (broken by id)
        key = {n["id"]: v for n, v in vals}
        ordered = [n for n, v in vals if v is not None]
        ordered.sort(key=lambda n: key[n["id"]], reverse=bool(feature.get("desc")))
        return [[[n["id"] for n in ordered]]], delta
    if not present:
        return [[0]], delta
    if any(isinstance(v, str) for v in present) and any(not isinstance(v, str) for v in present):
        raise Uncertain("mixed total")
    if typeof(e) == "str":
        return [[len(present), min(present), max(present)]], delta
    total = 0
    for v in present:
        total = _checked(total + v)
    if ill_conditioned(present, total):
        raise Uncertain("ill-conditioned float sum")
    return [[len(present), total, min(present), max(present)]], delta


def gadmit(feature, g, row_budget=3000):
    """(case dict, None) if admitted under the Gremlin profile on g, else (None, reason)."""
    try:
        gcheck(feature)
        q = gquery(feature)
        rows, delta = greference(feature, g)
    except Invalid as err:
        return None, "invalid: " + str(err)
    except Uncertain as err:
        return None, "outside contract: " + str(err)
    if len(rows) > row_budget:
        return None, "row budget"
    if delta < gecko_ast.MIN_DELTA:
        return None, "not discriminative"
    return {"id": "grem/" + feature["fid"], "family": "gremlin_" + feature["shape"], "query": q,
            "expected_rows": rows, "contract": "Gremlin profile of a closed feature", "cell": cell(feature),
            "language": "gremlin", "zero_sensitive": ZERO[0]}, None


def _gval(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v}L"
    if isinstance(v, float):
        return repr(v) + "d"
    return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"


def load_script(g):
    """Groovy that builds graph g in the owned in-memory graph (absent NULL properties)."""
    lines = ["v = [:]"]
    for n in sorted(g["nodes"], key=lambda n: n["id"]):
        props = "".join(f".property('{k}', {_gval(n[k])})" for k in ("id", "age", "score", "name", "flag")
                        if n.get(k) is not None)
        lines.append(f"v[{n['id']}] = g.addV('P'){props}.next()")
    for e in g["edges"]:
        lines.append(f"g.addE('R').from(v[{e['src']}]).to(v[{e['dst']}]).iterate()")
    lines.append("g.V().count().next()")
    return "\n".join(lines)


# Hand-written seed features: the fixed Gremlin catalog before any generated feature exists.
SEEDS = [
    ("projection", {"op": "/", "a": {"p": "age"}, "b": {"v": 2}}),
    ("projection", {"op": "%", "a": {"p": "age"}, "b": {"v": 3}}),
    ("projection", {"fn": "toInteger", "args": [{"p": "score"}]}),
    ("projection", {"fn": "coalesce", "args": [{"p": "age"}, {"v": -1}]}),
    ("projection", {"nb": "both", "agg": "count", "expr": {"p": "id", "of": "m"}, "where": None}),
    ("projection", {"nb": "out", "agg": "sum", "expr": {"p": "age", "of": "m"}, "where": None}),
    ("projection", {"fn": "size", "args": [{"p": "name"}]}),
    ("projection", {"fn": "toUpper", "args": [{"p": "name"}]}),
    ("filter", {"not": {"cmp": ">", "a": {"p": "age"}, "b": {"v": 2}}}),
    ("filter", {"or": [{"isnull": {"p": "age"}}, {"p": "flag"}]}),
    ("filter", {"in": {"p": "score"}, "list": [0, 0.5, -0.5]}),
    ("filter", {"str": "starts", "a": {"p": "name"}, "b": {"v": "n"}}),
    ("filter", {"cmp": "<", "a": {"p": "age"}, "b": {"nb": "in", "agg": "count", "expr": {"p": "id", "of": "m"},
                                                         "where": None}}),
    ("group", {"fn": "sign", "args": [{"p": "score"}]}),
    ("group", {"p": "flag"}),
    ("group", {"case": [[{"cmp": ">=", "a": {"p": "age"}, "b": {"v": 3}}, {"v": "high"}]], "else": {"v": "low"}}),
    ("order", {"p": "age"}),
    ("order", {"p": "name"}),
    ("order", {"op": "*", "a": {"p": "score"}, "b": {"v": -1}}),
    ("order", {"nb": "out", "agg": "max", "expr": {"p": "score", "of": "m"}, "where": None}),
    ("total", {"p": "score"}),
    ("total", {"p": "name"}),
    ("total", {"nb": "both", "agg": "min", "expr": {"p": "age", "of": "m"}, "where": None}),
]


def seed_features():
    return [{"fid": f"s{k:03d}", "shape": s, "desc": s == "order" and k % 2 == 0, "expr": e}
            for k, (s, e) in enumerate(SEEDS)]
