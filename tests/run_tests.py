"""Golden-file test runner for Vela programs.

Each case is a `.vela` file under `tests/cases/`. Its expected output lives in
a companion `.expected` file. A case that is supposed to fail carries a
`.expected-error` file instead, holding a substring the diagnostic must
contain — matching on a substring rather than the whole rendering means error
*wording* can improve without every test needing an update, while the error
*code* and the offending line stay pinned.

    python3 tests/run_tests.py                 run every case
    python3 tests/run_tests.py --filter list   only cases whose name matches
    python3 tests/run_tests.py --differential  also run each case on the VM
                                               and require identical output
    python3 tests/run_tests.py --update        rewrite .expected from actual

`--differential` is the check the spec asks for: both backends must produce
byte-identical observable output for every program here.
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = os.path.join(ROOT, "tests", "cases")
sys.path.insert(0, ROOT)

from velac.driver import compile_program  # noqa: E402
from velac.interp import Interpreter  # noqa: E402
from velac.values import VelaPanic  # noqa: E402

GREEN = "\x1b[32m"
RED = "\x1b[31;1m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"


def _color(enabled: bool, code: str, text: str) -> str:
    return f"{code}{text}{RESET}" if enabled else text


class Case:
    def __init__(self, path: str) -> None:
        self.path = path
        self.name = os.path.splitext(os.path.relpath(path, CASES))[0]
        base = os.path.splitext(path)[0]
        self.expected_path = base + ".expected"
        self.error_path = base + ".expected-error"

    @property
    def expects_error(self) -> bool:
        return os.path.exists(self.error_path)

    def expected(self) -> str:
        if os.path.exists(self.expected_path):
            with open(self.expected_path, encoding="utf-8") as fh:
                return fh.read()
        return ""

    def expected_error(self) -> str:
        with open(self.error_path, encoding="utf-8") as fh:
            return fh.read().strip()


def discover(pattern: str | None) -> list[Case]:
    out: list[Case] = []
    for dirpath, _dirnames, filenames in os.walk(CASES):
        for name in sorted(filenames):
            if not name.endswith(".vela"):
                continue
            case = Case(os.path.join(dirpath, name))
            if pattern is None or pattern in case.name:
                out.append(case)
    return sorted(out, key=lambda c: c.name)


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


def run_interpreted(case: Case) -> tuple[str, str | None]:
    """Compile and interpret. Returns (stdout, failure) — failure is None on success."""
    result = compile_program(case.path)

    if result.errors:
        rendered = "\n".join(
            result.source_map.render(e, color=False) for e in result.errors)
        return "", rendered

    out = io.StringIO()
    err = io.StringIO()
    interp = Interpreter(result.program, argv=[], stdout=out, stderr=err)

    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(max(previous, 20000))
    try:
        interp.run()
    except VelaPanic as exc:
        return out.getvalue(), f"runtime error: {exc.message}"
    except RecursionError:
        return out.getvalue(), "runtime error: stack overflow"
    finally:
        sys.setrecursionlimit(previous)

    return out.getvalue() + err.getvalue(), None


def run_vm(case: Case) -> tuple[str, str | None]:
    """Build a bytecode image and run it under the C VM."""
    vm = os.path.join(ROOT, "vm", "vela")
    if not os.path.exists(vm):
        return "", "vm not built (run `make -C vm`)"

    image = os.path.join(ROOT, "build", os.path.basename(case.path) + "c")
    os.makedirs(os.path.dirname(image), exist_ok=True)

    build = subprocess.run(
        [sys.executable, "-m", "velac", "build", case.path, "-o", image],
        cwd=ROOT, capture_output=True, text=True)
    if build.returncode != 0:
        return "", f"build failed: {build.stderr.strip()}"

    proc = subprocess.run([vm, image], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        return proc.stdout, f"vm exited {proc.returncode}: {proc.stderr.strip()}"
    return proc.stdout + proc.stderr, None


# --------------------------------------------------------------------------
# Checking
# --------------------------------------------------------------------------


def check(case: Case, actual: str, failure: str | None,
          update: bool) -> tuple[bool, str]:
    if case.expects_error:
        if failure is None:
            return False, "expected this to fail, but it succeeded"
        needle = case.expected_error()
        if needle and needle not in failure:
            return False, f"error did not mention {needle!r}\n--- actual ---\n{failure}"
        return True, ""

    if failure is not None:
        return False, failure

    if update:
        with open(case.expected_path, "w", encoding="utf-8") as fh:
            fh.write(actual)
        return True, ""

    expected = case.expected()
    if actual != expected:
        return False, _diff(expected, actual)
    return True, ""


def _diff(expected: str, actual: str) -> str:
    exp_lines = expected.splitlines()
    act_lines = actual.splitlines()
    out = []
    for i in range(max(len(exp_lines), len(act_lines))):
        e = exp_lines[i] if i < len(exp_lines) else "<missing>"
        a = act_lines[i] if i < len(act_lines) else "<missing>"
        if e != a:
            out.append(f"  line {i + 1}:\n    expected: {e!r}\n    actual:   {a!r}")
    return "\n".join(out) or "  outputs differ in trailing whitespace"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Vela test suite.")
    parser.add_argument("--filter", help="only run cases whose name contains this")
    parser.add_argument("--differential", action="store_true",
                        help="also run under the C VM and require identical output")
    parser.add_argument("--update", action="store_true",
                        help="rewrite .expected files from actual output")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    cases = discover(args.filter)
    if not cases:
        print("no test cases found")
        return 1

    passed = 0
    failures: list[tuple[str, str]] = []

    for case in cases:
        actual, failure = run_interpreted(case)
        ok, detail = check(case, actual, failure, args.update)

        if ok and args.differential and not case.expects_error:
            vm_out, vm_failure = run_vm(case)
            if vm_failure is not None:
                ok, detail = False, f"[vm] {vm_failure}"
            elif vm_out != actual:
                ok, detail = False, "[differential] backends disagree\n" + _diff(actual, vm_out)

        if ok:
            passed += 1
            if args.verbose:
                print(f"{_color(color, GREEN, 'pass')} {case.name}")
        else:
            failures.append((case.name, detail))
            print(f"{_color(color, RED, 'FAIL')} {case.name}")
            if detail:
                print(_color(color, DIM, "\n".join(
                    "      " + line for line in detail.splitlines())))

    total = len(cases)
    mode = " (differential)" if args.differential else ""
    print(f"\n{passed}/{total} passed{mode}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
