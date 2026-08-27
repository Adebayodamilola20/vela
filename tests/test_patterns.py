"""Exhaustiveness / redundancy checker tests."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from velac.lexer import tokenize  # noqa: E402
from velac.parser import Parser  # noqa: E402
from velac.patterns import check_match  # noqa: E402
from velac.registry import ConInfo, DataInfo, Registry  # noqa: E402
from velac.types import Scheme  # noqa: E402

FAILED: list[str] = []
PASSED = 0


def make_registry() -> Registry:
    reg = Registry()
    # type Option a = None | Some a
    reg.add_type(DataInfo("Option", 1, ["None", "Some"]))
    reg.add_con(ConInfo("None", "Option", 0, 0, Scheme([], None)))
    reg.add_con(ConInfo("Some", "Option", 1, 1, Scheme([], None)))
    # type Color = Red | Green | Blue
    reg.add_type(DataInfo("Color", 0, ["Red", "Green", "Blue"]))
    for i, c in enumerate(["Red", "Green", "Blue"]):
        reg.add_con(ConInfo(c, "Color", i, 0, Scheme([], None)))
    # type Tree a = Leaf | Node (Tree a) a (Tree a)
    reg.add_type(DataInfo("Tree", 1, ["Leaf", "Node"]))
    reg.add_con(ConInfo("Leaf", "Tree", 0, 0, Scheme([], None)))
    reg.add_con(ConInfo("Node", "Tree", 1, 3, Scheme([], None)))
    return reg


REG = make_registry()


def parse_pats(sources: list[str]):
    out = []
    for s in sources:
        p = Parser(tokenize(s, "<pat>"), "<pat>")
        out.append(p.parse_pattern())
    return out


def report(sources: list[str], guards: list[bool] | None = None):
    pats = parse_pats(sources)
    guards = guards or [False] * len(pats)
    return check_match(pats, guards, REG)


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(f"{name}: {detail}")


def exhaustive(name: str, sources: list[str]) -> None:
    r = report(sources)
    check(name, not r.missing, f"missing {r.missing}")


def missing(name: str, sources: list[str], expected: str) -> None:
    r = report(sources)
    check(name, r.missing == [expected], f"expected [{expected!r}], got {r.missing}")


def redundant(name: str, sources: list[str], expected: list[int]) -> None:
    r = report(sources)
    check(name, r.redundant == expected, f"expected {expected}, got {r.redundant}")


# -- wildcards --------------------------------------------------------------

exhaustive("wildcard", ["_"])
exhaustive("var", ["x"])
redundant("wildcard shadows", ["_", "1"], [1])

# -- booleans ---------------------------------------------------------------

exhaustive("bool both", ["True", "False"])
missing("bool missing False", ["True"], "False")
missing("bool missing True", ["False"], "True")
redundant("bool third arm", ["True", "False", "_"], [2])

# -- enums ------------------------------------------------------------------

exhaustive("color all", ["Red", "Green", "Blue"])
missing("color missing", ["Red", "Blue"], "Green")
missing("color missing first", ["Green", "Blue"], "Red")
redundant("color duplicate", ["Red", "Green", "Red", "Blue"], [2])

# -- option -----------------------------------------------------------------

exhaustive("option", ["None", "Some x"])
missing("option missing None", ["Some x"], "None")
missing("option missing Some", ["None"], "Some _")
exhaustive("nested option", ["None", "Some None", "Some (Some x)"])
missing("nested option gap", ["None", "Some None"], "Some (Some _)")

# -- lists ------------------------------------------------------------------

exhaustive("list nil/cons", ["[]", "x :: xs"])
missing("list missing nil", ["x :: xs"], "[]")
missing("list missing cons", ["[]"], "_ :: _")
exhaustive("list three cases", ["[]", "[x]", "x :: y :: rest"])
# the witness is the general "two or more elements", not just the 2-element list
missing("list two-elem gap", ["[]", "[x]"], "_ :: _ :: _")
missing("list literal gap", ["[]", "[x]", "[x, y]"], "_ :: _ :: _ :: _")
# a closed list literal in the last arm still leaves longer lists uncovered
missing("list exact lengths only", ["[]", "[x]", "[x, y]", "[x, y, z]"],
        "_ :: _ :: _ :: _ :: _")
redundant("list redundant", ["[]", "x :: xs", "[1]"], [2])

# -- tuples -----------------------------------------------------------------

exhaustive("tuple wild", ["(_, _)"])
exhaustive("tuple bools", ["(True, _)", "(False, True)", "(False, False)"])
missing("tuple gap", ["(True, True)", "(False, False)"], "(False, True)")
missing("tuple nested gap", ["(None, _)", "(Some x, True)"], "(Some _, False)")

# -- integer / string literals ---------------------------------------------

missing("int no wildcard", ["0", "1"], "2")
exhaustive("int with wildcard", ["0", "1", "_"])
missing("string no wildcard", ['"a"', '"b"'], '""')
redundant("int duplicate", ["0", "1", "0", "_"], [2])

# -- records ----------------------------------------------------------------

exhaustive("record wild fields", ["{ x = _, y = _ }"])
exhaustive("record shorthand", ["{ x, y }"])
missing("record bool field", ["{ x = True }"], "{ x = False }")
# differing label sets in one column are reconciled to their union
exhaustive("record union labels", ["{ x = True }", "{ y = True }", "{ x = False, y = False }",
                                   "{ x = False, y = True }", "{ x = True, y = False }"])

# -- as-patterns and guards -------------------------------------------------

exhaustive("as pattern", ["(x, y) as p"])
r = report(["x", "_"], [True, False])
check("guarded then wildcard is exhaustive", not r.missing, f"missing {r.missing}")
r = report(["x"], [True])
check("only guarded arm is not exhaustive", r.missing == ["_"], f"got {r.missing}")
r = report(["1", "_", "2"], [True, False, False])
check("arm after wildcard is redundant", r.redundant == [2], f"got {r.redundant}")

# -- deep nesting -----------------------------------------------------------

exhaustive("tree", ["Leaf", "Node _ _ _"])
missing("tree gap", ["Leaf", "Node Leaf x Leaf"], "Node (Node _ _ _) _ _")
exhaustive("deep list of options",
           ["[]", "None :: _", "Some _ :: []", "Some _ :: _ :: _"])

print(f"patterns: {PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  FAIL", f)
sys.exit(1 if FAILED else 0)
