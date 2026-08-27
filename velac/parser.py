"""Recursive-descent parser with precedence climbing for binary operators.

Grammar notes worth knowing when reading this file:

*   Application binds tighter than every binary operator, so `f x + g y`
    parses as `(f x) + (g y)`.
*   `if`, `let`, `match` and `\\` may not appear as bare function arguments;
    they must be parenthesised. This keeps `f -1` and friends unambiguous.
*   Inside a `match` scrutinee we set a "no brace" restriction so that the
    `{` opening the arm list is never mistaken for a record literal — the
    same trick Rust uses for struct literals in condition position.
"""

from __future__ import annotations

from contextlib import contextmanager

from . import ast as A
from .errors import Diagnostic, Label, Span
from .lexer import T, Token, tokenize

# --------------------------------------------------------------------------
# Operator precedence (higher number binds tighter)
# --------------------------------------------------------------------------

P_SEQ = 1
P_PIPE = 2
P_COMPOSE = 3
P_OR = 4
P_AND = 5
P_CMP = 6
P_CONS = 7
P_ADD = 8
P_MUL = 9
P_POW = 10

LEFT, RIGHT, NONE = "left", "right", "none"

BINOPS: dict[str, tuple[int, str]] = {
    ";": (P_SEQ, RIGHT),
    "|>": (P_PIPE, LEFT),
    ">>": (P_COMPOSE, RIGHT),
    "<<": (P_COMPOSE, RIGHT),
    "||": (P_OR, RIGHT),
    "&&": (P_AND, RIGHT),
    "==": (P_CMP, NONE),
    "!=": (P_CMP, NONE),
    "<": (P_CMP, NONE),
    "<=": (P_CMP, NONE),
    ">": (P_CMP, NONE),
    ">=": (P_CMP, NONE),
    "::": (P_CONS, RIGHT),
    "++": (P_CONS, RIGHT),
    "^": (P_CONS, RIGHT),
    "+": (P_ADD, LEFT),
    "-": (P_ADD, LEFT),
    "+.": (P_ADD, LEFT),
    "-.": (P_ADD, LEFT),
    "*": (P_MUL, LEFT),
    "/": (P_MUL, LEFT),
    "%": (P_MUL, LEFT),
    "*.": (P_MUL, LEFT),
    "/.": (P_MUL, LEFT),
    "**": (P_POW, RIGHT),
}

#: Tokens that can begin an atomic expression (and therefore an argument).
ATOM_START = frozenset({
    T.INT, T.FLOAT, T.STRING, T.LOWER, T.UPPER, T.LPAREN, T.LBRACKET, T.LBRACE,
})

#: Keywords usable as atoms.
ATOM_KEYWORDS = frozenset({"True", "False"})


