/* The bytecode interpreter.
 *
 * A stack machine with call frames and flat closures. Two things are worth
 * knowing before reading the loop:
 *
 * **Tail calls reuse the frame.** `OP_TAIL_CALL` overwrites the current
 * frame's slots with the new callee and arguments instead of pushing, so a
 * tail-recursive Vela function runs in constant space — the guarantee the
 * spec makes and the reference interpreter also honours.
 *
 * **Upvalues are open until the frame dies.** A closure captures a *pointer*
 * into the stack, so a `let rec` group whose slots are filled in after the
 * closures are built still sees the finished functions. On return, anything
 * still pointing into the dying frame is closed by copying the value into the
 * upvalue itself.
 */

#include <math.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

#include "vela.h"

VM vm;

void free_all_objects(void);

/* ------------------------------------------------------------------ */
/* Stack                                                              */
/* ------------------------------------------------------------------ */

void push(Value value)
{
    if ((size_t)(vm.stack_top - vm.stack) >= vm.stack_capacity) {
        runtime_error("stack overflow");
        return;
    }
    *vm.stack_top++ = value;
}

Value pop(void)
{
    if (vm.stack_top == vm.stack) return UNIT_VAL;
    return *(--vm.stack_top);
}

Value peek(int distance)
{
    return vm.stack_top[-1 - distance];
}

/* ------------------------------------------------------------------ */
/* Errors                                                             */
/* ------------------------------------------------------------------ */

void runtime_error(const char *format, ...)
{
    va_list args;
    va_start(args, format);
    fprintf(stderr, "runtime error: ");
    vfprintf(stderr, format, args);
    fprintf(stderr, "\n");
    va_end(args);

    /* A short trace: the innermost few frames are what a reader needs. */
    int shown = 0;
    for (int i = vm.frame_count - 1; i >= 0 && shown < 8; i--, shown++) {
        Function *fn = vm.frames[i].closure->fn;
        fprintf(stderr, "  in %s\n", fn->name ? fn->name : "<anonymous>");
    }

    vm.had_error = true;
}

/* ------------------------------------------------------------------ */
/* Lifecycle                                                          */
/* ------------------------------------------------------------------ */

void vm_init(void)
{
    memset(&vm, 0, sizeof vm);

    vm.stack_capacity = VELA_STACK_MAX;
    vm.stack = (Value *)malloc(sizeof(Value) * vm.stack_capacity);
    vm.stack_top = vm.stack;

    vm.frame_capacity = VELA_FRAMES_MAX;
    vm.frames = (CallFrame *)malloc(sizeof(CallFrame) * (size_t)vm.frame_capacity);
    vm.frame_count = 0;

    vm.next_gc = 1024 * 1024;

    if (vm.stack == NULL || vm.frames == NULL) {
        fprintf(stderr, "vela: out of memory\n");
        exit(70);
    }
}

void vm_free(void)
{
    free_all_objects();
    free(vm.stack);
    free(vm.frames);
    free(vm.globals);
    free(vm.temp_roots);
    memset(&vm, 0, sizeof vm);
}

/* ------------------------------------------------------------------ */
/* Upvalues                                                           */
/* ------------------------------------------------------------------ */

static ObjUpvalue *capture_upvalue(Value *local)
{
    /* The open list is kept sorted by stack address, descending, so the search
     * stops as soon as it passes the slot we want. */
    ObjUpvalue *previous = NULL;
    ObjUpvalue *current = vm.open_upvalues;
    while (current != NULL && current->location > local) {
        previous = current;
        current = current->next;
    }
    if (current != NULL && current->location == local) return current;

    ObjUpvalue *created = upvalue_new(local);
    created->next = current;
    if (previous == NULL) vm.open_upvalues = created;
    else previous->next = created;
    return created;
}

static void close_upvalues(Value *last)
{
    while (vm.open_upvalues != NULL && vm.open_upvalues->location >= last) {
        ObjUpvalue *upvalue = vm.open_upvalues;
        upvalue->closed = *upvalue->location;
        upvalue->location = &upvalue->closed;
        vm.open_upvalues = upvalue->next;
    }
}

/* ------------------------------------------------------------------ */
/* Calling                                                            */
/* ------------------------------------------------------------------ */

