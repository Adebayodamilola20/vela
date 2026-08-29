# A tour of Vela

A working introduction, meant to be read start to finish in about twenty
minutes. Every example here is runnable — put it in a file and run
`python3 -m velac run thefile.vela`.

For the precise rules, see [`spec.md`](spec.md). This document is the friendly
version.

---

## 1. Hello

```vela
let main () =
  print "Hello."
```

```sh
python3 -m velac run hello.vela
```

Every runnable program needs `main`, taking `()` — the single value of type
`Unit`. A module without `main` is a library, and `velac check` is happy with it.

## 2. Values and bindings

```vela
let answer = 42
let greeting = "hello"
let pi = 3.14159
let yes = True
let nothing = ()
```

Bindings are immutable. There is no assignment operator, and no way to change
`answer` after this line.

Local bindings use `let ... in`:

```vela
let main () =
  let x = 10 in
  let y = 20 in
  print (int_to_string (x + y))
```

## 3. Functions

Parameters are juxtaposed, not parenthesised:

```vela
let add a b = a + b
let square x = x * x
```

Called the same way:

```vela
add 3 4        -- 7
square (add 3 4)   -- 49
```

Anonymous functions use a backslash:

```vela
let double = \x -> x * 2
let sum3 = \a b c -> a + b + c
```

### Partial application

Supplying fewer arguments than a function takes gives you a function back:

```vela
let add a b = a + b
let add10 = add 10      -- a function

let main () =
  print (int_to_string (add10 5))   -- 15
```

### Recursion

Recursive functions need `rec`, so that a name can refer to itself:

```vela
let rec fact n = if n <= 1 then 1 else n * fact (n - 1)
```

Mutual recursion joins with `and`:

```vela
let rec is_even n = if n == 0 then True else is_odd (n - 1)
and is_odd n = if n == 0 then False else is_even (n - 1)
```

## 4. Everything is an expression

`if` returns a value, so `else` is mandatory and both branches must agree:

```vela
let sign n = if n > 0 then "positive" else "not positive"
```

There is no statement/expression split. `;` sequences two expressions and
discards the first, which must be `Unit`:

```vela
let main () =
  print "first";
  print "second"
```

## 5. Types you define

```vela
type Colour = Red | Green | Blue
```

Constructors may carry data:

```vela
type Shape
  = Circle Float
  | Rect Float Float
```

And be generic:

```vela
type Option a = None | Some a
type Tree a = Leaf | Node (Tree a) a (Tree a)
```

A type alias renames without creating a new type:

```vela
type alias Point = { x : Float, y : Float }
```

## 6. Pattern matching

The main way to take a value apart:

```vela
let area s =
  match s {
    Circle r -> 3.14159 *. r *. r,
    Rect w h -> w *. h,
  }
```

Note the commas between arms, and that a trailing one is allowed.

### Patterns nest

```vela
let rec depth t =
  match t {
    Leaf -> 0,
    Node l _ r -> 1 + max (depth l) (depth r),
  }
```

`_` matches anything without binding it.

### List patterns

```vela
let describe xs =
  match xs {
    [] -> "empty",
    [x] -> "exactly one",
    [x, y] -> "exactly two",
    x :: rest -> "many",
  }
```

`::` is cons: `x :: rest` splits off the head.

### Guards

An arm can carry a condition:

```vela
let classify n =
  match n {
    0 -> "zero",
    x if x < 0 -> "negative",
    x if x % 2 == 0 -> "positive even",
    _ -> "positive odd",
  }
```

An arm is taken only when its pattern matches *and* its guard holds.

### Aliases

`as` binds the whole value while still destructuring it:

```vela
let first_pair pairs =
  match pairs {
    [] -> "none",
    ((a, b) as p) :: _ -> show p,
  }
```

### Exhaustiveness

Leave out a case and the program does not compile:

```
error[E0200]: this `match` is not exhaustive
  = note: no arm matches `Blue`
```

This is an error, not a warning. The compiler builds a value that would have
fallen through and shows it to you.

## 7. Lists

```vela
let xs = [1, 2, 3]
let ys = 0 :: xs          -- [0, 1, 2, 3]
let zs = xs ++ [4, 5]     -- [1, 2, 3, 4, 5]
```

The `List` module has the rest:

```vela
import List

let main () =
  let nums = List.range 1 11 in          -- [1..10], end excluded
  print (show (List.map (\n -> n * n) nums));
  print (show (List.filter (\n -> n % 2 == 0) nums));
  print (int_to_string (List.sum nums));
  print (show (List.sort [3, 1, 2]))
```

