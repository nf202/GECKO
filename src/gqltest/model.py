from dataclasses import dataclass, asdict
import random


@dataclass(frozen=True)
class Feature:
    name: str
    predicate: str
    property_type: str
    provenance: str = "manual"


@dataclass(frozen=True)
class Context:
    name: str
    match: str
    projection: str = "a.age AS value"


# All predicates are deterministic, Boolean-valued, and reference a bound a.
# These are hand-authored bootstrap inputs, NOT LLM-synthesized outputs.
FEATURES = (
    Feature("integer_compare", "a.age > 0", "INT64"),
    Feature("integer_modulo", "a.age % 3 = 0", "INT64"),
    Feature("absolute", "abs(a.age) > 2", "INT64"),
    Feature("coalesce", "coalesce(a.age, 0) >= 0", "INT64"),
    Feature("null_test", "a.age IS NULL", "INT64"),
    Feature("boolean_property", "a.flag", "BOOL"),
    Feature("conjunction", "a.age > 0 AND a.flag", "INT64+BOOL"),
    Feature("disjunction", "a.age < 0 OR a.flag", "INT64+BOOL"),
    Feature("string_prefix", "a.name STARTS WITH 'n'", "STRING"),
    Feature("string_contains", "a.name CONTAINS '2'", "STRING"),
    Feature("lowercase", "toLower(a.name) = 'n2'", "STRING"),
    Feature("list_membership", "a.age IN [0, 1, 2]", "INT64"),
    Feature("numeric_floor", "floor(a.score) > 0", "DOUBLE"),
    Feature("string_size", "size(a.name) > 1", "STRING"),
)

CONTEXTS = (
    Context("node", "MATCH (a:P)"),
    Context("edge", "MATCH (a:P)-[r:R]->(b:P)"),
    Context("two_edge", "MATCH (a:P)-[r:R]->(b:P)-[s:R]->(c:P)"),
    Context("undirected", "MATCH (a:P)-[r:R]-(b:P)"),
    Context("bounded_path", "MATCH (a:P)-[:R*1..3]->(b:P)"),
)


def queries(feature, context):
    """Bag partition at MATCH-WHERE; deliberately excludes other clauses."""
    stem, projection, p = context.match, context.projection, feature.predicate
    return [
        f"{stem} RETURN {projection}",
        f"{stem} WHERE ({p}) RETURN {projection}",
        f"{stem} WHERE NOT ({p}) RETURN {projection}",
        f"{stem} WHERE ({p}) IS NULL RETURN {projection}",
    ]


def graph(seed, nodes=24):
    if nodes < 8:
        raise ValueError("At least eight nodes are needed for graph witnesses")
    rng = random.Random(seed)
    people = []
    for i in range(nodes):
        people.append({"id": i, "age": None if i % 5 == 0 else rng.randint(-4, 4),
                       "score": None if i % 7 == 0 else rng.randint(-8, 8) / 2,
                       "name": None if i % 6 == 0 else f"n{i % 4}",
                       "flag": None if i % 4 == 0 else bool(i % 2)})
    # Cycle, parallel edges, self loop, branch; last two nodes stay isolated.
    pairs = [(0, 1), (0, 1), (1, 2), (2, 0), (2, 2), (1, 3), (3, 4)]
    pairs += [(rng.randrange(nodes - 2), rng.randrange(nodes - 2))
              for _ in range(nodes)]
    edges = [{"eid": j, "src": a, "dst": b, "weight": j % 5}
             for j, (a, b) in enumerate(pairs)]
    return {"seed": seed, "nodes": people, "edges": edges}


def catalog():
    return {"features": [asdict(f) for f in FEATURES],
            "contexts": [asdict(c) for c in CONTEXTS],
            "origin": "manual bootstrap; no LLM calls"}
