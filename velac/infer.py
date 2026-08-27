"""Hindley–Milner type inference for Vela.

Highlights beyond textbook Algorithm W:

*   **Level-based generalization** (Rémy): no environment scan when
    generalizing a `let`.
*   **Row-polymorphic records**: `\\p -> p.x` gets `{ x : a | r } -> a`.
*   **Rigid annotations**: a type signature on a binding is checked by
    skolemizing its variables, so `let id : a -> a = \\x -> x + 1` is
    correctly rejected. Bare ascriptions `(e : T)` stay non-rigid and act as
    hints.
*   **Polymorphic recursion** where a signature is supplied — an annotated
    binding is visible at its full polymorphic scheme inside its own body.
*   **Multiple errors per run**: a failed unification records a diagnostic and
    yields a fresh variable so checking can continue.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ast as A
from .errors import Diagnostic, Label, Span
from .patterns import check_match
from .registry import ConInfo, DataInfo, Registry
from .types import (EMPTY_ROW, LITERAL_TYPES, Scheme, TBool, TCon, TFloat, TInt, TRec,
                    TString, TUnit, TVar, Type, TypeVarSupply, UnifyError, fn,
                    generalize, instantiate, list_of, prune, resolve, show, show_scheme,
                    tuple_of, unify)

# --------------------------------------------------------------------------
# Environments
# --------------------------------------------------------------------------


class Env:
    """A lexically scoped map from value names to type schemes."""

    __slots__ = ("vars", "parent", "sealed")

    def __init__(self, parent: "Env | None" = None, sealed: bool = False) -> None:
        self.vars: dict[str, Scheme] = {}
        self.parent = parent
        #: A sealed env is a boundary for the skolem-escape scan (the builtins).
        self.sealed = sealed

    def child(self) -> "Env":
        return Env(self)

    def define(self, name: str, scheme: Scheme) -> None:
        self.vars[name] = scheme

    def lookup(self, name: str) -> Scheme | None:
        env: Env | None = self
        while env is not None:
            got = env.vars.get(name)
            if got is not None:
                return got
            env = env.parent
        return None

    def names(self) -> list[str]:
        seen: list[str] = []
        env: Env | None = self
        while env is not None:
            seen.extend(env.vars)
            env = env.parent
        return seen


@dataclass(slots=True)
class ModuleTypes:
    """What inference produces for one module."""

    values: dict[str, Scheme]
    order: list[str]


# --------------------------------------------------------------------------
# Checker
# --------------------------------------------------------------------------


class Checker:
    def __init__(self, registry: Registry | None = None) -> None:
        self.reg = registry or Registry()
        self.supply = TypeVarSupply()
        self.errors: list[Diagnostic] = []
        self.warnings: list[Diagnostic] = []
        self.builtins = Env(None, sealed=True)
        self._install_builtin_schemes()

    # -- error plumbing ----------------------------------------------------

    def fail(self, diag: Diagnostic) -> Type:
        """Record an error and return a fresh variable so checking continues."""
        self.errors.append(diag)
        return self.supply.fresh()

    def error(self, message: str, span: Span, label: str = "",
              notes: list[str] | None = None, code: str = "E0100") -> Type:
        return self.fail(Diagnostic(message, [Label(span, label)], list(notes or ()), code))

    def warn(self, message: str, span: Span, notes: list[str] | None = None) -> None:
        d = Diagnostic(message, [Label(span, "")], list(notes or ()), "W0001")
        d.severity = "warning"
        self.warnings.append(d)

    def unify(self, expected: Type, actual: Type, span: Span, context: str = "",
              notes: list[str] | None = None) -> None:
        try:
            unify(expected, actual, self.supply)
        except UnifyError as e:
            message = f"{context}: {e.message}" if context else e.message
            all_notes = list(notes or ())
            if e.note:
                all_notes.append(e.note)
            self.errors.append(Diagnostic(message, [Label(span, "")], all_notes, "E0100"))

    # -- builtin schemes ---------------------------------------------------

    def _poly(self, n: int, build) -> Scheme:
        vars = [self.supply.fresh() for _ in range(n)]
        return Scheme(vars, build(*vars))

    def _install_builtin_schemes(self) -> None:
        env = self.builtins

        for op in ("+", "-", "*", "/", "%", "**"):
            env.define(op, Scheme.mono(fn(TInt, TInt, TInt)))
        for op in ("+.", "-.", "*.", "/."):
            env.define(op, Scheme.mono(fn(TFloat, TFloat, TFloat)))
        for op in ("==", "!=", "<", "<=", ">", ">="):
            env.define(op, self._poly(1, lambda a: fn(a, a, TBool)))

        env.define("::", self._poly(1, lambda a: fn(a, list_of(a), list_of(a))))
        env.define("++", self._poly(1, lambda a: fn(list_of(a), list_of(a), list_of(a))))
        env.define("^", Scheme.mono(fn(TString, TString, TString)))
        env.define(">>", self._poly(3, lambda a, b, c: fn(fn(a, b), fn(b, c), fn(a, c))))
        env.define("<<", self._poly(3, lambda a, b, c: fn(fn(b, c), fn(a, b), fn(a, c))))

        env.define("neg", Scheme.mono(fn(TInt, TInt)))
        env.define("negf", Scheme.mono(fn(TFloat, TFloat)))
        env.define("not", Scheme.mono(fn(TBool, TBool)))

        # Builtin data constructors get real schemes here (the Registry only
        # recorded their shapes so the pattern checker could see them).
        a = self.supply.fresh()
        self.reg.cons["Nil"].scheme = Scheme([a], list_of(a))
        b = self.supply.fresh()
        self.reg.cons["Cons"].scheme = Scheme([b], fn(b, list_of(b), list_of(b)))
        self.reg.cons["True"].scheme = Scheme.mono(TBool)
        self.reg.cons["False"].scheme = Scheme.mono(TBool)
        self.reg.cons["()"].scheme = Scheme.mono(TUnit)

    # ------------------------------------------------------------------
    # Surface types -> internal types
    # ------------------------------------------------------------------

    BUILTIN_TYPES = {"Int": TInt, "Float": TFloat, "Bool": TBool,
                     "String": TString, "Unit": TUnit}

    def convert_type(self, te: A.TypeExpr, var_map: dict[str, Type],
                     rigid: bool = False, depth: int = 0) -> Type:
        if depth > 100:
            return self.error("type alias expansion is too deep (cycle?)", te.span)

        match te:
            case A.TEVar(name=name):
                got = var_map.get(name)
                if got is None:
                    got = TCon("$" + name) if rigid else self.supply.fresh()
                    var_map[name] = got
                return got

            case A.TEFun(param=p, result=r):
                return TCon("->", [self.convert_type(p, var_map, rigid, depth),
                                   self.convert_type(r, var_map, rigid, depth)])

            case A.TETuple(items=items):
                return tuple_of([self.convert_type(i, var_map, rigid, depth) for i in items])

            case A.TEList(item=item):
                return list_of(self.convert_type(item, var_map, rigid, depth))

            case A.TERecord(fields=fields, rest=rest):
                converted = {k: self.convert_type(v, var_map, rigid, depth) for k, v in fields}
                if rest is None:
                    tail: Type = EMPTY_ROW
                else:
                    tail = var_map.get(rest)
                    if tail is None:
                        tail = TCon("$" + rest) if rigid else self.supply.fresh(is_row=True)
                        var_map[rest] = tail
                return TRec(converted, tail)

            case A.TECon(name=name, args=args, module=_):
                converted = [self.convert_type(a, var_map, rigid, depth) for a in args]

                if name in self.BUILTIN_TYPES:
                    if converted:
                        return self.error(
                            f"type `{name}` takes no arguments, but {len(converted)} given",
                            te.span)
                    return self.BUILTIN_TYPES[name]

                if name == "List":
                    if len(converted) != 1:
                        return self.error("`List` takes exactly one type argument", te.span,
                                          notes=["you can also write it as `[a]`"])
                    return list_of(converted[0])

                alias = self.reg.aliases.get(name)
                if alias is not None:
                    params, target = alias
                    if len(params) != len(converted):
                        return self.error(
                            f"type alias `{name}` expects {len(params)} argument(s), "
                            f"{len(converted)} given", te.span)
                    inner = dict(zip(params, converted))
                    return self.convert_type(target, inner, rigid, depth + 1)

                info = self.reg.types.get(name)
                if info is None:
                    return self.error(f"unknown type `{name}`", te.span,
                                      notes=self._suggest_type(name))
                if info.arity != len(converted):
                    return self.error(
                        f"type `{name}` expects {info.arity} argument(s), "
                        f"{len(converted)} given", te.span)
                return TCon(name, converted)

        raise AssertionError(f"unhandled type expression {te!r}")

    def _suggest_type(self, name: str) -> list[str]:
        candidates = list(self.BUILTIN_TYPES) + list(self.reg.types) + list(self.reg.aliases)
        near = _closest(name, candidates)
        return [f"did you mean `{near}`?"] if near else []

    # ------------------------------------------------------------------
    # Declarations
    # ------------------------------------------------------------------

    def declare_types(self, module: A.Module) -> None:
        """Register every `type` in a module before anything is type-checked.

        Two passes so that mutually recursive data types work: first record the
        names and arities, then build the constructor schemes.
        """
        data_decls = [d for d in module.decls if isinstance(d, A.DType)]
        alias_decls = [d for d in module.decls if isinstance(d, A.DTypeAlias)]

        for d in alias_decls:
            if d.name in self.reg.types or d.name in self.reg.aliases:
                self.error(f"type `{d.name}` is already defined", d.span)
            self.reg.aliases[d.name] = (d.params, d.target)

        for d in data_decls:
            if d.name in self.reg.types or d.name in self.reg.aliases:
                self.error(f"type `{d.name}` is already defined", d.span)
            if len(set(d.params)) != len(d.params):
                self.error(f"duplicate type parameter in `{d.name}`", d.span)
            self.reg.add_type(DataInfo(d.name, len(d.params),
                                       [c.name for c in d.constructors], d.span))

        for d in data_decls:
            self._build_constructors(d)

    def _build_constructors(self, d: A.DType) -> None:
        self.supply.enter()
        var_map: dict[str, Type] = {p: self.supply.fresh() for p in d.params}
        result = TCon(d.name, [var_map[p] for p in d.params])
        quantified = [v for v in var_map.values() if isinstance(v, TVar)]

        for tag, con in enumerate(d.constructors):
            if con.name in self.reg.cons and self.reg.cons[con.name].type_name != d.name:
                self.error(f"constructor `{con.name}` is already defined by type "
                           f"`{self.reg.cons[con.name].type_name}`", con.span)
            field_types = [self.convert_type(t, var_map) for t in con.args]
            for t in con.args:
                self._check_no_free_vars(t, set(d.params), d.name)
            scheme = Scheme(quantified, fn(*field_types, result) if field_types else result)
            self.reg.add_con(ConInfo(con.name, d.name, tag, len(con.args), scheme,
                                     field_types, con.span))
        self.supply.exit()

    def _check_no_free_vars(self, te: A.TypeExpr, bound: set[str], type_name: str) -> None:
        """Constructor arguments may only mention the data type's own parameters."""
        match te:
            case A.TEVar(name=n, span=span):
                if n not in bound:
                    self.error(
                        f"type variable `{n}` is not bound by type `{type_name}`", span,
                        notes=[f"add it as a parameter: `type {type_name} {n} = ...`"])
            case A.TEFun(param=p, result=r):
                self._check_no_free_vars(p, bound, type_name)
                self._check_no_free_vars(r, bound, type_name)
            case A.TETuple(items=items):
                for i in items:
                    self._check_no_free_vars(i, bound, type_name)
            case A.TEList(item=i):
                self._check_no_free_vars(i, bound, type_name)
            case A.TERecord(fields=fields, rest=rest, span=span):
                for _, v in fields:
                    self._check_no_free_vars(v, bound, type_name)
                if rest is not None and rest not in bound:
                    self.error(f"row variable `{rest}` is not bound by type `{type_name}`", span)
            case A.TECon(args=args):
                for a in args:
                    self._check_no_free_vars(a, bound, type_name)

    def infer_module(self, module: A.Module, env: Env) -> ModuleTypes:
        """Type-check a module's value declarations in order."""
        exports: dict[str, Scheme] = {}
        order: list[str] = []

        for decl in module.decls:
            if isinstance(decl, A.DType) or isinstance(decl, A.DTypeAlias):
                continue
            if isinstance(decl, A.DImport):
                continue
            if isinstance(decl, A.DExtern):
                self.supply.enter()
                ty = self.convert_type(decl.annotation, {})
                self.supply.exit()
                scheme = generalize(ty, self.supply.level)
                env.define(decl.name, scheme)
                exports[decl.name] = scheme
                order.append(decl.name)
                continue
            if isinstance(decl, A.DLet):
                schemes = self.infer_binding_group(decl.bindings, decl.recursive, env)
                for name, scheme in schemes.items():
                    if name in exports:
                        self.error(f"`{name}` is defined more than once in this module",
                                   decl.span)
                    env.define(name, scheme)
                    exports[name] = scheme
                    order.append(name)
                continue
            raise AssertionError(f"unhandled declaration {decl!r}")

        return ModuleTypes(exports, order)

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    @staticmethod
    def binding_body(b: A.Binding) -> A.Expr:
        """`f x y = e` is sugar for `f = \\x y -> e`."""
        if not b.params:
            return b.body
        return A.ELambda(b.span, list(b.params), b.body)

    def infer_binding_group(self, bindings: list[A.Binding], recursive: bool,
                            env: Env) -> dict[str, Scheme]:
        seen: dict[str, Span] = {}
        for b in bindings:
            if b.name in seen:
                self.error(f"`{b.name}` is bound twice in the same group",
                           b.name_span or b.span)
            seen[b.name] = b.name_span or b.span

        if not recursive:
            out: dict[str, Scheme] = {}
            for b in bindings:
                out[b.name] = self._infer_single(b, env)
            return out

        # Recursive group: pre-bind every name, then check the bodies.
        inner = env.child()
        temp: dict[str, Type] = {}
        annotated: dict[str, Scheme] = {}

        self.supply.enter()
        for b in bindings:
            if b.annotation is not None:
                # A signature enables polymorphic recursion: the name is
                # visible at its full scheme inside its own body.
                self.supply.enter()
                poly = self.convert_type(b.annotation, {})
                self.supply.exit()
                scheme = generalize(poly, self.supply.level)
                annotated[b.name] = scheme
                inner.define(b.name, scheme)
            else:
                var = self.supply.fresh()
                temp[b.name] = var
                inner.define(b.name, Scheme.mono(var))

        for b in bindings:
            body = self.binding_body(b)
            if b.annotation is not None:
                rigid_map: dict[str, Type] = {}
                rigid = self.convert_type(b.annotation, rigid_map, rigid=True)
                actual = self.infer(body, inner)
                self.unify(rigid, actual, b.span,
                           f"`{b.name}` does not match its type signature")
                self._check_escape(env, rigid_map, b.span, b.name)
            else:
                actual = self.infer(body, inner)
                self.unify(temp[b.name], actual, b.span,
                           f"conflicting types for recursive binding `{b.name}`")
        self.supply.exit()

        out = {}
        for b in bindings:
            if b.name in annotated:
                out[b.name] = annotated[b.name]
            else:
                out[b.name] = generalize(temp[b.name], self.supply.level)
        return out

    def _infer_single(self, b: A.Binding, env: Env) -> Scheme:
        body = self.binding_body(b)

        if b.annotation is None:
            self.supply.enter()
            ty = self.infer(body, env)
            self.supply.exit()
            return generalize(ty, self.supply.level)

        rigid_map: dict[str, Type] = {}
        self.supply.enter()
        rigid = self.convert_type(b.annotation, rigid_map, rigid=True)
        actual = self.infer(body, env)
        self.unify(rigid, actual, b.body.span,
                   f"`{b.name}` does not match its type signature",
                   notes=[f"signature says `{show(rigid)}`"])
        self.supply.exit()
        self._check_escape(env, rigid_map, b.span, b.name)

        self.supply.enter()
        poly = self.convert_type(b.annotation, {})
        self.supply.exit()
        return generalize(poly, self.supply.level)

    def _check_escape(self, env: Env, rigid_map: dict[str, Type], span: Span,
                      name: str) -> None:
        """Reject signatures whose variables leaked into the enclosing scope."""
        skolems = {t.name for t in rigid_map.values() if isinstance(t, TCon)}
        if not skolems:
            return
        cur: Env | None = env
        while cur is not None and not cur.sealed:
            for scheme in cur.vars.values():
                if scheme.vars:
                    continue  # polymorphic entries cannot capture a skolem
                if _mentions_skolem(scheme.type, skolems):
                    self.error(
                        f"the type signature of `{name}` is too general", span,
                        notes=["a type variable in the signature was forced to equal "
                               "a type from the surrounding scope"])
                    return
            cur = cur.parent

    # ------------------------------------------------------------------
    # Expressions
    # ------------------------------------------------------------------

    def infer(self, e: A.Expr, env: Env) -> Type:
        match e:
            case A.ELit(value=_, kind=kind):
                return LITERAL_TYPES[kind]

            case A.EVar(name=name, module=module, span=span):
                key = f"{module}.{name}" if module else name
                scheme = env.lookup(key)
                if scheme is None:
                    return self.error(f"unknown value `{key}`", span,
                                      notes=self._suggest_value(key, env))
                return instantiate(scheme, self.supply)

            case A.ECon(name=name, span=span):
                info = self.reg.cons.get(name)
                if info is None:
                    return self.error(f"unknown constructor `{name}`", span,
                                      notes=self._suggest_con(name))
                return instantiate(info.scheme, self.supply)

            case A.ELambda(params=params, body=body, span=span):
                inner = env.child()
                param_types = []
                for p in params:
                    t = self.supply.fresh()
                    param_types.append(t)
                    self.bind_pattern(p, t, inner, refutable=False)
                result = self.infer(body, inner)
                return fn(*param_types, result)

            case A.EApp(fn=callee, args=args, span=span):
                return self.infer_app(callee, args, env, span)

            case A.EBinOp(op=op, lhs=lhs, rhs=rhs, span=span, op_span=op_span):
                scheme = env.lookup(op)
                assert scheme is not None, f"operator {op} has no scheme"
                op_type = instantiate(scheme, self.supply)
                return self.apply_args(op_type, [lhs, rhs], env, span,
                                       op_span or span, f"operator `{op}`")

            case A.EUnOp(op=op, operand=operand, span=span):
                scheme = env.lookup(op)
                assert scheme is not None, f"operator {op} has no scheme"
                op_type = instantiate(scheme, self.supply)
                pretty = {"neg": "-", "negf": "-.", "not": "not"}[op]
                return self.apply_args(op_type, [operand], env, span, span,
                                       f"operator `{pretty}`")

            case A.EIf(cond=cond, then=then, otherwise=otherwise, span=span):
                ct = self.infer(cond, env)
                self.unify(TBool, ct, cond.span, "the condition of `if` must be a Bool")
                tt = self.infer(then, env)
                ft = self.infer(otherwise, env)
                self.unify(tt, ft, otherwise.span,
                           "the branches of `if` have different types",
                           notes=[f"`then` branch is `{show(resolve(tt))}`"])
                return tt

            case A.EAnd(lhs=lhs, rhs=rhs) | A.EOr(lhs=lhs, rhs=rhs):
                word = "&&" if isinstance(e, A.EAnd) else "||"
                lt = self.infer(lhs, env)
                self.unify(TBool, lt, lhs.span, f"the left operand of `{word}` must be a Bool")
                rt = self.infer(rhs, env)
                self.unify(TBool, rt, rhs.span, f"the right operand of `{word}` must be a Bool")
                return TBool

            case A.ESeq(first=first, second=second):
                ft = self.infer(first, env)
                self.unify(TUnit, ft, first.span,
                           "the left side of `;` must have type Unit",
                           notes=["use `let _ = ... in ...` to discard a non-Unit value"])
                return self.infer(second, env)

            case A.ELet(bindings=bindings, body=body, recursive=recursive):
                schemes = self.infer_binding_group(bindings, recursive, env)
                inner = env.child()
                for name, scheme in schemes.items():
                    inner.define(name, scheme)
                return self.infer(body, inner)

            case A.ELetPattern(pattern=pattern, value=value, body=body, span=span):
                self.supply.enter()
                vt = self.infer(value, env)
                self.supply.exit()
                inner = env.child()
                self.bind_pattern(pattern, vt, inner, refutable=True, generalize_at=self.supply.level)
                self._check_irrefutable(pattern, span)
                return self.infer(body, inner)

            case A.EMatch(scrutinee=scrutinee, arms=arms, span=span):
                return self.infer_match(scrutinee, arms, env, span)

            case A.ETuple(items=items):
                return tuple_of([self.infer(i, env) for i in items])

            case A.EList(items=items, span=span):
                elem = self.supply.fresh()
                for i, item in enumerate(items):
                    it = self.infer(item, env)
                    self.unify(elem, it, item.span,
                               "list elements must all have the same type",
                               notes=[f"earlier elements are `{show(resolve(elem))}`"] if i else None)
                return list_of(elem)

            case A.ERecord(fields=fields, base=base, span=span):
                field_types = {name: self.infer(value, env) for name, value in fields}
                if base is None:
                    return TRec(field_types, EMPTY_ROW)
                base_type = self.infer(base, env)
                rest = self.supply.fresh(is_row=True)
                self.unify(TRec(field_types, rest), base_type, span,
                           "record update",
                           notes=["`{ r | f = v }` requires `r` to already have field `f` "
                                  "at the same type"])
                return base_type

            case A.EField(record=record, name=name, span=span, name_span=name_span):
                rt = self.infer(record, env)
                result = self.supply.fresh()
                rest = self.supply.fresh(is_row=True)
                # No context prefix: the record/field messages already name the
                # field, and the span points straight at it.
                self.unify(TRec({name: result}, rest), rt, name_span or span)
                return result

            case A.EAnnot(expr=inner, annotation=annotation, span=span):
                # Bare ascriptions are hints, not rigid signatures.
                declared = self.convert_type(annotation, {})
                actual = self.infer(inner, env)
                self.unify(declared, actual, span, "type ascription")
                return declared

        raise AssertionError(f"unhandled expression {e!r}")

    # -- application -------------------------------------------------------

    def infer_app(self, callee: A.Expr, args: list[A.Expr], env: Env, span: Span) -> Type:
        callee_type = self.infer(callee, env)
        what = self._describe_callee(callee)
        return self.apply_args(callee_type, args, env, span, callee.span, what)

    @staticmethod
    def _describe_callee(callee: A.Expr) -> str:
        if isinstance(callee, A.EVar):
            key = f"{callee.module}.{callee.name}" if callee.module else callee.name
            return f"`{key}`"
        if isinstance(callee, A.ECon):
            return f"constructor `{callee.name}`"
        return "this function"

    def apply_args(self, callee_type: Type, args: list[A.Expr], env: Env,
                   span: Span, callee_span: Span, what: str) -> Type:
        for index, arg in enumerate(args):
            arg_type = self.infer(arg, env)
            result = self.supply.fresh()
            expected = prune(callee_type)

            if isinstance(expected, TCon) and expected.name == "->":
                # Unify against the declared parameter for a precise message.
                self.unify(expected.args[0], arg_type, arg.span,
                           f"{what} expects a different argument {index + 1}")
                callee_type = expected.args[1]
                continue

            if isinstance(expected, TVar):
                self.unify(expected, fn(arg_type, result), arg.span,
                           f"{what} is applied to too many arguments"
                           if index else f"{what} cannot be called")
                callee_type = result
                continue

            self.error(
                f"{what} is applied to {len(args)} argument(s) but is not a function",
                callee_span,
                notes=[f"it has type `{show(resolve(expected))}`"])
            return self.supply.fresh()

        return callee_type

    # -- match -------------------------------------------------------------

    def infer_match(self, scrutinee: A.Expr, arms: list[A.MatchArm], env: Env,
                    span: Span) -> Type:
        scrutinee_type = self.infer(scrutinee, env)
        result = self.supply.fresh()

        for arm in arms:
            inner = env.child()
            self.bind_pattern(arm.pattern, scrutinee_type, inner, refutable=True)
            if arm.guard is not None:
                gt = self.infer(arm.guard, inner)
                self.unify(TBool, gt, arm.guard.span, "a match guard must be a Bool")
            body_type = self.infer(arm.body, inner)
            self.unify(result, body_type, arm.body.span,
                       "match arms have different types",
                       notes=[f"earlier arms produce `{show(resolve(result))}`"])

        report = check_match([a.pattern for a in arms],
                             [a.guard is not None for a in arms], self.reg)
        if report.missing:
            self.error(
                "this `match` is not exhaustive", span,
                notes=[f"no arm matches `{w}`" for w in report.missing] +
                      ["add a `_ -> ...` arm to handle the rest"],
                code="E0200")
        for index in report.redundant:
            self.warn("this match arm is unreachable", arms[index].pattern.span,
                      notes=["an earlier arm already covers every value this one matches"])

        return result

    # -- patterns ----------------------------------------------------------

    def bind_pattern(self, p: A.Pattern, expected: Type, env: Env, refutable: bool,
                     generalize_at: int | None = None) -> None:
        """Type a pattern against `expected` and add its bindings to `env`."""
        seen: dict[str, Span] = {}
        self._bind_pattern(p, expected, env, refutable, seen, generalize_at)

    def _define(self, env: Env, name: str, ty: Type, generalize_at: int | None) -> None:
        if generalize_at is None:
            env.define(name, Scheme.mono(ty))
        else:
            env.define(name, generalize(ty, generalize_at))

    def _bind_pattern(self, p: A.Pattern, expected: Type, env: Env, refutable: bool,
                      seen: dict[str, Span], gen: int | None) -> None:
        match p:
            case A.PWild():
                return

            case A.PVar(name=name, span=span):
                if name in seen:
                    self.error(f"`{name}` is bound twice in the same pattern", span)
                seen[name] = span
                self._define(env, name, expected, gen)

            case A.PAs(pattern=inner, name=name, span=span):
                self._bind_pattern(inner, expected, env, refutable, seen, gen)
                if name in seen:
                    self.error(f"`{name}` is bound twice in the same pattern", span)
                seen[name] = span
                self._define(env, name, expected, gen)

            case A.PLit(value=_, kind=kind, span=span):
                self.unify(LITERAL_TYPES[kind], expected, span,
                           "this literal pattern has the wrong type")

            case A.PCon(name=name, args=args, span=span):
                info = self.reg.cons.get(name)
                if info is None:
                    self.error(f"unknown constructor `{name}`", span,
                               notes=self._suggest_con(name))
                    for a in args:
                        self._bind_pattern(a, self.supply.fresh(), env, refutable, seen, gen)
                    return
                if len(args) != info.arity:
                    self.error(
                        f"constructor `{name}` takes {info.arity} argument(s), "
                        f"{len(args)} given", span)
                con_type = instantiate(info.scheme, self.supply)
                params, result = _split_arrows(con_type, info.arity)
                self.unify(result, expected, span,
                           f"pattern `{name}` does not match the value being matched")
                for arg, t in zip(args, params):
                    self._bind_pattern(arg, t, env, refutable, seen, gen)

            case A.PTuple(items=items, span=span):
                item_types = [self.supply.fresh() for _ in items]
                self.unify(tuple_of(item_types), expected, span,
                           f"expected a {len(items)}-tuple pattern")
                for item, t in zip(items, item_types):
                    self._bind_pattern(item, t, env, refutable, seen, gen)

            case A.PList(items=items, span=span):
                elem = self.supply.fresh()
                self.unify(list_of(elem), expected, span, "this pattern expects a list")
                for item in items:
                    self._bind_pattern(item, elem, env, refutable, seen, gen)

            case A.PCons(head=head, tail=tail, span=span):
                elem = self.supply.fresh()
                self.unify(list_of(elem), expected, span, "the `::` pattern expects a list")
                self._bind_pattern(head, elem, env, refutable, seen, gen)
                self._bind_pattern(tail, list_of(elem), env, refutable, seen, gen)

            case A.PRecord(fields=fields, span=span):
                field_types = {name: self.supply.fresh() for name, _ in fields}
                rest = self.supply.fresh(is_row=True)
                self.unify(TRec(field_types, rest), expected, span,
                           "this record pattern does not match")
                for name, sub in fields:
                    self._bind_pattern(sub, field_types[name], env, refutable, seen, gen)
                return

        return

    def _check_irrefutable(self, p: A.Pattern, span: Span) -> None:
        report = check_match([p], [False], self.reg)
        if report.missing:
            self.error(
                "this binding pattern can fail", span,
                notes=[f"it does not match `{w}`" for w in report.missing] +
                      ["use a `match` expression to handle every case"],
                code="E0201")

    # -- suggestions -------------------------------------------------------

    def _suggest_value(self, name: str, env: Env) -> list[str]:
        near = _closest(name, env.names())
        return [f"did you mean `{near}`?"] if near else []

    def _suggest_con(self, name: str) -> list[str]:
        near = _closest(name, list(self.reg.cons))
        return [f"did you mean `{near}`?"] if near else []


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _split_arrows(t: Type, n: int) -> tuple[list[Type], Type]:
    params: list[Type] = []
    for _ in range(n):
        t = prune(t)
        if isinstance(t, TCon) and t.name == "->":
            params.append(t.args[0])
            t = t.args[1]
        else:
            break
    while len(params) < n:
        params.append(TVar(-1, 0))
    return params, t


def _mentions_skolem(t: Type, skolems: set[str]) -> bool:
    t = prune(t)
    if isinstance(t, TCon):
        if t.name in skolems:
            return True
        return any(_mentions_skolem(a, skolems) for a in t.args)
    if isinstance(t, TRec):
        from .types import flatten_record
        fields, tail = flatten_record(t)
        return (any(_mentions_skolem(v, skolems) for v in fields.values())
                or _mentions_skolem(tail, skolems))
    return False


def _closest(name: str, candidates: list[str]) -> str | None:
    """Cheap edit-distance suggestion for typo'd identifiers."""
    best: str | None = None
    best_score = 3  # only suggest when it is genuinely close
    for c in set(candidates):
        if c == name or c.startswith("$"):
            continue
        d = _edit_distance(name, c, best_score)
        if d < best_score:
            best, best_score = c, d
    return best


def _edit_distance(a: str, b: str, limit: int) -> int:
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
        if min(prev) > limit:
            return limit + 1
    return prev[-1]


__all__ = ["Checker", "Env", "ModuleTypes", "show_scheme"]
