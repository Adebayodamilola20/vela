/* Values, objects, the collector, and rendering.
 *
 * Rendering is the part to be careful with: `velac/interp.py` is the oracle,
 * and the differential tests compare stdout byte for byte, so `show` and the
 * float formatter here have to reproduce Python's output exactly rather than
 * merely something reasonable.
 */

#include <math.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

#include "vela.h"

#define GC_HEAP_GROW_FACTOR 2

/* ------------------------------------------------------------------ */
/* Allocation                                                         */
/* ------------------------------------------------------------------ */

void *reallocate(void *pointer, size_t old_size, size_t new_size)
{
    vm.bytes_allocated += new_size - old_size;

    if (new_size > old_size && vm.bytes_allocated > vm.next_gc) {
        collect_garbage();
    }

    if (new_size == 0) {
        free(pointer);
        return NULL;
    }

    void *result = realloc(pointer, new_size);
    if (result == NULL) {
        fprintf(stderr, "vela: out of memory\n");
        exit(70);
    }
    return result;
}

static Obj *allocate_object(size_t size, ObjType type)
{
    Obj *object = (Obj *)reallocate(NULL, 0, size);
    object->type = type;
    object->marked = false;
    object->next = vm.objects;
    vm.objects = object;
    return object;
}

#define ALLOCATE_OBJ(type, objectType) \
    (type *)allocate_object(sizeof(type), objectType)

/* ------------------------------------------------------------------ */
/* Temporary roots                                                    */
/* ------------------------------------------------------------------ */

/* A native that allocates more than once needs its intermediate results kept
 * alive; the stack is not enough because the arguments have already been
 * popped by the time it runs. */
void push_temp_root(Value value)
{
    if (vm.temp_root_count + 1 > vm.temp_root_capacity) {
        int old = vm.temp_root_capacity;
        vm.temp_root_capacity = old < 8 ? 8 : old * 2;
        vm.temp_roots = (Value *)reallocate(
            vm.temp_roots, sizeof(Value) * old,
            sizeof(Value) * vm.temp_root_capacity);
    }
    vm.temp_roots[vm.temp_root_count++] = value;
}

void pop_temp_root(int count)
{
    vm.temp_root_count -= count;
    if (vm.temp_root_count < 0) vm.temp_root_count = 0;
}

/* ------------------------------------------------------------------ */
/* Constructors                                                       */
/* ------------------------------------------------------------------ */

static uint32_t hash_string(const char *key, int length)
{
    uint32_t hash = 2166136261u;
    for (int i = 0; i < length; i++) {
        hash ^= (uint8_t)key[i];
        hash *= 16777619;
    }
    return hash;
}

ObjString *string_take(char *chars, int length)
{
    ObjString *string = ALLOCATE_OBJ(ObjString, OBJ_STRING);
    string->length = length;
    string->chars = chars;
    string->hash = hash_string(chars, length);
    return string;
}

ObjString *string_copy(const char *chars, int length)
{
    char *heap = (char *)reallocate(NULL, 0, (size_t)length + 1);
    memcpy(heap, chars, (size_t)length);
    heap[length] = '\0';
    return string_take(heap, length);
}

/* Every constructor below allocates twice: once for the object header, once
 * for its payload array. The second allocation can trigger a collection, and
 * at that moment the header is reachable from nothing — so it is pinned as a
 * temporary root across the gap.
 *
 * The count fields stay at zero until their array exists, because the
 * collector walks `count` entries of a pointer it would otherwise read before
 * it was set. */

ObjCon *con_new(int tag, int count)
{
    ObjCon *con = ALLOCATE_OBJ(ObjCon, OBJ_CON);
    con->tag = tag;
    con->count = 0;
    con->fields = NULL;

    if (count > 0) {
        push_temp_root(OBJ_VAL(con));
        con->fields = (Value *)reallocate(NULL, 0, sizeof(Value) * (size_t)count);
        pop_temp_root(1);
        for (int i = 0; i < count; i++) con->fields[i] = UNIT_VAL;
        con->count = count;
    }
    return con;
}

