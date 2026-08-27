"""Surface syntax tree for Vela.

Every node carries the `Span` it was parsed from. Nodes are plain dataclasses;
later passes attach inferred types via the `ty` slot where relevant.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import Span

# ==========================================================================
# Type expressions (surface syntax for types)
# ==========================================================================


@dataclass(slots=True)
class TypeExpr:
    span: Span


@dataclass(slots=True)
class TEVar(TypeExpr):
    """A lowercase type variable, e.g. `a`."""
    name: str


@dataclass(slots=True)
class TECon(TypeExpr):
    """A type constructor applied to arguments, e.g. `Option Int` or `Int`."""
    name: str
    args: list[TypeExpr] = field(default_factory=list)
    module: str | None = None


@dataclass(slots=True)
class TEFun(TypeExpr):
    param: TypeExpr
    result: TypeExpr


@dataclass(slots=True)
class TETuple(TypeExpr):
    items: list[TypeExpr]


@dataclass(slots=True)
class TEList(TypeExpr):
    item: TypeExpr


@dataclass(slots=True)
class TERecord(TypeExpr):
    fields: list[tuple[str, TypeExpr]]
    rest: str | None = None  # row variable name for open records


# ==========================================================================
# Patterns
# ==========================================================================


@dataclass(slots=True)
class Pattern:
    span: Span


@dataclass(slots=True)
class PWild(Pattern):
    pass


@dataclass(slots=True)
class PVar(Pattern):
    name: str


@dataclass(slots=True)
class PLit(Pattern):
    value: object
    kind: str  # "Int" | "Float" | "String" | "Bool" | "Unit"


@dataclass(slots=True)
class PCon(Pattern):
    """Constructor pattern, e.g. `Some x` or `Nil`."""
    name: str
    args: list[Pattern] = field(default_factory=list)
    module: str | None = None


@dataclass(slots=True)
class PTuple(Pattern):
    items: list[Pattern]


@dataclass(slots=True)
class PList(Pattern):
    items: list[Pattern]


@dataclass(slots=True)
class PCons(Pattern):
    head: Pattern
    tail: Pattern


@dataclass(slots=True)
class PRecord(Pattern):
    fields: list[tuple[str, Pattern]]
    open: bool = True  # record patterns match at least the listed fields


@dataclass(slots=True)
class PAs(Pattern):
    pattern: Pattern
    name: str


def pattern_vars(p: Pattern) -> list[tuple[str, Span]]:
    """All variables bound by a pattern, in left-to-right order."""
    out: list[tuple[str, Span]] = []

    def go(p: Pattern) -> None:
        if isinstance(p, PVar):
            out.append((p.name, p.span))
        elif isinstance(p, PAs):
            go(p.pattern)
            out.append((p.name, p.span))
        elif isinstance(p, PCon):
            for a in p.args:
                go(a)
        elif isinstance(p, (PTuple, PList)):
            for a in p.items:
                go(a)
        elif isinstance(p, PCons):
            go(p.head)
            go(p.tail)
        elif isinstance(p, PRecord):
            for _, sub in p.fields:
                go(sub)

    go(p)
    return out


# ==========================================================================
# Expressions
# ==========================================================================


@dataclass(slots=True)
class Expr:
    span: Span


@dataclass(slots=True)
class ELit(Expr):
    value: object
    kind: str  # "Int" | "Float" | "String" | "Bool" | "Unit"


@dataclass(slots=True)
class EVar(Expr):
    name: str
    module: str | None = None


@dataclass(slots=True)
class ECon(Expr):
    """A bare data constructor used as a value, e.g. `Some`."""
    name: str
    module: str | None = None


@dataclass(slots=True)
class ELambda(Expr):
    params: list[Pattern]
    body: Expr


@dataclass(slots=True)
class EApp(Expr):
    fn: Expr
    args: list[Expr]


@dataclass(slots=True)
class EBinOp(Expr):
    op: str
    lhs: Expr
    rhs: Expr
    op_span: Span | None = None


@dataclass(slots=True)
class EUnOp(Expr):
    op: str
    operand: Expr


@dataclass(slots=True)
class EIf(Expr):
    cond: Expr
    then: Expr
    otherwise: Expr


@dataclass(slots=True)
class EAnd(Expr):
    lhs: Expr
    rhs: Expr


@dataclass(slots=True)
class EOr(Expr):
    lhs: Expr
    rhs: Expr


@dataclass(slots=True)
class Binding:
    """One `name params = body` clause inside a let (or a top-level decl)."""
    name: str
    params: list[Pattern]
    body: Expr
    span: Span
    annotation: TypeExpr | None = None
    name_span: Span | None = None


@dataclass(slots=True)
class ELet(Expr):
    bindings: list[Binding]
    body: Expr
    recursive: bool = False


@dataclass(slots=True)
class ELetPattern(Expr):
    """`let (a, b) = e in body` — destructuring, never recursive."""
    pattern: Pattern
    value: Expr
    body: Expr


@dataclass(slots=True)
class ESeq(Expr):
    first: Expr
    second: Expr


@dataclass(slots=True)
class MatchArm:
    pattern: Pattern
    body: Expr
    span: Span
    guard: Expr | None = None


@dataclass(slots=True)
class EMatch(Expr):
    scrutinee: Expr
    arms: list[MatchArm]


@dataclass(slots=True)
class ETuple(Expr):
    items: list[Expr]


@dataclass(slots=True)
class EList(Expr):
    items: list[Expr]


@dataclass(slots=True)
class ERecord(Expr):
    fields: list[tuple[str, Expr]]
    base: Expr | None = None  # `{ base | f = v }`


@dataclass(slots=True)
class EField(Expr):
    record: Expr
    name: str
    name_span: Span | None = None


@dataclass(slots=True)
class EAnnot(Expr):
    expr: Expr
    annotation: TypeExpr


# ==========================================================================
# Declarations
# ==========================================================================


@dataclass(slots=True)
class Decl:
    span: Span


@dataclass(slots=True)
class DLet(Decl):
    """A (possibly mutually recursive) group of top-level value bindings."""
    bindings: list[Binding]
    recursive: bool = False


@dataclass(slots=True)
class ConDef:
    name: str
    args: list[TypeExpr]
    span: Span


@dataclass(slots=True)
class DType(Decl):
    name: str
    params: list[str]
    constructors: list[ConDef]


@dataclass(slots=True)
class DTypeAlias(Decl):
    name: str
    params: list[str]
    target: TypeExpr


@dataclass(slots=True)
class DImport(Decl):
    module: str
    alias: str | None = None


@dataclass(slots=True)
class DExtern(Decl):
    """`extern name : Type = "prim"` — binds a runtime primitive."""
    name: str
    annotation: TypeExpr
    prim: str


@dataclass(slots=True)
class Module:
    name: str
    decls: list[Decl]
    path: str = "<input>"
