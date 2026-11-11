"""Write-semantics templates with an independent Python graph reference.

Each template is applied to a freshly reset copy of the single fixture. The
reference mutates an explicit node/edge model; it does not call any database.
Families:
  W1  read/write visibility inside one statement (Halloween, MERGE, DELETE)
  W6  statement atomicity: a runtime error after earlier rows already wrote
  W7  index/scan consistency of follow-up reads after a write
Templates whose semantics are not fixed by the declared contract are marked
contract_uncertain and are reported separately from reference differences.
"""
from __future__ import annotations

import copy

NODE_KEYS = ("id", "age", "score", "name", "flag", "mark")
NODE_STATE = ("MATCH (n) RETURN n.id AS c0, n.age AS c1, n.score AS c2, n.name AS c3, "
              "n.flag AS c4, n.mark AS c5, labels(n) AS c6")
EDGE_STATE = ("MATCH (a)-[r]->(b) RETURN type(r) AS c0, r.eid AS c1, a.id AS c2, b.id AS c3, "
              "r.weight AS c4, r.mark AS c5")


class Model:
    """Property graph with node identity independent of the id property."""

    def __init__(self, g):
        self.nodes = [{**{k: n.get(k) for k in NODE_KEYS}, "labels": ["P"]} for n in g["nodes"]]
        by_id = {n["id"]: n for n in self.nodes}
        self.edges = [{"type": "R", "eid": e["eid"], "a": by_id[e["src"]], "b": by_id[e["dst"]],
                       "weight": e["weight"], "mark": None} for e in g["edges"]]

    def clone(self):
        return copy.deepcopy(self)

    def P(self):
        return [n for n in self.nodes if "P" in n["labels"]]

    def node(self, i):
        found = [n for n in self.P() if n["id"] == i]
        assert len(found) == 1, i
        return found[0]

    def new_node(self, label="P", **props):
        n = {**{k: None for k in NODE_KEYS}, **props, "labels": [label]}
        self.nodes.append(n)
        return n

    def new_edge(self, a, b, type_="R", **props):
        e = {"type": type_, "eid": None, "weight": None, "mark": None, **props, "a": a, "b": b}
        self.edges.append(e)
        return e

    def delete_edge(self, e):
        if any(x is e for x in self.edges):
            self.edges = [x for x in self.edges if x is not e]

    def detach_delete(self, n):
        self.edges = [e for e in self.edges if e["a"] is not n and e["b"] is not n]
        self.nodes = [x for x in self.nodes if x is not n]

    def undirected(self, pred=lambda e: True):
        """(a, r, b) rows of (a)-[r:R]-(b); a self-loop is matched once."""
        rows = []
        for e in self.edges:
            if e["type"] != "R" or not pred(e):
                continue
            rows.append((e["a"], e, e["b"]))
            if e["a"] is not e["b"]:
                rows.append((e["b"], e, e["a"]))
        return rows

    def state(self):
        return {"nodes": [[n[k] for k in NODE_KEYS] + [sorted(n["labels"])] for n in self.nodes],
                "edges": [[e["type"], e["eid"], e["a"]["id"], e["b"]["id"], e["weight"], e["mark"]]
                          for e in self.edges]}


def _gt(x, y):
    return None if x is None or y is None else x > y


def _lt(x, y):
    return None if x is None or y is None else x < y


# W7 probes on :P(age); the reference evaluates them over the post-write model.
PROBES = {
    "eq5": ("MATCH (n:P) WHERE n.age = 5 RETURN n.id AS c0",
            lambda m: [[n["id"]] for n in m.P() if n["age"] == 5]),
    "range": ("MATCH (n:P) WHERE n.age > 1 AND n.age <= 11 RETURN n.id AS c0",
              lambda m: [[n["id"]] for n in m.P() if n["age"] is not None and 1 < n["age"] <= 11]),
    "lt2": ("MATCH (n:P) WHERE n.age < 2 RETURN n.id AS c0",
            lambda m: [[n["id"]] for n in m.P() if _lt(n["age"], 2)]),
    "in": ("MATCH (n:P) WHERE n.age IN [1, 4, 7, 11, 105] RETURN n.id AS c0",
           lambda m: [[n["id"]] for n in m.P() if n["age"] in (1, 4, 7, 11, 105)]),
    "isnull": ("MATCH (n:P) WHERE n.age IS NULL RETURN n.id AS c0",
               lambda m: [[n["id"]] for n in m.P() if n["age"] is None]),
    "top3": ("MATCH (n:P) WHERE n.age IS NOT NULL RETURN n.id AS c0, n.age AS c1 "
             "ORDER BY n.age DESC, n.id LIMIT 3",
             lambda m: [[n["id"], n["age"]] for n in sorted(
                 (n for n in m.P() if n["age"] is not None), key=lambda n: (-n["age"], n["id"]))[:3]]),
}


