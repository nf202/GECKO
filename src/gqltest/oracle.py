from collections import Counter
import json
import math


def canonical(value):
    # Typed values prevent Python conflating True, 1 and 1.0.
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("Non-finite floats have no admitted comparison rule")
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["string", value]
    if isinstance(value, (list, tuple)):
        return ["list", [canonical(x) for x in value]]
    raise ValueError(f"Unadmitted result type: {type(value).__name__}")


def bag(rows):
    return Counter(json.dumps(canonical(row), separators=(",", ":"),
                              ensure_ascii=False) for row in rows)


def partition_holds(base, positive, negative, nulls):
    return bag(base) == bag(positive) + bag(negative) + bag(nulls)


def tri_not(value):
    if value not in (True, False, None):
        raise ValueError("Predicate must have a three-valued Boolean result")
    return None if value is None else not value
