"""Parser and finite evaluator for the admitted pure-predicate subset.

This is deliberately not a full Cypher/GQL parser. Unsupported constructs fail
closed; no Python eval, SQL execution, or model-supplied code is used.
"""
import re
from dataclasses import dataclass
from .reference import tri_and, tri_or

TYPES = {"age": "int", "name": "string", "flag": "bool", "score": "float"}
TOKEN = re.compile(r"\s+|(?:'[^'\\]*')|(?:\d+(?:\.\d+)?)|(?:[A-Za-z_][A-Za-z_0-9]*)|(?:<>|<=|>=|[=<>(),.\[\]-])")

class PredicateError(ValueError):
    pass

@dataclass(frozen=True)
class Expr:
    op: str
    args: tuple
    typ: str

    def value(self, node):
        if self.op == "literal":
            return self.args[0]
        if self.op == "property":
            return node[self.args[0]]
        xs = [a.value(node) for a in self.args]
        if self.op == "not":
            return None if xs[0] is None else not xs[0]
        if self.op == "and":
            return tri_and(*xs)
        if self.op == "or":
            return tri_or(*xs)
        if self.op == "is_null":
            return xs[0] is None
        if self.op == "is_not_null":
            return xs[0] is not None
        if self.op == "coalesce":
            return next((x for x in xs if x is not None), None)
        if self.op == "in":
            if xs[0] is None:
                return None
            if any(x is not None and x == xs[0] for x in xs[1:]):
                return True
            return None if any(x is None for x in xs[1:]) else False
        if any(x is None for x in xs):
            return None
        if self.op == "size": return len(xs[0])
        if self.op == "abs": return abs(xs[0])
        if self.op == "tolower": return xs[0].lower()
        if self.op == "starts": return xs[0].startswith(xs[1])
        if self.op == "contains": return xs[1] in xs[0]
        if self.op == "=": return xs[0] == xs[1]
        if self.op == "<>": return xs[0] != xs[1]
        if self.op == ">": return xs[0] > xs[1]
        if self.op == "<": return xs[0] < xs[1]
        if self.op == ">=": return xs[0] >= xs[1]
        if self.op == "<=": return xs[0] <= xs[1]
        raise PredicateError("Unknown evaluator operation")

