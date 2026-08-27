"""Runtime values, shared by the reference interpreter and the bytecode VM.

Both backends must render and compare values identically — the differential
tests diff their stdout byte for byte — so the formatting and ordering rules
live here rather than being reimplemented on each side.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# 64-bit integers
# --------------------------------------------------------------------------

_INT_MASK = (1 << 64) - 1
_INT_SIGN = 1 << 63


def wrap_int(n: int) -> int:
    """Reduce a Python int to 64-bit two's complement, as the spec requires.

    Python ints are arbitrary precision, so without this the interpreter would
    quietly disagree with the C VM the moment anything overflowed.
    """
    n &= _INT_MASK
    return n - (1 << 64) if n & _INT_SIGN else n


# --------------------------------------------------------------------------
# Value types
# --------------------------------------------------------------------------


class Unit:
    """The single value of type `Unit`."""

    __slots__ = ()
    _instance: "Unit | None" = None

    def __new__(cls) -> "Unit":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "()"


UNIT = Unit()


@dataclass(slots=True)
class Con:
    """A data value: a constructor tag plus its fields."""

    name: str
    tag: int
    fields: list[object] = field(default_factory=list)


@dataclass(slots=True)
class Tup:
    items: list[object]


@dataclass(slots=True)
class Record:
    """Field values keyed by label. Labels are kept sorted for stable output."""

    fields: dict[str, object]


@dataclass(slots=True)
class Closure:
    params: list[str]
    body: object                  # Core
    env: object                   # interp.Env
    name: str = "<anonymous>"

    #: Arguments already supplied by a partial application.
    applied: list[object] = field(default_factory=list)


@dataclass(slots=True)
class NativeClosure:
    """A native awaiting more arguments, from partial application."""

    name: str
    arity: int
    applied: list[object] = field(default_factory=list)


class VelaPanic(Exception):
    """A runtime error raised by `panic`, a failed match, or a bad primitive."""

    def __init__(self, message: str, span: object = None) -> None:
        super().__init__(message)
        self.message = message
        self.span = span


# --------------------------------------------------------------------------
# Lists
# --------------------------------------------------------------------------

NIL_TAG = 0
CONS_TAG = 1


def is_nil(v: object) -> bool:
    return isinstance(v, Con) and v.name == "Nil"


def is_cons(v: object) -> bool:
    return isinstance(v, Con) and v.name == "Cons"


def list_to_python(v: object) -> list[object]:
    """Flatten a `Cons`/`Nil` chain. Raises if it is not a proper list."""
    out: list[object] = []
    while True:
        if is_nil(v):
            return out
        if is_cons(v):
            out.append(v.fields[0])
            v = v.fields[1]
            continue
        raise VelaPanic("not a list")


def python_to_list(items: list[object], nil: Con | None = None) -> object:
    acc: object = nil if nil is not None else Con("Nil", NIL_TAG, [])
    for item in reversed(items):
        acc = Con("Cons", CONS_TAG, [item, acc])
    return acc


# --------------------------------------------------------------------------
# Equality and ordering
# --------------------------------------------------------------------------


def vela_eq(a: object, b: object) -> bool:
    """Structural equality.

    Functions are not comparable — the type checker rejects that, so reaching
    here with one is a compiler bug rather than a user error.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if isinstance(a, int) and isinstance(b, int):
        return a == b
    if isinstance(a, float) and isinstance(b, float):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if a is UNIT and b is UNIT:
        return True
    if isinstance(a, Con) and isinstance(b, Con):
        return (a.name == b.name
                and len(a.fields) == len(b.fields)
                and all(vela_eq(x, y) for x, y in zip(a.fields, b.fields)))
    if isinstance(a, Tup) and isinstance(b, Tup):
        return (len(a.items) == len(b.items)
                and all(vela_eq(x, y) for x, y in zip(a.items, b.items)))
    if isinstance(a, Record) and isinstance(b, Record):
        if set(a.fields) != set(b.fields):
            return False
        return all(vela_eq(a.fields[k], b.fields[k]) for k in a.fields)
    if isinstance(a, (Closure, NativeClosure)) or isinstance(b, (Closure, NativeClosure)):
        raise VelaPanic("cannot compare functions")
    return False


