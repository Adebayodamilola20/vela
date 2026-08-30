/* Loading a `.velac` image.
 *
 * The format is written by `velac/emit.py`; that docstring is the spec. Every
 * read here is bounds-checked against the file length, because a truncated or
 * hand-edited image must produce a diagnostic rather than a segfault.
 */

#include <stdlib.h>
#include <string.h>

#include "vela.h"

typedef struct {
    const uint8_t *data;
    size_t         length;
    size_t         offset;
    bool           failed;
} Reader;

static bool need(Reader *r, size_t bytes)
{
    if (r->failed) return false;
    if (r->offset + bytes > r->length) {
        r->failed = true;
        return false;
    }
    return true;
}

static uint8_t read_u8(Reader *r)
{
    if (!need(r, 1)) return 0;
    return r->data[r->offset++];
}

static uint16_t read_u16(Reader *r)
{
    if (!need(r, 2)) return 0;
    uint16_t value = (uint16_t)(r->data[r->offset] |
                                ((uint16_t)r->data[r->offset + 1] << 8));
    r->offset += 2;
    return value;
}

static uint32_t read_u32(Reader *r)
{
    if (!need(r, 4)) return 0;
    uint32_t value = (uint32_t)r->data[r->offset]
                   | ((uint32_t)r->data[r->offset + 1] << 8)
                   | ((uint32_t)r->data[r->offset + 2] << 16)
                   | ((uint32_t)r->data[r->offset + 3] << 24);
    r->offset += 4;
    return value;
}

static int64_t read_i64(Reader *r)
{
    if (!need(r, 8)) return 0;
    uint64_t value = 0;
    for (int i = 0; i < 8; i++) {
        value |= (uint64_t)r->data[r->offset + i] << (8 * i);
    }
    r->offset += 8;
    return (int64_t)value;
}

static double read_f64(Reader *r)
{
    uint64_t bits = 0;
    if (!need(r, 8)) return 0.0;
    for (int i = 0; i < 8; i++) {
        bits |= (uint64_t)r->data[r->offset + i] << (8 * i);
    }
    r->offset += 8;
    double out;
    memcpy(&out, &bits, sizeof out);
    return out;
}

/* Returns a malloc'd, NUL-terminated copy; `length_out` may be NULL. */
static char *read_string(Reader *r, int *length_out)
{
    uint32_t length = read_u32(r);
    if (!need(r, length)) return NULL;
    char *out = (char *)malloc((size_t)length + 1);
    if (out == NULL) return NULL;
    memcpy(out, r->data + r->offset, length);
    out[length] = '\0';
    r->offset += length;
    if (length_out != NULL) *length_out = (int)length;
    return out;
}

