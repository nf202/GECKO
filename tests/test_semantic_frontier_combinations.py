"""Independent truth-table and OPTIONAL multiplicity witnesses for the reference."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import semantic_frontier_combinations as ref


class ThreeValuedReferenceTests(unittest.TestCase):
    def test_complete_boolean_truth_tables(self):
        vals = [True, False, None]
        self.assertEqual([ref.truth_not(x) for x in vals], [False, True, None])
        self.assertEqual([[ref.truth_and(a, b) for b in vals] for a in vals],
                         [[True, False, None], [False, False, False], [None, False, None]])
        self.assertEqual([[ref.truth_or(a, b) for b in vals] for a in vals],
                         [[True, True, True], [True, False, None], [True, None, None]])

    def test_membership_null_and_empty_list(self):
        def member(x, ys):
            return ref.value(["in", ["lit", x], ["lit", ys]], {}, {})
        self.assertIs(member(None, []), False)
        self.assertIs(member(None, [1]), None)
        self.assertIs(member(1, [None, 1]), True)
        self.assertIs(member(2, [None, 1]), None)
        self.assertIs(member(2, [1]), False)

    def test_optional_local_and_post_filter_are_distinct(self):
        graph = {"nodes": [{"id": 0, "age": 0, "flag": None},
                           {"id": 1, "age": None, "flag": False},
                           {"id": 2, "age": 3, "flag": True}],
                 "edges": [{"eid": 0, "src": 0, "dst": 2},
                           {"eid": 1, "src": 0, "dst": 2}]}
        cases = {c["id"]: c for c in ref.cases(graph, 20261102)}
        # Predicate 0 is a.age > -2: filtering belongs to different operators.
        self.assertEqual(cases["composed/000/optional-local"]["expected_rows"],
                         [[0, 0, 2], [0, 1, 2], [1, None, None], [2, None, None]])
        self.assertEqual(cases["composed/000/optional-post"]["expected_rows"],
                         [[0, 0, 2], [0, 1, 2], [2, None, None]])
        self.assertEqual(cases["composed/000/optional-count"]["expected_rows"],
                         [[0, 2], [1, 0], [2, 0]])
        self.assertEqual(len(cases), 448)


if __name__ == "__main__":
    unittest.main()
