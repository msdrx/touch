#!/usr/bin/env python3
"""Static guards on the documentation set (R-05, R-33, R-38, R-40, R-57).

Run as `python3 tests/test_docs.py`; exits non-zero on the first failure. No
pytest, no runner. `test_shell.py` (monitoring module) is the sibling of this
file for the *module's* docs; this one guards the repo's own.

These are assertions about **source text**, because prose is never executed and
therefore never fails on its own. Each guard exists because the claim it pins
was once wrong in this repo:

* the `omnigent` claim (both docs said the run history came from another
  project; every `orch-config.json` proves otherwise),
* the bare "D8" anchor (one label, two decisions — R-38),
* "usage is copied onto every split record" (it grows; `$max` is the only
  correct fold — R-38),
* an unqualified "pause" promise (GD-4 forbids rendering a verb that cannot be
  honest),
* a `0.0.0.0` database and a published 27017 (GD-27),
* a layout table describing the tree as it was two migrations ago, and a hook
  path that had not existed since the plugin absorbed it (DUP-MAP-10 /
  PLUGIN-RUNTIME-13 — the middle section below).

A guard that only asserted the *presence* of good text would pass while the bad
text sat next to it, so the negative halves are the load-bearing ones.

Guards that read the gitignored run history (`PLAN`, `PROBES`, `REGISTER`)
SKIP with a printed reason when the run folder is not on disk, so this file is
green on a clean checkout instead of crashing (RENAME-SCOPE-15 /
AGGREGATOR-VISUAL-9). `tests/run_all.sh` reports the skip counts, so a green
suite never silently means "the files vanished".

The last two sections guard the PACKAGING documents (items 11 and 12 of the
plugin-pack plan) rather than this repo's own:

* `plugin/touch/README.md` is the trust surface a stranger reads before
  installing a plugin that ships a `PreToolUse` hook — the `/plugin` UI never
  renders it, so its install and update command lines are the only copy the
  user gets, verbatim, and its disclosures are the only ones there are;
* `plugin/touch/CHANGELOG.md`'s top entry must name the version in
  `.claude-plugin/plugin.json`, because that field is the ONLY place Touch
  declares one and a changelog that leads with a different number is a
  changelog for a release nobody can install;
* `scripts/release.sh` IS the release checklist (there is deliberately no
  RELEASE.md), so the guards here are that it exists, is executable, uses no
  `jq`, and hand-assembles nothing — it never `cp`s a tree anywhere, because
  publishing is a `git push` of THIS repository and the stage exists only to be
  scanned. Those are the ways that file stops being the thing it claims to be.

The packaging guards use `have_plugin()`, which — unlike `have()` — never
skips: a payload document that is missing FAILS, whatever else is on disk.
There is deliberately no "the shipping subtree is absent" branch to balance
`have()`'s, because that state cannot reach this file. Post-GD-U1 the subtree
is not an optional build product, it is the source, and the import of
`tests/_roots.py` a few lines down asserts the canonical trees exist; a tree
without the payload therefore dies LOUDLY at import, before any guard runs,
which is the bargain `_roots` exists to make. `scripts/release.sh` gets no skip
arm either — it is a repo-only file that ships in no payload, so "absent" is a
failure and never a skip.

The `jq` and `cp -r` guards read the script's CODE only — comments dropped,
whole-line and trailing alike, and the body of the checklist heredoc dropped
with them: the file names both in prose precisely to say it uses neither, and a
guard that failed on that prose would be a guard against writing the reason
down.

Item 12's last three guards are the only ones in this file that RUN anything:
`release.sh` against a throwaway fixture repo — under `--check`, with a
token-named blob planted in the fixture's history, and once for real against an
on-disk bare `origin` — asserting what each step printed and, for the real run,
that the branch actually landed on the remote. Source-text guards cannot see a
gate that is spelled plausibly and still answers wrongly: a bare `git config
user.email` reads the whole global cascade while reporting a per-repo answer,
and it survived three review rounds behind a substring check. Nor can they see
WHICH path an invocation expanded to, which is why the fixture's `claude` stub
logs its argv instead of only exiting 0 (the repo-root form of `claude plugin
tag` fails in reality and passes against a silent stub). The fixture exists
because the script's step 2 runs `tests/run_all.sh`, which runs this file:
pointing it at this repository would recurse forever.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The canonical trees are named through `tests/_roots.py`, never by a literal
# under REPO: GD-U1 moved `docs/` into the payload and this is the single flip
# point. `README.md`, `CLAUDE.md`, `CONTRIBUTING.md` and `inception.md` stay
# repo-root files — they are development documents that deliberately do not
# ship — so they keep their REPO anchors.
sys.dont_write_bytecode = True   # no .pyc droppings next to the tests (house
                                 # pattern, item 06; this file imports only
                                 # `tests/_roots.py`, nothing under the payload)
from _roots import CATALOG, PAYLOAD, SRC   # noqa: E402  (the bytecode flag
                         # must precede the first import, so these cannot sit
                         # with the rest)

README = REPO / "README.md"
CLAUDE = REPO / "CLAUDE.md"
CONTRIBUTING = REPO / "CONTRIBUTING.md"
INCEPTION = REPO / "inception.md"
CONTROL_DOC = SRC / "docs/control-semantics.md"
MONGO_DOC = SRC / "docs/mongo.md"

# The run-history artifacts. `.claude/local-orchestrators/` is gitignored and
# untracked (2026-07-27 amendment), so these files exist in a working tree that
# ran the orchestrations and in NO clean checkout — not `git archive HEAD`, not
# a fresh clone, not a packaged plugin. Reading them unguarded made this file
# crash with FileNotFoundError everywhere but this machine, which is the one
# thing a before/after gate may not do.
RECON = REPO / ".claude/local-orchestrators/touch-full-recon"
PLAN = RECON / "plan/touch-full-recon-plan.md"
PROBES = RECON / "report/probes.md"
REGISTER = RECON / "plan/findings-register.md"

# The packaging set (items 11 + 12). BOTH halves are guaranteed present, which
# is why neither has a skip path: the shipping subtree is the source after
# GD-U1 (`_roots` asserts it at import, above), and `scripts/release.sh` is a
# repo-only file that is simply expected to be there. `have_plugin()` below
# therefore reports a missing payload document as a FAILURE — that, and not a
# second skip rule, is the whole difference from `have()`.
#: Named through `_roots`, never spelled here (C-03): `tests/_roots.py` is the
#: ONE anchor for the canonical trees, and a second literal is a second thing
#: to forget the next time the layout moves.
PLUGIN = PAYLOAD
#: REPO-relative spellings of the two publication-critical paths, for the
#: release fixture below: it builds a throwaway repo with the same shape, and
#: the shape has to come from the same anchor as the real one or the fixture
#: silently stops testing this layout.
PLUGIN_REL = PAYLOAD.relative_to(REPO)
CATALOG_REL = CATALOG.relative_to(REPO)
PLUGIN_README = PLUGIN / "README.md"
PLUGIN_CHANGELOG = PLUGIN / "CHANGELOG.md"
PLUGIN_MANIFEST = PLUGIN / ".claude-plugin/plugin.json"
RELEASE_SH = REPO / "scripts/release.sh"

failures = []
skips = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def skip(msg):
    print(f"  SKIP: {msg}")
    skips.append(msg)


def have(path):
    """True when a run-history artifact can be read; skips (or fails) if not.

    The distinction is deliberate: an ABSENT run folder is a clean checkout and
    skips with a printed reason, while a run folder that IS on disk without its
    authoritative artifact is a real regression and still fails. "Green" must
    never quietly mean "the files vanished".
    """
    if path.is_file():
        return True
    rel = path.relative_to(REPO)
    if not RECON.is_dir():
        skip(f"{rel}: run history is gitignored — absent on a clean checkout")
        return False
    check(False, f"{rel} exists (its run folder is on disk, so it must be)")
    return False


def have_plugin(path):
    """`have()`'s counterpart for the shipping subtree — and it never skips.

    A missing payload document is always a real failure, because `plugin.json`
    claims a README and a LICENSE that a consumer will look for. There is
    deliberately no `PLUGIN.is_dir()` branch mirroring `have()`'s skip: `from
    _roots import SRC` at the top of this module asserts the canonical trees
    and dies at import if they are absent, and since GD-U1 that tree and this
    one are the same directory (`tests/_roots.py`: `SRC == PAYLOAD ==
    plugin/touch/`). "No payload at all" therefore cannot reach this function,
    and writing the branch anyway would be unreachable code whose printed
    reason nobody could ever see. After GD-U1 the subtree is not an optional
    build product, it is the source, and "the payload is missing" must be loud
    rather than silently skipped.

    The bool is what callers use to stop before reading a file that is not
    there, so one absent document costs one FAIL line rather than a traceback.
    It sits here beside `have()` rather than under the Item 11 banner it was
    written for, because the GD-U1 section above that banner calls it too: the
    file's convention is that a helper is introduced before the first section
    that uses it, and it now has two.
    """
    if path.is_file():
        return True
    check(False, f"{path.relative_to(REPO)} exists (the payload is the source, "
                 f"not an optional build product)")
    return False


def read(path):
    return path.read_text(encoding="utf-8", errors="replace")


def paragraphs(text):
    """Blank-line-separated blocks. Negation guards work per block, not per
    line: a wrapped sentence puts 'never' and the thing it forbids on
    different lines, and a line-local check would then fail on correct prose."""
    return re.split(r"\n\s*\n", text)


def flatten(text):
    """`text` with every run of whitespace collapsed to a single space.

    Prose wraps, and a wrapped phrase is still the phrase. CLAUDE.md's GD-U5
    note really does read "`.claude/settings.json` no\\nlonger carries a
    `hooks` block", so a literal `"no longer" in block` misses it — and a guard
    whose exemption vocabulary cannot survive a line break reports the opposite
    of the truth on correct text. Every phrase-level match flattens first.
    """
    return re.sub(r"\s+", " ", text)


def guard_blocks(text):
    """`paragraphs()`, except a markdown TABLE is split into its rows.

    A table has no blank lines in it, so `paragraphs()` returns the whole thing
    as one block — and any per-block exemption then covers every row at once.
    That is how CLAUDE.md's layout table (one row of which says "moved")
    silently exempted the other twenty from the dead-path guard. A `|`-row
    carries its own claim and can carry its own reason, so it is the right
    unit; everything else stays a paragraph, because prose wraps and a
    line-local check would then fail on correct text.
    """
    for para in paragraphs(text):
        lines = para.splitlines()
        if sum(1 for ln in lines if ln.lstrip().startswith("|")) >= 2:
            for ln in lines:
                yield ln
        else:
            yield para


# --------------------------------------------------------------- R-05: CLAUDE.md
def test_claude_md_pointers_and_no_omnigent():
    print("test_claude_md_pointers_and_no_omnigent")
    text = read(CLAUDE)
    for token in ("inception.md", "touch-aggregator-plan.md",
                  "touch-full-recon-plan.md", "plugin/touch/skills/orchestrate"):
        check(token in text, f"CLAUDE.md points at {token}")
    check("omnigent" not in text.lower(),
          "CLAUDE.md does not claim the run history came from `omnigent`")
    check("touch-mongo-live-plan.md" in text,
          "CLAUDE.md points at the amendment plan too (it outranks the normative one)")


def test_claude_md_true_inventory():
    print("test_claude_md_true_inventory")
    text = read(CLAUDE)
    check("no application source yet" not in text.lower(),
          "CLAUDE.md no longer says the repo has no application source")
    for token in ("aggregator/", "touch-visual/", "tests/", "docs/"):
        check(token in text, f"CLAUDE.md's inventory names {token}")
    for token in (".claude/settings.json", "statusline.sh", "jq"):
        check(token in text, f"CLAUDE.md names {token} (the files that shape the session)")


def test_claude_md_serve_blocks_and_ports():
    print("test_claude_md_serve_blocks_and_ports")
    text = read(CLAUDE)
    check("8931" in text and "8932" in text,
          "CLAUDE.md labels both serve blocks (legacy 8931, Touch 8932)")
    check("reserved" in text.lower(), "CLAUDE.md says the ports are reserved, not occupied")
    check("127.0.0.1:8932" in text or "binds 127.0.0.1" in text,
          "CLAUDE.md states the loopback-by-default bind (GD-13)")


# ------------------------------------------------- R-40 / GD-1: daemon lifecycle
def test_claude_md_watcher_lifecycle():
    print("test_claude_md_watcher_lifecycle")
    text = read(CLAUDE)
    check("stop its watcher" in text,
          "CLAUDE.md carries the 'when a run ends, stop its watcher' rule")
    check("ORCH_STATE_DIR" in text and "inside the paths being" in text,
          "CLAUDE.md states GD-1's SCOPED commit gate (watchers inside the commit path set)")
    if have(PLAN):
        check("inside the paths being" in read(PLAN),
              "the plan's GD-1 carries the same scoping (R-40)")


# --------------------------------------------------------- R-57 / GD-21: pymongo
def test_claude_md_names_the_pymongo_exception():
    print("test_claude_md_names_the_pymongo_exception")
    text = read(CLAUDE)
    check("pymongo" in text, "CLAUDE.md names pymongo")
    check("4.17.0" in text, "CLAUDE.md pins the version")
    for token in ("mongo_store.py", "mirror.py"):
        check(token in text, f"CLAUDE.md names {token} as an allowed importer")
    check("GD-21" in text, "CLAUDE.md cites GD-21 as the rule's source")


# ------------------------------------------------------------- R-38: the anchors
def test_plan_d8_is_split():
    print("test_plan_d8_is_split")
    if have(PLAN):
        plan = read(PLAN)
        check("D8.1" in plan, "the plan labels the stack decision D8.1")
        check("D8.2" in plan, "the plan labels the journal-`result` clause D8.2")
    check("D8.1" in read(INCEPTION) and "D8.2" in read(INCEPTION),
          "inception.md uses the split labels too")


def test_inception_usage_correction():
    print("test_inception_usage_correction")
    text = read(INCEPTION)
    check("copied onto every split record" not in text,
          "inception.md no longer claims usage is copied onto every split record")
    check("running counter" in text, "inception.md describes usage as a running counter")
    check("$max" in text, "inception.md states the `$max` fold")


def test_inception_truths():
    print("test_inception_truths")
    text = read(INCEPTION)
    check("omnigent" not in text.lower() or "was **false**" in text or "was false" in text,
          "inception.md does not repeat the omnigent claim as fact")
    check("29.5" in text and "316" in text,
          "inception.md carries the deduped token figure (≈29.5M in / 316k out)")
    check("AUDIT-13" in text, "inception.md names the source of that figure")
    check("resumeFromRunId" in text and "rejected" in text.lower(),
          "inception.md records that resumeFromRunId is rejected as 'restart' (GD-4)")


def test_probes_recorded():
    print("test_probes_recorded")
    if not have(PROBES):
        return
    text = read(PROBES)
    check("2026-07-26" in text, "probes carry the date they were run")
    check(text.count("claude ") >= 3, "probes quote the commands they ran")
    for token in ("hot-reload", "agents --json", "run_in_background", "pymongo",
                  "40573", "$jsonSchema"):
        check(token in text, f"probes.md records the {token} result")
    check("2.1.220" in text, "probes.md pins the CLI version they were run against")


# ------------------------------------------------ R-33 / R-05: README's promises
def test_readme_verb_table():
    print("test_readme_verb_table")
    text = read(README)
    for verb in ("start", "terminate", "stop", "restart", "pause"):
        check(re.search(rf"\*\*{verb}", text) is not None,
              f"README's verb table has a **{verb}** row")
    check("deterministic" in text and "model-mediated" in text,
          "README distinguishes deterministic from model-mediated verbs")
    check("resumeFromRunId" in text,
          "README states which meaning of restart is rejected (GD-4)")


def test_readme_pause_is_always_qualified():
    print("test_readme_pause_is_always_qualified")
    qualifiers = ("does not exist", "not shipped", "deferred", "hook gate",
                  "cannot be honest")
    bad = []
    for para in paragraphs(read(README)):
        if "pause" not in para.lower():
            continue
        # Quoted history (the verbatim original intent) is exempt: it is
        # labelled as the source of the requirement, not as a promise.
        if all(ln.strip().startswith(">") for ln in para.splitlines() if ln.strip()):
            continue
        if not any(q in para.lower() for q in qualifiers):
            bad.append(para.strip()[:90])
    check(not bad, f"every unquoted mention of pause carries its status (bad: {bad})")
    check("Original intent" in read(README),
          "the verbatim original intent is kept, and labelled as such")


def test_readme_run_section():
    print("test_readme_run_section")
    text = read(README)
    check("python3 -m aggregator.server" in text, "README says how to start Touch")
    check("token" in text.lower(), "README explains the per-boot token")
    check("run_all.sh" in text, "README says how to run the tests")
    check("control-semantics.md" in text, "README points at the verb-ladder doc")


def test_readme_mongo_disposition():
    print("test_readme_mongo_disposition")
    text = read(README)
    check("separate collections for separate session" in text.lower(),
          "README states the per-session-collection ask in the user's own words")
    check("declined" in text.lower(), "README says it was declined")
    check("sessionId" in text, "README says what replaced it (an indexed field)")
    check("docs/mongo.md" in text, "README points at the database doc")


# ------------------------------------------------------ GD-27: the network shape
def test_no_published_mongo_port():
    print("test_no_published_mongo_port")
    negations = ("never", "not ", "n't", "do not", "refus", "unauthenticated", "wrong")
    for path in (README, CLAUDE, INCEPTION, CONTROL_DOC, MONGO_DOC):
        for para in paragraphs(read(path)):
            low = para.lower()
            if "sbx ports" in low and "27017" in low:
                check(any(n in low for n in negations),
                      f"{path.name}: every `sbx ports … 27017` mention is a prohibition")
            if "0.0.0.0" in low and ("27017" in low or "mongod" in low):
                check(any(n in low for n in negations),
                      f"{path.name}: no 0.0.0.0 database example without a prohibition")
    check("127.0.0.1:27017:27017" in read(MONGO_DOC),
          "docs/mongo.md carries the loopback recipe")
    check("127.0.0.1:27017" in read(README) or "loopback" in read(README).lower(),
          "README references the loopback bind")
    check("27017" in read(CLAUDE) and "never publish" in read(CLAUDE).lower(),
          "CLAUDE.md carries the never-publish-27017 rule")


# ----------------------------------------------------- R-33: control-semantics.md
def test_control_semantics_doc():
    print("test_control_semantics_doc")
    text = read(CONTROL_DOC)
    for token in ("owned", "cooperating", "observed"):
        check(token in text, f"the doc defines the {token} session class (GD-6)")
    for token in ("start", "terminate", "stop", "restart", "pause"):
        check(token in text.lower(), f"the doc covers {token}")
    check("run-level" in text and "per-agent" in text,
          "the doc distinguishes run-level from per-agent stop (amended GD-8)")
    check("taskId" in text, "the doc names the Workflow run-level stop handle")
    check("403" in text, "the doc states that observed sessions 403 server-side")
    check("probes.md" in text, "the doc cites the probe evidence for the hook gate")


def test_docs_agree_on_restart():
    print("test_docs_agree_on_restart")
    # One meaning, in every document that mentions it (GD-4).
    for path in (README, CONTROL_DOC, INCEPTION):
        text = read(path)
        check("only:[ids]" in text or "only:[" in text,
              f"{path.name} states restart as re-invoke with only:[ids]")


def test_register_is_reachable():
    print("test_register_is_reachable")
    if not have(REGISTER):
        return
    check("findings-register" in read(REGISTER).lower() or "R-06" in read(REGISTER),
          "the register identifies itself")


# ============================================================================
# GD-U1 / GD-U4 / GD-U5 — the layout claims, after `plugin/touch/` went canonical
# ============================================================================

#: The documents that TELL A READER WHERE TO LOOK. A path named in one of these
#: is a direction someone will follow, which is why they are guarded as a set
#: and why the set is exactly this.
#:
#: `inception.md` is deliberately absent: it is a DATED snapshot ("Updated
#: 2026-07-26") of what was verified about the substrate, and its `.claude/`
#: paths are the record of a tree that existed then. A guard that forced it
#: current would destroy the one thing it is for.
#: The PAYLOAD documents are absent from this tuple, and that is no longer an
#: escalated gap: C-11 fixed the three lines that pointed a consumer at
#: `.claude/shared/monitoring/` (monitoring.md's self-location and its
#: state-dir mention, plus `touch-visual/app.js`'s caching-precedent comment),
#: and `test_payload_docs_run_from_an_installed_copy()` below guards the whole
#: payload for the literal instead of guarding three lines by name — a closed
#: SET beats a list of files someone has to remember to extend.
#:
#: They are deliberately NOT folded into DIRECTION_DOCS, because the arms this
#: tuple feeds are repo-development claims: `test_entry_points_are_the_wrappers`
#: requires `PYTHONPATH=plugin/touch`, which is the DEV checkout's plugin root
#: and exactly the wrong instruction to ship — a payload doc speaks in
#: `${CLAUDE_PLUGIN_ROOT}` terms (GD-C11), so it gets its own PYTHONPATH arm
#: with the payload's vocabulary rather than this one's.
DIRECTION_DOCS = (README, CLAUDE, CONTRIBUTING)

#: Paths that stopped existing when the plugin became the canonical home. Each
#: was a live instruction in a committed document at some point, and each now
#: resolves to nothing: the hook is `plugin/touch/hooks/`, the skills are
#: `plugin/touch/skills/`, the monitoring module is
#: `plugin/touch/shared/monitoring/`.
#:
#: Matched with the trailing slash STRIPPED, because a doc that writes
#: `` `.claude/hooks` `` or "the `.claude/skills` directory" directs a reader
#: exactly as hard as one that writes the slash. None of the three stems has a
#: live homonym in this tree (`.claude/` still holds `settings.json`,
#: `statusline.sh`, `shared/scripts/` and the untracked run history — no
#: `hooks`, no `skills`, no `shared/monitoring`), so the wider match costs no
#: false positives.
DEAD_PATHS = (".claude/hooks/", ".claude/skills/", ".claude/shared/monitoring/")

#: Words that mark a mention as HISTORY rather than an instruction. The
#: exemption is the same bargain `strip_comment()` makes further down: a guard
#: that cannot tell "run this" from "this is where it used to live" forbids
#: writing down why the layout changed, and that reason is exactly what stops
#: the next reader from rebuilding the old one.
#:
#: Kept DELIBERATELY narrow. `"until "` and `"moved"` were in this tuple and
#: cost the guard most of its reach: they are ordinary words, and one of them
#: appearing anywhere in a block exempted the block. Every mention that
#: legitimately needs the exemption today (`CLAUDE.md:74`, `CONTRIBUTING.md`'s
#: pinned-copy history) says "gone" or "no longer" outright, so nothing is
#: paid for the narrowing — and a sentence that cannot spell out that a path is
#: retired probably is not saying so.
RETIRED = ("gone", "no longer", "used to", "does not exist")


def test_direction_docs_name_no_dead_claude_path():
    print("test_direction_docs_name_no_dead_claude_path")
    bad = []
    for path in DIRECTION_DOCS:
        for para in guard_blocks(read(path)):
            low = para.lower()
            for dead in DEAD_PATHS:
                if dead.rstrip("/") in low and not any(r in low for r in RETIRED):
                    bad.append(f"{path.name}: `{dead}` in {para.strip()[:70]!r}")
    check(not bad,
          f"no document that directs a reader names a path GD-U1 retired, "
          f"except to say it is retired (bad: {bad})")


def test_claude_md_layout_table_is_current():
    print("test_claude_md_layout_table_is_current")
    text = read(CLAUDE)
    # DUP-MAP-10: the table listed only the pre-plugin trees, so "why are there
    # two copies?" had no answer in the file an agent reads first.
    #
    # Asserted on the ROW, not on the file. `token in text` said "the layout
    # table names X" while checking nothing of the kind: `"scripts/"` was
    # satisfied by the unrelated `shared/scripts/*-sox-installation.sh` mention
    # in the `.claude/` row, and `"plugin/"` by any of the forty
    # `plugin/touch/...` references elsewhere in the file — so both rows could
    # be deleted with the arm still green, which is precisely the DUP-MAP-10
    # regression it was written for. `guard_blocks()` already splits a markdown
    # table into rows; a row's FIRST cell is the path it documents.
    rows = [b.strip() for b in guard_blocks(text) if b.lstrip().startswith("|")]
    for token in ("plugin/", "scripts/", "tests/monitoring/",
                  "plugin/touch/aggregator/", "plugin/touch/shared/monitoring/"):
        check(any(r.startswith(f"| `{token}`") for r in rows),
              f"CLAUDE.md's layout table has a row whose subject is {token}")
    check("plugin/touch/hooks/orch_scope_guard.py" in text,
          "CLAUDE.md names the hook at the path that exists (PLUGIN-RUNTIME-13)")
    check(".claude/hooks/orch_scope_guard.py" not in text,
          "CLAUDE.md no longer names the retired hook path")
    # The same defect one directory over: the hook MANIFEST is
    # `plugin/touch/hooks/hooks.json`, sibling to the script — NOT
    # `.claude-plugin/hooks.json`, which has never existed and which
    # `plugin.json` (no `hooks` key) does not imply. GD-U5's claim is "there is
    # exactly one registration, and it is here", so the one thing a reader will
    # open to check must be at the path the sentence gives them.
    # One property per `check()`: folding the positive and the negative into
    # one `and` makes a failure message that cannot say which half went red.
    # The negative is not repeated here — the loop below already asserts it for
    # CLAUDE.md, because CONTRIBUTING.md carries the same layout table and made
    # the same mistake, so it is checked across every document that directs.
    check("hooks/hooks.json" in text,
          "CLAUDE.md names hooks.json at the path that exists (GD-U5)")
    for doc in DIRECTION_DOCS:
        check(".claude-plugin/hooks.json" not in read(doc),
              f"{doc.name} does not invent a hook manifest under "
              f"`.claude-plugin/` (it is `hooks/hooks.json`)")


def test_claude_md_records_the_single_hook_registration():
    print("test_claude_md_records_the_single_hook_registration")
    # GD-U5: the two registrations had the same matcher and fired the hook
    # TWICE per tool call (measured 2 vs 1). The note exists so the next reader
    # does not "fix" the absent `.claude/settings.json` block by restoring it.
    #
    # `"GD-U5" in text and "settings.json" in text` was the first spelling of
    # this arm and it could not fail: both substrings are already in CLAUDE.md
    # independently of the note — `GD-U5` in the `hooks/` layout row,
    # `settings.json` in the `.claude/` row — so the whole paragraph could be
    # deleted with the arm still green. Same defect class as the layout-token
    # arm above. So: locate the note as ONE block, and separately forbid the
    # sentence that would mean the double registration is back.
    text = read(CLAUDE)
    note = [b for b in guard_blocks(text)
            if "GD-U5" in b and "settings.json" in b
            and any(r in flatten(b).lower() for r in RETIRED)]
    check(note,
          "CLAUDE.md carries the GD-U5 note as one block: the plugin's "
          "`hooks/hooks.json` is the single registration and "
          "`.claude/settings.json` no longer carries a `hooks` block")
    # The negative half, which is what actually bites: a document that tells a
    # reader the guard IS registered in `.claude/settings.json` is the
    # regression, half-landed in prose. Exempt a block that says the
    # registration is retired (`RETIRED`, the file's own vocabulary) or states
    # it in the negative — CLAUDE.md's `.claude/` section says "it registers NO
    # hooks", which is the true sentence and must not trip the guard.
    #
    # Checked per SENTENCE, not per block. "Rules that bite" is a bullet list
    # with no blank lines in it, so `guard_blocks()` hands back the whole list
    # as ONE unit — and the true note's "no longer" then exempts every other
    # bullet in it, including a freshly added "the guard is also registered in
    # `.claude/settings.json`". That is the same one-exemption-covers-the-lot
    # failure `guard_blocks()` was written to fix for tables, one construct
    # over; a sentence is the unit that carries this claim, and it is the unit
    # that must carry the reason.
    negated = ("no hooks", "registers no", "not registered", "never registered")
    restored = []
    for doc in DIRECTION_DOCS:
        for sentence in re.split(r"(?<=[.!?])\s+", flatten(read(doc))):
            low = sentence.lower()
            if ".claude/settings.json" not in low or "regist" not in low:
                continue
            if any(r in low for r in RETIRED) or any(n in low for n in negated):
                continue
            restored.append(f"{doc.name}: {sentence.strip()[:70]}")
    check(not restored,
          f"no document says the scope guard is registered in "
          f"`.claude/settings.json` — the plugin's `hooks/hooks.json` is the "
          f"one and only registration (GD-U5) (bad: {restored})")


def test_claude_md_status_sh_fallback_is_the_real_one():
    print("test_claude_md_status_sh_fallback_is_the_real_one")
    text = read(CLAUDE)
    # `status.sh` does NOT spool into its own directory when `ORCH_STATE_DIR`
    # is unset. It resolves the project's tasks root, writes the newest task
    # folder there with a loud warning, and exits 2 when that fails too —
    # deliberately, because "a spool nobody reads is data loss with extra
    # steps" and an installed payload is a version-stamped cache that gets
    # swept. GD-U1 sharpened this rather than softening it: the "module dir"
    # is now INSIDE the payload, i.e. the exact write `in_plugin_cache()`
    # exists to refuse.
    #
    # The negative is the load-bearing half. CLAUDE.md carried the true ladder
    # in "Rules that bite" AND the retired fallback in the monitoring section,
    # 195 lines apart — two mutually exclusive behaviours for one program —
    # and a positive-only guard passes happily while the false sentence sits
    # next to the true one.
    check("falls back to the module dir" not in text,
          "CLAUDE.md does not promise the retired module-dir spool for `status.sh`")
    check("exits 2" in text,
          "CLAUDE.md states what `status.sh` does when it cannot resolve a task "
          "folder (exit 2 — never a write into the payload)")


def test_entry_points_are_the_wrappers():
    print("test_entry_points_are_the_wrappers")
    # GD-U4 / SINGLE-SOURCE-10: one supported entry point per program. The
    # module-direct form survives in exactly one shape — carrying the
    # PYTHONPATH that makes it work at all now that there is no root package.
    for path in DIRECTION_DOCS:
        text = read(path)
        for cmd in ("touch-serve", "touch-monitor", "touch-watcher"):
            check(cmd in text, f"{path.name} names the `{cmd}` entry point (GD-U4)")
        # Normalise whitespace first, then match, then exempt by looking for
        # the PYTHONPATH assignment ANYWHERE earlier on the line. The obvious
        # spelling — a fixed-width lookbehind on one literal — is wrong twice
        # over: `PYTHONPATH="plugin/touch" python3 …`, a double space or a
        # leading `env ` all become FALSE POSITIVES (the guard fires on correct
        # text), while `python3 -c "import aggregator…"`, the shape CLAUDE.md
        # actually uses for the mirror, is not matched at all — so half the
        # "there is no root package to import" property went unguarded.
        bare = []
        for ln in text.splitlines():
            flat = re.sub(r"\s+", " ", ln)
            m = re.search(r"python3 (?:-\w+ )*(?:-m aggregator|-c [\"']import aggregator)",
                          flat)
            if m is None:
                continue
            if re.search(r"PYTHONPATH=[\"']?plugin/touch", flat[:m.start()]):
                continue
            bare.append(ln.strip())
        check(not bare,
              f"{path.name}: every `python3 -m aggregator…` / `python3 -c "
              f"\"import aggregator…\"` carries `PYTHONPATH=plugin/touch` — "
              f"there is no root package to import (bad: {bare})")
    # test_readme_run_section pins the substring `python3 -m aggregator.server`;
    # GD-U4 keeps it literally true rather than deleting the pin.
    check("PYTHONPATH=plugin/touch python3 -m aggregator.server" in read(README),
          "the README's one module-direct line is the PYTHONPATH form (GD-U4)")


def test_shipped_docs_quote_measured_skill_costs():
    print("test_shipped_docs_quote_measured_skill_costs")
    if not (have_plugin(PLUGIN_README) and have_plugin(PLUGIN_CHANGELOG)):
        return
    # SKILLS-INTEGRATION-11: the count and the token figure were written out in
    # prose in nine places, and 459/"four skills" became false the day the six
    # engineering-practice skills landed. Both numbers are MEASURED claims
    # (`claude --plugin-dir plugin/touch plugin details touch`), the same
    # standard the hook's ~22 ms disclosure is held to two functions up.
    readme = read(PLUGIN_README)
    check("1,257" in readme,
          "the shipped README quotes the re-measured always-on figure")
    check(re.search(r"~459 tokens\s+always-on", readme) is None,
          "the shipped README no longer quotes 459 as the CURRENT always-on cost")
    # The `or "~1,257" in readme` this arm used to carry was satisfied by the
    # substring the arm above already requires, so it could not fail while that
    # one passed — a tautology dressed as a second check. The count claim has to
    # stand on its own.
    check("ten skills" in readme.lower(),
          "the shipped README says how many skills that figure covers")
    for name in ("architecture-boundaries", "architecture-tradeoffs",
                 "code-quality-review", "pattern-selection",
                 "refactoring-pass", "testing-discipline"):
        check(f"/touch:{name}" in readme,
              f"the shipped README's skill table lists /touch:{name}")
    # SKILLS-CONTENT-14: the six are condensations of named books shipping
    # under MIT. The attribution must say derived-from, not "here are the
    # books".
    check("not the works themselves" in readme,
          "the shipped README says the six are condensed guidance, not the works")
    check("1,257" in read(PLUGIN_CHANGELOG),
          "the CHANGELOG entry that adds the six also prices them")


def test_manifest_declares_both_skill_families():
    print("test_manifest_declares_both_skill_families")
    if not have_plugin(PLUGIN_MANIFEST):
        return
    raw = read(PLUGIN_MANIFEST)
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        check(False, f"plugin.json is valid JSON ({exc})")
        return
    desc = manifest.get("description", "")
    # SKILLS-CONTENT-13 / SKILLS-INTEGRATION-6: the `/plugin` UI shows this
    # string and never the README, so a user who enables Touch on it must not
    # then discover six skills nobody mentioned.
    check("engineering-practice" in desc,
          "the manifest description names the second skill family "
          "(SKILLS-CONTENT-13)")
    keywords = manifest.get("keywords") or []
    for kw in ("code-quality", "architecture"):
        check(kw in keywords, f"keywords carry `{kw}`")


def fenced_lines(text):
    """Every line inside a ``` fence, paired with its block's whole text.

    A command in prose is a citation; a command in a fence is something a
    reader copies. The block travels with the line because the exemption below
    is block-scoped: a fence that starts with `cd "$PLUGIN_ROOT"` has said
    where the import path comes from for every line after it.
    """
    out = []
    block, fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            if fence:
                for ln in block:
                    out.append((ln, "\n".join(block)))
                block = []
            fence = not fence
            continue
        if fence:
            block.append(line)
    for ln in block:                      # an unterminated fence still counts
        out.append((ln, "\n".join(block)))
    return out


def test_payload_docs_run_from_an_installed_copy():
    """C-11 / GD-C11: the payload is a COPY, and its docs must work from there.

    An installed Touch lives at `~/.claude/plugins/cache/<marketplace>/touch/
    <version>/`, so a payload document that names a path in this checkout — or
    a command that only resolves with this checkout as the cwd — is wrong in
    the only place it will ever be read.

    Both arms are negative, which is the load-bearing kind here: the fixed
    prose is `plugin/touch/`'s problem (C-11), while "no NEW file reintroduces
    it" is a property of the whole subtree and can only be checked as a set.
    """
    print("test_payload_docs_run_from_an_installed_copy")
    # PAYLOAD-8: `.claude/shared/monitoring` was the module's home until GD-U1
    # moved it into the plugin. It is now wrong ANYWHERE in the payload — there
    # is no project-side copy for it to mean, and a consumer following it lands
    # in their own `.claude/`, which Touch may not even write to.
    dead = []
    for path in sorted(PLUGIN.rglob("*")):
        if not path.is_file():
            continue
        if ".claude/shared/monitoring" in read(path):
            dead.append(str(path.relative_to(REPO)))
    check(not dead,
          f"no file under {PLUGIN_REL}/ names `.claude/shared/monitoring` — the "
          f"module ships at ${{CLAUDE_PLUGIN_ROOT}}/shared/monitoring/ (bad: {dead})")
    # PAYLOAD-2: `python3 -m aggregator.mirror` resolves only when the plugin
    # root is on `sys.path`, and in an installed copy that root is a
    # version-stamped cache directory nobody can guess. The invocation
    # therefore has to carry the path — `PYTHONPATH=` — or the block has to
    # `cd` there first. The dev checkout's own spelling (`plugin/touch`) is NOT
    # accepted as a substitute: it does not exist on a consumer's machine.
    bare = []
    for doc in sorted(PLUGIN.rglob("*.md")):
        for line, block in fenced_lines(read(doc)):
            flat = re.sub(r"\s+", " ", line)
            m = re.search(r"python3 (?:-\w+ )*(?:-m aggregator|-c [\"']import aggregator)",
                          flat)
            if m is None:
                continue
            if "PYTHONPATH=" in flat[:m.start()] or re.search(r"(?m)^\s*cd\s", block):
                continue
            bare.append(f"{doc.relative_to(REPO)}: {line.strip()[:70]}")
    check(not bare,
          f"every fenced `aggregator.` invocation in a payload doc carries "
          f"`PYTHONPATH=<plugin root>` or a `cd` (bad: {bare})")


def test_contributing_releasing_section_describes_this_model():
    """C-15: the Releasing section is the only prose account of how Touch ships.

    Read as a SECTION, not as a file: every one of these tokens appears
    somewhere else in CONTRIBUTING (the layout table names the catalog, the
    clone recipe names the repo), so a file-wide `in text` would pass while the
    section that tells a maintainer what to run said something else entirely.

    The dead half is the load-bearing one. `--release-clone`, a separate
    payload-only `msdrx/touch-plugin` install source and `sync_plugin.sh` are
    all instructions for a distribution model this repo no longer has — a
    maintainer who follows one publishes nothing, or publishes it twice.
    """
    print("test_contributing_releasing_section_describes_this_model")
    m = re.search(r"^##\s+Releasing\s*$", read(CONTRIBUTING), re.M)
    check(m is not None, "CONTRIBUTING.md has a `## Releasing` section")
    if m is None:
        return
    rest = read(CONTRIBUTING)[m.end():]
    nxt = re.search(r"^##\s+\S", rest, re.M)
    section = rest[:nxt.start()] if nxt else rest
    for token in (".claude-plugin/marketplace.json", "./plugin/touch",
                  "msdrx/touch", "release.sh --check"):
        check(token in section,
              f"CONTRIBUTING's Releasing section names `{token}`")
    for token in ("--release-clone", "msdrx/touch-plugin", "sync_plugin.sh"):
        check(token not in section,
              f"CONTRIBUTING's Releasing section does not name the retired "
              f"`{token}`")
    # The two decisions the section is the record of: the gate that stands
    # between a burned credential and a public marketplace (GD-C4 — naming the
    # knob, because "the preflight makes you confirm this" stopped being true
    # when the confirmation became a gate), and the source form that was
    # weighed and declined (GD-C8 — a record with no name in it is not a
    # record, and the next reader re-litigates it).
    check("RELEASE_HISTORY_ACCEPTED" in section,
          "CONTRIBUTING's Releasing section names the history gate's knob "
          "(`RELEASE_HISTORY_ACCEPTED`), not a confirmation that no longer "
          "exists (GD-C4)")
    check("git-subdir" in read(CONTRIBUTING),
          "CONTRIBUTING records that a `git-subdir` plugin source was weighed "
          "and declined (GD-C8)")


# ============================================================================
# Item 11 — the shipped README and CHANGELOG (the plugin's trust surface)
# ============================================================================

def parenthesized(text, index):
    """True when `text[index]` sits inside a `(...)` on the same block.

    Depth-counted rather than regex-matched, and always called per PARAGRAPH:
    an unbalanced parenthesis three sections earlier must not decide whether
    this sentence is a parenthetical.
    """
    depth = 0
    for ch in text[:index]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
    return depth > 0


def test_plugin_readme_install_and_update_commands():
    print("test_plugin_readme_install_and_update_commands")
    if not have_plugin(PLUGIN_README):
        return
    # BOTH READMEs, and verbatim, because the `/plugin` UI never shows either
    # one: whatever a user types comes from a README, and a paraphrase does not
    # run (DISTRIBUTION-7). The ROOT README is included as of C-13 — its
    # install block is the most-copied text in the repository and was the one
    # copy guarded nowhere, which is how it kept an ordering that cannot work.
    for path in (PLUGIN_README, README):
        # Both files are called `README.md`, so the message names the PATH: two
        # identical FAIL lines are a report nobody can act on.
        who = path.relative_to(REPO)
        text = read(path)
        for line in ("/plugin marketplace add msdrx/touch",
                     "/plugin install touch@msdrx-tools",
                     "/reload-plugins"):
            check(line in text,
                  f"{who} carries the install line `{line}`")
        # E3 / GD-C12: the `owner/repo` shorthand clones over SSH by default,
        # which fails for anyone without a key on the machine. Either
        # documented escape counts — the explicit HTTPS URL, or the env knob.
        fallback = ("/plugin marketplace add https://github.com/msdrx/touch.git",
                    "CLAUDE_CODE_PLUGIN_PREFER_HTTPS")
        check(any(f in text for f in fallback),
              f"{who} carries the HTTPS fallback for the shorthand clone "
              f"(one of {fallback})")
        # GD-C12's sequence, asserted as ORDER rather than presence: the `bin/`
        # wrappers are on `PATH` only while the plugin is ENABLED, so a
        # `touch-selfcheck` offered before the enable step is an instruction
        # that cannot succeed — the reader's first experience of Touch is
        # `command not found`. Measured forward from the install command,
        # because the root README also names `touch-selfcheck` in its wrapper
        # table long before the install section.
        i_install = text.find("/plugin install touch@msdrx-tools")
        if i_install < 0:
            continue                      # already FAILed above; do not also
                                          # report a nonsense ordering
        tail = text[i_install:]
        i_self = tail.find("touch-selfcheck")
        i_reload = tail.find("/reload-plugins")
        # `\benabl` and not `enable`: `defaultEnabled` has no word boundary in
        # front of its `E`, so the sentence that discloses the opt-in posture
        # cannot be mistaken for the instruction to act on it.
        m_enable = re.search(r"\benabl", tail)
        check(i_self > 0 and 0 < i_reload < i_self,
              f"{who}: `/reload-plugins` comes before the "
              f"`touch-selfcheck` verification")
        check(i_self > 0 and m_enable is not None and m_enable.start() < i_self,
              f"{who}: the reader is told to ENABLE Touch from `/plugin` "
              f"BEFORE verifying with `touch-selfcheck` — `bin/` is on PATH "
              f"only while the plugin is enabled (GD-C12)")
    text = read(PLUGIN_README)
    # Third-party marketplaces do NOT auto-update, so these two are the entire
    # upgrade path for a user who installed once (DISTRIBUTION-5, GD-T9).
    for line in ("/plugin marketplace update msdrx-tools",
                 "/plugin update touch@msdrx-tools"):
        check(line in text, f"the shipped README carries the update line `{line}`")
    check("auto-update" in text.lower(),
          "the README says third-party marketplaces do not auto-update by default")
    check("plugin.json" in text and "version" in text.lower(),
          "the README says only a version bump in plugin.json delivers anything")


def test_plugin_readme_trust_section():
    print("test_plugin_readme_trust_section")
    if not have_plugin(PLUGIN_README):
        return
    text = read(PLUGIN_README)
    check(re.search(r"^##\s+Trust and data handling\s*$", text, re.M) is not None,
          "the shipped README has a `## Trust and data handling` section")
    for token in ("~/.claude/projects/", "127.0.0.1:8932", "PreToolUse",
                  "run_scope_guard", "ACTIVE"):
        check(token in text, f"the trust section names {token}")
    check("inert" in text.lower(),
          "the README states the hook is inert without an ACTIVE sentinel")
    check(re.search(r"\d+\s*ms", text) is not None,
          "the README carries the MEASURED per-call hook cost, not an adjective (GD-T8)")
    check("no background process" in text.lower(),
          "the README states that installing starts no background process (GD-T6)")
    check("token" in text.lower() and "loopback" in text.lower(),
          "the README describes the loopback + per-boot-token posture")


def test_plugin_readme_network_guidance_is_generic():
    print("test_plugin_readme_network_guidance_is_generic")
    if not (have_plugin(PLUGIN_README) and have_plugin(PLUGIN_CHANGELOG)):
        return
    # `sbx ports` is this sandbox's command, not a consumer's. It may appear as
    # a parenthetical aside and nowhere else — a shipped README that instructs
    # a stranger to publish the dashboard's port is the one instruction Touch
    # must never give (GD-T8).
    bad = []
    for path in (PLUGIN_README, PLUGIN_CHANGELOG):
        for para in paragraphs(read(path)):
            for m in re.finditer(r"sbx ports", para):
                if not parenthesized(para, m.start()):
                    bad.append(f"{path.name}: {para.strip()[:70]}")
    check(not bad, f"every `sbx ports` mention in the payload docs is a parenthetical (bad: {bad})")
    readme = read(PLUGIN_README)
    check("ssh -L" in readme,
          "the README's network guidance offers the generic answer (an SSH tunnel)")
    check("loopback" in readme.lower(), "the README's network guidance is loopback-first")


def test_plugin_docs_carry_no_local_or_ladder_paths():
    print("test_plugin_docs_carry_no_local_or_ladder_paths")
    if not (have_plugin(PLUGIN_README) and have_plugin(PLUGIN_CHANGELOG)):
        return
    # Two different leaks, one guard: a path that only exists on the author's
    # machine, and a pointer into this repo's orchestration history. Neither
    # travels — the payload ships no `.claude/local-orchestrators/`, no plan
    # files and no findings, so a reference to one is a dead link that also
    # tells a stranger who wrote it and where (DISTRIBUTION-4).
    pii = ("/home/", "/Users/", "laniakea", "michaelsadradze", "-home-laniakea")
    ladder = ("-plan.md", "inception.md", "CLAUDE.md", "touch-full-recon",
              "touch-mongo-live", "touch-aggregator", "touch-plugin-pack",
              "local-orchestrators/touch-", "findings/")
    for path in (PLUGIN_README, PLUGIN_CHANGELOG):
        text = read(path)
        for token in pii:
            check(token not in text, f"{path.name} carries no `{token}` path")
        for token in ladder:
            check(token not in text, f"{path.name} carries no authority-ladder reference `{token}`")


def test_plugin_changelog_top_entry_matches_manifest():
    print("test_plugin_changelog_top_entry_matches_manifest")
    if not (have_plugin(PLUGIN_CHANGELOG) and have_plugin(PLUGIN_MANIFEST)):
        return
    # Parsed defensively, and the "declares a version" assertion placed FIRST.
    # A malformed manifest and a manifest with no `version` are precisely the
    # states this gate exists to catch, and reaching them through a bare
    # `json.loads(...)["version"]` aborts the module with a traceback: the
    # remaining item-12 guards never run and the operator gets no `FAIL:` line
    # and no summary — the crash this file's docstring promises not to be.
    raw = read(PLUGIN_MANIFEST)
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        check(False, f"plugin.json is valid JSON ({exc})")
        return
    version = manifest.get("version")
    check(version is not None,
          "plugin.json declares .version (the only place it may be declared — GD-T9)")
    if version is None:
        return
    m = re.search(r"^##\s+\[?(\d+\.\d+\.\d+[^\s\]]*)\]?", read(PLUGIN_CHANGELOG), re.M)
    check(m is not None, "the shipped CHANGELOG has a `## <version>` entry")
    if m:
        check(m.group(1) == version,
              f"the CHANGELOG's top entry ({m.group(1)}) is plugin.json's version ({version})")


# ============================================================================
# Item 12 — `scripts/release.sh` IS the checklist
# ============================================================================

def strip_comment(line):
    """One shell line with its `#` comment removed, quotes respected.

    `#` only opens a comment at the start of a word — line start or after
    whitespace — and never inside a quoted string. Both halves matter here:
    `$#` and `sed 's#a#b#'` must survive (the script's URL normaliser is
    written entirely in `#`-delimited `sed` expressions), while a trailing
    `foo   # no jq here` must not.

    An unbalanced quote (an apostrophe in prose, say) only makes the rest of
    that ONE line look quoted — state never carries to the next line — so the
    failure mode is "a comment was kept", never "a command was dropped".
    """
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote == "'":
            if ch == "'":
                quote = None
        elif quote == '"':
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = None
        elif ch == "\\":
            i += 1
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
        i += 1
    return line


def blank_heredoc_bodies(text):
    """`text` with every here-document BODY blanked out.

    A heredoc body is data, not code. `scripts/release.sh` prints its manual
    checklist out of a `<<'CHECKLIST'` block, and that checklist is the natural
    place to write "nothing is ever `cp -r`d: the stage is scanned, not
    assembled" or
    "this depends on no `jq`" — sentences the guards below would then read as
    commands. That is the same trap `strip_comment()` exists to avoid, one
    layer down: a guard that cannot tell prose from a command forbids writing
    down the reason.

    Here-STRINGS (`<<<`) are not here-documents and must survive — the script
    feeds `python3` the manifest that way — hence the `(?!<)`.

    Lines are blanked rather than removed, so a line number here is still a
    line number in the file. Called AFTER comment stripping, so a comment that
    merely mentions a heredoc opener cannot start one.
    """
    out = []
    delim = None
    for line in text.splitlines():
        if delim is not None:
            out.append("")
            if line.strip() == delim:
                delim = None
            continue
        out.append(line)
        m = re.search(r"<<-?\s*(?!<)(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", line)
        if m:
            delim = m.group(2)
    return "\n".join(out)


def script_code(text):
    """The script's executable text: comments dropped, heredoc bodies blanked.

    The `jq` and `cp -r` guards below must read this and not the raw file. The
    script names both in prose to explain why it uses neither, and a guard that
    could not tell a comment from a command would make writing that reason
    impossible — which is how the reason gets lost. Dropping only FULL-LINE
    comments would honour that promise for one comment shape and break it for
    the other, so the exemption is spelled out once, in `strip_comment()`; the
    operator-facing checklist the script prints is prose too, so it is dropped
    as well (`blank_heredoc_bodies()`).

    Lines are emptied rather than removed, so a line number in `code` is still
    a line number in the file.
    """
    return blank_heredoc_bodies(
        "\n".join(strip_comment(ln) for ln in text.splitlines()))


def cp_commands(code):
    """Every `cp` invocation in `code`, wherever it sits in a command line.

    Anchoring this at the start of a line — the obvious spelling — checks only
    the first command on each line, so `git checkout && cp -a plugin/touch/. …`
    is invisible to all three `cp` guards at once: the count still says one,
    the survivor still mentions `$STAGE`, and `cp -a` carries no `r` for the
    `cp -r` check to catch. The guard has to see EVERY `cp`, so match the word
    anywhere and cut each one at the next command separator.
    """
    found = []
    for m in re.finditer(r"(?<![\w./-])cp(?![\w./-])", code):
        segment = code[m.start():]
        for sep in ("\n", ";", "&&", "||", "|"):
            segment = segment.split(sep)[0]
        found.append(segment.strip())
    return found


def test_release_script_exists_and_is_executable():
    print("test_release_script_exists_and_is_executable")
    check(RELEASE_SH.is_file(), "scripts/release.sh exists")
    if not RELEASE_SH.is_file():
        return
    check(os.access(RELEASE_SH, os.X_OK),
          "scripts/release.sh is executable (a checklist you cannot run is prose)")
    text = read(RELEASE_SH)
    check(text.startswith("#!/usr/bin/env bash"), "scripts/release.sh has a bash shebang")


def test_release_script_uses_no_jq_and_never_copies_the_working_tree():
    print("test_release_script_uses_no_jq_and_never_copies_the_working_tree")
    if not RELEASE_SH.is_file():
        check(False, "scripts/release.sh exists")
        return
    code = script_code(read(RELEASE_SH))
    check(re.search(r"\bjq\b", code) is None,
          "scripts/release.sh runs no `jq` (GD-21; the statusline exception stops at that file)")
    check("python3 -c" in code and "plugin.json" in code,
          "the version is read with a stdlib `python3 -c`, not a JSON tool")
    check(re.search(r"\bcp\s+-[a-zA-Z]*[rR]", code) is None,
          "scripts/release.sh never `cp -r`s a tree")
    # ZERO copies now, not one. While releases were assembled in a separate
    # flat repo the script had exactly one legitimate `cp` — out of the
    # git-built stage into that clone. Publishing from this repo removed the
    # destination: the stage exists only to be SCANNED, and any `cp` at all
    # means someone started hand-assembling a payload again.
    copies = cp_commands(code)
    check(not copies,
          f"the script copies nothing — the stage is scanned, never assembled "
          f"(found {len(copies)}: {copies})")
    check("git archive" in code and ("HEAD:$PLUGIN" in code or "HEAD:plugin/touch" in code),
          "the payload is built by `git archive` of a committed tree")


def test_release_script_is_the_checklist():
    print("test_release_script_is_the_checklist")
    if not RELEASE_SH.is_file():
        check(False, "scripts/release.sh exists")
        return
    text = read(RELEASE_SH)
    code = script_code(text)
    check("no release.md" in text.lower(),
          "the script says why there is no separate RELEASE.md (GD-T9)")
    # The manual half: what the sandbox cannot verify is printed and confirmed,
    # cited as the COMMAND that measures it rather than a count that drifts
    # (PRIOR-AUDIT-12). `CLONES THIS REPOSITORY` is the item the whole layout
    # turns on — serving the catalog from here means the history goes with it,
    # and that sentence is the operator's last chance to reconsider.
    for token in ("CLONES THIS REPOSITORY", "rev-list --all --objects", "mytok",
                  "rotate", "filter-repo", "touch-selfcheck"):
        check(token in text, f"the preflight checklist names {token}")
    counted = [ln.strip()[:60] for ln in text.splitlines()
               if re.search(r"mytok|mongodb://", ln) and re.search(r"\b\d{2,}\b", ln)]
    check(not counted,
          f"no line pairs the contamination with a hard count — counts drift, commands do not (bad: {counted})")
    # The automated half, in order. The last three are the publish half: the
    # remote is resolved, HEAD is compared against its upstream, and the push
    # IS the release — there is no second repository to assemble any more.
    for token in ("status --porcelain", "tests/run_all.sh", "CHANGELOG.md",
                  "plugin validate", "--strict", "git archive",
                  "remote get-url", "rev-list --left-right --count",
                  "git push", "plugin tag", "--dry-run"):
        check(token in code, f"the script runs the `{token}` gate")
    check("--check" in code, "the script has a `--check` dry-run mode")


def test_release_script_avoids_the_shapes_that_reported_wrong_answers():
    """The command SHAPES that once made a gate report the opposite of the truth.

    Structural, not behavioural — nothing in this function runs anything;
    `test_release_script_check_mode_runs_its_gates_for_real()` below does that.
    The distinction is the point: every guard in the function above is
    satisfied by the printed checklist alone, which names `rev-list --all
    --objects` and `mytok` whether or not the code that runs them reports the
    truth. It once did not — `git rev-list … | grep -qi mytok` under `pipefail`
    returns 141, because `-q` exits on the first hit and kills the producer
    with SIGPIPE, so the token blob being PRESENT read as absent. On a small
    history git finishes writing first and the bug is invisible; it appears
    only once there is enough history to matter.

    Each assertion below names one spelling that lied, so the pair
    (`pipefail` present, no `git … | grep -q`) cannot be "satisfied" by
    dropping `pipefail` instead of fixing the pipeline. A shape guard is cheap
    and total where a behavioural one is expensive and sampled; both are here
    because neither alone caught what the other did.
    """
    print("test_release_script_avoids_the_shapes_that_reported_wrong_answers")
    if not RELEASE_SH.is_file():
        check(False, "scripts/release.sh exists")
        return
    code = script_code(read(RELEASE_SH))
    check(re.search(r"^\s*set\s+.*pipefail", code, re.M) is not None,
          "the script sets `pipefail` (a pipeline's failure must not be swallowed)")
    piped = re.findall(r"^.*\bgit\b[^\n|]*\|[^\n]*\bgrep\s+-[a-zA-Z]*q.*$", code, re.M)
    check(not piped,
          "no `git … | grep -q` under `pipefail` — SIGPIPE makes a match look "
          f"like a miss (bad: {piped})")
    # The dirty-tree gate must see untracked files; `git diff` alone does not,
    # and a payload file that was never `git add`ed is the accident that ships
    # a release missing a file while every gate reports green.
    check("status --porcelain" in code or "ls-files --others" in code,
          "the dirty-tree gate counts untracked files as dirty")
    # Repo identity is compared, never URL spelling: `https://…/touch.git` and
    # `git@github.com:msdrx/touch` are one repository, and step 7 asks whether
    # the remote it is about to push to is the one `plugin.json` sends readers
    # to. Both sides go through the same normaliser or the answer is spelling.
    check("norm_urls" in code and code.count("| norm_urls") >= 2,
          "both URLs pass through the same normaliser before comparison")
    # The push is the point of no return and it is ordinary: a release that can
    # rewrite the published branch is a release that can un-publish a version
    # users already installed.
    check(re.search(r"git\s+push[^\n]*(--force|--mirror|\s-f\b)", code) is None,
          "no `git push --force`/`--mirror` — publishing only ever adds commits")
    # The `git push` that publishes must name the remote and branch explicitly.
    # A bare `git push` obeys `push.default` and the branch's configured
    # upstream, so on a machine configured differently it can publish something
    # other than the branch every gate above just measured.
    pushes = re.findall(r"^\s*git\s+push[^\n]*$", code, re.M)
    check(pushes and all(("$REMOTE" in p and "$branch" in p) for p in pushes),
          f"every `git push` names $REMOTE and $branch explicitly (found: {pushes})")
    # A message about a push must not BE one. Backticks inside a double-quoted
    # string are command substitution, and this line once read
    # `note "… will need `git push -u $REMOTE $branch`"` — which pushes.
    quoted_backticks = [ln.strip() for ln in code.splitlines()
                        if re.match(r'\s*(note|ok|fail|skip)\s+"', ln)
                        and re.search(r'(?<!\\)`', ln)]
    check(not quoted_backticks,
          f"no unescaped backtick inside a double-quoted message — that is "
          f"command substitution, not formatting (bad: {quoted_backticks})")


def script_step(code, number):
    """The script's own text under its `step <n> "…"` banner, up to the next.

    Step 4 prints one of two banners depending on whether step 3 could read a
    version, so a section runs to the first banner with a HIGHER number rather
    than to the next banner of any number — otherwise step 4's section would
    end at step 4.
    """
    banners = [(int(m.group(1)), m.start())
               for m in re.finditer(r'^\s*step (\d+) "', code, re.M)]
    here = [pos for n, pos in banners if n == number]
    if not here:
        return ""
    later = [pos for n, pos in banners if n > number and pos > here[0]]
    return code[here[0]:later[0]] if later else code[here[0]:]


def usage_block(text):
    """The `# usage:` … `# exit status:` header, the operator's contract."""
    start = re.search(r"^# usage:", text, re.M)
    end = re.search(r"^# exit status", text, re.M)
    return text[start.start():end.start()] if start and end else ""


def test_release_script_gates_the_publish_half_before_the_point_of_no_return():
    """The gates C-04…C-07 added, read on the script's own text.

    Each arm is here because the gate it pins was once spelled in a way that
    could not fire, or fired only after the push had already happened — the one
    place a release gate is worth nothing. The behavioural arms below run the
    script; these say WHERE the gates are, which no transcript can show.
    """
    print("test_release_script_gates_the_publish_half_before_the_point_of_no_return")
    if not RELEASE_SH.is_file():
        check(False, "scripts/release.sh exists")
        return
    text = read(RELEASE_SH)
    code = script_code(text)

    # C-04 / GD-C3: `claude plugin tag` needs `<path>/.claude-plugin/plugin.json`
    # and the repo root holds only the CATALOG, so the repo-root form is rc=1 —
    # a gate that always failed for a reason that had nothing to do with the
    # release. Every invocation names the payload. The quote count skips the
    # banner strings (`step 8 "claude plugin tag $PLUGIN --dry-run"`), which
    # print a path and run nothing.
    tag_calls = []
    for ln in code.splitlines():
        for m in re.finditer(r"claude\s+plugin\s+tag\s+(\S+)", ln):
            if ln[:m.start()].count('"') % 2 == 1:
                continue
            tag_calls.append((ln.strip()[:60], m.group(1)))
    check(len(tag_calls) >= 2,
          f"the script invokes `claude plugin tag` for the dry run AND the "
          f"optional push (found {len(tag_calls)})")
    check(tag_calls and all(arg == '"$REPO/$PLUGIN"' for _, arg in tag_calls),
          f"every `claude plugin tag` names the PAYLOAD directory "
          f"(`\"$REPO/$PLUGIN\"`), never the repo root (found: {tag_calls})")
    # …and the dry run is a PRE-publish gate: before the push, and before the
    # `--check` early exit, so a dry run exercises it. Positions, because that
    # is the entire property — the command was already correct as text when it
    # sat after `git push`.
    dry = code.find("--dry-run")
    push = re.search(r"^\s*git\s+push", code, re.M)
    check(dry > 0 and push is not None and dry < push.start(),
          "the tag dry run runs BEFORE the push — a gate on the far side of "
          "the point of no return is a report, not a gate")
    # Reachable under `--check` too, which is the other half of GD-C3: the gate
    # is worth having only where it can still change the outcome, and a dry run
    # that stops short of it would report green on a release the real run
    # refuses. Measured against the early exit that ENDS a `--check` run — the
    # last `$mode = check` test with an `exit 0` under it.
    early = [m.start() for m in re.finditer(r'"\$mode"\s*=\s*check', code)
             if re.search(r"^\s*exit 0", code[m.start():m.start() + 500], re.M)]
    check(dry > 0 and early and dry < max(early),
          "the tag dry run sits before `--check`'s early exit, so a dry run "
          "actually exercises it (GD-C3)")

    # C-05 / GD-C4: the history scan is a GATE, and only its own knob clears it.
    # It was a `note` — a line the operator reads after publishing.
    step0 = script_step(code, 0)
    # The VERDICT branch only: from the last mention of the scan's variable to
    # the confirmation block that closes step 0. Scoped, because step 0 ends
    # with the interactive `RELEASE_CONFIRM` prompt for the checklist's other
    # bullets — a region that ran to the end of the step would read that as the
    # history gate's bypass and report the opposite of the truth.
    verdict = ""
    if "tokenblobs" in step0:
        verdict = step0[step0.rfind("tokenblobs"):]
        mode = verdict.find('"$mode"')
        verdict = verdict[:mode] if mode > 0 else verdict
    check("tokenblobs" in step0 and "mongouris" in step0,
          "step 0 scans for both a token-named blob and a credentialed "
          "`mongodb://` URI")
    check(re.search(r"^\s*fail ", verdict, re.M) is not None,
          "the contamination verdict reaches a `fail` — not a `note` the "
          "operator reads after the push (GD-C4)")
    # Which knob the verdict BRANCHES on, not which knob it mentions: the fail
    # message says "RELEASE_CONFIRM does NOT imply it" out loud, and a guard
    # that read that as a bypass would forbid writing the reason down — the
    # same bargain `strip_comment()` makes for comments.
    bypasses = [ln.strip() for ln in verdict.splitlines()
                if re.match(r"\s*(if|elif)\b", ln) and "RELEASE_" in ln]
    check(bypasses and all("RELEASE_HISTORY_ACCEPTED" in b for b in bypasses),
          f"the contamination verdict is cleared by `RELEASE_HISTORY_ACCEPTED` "
          f"(conditions found: {bypasses})")
    check(all("RELEASE_CONFIRM" not in b for b in bypasses),
          f"no condition in the verdict tests `RELEASE_CONFIRM` — one knob that "
          f"answers everything answers nothing (bad: {bypasses})")
    check(usage_block(text).count("RELEASE_HISTORY_ACCEPTED") >= 1
          and "RELEASE_CONFIRM" in usage_block(text),
          "the usage header documents both knobs")

    # C-06: provenance before validation. `claude plugin validate` reads the
    # file on disk and passes on a catalog whose source directory does not even
    # exist, so the step proves the bytes are HEAD's and resolves the entry
    # itself.
    step6 = script_step(code, 6)
    check("ls-files --error-unmatch" in step6,
          "step 6 proves the catalog is TRACKED (an untracked one is absent "
          "from every clone)")
    check("diff HEAD --quiet" in step6,
          "step 6 proves the catalog on disk is HEAD's bytes (it validates the "
          "file in place)")
    check(re.search(r"\[\s*-f\s+\"[^\"]*\$MANIFEST\"", step6) is not None,
          "step 6 resolves the entry's `source` and requires a $MANIFEST "
          "behind it — `--strict` alone passes a source that resolves to "
          "nothing")

    # C-07 / GD-C7: the suite runs against the tree that SHIPS, not the tree
    # you happen to be sitting in — the script's own headline.
    step2 = script_step(code, 2)
    tmp = re.search(r"(\w+)=\"?\$\(mktemp -d\)", step2)
    check(tmp is not None, "step 2 builds a throwaway directory (`mktemp -d`)")
    check(re.search(r"git archive[^\n|]*\bHEAD\b", step2) is not None
          and "tar -x" in step2,
          "step 2 extracts a clean checkout of HEAD (`git archive HEAD | tar -x`)")
    if tmp:
        var = tmp.group(1)
        ran = [ln.strip() for ln in step2.splitlines()
               if "run_all.sh" in ln and "cd" in ln and var in ln]
        check(ran,
              f"step 2 runs `run_all.sh` from the extracted tree, not from "
              f"$REPO (no `cd ${var}` on the line that runs it)")
    # …and the already-published guard (GD-C5): the tag `claude plugin tag`
    # would cut is asked about BEFORE anything is pushed.
    step3 = script_step(code, 3)
    check("--v" in step3 and ("ls-remote --tags" in step3 or "git tag -l" in step3),
          "step 3 refuses a version whose `{name}--v{version}` tag already "
          "exists (GD-C5)")
    check(re.search(r"^\s*fail .*already exists", step3, re.M) is not None,
          "the already-published guard is a `fail`, not a note")

    # The usage header is the only account of the run an operator reads before
    # starting it, and it renumbered twice in this plan. Every `step N` it
    # mentions must be a banner that exists — a header pointing at a step
    # number nobody prints is how "the dry run is step 10" survived the move.
    banners = {int(m.group(1)) for m in re.finditer(r'^\s*step (\d+) "', code, re.M)}
    header = {int(n) for n in re.findall(r"\bstep (\d+)\b", usage_block(text))}
    check(header and header <= banners,
          f"every `step N` in the usage header has a banner "
          f"(header {sorted(header)}, banners {sorted(banners)})")
    everywhere = {int(n) for n in re.findall(r"\bstep (\d+)\b", text)}
    check(everywhere <= banners,
          f"no comment anywhere names a step the script does not print "
          f"(orphans: {sorted(everywhere - banners)})")


# ---------------------------------------------------------------------------
# The behavioural arm: run `release.sh --check` and read what it printed.
# ---------------------------------------------------------------------------

def _run(cmd, cwd, env, timeout=300):
    """One subprocess, stderr folded into stdout so the transcript is ORDERED.

    The script writes `FAIL:` to stderr and everything else to stdout; read as
    two streams they cannot be attributed to a step, and this test's whole
    method is "what did step 7 print".
    """
    return subprocess.run([str(c) for c in cmd], cwd=str(cwd), env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, timeout=timeout)


def _git(args, cwd, env):
    """A fixture-building git command that must succeed.

    A silently failed `git init` would leave the fixture half-built and every
    assertion below would then report the SCRIPT as broken — a wrong diagnosis
    is worse than no test. Raising here instead sends the caller down the
    skip-with-reason path, which is what "a prerequisite is unusable" means in
    this suite.
    """
    proc = _run(["git", *args], cwd, env)
    if proc.returncode != 0:
        raise subprocess.SubprocessError(
            f"git {' '.join(args)} failed in {cwd} (rc={proc.returncode}): "
            f"{proc.stdout.strip()[:200]}")
    return proc


def _release_fixture(tmp):
    """A throwaway dev repo that `scripts/release.sh` can be run inside.

    This repository cannot host the run: step 2 of the script executes
    `tests/run_all.sh`, which executes THIS file, which would execute the
    script again — an unbounded regress. So the fixture is a real git repo
    carrying a stub runner, a minimal shipping subtree and one commit; every
    gate through step 7 then runs for real against data this test owns.

    Two deliberate stubs. `claude` is a shell script that APPENDS ITS ARGV to a
    log and exits 0 — hermetic and instant, and discriminating: a stub that
    only exits 0 makes `claude plugin tag .` and `claude plugin tag
    plugin/touch` indistinguishable, and the repo-root form is exactly the bug
    C-04 fixed (rc=1 in reality, "No plugin manifest found"). The log is how a
    test can tell which one the script ran.

    The git GLOBAL config carries an identity that no local config repeats.
    That scaffolding is now load-bearing for a different reason than the one it
    was written for: the REAL-mode arm below performs an actual `git push` into
    the fixture's bare `origin`, and a commit without a committer identity
    fails — `GIT_CONFIG_NOSYSTEM` plus a throwaway `GIT_CONFIG_GLOBAL` gives
    the fixture one without reading (or writing) the author's own git config.
    """
    dev = tmp / "devrepo"
    (dev / "scripts").mkdir(parents=True)
    (dev / "tests").mkdir()
    (dev / PLUGIN_REL / ".claude-plugin").mkdir(parents=True)
    (dev / CATALOG_REL).parent.mkdir(exist_ok=True)
    shutil.copy2(RELEASE_SH, dev / "scripts/release.sh")
    runner = dev / "tests/run_all.sh"
    runner.write_text("#!/bin/sh\necho 'stub suite (fixture)'\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
    (dev / PLUGIN_REL / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "touch", "version": "0.1.0"}) + "\n", encoding="utf-8")
    # The catalog at the REPO root, where a cloned marketplace is read from,
    # naming the payload subtree — the layout the script now gates on. A copy
    # of it inside the payload is what step 6 refuses. Both paths come from
    # `_roots` (C-03), so the fixture cannot keep testing a layout the repo has
    # moved on from.
    (dev / CATALOG_REL).write_text(
        json.dumps({"name": "msdrx-tools",
                    "plugins": [{"name": "touch",
                                 "source": f"./{PLUGIN_REL.as_posix()}"}]}) + "\n",
        encoding="utf-8")
    (dev / PLUGIN_REL / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.1.0\n", encoding="utf-8")

    stub_bin = tmp / "bin"
    stub_bin.mkdir()
    stub_claude = stub_bin / "claude"
    # One line per invocation, argv joined — enough to answer "which path did
    # the tag gate name". `${CLAUDE_STUB_LOG:-}` guarded so the stub still
    # exits 0 if a caller forgets the variable: a stub that fails on its own
    # bookkeeping would report the SCRIPT as broken.
    stub_claude.write_text(
        '#!/bin/sh\n'
        'if [ -n "${CLAUDE_STUB_LOG:-}" ]; then\n'
        '    printf \'%s\\n\' "$*" >> "$CLAUDE_STUB_LOG"\n'
        'fi\n'
        'exit 0\n', encoding="utf-8")
    stub_claude.chmod(0o755)
    stub_log = tmp / "claude-argv.log"

    global_cfg = tmp / "gitconfig-global"
    global_cfg.write_text(
        "[user]\n\tname = Global Identity\n\temail = global@example.invalid\n",
        encoding="utf-8")

    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = str(global_cfg)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "Fixture"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "fixture@example.invalid"
    env["PATH"] = str(stub_bin) + os.pathsep + env.get("PATH", "")
    env["CLAUDE_STUB_LOG"] = str(stub_log)
    for var in ("RELEASE_CONFIRM", "RELEASE_HISTORY_ACCEPTED", "RELEASE_REMOTE",
                "GIT_DIR", "GIT_WORK_TREE"):
        env.pop(var, None)

    _git(["init", "-q", "-b", "main", "."], dev, env)
    _git(["add", "-A"], dev, env)
    _git(["commit", "-q", "-m", "fixture"], dev, env)
    # A publish target, because publishing is now a push of THIS repo: step 7
    # resolves `origin`, fetches it and compares HEAD against its upstream. A
    # bare repo on disk plays the remote — no network, and `git push` against
    # it is a real push, which is what makes the ahead/behind arms below mean
    # anything.
    origin = tmp / "origin.git"
    _git(["init", "-q", "--bare", "-b", "main", str(origin)], tmp, env)
    _git(["remote", "add", "origin", str(origin)], dev, env)
    _git(["push", "-q", "-u", "origin", "main"], dev, env)
    return dev, env, origin, stub_log


def _tag_invocations(stub_log):
    """Every `claude plugin tag …` the stub recorded, argv-joined, one per line."""
    if not stub_log.is_file():
        return []
    return [ln for ln in read(stub_log).splitlines()
            if ln.startswith("plugin tag")]


def _step_section(transcript, number):
    """What the transcript printed under the `== <number>. …` banner.

    Bounded by the next banner, or by the end of the transcript when there is
    none — so under `--check`, where step 7 is the last banner, the section
    also carries the two-line verdict. That is why the callers below count
    GATE lines (`ok:`/`..`/`FAIL:`/`SKIP:`) rather than lines.
    """
    m = re.search(rf"^== {number}\..*$", transcript, re.M)
    if not m:
        return ""
    rest = transcript[m.end():]
    nxt = re.search(r"^== \d+\.", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def _gate_lines(section):
    return [ln.strip() for ln in section.splitlines()
            if ln.strip().startswith(("ok:", "..", "FAIL:", "SKIP:"))]


def test_release_script_check_mode_runs_its_gates_for_real():
    """`release.sh --check` executed against a fixture, and its output read.

    Every other guard in this section is source text — the SHAPES that once
    lied. None of them can see a gate that is spelled plausibly and still
    answers wrongly, which is what a bare `git config user.email` did: it
    survived three review rounds behind a `"user.email" in code` substring,
    reading the whole global cascade while reporting a per-repo answer. So this
    arm runs the thing and reads what it printed.

    `--check` is side-effect-free by construction — it runs every gate through
    step 8 and then exits, committing nothing and pushing nothing — and the
    fixture is a temp repo, so the cost is a few seconds. Skips with a printed
    reason when git or a POSIX shell is unavailable, the same bargain the rest
    of the suite makes.
    """
    print("test_release_script_check_mode_runs_its_gates_for_real")
    if not RELEASE_SH.is_file():
        check(False, "scripts/release.sh exists")
        return
    if shutil.which("git") is None:
        skip("git is not on PATH — the release script's gates cannot be exercised")
        return
    tmp = Path(tempfile.mkdtemp(prefix="release-check-"))
    try:
        # One try for the whole body: every git call here builds or points at a
        # fixture, so a git that cannot run means the prerequisite is absent —
        # a skip — never a verdict about the script.
        dev, env, _origin, stub_log = _release_fixture(tmp)
        script = dev / "scripts/release.sh"

        clean = _run([script, "--check"], dev, env)

        section = _step_section(clean.stdout, 7)
        # The transcript is printed only when it is evidence of a failure: a
        # passing guard that dumps sixty lines of someone else's output buries
        # the `ok:`/`FAIL:` ledger this suite is read by.
        if clean.returncode != 0:
            print(clean.stdout)
        check(clean.returncode == 0,
              f"a clean fixture passes every gate through step 7 (rc={clean.returncode})")
        # A step whose gates all sit inside one `if` that quietly does not hold
        # prints NOTHING while the run still reports "every gate through step 7
        # is green" — that is how the release-clone version of this step failed
        # for three review rounds. An empty section is a failure in its own
        # right, whatever the step is checking now.
        check(len(_gate_lines(section)) >= 3,
              f"step 7 actually printed its gates ({len(_gate_lines(section))} lines)")
        check("level with" in section,
              "step 7 says the branch is already published rather than implying "
              "a push happened")
        # C-04, measured rather than read: the tag gate must have named the
        # PAYLOAD. The source-text arm above proves the script SPELLS
        # `"$REPO/$PLUGIN"`; only the stub's log proves what that expanded to,
        # and the repo-root expansion is the one that fails in reality ("No
        # plugin manifest found") while a `exit 0` stub reports green.
        tags = _tag_invocations(stub_log)
        payload_arg = str(dev / PLUGIN_REL)
        check(tags, f"the `--check` run reached the tag dry run "
                    f"(stub log: {tags})")
        check(tags and all(t.split()[2:3] == [payload_arg] for t in tags),
              f"every `plugin tag` invocation named {payload_arg}, never the "
              f"repo root {dev} (found: {tags})")
        check(all("--dry-run" in t for t in tags),
              f"a `--check` run only ever DRY-RUNS the tag (found: {tags})")
        # Step 6's arm: the catalog is not payload. Put a copy back inside
        # the shipping subtree and the run must refuse it — this is the exact
        # regression the root-catalog layout replaced, and nothing else in the
        # script would notice a second file declaring the same marketplace.
        stow = dev / PLUGIN_REL / CATALOG_REL
        stow.write_text('{"name": "msdrx-tools", "plugins": []}\n', encoding="utf-8")
        _git(["add", "-A"], dev, env)
        _git(["commit", "-q", "-m", "stowaway"], dev, env)
        rc_stow = _run([script, "--check"], dev, env)
        check(rc_stow.returncode != 0
              and "the catalog is NOT payload" in rc_stow.stdout,
              "a marketplace.json inside the payload is refused (it would be a "
              "second catalog under the same marketplace name)")
        _git(["rm", "-q", str(stow)], dev, env)
        _git(["commit", "-q", "-m", "unstow"], dev, env)

        # The link-vs-source gate: `plugin.json`'s `repository` and the remote
        # being pushed to must be one repository, compared as identity.
        manifest = dev / "plugin/touch/.claude-plugin/plugin.json"
        manifest.write_text(json.dumps(
            {"name": "touch", "version": "0.1.0",
             "repository": "https://github.com/somebody/else"}) + "\n",
            encoding="utf-8")
        _git(["add", "-A"], dev, env)
        _git(["commit", "-q", "-m", "wrong repository field"], dev, env)
        rc_link = _run([script, "--check"], dev, env)
        check(rc_link.returncode != 0 and "link away from the repo" in rc_link.stdout,
              "a `repository` field naming another repo is refused — the plugin "
              "page would link away from the repo the install clones")

        # And a remote that does not exist is a full stop, not a skipped step:
        # a release with nowhere to push must not report green.
        env_noremote = dict(env)
        env_noremote["RELEASE_REMOTE"] = "nope"
        rc_noremote = _run([script, "--check"], dev, env_noremote)
        check(rc_noremote.returncode != 0
              and "no remote named 'nope'" in rc_noremote.stdout,
              "a missing publish remote fails loudly")
    except (OSError, subprocess.SubprocessError) as exc:
        skip(f"could not exercise scripts/release.sh here "
             f"({exc.__class__.__name__}: {exc})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_release_script_history_gate_blocks_and_takes_only_its_own_knob():
    """C-05 / GD-C4: the one property of this model that cannot be undone.

    Publishing from this repository hands every installer the whole history, so
    a burned credential reachable in it is not a housekeeping item — it is the
    release. The scan was a `note`: a line the operator reads AFTER publishing.
    This arm plants a token-named blob in a fixture's history and asserts the
    three things the gate promises — red by default, red still when the
    checklist is confirmed, green only when its own knob says so out loud.

    A token-NAMED file is enough because that is what the first `(b)` command
    looks for: `git rev-list --all --objects` prints object PATHS. Nothing that
    resembles a credential is written anywhere by this test.
    """
    print("test_release_script_history_gate_blocks_and_takes_only_its_own_knob")
    if not RELEASE_SH.is_file():
        check(False, "scripts/release.sh exists")
        return
    if shutil.which("git") is None:
        skip("git is not on PATH — the release script's gates cannot be exercised")
        return
    tmp = Path(tempfile.mkdtemp(prefix="release-history-"))
    try:
        dev, env, _origin, _log = _release_fixture(tmp)
        script = dev / "scripts/release.sh"
        (dev / "mytok2").write_text(
            "not a credential: this file is here for its NAME, which is what "
            "`git rev-list --all --objects` prints\n", encoding="utf-8")
        _git(["add", "-A"], dev, env)
        _git(["commit", "-q", "-m", "planted a token-named blob"], dev, env)

        red = _run([script, "--check"], dev, env)
        check(red.returncode != 0,
              f"a reachable token-named blob fails `--check` (rc={red.returncode})")
        check("still carries a burned credential" in red.stdout,
              "the failure names what it found and what to do about it")
        check("RELEASE_HISTORY_ACCEPTED=yes" in red.stdout,
              "the failure names the knob that would accept it")
        # The whole reason it is a second knob: `RELEASE_CONFIRM` answers the
        # four checklist bullets, and one knob that answers everything answers
        # nothing.
        confirmed = dict(env)
        confirmed["RELEASE_CONFIRM"] = "yes"
        rc_confirm = _run([script, "--check"], dev, confirmed)
        check(rc_confirm.returncode != 0,
              f"`RELEASE_CONFIRM=yes` does NOT clear the history gate "
              f"(rc={rc_confirm.returncode})")

        accepted = dict(env)
        accepted["RELEASE_HISTORY_ACCEPTED"] = "yes"
        green = _run([script, "--check"], dev, accepted)
        if green.returncode != 0:
            print(green.stdout)
        check(green.returncode == 0,
              f"`RELEASE_HISTORY_ACCEPTED=yes` is the way past it "
              f"(rc={green.returncode})")
        check("ACCEPTED by RELEASE_HISTORY_ACCEPTED=yes" in green.stdout,
              "the transcript records the acceptance rather than going quiet")
    except (OSError, subprocess.SubprocessError) as exc:
        skip(f"could not exercise scripts/release.sh here "
             f"({exc.__class__.__name__}: {exc})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_release_script_real_mode_publishes_by_pushing_this_repo():
    """The publish half, for real: the push IS the release (C-09, RELEASE-TESTS-2).

    Everything above stops at step 8. Steps 9 and 10 — the point of no return
    and the tag — were covered by nothing at all, which is how the tag gate sat
    on the far side of the push for as long as it did. So this arm runs the
    script in REAL mode against the fixture's on-disk bare `origin`: no
    network, a real `git push`, and afterwards the remote either has the commit
    or the test is red.

    `RELEASE_HISTORY_ACCEPTED=yes` is passed because the fixture's history is
    clean and the knob is therefore a no-op here — set deliberately so this arm
    tests the PUBLISH path and not the history gate, which
    `test_release_script_history_gate_blocks_and_takes_only_its_own_knob()`
    owns. Nothing in this repository is pushed anywhere: `dev` and `origin.git`
    are both inside a `mkdtemp` that the `finally` removes.
    """
    print("test_release_script_real_mode_publishes_by_pushing_this_repo")
    if not RELEASE_SH.is_file():
        check(False, "scripts/release.sh exists")
        return
    if shutil.which("git") is None:
        skip("git is not on PATH — the release script's gates cannot be exercised")
        return
    tmp = Path(tempfile.mkdtemp(prefix="release-real-"))
    try:
        dev, env, origin, stub_log = _release_fixture(tmp)
        script = dev / "scripts/release.sh"
        # Something to publish. The fixture's first commit is already on the
        # remote, and "nothing to push" is the OTHER arm below — a release with
        # a commit to publish has to be arranged, not assumed.
        (dev / PLUGIN_REL / "NOTICE.md").write_text(
            "a payload change to publish\n", encoding="utf-8")
        _git(["add", "-A"], dev, env)
        _git(["commit", "-q", "-m", "a payload change"], dev, env)
        head = _git(["rev-parse", "HEAD"], dev, env).stdout.strip()

        real = dict(env)
        real["RELEASE_CONFIRM"] = "yes"
        real["RELEASE_HISTORY_ACCEPTED"] = "yes"
        run = _run([script], dev, real)
        if run.returncode != 0:
            print(run.stdout)
        check(run.returncode == 0,
              f"a green fixture publishes end to end (rc={run.returncode})")
        check("tag dry run is clean" in _step_section(run.stdout, 8),
              "the tag gate is green BEFORE the push, where it can still stop it")
        check("pushed" in _step_section(run.stdout, 9),
              "step 9 says it pushed")
        remote_head = _git(["rev-parse", "refs/heads/main"], origin, env).stdout.strip()
        check(remote_head == head,
              f"the branch landed on the remote — the push IS the release "
              f"(remote {remote_head[:8]} vs HEAD {head[:8]})")
        # The tag is opt-in: `--tag-push` was not passed, so the only tag
        # invocation in the whole run must still be the dry run. A release that
        # tags without being asked cannot be repeated after a fix.
        tags = _tag_invocations(stub_log)
        check(tags and all("--dry-run" in t for t in tags),
              f"no tag was pushed without `--tag-push` (found: {tags})")

        # And running it again is not a second release: `git push` with nothing
        # to push must read as "already published", not as success or failure.
        again = _run([script], dev, real)
        if again.returncode != 0:
            print(again.stdout)
        check(again.returncode == 0,
              f"a second run is green (rc={again.returncode})")
        check("nothing to push" in _step_section(again.stdout, 9),
              "the second run reports nothing to push — the remote already has "
              "this commit, and only a version bump delivers an update")
    except (OSError, subprocess.SubprocessError) as exc:
        skip(f"could not exercise scripts/release.sh here "
             f"({exc.__class__.__name__}: {exc})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    for t in (test_claude_md_pointers_and_no_omnigent,
              test_claude_md_true_inventory,
              test_claude_md_serve_blocks_and_ports,
              test_claude_md_watcher_lifecycle,
              test_claude_md_names_the_pymongo_exception,
              test_plan_d8_is_split,
              test_inception_usage_correction,
              test_inception_truths,
              test_probes_recorded,
              test_readme_verb_table,
              test_readme_pause_is_always_qualified,
              test_readme_run_section,
              test_readme_mongo_disposition,
              test_no_published_mongo_port,
              test_control_semantics_doc,
              test_docs_agree_on_restart,
              test_register_is_reachable,
              # GD-U1/-U4/-U5 — the layout claims after the migration
              test_direction_docs_name_no_dead_claude_path,
              test_claude_md_layout_table_is_current,
              test_claude_md_records_the_single_hook_registration,
              test_claude_md_status_sh_fallback_is_the_real_one,
              test_entry_points_are_the_wrappers,
              test_shipped_docs_quote_measured_skill_costs,
              test_manifest_declares_both_skill_families,
              # C-11 / C-15 — the payload runs from a cache copy, and the
              # Releasing section is the only prose account of how it ships
              test_payload_docs_run_from_an_installed_copy,
              test_contributing_releasing_section_describes_this_model,
              # item 11 — the shipped README / CHANGELOG
              test_plugin_readme_install_and_update_commands,
              test_plugin_readme_trust_section,
              test_plugin_readme_network_guidance_is_generic,
              test_plugin_docs_carry_no_local_or_ladder_paths,
              test_plugin_changelog_top_entry_matches_manifest,
              # item 12 — scripts/release.sh
              test_release_script_exists_and_is_executable,
              test_release_script_uses_no_jq_and_never_copies_the_working_tree,
              test_release_script_is_the_checklist,
              test_release_script_avoids_the_shapes_that_reported_wrong_answers,
              test_release_script_gates_the_publish_half_before_the_point_of_no_return,
              test_release_script_check_mode_runs_its_gates_for_real,
              test_release_script_history_gate_blocks_and_takes_only_its_own_knob,
              test_release_script_real_mode_publishes_by_pushing_this_repo):
        t()
    print()
    for message in skips:
        print(f"skipped: {message}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all documentation guards passed ({len(skips)} skipped)")


if __name__ == "__main__":
    main()
