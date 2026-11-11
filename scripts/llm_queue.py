"""File-based language-model request queue (server side).

A campaign writes runs/<run>/llm/req_<id>.json and waits for resp_<id>.json,
which scripts/local_llm_worker.py produces with the locally authenticated
Codex CLI. Only prompts, schemas and model outputs cross the channel; no
credentials. The model never sees expected results.
"""
from __future__ import annotations
import hashlib
import json
import time
from pathlib import Path


def request(run_dir: Path, kind: str, prompt: str, schema: dict, timeout: float = 900.0) -> dict:
    q = run_dir / "llm"
    q.mkdir(exist_ok=True)
    rid = f"{len(list(q.glob('req_*.json'))) + 1:03d}_{kind}"
    sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    req = {"id": rid, "kind": kind, "prompt": prompt, "schema": schema, "prompt_sha256": sha,
           "created_unix": time.time()}
    tmp = q / f"req_{rid}.json.tmp"
    tmp.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    tmp.rename(q / f"req_{rid}.json")
    deadline = time.time() + timeout
    resp_path = q / f"resp_{rid}.json"
    while time.time() < deadline:
        if resp_path.exists():
            try:
                resp = json.loads(resp_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(1)
                continue
            if resp.get("prompt_sha256") != sha:
                return {"id": rid, "status": "binding_error", "output": None}
            return {"id": rid, **resp}
        time.sleep(3)
    return {"id": rid, "status": "timeout", "output": None}
