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
* a `0.0.0.0` database and a published 27017 (GD-27).

A guard that only asserted the *presence* of good text would pass while the bad
text sat next to it, so the negative halves are the load-bearing ones.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
CLAUDE = REPO / "CLAUDE.md"
INCEPTION = REPO / "inception.md"
CONTROL_DOC = REPO / "docs/control-semantics.md"
MONGO_DOC = REPO / "docs/mongo.md"
PLAN = REPO / ".claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md"
PROBES = REPO / ".claude/local-orchestrators/touch-full-recon/report/probes.md"
REGISTER = REPO / ".claude/local-orchestrators/touch-full-recon/plan/findings-register.md"

failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def read(path):
    return path.read_text(encoding="utf-8", errors="replace")


def paragraphs(text):
    """Blank-line-separated blocks. Negation guards work per block, not per
    line: a wrapped sentence puts 'never' and the thing it forbids on
    different lines, and a line-local check would then fail on correct prose."""
    return re.split(r"\n\s*\n", text)


# --------------------------------------------------------------- R-05: CLAUDE.md
def test_claude_md_pointers_and_no_omnigent():
    print("test_claude_md_pointers_and_no_omnigent")
    text = read(CLAUDE)
    for token in ("inception.md", "touch-aggregator-plan.md",
                  "touch-full-recon-plan.md", "touch-orchestrate"):
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
    plan = read(PLAN)
    check("inside the paths being" in plan,
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
    text = read(PROBES)
    check(PROBES.is_file(), "probes.md exists (R-04's evidence artifact)")
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
    check(REGISTER.is_file(), "the findings register exists (R-06)")
    check("findings-register" in read(REGISTER).lower() or "R-06" in read(REGISTER),
          "the register identifies itself")


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
              test_register_is_reachable):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all documentation guards passed")


if __name__ == "__main__":
    main()
