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
  `jq`, and never `cp -r`s the working tree into a release — the three ways
  that file stops being the thing it claims to be.

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

Item 12's last guard is the only one in this file that RUNS anything:
`release.sh --check` against a throwaway fixture repo, asserting what step 7
printed. Source-text guards cannot see a gate that is spelled plausibly and
still answers wrongly — `git config user.email` (which reads the global
cascade, not the release clone) is exactly that, and it survived three review
rounds behind a substring check. The fixture exists because the script's step 2
runs `tests/run_all.sh`, which runs this file: pointing it at this repository
would recurse forever.
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
from _roots import SRC   # noqa: E402  (the bytecode flag must precede the
                         # first import, so this one cannot sit with the rest)

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
PLUGIN = REPO / "plugin/touch"
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
#: Two PAYLOAD files are absent for a different and much less comfortable
#: reason: NO sub-plan of this migration owns their prose. Both live inside the
#: tree item 05 moved, and item 05 is a pure rename — editing them from here
#: would be the scope violation the migration's whole safety argument rests on
#: — while item 12 owns the documents listed below and not those. Three lines,
#: exhaustively, so the next reader does not stop at the first file:
#:   * `plugin/touch/shared/monitoring/monitoring.md:5` — "This module lives in
#:     `.claude/shared/monitoring/`", the module's own self-location;
#:   * `plugin/touch/shared/monitoring/monitoring.md:467` — the same path named
#:     as the non-authoritative state dir;
#:   * `plugin/touch/touch-visual/app.js:124` — a code comment pointing at
#:     `.claude/shared/monitoring/monitor_server.py` for the caching precedent.
#: All three ship to consumers. That is an OPEN, escalated gap, recorded here
#: and in this sub-plan's returned findings, not a guarded file and not a closed
#: one. Do NOT read the absence as coverage from somewhere else:
#: `tests/monitoring/test_shell.py` asserts the module's event schema and
#: lifecycle, and its only `.claude/shared/monitoring` mentions are `.gitignore`
#: arms — nothing there checks where the module lives. Whoever is handed those
#: files: fix the three lines, add `monitoring.md` to DIRECTION_DOCS, delete
#: this paragraph.
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
    text = read(PLUGIN_README)
    # Verbatim, because the `/plugin` UI never shows a README: whatever a user
    # types comes from this file, and a paraphrase does not run (DISTRIBUTION-7).
    for line in ("/plugin marketplace add msdrx/touch-plugin",
                 "/plugin install touch@msdrx-tools",
                 "/reload-plugins"):
        check(line in text, f"the shipped README carries the install line `{line}`")
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
    place to write "never `cp -r` the working tree into the release repo" or
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
    # The one legitimate copy, and what it copies out of: the git-built stage.
    copies = cp_commands(code)
    check(len(copies) == 1, f"exactly one `cp` invocation in the script (found {len(copies)}: {copies})")
    check(all("$STAGE" in c for c in copies),
          f"the only `cp` copies out of the git-built stage, never the working tree (found {copies})")
    # Stated positively as well as negatively: `$STAGE` present is satisfiable
    # by a second `cp` that ALSO names the working-tree subtree, and that copy
    # is the whole failure mode.
    working_tree = [c for c in copies if "$PLUGIN" in c or "plugin/touch" in c]
    check(not working_tree,
          f"no `cp` in the script names the working-tree subtree (bad: {working_tree})")
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
    # (PRIOR-AUDIT-12).
    for token in ("rev-parse origin/main", "rev-list --all --objects", "mytok",
                  "rotate", "release repo", "touch-selfcheck"):
        check(token in text, f"the preflight checklist names {token}")
    counted = [ln.strip()[:60] for ln in text.splitlines()
               if re.search(r"mytok|mongodb://", ln) and re.search(r"\b\d{2,}\b", ln)]
    check(not counted,
          f"no line pairs the contamination with a hard count — counts drift, commands do not (bad: {counted})")
    # The automated half, in order.
    for token in ("status --porcelain", "tests/run_all.sh", "CHANGELOG.md",
                  "plugin validate", "--strict", "--diff-filter=D",
                  "rev-list --all --count", "plugin tag", "--dry-run"):
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
    # The dev-remote gate compares repo identities, not URL spellings: the SSH
    # and HTTPS forms of the same repository must not compare unequal.
    check("norm_urls" in code and code.count("| norm_urls") >= 2,
          "both remote URL lists pass through the same normaliser before comparison")
    # Step 7's gates are only worth their comments if they actually run. Sniffing
    # for a `.git` DIRECTORY skips all of them in silence for a path INSIDE a
    # clone and for a linked worktree (whose `.git` is a file) — while steps 8-10
    # keep working, against the ENCLOSING repository: `git rm -rq .`, commit,
    # push. The repository must be resolved positively instead.
    check(re.search(r"-d\s+[\"']?\$(REL|\{REL\})/\.git", code) is None,
          "the release clone is not identified by sniffing for a `.git` directory "
          "(a worktree's is a file; a path inside a clone has none)")
    check("rev-parse --show-toplevel" in code,
          "the release clone is resolved with `rev-parse --show-toplevel` and must BE that toplevel")
    # Untracked-aware cleanliness is owed to BOTH trees. `git rm -rq .` removes
    # tracked files only, so anything untracked in the release clone survives it
    # and is published by the next `git add -A`.
    check(code.count("status --porcelain") >= 2,
          "the release clone gets the same untracked-aware clean check the dev repo does, "
          "before anything is written into it")
    # The identity gate, spelled discriminatingly. A bare `user.email` substring
    # passes for `git config user.email`, for `--global user.email`, and for a
    # line that merely mentions the key — and the bare spelling is exactly the
    # bug: `git config` reads the whole cascade, so on any machine with a global
    # identity it reports a per-repo identity that is not there, and the
    # operator's personal address lands in a public release commit. The flag IS
    # the gate, so the flag is what this asserts.
    check(re.search(r"config\s+--local\s+user\.email", code) is not None,
          "the release clone's identity is read with `config --local` — a bare "
          "`git config user.email` answers from the GLOBAL config and reports a "
          "per-repo identity that is not there")
    check(re.search(r"config\s+(?!--local\b)[^\n]*user\.email", code) is None,
          "no `git config … user.email` reads the cascade (only the --local form)")


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

    Two deliberate stubs. `claude` is a shell script that exits 0, which keeps
    step 6 hermetic and instant (`--check` never reaches step 10, and the
    manifests are validated for real by the payload gate, not here). The git
    GLOBAL config carries an identity that no local config repeats — that is
    the environment in which a bare `git config user.email` reports a per-repo
    identity that does not exist, and the only environment in which the
    `--local` fix is observable.
    """
    dev = tmp / "devrepo"
    (dev / "scripts").mkdir(parents=True)
    (dev / "tests").mkdir()
    (dev / "plugin/touch/.claude-plugin").mkdir(parents=True)
    shutil.copy2(RELEASE_SH, dev / "scripts/release.sh")
    runner = dev / "tests/run_all.sh"
    runner.write_text("#!/bin/sh\necho 'stub suite (fixture)'\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
    (dev / "plugin/touch/.claude-plugin/plugin.json").write_text(
        json.dumps({"name": "touch", "version": "0.1.0"}) + "\n", encoding="utf-8")
    (dev / "plugin/touch/.claude-plugin/marketplace.json").write_text(
        json.dumps({"name": "msdrx-tools",
                    "plugins": [{"name": "touch", "source": "./"}]}) + "\n",
        encoding="utf-8")
    (dev / "plugin/touch/CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.1.0\n", encoding="utf-8")

    stub_bin = tmp / "bin"
    stub_bin.mkdir()
    stub_claude = stub_bin / "claude"
    stub_claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub_claude.chmod(0o755)

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
    for var in ("RELEASE_CONFIRM", "RELEASE_COMMITS_EXPECTED",
                "GIT_DIR", "GIT_WORK_TREE"):
        env.pop(var, None)

    _git(["init", "-q", "-b", "main", "."], dev, env)
    _git(["add", "-A"], dev, env)
    _git(["commit", "-q", "-m", "fixture"], dev, env)
    return dev, env


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
    answers wrongly, which is what `git config user.email` (the whole cascade,
    not the release clone) did: it survived three review rounds behind a
    `"user.email" in code` substring, reporting "release clone has a per-repo
    user.email" about a clone that had none. So this arm runs the thing and
    reads what it printed.

    `--check` is side-effect-free by construction — it stops after step 7,
    commits nothing, pushes nothing — and the fixture is a temp repo, so the
    cost is a few seconds. Skips with a printed reason when git or a POSIX
    shell is unavailable, the same bargain the rest of the suite makes.
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
        dev, env = _release_fixture(tmp)
        script = dev / "scripts/release.sh"

        clone = tmp / "relclone"
        clone.mkdir()
        _git(["init", "-q", "-b", "main", "."], clone, env)
        clean = _run([script, "--check", "--release-clone", clone], dev, env)

        section = _step_section(clean.stdout, 7)
        # The transcript is printed only when it is evidence of a failure: a
        # passing guard that dumps sixty lines of someone else's output buries
        # the `ok:`/`FAIL:` ledger this suite is read by.
        if clean.returncode != 0:
            print(clean.stdout)
        check(clean.returncode == 0,
              f"a clean fixture passes every gate through step 7 (rc={clean.returncode})")
        # Step 7's gates once lived inside an `if [ -d "$REL/.git" ]` with no
        # `else`, so on a release clone git could still reach they printed
        # NOTHING and the run reported "every gate through step 7 is green".
        # An empty section is therefore a failure in its own right.
        check(len(_gate_lines(section)) >= 4,
              f"step 7 actually printed its gates ({len(_gate_lines(section))} lines)")
        check("no per-repo user.email" in section,
              "the identity advisory fires for a clone whose only identity is the GLOBAL "
              "git config — the environment where a release is really cut")
        check("resolves to its own repository root" in section,
              "the release clone is resolved positively, and says so")

        inside = tmp / "relclone/sub/dir"
        inside.mkdir(parents=True)
        rc_inside = _run([script, "--check", "--release-clone", inside], dev, env)
        check(rc_inside.returncode != 0 and "is INSIDE the repository" in rc_inside.stdout,
              "a path inside a clone is refused, not silently skipped "
              "(steps 8-9 would rewrite the enclosing repo)")

        rc_dev = _run([script, "--check", "--release-clone", dev], dev, env)
        check(rc_dev.returncode != 0
              and "resolves to this development repository" in rc_dev.stdout,
              "the development repository itself is refused as a release clone (GD-T3)")

        bare = tmp / "barerel.git"
        _git(["init", "-q", "--bare", str(bare)], tmp, env)
        rc_bare = _run([script, "--check", "--release-clone", bare], dev, env)
        check(rc_bare.returncode != 0 and "BARE repository" in rc_bare.stdout,
              "a bare repo is refused AS a bare repo, not misdiagnosed as 'not a git repository'")
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
              test_release_script_check_mode_runs_its_gates_for_real):
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
