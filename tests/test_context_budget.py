#!/usr/bin/env python3
"""The always-on context budget (D-22). Run as `python3 test_context_budget.py`;
exits non-zero on failure. No pytest, no runner.

WHAT THIS GUARDS, AND WHY IT IS A TEST RATHER THAN A HABIT
---------------------------------------------------------
Three files are read into EVERY agent of EVERY run, on every turn, before any
work happens: this repository's `CLAUDE.md`, the auto-memory index
`.touch/memory/MEMORY.md`, and the ten shipped skills' `description:`
frontmatter values. Nothing else in the tree is charged that way — a `SKILL.md`
body is paid only when the skill fires, a doc under `plugin/touch/docs/` only
when someone opens it.

Left ungated, that prefix grew +71% in five days and reached 28-36% of a run's
bill. It is the single largest token line item in the project and it is pure
editing to fix, which is exactly the shape of thing that never gets fixed
without a gate. So this file fails the build the way `tests/test_stdlib_only.py`
does: loudly, on a number, with the remedy printed.

THE ESTIMATOR
-------------
chars/4 over BYTES — `aggregator.costs.BYTES_PER_TOKEN`, calibrated once and
pinned there. Applied to bytes rather than decoded text on purpose:
`.touch/memory/MEMORY.md` is written by another process (the CLI's auto-memory
writer) and HAS been observed on this machine truncated mid-write, with a
partial multi-byte character at EOF. A budget guard that raises
`UnicodeDecodeError` on a torn read is a guard that goes red for a reason that
has nothing to do with the budget.

The estimate is deliberately crude and deliberately shared: `aggregator/costs.py`
`--baseline` measures the same three sources with the same function and prints
the same totals, so `scripts/release.sh`, a developer at a terminal and this
test cannot disagree about what the number is. Two estimators would be two
numbers.

THE CONTRACT WITH `aggregator/costs.py`
---------------------------------------
`costs.declared_budgets()` parses THIS file with `ast` — never imports it — and
reads exactly three module-level names, which are also the names in
`costs.BUDGET_KEYS`:

    CLAUDE_MD_BUDGET_TOKENS   MEMORY_BUDGET_TOKENS   SKILLS_BUDGET_TOKENS

They must stay plain integer assignments at module level. A PARTIAL declaration
is not a ceiling — the reader says so and falls back to its caller's number —
and an unknown `*_BUDGET_TOKENS` name is ignored rather than summed, so adding a
`TOTAL_BUDGET_TOKENS` here would be silently dropped there. Do not add one.

WHAT IS DELIBERATELY *NOT* BUDGETED
-----------------------------------
The `CLAUDE.md` in the directory ABOVE this checkout is also always-on, and it
is OUT of this repository's write scope: gating a release on a file nobody here
may edit is a gate that gets bypassed. It is reported, never asserted — this
file prints its size when it can see it, and never fails on it.

ONE PLANE THIS TEST CANNOT FIX
------------------------------
`.touch/memory/MEMORY.md` is only writable by the main terminal agent or the
flag-gated HTTP write plane: the scope guard denies subagent `Write`/`Edit`
under `.touch/memory/` (G14). So when the memory arm goes red, the remedy line
says who can act on it. The rule the index is held to — at most 20 entries, one
line each, newest first — is stated in `plugin/touch/docs/memory-home.md`,
which a subagent MAY edit.
"""
import os
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _roots import PAYLOAD, REPO                        # noqa: E402

# The one estimator, imported from the module that owns it (GD-D10: one
# derivation home). `aggregator/` is a package under the payload root, which is
# not on `sys.path` for a test run out of `tests/` — so the payload root goes on
# explicitly, the same hop `PYTHONPATH=plugin/touch` makes at a shell.
sys.path.insert(0, str(PAYLOAD))
from aggregator.costs import (                          # noqa: E402
    BUDGET_KEYS, description_text, estimate_tokens, skill_descriptions)

CLAUDE_MD = REPO / "CLAUDE.md"
MEMORY_MD = REPO / ".touch" / "memory" / "MEMORY.md"
SKILLS = PAYLOAD / "skills"
#: Out of write scope (see the module docstring): reported, never asserted.
ENCLOSING_CLAUDE_MD = REPO.parent / "CLAUDE.md"

# ---------------------------------------------------------------------------
# THE BUDGETS. `aggregator.costs.declared_budgets()` reads exactly these three
# names out of this file, by `ast`, and sums them for the release gate. Keep
# them plain module-level integer assignments; add no fourth `*_BUDGET_TOKENS`
# name (an unknown one is ignored there, so it would be a number nothing reads).
# ---------------------------------------------------------------------------

