"""Gremlin profile reference: the semantics pinned by scripts/probe_gremlin_semantics.py."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from gecko_gremlin import gadmit, greference, gquery, load_script, seed_features
from gremlin_engines import read_only
from gecko_ast import Uncertain

# The probe graph: 0 has a self-loop and an edge to 1; 1 lacks age and flag; 2 lacks name and flag.
G = {"nodes": [{"id": 0, "age": 5, "score": 2.5, "name": "ab", "flag": True},
               {"id": 1, "age": None, "score": -0.5, "name": "Straße", "flag": None},
               {"id": 2, "age": -3, "score": 0.0, "name": None, "flag": None}],
     "edges": [{"src": 0, "dst": 0}, {"src": 0, "dst": 1}, {"src": 1, "dst": 2}]}


def ref(shape, expr, desc=False):
    return greference({"fid": "t", "shape": shape, "desc": desc, "expr": expr}, G)[0]


def test_double_arithmetic_and_nothing():
    assert ref("projection", {"op": "/", "a": {"p": "age"}, "b": {"v": 2}}) == [[0, 2.5], [1], [2, -1.5]]
    assert ref("projection", {"op": "%", "a": {"p": "age"}, "b": {"v": 2}}) == [[0, 1.0], [1], [2, -1.0]]


def test_two_valued_predicates():
    assert ref("filter", {"not": {"cmp": ">", "a": {"p": "age"}, "b": {"v": 0}}}) == [[1], [2]]
    assert ref("filter", {"cmp": "<>", "a": {"p": "age"}, "b": {"v": 5}}) == [[2]]


def test_self_loop_twice_and_empty_reductions():
    assert ref("projection", {"nb": "both", "agg": "count", "expr": {"p": "id", "of": "m"}, "where": None}) \
        == [[0, 3], [1, 2], [2, 1]]
    assert ref("projection", {"nb": "out", "agg": "sum", "expr": {"p": "age", "of": "m"}, "where": None}) \
        == [[0, 5], [1, -3], [2]]


def test_order_and_group_drop_nothing():
    assert ref("order", {"p": "age"}) == [[[2, 0]]]
    assert ref("group", {"p": "age"}) == [[5, 1], [-3, 1]]


def test_trunc_signed_zero_and_toupper():
    assert ref("projection", {"fn": "toInteger", "args": [{"p": "score"}]})[1][1] == 0.0
    assert ref("projection", {"fn": "toUpper", "args": [{"p": "name"}]})[1] == [1, "STRASSE"]


def test_total_over_nothing():
    where = {"cmp": ">", "a": {"p": "age", "of": "m"}, "b": {"v": 100}}
    assert ref("total", {"nb": "out", "agg": "min", "expr": {"p": "age", "of": "m"}, "where": where}) == [[0]]


def test_rejections():
    f = {"fid": "t", "shape": "projection", "expr": {"fn": "coalesce", "args": [{"p": "age"}, {"v": None}]}}
    assert gadmit(f, G)[1].startswith("invalid")
    g2 = {"nodes": G["nodes"] + [{"id": 3, "score": -0.0}], "edges": []}
    case, _ = gadmit({"fid": "t", "shape": "group", "expr": {"p": "score"}}, g2)
    assert sorted(case["expected_rows"]) == [[-0.5, 1], [0.0, 2], [2.5, 1]] and case["zero_sensitive"]
    ceil = {"fn": "ceil", "args": [{"p": "score"}]}
    assert str(ref("projection", ceil)[1][1]) == "-0.0"


def test_queries_are_read_only_and_load_builds_graph():
    for f in seed_features():
        assert read_only(gquery(f)), f
    assert not read_only("g.V().drop()") and read_only("g.V().has('name', 'a;drop')")
    s = load_script(G)
    assert s.count("addV") == 3 and s.count("addE") == 3 and "property('age'" in s.splitlines()[1]
    assert "property('age'" not in s.splitlines()[2]


def test_signed_zero_dependence_is_recorded():
    g2 = {"nodes": G["nodes"] + [{"id": 3, "score": -0.0}], "edges": []}
    case, _ = gadmit({"fid": "t", "shape": "filter", "expr": {"in": {"p": "score"}, "list": [0, 0.5]}}, g2)
    assert case["expected_rows"] == [[2], [3]] and case["zero_sensitive"]
    case, _ = gadmit({"fid": "t", "shape": "filter", "expr": {"cmp": ">", "a": {"p": "age"}, "b": {"v": 0}}}, g2)
    assert not case["zero_sensitive"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("OK")
