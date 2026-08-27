# The Vela bytecode VM.
#
#   make            build ./vela
#   make debug      build with assertions and sanitizers
#   make clean
#
# `opcodes.h` is generated from `velac/prims.py`, so it is rebuilt whenever
# that table changes rather than being kept in sync by hand.

CC      ?= cc
CFLAGS  ?= -std=c11 -O2 -Wall -Wextra -Wpedantic -Wno-unused-parameter
LDFLAGS ?=
LDLIBS  ?= -lm

SRC_DIR := src
OBJ_DIR := build
TARGET  := vela

SOURCES := $(wildcard $(SRC_DIR)/*.c)
OBJECTS := $(patsubst $(SRC_DIR)/%.c,$(OBJ_DIR)/%.o,$(SOURCES))
GENERATED := $(SRC_DIR)/opcodes.h

PYTHON ?= python3

.PHONY: all debug clean

all: $(TARGET)

debug: CFLAGS := -std=c11 -O0 -g -Wall -Wextra -Wpedantic \
                 -Wno-unused-parameter -fsanitize=address,undefined
debug: LDFLAGS += -fsanitize=address,undefined
debug: clean $(TARGET)

$(TARGET): $(OBJECTS)
	$(CC) $(LDFLAGS) -o $@ $^ $(LDLIBS)

$(OBJ_DIR)/%.o: $(SRC_DIR)/%.c $(GENERATED) | $(OBJ_DIR)
	$(CC) $(CFLAGS) -I$(SRC_DIR) -c -o $@ $<

# Regenerate the opcode header from the compiler's tables.
$(GENERATED): ../velac/opcodes.py ../velac/prims.py
	cd .. && $(PYTHON) -m velac.opcodes

$(OBJ_DIR):
	@mkdir -p $(OBJ_DIR)

clean:
	@rm -rf $(OBJ_DIR) $(TARGET)
