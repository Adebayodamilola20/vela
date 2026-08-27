"""Type inference tests.

`ty("expr")` returns the inferred, generalized type rendered as a string.
`prog(src, "name")` type-checks a whole module and returns the scheme of one
top-level binding.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from velac.errors import Diagnostic  # noqa: E402
from velac.infer import Checker, Env  # noqa: E402
from velac.parser import parse_expr, parse_module  # noqa: E402
from velac.types import generalize, show_scheme  # noqa: E402

FAILED: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(f"{name}: {detail}")


def infer_expr_type(src: str) -> str:
    c = Checker()
    env = Env(c.builtins)
    e = parse_expr(src)
    c.supply.enter()
    t = c.infer(e, env)
    c.supply.exit()
    if c.errors:
        raise c.errors[0]
    return show_scheme(generalize(t, c.supply.level))


def ty(name: str, src: str, expected: str) -> None:
    try:
        got = infer_expr_type(src)
    except Diagnostic as d:
        FAILED.append(f"{name}: unexpected error: {d.message}")
        return
    check(name, got == expected, f"\n    src      {src}\n    expected {expected}\n    got      {got}")


def prog(name: str, src: str, binding: str, expected: str) -> None:
    try:
        c = Checker()
        env = Env(c.builtins)
        m = parse_module(src)
        c.declare_types(m)
        result = c.infer_module(m, env)
        if c.errors:
            raise c.errors[0]
        got = show_scheme(result.values[binding])
    except Diagnostic as d:
        FAILED.append(f"{name}: unexpected error: {d.message}")
        return
    except KeyError:
        FAILED.append(f"{name}: no binding named {binding!r}")
        return
    check(name, got == expected, f"\n    expected {expected}\n    got      {got}")


def bad(name: str, src: str, fragment: str = "") -> None:
    """Expect at least one type error, optionally containing `fragment`."""
    global PASSED
    c = Checker()
    env = Env(c.builtins)
    try:
        m = parse_module(src)
        c.declare_types(m)
        c.infer_module(m, env)
    except Diagnostic as d:
        c.errors.append(d)
    if not c.errors:
        FAILED.append(f"{name}: expected a type error, got none")
        return
    if fragment and not any(fragment in e.message or any(fragment in n for n in e.notes)
                            for e in c.errors):
        FAILED.append(f"{name}: expected message containing {fragment!r}, "
                      f"got {[e.message for e in c.errors]}")
        return
    PASSED += 1


# -- literals and operators -------------------------------------------------

ty("int", "1", "Int")
ty("float", "1.5", "Float")
ty("string", '"hi"', "String")
ty("bool", "True", "Bool")
ty("unit", "()", "Unit")
ty("arith", "1 + 2 * 3", "Int")
ty("float arith", "1.0 +. 2.0", "Float")
ty("compare", "1 < 2", "Bool")
ty("poly equality", "\\a b -> a == b", "forall a. a -> a -> Bool")
ty("concat", '"a" ^ "b"', "String")
ty("cons", "1 :: [2]", "[Int]")
ty("append", "\\xs ys -> xs ++ ys", "forall a. [a] -> [a] -> [a]")
ty("negate", "-5", "Int")
ty("not", "not True", "Bool")
ty("section", "(+)", "Int -> Int -> Int")

# -- functions --------------------------------------------------------------

ty("identity", "\\x -> x", "forall a. a -> a")
ty("const", "\\x y -> x", "forall a b. a -> b -> a")
ty("apply", "\\f x -> f x", "forall a b. (a -> b) -> a -> b")
ty("compose op", "\\f g -> f >> g", "forall a b c. (a -> b) -> (b -> c) -> a -> c")
ty("flip", "\\f a b -> f b a", "forall a b c. (a -> b -> c) -> b -> a -> c")
ty("curried add", "\\x -> \\y -> x + y", "Int -> Int -> Int")
ty("higher order", "\\f -> f 1", "forall a. (Int -> a) -> a")

# -- let polymorphism -------------------------------------------------------

ty("let poly", "let id = \\x -> x in (id 1, id True)", "(Int, Bool)")
ty("let generalizes", "let f = \\x -> x in f", "forall a. a -> a")
ty("lambda arg is monomorphic ok", "(\\f -> f 1) (\\x -> x)", "Int")
bad("lambda arg not polymorphic",
    "let bad = (\\f -> (f 1, f True)) (\\x -> x)")

# -- data types -------------------------------------------------------------

OPTION = "type Option a = None | Some a\n"
prog("constructor", OPTION + "let f = Some", "f", "forall a. a -> Option a")
prog("nullary con", OPTION + "let f = None", "f", "forall a. Option a")
prog("match option", OPTION + """
let get d o = match o { None -> d, Some x -> x }
""", "get", "forall a. a -> Option a -> a")
prog("recursive type", """
type Tree a = Leaf | Node (Tree a) a (Tree a)
let rec size t = match t { Leaf -> 0, Node l _ r -> size l + 1 + size r }
""", "size", "forall a. Tree a -> Int")
prog("mutual types", """
type Expr = Num Int | Neg Expr | Add Expr Expr
let rec eval e = match e { Num n -> n, Neg x -> -(eval x), Add a b -> eval a + eval b }
""", "eval", "Expr -> Int")

# -- lists and recursion ----------------------------------------------------

prog("map", """
let rec map f xs = match xs { [] -> [], y :: ys -> f y :: map f ys }
""", "map", "forall a b. (a -> b) -> [a] -> [b]")
prog("fold", """
let rec foldl f acc xs = match xs { [] -> acc, y :: ys -> foldl f (f acc y) ys }
""", "foldl", "forall a b. (a -> b -> a) -> a -> [b] -> a")
prog("length", """
let rec length xs = match xs { [] -> 0, _ :: ys -> 1 + length ys }
""", "length", "forall a. [a] -> Int")
prog("mutual recursion", """
let rec is_even n = if n == 0 then True else is_odd (n - 1)
and is_odd n = if n == 0 then False else is_even (n - 1)
""", "is_even", "Int -> Bool")

# -- tuples -----------------------------------------------------------------

ty("tuple", "(1, True)", "(Int, Bool)")
ty("swap", "\\p -> match p { (a, b) -> (b, a) }", "forall a b. (a, b) -> (b, a)")
ty("nested tuple", "((1, 2), 3)", "((Int, Int), Int)")

# -- records and row polymorphism ------------------------------------------

ty("record", "{ x = 1, y = True }", "{ x : Int, y : Bool }")
ty("empty record", "{}", "{}")
ty("projection is row polymorphic", "\\p -> p.x", "forall a b. { x : a | b } -> a")
ty("two fields", "\\p -> p.x + p.y",
   "forall a. { x : Int, y : Int | a } -> Int")
ty("record update keeps type", "\\p -> { p | x = 1 }",
   "forall a. { x : Int | a } -> { x : Int | a }")
ty("projection on literal", "{ x = 1 }.x", "Int")
ty("record pattern is open", "\\r -> match r { { x = a } -> a }",
   "forall a b. { x : a | b } -> a")
bad("missing field", "let f = { x = 1 }.y", "no field `y`")
bad("field type clash", "let f = \\p -> p.x + p.x\nlet g = f { x = True }")

# -- annotations ------------------------------------------------------------

prog("annotation ok", "let id : a -> a = \\x -> x", "id", "forall a. a -> a")
prog("annotation specializes", "let f : Int -> Int = \\x -> x", "f", "Int -> Int")
prog("ascription", "let n = (1 : Int)", "n", "Int")
bad("annotation too general", "let bad : a -> a = \\x -> x + 1")
bad("annotation wrong", "let bad : Int -> Bool = \\x -> x + 1")
bad("bad ascription", 'let bad = ("hi" : Int)')
prog("polymorphic recursion", """
type Nested a = Flat a | Deep (Nested (a, a))
let rec depth : Nested a -> Int = \\n ->
  match n { Flat _ -> 0, Deep inner -> 1 + depth inner }
