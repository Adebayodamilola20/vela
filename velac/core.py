"""Core IR and the lowering pass from the surface AST.

Core is deliberately small — no operators, no records-by-position, no nested
patterns — so that both the reference interpreter and the bytecode compiler
can stay simple.

Two things happen here that are worth calling out:

**Alpha renaming.**  Every local binder is given a globally unique name
(`x#17`). Shadowing therefore does not exist in Core, which makes the later
closure conversion a straight lookup.

**Pattern matching becomes a decision tree.**  Nested patterns are compiled
with the scheme from Maranget's "Compiling pattern matching to good decision
trees": pick a column, switch on its constructors, recurse. Each `match` arm
is emitted exactly once — arms reached from more than one leaf of the tree are
hoisted into a local *join function* and the leaves call it, which keeps code
size linear instead of exponential.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count

from . import ast as A
from .errors import Span
from .prims import BINOP_PRIM, UNOP_PRIM
from .registry import Registry

# ==========================================================================
# Core IR
# ==========================================================================


class Core:
    __slots__ = ()


@dataclass(slots=True)
class CLit(Core):
    kind: str          # "Int" | "Float" | "String" | "Bool" | "Unit"
    value: object


@dataclass(slots=True)
class CVar(Core):
    name: str


@dataclass(slots=True)
class CGlobal(Core):
    name: str


@dataclass(slots=True)
class CLam(Core):
    params: list[str]
    body: Core
    name: str = "<anonymous>"


@dataclass(slots=True)
class CApp(Core):
    fn: Core
    args: list[Core]
    span: Span | None = None


@dataclass(slots=True)
class CLet(Core):
    name: str
    value: Core
    body: Core


@dataclass(slots=True)
class CLetRec(Core):
    bindings: list[tuple[str, Core]]
    body: Core


@dataclass(slots=True)
class CIf(Core):
    cond: Core
    then: Core
    otherwise: Core


@dataclass(slots=True)
class CPrim(Core):
    op: str
    args: list[Core]
    span: Span | None = None


@dataclass(slots=True)
class CNative(Core):
    name: str
    args: list[Core]
    span: Span | None = None


@dataclass(slots=True)
class CCon(Core):
    """Build a data value. `con_id` indexes the program-wide constructor table."""
    name: str
    con_id: int
    args: list[Core]


@dataclass(slots=True)
class CTuple(Core):
    items: list[Core]


@dataclass(slots=True)
class CGetField(Core):
    """Field `index` of a data value or tuple."""
    value: Core
    index: int


@dataclass(slots=True)
class CCase(Core):
    """Switch on the constructor id of a data value."""
    scrutinee: Core
    cases: list[tuple[int, Core]]      # (con_id, body)
    default: Core | None


@dataclass(slots=True)
class CCaseLit(Core):
    """Switch on a literal value (Int / Float / String)."""
    scrutinee: Core
    cases: list[tuple[object, Core]]
    default: Core | None
    kind: str = "Int"


@dataclass(slots=True)
class CRecord(Core):
    labels: list[str]                  # sorted
    values: list[Core]


@dataclass(slots=True)
class CRecordGet(Core):
    value: Core
    label: str
    span: Span | None = None


@dataclass(slots=True)
class CRecordSet(Core):
    """Functional update: copy `base`, overwriting `labels`."""
    base: Core
    labels: list[str]
    values: list[Core]


@dataclass(slots=True)
class CFail(Core):
    message: str
    span: Span | None = None


@dataclass(slots=True)
class CSeq(Core):
    first: Core
    second: Core


@dataclass(slots=True)
class CoreProgram:
    """A whole program: constructor table plus top-level definitions."""

    globals: list[tuple[str, Core]] = field(default_factory=list)
    constructors: list[tuple[str, int, str]] = field(default_factory=list)  # name, arity, type
    entry: str | None = None


# ==========================================================================
# Patterns used by the match compiler (variable names preserved)
# ==========================================================================


class KPat:
    __slots__ = ()


@dataclass(slots=True)
class KWild(KPat):
    pass


@dataclass(slots=True)
class KVar(KPat):
    name: str


@dataclass(slots=True)
class KAs(KPat):
    name: str
    sub: KPat


@dataclass(slots=True)
class KCon(KPat):
    name: str
    con_id: int
    args: list[KPat]


@dataclass(slots=True)
class KTuple(KPat):
    args: list[KPat]


@dataclass(slots=True)
class KRecord(KPat):
    fields: dict[str, KPat]


@dataclass(slots=True)
class KLit(KPat):
    kind: str
    value: object


KWILD = KWild()


@dataclass(slots=True)
class Row:
    pats: list[KPat]
    bindings: list[tuple[str, str]]    # (surface name, occurrence temp)
    guard: A.Expr | None
    arm: int


# ==========================================================================
# Lowering
# ==========================================================================


class Lowerer:
    def __init__(self, registry: Registry) -> None:
        self.reg = registry
        self._ids = count()
        self.scopes: list[dict[str, str]] = []
        self.globals: dict[str, str] = {}      # qualified surface name -> global id
        self.con_ids: dict[str, int] = {}
        self.constructors: list[tuple[str, int, str]] = []
        self._register_constructors()

    # -- names -------------------------------------------------------------

    def fresh(self, hint: str = "t") -> str:
        return f"{hint}#{next(self._ids)}"

    def push_scope(self) -> None:
        self.scopes.append({})

    def pop_scope(self) -> None:
        self.scopes.pop()

    def bind_local(self, surface: str) -> str:
        unique = self.fresh(surface)
        self.scopes[-1][surface] = unique
        return unique

    def lookup_local(self, name: str) -> str | None:
        for scope in reversed(self.scopes):
            got = scope.get(name)
            if got is not None:
                return got
        return None

    # -- constructor table -------------------------------------------------

    def _register_constructors(self) -> None:
        """Assign a program-wide id to every constructor, builtins first."""
        for name in ("Nil", "Cons"):
            self._add_con(name)

    def _add_con(self, name: str) -> int:
        got = self.con_ids.get(name)
        if got is not None:
            return got
        info = self.reg.cons[name]
        con_id = len(self.constructors)
        self.con_ids[name] = con_id
        self.constructors.append((name, info.arity, info.type_name))
        return con_id

    def register_module_types(self, module: A.Module) -> None:
        for decl in module.decls:
            if isinstance(decl, A.DType):
                for con in decl.constructors:
                    self._add_con(con.name)

    def con_id(self, name: str) -> int:
        got = self.con_ids.get(name)
        return got if got is not None else self._add_con(name)

    # -- modules -----------------------------------------------------------

    def lower_module(self, module: A.Module, prefix: str,
                     imports: dict[str, str]) -> list[tuple[str, Core]]:
        """Lower one module's value declarations to global definitions.

        `imports` maps a qualified surface name (`List.map`) to the global id
        it was given when that module was lowered.
        """
        self.module_prefix = prefix
        self.imports = imports
        out: list[tuple[str, Core]] = []

        for decl in module.decls:
            if isinstance(decl, A.DExtern):
                gid = f"{prefix}.{decl.name}"
                self.globals[f"{prefix}.{decl.name}"] = gid
                out.append((gid, self._native_wrapper(decl)))
                continue

            if not isinstance(decl, A.DLet):
                continue

            # Pre-register the group's names so recursive references resolve.
            for b in decl.bindings:
                self.globals[f"{prefix}.{b.name}"] = f"{prefix}.{b.name}"

            for b in decl.bindings:
                self.push_scope()
                value = self.lower_binding(b)
                self.pop_scope()
                out.append((f"{prefix}.{b.name}", value))

        return out

    def _native_wrapper(self, decl: A.DExtern) -> Core:
        """`extern f : A -> B = "prim"` becomes `\\a -> <native prim> a`."""
        from .prims import NATIVE_ARITY

        arity = NATIVE_ARITY.get(decl.prim)
        if arity is None:
            arity = 1
        params = [self.fresh("a") for _ in range(arity)]
        body = CNative(decl.prim, [CVar(p) for p in params], decl.span)
        return CLam(params, body, decl.name)

    def lower_binding(self, b: A.Binding) -> Core:
        if not b.params:
            return self.lower(b.body)
        params = [self.bind_local_pattern_param(p) for p in b.params]
        body = self.lower(b.body)
        body = self._destructure_params(b.params, params, body)
        return CLam([p[0] for p in params], body, b.name)

    def bind_local_pattern_param(self, p: A.Pattern) -> tuple[str, A.Pattern | None]:
        """Give a parameter a name; complex patterns are destructured in the body."""
        if isinstance(p, A.PVar):
            return self.bind_local(p.name), None
        if isinstance(p, A.PWild):
            return self.fresh("_"), None
        return self.fresh("arg"), p

    def _destructure_params(self, patterns: list[A.Pattern],
                            params: list[tuple[str, A.Pattern | None]], body: Core) -> Core:
        for (name, pat) in reversed(params):
            if pat is not None:
                body = self.compile_match(CVar(name), [A.MatchArm(pat, None, pat.span, None)],
                                          pat.span, bodies=[body])
        return body

    # -- expressions -------------------------------------------------------

    def lower(self, e: A.Expr) -> Core:
        match e:
            case A.ELit(value=v, kind=k):
                return CLit(k, v)

            case A.EVar(name=name, module=module, span=span):
                if module is None:
                    local = self.lookup_local(name)
                    if local is not None:
                        return CVar(local)
                key = f"{module}.{name}" if module else f"{self.module_prefix}.{name}"
                gid = self.globals.get(key) or self.imports.get(key)
                if gid is None and module is None:
                    # fall back to an auto-imported (prelude) name
                    gid = self.imports.get(name)
                if gid is None:
                    raise AssertionError(f"unresolved name {key!r} reached lowering")
                return CGlobal(gid)

            case A.ECon(name=name):
                info = self.reg.cons[name]
                cid = self.con_id(name)
                if name == "True":
                    return CLit("Bool", True)
                if name == "False":
                    return CLit("Bool", False)
                if name == "()":
                    return CLit("Unit", None)
                if info.arity == 0:
                    return CCon(name, cid, [])
                params = [self.fresh("c") for _ in range(info.arity)]
                return CLam(params, CCon(name, cid, [CVar(p) for p in params]), name)

            case A.ELambda(params=params, body=body):
                self.push_scope()
                bound = [self.bind_local_pattern_param(p) for p in params]
                core_body = self.lower(body)
                core_body = self._destructure_params(params, bound, core_body)
                self.pop_scope()
                return CLam([b[0] for b in bound], core_body)

            case A.EApp(fn=callee, args=args, span=span):
                return CApp(self.lower(callee), [self.lower(a) for a in args], span)

            case A.EBinOp(op=op, lhs=lhs, rhs=rhs, span=span):
                return self.lower_binop(op, lhs, rhs, span)

            case A.EUnOp(op=op, operand=operand, span=span):
                return CPrim(UNOP_PRIM[op], [self.lower(operand)], span)

            case A.EIf(cond=cond, then=then, otherwise=otherwise):
                return CIf(self.lower(cond), self.lower(then), self.lower(otherwise))

            case A.EAnd(lhs=lhs, rhs=rhs):
                return CIf(self.lower(lhs), self.lower(rhs), CLit("Bool", False))

            case A.EOr(lhs=lhs, rhs=rhs):
                return CIf(self.lower(lhs), CLit("Bool", True), self.lower(rhs))

            case A.ESeq(first=first, second=second):
                return CSeq(self.lower(first), self.lower(second))

            case A.ELet(bindings=bindings, body=body, recursive=recursive):
                return self.lower_let(bindings, body, recursive)

            case A.ELetPattern(pattern=pattern, value=value, body=body, span=span):
                scrutinee = self.lower(value)
                self.push_scope()
                arm = A.MatchArm(pattern, None, span, None)
                inner = self.lower(body)
                result = self.compile_match(scrutinee, [arm], span, bodies=[inner])
                self.pop_scope()
                return result

            case A.EMatch(scrutinee=scrutinee, arms=arms, span=span):
                return self.compile_match(self.lower(scrutinee), arms, span)

            case A.ETuple(items=items):
                return CTuple([self.lower(i) for i in items])

            case A.EList(items=items):
                acc: Core = CCon("Nil", self.con_id("Nil"), [])
                for item in reversed(items):
                    acc = CCon("Cons", self.con_id("Cons"), [self.lower(item), acc])
                return acc

            case A.ERecord(fields=fields, base=base):
                pairs = sorted(fields, key=lambda kv: kv[0])
                labels = [k for k, _ in pairs]
                values = [self.lower(v) for _, v in pairs]
                if base is None:
                    return CRecord(labels, values)
                return CRecordSet(self.lower(base), labels, values)

            case A.EField(record=record, name=name, span=span):
                return CRecordGet(self.lower(record), name, span)

            case A.EAnnot(expr=inner):
                return self.lower(inner)

        raise AssertionError(f"cannot lower {e!r}")

    def lower_binop(self, op: str, lhs: A.Expr, rhs: A.Expr, span: Span) -> Core:
        left, right = self.lower(lhs), self.lower(rhs)
        if op == "::":
            return CCon("Cons", self.con_id("Cons"), [left, right])
        if op == ">>":
            x = self.fresh("x")
            f, g = self.fresh("f"), self.fresh("g")
            return CLet(f, left, CLet(g, right,
                                      CLam([x], CApp(CVar(g), [CApp(CVar(f), [CVar(x)], span)],
                                                     span))))
        if op == "<<":
            x = self.fresh("x")
            f, g = self.fresh("f"), self.fresh("g")
            return CLet(f, left, CLet(g, right,
                                      CLam([x], CApp(CVar(f), [CApp(CVar(g), [CVar(x)], span)],
                                                     span))))
        return CPrim(BINOP_PRIM[op], [left, right], span)

    def lower_let(self, bindings: list[A.Binding], body: A.Expr, recursive: bool) -> Core:
        if recursive:
            self.push_scope()
            names = [self.bind_local(b.name) for b in bindings]
            values = []
            for b in bindings:
                self.push_scope()
                values.append(self.lower_binding(b))
                self.pop_scope()
            core_body = self.lower(body)
            self.pop_scope()
            return CLetRec(list(zip(names, values)), core_body)

        # Non-recursive lets bind left to right; each value sees the previous.
        pairs: list[tuple[str, Core]] = []
        self.push_scope()
        for b in bindings:
            self.push_scope()
            value = self.lower_binding(b)
            self.pop_scope()
            pairs.append((self.bind_local(b.name), value))
        core_body = self.lower(body)
        self.pop_scope()
        for name, value in reversed(pairs):
            core_body = CLet(name, value, core_body)
        return core_body

    # ------------------------------------------------------------------
    # Match compilation
    # ------------------------------------------------------------------

    def compile_match(self, scrutinee: Core, arms: list[A.MatchArm], span: Span,
                      bodies: list[Core] | None = None) -> Core:
        """Compile `match scrutinee { arms }` into a decision tree.

        `bodies` lets callers (parameter destructuring, `let pat = ...`) supply
        already-lowered arm bodies instead of surface expressions.
        """
        root = self.fresh("scrut")

        # Canonical variable order per arm, so every leaf binds them alike.
        arm_vars = [[n for n, _ in A.pattern_vars(a.pattern)] for a in arms]

        # Lower each arm body in a scope where its pattern variables are bound
        # to fresh names; the decision tree binds those names to occurrences.
        arm_renames: list[dict[str, str]] = []
        arm_bodies: list[Core] = []
        for i, arm in enumerate(arms):
            renames = {v: self.fresh(v) for v in arm_vars[i]}
            arm_renames.append(renames)
            if bodies is not None:
                arm_bodies.append(bodies[i])
            else:
                self.push_scope()
                self.scopes[-1].update(renames)
                arm_bodies.append(self.lower(arm.body))
                self.pop_scope()

        arm_guards: list[Core | None] = []
        for i, arm in enumerate(arms):
            if arm.guard is None:
                arm_guards.append(None)
            else:
                self.push_scope()
                self.scopes[-1].update(arm_renames[i])
                arm_guards.append(self.lower(arm.guard))
                self.pop_scope()

        rows = [Row([self.to_kpat(arm.pattern, arm_renames[i])], [], arm.guard, i)
                for i, arm in enumerate(arms)]

        tree = self._compile_rows([root], rows)

        # Count how often each arm is reached; single-use arms get inlined.
        uses: dict[int, int] = {}
        _count_leaves(tree, uses)

        joins: dict[int, str] = {}
        join_defs: list[tuple[str, Core]] = []
        for i, body in enumerate(arm_bodies):
            if uses.get(i, 0) > 1:
                name = self.fresh(f"join{i}")
                joins[i] = name
                params = [arm_renames[i][v] for v in arm_vars[i]] or [self.fresh("_u")]
                join_defs.append((name, CLam(params, body, name)))

        result = self._emit(tree, arm_bodies, arm_guards, arm_renames, arm_vars, joins, span)
        for name, lam in reversed(join_defs):
            result = CLet(name, lam, result)
        return CLet(root, scrutinee, result)

    # -- surface pattern -> KPat ------------------------------------------

    def to_kpat(self, p: A.Pattern, renames: dict[str, str]) -> KPat:
        match p:
            case A.PWild():
                return KWILD
            case A.PVar(name=n):
                return KVar(renames[n])
            case A.PAs(pattern=inner, name=n):
                return KAs(renames[n], self.to_kpat(inner, renames))
            case A.PLit(value=v, kind=k):
                if k == "Bool":
                    return KCon("True" if v else "False", -1, [])
                if k == "Unit":
                    return KWILD  # Unit has a single value; nothing to test
                return KLit(k, v)
            case A.PCon(name=n, args=args):
                if n == "True" or n == "False":
                    return KCon(n, -1, [])
                if n == "()":
                    return KWILD
                return KCon(n, self.con_id(n), [self.to_kpat(a, renames) for a in args])
            case A.PTuple(items=items):
                return KTuple([self.to_kpat(i, renames) for i in items])
            case A.PList(items=items):
                acc: KPat = KCon("Nil", self.con_id("Nil"), [])
                for item in reversed(items):
                    acc = KCon("Cons", self.con_id("Cons"),
                               [self.to_kpat(item, renames), acc])
                return acc
            case A.PCons(head=h, tail=t):
                return KCon("Cons", self.con_id("Cons"),
                            [self.to_kpat(h, renames), self.to_kpat(t, renames)])
            case A.PRecord(fields=fields):
                return KRecord({k: self.to_kpat(v, renames) for k, v in fields})
        raise AssertionError(f"cannot lower pattern {p!r}")

    # -- the decision tree -------------------------------------------------

    def _compile_rows(self, occs: list[str], rows: list[Row]) -> "Tree":
        rows = [self._simplify(r, occs) for r in rows]

        if not rows:
            return TFail()

        first = rows[0]
        if all(isinstance(p, KWild) for p in first.pats):
            if first.guard is None:
                return TLeaf(first.arm, list(first.bindings))
            return TGuard(first.arm, list(first.bindings),
                          self._compile_rows(occs, rows[1:]))

        col = next(i for i, p in enumerate(first.pats) if not isinstance(p, KWild))
        if col != 0:
            occs = list(occs)
            occs[0], occs[col] = occs[col], occs[0]
            rows = [Row([*r.pats], r.bindings, r.guard, r.arm) for r in rows]
            for r in rows:
                r.pats[0], r.pats[col] = r.pats[col], r.pats[0]

        head_occ = occs[0]
        rest_occs = occs[1:]

        kind, keys, labels = self._column_info(rows)

        if kind == "record":
            # Records never fail to match; just project every mentioned label.
            sub_occs = [self.fresh("f") for _ in labels]
            sub_rows = []
            for r in rows:
                head = r.pats[0]
                if isinstance(head, KRecord):
                    subs = [head.fields.get(lab, KWILD) for lab in labels]
                else:
                    subs = [KWILD] * len(labels)
                sub_rows.append(Row(subs + r.pats[1:], r.bindings, r.guard, r.arm))
            inner = self._compile_rows(sub_occs + rest_occs, sub_rows)
            return TProject(head_occ, list(zip(sub_occs, labels)), inner)

        if kind == "tuple":
            arity = keys[0][1]
            sub_occs = [self.fresh("e") for _ in range(arity)]
            sub_rows = []
            for r in rows:
                head = r.pats[0]
                subs = head.args if isinstance(head, KTuple) else [KWILD] * arity
                sub_rows.append(Row(list(subs) + r.pats[1:], r.bindings, r.guard, r.arm))
            inner = self._compile_rows(sub_occs + rest_occs, sub_rows)
            return TDestructure(head_occ, sub_occs, inner)

        if kind == "lit":
            cases: list[tuple[object, Tree]] = []
            for value in keys:
                sub_rows = [
                    Row(r.pats[1:], r.bindings, r.guard, r.arm)
                    for r in rows
                    if isinstance(r.pats[0], KWild)
                    or (isinstance(r.pats[0], KLit) and r.pats[0].value == value)
                ]
                cases.append((value, self._compile_rows(rest_occs, sub_rows)))
            default_rows = [Row(r.pats[1:], r.bindings, r.guard, r.arm)
                            for r in rows if isinstance(r.pats[0], KWild)]
            default = self._compile_rows(rest_occs, default_rows)
            lit_kind = next(p.kind for p in (r.pats[0] for r in rows) if isinstance(p, KLit))
            return TSwitchLit(head_occ, cases, default, lit_kind)

        # kind == "con"
        present = {name for name, _ in keys}
        type_name = self.reg.type_of_con(next(iter(present)))
        all_cons = self.reg.constructors_of(type_name)
        complete = set(all_cons) <= present

        cases_con: list[tuple[str, int, list[str], Tree]] = []
        for name in all_cons:
            if name not in present:
                continue
            arity = self.reg.arity_of_con(name)
            sub_occs = [self.fresh("d") for _ in range(arity)]
            sub_rows = []
            for r in rows:
                head = r.pats[0]
                if isinstance(head, KWild):
                    sub_rows.append(Row([KWILD] * arity + r.pats[1:], r.bindings,
                                        r.guard, r.arm))
                elif isinstance(head, KCon) and head.name == name:
                    sub_rows.append(Row(list(head.args) + r.pats[1:], r.bindings,
                                        r.guard, r.arm))
            inner = self._compile_rows(sub_occs + rest_occs, sub_rows)
            cases_con.append((name, self.con_id(name) if name not in ("True", "False") else -1,
                              sub_occs, inner))

        default_tree: Tree | None = None
        if not complete:
            default_rows = [Row(r.pats[1:], r.bindings, r.guard, r.arm)
                            for r in rows if isinstance(r.pats[0], KWild)]
            default_tree = self._compile_rows(rest_occs, default_rows)

        if type_name == "Bool":
            return TSwitchBool(head_occ, dict((n, t) for n, _, _, t in cases_con), default_tree)
        return TSwitchCon(head_occ, cases_con, default_tree)

    @staticmethod
    def _simplify(r: Row, occs: list[str]) -> Row:
        """Strip variable/as patterns from every column, recording bindings."""
        pats = list(r.pats)
        bindings = list(r.bindings)
        for i, p in enumerate(pats):
            while True:
                if isinstance(p, KVar):
                    bindings.append((p.name, occs[i]))
                    p = KWILD
                elif isinstance(p, KAs):
                    bindings.append((p.name, occs[i]))
                    p = p.sub
                else:
                    break
            pats[i] = p
        return Row(pats, bindings, r.guard, r.arm)

    @staticmethod
    def _column_info(rows: list[Row]):
        """Classify column 0: which family, which keys, which record labels."""
        kind = None
        con_keys: list[tuple[str, int]] = []
        lit_keys: list[object] = []
        labels: list[str] = []
        for r in rows:
            head = r.pats[0]
            if isinstance(head, KWild):
                continue
            if isinstance(head, KCon):
                kind = "con"
                if not any(n == head.name for n, _ in con_keys):
                    con_keys.append((head.name, len(head.args)))
            elif isinstance(head, KTuple):
                kind = "tuple"
                if not con_keys:
                    con_keys.append(("tuple", len(head.args)))
            elif isinstance(head, KRecord):
                kind = "record"
                for lab in head.fields:
                    if lab not in labels:
                        labels.append(lab)
            elif isinstance(head, KLit):
                kind = "lit"
                if head.value not in lit_keys:
                    lit_keys.append(head.value)
        labels.sort()
        return kind, (lit_keys if kind == "lit" else con_keys), labels

    # -- tree -> Core ------------------------------------------------------

    def _emit(self, tree: "Tree", bodies: list[Core], guards: list[Core | None],
              renames: list[dict[str, str]], arm_vars: list[list[str]],
              joins: dict[int, str], span: Span) -> Core:
        emit = lambda t: self._emit(t, bodies, guards, renames, arm_vars, joins, span)

        match tree:
            case TFail():
                return CFail("no match arm applies", span)

            case TLeaf(arm=arm, bindings=bindings):
                return self._leaf(arm, bindings, bodies, renames, arm_vars, joins)

            case TGuard(arm=arm, bindings=bindings, on_fail=on_fail):
                guard = guards[arm]
                assert guard is not None
                success = self._leaf(arm, bindings, bodies, renames, arm_vars, joins)
                node: Core = CIf(guard, success, emit(on_fail))
                return self._wrap_bindings(bindings, node)

            case TProject(occ=occ, fields=fields, body=body):
                node = emit(body)
                for name, label in reversed(fields):
                    node = CLet(name, CRecordGet(CVar(occ), label), node)
                return node

            case TDestructure(occ=occ, sub_occs=sub_occs, body=body):
                node = emit(body)
                for i, name in reversed(list(enumerate(sub_occs))):
                    node = CLet(name, CGetField(CVar(occ), i), node)
                return node

            case TSwitchBool(occ=occ, cases=cases, default=default):
                then = cases.get("True")
                els = cases.get("False")
                fallback = emit(default) if default is not None else CFail("no match arm applies", span)
                return CIf(CVar(occ),
                           emit(then) if then is not None else fallback,
                           emit(els) if els is not None else fallback)

            case TSwitchCon(occ=occ, cases=cases, default=default):
                out_cases: list[tuple[int, Core]] = []
                for name, con_id, sub_occs, sub in cases:
                    node = emit(sub)
                    for i, sub_name in reversed(list(enumerate(sub_occs))):
                        node = CLet(sub_name, CGetField(CVar(occ), i), node)
                    out_cases.append((con_id, node))
                return CCase(CVar(occ), out_cases,
                             emit(default) if default is not None else None)

            case TSwitchLit(occ=occ, cases=cases, default=default, kind=kind):
                return CCaseLit(CVar(occ), [(v, emit(t)) for v, t in cases],
                                emit(default) if default is not None else None, kind)

        raise AssertionError(f"unhandled tree node {tree!r}")

    @staticmethod
    def _wrap_bindings(bindings: list[tuple[str, str]], body: Core) -> Core:
        for name, occ in reversed(bindings):
            body = CLet(name, CVar(occ), body)
        return body

    def _leaf(self, arm: int, bindings: list[tuple[str, str]], bodies: list[Core],
              renames: list[dict[str, str]], arm_vars: list[list[str]],
              joins: dict[int, str]) -> Core:
        table = dict(bindings)
        join = joins.get(arm)
        if join is not None:
            args = [CVar(table[renames[arm][v]]) for v in arm_vars[arm]]
            if not args:
                args = [CLit("Unit", None)]
            return CApp(CVar(join), args)
        return self._wrap_bindings(bindings, bodies[arm])


# ==========================================================================
# Decision tree nodes
# ==========================================================================


class Tree:
    __slots__ = ()


@dataclass(slots=True)
class TFail(Tree):
    pass


@dataclass(slots=True)
class TLeaf(Tree):
    arm: int
    bindings: list[tuple[str, str]]


@dataclass(slots=True)
class TGuard(Tree):
    arm: int
    bindings: list[tuple[str, str]]
    on_fail: Tree


@dataclass(slots=True)
class TProject(Tree):
    occ: str
    fields: list[tuple[str, str]]   # (temp name, label)
    body: Tree


@dataclass(slots=True)
class TDestructure(Tree):
    occ: str
    sub_occs: list[str]
    body: Tree


@dataclass(slots=True)
class TSwitchCon(Tree):
    occ: str
    cases: list[tuple[str, int, list[str], Tree]]
    default: Tree | None


@dataclass(slots=True)
class TSwitchBool(Tree):
    occ: str
    cases: dict[str, Tree]
    default: Tree | None


@dataclass(slots=True)
class TSwitchLit(Tree):
    occ: str
    cases: list[tuple[object, Tree]]
    default: Tree | None
    kind: str


def _count_leaves(tree: Tree, uses: dict[int, int]) -> None:
    match tree:
        case TLeaf(arm=arm):
            uses[arm] = uses.get(arm, 0) + 1
        case TGuard(arm=arm, on_fail=on_fail):
            uses[arm] = uses.get(arm, 0) + 1
            _count_leaves(on_fail, uses)
        case TProject(body=body) | TDestructure(body=body):
            _count_leaves(body, uses)
        case TSwitchCon(cases=cases, default=default):
            for _, _, _, sub in cases:
                _count_leaves(sub, uses)
            if default is not None:
                _count_leaves(default, uses)
        case TSwitchBool(cases=cases, default=default):
            for sub in cases.values():
                _count_leaves(sub, uses)
            if default is not None:
                _count_leaves(default, uses)
        case TSwitchLit(cases=cases, default=default):
            for _, sub in cases:
                _count_leaves(sub, uses)
            if default is not None:
                _count_leaves(default, uses)
        case TFail():
            pass
