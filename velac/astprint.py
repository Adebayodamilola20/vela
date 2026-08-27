"""S-expression rendering of the surface AST.

Used by the parser tests (structure is easy to assert on as a string) and by
`velac --dump-ast`.
"""

from __future__ import annotations

from . import ast as A


def _lit(value: object, kind: str) -> str:
    if kind == "Unit":
        return "unit"
    if kind == "String":
        return f"{value!r}"
    if kind == "Bool":
        return "true" if value else "false"
    return str(value)


def type_to_sexp(t: A.TypeExpr) -> str:
    match t:
        case A.TEVar(name=n):
            return n
        case A.TECon(name=n, args=args, module=m):
            head = f"{m}.{n}" if m else n
            return f"({head} {' '.join(type_to_sexp(a) for a in args)})" if args else head
        case A.TEFun(param=p, result=r):
            return f"(-> {type_to_sexp(p)} {type_to_sexp(r)})"
        case A.TETuple(items=items):
            return f"(tuple {' '.join(type_to_sexp(i) for i in items)})"
        case A.TEList(item=i):
            return f"(list {type_to_sexp(i)})"
        case A.TERecord(fields=fs, rest=rest):
            body = " ".join(f"({k} {type_to_sexp(v)})" for k, v in fs)
            tail = f" | {rest}" if rest else ""
            return f"(record {body}{tail})"
    raise AssertionError(f"unhandled type node {t!r}")


def pat_to_sexp(p: A.Pattern) -> str:
    match p:
        case A.PWild():
            return "_"
        case A.PVar(name=n):
            return n
        case A.PLit(value=v, kind=k):
            return _lit(v, k)
        case A.PCon(name=n, args=args, module=m):
            head = f"{m}.{n}" if m else n
            return f"({head} {' '.join(pat_to_sexp(a) for a in args)})" if args else head
        case A.PTuple(items=items):
            return f"(tuple {' '.join(pat_to_sexp(i) for i in items)})"
        case A.PList(items=items):
            return f"(list {' '.join(pat_to_sexp(i) for i in items)})" if items else "(list)"
        case A.PCons(head=h, tail=t):
            return f"(:: {pat_to_sexp(h)} {pat_to_sexp(t)})"
        case A.PRecord(fields=fs):
            return f"(precord {' '.join(f'({k} {pat_to_sexp(v)})' for k, v in fs)})"
        case A.PAs(pattern=inner, name=n):
            return f"(as {pat_to_sexp(inner)} {n})"
    raise AssertionError(f"unhandled pattern node {p!r}")


def _binding_to_sexp(b: A.Binding) -> str:
    params = "".join(" " + pat_to_sexp(p) for p in b.params)
    ann = f" : {type_to_sexp(b.annotation)}" if b.annotation else ""
    return f"({b.name}{params}{ann} = {to_sexp(b.body)})"


def to_sexp(e: A.Expr) -> str:
    match e:
        case A.ELit(value=v, kind=k):
            return _lit(v, k)
        case A.EVar(name=n, module=m):
            return f"{m}.{n}" if m else n
        case A.ECon(name=n, module=m):
            return f"{m}.{n}" if m else n
        case A.ELambda(params=ps, body=b):
            return f"(lambda ({' '.join(pat_to_sexp(p) for p in ps)}) {to_sexp(b)})"
        case A.EApp(fn=f, args=args):
            return f"(app {to_sexp(f)} {' '.join(to_sexp(a) for a in args)})"
        case A.EBinOp(op=op, lhs=l, rhs=r):
            return f"({op} {to_sexp(l)} {to_sexp(r)})"
        case A.EUnOp(op=op, operand=o):
            return f"({op} {to_sexp(o)})"
        case A.EIf(cond=c, then=t, otherwise=f):
            return f"(if {to_sexp(c)} {to_sexp(t)} {to_sexp(f)})"
        case A.EAnd(lhs=l, rhs=r):
            return f"(and {to_sexp(l)} {to_sexp(r)})"
        case A.EOr(lhs=l, rhs=r):
            return f"(or {to_sexp(l)} {to_sexp(r)})"
        case A.ELet(bindings=bs, body=body, recursive=rec):
            kw = "letrec" if rec else "let"
            return f"({kw} ({' '.join(_binding_to_sexp(b) for b in bs)}) {to_sexp(body)})"
        case A.ELetPattern(pattern=p, value=v, body=body):
            return f"(letpat {pat_to_sexp(p)} {to_sexp(v)} {to_sexp(body)})"
        case A.ESeq(first=a, second=b):
            return f"(seq {to_sexp(a)} {to_sexp(b)})"
        case A.EMatch(scrutinee=s, arms=arms):
            parts = []
            for arm in arms:
                guard = f" (when {to_sexp(arm.guard)})" if arm.guard else ""
                parts.append(f"({pat_to_sexp(arm.pattern)}{guard} -> {to_sexp(arm.body)})")
            return f"(match {to_sexp(s)} {' '.join(parts)})"
        case A.ETuple(items=items):
            return f"(tuple {' '.join(to_sexp(i) for i in items)})"
        case A.EList(items=items):
            return f"(list {' '.join(to_sexp(i) for i in items)})" if items else "(list)"
        case A.ERecord(fields=fs, base=base):
            body = " ".join(f"({k} {to_sexp(v)})" for k, v in fs)
            if base is not None:
                return f"(update {to_sexp(base)} {body})"
            return f"(record {body})" if fs else "(record)"
        case A.EField(record=r, name=n):
            return f"(. {to_sexp(r)} {n})"
        case A.EAnnot(expr=inner, annotation=t):
            return f"(the {type_to_sexp(t)} {to_sexp(inner)})"
    raise AssertionError(f"unhandled expression node {e!r}")


def decl_to_sexp(d: A.Decl) -> str:
    match d:
        case A.DLet(bindings=bs, recursive=rec):
            kw = "define-rec" if rec else "define"
            return f"({kw} {' '.join(_binding_to_sexp(b) for b in bs)})"
        case A.DType(name=n, params=ps, constructors=cons):
            head = f"{n}{''.join(' ' + p for p in ps)}"
            body = " ".join(
                f"({c.name} {' '.join(type_to_sexp(a) for a in c.args)})" if c.args else f"({c.name})"
                for c in cons)
            return f"(data ({head}) {body})"
        case A.DTypeAlias(name=n, params=ps, target=t):
            head = f"{n}{''.join(' ' + p for p in ps)}"
            return f"(alias ({head}) {type_to_sexp(t)})"
        case A.DImport(module=m, alias=a):
            return f"(import {m} as {a})" if a else f"(import {m})"
        case A.DExtern(name=n, annotation=t, prim=prim):
            return f"(extern {n} : {type_to_sexp(t)} = {prim!r})"
    raise AssertionError(f"unhandled declaration node {d!r}")


def module_to_sexp(m: A.Module) -> str:
    return "\n".join(decl_to_sexp(d) for d in m.decls)
