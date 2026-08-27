"""Parser tests: assert on the s-expression rendering of the AST."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from velac.astprint import module_to_sexp, to_sexp  # noqa: E402
from velac.errors import Diagnostic  # noqa: E402
from velac.parser import parse_expr, parse_module  # noqa: E402

FAILED: list[str] = []
PASSED = 0


def eq(name: str, src: str, expected: str) -> None:
    global PASSED
    try:
        got = to_sexp(parse_expr(src))
    except Diagnostic as d:
        FAILED.append(f"{name}: raised {d.message}")
        return
    if got == expected:
        PASSED += 1
    else:
        FAILED.append(f"{name}:\n    src      {src}\n    expected {expected}\n    got      {got}")


def eq_mod(name: str, src: str, expected: str) -> None:
    global PASSED
    try:
        got = module_to_sexp(parse_module(src))
    except Diagnostic as d:
        FAILED.append(f"{name}: raised {d.message}")
        return
    if got == expected:
        PASSED += 1
    else:
        FAILED.append(f"{name}:\n    src      {src}\n    expected {expected}\n    got      {got}")


def bad(name: str, src: str, module: bool = False) -> None:
    global PASSED
    try:
        parse_module(src) if module else parse_expr(src)
    except Diagnostic:
        PASSED += 1
    else:
        FAILED.append(f"{name}: expected a parse error, got none")


# -- precedence -------------------------------------------------------------

eq("add/mul", "1 + 2 * 3", "(+ 1 (* 2 3))")
eq("mul/add", "1 * 2 + 3", "(+ (* 1 2) 3)")
eq("left assoc minus", "1 - 2 - 3", "(- (- 1 2) 3)")
eq("right assoc pow", "2 ** 3 ** 4", "(** 2 (** 3 4))")
eq("cons right assoc", "1 :: 2 :: xs", "(:: 1 (:: 2 xs))")
eq("app binds tightest", "f x + g y", "(+ (app f x) (app g y))")
eq("app curried", "f x y z", "(app f x y z)")
eq("cmp vs add", "a + b == c", "(== (+ a b) c)")
eq("and/or", "a || b && c", "(or a (and b c))")
eq("not", "not a && b", "(and (not a) b)")
eq("unary minus", "-x + 1", "(+ (neg x) 1)")
eq("unary minus binds looser than pow", "-x ** 2", "(neg (** x 2))")
eq("float ops", "1.0 +. 2.0 *. 3.0", "(+. 1.0 (*. 2.0 3.0))")
eq("pipe", "x |> f", "(app f x)")
eq("pipe with args", "x |> f a", "(app f a x)")
eq("pipe chain", "x |> f |> g", "(app g (app f x))")
eq("compose", "f >> g >> h", "(>> f (>> g h))")
eq("seq", "a ; b ; c", "(seq a (seq b c))")
bad("no chained comparison", "a < b < c")

# -- atoms ------------------------------------------------------------------

eq("unit", "()", "unit")
eq("paren", "(1 + 2)", "(+ 1 2)")
eq("tuple", "(1, 2, 3)", "(tuple 1 2 3)")
eq("list", "[1, 2, 3]", "(list 1 2 3)")
eq("empty list", "[]", "(list)")
eq("trailing comma list", "[1, 2,]", "(list 1 2)")
eq("operator section", "(+)", "+")
eq("section applied", "fold (+) 0 xs", "(app fold + 0 xs)")
eq("ascription", "(x : Int)", "(the Int x)")
eq("qualified var", "List.map f xs", "(app List.map f xs)")
eq("constructor", "Some 1", "(app Some 1)")
eq("qualified con", "Option.Some 1", "(app Option.Some 1)")

# -- records ----------------------------------------------------------------

eq("record", "{ x = 1, y = 2 }", "(record (x 1) (y 2))")
eq("empty record", "{}", "(record)")
eq("record shorthand", "{ x, y }", "(record (x x) (y y))")
eq("record update", "{ p | x = 3 }", "(update p (x 3))")
eq("projection", "p.x", "(. p x)")
eq("chained projection", "p.a.b", "(. (. p a) b)")
eq("projection then app", "f p.x", "(app f (. p x))")
bad("duplicate field", "{ x = 1, x = 2 }")

# -- binders ----------------------------------------------------------------

eq("lambda", "\\x -> x", "(lambda (x) x)")
eq("lambda multi", "\\x y -> x + y", "(lambda (x y) (+ x y))")
eq("lambda pattern", "\\(a, b) -> a", "(lambda ((tuple a b)) a)")
eq("if", "if a then b else c", "(if a b c)")
eq("let", "let x = 1 in x", "(let ((x = 1)) x)")
eq("let fn", "let f a b = a in f", "(let ((f a b = a)) f)")
eq("let annotated", "let f : Int -> Int = g in f", "(let ((f : (-> Int Int) = g)) f)")
eq("let rec", "let rec f n = f n in f", "(letrec ((f n = (app f n))) f)")
eq("let destructure", "let (a, b) = p in a", "(letpat (tuple a b) p a)")
eq("let cons destructure", "let x :: xs = l in x", "(letpat (:: x xs) l x)")
eq("nested let", "let x = 1 in let y = 2 in x", "(let ((x = 1)) (let ((y = 2)) x))")

# -- match ------------------------------------------------------------------

eq("match", "match x { 0 -> a, _ -> b }", "(match x (0 -> a) (_ -> b))")
eq("match cons", "match xs { [] -> 0, y :: ys -> y }",
   "(match xs ((list) -> 0) ((:: y ys) -> y))")
eq("match con", "match o { None -> 0, Some v -> v }",
   "(match o (None -> 0) ((Some v) -> v))")
eq("match guard", "match x { n if n > 0 -> a, _ -> b }",
   "(match x (n (when (> n 0)) -> a) (_ -> b))")
eq("match as", "match x { (a, b) as p -> p }", "(match x ((as (tuple a b) p) -> p))")
eq("match leading bars", "match x { | 0 -> a, | _ -> b }", "(match x (0 -> a) (_ -> b))")
eq("match trailing comma", "match x { _ -> a, }", "(match x (_ -> a))")
eq("match record pattern", "match p { { x = a, y } -> a }",
   "(match p ((precord (x a) (y y)) -> a))")
eq("match negative literal", "match n { -1 -> a, _ -> b }", "(match n (-1 -> a) (_ -> b))")
# the brace restriction: `f x` is the scrutinee, `{` opens the arms
eq("match app scrutinee", "match f x { _ -> a }", "(match (app f x) (_ -> a))")
bad("match needs arms", "match x { }")

# -- declarations -----------------------------------------------------------

eq_mod("top let", "let x = 1", "(define (x = 1))")
eq_mod("top let fn", "let add a b = a + b", "(define (add a b = (+ a b)))")
eq_mod("two decls", "let x = 1\nlet y = 2", "(define (x = 1))\n(define (y = 2))")
eq_mod("let rec and", "let rec f n = g n and g n = f n",
       "(define-rec (f n = (app g n)) (g n = (app f n)))")
eq_mod("data decl", "type Option a = None | Some a", "(data (Option a) (None) (Some a))")
eq_mod("data leading bar", "type B = | T | F", "(data (B) (T) (F))")
eq_mod("recursive data", "type Tree a = Leaf | Node (Tree a) a (Tree a)",
       "(data (Tree a) (Leaf) (Node (Tree a) a (Tree a)))")
eq_mod("alias", "type alias Point = { x : Float, y : Float }",
       "(alias (Point) (record (x Float) (y Float)))")
eq_mod("open record type", "type alias HasX r = { x : Int | r }",
       "(alias (HasX r) (record (x Int) | r))")
eq_mod("import", "import List", "(import List)")
eq_mod("import as", "import List as L", "(import List as L)")
eq_mod("extern", 'extern add : Int -> Int -> Int = "int_add"',
       "(extern add : (-> Int (-> Int Int)) = 'int_add')")
eq_mod("list type", "let f : [Int] -> Int = g", "(define (f : (-> (list Int) Int) = g))")
eq_mod("tuple type", "let f : (Int, Bool) = p", "(define (f : (tuple Int Bool) = p))")
bad("and without rec", "let f = 1 and g = 2", module=True)
bad("junk decl", "1 + 1", module=True)

# -- multi-line realistic program ------------------------------------------

PROGRAM = """
type Option a = None | Some a

let rec map f xs =
  match xs {
    [] -> [],
    y :: ys -> f y :: map f ys,
  }

let main =
  let xs = [1, 2, 3] in
  xs |> map (\\n -> n * 2)
"""
eq_mod("program", PROGRAM,
       "(data (Option a) (None) (Some a))\n"
       "(define-rec (map f xs = (match xs ((list) -> (list)) "
       "((:: y ys) -> (:: (app f y) (app map f ys))))))\n"
       "(define (main = (let ((xs = (list 1 2 3))) "
       "(app map (lambda (n) (* n 2)) xs))))")

print(f"parser: {PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  FAIL", f)
sys.exit(1 if FAILED else 0)
