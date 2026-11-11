"""Exact string serialization for Gecko fixture setup transport only.

No changes to proposed values, AST admission, evaluation, query structure or scoring.
Only isolated transport2 entry points install these adapters.
"""
import json
import re
from types import MethodType

def cypher_string(value):
    return json.dumps(value,ensure_ascii=False)

def groovy_string(value):
    # Explicit UTF-16 units avoid Groovy interpolation and control characters in source.
    encoded=value.encode('utf-16-be',errors='surrogatepass')
    units=[int.from_bytes(encoded[i:i+2],'big') for i in range(0,len(encoded),2)]
    return 'new String(['+','.join(map(str,units))+'] as char[])'

def param_header(graph, params):
    from falkordb.helpers import stringify_param_value
    if params is None:return ''
    if not isinstance(params,dict):raise TypeError("'params' must be a dict")
    parts=[]
    for key,value in params.items():
        key=key.decode() if isinstance(key,bytes) else str(key)
        if not key or '`' in key:raise ValueError('Unsupported parameter name')
        # Gecko setup parameters are scalar; keep client serialization for all nonstrings.
        rendered=cypher_string(value) if isinstance(value,str) else stringify_param_value(value)
        parts.append('`'+key+'`='+rendered)
    return 'CYPHER '+' '.join(parts)+' '

def configure_falkor(engine):
    engine.graph._build_params_header=MethodType(param_header,engine.graph)

def setup_query(engine,query,params=None):
    """Only owned fixture writes: floats travel as exact decimal strings."""
    converted={}
    for key,value in (params or {}).items():
        if isinstance(value,float):
            name=key+'_setup_decimal'
            query=re.sub(r'\$'+re.escape(key)+r'\b','toFloat($'+name+')',query)
            converted[name]=repr(value)
        else:converted[key]=value
    return engine.graph.query(query,params=converted or None,timeout=5000)

def fixture_state(engine):
    """Owned setup check only: bypass lossy numeric wire rendering, not the oracle."""
    from semantic_write_frontier import NODE_STATE,EDGE_STATE
    from gqltest.semantic_suite import canonical
    nodes=engine.graph.query(NODE_STATE.replace('n.score AS c2','toString(n.score) AS c2'),timeout=5000).result_set
    edges=engine.graph.query(EDGE_STATE.replace('r.weight AS c4','toString(r.weight) AS c4'),timeout=5000).result_set
    for row in nodes:
        row[2]=float(row[2]) if row[2] is not None else None
        row[6]=sorted(row[6] or [])
    for row in edges:row[4]=float(row[4]) if row[4] is not None else None
    return {'nodes':canonical(nodes),'edges':canonical(edges)}

def reset_falkor(engine,graph,expected):
    from campaign_single_seed_writes import same_state
    setup_query(engine,'MATCH(n) DETACH DELETE n')
    for node in graph['nodes']:
        setup_query(engine,'CREATE (:P {id:$id,age:$age,score:$score,name:$name,flag:$flag})',node)
    for edge in graph['edges']:
        setup_query(engine,'MATCH(a:P),(b:P) WHERE a.id=$src AND b.id=$dst CREATE (a)-[:R {eid:$eid,weight:$weight}]->(b)',edge)
    observed=fixture_state(engine)
    if not same_state(observed,expected):raise RuntimeError('Falkor setup transport exact state mismatch')

def install(campaign):
    import gecko_gremlin
    old_gval=gecko_gremlin._gval
    gecko_gremlin._gval=lambda value:groovy_string(value) if isinstance(value,str) else old_gval(value)
    old_launch=campaign.launch_all
    def launch(out, engines=campaign.ENGINES):
        handles=old_launch(out,engines)
        for name,item in handles.items():
            if name.startswith('falkordb'):configure_falkor(item[0])
        return handles
    campaign.launch_all=launch
    old_reset=campaign.reset
    campaign.reset=lambda engine,graph,expected:reset_falkor(engine,graph,expected) if getattr(engine,'name',None)=='falkordb' else old_reset(engine,graph,expected)
    campaign.EXTRA_SOURCES += ['scripts/gecko_transport2_codec_20261109.py','scripts/gecko_transport2_entry_20261109.py']