ObjTuple *tuple_new(int count)
{
    ObjTuple *tuple = ALLOCATE_OBJ(ObjTuple, OBJ_TUPLE);
    tuple->count = 0;
    tuple->items = NULL;

    if (count > 0) {
        push_temp_root(OBJ_VAL(tuple));
        tuple->items = (Value *)reallocate(NULL, 0, sizeof(Value) * (size_t)count);
        pop_temp_root(1);
        for (int i = 0; i < count; i++) tuple->items[i] = UNIT_VAL;
        tuple->count = count;
    }
    return tuple;
}

ObjRecord *record_new(int count)
{
    ObjRecord *record = ALLOCATE_OBJ(ObjRecord, OBJ_RECORD);
    record->count = 0;
    record->labels = NULL;
    record->values = NULL;

    if (count > 0) {
        push_temp_root(OBJ_VAL(record));
        record->labels = (ObjString **)reallocate(
            NULL, 0, sizeof(ObjString *) * (size_t)count);
        for (int i = 0; i < count; i++) record->labels[i] = NULL;
        record->values = (Value *)reallocate(
            NULL, 0, sizeof(Value) * (size_t)count);
        pop_temp_root(1);
        for (int i = 0; i < count; i++) record->values[i] = UNIT_VAL;
        record->count = count;
    }
    return record;
}

ObjClosure *closure_new(Function *fn)
{
    ObjClosure *closure = ALLOCATE_OBJ(ObjClosure, OBJ_CLOSURE);
    closure->fn = fn;
    closure->upvalue_count = 0;
    closure->upvalues = NULL;
    closure->applied = NULL;
    closure->applied_count = 0;

    if (fn->upvalue_count > 0) {
        push_temp_root(OBJ_VAL(closure));
        closure->upvalues = (ObjUpvalue **)reallocate(
            NULL, 0, sizeof(ObjUpvalue *) * (size_t)fn->upvalue_count);
        pop_temp_root(1);
        for (int i = 0; i < fn->upvalue_count; i++) closure->upvalues[i] = NULL;
        closure->upvalue_count = fn->upvalue_count;
    }
    return closure;
}

ObjNative *native_new(int native_id)
{
    ObjNative *native = ALLOCATE_OBJ(ObjNative, OBJ_NATIVE);
    native->native_id = native_id;
    native->applied = NULL;
    native->applied_count = 0;
    return native;
}

ObjUpvalue *upvalue_new(Value *slot)
{
    ObjUpvalue *upvalue = ALLOCATE_OBJ(ObjUpvalue, OBJ_UPVALUE);
    upvalue->location = slot;
    upvalue->closed = UNIT_VAL;
    upvalue->next = NULL;
    return upvalue;
}

/* ------------------------------------------------------------------ */
/* Arithmetic                                                         */
/* ------------------------------------------------------------------ */

/* Signed overflow is undefined in C, so wrap through the unsigned domain.
 * The spec says ints are two's complement and wrap. */

int64_t wrap_add(int64_t a, int64_t b)
{
    return (int64_t)((uint64_t)a + (uint64_t)b);
}

int64_t wrap_sub(int64_t a, int64_t b)
{
    return (int64_t)((uint64_t)a - (uint64_t)b);
}

int64_t wrap_mul(int64_t a, int64_t b)
{
    return (int64_t)((uint64_t)a * (uint64_t)b);
}

/* ------------------------------------------------------------------ */
/* Equality and ordering                                              */
/* ------------------------------------------------------------------ */

