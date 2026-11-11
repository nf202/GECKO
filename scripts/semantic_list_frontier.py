"""Finite list/quantifier references; no query parsing or engine-derived answers.

All cases use the existing fixture. Quantifiers follow the declared Cypher
profile: False dominates ALL, True dominates ANY, NONE is NOT ANY, an empty
list has vacuous ALL/NONE truth, and a NULL list yields NULL.
"""
from __future__ import annotations


def quantified(kind, values):
    if values is None:
        return None
    if kind == "all":
        return False if False in values else None if None in values else True
    if kind in ("any", "none"):
        answer = True if True in values else None if None in values else False
        return answer if kind == "any" or answer is None else not answer
    raise ValueError(kind)


def list_inputs(age):
    return {
        "mixed": [age, -1, 0, 1, None, age],
        "empty": [],
        "null": None,
        "singleton": [age],
        "compacted": [x for x in [age, -1, 0, 1, None] if x is not None],
    }


LIST_EXPRESSIONS = {
    "mixed": "[a.age,-1,0,1,NULL,a.age]",
    "empty": "[]",
    "null": "NULL",
    "singleton": "[a.age]",
    "compacted": "[x IN [a.age,-1,0,1,NULL] WHERE x IS NOT NULL]",
}


def predicate(values, threshold):
    if values is None:
        return None
    if threshold is None:
        return [x is None for x in values]
    return [None if x is None else x > threshold for x in values]


def fold_sum(values, coalesced=False):
    if values is None:
        return None
    if not coalesced and None in values:
        return None
    return sum(x for x in values if x is not None)


def optional_groups(g, mode, threshold):
    """Return ordered COLLECT(age) groups, dropping NULL collection values."""
    nodes = {n["id"]: n for n in g["nodes"]}
    groups = []
    for a in g["nodes"]:
        matches = sorted((e for e in g["edges"] if e["src"] == a["id"]), key=lambda e: e["eid"])
        if mode == "target-local":
            matches = [e for e in matches if nodes[e["dst"]]["age"] is not None
                       and nodes[e["dst"]]["age"] > threshold]
        elif mode == "source-local":
            if a["age"] is None or a["age"] <= threshold:
                matches = []
        elif mode not in ("plain", "post"):
            raise ValueError(mode)
        ages = [nodes[e["dst"]]["age"] if e is not None else None for e in matches or [None]]
        if mode == "post":
            ages = [x for x in ages if x is not None and x > threshold]
            if not ages:
                continue
        groups.append([a["id"], [x for x in ages if x is not None]])
    return groups


