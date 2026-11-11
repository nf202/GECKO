"""Finite path/edge multiplicity contracts for the graph-testing oracle.

The evaluator is deliberately explicit about whether a target engine treats a
variable-length match as a walk (relationship reuse allowed) or an edge-simple
path. It returns source-node projections with multiplicity, matching the
read-only query templates used by the path probe.
"""
from dataclasses import dataclass

@dataclass(frozen=True)
class PathContract:
    name: str
    reuse_relationships: bool
    undirected_self_loop_orientations: int = 2

KUZU_WALK = PathContract("kuzu-walk-v1", reuse_relationships=True)
EDGE_SIMPLE = PathContract("edge-simple-v1", reuse_relationships=False, undirected_self_loop_orientations=1)

def _adjacency(g, undirected=False):
    adj={n["id"]:[] for n in g["nodes"]}
    for e in g["edges"]:
        adj[e["src"]].append((e["eid"],e["dst"]))
        if undirected:
            adj[e["dst"]].append((e["eid"],e["src"]))
    return adj

def _node_map(g):
    return {n["id"]: n for n in g["nodes"]}

def bindings(g, context, contract=KUZU_WALK):
    """Return source-node bindings in query-result multiplicity order."""
    byid=_node_map(g)
    if context=="node":
        return list(g["nodes"])
    if context=="edge":
        return [byid[e["src"]] for e in g["edges"]]
    if context=="undirected":
        out=[]
        for e in g["edges"]:
            out.append(byid[e["src"]])
            if e["src"]!=e["dst"] or contract.undirected_self_loop_orientations==2:
                out.append(byid[e["dst"]])
        return out
    if context=="two_edge":
        adj=_adjacency(g)
        out=[]
        for start in byid:
            for eid1,mid in adj[start]:
                for eid2,_ in adj[mid]:
                    if contract.reuse_relationships or eid1!=eid2:
                        out.append(byid[start])
        return out
    if context=="bounded_path":
        adj=_adjacency(g);out=[]
        def extend(start,node,used,depth):
            if depth>=1: out.append(byid[start])
            if depth==3:return
            for eid,nxt in adj[node]:
                if contract.reuse_relationships or eid not in used:
                    extend(start,nxt,used+(eid,),depth+1)
        for start in byid: extend(start,start,(),0)
        return out
    raise ValueError("Unknown path context: "+context)

def finite_expected_path(g, expr, context, contract=KUZU_WALK):
    rows=[[],[],[],[]]
    for node in bindings(g,context,contract):
        value=expr.value(node)
        row=[node["age"]]
        rows[0].append(row)
        index=3 if value is None else 1 if value else 2
        rows[index].append(row)
    return rows

def binding_count(g,context,contract=KUZU_WALK):
    return len(bindings(g,context,contract))
