"""Serialise an `Image` to a `.velac` file.

Format — everything little-endian, lengths as u32 unless noted:

    magic     "VELA"
    version   u16
    flags     u16                  (reserved, must be 0)

    constants u32 count, then each: u8 tag, then payload
                Int     i64
                Float   f64
                String  u32 byte length, then UTF-8 bytes
                Bool    u8
                Unit    -
    labels    u32 count, then each: u16 field count, then that many
                                    u32-length UTF-8 strings
    cons      u32 count, then each: u16 arity, u32-length name,
                                    u32-length type name
    globals   u32 count, then that many u32-length names (debug only)
    functions u32 count, then each:
                u32-length name
                u16 arity
                u16 max_slots
                u16 upvalue count, then each: u8 from_parent_local, u16 index
                u32 code length, then that many bytes
    entry     u32 function index

The global *names* are written even though the VM addresses globals by index,
because a stack trace that says `List.map` is worth the handful of bytes.
"""

from __future__ import annotations

import struct

from .compile import Image
from .opcodes import (
    MAGIC, TAG_BOOL, TAG_FLOAT, TAG_INT, TAG_STRING, TAG_UNIT, VERSION,
)


def _u8(value: int) -> bytes:
    return struct.pack("<B", value)


def _u16(value: int) -> bytes:
    return struct.pack("<H", value)


def _u32(value: int) -> bytes:
    return struct.pack("<I", value)


def _i64(value: int) -> bytes:
    return struct.pack("<q", value)


def _f64(value: float) -> bytes:
    return struct.pack("<d", value)


def _string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return _u32(len(raw)) + raw


def serialise(image: Image) -> bytes:
    out = bytearray()
    out += MAGIC
    out += _u16(VERSION)
    out += _u16(0)

    # -- constants ---------------------------------------------------------
    out += _u32(len(image.constants))
    for kind, value in image.constants:
        if kind == "Int":
            out += _u8(TAG_INT) + _i64(int(value))
        elif kind == "Float":
            out += _u8(TAG_FLOAT) + _f64(float(value))
        elif kind == "String":
            out += _u8(TAG_STRING) + _string(str(value))
        elif kind == "Bool":
            out += _u8(TAG_BOOL) + _u8(1 if value else 0)
        elif kind == "Unit":
            out += _u8(TAG_UNIT)
        else:
            raise ValueError(f"cannot serialise a {kind} constant")

    # -- record label sets -------------------------------------------------
    out += _u32(len(image.label_sets))
    for labels in image.label_sets:
        out += _u16(len(labels))
        for label in labels:
            out += _string(label)

    # -- constructors ------------------------------------------------------
    out += _u32(len(image.constructors))
    for name, arity, type_name in image.constructors:
        out += _u16(arity)
        out += _string(name)
        out += _string(type_name)

    # -- globals -----------------------------------------------------------
    out += _u32(len(image.globals))
    for name in image.globals:
        out += _string(name)

    # -- functions ---------------------------------------------------------
    out += _u32(len(image.functions))
    for fn in image.functions:
        out += _string(fn.name)
        out += _u16(fn.arity)
        out += _u16(fn.max_slots)
        out += _u16(len(fn.upvalues))
        for up in fn.upvalues:
            out += _u8(1 if up.from_parent_local else 0)
            out += _u16(up.index)
        out += _u32(len(fn.code))
        out += bytes(fn.code)

    out += _u32(image.entry)
    return bytes(out)


def write_image(image: Image, path: str) -> None:
    with open(path, "wb") as fh:
        fh.write(serialise(image))


# --------------------------------------------------------------------------
# Disassembly
# --------------------------------------------------------------------------


def disassemble(image: Image) -> str:
    """Render an image as text, for `velac build --dump` and for debugging."""
    from .opcodes import OPERANDS, Op

    lines: list[str] = []

    lines.append("constants:")
    for i, (kind, value) in enumerate(image.constants):
        lines.append(f"  {i:>4}  {kind:<7} {value!r}")

    if image.label_sets:
        lines.append("record label sets:")
        for i, labels in enumerate(image.label_sets):
            lines.append(f"  {i:>4}  {{{', '.join(labels)}}}")

    lines.append("globals:")
    for i, name in enumerate(image.globals):
        lines.append(f"  {i:>4}  {name}")

    for fn in image.functions:
        marker = "  <-- entry" if fn.index == image.entry else ""
        lines.append("")
        lines.append(f"function {fn.index} {fn.name}/{fn.arity} "
                     f"(slots {fn.max_slots}, upvalues {len(fn.upvalues)}){marker}")
        for i, up in enumerate(fn.upvalues):
            source = "local" if up.from_parent_local else "upvalue"
            lines.append(f"    upvalue {i}: parent {source} {up.index}")

        offset = 0
        code = fn.code
        while offset < len(code):
            op = Op(code[offset])
            count = OPERANDS[op.name]
            operands = [
                code[offset + 1 + 2 * i] | (code[offset + 2 + 2 * i] << 8)
                for i in range(count)
            ]
            rendered = " ".join(str(v) for v in operands)
            note = _annotate(image, op, operands, offset, count)
            lines.append(f"  {offset:>5}  {op.name:<16} {rendered:<12}{note}")
            offset += 1 + 2 * count

    return "\n".join(lines)


def _annotate(image: Image, op, operands: list[int], offset: int,
              count: int) -> str:
    """A short comment explaining what an operand refers to."""
    from .opcodes import Op

    end = offset + 1 + 2 * count
    if op in (Op.JUMP, Op.JUMP_IF_FALSE, Op.LOOP):
        target = end + operands[0] if op is not Op.LOOP else end - operands[0]
        return f"; -> {target}"
    if op in (Op.JUMP_IF_TAG, Op.JUMP_IF_LIT):
        target = end + operands[1]
        if op is Op.JUMP_IF_LIT and operands[0] < len(image.constants):
            return f"; {image.constants[operands[0]][1]!r} -> {target}"
        if op is Op.JUMP_IF_TAG and operands[0] < len(image.constructors):
            return f"; {image.constructors[operands[0]][0]} -> {target}"
        return f"; -> {target}"
    if op is Op.CONST and operands[0] < len(image.constants):
        return f"; {image.constants[operands[0]][1]!r}"
    if op in (Op.GET_GLOBAL, Op.SET_GLOBAL) and operands[0] < len(image.globals):
        return f"; {image.globals[operands[0]]}"
    if op is Op.MAKE_CON and operands[0] < len(image.constructors):
        return f"; {image.constructors[operands[0]][0]}"
    if op in (Op.MAKE_RECORD, Op.RECORD_SET) and operands[0] < len(image.label_sets):
        return f"; {{{', '.join(image.label_sets[operands[0]])}}}"
    if op is Op.RECORD_GET and operands[0] < len(image.constants):
        return f"; .{image.constants[operands[0]][1]}"
    if op is Op.CLOSURE and operands[0] < len(image.functions):
        return f"; {image.functions[operands[0]].name}"
    if op is Op.CALL_NATIVE:
        from .prims import NATIVES
        if operands[0] < len(NATIVES):
            return f"; {NATIVES[operands[0]][0]}"
    if op is Op.FAIL and operands[0] < len(image.constants):
        return f"; {image.constants[operands[0]][1]!r}"
    return ""