/* Push a frame for `closure`, whose arguments are already on the stack.
 * `already` counts arguments carried over from a partial application, which
 * live in `closure->applied` and are copied down first. */
static bool push_frame(ObjClosure *closure, int arg_count)
{
    Function *fn = closure->fn;
    int supplied = closure->applied_count + arg_count;

    if (supplied < fn->arity) {
        /* Under-applied: build a new closure remembering what we have.
         * `closure_new` already copied the upvalue array's shape; the values
         * are shared, since upvalues are themselves objects. */
        ObjClosure *partial = closure_new(fn);
        push_temp_root(OBJ_VAL(partial));

        for (int i = 0; i < closure->upvalue_count; i++) {
            partial->upvalues[i] = closure->upvalues[i];
        }

        /* Allocate before publishing the count, so a collection triggered by
         * this very allocation does not walk an array that is not there. */
        Value *slots = (Value *)reallocate(NULL, 0,
                                           sizeof(Value) * (size_t)supplied);
        for (int i = 0; i < closure->applied_count; i++) {
            slots[i] = closure->applied[i];
        }
        for (int i = 0; i < arg_count; i++) {
            slots[closure->applied_count + i] = vm.stack_top[-arg_count + i];
        }
        partial->applied = slots;
        partial->applied_count = supplied;

        pop_temp_root(1);

        vm.stack_top -= arg_count + 1;   /* arguments and the callee */
        push(OBJ_VAL(partial));
        return true;
    }

    if (vm.frame_count == vm.frame_capacity) {
        runtime_error("call depth exceeded");
        return false;
    }

    /* Lay the frame out as [callee][applied...][args...]. The callee slot is
     * overwritten by the first parameter, so slot 0 is parameter 0. */
    Value *base = vm.stack_top - arg_count - 1;

    if (closure->applied_count > 0) {
        /* Make room for the arguments already held by the closure. */
        memmove(base + 1 + closure->applied_count, base + 1,
                sizeof(Value) * (size_t)arg_count);
        for (int i = 0; i < closure->applied_count; i++) {
            base[1 + i] = closure->applied[i];
        }
        vm.stack_top += closure->applied_count;
    }

    memmove(base, base + 1, sizeof(Value) * (size_t)supplied);
    vm.stack_top = base + supplied;

    /* Reserve the locals the function may declare beyond its parameters. */
    for (int i = supplied; i < fn->max_slots; i++) {
        push(UNIT_VAL);
    }

    CallFrame *frame = &vm.frames[vm.frame_count++];
    frame->closure = closure;
    frame->ip = fn->code;
    frame->slots = base;
    return true;
}

/* Apply a native, which may be under- or over-applied like any function. */
static bool call_native_value(ObjNative *native, int arg_count)
{
    int id = native->native_id;
    int arity = VELA_NATIVE_ARITY[id];
    int supplied = native->applied_count + arg_count;

    if (supplied < arity) {
        ObjNative *partial = native_new(id);
        push_temp_root(OBJ_VAL(partial));

        Value *slots = (Value *)reallocate(NULL, 0,
                                           sizeof(Value) * (size_t)supplied);
        for (int i = 0; i < native->applied_count; i++) {
            slots[i] = native->applied[i];
        }
        for (int i = 0; i < arg_count; i++) {
            slots[native->applied_count + i] = vm.stack_top[-arg_count + i];
        }
        partial->applied = slots;
        partial->applied_count = supplied;

        pop_temp_root(1);

        vm.stack_top -= arg_count + 1;
        push(OBJ_VAL(partial));
        return true;
    }

    if (supplied > arity) {
        runtime_error("native %s applied to too many arguments",
                      VELA_NATIVE_NAMES[id]);
        return false;
    }

    Value args[8];
    for (int i = 0; i < native->applied_count; i++) args[i] = native->applied[i];
    for (int i = 0; i < arg_count; i++) {
        args[native->applied_count + i] = vm.stack_top[-arg_count + i];
    }

    Value result;
    if (!native_call(id, arity, args, &result)) return false;

    vm.stack_top -= arg_count + 1;
    push(result);
    return true;
}

