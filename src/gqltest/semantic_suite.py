"""Frozen finite-reference suite beyond MATCH-WHERE partitioning.

This module enumerates fixture rows directly; it never obtains expected
answers from a query engine or parses/evaluates the generated Cypher text.
"""
from __future__ import annotations
import collections, json, random
from decimal import Decimal
THRESHOLDS=(-1,0,1)
FAMILIES=(
    "optional_plain","optional_target_where","optional_source_where","optional_post_where",
    "optional_chain","optional_distinct","optional_aggregate","node_group",
    "empty_aggregate","empty_group","collect_size","collect_distinct_size",
    "case_bucket","list_comprehension","unwind_duplicates","empty_unwind_count",
    "union_all","union_distinct","node_aggregate","edge_aggregate",
)
PARAMETRIC={"optional_target_where","optional_source_where","optional_post_where","case_bucket","list_comprehension"}

def fixture(seed):
    rng=random.Random(seed)
    ages=[None,0,1,-1,2,-2,None]+[rng.randint(-5,5) for _ in range(5)]
    nodes=[{"id":i,"age":a,"score":None if a is None else a/2,
            "name":None if i%4==0 else f"n{i%3}","flag":None if i%3==0 else bool(i%2)}
           for i,a in enumerate(ages)]
    pairs=[(0,1),(0,1),(1,1),(1,2),(2,3),(3,0),(4,5),(6,6)]
    pairs += [(rng.randrange(10),rng.randrange(10)) for _ in range(10)]
    return {"seed":seed,"nodes":nodes,"edges":[{"eid":i,"src":s,"dst":t,"weight":(i%7)-3} for i,(s,t) in enumerate(pairs)]}

def specs():
    out=[]
    for family in FAMILIES:
        for threshold in (THRESHOLDS if family in PARAMETRIC else (0,)):
            key=f"{family}:{threshold}" if family in PARAMETRIC else family
            out.append({"id":key,"family":family,"threshold":threshold,"query":query(family,threshold)})
    return out

def query(f,t):
    match="MATCH (a:P)"; opt=match+" OPTIONAL MATCH (a)-[r:R]->(b:P)"
    projection=" RETURN a.id, r.eid, b.id"
    if f=="optional_plain": return opt+projection
    if f=="optional_target_where": return opt+f" WHERE b.age > {t}"+projection
    if f=="optional_source_where": return opt+f" WHERE a.age > {t}"+projection
    if f=="optional_post_where": return opt+f" WITH a,r,b WHERE b.age > {t}"+projection
    if f=="optional_chain": return opt+" OPTIONAL MATCH (b)-[s:R]->(c:P) RETURN a.id,r.eid,b.id,s.eid,c.id"
    if f=="optional_distinct": return opt+" RETURN DISTINCT a.id,b.id"
    if f=="optional_aggregate": return opt+" RETURN a.id,count(*),count(r.eid),count(b.age),sum(r.weight),min(b.age),max(b.age),avg(b.age)"
    if f=="node_group": return match+" RETURN a.age,count(*),count(a.age),sum(a.age),count(DISTINCT a.age)"
    if f=="empty_aggregate": return match+" WHERE a.id < 0 RETURN count(*),count(a.age),sum(a.age),min(a.age),max(a.age),avg(a.age)"
    if f=="empty_group": return match+" WHERE a.id < 0 RETURN a.age,count(*)"
    if f=="collect_size": return opt+" WITH a.id AS aid,collect(b.id) AS ids RETURN aid,size(ids)"
    if f=="collect_distinct_size": return opt+" WITH a.id AS aid,collect(DISTINCT b.id) AS ids RETURN aid,size(ids)"
    if f=="case_bucket": return match+f" RETURN a.id,CASE WHEN a.age IS NULL THEN 'missing' WHEN a.age > {t} THEN 'positive' ELSE 'other' END"
    if f=="list_comprehension": return match+f" RETURN a.id,[x IN [a.age,0,2] WHERE x > {t} | x + 1]"
    if f=="unwind_duplicates": return match+" UNWIND [a.age,a.age,0,NULL] AS x RETURN a.id,x"
    if f=="empty_unwind_count": return "UNWIND [] AS x RETURN count(*)"
    if f in ("union_all","union_distinct"):
        return match+" RETURN a.id AS id UNION"+(" ALL" if f=="union_all" else "")+" "+match+" RETURN a.id AS id"
    aggregates=" RETURN count(*),count(a.age),sum(a.age),min(a.age),max(a.age),avg(a.age)"
    if f=="node_aggregate": return match+aggregates
    if f=="edge_aggregate": return "MATCH (a:P)-[r:R]->(b:P)"+aggregates
    raise ValueError(f)

