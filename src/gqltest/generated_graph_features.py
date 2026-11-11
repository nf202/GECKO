"""Unexecuted closed bounded-path feature prototype.

Cypher compilation and finite evaluation use separate algorithms. No model
output, arbitrary query text or engine result supplies reference values.
Direction is outgoing/incoming only; undirected orientation is unadmitted.
"""
from .oracle import bag

DIALECT = "cypher-clause-independent-v1"
PROPERTIES = {"id": "int", "age": "int", "name": "string", "flag": "bool"}
MAX_DEPTH = 8
MAX_AST_NODES = 64
MAX_GRAPH_NODES = 16
MAX_GRAPH_EDGES = 32
COMPARISONS = ("=", "<>", "<", "<=", ">", ">=")


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        raise ValueError("closed AST fields")


def _integer(value):
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise ValueError("signed int64 required")


def _string(value):
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("bounded string required")
    if any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError("control/surrogate characters outside literal contract")


def _literal_type(value):
    if value is None:
        return "null"
    if type(value) is bool:
        return "bool"
    if type(value) is int:
        _integer(value)
        return "int"
    if isinstance(value, str):
        _string(value)
        return "string"
    raise ValueError("only null/bool/int64/string literals")


def _type(ast, depth=0, count=None):
    if count is None:
        count = [0]
    count[0] += 1
    if depth > MAX_DEPTH or count[0] > MAX_AST_NODES:
        raise ValueError("predicate depth/node budget")
    if not isinstance(ast, dict) or "op" not in ast:
        raise ValueError("predicate AST object")
    op = ast["op"]
    if op == "literal":
        _fields(ast, ("op", "value"))
        return _literal_type(ast["value"])
    if op == "property":
        _fields(ast, ("op", "endpoint", "key"))
        if ast["endpoint"] not in ("start", "end") or not isinstance(ast["key"], str) or ast["key"] not in PROPERTIES:
            raise ValueError("declared endpoint property required")
        return PROPERTIES[ast["key"]]
    if op == "path_length":
        _fields(ast, ("op",))
        return "int"
    if op == "compare":
        _fields(ast, ("op", "operator", "left", "right"))
        if ast["operator"] not in COMPARISONS:
            raise ValueError("comparison operator")
        a = _type(ast["left"], depth + 1, count)
        b = _type(ast["right"], depth + 1, count)
        if a != b and "null" not in (a, b):
            raise ValueError("cross-type comparison")
        nonnull = {x for x in (a, b) if x != "null"}
        if ast["operator"] not in ("=", "<>") and nonnull - {"int"}:
            raise ValueError("ordering restricted to nullable integers")
        return "bool"
    if op in ("and", "or"):
        _fields(ast, ("op", "args"))
        if not isinstance(ast["args"], list) or not 2 <= len(ast["args"]) <= 4:
            raise ValueError("Boolean arity 2..4")
        if any(_type(x, depth + 1, count) not in ("bool", "null") for x in ast["args"]):
            raise ValueError("Boolean operands")
        return "bool"
    if op in ("not", "is_null"):
        _fields(ast, ("op", "arg"))
        typ = _type(ast["arg"], depth + 1, count)
        if op == "not" and typ not in ("bool", "null"):
            raise ValueError("Boolean NOT operand")
        return "bool"
    raise ValueError("unadmitted operation")


def validate_context(context):
    _fields(context, ("direction", "min_length", "max_length", "reuse"))
    if context["direction"] not in ("out", "in"):
        raise ValueError("directed outgoing/incoming only")
    if context["reuse"] not in ("walk", "edge_simple"):
        raise ValueError("explicit relationship-reuse contract")
    if type(context["min_length"]) is not int or type(context["max_length"]) is not int:
        raise ValueError("integer length bounds")
    if not 0 <= context["min_length"] <= context["max_length"] <= 3:
        raise ValueError("length bounds must lie in 0..3")
    return context


def validate_feature(feature):
    _fields(feature, ("context", "predicate"))
    validate_context(feature["context"])
    if _type(feature["predicate"]) not in ("bool", "null"):
        raise ValueError("nullable Boolean predicate required")
    return feature


def validate_graph(graph):
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
        raise ValueError("finite nodes/edges graph")
    if len(graph["nodes"]) > MAX_GRAPH_NODES or len(graph["edges"]) > MAX_GRAPH_EDGES:
        raise ValueError("finite graph budget")
    nodes = {}
    for node in graph["nodes"]:
        if not isinstance(node, dict) or "id" not in node:
            raise ValueError("node id")
        _integer(node["id"])
        if node["id"] in nodes:
            raise ValueError("unique node ids required")
        for prop, typ in PROPERTIES.items():
            value = node.get(prop)
            if value is not None and _literal_type(value) != typ:
                raise ValueError("typed declared property")
        nodes[node["id"]] = node
    seen = set()
    for edge in graph["edges"]:
        if not isinstance(edge, dict) or not {"eid", "src", "dst"} <= set(edge):
            raise ValueError("edge identity/endpoints")
        for key in ("eid", "src", "dst"):
            _integer(edge[key])
        if edge["eid"] in seen or edge["src"] not in nodes or edge["dst"] not in nodes:
            raise ValueError("unique edge ids and existing endpoints required")
        seen.add(edge["eid"])
    return nodes