# W7 writes shared by the per-statement and explicit-transaction batches.
def w_shift(m):
    for n in [n for n in m.P() if n["age"] is not None and n["age"] >= 1]:
        n["age"] += 10


def w_neg(m):
    for n in [n for n in m.P() if _lt(n["age"], 0)]:
        n["age"] = -n["age"]


def w_remove_even(m):
    for n in [n for n in m.P() if n["age"] is not None and n["age"] % 2 == 0]:
        n["age"] = None


def w_null_flag(m):
    for n in [n for n in m.P() if n["flag"] is True]:
        n["age"] = None


def w_create(m):
    for i, a in ((100, 3), (101, 3), (102, 5), (103, -4)):
        m.new_node(id=i, age=a)


def w_delete(m):
    for n in [n for n in m.P() if n["age"] in (1, 5)]:
        m.detach_delete(n)


def w_replace(m):
    for i in (0, 6, 8):
        n = m.node(i)
        for k in NODE_KEYS:
            n[k] = None
        n["id"], n["age"] = i, 7


def w_fill_null(m):
    for n in [n for n in m.P() if n["age"] is None]:
        n["age"], n["mark"] = 3, 1


def w_relabel(m):
    for n in [n for n in m.P() if _gt(n["age"], 1)]:
        n["labels"] = ["Q"]


def w_merge(m):
    for x in (5, 6, 5):
        found = [n for n in m.P() if n["id"] == 200 + x]
        if found:
            found[0]["age"] += 100
        else:
            m.new_node(id=200 + x, age=x)


W7_WRITES = [
    ("shift", "MATCH (n:P) WHERE n.age >= 1 SET n.age = n.age + 10", w_shift),
    ("negate", "MATCH (n:P) WHERE n.age < 0 SET n.age = -n.age", w_neg),
    ("remove-even", "MATCH (n:P) WHERE n.age % 2 = 0 REMOVE n.age", w_remove_even),
    ("set-null", "MATCH (n:P) WHERE n.flag = true SET n.age = NULL", w_null_flag),
    ("create", "UNWIND [[100, 3], [101, 3], [102, 5], [103, -4]] AS r CREATE (:P {id: r[0], age: r[1]})", w_create),
    ("detach-delete", "MATCH (n:P) WHERE n.age IN [1, 5] DETACH DELETE n", w_delete),
    ("replace-map", "MATCH (n:P) WHERE n.id IN [0, 6, 8] SET n = {id: n.id, age: 7}", w_replace),
    ("fill-null", "MATCH (n:P) WHERE n.age IS NULL SET n += {age: 3, mark: 1}", w_fill_null),
    ("relabel", "MATCH (n:P) WHERE n.age > 1 REMOVE n:P SET n:Q", w_relabel),
    ("merge-on-match", "UNWIND [5, 6, 5] AS x MERGE (n:P {id: 200 + x}) "
                       "ON CREATE SET n.age = x ON MATCH SET n.age = n.age + 100", w_merge),
]


def changed_probes(before_model, after_model):
    """Probes whose reference answer differs between the two models."""
    probes = []
    for pname, (pq, pref) in PROBES.items():
        before, after = pref(before_model), pref(after_model)
        if sorted(map(repr, before)) != sorted(map(repr, after)):
            probes.append({"name": pname, "query": pq, "expected_rows": after})
    return probes


def trigger(kind, var, k):
    """Runtime error exactly when var = k; earlier rows evaluate normally."""
    if kind == "div":
        return f"100 / ({var} - {k})"
    return f"CASE WHEN {var} = {k} THEN ({var} > 0) + 1 ELSE 1 END"


