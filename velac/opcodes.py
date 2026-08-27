"""The bytecode instruction set.

This module is the single source of truth for the VM's opcodes. `compile.py`
emits against the `Op` enum, `emit.py` serialises it, `interp_bc.py` decodes it,
and `vm/src/opcodes.h` is *generated* from it by `python3 -m velac.opcodes`.
Nothing else may hardcode an opcode number.

Encoding
--------
Every instruction is one byte of opcode followed by a fixed number of operands,
each a little-endian `u16`. Fixed-width operands cost a little space but let the
VM advance the instruction pointer without a decode table, and let a jump be
patched after the fact without moving anything.

Jumps are *unsigned* offsets relative to the byte just past the operand, so a
forward jump of 0 is a no-op. `LOOP` is the backward form, subtracting instead
of adding, so the operand stays unsigned in both directions.
"""

from __future__ import annotations

from enum import IntEnum

from .prims import INLINE_PRIMS, NATIVES

# --------------------------------------------------------------------------
# Instruction table
# --------------------------------------------------------------------------

#: (mnemonic, operand count, stack effect, doc)
#:
#: A stack effect of `None` means "depends on an operand" — the compiler
#: computes it rather than reading it from here.
_CORE: list[tuple[str, int, int | None, str]] = [
    ("NOP",            0,  0, "do nothing"),
    ("CONST",          1,  1, "push constants[a]"),
    ("TRUE",           0,  1, "push True"),
    ("FALSE",          0,  1, "push False"),
    ("UNIT",           0,  1, "push ()"),
    ("POP",            0, -1, "discard the top of the stack"),
    ("DUP",            0,  1, "duplicate the top of the stack"),

    # -- variables --------------------------------------------------------
    ("GET_LOCAL",      1,  1, "push frame slot a"),
    ("SET_LOCAL",      1, -1, "pop into frame slot a"),
    ("GET_UPVAL",      1,  1, "push upvalue a of the running closure"),
    ("GET_GLOBAL",     1,  1, "push globals[a]"),
    ("SET_GLOBAL",     1, -1, "pop into globals[a]"),

    # -- control flow -----------------------------------------------------
    ("JUMP",           1,  0, "ip += a"),
    ("JUMP_IF_FALSE",  1, -1, "pop; if falsey, ip += a"),
    ("JUMP_IF_TAG",    1,  0, "peek; if its constructor tag == a, ip += b"),
    ("JUMP_IF_LIT",    1,  0, "peek; if it equals constants[a], ip += b"),
    ("LOOP",           1,  0, "ip -= a"),

    # -- calls ------------------------------------------------------------
    ("CALL",           1,  None, "call the value below a arguments"),
    ("TAIL_CALL",      1,  None, "as CALL, reusing the current frame"),
    ("RETURN",         0,  0, "return the top of the stack"),
    ("CLOSURE",        1,  1, "instantiate functions[a], capturing upvalues"),
    ("CALL_NATIVE",    2,  None, "call native a with b arguments"),

    # -- data -------------------------------------------------------------
    ("MAKE_CON",       2,  None, "build constructor a from b stacked fields"),
    ("MAKE_TUPLE",     1,  None, "build a tuple from a stacked items"),
    ("GET_FIELD",      1,  0, "replace the top with its field a"),
    ("MAKE_RECORD",    1,  None, "build a record with label set a"),
    ("RECORD_GET",     1,  0, "replace the top record with its field named a"),
    ("RECORD_SET",     1,  None, "functional update with label set a"),

    # -- misc -------------------------------------------------------------
    ("FAIL",           1,  0, "abort with constants[a] as the message"),
    ("HALT",           0,  0, "stop the interpreter"),
]

#: `JUMP_IF_TAG` and `JUMP_IF_LIT` take two operands (key, offset). The table
#: above lists one because the second is always the jump; record the truth here
#: so the encoder and the C header agree.
_EXTRA_OPERAND = {"JUMP_IF_TAG", "JUMP_IF_LIT"}