#: `CLAUDE.md`. Was 8,394 tok (33,579 B) before D-22 split the run-folder
#: inventory, the memory-home essay and the settings history out into
#: `plugin/touch/docs/`. The ceiling is the plan's number, not the measurement:
#: it has to leave room for a paragraph without becoming a licence for a page.
#:
#: D-22 named TWO targets for that split — "≤ ~12 KB" and "≤ 6,000 tok" — and
#: they disagree by about 2x: at `BYTES_PER_TOKEN = 4`, 12 KB is ~3,000 tok, so
#: no single file can satisfy both. The TOKEN figure governs, and the byte
#: target was consciously dropped rather than missed: tokens are the unit the
#: estimator, `scripts/release.sh`'s regression gate and this test all share,
#: and the one the item's own Test paragraph makes executable. Recorded here so
#: a reviewer of the merged change-set finds the decision instead of
#: re-litigating it from the file's byte count — which this test prints on every
#: run and no comment should try to keep up with.
CLAUDE_MD_BUDGET_TOKENS = 6000

#: The auto-memory index. Small today (~220 tok) and capped low on purpose —
#: the index is a table of contents, and the moment it starts carrying detail
#: it is a second CLAUDE.md that nobody is guarding.
MEMORY_BUDGET_TOKENS = 800

#: The SUM of the ten skills' `description:` values. Measured ~914 tok by this
#: estimator; `claude plugin details touch` reports ~1,261 for the same set with
#: the real tokenizer and the per-skill metadata included. Both numbers are
#: honest about different things — the ceiling is set on THIS estimator, which
#: is the one every consumer of it uses.
SKILLS_BUDGET_TOKENS = 1400

failures = []
skips = []
notes = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def skip(msg):
    print(f"  SKIP: {msg}")
    skips.append(msg)


def note(msg):
    print(f"  note: {msg}")
    notes.append(msg)


def check_budget(label, tokens, size, ceiling, remedy):
    """One budget arm, with the remedy printed ONLY when it is needed.

    `check()` prints its message on both paths, which would put "over budget,
    move a section out" on a green line — a suite that tells you to act when it
    is telling you nothing is wrong is a suite people stop reading.
    """
    ok = tokens <= ceiling
    check(ok, f"{label} within its {ceiling:,} tok budget "
              f"(measured {tokens:,} tok / {size:,} B)")
    if not ok:
        print(f"        remedy: {remedy}")