bool values_equal(Value a, Value b)
{
    if (a.type != b.type) return false;

    switch (a.type) {
    case VAL_INT:   return AS_INT(a) == AS_INT(b);
    case VAL_FLOAT: return AS_FLOAT(a) == AS_FLOAT(b);
    case VAL_BOOL:  return AS_BOOL(a) == AS_BOOL(b);
    case VAL_UNIT:  return true;
    case VAL_OBJ:   break;
    }

    Obj *ao = AS_OBJ(a);
    Obj *bo = AS_OBJ(b);
    if (ao->type != bo->type) return false;

    switch (ao->type) {
    case OBJ_STRING: {
        ObjString *x = (ObjString *)ao;
        ObjString *y = (ObjString *)bo;
        return x->length == y->length &&
               memcmp(x->chars, y->chars, (size_t)x->length) == 0;
    }
    case OBJ_CON: {
        ObjCon *x = (ObjCon *)ao;
        ObjCon *y = (ObjCon *)bo;
        if (x->tag != y->tag || x->count != y->count) return false;
        for (int i = 0; i < x->count; i++) {
            if (!values_equal(x->fields[i], y->fields[i])) return false;
        }
        return true;
    }
    case OBJ_TUPLE: {
        ObjTuple *x = (ObjTuple *)ao;
        ObjTuple *y = (ObjTuple *)bo;
        if (x->count != y->count) return false;
        for (int i = 0; i < x->count; i++) {
            if (!values_equal(x->items[i], y->items[i])) return false;
        }
        return true;
    }
    case OBJ_RECORD: {
        ObjRecord *x = (ObjRecord *)ao;
        ObjRecord *y = (ObjRecord *)bo;
        if (x->count != y->count) return false;
        for (int i = 0; i < x->count; i++) {
            if (x->labels[i]->length != y->labels[i]->length ||
                memcmp(x->labels[i]->chars, y->labels[i]->chars,
                       (size_t)x->labels[i]->length) != 0) {
                return false;
            }
            if (!values_equal(x->values[i], y->values[i])) return false;
        }
        return true;
    }
    default:
        return ao == bo;
    }
}

static int cmp_i64(int64_t a, int64_t b) { return a < b ? -1 : (a > b ? 1 : 0); }
static int cmp_dbl(double a, double b)   { return a < b ? -1 : (a > b ? 1 : 0); }

int values_compare(Value a, Value b, bool *ok)
{
    *ok = true;

    if (a.type == VAL_INT && b.type == VAL_INT)
        return cmp_i64(AS_INT(a), AS_INT(b));
    if (a.type == VAL_FLOAT && b.type == VAL_FLOAT)
        return cmp_dbl(AS_FLOAT(a), AS_FLOAT(b));
    if (a.type == VAL_BOOL && b.type == VAL_BOOL)
        return cmp_i64(AS_BOOL(a) ? 1 : 0, AS_BOOL(b) ? 1 : 0);
    if (a.type == VAL_UNIT && b.type == VAL_UNIT)
        return 0;

    if (a.type != VAL_OBJ || b.type != VAL_OBJ) { *ok = false; return 0; }

    Obj *ao = AS_OBJ(a);
    Obj *bo = AS_OBJ(b);
    if (ao->type != bo->type) { *ok = false; return 0; }

    switch (ao->type) {
    case OBJ_STRING: {
        ObjString *x = (ObjString *)ao;
        ObjString *y = (ObjString *)bo;
        int shorter = x->length < y->length ? x->length : y->length;
        int order = memcmp(x->chars, y->chars, (size_t)shorter);
        if (order != 0) return order < 0 ? -1 : 1;
        return cmp_i64(x->length, y->length);
    }
    case OBJ_CON: {
        ObjCon *x = (ObjCon *)ao;
        ObjCon *y = (ObjCon *)bo;
        if (x->tag != y->tag) return cmp_i64(x->tag, y->tag);
        int shorter = x->count < y->count ? x->count : y->count;
        for (int i = 0; i < shorter; i++) {
            int order = values_compare(x->fields[i], y->fields[i], ok);
            if (!*ok || order != 0) return order;
        }
        return cmp_i64(x->count, y->count);
    }
    case OBJ_TUPLE: {
        ObjTuple *x = (ObjTuple *)ao;
        ObjTuple *y = (ObjTuple *)bo;
        int shorter = x->count < y->count ? x->count : y->count;
        for (int i = 0; i < shorter; i++) {
            int order = values_compare(x->items[i], y->items[i], ok);
            if (!*ok || order != 0) return order;
        }
        return cmp_i64(x->count, y->count);
    }
    case OBJ_RECORD: {
        /* Labels are stored sorted, so a positional walk is the same as the
         * sorted-key walk the reference interpreter does. */
        ObjRecord *x = (ObjRecord *)ao;
        ObjRecord *y = (ObjRecord *)bo;
        int shorter = x->count < y->count ? x->count : y->count;
        for (int i = 0; i < shorter; i++) {
            ObjString *lx = x->labels[i];
            ObjString *ly = y->labels[i];
            int shorter_label = lx->length < ly->length ? lx->length : ly->length;
            int order = memcmp(lx->chars, ly->chars, (size_t)shorter_label);
            if (order != 0) return order < 0 ? -1 : 1;
            if (lx->length != ly->length)
                return lx->length < ly->length ? -1 : 1;
            order = values_compare(x->values[i], y->values[i], ok);
            if (!*ok || order != 0) return order;
        }
        return cmp_i64(x->count, y->count);
    }
    default:
        *ok = false;
        return 0;
    }
}

