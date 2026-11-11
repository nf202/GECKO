"""Hand witnesses and exhaustive finite logic checks for the new list oracle."""
from itertools import product
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import semantic_list_frontier as ref


class ListReferenceTests(unittest.TestCase):
    def test_quantifiers_match_all_boolean_completions(self):
        # A NULL predicate denotes an unknown Boolean. A result is determined
        # precisely when all possible Boolean completions agree.
        for size in range(5):
            for values in product((False, True, None), repeat=size):
                completions = list(product(*[(False, True) if x is None else (x,) for x in values]))
                for kind in ("any", "all", "none"):
                    answers = {(any(xs) if kind == "any" else all(xs) if kind == "all" else not any(xs))
                               for xs in completions}
                    self.assertIs(ref.quantified(kind, values), next(iter(answers)) if len(answers) == 1 else None)

    def test_null_and_empty_inputs_and_fold(self):
        self.assertEqual([ref.quantified(k, []) for k in ("any", "all", "none")], [False, True, True])
        self.assertEqual([ref.quantified(k, None) for k in ("any", "all", "none")], [None, None, None])
        self.assertIsNone(ref.fold_sum([1, None, 2]))
        self.assertEqual(ref.fold_sum([1, None, 2], True), 3)
        self.assertEqual(ref.fold_sum([]), 0)
        self.assertIsNone(ref.fold_sum(None, True))

    def setUp(self):
        self.g = {"nodes": [{"id": 0, "age": None}, {"id": 1, "age": 2}, {"id": 2, "age": -1}],
                  "edges": [{"eid": 2, "src": 0, "dst": 1}, {"eid": 0, "src": 0, "dst": 2},
                            {"eid": 1, "src": 0, "dst": 1}, {"eid": 3, "src": 1, "dst": 0}]}

    def test_collect_null_dropping_multiplicity_and_filter_scope(self):
        self.assertEqual(ref.optional_groups(self.g, "plain", 0), [[0, [-1, 2, 2]], [1, []], [2, []]])
        self.assertEqual(ref.optional_groups(self.g, "target-local", 0), [[0, [2, 2]], [1, []], [2, []]])
        self.assertEqual(ref.optional_groups(self.g, "source-local", 0), [[0, []], [1, []], [2, []]])
        self.assertEqual(ref.optional_groups(self.g, "post", 0), [[0, [2, 2]]])

    def test_hand_counted_query_expectations(self):
        cases = {c["id"]: c for c in ref.cases(self.g)}
        def rows(key):
            return cases["lists/" + key]["expected_rows"]
        self.assertEqual(rows("index/mixed/-1"), [[0, None], [1, 2], [2, -1]])
        self.assertEqual(rows("slice/mixed/-3--1"), [[0, [1, None]], [1, [1, None]], [2, [1, None]]])
        self.assertEqual(rows("quantifier/singleton/any/gt-0/projection"), [[0, None], [1, True], [2, False]])
        self.assertEqual(rows("quantifier/null/all/is-null/count"), [[0]])
        self.assertEqual(rows("optional/plain/-1/unwind"), [[0, -1], [0, 2], [0, 2]])
        self.assertEqual(rows("optional/plain/-1/reduce"), [[0, 3, 3], [1, 0, 0], [2, 0, 0]])


if __name__ == "__main__":
    unittest.main()
