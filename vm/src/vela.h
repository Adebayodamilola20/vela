/* Shared declarations for the Vela bytecode VM.
 *
 * The VM is a stack machine with call frames, flat closures and open upvalues.
 * Its observable behaviour must match `velac/interp.py` exactly — the
 * differential tests diff stdout byte for byte — so anything user-visible
 * (float formatting, `show`, ordering, error messages) is specified there and
 * mirrored here.
 */

#ifndef VELA_H
#define VELA_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "opcodes.h"

#define VELA_STACK_MAX   (1024 * 1024)
#define VELA_FRAMES_MAX  (64 * 1024)

/* ------------------------------------------------------------------ */
/* Values                                                             */
/* ------------------------------------------------------------------ */

typedef enum {
    VAL_INT,
    VAL_FLOAT,
    VAL_BOOL,
    VAL_UNIT,
    VAL_OBJ
} ValueType;

typedef struct Obj Obj;

typedef struct {
    ValueType type;
    union {
        int64_t  integer;
        double   number;
        bool     boolean;
        Obj     *obj;
    } as;
} Value;

#define INT_VAL(v)    ((Value){VAL_INT,   {.integer = (v)}})
#define FLOAT_VAL(v)  ((Value){VAL_FLOAT, {.number  = (v)}})
#define BOOL_VAL(v)   ((Value){VAL_BOOL,  {.boolean = (v)}})
#define UNIT_VAL      ((Value){VAL_UNIT,  {.integer = 0}})
#define OBJ_VAL(v)    ((Value){VAL_OBJ,   {.obj = (Obj *)(v)}})

#define IS_INT(v)     ((v).type == VAL_INT)
#define IS_FLOAT(v)   ((v).type == VAL_FLOAT)
#define IS_BOOL(v)    ((v).type == VAL_BOOL)
#define IS_UNIT(v)    ((v).type == VAL_UNIT)
#define IS_OBJ(v)     ((v).type == VAL_OBJ)

#define AS_INT(v)     ((v).as.integer)
#define AS_FLOAT(v)   ((v).as.number)
#define AS_BOOL(v)    ((v).as.boolean)
#define AS_OBJ(v)     ((v).as.obj)

/* ------------------------------------------------------------------ */
/* Objects                                                            */
/* ------------------------------------------------------------------ */

typedef enum {
    OBJ_STRING,
    OBJ_CON,        /* a data value: tag plus fields                    */
    OBJ_TUPLE,
    OBJ_RECORD,
    OBJ_CLOSURE,
    OBJ_UPVALUE,
    OBJ_NATIVE      /* a partially applied native                       */
} ObjType;

struct Obj {
    ObjType type;
    bool    marked;
    Obj    *next;   /* every object, for the sweep                      */
};

typedef struct {
    Obj      obj;
    int      length;      /* bytes, excluding the terminator            */
    uint32_t hash;
    char    *chars;
} ObjString;

typedef struct {
    Obj    obj;
    int    tag;
    int    count;
    Value *fields;
} ObjCon;

typedef struct {
    Obj    obj;
    int    count;
    Value *items;
} ObjTuple;

/* Records carry their own sorted label array so two records built from
 * different label sets still compare and print correctly. */
typedef struct {
    Obj         obj;
    int         count;
    ObjString **labels;   /* sorted                                     */
    Value      *values;
} ObjRecord;

typedef struct ObjUpvalue {
    Obj                obj;
    Value             *location;   /* into the stack while open         */
    Value              closed;
    struct ObjUpvalue *next;
} ObjUpvalue;

typedef struct {
    char      *name;
    int        arity;
    int        max_slots;
    int        upvalue_count;
    uint8_t   *upvalue_is_local;
    uint16_t  *upvalue_index;
    uint8_t   *code;
    uint32_t   code_length;
} Function;

typedef struct {
    Obj          obj;
    Function    *fn;
    ObjUpvalue **upvalues;
    int          upvalue_count;
    /* Arguments accumulated by partial application. */
    Value       *applied;
    int          applied_count;
} ObjClosure;

typedef struct {
    Obj    obj;
    int    native_id;
    Value *applied;
    int    applied_count;
} ObjNative;