/* ------------------------------------------------------------------ */
/* Float formatting                                                   */
/* ------------------------------------------------------------------ */

/* Reproduce Python's `repr` for floats exactly.
 *
 * Two separate decisions, and conflating them is the trap: first find the
 * *shortest digit string* that round-trips, then choose a presentation. Using
 * `%g` for both gets the second wrong, because `%g` switches to exponent form
 * as soon as the exponent reaches the precision — so 2500.0 comes out as
 * `2.5e+03` where Python says `2500.0`.
 *
 * Python's rule, from CPython's `format_float_short`: with `decpt` the
 * position of the decimal point, use exponent form when `decpt <= -4` or
 * `decpt > 16`, and fixed form otherwise.
 */
void format_double(double value, char *out, size_t size)
{
    if (isnan(value)) { snprintf(out, size, "NaN"); return; }
    if (isinf(value)) {
        snprintf(out, size, value > 0 ? "Infinity" : "-Infinity");
        return;
    }
    if (value == 0.0) {
        snprintf(out, size, signbit(value) ? "-0.0" : "0.0");
        return;
    }

    /* The fewest digits after the point in `%e` form that still round-trips;
     * `digits + 1` is then the number of significant digits. */
    char scientific[64];
    int digits = 0;
    for (; digits <= 17; digits++) {
        snprintf(scientific, sizeof scientific, "%.*e", digits, value);
        if (strtod(scientific, NULL) == value) break;
    }

    const char *marker = strchr(scientific, 'e');
    int exponent = marker != NULL ? atoi(marker + 1) : 0;
    int decpt = exponent + 1;

    if (decpt > -4 && decpt <= 16) {
        int places = digits - exponent;
        if (places < 0) places = 0;
        snprintf(out, size, "%.*f", places, value);
        /* Always show a float as a float. */
        if (strchr(out, '.') == NULL) {
            size_t length = strlen(out);
            if (length + 3 <= size) {
                out[length] = '.';
                out[length + 1] = '0';
                out[length + 2] = '\0';
            }
        }
        return;
    }

    /* Exponent form. `%e` always pads the exponent to at least two digits,
     * which is what Python does too, so the mantissa is the only fix-up: a
     * minimal digit count never leaves trailing zeros, but `%.0e` writes
     * `1e+16` where the fixed branch would have written `1.0`. */
    snprintf(out, size, "%s", scientific);
}

/* ------------------------------------------------------------------ */
/* Rendering                                                          */
/* ------------------------------------------------------------------ */

typedef struct {
    char  *data;
    size_t length;
    size_t capacity;
} Buffer;

static void buffer_init(Buffer *b)
{
    b->data = NULL;
    b->length = 0;
    b->capacity = 0;
}

static void buffer_reserve(Buffer *b, size_t extra)
{
    if (b->length + extra + 1 <= b->capacity) return;
    size_t capacity = b->capacity < 32 ? 32 : b->capacity;
    while (capacity < b->length + extra + 1) capacity *= 2;
    /* Plain realloc: this memory is not GC-managed and must not trigger a
     * collection while a half-built string is unreachable. */
    b->data = (char *)realloc(b->data, capacity);
    if (b->data == NULL) {
        fprintf(stderr, "vela: out of memory\n");
        exit(70);
    }
    b->capacity = capacity;
}

static void buffer_append(Buffer *b, const char *text, size_t length)
{
    buffer_reserve(b, length);
    memcpy(b->data + b->length, text, length);
    b->length += length;
    b->data[b->length] = '\0';
}