class Parser:
    def __init__(self, tokens: list[Token], filename: str = "<input>") -> None:
        self.toks = tokens
        self.pos = 0
        self.file = filename
        self._no_brace = 0

    # ---------------------------------------------------------------- utils

    @property
    def cur(self) -> Token:
        return self.toks[self.pos]

    def peek(self, offset: int = 0) -> Token:
        j = self.pos + offset
        return self.toks[j] if j < len(self.toks) else self.toks[-1]

    def advance(self) -> Token:
        tok = self.toks[self.pos]
        if tok.kind is not T.EOF:
            self.pos += 1
        return tok

    def at(self, kind: T, text: str | None = None) -> bool:
        tok = self.cur
        return tok.kind is kind and (text is None or tok.text == text)

    def at_keyword(self, *words: str) -> bool:
        return self.cur.kind is T.KEYWORD and self.cur.text in words

    def accept(self, kind: T, text: str | None = None) -> Token | None:
        if self.at(kind, text):
            return self.advance()
        return None

    def accept_keyword(self, *words: str) -> Token | None:
        if self.at_keyword(*words):
            return self.advance()
        return None

    def expect(self, kind: T, what: str, text: str | None = None) -> Token:
        if self.at(kind, text):
            return self.advance()
        raise self.error(f"expected {what}, found {self.describe(self.cur)}", self.cur.span)

    def expect_keyword(self, word: str) -> Token:
        if self.at_keyword(word):
            return self.advance()
        raise self.error(f"expected `{word}`, found {self.describe(self.cur)}", self.cur.span)

    @staticmethod
    def describe(tok: Token) -> str:
        if tok.kind is T.EOF:
            return "end of file"
        return f"`{tok.text}`"

    def error(self, msg: str, span: Span, note: str | None = None) -> Diagnostic:
        return Diagnostic(msg, [Label(span, "")], [note] if note else [], code="E0002")

    @contextmanager
    def no_brace(self):
        self._no_brace += 1
        try:
            yield
        finally:
            self._no_brace -= 1

    @contextmanager
    def allow_brace(self):
        saved, self._no_brace = self._no_brace, 0
        try:
            yield
        finally:
            self._no_brace = saved

    # ============================================================== modules

    def parse_module(self, name: str) -> A.Module:
        decls: list[A.Decl] = []
        while not self.cur.is_eof:
            decls.append(self.parse_decl())
        return A.Module(name, decls, self.file)

    def parse_decl(self) -> A.Decl:
        tok = self.cur
        if tok.kind is T.KEYWORD:
            if tok.text == "let":
                return self.parse_let_decl()
            if tok.text == "type":
                return self.parse_type_decl()
            if tok.text == "import":
                return self.parse_import_decl()
            if tok.text == "extern":
                return self.parse_extern_decl()
        raise self.error(
            f"expected a top-level declaration, found {self.describe(tok)}", tok.span,
            "declarations start with `let`, `type`, `import` or `extern`")

    # -- let ---------------------------------------------------------------

    def parse_let_decl(self) -> A.DLet:
        start = self.expect_keyword("let").span
        recursive = self.accept_keyword("rec") is not None
        bindings = [self.parse_binding()]
        while self.accept_keyword("and"):
            if not recursive:
                raise self.error(
                    "`and` may only join bindings in a `let rec` group",
                    self.toks[self.pos - 1].span,
                    "write `let rec f ... and g ...`")
            bindings.append(self.parse_binding())
        span = start.to(bindings[-1].span)
        return A.DLet(span, bindings, recursive)

    def parse_binding(self) -> A.Binding:
        """`name params... [: Type] = expr`"""
        name_tok = self.cur
        if name_tok.kind is not T.LOWER:
            raise self.error(
                f"expected a binding name, found {self.describe(name_tok)}", name_tok.span,
                "value names start with a lowercase letter")
        self.advance()
        params: list[A.Pattern] = []
        while self.starts_param():
            params.append(self.parse_atom_pattern())
        annotation = None
        if self.accept(T.COLON):
            annotation = self.parse_type()
        self.expect(T.EQUALS, "`=` in binding")
        body = self.parse_expr()
        return A.Binding(name_tok.text, params, body, name_tok.span.to(body.span),
                         annotation, name_tok.span)

    def starts_param(self) -> bool:
        k = self.cur.kind
        return k in (T.LOWER, T.UNDERSCORE, T.LPAREN, T.LBRACKET, T.LBRACE, T.UPPER)

    # -- type --------------------------------------------------------------

    def parse_type_decl(self) -> A.Decl:
        start = self.expect_keyword("type").span
        if self.accept_keyword("alias"):
            name = self.expect(T.UPPER, "a type name").text
            params = []
            while self.at(T.LOWER):
                params.append(self.advance().text)
            self.expect(T.EQUALS, "`=` in type alias")
            target = self.parse_type()
            return A.DTypeAlias(start.to(target.span), name, params, target)

        name_tok = self.expect(T.UPPER, "a type name")
        params: list[str] = []
        while self.at(T.LOWER):
            params.append(self.advance().text)
        self.expect(T.EQUALS, "`=` in type declaration")
        self.accept(T.PIPE)  # optional leading bar
        cons = [self.parse_constructor()]
        while self.accept(T.PIPE):
            cons.append(self.parse_constructor())
        return A.DType(start.to(cons[-1].span), name_tok.text, params, cons)

    def parse_constructor(self) -> A.ConDef:
        tok = self.expect(T.UPPER, "a constructor name")
        args: list[A.TypeExpr] = []
        while self.starts_atomic_type():
            args.append(self.parse_atomic_type())
        span = tok.span.to(args[-1].span) if args else tok.span
        return A.ConDef(tok.text, args, span)

    # -- import / extern ---------------------------------------------------

    def parse_import_decl(self) -> A.DImport:
        start = self.expect_keyword("import").span
        name = self.expect(T.UPPER, "a module name")
        alias = None
        end = name.span
        if self.accept_keyword("as"):
            alias_tok = self.expect(T.UPPER, "a module alias")
            alias = alias_tok.text
            end = alias_tok.span
        return A.DImport(start.to(end), name.text, alias)

    def parse_extern_decl(self) -> A.DExtern:
        start = self.expect_keyword("extern").span
        name = self.expect(T.LOWER, "a name for the primitive")
        self.expect(T.COLON, "`:` before the primitive's type")
        annotation = self.parse_type()
        self.expect(T.EQUALS, "`=` before the primitive's identifier")
        prim = self.expect(T.STRING, "the primitive name as a string")
        return A.DExtern(start.to(prim.span), name.text, annotation, prim.value)

    # ================================================================ types

    def parse_type(self) -> A.TypeExpr:
        left = self.parse_type_app()
        if self.accept(T.ARROW):
            right = self.parse_type()
            return A.TEFun(left.span.to(right.span), left, right)
        return left

    def parse_type_app(self) -> A.TypeExpr:
        head = self.parse_atomic_type()
        if isinstance(head, A.TECon) and not head.args:
            args: list[A.TypeExpr] = []
            while self.starts_atomic_type():
                args.append(self.parse_atomic_type())
            if args:
                return A.TECon(head.span.to(args[-1].span), head.name, args, head.module)
        return head

    def starts_atomic_type(self) -> bool:
        return self.cur.kind in (T.LOWER, T.UPPER, T.LPAREN, T.LBRACKET, T.LBRACE)

    def parse_atomic_type(self) -> A.TypeExpr:
        tok = self.cur
        if tok.kind is T.LOWER:
            self.advance()
            return A.TEVar(tok.span, tok.text)

        if tok.kind is T.UPPER:
            self.advance()
            module = None
            name = tok.text
            if self.at(T.DOT) and self.peek(1).kind is T.UPPER:
                self.advance()
                inner = self.advance()
                module, name = name, inner.text
                return A.TECon(tok.span.to(inner.span), name, [], module)
            return A.TECon(tok.span, name, [], module)

        if tok.kind is T.LBRACKET:
            self.advance()
            item = self.parse_type()
            close = self.expect(T.RBRACKET, "`]` to close a list type")
            return A.TEList(tok.span.to(close.span), item)

        if tok.kind is T.LPAREN:
            self.advance()
            if self.at(T.RPAREN):
                close = self.advance()
                return A.TECon(tok.span.to(close.span), "Unit", [])
            first = self.parse_type()
            if self.at(T.COMMA):
                items = [first]
                while self.accept(T.COMMA):
                    items.append(self.parse_type())
                close = self.expect(T.RPAREN, "`)` to close a tuple type")
                return A.TETuple(tok.span.to(close.span), items)
            close = self.expect(T.RPAREN, "`)` to close a parenthesised type")
            return first

        if tok.kind is T.LBRACE:
            self.advance()
            fields: list[tuple[str, A.TypeExpr]] = []
            rest = None
            if not self.at(T.RBRACE):
                while True:
                    label = self.expect(T.LOWER, "a record field name")
                    self.expect(T.COLON, "`:` after a record field name")
                    fields.append((label.text, self.parse_type()))
                    if not self.accept(T.COMMA):
                        break
                    if self.at(T.RBRACE) or self.at(T.PIPE):
                        break
            if self.accept(T.PIPE):
                rest = self.expect(T.LOWER, "a row variable after `|`").text
            close = self.expect(T.RBRACE, "`}` to close a record type")
            return A.TERecord(tok.span.to(close.span), fields, rest)

        raise self.error(f"expected a type, found {self.describe(tok)}", tok.span)

    # ============================================================= patterns

    def parse_pattern(self) -> A.Pattern:
        """Full pattern, including `::` chains and `as` aliases."""
        pat = self.parse_cons_pattern()
        while self.at_keyword("as"):
            self.advance()
            name = self.expect(T.LOWER, "a name after `as`")
            pat = A.PAs(pat.span.to(name.span), pat, name.text)
        return pat

    def parse_cons_pattern(self) -> A.Pattern:
        head = self.parse_app_pattern()
        if self.at(T.OP, "::"):
            self.advance()
            tail = self.parse_cons_pattern()  # right associative
            return A.PCons(head.span.to(tail.span), head, tail)
        return head

    def parse_app_pattern(self) -> A.Pattern:
        """A constructor applied to argument patterns, or a single atom."""
        tok = self.cur
        if tok.kind is T.UPPER:
            self.advance()
            module = None
            name = tok.text
            end = tok.span
            if self.at(T.DOT) and self.peek(1).kind is T.UPPER:
                self.advance()
                inner = self.advance()
                module, name, end = name, inner.text, inner.span
            args: list[A.Pattern] = []
            while self.starts_atom_pattern():
                args.append(self.parse_atom_pattern())
            span = tok.span.to(args[-1].span) if args else tok.span.to(end)
            return A.PCon(span, name, args, module)
        return self.parse_atom_pattern()

    def starts_atom_pattern(self) -> bool:
        k = self.cur.kind
        if k is T.KEYWORD:
            return self.cur.text in ATOM_KEYWORDS
        return k in (T.LOWER, T.UNDERSCORE, T.UPPER, T.LPAREN, T.LBRACKET, T.LBRACE,
                     T.INT, T.FLOAT, T.STRING)

    def parse_atom_pattern(self) -> A.Pattern:
        tok = self.cur

        if tok.kind is T.UNDERSCORE:
            self.advance()
            return A.PWild(tok.span)

        if tok.kind is T.LOWER:
            self.advance()
            return A.PVar(tok.span, tok.text)

        if tok.kind is T.INT:
            self.advance()
            return A.PLit(tok.span, tok.value, "Int")
        if tok.kind is T.FLOAT:
            self.advance()
            return A.PLit(tok.span, tok.value, "Float")
        if tok.kind is T.STRING:
            self.advance()
            return A.PLit(tok.span, tok.value, "String")
        if tok.kind is T.KEYWORD and tok.text in ("True", "False"):
            self.advance()
            return A.PLit(tok.span, tok.text == "True", "Bool")

        if tok.kind is T.OP and tok.text == "-" and self.peek(1).kind in (T.INT, T.FLOAT):
            self.advance()
            lit = self.advance()
            kind = "Int" if lit.kind is T.INT else "Float"
            return A.PLit(tok.span.to(lit.span), -lit.value, kind)

        if tok.kind is T.UPPER:
            return self.parse_app_pattern()

        if tok.kind is T.LPAREN:
            self.advance()
            if self.at(T.RPAREN):
                close = self.advance()
                return A.PLit(tok.span.to(close.span), None, "Unit")
            with self.allow_brace():
                first = self.parse_pattern()
                if self.at(T.COMMA):
                    items = [first]
                    while self.accept(T.COMMA):
                        if self.at(T.RPAREN):
                            break
                        items.append(self.parse_pattern())
                    close = self.expect(T.RPAREN, "`)` to close a tuple pattern")
                    return A.PTuple(tok.span.to(close.span), items)
                close = self.expect(T.RPAREN, "`)` to close a pattern")
            return first

        if tok.kind is T.LBRACKET:
            self.advance()
            items = []
            with self.allow_brace():
                if not self.at(T.RBRACKET):
                    while True:
                        items.append(self.parse_pattern())
                        if not self.accept(T.COMMA):
                            break
                        if self.at(T.RBRACKET):
                            break
                close = self.expect(T.RBRACKET, "`]` to close a list pattern")
            return A.PList(tok.span.to(close.span), items)

        if tok.kind is T.LBRACE:
            self.advance()
            fields: list[tuple[str, A.Pattern]] = []
            with self.allow_brace():
                if not self.at(T.RBRACE):
                    while True:
                        label = self.expect(T.LOWER, "a record field name")
                        if self.accept(T.EQUALS):
                            fields.append((label.text, self.parse_pattern()))
                        else:  # `{ x, y }` shorthand binds `x` and `y`
                            fields.append((label.text, A.PVar(label.span, label.text)))
                        if not self.accept(T.COMMA):
                            break
                        if self.at(T.RBRACE):
                            break
                close = self.expect(T.RBRACE, "`}` to close a record pattern")
            return A.PRecord(tok.span.to(close.span), fields, True)

        raise self.error(f"expected a pattern, found {self.describe(tok)}", tok.span)

    # ========================================================== expressions

    def parse_expr(self) -> A.Expr:
        return self.parse_binary(P_SEQ)

    def peek_binop(self) -> str | None:
        tok = self.cur
        if tok.kind is T.OP and tok.text in BINOPS:
            return tok.text
        if tok.kind is T.SEMI:
            return ";"
        return None

    def parse_binary(self, min_prec: int) -> A.Expr:
        lhs = self.parse_unary()
        while True:
            op = self.peek_binop()
            if op is None:
                break
            prec, assoc = BINOPS[op]
            if prec < min_prec:
                break
            op_tok = self.advance()

            # `e1 ;` at the end of a block is just `e1`
            if op == ";" and (self.at(T.RBRACE) or self.at(T.RPAREN) or self.cur.is_eof):
                break

            next_min = prec + 1 if assoc in (LEFT, NONE) else prec
            rhs = self.parse_binary(next_min)
            lhs = self.build_binop(op, lhs, rhs, op_tok.span)

            if assoc is NONE:
                follow = self.peek_binop()
                if follow is not None and BINOPS[follow][0] == prec:
                    raise self.error(
                        f"cannot chain `{op}` with `{follow}`", self.cur.span,
                        "comparison operators do not associate; add parentheses")
        return lhs

    def build_binop(self, op: str, lhs: A.Expr, rhs: A.Expr, op_span: Span) -> A.Expr:
        span = lhs.span.to(rhs.span)
        if op == ";":
            return A.ESeq(span, lhs, rhs)
        if op == "&&":
            return A.EAnd(span, lhs, rhs)
        if op == "||":
            return A.EOr(span, lhs, rhs)
        if op == "|>":
            # x |> f  ==>  f x   (and  x |> f a  ==>  f a x)
            if isinstance(rhs, A.EApp):
                return A.EApp(span, rhs.fn, [*rhs.args, lhs])
            return A.EApp(span, rhs, [lhs])
        return A.EBinOp(span, op, lhs, rhs, op_span)

    def parse_unary(self) -> A.Expr:
        tok = self.cur
        if tok.kind is T.OP and tok.text in ("-", "-."):
            self.advance()
            operand = self.parse_binary(P_POW)
            op = "neg" if tok.text == "-" else "negf"
            return A.EUnOp(tok.span.to(operand.span), op, operand)
        if tok.kind is T.KEYWORD and tok.text == "not":
            self.advance()
            operand = self.parse_binary(P_POW)
            return A.EUnOp(tok.span.to(operand.span), "not", operand)
        return self.parse_application()

    def parse_application(self) -> A.Expr:
        fn = self.parse_postfix()
        args: list[A.Expr] = []
        while self.starts_atom():
            args.append(self.parse_postfix())
        if not args:
            return fn
        return A.EApp(fn.span.to(args[-1].span), fn, args)

    def starts_atom(self) -> bool:
        tok = self.cur
        if tok.kind is T.LBRACE and self._no_brace:
            return False
        if tok.kind is T.KEYWORD:
            return tok.text in ATOM_KEYWORDS
        return tok.kind in ATOM_START

    def parse_postfix(self) -> A.Expr:
        expr = self.parse_atom()
        while self.at(T.DOT):
            # `.` is only projection when followed immediately by a field name
            if self.peek(1).kind is not T.LOWER:
                break
            self.advance()
            name = self.advance()
            expr = A.EField(expr.span.to(name.span), expr, name.text, name.span)
        return expr

    # -- atoms -------------------------------------------------------------

    def parse_atom(self) -> A.Expr:
        tok = self.cur

        if tok.kind is T.INT:
            self.advance()
            return A.ELit(tok.span, tok.value, "Int")
        if tok.kind is T.FLOAT:
            self.advance()
            return A.ELit(tok.span, tok.value, "Float")
        if tok.kind is T.STRING:
            self.advance()
            return A.ELit(tok.span, tok.value, "String")

        if tok.kind is T.KEYWORD:
            if tok.text in ("True", "False"):
                self.advance()
                return A.ELit(tok.span, tok.text == "True", "Bool")
            if tok.text == "if":
                return self.parse_if()
            if tok.text == "let":
                return self.parse_let_expr()
            if tok.text == "match":
                return self.parse_match()
            raise self.error(
                f"`{tok.text}` cannot start an expression here", tok.span,
                "wrap `if`, `let` and `match` in parentheses to use them as arguments")

        if tok.kind is T.BACKSLASH:
            return self.parse_lambda()

        if tok.kind is T.LOWER:
            self.advance()
            return A.EVar(tok.span, tok.text)

        if tok.kind is T.UPPER:
            self.advance()
            if self.at(T.DOT) and self.peek(1).kind in (T.LOWER, T.UPPER):
                self.advance()
                inner = self.advance()
                span = tok.span.to(inner.span)
                if inner.kind is T.LOWER:
                    return A.EVar(span, inner.text, tok.text)
                return A.ECon(span, inner.text, tok.text)
            return A.ECon(tok.span, tok.text)

        if tok.kind is T.LPAREN:
            return self.parse_paren()

        if tok.kind is T.LBRACKET:
            self.advance()
            items: list[A.Expr] = []
            with self.allow_brace():
                if not self.at(T.RBRACKET):
                    while True:
                        items.append(self.parse_expr())
                        if not self.accept(T.COMMA):
                            break
                        if self.at(T.RBRACKET):
                            break
                close = self.expect(T.RBRACKET, "`]` to close a list")
            return A.EList(tok.span.to(close.span), items)

        if tok.kind is T.LBRACE:
            return self.parse_record()

        raise self.error(f"expected an expression, found {self.describe(tok)}", tok.span)

    def parse_paren(self) -> A.Expr:
        open_tok = self.expect(T.LPAREN, "`(`")

        if self.at(T.RPAREN):
            close = self.advance()
            return A.ELit(open_tok.span.to(close.span), None, "Unit")

        # operator section: `(+)`, `(::)`, ...
        if self.cur.kind is T.OP and self.peek(1).kind is T.RPAREN:
            op_tok = self.advance()
            close = self.advance()
            return A.EVar(open_tok.span.to(close.span), op_tok.text)

        with self.allow_brace():
            first = self.parse_expr()

            if self.at(T.COLON):
                self.advance()
                annotation = self.parse_type()
                close = self.expect(T.RPAREN, "`)` after a type ascription")
                return A.EAnnot(open_tok.span.to(close.span), first, annotation)

            if self.at(T.COMMA):
                items = [first]
                while self.accept(T.COMMA):
                    if self.at(T.RPAREN):
                        break
                    items.append(self.parse_expr())
                close = self.expect(T.RPAREN, "`)` to close a tuple")
                return A.ETuple(open_tok.span.to(close.span), items)

            close = self.expect(T.RPAREN, "`)` to close a parenthesised expression")
        return first

    def parse_record(self) -> A.Expr:
        open_tok = self.expect(T.LBRACE, "`{`")
        with self.allow_brace():
            if self.at(T.RBRACE):
                close = self.advance()
                return A.ERecord(open_tok.span.to(close.span), [], None)

            base: A.Expr | None = None
            # `{ x = ... }` / `{ x, ... }` are literals; anything else is an update
            is_literal = self.cur.kind is T.LOWER and self.peek(1).kind in (T.EQUALS, T.COMMA, T.RBRACE)
            if not is_literal:
                base = self.parse_expr()
                self.expect(T.PIPE, "`|` after the record being updated")

            fields: list[tuple[str, A.Expr]] = []
            while True:
                label = self.expect(T.LOWER, "a record field name")
                if self.accept(T.EQUALS):
                    fields.append((label.text, self.parse_expr()))
                else:  # `{ x, y }` shorthand
                    fields.append((label.text, A.EVar(label.span, label.text)))
                if not self.accept(T.COMMA):
                    break
                if self.at(T.RBRACE):
                    break
            close = self.expect(T.RBRACE, "`}` to close a record")

        span = open_tok.span.to(close.span)
        seen: dict[str, bool] = {}
        for name, _ in fields:
            if name in seen:
                raise self.error(f"duplicate field `{name}` in record", span)
            seen[name] = True
        return A.ERecord(span, fields, base)

    # -- compound forms ----------------------------------------------------

    def parse_lambda(self) -> A.Expr:
        start = self.expect(T.BACKSLASH, "`\\`").span
        params: list[A.Pattern] = []
        while not self.at(T.ARROW):
            if not self.starts_atom_pattern():
                raise self.error(
                    f"expected a parameter, found {self.describe(self.cur)}", self.cur.span)
            params.append(self.parse_atom_pattern())
        if not params:
            raise self.error("a lambda needs at least one parameter", start,
                             "write `\\x -> ...`")
        self.expect(T.ARROW, "`->` after lambda parameters")
        body = self.parse_expr()
        return A.ELambda(start.to(body.span), params, body)

    def parse_if(self) -> A.Expr:
        start = self.expect_keyword("if").span
        cond = self.parse_expr()
        self.expect_keyword("then")
        then = self.parse_expr()
        self.expect_keyword("else")
        otherwise = self.parse_expr()
        return A.EIf(start.to(otherwise.span), cond, then, otherwise)

    def parse_let_expr(self) -> A.Expr:
        start = self.expect_keyword("let").span
        recursive = self.accept_keyword("rec") is not None

        # destructuring let: `let (a, b) = e in ...`
        if not recursive and not self._binding_ahead():
            pattern = self.parse_pattern()
            self.expect(T.EQUALS, "`=` in a destructuring let")
            value = self.parse_expr()
            self.expect_keyword("in")
            body = self.parse_expr()
            return A.ELetPattern(start.to(body.span), pattern, value, body)

        bindings = [self.parse_binding()]
        while self.accept_keyword("and"):
            if not recursive:
                raise self.error("`and` requires `let rec`", self.toks[self.pos - 1].span)
            bindings.append(self.parse_binding())
        self.expect_keyword("in")
        body = self.parse_expr()
        return A.ELet(start.to(body.span), bindings, body, recursive)

    def _binding_ahead(self) -> bool:
        """True when the upcoming tokens are `name params... =` (a value binding)."""
        if self.cur.kind is not T.LOWER:
            return False
        j = self.pos + 1
        depth = 0
        while j < len(self.toks):
            k = self.toks[j].kind
            if k in (T.LPAREN, T.LBRACKET, T.LBRACE):
                depth += 1
            elif k in (T.RPAREN, T.RBRACKET, T.RBRACE):
                if depth == 0:
                    return False
                depth -= 1
            elif depth == 0:
                if k is T.EQUALS or k is T.COLON:
                    return True
                if k not in (T.LOWER, T.UNDERSCORE, T.UPPER):
                    return False
            j += 1
        return False

    def parse_match(self) -> A.Expr:
        start = self.expect_keyword("match").span
        with self.no_brace():
            scrutinee = self.parse_expr()
        self.expect(T.LBRACE, "`{` to open the match arms")
        arms: list[A.MatchArm] = []
        with self.allow_brace():
            while not self.at(T.RBRACE):
                self.accept(T.PIPE)  # tolerate ML-style leading bars
                pattern = self.parse_pattern()
                guard = None
                if self.at_keyword("if"):
                    self.advance()
                    guard = self.parse_expr()
                self.expect(T.ARROW, "`->` after a match pattern")
                body = self.parse_expr()
                arms.append(A.MatchArm(pattern, body, pattern.span.to(body.span), guard))
                if not self.accept(T.COMMA):
                    break
            close = self.expect(T.RBRACE, "`}` to close the match arms")
        if not arms:
            raise self.error("a match needs at least one arm", start.to(close.span))
        return A.EMatch(start.to(close.span), scrutinee, arms)


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def parse_module(src: str, filename: str = "<input>", name: str = "Main") -> A.Module:
    return Parser(tokenize(src, filename), filename).parse_module(name)


def parse_expr(src: str, filename: str = "<input>") -> A.Expr:
    p = Parser(tokenize(src, filename), filename)
    expr = p.parse_expr()
    if not p.cur.is_eof:
        raise p.error(f"unexpected {p.describe(p.cur)} after expression", p.cur.span)
    return expr
