"""Independent finite witness evaluator for the admitted node/one-edge subset.

This is a validation control, not a complete graph-language interpreter.
"""
import math


def tri_and(a, b):
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def tri_or(a, b):
    if a is True or b is True:
        return True
    if a is None or b is None:
        return None
    return False


def evaluate(name, node):
    age, flag, text, score = (node[k] for k in ("age", "flag", "name", "score"))
    positive = None if age is None else age > 0
    if name == "integer_compare":
        return positive
    if name == "integer_modulo":
        # Only equality to zero; sign differences for negative remainder do not matter.
        return None if age is None else age % 3 == 0
    if name == "absolute":
        return None if age is None else abs(age) > 2
    if name == "coalesce":
        return (0 if age is None else age) >= 0
    if name == "null_test":
        return age is None
    if name == "boolean_property":
        return flag
    if name == "conjunction":
        return tri_and(positive, flag)
    if name == "disjunction":
        return tri_or(None if age is None else age < 0, flag)
    if name == "string_prefix":
        return None if text is None else text.startswith("n")
    if name == "string_contains":
        return None if text is None else "2" in text
    if name == "lowercase":
        return None if text is None else text.lower() == "n2"
    if name == "list_membership":
        return None if age is None else age in (0, 1, 2)
    if name == "numeric_floor":
        return None if score is None else math.floor(score) > 0
    if name == "string_size":
        return None if text is None else len(text) > 1
    raise ValueError(f"No reference rule for {name}")


def expected(g, feature_name, context_name):
    if context_name == "node":
        bindings = g["nodes"]
    elif context_name == "edge":
        by_id = {n["id"]: n for n in g["nodes"]}
        bindings = [by_id[e["src"]] for e in g["edges"]]
    else:
        return None
    rows = [[], [], [], []]
    for node in bindings:
        row = [node["age"]]
        rows[0].append(row)
        outcome = evaluate(feature_name, node)
        index = 3 if outcome is None else 1 if outcome else 2
        rows[index].append(row)
    return rows
