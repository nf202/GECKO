"""Direct graph enumeration for bounded trails, scoped joins and list pipelines."""
from __future__ import annotations
from collections import defaultdict


def paths(g, direction, lo, hi, reuse=False):
    adjacency = defaultdict(list)
    for e in g["edges"]:
        if direction in ("out", "undirected"):
            adjacency[e["src"]].append((e, e["dst"]))
        if direction == "in" or (direction == "undirected" and e["src"] != e["dst"]):
            adjacency[e["dst"]].append((e, e["src"]))
    result = []
    def visit(start, current, ids):
        if lo <= len(ids) <= hi:
            result.append([start, current, list(ids), len(ids)])
        if len(ids) == hi:
            return
        for e, target in adjacency[current]:
            if reuse or e["eid"] not in ids:
                visit(start, target, ids + [e["eid"]])
    for node in g["nodes"]:
        visit(node["id"], node["id"], [])
    return result


def segment(left, rel, right, direction):
    if direction == "out":
        return f"({left})-[{rel}]->({right})"
    if direction == "in":
        return f"({left})<-[{rel}]-({right})"
    return f"({left})-[{rel}]-({right})"


def cases(g):
    from campaign_single_seed_frontier import alias_returns, FORBIDDEN
    out = []
    nodes, edges = g["nodes"], g["edges"]
    by_id = {n["id"]: n for n in nodes}
    def add(key, family, query, expected, contract):
        query = alias_returns(" " + query)
        if FORBIDDEN.search(query) or ";" in query:
            raise ValueError("unsafe structural query")
        out.append({"id": "structural/" + key, "family": family, "query": query,
                    "expected_rows": expected, "contract": contract})
    for direction in ("out", "in", "undirected"):
        for lo, hi in ((0, 0), (0, 1), (1, 1), (1, 2), (2, 3), (0, 3)):
            rows = paths(g, direction, lo, hi)
            pattern = segment("a:P", f"rs:R*{lo}..{hi}", "b:P", direction)
            prefix = f"MATCH p={pattern}"
            contract = "Cypher-profile edge-simple bounded trails; vertices may repeat; undirected self-loops counted once."
            key = f"variable/{direction}/{lo}-{hi}/"
            add(key + "rows", "bounded_trail_rows", prefix + " RETURN a.id,b.id,[e IN relationships(p) | e.eid],length(p)", rows, contract)
            add(key + "count", "bounded_trail_count", prefix + " RETURN count(*)", [[len(rows)]], contract)
            groups = defaultdict(int)
            for a, b, ids, size in rows:
                groups[(a, b)] += 1
            add(key + "group", "bounded_trail_endpoint_group", prefix + " RETURN a.id,b.id,count(*)",
                [[a, b, count] for (a, b), count in groups.items()], contract)
            add(key + "distinct", "bounded_trail_distinct_endpoints", prefix + " RETURN DISTINCT a.id,b.id",
                [[a, b] for a, b in groups], contract)
        for hops in (2, 3):
            for form in ("same-clause", "same-guarded", "split-clause", "split-guarded"):
                rels = [f"r{i}" for i in range(hops)]
                parts = [segment(f"n{i}:P", rel + ":R", f"n{i+1}:P", direction) for i, rel in enumerate(rels)]
                split = form.startswith("split")
                prefix = " MATCH ".join(parts) if split else ", ".join(parts)
                prefix = "MATCH " + prefix
                guarded = form.endswith("guarded")
                if guarded:
                    prefix += " WHERE " + " AND ".join(f"{a}.eid <> {b}.eid" for i, a in enumerate(rels) for b in rels[i+1:])
                rows = paths(g, direction, hops, hops, reuse=split and not guarded)
                query = prefix + " RETURN n0.id,n" + str(hops) + ".id,[" + ",".join(r + ".eid" for r in rels) + "]"
                add(f"fixed/{direction}/{hops}/{form}", "relationship_scope_" + form, query,
                    [[a, b, ids] for a, b, ids, length in rows],
                    "Cypher-profile uniqueness within one MATCH; separate MATCH clauses permit relationship reuse unless explicitly excluded.")

    # Left joins are evaluated independently in each OPTIONAL clause.
    for threshold in (-1, 0, 1):
        for mode in ("plain", "second-local", "second-post", "distinct-rel", "source-local"):
            rows = []
            for a in nodes:
                first = [e for e in edges if e["src"] == a["id"]]
                for e in first or [None]:
                    b = by_id[e["dst"]] if e else None
                    second = [f for f in edges if b and f["src"] == b["id"]]
                    if mode == "second-local":
                        second = [f for f in second if by_id[f["dst"]]["age"] is not None and by_id[f["dst"]]["age"] > threshold]
                    if mode == "distinct-rel":
                        second = [f for f in second if e and f["eid"] != e["eid"]]
                    if mode == "source-local":
                        second = second if a["age"] is not None and a["age"] > threshold else []
                    for f in second or [None]:
                        c = by_id[f["dst"]] if f else None
                        if mode == "second-post" and (c is None or c["age"] is None or c["age"] <= threshold):
                            continue
                        rows.append([a["id"], e["eid"] if e else None, b["id"] if b else None,
                                     f["eid"] if f else None, c["id"] if c else None])
            prefix = "MATCH (a:P) OPTIONAL MATCH (a)-[r:R]->(b:P) OPTIONAL MATCH (b)-[s:R]->(c:P)"
            if mode == "second-local":
                prefix += f" WHERE c.age > {threshold}"
            elif mode == "second-post":
                prefix += f" WITH a,r,b,s,c WHERE c.age > {threshold}"
            elif mode == "distinct-rel":
                prefix += " WHERE r.eid <> s.eid"
            elif mode == "source-local":
                prefix += f" WHERE a.age > {threshold}"
            key = f"optional-chain/{mode}/{threshold}/"
            add(key + "rows", "optional_chain_" + mode, prefix + " RETURN a.id,r.eid,b.id,s.eid,c.id", rows,
                "Two separate left joins; local predicates null-extend the second optional match, post predicates discard rows.")
            groups = defaultdict(list)
            for row in rows:
                groups[row[0]].append(row)
            add(key + "aggregate", "optional_chain_group_" + mode,
                prefix + " RETURN a.id,count(*),count(r.eid),count(s.eid),count(DISTINCT c.id)",
                [[a, len(rs), sum(r[1] is not None for r in rs), sum(r[3] is not None for r in rs),
                  len({r[4] for r in rs if r[4] is not None})] for a, rs in groups.items()],
                "Counts preserve null-extended left-row multiplicity; DISTINCT non-NULL endpoints only.")

    # Re-aggregation, collect/unwind and ordered slicing without relying on list order.
    for threshold in (-1, 0, 1, 2):
        selected = [e for e in edges if by_id[e["dst"]]["age"] is not None and by_id[e["dst"]]["age"] > threshold]
        prefix = f"MATCH (a:P)-[r:R]->(b:P) WHERE b.age > {threshold}"
        add(f"pipeline/unwind/{threshold}", "collect_unwind_relationships",
            prefix + " WITH a.id AS aid,collect(r.eid) AS xs UNWIND xs AS x RETURN aid,x",
            [[e["src"], e["eid"]] for e in selected], "COLLECT and UNWIND retain each relationship property occurrence.")
        groups = defaultdict(list)
        for e in selected:
            groups[e["src"]].append(e)
        add(f"pipeline/regroup/{threshold}", "nested_aggregation", prefix +
            " WITH a.id AS aid,count(r.eid) AS k WITH k,count(*) AS groups RETURN k,groups",
            [[k, sum(len(es) == k for es in groups.values())] for k in sorted({len(es) for es in groups.values()})],
            "Aggregation over explicitly read relationship IDs, then regrouping by count.")
        add(f"pipeline/distinct-unwind/{threshold}", "distinct_collect_unwind", prefix +
            " WITH a.id AS aid,collect(DISTINCT b.id) AS xs UNWIND xs AS bid RETURN aid,bid",
            [[a, b] for a, b in sorted({(e["src"], e["dst"]) for e in selected})],
            "Only endpoint duplication is removed by DISTINCT within each source group.")
        add(f"pipeline/case/{threshold}", "case_three_valued_projection", "MATCH (a:P) RETURN a.id," +
            f"CASE WHEN a.age > {threshold} THEN 1 WHEN NOT (a.age > {threshold}) THEN 0 ELSE -1 END",
            [[n["id"], -1 if n["age"] is None else int(n["age"] > threshold)] for n in nodes],
            "CASE admits only True WHEN branches; the unknown case reaches ELSE.")
        for skip, limit in ((0, 1), (1, 3), (4, 2)):
            sorted_edges = sorted(selected, key=lambda e: (e["src"], e["eid"]))[skip:skip+limit]
            add(f"pipeline/ordered/{threshold}/{skip}-{limit}", "ordered_relationship_slice", prefix +
                f" WITH a.id AS aid,r.eid AS eid ORDER BY aid,eid SKIP {skip} LIMIT {limit} RETURN aid,eid",
                [[e["src"], e["eid"]] for e in sorted_edges], "Unique relationship IDs give a deterministic total order before slicing.")
    assert len({c["id"] for c in out}) == len(out)
    return out
