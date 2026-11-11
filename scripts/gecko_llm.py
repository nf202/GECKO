"""Prompts, schemas and output sanitation for the two model components.

AI-2 (value mining): one call proposes boundary values per property type.
AI-1 (targeted feature generation): one call per generation proposes closed
ASTs (gecko_ast) aimed at new graph values/structures and uncovered cells.
Neither prompt contains expected results.
"""
from __future__ import annotations
import json
import math
from collections import Counter

from gecko_ast import PROPS, SHAPES, FUNCS

MINING_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["int_values", "float_values", "string_values"],
    "properties": {
        "int_values": {"type": "array", "minItems": 4, "maxItems": 12, "items": {"type": "integer"}},
        "float_values": {"type": "array", "minItems": 4, "maxItems": 12, "items": {"type": "number"}},
        "string_values": {"type": "array", "minItems": 4, "maxItems": 12, "items": {"type": "string"}},
    },
}

GEN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["features"],
    "properties": {"features": {"type": "array", "maxItems": 8, "items": {
        "type": "object", "additionalProperties": False, "required": ["shape", "desc", "expr"],
        "properties": {"shape": {"type": "string", "enum": list(SHAPES)},
                       "desc": {"type": "boolean"},
                       "expr": {"type": "string", "description": "JSON-encoded AST"}}}}},
}


def mining_prompt(engines):
    return (
        "You are helping to test property-graph database engines (" + ", ".join(engines) + ").\n"
        "From the documented semantics of their Cypher functions, operators and value types, propose boundary "
        "VALUES to store as node properties. Good values sit where engines' type conversion, arithmetic, "
        "comparison, ordering, string or NULL handling is likely to differ (e.g., signed zero, values near "
        "rounding boundaries, characters with special case mappings).\n"
        "Constraints: integers with absolute value at most 2^40; finite floats with absolute value at most 1e15; "
        "strings of at most 20 characters, no newline, no quote or backslash. Do not explain; do not predict "
        "any query result. Return JSON only.")


def sanitize_values(out):
    if not isinstance(out, dict):
        return {"int": [], "float": [], "str": []}
    ints = [v for v in out.get("int_values", []) if isinstance(v, int) and not isinstance(v, bool) and abs(v) <= 2 ** 40]
    floats = [float(v) for v in out.get("float_values", [])
              if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and abs(v) <= 1e15]
    strs = [s for s in out.get("string_values", []) if isinstance(s, str) and len(s) <= 20
            and not any(c in s for c in "'\"\\\n\r") and all(ord(c) <= 0xFFFF for c in s)]
    dedup = lambda xs: list(dict.fromkeys(xs))
    return {"int": dedup(ints), "float": dedup(floats), "str": dedup(strs)}


GRAMMAR = """Closed AST grammar (JSON objects). Variable n is the current :P node; inside "nb", m is the neighbour.
  {"p": PROP} property of n; inside "nb" use {"p": PROP, "of": "m"}. PROP in id(int), age(int), score(float), name(string), flag(boolean); any may be NULL.
  {"v": LITERAL} int, float, string (<=40 chars, no quotes) or null
  {"op": "+|-|*|/|%", "a": E, "b": E}            numbers only; integer / and % truncate toward zero
  {"cmp": "=|<>|<|<=|>|>=", "a": E, "b": E}       same type family only
  {"and": [E, E]}, {"or": [E, E]}, {"xor": [E, E]}, {"not": E}, {"isnull": E}, {"notnull": E}
  {"in": E, "list": [LITERAL, ...]}                 at most 8 elements, same type family, null allowed
  {"str": "starts|ends|contains", "a": E, "b": E}
  {"fn": NAME, "args": [E, ...]}  NAME in toInteger, toFloat, abs, sign, floor, ceil (one number); size, toUpper,
        toLower, reverse (one string); left/right(string, int literal 0..20); substring(string, int lit, int lit);
        coalesce(2-3 args, same type); toString(int or boolean)
  {"case": [[COND, VALUE], ...], "else": E or null}   1-4 arms, values of one type
  {"nb": "out|in|both", "agg": "count|sum|min|max", "expr": E over m, "where": E over m or null}
Shapes: projection (one value per node), filter (boolean), group (group by the value, count rows),
  order (ORDER BY value; set desc), total (count/sum/min/max over all nodes).
Rules: at most 40 AST nodes, depth 6. Division by a value that can be zero, toString of floats, and comparisons
across types are rejected. The value must differ between nodes on the given graph."""


