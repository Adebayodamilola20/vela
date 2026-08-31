"""The `velac` command line.

    velac run    prog.vela [args...]   compile and interpret
    velac check  prog.vela             type-check only
    velac build  prog.vela -o out.velac
    velac ast    prog.vela             dump the surface AST
    velac core   prog.vela             dump the Core IR

`run` uses the reference interpreter. The bytecode VM is reached by building an
image and passing it to `vm/vela`; `tests/run_tests.py --differential` checks
the two agree.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

from .driver import compile_program, report
from .errors import Diagnostic
from .values import VelaPanic

# Every stage — parsing, inference, lowering, the interpreter — walks the tree
# recursively, so a deeply nested expression costs Python stack in proportion
# to its depth. CPython's default ceiling is around a thousand frames, which a
# few hundred levels of nesting is enough to reach, so the work runs on a
# thread with a stack sized to match a raised limit.
_STACK_BYTES = 64 * 1024 * 1024
_RECURSION_LIMIT = 30000


def _use_color(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(stream, "isatty") and stream.isatty()


def _search_dirs(args) -> list[str]:
    return list(args.include or ())


def cmd_run(args) -> int:
    result = compile_program(args.file, _search_dirs(args))
    report(result, sys.stderr, _use_color(sys.stderr))
    if not result.ok:
        return 1

    from .interp import run_program

    try:
        run_program(result.program, argv=args.args)
    except VelaPanic as exc:
        location = f"{exc.span}: " if exc.span is not None else ""
        sys.stderr.write(f"runtime error: {location}{exc.message}\n")
        return 70
    except RecursionError:
        sys.stderr.write("runtime error: stack overflow (infinite recursion?)\n")
        return 70
    return 0


def cmd_check(args) -> int:
    result = compile_program(args.file, _search_dirs(args), require_entry=False)
    report(result, sys.stderr, _use_color(sys.stderr))
    if not result.ok:
        return 1

    if args.types:
        types = result.module_types.get(result.entry_module)
        if types is not None:
            for name in types.order:
                sys.stdout.write(f"{name} : {types.values[name]}\n")
    else:
        sys.stderr.write("ok\n")
    return 0


def cmd_build(args) -> int:
    result = compile_program(args.file, _search_dirs(args))
    report(result, sys.stderr, _use_color(sys.stderr))
    if not result.ok:
        return 1

    from .compile import compile_core, CompileError
    from .emit import disassemble, write_image

    try:
        image = compile_core(result.program)
    except CompileError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    if args.dump:
        sys.stdout.write(disassemble(image) + "\n")
        return 0

    out = args.output or os.path.splitext(args.file)[0] + ".velac"
    write_image(image, out)
    sys.stderr.write(f"wrote {out}\n")
    return 0


def cmd_ast(args) -> int:
    from .astprint import module_to_sexp
    from .errors import SourceMap
    from .modules import load_program

    try:
        graph = load_program(args.file, _search_dirs(args), SourceMap())
    except Diagnostic as d:
        sys.stderr.write(str(d) + "\n")
        return 1

    for mod in graph.ordered():
        if len(graph.order) > 1:
            sys.stdout.write(f"-- module {mod.name}\n")
        sys.stdout.write(module_to_sexp(mod.module) + "\n")
    return 0


def cmd_core(args) -> int:
    result = compile_program(args.file, _search_dirs(args))
    report(result, sys.stderr, _use_color(sys.stderr))
    if not result.ok:
        return 1

    from .coreprint import render_program

    sys.stdout.write(render_program(result.program) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="velac", description="The Vela compiler and interpreter.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("file", help="the .vela file to compile")
        p.add_argument("-I", "--include", action="append", metavar="DIR",
                       help="an extra directory to search for modules")
        return p

    run = add_common(sub.add_parser("run", help="compile and interpret"))
    run.add_argument("args", nargs=argparse.REMAINDER,
                     help="arguments passed to the program")
    run.set_defaults(func=cmd_run)

    check = add_common(sub.add_parser("check", help="type-check only"))
    check.add_argument("--types", action="store_true",
                       help="print the inferred type of every export")
    check.set_defaults(func=cmd_check)

    build = add_common(sub.add_parser("build", help="write a bytecode image"))
    build.add_argument("-o", "--output", help="output path")
    build.add_argument("--dump", action="store_true",
                       help="disassemble to stdout instead of writing a file")
    build.set_defaults(func=cmd_build)

    add_common(sub.add_parser("ast", help="dump the surface AST")).set_defaults(
        func=cmd_ast)
    add_common(sub.add_parser("core", help="dump the Core IR")).set_defaults(
        func=cmd_core)

    return parser


def _dispatch(args) -> int:
    try:
        return args.func(args)
    except Diagnostic as d:
        sys.stderr.write(f"error: {d.message}\n")
        return 1
    except RecursionError:
        # Past the raised limit. Report it the way any other input problem is
        # reported, rather than letting a traceback out.
        sys.stderr.write(
            f"error: {args.file}: nested too deeply for the compiler\n")
        return 1
    except BrokenPipeError:
        return 0


def _run_deep(work) -> int:
    """Run `work` on a thread with room to recurse, and return its status."""
    outcome: list[object] = []

    def entry() -> None:
        sys.setrecursionlimit(_RECURSION_LIMIT)
        try:
            outcome.append(work())
        except BaseException as exc:   # re-raised on the calling thread
            outcome.append(exc)

    try:
        threading.stack_size(_STACK_BYTES)
    except (ValueError, RuntimeError):
        pass   # the platform will not take the hint; the default has to do

    # A daemon, so an interrupt during a long compile can still end the process.
    thread = threading.Thread(target=entry, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130

    if not outcome:
        return 1
    result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    assert isinstance(result, int)
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run_deep(lambda: _dispatch(args))
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
