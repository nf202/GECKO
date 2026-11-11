"""Owned FalkorDB 6.0.3 launcher in the existing project-local Jammy namespace."""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime/falkordb-6.0.3-client"))
sys.path.insert(0, str(ROOT / "src"))
from gqltest.semantic_suite import canonical


class Rows:
    def __init__(self, result):
        self.keys = [str(h[1]) for h in result.header]
        self.rows = result.result_set
        self.position = 0

    def get_column_names(self):
        return self.keys

    def has_next(self):
        return self.position < len(self.rows)

    def get_next(self):
        row = self.rows[self.position]
        self.position += 1
        return row

    def close(self):
        pass


class ReadConnection:
    def __init__(self, graph):
        self.graph = graph

    def execute(self, query):
        return Rows(self.graph.ro_query(query, timeout=2000))


class FalkorEngine:
    def __init__(self, port):
        from falkordb import FalkorDB
        self.db = FalkorDB(host="127.0.0.1", port=port, socket_timeout=5,
                           socket_connect_timeout=2, protocol=2)
        self.graph = self.db.select_graph("single_seed_frontier")
        self.conn = ReadConnection(self.graph)
        self.name, self.version = "falkordb", "6.0.3"

    def setup(self, g):
        if self.db.connection.dbsize() != 0:
            raise RuntimeError("Refusing nonempty FalkorDB instance")
        for n in g["nodes"]:
            self.graph.query("CREATE (:P {id:$id,age:$age,score:$score,name:$name,flag:$flag})", params=n, timeout=2000)
        for e in g["edges"]:
            self.graph.query("MATCH (a:P),(b:P) WHERE a.id=$src AND b.id=$dst "
                             "CREATE (a)-[:R {eid:$eid,weight:$weight}]->(b)", params=e, timeout=2000)

    def execute(self, query):
        start = time.monotonic()
        try:
            r = self.graph.query(query, timeout=2000)
            return {"status": "ok", "rows": canonical(r.result_set),
                    "header": r.header, "seconds": time.monotonic() - start}
        except Exception as e:
            return {"status": "error", "error_type": type(e).__name__,
                    "error": str(e), "seconds": time.monotonic() - start}

    def await_index(self):
        deadline = time.monotonic() + 20
        last = None
        while time.monotonic() < deadline:
            last = self.execute("CALL db.indexes()")
            if last["status"] == "ok":
                names = [str(h[1]).lower() for h in last["header"]]
                if "status" in names:
                    statuses = [str(row[names.index("status")]).upper() for row in last["rows"]]
                    if statuses and all(s == "OPERATIONAL" for s in statuses):
                        return {"verified": True, "catalog": last}
            time.sleep(.1)
        return {"verified": False, "catalog": last}

    def close(self):
        self.db.close()


def launch(run):
    from campaign_current_path_semantics import free_port, sha, save
    run.resolve().relative_to(ROOT / "runs")
    port = free_port()
    rootfs = ROOT / "runtime/jammy-bwrap/rootfs"
    redis = ROOT / "runtime/falkordb-6.0.3-redis/redis-8.10.2/src/redis-server"
    module = ROOT / "runtime/falkordb-6.0.3-inputs/falkordb-amazonlinux2023-x64.so"
    command = ["/usr/bin/env", "-i", "/usr/bin/bwrap", "--die-with-parent",
               "--ro-bind", str(rootfs), "/", "--bind", str(ROOT), str(ROOT),
               "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/run", "--tmpfs", "/tmp",
               "--unshare-pid", "--unshare-uts", "--unshare-ipc", "--share-net",
               "--uid", "0", "--gid", "0", "--setenv", "PATH", "/usr/bin:/bin",
               "--setenv", "HOME", "/tmp", "--setenv", "LANG", "C.UTF-8",
               str(redis), "--bind", "127.0.0.1", "--port", str(port),
               "--protected-mode", "yes", "--save", "", "--appendonly", "no",
               "--maxmemory", "1gb", "--dir", str(run), "--daemonize", "no",
               "--loadmodule", str(module), "THREAD_COUNT", "2", "OMP_THREAD_COUNT", "2"]
    log = (run / "process.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(command, cwd=run, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    receipt = {"engine": "falkordb-6.0.3", "project_owned": True, "pid": proc.pid,
               "port": port, "binary_sha256": sha(redis), "module_sha256": sha(module),
               "command": command, "terminated": False, "project_root": str(ROOT),
               "host": "project-local Jammy namespace on existing host kernel",
               "redis_build": "8.10.2; MALLOC=libc BUILD_TLS=no BUILD_WITH_MODULES=no OPTIMIZATION=-O2; -j2"}
    save(run / "ownership.json", receipt)
    engine = None
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"FalkorDB exited {proc.returncode}; see {run / 'process.log'}")
            try:
                engine = FalkorEngine(port)
                engine.db.connection.ping()
                receipt["modules"] = engine.db.connection.execute_command("MODULE", "LIST")
                receipt["redis_info"] = engine.db.connection.info("server")
                save(run / "ownership.json", receipt)
                return engine, proc, log, receipt
            except Exception:
                if engine:
                    engine.close()
                engine = None
                time.sleep(.2)
        raise TimeoutError("FalkorDB readiness exceeded 45 seconds")
    except BaseException:
        if engine:
            engine.close()
        stop(proc, log, receipt, run)
        raise


def stop(proc, log, receipt, run):
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
    receipt.update(terminated=True, engine_exit=proc.returncode)
    (run / "ownership.json").write_text(json.dumps(receipt, indent=2, default=str) + "\n")
    log.close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    run = a.output.resolve()
    run.relative_to(ROOT / "runs")
    run.mkdir(parents=True, exist_ok=False)
    engine, proc, log, receipt = launch(run)
    try:
        result = engine.execute("RETURN 1 AS c0,NULL AS c1,[1,NULL] AS c2,true AS c3")
        (run / "preflight.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        if result["status"] != "ok" or result["rows"] != [[1, None, [1, None], True]]:
            raise SystemExit(2)
    finally:
        engine.close()
        stop(proc, log, receipt, run)
