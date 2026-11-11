"""Alternative single fixtures with the same property schema as semantic_suite.fixture.

Each shape is one deterministic graph for one seed; switching shape is not a
multi-seed design. Properties keep the schema (id, age, score, name, flag) so
every existing reference model and engine setup applies unchanged.
"""
from __future__ import annotations
import random


def dense(seed):
    """30 nodes: long chain, out/in hub, parallel edges, parallel/loop+incoming, cycles."""
    rng = random.Random(seed)
    ages = [None, 0, 1, -1, 2, -2, None, 3, 3, -3, 0, 7, -7, None, 1, 1, 5, -5, 10, -10,
            None, 4, 4, 2, -2, 6, None, 0, 9, -9]
    names = [None, "n1", "", "n2", "n1", "N1", "n0", "", None, "n2"]
    nodes = []
    for i, a in enumerate(ages):
        nodes.append({"id": i, "age": a, "score": None if a is None else a / 2,
                      "name": names[i % len(names)] if i % 7 else None,
                      "flag": None if i % 5 == 0 else bool((i * 7) % 3 % 2)})
    pairs = [(0, 1), (0, 1), (0, 1), (1, 0),            # triple parallel + reverse
             (2, 2), (2, 2), (3, 3), (4, 3),            # parallel self-loops; loop + incoming
             (5, 6), (6, 7), (7, 5),                    # 3-cycle
             (8, 9), (9, 8),                            # 2-cycle
             (10, 11), (11, 12), (12, 13), (13, 14), (14, 15), (15, 16)]  # chain
    pairs += [(20, t) for t in (21, 22, 23, 24, 25)]    # out-hub
    pairs += [(s, 26) for s in (21, 22, 23, 27)]        # in-hub
    pairs += [(rng.randrange(30), rng.randrange(30)) for _ in range(12)]
    return {"seed": seed, "shape": "dense30", "nodes": nodes,
            "edges": [{"eid": i, "src": s, "dst": t, "weight": (i % 7) - 3} for i, (s, t) in enumerate(pairs)]}


SHAPES = {"dense30": dense}
