<div align="center">

# Vela

**A small, strict, statically-typed functional language — with a compiler, a reference interpreter, and a bytecode VM that must agree with it byte for byte.**

[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![tests](https://img.shields.io/badge/tests-245%20passing-brightgreen.svg)](#testing)
[![backends](https://img.shields.io/badge/backends-2%20(verified%20equivalent)-brightgreen.svg)](#why-two-backends)
[![python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](#requirements)
[![C](https://img.shields.io/badge/C-C11-blue.svg)](#requirements)

*Named for the constellation — Latin for "the sails".*

</div>

---

```vela
import List

type Tree a = Leaf | Node (Tree a) a (Tree a)

let rec insert x t =
  match t {
    Leaf -> Node Leaf x Leaf,
    Node l v r ->
      if x < v then Node (insert x l) v r
      else if x > v then Node l v (insert x r)
      else t,
  }

let rec to_list t =
  match t {
    Leaf -> [],
    Node l v r -> to_list l ++ [v] ++ to_list r,
  }

let main () =
  [5, 3, 8, 1] |> List.fold_left (flip insert) Leaf |> to_list |> show |> print
```
```
[1, 3, 5, 8]
```

You wrote no types. The compiler inferred every one of them — including
`insert : forall a. a -> Tree a -> Tree a` — and would have refused to build if
you had left out the `Leaf` case.

---

## Contents

- [What Vela is](#what-vela-is)
- [Quick start](#quick-start)
- [**Full language tour →**](docs/tour.md)
- [**Language specification →**](docs/spec.md)
- [Requirements](#requirements)
- [Running programs](#running-programs)
- [A tour of the language](#a-tour-of-the-language)
- [Why two backends](#why-two-backends)
- [How it is built](#how-it-is-built)
- [The standard library](#the-standard-library)
- [Testing](#testing)
- [Design decisions](#design-decisions)
- [What Vela does not have](#what-vela-does-not-have)
- [Repository layout](#repository-layout)
- [License](#license)

---

## What Vela is

Vela is a programming language in the **ML family** — the tradition of OCaml,
Elm, F# and Haskell. It is small enough to read end to end, and complete enough
to write real programs in.

Four things define it:

**Everything is inferred.** Hindley–Milner with let-polymorphism. You may write
type annotations and they are checked, but nothing requires them. The 943-line
standard library in `std/` contains **zero** type annotations, and every
function in it is fully typed at compile time.

**Missing cases do not compile.** A `match` that fails to cover its input is an
error, not a warning — and the compiler tells you which value you forgot.

**Records are row-polymorphic.** A function needing `x` and `y` accepts any
record that has them, and the other fields travel through untouched.

**Tail calls never grow the stack.** On both backends. A loop written as
recursion runs in constant space, verified to 500,000 frames deep.

---

## Quick start

```sh
git clone https://github.com/Adebayodamilola20/vela.git
cd vela
python3 -m velac run examples/hello.vela
```

```
Hello from Vela.
```

That is the whole install. The compiler is pure Python with **no dependencies**
— no pip, no virtualenv, no build step.

To also build the fast backend:

```sh
make -C vm
python3 -m velac build examples/nqueens.vela -o build/nqueens.velac
./vm/vela build/nqueens.velac
```

```
8 queens: 92 solutions
```

---

## Requirements

| For | You need | Notes |
|---|---|---|
| Compiler + interpreter | **Python 3.12+** | No third-party packages |
| Bytecode VM | **A C11 compiler** and `make` | clang or gcc; `-lm` is the only library |

Everything else is in this repository. Nothing is downloaded at build time.

Check what you have:

```sh
python3 --version     # needs 3.12 or newer
cc --version          # clang or gcc
```

---

## Running programs

Vela has one command, `velac`, with five subcommands.

### `velac run` — compile and execute

```sh
python3 -m velac run examples/stdlib_tour.vela
```

Pass arguments to the program after the filename; they arrive via `arg_count`
and `arg_get`:

```sh
python3 -m velac run myprogram.vela alpha beta
```

### `velac check` — type-check without running

```sh
python3 -m velac check std/List.vela
```
```
ok
```

Add `--types` to print every inferred signature. This is the fastest way to see
what inference actually concluded:

```sh
python3 -m velac check std/Prelude.vela --types
```
```
flip : forall a b c. (a -> b -> c) -> b -> a -> c
uncurry : forall a b c. (a -> b -> c) -> (a, b) -> c
swap : forall a b. (a, b) -> (b, a)
compare : forall a. a -> a -> Int
...
```

Note that `check` does not require a `main`, so it works on library modules.

### `velac build` — produce a bytecode image

```sh
python3 -m velac build program.vela -o program.velac
./vm/vela program.velac
```

To read the bytecode instead of writing it:

```sh
python3 -m velac build program.vela --dump
```
```
function 23 name/1 (slots 2, upvalues 0)
      0  GET_LOCAL        0
      3  SET_LOCAL        1
      6  GET_LOCAL        1
      9  JUMP_IF_TAG      2 3         ; Red -> 17
     14  JUMP             5           ; -> 22
     17  POP
     18  CONST            0           ; 'red'
     21  RETURN
```

### `velac ast` and `velac core` — see inside the compiler

```sh
python3 -m velac ast program.vela     # the parsed syntax tree
python3 -m velac core program.vela    # the desugared intermediate form
```

`core` is the interesting one: it shows what your surface syntax actually became
— which `match` turned into a jump table, where a join function was introduced,
which names a closure captured.

### Where modules are found

`import Foo` searches, in order:

1. the directory of the importing file,
2. every entry in `$VELA_PATH` (colon-separated),
3. the bundled `std/`.

Add more directories with `-I`:

```sh
python3 -m velac run app.vela -I ./lib -I ./vendor
```

---

## A tour of the language

> A longer, worked introduction lives in **[`docs/tour.md`](docs/tour.md)** —
> about twenty minutes, every example runnable. What follows is the summary.

### Values and functions

```vela
let x = 42
let add a b = a + b
let rec fact n = if n <= 1 then 1 else n * fact (n - 1)

-- annotations are optional, and checked when present
let identity : a -> a = \x -> x
```

Everything is an expression. `if` always needs its `else`, and both branches
must have the same type.

### Types you define

```vela
type Option a = None | Some a
type Tree a   = Leaf | Node (Tree a) a (Tree a)
type alias Point = { x : Float, y : Float }
```

### Pattern matching

```vela
let describe xs =
  match xs {
    [] -> "empty",
    [x] -> "one: " ^ int_to_string x,
    [x, y] -> "two",
    x :: _ -> "many, starting " ^ int_to_string x,
  }
```

Patterns nest, bind, alias (`p as name`), and take guards:

```vela
let classify n =
  match n {
    0 -> "zero",
    x if x < 0 -> "negative",
    x if x % 2 == 0 -> "positive even",
    _ -> "positive odd",
  }
```

Leave a case out and you get an error naming the value you missed:

```
error[E0200]: this `match` is not exhaustive
  --> colours.vela:6:3
    |
  6 |   match c {
    |   ^^^^^^^^^
  = note: no arm matches `Blue`
  = note: add a `_ -> ...` arm to handle the rest
```

### Records and row polymorphism

```vela
let dist p = Float.sqrt (p.x *. p.x +. p.y *. p.y)
```

Inference gives this:

```
dist : { x : Float, y : Float | r } -> Float
```

The `| r` is a **row variable**: `dist` takes *any* record carrying `x` and `y`.

```vela
dist { x = 3.0, y = 4.0 }                              -- 5.0
dist { x = 3.0, y = 4.0, name = "corner", weight = 12 } -- 5.0, extras ignored
```

Field order never matters. Functional update copies:

```vela
let moved = { point | x = 10.0 }   -- point itself is unchanged
```

### Operators

Loosest to tightest:

| Prec | Operators | Notes |
|---|---|---|
| 1 | `;` | sequencing |
| 2 | `\|>` | pipeline: `x \|> f` is `f x` |
| 3 | `>>` `<<` | function composition |
| 4 | `\|\|` | short-circuit or |
| 5 | `&&` | short-circuit and |
| 6 | `==` `!=` `<` `<=` `>` `>=` | polymorphic, non-chaining |
| 7 | `::` `++` `^` | cons, list append, string concat |
| 8 | `+` `-` `+.` `-.` | |
| 9 | `*` `/` `%` `*.` `/.` | |
| 10 | `**` | integer exponent |
| 11 | unary `-` `not` | |

Pipelines read left to right:

```vela
List.range 1 21
  |> List.filter Int.is_odd
  |> List.map (\n -> n * n)
  |> List.filter (\n -> n > 50)
  |> List.sum
```

### Note: numeric operators are not overloaded

```vela
1 + 2        -- Int
1.0 +. 2.0   -- Float — a different operator
```

This is deliberate, and it is the design decision most likely to surprise you.
See [Design decisions](#design-decisions).

Comparison **is** polymorphic — `<`, `==` and friends work on any type, via a
single total ordering.

### Modules

One file is one module. Only top-level `let` and `type` are exported, and a
name starting with `_` is private.

```vela
import List
import Option
import String as S

let main () = print (S.to_upper "vela")
```

Import cycles are a compile error.

---

## Why two backends

This is the part of the project worth explaining properly.

```
                    ┌─→  velac/interp.py   the reference — slow, simple, correct
 source .vela  ─────┤
                    └─→  vm/vela           the C bytecode VM — 86× faster
```

The Python interpreter is **deliberately unoptimised**. Its job is to be
obviously right: it is the definition of what a Vela program *means*. The C VM
is the one built for speed.

The rule tying them together:

> **Both backends must produce byte-identical output for every program in `tests/cases/`.**

That is enforced, not merely hoped for:

```sh
python3 tests/run_tests.py --differential
```
```
10/10 passed (differential)
```

**Why it matters.** A language with one implementation has no way to tell a
deliberate behaviour from an accident — its bugs quietly become the
specification. With two, a disagreement is a signal.

This check found both real bugs during development, neither of which is visible
from inside a single implementation:

1. **Float rendering.** The VM printed `2.5e+03` where the reference said
   `2500.0`. The cause was using `%g` to make two decisions at once — choosing
   the digits *and* choosing fixed-versus-exponent form. `%g` switches to
   exponent form as soon as the exponent reaches the precision. Those are now
   separate steps, following CPython's rule.

2. **A garbage collector use-after-free.** `con_new` allocated an object header,
   then allocated its field array. A collection triggered by that *second*
   allocation swept the header, which nothing referenced yet. Any program
   building more than ~16,000 list cells crashed. Four other constructors had
   the same shape.

**The speed difference**, on `examples/nqueens.vela`:

| Backend | Time |
|---|---|
| Reference interpreter (Python) | 2.57 s |
| Bytecode VM (C) | 0.03 s |
| | **86× faster** |

Identical output, to the byte.

---

## How it is built

```
 source .vela
   │
   │  lexer.py       tokens, each carrying a source span
   │  parser.py      surface syntax tree
   │  modules.py     import graph, topological order, cycle detection
   │  infer.py       Hindley–Milner: unification, levels, generalization
   │  patterns.py    exhaustiveness and redundancy (Maranget's algorithm)
   │  core.py        Core IR — desugared, alpha-renamed, decision trees
   │  compile.py     closure conversion, upvalue resolution, bytecode
   │  emit.py        the .velac binary image
   ↓
 vm/vela  (C)   ── must agree with ──   velac/interp.py  (Python)
```

### The compiler

| Stage | What happens |
|---|---|
| **Lexing** | Source becomes tokens. Every token keeps a byte span so diagnostics can quote the offending line. |
| **Parsing** | Tokens become a tree. Operator precedence is table-driven. |
| **Module resolution** | Imports are found and ordered so dependencies are checked first. Cycles are rejected. |
| **Inference** | Types are solved by unification. Generalization uses levels (Rémy's algorithm) rather than scanning the environment. |
| **Pattern checking** | Every `match` is checked for coverage and for arms that can never fire, and a counterexample is constructed for the ones that fail. |
| **Lowering to Core** | Operators disappear into primitives, records become positional, and nested patterns become a decision tree. Every binder is renamed unique, so shadowing does not exist past this point. |
| **Bytecode** | Lexical scope becomes frames and upvalues. Calls in tail position emit `TAIL_CALL`. |

### The virtual machine

A stack machine, roughly 2,900 lines of C11:

- **Flat closures with open upvalues.** A closure holds direct references to
  what it captured, so a variable lookup is one indexed load. Upvalues point
  into the stack while their frame lives and are *closed* when it dies — which
  is what lets a `let rec` group capture siblings that do not exist yet.
- **Frame-reusing tail calls.** `TAIL_CALL` overwrites the current frame instead
  of pushing a new one. That is the constant-stack guarantee, on this side.
- **Non-moving mark-and-sweep GC.** Object identity is stable and there are no
  finalizers.
- **51 opcodes**, generated from a single table so the compiler and the VM
  cannot disagree about what a byte means.

### One source of truth for opcodes

`vm/src/opcodes.h` is **generated** from `velac/prims.py`:

```sh
python3 -m velac.opcodes      # rewrites vm/src/opcodes.h
```

The Makefile reruns this whenever the table changes. The two halves of the
system cannot drift apart, because there is only one copy of the fact.

---

## The standard library

`std/` is written **in Vela**. `Prelude` is imported implicitly; the rest is
explicit.

| Module | Contents |
|---|---|
| **Prelude** | `print`, `show`, `compare`, `panic`, `min`/`max`, tuple and function helpers |
| **List** | 40 functions — `map`, `filter`, `fold_left`, `sort_by`, `zip`, `range`… all constant-stack |
| **String** | UTF-8 text. Indices count **characters**, not bytes: `length "héllo"` is 5 |
| **Option** | a value that may be absent — Vela has no null |
| **Result** | a value, or an explanation of why there isn't one |
| **Int** | wrapping 64-bit arithmetic, bit operations, `gcd`, flooring division |
| **Float** | IEEE-754, trigonometry, `close_enough` for tolerance comparison |
| **Array** | constant-time indexing, persistent update |

Every signature in it was inferred, not declared:

```sh
python3 -m velac check std/List.vela --types
```
```
map : forall a b. (a -> b) -> [a] -> [b]
fold_left : forall a b. (a -> b -> a) -> a -> [b] -> a
zip : forall a b. [a] -> [b] -> [(a, b)]
sort_by : forall a. (a -> a -> Int) -> [a] -> [a]
...
```

---

## Testing

```sh
# unit tests, one file per compiler stage
python3 tests/test_lexer.py       # 41 checks
python3 tests/test_parser.py      # 78 checks
python3 tests/test_infer.py       # 73 checks
python3 tests/test_patterns.py    # 43 checks

# whole-program golden-file tests
python3 tests/run_tests.py

# ...and require both backends to agree byte for byte
python3 tests/run_tests.py --differential
```

Useful flags: `--filter NAME` to run a subset, `-v` to list each case,
`--update` to rewrite the expected output after an intentional change.

**How a case works.** A `.vela` file in `tests/cases/` with a `.expected` file
beside it. A program that is *supposed* to fail gets a `.expected-error` file
holding a substring the diagnostic must contain — so error wording can improve
without churning tests, while the error itself stays pinned.

**Memory checking.** The VM builds clean under AddressSanitizer and
UndefinedBehaviorSanitizer, including a GC stress case:

```sh
make -C vm debug
./vm/vela build/yourprogram.velac
```

---

## Design decisions

### Numeric operators are not overloaded

`+` is `Int -> Int -> Int`. Floats use `+.`.

**Why.** Overloading `+` across numeric types requires either type classes
(Haskell's `Num a =>`, with dictionary passing and defaulting rules) or ad-hoc
resolution that makes inference undecidable in corners. Refusing to overload
keeps full inference with no constraint machinery at all. OCaml makes the same
choice.

**The cost is real**: you will write `+` where you meant `+.`. The error message
says so directly, which is the compensation.

### Non-exhaustive matches are errors

Most languages warn. A warning in a large codebase is a thing you scroll past.

The checker uses Maranget's usefulness algorithm, which does more than detect
the gap — it **constructs a value that would fall through**, and prints it.

### Comparison is polymorphic, arithmetic is not

`<` and `==` work on any type through one total ordering defined once — over
integers, floats, strings, constructors (by tag, then field-wise), tuples, and
records (by sorted label). This is why `List.sort` needs no constraint.

### Tail calls are guaranteed, not optimised-if-you-are-lucky

The reference interpreter loops instead of recursing when an expression is in
tail position. The VM reuses the frame. Both are checked at 500,000 frames deep,
including *mutual* recursion.

### Strict evaluation, no mutation

Call-by-value, left-to-right. There are no mutable references in the surface
language — which, incidentally, is why the value restriction is unnecessary and
generalization can be unconditional at `let`.

---

## What Vela does not have

Stated plainly, because knowing the gaps matters more than a feature list:

- **No type classes or traits.** No user-defined polymorphism over types.
- **No mutation.** `Array.set` returns a new array.
- **No effect tracking.** `print` is typed as though it were pure.
- **No modules as values.** Nothing like OCaml's functors.
- **No `Map` / `Dict`.** The most keenly felt gap in the standard library.
- **No concurrency.**
- **No FFI.** Natives are a fixed table, extended by editing `velac/prims.py`.

---

## Repository layout

```
velac/          the compiler and reference interpreter (Python, ~7,900 lines)
  lexer.py        source → tokens
  parser.py       tokens → syntax tree
  infer.py        Hindley–Milner type inference
  patterns.py     exhaustiveness checking
  core.py         Core IR and pattern-match compilation
  compile.py      bytecode generation
  emit.py         .velac image format and disassembler
  interp.py       the reference interpreter
  opcodes.py      the instruction set (generates the C header)
  driver.py       the pipeline
  __main__.py     the CLI

vm/             the bytecode VM (C11, ~2,900 lines)
  src/vm.c        the interpreter loop
  src/value.c     values, rendering, garbage collection
  src/natives.c   the 50 native functions
  src/image.c     .velac loader
  Makefile

std/            the standard library, written in Vela
examples/       runnable programs
tests/          unit tests, golden cases, the runner
docs/spec.md    the language specification
```

---

## License

MIT — see [LICENSE](LICENSE).

Copyright © 2026 Stephen Adebayo.
