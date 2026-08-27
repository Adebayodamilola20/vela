"""Unit tests for the Vela lexer."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from velac.errors import Diagnostic  # noqa: E402
from velac.lexer import T, tokenize  # noqa: E402

FAILED: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(f"{name}: {detail}")


def kinds(src: str) -> list[str]:
    return [t.kind.name for t in tokenize(src) if t.kind is not T.EOF]


def values(src: str) -> list[object]:
    return [t.value for t in tokenize(src) if t.value is not None]


# -- literals ---------------------------------------------------------------

check("int decimal", values("42") == [42], values("42"))
check("int underscores", values("1_000_000") == [1000000], values("1_000_000"))
check("int hex", values("0xff") == [255], values("0xff"))
check("int binary", values("0b1011") == [11], values("0b1011"))
check("int octal", values("0o17") == [15], values("0o17"))
check("float", values("3.14") == [3.14], values("3.14"))
check("float exp", values("1.0e-9") == [1.0e-9], values("1.0e-9"))
check("float exp no dot", values("2e10") == [2e10], values("2e10"))
check("int then dot is not float", kinds("1.x") == ["INT", "DOT", "LOWER"], kinds("1.x"))
check("i64 wrap", values("9223372036854775808") == [-9223372036854775808],
      values("9223372036854775808"))

# -- strings ----------------------------------------------------------------

check("string simple", values('"hi"') == ["hi"], values('"hi"'))
check("string escapes", values(r'"a\tb\nc"') == ["a\tb\nc"], values(r'"a\tb\nc"'))
check("string unicode", values(r'"\u{41}\u{1F600}"') == ["A\U0001F600"], values(r'"\u{41}"'))
check("string quote escape", values(r'"say \"hi\""') == ['say "hi"'], values(r'"say \"hi\""'))

# -- comments ---------------------------------------------------------------

check("line comment", kinds("1 -- ignored\n2") == ["INT", "INT"], kinds("1 -- x\n2"))
check("block comment", kinds("1 {- x -} 2") == ["INT", "INT"], kinds("1 {- x -} 2"))
check("nested block comment", kinds("1 {- a {- b -} c -} 2") == ["INT", "INT"],
      kinds("1 {- a {- b -} c -} 2"))

# -- identifiers and keywords ----------------------------------------------

check("lower ident", kinds("foo") == ["LOWER"], kinds("foo"))
check("prime ident", kinds("map'") == ["LOWER"], kinds("map'"))
check("upper ident", kinds("Some") == ["UPPER"], kinds("Some"))
check("keyword", kinds("let") == ["KEYWORD"], kinds("let"))
check("underscore", kinds("_") == ["UNDERSCORE"], kinds("_"))
check("underscore-prefixed is lower", kinds("_x") == ["LOWER"], kinds("_x"))

# -- operators (maximal munch) ---------------------------------------------

check("arrow", kinds("->") == ["ARROW"], kinds("->"))
check("pipe-op vs pipe", kinds("|> |") == ["OP", "PIPE"], kinds("|> |"))
check("float ops", [t.text for t in tokenize("+. -. *. /.") if t.kind is T.OP]
      == ["+.", "-.", "*.", "/."])
check("cmp ops", [t.text for t in tokenize("== != <= >= < >") if t.kind is T.OP]
      == ["==", "!=", "<=", ">=", "<", ">"])
check("cons/append", [t.text for t in tokenize(":: ++ ^") if t.kind is T.OP] == ["::", "++", "^"])
check("colon vs cons", kinds(": ::") == ["COLON", "OP"], kinds(": ::"))
check("power", [t.text for t in tokenize("** *") if t.kind is T.OP] == ["**", "*"])
check("compose", [t.text for t in tokenize(">> <<") if t.kind is T.OP] == [">>", "<<"])

# -- spans ------------------------------------------------------------------

toks = tokenize("let x =\n  42\n", "f.vela")
check("span line 1", toks[0].span.line == 1 and toks[0].span.col == 1,
      f"{toks[0].span.line}:{toks[0].span.col}")
lit = [t for t in toks if t.kind is T.INT][0]
check("span line 2", lit.span.line == 2 and lit.span.col == 3, f"{lit.span.line}:{lit.span.col}")
check("eof present", toks[-1].kind is T.EOF)

# -- errors -----------------------------------------------------------------


def expect_error(name: str, src: str) -> None:
    try:
        tokenize(src)
    except Diagnostic:
        check(name, True)
    else:
        check(name, False, "expected a Diagnostic, got none")


expect_error("unterminated string", '"abc')
expect_error("newline in string", '"abc\ndef"')
expect_error("unterminated block comment", "{- forever")
expect_error("bad escape", r'"\q"')
expect_error("bad unicode escape", r'"\u{zz}"')
expect_error("empty hex", "0x")
expect_error("stray char", "@")

# -- report -----------------------------------------------------------------

print(f"lexer: {PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  FAIL", f)
sys.exit(1 if FAILED else 0)
