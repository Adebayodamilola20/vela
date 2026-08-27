"""The primitive operations shared by both backends.

Two families:

*   **Inline prims** compile to a dedicated VM opcode. These are the ones the
    surface operators lower to, so arithmetic never pays for a call.
*   **Natives** are runtime functions reached by index through one
    `CALL_NATIVE` opcode. The standard library binds them with `extern`.

The tables here are the single source of truth: `velac/opcodes.py` generates
the C header from them, the reference interpreter implements them by name, and
the VM implements them by index. If the three ever disagree, the differential
tests catch it.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Inline primitives (one dedicated opcode each)
# --------------------------------------------------------------------------

#: name -> (arity, opcode suffix)
INLINE_PRIMS: dict[str, tuple[int, str]] = {
    "int_add": (2, "ADD_INT"),
    "int_sub": (2, "SUB_INT"),
    "int_mul": (2, "MUL_INT"),
    "int_div": (2, "DIV_INT"),
    "int_mod": (2, "MOD_INT"),
    "int_pow": (2, "POW_INT"),
    "int_neg": (1, "NEG_INT"),
    "float_add": (2, "ADD_FLOAT"),
    "float_sub": (2, "SUB_FLOAT"),
    "float_mul": (2, "MUL_FLOAT"),
    "float_div": (2, "DIV_FLOAT"),
    "float_neg": (1, "NEG_FLOAT"),
    "eq": (2, "EQ"),
    "ne": (2, "NE"),
    "lt": (2, "LT"),
    "le": (2, "LE"),
    "gt": (2, "GT"),
    "ge": (2, "GE"),
    "bool_not": (1, "NOT"),
    "string_concat": (2, "CONCAT_STRING"),
    "list_append": (2, "APPEND_LIST"),
}

# --------------------------------------------------------------------------
# Native functions (called by index)
# --------------------------------------------------------------------------

#: Order defines the native index baked into bytecode — only ever append here.
NATIVES: list[tuple[str, int]] = [
    ("print", 1),
    ("print_err", 1),
    ("show", 1),
    ("int_to_string", 1),
    ("float_to_string", 1),
    ("string_to_int", 1),
    ("string_to_float", 1),
    ("int_to_float", 1),
    ("float_to_int", 1),
    ("string_length", 1),
    ("string_get", 2),
    ("string_slice", 3),
    ("string_index_of", 2),
    ("string_split", 2),
    ("string_join", 2),
    ("string_from_code", 1),
    ("string_code_at", 2),
    ("string_trim", 1),
    ("string_upper", 1),
    ("string_lower", 1),
    ("float_sqrt", 1),
    ("float_floor", 1),
    ("float_ceil", 1),
    ("float_abs", 1),
    ("float_sin", 1),
    ("float_cos", 1),
    ("float_tan", 1),
    ("float_exp", 1),
    ("float_log", 1),
    ("float_pow", 2),
    ("int_abs", 1),
    ("int_min", 2),
    ("int_max", 2),
    ("int_shl", 2),
    ("int_shr", 2),
    ("int_and", 2),
    ("int_or", 2),
    ("int_xor", 2),
    ("compare", 2),
    ("panic", 1),
    ("array_new", 2),
    ("array_get", 2),
    ("array_set", 3),
    ("array_length", 1),
    ("array_from_list", 1),
    ("array_to_list", 1),
    ("clock_ms", 1),
    ("read_line", 1),
    ("arg_count", 1),
    ("arg_get", 1),
]

NATIVE_INDEX: dict[str, int] = {name: i for i, (name, _) in enumerate(NATIVES)}
NATIVE_ARITY: dict[str, int] = {name: arity for name, arity in NATIVES}


def native_index(name: str) -> int | None:
    return NATIVE_INDEX.get(name)


# --------------------------------------------------------------------------
# Surface operator -> primitive
# --------------------------------------------------------------------------

BINOP_PRIM: dict[str, str] = {
    "+": "int_add",
    "-": "int_sub",
    "*": "int_mul",
    "/": "int_div",
    "%": "int_mod",
    "**": "int_pow",
    "+.": "float_add",
    "-.": "float_sub",
    "*.": "float_mul",
    "/.": "float_div",
    "==": "eq",
    "!=": "ne",
    "<": "lt",
    "<=": "le",
    ">": "gt",
    ">=": "ge",
    "^": "string_concat",
    "++": "list_append",
}

UNOP_PRIM: dict[str, str] = {
    "neg": "int_neg",
    "negf": "float_neg",
    "not": "bool_not",
}
