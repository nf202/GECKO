import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.*;
import java.util.concurrent.*;
import javax.script.Bindings;

import org.apache.tinkerpop.gremlin.groovy.jsr223.GremlinGroovyScriptEngine;
import org.apache.tinkerpop.gremlin.process.traversal.Traversal;
import org.apache.tinkerpop.gremlin.structure.Element;
import org.apache.tinkerpop.gremlin.structure.Graph;
import org.apache.tinkerpop.gremlin.structure.util.GraphFactory;

/**
 * Embedded Gremlin batch runner for Gecko (one JVM per graph state and engine).
 *
 * args: kind (tinkergraph | janusgraph), input file, output file, timeout ms.
 * Input lines are "LOAD\t<base64 Groovy>" (owned-graph setup, writes allowed) followed by
 * "<case id>\t<base64 Gremlin>" measured queries. Each measured query runs with a timeout and,
 * on transactional graphs, inside a transaction that is rolled back. Output is one JSON object
 * per input line, serialized here without type coercion (Long stays integral, Double keeps ".0").
 */
public class GremlinBatchRunner {
    public static void main(String[] args) throws Exception {
        String kind = args[0];
        List<String> lines = Files.readAllLines(Paths.get(args[1]), StandardCharsets.UTF_8);
        long timeoutMs = Long.parseLong(args[3]);
        Map<String, Object> conf = new HashMap<>();
        if (kind.equals("janusgraph")) {
            conf.put("gremlin.graph", "org.janusgraph.core.JanusGraphFactory");
            conf.put("storage.backend", "inmemory");
        } else {
            conf.put("gremlin.graph", "org.apache.tinkerpop.gremlin.tinkergraph.structure.TinkerGraph");
        }
        Graph graph = GraphFactory.open(conf);
        boolean tx = graph.features().graph().supportsTransactions();
        GremlinGroovyScriptEngine engine = new GremlinGroovyScriptEngine();
        Bindings b = engine.createBindings();
        b.put("graph", graph);
        b.put("g", graph.traversal());
        StringBuilder out = new StringBuilder();
        ExecutorService ex = Executors.newSingleThreadExecutor();
        for (String line : lines) {
            if (line.isEmpty()) continue;
            int tab = line.indexOf('\t');
            String id = line.substring(0, tab);
            String script = new String(Base64.getDecoder().decode(line.substring(tab + 1)), StandardCharsets.UTF_8);
            StringBuilder rec = new StringBuilder("{\"id\":");
            str(id, rec);
            long t0 = System.nanoTime();
            Future<Object> f = ex.submit(() -> {
                Object r = engine.eval(script, b);
                if (r instanceof Traversal) r = ((Traversal<?, ?>) r).toList();
                return r;
            });
            try {
                Object r = f.get(timeoutMs, TimeUnit.MILLISECONDS);
                if (id.equals("LOAD") && tx) graph.tx().commit();
                rec.append(",\"status\":\"ok\",\"result\":");
                js(r, rec);
            } catch (TimeoutException e) {
                f.cancel(true);
                ex.shutdownNow();
                ex = Executors.newSingleThreadExecutor();
                rec.append(",\"status\":\"error\",\"error_type\":\"timeout\",\"error\":\"timed out\"");
            } catch (ExecutionException e) {
                Throwable c = e.getCause() == null ? e : e.getCause();
                while (c.getCause() != null && c.getCause() != c && c instanceof javax.script.ScriptException) c = c.getCause();
                rec.append(",\"status\":\"error\",\"error_type\":");
                str(c.getClass().getName(), rec);
                rec.append(",\"error\":");
                str(String.valueOf(c.getMessage()), rec);
            } finally {
                if (tx && !id.equals("LOAD")) {
                    try { graph.tx().rollback(); } catch (Exception ignored) { }
                }
            }
            rec.append(",\"elapsed_s\":").append((System.nanoTime() - t0) / 1e9).append("}\n");
            out.append(rec);
        }
        ex.shutdownNow();
        Files.write(Paths.get(args[2]), out.toString().getBytes(StandardCharsets.UTF_8));
        try { graph.close(); } catch (Exception ignored) { }
        System.exit(0);
    }

    static void str(String s, StringBuilder sb) {
        sb.append('"');
        for (char c : s.toCharArray()) {
            if (c == '"' || c == '\\') sb.append('\\').append(c);
            else if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
            else sb.append(c);
        }
        sb.append('"');
    }

    static void js(Object o, StringBuilder sb) {
        if (o == null) {
            sb.append("null");
        } else if (o instanceof Boolean || o instanceof Long || o instanceof Integer || o instanceof Short
                || o instanceof Byte || o instanceof java.math.BigInteger) {
            sb.append(o);
        } else if (o instanceof Double || o instanceof Float) {
            double d = ((Number) o).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                sb.append("{\"__nonfinite__\":");
                str(Double.toString(d), sb);
                sb.append('}');
            } else {
                sb.append(Double.toString(d));
            }
        } else if (o instanceof java.math.BigDecimal) {
            sb.append("{\"__decimal__\":");
            str(o.toString(), sb);
            sb.append('}');
        } else if (o instanceof String) {
            str((String) o, sb);
        } else if (o instanceof Map.Entry) {
            Map.Entry<?, ?> e = (Map.Entry<?, ?>) o;
            sb.append("{\"__entry__\":[");
            js(e.getKey(), sb);
            sb.append(',');
            js(e.getValue(), sb);
            sb.append("]}");
        } else if (o instanceof Map) {
            sb.append("{\"__map__\":[");
            boolean first = true;
            for (Map.Entry<?, ?> e : ((Map<?, ?>) o).entrySet()) {
                if (!first) sb.append(',');
                first = false;
                sb.append('[');
                js(e.getKey(), sb);
                sb.append(',');
                js(e.getValue(), sb);
                sb.append(']');
            }
            sb.append("]}");
        } else if (o instanceof Iterable) {
            sb.append('[');
            boolean first = true;
            for (Object x : (Iterable<?>) o) {
                if (!first) sb.append(',');
                first = false;
                js(x, sb);
            }
            sb.append(']');
        } else if (o instanceof Element) {
            sb.append("{\"__element__\":");
            str(String.valueOf(((Element) o).id()), sb);
            sb.append('}');
        } else {
            sb.append("{\"__other__\":");
            str(o.getClass().getName() + ":" + o, sb);
            sb.append('}');
        }
    }
}