def optional_rows(g, predicate=None):
    by_id={n["id"]:n for n in g["nodes"]}
    out=[]
    for a in g["nodes"]:
        matches=[(a,e,by_id[e["dst"]]) for e in g["edges"] if e["src"]==a["id"]]
        if predicate is not None: matches=[row for row in matches if predicate(*row)]
        out.extend(matches or [(a,None,None)])
    return out

def aggregate(values):
    nn=[v for v in values if v is not None]
    return [len(values),len(nn),sum(nn),min(nn) if nn else None,max(nn) if nn else None,sum(nn)/len(nn) if nn else None]

def reference(g,spec):
    f,t=spec["family"],spec["threshold"]; ns=g["nodes"]; by_id={n["id"]:n for n in ns}
    gt=lambda age: age is not None and age>t
    opt=optional_rows(g)
    if f in ("optional_plain","optional_target_where","optional_source_where","optional_post_where"):
        if f=="optional_target_where": opt=optional_rows(g,lambda a,e,b:gt(b["age"]))
        if f=="optional_source_where": opt=optional_rows(g,lambda a,e,b:gt(a["age"]))
        if f=="optional_post_where": opt=[r for r in opt if r[2] is not None and gt(r[2]["age"])]
        return [[a["id"],e["eid"] if e else None,b["id"] if b else None] for a,e,b in opt]
    if f=="optional_chain":
        rows=[]
        for a,e,b in opt:
            second=[(s,by_id[s["dst"]]) for s in g["edges"] if b is not None and s["src"]==b["id"]]
            for s,c in (second or [(None,None)]):
                rows.append([a["id"],e["eid"] if e else None,b["id"] if b else None,s["eid"] if s else None,c["id"] if c else None])
        return rows
    if f=="optional_distinct":
        return [list(r) for r in set((a["id"],b["id"] if b else None) for a,e,b in opt)]
    if f=="optional_aggregate":
        rows=[]
        for a in ns:
            subset=[r for r in opt if r[0]["id"]==a["id"]]
            vals=[b["age"] if b else None for _,e,b in subset]; agg=aggregate(vals)
            rows.append([a["id"],len(subset),sum(e is not None for _,e,b in subset),agg[1],
                         sum(e["weight"] for _,e,b in subset if e),*agg[3:]])
        return rows
    if f=="node_group":
        grouped=collections.defaultdict(list)
        for n in ns: grouped[n["age"]].append(n["age"])
        return [[age,len(vals),sum(v is not None for v in vals),sum(v for v in vals if v is not None),len({v for v in vals if v is not None})] for age,vals in grouped.items()]
    if f=="empty_aggregate": return [aggregate([])]
    if f=="empty_group": return []
    if f in ("collect_size","collect_distinct_size"):
        result=[]
        for a in ns:
            vals=[b["id"] for n,e,b in opt if n["id"]==a["id"] and b is not None]
            result.append([a["id"],len(set(vals) if f=="collect_distinct_size" else vals)])
        return result
    if f=="case_bucket": return [[n["id"],"missing" if n["age"] is None else "positive" if gt(n["age"]) else "other"] for n in ns]
    if f=="list_comprehension": return [[n["id"],[v+1 for v in [n["age"],0,2] if gt(v)]] for n in ns]
    if f=="unwind_duplicates": return [[n["id"],x] for n in ns for x in (n["age"],n["age"],0,None)]
    if f=="empty_unwind_count": return [[0]]
    if f in ("union_all","union_distinct"): return [[n["id"]] for n in ns]*(2 if f=="union_all" else 1)
    if f=="node_aggregate": return [aggregate([n["age"] for n in ns])]
    if f=="edge_aggregate": return [aggregate([by_id[e["src"]]["age"] for e in g["edges"]])]
    raise ValueError(f)

def canonical(value):
    # Predeclared numerical tolerance: round finite numeric outputs to 9 decimal
    # places, and equate exact integral floats with integers; NULL stays distinct.
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else canonical(float(value))
    if isinstance(value,float):
        value=round(value,9)
        return int(value) if value.is_integer() else value
    if isinstance(value,list): return [canonical(v) for v in value]
    if isinstance(value,dict): return {k:canonical(v) for k,v in value.items()}
    return value
def bag(rows):
    return collections.Counter(json.dumps(canonical(r),sort_keys=True) for r in rows)
