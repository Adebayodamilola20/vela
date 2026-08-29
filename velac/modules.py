"""Module resolution: find imports, order them, reject cycles.

Search order for `import Foo`, per the spec:

1.  the directory of the file doing the importing,
2.  each entry of `VELA_PATH` (colon-separated),
3.  the bundled `std/`.

Loading is depth-first from the root module. Because each module is parsed
exactly once and memoised by resolved path, a diamond (`A` imports `B` and `C`,
both of which import `D`) parses `D` once and orders it before either.

Cycles are a compile error rather than something to be broken arbitrarily:
Vela generalises `let` at module scope, and a cycle would make the order in
which two modules' schemes are generalised ambiguous.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import ast as A
from .errors import Diagnostic, Label, Span, SourceMap
from .parser import parse_module

#: Where the bundled standard library lives, relative to this file.
STD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "std")

SOURCE_SUFFIX = ".vela"


@dataclass(slots=True)
class LoadedModule:
    """One parsed module plus where it came from."""

    name: str
    path: str
    module: A.Module
    #: Local alias -> real module name, from this module's `import` decls.
    imports: dict[str, str] = field(default_factory=dict)
    #: Real module names this one depends on, in source order.
    deps: list[str] = field(default_factory=list)


class ModuleGraph:
    """The set of modules reachable from a root, in dependency order."""

    def __init__(self, source_map: SourceMap | None = None) -> None:
        self.modules: dict[str, LoadedModule] = {}
        self.order: list[str] = []
        self.source_map = source_map if source_map is not None else SourceMap()
        self._search_path: list[str] = []
        #: Names currently being loaded, for cycle reporting.
        self._loading: list[str] = []

    # -- search path -------------------------------------------------------

    @staticmethod
    def _env_path() -> list[str]:
        raw = os.environ.get("VELA_PATH", "")
        return [p for p in raw.split(os.pathsep) if p]

    def _candidates(self, name: str, importer_dir: str) -> list[str]:
        parts = name.split(".")
        rel = os.path.join(*parts) + SOURCE_SUFFIX
        roots = [importer_dir, *self._env_path(), *self._search_path, STD_DIR]
        seen: set[str] = set()
        out: list[str] = []
        for root in roots:
            candidate = os.path.normpath(os.path.join(root, rel))
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
        return out

    def add_search_dir(self, path: str) -> None:
        if path not in self._search_path:
            self._search_path.append(path)

    # -- loading -----------------------------------------------------------

    def load_root(self, path: str, name: str | None = None) -> LoadedModule:
        """Parse `path` and everything it transitively imports."""
        self._check_root_path(path)
        path = os.path.abspath(path)
        if name is None:
            name = os.path.splitext(os.path.basename(path))[0]
        root = self._load_file(path, name, span=None)
        self._visit(root)
        return root

    @staticmethod
    def _check_root_path(path: str) -> None:
        """Reject an obviously wrong argument with advice rather than errno.

        The common cases are a directory (often because a shell expanded
        something — zsh turns `...` into `../..`) and a file that exists but
        is not Vela source. Both used to surface as a bare OSError message.
        """
        if os.path.isdir(path):
            entries = []
            try:
                entries = sorted(
                    e for e in os.listdir(path) if e.endswith(SOURCE_SUFFIX)
                )[:4]
            except OSError:
                pass

            notes = [f"`{path}` is a directory, not a file."]
            if entries:
                notes.append("did you mean one of these?")
                notes.extend(f"  {os.path.join(path, e)}" for e in entries)
            else:
                notes.append(f"pass the path to a {SOURCE_SUFFIX} file.")
            # Worth calling out: it is rarely what the user typed.
            notes.append("note that zsh expands `...` to `../..`.")
            raise Diagnostic(f"expected a {SOURCE_SUFFIX} file", [],
                             notes=notes, code="E0001")

        if not os.path.exists(path):
            raise Diagnostic(
                f"no such file: {path}", [],
                notes=[f"pass the path to a {SOURCE_SUFFIX} file."],
                code="E0001")

        if not path.endswith(SOURCE_SUFFIX):
            raise Diagnostic(
                f"`{path}` is not a {SOURCE_SUFFIX} file", [],
                notes=["Vela source files end in .vela."],
                code="E0001")

    def _read(self, path: str, span: Span | None) -> str:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise Diagnostic(
                f"cannot read {path}: {exc.strerror or exc}",
                [Label(span, "imported here")] if span else [],
                code="E0001",
            ) from exc

    def _load_file(self, path: str, name: str, span: Span | None) -> LoadedModule:
        existing = self.modules.get(name)
        if existing is not None:
            return existing

        text = self._read(path, span)
        self.source_map.add(path, text)
        module = parse_module(text, filename=path, name=name)

        loaded = LoadedModule(name=name, path=path, module=module)
        for decl in module.decls:
            if isinstance(decl, A.DImport):
                alias = decl.alias or decl.module
                loaded.imports[alias] = decl.module
                if decl.module not in loaded.deps:
                    loaded.deps.append(decl.module)

        self.modules[name] = loaded
        return loaded

    def _resolve(self, name: str, importer: LoadedModule, span: Span | None) -> str:
        for candidate in self._candidates(name, os.path.dirname(importer.path)):
            if os.path.isfile(candidate):
                return candidate

        searched = self._candidates(name, os.path.dirname(importer.path))
        raise Diagnostic(
            f"cannot find module `{name}`",
            [Label(span, "imported here")] if span else [],
            notes=[
                "searched:",
                *(f"  {p}" for p in searched[:6]),
                "set VELA_PATH to add more search directories",
            ],
            code="E0002",
        )

    def _import_span(self, module: A.Module, name: str) -> Span | None:
        for decl in module.decls:
            if isinstance(decl, A.DImport) and decl.module == name:
                return decl.span
        return None

    def _visit(self, mod: LoadedModule) -> None:
        """Depth-first load, appending to `order` after all dependencies."""
        if mod.name in self.order:
            return

        if mod.name in self._loading:
            cycle = [*self._loading[self._loading.index(mod.name):], mod.name]
            raise Diagnostic(
                f"import cycle: {' -> '.join(cycle)}",
                [Label(mod.module.decls[0].span, "in this module")]
                if mod.module.decls else [],
                notes=["Vela generalises module-level bindings, so imports must "
                       "form a directed acyclic graph."],
                code="E0003",
            )

        self._loading.append(mod.name)
        for dep_name in mod.deps:
            span = self._import_span(mod.module, dep_name)
            known = self.modules.get(dep_name)
            if known is None:
                path = self._resolve(dep_name, mod, span)
                known = self._load_file(path, dep_name, span)
            self._visit(known)
        self._loading.pop()

        self.order.append(mod.name)

    # -- queries -----------------------------------------------------------

    def ordered(self) -> list[LoadedModule]:
        """Every loaded module, dependencies before dependents."""
        return [self.modules[name] for name in self.order]

    def qualified_imports(self, mod: LoadedModule) -> dict[str, str]:
        """Map the names `mod` can see (`List.map`, and aliases) to real ones.

        The value is the *canonical* qualified name (`List.map`), which is also
        the global id the lowerer assigns, so this doubles as the import table
        Core lowering needs.
        """
        out: dict[str, str] = {}
        for alias, real in mod.imports.items():
            dep = self.modules.get(real)
            if dep is None:
                continue
            for name in _exported_names(dep.module):
                out[f"{alias}.{name}"] = f"{real}.{name}"
        return out


def _exported_names(module: A.Module) -> list[str]:
    """Top-level value names a module exports (leading `_` is private)."""
    names: list[str] = []
    for decl in module.decls:
        if isinstance(decl, A.DLet):
            names.extend(b.name for b in decl.bindings if not b.name.startswith("_"))
        elif isinstance(decl, A.DExtern) and not decl.name.startswith("_"):
            names.append(decl.name)
    return names


def load_program(path: str, extra_dirs: list[str] | None = None,
                 source_map: SourceMap | None = None) -> ModuleGraph:
    """Load `path` and its imports, ready for type-checking in `order`."""
    graph = ModuleGraph(source_map)
    for d in extra_dirs or ():
        graph.add_search_dir(d)
    graph.load_root(path)
    return graph