class Parser:
    def __init__(self, text):
        self.tokens, pos = [], 0
        while pos < len(text):
            match = TOKEN.match(text, pos)
            if not match:
                raise PredicateError("Unadmitted token at position %d" % pos)
            value = match.group()
            if not value.isspace():
                self.tokens.append(value)
            pos = match.end()
        self.i = 0

    def peek(self):
        return self.tokens[self.i].upper() if self.i < len(self.tokens) else ""

    def take(self, token=None):
        if self.i == len(self.tokens) or (token and self.peek() != token):
            raise PredicateError("Expected %s at token %d" % (token, self.i))
        value = self.tokens[self.i]
        self.i += 1
        return value

    def accept(self, token):
        if self.peek() == token:
            self.take()
            return True
        return False

    def boolean(self, expr):
        if expr.typ != "bool":
            raise PredicateError("Boolean expression required")
        return expr

    def parse(self):
        expr = self.boolean(self.or_expr())
        if self.i != len(self.tokens):
            raise PredicateError("Trailing syntax or full query is not admitted")
        return expr

    def or_expr(self):
        x = self.and_expr()
        while self.accept("OR"):
            x = Expr("or", (self.boolean(x), self.boolean(self.and_expr())), "bool")
        return x

    def and_expr(self):
        x = self.not_expr()
        while self.accept("AND"):
            x = Expr("and", (self.boolean(x), self.boolean(self.not_expr())), "bool")
        return x

    def not_expr(self):
        if self.accept("NOT"):
            return Expr("not", (self.boolean(self.not_expr()),), "bool")
        return self.comparison()

    def compatible(self, a, b):
        if a.typ != b.typ and "null" not in (a.typ, b.typ):
            raise PredicateError("Cross-type comparison is outside the admitted subset")

    def comparison(self):
        a = self.atom()
        if self.accept("IS"):
            negate = self.accept("NOT")
            self.take("NULL")
            return Expr("is_not_null" if negate else "is_null", (a,), "bool")
        if self.accept("IN"):
            self.take("[")
            items = []
            if self.peek() != "]":
                while True:
                    item = self.atom()
                    if item.op != "literal":
                        raise PredicateError("Membership requires a literal list")
                    self.compatible(a, item)
                    items.append(item)
                    if not self.accept(","): break
            self.take("]")
            if not items: raise PredicateError("Empty-list membership requires a separate contract")
            return Expr("in", (a, *items), "bool")
        if self.peek() in ("STARTS", "CONTAINS"):
            op = self.take().upper()
            if op == "STARTS": self.take("WITH")
            b = self.atom()
            if a.typ != "string" or b.typ not in ("string", "null"):
                raise PredicateError("String operands required")
            return Expr("starts" if op == "STARTS" else "contains", (a, b), "bool")
        if self.peek() in ("=", "<>", ">", "<", ">=", "<="):
            op = self.take()
            b = self.atom()
            self.compatible(a, b)
            return Expr(op, (a, b), "bool")
        return a

    def atom(self):
        if self.accept("("):
            x = self.or_expr()
            self.take(")")
            return x
        negative = self.accept("-")
        tok = self.take()
        if re.fullmatch(r"\d+(?:\.\d+)?", tok):
            n = float(tok) if "." in tok else int(tok)
            n = -n if negative else n
            if isinstance(n, int) and not -(2**63) <= n < 2**63:
                raise PredicateError("Integer literal outside signed 64-bit domain")
            return Expr("literal", (n,), "float" if isinstance(n, float) else "int")
        if negative:
            raise PredicateError("Only numeric literals may use unary minus")
        if tok.startswith("'"):
            # ASCII ensures Python code-point length agrees with admitted witnesses.
            if not tok[1:-1].isascii():
                raise PredicateError("Non-ASCII string semantics require a separate contract")
            return Expr("literal", (tok[1:-1],), "string")
        upper = tok.upper()
        if upper in ("NULL", "TRUE", "FALSE"):
            value = {"NULL": None, "TRUE": True, "FALSE": False}[upper]
            return Expr("literal", (value,), "null" if value is None else "bool")
        if tok == "a":
            self.take(".")
            prop = self.take()
            if prop not in TYPES:
                raise PredicateError("Unknown bound property")
            return Expr("property", (prop,), TYPES[prop])
        func = tok.lower()
        if func not in ("size", "abs", "tolower", "coalesce"):
            raise PredicateError("Unknown identifier/function: %s" % tok)
        self.take("(")
        xs = [self.or_expr()]
        while self.accept(","):
            xs.append(self.or_expr())
        self.take(")")
        if func == "coalesce":
            if len(xs) < 2: raise PredicateError("coalesce requires >=2 arguments")
            nonnull = [x for x in xs if x.typ != "null"]
            typ = nonnull[0].typ if nonnull else "null"
            if any(x.typ != typ for x in nonnull): raise PredicateError("coalesce type mismatch")
        else:
            if len(xs) != 1: raise PredicateError("Unary function required")
            if func in ("size", "tolower"):
                if xs[0].typ != "string": raise PredicateError("String function requires a string")
                typ = "int" if func == "size" else "string"
            else:
                if xs[0].typ != "int": raise PredicateError("abs restricted to safe integer domain")
                typ = "int"
        return Expr(func, tuple(xs), typ)

def compile_predicate(text):
    return Parser(text).parse()

def finite_expected(g, expr, context):
    if context == "node":
        bindings = g["nodes"]
    elif context == "edge":
        byid = {n["id"]: n for n in g["nodes"]}
        bindings = [byid[e["src"]] for e in g["edges"]]
    else:
        raise PredicateError("No finite semantics contract for this context")
    rows = [[], [], [], []]
    for node in bindings:
        result = expr.value(node)
        if result is not None and type(result) is not bool:
            raise PredicateError("Non-Boolean evaluation")
        row = [node["age"]]
        rows[0].append(row)
        rows[3 if result is None else 1 if result else 2].append(row)
    return rows