def _quote(value):
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _compile_predicate(ast, length):
    op = ast["op"]
    if op == "literal":
        return _quote(ast["value"])
    if op == "property":
        return ("a" if ast["endpoint"] == "start" else "b") + "." + ast["key"]
    if op == "path_length":
        return str(length)
    if op == "compare":
        return "(" + _compile_predicate(ast["left"], length) + " " + ast["operator"] + " " + _compile_predicate(ast["right"], length) + ")"
    if op in ("and", "or"):
        return "(" + (" " + op.upper() + " ").join(_compile_predicate(x, length) for x in ast["args"]) + ")"
    if op == "not":
        return "(NOT (" + _compile_predicate(ast["arg"], length) + "))"
    if op == "is_null":
        return "(" + _compile_predicate(ast["arg"], length) + " IS NULL)"
    raise ValueError("unadmitted compiler operation")


def _branch(feature, length, partition):
    context = feature["context"]
    if length == 0:
        stem = "MATCH (a:P) WITH a, a AS b"
        edges = "[]"
        constraints = []
    else:
        vertices = ["a"] + ["v" + str(i) for i in range(1, length)] + ["b"]
        def link(i):
            middle = "-[r" + str(i) + ":R]->" if context["direction"] == "out" else "<-[r" + str(i) + ":R]-"
            return middle + "(" + vertices[i + 1] + ":P)"
        if context["reuse"] == "walk":
            stem = " ".join("MATCH (" + vertices[i] + ":P)" + link(i) for i in range(length))
            constraints = []
        else:
            stem = "MATCH (a:P)" + "".join(link(i) for i in range(length))
            constraints = ["r" + str(i) + ".eid <> r" + str(j) + ".eid" for i in range(length) for j in range(i + 1, length)]
        edges = "[" + ", ".join("r" + str(i) + ".eid" for i in range(length)) + "]"
    pred = _compile_predicate(feature["predicate"], length)
    if partition == "true":
        constraints.append("(" + pred + ")")
    elif partition == "false":
        constraints.append("NOT (" + pred + ")")
    elif partition == "null":
        constraints.append("(" + pred + ") IS NULL")
    if constraints:
        stem += " WHERE " + " AND ".join("(" + x + ")" for x in constraints)
    return stem + " RETURN a.id AS start_id, b.id AS end_id, " + edges + " AS edge_ids, " + str(length) + " AS path_length"


def compile_queries(feature, dialect=DIALECT):
    """Return [base,true,false,null] read queries; live dialect validation pending.

    Walk reuse requires relationship uniqueness to reset across MATCH clauses.
    Edge-simple compilation additionally compares unique non-null eid properties.
    No native variable-length default is assumed. UNION ALL preserves bindings.
    """
    validate_feature(feature)
    if dialect != DIALECT:
        raise ValueError("unvalidated Cypher dialect contract")
    context = feature["context"]
    return [" UNION ALL ".join(_branch(feature, n, part) for n in range(context["min_length"], context["max_length"] + 1))
            for part in ("base", "true", "false", "null")]


def bindings(graph, context):
    """Independent directed adjacency DFS, admitting repeated nodes in both modes."""
    validate_context(context)
    nodes = validate_graph(graph)
    adjacent = {node_id: [] for node_id in nodes}
    for edge in graph["edges"]:
        src, dst = ((edge["src"], edge["dst"]) if context["direction"] == "out" else (edge["dst"], edge["src"]))
        adjacent[src].append((edge["eid"], dst))
    result = []
    def extend(start, current, edges):
        length = len(edges)
        if length >= context["min_length"]:
            result.append({"start": start, "end": current, "edge_ids": list(edges), "path_length": length})
        if length == context["max_length"]:
            return
        for edge_id, nxt in adjacent[current]:
            if context["reuse"] == "walk" or edge_id not in edges:
                extend(start, nxt, edges + (edge_id,))
    for start in nodes:
        extend(start, start, ())
    return result


def _evaluate(ast, binding, nodes):
    op = ast["op"]
    if op == "literal":
        return ast["value"]
    if op == "property":
        return nodes[binding[ast["endpoint"]]].get(ast["key"])
    if op == "path_length":
        return binding["path_length"]
    if op == "is_null":
        return _evaluate(ast["arg"], binding, nodes) is None
    if op == "not":
        value = _evaluate(ast["arg"], binding, nodes)
        return None if value is None else not value
    if op in ("and", "or"):
        values = [_evaluate(x, binding, nodes) for x in ast["args"]]
        if op == "and":
            if any(x is False for x in values):
                return False
            return None if any(x is None for x in values) else True
        if any(x is True for x in values):
            return True
        return None if any(x is None for x in values) else False
    if op == "compare":
        left = _evaluate(ast["left"], binding, nodes)
        right = _evaluate(ast["right"], binding, nodes)
        if left is None or right is None:
            return None
        operator = ast["operator"]
        if operator == "=":
            return left == right
        if operator == "<>":
            return left != right
        if operator == "<":
            return left < right
        if operator == "<=":
            return left <= right
        if operator == ">":
            return left > right
        if operator == ">=":
            return left >= right
    raise ValueError("unadmitted evaluator operation")


def expected_partitions(graph, feature):
    """Independent finite row lists [base,true,false,null], retaining all paths."""
    validate_feature(feature)
    nodes = validate_graph(graph)
    rows = [[], [], [], []]
    for binding in bindings(graph, feature["context"]):
        row = [binding["start"], binding["end"], binding["edge_ids"], binding["path_length"]]
        value = _evaluate(feature["predicate"], binding, nodes)
        if value is not None and type(value) is not bool:
            raise ValueError("non-Boolean finite result")
        rows[0].append(row)
        rows[3 if value is None else 1 if value is True else 2].append(row)
    return rows


def expected_rowbags(graph, feature):
    return [bag(rows) for rows in expected_partitions(graph, feature)]