static bool call_value(Value callee, int arg_count)
{
    if (IS_CLOSURE(callee)) return push_frame(AS_CLOSURE(callee), arg_count);
    if (IS_NATIVE(callee))  return call_native_value(AS_NATIVE(callee), arg_count);

    char *rendered = value_to_show(callee);
    runtime_error("%s is not a function", rendered);
    free(rendered);
    return false;
}

/* ------------------------------------------------------------------ */
/* The loop                                                           */
/* ------------------------------------------------------------------ */

#define READ_BYTE()   (*frame->ip++)
#define READ_U16()    (frame->ip += 2, \
                       (uint16_t)(frame->ip[-2] | (frame->ip[-1] << 8)))

#define BINARY_INT(op)                                                    \
    do {                                                                  \
        Value b = pop(); Value a = pop();                                 \
        if (!IS_INT(a) || !IS_INT(b)) {                                   \
            runtime_error("expected two Ints");                           \
            return RUN_RUNTIME_ERROR;                                     \
        }                                                                 \
        push(INT_VAL(op(AS_INT(a), AS_INT(b))));                          \
    } while (false)

#define BINARY_FLOAT(op)                                                  \
    do {                                                                  \
        Value b = pop(); Value a = pop();                                 \
        if (!IS_FLOAT(a) || !IS_FLOAT(b)) {                               \
            runtime_error("expected two Floats");                         \
            return RUN_RUNTIME_ERROR;                                     \
        }                                                                 \
        push(FLOAT_VAL(AS_FLOAT(a) op AS_FLOAT(b)));                      \
    } while (false)

#define COMPARISON(cmp)                                                   \
    do {                                                                  \
        Value b = pop(); Value a = pop();                                 \
        bool ok = true;                                                   \
        int order = values_compare(a, b, &ok);                            \
        if (!ok) {                                                        \
            runtime_error("cannot compare these values");                 \
            return RUN_RUNTIME_ERROR;                                     \
        }                                                                 \
        push(BOOL_VAL(order cmp 0));                                      \
    } while (false)