def vela_compare(a: object, b: object) -> int:
    """Total order: -1, 0 or 1.

    Constructors order by tag, then field-wise; tuples and records order
    lexicographically, records by sorted label.
    """
    if isinstance(a, bool) and isinstance(b, bool):
        return _cmp(int(a), int(b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return _cmp(a, b)
    if isinstance(a, str) and isinstance(b, str):
        return _cmp(a, b)
    if a is UNIT and b is UNIT:
        return 0
    if isinstance(a, Con) and isinstance(b, Con):
        if a.tag != b.tag:
            return _cmp(a.tag, b.tag)
        for x, y in zip(a.fields, b.fields):
            c = vela_compare(x, y)
            if c:
                return c
        return _cmp(len(a.fields), len(b.fields))
    if isinstance(a, Tup) and isinstance(b, Tup):
        for x, y in zip(a.items, b.items):
            c = vela_compare(x, y)
            if c:
                return c
        return _cmp(len(a.items), len(b.items))
    if isinstance(a, Record) and isinstance(b, Record):
        for key in sorted(set(a.fields) | set(b.fields)):
            if key not in a.fields:
                return -1
            if key not in b.fields:
                return 1
            c = vela_compare(a.fields[key], b.fields[key])
            if c:
                return c
        return 0
    raise VelaPanic("cannot compare these values")


def _cmp(a, b) -> int:
    return -1 if a < b else (1 if a > b else 0)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def show_float(f: float) -> str:
    """Render a float so both backends agree.

    Python's `repr` gives the shortest round-tripping form, which is what we
    want, but it prints integral values as `3.0` and infinities as `inf`;
    normalise the special cases explicitly.
    """
    if f != f:
        return "NaN"
    if f == float("inf"):
        return "Infinity"
    if f == float("-inf"):
        return "-Infinity"
    out = repr(f)
    if out.endswith(".0"):
        return out
    if "e" in out or "." in out:
        return out
    return out + ".0"


_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\0": "\\0",
}


def quote_string(s: str) -> str:
    out = ['"']
    for ch in s:
        esc = _ESCAPES.get(ch)
        out.append(esc if esc is not None else ch)
    out.append('"')
    return "".join(out)


def show(v: object, quoted: bool = True) -> str:
    """Render a value the way `show` does.

    `quoted` controls only the *outermost* string: `print (show "hi")` writes
    `"hi"` with quotes, while nested strings are always quoted so that
    `show ["a"]` is unambiguous.
    """
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return show_float(v)
    if isinstance(v, str):
        return quote_string(v) if quoted else v
    if v is UNIT:
        return "()"
    if isinstance(v, Con):
        if is_nil(v) or is_cons(v):
            try:
                items = list_to_python(v)
            except VelaPanic:
                items = None
            if items is not None:
                return "[" + ", ".join(show(i) for i in items) + "]"
        if not v.fields:
            return v.name
        return v.name + " " + " ".join(_show_atom(f) for f in v.fields)
    if isinstance(v, Tup):
        return "(" + ", ".join(show(i) for i in v.items) + ")"
    if isinstance(v, Record):
        inner = ", ".join(f"{k} = {show(v.fields[k])}" for k in sorted(v.fields))
        return "{ " + inner + " }" if inner else "{}"
    if isinstance(v, Closure):
        return f"<fn {v.name}>"
    if isinstance(v, NativeClosure):
        return f"<native {v.name}>"
    return str(v)


def _show_atom(v: object) -> str:
    """`show`, parenthesised when it would otherwise be ambiguous as an argument."""
    text = show(v)
    if isinstance(v, Con) and v.fields and not (is_cons(v) or is_nil(v)):
        return f"({text})"
    if isinstance(v, (int, float)) and not isinstance(v, bool) and text.startswith("-"):
        return f"({text})"
    return text