## 8. Tuples and records

Tuples group a fixed number of possibly different types:

```vela
let pair = (1, "one")
let triple = (1, 2.0, "three")

let main () =
  print (int_to_string (fst pair))
```

Records name their parts:

```vela
let point = { x = 3.0, y = 4.0 }

let main () =
  print (float_to_string point.x)
```

Update copies rather than mutating:

```vela
let moved = { point | x = 10.0 }
-- point is still { x = 3.0, y = 4.0 }
```

### Row polymorphism

A function that reads `x` and `y` accepts any record that has them:

```vela
import Float

let dist p = Float.sqrt (p.x *. p.x +. p.y *. p.y)
```

Inferred as:

```
dist : { x : Float, y : Float | r } -> Float
```

So all of these work:

```vela
dist { x = 3.0, y = 4.0 }
dist { y = 4.0, x = 3.0 }                            -- order is irrelevant
dist { x = 3.0, y = 4.0, name = "corner", id = 7 }   -- extras ride along
```

## 9. Option and Result

There is no null. A function that might not return an answer says so:

```vela
import List
import Option

let main () =
  match List.head [] {
    Option.Some x -> print (int_to_string x),
    Option.None -> print "the list was empty",
  }
```

Or, more briefly:

```vela
print (int_to_string (Option.with_default 0 (List.head [])))
```

`Result` carries a reason for the failure:

```vela
import Result
import String

let main () =
  let parsed = Result.from_option "not a number" (String.to_int "42") in
  print (int_to_string (Result.with_default 0 parsed))
```

## 10. Numbers

Int and Float have **separate operators**. This is the thing that catches people:

```vela
1 + 2        -- Int addition
1.0 +. 2.0   -- Float addition — note the dot
```

| Int | Float |
|---|---|
| `+` `-` `*` `/` `%` `**` | `+.` `-.` `*.` `/.` |

Comparison is shared and works on anything:

```vela
1 < 2
1.0 < 2.0
"a" < "b"
[1, 2] < [1, 3]
```

Convert explicitly:

```vela
int_to_float 3      -- 3.0
float_to_int 3.9    -- 3
```

Integers are 64-bit and **wrap** rather than overflow. Division truncates
toward zero, so `-17 / 5` is `-3` and `-17 % 5` is `-2`.

## 11. Pipelines and composition

`|>` passes a value forward:

```vela
import List
import Int

let main () =
  List.range 1 21
    |> List.filter Int.is_odd
    |> List.map (\n -> n * n)
    |> List.sum
    |> int_to_string
    |> print
```

`>>` and `<<` compose functions without applying them:

```vela
let add1 = \x -> x + 1
let add3 = add1 >> add1 >> add1     -- left to right
let also3 = add1 << add1 << add1    -- right to left
```

## 12. Modules

One file, one module. The filename is the module name.

```vela
-- Geometry.vela
type alias Point = { x : Float, y : Float }

let origin = { x = 0.0, y = 0.0 }

let _helper x = x * 2       -- private: leading underscore
```

```vela
-- main.vela
import Geometry

let main () = print (show Geometry.origin)
```

Rename on import when it helps:

```vela
import String as S

let main () = print (S.to_upper "vela")
```

`Prelude` is imported for you. Everything else is explicit.

## 13. Tail recursion

A call in tail position — the last thing a function does — does not grow the
stack. This runs fine:

```vela
let rec count n acc = if n <= 0 then acc else count (n - 1) (acc + 1)

let main () = print (int_to_string (count 1000000 0))
```

The pattern to learn is the accumulator: carry the running answer in a
parameter, so the recursive call is the *whole* body of its branch rather than
part of a larger expression.

```vela
-- grows the stack: the call is inside a `+`
let rec sum_bad xs =
  match xs { [] -> 0, x :: rest -> x + sum_bad rest }

-- constant stack: the call is the entire branch
let sum_good xs =
  let rec go acc rest =
    match rest { [] -> acc, x :: tail -> go (acc + x) tail }
  in go 0 xs
```

## 14. Where to go next

- [`spec.md`](spec.md) — the exact rules
- `std/` — the standard library, all of it written in Vela and worth reading
- `examples/nqueens.vela` — a complete program with real structure
- `python3 -m velac check yourfile.vela --types` — the best way to check your
  understanding against the compiler's