def cases(g):
    base = Model(g)
    out = []

    def add(key, family, query, mutate, contract, uncertain=False, alternatives=None, rows=True):
        m = base.clone()
        result = mutate(m)
        case = {"id": "writes/" + key, "family": family, "query": query,
                "expected_error": False, "expected_state": m.state(),
                "expected_rows": result if rows else None,
                "contract": contract, "contract_uncertain": uncertain}
        if alternatives:
            alt = {}
            for name, fn in alternatives.items():
                a = base.clone()
                fn(a)
                alt[name] = a.state()
            case["alternative_states"] = alt
        out.append(case)

    # ---------------- W1 visibility ----------------
    def v_create_each(m):
        for n in list(m.P()):
            m.new_node(id=n["id"] + 100, mark=1)
        return []
    add("w1/create-per-node", "w1_halloween_create",
        "MATCH (n:P) CREATE (:P {id: n.id + 100, mark: 1})", v_create_each,
        "MATCH rows are enumerated before newly created nodes become matchable.")

    def v_create_pos(m):
        for n in [n for n in m.P() if _gt(n["age"], 0)]:
            m.new_node(id=n["id"] + 100, age=n["age"])
        return []
    add("w1/create-matching-label", "w1_halloween_create",
        "MATCH (n:P) WHERE n.age > 0 CREATE (:P {id: n.id + 100, age: n.age})", v_create_pos,
        "Created nodes satisfy the MATCH predicate but must not be re-matched.")

    def v_copy_edges(m):
        for e in [e for e in m.edges if e["type"] == "R"]:
            m.new_edge(e["a"], e["b"], eid=e["eid"] + 100, weight=e["weight"])
        return []
    add("w1/copy-edges", "w1_halloween_create",
        "MATCH (a:P)-[r:R]->(b:P) CREATE (a)-[:R {eid: r.eid + 100, weight: r.weight}]->(b)",
        v_copy_edges, "Each of the 18 original relationships is copied exactly once.")

    def v_reverse_edges(m):
        for e in [e for e in m.edges if e["type"] == "R"]:
            m.new_edge(e["b"], e["a"], eid=e["eid"] + 100)
        return []
    add("w1/reverse-edges", "w1_halloween_create",
        "MATCH (a:P)-[r:R]->(b:P) CREATE (b)-[:R {eid: r.eid + 100}]->(a)",
        v_reverse_edges, "Reversed copies are not re-matched by the same MATCH.")

    def v_undirected_copy(m):
        for a, r, b in m.undirected(lambda e: e["eid"] is not None and e["eid"] < 4):
            m.new_edge(a, b, eid=100 + r["eid"] * 10 + a["id"])
        return []
    add("w1/undirected-copy", "w1_halloween_create",
        "MATCH (a:P)-[r:R]-(b:P) WHERE r.eid < 4 CREATE (a)-[:R {eid: 100 + r.eid * 10 + a.id}]->(b)",
        v_undirected_copy, "Undirected match yields two rows per non-loop edge and one per self-loop.")

    def v_merge_unwind(m):
        for x in (1, 1, 2):
            if not any(n["id"] == 100 + x for n in m.P()):
                m.new_node(id=100 + x)
        return []
    add("w1/merge-unwind-duplicates", "w1_merge_visibility",
        "UNWIND [1, 1, 2] AS x MERGE (:P {id: 100 + x})", v_merge_unwind,
        "MERGE observes nodes created by earlier rows of the same statement.")

    def v_merge_rel_unwind(m):
        a = m.node(1)
        for x in (1, 1, 2, 2, 1):
            b = m.node(x + 1)
            if not any(e["a"] is a and e["b"] is b and e["eid"] == 200 + x for e in m.edges):
                m.new_edge(a, b, eid=200 + x)
        return []
    add("w1/merge-rel-unwind", "w1_merge_visibility",
        "UNWIND [1, 1, 2, 2, 1] AS x MATCH (a:P {id: 1}), (b:P {id: x + 1}) MERGE (a)-[:R {eid: 200 + x}]->(b)",
        v_merge_rel_unwind, "Relationship MERGE observes relationships created by earlier rows.")

    def v_merge_by_age(m):
        for n in [n for n in m.P() if n["age"] is not None]:
            if not any(x["id"] == n["age"] for x in m.P()):
                m.new_node(id=n["age"])
        return []
    add("w1/merge-by-property", "w1_merge_visibility",
        "MATCH (n:P) WHERE n.age IS NOT NULL MERGE (m:P {id: n.age})", v_merge_by_age,
        "Ages without a node of that id create one node each, deduplicated across rows.")

    def v_set_then_count(m):
        for n in m.P():
            n["mark"] = 1
        return [[12, 12]]
    add("w1/set-aggregate-barrier", "w1_clause_visibility",
        "MATCH (n:P) SET n.mark = 1 WITH count(n) AS c MATCH (m:P) WHERE m.mark = 1 RETURN c AS c0, count(m) AS c1",
        v_set_then_count, "Aggregation completes the SET clause before the following MATCH.")

    def v_delete_then_count(m):
        for e in [e for e in m.edges if e["a"]["id"] == 0 and e["type"] == "R"]:
            m.delete_edge(e)
        return [[2, 0]]
    add("w1/delete-aggregate-barrier", "w1_clause_visibility",
        "MATCH (a:P)-[r:R]->(b:P) WHERE a.id = 0 DELETE r WITH count(*) AS c "
        "OPTIONAL MATCH (x:P {id: 0})-[s:R]->() RETURN c AS c0, count(s) AS c1",
        v_delete_then_count, "Deleted relationships are invisible after an aggregation barrier.")

    def v_double_delete(m):
        for _, r, _ in m.undirected(lambda e: e["eid"] in (3, 8)):
            m.delete_edge(r)
        return []
    add("w1/undirected-double-delete", "w1_delete",
        "MATCH (a:P)-[r:R]-(b:P) WHERE r.eid IN [3, 8] DELETE r", v_double_delete,
        "Each relationship is matched twice; the second DELETE of the same relationship is a no-op.")

    def v_cross_delete(m):
        targets = [m.edges[i] for i, e in enumerate(m.edges) if e["eid"] in (3, 8)]
        for e in targets:
            m.delete_edge(e)
        return []
    add("w1/cross-double-delete", "w1_delete",
        "MATCH (a:P)-[r:R]->(b:P) WHERE r.eid IN [3, 8] MATCH (b)-[s:R]->(c:P) WHERE s.eid IN [3, 8] DELETE r, s",
        v_cross_delete, "Both rows delete both relationships; repeated deletes in one statement are allowed.")

    def v_detach_two(m):
        for i in (1, 2):
            m.detach_delete(m.node(i))
        return []
    add("w1/detach-delete-two", "w1_delete",
        "MATCH (n:P) WHERE n.id IN [1, 2] DETACH DELETE n", v_detach_two,
        "Removes the nodes and every incident relationship, including the self-loop.")

    def v_detach_neighbors(m):
        center = m.node(1)
        doomed = {id(b): b for a, r, b in m.undirected() if a is center}
        doomed[id(center)] = center
        for n in doomed.values():
            m.detach_delete(n)
        return []
    add("w1/detach-delete-neighbors", "w1_delete",
        "MATCH (n:P)-[r:R]-(m:P) WHERE n.id = 1 DETACH DELETE n, m", v_detach_neighbors,
        "Repeated DETACH DELETE of the same node across rows is a no-op.")

    def v_foreach(m):
        for n in [n for n in m.P() if _gt(n["age"], 0)]:
            for i in range(1, n["age"] + 1):
                m.new_node(id=1000 + n["id"] * 10 + i)
        return []
    add("w1/foreach-create", "w1_foreach",
        "MATCH (n:P) WHERE n.age > 0 FOREACH (i IN range(1, n.age) | CREATE (:P {id: 1000 + n.id * 10 + i}))",
        v_foreach, "FOREACH creates one node per element of range(1, age).")

    def v_double_decrement(m):
        for n in [n for n in m.P() if _gt(n["age"], 0)]:
            n["age"] -= 1
            if n["age"] > 0:
                n["age"] -= 1
        return []
    add("w1/set-then-filter", "w1_clause_visibility",
        "MATCH (n:P) WHERE n.age > 0 SET n.age = n.age - 1 WITH n WHERE n.age > 0 SET n.age = n.age - 1",
        v_double_decrement, "A later clause on the same row reads the value written by an earlier clause.")

    def v_equal_age_pairs(m):
        ps = m.P()
        for a in ps:
            for b in ps:
                if a["id"] < b["id"] and a["age"] is not None and a["age"] == b["age"]:
                    m.new_edge(a, b, "S", eid=500 + a["id"] * 12 + b["id"])
        return []
    add("w1/pair-create", "w1_halloween_create",
        "MATCH (a:P), (b:P) WHERE a.id < b.id AND a.age = b.age CREATE (a)-[:S {eid: 500 + a.id * 12 + b.id}]->(b)",
        v_equal_age_pairs, "Cartesian MATCH then CREATE of a new relationship type.")

    def shift_snapshot(m):
        old = {n["id"]: n["age"] for n in m.P()}
        for n in m.P():
            if n["id"] - 1 in old:
                n["age"] = old[n["id"] - 1]

    def shift_sequential(m):
        for i in range(1, 12):
            m.node(i)["age"] = m.node(i - 1)["age"]

    def v_shift(m):
        shift_snapshot(m)
        return []
    add("w1/chained-neighbor-set", "w1_clause_visibility",
        "MATCH (n:P) WITH n ORDER BY n.id MATCH (m:P) WHERE m.id = n.id + 1 SET m.age = n.age",
        v_shift, "Whether n.age reads values written for earlier rows is not fixed by the declared contract.",
        uncertain=True, alternatives={"snapshot": shift_snapshot, "sequential": shift_sequential}, rows=False)

    def v_mark_then_match(m):
        for n in m.P():
            n["mark"] = n["id"]
        return [[i, i - 1] for i in range(1, 12)]
    add("w1/set-then-match-others", "w1_clause_visibility",
        "MATCH (n:P) SET n.mark = n.id WITH n MATCH (m:P) WHERE m.mark = n.id - 1 RETURN n.id AS c0, m.id AS c1",
        v_mark_then_match, "Clause-complete reference; pipelined engines may legitimately differ.",
        uncertain=True)

    def v_create_then_match(m):
        for i in (0, 1, 2):
            m.new_node(id=100 + i)
        return [[100 + i, 3] for i in (0, 1, 2)]
    add("w1/create-then-match-new", "w1_clause_visibility",
        "MATCH (n:P) WHERE n.id < 3 CREATE (m:P {id: n.id + 100}) WITH m MATCH (k:P) WHERE k.id >= 100 "
        "RETURN m.id AS c0, count(k) AS c1",
        v_create_then_match, "Clause-complete reference; pipelined engines may legitimately differ.",
        uncertain=True)

    def v_merge_full_pattern(m):
        a = m.new_node(id=1)
        b = m.new_node(id=600)
        m.new_edge(a, b, eid=600)
        return []
    add("w1/merge-full-pattern", "w1_merge_pattern",
        "MERGE (a:P {id: 1})-[:R {eid: 600}]->(b:P {id: 600})", v_merge_full_pattern,
        "An unbound MERGE pattern that does not exist as a whole creates every element, duplicating id 1.")

    def v_merge_bound_start(m):
        m.new_edge(m.node(1), m.new_node(id=2), eid=601)
        return []
    add("w1/merge-bound-start", "w1_merge_pattern",
        "MATCH (a:P {id: 1}) MERGE (a)-[:R {eid: 601}]->(b:P {id: 2})", v_merge_bound_start,
        "Only the bound node is reused; the unbound end node is created.")

    def v_merge_existing_rel(m):
        a, b = m.node(1), m.node(2)
        for e in m.edges:
            if e["a"] is a and e["b"] is b and e["type"] == "R":
                e["mark"] = 2
        return []
    add("w1/merge-on-match", "w1_merge_pattern",
        "MATCH (a:P {id: 1}), (b:P {id: 2}) MERGE (a)-[r:R]->(b) ON CREATE SET r.mark = 1 ON MATCH SET r.mark = 2",
        v_merge_existing_rel, "An existing relationship takes the ON MATCH branch.")

    def undirected_ab(m):
        m.new_edge(m.node(2), m.node(4), eid=700)

    def undirected_ba(m):
        m.new_edge(m.node(4), m.node(2), eid=700)

    def v_merge_undirected(m):
        undirected_ab(m)
        return []
    add("w1/merge-undirected-create", "w1_merge_pattern",
        "MATCH (a:P {id: 2}), (b:P {id: 4}) MERGE (a)-[r:R]-(b) ON CREATE SET r.eid = 700",
        v_merge_undirected, "Created direction of an undirected MERGE is implementation-chosen.",
        uncertain=True, alternatives={"a_to_b": undirected_ab, "b_to_a": undirected_ba})

    def v_map_merge(m):
        for n in m.P():
            n["mark"], n["age"] = n["id"], None
        return []
    add("w1/map-merge-null", "w1_set_map",
        "MATCH (n:P) SET n += {mark: n.id, age: NULL}", v_map_merge,
        "A NULL value in += removes that property.")

    def v_map_replace(m):
        for i in (3, 5):
            n = m.node(i)
            for k in NODE_KEYS:
                n[k] = None
            n["id"], n["mark"] = i, 1
        return []
    add("w1/map-replace", "w1_set_map",
        "MATCH (n:P) WHERE n.id IN [3, 5] SET n = {id: n.id, mark: 1}", v_map_replace,
        "SET n = map replaces all properties.")

    def v_remove_two(m):
        for n in [n for n in m.P() if n["flag"] is False]:
            n["age"] = n["score"] = None
        return []
    add("w1/remove-two", "w1_set_map",
        "MATCH (n:P) WHERE n.flag = false REMOVE n.age, n.score", v_remove_two,
        "REMOVE of two properties on the selected rows.")

    def v_optional_null_set(m):
        m.node(1)["mark"] = 1
        return []
    add("w1/optional-null-set", "w1_set_map",
        "OPTIONAL MATCH (n:P {id: 99}) SET n.mark = 1 WITH count(*) AS c MATCH (m:P {id: 1}) SET m.mark = c",
        v_optional_null_set, "SET on a NULL binding is ignored; the NULL row still counts once.")

    def v_shift_ids(m):
        a, b = m.node(7), m.node(8)
        a["id"], b["id"] = 8, 9
        return []
    add("w1/shift-ids", "w1_set_map",
        "MATCH (n:P) WHERE n.id IN [7, 8] SET n.id = n.id + 1", v_shift_ids,
        "Updating the filtered property does not re-qualify rows.")

    def v_top3(m):
        for n in sorted((n for n in m.P() if n["age"] is not None), key=lambda n: (-n["age"], n["id"]))[:3]:
            n["mark"] = 1
        return []
    add("w1/ordered-limit-set", "w1_set_map",
        "MATCH (n:P) WHERE n.age IS NOT NULL WITH n ORDER BY n.age DESC, n.id LIMIT 3 SET n.mark = 1",
        v_top3, "Only the top three rows are written.")

    def v_group_mark(m):
        groups = {}
        for n in m.P():
            s = None if n["age"] is None else (n["age"] > 0) - (n["age"] < 0)
            groups.setdefault(s, []).append(n)
        for ns in groups.values():
            for n in ns:
                n["mark"] = len(ns)
        return []
    add("w1/group-collect-unwind-set", "w1_set_map",
        "MATCH (n:P) WITH sign(n.age) AS s, collect(n) AS ns UNWIND ns AS x SET x.mark = size(ns)",
        v_group_mark, "Grouped collection then per-member write; NULL forms one group.")

    # ---------------- W6 atomicity ----------------
    ordered_nodes = sorted(base.P(), key=lambda n: n["id"])

    def node_op(fn):
        def run(m, k):
            for n0 in ordered_nodes:
                if n0["id"] == k:
                    return
                fn(m, m.node(n0["id"]))
        return run

    def eid_delete(m, k):
        for eid in sorted(e["eid"] for e in base.edges):
            if eid == k:
                return
            m.delete_edge(next(e for e in m.edges if e["eid"] == eid))

    def node_delete(m, k):
        for n0 in ordered_nodes:
            if n0["id"] == k:
                return
            m.detach_delete(m.node(n0["id"]))

    atomic_ops = [
        ("set", "MATCH (n:P) WITH n ORDER BY n.id SET n.mark = {t}", "n.id",
         node_op(lambda m, n: n.__setitem__("mark", 1)), (1, 6, 11)),
        ("create-node", "MATCH (n:P) WITH n ORDER BY n.id CREATE (:P {{id: 100 + n.id, mark: {t}}})", "n.id",
         node_op(lambda m, n: m.new_node(id=100 + n["id"])), (1, 6, 11)),
        ("set-two-items", "MATCH (n:P) WITH n ORDER BY n.id SET n.name = NULL, n.mark = {t}", "n.id",
         node_op(lambda m, n: n.__setitem__("name", None) or n.__setitem__("mark", 1)), (1, 6, 11)),
        ("delete-rel", "MATCH (a:P)-[r:R]->(b:P) WITH r, r.eid AS e ORDER BY e DELETE r RETURN {t} AS c0", "e",
         eid_delete, (1, 9, 17)),
        ("detach-delete", "MATCH (n:P) WITH n, n.id AS i ORDER BY i DETACH DELETE n RETURN {t} AS c0", "i",
         node_delete, (1, 6, 11)),
        ("create-rel", "MATCH (n:P) WITH n ORDER BY n.id CREATE (n)-[:R {{eid: 300 + n.id, mark: {t}}}]->(n)", "n.id",
         node_op(lambda m, n: m.new_edge(n, n, eid=300 + n["id"])), (1, 6, 11)),
        ("merge-node", "MATCH (n:P) WITH n ORDER BY n.id MERGE (m:P {{id: 400 + n.id}}) SET m.mark = {t}", "n.id",
         node_op(lambda m, n: m.new_node(id=400 + n["id"])), (1, 6, 11)),
        ("set-label", "MATCH (n:P) WITH n ORDER BY n.id SET n:Q SET n.mark = {t}", "n.id",
         node_op(lambda m, n: n["labels"].append("Q")), (1, 6, 11)),
    ]
    fixture_state = base.state()
    for kind in ("div", "type"):
        for name, template, var, partial_fn, ks in atomic_ops:
            for k in ks:
                partial = base.clone()
                partial_fn(partial, k)
                out.append({"id": f"writes/w6/{name}/{kind}/k{k}", "family": "w6_atomicity_" + name.replace("-", "_"),
                            "query": template.format(t=trigger(kind, var, k)),
                            "expected_error": True, "expected_state": fixture_state, "expected_rows": None,
                            "partial_state_before_error": partial.state(),
                            "contract": "An auto-commit statement that raises a runtime error leaves no effects.",
                            "contract_uncertain": False})
        for name, query, cvar in (
                ("after-aggregate", "MATCH (n:P) SET n.mark = 1 WITH count(*) AS c RETURN {t} AS c0", "c"),
                ("after-foreach", "MATCH (n:P) WITH collect(n) AS ns FOREACH (x IN ns | SET x.mark = 1) "
                                  "WITH size(ns) AS c RETURN {t} AS c0", "c")):
            partial = base.clone()
            for n in partial.P():
                n["mark"] = 1
            out.append({"id": f"writes/w6/{name}/{kind}", "family": "w6_atomicity_" + name.replace("-", "_"),
                        "query": query.format(t=trigger(kind, cvar, 12)),
                        "expected_error": True, "expected_state": fixture_state, "expected_rows": None,
                        "partial_state_before_error": partial.state(),
                        "contract": "An error raised after every write completed still rolls back the statement.",
                        "contract_uncertain": False})

    # ---------------- W7 write then indexed/scan reads ----------------
    for name, query, fn in W7_WRITES:
        m = base.clone()
        fn(m)
        probes = changed_probes(base, m)
        out.append({"id": f"writes/w7/{name}", "family": "w7_index_after_write", "query": query,
                    "expected_error": False, "expected_state": m.state(), "expected_rows": [],
                    "probes": probes,
                    "contract": "Follow-up reads (index or scan) reflect the committed write.",
                    "contract_uncertain": False})
    return out


