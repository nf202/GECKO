"""Typed composed predicates with an independent three-valued Python reference."""
from __future__ import annotations
import json
import random


def truth_not(a):
    return None if a is None else not a


def truth_and(a, b):
    if a is False or b is False:
        return False
    return None if a is None or b is None else True


def truth_or(a, b):
    if a is True or b is True:
        return True
    return None if a is None or b is None else False


def value(ast, a, b):
    op = ast[0]
    if op == "lit":
        return ast[1]
    if op == "prop":
        n = a if ast[1] == "a" else b
        return n.get(ast[2]) if n is not None else None
    if op == "coalesce":
        x = value(ast[1], a, b)
        return value(ast[2], a, b) if x is None else x
    if op in ("add", "sub"):
        x, y = value(ast[1], a, b), value(ast[2], a, b)
        if x is None or y is None:
            return None
        return x + y if op == "add" else x - y
    if op == "isnull":
        return value(ast[1], a, b) is None
    if op == "not":
        return truth_not(value(ast[1], a, b))
    x, y = value(ast[1], a, b), value(ast[2], a, b)
    if op == "and":
        return truth_and(x, y)
    if op == "or":
        return truth_or(x, y)
    if op == "in":
        comparisons = [None if x is None or item is None else x == item for item in y]
        return True if True in comparisons else None if None in comparisons else False
    if x is None or y is None:
        return None
    if op == "eq":
        return x == y
    if op == "ne":
        return x != y
    if op == "gt":
        return x > y
    if op == "le":
        return x <= y
    raise ValueError(op)


def compile_expr(ast, names=None):
    names = names or {"a": "a", "b": "b"}
    op = ast[0]
    if op == "lit":
        return json.dumps(ast[1], ensure_ascii=False).replace("null", "NULL") if ast[1] is not None else "NULL"
    if op == "prop":
        return names[ast[1]] + "." + ast[2]
    if op == "coalesce":
        return "coalesce(" + compile_expr(ast[1], names) + "," + compile_expr(ast[2], names) + ")"
    if op == "isnull":
        return "(" + compile_expr(ast[1], names) + " IS NULL)"
    if op == "not":
        return "(NOT " + compile_expr(ast[1], names) + ")"
    operators = {"add": "+", "sub": "-", "eq": "=", "ne": "<>", "gt": ">", "le": "<=",
                 "in": "IN", "and": "AND", "or": "OR"}
    return "(" + compile_expr(ast[1], names) + " " + operators[op] + " " + compile_expr(ast[2], names) + ")"


def predicates(seed, count=64):
    aa, ba = ["prop", "a", "age"], ["prop", "b", "age"]
    lit = lambda x: ["lit", x]
    atoms = [["gt", aa, lit(t)] for t in (-2, -1, 0, 1, 2)]
    atoms += [["le", ba, lit(t)] for t in (-2, -1, 0, 1, 2)]
    atoms += [["eq", aa, ba], ["isnull", aa], ["isnull", ba],
              ["eq", ["prop", "a", "flag"], lit(True)],
              ["eq", ["prop", "b", "flag"], lit(False)],
              ["ne", ["coalesce", aa, lit(0)], ba],
              ["gt", ["sub", aa, ba], lit(0)],
              ["in", aa, lit([None, -1, 0, 2])],
              ["not", ["in", ba, lit([-2, 1, None])]]]
    rng = random.Random(seed)
    chosen, seen = [], set()
    def admit(p):
        key = json.dumps(p, sort_keys=True)
        if key not in seen:
            chosen.append(p)
            seen.add(key)
    for atom in atoms:
        admit(atom)
    while len(chosen) < count:
        left = rng.choice(atoms)
        right = rng.choice(atoms)
        if rng.random() < .5:
            right = ["not", right]
        expression = [rng.choice(["and", "or"]), left, right]
        if rng.random() < .5:
            expression = [rng.choice(["and", "or"]), expression, rng.choice(atoms)]
        admit(expression)
    return chosen[:count]


def cases(g, seed):
    from campaign_single_seed_frontier import alias_returns, FORBIDDEN
    ns, es = g["nodes"], g["edges"]
    by_id = {n["id"]: n for n in ns}
    edges = [(by_id[e["src"]], e, by_id[e["dst"]]) for e in es]
    optional = []
    for a in ns:
        matching = [r for r in edges if r[0]["id"] == a["id"]]
        optional.extend(matching or [(a, None, None)])
    records = []
    projection = lambda rows: [[a["id"], e["eid"] if e else None, b["id"] if b else None] for a, e, b in rows]
    for number, ast in enumerate(predicates(seed)):
        compiled = compile_expr(ast)
        qualified = [r for r in edges if value(ast, r[0], r[2]) is True]
        local = []
        counts = []
        for a in ns:
            matching = [r for r in qualified if r[0]["id"] == a["id"]]
            local.extend(matching or [(a, None, None)])
            counts.append([a["id"], len(matching)])
        post = [r for r in optional if value(ast, r[0], r[2]) is True]
        templates = [
            ("node-filter", f"MATCH (a:P) WITH a,a AS b WHERE {compiled} RETURN a.id",
             [[a["id"]] for a in ns if value(ast, a, a) is True]),
            ("node-projection", f"MATCH (a:P) WITH a,a AS b RETURN a.id,{compiled}",
             [[a["id"], value(ast, a, a)] for a in ns]),
            ("edge-filter", f"MATCH (a:P)-[r:R]->(b:P) WHERE {compiled} RETURN a.id,r.eid,b.id", projection(qualified)),
            ("optional-local", f"MATCH (a:P) OPTIONAL MATCH (a)-[r:R]->(b:P) WHERE {compiled} RETURN a.id,r.eid,b.id", projection(local)),
            ("optional-post", f"MATCH (a:P) OPTIONAL MATCH (a)-[r:R]->(b:P) WITH a,r,b WHERE {compiled} RETURN a.id,r.eid,b.id", projection(post)),
            ("optional-count", f"MATCH (a:P) OPTIONAL MATCH (a)-[r:R]->(b:P) WHERE {compiled} RETURN a.id,count(r.eid)", counts),
            ("scope-alias", "MATCH (a:P)-[r:R]->(b:P) WITH a AS source,b AS target,r AS link WHERE " +
             compile_expr(ast, {"a": "source", "b": "target"}) + " RETURN source.id,link.eid,target.id", projection(qualified)),
        ]
        for context, query, expected in templates:
            query = alias_returns(query)
            if FORBIDDEN.search(query) or ";" in query:
                raise ValueError("unsafe composed query")
            records.append({"id": f"composed/{number:03d}/{context}", "family": "composed_" + context,
                            "query": query, "expected_rows": expected, "predicate_ast": ast,
                            "contract": "Typed small-integer/Boolean three-valued reference; WHERE admits only True; OPTIONAL retains left rows."})
    return records
