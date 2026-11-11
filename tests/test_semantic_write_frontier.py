"""Hand-computed checks for the write-semantics reference model."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from gqltest.semantic_suite import fixture
from semantic_write_frontier import cases, validate


class WriteFrontierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = fixture(20261102)
        cls.cases = {c["id"]: c for c in cases(cls.g)}

    def state(self, key):
        return self.cases["writes/" + key]["expected_state"]

    def test_power_checks(self):
        report = validate(list(self.cases.values()), self.g)
        self.assertEqual(report["cases"], 93)
        self.assertEqual(report["atomic_with_prior_write"], 52)

    def test_halloween_counts(self):
        self.assertEqual(len(self.state("w1/create-per-node")["nodes"]), 24)
        self.assertEqual(len(self.state("w1/copy-edges")["edges"]), 36)
        # eids 0, 1 and 3 are matched in both directions; self-loop 2 once.
        self.assertEqual(len(self.state("w1/undirected-copy")["edges"]), 25)

    def test_merge_visibility(self):
        self.assertEqual(len(self.state("w1/merge-unwind-duplicates")["nodes"]), 14)
        self.assertEqual(sorted(n[0] for n in self.state("w1/merge-by-property")["nodes"][12:]), [-4, -2, -1])
        # MERGE of an unbound pattern duplicates the existing id 1.
        self.assertEqual(sum(n[0] == 1 for n in self.state("w1/merge-full-pattern")["nodes"]), 2)

    def test_delete(self):
        self.assertEqual(len(self.state("w1/undirected-double-delete")["edges"]), 16)
        self.assertEqual(len(self.state("w1/detach-delete-two")["edges"]), 11)
        self.assertEqual(sorted(e[1] for e in self.state("w1/detach-delete-neighbors")["edges"]),
                         [6, 7, 10, 11, 12, 14, 15, 16, 17])

    def test_atomicity_cases_expect_fixture(self):
        w6 = [c for c in self.cases.values() if c["expected_error"]]
        base = self.cases["writes/w6/set/div/k1"]["expected_state"]
        self.assertTrue(all(c["expected_state"] == base for c in w6))
        partial = self.cases["writes/w6/set/div/k6"]["partial_state_before_error"]
        self.assertEqual(sorted(n[0] for n in partial["nodes"] if n[5] == 1), [0, 1, 2, 3, 4, 5])

    def test_index_probe_reference(self):
        shift = {p["name"]: p["expected_rows"] for p in self.cases["writes/w7/shift"]["probes"]}
        # Ages 1 -> 11 stay inside (1, 11]; 2, 4 and 5 move to 12, 14 and 15.
        self.assertEqual(sorted(shift["range"]), [[2], [9]])
        relabel = {p["name"]: p["expected_rows"] for p in self.cases["writes/w7/relabel"]["probes"]}
        self.assertEqual(relabel["eq5"], [])


class StrictWriteFrontierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from semantic_write_frontier import strict_cases
        cls.g = fixture(20261102)
        cls.cases = {c["id"]: c for c in strict_cases(cls.g)}

    def test_power_checks(self):
        from semantic_write_frontier import validate_strict
        report = validate_strict(list(self.cases.values()), self.g)
        self.assertEqual((report["runtime_error_cases"], report["controls"], report["transactions"]), (63, 21, 25))

    def test_truncating_division(self):
        marks = dict((n[0], n[5]) for n in self.cases["writes-strict/w6b/divprop/set/control"]["expected_state"]["nodes"])
        # 100 / (-4 - 99) and 100 / (-2 - 99) truncate to 0; 100 / (0 - 99) to -1; NULL age stays NULL.
        self.assertEqual((marks[8], marks[5], marks[1], marks[0]), (0, 0, -1, None))
        partial = self.cases["writes-strict/w6b/divprop/set/k7"]["partial_state_before_error"]
        self.assertEqual(dict((n[0], n[5]) for n in partial["nodes"])[1], -25)

    def test_transactions(self):
        self.assertEqual(len(self.cases["writes-strict/t3/create+detach-delete"]["expected_state"]["nodes"]), 11)
        t2 = self.cases["writes-strict/t2/shift"]["transaction"]["steps"]
        self.assertEqual([s["expect"] for s in t2], ["ok", "error"])


if __name__ == "__main__":
    unittest.main()
