"""Exhaustiveness and redundancy checking for `match`.

Implements the *usefulness* algorithm from Luc Maranget, "Warnings for pattern
matching" (JFP 2007).

    U(P, q)  — is the pattern vector q useful with respect to matrix P?
               i.e. is there a value matched by q but by no row of P?

From it we get both properties we care about:

    exhaustive(P)   ==  not U(P, (_, _, ..., _))
    redundant(row i) ==  not U(rows 0..i-1, row i)

`I(P, n)` is the witness-producing variant: when a match is not exhaustive it
returns an actual example value that slips through, which we print back to the
programmer.

Records need care. Because Vela records are row-polymorphic, two record
patterns in the same column may mention different field sets. We reconcile
them per-column: the column's shape is the *union* of the labels mentioned,
and every record pattern is widened with wildcards for the labels it omits.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ast as A
from .registry import Registry

# --------------------------------------------------------------------------
# Normalized patterns
# --------------------------------------------------------------------------


class NPat:
    __slots__ = ()


@dataclass(slots=True)
class NWild(NPat):
    pass


@dataclass(slots=True)
class NCon(NPat):
    """A data constructor, including the builtin Nil/Cons/True/False/()."""
    name: str
    args: list[NPat]


@dataclass(slots=True)
class NTuple(NPat):
    args: list[NPat]


@dataclass(slots=True)
class NRecord(NPat):
    fields: dict[str, NPat]


@dataclass(slots=True)
class NLit(NPat):
    kind: str  # "Int" | "Float" | "String"
    value: object


WILD = NWild()


def normalize(p: A.Pattern) -> NPat:
    """Surface pattern -> normalized pattern (bindings are erased)."""
    match p:
        case A.PWild() | A.PVar():
            return WILD
        case A.PAs(pattern=inner):
            return normalize(inner)
        case A.PLit(value=v, kind=k):
            if k == "Bool":
                return NCon("True" if v else "False", [])
            if k == "Unit":
                return NCon("()", [])
            return NLit(k, v)
        case A.PCon(name=n, args=args):
            return NCon(n, [normalize(a) for a in args])
        case A.PTuple(items=items):
            return NTuple([normalize(i) for i in items])
        case A.PList(items=items):
            acc: NPat = NCon("Nil", [])
            for item in reversed(items):
                acc = NCon("Cons", [normalize(item), acc])
            return acc
        case A.PCons(head=h, tail=t):
            return NCon("Cons", [normalize(h), normalize(t)])
        case A.PRecord(fields=fs):
            return NRecord({k: normalize(v) for k, v in fs})
    raise AssertionError(f"unhandled pattern {p!r}")


# --------------------------------------------------------------------------
# Column shapes
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Shape:
    """What the head column of a matrix looks like."""

    kind: str                 # "con" | "tuple" | "record" | "lit" | "empty"
    con_names: set[str]
    tuple_arity: int = 0
    labels: tuple[str, ...] = ()
    literals: tuple[tuple[str, object], ...] = ()

    @property
    def is_empty(self) -> bool:
        return self.kind == "empty"


def column_shape(matrix: list[list[NPat]]) -> Shape:
    con_names: set[str] = set()
    tuple_arity = 0
    labels: set[str] = set()
    literals: list[tuple[str, object]] = []
    kind = "empty"

    for row in matrix:
        head = row[0]
        if isinstance(head, NWild):
            continue
        if isinstance(head, NCon):
            kind = "con"
            con_names.add(head.name)
        elif isinstance(head, NTuple):
            kind = "tuple"
            tuple_arity = len(head.args)
        elif isinstance(head, NRecord):
            kind = "record"
            labels.update(head.fields)
        elif isinstance(head, NLit):
            kind = "lit"
            key = (head.kind, head.value)
            if key not in literals:
                literals.append(key)

    return Shape(kind, con_names, tuple_arity, tuple(sorted(labels)), tuple(literals))


def shape_is_complete(shape: Shape, reg: Registry) -> bool:
    if shape.kind == "con":
        return reg.is_complete(shape.con_names)
    if shape.kind in ("tuple", "record"):
        return True  # a single anonymous constructor
    return False  # literals: the signature is effectively infinite


def shape_constructors(shape: Shape, reg: Registry) -> list[tuple[object, int]]:
    """The (key, arity) pairs a complete shape must be specialized against."""
    if shape.kind == "con":
        type_name = reg.type_of_con(next(iter(shape.con_names)))
        return [(name, reg.arity_of_con(name)) for name in reg.constructors_of(type_name)]
    if shape.kind == "tuple":
        return [(("tuple", shape.tuple_arity), shape.tuple_arity)]
    if shape.kind == "record":
        return [(("record", shape.labels), len(shape.labels))]
    return []


# --------------------------------------------------------------------------
# Matrix operations
# --------------------------------------------------------------------------


def _sub_patterns(head: NPat, key: object, arity: int, shape: Shape) -> list[NPat] | None:
    """Destructure `head` against constructor `key`, or None if it cannot match."""
    if isinstance(head, NWild):
        return [WILD] * arity

    if isinstance(key, str):  # data constructor
        if isinstance(head, NCon) and head.name == key:
            return list(head.args)
        return None

    kind, payload = key
    if kind == "tuple":
        if isinstance(head, NTuple) and len(head.args) == payload:
            return list(head.args)
        return None
    if kind == "record":
        if isinstance(head, NRecord):
            # widen: labels this pattern omits are unconstrained
            return [head.fields.get(label, WILD) for label in payload]
        return None
    if kind == "lit":
        if isinstance(head, NLit) and (head.kind, head.value) == payload:
            return []
        return None
    raise AssertionError(f"bad constructor key {key!r}")


def specialize(matrix: list[list[NPat]], key: object, arity: int,
               shape: Shape) -> list[list[NPat]]:
    out: list[list[NPat]] = []
    for row in matrix:
        sub = _sub_patterns(row[0], key, arity, shape)
        if sub is not None:
            out.append(sub + row[1:])
    return out


def default_matrix(matrix: list[list[NPat]]) -> list[list[NPat]]:
    """Rows whose head matches *any* constructor not otherwise listed."""
    return [row[1:] for row in matrix if isinstance(row[0], NWild)]


# --------------------------------------------------------------------------
# Usefulness
# --------------------------------------------------------------------------


def is_useful(matrix: list[list[NPat]], vector: list[NPat], reg: Registry) -> bool:
    if not matrix:
        return True
    if not vector:
        return False

    head = vector[0]
    rest = vector[1:]

    if isinstance(head, NWild):
        shape = column_shape(matrix)
        if not shape.is_empty and shape_is_complete(shape, reg):
            for key, arity in shape_constructors(shape, reg):
                sub = specialize(matrix, key, arity, shape)
                if is_useful(sub, [WILD] * arity + rest, reg):
                    return True
            return False
        return is_useful(default_matrix(matrix), rest, reg)

    shape = column_shape(matrix + [vector])
    key, arity = _key_of(head, shape)
    sub_vec = _sub_patterns(head, key, arity, shape)
    assert sub_vec is not None
    return is_useful(specialize(matrix, key, arity, shape), sub_vec + rest, reg)


def _key_of(head: NPat, shape: Shape) -> tuple[object, int]:
    if isinstance(head, NCon):
        return head.name, len(head.args)
    if isinstance(head, NTuple):
        return ("tuple", len(head.args)), len(head.args)
    if isinstance(head, NRecord):
        return ("record", shape.labels), len(shape.labels)
    if isinstance(head, NLit):
        return ("lit", (head.kind, head.value)), 0
    raise AssertionError(f"wildcard has no constructor key: {head!r}")


# --------------------------------------------------------------------------
# Witness generation (I(P, n) in the paper)
# --------------------------------------------------------------------------


def witness(matrix: list[list[NPat]], width: int, reg: Registry) -> list[NPat] | None:
    """A value vector matched by nothing in `matrix`, or None if exhaustive."""
    if not matrix:
        return [WILD] * width
    if width == 0:
        return None

    shape = column_shape(matrix)

    if not shape.is_empty and shape_is_complete(shape, reg):
        for key, arity in shape_constructors(shape, reg):
            sub = witness(specialize(matrix, key, arity, shape), arity + width - 1, reg)
            if sub is not None:
                return [_rebuild(key, sub[:arity])] + sub[arity:]
        return None

    rest = witness(default_matrix(matrix), width - 1, reg)
    if rest is None:
        return None
    return [_missing_pattern(shape, reg)] + rest


def _rebuild(key: object, args: list[NPat]) -> NPat:
    if isinstance(key, str):
        return NCon(key, args)
    kind, payload = key
    if kind == "tuple":
        return NTuple(args)
    if kind == "record":
        return NRecord(dict(zip(payload, args)))
    if kind == "lit":
        return NLit(payload[0], payload[1])
    raise AssertionError(f"bad key {key!r}")


def _missing_pattern(shape: Shape, reg: Registry) -> NPat:
    """A concrete example the column fails to cover."""
    if shape.kind == "con":
        missing = reg.missing_constructors(shape.con_names)
        if missing:
            name = missing[0]
            return NCon(name, [WILD] * reg.arity_of_con(name))
        return WILD
    if shape.kind == "lit":
        return _missing_literal(shape.literals)
    return WILD


def _missing_literal(literals: tuple[tuple[str, object], ...]) -> NPat:
    if not literals:
        return WILD
    kind = literals[0][0]
    taken = {v for k, v in literals if k == kind}
    if kind == "Int":
        n = 0
        while n in taken:
            n += 1
        return NLit("Int", n)
    if kind == "Float":
        x = 0.0
        while x in taken:
            x += 1.0
        return NLit("Float", x)
    if kind == "String":
        s = ""
        while s in taken:
            s += "x"
        return NLit("String", s)
    return WILD


# --------------------------------------------------------------------------
# Rendering witnesses back to Vela syntax
# --------------------------------------------------------------------------


def render(p: NPat, prec: int = 0) -> str:
    match p:
        case NWild():
            return "_"
        case NLit(kind=k, value=v):
            if k == "String":
                return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
            return str(v)
        case NTuple(args=args):
            return "(" + ", ".join(render(a) for a in args) + ")"
        case NRecord(fields=fs):
            inner = ", ".join(f"{k} = {render(v)}" for k, v in sorted(fs.items()))
            return "{ " + inner + " }"
        case NCon(name=n, args=args):
            if n == "()":
                return "()"
            if n == "Nil":
                return "[]"
            if n == "Cons":
                return _render_list(p)
            if not args:
                return n
            s = n + " " + " ".join(render(a, 1) for a in args)
            return f"({s})" if prec > 0 else s
    raise AssertionError(f"unhandled normalized pattern {p!r}")


def _render_list(p: NPat) -> str:
    """Prefer `[a, b]` over `a :: b :: []`, falling back to `::` for open tails."""
    items: list[str] = []
    cur = p
    while isinstance(cur, NCon) and cur.name == "Cons":
        items.append(render(cur.args[0], 1))
        cur = cur.args[1]
    if isinstance(cur, NCon) and cur.name == "Nil":
        return "[" + ", ".join(items) + "]"
    return " :: ".join(items + [render(cur, 1)])


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


@dataclass(slots=True)
class MatchReport:
    missing: list[str]            # rendered counterexamples (empty => exhaustive)
    redundant: list[int]          # indices of arms that can never fire


def check_match(arms: list[A.Pattern], guarded: list[bool], reg: Registry) -> MatchReport:
    """Check one `match` for exhaustiveness and redundancy.

    Guarded arms are excluded from the matrix: a guard may fail at runtime, so
    such an arm neither guarantees coverage nor shadows later arms.
    """
    normalized = [normalize(p) for p in arms]

    redundant: list[int] = []
    seen: list[list[NPat]] = []
    for i, (pat, has_guard) in enumerate(zip(normalized, guarded)):
        if not is_useful(seen, [pat], reg):
            redundant.append(i)
        if not has_guard:
            seen.append([pat])

    example = witness(seen, 1, reg)
    missing = [render(example[0])] if example is not None else []
    return MatchReport(missing, redundant)