def measure(path):
    """`(tokens, bytes)` for one always-on file, or None when it is absent.

    Bytes, not text: see the module docstring on torn reads.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return (estimate_tokens(raw), len(raw))


def test_claude_md_is_within_budget():
    print("test_claude_md_is_within_budget")
    measured = measure(CLAUDE_MD)
    if measured is None:
        check(False, f"{CLAUDE_MD.relative_to(REPO)} is readable")
        return
    tokens, size = measured
    check_budget("CLAUDE.md", tokens, size, CLAUDE_MD_BUDGET_TOKENS,
                 "move a section into plugin/touch/docs/ and leave a pointer. "
                 "Do not raise this number without a decision — it is the "
                 "prefix every agent of every run re-reads on every turn")


def test_memory_index_is_within_budget():
    print("test_memory_index_is_within_budget")
    measured = measure(MEMORY_MD)
    if measured is None:
        # A clean checkout has the tracked subtree; a packaged/unpacked copy
        # does not carry `.touch/` at all. Absent is not a verdict.
        skip(f"{MEMORY_MD.relative_to(REPO)} is absent — no memory index to "
             f"budget in this tree")
        return
    tokens, size = measured
    check_budget("the memory index", tokens, size, MEMORY_BUDGET_TOKENS,
                 "the MAIN TERMINAL AGENT (not a subagent — G14 denies it) "
                 "trims it to 20 one-line entries, newest first, per "
                 "plugin/touch/docs/memory-home.md")


def test_skill_descriptions_are_within_budget():
    print("test_skill_descriptions_are_within_budget")
    described, count = skill_descriptions(REPO)
    check(count > 0,
          f"the skill descriptions were found under {SKILLS.relative_to(REPO)} "
          f"(found {count})")
    if not count:
        return
    tokens = estimate_tokens(described)
    check_budget(f"the {count} always-on skill descriptions", tokens, described,
                 SKILLS_BUDGET_TOKENS,
                 "shorten a `description:` — the SKILL.md BODY is free, it is "
                 "read only when the skill fires")


def test_every_skill_declares_a_description():
    """A skill with no `description:` is invisible to the model AND free.

    Worth its own arm because the budget arm above would go *greener* as
    descriptions vanish: a suite that rewards deleting the thing it measures is
    measuring the wrong direction.
    """
    print("test_every_skill_declares_a_description")
    try:
        names = sorted(p.name for p in SKILLS.iterdir() if p.is_dir())
    except OSError:
        check(False, f"{SKILLS.relative_to(REPO)} is a directory")
        return
    missing = []
    for name in names:
        skill = SKILLS / name / "SKILL.md"
        if not skill.is_file():
            missing.append(f"{name} (no SKILL.md)")
            continue
        if description_text(skill.read_text(encoding="utf-8",
                                           errors="replace")) is None:
            missing.append(f"{name} (no description:)")
    check(not missing,
          f"every skill directory declares an always-on `description:` "
          f"(bad: {missing})")


def test_budget_names_match_the_reader():
    """The three names `aggregator/costs.py` parses out of THIS file.

    The coupling is by NAME and by `ast`, not by import, so a rename here is
    silent there: the reader reports "incomplete" and the release gate quietly
    falls back to its caller's ceiling — green, and no longer measuring the
    budget. Asserting the contract in the file that owns the numbers is what
    makes that failure loud.
    """
    print("test_budget_names_match_the_reader")
    declared = {
        "CLAUDE_MD_BUDGET_TOKENS": CLAUDE_MD_BUDGET_TOKENS,
        "MEMORY_BUDGET_TOKENS": MEMORY_BUDGET_TOKENS,
        "SKILLS_BUDGET_TOKENS": SKILLS_BUDGET_TOKENS,
    }
    check(set(BUDGET_KEYS) == set(declared),
          f"this file declares exactly the names aggregator.costs reads "
          f"(here: {sorted(declared)}, reader: {sorted(BUDGET_KEYS)})")
    for name, value in sorted(declared.items()):
        check(isinstance(value, int) and not isinstance(value, bool)
              and value > 0,
              f"{name} is a positive int ({value!r})")
    # …and they are literally assigned at module level, which is the only shape
    # `ast`-parsing can see. A computed value (`6000 if X else 7000`) parses to
    # something that is not `ast.Constant` and is skipped by the reader.
    source = Path(__file__).read_text(encoding="utf-8")
    for name in declared:
        check(re.search(rf"(?m)^{name}(?:\s*:\s*int)?\s*=\s*\d+\s*$", source)
              is not None,
              f"{name} is a plain module-level integer literal assignment")


def test_the_enclosing_claude_md_is_reported_not_gated():
    """Out of write scope, so it is a printed fact and never a failure."""
    print("test_the_enclosing_claude_md_is_reported_not_gated")
    measured = measure(ENCLOSING_CLAUDE_MD)
    if measured is None:
        note(f"no CLAUDE.md above {REPO.name} — nothing to report")
        return
    tokens, size = measured
    note(f"{os.path.join('..', 'CLAUDE.md')}: {tokens:,} tok / {size:,} B — "
         f"always-on too, and OUT of this repo's write scope: reported, never "
         f"gated (D-22)")
    check(True, "the enclosing CLAUDE.md is reported, not gated")


def main():
    for t in (test_claude_md_is_within_budget,
              test_memory_index_is_within_budget,
              test_skill_descriptions_are_within_budget,
              test_every_skill_declares_a_description,
              test_budget_names_match_the_reader,
              test_the_enclosing_claude_md_is_reported_not_gated):
        t()
    print()
    # The one-line summary a release transcript can be read for.
    total = 0
    for label, path in (("CLAUDE.md", CLAUDE_MD), ("MEMORY.md", MEMORY_MD)):
        measured = measure(path)
        if measured:
            total += measured[0]
            print(f"{label:<24}{measured[0]:>8,} tok {measured[1]:>9,} B")
        else:
            print(f"{label:<24}{'absent':>8}")
    described, count = skill_descriptions(REPO)
    total += estimate_tokens(described)
    print(f"{f'skill descriptions ({count})':<24}"
          f"{estimate_tokens(described):>8,} tok {described:>9,} B")
    ceiling = (CLAUDE_MD_BUDGET_TOKENS + MEMORY_BUDGET_TOKENS
               + SKILLS_BUDGET_TOKENS)
    print(f"{'TOTAL':<24}{total:>8,} tok  (declared ceiling {ceiling:,})")
    print()
    for message in skips:
        print(f"skipped: {message}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"the always-on context budget holds ({len(skips)} skipped)")


if __name__ == "__main__":
    main()
