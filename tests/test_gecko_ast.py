"""Hand-computed checks for the closed feature grammar and its reference."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from gecko_ast import admit, query, check, Invalid

G = {"nodes": [{"id": 0, "age": None, "score": -1.5, "name": "é", "flag": True},
               {"id": 1, "age": 3, "score": -0.5, "name": "ab", "flag": False},
               {"id": 2, "age": -7, "score": 2.5, "name": None, "flag": None}],
     "edges": [{"eid": 0, "src": 0, "dst": 1, "weight": 0}, {"eid": 1, "src": 0, "dst": 1, "weight": 0},
               {"eid": 2, "src": 1, "dst": 1, "weight": 0}]}


def feat(shape, expr, **kw):
    return {"fid": "t", "shape": shape, "expr": expr, **kw}


class GeckoAstTest(unittest.TestCase):
    def test_truncating_conversion(self):
        case, why = admit(feat("projection", {"fn": "toInteger", "args": [{"p": "score"}]}), G)
        self.assertIsNone(why)
        self.assertEqual(case["expected_rows"], [[0, -1], [1, 0], [2, 2]])
        self.assertEqual(case["query"], "MATCH (n:P) RETURN n.id AS c0, toInteger(n.score) AS c1")

    def test_integer_division_and_modulo(self):
        rows = admit(feat("projection", {"op": "/", "a": {"p": "age"}, "b": {"v": 2}}), G)[0]["expected_rows"]
        self.assertEqual(rows, [[0, None], [1, 1], [2, -3]])
        rows = admit(feat("projection", {"op": "%", "a": {"p": "age"}, "b": {"v": 2}}), G)[0]["expected_rows"]
        self.assertEqual(rows, [[0, None], [1, 1], [2, -1]])

    def test_three_valued_filter_and_in(self):
        case, _ = admit(feat("filter", {"in": {"p": "age"}, "list": [3, None]}), G)
        self.assertEqual(case["expected_rows"], [[1]])

    def test_neighbor_aggregate_counts_parallel_and_loops(self):
        case, _ = admit(feat("projection", {"nb": "both", "agg": "count"}), G)
        # node 0: two out-edges; node 1: two in-edges + self-loop once; node 2: none
        self.assertEqual(case["expected_rows"], [[0, 2], [1, 3], [2, 0]])
        self.assertIn("size([(n)-[:R]-(m) | 1])", case["query"])

    def test_order_desc_nulls_first(self):
        case, _ = admit(feat("order", {"p": "age"}, desc=True), G)
        self.assertEqual(case["expected_rows"], [[[0, 1, 2]]])

    def test_rejections(self):
        self.assertEqual(admit(feat("projection", {"op": "/", "a": {"v": 1}, "b": {"p": "age"}}),
                               {"nodes": [{"id": 0, "age": 0}, {"id": 1, "age": 1}], "edges": []})[1],
                         "outside contract: division by zero")
        self.assertTrue(admit(feat("projection", {"v": 1}), G)[1].startswith("not discriminative"))
        with self.assertRaises(Invalid):
            check(feat("projection", {"op": "+", "a": {"p": "name"}, "b": {"v": 1}}))
        with self.assertRaises(Invalid):
            check(feat("projection", {"fn": "toString", "args": [{"p": "score"}]}))


if __name__ == "__main__":
    unittest.main()