def graph_summary(g):
    vals = {p: Counter() for p in ("age", "score", "name", "flag")}
    for n in g["nodes"]:
        for p in vals:
            vals[p][json.dumps(n.get(p))] += 1
    pairs = Counter((e["src"], e["dst"]) for e in g["edges"])
    return {"nodes": len(g["nodes"]), "relationships": len(g["edges"]),
            "self_loops": sum(e["src"] == e["dst"] for e in g["edges"]),
            "parallel_extra": sum(c - 1 for c in pairs.values() if c > 1),
            "property_values": {p: [json.loads(k) for k, _ in c.most_common(12)] for p, c in vals.items()}}


def graph_diff(new, old):
    old_vals = {p: {json.dumps(n.get(p)) for n in old["nodes"]} for p in ("age", "score", "name", "flag")}
    new_vals = {p: sorted({json.dumps(n.get(p)) for n in new["nodes"]} - old_vals[p]) for p in old_vals}
    a, b = graph_summary(new), graph_summary(old)
    return {"new_property_values": {p: [json.loads(v) for v in vs] for p, vs in new_vals.items() if vs},
            "structure_change": {k: [b[k], a[k]] for k in ("nodes", "relationships", "self_loops", "parallel_extra")
                                 if a[k] != b[k]}}


def generation_prompt(g, diff, symptom_names, target_cells, recent_queries, rejected=None, v2=False):
    extra = ""
    if v2:
        extra += ("Also allowed: boolean ORDER BY keys and min/max over booleans; substring past the end; "
                  "integer overflow or integer division by zero when it happens on every row outside any "
                  "AND/OR/CASE/coalesce (the expected outcome is then an engine error).\n")
    if rejected:
        extra += ("Some of your previous proposals were rejected by the checker; fix them if possible "
                  "(reason: AST): " + json.dumps(rejected[:6]) + "\n")
    return (
        "You generate test features for differential testing of Cypher graph databases (Neo4j, Memgraph, FalkorDB).\n"
        "Graph: (:P {id, age, score, name, flag}) nodes and [:R] relationships; parallel relationships and "
        "self-loops exist.\n\n" + GRAMMAR + "\n\n"
        "Current graph summary: " + json.dumps(graph_summary(g)) + "\n"
        "Values and structures new in this generation: " + json.dumps(diff) + "\n"
        "Symptom families already seen (do not target these again): " + json.dumps(sorted(symptom_names)) + "\n"
        "Feature cells (shape:types) to target: " + json.dumps(sorted(target_cells)[:12]) + "\n"
        "Recently generated queries (avoid repeating): " + json.dumps(recent_queries[-12:]) + "\n" + extra + "\n"
        "Propose up to 8 features that combine the new values or structures with functions, operators, "
        "three-valued logic, neighbour aggregation or ordering in ways where engines could disagree. "
        "Return each AST as a JSON-encoded string in \"expr\". Do not predict results.")


def parse_features(out, start_index):
    feats, errors = [], []
    for k, item in enumerate((out or {}).get("features", [])):
        try:
            expr = json.loads(item["expr"])
            feats.append({"fid": f"{start_index + k:04d}", "shape": item["shape"], "desc": bool(item.get("desc")),
                          "expr": expr})
        except (KeyError, TypeError, json.JSONDecodeError) as err:
            errors.append(f"{start_index + k:04d}: unparsable expr ({type(err).__name__})")
    return feats, errors