static RunResult run(void)
{
    CallFrame *frame = &vm.frames[vm.frame_count - 1];

    for (;;) {
        uint8_t instruction = READ_BYTE();

        switch (instruction) {

        case OP_NOP:
            break;

        case OP_CONST: {
            uint16_t index = READ_U16();
            push(vm.image->constants[index]);
            break;
        }

        case OP_TRUE:  push(BOOL_VAL(true));  break;
        case OP_FALSE: push(BOOL_VAL(false)); break;
        case OP_UNIT:  push(UNIT_VAL);        break;
        case OP_POP:   pop();                 break;
        case OP_DUP:   push(peek(0));         break;

        case OP_GET_LOCAL: {
            uint16_t slot = READ_U16();
            push(frame->slots[slot]);
            break;
        }

        case OP_SET_LOCAL: {
            uint16_t slot = READ_U16();
            frame->slots[slot] = pop();
            break;
        }

        case OP_GET_UPVAL: {
            uint16_t index = READ_U16();
            push(*frame->closure->upvalues[index]->location);
            break;
        }

        case OP_GET_GLOBAL: {
            uint16_t index = READ_U16();
            push(vm.globals[index]);
            break;
        }

        case OP_SET_GLOBAL: {
            uint16_t index = READ_U16();
            vm.globals[index] = pop();
            break;
        }

        case OP_JUMP: {
            uint16_t offset = READ_U16();
            frame->ip += offset;
            break;
        }

        case OP_LOOP: {
            uint16_t offset = READ_U16();
            frame->ip -= offset;
            break;
        }

        case OP_JUMP_IF_FALSE: {
            uint16_t offset = READ_U16();
            Value condition = pop();
            if (IS_BOOL(condition) && !AS_BOOL(condition)) frame->ip += offset;
            break;
        }

        case OP_JUMP_IF_TAG: {
            uint16_t tag = READ_U16();
            uint16_t offset = READ_U16();
            Value scrutinee = peek(0);
            if (IS_CON(scrutinee) && AS_CON(scrutinee)->tag == (int)tag) {
                frame->ip += offset;
            }
            break;
        }

        case OP_JUMP_IF_LIT: {
            uint16_t index = READ_U16();
            uint16_t offset = READ_U16();
            if (values_equal(peek(0), vm.image->constants[index])) {
                frame->ip += offset;
            }
            break;
        }

        case OP_CALL: {
            uint16_t arg_count = READ_U16();
            Value callee = peek(arg_count);
            if (!call_value(callee, arg_count)) return RUN_RUNTIME_ERROR;
            frame = &vm.frames[vm.frame_count - 1];
            break;
        }

        case OP_TAIL_CALL: {
            uint16_t arg_count = READ_U16();
            Value callee = peek(arg_count);

            /* Only a closure of matching arity can reuse the frame; anything
             * else (a native, a partial application) falls back to a normal
             * call so the general path stays in one place. */
            if (!IS_CLOSURE(callee) ||
                AS_CLOSURE(callee)->applied_count != 0 ||
                AS_CLOSURE(callee)->fn->arity != arg_count) {
                if (!call_value(callee, arg_count)) return RUN_RUNTIME_ERROR;
                frame = &vm.frames[vm.frame_count - 1];
                break;
            }

            ObjClosure *closure = AS_CLOSURE(callee);
            Value *args = vm.stack_top - arg_count;

            /* Anything captured from this frame must be closed before the
             * slots are overwritten. */
            close_upvalues(frame->slots);

            memmove(frame->slots, args, sizeof(Value) * (size_t)arg_count);
            vm.stack_top = frame->slots + arg_count;
            for (int i = arg_count; i < closure->fn->max_slots; i++) {
                push(UNIT_VAL);
            }

            frame->closure = closure;
            frame->ip = closure->fn->code;
            break;
        }

        case OP_RETURN: {
            Value result = pop();
            close_upvalues(frame->slots);
            vm.frame_count--;

            if (vm.frame_count == 0) {
                push(result);
                return RUN_OK;
            }

            vm.stack_top = frame->slots;
            push(result);
            frame = &vm.frames[vm.frame_count - 1];
            break;
        }

        case OP_CLOSURE: {
            uint16_t index = READ_U16();
            Function *fn = &vm.image->functions[index];
            ObjClosure *closure = closure_new(fn);
            push(OBJ_VAL(closure));   /* rooted before capturing upvalues */

            for (int i = 0; i < fn->upvalue_count; i++) {
                if (fn->upvalue_is_local[i]) {
                    closure->upvalues[i] =
                        capture_upvalue(frame->slots + fn->upvalue_index[i]);
                } else {
                    closure->upvalues[i] =
                        frame->closure->upvalues[fn->upvalue_index[i]];
                }
            }
            break;
        }

        case OP_CALL_NATIVE: {
            uint16_t id = READ_U16();
            uint16_t arg_count = READ_U16();

            Value args[8];
            for (int i = 0; i < arg_count && i < 8; i++) {
                args[i] = vm.stack_top[-arg_count + i];
            }

            Value result;
            if (!native_call(id, arg_count, args, &result)) {
                return RUN_RUNTIME_ERROR;
            }
            vm.stack_top -= arg_count;
            push(result);
            break;
        }

        case OP_MAKE_CON: {
            uint16_t tag = READ_U16();
            uint16_t count = READ_U16();
            ObjCon *con = con_new((int)tag, (int)count);
            for (int i = count - 1; i >= 0; i--) con->fields[i] = pop();
            push(OBJ_VAL(con));
            break;
        }

        case OP_MAKE_TUPLE: {
            uint16_t count = READ_U16();
            ObjTuple *tuple = tuple_new((int)count);
            for (int i = count - 1; i >= 0; i--) tuple->items[i] = pop();
            push(OBJ_VAL(tuple));
            break;
        }

        case OP_GET_FIELD: {
            uint16_t index = READ_U16();
            Value target = pop();
            if (IS_CON(target) && index < AS_CON(target)->count) {
                push(AS_CON(target)->fields[index]);
            } else if (IS_TUPLE(target) && index < AS_TUPLE(target)->count) {
                push(AS_TUPLE(target)->items[index]);
            } else {
                runtime_error("cannot project field %u", index);
                return RUN_RUNTIME_ERROR;
            }
            break;
        }

        case OP_MAKE_RECORD: {
            uint16_t set_index = READ_U16();
            LabelSet *set = &vm.image->label_sets[set_index];
            ObjRecord *record = record_new(set->count);
            for (int i = 0; i < set->count; i++) record->labels[i] = set->labels[i];
            for (int i = set->count - 1; i >= 0; i--) record->values[i] = pop();
            push(OBJ_VAL(record));
            break;
        }

        case OP_RECORD_GET: {
            uint16_t name_index = READ_U16();
            Value target = pop();
            ObjString *label = AS_STRING(vm.image->constants[name_index]);
            if (!IS_RECORD(target)) {
                runtime_error("expected a record");
                return RUN_RUNTIME_ERROR;
            }
            ObjRecord *record = AS_RECORD(target);
            bool found = false;
            for (int i = 0; i < record->count; i++) {
                if (record->labels[i]->length == label->length &&
                    memcmp(record->labels[i]->chars, label->chars,
                           (size_t)label->length) == 0) {
                    push(record->values[i]);
                    found = true;
                    break;
                }
            }
            if (!found) {
                runtime_error("no field `%s` on this record", label->chars);
                return RUN_RUNTIME_ERROR;
            }
            break;
        }

        case OP_RECORD_SET: {
            uint16_t set_index = READ_U16();
            LabelSet *set = &vm.image->label_sets[set_index];

            /* The updates sit above the base record on the stack. */
            Value base = vm.stack_top[-set->count - 1];
            if (!IS_RECORD(base)) {
                runtime_error("cannot update a non-record");
                return RUN_RUNTIME_ERROR;
            }
            ObjRecord *source = AS_RECORD(base);

            ObjRecord *result = record_new(source->count);
            push(OBJ_VAL(result));   /* root it before anything else runs */
            for (int i = 0; i < source->count; i++) {
                result->labels[i] = source->labels[i];
                result->values[i] = source->values[i];
            }
            pop();

            for (int j = set->count - 1; j >= 0; j--) {
                Value updated = vm.stack_top[-(set->count - j)];
                ObjString *label = set->labels[j];
                for (int i = 0; i < result->count; i++) {
                    if (result->labels[i]->length == label->length &&
                        memcmp(result->labels[i]->chars, label->chars,
                               (size_t)label->length) == 0) {
                        result->values[i] = updated;
                        break;
                    }
                }
            }

            vm.stack_top -= set->count + 1;
            push(OBJ_VAL(result));
            break;
        }

        case OP_FAIL: {
            uint16_t index = READ_U16();
            Value message = vm.image->constants[index];
            runtime_error("%s", IS_STRING(message) ? AS_CSTRING(message) : "failed");
            return RUN_RUNTIME_ERROR;
        }

        case OP_HALT:
            return RUN_OK;

        /* -- inline primitives -------------------------------------- */

        case OP_ADD_INT: BINARY_INT(wrap_add); break;
        case OP_SUB_INT: BINARY_INT(wrap_sub); break;
        case OP_MUL_INT: BINARY_INT(wrap_mul); break;

        case OP_DIV_INT: {
            Value b = pop(); Value a = pop();
            if (AS_INT(b) == 0) {
                runtime_error("division by zero");
                return RUN_RUNTIME_ERROR;
            }
            if (AS_INT(a) == INT64_MIN && AS_INT(b) == -1) {
                push(INT_VAL(INT64_MIN));   /* wraps, matching the spec */
            } else {
                push(INT_VAL(AS_INT(a) / AS_INT(b)));
            }
            break;
        }

        case OP_MOD_INT: {
            Value b = pop(); Value a = pop();
            if (AS_INT(b) == 0) {
                runtime_error("modulo by zero");
                return RUN_RUNTIME_ERROR;
            }
            if (AS_INT(a) == INT64_MIN && AS_INT(b) == -1) {
                push(INT_VAL(0));
            } else {
                push(INT_VAL(AS_INT(a) % AS_INT(b)));
            }
            break;
        }

        case OP_POW_INT: {
            Value b = pop(); Value a = pop();
            if (AS_INT(b) < 0) {
                runtime_error("negative exponent");
                return RUN_RUNTIME_ERROR;
            }
            int64_t result = 1;
            int64_t base = AS_INT(a);
            int64_t exponent = AS_INT(b);
            while (exponent > 0) {
                if (exponent & 1) result = wrap_mul(result, base);
                base = wrap_mul(base, base);
                exponent >>= 1;
            }
            push(INT_VAL(result));
            break;
        }

        case OP_NEG_INT: {
            Value a = pop();
            push(INT_VAL(wrap_sub(0, AS_INT(a))));
            break;
        }

        case OP_ADD_FLOAT: BINARY_FLOAT(+); break;
        case OP_SUB_FLOAT: BINARY_FLOAT(-); break;
        case OP_MUL_FLOAT: BINARY_FLOAT(*); break;
        case OP_DIV_FLOAT: BINARY_FLOAT(/); break;

        case OP_NEG_FLOAT: {
            Value a = pop();
            push(FLOAT_VAL(-AS_FLOAT(a)));
            break;
        }

        case OP_EQ: {
            Value b = pop(); Value a = pop();
            push(BOOL_VAL(values_equal(a, b)));
            break;
        }

        case OP_NE: {
            Value b = pop(); Value a = pop();
            push(BOOL_VAL(!values_equal(a, b)));
            break;
        }

        case OP_LT: COMPARISON(<);  break;
        case OP_LE: COMPARISON(<=); break;
        case OP_GT: COMPARISON(>);  break;
        case OP_GE: COMPARISON(>=); break;

        case OP_NOT: {
            Value a = pop();
            push(BOOL_VAL(!(IS_BOOL(a) && AS_BOOL(a))));
            break;
        }

        case OP_CONCAT_STRING: {
            Value b = peek(0); Value a = peek(1);
            if (!IS_STRING(a) || !IS_STRING(b)) {
                runtime_error("expected two Strings");
                return RUN_RUNTIME_ERROR;
            }
            ObjString *left = AS_STRING(a);
            ObjString *right = AS_STRING(b);
            int length = left->length + right->length;
            char *chars = (char *)reallocate(NULL, 0, (size_t)length + 1);
            memcpy(chars, left->chars, (size_t)left->length);
            memcpy(chars + left->length, right->chars, (size_t)right->length);
            chars[length] = '\0';
            ObjString *result = string_take(chars, length);
            pop(); pop();
            push(OBJ_VAL(result));
            break;
        }

        case OP_APPEND_LIST: {
            /* Copy the left spine, then point its tail at the right list. */
            Value right = peek(0);
            Value left = peek(1);

            int count = 0;
            for (Value cursor = left; IS_CON(cursor) && AS_CON(cursor)->count == 2;
                 cursor = AS_CON(cursor)->fields[1]) {
                count++;
            }

            Value result = right;
            for (int i = count - 1; i >= 0; i--) {
                Value cursor = left;
                for (int j = 0; j < i; j++) cursor = AS_CON(cursor)->fields[1];
                push(result);                    /* keep the tail reachable */
                ObjCon *cell = con_new(1, 2);    /* Cons */
                pop();
                cell->fields[0] = AS_CON(cursor)->fields[0];
                cell->fields[1] = result;
                result = OBJ_VAL(cell);
            }

            pop(); pop();
            push(result);
            break;
        }

        default:
            runtime_error("unknown opcode %d", instruction);
            return RUN_RUNTIME_ERROR;
        }
    }
}

#undef READ_BYTE
#undef READ_U16
#undef BINARY_INT
#undef BINARY_FLOAT
#undef COMPARISON

/* ------------------------------------------------------------------ */
/* Entry                                                              */
/* ------------------------------------------------------------------ */

RunResult vm_run(Image *image, int argc, char **argv)
{
    vm.image = image;
    vm.argc = argc;
    vm.argv = argv;

    vm.global_count = image->global_count;
    vm.globals = (Value *)calloc((size_t)image->global_count + 1, sizeof(Value));
    for (int i = 0; i < vm.global_count; i++) vm.globals[i] = UNIT_VAL;

    Function *entry = &image->functions[image->entry];
    ObjClosure *closure = closure_new(entry);
    push(OBJ_VAL(closure));

    CallFrame *frame = &vm.frames[vm.frame_count++];
    frame->closure = closure;
    frame->ip = entry->code;
    frame->slots = vm.stack_top - 1;

    for (int i = 0; i < entry->max_slots; i++) push(UNIT_VAL);

    RunResult result = run();
    return vm.had_error ? RUN_RUNTIME_ERROR : result;
}