def validate(case_list, g):
    """Pre-freeze power checks; raises on a template that cannot detect its target."""
    fixture_state = Model(g).state()
    same = lambda a, b: (sorted(map(repr, a["nodes"])), sorted(map(repr, a["edges"]))) == \
                        (sorted(map(repr, b["nodes"])), sorted(map(repr, b["edges"])))
    ids = [c["id"] for c in case_list]
    queries = [c["query"] for c in case_list]
    assert len(set(ids)) == len(ids) and len(set(queries)) == len(queries)
    report = {"cases": len(case_list), "state_changing": 0, "atomic_with_prior_write": 0,
              "index_probes": 0, "contract_uncertain": 0}
    for c in case_list:
        if c["expected_error"]:
            assert not same(c["partial_state_before_error"], fixture_state), c["id"]
            report["atomic_with_prior_write"] += 1
        else:
            assert not same(c["expected_state"], fixture_state), c["id"]
            report["state_changing"] += 1
        if "probes" in c:
            assert len(c["probes"]) >= 2, c["id"]
            report["index_probes"] += len(c["probes"])
        report["contract_uncertain"] += c["contract_uncertain"]
    return report


# ---------------- Strict batch: runtime-only errors and explicit transactions ----------------
# Each trigger reads a property, so no static type check can reject the query;
# the K=99 controls prove the same text executes and writes when nothing fires.
STRICT_K = {"divprop": (3, 7, 10), "flagadd": (1, 7, 10), "absname": (2, 7, 10)}
DIVPROP_C = {3: -1, 7: 4, 10: 5, 99: 99}  # first node in id order whose age equals c is K
CONTROL_K = 99


