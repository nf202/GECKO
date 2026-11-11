"""Local worker for scripts/llm_queue.py: polls a server run, answers requests with local Codex.

Run on the workstation that holds the Codex login. Model and reasoning effort
can be frozen explicitly, otherwise they come from the existing Codex config;
tools, web search and MCP servers
are disabled for generation. Stops when the run writes summary.json, after
--max-calls answered requests, or after --idle seconds without requests.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
from pathlib import Path
import subprocess
import time
import tomllib

HOST = None  # Set explicitly through --host.
REMOTE_ROOT = None  # Set explicitly through --remote-root.
NODE = shutil.which("node")
CLI = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@openai/codex/bin/codex.js"
LOCAL = Path(__file__).resolve().parents[1] / "relay_runtime"


def ssh(cmd, timeout=60):
    return subprocess.run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", HOST, cmd],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)


def codex(prompt, schema, folder, model, effort, mcp):
    (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
    (folder / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    cmd = [str(NODE), str(CLI), "-a", "never", "exec", "--model", model,
           "-c", "model_reasoning_effort=" + json.dumps(effort), "--sandbox", "read-only",
           "--skip-git-repo-check", "--ephemeral", "-c", 'web_search="disabled"']
    for f in ("shell_tool", "apps", "plugins", "multi_agent", "browser_use", "computer_use", "image_generation"):
        cmd += ["--disable", f]
    for name in mcp:
        cmd += ["-c", f"mcp_servers.{name}.enabled=false"]
    cmd += ["--json", "--color", "never", "--output-schema", str(folder / "schema.json"),
            "--output-last-message", str(folder / "raw_output.json"), "-"]
    env = os.environ.copy()
    tmp = folder / "tmp"
    tmp.mkdir(exist_ok=True)
    env.update(TMP=str(tmp), TEMP=str(tmp), TMPDIR=str(tmp))
    started = time.time()
    try:
        p = subprocess.run(cmd, cwd=folder, env=env, input=prompt, text=True, encoding="utf-8",
                           capture_output=True, timeout=300)
        code, events = p.returncode, p.stdout
        stderr = p.stderr
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or b""
        code, events = 124, partial.decode("utf-8", "replace") if isinstance(partial, bytes) else partial
        stderr = exc.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
    (folder / "events.jsonl").write_text(events or "", encoding="utf-8")
    (folder / "stderr.txt").write_text(stderr or "", encoding="utf-8")
    out = None
    raw = folder / "raw_output.json"
    if code == 0 and raw.exists():
        try:
            out = json.loads(raw.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            out = None
    usage = [json.loads(l).get("usage") for l in (events or "").splitlines()
             if l.startswith("{") and '"turn.completed"' in l]
    return {"status": "ok" if out is not None else "failed", "exit_code": code, "output": out,
            "elapsed_seconds": round(time.time() - started, 1), "usage": usage}


def main():
    global HOST, REMOTE_ROOT, NODE, CLI
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", required=True, help="SSH destination hosting the campaign")
    ap.add_argument("--remote-root", required=True, help="absolute remote project runs directory")
    ap.add_argument("--node", default=NODE, help="Node executable (default: PATH)")
    ap.add_argument("--cli", type=Path, default=CLI, help="installed Codex CLI JavaScript entry")
    ap.add_argument("--run", required=True, help="absolute remote run directory under --remote-root")
    ap.add_argument("--max-calls", type=int, default=40)
    ap.add_argument("--idle", type=int, default=3600)
    ap.add_argument("--model", help="freeze the experiment model instead of reading the current default")
    ap.add_argument("--effort", help="freeze the experiment reasoning effort")
    a = ap.parse_args()
    HOST, REMOTE_ROOT, NODE, CLI = a.host, a.remote_root.rstrip("/") + "/", a.node, a.cli
    if HOST.startswith("-") or any(c.isspace() for c in HOST):
        ap.error("invalid SSH destination")
    if not REMOTE_ROOT.startswith("/") or ".." in REMOTE_ROOT.split("/"):
        ap.error("--remote-root must be an absolute path without parent traversal")
    if not NODE or not CLI.is_file():
        ap.error("provide an installed Node executable and --cli JavaScript entry")
    if not a.run.startswith(REMOTE_ROOT) or not re.fullmatch(r"[A-Za-z0-9_-]+", a.run[len(REMOTE_ROOT):]):
        raise SystemExit("run must be under " + REMOTE_ROOT)
    cfg = tomllib.loads((Path.home() / ".codex/config.toml").read_text(encoding="utf-8"))
    model, effort = a.model or cfg["model"], a.effort or cfg["model_reasoning_effort"]
    if "qwen" in model.lower():
        raise SystemExit("non-Qwen model required")
    mcp = list(cfg.get("mcp_servers", {}))
    local_run = LOCAL / ("queue-" + Path(a.run).name)
    local_run.mkdir(parents=True, exist_ok=True)
    done, last_activity = set(), time.time()
    print(json.dumps({"worker": "started", "run": a.run, "model": model, "effort": effort}), flush=True)
    while len(done) < a.max_calls and time.time() - last_activity < a.idle:
        try:
            r = ssh(f"cd {shlex.quote(a.run)} 2>/dev/null && {{ ls llm 2>/dev/null; test -e summary.json && echo __DONE__; }}")
        except subprocess.TimeoutExpired:
            print(json.dumps({"transport_poll": "timeout", "model_call_started": False}), flush=True)
            time.sleep(5)
            continue
        if r.returncode == 255:
            print(json.dumps({"transport_poll": "ssh_failed", "model_call_started": False}), flush=True)
            time.sleep(5)
            continue
        names = r.stdout.split()
        pending = sorted(n[4:-5] for n in names if n.startswith("req_") and n.endswith(".json")
                         and f"resp_{n[4:-5]}.json" not in names and n[4:-5] not in done)
        if not pending:
            if "__DONE__" in names:
                break
            time.sleep(5)
            continue
        rid = pending[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", rid):
            raise SystemExit("invalid queue request id")
        req_path = shlex.quote(a.run + "/llm/req_" + rid + ".json")
        req = json.loads(ssh(f"cat {req_path}").stdout)
        if hashlib.sha256(req["prompt"].encode("utf-8")).hexdigest() != req["prompt_sha256"]:
            raise SystemExit("prompt transport hash mismatch")
        folder = local_run / rid
        folder.mkdir(exist_ok=True)
        print(json.dumps({"call": rid, "kind": req["kind"]}), flush=True)
        resp = codex(req["prompt"], req["schema"], folder, model, effort, mcp)
        # Preserve failed model calls. No silent retry or overwriting of its evidence.
        resp.update(prompt_sha256=req["prompt_sha256"], model=model, reasoning_effort=effort, qwen_used=False)
        (folder / "response.json").write_text(json.dumps(resp, ensure_ascii=False), encoding="utf-8")
        remote_resp = shlex.quote(a.run + "/llm/resp_" + rid + ".json")
        remote_tmp = shlex.quote(a.run + "/llm/resp_" + rid + ".json.tmp")
        up = subprocess.run(["ssh", "-o", "BatchMode=yes", HOST,
                             f"cat > {remote_tmp} && mv {remote_tmp} {remote_resp}"],
                            input=json.dumps(resp, ensure_ascii=False), text=True, encoding="utf-8",
                            capture_output=True, timeout=120)
        if up.returncode:
            raise SystemExit("upload failed: " + up.stderr)
        done.add(rid)
        last_activity = time.time()
        print(json.dumps({"answered": rid, "status": resp["status"], "seconds": resp["elapsed_seconds"]}), flush=True)
    print(json.dumps({"worker": "stopped", "calls": len(done)}), flush=True)


if __name__ == "__main__":
    main()
