"""Core IR to bytecode: closure conversion, upvalue resolution, emission.

The output is a `Image` — a constant pool, a function table, and a global
count — which `emit.py` serialises and the C VM loads.

**Closure conversion.** Core has lexical scope; the VM has frames and upvalue
arrays. Each `CLam` becomes a `Function` with its own slot table. A name that
is not a parameter or a local of the function being compiled is resolved by
walking outwards through the enclosing compilers: found in the immediate
parent's locals, it becomes an upvalue capturing that slot; found further out,
it becomes an upvalue capturing the parent's upvalue, recursively. This is the
standard "flat closure" scheme — every closure holds direct references to
everything it needs, so a variable lookup at runtime is one indexed load rather
than a walk up a chain of environments.

**Recursion.** A `CLetRec` binds its names to slots *before* compiling any of
the bodies, so a closure in the group can capture a sibling that has not been
built yet. The slot is filled in afterwards; because the closure captured the
slot rather than its value, it sees the final function.

**Tail calls.** `compile_expr` carries a `tail` flag. In tail position a call
emits `TAIL_CALL` instead of `CALL`, and the VM reuses the frame — that is what
makes the spec's constant-stack guarantee hold on this backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .core import (
    CApp, CCase, CCaseLit, CCon, CFail, CGetField, CGlobal, CIf, CLam, CLet,
    CLetRec, CLit, CNative, CoreProgram, CPrim, CRecord, CRecordGet,
    CRecordSet, CSeq, CTuple, CVar,
)
from .opcodes import Op, PRIM_OP
from .prims import NATIVE_ARITY, NATIVE_INDEX

#: Operands are u16, so these are the hard limits on a single function.
MAX_OPERAND = 0xFFFF


class CompileError(Exception):
    pass


# --------------------------------------------------------------------------
# Output structures
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Upvalue:
    """Where a captured variable comes from.

    `from_parent_local` distinguishes the two cases the VM must handle: capture
    the parent frame's slot, or copy the parent closure's own upvalue.
    """

    index: int
    from_parent_local: bool


@dataclass(slots=True)
class Function:
    name: str
    arity: int
    code: bytearray = field(default_factory=bytearray)
    upvalues: list[Upvalue] = field(default_factory=list)
    #: Highest number of frame slots live at once.
    max_slots: int = 0
    #: Index into `Image.functions`.
    index: int = 0


@dataclass(slots=True)
class Image:
    functions: list[Function] = field(default_factory=list)
    constants: list[tuple[str, object]] = field(default_factory=list)
    #: Sorted label lists, referenced by MAKE_RECORD / RECORD_SET.
    label_sets: list[tuple[str, ...]] = field(default_factory=list)
    globals: list[str] = field(default_factory=list)
    constructors: list[tuple[str, int, str]] = field(default_factory=list)
    entry: int = -1


# --------------------------------------------------------------------------
# Per-function compiler
# --------------------------------------------------------------------------


class FunctionCompiler:
    def __init__(self, image_builder: "Compiler", name: str, params: list[str],
                 parent: "FunctionCompiler | None") -> None:
        self.builder = image_builder
        self.parent = parent
        self.fn = Function(name=name, arity=len(params))

        #: name -> frame slot. Parameters occupy the first slots.
        self.locals: dict[str, int] = {}
        self.next_slot = 0
        for p in params:
            self.declare(p)

        #: name -> index into `self.fn.upvalues`
        self.upvalue_names: dict[str, int] = {}

    # -- slots -------------------------------------------------------------

    def declare(self, name: str) -> int:
        slot = self.next_slot
        self.next_slot += 1
        if self.next_slot > self.fn.max_slots:
            self.fn.max_slots = self.next_slot
        if slot > MAX_OPERAND:
            raise CompileError(f"function `{self.fn.name}` needs too many slots")
        self.locals[name] = slot
        return slot

    # -- emission ----------------------------------------------------------

    def emit(self, op: Op, *operands: int) -> int:
        """Append an instruction. Returns the offset of its first operand."""
        code = self.fn.code
        code.append(int(op))
        at = len(code)
        for value in operands:
            if not 0 <= value <= MAX_OPERAND:
                raise CompileError(
                    f"operand {value} for {op.name} does not fit in 16 bits")
            code.append(value & 0xFF)
            code.append((value >> 8) & 0xFF)
        return at

    def patch_jump(self, at: int) -> None:
        """Point the jump whose operand starts at `at` here."""
        target = len(self.fn.code)
        offset = target - (at + 2)
        if offset > MAX_OPERAND:
            raise CompileError("jump too far; split this function up")
        self.fn.code[at] = offset & 0xFF
        self.fn.code[at + 1] = (offset >> 8) & 0xFF

    # -- name resolution ---------------------------------------------------

    def resolve_upvalue(self, name: str) -> int | None:
        """Find `name` in an enclosing function, adding upvalues as needed."""
        if self.parent is None:
            return None

        existing = self.upvalue_names.get(name)
        if existing is not None:
            return existing

        parent_slot = self.parent.locals.get(name)
        if parent_slot is not None:
            return self._add_upvalue(name, parent_slot, from_parent_local=True)

        # Not in the immediate parent — ask it to capture from further out.
        outer = self.parent.resolve_upvalue(name)
        if outer is None:
            return None
        return self._add_upvalue(name, outer, from_parent_local=False)

    def _add_upvalue(self, name: str, index: int, from_parent_local: bool) -> int:
        self.fn.upvalues.append(Upvalue(index, from_parent_local))
        position = len(self.fn.upvalues) - 1
        self.upvalue_names[name] = position
        return position

    def load(self, name: str) -> None:
        slot = self.locals.get(name)
        if slot is not None:
            self.emit(Op.GET_LOCAL, slot)
            return
        up = self.resolve_upvalue(name)
        if up is not None:
            self.emit(Op.GET_UPVAL, up)
            return
        raise CompileError(f"unbound variable {name!r} reached the compiler")


# --------------------------------------------------------------------------
# Whole-program compiler
# --------------------------------------------------------------------------


class Compiler:
    def __init__(self, program: CoreProgram) -> None:
        self.program = program
        self.image = Image(constructors=list(program.constructors))
        self._constants: dict[tuple[str, object], int] = {}
        self._label_sets: dict[tuple[str, ...], int] = {}
        self.global_index: dict[str, int] = {}

    # -- pools -------------------------------------------------------------

    def constant(self, kind: str, value: object) -> int:
        key = (kind, value)
        got = self._constants.get(key)
        if got is not None:
            return got
        index = len(self.image.constants)
        if index > MAX_OPERAND:
            raise CompileError("too many constants in one program")
        self.image.constants.append((kind, value))
        self._constants[key] = index
        return index

    def label_set(self, labels: list[str]) -> int:
        key = tuple(labels)
        got = self._label_sets.get(key)
        if got is not None:
            return got
        index = len(self.image.label_sets)
        self.image.label_sets.append(key)
        self._label_sets[key] = index
        return index

    def global_slot(self, name: str) -> int:
        got = self.global_index.get(name)
        if got is not None:
            return got
        index = len(self.image.globals)
        self.image.globals.append(name)
        self.global_index[name] = index
        return index

    # -- program -----------------------------------------------------------

    def compile(self) -> Image:
        # Reserve a slot for every global first, so a definition may refer to
        # one that appears later in the file.
        for name, _ in self.program.globals:
            self.global_slot(name)

        top = FunctionCompiler(self, "<toplevel>", [], None)

        for name, value in self.program.globals:
            self.compile_expr(top, value, tail=False)
            top.emit(Op.SET_GLOBAL, self.global_slot(name))

        if self.program.entry is not None:
            entry_slot = self.global_index.get(self.program.entry)
            if entry_slot is None:
                raise CompileError(f"entry {self.program.entry!r} was never defined")
            top.emit(Op.GET_GLOBAL, entry_slot)
            top.emit(Op.UNIT)
            top.emit(Op.CALL, 1)
            top.emit(Op.POP)

        top.emit(Op.HALT)
        self._register(top.fn)
        self.image.entry = top.fn.index
        return self.image

    def _register(self, fn: Function) -> Function:
        fn.index = len(self.image.functions)
        self.image.functions.append(fn)
        return fn

    # -- expressions -------------------------------------------------------

    def compile_expr(self, fc: FunctionCompiler, e: object, tail: bool) -> None:
        match e:
            case CLit(kind=kind, value=value):
                self._literal(fc, kind, value)

            case CVar(name=name):
                fc.load(name)

            case CGlobal(name=name):
                fc.emit(Op.GET_GLOBAL, self.global_slot(name))

            case CLam():
                self.compile_lambda(fc, e)

            case CLet(name=name, value=value, body=body):
                self.compile_expr(fc, value, tail=False)
                slot = fc.declare(name)
                fc.emit(Op.SET_LOCAL, slot)
                self.compile_expr(fc, body, tail)

            case CLetRec(bindings=bindings, body=body):
                # Slots first, so a body can capture a sibling defined later.
                for name, _ in bindings:
                    slot = fc.declare(name)
                    fc.emit(Op.UNIT)
                    fc.emit(Op.SET_LOCAL, slot)
                for name, value in bindings:
                    self.compile_expr(fc, value, tail=False)
                    fc.emit(Op.SET_LOCAL, fc.locals[name])
                self.compile_expr(fc, body, tail)

            case CIf(cond=cond, then=then, otherwise=otherwise):
                self.compile_expr(fc, cond, tail=False)
                else_jump = fc.emit(Op.JUMP_IF_FALSE, 0)
                self.compile_expr(fc, then, tail)
                end_jump = fc.emit(Op.JUMP, 0)
                fc.patch_jump(else_jump)
                self.compile_expr(fc, otherwise, tail)
                fc.patch_jump(end_jump)

            case CSeq(first=first, second=second):
                self.compile_expr(fc, first, tail=False)
                fc.emit(Op.POP)
                self.compile_expr(fc, second, tail)

            case CApp(fn=fn, args=args):
                self.compile_expr(fc, fn, tail=False)
                for arg in args:
                    self.compile_expr(fc, arg, tail=False)
                fc.emit(Op.TAIL_CALL if tail else Op.CALL, len(args))

            case CPrim(op=op, args=args):
                for arg in args:
                    self.compile_expr(fc, arg, tail=False)
                opcode = PRIM_OP.get(op)
                if opcode is None:
                    raise CompileError(f"no opcode for primitive {op!r}")
                fc.emit(opcode)

            case CNative(name=name, args=args):
                index = NATIVE_INDEX.get(name)
                if index is None:
                    raise CompileError(f"unknown native {name!r}")
                for arg in args:
                    self.compile_expr(fc, arg, tail=False)
                fc.emit(Op.CALL_NATIVE, index, len(args))

            case CCon(name=name, con_id=con_id, args=args):
                for arg in args:
                    self.compile_expr(fc, arg, tail=False)
                fc.emit(Op.MAKE_CON, con_id, len(args))

            case CTuple(items=items):
                for item in items:
                    self.compile_expr(fc, item, tail=False)
                fc.emit(Op.MAKE_TUPLE, len(items))

            case CGetField(value=value, index=index):
                self.compile_expr(fc, value, tail=False)
                fc.emit(Op.GET_FIELD, index)

            case CCase(scrutinee=scrutinee, cases=cases, default=default):
                self.compile_case(fc, scrutinee, cases, default, tail,
                                  literal=False, kind="Int")

            case CCaseLit(scrutinee=scrutinee, cases=cases, default=default,
                          kind=kind):
                self.compile_case(fc, scrutinee, cases, default, tail,
                                  literal=True, kind=kind)

            case CRecord(labels=labels, values=values):
                for value in values:
                    self.compile_expr(fc, value, tail=False)
                fc.emit(Op.MAKE_RECORD, self.label_set(list(labels)))

            case CRecordGet(value=value, label=label):
                self.compile_expr(fc, value, tail=False)
                fc.emit(Op.RECORD_GET, self.constant("String", label))

            case CRecordSet(base=base, labels=labels, values=values):
                self.compile_expr(fc, base, tail=False)
                for value in values:
                    self.compile_expr(fc, value, tail=False)
                fc.emit(Op.RECORD_SET, self.label_set(list(labels)))

            case CFail(message=message):
                fc.emit(Op.FAIL, self.constant("String", message))

            case _:
                raise CompileError(f"cannot compile {e!r}")

        if tail and not isinstance(e, (CIf, CSeq, CLet, CLetRec, CApp,
                                       CCase, CCaseLit, CFail)):
            fc.emit(Op.RETURN)

    # -- pieces ------------------------------------------------------------

    def _literal(self, fc: FunctionCompiler, kind: str, value: object) -> None:
        if kind == "Bool":
            fc.emit(Op.TRUE if value else Op.FALSE)
        elif kind == "Unit":
            fc.emit(Op.UNIT)
        else:
            fc.emit(Op.CONST, self.constant(kind, value))

    def compile_lambda(self, fc: FunctionCompiler, lam: CLam) -> None:
        inner = FunctionCompiler(self, lam.name, list(lam.params), fc)
        self.compile_expr(inner, lam.body, tail=True)
        # `compile_expr` in tail position leaves a RETURN on every path that
        # can fall through; add one for the paths that cannot (a bare CApp
        # ends in TAIL_CALL, which the VM treats as returning).
        inner.emit(Op.RETURN)
        self._register(inner.fn)
        fc.emit(Op.CLOSURE, inner.fn.index)

    def compile_case(self, fc: FunctionCompiler, scrutinee: object,
                     cases: list, default: object | None, tail: bool,
                     literal: bool, kind: str) -> None:
        """A linear chain of tests.

        A jump table would be faster, but constructor ids are program-wide and
        so are sparse within any one match; the chain keeps the image small and
        the decision tree from `core.py` has already cut the number of tests to
        the minimum.
        """
        self.compile_expr(fc, scrutinee, tail=False)

        end_jumps: list[int] = []
        for key, body in cases:
            if literal:
                operand = self.constant(kind, key)
                test = fc.emit(Op.JUMP_IF_LIT, operand, 0)
            else:
                if key < 0:
                    # Bool is lowered to CIf by core.py, so a negative tag
                    # here means the pattern compiler produced something the
                    # backend was never taught to switch on.
                    raise CompileError(
                        f"constructor tag {key} cannot be switched on")
                test = fc.emit(Op.JUMP_IF_TAG, key, 0)
            # The jump above skips the *next* jump, which is the "no match"
            # path; falling through means this arm did not apply.
            skip = fc.emit(Op.JUMP, 0)
            fc.patch_jump(test + 2)      # the offset operand of the test
            fc.emit(Op.POP)              # discard the scrutinee
            self.compile_expr(fc, body, tail)
            if not tail:
                end_jumps.append(fc.emit(Op.JUMP, 0))
            fc.patch_jump(skip)

        fc.emit(Op.POP)
        if default is not None:
            self.compile_expr(fc, default, tail)
        else:
            fc.emit(Op.FAIL, self.constant("String", "no match arm applies"))

        for jump in end_jumps:
            fc.patch_jump(jump)


def compile_core(program: CoreProgram) -> Image:
    return Compiler(program).compile()