def strict_trigger(kind, k, v):
    if kind == "divprop":
        return f"100 / ({v['age']} - {DIVPROP_C[k]})"
    if kind == "flagadd":
        return f"CASE WHEN {v['id']} = {k} THEN {v['flag']} + 1 ELSE 1 END"
    return f"CASE WHEN {v['id']} = {k} THEN abs({v['name']}) ELSE 1 END"


def strict_value(kind, n, k):
    """Value written for a non-firing row; Cypher integer division truncates toward zero."""
    if kind != "divprop":
        return 1
    return None if n["age"] is None else int(100 / (n["age"] - DIVPROP_C[k]))


def _set_two(m, n, val):
    n["name"], n["mark"] = None, val


def _set_label(m, n, val):
    n["labels"].append("Q")
    n["mark"] = val


NODE_VARS = {"id": "n.id", "age": "n.age", "flag": "n.flag", "name": "n.name"}
DELETE_VARS = {"id": "i", "age": "a", "flag": "f", "name": "s"}
STRICT_OPS = [
    ("set", "MATCH (n:P) WITH n ORDER BY n.id SET n.mark = {t}", NODE_VARS,
     lambda m, n, val: n.__setitem__("mark", val)),
    ("create-node", "MATCH (n:P) WITH n ORDER BY n.id CREATE (:P {{id: 100 + n.id, mark: {t}}})", NODE_VARS,
     lambda m, n, val: m.new_node(id=100 + n["id"], mark=val)),
    ("set-two-items", "MATCH (n:P) WITH n ORDER BY n.id SET n.name = NULL, n.mark = {t}", NODE_VARS, _set_two),
    ("create-rel", "MATCH (n:P) WITH n ORDER BY n.id CREATE (n)-[:R {{eid: 300 + n.id, mark: {t}}}]->(n)", NODE_VARS,
     lambda m, n, val: m.new_edge(n, n, eid=300 + n["id"], mark=val)),
    ("merge-node", "MATCH (n:P) WITH n ORDER BY n.id MERGE (m:P {{id: 400 + n.id}}) SET m.mark = {t}", NODE_VARS,
     lambda m, n, val: m.new_node(id=400 + n["id"], mark=val)),
    ("set-label", "MATCH (n:P) WITH n ORDER BY n.id SET n:Q SET n.mark = {t}", NODE_VARS, _set_label),
    ("detach-delete", "MATCH (n:P) WITH n, n.id AS i, n.age AS a, n.flag AS f, n.name AS s ORDER BY i "
                      "DETACH DELETE n RETURN {t} AS c0", DELETE_VARS,
     lambda m, n, val: m.detach_delete(n)),
]
TX_FAIL = "MATCH (n:P {id: 1}) RETURN 100 / toInteger(n.score) AS c0"  # node 1 keeps score 0.0 under every write
TX_PAIRS = [("shift", "negate"), ("create", "detach-delete"), ("relabel", "fill-null"),
            ("remove-even", "set-null"), ("replace-map", "merge-on-match")]