def _build() -> list[tuple[str, int, int | None, str]]:
    """Core instructions, then one opcode per inline primitive."""
    table = [
        (name, count + (1 if name in _EXTRA_OPERAND else 0), effect, doc)
        for name, count, effect, doc in _CORE
    ]
    for prim, (arity, suffix) in INLINE_PRIMS.items():
        # A prim pops its arguments and pushes one result.
        table.append((suffix, 0, 1 - arity, f"{prim}/{arity}"))
    return table


TABLE = _build()

#: mnemonic -> opcode byte
Op = IntEnum("Op", [(name, i) for i, (name, *_) in enumerate(TABLE)])

OPERANDS: dict[str, int] = {name: n for name, n, _, _ in TABLE}
STACK_EFFECT: dict[str, int | None] = {name: e for name, _, e, _ in TABLE}
DOC: dict[str, str] = {name: d for name, _, _, d in TABLE}

#: Inline primitive name -> the opcode that implements it.
PRIM_OP: dict[str, "Op"] = {
    prim: Op[suffix] for prim, (_, suffix) in INLINE_PRIMS.items()
}

assert len(TABLE) <= 256, "the opcode byte only has room for 256 instructions"


def operand_count(op: "Op") -> int:
    return OPERANDS[op.name]


def instruction_size(op: "Op") -> int:
    """Bytes occupied by `op` and its operands."""
    return 1 + 2 * OPERANDS[op.name]


# --------------------------------------------------------------------------
# Binary image format
# --------------------------------------------------------------------------

MAGIC = b"VELA"
VERSION = 1

#: Constant pool tags.
TAG_INT = 0
TAG_FLOAT = 1
TAG_STRING = 2
TAG_BOOL = 3
TAG_UNIT = 4
TAG_LABELS = 5     # a record's sorted label list


# --------------------------------------------------------------------------
# C header generation
# --------------------------------------------------------------------------


def c_header() -> str:
    """Render `vm/src/opcodes.h` from the table above."""
    lines = [
        "/* Generated by `python3 -m velac.opcodes`. Do not edit. */",
        "#ifndef VELA_OPCODES_H",
        "#define VELA_OPCODES_H",
        "",
        "typedef enum {",
    ]
    for i, (name, operands, _, doc) in enumerate(TABLE):
        lines.append(f"    OP_{name:<16} = {i:>3},  /* {operands} operand(s): {doc} */")
    lines += [
        "} OpCode;",
        "",
        f"#define VELA_OPCODE_COUNT {len(TABLE)}",
        "",
        "/* Operand count per opcode, indexed by OpCode. */",
        "static const unsigned char VELA_OPERANDS[VELA_OPCODE_COUNT] = {",
    ]
    row = "    " + ", ".join(str(n) for _, n, _, _ in TABLE)
    lines += [row, "};", ""]

    lines += [
        "/* Native function arities, indexed by native id. */",
        f"#define VELA_NATIVE_COUNT {len(NATIVES)}",
        "static const unsigned char VELA_NATIVE_ARITY[VELA_NATIVE_COUNT] = {",
        "    " + ", ".join(str(arity) for _, arity in NATIVES),
        "};",
        "",
        "static const char *const VELA_NATIVE_NAMES[VELA_NATIVE_COUNT] = {",
        "    " + ", ".join(f'"{name}"' for name, _ in NATIVES),
        "};",
        "",
        f'#define VELA_MAGIC "{MAGIC.decode()}"',
        f"#define VELA_VERSION {VERSION}",
        "",
        "#endif /* VELA_OPCODES_H */",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    import os
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "vm", "src", "opcodes.h")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(c_header())
    print(f"wrote {out} ({len(TABLE)} opcodes, {len(NATIVES)} natives)", file=sys.stderr)


if __name__ == "__main__":
    main()
