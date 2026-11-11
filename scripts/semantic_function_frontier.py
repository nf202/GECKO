"""Row-dependent references for scalar functions, subqueries, map projection and ordering.

Every expected value is computed from the fixture in Python under the declared
openCypher/Neo4j-documented semantics; no engine is consulted. Each projection
template returns one row per :P node keyed by n.id, so a per-binding error is
observable. Templates whose semantics engines are known to define differently
are omitted rather than labelled.
"""
from __future__ import annotations
import math
import re
from collections import defaultdict


def tdiv(a, b):
    if a is None or b is None:
        return None
    if isinstance(a, int) and isinstance(b, int):
        q = abs(a) // abs(b)
        return q if (a >= 0) == (b > 0) else -q
    return a / b


def tmod(a, b):
    if a is None or b is None:
        return None
    return a - b * tdiv(a, b)


def nn(f):
    """Lift f so that any NULL argument yields NULL."""
    return lambda *xs: None if any(x is None for x in xs) else f(*xs)


def and3(a, b):
    if a is False or b is False:
        return False
    return None if a is None or b is None else True


def or3(a, b):
    if a is True or b is True:
        return True
    return None if a is None or b is None else False


def not3(a):
    return None if a is None else not a


def xor3(a, b):
    return None if a is None or b is None else a != b


def cmp_lt(a, b):
    return None if a is None or b is None else a < b


