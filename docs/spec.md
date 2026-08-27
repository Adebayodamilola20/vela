# The Vela Language — Specification v0.1

Vela is a small, strict, statically-typed functional language in the ML family.
Everything is an expression. Types are fully inferred (Hindley–Milner with
let-polymorphism); annotations are optional and checked, never required.

---

## 1. Lexical structure

### Comments

```vela
-- line comment, runs to end of line
{- block comment, {- nests properly -} -}
```

### Literals

| Kind    | Examples                                   |
|---------|--------------------------------------------|
| Int     | `0`, `42`, `1_000_000`, `0xff`, `0b1011`   |
| Float   | `3.14`, `1.0e-9`, `2.5e10`                 |
| String  | `"hi"`, `"tab\there"`, `"\u{1F600}"`       |
| Bool    | `True`, `False`                            |
| Unit    | `()`                                       |

Ints are 64-bit two's complement and wrap on overflow. Floats are IEEE-754
binary64. String literals are UTF-8; `\n \r \t \\ \" \0 \u{...}` are escapes.

### Identifiers

- *Lowercase* identifiers (`foo`, `map'`, `is_even`) name values and type variables.
- *Uppercase* identifiers (`Some`, `List`, `Int`) name constructors, types and modules.
- Trailing `'` is allowed. `_` alone is the wildcard; `_name` is a deliberately unused binding.

### Keywords

```
let rec and in if then else match type alias import as extern
True False not do
```

### Operators

Listed loosest to tightest. All are left-associative unless marked.

| Prec | Operators                    | Assoc | Notes                          |
|------|------------------------------|-------|--------------------------------|
| 1    | `;`                          | right | sequencing, discards the left  |
| 2    | `\|>`                        | left  | `x \|> f`  ==  `f x`           |
| 3    | `>>` `<<`                    | right | function composition           |
| 4    | `\|\|`                       | right | short-circuit                  |
| 5    | `&&`                         | right | short-circuit                  |
| 6    | `== != < <= > >=`            | none  | non-chaining                   |
| 7    | `::`  `++`  `^`              | right | cons, list append, string cat  |
| 8    | `+ - +. -.`                  | left  |                                |
| 9    | `* / % *. /.`                | left  |                                |
| 10   | `**`                         | right | Int exponent                   |
| 11   | unary `-` `-.` `not`         | —     | prefix; `-x ** 2` is `-(x**2)` |
| 12   | juxtaposition (application)  | left  | `f x y`                        |
| 13   | `.field`                     | left  | record projection              |

`and` joins bindings in a `let rec` group, so boolean conjunction is spelled
`&&` and disjunction `||`.

Numeric operators are *not* overloaded: `+` is `Int -> Int -> Int` and `+.` is
`Float -> Float -> Float`. This keeps inference decidable without type classes.

---

## 2. Types

```
τ ::= Int | Float | Bool | String | Unit
    | a                       -- type variable
    | τ -> τ                  -- function
    | (τ, τ, ...)             -- tuple, arity >= 2
    | [τ]                     -- list
    | T τ...                  -- type constructor application
    | { l : τ, ... }          -- closed record
    | { l : τ, ... | r }      -- open record (row variable r)
```

A type *scheme* is `forall a b .... τ`. Generalization happens only at `let`,
and only over variables not free in the environment (level-based, Rémy's
algorithm). The value restriction is not needed — Vela has no mutable
references in the surface language.

### Records and row polymorphism

```vela
let dist p = sqrt (p.x *. p.x +. p.y *. p.y)
-- dist : { x : Float, y : Float | r } -> Float
```

`dist` accepts *any* record that has at least `x` and `y`. Field order never
matters. `{ p | x = 3.0 }` is functional update, and requires `p` to already
have field `x` at the same type.

---

## 3. Declarations

```vela
type Option a = None | Some a
type Tree a   = Leaf | Node (Tree a) a (Tree a)
type alias Point = { x : Float, y : Float }

let  x        = 1 + 2
let  add a b  = a + b
let rec fact n = if n <= 1 then 1 else n * fact (n - 1)
let  id : a -> a = \x -> x        -- annotation is checked
```

Top-level `let`s may be mutually recursive when written as a `let rec ... and`
group. Declaration order does not matter for `type`s; value declarations are
visible from their point of definition onward, plus within their own `rec` group.

---

## 4. Expressions

```vela
\x y -> body                      -- lambda
f x y                             -- application
if c then a else b                -- else is mandatory; both branches same type
let x = e in body                 -- local binding, generalized
let rec f n = ... in body
e1 ; e2                           -- sequence; e1 must have type Unit
(e : T)                           -- type ascription
match e { p -> e, p -> e }        -- pattern match, trailing comma ok
[a, b, c]                         -- list
(a, b)                            -- tuple
{ x = 1, y = 2 }                  -- record
{ r | x = 1 }                     -- record update
r.x                               -- projection
```

`match` arms are checked for **exhaustiveness** and **redundancy** at compile
time using Maranget's usefulness algorithm. A non-exhaustive match is an error,
not a warning.

### Patterns

```
p ::= _ | x | literal | C p... | (p, p, ...) | [p, ...] | p :: p
    | { l = p, ... }          -- record pattern, matches open records
    | p as x                  -- alias
```

---

## 5. Modules

One file is one module. `import Foo` searches, in order:
the importing file's directory, then `VELA_PATH`, then the bundled `std/`.
Names are then reachable as `Foo.bar`. `import Foo as F` renames.
Only top-level `let` and `type` declarations are exported; a name starting
with `_` is private to its module. Import cycles are a compile error.

---

## 6. Evaluation

Strict, call-by-value, left-to-right argument evaluation. Function calls in
tail position do not grow the stack — the VM reuses the frame. `and`/`or`
short-circuit. The garbage collector is a non-moving mark-and-sweep collector;
allocation may trigger a collection, but object identity is stable and there
are no finalizers.

---

## 7. Compilation pipeline

```
 source .vela
   │  lexer.py         → token stream (with spans)
   │  parser.py        → surface AST
   │  modules.py       → dependency graph, topological order
   │  infer.py         → typed AST + type schemes   (unification, levels)
   │  patterns.py      → exhaustiveness / redundancy
   │  core.py          → Core IR (desugared: no records-by-name, no operators)
   │  compile.py       → closure conversion, upvalue resolution, bytecode
   │  emit.py          → .velac binary image
   ↓
 vm/  C bytecode interpreter ── or ── velac/interp.py (reference oracle)
```

Both backends must produce byte-identical observable output for every program
in `tests/`. That equivalence is enforced by `tests/run_tests.py --differential`.