def strict_cases(g):
    base = Model(g)
    fixture_state = base.state()
    ordered = sorted(base.P(), key=lambda n: n["id"])
    out = []
    for kind, ks in STRICT_K.items():
        for name, template, v, op in STRICT_OPS:
            family = "w6b_" + kind + "_" + name.replace("-", "_")
            for k in ks:
                partial = base.clone()
                for n0 in ordered:
                    if n0["id"] == k:
                        break
                    op(partial, partial.node(n0["id"]), strict_value(kind, n0, k))
                out.append({"id": f"writes-strict/w6b/{kind}/{name}/k{k}", "family": family,
                            "query": template.format(t=strict_trigger(kind, k, v)),
                            "expected_error": True, "expected_state": fixture_state, "expected_rows": None,
                            "partial_state_before_error": partial.state(),
                            "contract": "A data-dependent runtime error rolls back the whole auto-commit statement.",
                            "contract_uncertain": False})
            full, rows = base.clone(), []
            for n0 in ordered:
                val = strict_value(kind, n0, CONTROL_K)
                op(full, full.node(n0["id"]), val)
                rows.append([val])
            out.append({"id": f"writes-strict/w6b/{kind}/{name}/control", "family": family + "_control",
                        "query": template.format(t=strict_trigger(kind, CONTROL_K, v)),
                        "expected_error": False, "expected_state": full.state(),
                        "expected_rows": rows if name == "detach-delete" else [],
                        "contract": "Same expression with no firing row: executes and applies every write.",
                        "contract_uncertain": False})
    writes = {name: (query, fn) for name, query, fn in W7_WRITES}

    def tx_case(key, family, steps, end, after_model, after_probes, contract):
        return {"id": "writes-strict/" + key, "family": family,
                "query": " ; ".join(s["query"] for s in steps) + " ; " + end.upper(),
                "transaction": {"steps": steps, "end": end}, "expected_error": False,
                "expected_state": after_model.state(), "expected_rows": None,
                "after_probes": after_probes, "contract": contract, "contract_uncertain": False}

    for name, (query, fn) in writes.items():
        written = base.clone()
        fn(written)
        inside = changed_probes(base, written)
        restored = [{"name": p["name"], "query": p["query"], "expected_rows": PROBES[p["name"]][1](base)}
                    for p in inside]
        steps = [{"query": query, "expect": "ok", "expected_rows": []}] + \
                [{"query": p["query"], "expect": "ok", "expected_rows": p["expected_rows"], "probe": p["name"]}
                 for p in inside]
        out.append(tx_case(f"t1/{name}", "t1_uncommitted_read_rollback", steps, "rollback", base, restored,
                           "Reads inside the transaction see its uncommitted write; rollback restores data and index."))
        steps = [{"query": query, "expect": "ok", "expected_rows": []},
                 {"query": TX_FAIL, "expect": "error", "expected_rows": None}]
        out.append(tx_case(f"t2/{name}", "t2_failed_statement_rollback", steps, "rollback", base, restored,
                           "A failed statement ends the transaction; nothing written earlier in it persists."))
    for first, second in TX_PAIRS:
        both = base.clone()
        writes[first][1](both)
        writes[second][1](both)
        probes = changed_probes(base, both)
        steps = [{"query": writes[first][0], "expect": "ok", "expected_rows": []},
                 {"query": writes[second][0], "expect": "ok", "expected_rows": []}] + \
                [{"query": p["query"], "expect": "ok", "expected_rows": p["expected_rows"], "probe": p["name"]}
                 for p in probes]
        out.append(tx_case(f"t3/{first}+{second}", "t3_two_writes_commit", steps, "commit", both, probes,
                           "The second statement sees the first; commit makes both visible to later reads."))
    return out


