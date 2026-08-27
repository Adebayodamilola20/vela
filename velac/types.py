"""Type representation, unification, generalization and pretty-printing.

Design notes
------------

*Mutable type variables with levels.*  A `TVar` is a destructively-updated
cell.  Each unbound variable records the `let`-nesting *level* at which it was
created.  Generalizing at level `L` quantifies exactly those variables whose
level is greater than `L`, which is Rémy's trick for avoiding the expensive
"free variables of the environment" computation.

*Records as fields + tail.*  A record type is a set of known fields plus a
tail, which is either `EMPTY_ROW` (the record is closed) or a type variable
(the record is open and can grow).  Unifying two records matches the common
fields and pushes the leftovers into the other side's tail — this is what
gives `\\p -> p.x` the type `{ x : a | r } -> a`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count

from .errors import Diagnostic, Label, Span

# --------------------------------------------------------------------------
# Representation
# --------------------------------------------------------------------------


class Type:
    __slots__ = ()


@dataclass(eq=False, slots=True)
class TVar(Type):
    id: int
    level: int
    link: Type | None = None
    #: Set for variables that stand for a record tail; keeps error messages honest.
    is_row: bool = False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"t{self.id}" if self.link is None else f"t{self.id}={self.link!r}"


@dataclass(eq=False, slots=True)
class TCon(Type):
    """A type constructor applied to zero or more arguments.

    Special names: `->` (arity 2), `Tuple` (variadic), `List` (arity 1),
    `{}` (the empty row) and `$name` (a rigid skolem from an annotation).
    """

    name: str
    args: list[Type] = field(default_factory=list)

    def __repr__(self) -> str:  # pragma: no cover
        return self.name if not self.args else f"({self.name} {' '.join(map(repr, self.args))})"


@dataclass(eq=False, slots=True)
class TRec(Type):
    """`{ f1 : t1, ... | tail }`"""

    fields: dict[str, Type]
    tail: Type

    def __repr__(self) -> str:  # pragma: no cover
        inner = ", ".join(f"{k}: {v!r}" for k, v in self.fields.items())
        return f"{{{inner} | {self.tail!r}}}"


# Ground types --------------------------------------------------------------

TInt = TCon("Int")
TFloat = TCon("Float")
TBool = TCon("Bool")
TString = TCon("String")
TUnit = TCon("Unit")
EMPTY_ROW = TCon("{}")

LITERAL_TYPES = {"Int": TInt, "Float": TFloat, "Bool": TBool,
                 "String": TString, "Unit": TUnit}


def fn(*types: Type) -> Type:
    """`fn(a, b, c)` builds `a -> b -> c` (curried, right-associated)."""
    result = types[-1]
    for t in reversed(types[:-1]):
        result = TCon("->", [t, result])
    return result


def list_of(t: Type) -> Type:
    return TCon("List", [t])


def tuple_of(items: list[Type]) -> Type:
    return TCon("Tuple", list(items))


def closed_record(fields: dict[str, Type]) -> Type:
    return TRec(dict(fields), EMPTY_ROW)


def is_arrow(t: Type) -> bool:
    return isinstance(t, TCon) and t.name == "->" and len(t.args) == 2


def unfold_arrows(t: Type) -> tuple[list[Type], Type]:
    """`a -> b -> c` becomes `([a, b], c)`."""
    params: list[Type] = []
    while True:
        t = prune(t)
        if is_arrow(t):
            params.append(t.args[0])
            t = t.args[1]
        else:
            return params, t


# --------------------------------------------------------------------------
# Variable supply and levels
# --------------------------------------------------------------------------


class TypeVarSupply:
    def __init__(self) -> None:
        self._ids = count()
        self.level = 0

    def fresh(self, is_row: bool = False) -> TVar:
        return TVar(next(self._ids), self.level, None, is_row)

    def enter(self) -> None:
        self.level += 1

    def exit(self) -> None:
        self.level -= 1


# --------------------------------------------------------------------------
# Pruning (path compression through resolved variables)
# --------------------------------------------------------------------------


def prune(t: Type) -> Type:
    while isinstance(t, TVar) and t.link is not None:
        # path compression: point straight at the end of the chain
        inner = t.link
        while isinstance(inner, TVar) and inner.link is not None:
            inner = inner.link
        t.link = inner
        t = inner
    return t


def resolve(t: Type) -> Type:
    """Fully resolve a type, following links everywhere (used for printing)."""
    t = prune(t)
    if isinstance(t, TCon):
        return TCon(t.name, [resolve(a) for a in t.args])
    if isinstance(t, TRec):
        fields, tail = flatten_record(t)
        return TRec({k: resolve(v) for k, v in fields.items()}, prune(tail))
    return t


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


def flatten_record(t: TRec) -> tuple[dict[str, Type], Type]:
    """Collect every known field of a record, following its tail links."""
    fields: dict[str, Type] = {}
    tail: Type = t
    while True:
        tail = prune(tail)
        if isinstance(tail, TRec):
            for k, v in tail.fields.items():
                fields.setdefault(k, v)
            tail = tail.tail
        else:
            return fields, tail


# --------------------------------------------------------------------------
# Unification
# --------------------------------------------------------------------------


class UnifyError(Exception):
    """Raised by `unify`; the caller turns it into a Diagnostic with a span."""

    def __init__(self, message: str, note: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.note = note


def occurs_in(var: TVar, t: Type) -> bool:
    t = prune(t)
    if t is var:
        return True
    if isinstance(t, TVar):
        return False
    if isinstance(t, TCon):
        return any(occurs_in(var, a) for a in t.args)
    if isinstance(t, TRec):
        fields, tail = flatten_record(t)
        return any(occurs_in(var, v) for v in fields.values()) or occurs_in(var, tail)
    return False


def adjust_levels(var: TVar, t: Type) -> None:
    """Lower the level of every unbound variable in `t` to at most `var.level`."""
    t = prune(t)
    if isinstance(t, TVar):
        if t.level > var.level:
            t.level = var.level
    elif isinstance(t, TCon):
        for a in t.args:
            adjust_levels(var, a)
    elif isinstance(t, TRec):
        fields, tail = flatten_record(t)
        for v in fields.values():
            adjust_levels(var, v)
        adjust_levels(var, tail)


def bind(var: TVar, t: Type, supply: TypeVarSupply) -> None:
    if occurs_in(var, t):
        raise UnifyError(
            f"cannot construct the infinite type `{show(var)} = {show(t)}`",
            "this usually means a recursive value is missing an argument")
    adjust_levels(var, t)
    var.link = t


def unify(a: Type, b: Type, supply: TypeVarSupply) -> None:
    a, b = prune(a), prune(b)
    if a is b:
        return

    if isinstance(a, TVar):
        bind(a, b, supply)
        return
    if isinstance(b, TVar):
        bind(b, a, supply)
        return

    if isinstance(a, TCon) and isinstance(b, TCon):
        if a.name != b.name or len(a.args) != len(b.args):
            raise UnifyError(f"type mismatch: expected `{show(a)}`, found `{show(b)}`",
                             _mismatch_note(a, b))
        for x, y in zip(a.args, b.args):
            unify(x, y, supply)
        return

    if isinstance(a, TRec) and isinstance(b, TRec):
        unify_records(a, b, supply)
        return

    raise UnifyError(f"type mismatch: expected `{show(a)}`, found `{show(b)}`",
                     _mismatch_note(a, b))


def _mismatch_note(a: Type, b: Type) -> str | None:
    names = {getattr(a, "name", None), getattr(b, "name", None)}
    if names == {"Int", "Float"}:
        return ("Int and Float are distinct; use `1.0` style literals and the "
                "`+. -. *. /.` operators for floats, or convert with "
                "`int_to_float` / `float_to_int`")
    if names == {"String", "Int"}:
        return "convert with `show` or `int_to_string`"
    if isinstance(a, TCon) and isinstance(b, TCon) and a.name == b.name:
        return "the type constructors match but their arguments do not"
    if isinstance(a, TCon) and a.name == "->" and not (isinstance(b, TCon) and b.name == "->"):
        return "this looks like a function applied to too few arguments"
    if isinstance(b, TCon) and b.name == "->" and not (isinstance(a, TCon) and a.name == "->"):
        return "this looks like a function applied to too many arguments"
    return None


def unify_records(a: TRec, b: TRec, supply: TypeVarSupply) -> None:
    fa, ta = flatten_record(a)
    fb, tb = flatten_record(b)

    for name in fa.keys() & fb.keys():
        try:
            unify(fa[name], fb[name], supply)
        except UnifyError as e:
            raise UnifyError(f"field `{name}` has conflicting types: {e.message}", e.note) from None

    only_a = {k: v for k, v in fa.items() if k not in fb}
    only_b = {k: v for k, v in fb.items() if k not in fa}

    ta, tb = prune(ta), prune(tb)
    closed_a = ta is EMPTY_ROW or (isinstance(ta, TCon) and ta.name == "{}")
    closed_b = tb is EMPTY_ROW or (isinstance(tb, TCon) and tb.name == "{}")

    # `a` is the expected type and `b` the one we actually found, so a field
    # present only in `a` is one the value is *missing*, and vice versa.
    if only_a and closed_b:
        names = ", ".join(f"`{k}`" for k in sorted(only_a))
        raise UnifyError(
            f"this record has no field {names}",
            f"it has {_field_summary(fb)}")
    if only_b and closed_a:
        names = ", ".join(f"`{k}`" for k in sorted(only_b))
        raise UnifyError(
            f"this record has unexpected field {names}",
            f"the expected record has {_field_summary(fa)}")

    if only_a and only_b:
        rest = supply.fresh(is_row=True)
        _bind_tail(ta, TRec(only_b, rest), supply)
        _bind_tail(tb, TRec(only_a, rest), supply)
    elif only_b:
        _bind_tail(ta, TRec(only_b, tb), supply)
    elif only_a:
        _bind_tail(tb, TRec(only_a, ta), supply)
    else:
        unify(ta, tb, supply)


def _field_summary(fields: dict[str, Type]) -> str:
    if not fields:
        return "no fields"
    return ", ".join(f"`{k}`" for k in sorted(fields))


def _bind_tail(tail: Type, rec: TRec, supply: TypeVarSupply) -> None:
    tail = prune(tail)
    if isinstance(tail, TVar):
        bind(tail, rec, supply)
    else:
        unify(tail, rec, supply)


# --------------------------------------------------------------------------
# Schemes: generalization and instantiation
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Scheme:
    vars: list[TVar]
    type: Type

    @staticmethod
    def mono(t: Type) -> "Scheme":
        return Scheme([], t)

    def __repr__(self) -> str:  # pragma: no cover
        return show_scheme(self)


def free_vars(t: Type, acc: list[TVar] | None = None, seen: set[int] | None = None) -> list[TVar]:
    acc = [] if acc is None else acc
    seen = set() if seen is None else seen
    t = prune(t)
    if isinstance(t, TVar):
        if t.id not in seen:
            seen.add(t.id)
            acc.append(t)
    elif isinstance(t, TCon):
        for a in t.args:
            free_vars(a, acc, seen)
    elif isinstance(t, TRec):
        fields, tail = flatten_record(t)
        for v in fields.values():
            free_vars(v, acc, seen)
        free_vars(tail, acc, seen)
    return acc


def generalize(t: Type, level: int) -> Scheme:
    """Quantify every unbound variable created deeper than `level`."""
    quantified = [v for v in free_vars(t) if v.level > level]
    return Scheme(quantified, t)


def instantiate(scheme: Scheme, supply: TypeVarSupply) -> Type:
    if not scheme.vars:
        return scheme.type
    mapping = {v.id: supply.fresh(v.is_row) for v in scheme.vars}
    return _substitute(scheme.type, mapping)


def _substitute(t: Type, mapping: dict[int, TVar]) -> Type:
    t = prune(t)
    if isinstance(t, TVar):
        return mapping.get(t.id, t)
    if isinstance(t, TCon):
        if not t.args:
            return t
        return TCon(t.name, [_substitute(a, mapping) for a in t.args])
    if isinstance(t, TRec):
        fields, tail = flatten_record(t)
        return TRec({k: _substitute(v, mapping) for k, v in fields.items()},
                    _substitute(tail, mapping))
    return t


# --------------------------------------------------------------------------
# Pretty printing
# --------------------------------------------------------------------------

_GREEK = "abcdefghijklmnopqrstuvwxyz"


class _Namer:
    """Assigns short, stable names (a, b, ... z, a1, b1, ...) to type variables."""

    def __init__(self) -> None:
        self.names: dict[int, str] = {}

    def name(self, var: TVar) -> str:
        got = self.names.get(var.id)
        if got is None:
            n = len(self.names)
            got = _GREEK[n % 26] + ("" if n < 26 else str(n // 26))
            self.names[var.id] = got
        return got


def show(t: Type, namer: _Namer | None = None) -> str:
    return _show(t, namer or _Namer(), 0)


def _show(t: Type, namer: _Namer, prec: int) -> str:
    t = prune(t)

    if isinstance(t, TVar):
        return namer.name(t)

    if isinstance(t, TRec):
        fields, tail = flatten_record(t)
        parts = [f"{k} : {_show(v, namer, 0)}" for k, v in sorted(fields.items())]
        tail = prune(tail)
        if isinstance(tail, TVar):
            inner = ", ".join(parts)
            return "{ " + inner + (" | " if parts else "| ") + namer.name(tail) + " }"
        return "{ " + ", ".join(parts) + " }" if parts else "{}"

    assert isinstance(t, TCon)

    if t.name == "->":
        left = _show(t.args[0], namer, 2)
        right = _show(t.args[1], namer, 1)
        s = f"{left} -> {right}"
        return f"({s})" if prec >= 2 else s

    if t.name == "Tuple":
        return "(" + ", ".join(_show(a, namer, 0) for a in t.args) + ")"

    if t.name == "List":
        return "[" + _show(t.args[0], namer, 0) + "]"

    if t.name == "{}":
        return "{}"

    if t.name.startswith("$"):  # rigid skolem from a user annotation
        return t.name[1:]

    if not t.args:
        return t.name

    s = t.name + " " + " ".join(_show(a, namer, 3) for a in t.args)
    return f"({s})" if prec >= 3 else s


def show_scheme(scheme: Scheme) -> str:
    namer = _Namer()
    for v in scheme.vars:
        namer.name(v)
    body = _show(scheme.type, namer, 0)
    if not scheme.vars:
        return body
    quant = " ".join(namer.name(v) for v in scheme.vars)
    return f"forall {quant}. {body}"


def show_pair(a: Type, b: Type) -> tuple[str, str]:
    """Render two types with a *shared* variable namer so `a` means the same thing."""
    namer = _Namer()
    return _show(a, namer, 0), _show(b, namer, 0)


# --------------------------------------------------------------------------
# Turning a UnifyError into a Diagnostic
# --------------------------------------------------------------------------


def type_error(err: UnifyError, span: Span, context: str = "",
               extra_notes: list[str] | None = None) -> Diagnostic:
    notes = list(extra_notes or ())
    if err.note:
        notes.append(err.note)
    message = f"{context}: {err.message}" if context else err.message
    return Diagnostic(message, [Label(span, "")], notes, code="E0100")
