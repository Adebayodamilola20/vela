"""Human-readable Core IR, for `velac core`.

Core is what the type checker and the pattern compiler have finished with, so
reading it is the quickest way to see what a surface construct actually costs —
which `match` became a jump table, where a join function was introduced, which
names a closure captured.

Output is indented pseudo-Vela rather than s-expressions: decision trees nest
deeply, and parentheses stop being legible several levels down.
"""

from __future__ import annotations

from .core import (
    CApp, CCase, CCaseLit, CCon, CFail, CGetField, CGlobal, CIf, CLam, CLet,
    CLetRec, CLit, CNative, CoreProgram, CPrim, CRecord, CRecordGet,
    CRecordSet, CSeq, CTuple, CVar,
)
from .values import quote_string, show_float

INDENT = "  "


def render_expr(e: object, depth: int = 0) -> str:
    pad = INDENT * depth
    inner = INDENT * (depth + 1)

    match e:
        case CLit(kind=kind, value=value):
            return _literal(kind, value)

        case CVar(name=name):
            return name

        case CGlobal(name=name):
            return f"@{name}"

        case CLam(params=params, body=body, name=name):
            label = "" if name == "<anonymous>" else f" {{{name}}}"
            joined = " ".join(params) if params else "()"
            return f"\\{joined}{label} ->\n{inner}{render_expr(body, depth + 1)}"

        case CApp(fn=fn, args=args):
            parts = " ".join(_atom(a, depth) for a in args)
            return f"({_atom(fn, depth)} {parts})" if args else f"({_atom(fn, depth)})"

        case CLet(name=name, value=value, body=body):
            return (f"let {name} = {render_expr(value, depth + 1)} in\n"
                    f"{pad}{render_expr(body, depth)}")

        case CLetRec(bindings=bindings, body=body):
            lines = [f"let rec"]
            for i, (name, value) in enumerate(bindings):
                keyword = "" if i == 0 else f"{pad}and"
                lines.append(f"{keyword} {name} = {render_expr(value, depth + 1)}")
            lines.append(f"{pad}in\n{pad}{render_expr(body, depth)}")
            return "\n".join(lines)

        case CIf(cond=cond, then=then, otherwise=otherwise):
            return (f"if {render_expr(cond, depth)}\n"
                    f"{inner}then {render_expr(then, depth + 1)}\n"
                    f"{inner}else {render_expr(otherwise, depth + 1)}")

        case CSeq(first=first, second=second):
            return f"{render_expr(first, depth)};\n{pad}{render_expr(second, depth)}"

        case CPrim(op=op, args=args):
            return f"#{op}({', '.join(render_expr(a, depth) for a in args)})"

        case CNative(name=name, args=args):
            return f"%{name}({', '.join(render_expr(a, depth) for a in args)})"

        case CCon(name=name, con_id=con_id, args=args):
            if not args:
                return f"{name}#{con_id}"
            body = ", ".join(render_expr(a, depth) for a in args)
            return f"{name}#{con_id}({body})"

        case CTuple(items=items):
            return "(" + ", ".join(render_expr(i, depth) for i in items) + ")"

        case CGetField(value=value, index=index):
            return f"{_atom(value, depth)}.{index}"

        case CCase(scrutinee=scrutinee, cases=cases, default=default):
            lines = [f"case {render_expr(scrutinee, depth)} of"]
            for con_id, body in cases:
                lines.append(f"{inner}#{con_id} -> {render_expr(body, depth + 2)}")
            if default is not None:
                lines.append(f"{inner}_ -> {render_expr(default, depth + 2)}")
            return "\n".join(lines)

        case CCaseLit(scrutinee=scrutinee, cases=cases, default=default, kind=kind):
            lines = [f"case {render_expr(scrutinee, depth)} of  -- {kind}"]
            for value, body in cases:
                lines.append(f"{inner}{_literal(kind, value)} -> "
                             f"{render_expr(body, depth + 2)}")
            if default is not None:
                lines.append(f"{inner}_ -> {render_expr(default, depth + 2)}")
            return "\n".join(lines)

        case CRecord(labels=labels, values=values):
            body = ", ".join(f"{k} = {render_expr(v, depth)}"
                             for k, v in zip(labels, values))
            return "{ " + body + " }"

        case CRecordGet(value=value, label=label):
            return f"{_atom(value, depth)}.{label}"

        case CRecordSet(base=base, labels=labels, values=values):
            body = ", ".join(f"{k} = {render_expr(v, depth)}"
                             for k, v in zip(labels, values))
            return "{ " + f"{render_expr(base, depth)} | {body}" + " }"

        case CFail(message=message):
            return f"fail {quote_string(message)}"

    return f"<{type(e).__name__}>"


def _atom(e: object, depth: int) -> str:
    """Render, parenthesising anything that would not bind tightly enough."""
    text = render_expr(e, depth)
    if isinstance(e, (CVar, CGlobal, CLit, CTuple, CRecord, CGetField,
                      CRecordGet, CApp, CPrim, CNative)):
        return text
    if isinstance(e, CCon) and not e.args:
        return text
    return f"({text})"


def _literal(kind: str, value: object) -> str:
    if kind == "String":
        return quote_string(str(value))
    if kind == "Float":
        return show_float(float(value))
    if kind == "Bool":
        return "True" if value else "False"
    if kind == "Unit":
        return "()"
    return str(value)


def render_program(program: CoreProgram) -> str:
    out: list[str] = []

    if program.constructors:
        out.append("-- constructors")
        for i, (name, arity, type_name) in enumerate(program.constructors):
            out.append(f"--   {i:>3}  {name}/{arity} : {type_name}")
        out.append("")

    for name, value in program.globals:
        out.append(f"{name} =")
        out.append(INDENT + render_expr(value, 1))
        out.append("")

    if program.entry:
        out.append(f"-- entry: {program.entry}")
    return "\n".join(out)