def validate_strict(case_list, g):
    """Pre-freeze power checks for the strict batch."""
    fixture_state = Model(g).state()
    same = lambda a, b: (sorted(map(repr, a["nodes"])), sorted(map(repr, a["edges"]))) == \
                        (sorted(map(repr, b["nodes"])), sorted(map(repr, b["edges"])))
    ids = [c["id"] for c in case_list]
    assert len(set(ids)) == len(ids) and len({c["query"] for c in case_list}) == len(case_list)
    report = {"cases": len(case_list), "runtime_error_cases": 0, "controls": 0, "transactions": 0,
              "in_transaction_probes": 0}
    for c in case_list:
        if c["expected_error"]:
            assert not same(c["partial_state_before_error"], fixture_state), c["id"]
            report["runtime_error_cases"] += 1
        elif "transaction" in c:
            probes = [s for s in c["transaction"]["steps"] if "probe" in s]
            if c["transaction"]["end"] == "commit":
                assert not same(c["expected_state"], fixture_state), c["id"]
            if not c["family"].startswith("t2"):
                assert len(probes) >= 2, c["id"]
            report["transactions"] += 1
            report["in_transaction_probes"] += len(probes)
        else:
            assert not same(c["expected_state"], fixture_state), c["id"]
            report["controls"] += 1
    return report
