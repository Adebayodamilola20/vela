"""The reference interpreter — a tree-walking evaluator over Core IR.

This is the oracle. It is not fast and does not try to be; it exists so that
every language question has one unambiguous answer, and so the bytecode VM can
be checked against something simpler than itself.

**Tail calls.** `eval` is a loop, not a recursive function. Anything in tail
position (an `if` branch, a `let` body, the last expression of a sequence, a
`case` arm) rebinds `expr` and `env` and continues around the loop rather than
recursing, so a tail-recursive Vela function runs in constant Python stack.
Non-tail calls do recurse, which is why `run` raises the recursion limit.
"""

from __future__ import annotations

import math
import sys
import time

from .core import (
    CApp, CCase, CCaseLit, CCon, CFail, CGetField, CGlobal, CIf, CLam, CLet,
    CLetRec, CLit, CNative, CoreProgram, CPrim, CRecord, CRecordGet,
    CRecordSet, CSeq, CTuple, CVar,
)
from .prims import NATIVE_ARITY
from .values import (
    Closure, Con, NativeClosure, Record, Tup, UNIT, VelaPanic, list_to_python,
    python_to_list, show, show_float, vela_compare, vela_eq, wrap_int,
)


class Env:
    """A chain of frames. Core is alpha-renamed, so names never collide."""

    __slots__ = ("vars", "parent")

    def __init__(self, parent: "Env | None" = None) -> None:
        self.vars: dict[str, object] = {}
        self.parent = parent

    def lookup(self, name: str) -> object:
        env: Env | None = self
        while env is not None:
            slot = env.vars
            if name in slot:
                return slot[name]
            env = env.parent
        raise VelaPanic(f"unbound variable {name!r}")

    def define(self, name: str, value: object) -> None:
        self.vars[name] = value