def cases(g):
    from campaign_single_seed_frontier import alias_returns, FORBIDDEN
    out = []

    def add(key, family, query, expected, contract):
        query = alias_returns(" " + query)
        if FORBIDDEN.search(query) or ";" in query:
            raise ValueError("non-read list template")
        out.append({"id": "lists/" + key, "family": family, "query": query,
                    "expected_rows": expected, "contract": contract})

    for list_key, expression in LIST_EXPRESSIONS.items():
        inputs = [(n["id"], list_inputs(n["age"])[list_key]) for n in g["nodes"]]
        prefix = f"MATCH (a:P) WITH a,{expression} AS xs"
        for threshold in (-1, 0, 1, None):
            pred = "x IS NULL" if threshold is None else f"x > {threshold}"
            label = "is-null" if threshold is None else f"gt-{threshold}"
            for kind in ("any", "all", "none"):
                term = f"{kind}(x IN xs WHERE {pred})"
                rows = [[aid, quantified(kind, predicate(xs, threshold))] for aid, xs in inputs]
                key = f"quantifier/{list_key}/{kind}/{label}/"
                contract = "Declared Cypher three-valued list quantifier profile; NULL list differs from an empty list."
                add(key + "projection", "list_quantifier_projection", prefix + " RETURN a.id," + term, rows, contract)
                add(key + "filter", "list_quantifier_filter", prefix + " WHERE " + term + " RETURN a.id",
                    [[aid] for aid, answer in rows if answer is True], contract)
                add(key + "count", "list_quantifier_count", prefix + " WHERE " + term + " RETURN count(*)",
                    [[sum(answer is True for aid, answer in rows)]], contract)
                add(key + "case", "list_quantifier_case", prefix + " RETURN a.id,CASE WHEN " + term +
                    " THEN 1 WHEN NOT (" + term + ") THEN 0 ELSE -1 END",
                    [[aid, -1 if answer is None else int(answer)] for aid, answer in rows], contract)

        for index in (-6, -1, 0, 1, 6):
            add(f"index/{list_key}/{index}", "nullable_list_index", prefix + f" RETURN a.id,xs[{index}]",
                [[aid, xs[index] if xs is not None and -len(xs) <= index < len(xs) else None] for aid, xs in inputs],
                "Negative indices count from the end; NULL and out-of-bounds access return NULL.")
        for lo, hi in ((0, 0), (0, 2), (-3, -1), (-9, 9), (3, 1)):
            add(f"slice/{list_key}/{lo}-{hi}", "nullable_list_slice", prefix + f" RETURN a.id,xs[{lo}..{hi}]",
                [[aid, None if xs is None else xs[lo:hi]] for aid, xs in inputs],
                "Half-open slice with negative endpoint translation, clamping and an empty inverted interval.")
        for threshold in (-1, 0, 1):
            add(f"comprehension/{list_key}/{threshold}", "nullable_list_comprehension",
                prefix + f" RETURN a.id,[x IN xs WHERE x > {threshold} | x+1]",
                [[aid, None if xs is None else [x+1 for x in xs if x is not None and x > threshold]] for aid, xs in inputs],
                "Only True predicate values survive comprehension; a NULL input list remains NULL.")
        for coalesced in (False, True):
            term = "coalesce(x,0)" if coalesced else "x"
            add(f"reduce/{list_key}/{int(coalesced)}", "nullable_list_reduce",
                prefix + f" RETURN a.id,reduce(s=0,x IN xs | s+{term})",
                [[aid, fold_sum(xs, coalesced)] for aid, xs in inputs],
                "REDUCE starts at 0; ordinary addition propagates NULL; coalescing elements preserves a NULL input list.")

    for threshold in (-1, 0, 1):
        for mode in ("plain", "target-local", "source-local", "post"):
            prefix = "MATCH (a:P) OPTIONAL MATCH (a)-[r:R]->(b:P)"
            if mode == "target-local":
                prefix += f" WHERE b.age > {threshold}"
            elif mode == "source-local":
                prefix += f" WHERE a.age > {threshold}"
            elif mode == "post":
                prefix += f" WITH a,r,b WHERE b.age > {threshold}"
            prefix += " WITH a,r,b ORDER BY a.id,r.eid WITH a,collect(b.age) AS xs"
            rows = optional_groups(g, mode, threshold)
            key = f"optional/{mode}/{threshold}/"
            contract = "OPTIONAL is a left join with clause-local predicates; post filtering can remove a group. COLLECT drops NULLs and keeps duplicates in the preceding total relationship-ID order."
            add(key + "rows", "optional_ordered_collect", prefix + " RETURN a.id,xs", rows, contract)
            add(key + "unwind", "optional_collect_unwind", prefix + " UNWIND xs AS x RETURN a.id,x",
                [[aid, x] for aid, xs in rows for x in xs], contract)
            add(key + "reduce", "optional_collect_reduce", prefix + " RETURN a.id,size(xs),reduce(s=0,x IN xs | s+x)",
                [[aid, len(xs), sum(xs)] for aid, xs in rows], contract)
            add(key + "quantifiers", "optional_collect_quantifiers", prefix +
                f" RETURN a.id,any(x IN xs WHERE x > {threshold}),all(x IN xs WHERE x > {threshold}),none(x IN xs WHERE x > {threshold})",
                [[aid] + [quantified(kind, predicate(xs, threshold)) for kind in ("any", "all", "none")] for aid, xs in rows], contract)

    assert len({c["id"] for c in out}) == len(out)
    # Several plain OPTIONAL controls do not use the threshold. Count each
    # exact query once, keeping the redundant template names as provenance.
    from gqltest.semantic_suite import bag
    unique = {}
    for case in out:
        previous = unique.get(case["query"])
        if previous is None:
            unique[case["query"]] = case
        else:
            if bag(previous["expected_rows"]) != bag(case["expected_rows"]):
                raise AssertionError("inconsistent references for the same query")
            previous.setdefault("equivalent_template_ids", []).append(case["id"])
    return list(unique.values())
