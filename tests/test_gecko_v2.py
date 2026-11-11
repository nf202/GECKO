"""Gecko-FE (V2) contract: boolean orderability, substring past the end, signed-zero ties,
expected-error cases for integer overflow and integer division by zero."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
import gecko_ast
from gecko_ast import admit
from gecko_gremlin import gadmit

G = {"nodes": [{"id": 0, "age": 5, "score": 0.0, "name": "ab", "flag": True},
               {"id": 1, "age": None, "score": -0.0, "name": "c", "flag": False},
               {"id": 2, "age": -3, "score": 1.5, "name": None, "flag": None},
               {"id": 3, "age": 9223372036854775807, "score": 2.0, "name": "x", "flag": False}],
     "edges": []}


def feat(shape, expr, desc=False):
    return {"fid": "t", "shape": shape, "desc": desc, "expr": expr}


def test_v2():
    gecko_ast.V2 = True
    try:
        c, why = admit(feat("order", {"p": "flag"}), G)
        assert c["expected_rows"] == [[[1, 3, 0, 2]]], (c, why)
        c, _ = admit(feat("order", {"p": "flag"}, desc=True), G)
        assert c["expected_rows"] == [[[2, 0, 1, 3]]]
        assert "min(" in admit(feat("total", {"p": "flag"}), G)[0]["query"]
        assert admit(feat("total", {"p": "flag"}), G)[0]["expected_rows"] == [[3, False, True]]
        c, _ = admit(feat("projection", {"fn": "substring", "args": [{"p": "name"}, {"v": 5}, {"v": 2}]}), G)
        assert [r[1] for r in c["expected_rows"]] == ["", "", None, ""]
        c, _ = admit(feat("order", {"p": "score"}), G)
        assert c["expected_rows"] == [[[0, 1, 2, 3]]]  # -0.0 ties with 0.0, broken by id
        c, _ = admit(feat("projection", {"op": "+", "a": {"p": "age"}, "b": {"v": 1}}), G)
        assert c["expected_error"] == "integer overflow" and c["expected_rows"] == []
        c, _ = admit(feat("filter", {"cmp": ">", "a": {"op": "/", "a": {"p": "age"}, "b": {"v": 0}}, "b": {"v": 1}}), G)
        assert c["expected_error"] == "integer division by zero"
        c, why = admit(feat("projection", {"case": [[{"not": {"p": "flag"}}, {"op": "+", "a": {"p": "age"}, "b": {"v": 1}}]],
                                           "else": {"v": 0}}), G)
        assert c is None and "conditional" in why
        # Gremlin profile keeps booleans out of ordering
        assert gadmit(feat("order", {"p": "flag"}), G)[1].startswith("invalid")
    finally:
        gecko_ast.V2 = False
    assert admit(feat("order", {"p": "flag"}), G)[1].startswith("invalid")


if __name__ == "__main__":
    test_v2()
    print("OK")
