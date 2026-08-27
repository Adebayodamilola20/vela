"""The compilation pipeline, end to end.

`compile_program` walks the stages listed in the spec — load, declare types,
infer, check patterns, lower — and hands back a `CoreProgram` plus every
diagnostic collected along the way. Errors are accumulated rather than thrown
one at a time, so a single run reports everything it can rather than making the
user recompile once per mistake.

Two things the stage list does not make obvious:

**Types are program-wide, values are not.**  Every module's `type` declarations
are registered before *any* module is inferred, because a constructor named in
one module must resolve the same way everywhere. Values stay per-module and are
reached through qualified names.

**Prelude is implicit.**  Every module except `Prelude` itself can see its
exports unqualified. That is the only piece of ambient scope in the language.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import ast as A
from .core import CoreProgram, Lowerer
from .errors import Diagnostic, Label, SourceMap
from .infer import Checker, Env, ModuleTypes
from .modules import ModuleGraph, load_program

PRELUDE = "Prelude"


@dataclass
class CompileResult:
    program: CoreProgram | None
    errors: list[Diagnostic] = field(default_factory=list)
    warnings: list[Diagnostic] = field(default_factory=list)
    source_map: SourceMap = field(default_factory=SourceMap)
    module_types: dict[str, ModuleTypes] = field(default_factory=dict)
    entry_module: str = "Main"

    @property
    def ok(self) -> bool:
        return self.program is not None and not self.errors


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def compile_program(path: str, extra_dirs: list[str] | None = None,
                    entry: str = "main", require_entry: bool = True) -> CompileResult:
    """Compile `path` and everything it imports down to Core.

    `require_entry` is off for `velac check`, which is equally useful on a
    library module that deliberately has no `main`.
    """
    source_map = SourceMap()
    result = CompileResult(program=None, source_map=source_map)

    try:
        graph = load_program(path, extra_dirs, source_map)
    except Diagnostic as d:
        result.errors.append(d)
        return result

    root_name = graph.order[-1] if graph.order else "Main"
    result.entry_module = root_name

    # Prelude is implicitly available everywhere, so pull it in unless the
    # program is the Prelude itself or has no standard library to find.
    prelude = _load_prelude(graph, root_name)

    checker = Checker()

    # 1. Types first, across every module.
    for mod in graph.ordered():
        try:
            checker.declare_types(mod.module)
        except Diagnostic as d:
            result.errors.append(d)

    # 2. Infer each module in dependency order.
    module_types: dict[str, ModuleTypes] = {}
    for mod in graph.ordered():
        env = checker.builtins.child()

        if prelude is not None and mod.name != PRELUDE:
            for name, scheme in module_types.get(PRELUDE, _EMPTY).values.items():
                env.define(name, scheme)

        for qualified, real in graph.qualified_imports(mod).items():
            owner, _, plain = real.rpartition(".")
            types = module_types.get(owner)
            if types is None:
                continue
            scheme = types.values.get(plain)
            if scheme is not None:
                env.define(qualified, scheme)

        try:
            module_types[mod.name] = checker.infer_module(mod.module, env)
        except Diagnostic as d:
            result.errors.append(d)
            module_types[mod.name] = _EMPTY

    result.module_types = module_types
    result.errors.extend(checker.errors)
    result.warnings.extend(checker.warnings)

    if result.errors:
        return result

    # 3. Lower every module to Core.
    lowerer = Lowerer(checker.reg)
    for mod in graph.ordered():
        lowerer.register_module_types(mod.module)

    program = CoreProgram(constructors=lowerer.constructors)
    try:
        for mod in graph.ordered():
            imports = graph.qualified_imports(mod)
            if prelude is not None and mod.name != PRELUDE:
                for name in module_types.get(PRELUDE, _EMPTY).values:
                    imports.setdefault(name, f"{PRELUDE}.{name}")
            program.globals.extend(
                lowerer.lower_module(mod.module, mod.name, imports)
            )
    except Diagnostic as d:
        result.errors.append(d)
        return result

    program.constructors = lowerer.constructors
    entry_id = f"{root_name}.{entry}"
    has_entry = any(name == entry_id for name, _ in program.globals)
    if has_entry:
        program.entry = entry_id
    elif require_entry:
        result.errors.append(Diagnostic(
            f"`{root_name}` does not define `{entry}`",
            [],
            [f"a runnable program needs `let {entry} () = ...` at the top level"],
            "E0004",
        ))
        return result
    result.program = program
    return result


_EMPTY = ModuleTypes({}, [])


def _load_prelude(graph: ModuleGraph, root_name: str) -> str | None:
    """Load `std/Prelude.vela` into the graph ahead of everything else."""
    if root_name == PRELUDE or PRELUDE in graph.modules:
        return PRELUDE if PRELUDE in graph.modules else None

    from .modules import STD_DIR

    path = os.path.join(STD_DIR, f"{PRELUDE}.vela")
    if not os.path.isfile(path):
        return None

    loaded = graph._load_file(path, PRELUDE, span=None)
    graph._visit(loaded)
    # `_visit` appends to `order`, so Prelude now sits before its dependents
    # only if it was loaded first; make that true explicitly.
    if graph.order[-1] == PRELUDE and len(graph.order) > 1:
        graph.order.remove(PRELUDE)
        graph.order.insert(0, PRELUDE)
    return PRELUDE


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def report(result: CompileResult, stream, color: bool = True) -> None:
    """Print every diagnostic, warnings first so errors end up nearest the prompt."""
    for warning in result.warnings:
        stream.write(result.source_map.render(warning, color) + "\n")
    for err in result.errors:
        stream.write(result.source_map.render(err, color) + "\n")

    if result.errors:
        n = len(result.errors)
        stream.write(f"\n{n} error{'s' if n != 1 else ''}\n")
