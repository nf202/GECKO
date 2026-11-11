import time


def diagnostic(error):
    text = str(error)
    low = text.lower()
    if "timeout" in low or "timed out" in low or "interrupted" in low:
        return "timeout", text
    # This records a rejection in this exact context, NOT a permanently
    # unsupported feature or a confirmed database defect.
    if any(x in low for x in ("parser exception", "binder exception", "syntaxerror")):
        return "context_rejection", text
    return "unknown_error", text


class KuzuEngine:
    def __init__(self, path, timeout_ms=2000):
        import kuzu
        self.name, self.version = "kuzu", kuzu.__version__
        self.db = kuzu.Database(str(path), buffer_pool_size=256 * 1024**2,
                                max_num_threads=2, max_db_size=1024**3)
        self.conn = kuzu.Connection(self.db, num_threads=2)
        self.conn.set_query_timeout(timeout_ms)

    def _execute(self, query):
        result = self.conn.execute(query)
        if isinstance(result, list):
            raise ValueError("Multiple statements are not admitted")
        try:
            rows = []
            while result.has_next():
                rows.append(result.get_next())
            return rows
        finally:
            result.close()

    def setup(self, g):
        self._execute("CREATE NODE TABLE P(id INT64, age INT64, score DOUBLE, name STRING, flag BOOL, PRIMARY KEY(id))")
        self._execute("CREATE REL TABLE R(FROM P TO P, eid INT64, weight INT64)")
        for n in g["nodes"]:
            self.conn.execute("CREATE (:P {id:$id, age:$age, score:$score, name:$name, flag:$flag})", n).close()
        for e in g["edges"]:
            self.conn.execute("MATCH (a:P), (b:P) WHERE a.id=$src AND b.id=$dst CREATE (a)-[:R {eid:$eid, weight:$weight}]->(b)", e).close()

    def execute(self, query):
        start = time.perf_counter()
        try:
            return {"status": "ok", "rows": self._execute(query),
                    "seconds": time.perf_counter() - start}
        except Exception as err:
            status, detail = diagnostic(err)
            return {"status": status, "error": detail,
                    "seconds": time.perf_counter() - start}

    def close(self):
        self.conn.close()
        self.db.close()


class BoltEngine:
    def __init__(self, uri, name="memgraph", timeout_ms=2000, version=None):
        from neo4j import GraphDatabase
        # Only project-owned local instance; explicit ownership receipt required by runner.
        if not uri.startswith("bolt://127.0.0.1:"):
            raise ValueError("Prototype only permits a dedicated local Bolt instance")
        self.driver = GraphDatabase.driver(uri, auth=None, connection_timeout=5)
        self.driver.verify_connectivity()
        self.name, self.timeout = name, timeout_ms / 1000
        if name == "memgraph":
            if not version:
                self.driver.close()
                raise ValueError("Memgraph version must come from the launched binary receipt")
            self.version = version
        else:
            with self.driver.session() as session:
                rows = session.run("CALL dbms.components() YIELD name, versions RETURN name, versions").values()
            self.version = str(rows)

    def setup(self, g):
        with self.driver.session() as session:
            # Do not clear an existing database. An empty owned instance is required.
            if session.run("MATCH (n) RETURN count(n)").single()[0] != 0:
                raise RuntimeError("Refusing to overwrite nonempty Bolt database")
            for n in g["nodes"]:
                session.run("CREATE (:P {id:$id, age:$age, score:$score, name:$name, flag:$flag})", n).consume()
            for e in g["edges"]:
                session.run("MATCH (a:P), (b:P) WHERE a.id=$src AND b.id=$dst CREATE (a)-[:R {eid:$eid, weight:$weight}]->(b)", e).consume()

    def execute(self, query):
        from neo4j import Query
        start = time.perf_counter()
        try:
            with self.driver.session() as session:
                rows = session.run(Query(query, timeout=self.timeout)).values()
            return {"status": "ok", "rows": rows, "seconds": time.perf_counter() - start}
        except Exception as err:
            status, detail = diagnostic(err)
            return {"status": status, "error": detail, "seconds": time.perf_counter() - start}

    def close(self):
        self.driver.close()
