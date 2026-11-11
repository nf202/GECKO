"""Invariant checks for the evolving-graph operators and signal labelling."""
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from gqltest.semantic_suite import fixture
from graph_evolution import OPERATORS, LIMITS, apply, check, degrees, mutate


class GraphEvolutionTest(unittest.TestCase):
    def test_every_operator_keeps_invariants(self):
        for op in OPERATORS:
            rng = random.Random(7)
            g = fixture(20261102)
            for _ in range(20):
                apply(op, g, rng)
                check(g)
                self.assertLessEqual(max(degrees(g).values()), LIMITS["degree"], op)

    def test_mutation_is_deterministic_and_does_not_touch_parent(self):
        parent = fixture(20261102)
        before = repr(parent)
        w = {o: 1.0 for o in OPERATORS}
        a = mutate(parent, random.Random(1), w, 4)
        b = mutate(parent, random.Random(1), w, 4)
        self.assertEqual(repr(a), repr(b))
        self.assertEqual(repr(parent), before)

    def test_limits_hold_over_long_trajectory(self):
        rng, g = random.Random(3), fixture(20261102)
        w = {o: 1.0 for o in OPERATORS}
        w["add_chain"] = w["add_node"] = 20.0
        for _ in range(80):
            g, _ = mutate(g, rng, w, 4)
        self.assertLessEqual(len(g["nodes"]), LIMITS["nodes"])
        self.assertLessEqual(len(g["edges"]), LIMITS["edges"])

    def test_reference_suspect_when_all_engines_agree(self):
        from campaign_evolving_graph import classify
        case = {"id": "x", "family": "f", "query": "MATCH (n) RETURN 1 AS c0", "expected_rows": [[2]]}
        row = {"id": "x", "match": False, "stable": True, "runs": [{"status": "ok", "rows": [[1]]}]}
        labels = classify({"a": {"x": row}, "b": {"x": row}}, [case])
        self.assertEqual(set(labels.values()), {"reference_suspect"})
        one = classify({"a": {"x": row}, "b": {"x": {**row, "match": True}}}, [case])
        self.assertEqual(one[("a", "x")], "unexplained")


if __name__ == "__main__":
    unittest.main()
