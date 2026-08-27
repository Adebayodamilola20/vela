"""The global type/constructor registry.

Types and data constructors live in a single program-wide namespace (values do
not — those are per-module and reached through qualified names). Keeping the
registry separate from `infer` lets the pattern checker consult constructor
signatures without importing the inference engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import Span
from .types import Scheme, Type


@dataclass(slots=True)
class ConInfo:
    name: str
    type_name: str
    tag: int            # index within its data type; also the runtime tag
    arity: int
    scheme: Scheme      # forall params. arg1 -> ... -> T params
    field_types: list[Type] = field(default_factory=list)
    span: Span | None = None
    #: True for constructors the runtime represents specially (Nil/Cons/True/...)
    builtin: bool = False


@dataclass(slots=True)
class DataInfo:
    name: str
    arity: int
    constructors: list[str]
    span: Span | None = None
    builtin: bool = False


class Registry:
    def __init__(self) -> None:
        self.types: dict[str, DataInfo] = {}
        self.cons: dict[str, ConInfo] = {}
        self.aliases: dict[str, tuple[list[str], object]] = {}  # name -> (params, TypeExpr)
        self._install_builtins()

    # -- construction ------------------------------------------------------

    def add_type(self, info: DataInfo) -> None:
        self.types[info.name] = info

    def add_con(self, info: ConInfo) -> None:
        self.cons[info.name] = info

    def constructors_of(self, type_name: str) -> list[str]:
        info = self.types.get(type_name)
        return list(info.constructors) if info else []

    def type_of_con(self, con: str) -> str | None:
        info = self.cons.get(con)
        return info.type_name if info else None

    def arity_of_con(self, con: str) -> int:
        info = self.cons.get(con)
        return info.arity if info else 0

    # -- builtins ----------------------------------------------------------

    def _install_builtins(self) -> None:
        """Bool, List and Unit behave like data types for pattern purposes.

        Their `Scheme`s are filled in by `infer`, which owns the type-variable
        supply; here we only record the shapes the pattern checker needs.
        """
        from .types import Scheme as S

        self.add_type(DataInfo("Bool", 0, ["False", "True"], None, True))
        self.add_con(ConInfo("False", "Bool", 0, 0, S([], None), [], None, True))
        self.add_con(ConInfo("True", "Bool", 1, 0, S([], None), [], None, True))

        self.add_type(DataInfo("List", 1, ["Nil", "Cons"], None, True))
        self.add_con(ConInfo("Nil", "List", 0, 0, S([], None), [], None, True))
        self.add_con(ConInfo("Cons", "List", 1, 2, S([], None), [], None, True))

        self.add_type(DataInfo("Unit", 0, ["()"], None, True))
        self.add_con(ConInfo("()", "Unit", 0, 0, S([], None), [], None, True))

    # -- queries used by the pattern checker -------------------------------

    def is_complete(self, con_names: set[str]) -> bool:
        """True when `con_names` covers every constructor of its data type."""
        if not con_names:
            return False
        first = next(iter(con_names))
        type_name = self.type_of_con(first)
        if type_name is None:
            return False
        return set(self.constructors_of(type_name)) <= con_names

    def missing_constructors(self, con_names: set[str]) -> list[str]:
        if not con_names:
            return []
        type_name = self.type_of_con(next(iter(con_names)))
        if type_name is None:
            return []
        return [c for c in self.constructors_of(type_name) if c not in con_names]
