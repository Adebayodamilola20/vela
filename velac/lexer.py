"""Hand-written lexer for Vela.

Produces a flat token list terminated by a single EOF token. Every token
carries a `Span` so later stages can point at exact source text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from .errors import Diagnostic, Label, Span


class T(Enum):
    INT = auto()
    FLOAT = auto()
    STRING = auto()
    LOWER = auto()      # value identifier / type variable
    UPPER = auto()      # constructor / type / module name
    OP = auto()         # operator symbol
    KEYWORD = auto()
    LPAREN = auto()
    RPAREN = auto()
    LBRACKET = auto()
    RBRACKET = auto()
    LBRACE = auto()
    RBRACE = auto()
    COMMA = auto()
    SEMI = auto()
    PIPE = auto()       # bare `|` (record update, type alternatives)
    ARROW = auto()      # ->
    EQUALS = auto()     # bare `=` in bindings
    COLON = auto()      # type ascription
    DOT = auto()        # record projection
    BACKSLASH = auto()  # lambda
    UNDERSCORE = auto()
    EOF = auto()


KEYWORDS = frozenset(
    """let rec in if then else match type import as extern
       True False and or not do alias""".split()
)

#: Multi-character operators, longest first so maximal munch works.
OPERATORS = [
    "|>", "**", "==", "!=", "<=", ">=", "::", "++", ">>", "<<", "&&", "||",
    "+.", "-.", "*.", "/.",
    "+", "-", "*", "/", "%", "<", ">", "^",
]

_IDENT_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_IDENT_CONT = _IDENT_START | set("0123456789'")
_DIGITS = set("0123456789")
_OP_CHARS = set("+-*/%<>=!|^&:.~")

#: `and` joins bindings in a `let rec` group, so boolean conjunction is `&&`.


@dataclass(slots=True)
class Token:
    kind: T
    text: str
    span: Span
    value: object = None  # decoded payload for INT / FLOAT / STRING

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.kind.name}({self.text!r})"

    @property
    def is_eof(self) -> bool:
        return self.kind is T.EOF


class Lexer:
    def __init__(self, src: str, filename: str = "<input>") -> None:
        self.src = src
        self.file = filename
        self.i = 0
        self.line = 1
        self.line_start = 0
        self.tokens: list[Token] = []

    # -- helpers -----------------------------------------------------------

    def _span(self, start: int, line: int, line_start: int) -> Span:
        return Span(self.file, start, self.i, line, start - line_start + 1)

    def _err(self, msg: str, start: int, note: str | None = None) -> Diagnostic:
        span = Span(self.file, start, max(start + 1, self.i), self.line, start - self.line_start + 1)
        return Diagnostic(msg, [Label(span, "")], [note] if note else [], code="E0001")

    def _peek(self, offset: int = 0) -> str:
        j = self.i + offset
        return self.src[j] if j < len(self.src) else ""

    def _newline(self) -> None:
        self.line += 1
        self.line_start = self.i

    # -- main loop ---------------------------------------------------------

    def tokenize(self) -> list[Token]:
        src = self.src
        n = len(src)
        while self.i < n:
            ch = src[self.i]

            if ch == "\n":
                self.i += 1
                self._newline()
                continue
            if ch in " \t\r":
                self.i += 1
                continue

            # comments
            if ch == "-" and self._peek(1) == "-":
                while self.i < n and src[self.i] != "\n":
                    self.i += 1
                continue
            if ch == "{" and self._peek(1) == "-":
                self._block_comment()
                continue

            start, line, line_start = self.i, self.line, self.line_start

            if ch in _DIGITS:
                self._number(start, line, line_start)
            elif ch in _IDENT_START:
                self._ident(start, line, line_start)
            elif ch == '"':
                self._string(start, line, line_start)
            else:
                self._punct(ch, start, line, line_start)

        self.tokens.append(Token(T.EOF, "", Span(self.file, n, n, self.line, self.i - self.line_start + 1)))
        return self.tokens

    def _push(self, kind: T, start: int, line: int, line_start: int, value: object = None) -> None:
        self.tokens.append(Token(kind, self.src[start:self.i], self._span(start, line, line_start), value))

    # -- block comments (nesting) -----------------------------------------

    def _block_comment(self) -> None:
        start = self.i
        depth = 0
        n = len(self.src)
        while self.i < n:
            if self.src.startswith("{-", self.i):
                depth += 1
                self.i += 2
            elif self.src.startswith("-}", self.i):
                depth -= 1
                self.i += 2
                if depth == 0:
                    return
            else:
                if self.src[self.i] == "\n":
                    self.i += 1
                    self._newline()
                else:
                    self.i += 1
        raise self._err("unterminated block comment", start,
                        "block comments open with `{-` and close with `-}`")

    # -- numbers -----------------------------------------------------------

    def _number(self, start: int, line: int, line_start: int) -> None:
        src, n = self.src, len(self.src)

        # radix prefixes
        # note: `"" in "xXbBoO"` is True, so guard against end-of-input first
        if src[self.i] == "0" and self._peek(1) != "" and self._peek(1) in "xXbBoO":
            base = {"x": 16, "b": 2, "o": 8}[self._peek(1).lower()]
            self.i += 2
            digits_start = self.i
            valid = {16: "0123456789abcdefABCDEF", 2: "01", 8: "01234567"}[base]
            while self.i < n and (src[self.i] in valid or src[self.i] == "_"):
                self.i += 1
            body = src[digits_start:self.i].replace("_", "")
            if not body:
                raise self._err(f"expected base-{base} digits after `{src[start:digits_start]}`", start)
            self._push(T.INT, start, line, line_start, self._wrap_i64(int(body, base)))
            return

        while self.i < n and (src[self.i] in _DIGITS or src[self.i] == "_"):
            self.i += 1

        is_float = False
        # fractional part — but `1.` followed by a non-digit is an Int then `.`
        if self.i < n and src[self.i] == "." and self._peek(1) in _DIGITS:
            is_float = True
            self.i += 1
            while self.i < n and (src[self.i] in _DIGITS or src[self.i] == "_"):
                self.i += 1
        # exponent
        if self.i < n and src[self.i] in "eE":
            save = self.i
            j = self.i + 1
            if j < n and src[j] in "+-":
                j += 1
            if j < n and src[j] in _DIGITS:
                is_float = True
                self.i = j
                while self.i < n and (src[self.i] in _DIGITS or src[self.i] == "_"):
                    self.i += 1
            else:
                self.i = save

        text = src[start:self.i].replace("_", "")
        if is_float:
            self._push(T.FLOAT, start, line, line_start, float(text))
        else:
            self._push(T.INT, start, line, line_start, self._wrap_i64(int(text)))

    @staticmethod
    def _wrap_i64(v: int) -> int:
        """Vela Ints are 64-bit and wrap; do it at lex time for literals too."""
        v &= (1 << 64) - 1
        return v - (1 << 64) if v >= (1 << 63) else v

    # -- identifiers -------------------------------------------------------

    def _ident(self, start: int, line: int, line_start: int) -> None:
        src, n = self.src, len(self.src)
        while self.i < n and src[self.i] in _IDENT_CONT:
            self.i += 1
        text = src[start:self.i]
        if text == "_":
            self._push(T.UNDERSCORE, start, line, line_start)
        elif text in KEYWORDS:
            self._push(T.KEYWORD, start, line, line_start)
        elif text[0].isupper():
            self._push(T.UPPER, start, line, line_start)
        else:
            self._push(T.LOWER, start, line, line_start)

    # -- strings -----------------------------------------------------------

    def _string(self, start: int, line: int, line_start: int) -> None:
        src, n = self.src, len(self.src)
        self.i += 1  # opening quote
        buf: list[str] = []
        while True:
            if self.i >= n:
                raise self._err("unterminated string literal", start)
            ch = src[self.i]
            if ch == '"':
                self.i += 1
                break
            if ch == "\n":
                raise self._err("unterminated string literal", start,
                                "strings may not span lines; use `\\n`")
            if ch == "\\":
                self.i += 1
                if self.i >= n:
                    raise self._err("unterminated escape sequence", start)
                esc = src[self.i]
                simple = {"n": "\n", "t": "\t", "r": "\r", "0": "\0",
                          "\\": "\\", '"': '"', "'": "'"}
                if esc in simple:
                    buf.append(simple[esc])
                    self.i += 1
                elif esc == "u":
                    self.i += 1
                    if self._peek() != "{":
                        raise self._err("expected `{` after `\\u`", start,
                                        "unicode escapes look like `\\u{1F600}`")
                    self.i += 1
                    hex_start = self.i
                    while self.i < n and src[self.i] != "}":
                        self.i += 1
                    if self.i >= n:
                        raise self._err("unterminated unicode escape", start)
                    code = src[hex_start:self.i]
                    self.i += 1  # closing brace
                    try:
                        cp = int(code, 16)
                        buf.append(chr(cp))
                    except ValueError:
                        raise self._err(f"invalid unicode escape `\\u{{{code}}}`", start) from None
                else:
                    raise self._err(f"unknown escape sequence `\\{esc}`", start,
                                    "valid escapes: \\n \\t \\r \\0 \\\\ \\\" \\u{...}")
            else:
                buf.append(ch)
                self.i += 1
        self._push(T.STRING, start, line, line_start, "".join(buf))

    # -- punctuation and operators ----------------------------------------

    _SIMPLE = {
        "(": T.LPAREN, ")": T.RPAREN,
        "[": T.LBRACKET, "]": T.RBRACKET,
        "{": T.LBRACE, "}": T.RBRACE,
        ",": T.COMMA, ";": T.SEMI,
        "\\": T.BACKSLASH,
    }

    def _punct(self, ch: str, start: int, line: int, line_start: int) -> None:
        # maximal munch over multi-char operators first
        if ch in _OP_CHARS:
            # `->` must beat `-`, so it is tested before the operator table
            if self.src.startswith("->", self.i):
                self.i += 2
                self._push(T.ARROW, start, line, line_start)
                return
            for op in OPERATORS:
                if self.src.startswith(op, self.i):
                    self.i += len(op)
                    self._push(T.OP, start, line, line_start)
                    return
            if ch == "|":
                self.i += 1
                self._push(T.PIPE, start, line, line_start)
                return
            if ch == "=":
                self.i += 1
                self._push(T.EQUALS, start, line, line_start)
                return
            if ch == ":":
                self.i += 1
                self._push(T.COLON, start, line, line_start)
                return
            if ch == ".":
                self.i += 1
                self._push(T.DOT, start, line, line_start)
                return

        kind = self._SIMPLE.get(ch)
        if kind is not None:
            self.i += 1
            self._push(kind, start, line, line_start)
            return

        self.i += 1
        raise self._err(f"unexpected character `{ch}`", start)


def tokenize(src: str, filename: str = "<input>") -> list[Token]:
    return Lexer(src, filename).tokenize()