class Interpreter:
    def __init__(self, program: CoreProgram, argv: list[str] | None = None,
                 stdout=None, stderr=None) -> None:
        self.program = program
        self.globals: dict[str, object] = {}
        self.argv = argv or []
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        self.con_tags: dict[str, int] = {
            name: i for i, (name, _, _) in enumerate(program.constructors)
        }

    # ------------------------------------------------------------------
    # Program
    # ------------------------------------------------------------------

    def run(self) -> object:
        """Evaluate every top-level definition in order, then `entry`."""
        root = Env()
        for name, value in self.program.globals:
            # Globals are visible to each other, so a function defined later
            # can still be called from one defined earlier; the value is
            # forced now but its free globals resolve through `self.globals`.
            self.globals[name] = self.eval(value, root)

        if self.program.entry is None:
            return UNIT

        main = self.globals.get(self.program.entry)
        if main is None:
            raise VelaPanic(f"entry point {self.program.entry!r} is not defined")
        if isinstance(main, (Closure, NativeClosure)):
            return self.apply(main, [UNIT])
        return main

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def eval(self, expr: object, env: Env) -> object:
        while True:
            match expr:
                case CLit(value=v, kind=k):
                    if k == "Unit":
                        return UNIT
                    return v

                case CVar(name=name):
                    return env.lookup(name)

                case CGlobal(name=name):
                    if name in self.globals:
                        return self.globals[name]
                    raise VelaPanic(f"undefined global {name!r}")

                case CLam(params=params, body=body, name=name):
                    return Closure(params, body, env, name)

                case CLet(name=name, value=value, body=body):
                    inner = Env(env)
                    inner.define(name, self.eval(value, env))
                    env = inner
                    expr = body
                    continue

                case CLetRec(bindings=bindings, body=body):
                    inner = Env(env)
                    # Pre-bind so each closure captures an env in which all the
                    # group's names already exist.
                    for name, _ in bindings:
                        inner.define(name, UNIT)
                    for name, value in bindings:
                        inner.vars[name] = self.eval(value, inner)
                    env = inner
                    expr = body
                    continue

                case CIf(cond=cond, then=then, otherwise=otherwise):
                    expr = then if self.eval(cond, env) is True else otherwise
                    continue

                case CSeq(first=first, second=second):
                    self.eval(first, env)
                    expr = second
                    continue

                case CApp(fn=fn, args=args, span=span):
                    callee = self.eval(fn, env)
                    values = [self.eval(a, env) for a in args]

                    # Tail call: rebind rather than recurse.
                    if isinstance(callee, Closure):
                        supplied = callee.applied + values
                        if len(supplied) == len(callee.params):
                            inner = Env(callee.env)
                            for p, v in zip(callee.params, supplied):
                                inner.define(p, v)
                            env = inner
                            expr = callee.body
                            continue
                    try:
                        return self.apply(callee, values)
                    except VelaPanic as exc:
                        if exc.span is None:
                            exc.span = span
                        raise

                case CPrim(op=op, args=args, span=span):
                    values = [self.eval(a, env) for a in args]
                    try:
                        return self.prim(op, values)
                    except VelaPanic as exc:
                        if exc.span is None:
                            exc.span = span
                        raise

                case CNative(name=name, args=args, span=span):
                    values = [self.eval(a, env) for a in args]
                    try:
                        return self.native(name, values)
                    except VelaPanic as exc:
                        if exc.span is None:
                            exc.span = span
                        raise

                case CCon(name=name, con_id=cid, args=args):
                    return Con(name, cid, [self.eval(a, env) for a in args])

                case CTuple(items=items):
                    return Tup([self.eval(i, env) for i in items])

                case CGetField(value=value, index=index):
                    target = self.eval(value, env)
                    if isinstance(target, Con):
                        return target.fields[index]
                    if isinstance(target, Tup):
                        return target.items[index]
                    raise VelaPanic(f"cannot project field {index} from {show(target)}")

                case CCase(scrutinee=scrutinee, cases=cases, default=default):
                    value = self.eval(scrutinee, env)
                    tag = value.tag if isinstance(value, Con) else -1
                    chosen = None
                    for con_id, body in cases:
                        if con_id == tag:
                            chosen = body
                            break
                    if chosen is None:
                        if default is None:
                            raise VelaPanic("no match arm applies")
                        chosen = default
                    expr = chosen
                    continue

                case CCaseLit(scrutinee=scrutinee, cases=cases, default=default):
                    value = self.eval(scrutinee, env)
                    chosen = None
                    for key, body in cases:
                        if vela_eq(value, key):
                            chosen = body
                            break
                    if chosen is None:
                        if default is None:
                            raise VelaPanic("no match arm applies")
                        chosen = default
                    expr = chosen
                    continue

                case CRecord(labels=labels, values=values):
                    return Record({k: self.eval(v, env) for k, v in zip(labels, values)})

                case CRecordGet(value=value, label=label, span=span):
                    target = self.eval(value, env)
                    if isinstance(target, Record) and label in target.fields:
                        return target.fields[label]
                    raise VelaPanic(f"no field `{label}` on {show(target)}", span)

                case CRecordSet(base=base, labels=labels, values=values):
                    target = self.eval(base, env)
                    if not isinstance(target, Record):
                        raise VelaPanic(f"cannot update {show(target)}")
                    fields = dict(target.fields)
                    for k, v in zip(labels, values):
                        fields[k] = self.eval(v, env)
                    return Record(fields)

                case CFail(message=message, span=span):
                    raise VelaPanic(message, span)

            raise VelaPanic(f"cannot evaluate {expr!r}")

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------

    def apply(self, callee: object, args: list[object]) -> object:
        """Apply a callable, handling partial application and oversupply."""
        while True:
            if isinstance(callee, Closure):
                supplied = callee.applied + args
                need = len(callee.params)
                if len(supplied) < need:
                    return Closure(callee.params, callee.body, callee.env,
                                   callee.name, supplied)
                inner = Env(callee.env)
                for p, v in zip(callee.params, supplied[:need]):
                    inner.define(p, v)
                result = self.eval(callee.body, inner)
                extra = supplied[need:]
                if not extra:
                    return result
                # The function returned another function and we still have
                # arguments in hand — keep going.
                callee, args = result, extra
                continue

            if isinstance(callee, NativeClosure):
                supplied = callee.applied + args
                if len(supplied) < callee.arity:
                    return NativeClosure(callee.name, callee.arity, supplied)
                result = self.native(callee.name, supplied[:callee.arity])
                extra = supplied[callee.arity:]
                if not extra:
                    return result
                callee, args = result, extra
                continue

            raise VelaPanic(f"{show(callee)} is not a function")

    # ------------------------------------------------------------------
    # Inline primitives
    # ------------------------------------------------------------------

    def prim(self, op: str, a: list[object]) -> object:
        match op:
            case "int_add":
                return wrap_int(a[0] + a[1])
            case "int_sub":
                return wrap_int(a[0] - a[1])
            case "int_mul":
                return wrap_int(a[0] * a[1])
            case "int_div":
                if a[1] == 0:
                    raise VelaPanic("division by zero")
                # Truncate toward zero, as C does — Python floors instead.
                return wrap_int(int(a[0] / a[1]) if (a[0] < 0) != (a[1] < 0)
                                else a[0] // a[1])
            case "int_mod":
                if a[1] == 0:
                    raise VelaPanic("modulo by zero")
                return wrap_int(a[0] - a[1] * int(a[0] / a[1]))
            case "int_pow":
                if a[1] < 0:
                    raise VelaPanic("negative exponent")
                return wrap_int(a[0] ** a[1])
            case "int_neg":
                return wrap_int(-a[0])
            case "float_add":
                return a[0] + a[1]
            case "float_sub":
                return a[0] - a[1]
            case "float_mul":
                return a[0] * a[1]
            case "float_div":
                return _float_div(a[0], a[1])
            case "float_neg":
                return -a[0]
            case "eq":
                return vela_eq(a[0], a[1])
            case "ne":
                return not vela_eq(a[0], a[1])
            case "lt":
                return vela_compare(a[0], a[1]) < 0
            case "le":
                return vela_compare(a[0], a[1]) <= 0
            case "gt":
                return vela_compare(a[0], a[1]) > 0
            case "ge":
                return vela_compare(a[0], a[1]) >= 0
            case "bool_not":
                return not a[0]
            case "string_concat":
                return a[0] + a[1]
            case "list_append":
                return python_to_list(list_to_python(a[0]) + list_to_python(a[1]))
        raise VelaPanic(f"unknown primitive {op!r}")

    # ------------------------------------------------------------------
    # Natives
    # ------------------------------------------------------------------

    def native(self, name: str, a: list[object]) -> object:
        match name:
            case "print":
                self.stdout.write(_text(a[0]) + "\n")
                return UNIT
            case "print_err":
                self.stderr.write(_text(a[0]) + "\n")
                return UNIT
            case "show":
                return show(a[0])

            # -- conversions ------------------------------------------------
            case "int_to_string":
                return str(a[0])
            case "float_to_string":
                return show_float(a[0])
            case "string_to_int":
                return _parse_int(a[0])
            case "string_to_float":
                return _parse_float(a[0])
            case "int_to_float":
                return float(a[0])
            case "float_to_int":
                if a[0] != a[0] or a[0] in (float("inf"), float("-inf")):
                    raise VelaPanic("cannot convert this float to an int")
                return wrap_int(int(a[0]))

            # -- strings ----------------------------------------------------
            case "string_length":
                return len(a[0])
            case "string_get":
                s, i = a[0], a[1]
                if i < 0 or i >= len(s):
                    raise VelaPanic(f"string index {i} out of bounds (length {len(s)})")
                return s[i]
            case "string_slice":
                s, start, end = a[0], a[1], a[2]
                start = max(0, min(start, len(s)))
                end = max(start, min(end, len(s)))
                return s[start:end]
            case "string_index_of":
                return a[0].find(a[1])
            case "string_split":
                s, sep = a[0], a[1]
                parts = list(s) if sep == "" else s.split(sep)
                return python_to_list(parts)
            case "string_join":
                return a[1].join(list_to_python(a[0]))
            case "string_from_code":
                return chr(a[0])
            case "string_code_at":
                s, i = a[0], a[1]
                if i < 0 or i >= len(s):
                    raise VelaPanic(f"string index {i} out of bounds (length {len(s)})")
                return ord(s[i])
            case "string_trim":
                return a[0].strip()
            case "string_upper":
                return a[0].upper()
            case "string_lower":
                return a[0].lower()

            # -- floats -----------------------------------------------------
            case "float_sqrt":
                return math.sqrt(a[0]) if a[0] >= 0 else float("nan")
            case "float_floor":
                return math.floor(a[0]) * 1.0
            case "float_ceil":
                return math.ceil(a[0]) * 1.0
            case "float_abs":
                return abs(a[0])
            case "float_sin":
                return math.sin(a[0])
            case "float_cos":
                return math.cos(a[0])
            case "float_tan":
                return math.tan(a[0])
            case "float_exp":
                return math.exp(a[0])
            case "float_log":
                if a[0] <= 0:
                    return float("-inf") if a[0] == 0 else float("nan")
                return math.log(a[0])
            case "float_pow":
                return math.pow(a[0], a[1])

            # -- ints -------------------------------------------------------
            case "int_abs":
                return wrap_int(abs(a[0]))
            case "int_min":
                return min(a[0], a[1])
            case "int_max":
                return max(a[0], a[1])
            case "int_shl":
                return wrap_int(a[0] << (a[1] & 63))
            case "int_shr":
                return wrap_int(a[0] >> (a[1] & 63))
            case "int_and":
                return wrap_int(a[0] & a[1])
            case "int_or":
                return wrap_int(a[0] | a[1])
            case "int_xor":
                return wrap_int(a[0] ^ a[1])

            case "compare":
                return vela_compare(a[0], a[1])
            case "panic":
                raise VelaPanic(_text(a[0]))

            # -- arrays -----------------------------------------------------
            case "array_new":
                n = a[0]
                if n < 0:
                    raise VelaPanic(f"cannot create an array of length {n}")
                return Tup([a[1]] * n)
            case "array_get":
                arr, i = a[0], a[1]
                if not isinstance(arr, Tup):
                    raise VelaPanic("array_get expects an array")
                if i < 0 or i >= len(arr.items):
                    raise VelaPanic(f"array index {i} out of bounds "
                                    f"(length {len(arr.items)})")
                return arr.items[i]
            case "array_set":
                arr, i, v = a[0], a[1], a[2]
                if not isinstance(arr, Tup):
                    raise VelaPanic("array_set expects an array")
                if i < 0 or i >= len(arr.items):
                    raise VelaPanic(f"array index {i} out of bounds "
                                    f"(length {len(arr.items)})")
                items = list(arr.items)
                items[i] = v
                return Tup(items)
            case "array_length":
                return len(a[0].items) if isinstance(a[0], Tup) else 0
            case "array_from_list":
                return Tup(list_to_python(a[0]))
            case "array_to_list":
                return python_to_list(list(a[0].items))

            # -- environment -------------------------------------------------
            case "clock_ms":
                return wrap_int(int(time.time() * 1000))
            case "read_line":
                line = sys.stdin.readline()
                return line[:-1] if line.endswith("\n") else line
            case "arg_count":
                return len(self.argv)
            case "arg_get":
                i = a[0]
                return self.argv[i] if 0 <= i < len(self.argv) else ""

        raise VelaPanic(f"unknown native {name!r}")


def _float_div(x: float, y: float) -> float:
    """IEEE-754 division, including the infinities Python raises on."""
    if y == 0.0:
        if x == 0.0 or x != x:
            return float("nan")
        negative = (x < 0) != (math.copysign(1.0, y) < 0)
        return float("-inf") if negative else float("inf")
    return x / y


def _parse_int(s: str) -> object:
    try:
        return wrap_int(int(s.strip(), 10))
    except ValueError:
        raise VelaPanic(f"cannot parse {s!r} as an Int") from None


def _parse_float(s: str) -> object:
    try:
        return float(s.strip())
    except ValueError:
        raise VelaPanic(f"cannot parse {s!r} as a Float") from None


def _text(v: object) -> str:
    """`print` writes a String as-is and anything else via `show`."""
    return v if isinstance(v, str) else show(v)


def run_program(program: CoreProgram, argv: list[str] | None = None,
                stdout=None, stderr=None, recursion_limit: int = 20000) -> object:
    """Convenience entry point used by the CLI and the test runner."""
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(max(previous, recursion_limit))
    try:
        return Interpreter(program, argv, stdout, stderr).run()
    finally:
        sys.setrecursionlimit(previous)