static void buffer_puts(Buffer *b, const char *text)
{
    buffer_append(b, text, strlen(text));
}

static void write_value(Buffer *b, Value value, bool quote_strings);

static bool is_con_named(Value value, const char *name, const Image *image)
{
    if (!IS_CON(value)) return false;
    int tag = AS_CON(value)->tag;
    if (tag < 0 || tag >= image->constructor_count) return false;
    return strcmp(image->constructors[tag].name, name) == 0;
}

static void write_string_escaped(Buffer *b, ObjString *string)
{
    buffer_puts(b, "\"");
    for (int i = 0; i < string->length; i++) {
        char c = string->chars[i];
        switch (c) {
        case '\\': buffer_puts(b, "\\\\"); break;
        case '"':  buffer_puts(b, "\\\""); break;
        case '\n': buffer_puts(b, "\\n");  break;
        case '\r': buffer_puts(b, "\\r");  break;
        case '\t': buffer_puts(b, "\\t");  break;
        case '\0': buffer_puts(b, "\\0");  break;
        default:   buffer_append(b, &c, 1); break;
        }
    }
    buffer_puts(b, "\"");
}

/* A list renders as `[a, b, c]` rather than nested `Cons`. */
static bool try_write_list(Buffer *b, Value value)
{
    if (!is_con_named(value, "Nil", vm.image) &&
        !is_con_named(value, "Cons", vm.image)) {
        return false;
    }

    Buffer inner;
    buffer_init(&inner);
    buffer_puts(&inner, "[");

    Value current = value;
    bool first = true;
    while (true) {
        if (is_con_named(current, "Nil", vm.image)) break;
        if (!is_con_named(current, "Cons", vm.image) ||
            AS_CON(current)->count != 2) {
            free(inner.data);
            return false;
        }
        if (!first) buffer_puts(&inner, ", ");
        first = false;
        write_value(&inner, AS_CON(current)->fields[0], true);
        current = AS_CON(current)->fields[1];
    }

    buffer_puts(&inner, "]");
    buffer_append(b, inner.data, inner.length);
    free(inner.data);
    return true;
}

/* Constructor arguments are parenthesised when they would otherwise be
 * ambiguous, matching `_show_atom` in the reference interpreter. */
static void write_atom(Buffer *b, Value value)
{
    bool parenthesise = false;

    if (IS_CON(value) && AS_CON(value)->count > 0 &&
        !is_con_named(value, "Cons", vm.image) &&
        !is_con_named(value, "Nil", vm.image)) {
        parenthesise = true;
    }
    if (IS_INT(value) && AS_INT(value) < 0) parenthesise = true;
    if (IS_FLOAT(value) && (AS_FLOAT(value) < 0 || signbit(AS_FLOAT(value)))) {
        parenthesise = true;
    }

    if (parenthesise) buffer_puts(b, "(");
    write_value(b, value, true);
    if (parenthesise) buffer_puts(b, ")");
}

