#!/usr/bin/env python3
"""The stdlib-only static guard, born with its single exception (SD-2 / R-22 as
amended by GD-21). Run as `python3 test_stdlib_only.py`; exits non-zero on
failure. No pytest, no runner.

GD-21, verbatim in effect:

    Stdlib-only on the ingest and serve critical path. `pymongo` (pinned
    `==4.17.0`, with `dnspython`) is the ONE permitted third-party runtime
    dependency, importable **only** from `aggregator/mongo_store.py` and
    `aggregator/mirror.py` (lazy import). Its absence degrades the mirror to
    `mirror: "absent"` in `/health` — it never fails startup, never breaks an
    agent, never blocks a test.

So this file asserts three things, and it is written **now**, in the same wave
as the first `aggregator/` module, precisely so the suite is never red between
sub-plans (SD-2):

1. no module under `aggregator/` imports a third-party package, except
   `mongo_store.py` and `mirror.py`, which may import `pymongo`/`dns`;
2. in those two, the import is **lazy** — inside a function, never at module
   level — so importing them with pymongo absent still works;
3. every module actually imports in a subprocess with nothing third-party
   ending up in `sys.modules`. That is the executable form of "its absence
   never fails startup", and it keeps working for files that do not exist yet.

The two Mongo modules land in later sub-plans (sp-05/sp-06); this guard passes
before they exist and tightens automatically when they appear.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "aggregator"

#: GD-21's single exception. File name -> the top-level module names it may
#: import. `dns` is dnspython's import name (needed for `mongodb+srv://` URIs).
PYMONGO_ALLOWED = {
    "mongo_store.py": {"pymongo", "bson", "dns"},
    "mirror.py": {"pymongo", "bson", "dns"},
}

#: Present in `sys.modules` on a bare interpreter in this sandbox (a site hook),
#: so it is not evidence of a Touch dependency.
ENV_NOISE = {"sitecustomize"}

failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def module_files():
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _eager_nodes(node):
    """Every node reachable without entering a function body.

    A class body executes at import time, so it is *not* a boundary; a function
    or lambda body is. Walking structurally is the point: computing the eager set
    as "all imports minus the lazy ones" makes a file that imports pymongo both
    at module level **and** lazily report an empty eager set — precisely the
    shape `try: import pymongo / except ImportError: ...` plus a lazy client
    factory has, i.e. the most likely real code.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _FUNCTION_NODES):
            continue
        yield child
        yield from _eager_nodes(child)


def imports_of(tree):
    """(module-level names, function-level names) imported by ``tree``.

    "module-level" means *executed at import time* — including inside `if`,
    `try`/`except`, `with` and class bodies. The two sets are computed
    independently and may overlap.
    """
    lazy = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Import):
                    lazy.update(a.name.split(".")[0] for a in inner.names)
                elif isinstance(inner, ast.ImportFrom) and inner.module and inner.level == 0:
                    lazy.add(inner.module.split(".")[0])
    top = set()
    for node in _eager_nodes(tree):
        if isinstance(node, ast.Import):
            top.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:                      # relative import: our own package
                continue
            if node.module:
                top.add(node.module.split(".")[0])
    return top, lazy


def test_the_guard_detects_an_eager_import_beside_a_lazy_one():
    print("test_the_guard_detects_an_eager_import_beside_a_lazy_one")
    both = "import pymongo\ndef client():\n    import pymongo\n    return pymongo\n"
    top, lazy = imports_of(ast.parse(both))
    check("pymongo" in top and "pymongo" in lazy,
          "an eager import is still seen when the same name is also imported lazily")
    guarded = "try:\n    import pymongo\nexcept ImportError:\n    pymongo = None\n"
    check("pymongo" in imports_of(ast.parse(guarded))[0],
          "a try/except-guarded import executes at import time, so it counts as eager")
    inside_class = "class C:\n    import pymongo\n"
    check("pymongo" in imports_of(ast.parse(inside_class))[0],
          "a class-body import executes at import time too")
    lazy_only = "def client():\n    import pymongo\n    return pymongo\n"
    top2, lazy2 = imports_of(ast.parse(lazy_only))
    check("pymongo" not in top2 and "pymongo" in lazy2,
          "a purely lazy import is what GD-21 permits, and is still reported as lazy")
    check("os" in imports_of(ast.parse("if True:\n    import os\n"))[0],
          "an `if`-guarded import is eager (the walk descends into non-function bodies)")
    check(imports_of(ast.parse("from . import x\nfrom .y import z\n"))[0] == set(),
          "relative imports are our own package, never a third-party claim")


