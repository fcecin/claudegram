#!/usr/bin/env python3
"""Deps-free test runner (no pytest). Discovers tests/test_*.py, runs every top-level
`test_*` callable (sync or async, each in a fresh event loop), resets shared state
between tests, and exits non-zero if any fail. Invoked by ../test.sh."""

import asyncio
import importlib
import inspect
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests import fakes  # noqa: E402  (also sets logging + fast debounce)


def main():
    tests_dir = pathlib.Path(__file__).resolve().parent
    files = sorted(tests_dir.glob("test_*.py"))
    total = passed = 0
    failures = []
    for f in files:
        mod = importlib.import_module(f"tests.{f.stem}")
        fns = [(n, o) for n, o in sorted(vars(mod).items())
               if n.startswith("test_") and callable(o)]
        for name, fn in fns:
            total += 1
            # Isolation: fresh registry + no leftover flags before each test.
            fakes.reset_registry()
            fakes.clear_flags()
            try:
                if inspect.iscoroutinefunction(fn):
                    asyncio.run(fn())
                else:
                    fn()
                passed += 1
                print(f"  ok   {f.stem}.{name}")
            except Exception:
                failures.append((f.stem, name, traceback.format_exc()))
                print(f" FAIL  {f.stem}.{name}")
    fakes.clear_flags()
    print()
    for stem, name, tb in failures:
        print(f"==================== FAIL {stem}.{name} ====================\n{tb}")
    print(f"{passed}/{total} passed" + ("" if not failures else f"  ({len(failures)} FAILED)"))
    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