static void write_value(Buffer *b, Value value, bool quote_strings)
{
    char scratch[64];

    switch (value.type) {
    case VAL_INT:
        snprintf(scratch, sizeof scratch, "%lld", (long long)AS_INT(value));
        buffer_puts(b, scratch);
        return;
    case VAL_FLOAT:
        format_double(AS_FLOAT(value), scratch, sizeof scratch);
        buffer_puts(b, scratch);
        return;
    case VAL_BOOL:
        buffer_puts(b, AS_BOOL(value) ? "True" : "False");
        return;
    case VAL_UNIT:
        buffer_puts(b, "()");
        return;
    case VAL_OBJ:
        break;
    }

    switch (OBJ_TYPE(value)) {
    case OBJ_STRING: {
        ObjString *string = AS_STRING(value);
        if (quote_strings) write_string_escaped(b, string);
        else buffer_append(b, string->chars, (size_t)string->length);
        return;
    }
    case OBJ_CON: {
        if (try_write_list(b, value)) return;
        ObjCon *con = AS_CON(value);
        const char *name = (con->tag >= 0 && con->tag < vm.image->constructor_count)
            ? vm.image->constructors[con->tag].name
            : "<con>";
        buffer_puts(b, name);
        for (int i = 0; i < con->count; i++) {
            buffer_puts(b, " ");
            write_atom(b, con->fields[i]);
        }
        return;
    }
    case OBJ_TUPLE: {
        ObjTuple *tuple = AS_TUPLE(value);
        buffer_puts(b, "(");
        for (int i = 0; i < tuple->count; i++) {
            if (i > 0) buffer_puts(b, ", ");
            write_value(b, tuple->items[i], true);
        }
        buffer_puts(b, ")");
        return;
    }
    case OBJ_RECORD: {
        ObjRecord *record = AS_RECORD(value);
        if (record->count == 0) { buffer_puts(b, "{}"); return; }
        buffer_puts(b, "{ ");
        for (int i = 0; i < record->count; i++) {
            if (i > 0) buffer_puts(b, ", ");
            buffer_append(b, record->labels[i]->chars,
                          (size_t)record->labels[i]->length);
            buffer_puts(b, " = ");
            write_value(b, record->values[i], true);
        }
        buffer_puts(b, " }");
        return;
    }
    case OBJ_CLOSURE: {
        ObjClosure *closure = AS_CLOSURE(value);
        buffer_puts(b, "<fn ");
        buffer_puts(b, closure->fn->name ? closure->fn->name : "?");
        buffer_puts(b, ">");
        return;
    }
    case OBJ_NATIVE: {
        ObjNative *native = AS_NATIVE(value);
        buffer_puts(b, "<native ");
        buffer_puts(b, native->native_id < VELA_NATIVE_COUNT
                        ? VELA_NATIVE_NAMES[native->native_id] : "?");
        buffer_puts(b, ">");
        return;
    }
    default:
        buffer_puts(b, "<object>");
        return;
    }
}

char *value_to_show(Value value)
{
    Buffer b;
    buffer_init(&b);
    write_value(&b, value, true);
    if (b.data == NULL) { b.data = strdup(""); }
    return b.data;
}

char *value_to_text(Value value)
{
    Buffer b;
    buffer_init(&b);
    write_value(&b, value, false);
    if (b.data == NULL) { b.data = strdup(""); }
    return b.data;
}

/* ------------------------------------------------------------------ */
/* Garbage collection                                                 */
/* ------------------------------------------------------------------ */

typedef struct {
    Obj  **items;
    int    count;
    int    capacity;
} GrayStack;

static GrayStack gray;

static void gray_push(Obj *object)
{
    if (gray.count + 1 > gray.capacity) {
        gray.capacity = gray.capacity < 16 ? 16 : gray.capacity * 2;
        gray.items = (Obj **)realloc(gray.items, sizeof(Obj *) * (size_t)gray.capacity);
        if (gray.items == NULL) {
            fprintf(stderr, "vela: out of memory during gc\n");
            exit(70);
        }
    }
    gray.items[gray.count++] = object;
}

void mark_object(Obj *object)
{
    if (object == NULL || object->marked) return;
    object->marked = true;
    gray_push(object);
}

void mark_value(Value value)
{
    if (IS_OBJ(value)) mark_object(AS_OBJ(value));
}

static void blacken_object(Obj *object)
{
    switch (object->type) {
    case OBJ_STRING:
        break;
    case OBJ_CON: {
        ObjCon *con = (ObjCon *)object;
        for (int i = 0; i < con->count; i++) mark_value(con->fields[i]);
        break;
    }
    case OBJ_TUPLE: {
        ObjTuple *tuple = (ObjTuple *)object;
        for (int i = 0; i < tuple->count; i++) mark_value(tuple->items[i]);
        break;
    }
    case OBJ_RECORD: {
        ObjRecord *record = (ObjRecord *)object;
        for (int i = 0; i < record->count; i++) {
            mark_object((Obj *)record->labels[i]);
            mark_value(record->values[i]);
        }
        break;
    }
    case OBJ_CLOSURE: {
        ObjClosure *closure = (ObjClosure *)object;
        for (int i = 0; i < closure->upvalue_count; i++) {
            mark_object((Obj *)closure->upvalues[i]);
        }
        for (int i = 0; i < closure->applied_count; i++) {
            mark_value(closure->applied[i]);
        }
        break;
    }
    case OBJ_UPVALUE:
        mark_value(((ObjUpvalue *)object)->closed);
        break;
    case OBJ_NATIVE: {
        ObjNative *native = (ObjNative *)object;
        for (int i = 0; i < native->applied_count; i++) {
            mark_value(native->applied[i]);
        }
        break;
    }
    }
}

