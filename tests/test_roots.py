#!/usr/bin/env python3
"""Stdlib-only tests for the anchor module itself, `tests/_roots.py` (I1;
LAYOUT-14, PROTOCOL-17). Run as `python3 test_roots.py`; exits non-zero on
failure. No pytest, no runner.

`_roots.py` is imported by ~25 test files for its constants, so it is the one
file in the suite whose *shape* the suite has to guard: a wrong flip point takes
every importer down at once, and the two constants added here — `ORCH_REL` /
`ORCH` — carry two decisions that are easy to "improve" back into defects.

1. The leaf name `local-orchestrators` is load-bearing (G10). The scope guard
   matches it as a bare literal segment, so renaming the leaf would make the
   hook deny unrelated paths in every project the plugin is enabled in. The move
   is a `.claude` -> `.touch` edit of the FIRST component only.
2. `ORCH` deliberately gets NO import-time assert, unlike `SRC`/`MON`/`CATALOG`.
   The tasks root is gitignored, hence absent from a clean checkout of HEAD —
   the exact tree `scripts/release.sh` step 2 runs the whole suite in. An assert
   there converts "this repo has no runs yet" into an import failure in every
   test file, during a release. The AST check below is why that cannot be added
   back by a later reader who notices the asymmetry and tidies it up.
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The canonical trees are named through `tests/_roots.py`, never by a
# hand-spelled literal — and here the module under test IS that anchor.
from _roots import ORCH, ORCH_REL, REPO                   # noqa: E402

ANCHOR = Path(__file__).resolve().parent / "_roots.py"

failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


# --- flip point 3 has the shape every resolver joins against ---------------
def test_tasks_root_anchor_shape():
    print("test_tasks_root_anchor_shape")
    check(not ORCH_REL.is_absolute(),
          "ORCH_REL is RELATIVE — resolvers join it onto a resolved project root")
    check(ORCH_REL.parts == (".touch", "local-orchestrators"),
          f"ORCH_REL is exactly .touch/local-orchestrators (got {ORCH_REL})")
    check(ORCH_REL.name == "local-orchestrators",
          "the leaf name is kept (G10: the scope guard matches it as a literal "
          "segment; a rename denies unrelated paths)")
    check(ORCH == REPO / ORCH_REL,
          "ORCH is this repo's own run history, REPO / ORCH_REL")
    check(ORCH.is_absolute(), "ORCH is absolute, like every other anchor here")


# --- the deliberate asymmetry: no assert on the gitignored tree ------------
def test_no_import_time_assert_on_the_tasks_root():
    print("test_no_import_time_assert_on_the_tasks_root")
    tree = ast.parse(ANCHOR.read_text(), filename=str(ANCHOR))
    named = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            named |= {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    check("ORCH" not in named and "ORCH_REL" not in named,
          "no import-time assert mentions ORCH/ORCH_REL — the tree is "
          f"gitignored and absent in release.sh step 2's clean checkout "
          f"(offenders: {sorted(named & {'ORCH', 'ORCH_REL'}) or 'none'})")
    check(named,
          "…while the other anchors DO keep their loud asserts "
          f"(asserted names: {sorted(named)})")


# --- the anchor stays importable, which is its whole job -------------------
def test_anchor_is_import_safe_here():
    print("test_anchor_is_import_safe_here")
    # This file's own import already proved it, but state the invariant so the
    # reason is on the record: importing must not depend on ORCH existing.
    check(True, f"_roots imported with ORCH {'present' if ORCH.is_dir() else 'ABSENT'} "
                f"on disk ({ORCH})")


def main():
    test_tasks_root_anchor_shape()
    test_no_import_time_assert_on_the_tasks_root()
    test_anchor_is_import_safe_here()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all _roots anchor tests passed")


if __name__ == "__main__":
    main()