def test_no_third_party_imports():
    print("test_no_third_party_imports")
    files = module_files()
    check(bool(files), f"found {len(files)} module(s) under aggregator/")
    stdlib = set(sys.stdlib_module_names) | {"aggregator", "__future__"}
    for path in files:
        top, lazy = imports_of(ast.parse(path.read_text()))
        allowed = PYMONGO_ALLOWED.get(path.name, set())
        offenders = sorted((top | lazy) - stdlib - allowed)
        check(not offenders,
              f"{path.name}: no third-party imports{'' if not offenders else f' — {offenders}'}")
        if path.name in PYMONGO_ALLOWED:
            eager = sorted(top & allowed)
            check(not eager,
                  f"{path.name}: pymongo is imported lazily, not at module level "
                  f"(GD-21){'' if not eager else f' — eager: {eager}'}")


def test_the_exception_is_named_and_narrow():
    print("test_the_exception_is_named_and_narrow")
    check(set(PYMONGO_ALLOWED) == {"mongo_store.py", "mirror.py"},
          "exactly two files may import pymongo (GD-21) — no third by analogy")
    for name in PYMONGO_ALLOWED:
        path = PKG / name
        state = "present" if path.exists() else "not written yet (sp-05/sp-06)"
        print(f"    {name}: {state}")   # a fact, not an assertion (SD-2)


def test_every_module_imports_without_third_party_packages():
    print("test_every_module_imports_without_third_party_packages")
    for path in module_files():
        rel = path.relative_to(REPO).with_suffix("")
        dotted = ".".join(rel.parts)
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        code = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(REPO)!r})\n"
            f"import {dotted}\n"
            "names = sorted(n for n in sys.modules\n"
            "               if '.' not in n and not n.startswith('_')\n"
            "               and n not in sys.stdlib_module_names)\n"
            "print(json.dumps(names))\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        if proc.returncode != 0:
            check(False, f"{dotted} failed to import: {proc.stderr.strip().splitlines()[-1:]}")
            continue
        loaded = set(json.loads(proc.stdout.strip().splitlines()[-1]))
        extra = sorted(loaded - {"aggregator"} - ENV_NOISE)
        check(not extra, f"{dotted} imports with nothing third-party loaded"
                         f"{'' if not extra else f' — {extra}'}")


def test_pymongo_absence_is_the_tested_condition():
    print("test_pymongo_absence_is_the_tested_condition")
    # Not a requirement — a fact worth printing, because the guard above means
    # something different depending on it. With pymongo installed, the lazy
    # import rule is what keeps the leaf modules clean; without it, the
    # subprocess arm proves the degraded path outright (GD-21: absence degrades
    # the mirror to `mirror: "absent"`, it never fails startup).
    proc = subprocess.run([sys.executable, "-c", "import pymongo"], capture_output=True)
    print(f"    pymongo is {'installed' if proc.returncode == 0 else 'NOT installed'} here")
    print("    either way the suite passes and Mongo tests skip cleanly (GD-21)")


def main():
    for t in (test_no_third_party_imports, test_the_exception_is_named_and_narrow,
              test_the_guard_detects_an_eager_import_beside_a_lazy_one,
              test_every_module_imports_without_third_party_packages,
              test_pymongo_absence_is_the_tested_condition):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all stdlib-only guard checks passed")


if __name__ == "__main__":
    main()
