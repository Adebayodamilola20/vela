/* The native functions, indexed by the order in `velac/prims.py`.
 *
 * Every one of these has a twin in `Interpreter.native` in the reference
 * interpreter, and the two must agree on results *and* on which inputs are
 * errors. Where Python is forgiving and C is not (or the reverse), the
 * behaviour is pinned to whatever the reference does.
 *
 * Strings are UTF-8, and the indexing natives count characters rather than
 * bytes, matching Python's `str`.
 */

#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "vela.h"

/* ------------------------------------------------------------------ */
/* UTF-8 helpers                                                      */
/* ------------------------------------------------------------------ */

static int utf8_sequence_length(unsigned char first)
{
    if (first < 0x80) return 1;
    if ((first & 0xE0) == 0xC0) return 2;
    if ((first & 0xF0) == 0xE0) return 3;
    if ((first & 0xF8) == 0xF0) return 4;
    return 1;   /* malformed; treat as one byte so we always advance */
}

static int utf8_length(const char *chars, int bytes)
{
    int count = 0;
    for (int i = 0; i < bytes; ) {
        i += utf8_sequence_length((unsigned char)chars[i]);
        count++;
    }
    return count;
}

/* Byte offset of character `index`, or `bytes` if it is past the end. */
static int utf8_offset(const char *chars, int bytes, int index)
{
    int offset = 0;
    for (int i = 0; i < index && offset < bytes; i++) {
        offset += utf8_sequence_length((unsigned char)chars[offset]);
    }
    return offset > bytes ? bytes : offset;
}

static uint32_t utf8_decode(const char *chars, int length)
{
    unsigned char first = (unsigned char)chars[0];
    if (length == 1) return first;
    if (length == 2) return (uint32_t)((first & 0x1F) << 6)
                          | ((unsigned char)chars[1] & 0x3F);
    if (length == 3) return (uint32_t)((first & 0x0F) << 12)
                          | (uint32_t)(((unsigned char)chars[1] & 0x3F) << 6)
                          | ((unsigned char)chars[2] & 0x3F);
    return (uint32_t)((first & 0x07) << 18)
         | (uint32_t)(((unsigned char)chars[1] & 0x3F) << 12)
         | (uint32_t)(((unsigned char)chars[2] & 0x3F) << 6)
         | ((unsigned char)chars[3] & 0x3F);
}

static int utf8_encode(uint32_t code, char *out)
{
    if (code < 0x80)    { out[0] = (char)code; return 1; }
    if (code < 0x800)   {
        out[0] = (char)(0xC0 | (code >> 6));
        out[1] = (char)(0x80 | (code & 0x3F));
        return 2;
    }
    if (code < 0x10000) {
        out[0] = (char)(0xE0 | (code >> 12));
        out[1] = (char)(0x80 | ((code >> 6) & 0x3F));
        out[2] = (char)(0x80 | (code & 0x3F));
        return 3;
    }
    out[0] = (char)(0xF0 | (code >> 18));
    out[1] = (char)(0x80 | ((code >> 12) & 0x3F));
    out[2] = (char)(0x80 | ((code >> 6) & 0x3F));
    out[3] = (char)(0x80 | (code & 0x3F));
    return 4;
}

/* ------------------------------------------------------------------ */
/* List helpers                                                       */
/* ------------------------------------------------------------------ */

/* Nil and Cons are registered first by the lowerer, so their tags are fixed. */
#define TAG_NIL  0
#define TAG_CONS 1

static Value make_nil(void)
{
    return OBJ_VAL(con_new(TAG_NIL, 0));
}

static Value list_from_values(Value *items, int count)
{
    Value result = make_nil();
    push_temp_root(result);
    for (int i = count - 1; i >= 0; i--) {
        ObjCon *cell = con_new(TAG_CONS, 2);
        cell->fields[0] = items[i];
        cell->fields[1] = result;
        result = OBJ_VAL(cell);
        pop_temp_root(1);
        push_temp_root(result);
    }
    pop_temp_root(1);
    return result;
}