Image *image_load(const char *path, char **error)
{
    *error = NULL;

    FILE *file = fopen(path, "rb");
    if (file == NULL) {
        *error = strdup("cannot open image");
        return NULL;
    }

    fseek(file, 0, SEEK_END);
    long size = ftell(file);
    fseek(file, 0, SEEK_SET);
    if (size < 0) {
        fclose(file);
        *error = strdup("cannot measure image");
        return NULL;
    }

    uint8_t *buffer = (uint8_t *)malloc((size_t)size);
    if (buffer == NULL) {
        fclose(file);
        *error = strdup("out of memory reading image");
        return NULL;
    }
    if (fread(buffer, 1, (size_t)size, file) != (size_t)size) {
        free(buffer);
        fclose(file);
        *error = strdup("short read on image");
        return NULL;
    }
    fclose(file);

    Reader r = {buffer, (size_t)size, 0, false};

    if (!need(&r, 4) || memcmp(r.data, VELA_MAGIC, 4) != 0) {
        free(buffer);
        *error = strdup("not a Vela image (bad magic)");
        return NULL;
    }
    r.offset += 4;

    uint16_t version = read_u16(&r);
    read_u16(&r);  /* flags, reserved */
    if (version != VELA_VERSION) {
        free(buffer);
        char *message = (char *)malloc(96);
        snprintf(message, 96, "image version %u, but this VM speaks %d",
                 version, VELA_VERSION);
        *error = message;
        return NULL;
    }

    Image *image = (Image *)calloc(1, sizeof(Image));
    if (image == NULL) {
        free(buffer);
        *error = strdup("out of memory");
        return NULL;
    }

    /* Make the image a collector root before allocating anything into it.
     * Loading builds ObjStrings, and any of those allocations can trigger a
     * collection; until the image is reachable from `mark_roots` the strings
     * already read are reachable from nothing at all and get swept, leaving
     * the constant table pointing at freed memory.
     *
     * Every count is zero here, so marking walks nothing, and each array is
     * allocated before its count is published — the same discipline the
     * object constructors in `value.c` follow, for the same reason. */
    vm.image = image;

    /* -- constants -------------------------------------------------- */
    int constant_count = (int)read_u32(&r);
    image->constants = (Value *)calloc((size_t)constant_count + 1,
                                       sizeof(Value));
    if (image->constants == NULL) goto out_of_memory;
    image->constant_count = constant_count;
    for (int i = 0; i < image->constant_count && !r.failed; i++) {
        uint8_t tag = read_u8(&r);
        switch (tag) {
        case 0: image->constants[i] = INT_VAL(read_i64(&r)); break;
        case 1: image->constants[i] = FLOAT_VAL(read_f64(&r)); break;
        case 2: {
            int length = 0;
            char *text = read_string(&r, &length);
            if (text == NULL) { r.failed = true; break; }
            image->constants[i] = OBJ_VAL(string_take(text, length));
            break;
        }
        case 3: image->constants[i] = BOOL_VAL(read_u8(&r) != 0); break;
        case 4: image->constants[i] = UNIT_VAL; break;
        default:
            r.failed = true;
            break;
        }
    }

    /* -- record label sets ------------------------------------------ */
    int label_set_count = (int)read_u32(&r);
    image->label_sets = (LabelSet *)calloc((size_t)label_set_count + 1,
                                           sizeof(LabelSet));
    if (image->label_sets == NULL) goto out_of_memory;
    image->label_set_count = label_set_count;
    for (int i = 0; i < image->label_set_count && !r.failed; i++) {
        int count = (int)read_u16(&r);
        image->label_sets[i].labels =
            (ObjString **)calloc((size_t)count + 1, sizeof(ObjString *));
        if (image->label_sets[i].labels == NULL) goto out_of_memory;
        image->label_sets[i].count = count;
        for (int j = 0; j < count && !r.failed; j++) {
            int length = 0;
            char *text = read_string(&r, &length);
            if (text == NULL) { r.failed = true; break; }
            image->label_sets[i].labels[j] = string_take(text, length);
        }
    }

    /* -- constructors ------------------------------------------------ */
    int constructor_count = (int)read_u32(&r);
    image->constructors = (ConstructorInfo *)calloc(
        (size_t)constructor_count + 1, sizeof(ConstructorInfo));
    if (image->constructors == NULL) goto out_of_memory;
    image->constructor_count = constructor_count;
    for (int i = 0; i < image->constructor_count && !r.failed; i++) {
        image->constructors[i].arity = (int)read_u16(&r);
        image->constructors[i].name = read_string(&r, NULL);
        image->constructors[i].type_name = read_string(&r, NULL);
        if (image->constructors[i].name == NULL) r.failed = true;
    }

    /* -- globals ----------------------------------------------------- */
    int global_count = (int)read_u32(&r);
    image->global_names = (char **)calloc((size_t)global_count + 1,
                                          sizeof(char *));
    if (image->global_names == NULL) goto out_of_memory;
    image->global_count = global_count;
    for (int i = 0; i < image->global_count && !r.failed; i++) {
        image->global_names[i] = read_string(&r, NULL);
        if (image->global_names[i] == NULL) r.failed = true;
    }

    /* -- functions --------------------------------------------------- */
    int function_count = (int)read_u32(&r);
    image->functions = (Function *)calloc((size_t)function_count + 1,
                                          sizeof(Function));
    if (image->functions == NULL) goto out_of_memory;
    image->function_count = function_count;
    for (int i = 0; i < image->function_count && !r.failed; i++) {
        Function *fn = &image->functions[i];
        fn->name = read_string(&r, NULL);
        fn->arity = (int)read_u16(&r);
        fn->max_slots = (int)read_u16(&r);
        fn->upvalue_count = (int)read_u16(&r);

        if (fn->upvalue_count > 0) {
            fn->upvalue_is_local =
                (uint8_t *)calloc((size_t)fn->upvalue_count, sizeof(uint8_t));
            fn->upvalue_index =
                (uint16_t *)calloc((size_t)fn->upvalue_count, sizeof(uint16_t));
            for (int j = 0; j < fn->upvalue_count && !r.failed; j++) {
                fn->upvalue_is_local[j] = read_u8(&r);
                fn->upvalue_index[j] = read_u16(&r);
            }
        }

        fn->code_length = read_u32(&r);
        if (!need(&r, fn->code_length)) { r.failed = true; break; }
        fn->code = (uint8_t *)malloc(fn->code_length + 1);
        if (fn->code == NULL) { r.failed = true; break; }
        memcpy(fn->code, r.data + r.offset, fn->code_length);
        fn->code[fn->code_length] = (uint8_t)OP_HALT;  /* a stop backstop */
        r.offset += fn->code_length;
    }

    image->entry = (int)read_u32(&r);

    free(buffer);

    if (r.failed) {
        vm.image = NULL;
        image_free(image);
        *error = strdup("image is truncated or malformed");
        return NULL;
    }
    if (image->entry < 0 || image->entry >= image->function_count) {
        vm.image = NULL;
        image_free(image);
        *error = strdup("image entry point is out of range");
        return NULL;
    }

    return image;

out_of_memory:
    free(buffer);
    vm.image = NULL;
    image_free(image);
    *error = strdup("out of memory reading image");
    return NULL;
}

void image_free(Image *image)
{
    if (image == NULL) return;

    free(image->constants);

    for (int i = 0; i < image->label_set_count; i++) {
        free(image->label_sets[i].labels);
    }
    free(image->label_sets);

    for (int i = 0; i < image->constructor_count; i++) {
        free(image->constructors[i].name);
        free(image->constructors[i].type_name);
    }
    free(image->constructors);

    for (int i = 0; i < image->global_count; i++) {
        free(image->global_names[i]);
    }
    free(image->global_names);

    for (int i = 0; i < image->function_count; i++) {
        free(image->functions[i].name);
        free(image->functions[i].code);
        free(image->functions[i].upvalue_is_local);
        free(image->functions[i].upvalue_index);
    }
    free(image->functions);

    free(image);
}
