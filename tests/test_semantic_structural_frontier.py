"""Hand-counted multigraph witnesses for path and scope references."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import semantic_structural_frontier as ref


class StructuralReferenceTests(unittest.TestCase):
    def setUp(self):
        self.g = {"nodes": [{"id": 0, "age": None}, {"id": 1, "age": 1}],
                  "edges": [{"eid": 0, "src": 0, "dst": 1},
                            {"eid": 1, "src": 0, "dst": 1},
                            {"eid": 2, "src": 1, "dst": 1}]}

    def test_zero_length_includes_each_node_once(self):
        self.assertEqual(ref.paths(self.g, "out", 0, 0), [[0, 0, [], 0], [1, 1, [], 0]])

    def test_parallel_paths_and_relationship_reuse(self):
        self.assertEqual(ref.paths(self.g, "out", 2, 2), [[0, 1, [0, 2], 2], [0, 1, [1, 2], 2]])
        self.assertEqual(len(ref.paths(self.g, "out", 2, 2, reuse=True)), 3)
        self.assertEqual(len(ref.paths(self.g, "undirected", 1, 1)), 5)
        self.assertEqual(len(ref.paths(self.g, "undirected", 2, 2)), 8)

    def test_optional_separate_clause_keeps_self_loop_reuse(self):
        cases = {c["id"]: c for c in ref.cases(self.g)}
        self.assertEqual(cases["structural/optional-chain/plain/0/rows"]["expected_rows"],
                         [[0, 0, 1, 2, 1], [0, 1, 1, 2, 1], [1, 2, 1, 2, 1]])
        self.assertEqual(cases["structural/optional-chain/distinct-rel/0/rows"]["expected_rows"],
                         [[0, 0, 1, 2, 1], [0, 1, 1, 2, 1], [1, 2, 1, None, None]])


if __name__ == "__main__":
    unittest.main()