static void free_object(Obj *object)
{
    switch (object->type) {
    case OBJ_STRING: {
        ObjString *string = (ObjString *)object;
        reallocate(string->chars, (size_t)string->length + 1, 0);
        reallocate(object, sizeof(ObjString), 0);
        break;
    }
    case OBJ_CON: {
        ObjCon *con = (ObjCon *)object;
        reallocate(con->fields, sizeof(Value) * (size_t)con->count, 0);
        reallocate(object, sizeof(ObjCon), 0);
        break;
    }
    case OBJ_TUPLE: {
        ObjTuple *tuple = (ObjTuple *)object;
        reallocate(tuple->items, sizeof(Value) * (size_t)tuple->count, 0);
        reallocate(object, sizeof(ObjTuple), 0);
        break;
    }
    case OBJ_RECORD: {
        ObjRecord *record = (ObjRecord *)object;
        reallocate(record->labels, sizeof(ObjString *) * (size_t)record->count, 0);
        reallocate(record->values, sizeof(Value) * (size_t)record->count, 0);
        reallocate(object, sizeof(ObjRecord), 0);
        break;
    }
    case OBJ_CLOSURE: {
        ObjClosure *closure = (ObjClosure *)object;
        reallocate(closure->upvalues,
                   sizeof(ObjUpvalue *) * (size_t)closure->upvalue_count, 0);
        reallocate(closure->applied,
                   sizeof(Value) * (size_t)closure->applied_count, 0);
        reallocate(object, sizeof(ObjClosure), 0);
        break;
    }
    case OBJ_UPVALUE:
        reallocate(object, sizeof(ObjUpvalue), 0);
        break;
    case OBJ_NATIVE: {
        ObjNative *native = (ObjNative *)object;
        reallocate(native->applied,
                   sizeof(Value) * (size_t)native->applied_count, 0);
        reallocate(object, sizeof(ObjNative), 0);
        break;
    }
    }
}

static void mark_roots(void)
{
    for (Value *slot = vm.stack; slot < vm.stack_top; slot++) {
        mark_value(*slot);
    }
    for (int i = 0; i < vm.frame_count; i++) {
        mark_object((Obj *)vm.frames[i].closure);
    }
    for (ObjUpvalue *up = vm.open_upvalues; up != NULL; up = up->next) {
        mark_object((Obj *)up);
    }
    for (int i = 0; i < vm.global_count; i++) {
        mark_value(vm.globals[i]);
    }
    for (int i = 0; i < vm.temp_root_count; i++) {
        mark_value(vm.temp_roots[i]);
    }
    if (vm.image != NULL) {
        for (int i = 0; i < vm.image->constant_count; i++) {
            mark_value(vm.image->constants[i]);
        }
        for (int i = 0; i < vm.image->label_set_count; i++) {
            LabelSet *set = &vm.image->label_sets[i];
            for (int j = 0; j < set->count; j++) {
                mark_object((Obj *)set->labels[j]);
            }
        }
    }
}

void collect_garbage(void)
{
    mark_roots();

    while (gray.count > 0) {
        blacken_object(gray.items[--gray.count]);
    }

    Obj **link = &vm.objects;
    while (*link != NULL) {
        Obj *object = *link;
        if (object->marked) {
            object->marked = false;
            link = &object->next;
        } else {
            *link = object->next;
            free_object(object);
        }
    }

    vm.next_gc = vm.bytes_allocated * GC_HEAP_GROW_FACTOR;
    if (vm.next_gc < 1024 * 1024) vm.next_gc = 1024 * 1024;
}

void free_all_objects(void)
{
    Obj *object = vm.objects;
    while (object != NULL) {
        Obj *next = object->next;
        free_object(object);
        object = next;
    }
    vm.objects = NULL;
    free(gray.items);
    gray.items = NULL;
    gray.count = 0;
    gray.capacity = 0;
}
