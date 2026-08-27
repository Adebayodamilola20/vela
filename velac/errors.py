"""Source positions, spans, and human-readable diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Positions
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Span:
    """A half-open byte range [start, end) inside a single source file."""

    file: str
    start: int
    end: int
    line: int  # 1-based line of `start`
    col: int  # 1-based column of `start`

    def to(self, other: "Span") -> "Span":
        """The smallest span covering both `self` and `other`."""
        if other is None:
            return self
        if other.file != self.file:
            return self
        if self.start <= other.start:
            return Span(self.file, self.start, max(self.end, other.end), self.line, self.col)
        return Span(self.file, other.start, max(self.end, other.end), other.line, other.col)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.file}:{self.line}:{self.col}"


#: Used for compiler-synthesised nodes that have no real source location.
SYNTHETIC = Span("<synthetic>", 0, 0, 0, 0)


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------


@dataclass
class Label:
    span: Span
    message: str
    primary: bool = True


@dataclass
class Diagnostic(Exception):
    """A compile-time error, carrying enough context to render nicely."""

    message: str
    labels: list[Label] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    code: str | None = None
    severity: str = "error"

    def __str__(self) -> str:
        return self.message


class VelaError(Diagnostic):
    """Alias kept so `except VelaError` reads well at call sites."""


def error(message: str, span: Span | None = None, label: str = "", *,
          notes: list[str] | None = None, code: str | None = None) -> Diagnostic:
    labels = [Label(span, label)] if span is not None else []
    return Diagnostic(message, labels, list(notes or ()), code)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_ANSI = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "red": "\x1b[31;1m",
    "yellow": "\x1b[33;1m",
    "blue": "\x1b[34;1m",
    "cyan": "\x1b[36;1m",
}


class SourceMap:
    """Remembers file contents so diagnostics can quote the offending line."""

    def __init__(self) -> None:
        self._files: dict[str, str] = {}

    def add(self, path: str, text: str) -> None:
        self._files[path] = text

    def get(self, path: str) -> str | None:
        return self._files.get(path)

    # -- span → (line_no, col, line_text) ---------------------------------

    def line_at(self, span: Span) -> tuple[int, int, str] | None:
        text = self._files.get(span.file)
        if text is None:
            return None
        start = text.rfind("\n", 0, span.start) + 1
        end = text.find("\n", span.start)
        if end == -1:
            end = len(text)
        line_no = text.count("\n", 0, span.start) + 1
        return line_no, span.start - start + 1, text[start:end]

    # -- full render -------------------------------------------------------

    def render(self, diag: Diagnostic, color: bool = True) -> str:
        def c(name: str, s: str) -> str:
            return f"{_ANSI[name]}{s}{_ANSI['reset']}" if color else s

        sev_color = {"error": "red", "warning": "yellow", "note": "blue"}.get(diag.severity, "red")
        head = c(sev_color, diag.severity)
        if diag.code:
            head += c(sev_color, f"[{diag.code}]")
        out = [f"{head}: {c('bold', diag.message)}"]

        for label in diag.labels:
            span = label.span
            loc = self.line_at(span)
            if loc is None:
                out.append(f"  {c('blue', '-->')} {span.file}:{span.line}:{span.col}")
                continue
            line_no, col, line_text = loc
            gutter = str(line_no)
            pad = " " * len(gutter)
            width = max(1, min(span.end - span.start, len(line_text) - col + 1))
            caret_ch = "^" if label.primary else "-"
            caret = " " * (col - 1) + caret_ch * width
            out.append(f"  {c('blue', '-->')} {span.file}:{line_no}:{col}")
            out.append(f"  {c('blue', pad + ' |')}")
            out.append(f"  {c('blue', gutter + ' |')} {line_text}")
            tail = f" {label.message}" if label.message else ""
            out.append(f"  {c('blue', pad + ' |')} {c(sev_color, caret)}{c(sev_color, tail)}")

        for note in diag.notes:
            out.append(f"  {c('cyan', '= note:')} {note}")
        return "\n".join(out)


#: Process-wide source map. The driver populates it as it reads files.
SOURCES = SourceMap()