""", "depth", "forall a. Nested a -> Int")

# -- errors -----------------------------------------------------------------

bad("int plus bool", "let x = 1 + True")
bad("int plus float", "let x = 1 + 1.0", "Int and Float are distinct")
bad("unknown value", "let x = nonexistent")
bad("unknown constructor", "let x = Nope 1")
bad("wrong con arity", OPTION + "let x = Some 1 2")
bad("if branches differ", "let x = if True then 1 else False")
bad("non-bool condition", "let x = if 1 then 1 else 2")
bad("occurs check", "let f = \\x -> x x", "infinite type")
bad("non-exhaustive", OPTION + "let f = \\o -> match o { Some x -> x }", "not exhaustive")
bad("bad list", "let x = [1, True]")
bad("seq needs unit", "let x = 1 ; 2", "must have type Unit")
bad("duplicate binding", "let x = 1\nlet x = 2", "more than once")
bad("unbound type var in data", "type Bad = Con a", "not bound")
bad("dup pattern var", OPTION + "let f = \\o -> match o { Some x -> x, None -> 0 }\n"
    "let g = \\p -> match p { (x, x) -> x }", "bound twice")
bad("refutable let", OPTION + "let f = \\o -> let Some x = o in x", "can fail")

# -- suggestions ------------------------------------------------------------


def has_note(src: str, fragment: str) -> bool:
    c = Checker()
    env = Env(c.builtins)
    m = parse_module(src)
    c.declare_types(m)
    c.infer_module(m, env)
    return any(fragment in n for e in c.errors for n in e.notes)


check("typo suggestion", has_note("let length = 1\nlet x = lenth", "did you mean `length`"),
      "no suggestion produced")
check("type typo suggestion", has_note("let x : Itn = 1", "did you mean `Int`"),
      "no type suggestion produced")

# -- warnings ---------------------------------------------------------------


def warnings_of(src: str) -> list[str]:
    c = Checker()
    env = Env(c.builtins)
    m = parse_module(src)
    c.declare_types(m)
    c.infer_module(m, env)
    return [w.message for w in c.warnings]


check("unreachable arm warned",
      any("unreachable" in w for w in warnings_of(
          "let f = \\n -> match n { _ -> 1, 0 -> 2 }")),
      "expected an unreachable-arm warning")
check("no spurious warning",
      not warnings_of("let f = \\n -> match n { 0 -> 1, _ -> 2 }"),
      "unexpected warning")

print(f"infer: {PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  FAIL", f)
sys.exit(1 if FAILED else 0)