#define OBJ_TYPE(v)     (AS_OBJ(v)->type)
#define IS_STRING(v)    (IS_OBJ(v) && OBJ_TYPE(v) == OBJ_STRING)
#define IS_CON(v)       (IS_OBJ(v) && OBJ_TYPE(v) == OBJ_CON)
#define IS_TUPLE(v)     (IS_OBJ(v) && OBJ_TYPE(v) == OBJ_TUPLE)
#define IS_RECORD(v)    (IS_OBJ(v) && OBJ_TYPE(v) == OBJ_RECORD)
#define IS_CLOSURE(v)   (IS_OBJ(v) && OBJ_TYPE(v) == OBJ_CLOSURE)
#define IS_NATIVE(v)    (IS_OBJ(v) && OBJ_TYPE(v) == OBJ_NATIVE)

#define AS_STRING(v)    ((ObjString *)AS_OBJ(v))
#define AS_CSTRING(v)   (((ObjString *)AS_OBJ(v))->chars)
#define AS_CON(v)       ((ObjCon *)AS_OBJ(v))
#define AS_TUPLE(v)     ((ObjTuple *)AS_OBJ(v))
#define AS_RECORD(v)    ((ObjRecord *)AS_OBJ(v))
#define AS_CLOSURE(v)   ((ObjClosure *)AS_OBJ(v))
#define AS_NATIVE(v)    ((ObjNative *)AS_OBJ(v))

/* ------------------------------------------------------------------ */
/* The loaded image                                                   */
/* ------------------------------------------------------------------ */

typedef struct {
    char *name;
    int   arity;
    char *type_name;
} ConstructorInfo;

typedef struct {
    int          count;
    ObjString  **labels;
} LabelSet;

typedef struct {
    Value           *constants;
    int              constant_count;

    LabelSet        *label_sets;
    int              label_set_count;

    ConstructorInfo *constructors;
    int              constructor_count;

    char           **global_names;
    int              global_count;

    Function        *functions;
    int              function_count;

    int              entry;
} Image;

/* ------------------------------------------------------------------ */
/* The VM                                                             */
/* ------------------------------------------------------------------ */

typedef struct {
    ObjClosure *closure;
    uint8_t    *ip;
    Value      *slots;
} CallFrame;

typedef struct {
    Image      *image;

    Value      *stack;
    Value      *stack_top;
    size_t      stack_capacity;

    CallFrame  *frames;
    int         frame_count;
    int         frame_capacity;

    Value      *globals;
    int         global_count;

    ObjUpvalue *open_upvalues;

    Obj        *objects;       /* every allocation, for the sweep       */
    size_t      bytes_allocated;
    size_t      next_gc;

    /* Objects the collector must treat as roots while a native runs. */
    Value      *temp_roots;
    int         temp_root_count;
    int         temp_root_capacity;

    int         argc;
    char      **argv;

    bool        had_error;
} VM;

extern VM vm;

typedef enum {
    RUN_OK,
    RUN_RUNTIME_ERROR
} RunResult;

/* -- lifecycle ----------------------------------------------------- */

void vm_init(void);
void vm_free(void);
RunResult vm_run(Image *image, int argc, char **argv);
void runtime_error(const char *format, ...);

/* -- stack --------------------------------------------------------- */

void  push(Value value);
Value pop(void);
Value peek(int distance);

/* -- allocation ---------------------------------------------------- */

ObjString  *string_take(char *chars, int length);
ObjString  *string_copy(const char *chars, int length);
ObjCon     *con_new(int tag, int count);
ObjTuple   *tuple_new(int count);
ObjRecord  *record_new(int count);
ObjClosure *closure_new(Function *fn);
ObjNative  *native_new(int native_id);
ObjUpvalue *upvalue_new(Value *slot);

void *reallocate(void *pointer, size_t old_size, size_t new_size);
void  collect_garbage(void);
void  mark_value(Value value);
void  mark_object(Obj *object);
void  push_temp_root(Value value);
void  pop_temp_root(int count);

/* -- values -------------------------------------------------------- */

bool  values_equal(Value a, Value b);
int   values_compare(Value a, Value b, bool *ok);
char *value_to_show(Value value);      /* malloc'd; caller frees       */
char *value_to_text(Value value);      /* `print` semantics            */
void  format_double(double value, char *out, size_t size);
int64_t wrap_add(int64_t a, int64_t b);
int64_t wrap_sub(int64_t a, int64_t b);
int64_t wrap_mul(int64_t a, int64_t b);

/* -- image --------------------------------------------------------- */

Image *image_load(const char *path, char **error);
void   image_free(Image *image);

/* -- natives ------------------------------------------------------- */

bool native_call(int id, int arg_count, Value *args, Value *result);

#endif /* VELA_H */
