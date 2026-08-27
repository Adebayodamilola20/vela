# Vela

A small, strict, statically-typed functional language in the ML family, with
two backends: a reference interpreter in Python and a bytecode VM in C.

Everything is an expression. Types are fully inferred — Hindley–Milner with
let-polymorphism and row-polymorphic records — so annotations are optional and
checked, never required.

```vela
let rec fact n = if n <= 1 then 1 else n * fact (n - 1)

let dist p = sqrt (p.x *. p.x +. p.y *. p.y)
-- inferred: { x : Float, y : Float | r } -> Float

let main () =
  print (int_to_string (fact 10))
```

See [`docs/spec.md`](docs/spec.md) for the language definition.

## Getting started

Nothing to install — the compiler is pure Python 3.12+ with no dependencies.

```sh
python3 -m velac run examples/hello.vela
```

To build and use the bytecode VM:

```sh
make -C vm                                   # builds vm/vela
python3 -m velac build examples/hello.vela -o build/hello.velac
./vm/vela build/hello.velac
```

## The `velac` command

| Command | What it does |
|---|---|
| `velac run prog.vela [args...]` | compile and interpret |
| `velac check prog.vela` | type-check only |
| `velac check prog.vela --types` | print every inferred signature |
| `velac build prog.vela -o out.velac` | write a bytecode image |
| `velac build prog.vela --dump` | disassemble instead of writing |
| `velac ast prog.vela` | dump the surface AST |
| `velac core prog.vela` | dump the Core IR |

`velac check --types` is the fastest way to see what inference concluded:

```
$ python3 -m velac check std/Prelude.vela --types
flip : forall a b c. (a -> b -> c) -> b -> a -> c
uncurry : forall a b c. (a -> b -> c) -> (a, b) -> c
...
```

## The pipeline

```
 source .vela
   │  lexer.py      → tokens with spans
   │  parser.py     → surface AST
   │  modules.py    → dependency graph, topological order
   │  infer.py      → types (unification, levels, generalization)
   │  patterns.py   → exhaustiveness / redundancy (Maranget)
   │  core.py       → Core IR (decision trees, alpha-renamed)
   │  compile.py    → closure conversion, upvalues, bytecode
   │  emit.py       → .velac image
   ↓
 vm/  C interpreter  ── or ──  velac/interp.py (reference oracle)
```

Both backends must produce byte-identical output for every program in
`tests/cases/`. That is enforced, not merely intended:

```sh
python3 tests/run_tests.py --differential
```

## Tests

```sh
python3 tests/run_tests.py              # golden-file tests
python3 tests/run_tests.py --differential   # ...and check both backends agree
python3 tests/test_lexer.py             # unit tests, one file per stage
python3 tests/test_parser.py
python3 tests/test_infer.py
python3 tests/test_patterns.py
```

A case is a `.vela` file with a `.expected` file beside it. A case that should
fail to compile gets a `.expected-error` file holding a substring the
diagnostic must contain — so error wording can improve without churning tests,
while the error itself stays pinned.

## The standard library

`std/` is written in Vela. `Prelude` is imported implicitly; everything else is
explicit.

| Module | Contents |
|---|---|
| `Prelude` | `print`, `show`, `compare`, `panic`, tuple and function helpers |
| `List` | 40 functions, all constant-stack |
| `String` | UTF-8 text; indices count characters, not bytes |
| `Option` | a value that may be absent |
| `Result` | a value or an explanation |
| `Int` | wrapping 64-bit arithmetic, bit operations, `gcd` |
| `Float` | IEEE-754, trigonometry, `close_enough` |
| `Array` | constant-time indexing, persistent update |

## Design notes

**Numeric operators are not overloaded.** `+` is `Int -> Int -> Int`; floats
use `+.`. This keeps inference decidable without type classes. Comparison
(`==`, `<`, …) *is* polymorphic, via a total ordering defined once.

**Non-exhaustive matches are errors.** Not warnings. The checker uses
Maranget's usefulness algorithm and reports a concrete counterexample:

```
error[E0200]: this `match` is not exhaustive
  = note: no arm matches `Blue`
```

**Tail calls do not grow the stack**, on both backends. The interpreter loops
instead of recursing; the VM reuses the frame.

**Records are row-polymorphic.** A function needing `x` and `y` accepts any
record that has them, and the extra fields travel through untouched.

**The GC is non-moving mark-and-sweep**, so object identity is stable.

## Repository layout

```
velac/     the compiler and reference interpreter (Python)
vm/src/    the bytecode VM (C11)
std/       the standard library (Vela)
examples/  runnable programs
tests/     unit tests, golden-file cases, the runner
docs/      the language specification
```

`vm/src/opcodes.h` is generated from `velac/prims.py` by
`python3 -m velac.opcodes`; the Makefile regenerates it when that table
changes, so the two can never drift apart.