static bool list_length(Value list, int *out)
{
    int count = 0;
    Value cursor = list;
    while (IS_CON(cursor)) {
        ObjCon *con = AS_CON(cursor);
        if (con->tag == TAG_NIL) { *out = count; return true; }
        if (con->tag != TAG_CONS || con->count != 2) return false;
        count++;
        cursor = con->fields[1];
    }
    return false;
}

/* ------------------------------------------------------------------ */
/* Natives                                                            */
/* ------------------------------------------------------------------ */

static Value string_value(const char *chars, int length)
{
    return OBJ_VAL(string_copy(chars, length));
}

bool native_call(int id, int arg_count, Value *args, Value *result)
{
    (void)arg_count;
    *result = UNIT_VAL;

    switch (id) {

    /* -- output ----------------------------------------------------- */
    case 0: {   /* print */
        char *text = value_to_text(args[0]);
        fputs(text, stdout);
        fputc('\n', stdout);
        free(text);
        return true;
    }
    case 1: {   /* print_err */
        char *text = value_to_text(args[0]);
        fputs(text, stderr);
        fputc('\n', stderr);
        free(text);
        return true;
    }
    case 2: {   /* show */
        char *text = value_to_show(args[0]);
        *result = string_value(text, (int)strlen(text));
        free(text);
        return true;
    }

    /* -- conversions ------------------------------------------------- */
    case 3: {   /* int_to_string */
        char buffer[32];
        int length = snprintf(buffer, sizeof buffer, "%lld",
                              (long long)AS_INT(args[0]));
        *result = string_value(buffer, length);
        return true;
    }
    case 4: {   /* float_to_string */
        char buffer[64];
        format_double(AS_FLOAT(args[0]), buffer, sizeof buffer);
        *result = string_value(buffer, (int)strlen(buffer));
        return true;
    }
    case 5: {   /* string_to_int */
        ObjString *s = AS_STRING(args[0]);
        char *end = NULL;
        long long value = strtoll(s->chars, &end, 10);
        while (end != NULL && *end == ' ') end++;
        if (end == s->chars || (end != NULL && *end != '\0')) {
            runtime_error("cannot parse '%s' as an Int", s->chars);
            return false;
        }
        *result = INT_VAL((int64_t)value);
        return true;
    }
    case 6: {   /* string_to_float */
        ObjString *s = AS_STRING(args[0]);
        char *end = NULL;
        double value = strtod(s->chars, &end);
        while (end != NULL && *end == ' ') end++;
        if (end == s->chars || (end != NULL && *end != '\0')) {
            runtime_error("cannot parse '%s' as a Float", s->chars);
            return false;
        }
        *result = FLOAT_VAL(value);
        return true;
    }
    case 7:     /* int_to_float */
        *result = FLOAT_VAL((double)AS_INT(args[0]));
        return true;
    case 8: {   /* float_to_int */
        double value = AS_FLOAT(args[0]);
        if (isnan(value) || isinf(value)) {
            runtime_error("cannot convert this float to an int");
            return false;
        }
        *result = INT_VAL((int64_t)value);
        return true;
    }

    /* -- strings ----------------------------------------------------- */
    case 9: {   /* string_length */
        ObjString *s = AS_STRING(args[0]);
        *result = INT_VAL(utf8_length(s->chars, s->length));
        return true;
    }
    case 10: {  /* string_get */
        ObjString *s = AS_STRING(args[0]);
        int64_t index = AS_INT(args[1]);
        int count = utf8_length(s->chars, s->length);
        if (index < 0 || index >= count) {
            runtime_error("string index %lld out of bounds (length %d)",
                          (long long)index, count);
            return false;
        }
        int start = utf8_offset(s->chars, s->length, (int)index);
        int width = utf8_sequence_length((unsigned char)s->chars[start]);
        *result = string_value(s->chars + start, width);
        return true;
    }
    case 11: {  /* string_slice */
        ObjString *s = AS_STRING(args[0]);
        int count = utf8_length(s->chars, s->length);
        int64_t from = AS_INT(args[1]);
        int64_t to = AS_INT(args[2]);
        if (from < 0) from = 0;
        if (from > count) from = count;
        if (to < from) to = from;
        if (to > count) to = count;
        int start = utf8_offset(s->chars, s->length, (int)from);
        int end = utf8_offset(s->chars, s->length, (int)to);
        *result = string_value(s->chars + start, end - start);
        return true;
    }
    case 12: {  /* string_index_of */
        ObjString *haystack = AS_STRING(args[0]);
        ObjString *needle = AS_STRING(args[1]);
        if (needle->length == 0) { *result = INT_VAL(0); return true; }
        const char *found = strstr(haystack->chars, needle->chars);
        if (found == NULL) { *result = INT_VAL(-1); return true; }
        *result = INT_VAL(utf8_length(haystack->chars,
                                      (int)(found - haystack->chars)));
        return true;
    }
    case 13: {  /* string_split */
        ObjString *s = AS_STRING(args[0]);
        ObjString *sep = AS_STRING(args[1]);

        Value *parts = NULL;
        int count = 0, capacity = 0;

        #define PUSH_PART(ptr, len)                                        \
            do {                                                           \
                if (count + 1 > capacity) {                                \
                    capacity = capacity < 8 ? 8 : capacity * 2;            \
                    parts = (Value *)realloc(parts,                        \
                                             sizeof(Value) * (size_t)capacity); \
                }                                                          \
                parts[count] = string_value((ptr), (len));                 \
                push_temp_root(parts[count]);                              \
                count++;                                                   \
            } while (false)

        if (sep->length == 0) {
            for (int i = 0; i < s->length; ) {
                int width = utf8_sequence_length((unsigned char)s->chars[i]);
                PUSH_PART(s->chars + i, width);
                i += width;
            }
        } else {
            const char *cursor = s->chars;
            const char *end = s->chars + s->length;
            while (true) {
                const char *hit = strstr(cursor, sep->chars);
                if (hit == NULL || hit > end) {
                    PUSH_PART(cursor, (int)(end - cursor));
                    break;
                }
                PUSH_PART(cursor, (int)(hit - cursor));
                cursor = hit + sep->length;
            }
        }
        #undef PUSH_PART

        *result = list_from_values(parts, count);
        pop_temp_root(count);
        free(parts);
        return true;
    }
    case 14: {  /* string_join */
        int count = 0;
        if (!list_length(args[0], &count)) {
            runtime_error("string_join expects a list");
            return false;
        }
        ObjString *sep = AS_STRING(args[1]);

        int total = 0;
        Value cursor = args[0];
        for (int i = 0; i < count; i++) {
            total += AS_STRING(AS_CON(cursor)->fields[0])->length;
            cursor = AS_CON(cursor)->fields[1];
        }
        if (count > 1) total += sep->length * (count - 1);

        /* `reallocate`, not `malloc`: this buffer becomes an ObjString's, and
         * the collector charges it back on free. Allocating it off the books
         * would drift `bytes_allocated` down on every collection. */
        char *chars = (char *)reallocate(NULL, 0, (size_t)total + 1);
        int offset = 0;
        cursor = args[0];
        for (int i = 0; i < count; i++) {
            if (i > 0) {
                memcpy(chars + offset, sep->chars, (size_t)sep->length);
                offset += sep->length;
            }
            ObjString *part = AS_STRING(AS_CON(cursor)->fields[0]);
            memcpy(chars + offset, part->chars, (size_t)part->length);
            offset += part->length;
            cursor = AS_CON(cursor)->fields[1];
        }
        chars[offset] = '\0';
        *result = OBJ_VAL(string_take(chars, offset));
        return true;
    }
    case 15: {  /* string_from_code */
        char buffer[4];
        int width = utf8_encode((uint32_t)AS_INT(args[0]), buffer);
        *result = string_value(buffer, width);
        return true;
    }
    case 16: {  /* string_code_at */
        ObjString *s = AS_STRING(args[0]);
        int64_t index = AS_INT(args[1]);
        int count = utf8_length(s->chars, s->length);
        if (index < 0 || index >= count) {
            runtime_error("string index %lld out of bounds (length %d)",
                          (long long)index, count);
            return false;
        }
        int start = utf8_offset(s->chars, s->length, (int)index);
        int width = utf8_sequence_length((unsigned char)s->chars[start]);
        *result = INT_VAL(utf8_decode(s->chars + start, width));
        return true;
    }
    case 17: {  /* string_trim */
        ObjString *s = AS_STRING(args[0]);
        int start = 0;
        int end = s->length;
        while (start < end && (unsigned char)s->chars[start] <= ' ') start++;
        while (end > start && (unsigned char)s->chars[end - 1] <= ' ') end--;
        *result = string_value(s->chars + start, end - start);
        return true;
    }
    case 18:    /* string_upper */
    case 19: {  /* string_lower */
        /* ASCII only, which is what Python's str.upper does for ASCII input;
         * non-ASCII passes through unchanged rather than being mangled. */
        ObjString *s = AS_STRING(args[0]);
        char *chars = (char *)reallocate(NULL, 0, (size_t)s->length + 1);
        for (int i = 0; i < s->length; i++) {
            unsigned char c = (unsigned char)s->chars[i];
            if (id == 18 && c >= 'a' && c <= 'z') c = (unsigned char)(c - 32);
            else if (id == 19 && c >= 'A' && c <= 'Z') c = (unsigned char)(c + 32);
            chars[i] = (char)c;
        }
        chars[s->length] = '\0';
        *result = OBJ_VAL(string_take(chars, s->length));
        return true;
    }

    /* -- floats ------------------------------------------------------ */
    case 20: {  /* float_sqrt */
        double x = AS_FLOAT(args[0]);
        *result = FLOAT_VAL(x < 0 ? NAN : sqrt(x));
        return true;
    }
    case 21: *result = FLOAT_VAL(floor(AS_FLOAT(args[0]))); return true;
    case 22: *result = FLOAT_VAL(ceil(AS_FLOAT(args[0])));  return true;
    case 23: *result = FLOAT_VAL(fabs(AS_FLOAT(args[0])));  return true;
    case 24: *result = FLOAT_VAL(sin(AS_FLOAT(args[0])));   return true;
    case 25: *result = FLOAT_VAL(cos(AS_FLOAT(args[0])));   return true;
    case 26: *result = FLOAT_VAL(tan(AS_FLOAT(args[0])));   return true;
    case 27: *result = FLOAT_VAL(exp(AS_FLOAT(args[0])));   return true;
    case 28: {  /* float_log */
        double x = AS_FLOAT(args[0]);
        if (x < 0)  { *result = FLOAT_VAL(NAN); return true; }
        if (x == 0) { *result = FLOAT_VAL(-INFINITY); return true; }
        *result = FLOAT_VAL(log(x));
        return true;
    }
    case 29:
        *result = FLOAT_VAL(pow(AS_FLOAT(args[0]), AS_FLOAT(args[1])));
        return true;

    /* -- ints -------------------------------------------------------- */
    case 30: {  /* int_abs */
        int64_t v = AS_INT(args[0]);
        *result = INT_VAL(v < 0 ? wrap_sub(0, v) : v);
        return true;
    }
    case 31: {  /* int_min */
        int64_t a = AS_INT(args[0]), b = AS_INT(args[1]);
        *result = INT_VAL(a < b ? a : b);
        return true;
    }
    case 32: {  /* int_max */
        int64_t a = AS_INT(args[0]), b = AS_INT(args[1]);
        *result = INT_VAL(a > b ? a : b);
        return true;
    }
    case 33:    /* int_shl */
        *result = INT_VAL((int64_t)((uint64_t)AS_INT(args[0])
                                    << (AS_INT(args[1]) & 63)));
        return true;
    case 34:    /* int_shr */
        *result = INT_VAL(AS_INT(args[0]) >> (AS_INT(args[1]) & 63));
        return true;
    case 35: *result = INT_VAL(AS_INT(args[0]) & AS_INT(args[1])); return true;
    case 36: *result = INT_VAL(AS_INT(args[0]) | AS_INT(args[1])); return true;
    case 37: *result = INT_VAL(AS_INT(args[0]) ^ AS_INT(args[1])); return true;

    case 38: {  /* compare */
        bool ok = true;
        int order = values_compare(args[0], args[1], &ok);
        if (!ok) {
            runtime_error("cannot compare these values");
            return false;
        }
        *result = INT_VAL(order);
        return true;
    }

    case 39: {  /* panic */
        char *text = value_to_text(args[0]);
        runtime_error("%s", text);
        free(text);
        return false;
    }

    /* -- arrays ------------------------------------------------------ */
    case 40: {  /* array_new */
        int64_t n = AS_INT(args[0]);
        /* Bounded above as well as below: an array's length is an `int`, and
         * silently truncating to it would hand back an array of the wrong
         * size rather than reporting that the request cannot be met. */
        if (n < 0 || n > INT32_MAX) {
            runtime_error("cannot create an array of length %lld", (long long)n);
            return false;
        }
        ObjTuple *array = tuple_new((int)n);
        for (int i = 0; i < (int)n; i++) array->items[i] = args[1];
        *result = OBJ_VAL(array);
        return true;
    }
    case 41: {  /* array_get */
        if (!IS_TUPLE(args[0])) {
            runtime_error("array_get expects an array");
            return false;
        }
        ObjTuple *array = AS_TUPLE(args[0]);
        int64_t index = AS_INT(args[1]);
        if (index < 0 || index >= array->count) {
            runtime_error("array index %lld out of bounds (length %d)",
                          (long long)index, array->count);
            return false;
        }
        *result = array->items[index];
        return true;
    }
    case 42: {  /* array_set */
        if (!IS_TUPLE(args[0])) {
            runtime_error("array_set expects an array");
            return false;
        }
        ObjTuple *source = AS_TUPLE(args[0]);
        int64_t index = AS_INT(args[1]);
        if (index < 0 || index >= source->count) {
            runtime_error("array index %lld out of bounds (length %d)",
                          (long long)index, source->count);
            return false;
        }
        ObjTuple *copy = tuple_new(source->count);
        for (int i = 0; i < source->count; i++) copy->items[i] = source->items[i];
        copy->items[index] = args[2];
        *result = OBJ_VAL(copy);
        return true;
    }
    case 43:    /* array_length */
        *result = INT_VAL(IS_TUPLE(args[0]) ? AS_TUPLE(args[0])->count : 0);
        return true;
    case 44: {  /* array_from_list */
        int count = 0;
        if (!list_length(args[0], &count)) {
            runtime_error("array_from_list expects a list");
            return false;
        }
        ObjTuple *array = tuple_new(count);
        push_temp_root(OBJ_VAL(array));
        Value cursor = args[0];
        for (int i = 0; i < count; i++) {
            array->items[i] = AS_CON(cursor)->fields[0];
            cursor = AS_CON(cursor)->fields[1];
        }
        pop_temp_root(1);
        *result = OBJ_VAL(array);
        return true;
    }
    case 45: {  /* array_to_list */
        if (!IS_TUPLE(args[0])) {
            runtime_error("array_to_list expects an array");
            return false;
        }
        ObjTuple *array = AS_TUPLE(args[0]);
        *result = list_from_values(array->items, array->count);
        return true;
    }

    /* -- environment ------------------------------------------------- */
    case 46: {  /* clock_ms */
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        *result = INT_VAL((int64_t)ts.tv_sec * 1000
                        + (int64_t)(ts.tv_nsec / 1000000));
        return true;
    }
    case 47: {  /* read_line */
        char *line = NULL;
        size_t capacity = 0;
        ssize_t length = getline(&line, &capacity, stdin);
        if (length < 0) {
            free(line);
            *result = string_value("", 0);
            return true;
        }
        if (length > 0 && line[length - 1] == '\n') length--;
        *result = string_value(line, (int)length);
        free(line);
        return true;
    }
    case 48:    /* arg_count */
        *result = INT_VAL(vm.argc);
        return true;
    case 49: {  /* arg_get */
        int64_t index = AS_INT(args[0]);
        if (index < 0 || index >= vm.argc) {
            *result = string_value("", 0);
        } else {
            *result = string_value(vm.argv[index],
                                   (int)strlen(vm.argv[index]));
        }
        return true;
    }

    default:
        runtime_error("unknown native %d", id);
        return false;
    }
}