def cases(g, strict=True):
    nodes = sorted(g["nodes"], key=lambda n: n["id"])
    out_adj, in_adj = defaultdict(list), defaultdict(list)
    for e in g["edges"]:
        out_adj[e["src"]].append(e)
        in_adj[e["dst"]].append(e)
    by_id = {n["id"]: n for n in nodes}

    def undirected(nid):
        # (n)--(m): each incident relationship once per direction; a self-loop once.
        rows = [(e, e["dst"]) for e in out_adj[nid]]
        rows += [(e, e["src"]) for e in in_adj[nid] if e["src"] != e["dst"]]
        return rows

    out = []

    def per_node(key, family, expr, fn, extra_cols=1):
        values = [[n["id"], fn(n)] for n in nodes]
        if len({repr(v[1]) for v in values}) < 2:
            if strict:
                raise AssertionError(f"constant reference: {key}")
            return
        out.append({"id": "functions/" + key, "family": family,
                    "query": f"MATCH (n:P) RETURN n.id AS c0, {expr} AS c1", "expected_rows": values,
                    "contract": "openCypher scalar semantics; NULL propagates unless stated."})

    name, age, score, flag = (lambda n: n["name"]), (lambda n: n["age"]), (lambda n: n["score"]), (lambda n: n["flag"])

    # ---- strings ----
    per_node("str/upper", "string_fn", "toUpper(n.name)", lambda n: nn(str.upper)(name(n)))
    per_node("str/lower", "string_fn", "toLower(n.name)", lambda n: nn(str.lower)(name(n)))
    per_node("str/size", "string_fn", "size(n.name)", lambda n: nn(len)(name(n)))
    per_node("str/starts-n", "string_pred", "n.name STARTS WITH 'n'", lambda n: nn(lambda s: s.startswith("n"))(name(n)))
    per_node("str/starts-empty", "string_pred", "n.name STARTS WITH ''", lambda n: nn(lambda s: True)(name(n)))
    per_node("str/ends-1", "string_pred", "n.name ENDS WITH '1'", lambda n: nn(lambda s: s.endswith("1"))(name(n)))
    per_node("str/contains-empty", "string_pred", "n.name CONTAINS ''", lambda n: nn(lambda s: True)(name(n)))
    per_node("str/contains-N", "string_pred", "n.name CONTAINS 'N'", lambda n: nn(lambda s: "N" in s)(name(n)))
    per_node("str/regex", "string_pred", "n.name =~ 'n[0-9]'", lambda n: nn(lambda s: re.fullmatch("n[0-9]", s) is not None)(name(n)))
    per_node("str/regex-empty", "string_pred", "n.name =~ ''", lambda n: nn(lambda s: s == "")(name(n)))
    per_node("str/substring01", "string_fn", "substring(n.name, 0, 1)", lambda n: nn(lambda s: s[:1])(name(n)))
    per_node("str/left", "string_fn", "left(n.name, 1)", lambda n: nn(lambda s: s[:1])(name(n)))
    per_node("str/right", "string_fn", "right(n.name, 1)", lambda n: nn(lambda s: s[-1:] if s else "")(name(n)))
    per_node("str/reverse", "string_fn", "reverse(n.name)", lambda n: nn(lambda s: s[::-1])(name(n)))
    per_node("str/replace", "string_fn", "replace(n.name, 'n', 'xy')", lambda n: nn(lambda s: s.replace("n", "xy"))(name(n)))
    per_node("str/trim", "string_fn", "trim(' ' + n.name + ' ')", lambda n: nn(lambda s: s.strip(" "))(name(n)))
    per_node("str/concat-id", "string_fn", "n.name + toString(n.id)", lambda n: nn(lambda s: s + str(n["id"]))(name(n)))
    per_node("str/lt-n1", "string_cmp", "n.name < 'n1'", lambda n: cmp_lt(name(n), "n1"))
    per_node("str/ge-N", "string_cmp", "n.name >= 'N'", lambda n: nn(lambda s: s >= "N")(name(n)))
    per_node("str/eq-empty", "string_cmp", "n.name = ''", lambda n: nn(lambda s: s == "")(name(n)))
    per_node("str/in-list", "string_cmp", "n.name IN ['', 'n1']", lambda n: nn(lambda s: s in ("", "n1"))(name(n)))
    per_node("str/coalesce", "string_fn", "coalesce(n.name, 'none')", lambda n: "none" if name(n) is None else name(n))

    # ---- numbers ----
    per_node("num/div2", "integer_arith", "n.age / 2", lambda n: tdiv(age(n), 2))
    per_node("num/div-neg3", "integer_arith", "n.age / -3", lambda n: tdiv(age(n), -3))
    per_node("num/mod3", "integer_arith", "n.age % 3", lambda n: tmod(age(n), 3))
    per_node("num/mod-neg4", "integer_arith", "n.age % -4", lambda n: tmod(age(n), -4))
    per_node("num/div-float", "mixed_arith", "n.age / 2.0", lambda n: nn(lambda a: a / 2.0)(age(n)))
    per_node("num/abs", "numeric_fn", "abs(n.age)", lambda n: nn(abs)(age(n)))
    per_node("num/sign", "numeric_fn", "sign(n.age)", lambda n: nn(lambda a: (a > 0) - (a < 0))(age(n)))
    per_node("num/floor", "numeric_fn", "floor(n.score)", lambda n: nn(lambda s: float(math.floor(s)))(score(n)))
    per_node("num/ceil", "numeric_fn", "ceil(n.score)", lambda n: nn(lambda s: float(math.ceil(s)))(score(n)))
    per_node("num/pow", "numeric_fn", "n.age ^ 2", lambda n: nn(lambda a: float(a) ** 2)(age(n)))
    per_node("num/to-int", "conversion", "toInteger(n.score)", lambda n: nn(lambda s: int(s))(score(n)))
    per_node("num/to-float", "conversion", "toFloat(n.age)", lambda n: nn(float)(age(n)))
    per_node("num/to-string", "conversion", "toString(n.age)", lambda n: nn(str)(age(n)))
    per_node("num/range-size", "list_fn", "size(range(0, coalesce(n.age, 0)))",
             lambda n: len(range(0, (age(n) or 0) + 1)))
    per_node("num/range-down", "list_fn", "range(coalesce(n.age, 0), 0, -2)",
             lambda n: list(range(age(n) or 0, -1, -2)))
    per_node("num/gt-mixed", "mixed_cmp", "n.age > n.score", lambda n: nn(lambda a, s: a > s)(age(n), score(n)))
    per_node("num/eq-mixed", "mixed_cmp", "n.age = n.score * 2", lambda n: nn(lambda a, s: a == s * 2)(age(n), score(n)))
    per_node("num/neg-zero", "mixed_cmp", "n.score * -1 = 0.0", lambda n: nn(lambda s: -s == 0.0)(score(n)))

    # ---- CASE and three-valued logic ----
    per_node("case/simple-null", "case_null",
             "CASE n.age WHEN 0 THEN 'zero' WHEN NULL THEN 'null' ELSE 'other' END",
             lambda n: "zero" if age(n) == 0 else "other")
    per_node("case/searched-gap", "case_null", "CASE WHEN n.age > 0 THEN 1 WHEN n.age < 0 THEN -1 END",
             lambda n: None if age(n) in (None, 0) else (1 if age(n) > 0 else -1))
    per_node("case/simple-bool", "case_null", "CASE n.flag WHEN true THEN 't' WHEN false THEN 'f' END",
             lambda n: {True: "t", False: "f"}.get(flag(n)))
    per_node("bool/or-null", "three_valued", "n.age IS NULL OR n.flag",
             lambda n: or3(age(n) is None, flag(n)))
    per_node("bool/xor", "three_valued", "n.flag XOR (n.age > 0)",
             lambda n: xor3(flag(n), nn(lambda a: a > 0)(age(n))))
    per_node("bool/not-and", "three_valued", "NOT (n.flag AND n.age < 3)",
             lambda n: not3(and3(flag(n), nn(lambda a: a < 3)(age(n)))))

    # ---- pattern comprehension, subqueries, maps ----
    per_node("pat/out-size", "pattern_comprehension", "size([(n)-[:R]->(m) | m.id])", lambda n: len(out_adj[n["id"]]))
    per_node("pat/in-size", "pattern_comprehension", "size([(n)<-[:R]-(m) | m.id])", lambda n: len(in_adj[n["id"]]))
    per_node("pat/und-size", "pattern_comprehension", "size([(n)-[:R]-(m) | m.id])", lambda n: len(undirected(n["id"])))
    per_node("pat/sum-pos", "pattern_comprehension",
             "reduce(s = 0, x IN [(n)-[:R]->(m) WHERE m.age > 0 | m.age] | s + x)",
             lambda n: sum(by_id[e["dst"]]["age"] for e in out_adj[n["id"]]
                           if by_id[e["dst"]]["age"] is not None and by_id[e["dst"]]["age"] > 0))
    per_node("pat/loop-size", "pattern_comprehension", "size([(n)-[r:R]->(n) | r.eid])",
             lambda n: sum(e["dst"] == n["id"] for e in out_adj[n["id"]]))
    per_node("sub/count-out", "count_subquery", "COUNT { (n)-[:R]->(m) }", lambda n: len(out_adj[n["id"]]))
    per_node("sub/count-und", "count_subquery", "COUNT { (n)-[:R]-(m) }", lambda n: len(undirected(n["id"])))
    per_node("sub/count-where", "count_subquery", "COUNT { (n)-[:R]->(m) WHERE m.age IS NULL }",
             lambda n: sum(by_id[e["dst"]]["age"] is None for e in out_adj[n["id"]]))
    per_node("sub/exists-null", "exists_subquery", "EXISTS { (n)-[:R]->(m) WHERE m.age IS NULL }",
             lambda n: any(by_id[e["dst"]]["age"] is None for e in out_adj[n["id"]]))
    per_node("sub/exists-loop", "exists_subquery", "EXISTS { (n)-[:R]->(n) }",
             lambda n: any(e["dst"] == n["id"] for e in out_adj[n["id"]]))
    per_node("sub/exists-2hop", "exists_subquery", "EXISTS { (n)-[:R]->()-[:R]->(n) }",
             lambda n: any(e2["dst"] == n["id"] for e in out_adj[n["id"]] for e2 in out_adj[e["dst"]] if e2 is not e))
    per_node("map/projection", "map_projection", "n{.id, .age}", lambda n: {"id": n["id"], "age": age(n)})
    per_node("map/literal-entry", "map_projection", "n{.name, twice: n.age * 2}",
             lambda n: {"name": name(n), "twice": nn(lambda a: a * 2)(age(n))})
    per_node("map/properties", "map_projection", "properties(n)",
             lambda n: {k: n[k] for k in ("id", "age", "score", "name", "flag") if n[k] is not None})
    per_node("map/keys-size", "map_projection", "size(keys(n))",
             lambda n: sum(n[k] is not None for k in ("id", "age", "score", "name", "flag")))
    per_node("map/has-age-key", "map_projection", "'age' IN keys(n)", lambda n: age(n) is not None)

    # ---- grouped aggregates (rows keyed by group) ----
    def grouped(key, family, by_expr, by_fn, agg_exprs, agg_fn):
        groups = defaultdict(list)
        for n in nodes:
            groups[by_fn(n)].append(n)
        rows = [[k] + agg_fn(ns) for k, ns in groups.items()]
        cols = ", ".join(f"{e} AS c{i + 1}" for i, e in enumerate(agg_exprs))
        out.append({"id": "functions/" + key, "family": family,
                    "query": f"MATCH (n:P) RETURN {by_expr} AS c0, {cols}", "expected_rows": rows,
                    "contract": "Grouping keys compare NULL equal to NULL; aggregates ignore NULL; sum of none is 0."})

    def agg_num(ns):
        ages = [n["age"] for n in ns if n["age"] is not None]
        scores = [n["score"] for n in ns if n["score"] is not None]
        return [len(ns), len(ages), sum(ages), min(ages) if ages else None, max(ages) if ages else None,
                sum(scores) / len(scores) if scores else None]
    num_aggs = ["count(*)", "count(n.age)", "sum(n.age)", "min(n.age)", "max(n.age)", "avg(n.score)"]
    grouped("agg/by-flag", "grouped_aggregate", "n.flag", flag, num_aggs, agg_num)
    grouped("agg/by-name", "grouped_aggregate", "n.name", name, num_aggs, agg_num)
    grouped("agg/by-sign", "grouped_aggregate", "sign(n.age)",
            lambda n: nn(lambda a: (a > 0) - (a < 0))(age(n)), num_aggs, agg_num)

    def agg_str(ns):
        names = [n["name"] for n in ns if n["name"] is not None]
        return [min(names) if names else None, max(names) if names else None, len(set(names)),
                len({n["age"] for n in ns if n["age"] is not None})]
    str_aggs = ["min(n.name)", "max(n.name)", "count(DISTINCT n.name)", "size(collect(DISTINCT n.age))"]
    grouped("agg/str-by-flag", "grouped_aggregate", "n.flag", flag, str_aggs, agg_str)
    grouped("agg/str-by-age-mod", "grouped_aggregate", "n.age % 2",
            lambda n: tmod(age(n), 2), str_aggs, agg_str)

    # ---- ORDER BY captured through collect ----
    def ordered(key, sort_expr, keyfn, desc=False, skip=0, limit=None):
        # Stable two-pass sort: n.id ascending tiebreak, then the primary key in its own direction.
        ordered_nodes = sorted(nodes, key=lambda n: n["id"])
        present = sorted((n for n in ordered_nodes if keyfn(n) is not None), key=keyfn, reverse=desc)
        missing = [n for n in ordered_nodes if keyfn(n) is None]
        ordered_ids = [n["id"] for n in (missing + present if desc else present + missing)]
        ordered_ids = ordered_ids[skip:None if limit is None else skip + limit]
        tail = (f" SKIP {skip}" if skip else "") + (f" LIMIT {limit}" if limit is not None else "")
        direction = " DESC" if desc else ""
        out.append({"id": "functions/order/" + key, "family": "order_by_nulls",
                    "query": f"MATCH (n:P) WITH n ORDER BY {sort_expr}{direction}, n.id{tail} RETURN collect(n.id) AS c0",
                    "expected_rows": [[ordered_ids]],
                    "contract": "NULL sorts last ascending, first descending; collect preserves WITH order."})
    ordered("name-asc", "n.name", name)
    ordered("name-desc", "n.name", name, desc=True)
    ordered("age-asc", "n.age", age)
    ordered("age-desc-skip-limit", "n.age", age, desc=True, skip=3, limit=8)
    ordered("score-asc", "n.score", score)
    ordered("flag-asc", "n.flag", flag)
    ordered("flag-desc", "n.flag", flag, desc=True)
    ordered("upper-name", "toUpper(n.name)", lambda n: nn(str.upper)(name(n)))
    ordered("age-mod", "n.age % 3", lambda n: tmod(age(n), 3), limit=12)
    distinct_ages = sorted({n["age"] for n in nodes if n["age"] is not None})
    out.append({"id": "functions/order/distinct-age", "family": "order_by_nulls",
                "query": "MATCH (n:P) WITH DISTINCT n.age AS a ORDER BY a RETURN collect(a) AS c0",
                "expected_rows": [[distinct_ages]], "contract": "collect drops NULL; DISTINCT keeps one NULL group."})
    assert len({c["id"] for c in out}) == len(out) and len({c["query"] for c in out}) == len(out)
    return out
