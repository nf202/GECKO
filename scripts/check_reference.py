"""Admit example features and compute reference rows without engines or models."""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "src")]

from gecko_ast import admit
from gecko_gremlin import gadmit


def main():
    graph = json.loads((ROOT / "examples/graph.json").read_text(encoding="utf-8"))
    features = json.loads((ROOT / "examples/features.json").read_text(encoding="utf-8"))
    results = []
    for language, admission in (("cypher", admit), ("gremlin", gadmit)):
        for feature in features:
            case, reason = admission(feature, graph)
            if case is None:
                raise RuntimeError(f"Example rejected ({language}, {feature['fid']}): {reason}")
            results.append({"language": language, "id": case["id"],
                            "query": case["query"], "expected_rows": case["expected_rows"]})
    print(json.dumps({"database_calls": 0, "model_calls": 0, "cases": results},
                     indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
