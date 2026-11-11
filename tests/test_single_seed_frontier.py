"""Regression checks for the query-token boundary that invalidated pilot UNIONs."""
import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/campaign_single_seed_frontier.py"
SPEC = importlib.util.spec_from_file_location("single_seed_frontier", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AliasRegressionTests(unittest.TestCase):
    def test_union_all_keeps_separate_keyword(self):
        query = MODULE.alias_returns("MATCH (a:P) RETURN a.id AS id UNION ALL MATCH (a:P) RETURN a.id AS id")
        self.assertIn("AS c0 UNION ALL MATCH", query)
        self.assertNotIn("c0UNION", query)

    def test_union_distinct_keeps_separate_keyword(self):
        query = MODULE.alias_returns("MATCH (a:P) RETURN a.age UNION MATCH (a:P) RETURN a.age")
        self.assertIn("AS c0 UNION MATCH", query)

    def test_function_commas_do_not_change_column_count(self):
        query = MODULE.alias_returns("MATCH (a:P) RETURN a.id,coalesce(a.age,0),[a.age,0,NULL]")
        self.assertIn("coalesce(a.age,0) AS c1", query)
        self.assertIn("[a.age,0,NULL] AS c2", query)

    def test_nullable_parallel_edge_witness(self):
        graph = {"seed": 20261102, "nodes": [
            {"id": 0, "age": None, "score": None, "name": None, "flag": None},
            {"id": 1, "age": 1, "score": .5, "name": "one", "flag": True}],
            "edges": [{"eid": 0, "src": 0, "dst": 1, "weight": 1},
                      {"eid": 1, "src": 0, "dst": 1, "weight": 1},
                      {"eid": 2, "src": 1, "dst": 1, "weight": 1}]}
        cases = {c["id"]: c for c in MODULE.catalog(graph)}
        self.assertEqual(cases["twohop-count/out"]["expected_rows"], [[2]])
        self.assertEqual(MODULE.bag(cases["optional-count/0"]["expected_rows"]), MODULE.bag([[0, 2], [1, 1]]))


if __name__ == "__main__":
    unittest.main()
