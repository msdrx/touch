#!/usr/bin/env python3
"""Stdlib-only tests for `aggregator/costs.py` — the cost reader (D-21).

Run as `python3 test_cost.py`; exits non-zero on failure. No pytest, no runner
— `run_all.sh` picks it up by its `test_*.py` glob.

D-21's own test list, honoured one function each:

* **frozen mini-corpus → exact known totals.** `tests/cost-corpus/` carries a
  run whose numbers were computed by hand (see its `README.md`), and every one
  of them is asserted as an exact integer or to 1e-12. Not "roughly right": a
  cost reader that is approximately correct is a cost reader nobody can use to
  argue about a budget. The run is deliberately **resumed** — the same runId
  under two session directories — because a corpus with one session directory
  cannot tell a reader that reads the whole run from one that reads the
  launching session and stops.
* **no corpus → clean skip.** A checkout with no run history, an empty `wf_dir`
  and an archived session all report *absent* and exit 0. A release cut from a
  clean tree must not go red for having no history to price.
* **`scripts/release.sh` invokes it without network.** Source assertions on the
  gate: both arms present, the module-direct `PYTHONPATH=` form, no `curl` /
  `wget` / `pip` anywhere near it, and an absent reader SKIPs rather than fails.

Plus the clauses that list implies and would not otherwise be pinned: the
max-fold per `message.id` (a summing reader reports 210 output tokens where the
truth is 200), the run-union across a resumed run's session directories and the
scope fence that keeps a foreign project out of it, the TTL split cross-checked
against the write it describes, the 1-hour cache-write multiplier, `<synthetic>`
priced at zero rather than reported unpriced, an unknown model counted but NEVER
priced, the driver resolution from a `wf_dir` (and its refusal on a path of
another shape), and — because this module reads `~/.claude` — that a whole
analysis writes not one byte anywhere.

And the offline LEVEL half (LC-12), which is a different question from every
figure above it: `contextPeak` / `contextFinal` / `contextFinalTs` /
`contextByOwner` are how FULL a window was at an instant, not what a run spent.
Four clauses are pinned because each has a tempting wrong answer: the level is
the greatest-TIMESTAMP turn (not the largest, not the last dict entry — on a
corpus with no compaction those coincide on every run and then stop, silently);
it is non-monotonic, so `final` below `peak` is correct and is never clamped up;
an unmeasured level is **absent**, never `0`, down to an owner with nothing but
`<synthetic>` turns being missing from `contextByOwner` rather than present with
a zero; and the driver's level carries the scope that produced it, because a
sliced final turn is not "the driver's context now". The money block is pinned
byte-identical across the whole addition — the four keys live outside it.

Two of those clauses are pinned against the MONEY reading specifically, because
the level is a second read of the same bytes and the two are meant to disagree
there: a `null` component that money floors to 0 must yield no level at all
(24,502 tok of "nobody measured this" is the defect in miniature), and a
multi-iteration `usage` that money sums at the top level must read
`iterations[-1]` — one API call's prompt, not an aggregate of three.

The corpus freeze lives here rather than in `tests/fixtures/MANIFEST.sha256`:
that manifest has one owner and this sub-plan is not it, and the corpus is
synthesized rather than copied, which `tests/fixtures/PROVENANCE.md` forbids of
anything under its tree. The digests below are the freeze.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _roots                                                      # noqa: E402
sys.path.insert(0, str(_roots.SRC))

from aggregator import costs as costs_mod                          # noqa: E402

REPO = _roots.REPO
CORPUS = REPO / "tests" / "cost-corpus"
RUN_ID = "wf_c0570001-a1b"
#: The session the run LAUNCHED from, and the one it resumed into after a
#: `/clear`. Both hold a `…/subagents/workflows/<RUN_ID>/` directory and both
#: hold their own driver transcript — which is what a resumed run looks like on
#: disk (R-49), and what reading the anchor alone silently truncates.
SESSION = "c05715f0-0000-4000-8000-00000000c057"
SESSION2 = "c0572222-0000-4000-8000-000000002222"
#: The CO-TENANT run: a second runId under SESSION, sharing its one driver
#: transcript. Its whole reason for existing is that a session is not dedicated
#: to a run — 11 session directories on the machine this was written on hold
#: 2-3 runIds — and a corpus with one run per session cannot express the case
#: where an unbounded driver fold charges one session's turns to every run it
#: launched.
RUN_ID2 = "wf_c0570002-b2c"
PROJECT = CORPUS / "projects" / "-tmp-cost-fixture"
WF_DIR = PROJECT / SESSION / "subagents" / "workflows" / RUN_ID
WF_DIR2 = PROJECT / SESSION2 / "subagents" / "workflows" / RUN_ID
WF_DIR_COTENANT = PROJECT / SESSION / "subagents" / "workflows" / RUN_ID2
DRIVER = PROJECT / f"{SESSION}.jsonl"
DRIVER2 = PROJECT / f"{SESSION2}.jsonl"
RELEASE_SH = REPO / "scripts" / "release.sh"

#: The freeze. Regenerating these is a deliberate act, never a fix for a red
#: test: every expected total below was computed by hand from these exact
#: bytes, so a changed digest and an unchanged expectation cannot both be right.
FROZEN = {
    f"projects/-tmp-cost-fixture/{SESSION}.jsonl":
        "115f870fab084e1c594c7c5e661b79d26087bb13dafeb20f6688a13ec78739c6",
    f"projects/-tmp-cost-fixture/{SESSION}/subagents/workflows/{RUN_ID}/"
    "agent-a00000000000000a1.jsonl":
        "628adf5fb3cadc6d9ec9701de8341cb03685564dc105c3da2db683ef84c6abb2",
    f"projects/-tmp-cost-fixture/{SESSION}/subagents/workflows/{RUN_ID}/"
    "agent-a00000000000000a2.jsonl":
        "2cce5c490e6ea52fccbde2e6e4522b3e2eefcd327740e85793ece8e00d09f172",
    f"projects/-tmp-cost-fixture/{SESSION}/subagents/workflows/{RUN_ID2}/"
    "agent-b00000000000000b1.jsonl":
        "abc776db63e54830969e272cefa62fea1b65d449e1333aceec5eec8393a3f9ba",
    f"projects/-tmp-cost-fixture/{SESSION2}.jsonl":
        "b61dca61aaf98f45220db5910196271708c74be220fe425cdb6dea0b915cbd07",
    f"projects/-tmp-cost-fixture/{SESSION2}/subagents/workflows/{RUN_ID}/"
    "agent-a00000000000000a3.jsonl":
        "4971e39528138efc2319df1c22ae1903bbfb339b6a42594b9a232c746a709493",
}

#: Hand-computed from the corpus. The arithmetic, once, so a reader of this
#: file can check it without running anything:
#:
#:   SESSION 1 — agent 1 (claude-opus-5, $5 in / $25 out per MTok)
#:     msg_c1  in 100  cached 1000  write5m 2000  out  50   context 3100
#:     msg_c2  in  20  cached 5000  write    0    out 200   context 5020
#:             (written twice, out 10 then 200 — max-fold keeps 200)
#:     msg_c4  <synthetic>, all zero but out 1     context    0
#:   SESSION 1 — agent 2 (claude-fable-5, $10 in / $50 out per MTok)
#:     msg_c3  in  10  cached  500  write1h  100  out  25   context  610
#:   SESSION 2 — agent 3 (claude-opus-5), the slice a `/clear` moved
#:     msg_c5  in  40  cached  500  write5m  400  out 100   context  940
#:     msg_c6  in  20  cached 1000  write    0    out  60   context 1020
#:
#:   context-integral 3100 + 5020 + 0 + 610 + 940 + 1020 = 10690
#:   baseline/turn    median over agents of each one's first turn with NON-ZERO
#:                    context (price is irrelevant — an unpriced model that
#:                    read a prefix still seeds a baseline; a zero-context
#:                    `<synthetic>` turn does not)
#:                    = median(3100, 610, 940) = 940
#:   $ input       (100+20+40+20)/1e6*5 + 10/1e6*10           = 0.0010
#:   $ cache-read  (1000+5000+500+1000)/1e6*5*0.1
#:                 + 500/1e6*10*0.1                           = 0.00425
#:   $ cache-write (2000+400)/1e6*5*1.25 + 100/1e6*10*2.0     = 0.0170
#:   $ output      (50+200+100+60)/1e6*25 + 25/1e6*50         = 0.0115
#:                 (the synthetic's 1 output token prices at zero)
EXPECTED = {
    "agents": 3,
    "turns": 6,
    "contextIntegral": 10690,
    "baselinePerTurn": 940.0,
    "promptTokensPerAgent": 10690 / 3,
    "tokens": {"in": 190, "out": 436, "cached": 8000, "cache_write": 2500,
               "write5m": 2400, "write1h": 100},
    "dollars": {"input": 0.0010, "cacheRead": 0.00425,
                "cacheWrite": 0.0170, "output": 0.0115, "total": 0.03375},
}

#: What the SAME anchor reports with `--single-session` — i.e. what every
#: reading before the run-union fix reported by default. Kept as an expectation
#: rather than deleted, because "the escape hatch still returns one directory"
#: and "the default no longer does" are two claims and both need pinning.
EXPECTED_SOLO = {
    "agents": 2,
    "turns": 4,
    "contextIntegral": 8730,
    "baselinePerTurn": 1855.0,
    "tokens": {"in": 130, "out": 276, "cached": 6500, "cache_write": 2100,
               "write5m": 2000, "write1h": 100},
    "dollars": {"input": 0.0007, "cacheRead": 0.0035,
                "cacheWrite": 0.0145, "output": 0.0075, "total": 0.0262},
    "driverContextIntegral": 705,
    "driverShare": 705 / (8730 + 705),
}

#: The driver half, ONE transcript per session directory and both folded:
#:   session 1  in  5 + cached 300 + write5m 400 =  705 context, $0.00305
#:   session 2  in 10 + cached 700 + write5m 200 =  910 context, $0.00215
#: (5/1e6*5 + 300/1e6*0.5 + 400/1e6*6.25 + 15/1e6*25 = 0.00305; and
#:  10/1e6*5 + 700/1e6*0.5 + 200/1e6*6.25 + 20/1e6*25 = 0.00215.)
EXPECTED_DRIVER = {"contextIntegral": 1615, "turns": 2, "sessions": 2,
                   "total": 0.0052, "share": 1615 / (10690 + 1615)}

#: The CO-TENANT run (`RUN_ID2`), which shares SESSION's single driver
#: transcript with `RUN_ID` and runs two hours later:
#:   agent b1  in 60 + cached 400 + write5m 500 =  960 context
#:   driver    in 10 + cached 100 + write5m 200 =  310 context
#: The two runs' driver turns are two hours apart, so the run windows are
#: disjoint and each reading sees exactly one of them. That is the property
#: under test: `EXPECTED_DRIVER` above is UNCHANGED by this run's existence.
EXPECTED_COTENANT = {
    "agents": 1,
    "turns": 1,
    "contextIntegral": 960,
    # 60/1e6*5 + 400/1e6*0.5 + 500/1e6*6.25 + 30/1e6*25 = 0.004375
    "total": 0.004375,
    "driverContextIntegral": 310,
    "driverTurns": 1,
    # 10/1e6*5 + 100/1e6*0.5 + 200/1e6*6.25 + 20/1e6*25 = 0.00185
    "driverTotal": 0.00185,
}

#: The LEVEL half over the same corpus, hand-computed from the same rows. Every
#: agent record is stamped `2026-07-31T00:00:00.000Z`, so the "latest" turn is
#: decided by the timestamp tie-break — fold position, i.e. (path order, line
#: number): `msg_c6` is the last row of the last transcript read.
#:
#:   peak   = max(3100, 5020, 610, 940, 1020)          = 5020   (msg_c2)
#:   final  = the greatest (ts, fold position) turn    = 1020   (msg_c6)
#:   agent 1 (a1)  peak 5020 (msg_c2), final 5020 — msg_c4 is `<synthetic>`
#:                 with zero context and never qualifies as a LEVEL
#:   agent 2 (a2)  peak  610, final  610
#:   agent 3 (a3)  peak 1020, final 1020 (msg_c6 after msg_c5's 940)
#:
#: Note what is NOT here: `contextIntegral` (10,690) is a sum over all six
#: turns including the synthetic's zero, and the peak is not a term of it.
LEVEL_TS = "2026-07-31T00:00:00+00:00"
EXPECTED_LEVEL = {
    "contextPeak": 5020,
    "contextFinal": 1020,
    "contextFinalTs": LEVEL_TS,
    "contextByOwner": {
        "a00000000000000a1": {"peak": 5020, "final": 5020, "ts": LEVEL_TS},
        "a00000000000000a2": {"peak": 610, "final": 610, "ts": LEVEL_TS},
        "a00000000000000a3": {"peak": 1020, "final": 1020, "ts": LEVEL_TS},
    },
}

#: The summary keys that existed BEFORE the level half, and the four it added.
#: Named as two frozen sets so the regression arm can say the exact thing the
#: sub-plan promises — the addition is additive, nothing was renamed, nothing
#: moved into or out of the money block — rather than spot-checking a few.
MONEY_ERA_KEYS = frozenset({
    "agents", "turns", "contextIntegral", "baselinePerTurn", "baselineShare",
    "promptTokensPerAgent", "tokens", "dollars", "topReReadFiles",
    "unpricedModels", "files", "unusableUsage", "cacheWritesWithoutTtlSplit",
})
LEVEL_KEYS = frozenset({"contextPeak", "contextFinal", "contextFinalTs",
                        "contextByOwner"})

#: `--driver-whole-session` over SESSION's transcript folds BOTH runs' driver
#: turns: 705 (RUN_ID's) + 310 (RUN_ID2's) = 1015, plus session 2's 910 = 1925.
#: The number is not wrong — it is a different question, which is why the scope
#: is labelled and the rendering refuses to call it "of the run".
EXPECTED_WHOLE_SESSION_DRIVER = {"contextIntegral": 1925, "turns": 3}

failures = []
skips = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        failures.append(msg)
        print(f"  FAIL: {msg}")


def skip(msg):
    skips.append(msg)
    print(f"  SKIP: {msg}")


def close(got, want, msg, tol=1e-12):
    check(abs(got - want) <= tol, f"{msg} ({got!r} vs {want!r})")


def analyze_corpus(**kwargs):
    return costs_mod.analyze(wf_dir=str(WF_DIR), **kwargs)


def _driver_numbers(report):
    """The driver row minus the two fields that name paths, which follow the
    anchor rather than the run."""
    return {name: value for name, value in report["driver"].items()
            if name not in ("transcript", "transcripts")}


# --- the freeze ------------------------------------------------------------


def test_the_mini_corpus_is_frozen():
    print("test_the_mini_corpus_is_frozen")
    present = sorted(str(p.relative_to(CORPUS)) for p in CORPUS.rglob("*.jsonl"))
    check(present == sorted(FROZEN),
          f"the corpus holds exactly the frozen files (extra/missing: "
          f"{sorted(set(present) ^ set(FROZEN))})")
    for relative, digest in sorted(FROZEN.items()):
        path = CORPUS / relative
        if not path.is_file():
            check(False, f"{relative} is present")
            continue
        got = hashlib.sha256(path.read_bytes()).hexdigest()
        check(got == digest,
              f"{os.path.basename(relative)} is byte-identical to the freeze")
    check((CORPUS / "README.md").is_file(),
          "the corpus says in prose why it is not under tests/fixtures/")


# --- exact totals ----------------------------------------------------------


def test_exact_totals_over_the_frozen_corpus():
    print("test_exact_totals_over_the_frozen_corpus")
    report = analyze_corpus()
    check(report["corpus"] == "present", "the corpus is found from the wf_dir alone")
    check(report["runId"] == "wf_c0570001-a1b", "the runId is the wf_dir's basename")
    summary = report["agentsSummary"]
    for name in ("agents", "turns", "contextIntegral"):
        check(summary[name] == EXPECTED[name],
              f"{name} == {EXPECTED[name]} (got {summary[name]})")
    close(summary["baselinePerTurn"], EXPECTED["baselinePerTurn"],
          "baseline/turn is the median, across agents, of each agent's first "
          "turn with non-zero context")
    close(summary["baselineShare"],
          EXPECTED["baselinePerTurn"] * EXPECTED["turns"] / EXPECTED["contextIntegral"],
          "baseline share is baseline x turns / context-integral")
    close(summary["promptTokensPerAgent"], EXPECTED["promptTokensPerAgent"],
          "prompt tok/agent is the context-integral over the agent count")
    for name, want in sorted(EXPECTED["tokens"].items()):
        check(summary["tokens"][name] == want,
              f"tokens.{name} == {want} (got {summary['tokens'][name]})")
    for name, want in sorted(EXPECTED["dollars"].items()):
        close(summary["dollars"][name], want, f"dollars.{name}")
    check(summary["topReReadFiles"] ==
          [{"path": "/x/a.py", "reads": 4}, {"path": "/x/c.py", "reads": 2},
           {"path": "/x/b.py", "reads": 1}],
          f"the top re-read list is ranked by count (got {summary['topReReadFiles']})")
    check(summary["cacheWritesWithoutTtlSplit"] == 0,
          "every cache write in the corpus carries its TTL split, so none is assumed")
    check(summary["tokens"]["write5m"] + summary["tokens"]["write1h"]
          == summary["tokens"]["cache_write"],
          "the reported 5m/1h pair sums to the cache write it describes — the "
          "report can never contradict itself about how much was written")


def test_the_driver_row_and_its_share_of_the_run():
    print("test_the_driver_row_and_its_share_of_the_run")
    report = analyze_corpus()
    driver = report["driver"]
    check(driver is not None, "the driver transcript was found beside the session dir")
    if driver is None:
        return
    check(driver["transcripts"] == [str(DRIVER), str(DRIVER2)],
          f"the driver row names EVERY transcript it read — a resumed run has "
          f"one per session (got {driver.get('transcripts')})")
    check(driver["transcript"] == str(DRIVER),
          "and still names the launching session's first, for a reader who "
          "wants the one path")
    check(driver["sessions"] == EXPECTED_DRIVER["sessions"],
          f"over {EXPECTED_DRIVER['sessions']} sessions (got {driver.get('sessions')})")
    check(driver["contextIntegral"] == EXPECTED_DRIVER["contextIntegral"],
          f"the driver's context-integral is {EXPECTED_DRIVER['contextIntegral']} "
          f"(got {driver['contextIntegral']})")
    check(driver["turns"] == EXPECTED_DRIVER["turns"], "the driver's turn count")
    close(driver["dollars"]["total"], EXPECTED_DRIVER["total"], "the driver's dollars")
    close(report["driverShare"], EXPECTED_DRIVER["share"],
          "the driver share is driver / (agents + driver) — the shape the "
          "corrected 12.2 % prior was measured with")
    check(report["driverShare"] not in (0, None) and report["driverShare"] < 1,
          "the driver share is a proper fraction, never a hard-coded constant")
    # Both halves come from the same set of sessions. The single-session
    # reading of the same anchor gets BOTH wrong, in opposite directions, so
    # its share differs by much more than either half does.
    solo = costs_mod.analyze(wf_dir=str(WF_DIR), expand=False)
    check(abs(solo["driverShare"] - report["driverShare"]) > 0.05,
          f"and it is not the anchor-only share, which is wrong twice over "
          f"(union {report['driverShare']!r} vs anchor-only "
          f"{solo['driverShare']!r})")
    check(report["driverSessionsMissing"] == 0,
          "every session in this corpus still has its transcript on disk")


def test_a_resumed_run_is_folded_across_every_session_directory():
    print("test_a_resumed_run_is_folded_across_every_session_directory")
    # A `/clear` mid-run gives the process a new sessionId while the runId
    # stays, so the run's later agents — and its later driver turns — land
    # under a DIFFERENT session directory. Reading the anchor alone is not a
    # near-miss: on this machine it reports 5 agents of a run's 70.
    from_first = costs_mod.analyze(wf_dir=str(WF_DIR))
    from_second = costs_mod.analyze(wf_dir=str(WF_DIR2))
    check(from_first["runDirs"] == [str(WF_DIR), str(WF_DIR2)],
          f"the run is located as EVERY `…/workflows/<runId>/` under the "
          f"project, sorted (got {from_first['runDirs']})")
    check(from_first["sessionIds"] == sorted([SESSION, SESSION2]),
          f"and the sessions it spans are named, so a resumed run is visible "
          f"rather than merely correct (got {from_first['sessionIds']})")
    for name in ("agentsSummary", "driverShare", "runDirs", "sessionIds"):
        check(from_first[name] == from_second[name],
              f"naming EITHER session directory reads the same whole run "
              f"({name})")
    check(_driver_numbers(from_first) == _driver_numbers(from_second),
          "including every number on the driver row")
    check(sorted(from_first["driver"]["transcripts"])
          == sorted(from_second["driver"]["transcripts"])
          == [str(DRIVER), str(DRIVER2)],
          "and the same set of driver transcripts")
    # What legitimately DOES differ: which transcript is named first. It is the
    # ANCHOR's own, not the alphabetically lowest sessionId — `run_dirs_for`
    # sorts by path string, so a resumed run whose second session sorts first
    # would otherwise silently rename "the launching session" in the report.
    # SESSION < SESSION2 as strings, so an alphabetical answer cannot pass
    # both halves of this check.
    check(from_first["driver"]["transcript"] == str(DRIVER) and
          from_second["driver"]["transcript"] == str(DRIVER2),
          f"the driver transcript named first is the ANCHOR's, never whichever "
          f"sessionId sorts lowest (got {from_first['driver']['transcript']!r} "
          f"and {from_second['driver']['transcript']!r})")
    check(from_second["wfDir"] == str(WF_DIR2),
          "only the anchor the caller named differs between the two readings")
    # The escape hatch: an operator who means one directory gets one.
    solo = costs_mod.analyze(wf_dir=str(WF_DIR), expand=False)
    check(solo["runDirs"] == [str(WF_DIR)],
          f"--single-session reads the named directory alone (got {solo['runDirs']})")
    summary = solo["agentsSummary"]
    for name in ("agents", "turns", "contextIntegral"):
        check(summary[name] == EXPECTED_SOLO[name],
              f"and reports only that session's {name} == {EXPECTED_SOLO[name]} "
              f"(got {summary[name]})")
    close(summary["baselinePerTurn"], EXPECTED_SOLO["baselinePerTurn"],
          "including a baseline over that session's agents only")
    for name, want in sorted(EXPECTED_SOLO["tokens"].items()):
        check(summary["tokens"][name] == want,
              f"solo tokens.{name} == {want} (got {summary['tokens'][name]})")
    for name, want in sorted(EXPECTED_SOLO["dollars"].items()):
        close(summary["dollars"][name], want, f"solo dollars.{name}")
    check(solo["driver"]["sessions"] == 1 and
          solo["driver"]["contextIntegral"] == EXPECTED_SOLO["driverContextIntegral"],
          "and one driver transcript, not the run's other sessions'")
    close(solo["driverShare"], EXPECTED_SOLO["driverShare"],
          "with a share taken over that same one session, both halves")
    # The union is strictly bigger — the property that made the defect a
    # SILENT under-report rather than a visible error.
    check(from_first["agentsSummary"]["contextIntegral"]
          > summary["contextIntegral"],
          "the union is the larger number; the anchor-only reading under-reports")
    rendered = costs_mod.render(from_first)
    check("sessions        2" in rendered,
          f"and the human rendering says the run was resumed (got {rendered!r})")


def test_a_foreign_project_with_the_same_run_id_is_not_folded_in():
    print("test_a_foreign_project_with_the_same_run_id_is_not_folded_in")
    # `find_run_dirs` globs `projects/*/*` — every slug under the root, not
    # just this project's. Without the scope fence, another checkout that
    # happened to produce a `wf_<12hex>` of the same name would contribute its
    # agents and its driver to this run's totals.
    with tempfile.TemporaryDirectory(prefix="cost-foreign-") as tmp:
        root = Path(tmp)
        mine = (root / "projects" / "-tmp-cost-fixture" / SESSION
                / "subagents" / "workflows" / RUN_ID)
        theirs = (root / "projects" / "-tmp-somebody-else" / SESSION2
                  / "subagents" / "workflows" / RUN_ID)
        for directory in (mine, theirs):
            directory.mkdir(parents=True)
        source = WF_DIR / "agent-a00000000000000a2.jsonl"
        (mine / source.name).write_bytes(source.read_bytes())
        (theirs / "agent-a00000000000000a3.jsonl").write_bytes(
            (WF_DIR2 / "agent-a00000000000000a3.jsonl").read_bytes())
        found = costs_mod.run_dirs_for(str(mine))
        check(found == (str(mine),),
              f"only this project's slug is folded in (got {found})")
        report = costs_mod.analyze(wf_dir=str(mine))
        check(report["agentsSummary"]["agents"] == 1,
              f"so the foreign run's agent never lands in these totals "
              f"(got {report['agentsSummary']['agents']})")


def test_a_wf_dir_outside_any_projects_tree_is_read_exactly_as_given():
    print("test_a_wf_dir_outside_any_projects_tree_is_read_exactly_as_given")
    # The expansion is a best effort over a path shape SUBSTRATE-7 describes.
    # A directory of another shape is not an error and is not guessed about —
    # it is read as itself.
    with tempfile.TemporaryDirectory(prefix="cost-shapeless-") as tmp:
        odd = Path(tmp) / "subagents" / "workflows" / RUN_ID
        odd.mkdir(parents=True)
        check(costs_mod.claude_root_for(str(odd)) is None,
              "a path with no `projects/` above it resolves to no root")
        check(costs_mod.run_dirs_for(str(odd)) == (str(odd),),
              "and is read exactly as given, never expanded against a guess")
        check(costs_mod.run_dirs_for(None) == (),
              "no wf_dir is no run directories, not a cwd walk")
    check(costs_mod.claude_root_for(str(WF_DIR)) == str(CORPUS),
          f"the root is derived from the named path, not from $HOME "
          f"(got {costs_mod.claude_root_for(str(WF_DIR))})")


def test_the_streaming_duplicate_is_max_folded_not_summed():
    print("test_the_streaming_duplicate_is_max_folded_not_summed")
    fold = costs_mod.Fold()
    costs_mod.fold_transcript(WF_DIR / "agent-a00000000000000a1.jsonl", fold)
    turn = fold.turns.get("msg_c2")
    check(turn is not None, "the duplicated message id folds to ONE turn")
    if turn is None:
        return
    check(turn.tokens["out"] == 200,
          f"output is the MAX across repeats, not the sum (got {turn.tokens['out']}, "
          f"a summing reader would say 210)")
    check(turn.tokens["cached"] == 5000,
          f"the repeated cache read is folded too, not doubled "
          f"(got {turn.tokens['cached']})")
    check(len(fold.turns) == 3,
          f"agent 1 has three billed message ids, not four lines "
          f"(got {len(fold.turns)})")


def test_a_synthetic_turn_is_non_billable_never_unpriced():
    print("test_a_synthetic_turn_is_non_billable_never_unpriced")
    check(costs_mod.price_for("<synthetic>") == (0.0, 0.0),
          "`<synthetic>` prices at zero — a known state, not an unknown model")
    summary = analyze_corpus()["agentsSummary"]
    check(summary["unpricedModels"] == {},
          f"nothing in the corpus is reported unpriced "
          f"(got {summary['unpricedModels']})")
    check(summary["turns"] == EXPECTED["turns"],
          "the synthetic turn is still COUNTED as a turn (it happened)")


def test_an_unknown_model_is_counted_but_never_priced():
    print("test_an_unknown_model_is_counted_but_never_priced")
    check(costs_mod.price_for("claude-not-a-model-9") is None,
          "an unknown model has no price — None, never a guess")
    check(costs_mod.price_for(None) is None, "a missing model has no price")
    fold = costs_mod.Fold()
    fold.note_turn("msg_x", "claude-not-a-model-9", "agent",
                   {"in": 10, "out": 20, "cached": 30, "cache_write": 40},
                   {"write5m": 40, "write1h": 0})
    summary = costs_mod.summarize(fold)
    check(summary["tokens"]["in"] == 10 and summary["tokens"]["out"] == 20,
          "an unknown model's TOKENS are still counted")
    check(summary["contextIntegral"] == 80,
          f"its context still lands in the integral (got {summary['contextIntegral']})")
    close(summary["dollars"]["total"], 0.0,
          "its DOLLARS are zero — the reader never invents a rate")
    check(summary["unpricedModels"] == {"claude-not-a-model-9": 1},
          f"and it is named as unpriced (got {summary['unpricedModels']})")


def test_a_dated_model_id_resolves_to_the_model_it_names():
    print("test_a_dated_model_id_resolves_to_the_model_it_names")
    # The API returns dated ids for several models and `message.model` records
    # them verbatim — this repo's own transcripts carry 37 turns of
    # `claude-haiku-4-5-20251001`. Left unresolved they fall through to
    # "unpriced", their tokens counted and their DOLLARS silently dropped,
    # which is the exact under-reporting this module exists to end.
    for dated, bare in (("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
                        ("claude-opus-4-6-20260401", "claude-opus-4-6"),
                        ("claude-sonnet-4-6-20250929", "claude-sonnet-4-6")):
        check(costs_mod.price_for(dated) == costs_mod.PRICES[bare],
              f"{dated} prices as {bare} — a lookup miss fixed, not a rate guessed "
              f"(got {costs_mod.price_for(dated)})")
    check(costs_mod.price_for("claude-opus-5") == (5.0, 25.0),
          "the bare form is untouched by the normalization")
    # It can only ever remove a DATE. Anything else stays unknown, so the
    # normalization cannot degenerate into a prefix guess.
    for unknown in ("claude-not-a-model-9", "claude-not-a-model-20251001",
                    "claude-haiku-4-5-2025100", "claude-haiku-4-5-19991231",
                    "20251001", ""):
        check(costs_mod.price_for(unknown) is None,
              f"{unknown!r} is still unknown — None, never a nearest neighbour")
    fold = costs_mod.Fold()
    fold.note_turn("m", "claude-haiku-4-5-20251001", "a",
                   {"in": 1_000_000, "out": 1_000_000, "cached": 0, "cache_write": 0},
                   {"write5m": 0, "write1h": 0})
    summary = costs_mod.summarize(fold)
    check(summary["unpricedModels"] == {},
          f"so a dated turn is not reported unpriced (got {summary['unpricedModels']})")
    close(summary["dollars"]["total"], 6.0,
          "and its dollars land: $1 in + $5 out per MTok")


def test_the_two_diagnostics_are_counted_per_turn_not_per_record():
    print("test_the_two_diagnostics_are_counted_per_turn_not_per_record")
    # A streamed turn is written several times. Tokens are max-folded per
    # message id; the diagnostics have to fold the same way, or one turn's
    # missing TTL split is reported four times and the number grows with how
    # slowly the turn streamed.
    fold = costs_mod.Fold()
    message = {"id": "m", "model": "claude-opus-5",
               "usage": {"input_tokens": 0, "output_tokens": 0,
                         "cache_read_input_tokens": 0,
                         "cache_creation_input_tokens": 800}}
    for _ in range(4):
        costs_mod._fold_record({"type": "assistant", "message": message}, fold)
    check(fold.split_missing == 1,
          f"one streamed turn with no TTL split counts ONCE, not once per "
          f"record (got {fold.split_missing})")
    check(costs_mod.summarize(fold)["cacheWritesWithoutTtlSplit"] == 1,
          "and the summary says one turn, not four")
    # A split seen on ANY record of the turn settles it for the whole turn.
    mixed = costs_mod.Fold()
    costs_mod._fold_record({"type": "assistant", "message": message}, mixed)
    split_record = {"id": "m", "model": "claude-opus-5",
                    "usage": {"input_tokens": 0, "output_tokens": 0,
                              "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 800,
                              "cache_creation": {
                                  "ephemeral_5m_input_tokens": 300,
                                  "ephemeral_1h_input_tokens": 500}}}
    costs_mod._fold_record({"type": "assistant", "message": split_record}, mixed)
    check(mixed.split_missing == 0,
          f"a turn whose split arrived on a LATER record is measured, not "
          f"assumed (got {mixed.split_missing})")
    check(mixed.turns["m"].tokens["write1h"] == 500,
          "and the measured split is what prices it")
    # Same rule for a refused usage: the record is repeated, the turn is one.
    refused = costs_mod.Fold()
    broken = {"id": "m", "model": "claude-opus-5",
              "usage": {"input_tokens": 1.5, "output_tokens": 2}}
    for _ in range(3):
        costs_mod._fold_record({"type": "assistant", "message": broken}, refused)
    check(refused.unusable == 1,
          f"a refused turn written three times is ONE unusable turn "
          f"(got {refused.unusable})")
    anonymous = costs_mod.Fold()
    costs_mod._fold_record(
        {"type": "assistant", "message": {"usage": {"input_tokens": 1.5}}},
        anonymous)
    check(anonymous.unusable == 1,
          "a refused record with no id to fold on is still counted, not lost")


def test_a_zero_context_turn_never_drags_an_agents_baseline():
    print("test_a_zero_context_turn_never_drags_an_agents_baseline")
    # `<synthetic>` turns carry all-zero usage. One landing FIRST in an agent's
    # file would contribute a 0 to the median for an agent that read a full
    # prefix like every other.
    fold = costs_mod.Fold()
    zero = {"in": 0, "out": 1, "cached": 0, "cache_write": 0}
    real = {"in": 100, "out": 10, "cached": 900, "cache_write": 0}
    nosplit = {"write5m": 0, "write1h": 0}
    fold.note_turn("s1", "<synthetic>", "agent-1", zero, nosplit)
    fold.note_turn("r1", "claude-opus-5", "agent-1", real, nosplit)
    fold.note_turn("r2", "claude-opus-5", "agent-2", real, nosplit)
    summary = costs_mod.summarize(fold)
    check(summary["baselinePerTurn"] == 1000.0,
          f"the baseline is the first turn that actually READ something "
          f"(got {summary['baselinePerTurn']}, a zero-seeded median says 500)")
    check(summary["agents"] == 2,
          f"and the agent whose first turn was synthetic is still an agent "
          f"(got {summary['agents']})")
    empty = costs_mod.Fold()
    empty.note_turn("s", "<synthetic>", "agent-1", zero, nosplit)
    only = costs_mod.summarize(empty)
    check(only["baselinePerTurn"] == 0 and only["agents"] == 1,
          "an agent with nothing but zero-context turns has a zero baseline "
          "and is still counted — no exception, no crash")


# --- the offline LEVEL half (LC-12) ----------------------------------------


def _assistant(message_id, prompt, *, stamp=None, model="claude-opus-5"):
    """One assistant record whose three prompt components sum to ``prompt``.

    Split across all three on purpose: the level is
    `input + cache_read + cache_creation`, and a record that puts the whole
    figure in `input_tokens` would pass a reader that dropped either of the
    other two.
    """
    usage = {"input_tokens": prompt - (prompt // 2) - (prompt // 4),
             "cache_read_input_tokens": prompt // 2,
             "cache_creation_input_tokens": prompt // 4,
             "output_tokens": 7}
    record = {"type": "assistant",
              "message": {"id": message_id, "model": model, "usage": usage}}
    if stamp is not None:
        record["timestamp"] = stamp
    return record


def test_the_context_level_is_the_latest_turn_not_the_largest_or_the_last_one():
    print("test_the_context_level_is_the_latest_turn_not_the_largest_or_the_last_one")
    # THE trap this whole reading exists to avoid. Until something compacts,
    # the largest turn IS the latest turn on every run ever recorded, so `max`
    # passes every test anyone would think to write — and is wrong forever
    # afterwards, silently, in the one direction that overstates. This fold
    # makes the three candidate answers three different numbers: 9,000 is the
    # largest, 5,000 is the last dict entry, 3,000 is the truth.
    fold = costs_mod.Fold()
    # Folded in PATH order, which is NOT time order: a run spans several
    # transcripts, they are read sorted by name, and the last one read can hold
    # the oldest rows.
    costs_mod._fold_record(_assistant("msg_big", 9000,
                                      stamp="2026-07-31T10:00:00.000Z"), fold)
    costs_mod._fold_record(_assistant("msg_latest", 3000,
                                      stamp="2026-07-31T12:00:00.000Z"), fold)
    costs_mod._fold_record(_assistant("msg_oldest", 5000,
                                      stamp="2026-07-31T09:00:00.000Z"), fold)
    summary = costs_mod.summarize(fold)
    check(summary["contextFinal"] == 3000,
          f"the final level is the greatest-TIMESTAMP turn — not the largest "
          f"(9000) and not the last entry folded (5000) "
          f"(got {summary['contextFinal']})")
    check(summary["contextFinalTs"] == "2026-07-31T12:00:00+00:00",
          f"stamped with that record's OWN timestamp, never a wall clock "
          f"(got {summary['contextFinalTs']})")
    check(summary["contextPeak"] == 9000,
          f"the peak is the max over the same turns (got {summary['contextPeak']})")
    check(summary["contextFinal"] < summary["contextPeak"],
          "a level BELOW its own peak is correct, not a bug: occupancy is "
          "non-monotonic — a compaction lowers it — and `final` is never "
          "clamped up to `peak`")
    check(summary["contextIntegral"] == 17000,
          f"and the integral still sums all three, untouched by any of it "
          f"(got {summary['contextIntegral']})")
    # The timestamp tie-break: fold position, i.e. (path order, line number).
    # Every record in the frozen corpus shares one millisecond, so without this
    # the answer there would depend on dict iteration order.
    #
    # Asserted in BOTH arrangements, and that is the whole point of the pair.
    # One arrangement cannot tell "later fold position wins" from three other
    # rules that happen to agree with it — a `>=` on the timestamp with no
    # ordinal term at all, "largest wins", "first folded wins". Swapping the
    # two turns and demanding the answer swap with them kills all three at
    # once, because no rule but fold position answers 2000 here and 4000 there.
    for first, second, want in ((4000, 2000, 2000), (2000, 4000, 4000)):
        tied = costs_mod.Fold()
        costs_mod._fold_record(_assistant("msg_first", first,
                                          stamp="2026-07-31T10:00:00.000Z"), tied)
        costs_mod._fold_record(_assistant("msg_second", second,
                                          stamp="2026-07-31T10:00:00.000Z"), tied)
        got_tie = costs_mod.summarize(tied)["contextFinal"]
        check(got_tie == want,
              f"two turns stamped to the same millisecond resolve by fold "
              f"position — the order the bytes are on disk, not the order a "
              f"dict iterates, not the larger of the two, not the first seen "
              f"(folded {first} then {second}, wanted {want}, got {got_tie})")
    # A turn nothing could PLACE cannot win "latest" — the same floor direction
    # `note_undated` takes — but it is still a real reading, so it counts for
    # the peak, which needs no ordering at all.
    undated = costs_mod.Fold()
    costs_mod._fold_record(_assistant("msg_dated", 1000,
                                      stamp="2026-07-31T10:00:00.000Z"), undated)
    costs_mod._fold_record(_assistant("msg_undated", 8000), undated)
    got = costs_mod.summarize(undated)
    check(got["contextFinal"] == 1000 and got["contextFinalTs"] is not None,
          f"an undated turn never becomes 'the latest' by default "
          f"(got {got['contextFinal']})")
    check(got["contextPeak"] == 8000,
          f"but it still counts for the peak, which needs no ordering "
          f"(got {got['contextPeak']})")


def test_an_unmeasured_context_level_is_absent_never_zero():
    print("test_an_unmeasured_context_level_is_absent_never_zero")
    # The defining discipline: unknown is spelled as an ABSENCE. A fabricated
    # or guessed number on a card is worse than showing nothing, and `0` is the
    # most fabricable number there is — `<synthetic>` turns carry all-zero
    # usage and a transcript can END on one, so a reader that took the last
    # turn unconditionally reports a busy agent as empty.
    synthetic = costs_mod.Fold()
    costs_mod._fold_record(
        {"type": "assistant", "timestamp": "2026-07-31T10:00:00.000Z",
         "message": {"id": "msg_s", "model": "<synthetic>",
                     "usage": {"input_tokens": 0, "output_tokens": 1,
                               "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0}}}, synthetic)
    summary = costs_mod.summarize(synthetic)
    check(summary["contextPeak"] is None and summary["contextFinal"] is None
          and summary["contextFinalTs"] is None,
          f"a fold whose only turn is `<synthetic>` reports None, never 0 "
          f"(got peak={summary['contextPeak']!r}, "
          f"final={summary['contextFinal']!r})")
    check(summary["contextByOwner"] == {},
          f"and the owner is ABSENT from contextByOwner rather than present "
          f"with a zero (got {summary['contextByOwner']!r})")
    check(summary["agents"] == 1 and summary["turns"] == 1,
          f"while still being counted as an agent that took a turn — the "
          f"absence is about the LEVEL, not about the agent "
          f"(got {summary['agents']} agents, {summary['turns']} turns)")
    # A mixed fold: the agent with a real turn is present, the synthetic-only
    # one is not, and neither answer is a zero.
    mixed = costs_mod.Fold()
    costs_mod._fold_record(
        dict(_assistant("msg_real", 2000, stamp="2026-07-31T10:00:00.000Z"),
             agentId="busy"), mixed)
    costs_mod._fold_record(
        {"type": "assistant", "timestamp": "2026-07-31T10:00:00.000Z",
         "agentId": "quiet",
         "message": {"id": "msg_s2", "model": "<synthetic>",
                     "usage": {"input_tokens": 0, "output_tokens": 1,
                               "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0}}}, mixed)
    by_owner = costs_mod.summarize(mixed)["contextByOwner"]
    check(sorted(by_owner) == ["busy"],
          f"one owner measured, one owner unknown, and unknown is an absent "
          f"key (got {sorted(by_owner)})")
    check(by_owner["busy"] == {"peak": 2000, "final": 2000,
                               "ts": "2026-07-31T10:00:00+00:00"},
          f"the measured one carries its peak, its level and the instant it "
          f"was read at (got {by_owner['busy']})")
    # An id that is not a real `msg_…` is not a billed assistant turn, and the
    # level rule says so in as many words. The money block still counts it.
    aliased = costs_mod.Fold()
    costs_mod._fold_record(_assistant("m1", 2000,
                                      stamp="2026-07-31T10:00:00.000Z"), aliased)
    alias_summary = costs_mod.summarize(aliased)
    check(alias_summary["contextFinal"] is None
          and alias_summary["contextIntegral"] == 2000,
          f"a non-`msg_` id yields no level, while the integral still counts "
          f"it (got final={alias_summary['contextFinal']!r}, "
          f"integral={alias_summary['contextIntegral']})")
    # The human rendering says nothing rather than something wrong: a
    # fixed-width row has no spelling of "unknown" a reader will not quote back
    # as a number.
    blank = costs_mod.render({"corpus": "present", "runId": "wf_x",
                              "agentsSummary": summary, "driver": None})
    check("context final" not in blank,
          f"and `render` prints no level line at all when none was measured "
          f"(got {blank!r})")


def test_the_level_refuses_what_the_money_reading_floors():
    print("test_the_level_refuses_what_the_money_reading_floors")
    # The level is a SECOND reading of the same bytes, and it has to be: the
    # money reader coerces a `null` component to 0, which is the right floor
    # for a sum and a fabricated measurement for an instant. A row that says
    # nothing must produce no level — while still being counted as a turn and
    # still contributing its (floored) tokens to the integral, because the
    # money block's job is unchanged.
    stamp = "2026-07-31T10:00:00.000Z"

    def _fold_usage(usage):
        one = costs_mod.Fold()
        costs_mod._fold_record(
            {"type": "assistant", "timestamp": stamp,
             "message": {"id": "msg_n", "model": "claude-opus-5",
                         "usage": usage}}, one)
        return costs_mod.summarize(one)

    nulled = _fold_usage({"input_tokens": 24502, "output_tokens": 9,
                          "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": None})
    check(nulled["contextFinal"] is None and nulled["contextPeak"] is None
          and nulled["contextFinalTs"] is None
          and nulled["contextByOwner"] == {},
          f"a `null` component yields NO level — 24,502 tok of it is a number "
          f"nobody measured, and publishing one is the whole defect this "
          f"reading exists to refuse (got final={nulled['contextFinal']!r}, "
          f"peak={nulled['contextPeak']!r})")
    check(nulled["turns"] == 1 and nulled["contextIntegral"] == 24502,
          f"while the money block reads it exactly as it always did: one turn, "
          f"the null floored to 0 (got {nulled['turns']} turns, integral "
          f"{nulled['contextIntegral']})")
    # A `bool` is an `int` SUBCLASS, so `isinstance` would read `true` as 1 —
    # and a float is not a token count at all. Both refuse the row outright for
    # money too, so the level's absence here is belt-and-braces; it is pinned
    # because the two readers must not drift apart on it.
    for bad in ({"input_tokens": 100, "output_tokens": 9,
                 "cache_creation_input_tokens": True,
                 "cache_read_input_tokens": 0},
                {"input_tokens": 12.5, "output_tokens": 9,
                 "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": 0}):
        got = _fold_usage(bad)
        check(got["contextFinal"] is None and got["contextPeak"] is None,
              f"a bool and a float are not token counts and yield no level "
              f"(usage {bad}, got {got['contextFinal']!r})")
    # And a component that simply is not there: three keys are the arithmetic,
    # so two of them is not a reading of it.
    partial = _fold_usage({"input_tokens": 5000, "output_tokens": 9,
                           "cache_read_input_tokens": 1000})
    check(partial["contextFinal"] is None,
          f"a `usage` missing a component says nothing about the whole prompt "
          f"(got {partial['contextFinal']!r})")


def test_the_level_reads_one_api_call_out_of_a_multi_iteration_usage():
    print("test_the_level_reads_one_api_call_out_of_a_multi_iteration_usage")
    # GD-LC-2's last clause, and the one place money and the level are SUPPOSED
    # to disagree. When `usage.iterations` holds more than one entry the top
    # level aggregates several API calls: every one of them was billed, so the
    # sum is the right bill — and it is a prompt that never existed, so it is
    # the wrong level. The level reads `iterations[-1]`, which is unambiguously
    # one call's prompt whichever way the top level was computed.
    fold = costs_mod.Fold()
    costs_mod._fold_record(
        {"type": "assistant", "timestamp": "2026-07-31T10:00:00.000Z",
         "message": {"id": "msg_it", "model": "claude-opus-5",
                     "usage": {"input_tokens": 300, "output_tokens": 40,
                               "cache_creation_input_tokens": 1200,
                               "cache_read_input_tokens": 4500,
                               "iterations": [
                                   {"input_tokens": 100,
                                    "cache_creation_input_tokens": 1000,
                                    "cache_read_input_tokens": 1000},
                                   {"input_tokens": 100,
                                    "cache_creation_input_tokens": 100,
                                    "cache_read_input_tokens": 1500},
                                   {"input_tokens": 100,
                                    "cache_creation_input_tokens": 100,
                                    "cache_read_input_tokens": 2000}]}}}, fold)
    summary = costs_mod.summarize(fold)
    check(summary["contextFinal"] == 2200 and summary["contextPeak"] == 2200,
          f"the level is the LAST iteration's prompt (100 + 100 + 2000), not "
          f"the 6,000-token aggregate of three calls — 2.73× high here, and "
          f"in the overstating direction (got {summary['contextFinal']})")
    check(summary["contextIntegral"] == 6000,
          f"while the integral keeps the top-level sum, because every one of "
          f"those iterations was billed and the money arithmetic is unchanged "
          f"(got {summary['contextIntegral']})")
    # A `len == 1` list, and no list at all, both read the top level — which is
    # how every sampled row on this machine behaves.
    for iterations in ([{"input_tokens": 1, "cache_creation_input_tokens": 1,
                         "cache_read_input_tokens": 1}], None):
        usage = {"input_tokens": 500, "output_tokens": 5,
                 "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": 1500}
        if iterations is not None:
            usage["iterations"] = iterations
        single = costs_mod.Fold()
        costs_mod._fold_record(
            {"type": "assistant", "timestamp": "2026-07-31T10:00:00.000Z",
             "message": {"id": "msg_one", "model": "claude-opus-5",
                         "usage": usage}}, single)
        got = costs_mod.summarize(single)["contextFinal"]
        check(got == 2000,
              f"a single-entry `iterations` (and an absent one) reads the TOP "
              f"level (wanted 2000, got {got})")
    # A malformed `iterations` is refused rather than fallen back on: falling
    # back to the aggregate would be answering the question with the number the
    # clause exists to reject.
    junk = costs_mod.Fold()
    costs_mod._fold_record(
        {"type": "assistant", "timestamp": "2026-07-31T10:00:00.000Z",
         "message": {"id": "msg_junk", "model": "claude-opus-5",
                     "usage": {"input_tokens": 500, "output_tokens": 5,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 1500,
                               "iterations": ["nonsense", "more"]}}}, junk)
    check(costs_mod.summarize(junk)["contextFinal"] is None,
          "an `iterations` list this reader cannot parse yields no level, "
          "rather than silently falling back to the aggregate it rejects")


def test_the_level_keys_are_additive_and_the_money_block_is_untouched():
    print("test_the_level_keys_are_additive_and_the_money_block_is_untouched")
    # The regression arm. Every figure the money era pinned by hand is asserted
    # again, unchanged, beside the four new keys — because "levels were added"
    # and "the cost reader still reports the same cost" are two claims and the
    # second is the one a release gate leans on.
    summary = analyze_corpus()["agentsSummary"]
    check(set(summary) == MONEY_ERA_KEYS | LEVEL_KEYS,
          f"the summary gained exactly four keys and renamed none "
          f"(unexpected: {sorted(set(summary) ^ (MONEY_ERA_KEYS | LEVEL_KEYS))})")
    check(not (LEVEL_KEYS & set(summary["dollars"])),
          f"and not one of them landed inside the money block, whose "
          f"documented invariant — every figure is a floor — is a claim about "
          f"a SUM and says nothing about an instant "
          f"(got {sorted(summary['dollars'])})")
    check(summary["contextIntegral"] == EXPECTED["contextIntegral"],
          f"the context-integral is byte-identical to its hand-computed value "
          f"({EXPECTED['contextIntegral']}, got {summary['contextIntegral']})")
    close(summary["baselinePerTurn"], EXPECTED["baselinePerTurn"],
          "so is the baseline")
    for name, want in sorted(EXPECTED["dollars"].items()):
        close(summary["dollars"][name], want, f"and every dollar figure ({name})")
    for name, want in sorted(EXPECTED["tokens"].items()):
        check(summary["tokens"][name] == want,
              f"and every token total (tokens.{name} == {want}, got "
              f"{summary['tokens'][name]})")
    for name, want in sorted(EXPECTED_LEVEL.items()):
        check(summary[name] == want,
              f"{name} == {want!r} (got {summary[name]!r})")
    # The two questions are different arithmetic over the same turns. Asserting
    # `peak < integral` would be asserting arithmetic — a max over terms of a
    # sum of positives cannot exceed it — so the claim made here is the one a
    # reader could actually get wrong: the peak is not a TERM of the integral
    # you would reach for (it is not the last turn's, nor the largest owner's
    # total), and the two are not each other's sanity check.
    check(summary["contextPeak"] != summary["contextIntegral"]
          and summary["contextPeak"] != summary["contextFinal"]
          and summary["contextPeak"] != sum(
              one["final"] for one in summary["contextByOwner"].values()),
          f"the peak is its own number: not the integral "
          f"({summary['contextIntegral']}), not the final level "
          f"({summary['contextFinal']}), not a sum across owners — a level is "
          f"never an aggregate of levels (got {summary['contextPeak']})")
    # The one `render()` line, and the rule about where it may sit: a reader
    # scanning a money column reads every number in it as money.
    rendered = costs_mod.render(analyze_corpus()).splitlines()
    matches = [i for i, line in enumerate(rendered) if "context final" in line]
    check(len(matches) == 1,
          f"the level is rendered on exactly ONE line (got {len(matches)})")
    if matches:
        row = matches[0]
        check(f"{EXPECTED_LEVEL['contextFinal']:,} tok" in rendered[row]
              and EXPECTED_LEVEL["contextFinalTs"] in rendered[row],
              f"which carries the number and the instant it was read at "
              f"(got {rendered[row]!r})")
        # …and WHOSE it is. Every other figure in that block is a run-wide
        # fact; this one is one agent's window, and a line that did not say so
        # would be quoted back as "the run's context".
        check("(a00000000000000a3)" in rendered[row],
              f"and names the agent it belongs to, because a level is never a "
              f"run-wide aggregate (got {rendered[row]!r})")
        neighbours = [rendered[i].strip() for i in (row - 1, row + 1)
                      if 0 <= i < len(rendered)]
        check(not any(one.startswith("$") for one in neighbours),
              f"and never sits next to a dollar figure (got {neighbours})")


def test_the_drivers_level_carries_the_scope_that_produced_it():
    print("test_the_drivers_level_carries_the_scope_that_produced_it")
    # A sliced final turn is not "the driver's context now". The driver fold is
    # bounded to the run's window, so the close-out tail after the last agent
    # record is outside it — and under `--driver-whole-session` the last turn
    # may belong to another run altogether. Either way the number needs the
    # label that produced it.
    report = analyze_corpus()
    driver = report["driver"]
    check(driver.get("contextScope") == report["driverScope"] == "run window",
          f"the driver's level inherits the scope beside it "
          f"(got {driver.get('contextScope')!r} vs "
          f"{report['driverScope']!r})")
    check("contextFinalScope" not in driver,
          "and the key is named for the BLOCK it scopes, not for one of the "
          "four keys in it — all four are computed over the same sliced fold, "
          "so a `contextFinal`-shaped name would leave a reader of "
          "`contextPeak` looking for a label that was never written")
    check(driver["contextFinal"] == 910 and driver["contextPeak"] == 910,
          f"and is measured over the same slice the driver's dollars are "
          f"(got final={driver['contextFinal']}, peak={driver['contextPeak']})")
    whole = analyze_corpus(whole_session=True)
    check(whole["driver"].get("contextScope") == whole["driverScope"]
          == "whole session",
          f"declining the slice relabels the level too, rather than leaving it "
          f"claiming to be the run's (got "
          f"{whole['driver'].get('contextScope')!r})")
    # And it is never PRINTED as a bare number: the human row is one line of
    # dollars and turns, and an unlabelled level in it would read as the run's.
    rendered = costs_mod.render(report)
    check(rendered.count("context final") == 1,
          f"the rendering prints the run's level once and the driver's not at "
          f"all — the label lives in the JSON, the number stays out of the "
          f"money row (got {rendered.count('context final')})")


def test_the_rendering_reports_every_degradation_it_measured():
    print("test_the_rendering_reports_every_degradation_it_measured")
    # `render()` is what lands in a release transcript; the JSON is read by
    # nobody. A degradation the JSON carries and the text drops is hidden.
    fold = costs_mod.Fold()
    costs_mod._fold_record(
        {"type": "assistant",
         "message": {"id": "m1", "model": "claude-opus-5",
                     "usage": {"input_tokens": 0, "output_tokens": 0,
                               "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 800}}}, fold)
    costs_mod._fold_record(
        {"type": "assistant",
         "message": {"id": "m2", "usage": {"input_tokens": 1.5}}}, fold)
    fold.note_turn("m3", "", "a", {"in": 5, "out": 5, "cached": 0,
                                   "cache_write": 0}, {"write5m": 0, "write1h": 0})
    summary = costs_mod.summarize(fold)
    check(summary["unusableUsage"] == 1, "the refused turn is in the summary")
    check(summary["unpricedModels"] == {"(no model recorded)": 1},
          f"a turn with no model recorded is NAMED, not rendered as a blank "
          f"(got {summary['unpricedModels']})")
    rendered = costs_mod.render({"corpus": "present", "runId": "wf_x",
                                 "agentsSummary": summary, "driver": None})
    check("unusable usage: 1" in rendered,
          f"and the human rendering says so out loud (got {rendered!r})")
    check("no TTL split: 1" in rendered,
          "beside the TTL-split warning it already printed")
    check("x1" in rendered and "unpriced models  x1" not in rendered,
          "and the unpriced line never renders a blank name before its count")
    # A driver share of exactly zero is a MEASUREMENT, not an absence.
    zero_share = costs_mod.render({
        "corpus": "present", "runId": "wf_x", "agentsSummary": summary,
        "driver": {"turns": 3, "contextIntegral": 0,
                   "dollars": {"total": 0.0}},
        "driverShare": 0.0, "driverScope": "run window"})
    check("0.0 % of the run" in zero_share,
          f"a 0.0 % driver share prints as 0.0 %, not as a dropped clause "
          f"(got {zero_share!r})")
    # ...and the SAME number under a whole-session scope must not claim to be
    # "of the run" — that clause is the one a reader quotes.
    whole = costs_mod.render({
        "corpus": "present", "runId": "wf_x", "agentsSummary": summary,
        "driver": {"turns": 3, "contextIntegral": 0,
                   "dollars": {"total": 0.0}},
        "driverShare": 0.0, "driverScope": "whole session"})
    check("% of the run" not in whole,
          f"a whole-session driver figure never renders as '% of the run' "
          f"(got {whole!r})")
    check("WHOLE-SESSION figure" in whole and "scope: whole session" in whole,
          f"it says what it actually is, twice (got {whole!r})")
    # The rule above applies to BOTH folds or it is not a rule: `summarize`
    # measures the same three degradations for the driver and puts them in the
    # JSON, so a rendering that reads them off the agent summary only hides
    # exactly what it promises to report. Latent on today's corpora — which is
    # why it is pinned rather than waited for.
    degraded = costs_mod.render({
        "corpus": "present", "runId": "wf_x", "agentsSummary": summary,
        "driver": {"turns": 3, "contextIntegral": 10,
                   "dollars": {"total": 0.0},
                   "unpricedModels": {"claude-not-a-model-9": 2},
                   "cacheWritesWithoutTtlSplit": 4,
                   "unusableUsage": 7},
        "driverShare": 0.5, "driverScope": "run window"})
    check("driver unpriced models claude-not-a-model-9 x2" in degraded,
          f"the driver's unpriced models are named, not just JSON "
          f"(got {degraded!r})")
    check("driver cache writes with no TTL split: 4" in degraded,
          "so is its assumed-TTL count")
    check("driver turns with unusable usage: 7" in degraded,
          "so is its refused-usage count")
    check(costs_mod._degradations({}) == [],
          "and a clean summary renders no degradation line at all")


def test_the_cache_write_ttl_split_is_priced_at_two_different_rates():
    print("test_the_cache_write_ttl_split_is_priced_at_two_different_rates")
    check(costs_mod.CACHE_READ_MULTIPLIER == 0.1,
          "a cache read bills at 0.1x the input rate")
    check(costs_mod.CACHE_WRITE_5M_MULTIPLIER == 1.25,
          "a 5-minute cache write bills at 1.25x the input rate")
    check(costs_mod.CACHE_WRITE_1H_MULTIPLIER == 2.0,
          "a 1-hour cache write bills at 2x the input rate")
    check(costs_mod.PRICES["claude-opus-5"] == (5.0, 25.0),
          "Opus 5 is $5 / $25 per MTok")
    check(costs_mod.PRICES["claude-fable-5"] == (10.0, 50.0),
          "Fable 5 is $10 / $50 per MTok — 2x Opus, which is why the mix matters")
    check(costs_mod.PRICES["claude-haiku-4-5"] == (1.0, 5.0),
          "Haiku 4.5 is $1 / $5 per MTok")
    # Sonnet 5 is the one row that is not the sticker: the skill records
    # "$3.00 ($2.00 intro through 2026-08-31)", and this module bills the
    # floor, so it carries the introductory pair and the date it lapses.
    check(costs_mod.PRICES["claude-sonnet-5"] == (2.0, 10.0),
          f"Sonnet 5 carries its introductory $2 / $10, not the $3 / $15 "
          f"sticker (got {costs_mod.PRICES['claude-sonnet-5']})")
    check(costs_mod.SONNET_5_INTRO_RATE_ENDS == "2026-08-31",
          "and the date that rate lapses is named, not buried")
    check(all(len(pair) == 2 and all(isinstance(one, float) for one in pair)
              for pair in costs_mod.PRICES.values()),
          "every row is an (input, output) pair of floats per MTok")
    # Same tokens, different TTL: the 1h write must cost 1.6x the 5m one.
    five = costs_mod.Fold()
    five.note_turn("m", "claude-opus-5", "a",
                   {"in": 0, "out": 0, "cached": 0, "cache_write": 1_000_000},
                   {"write5m": 1_000_000, "write1h": 0})
    hour = costs_mod.Fold()
    hour.note_turn("m", "claude-opus-5", "a",
                   {"in": 0, "out": 0, "cached": 0, "cache_write": 1_000_000},
                   {"write5m": 0, "write1h": 1_000_000})
    close(costs_mod.summarize(five)["dollars"]["cacheWrite"], 6.25,
          "1 MTok of 5-minute cache write on Opus 5")
    close(costs_mod.summarize(hour)["dollars"]["cacheWrite"], 10.0,
          "1 MTok of 1-hour cache write on Opus 5")


def test_a_cache_write_with_no_ttl_split_is_assumed_and_counted():
    print("test_a_cache_write_with_no_ttl_split_is_assumed_and_counted")
    fold = costs_mod.Fold()
    message = {"id": "m", "model": "claude-opus-5",
               "usage": {"input_tokens": 0, "output_tokens": 0,
                         "cache_read_input_tokens": 0,
                         "cache_creation_input_tokens": 800}}
    costs_mod._fold_record({"type": "assistant", "message": message}, fold)
    check(fold.split_missing == 1,
          f"the missing split is COUNTED, not silently assumed "
          f"(got {fold.split_missing})")
    turn = fold.turns["m"]
    check(turn.tokens["write5m"] == 0 and turn.tokens["write1h"] == 0,
          f"the FOLD carries only what was measured — it never writes the "
          f"assumption in, where a later record's real split would have to "
          f"outvote it (got {turn.tokens['write5m']} / {turn.tokens['write1h']})")
    check(turn.billed_writes == (800, 0),
          f"the assumption is applied at PRICING time: the whole write falls "
          f"back to the cheaper 5-minute rate, so the dollar figure stays a "
          f"FLOOR (got {turn.billed_writes})")
    summary = costs_mod.summarize(fold)
    check(summary["cacheWritesWithoutTtlSplit"] == 1,
          "and the report says so out loud")
    check(summary["tokens"]["write5m"] == 800
          and summary["tokens"]["write1h"] == 0,
          f"while the reported pair is what was billed (got {summary['tokens']})")


def _write_record(message_id, total, creation=None):
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": total}
    if creation is not None:
        usage["cache_creation"] = creation
    return {"type": "assistant",
            "message": {"id": message_id, "model": "claude-opus-5",
                        "usage": usage}}


def test_a_ttl_split_that_does_not_reconcile_is_not_a_measurement():
    print("test_a_ttl_split_that_does_not_reconcile_is_not_a_measurement")
    # The three cache-write fields are max-folded INDEPENDENTLY, so a split
    # that disagrees with the write it describes is reachable in both
    # directions. Neither is observed on today's corpora — which is exactly
    # why it would be wrong the first time it mattered and nobody would notice.
    #
    # (a) OVER-count: a streamed turn whose records carry DIFFERENT splits for
    # the same 500-token write puts the maximum of each in both buckets. A
    # reader that trusts the pair bills 500@5m + 500@1h = $0.008125 for a
    # 500-token write, which is the one direction "every dollar figure is a
    # floor" cannot survive.
    mixed = costs_mod.Fold()
    costs_mod._fold_record(
        _write_record("m", 500, {"ephemeral_5m_input_tokens": 500,
                                 "ephemeral_1h_input_tokens": 0}), mixed)
    costs_mod._fold_record(
        _write_record("m", 500, {"ephemeral_5m_input_tokens": 0,
                                 "ephemeral_1h_input_tokens": 500}), mixed)
    turn = mixed.turns["m"]
    check(turn.tokens["write5m"] == 500 and turn.tokens["write1h"] == 500,
          f"the raw fold really does double the write "
          f"(got {turn.tokens['write5m']} / {turn.tokens['write1h']})")
    check(turn.split_measured is False,
          "a pair that does not sum to the write it describes is not a measurement")
    check(turn.billed_writes == (500, 0),
          f"so the whole write bills at the cheaper 5-minute rate "
          f"(got {turn.billed_writes})")
    summary = costs_mod.summarize(mixed)
    close(summary["dollars"]["cacheWrite"], 500 / 1e6 * 5 * 1.25,
          "the floor holds ($0.003125, not the $0.008125 a trusting reader bills)")
    check(summary["cacheWritesWithoutTtlSplit"] == 1,
          "and the turn is REPORTED as assumed, not silently repaired")
    check(summary["tokens"]["write5m"] == 500
          and summary["tokens"]["write1h"] == 0,
          f"the reported pair is what was billed, so it still sums to "
          f"cache_write (got {summary['tokens']})")

    # (b) SILENT ZERO: a `cache_creation` dict carrying only a zeroed key marks
    # the split "seen" while summing BELOW the write — pricing a real write at
    # $0 and reporting no degradation at all.
    empty = costs_mod.Fold()
    costs_mod._fold_record(
        _write_record("m", 500, {"ephemeral_5m_input_tokens": 0}), empty)
    summary = costs_mod.summarize(empty)
    close(summary["dollars"]["cacheWrite"], 500 / 1e6 * 5 * 1.25,
          "a zeroed split does not price a real 500-token write at zero")
    check(summary["cacheWritesWithoutTtlSplit"] == 1,
          "and it is counted as assumed, which is what the JSON is for")

    # A split that DOES reconcile is still a measurement and still priced as one.
    honest = costs_mod.Fold()
    costs_mod._fold_record(
        _write_record("m", 800, {"ephemeral_5m_input_tokens": 300,
                                 "ephemeral_1h_input_tokens": 500}), honest)
    check(honest.turns["m"].split_measured is True,
          "300 + 500 == 800, so this one is measured")
    check(honest.turns["m"].billed_writes == (300, 500),
          "and it prices at the two rates it names")
    check(costs_mod.summarize(honest)["cacheWritesWithoutTtlSplit"] == 0,
          "with nothing to warn about")


def test_a_refused_turns_reads_still_land_in_the_census():
    print("test_a_refused_turns_reads_still_land_in_the_census")
    # The re-read census is a fact about tool calls. Whether `usage` parsed has
    # nothing to do with which files the turn read, and gating one on the other
    # drops exactly the turns the census exists to explain.
    fold = costs_mod.Fold()
    costs_mod._fold_record(
        {"type": "assistant",
         "message": {"id": "m", "model": "claude-opus-5",
                     "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                  "input": {"file_path": "/x/refused.py"}}],
                     "usage": {"input_tokens": 1.5, "output_tokens": 2}}}, fold)
    check(fold.unusable == 1, "the turn's usage is still refused, never coerced")
    check(fold.reads == {"/x/refused.py": 1},
          f"and its Read is still counted (got {fold.reads})")


# --- resolution ------------------------------------------------------------


def test_the_driver_transcript_resolves_from_the_wf_dir():
    print("test_the_driver_transcript_resolves_from_the_wf_dir")
    check(costs_mod.session_dir_for(str(WF_DIR)) == str(PROJECT / SESSION),
          "parent-of-parent-of-parent of a wf_dir is the session directory")
    check(costs_mod.driver_transcript_for(str(WF_DIR)) == str(DRIVER),
          "the transcript is that directory's SIBLING, named for it")
    for wrong in ("/tmp/a/b/c", str(PROJECT / SESSION), ""):
        check(costs_mod.session_dir_for(wrong) is None,
              f"a path of another shape resolves to nothing, not to a directory "
              f"that happens to exist ({wrong!r})")
    # The shape check that the `subagents/workflows` pair alone does NOT make:
    # every case above fails on those two components, so none of them exercises
    # whether the resolved directory is session-shaped. Without that third
    # check `/srv/backup/subagents/workflows/wf_x` yields `/srv/backup`, and a
    # `/srv/backup.jsonl` that happened to exist would be folded as "the
    # driver's own transcript".
    for impostor in ("/srv/backup/subagents/workflows/wf_x",
                     "/tmp/not-a-uuid/subagents/workflows/wf_c0570001-a1b",
                     str(PROJECT / "nope" / "subagents" / "workflows" / RUN_ID)):
        check(costs_mod.session_dir_for(impostor) is None,
              f"the two intermediate components are right but the session is "
              f"not uuid-shaped, so it resolves to nothing ({impostor!r})")
        check(costs_mod.driver_transcript_for(impostor) is None,
              f"and no sibling .jsonl is adopted as a driver transcript "
              f"({impostor!r})")
    # ...while the real shape still resolves, so the guard is not merely strict.
    check(costs_mod.session_dir_for(str(WF_DIR2)) == str(PROJECT / SESSION2),
          "a genuine session directory is still resolved")
    check(costs_mod.driver_transcript_for(None) is None,
          "no wf_dir means no driver transcript")


def test_the_run_is_found_through_the_task_folders_orch_config():
    print("test_the_run_is_found_through_the_task_folders_orch_config")
    with tempfile.TemporaryDirectory(prefix="cost-task-") as tmp:
        task = Path(tmp) / "sp-cost-fixture"
        task.mkdir()
        (task / "orch-config.json").write_text(
            json.dumps({"wf_dir": str(WF_DIR), "port": 8931}) + "\n",
            encoding="utf-8")
        check(costs_mod.read_config(task)["wf_dir"] == str(WF_DIR),
              "the config's wf_dir is the join key from a task folder to a run")
        report = costs_mod.analyze(task_dir=str(task))
        check(report["corpus"] == "present",
              "naming the TASK folder is enough — the wf_dir comes off its config")
        check(report["agentsSummary"]["contextIntegral"] == EXPECTED["contextIntegral"],
              "and it reads the same run, to the same totals")
        check(report["taskDir"] == str(task), "the report names the task it read")
    # A folder with no config at all is the "plan only, never run" kind.
    with tempfile.TemporaryDirectory(prefix="cost-empty-") as tmp:
        check(costs_mod.read_config(tmp) == {},
              "a task folder with no orch-config.json reads as empty, not as an error")


# --- no corpus is a clean skip --------------------------------------------


def test_no_corpus_is_a_clean_skip():
    print("test_no_corpus_is_a_clean_skip")
    with tempfile.TemporaryDirectory(prefix="cost-nocorpus-") as tmp:
        empty = Path(tmp) / "subagents" / "workflows" / "wf_nothing-000"
        empty.mkdir(parents=True)
        report = costs_mod.analyze(wf_dir=str(empty))
        check(report["corpus"] == "absent",
              "an empty run directory reports absent, not zero")
        check("agentsSummary" not in report,
              "and invents no summary to go with it")
        check("no agent transcript" in report.get("note", ""),
              f"the note says what was missing (got {report.get('note')!r})")
        rendered = costs_mod.render(report)
        check("no corpus" in rendered,
              f"the human rendering says 'no corpus' (got {rendered!r})")
        code = costs_mod.main(["--wf-dir", str(empty)])
        check(code == 0, f"and the CLI exits 0 — a skip, not a failure (rc={code})")


def test_an_archived_session_reports_the_driver_unavailable_not_zero():
    print("test_an_archived_session_reports_the_driver_unavailable_not_zero")
    with tempfile.TemporaryDirectory(prefix="cost-archived-") as tmp:
        run = (Path(tmp) / "projects" / "-p" / SESSION / "subagents"
               / "workflows" / "wf_c0570001-a1b")
        run.mkdir(parents=True)
        agent = WF_DIR / "agent-a00000000000000a2.jsonl"
        (run / agent.name).write_bytes(agent.read_bytes())
        report = costs_mod.analyze(wf_dir=str(run))
        check(report["corpus"] == "present", "the agents are still priced")
        check(report["driver"] is None, "the driver row is None, never a zero row")
        check(report["driverShare"] is None,
              "and the share is withheld rather than computed against a fiction")
        check("archived" in report.get("driverNote", ""),
              f"the note says why (got {report.get('driverNote')!r})")
    # A resumed run can be PARTLY archived: one session's transcript swept,
    # another's still there. Folding what survives and calling it "the driver
    # cost" would be a floor presented as a total, so the shortfall is counted.
    with tempfile.TemporaryDirectory(prefix="cost-partial-") as tmp:
        project = Path(tmp) / "projects" / "-p"
        for session, agent in ((SESSION, WF_DIR / "agent-a00000000000000a2.jsonl"),
                               (SESSION2,
                                WF_DIR2 / "agent-a00000000000000a3.jsonl")):
            run = project / session / "subagents" / "workflows" / RUN_ID
            run.mkdir(parents=True)
            (run / agent.name).write_bytes(agent.read_bytes())
        # Only the FIRST session keeps its transcript.
        (project / f"{SESSION}.jsonl").write_bytes(DRIVER.read_bytes())
        report = costs_mod.analyze(
            wf_dir=str(project / SESSION / "subagents" / "workflows" / RUN_ID))
        check(len(report["runDirs"]) == 2 and report["driver"]["sessions"] == 1,
              f"both run directories are read; one driver transcript survives "
              f"(got {len(report['runDirs'])} dirs, "
              f"{report['driver']['sessions']} transcripts)")
        check(report["driverSessionsMissing"] == 1,
              f"and the shortfall is COUNTED (got {report['driverSessionsMissing']})")
        check("floor" in report.get("driverNote", ""),
              f"the note says the driver row is a floor, not a total "
              f"(got {report.get('driverNote')!r})")
        rendered = costs_mod.render(report)
        check("floor" in rendered,
              "and the human rendering carries the note too — the JSON is read "
              "by nobody")
        # The excluded turns, with NO co-tenant to blame them on. This fixture
        # is the case the old unconditional clause got wrong: it told the
        # reader the dropped turns "belong to other runs" while the field two
        # lines up said there are none. They are this run's own driver work
        # outside the agent span — pre-launch drafting and the close-out tail
        # — which is why the row is a floor and not a total.
        check(report["driverCoTenantRuns"] == [],
              f"no other run shares these transcripts "
              f"(got {report['driverCoTenantRuns']})")
        check(report["driver"]["turnsOutsideWindow"] >= 1,
              f"and yet the window still excluded a turn "
              f"(got {report['driver']['turnsOutsideWindow']})")
        check("belong to other runs" not in rendered,
              f"so nothing anywhere says they belong to other runs — there are "
              f"no other runs to belong to (got {rendered!r})")
        for expected in ("no other run shares these transcripts",
                         "OWN work outside the agent span",
                         "close-out tail"):
            check(expected in rendered,
                  f"the rendering says what they actually are: {expected!r}")
        check(expected in report.get("driverNote", ""),
              "and so does the JSON, because a close-out epilogue reads that "
              "and not the transcript")


# --- the file plane it must never touch ------------------------------------


#: Every variable that could steer a resolver away from the temp HOME below.
#: Cleared, not just overridden: the point of the arm is to make the module
#: resolve a home for real, and an inherited `$CLAUDE_PROJECT_DIR` would send
#: it back to this checkout where the assertion cannot bite.
_HOME_ANCHORS = ("HOME", "CLAUDE_PROJECT_DIR", "TOUCH_PROJECT_CWD",
                 "ORCH_TASKS_ROOT", "TOUCH_LEGACY_ROOT", "TOUCH_CLAUDE_ROOT",
                 "ORCH_STATE_DIR")


def test_a_whole_analysis_writes_nothing_anywhere():
    print("test_a_whole_analysis_writes_nothing_anywhere")

    def snapshot(root):
        return {str(p): p.stat().st_mtime_ns for p in Path(root).rglob("*")}

    before = snapshot(CORPUS)
    with tempfile.TemporaryDirectory(prefix="cost-home-") as home:
        (Path(home) / ".claude").mkdir()      # the marker every resolver walks to
        home_before = snapshot(home)
        saved = {name: os.environ.get(name) for name in _HOME_ANCHORS}
        saved_cwd = os.getcwd()
        os.environ["HOME"] = home
        for name in _HOME_ANCHORS[1:]:
            os.environ.pop(name, None)
        buffer = io.StringIO()
        try:
            # Explicit paths first — the ordinary call shape.
            costs_mod.analyze(wf_dir=str(WF_DIR))
            costs_mod.baseline(REPO, ceiling=10 ** 9)
            # Then the shapes that RESOLVE a home: with nothing named, both CLI
            # modes walk up from the cwd to a `.claude/` marker. Without this
            # arm the assertion below is a tautology — neither call above ever
            # touches $HOME, so it could not fail whatever the module did.
            os.chdir(home)
            with contextlib.redirect_stdout(buffer):
                unnamed = costs_mod.main([])
                unnamed_baseline = costs_mod.main(["--baseline"])
        finally:
            os.chdir(saved_cwd)
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        check(unnamed == 0 and unnamed_baseline == 0,
              f"a run with no history resolves a home and exits 0 "
              f"(rc {unnamed} / {unnamed_baseline})")
        check("no corpus" in buffer.getvalue(),
              "and says so rather than inventing a run to price")
        leaked = sorted(set(snapshot(home)) - set(home_before))
        check(not leaked,
              f"nothing was written ANYWHERE under a fresh HOME — not just "
              f"under ~/.claude ({leaked})")
    check(snapshot(CORPUS) == before,
          "and the frozen corpus is untouched (not even an mtime)")


# --- D-22's half: the baseline reader --------------------------------------


def _fixture_repo(tmp, *, claude=b"x" * 4000, memory=b"y" * 400,
                  skills=("alpha", "beta"), budget=None):
    root = Path(tmp)
    (root / "CLAUDE.md").write_bytes(claude)
    (root / ".touch" / "memory").mkdir(parents=True)
    (root / ".touch" / "memory" / "MEMORY.md").write_bytes(memory)
    for name in skills:
        directory = root / "plugin" / "touch" / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {'d' * 40}\n---\n\nbody\n",
            encoding="utf-8")
    if budget is not None:
        (root / "tests").mkdir(exist_ok=True)
        (root / "tests" / "test_context_budget.py").write_text(budget,
                                                               encoding="utf-8")
    return root


def test_the_baseline_measures_the_prefix_this_repo_owns():
    print("test_the_baseline_measures_the_prefix_this_repo_owns")
    with tempfile.TemporaryDirectory(prefix="cost-baseline-") as tmp:
        root = _fixture_repo(tmp)
        measured = costs_mod.baseline(root, ceiling=10 ** 9)
        by_name = {entry["name"]: entry for entry in measured["entries"]}
        check(by_name["CLAUDE.md"]["tokens"] == 1000,
              f"CLAUDE.md is estimated chars/4 (got {by_name['CLAUDE.md']['tokens']})")
        check(by_name["MEMORY.md"]["tokens"] == 100, "the memory index too")
        skill_entry = by_name["skill descriptions (2)"]
        check(skill_entry["tokens"] == 20,
              f"only the description frontmatter counts, not the body "
              f"(got {skill_entry['tokens']})")
        check(measured["totalTokens"] == 1120,
              f"the total is their sum (got {measured['totalTokens']})")
        check(measured["over"] is False, "and it is under a generous ceiling")
        check(costs_mod.estimate_tokens(b"abcd" * 7) == 7,
              "the estimator is chars/4 over bytes, pinned")


def test_the_baseline_gates_and_says_which_number_it_used():
    print("test_the_baseline_gates_and_says_which_number_it_used")
    with tempfile.TemporaryDirectory(prefix="cost-gate-") as tmp:
        root = _fixture_repo(tmp)
        under = costs_mod.baseline(root, ceiling=2000)
        check(under["over"] is False and costs_mod.main(
            ["--baseline", "--repo", str(root), "--ceiling", "2000"]) == 0,
            "under the ceiling the CLI exits 0")
        over = costs_mod.baseline(root, ceiling=100)
        check(over["over"] is True and costs_mod.main(
            ["--baseline", "--repo", str(root), "--ceiling", "100"]) == 1,
            "over the ceiling it exits 1 — the gate bites")
        check("the caller's recorded ceiling" in over["ceilingSource"],
              f"and it names where the number came from "
              f"(got {over['ceilingSource']!r})")
        check(costs_mod.baseline(root)["ceiling"] is None,
              "no ceiling given: the baseline is PRINTED, never invented")
        check(costs_mod.baseline(root)["over"] is False,
              "and an absent ceiling cannot be exceeded")


def test_the_budget_test_outranks_the_callers_ceiling_once_it_exists():
    print("test_the_budget_test_outranks_the_callers_ceiling_once_it_exists")
    declared = ("CLAUDE_MD_BUDGET_TOKENS = 600\n"
                "MEMORY_BUDGET_TOKENS = 80\n"
                "SKILLS_BUDGET_TOKENS = 40\n"
                "UNRELATED = 999999\n")
    with tempfile.TemporaryDirectory(prefix="cost-budget-") as tmp:
        root = _fixture_repo(tmp, budget=declared)
        budgets = costs_mod.declared_budgets(root)
        check(budgets == {"CLAUDE_MD_BUDGET_TOKENS": 600,
                          "MEMORY_BUDGET_TOKENS": 80,
                          "SKILLS_BUDGET_TOKENS": 40},
              f"only the named BUDGET_KEYS constants are read (got {budgets})")
        measured = costs_mod.baseline(root, ceiling=10 ** 9)
        check(measured["ceiling"] == 720,
              f"their SUM is the ceiling, not the caller's (got {measured['ceiling']})")
        check(measured["over"] is True,
              "so a tree over the declared budget is red even under a generous knob")
        check(measured["ceilingSource"].endswith("test_context_budget.py"),
              f"and the source is named (got {measured['ceilingSource']!r})")
    # The trap a suffix match walks into: sp-10 declares a TOTAL beside its
    # parts, the reader sums all four, and the ceiling silently doubles — the
    # gate stops biting while `ceilingSource` still names the budget file.
    with tempfile.TemporaryDirectory(prefix="cost-budget-total-") as tmp:
        root = _fixture_repo(tmp, budget=declared + "TOTAL_BUDGET_TOKENS = 720\n")
        budgets = costs_mod.declared_budgets(root)
        check("TOTAL_BUDGET_TOKENS" not in (budgets or {}),
              f"a total-plus-parts file does not add its total to its parts "
              f"(got {sorted(budgets or {})})")
        check(costs_mod.baseline(root, ceiling=10 ** 9)["ceiling"] == 720,
              "so the ceiling stays the sum of the three named parts, not 1440")
        check(set(costs_mod.BUDGET_KEYS) == {"CLAUDE_MD_BUDGET_TOKENS",
                                             "MEMORY_BUDGET_TOKENS",
                                             "SKILLS_BUDGET_TOKENS"},
              f"and the contract sp-10 is held to is stated by name, in one "
              f"place (got {costs_mod.BUDGET_KEYS})")
    # The mirror trap: a PARTIAL declaration. sp-10 can reach one by declaring
    # its constants incrementally or by landing two of three, and summing what
    # happens to be there gates the release at 680 while `ceilingSource`
    # confidently names the file — the same failure as the total-plus-parts
    # case above, in the other direction.
    with tempfile.TemporaryDirectory(prefix="cost-budget-partial-") as tmp:
        root = _fixture_repo(tmp, budget=("CLAUDE_MD_BUDGET_TOKENS = 600\n"
                                          "MEMORY_BUDGET_TOKENS = 80\n"))
        measured = costs_mod.baseline(root, ceiling=10 ** 9)
        check(measured["ceiling"] == 10 ** 9,
              f"a file missing one of the three parts is not a ceiling — the "
              f"caller's number stands (got {measured['ceiling']})")
        check(measured["over"] is False,
              "so the gate does not go red against a number nobody declared")
        check("incomplete" in measured["ceilingSource"] and
              "SKILLS_BUDGET_TOKENS" in measured["ceilingSource"],
              f"and it says which name is missing rather than naming the file "
              f"as the source (got {measured['ceilingSource']!r})")
        check(measured["declaredBudgets"] ==
              {"CLAUDE_MD_BUDGET_TOKENS": 600, "MEMORY_BUDGET_TOKENS": 80},
              f"what WAS declared is still reported, so the partial state is "
              f"visible (got {measured['declaredBudgets']})")
    # An ANNOTATED declaration is a complete one. `X: int = 6000` is an
    # `ast.AnnAssign` and matches no `ast.Assign` filter, so a reader that saw
    # only the second form would call a finished budget file partial purely
    # because sp-10 typed its constants.
    with tempfile.TemporaryDirectory(prefix="cost-budget-annotated-") as tmp:
        root = _fixture_repo(tmp, budget=("CLAUDE_MD_BUDGET_TOKENS: int = 600\n"
                                          "MEMORY_BUDGET_TOKENS: int = 80\n"
                                          "SKILLS_BUDGET_TOKENS: int = 40\n"
                                          "LATER: int\n"))
        check(costs_mod.declared_budgets(root) ==
              {"CLAUDE_MD_BUDGET_TOKENS": 600, "MEMORY_BUDGET_TOKENS": 80,
               "SKILLS_BUDGET_TOKENS": 40},
              f"an annotated assignment is read like a bare one, and a bare "
              f"annotation with no value is skipped rather than crashing "
              f"(got {costs_mod.declared_budgets(root)})")
        check(costs_mod.baseline(root, ceiling=10 ** 9)["ceiling"] == 720,
              "so the declared sum is the ceiling, exactly as for `X = 600`")
    with tempfile.TemporaryDirectory(prefix="cost-nobudget-") as tmp:
        root = _fixture_repo(tmp)
        check(costs_mod.declared_budgets(root) is None,
              "absent today — the caller's ceiling stands until sp-10 lands it")
        check(costs_mod.baseline(root, ceiling=2000)["ceilingSource"] ==
              "the caller's recorded ceiling",
              "and an ABSENT file is not reported as an incomplete one")


def test_a_folded_yaml_description_is_measured_not_read_as_zero():
    print("test_a_folded_yaml_description_is_measured_not_read_as_zero")
    # All ten of Touch's skills are single-line today, so this is latent rather
    # than live — which is exactly why it needs a test: a folded description
    # would otherwise contribute ~0 bytes and UNDER-report the always-on prefix
    # D-22's gate exists to cap, in the one direction that never goes red.
    single = costs_mod.description_text(
        "---\nname: a\ndescription: hello world\n---\n\nbody\n")
    check(single == "hello world",
          f"the single-line form is read as before (got {single!r})")
    folded = costs_mod.description_text(
        "---\nname: a\ndescription: >-\n  hello\n  world\n---\n\nbody\n")
    check(folded == "hello world",
          f"a folded `>-` description is joined and measured, not dropped "
          f"(got {folded!r})")
    literal = costs_mod.description_text(
        "---\nname: a\ndescription: |\n  hello\n  world\n---\n\nbody\n")
    check(literal == "hello world",
          f"and the literal `|` form too (got {literal!r})")
    check(costs_mod.description_text("---\nname: a\n---\n") is None,
          "a SKILL.md with no description contributes nothing")
    # End to end, through the byte count the budget actually uses.
    with tempfile.TemporaryDirectory(prefix="cost-folded-") as tmp:
        root = _fixture_repo(tmp, skills=())
        directory = root / "plugin" / "touch" / "skills" / "folded"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            "---\nname: folded\ndescription: >-\n  " + "d" * 40 + "\n---\n\nbody\n",
            encoding="utf-8")
        described, count = costs_mod.skill_descriptions(root)
        check((described, count) == (40, 1),
              f"skill_descriptions counts a folded description's real bytes "
              f"(got {described} bytes over {count} skill(s))")


def test_the_baseline_survives_a_torn_multibyte_read():
    print("test_the_baseline_survives_a_torn_multibyte_read")
    # `.touch/memory/MEMORY.md` is written by another process, and has been
    # observed on this machine truncated mid-character. A budget reader that
    # raises UnicodeDecodeError there goes red for a reason that has nothing to
    # do with the budget.
    with tempfile.TemporaryDirectory(prefix="cost-torn-") as tmp:
        root = _fixture_repo(tmp, memory="ok —".encode()[:-1])
        measured = costs_mod.baseline(root, ceiling=10 ** 9)
        check(measured["totalTokens"] > 0,
              "a torn multi-byte tail is measured, not raised on")
        by_name = {entry["name"]: entry for entry in measured["entries"]}
        check(by_name["MEMORY.md"]["present"] is True,
              "the file is present; only its last character is half-written")
    with tempfile.TemporaryDirectory(prefix="cost-absent-") as tmp:
        root = Path(tmp)
        (root / "CLAUDE.md").write_bytes(b"z" * 40)
        measured = costs_mod.baseline(root, ceiling=10 ** 9)
        by_name = {entry["name"]: entry for entry in measured["entries"]}
        check(by_name["MEMORY.md"]["present"] is False
              and by_name["MEMORY.md"]["tokens"] == 0,
              "a missing source contributes zero and is marked absent, not skipped")


# --- the CLI ---------------------------------------------------------------


def _payload_bytecode():
    """Every file under the payload's `aggregator/__pycache__`, or []."""
    cache = _roots.SRC / "aggregator" / "__pycache__"
    return sorted(str(p) for p in cache.rglob("*")) if cache.is_dir() else []


def test_the_cli_prints_json_and_refuses_what_it_cannot_parse():
    print("test_the_cli_prints_json_and_refuses_what_it_cannot_parse")
    bytecode_before = _payload_bytecode()
    proc = subprocess.run(
        [sys.executable, "-P", "-m", "aggregator.costs",
         "--wf-dir", str(WF_DIR), "--json"],
        # PYTHONDONTWRITEBYTECODE, because `sys.dont_write_bytecode` above does
        # NOT survive fork/exec: without it this child compiles the payload
        # modules and drops an `aggregator/__pycache__` into `plugin/touch/`,
        # which `tests/test_package.py` correctly calls a never-ship path. The
        # full suite exports it for every file; running this one directly (a
        # documented workflow) has to set it itself.
        cwd=str(REPO), env={**os.environ, "PYTHONPATH": str(_roots.SRC),
                            "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
    check(proc.returncode == 0, f"the module-direct form runs (rc={proc.returncode}, "
                                f"stderr={proc.stderr.strip()[:200]})")
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        payload = None
    check(isinstance(payload, dict), "--json prints a JSON object and nothing else")
    if isinstance(payload, dict):
        check(payload["agentsSummary"]["contextIntegral"] == EXPECTED["contextIntegral"],
              "and it carries the same totals as the in-process call")
    check(costs_mod.main(["--nope"]) == 2, "an unknown flag is a usage error (rc 2)")
    check(costs_mod.main(["--task"]) == 2, "a flag with no value is a usage error")
    check(costs_mod.main(["--help"]) == 0, "--help exits 0")
    # `--wf-dir` wins over `--task` inside analyze, so accepting both would
    # print a report naming a task folder it did not use to find the run.
    check(costs_mod.main(["--task", "/x", "--wf-dir", str(WF_DIR)]) == 2,
          "naming the run two ways at once is refused, never resolved by "
          "precedence the usage line does not state")
    check(costs_mod._parse(["--single-session"])["expand"] is False,
          "--single-session turns the run-union off")
    check(costs_mod._parse([])["expand"] is True,
          "and the union is the default, because the default is what "
          "release.sh runs")
    check(_payload_bytecode() == bytecode_before,
          "and the child compiled nothing INTO the payload tree — "
          "`sys.dont_write_bytecode` does not survive fork/exec, so a module-"
          "direct child that omits PYTHONDONTWRITEBYTECODE leaves a "
          "never-ship `__pycache__` behind and reddens tests/test_package.py")


# --- the driver slice ------------------------------------------------------


def test_a_session_shared_by_two_runs_does_not_bill_both_for_the_same_turns():
    print("test_a_session_shared_by_two_runs_does_not_bill_both_for_the_same_turns")
    # The case the one-run-per-session corpus could not express. SESSION holds
    # RUN_ID and RUN_ID2 and ONE driver transcript; an unbounded fold hands
    # every turn in that file to both runs, so summing the tool's own driver
    # dollars across a session's runs over-counts.
    first = analyze_corpus()
    second = costs_mod.analyze(wf_dir=str(WF_DIR_COTENANT))
    check(first["driverCoTenantRuns"] == [RUN_ID2],
          f"the run names the other run sharing its session transcript "
          f"(got {first['driverCoTenantRuns']})")
    check(second["driverCoTenantRuns"] == [RUN_ID],
          f"and the co-tenant names it back (got {second['driverCoTenantRuns']})")
    check(first["driverScope"] == "run window" == second["driverScope"],
          f"both readings say which figure they are "
          f"(got {first['driverScope']!r} / {second['driverScope']!r})")
    # The headline property: the co-tenant's EXISTENCE changes nothing about
    # the original run's driver row. Before the slice, RUN_ID's driver row
    # silently absorbed RUN_ID2's turn.
    check(first["driver"]["contextIntegral"] == EXPECTED_DRIVER["contextIntegral"],
          f"the original run's driver total is untouched by the co-tenant "
          f"({first['driver']['contextIntegral']} vs "
          f"{EXPECTED_DRIVER['contextIntegral']})")
    check(first["driver"]["turnsOutsideWindow"] == 1,
          f"because exactly one turn in that transcript was placed OUTSIDE "
          f"this run's window (got {first['driver']['turnsOutsideWindow']})")
    # HERE a co-tenant does exist, so "some belong to the other run(s)" is a
    # measured statement rather than the unconditional one it replaced.
    rendered = costs_mod.render(first)
    check("some belong to the other run(s) named above" in rendered
          and RUN_ID2 in rendered,
          f"and the rendering attributes them to the run it NAMED, having "
          f"measured that it exists (got {rendered!r})")
    close(first["driverShare"], EXPECTED_DRIVER["share"],
          "and the share is still the run's, not the session's")
    # The co-tenant gets its own slice, not the same one.
    check(second["agentsSummary"]["agents"] == EXPECTED_COTENANT["agents"] and
          second["agentsSummary"]["contextIntegral"] ==
          EXPECTED_COTENANT["contextIntegral"],
          f"the co-tenant reads its own agents "
          f"(got {second['agentsSummary']['agents']} agents, "
          f"{second['agentsSummary']['contextIntegral']} tok)")
    check(second["driver"]["contextIntegral"] ==
          EXPECTED_COTENANT["driverContextIntegral"],
          f"and its own driver turn only "
          f"({second['driver']['contextIntegral']} vs "
          f"{EXPECTED_COTENANT['driverContextIntegral']})")
    close(second["driver"]["dollars"]["total"], EXPECTED_COTENANT["driverTotal"],
          "priced at the same rates")
    # The two slices are DISJOINT: summing them equals the whole session, so no
    # turn is billed twice and none is dropped.
    check(first["driver"]["contextIntegral"] + second["driver"]["contextIntegral"]
          == EXPECTED_WHOLE_SESSION_DRIVER["contextIntegral"],
          f"the two runs' driver slices sum to the whole session and no more — "
          f"the over-count is gone rather than moved "
          f"({first['driver']['contextIntegral']} + "
          f"{second['driver']['contextIntegral']} vs "
          f"{EXPECTED_WHOLE_SESSION_DRIVER['contextIntegral']})")


def test_the_window_is_reported_and_reaches_back_to_the_launch_record():
    print("test_the_window_is_reported_and_reaches_back_to_the_launch_record")
    report = analyze_corpus()
    window = report["driverWindow"]
    check(isinstance(window, list) and len(window) == 2,
          f"the slice is auditable — the window is IN the report, not implied "
          f"(got {window!r})")
    # JSON-serializable, because `--json` is the arm a close-out epilogue reads.
    json.dumps(report)
    check(report.get("driverLaunchSeen") is True,
          "the launch record joining this session to this run was found")
    # The launching turn predates every agent record it created, so a window
    # taken from agent records alone would exclude the turn that started the run.
    check(window[0] < "2026-07-31T00:00:00",
          f"and the window reaches back to it, before the first agent record "
          f"(got {window[0]!r})")
    check(costs_mod.launch_moment_for(str(DRIVER), RUN_ID) is not None,
          "launch_moment_for finds the run it is asked about")
    check(costs_mod.launch_moment_for(str(DRIVER), "wf_000000000000") is None,
          "and returns None for a run this transcript never launched, rather "
          "than the first launch it happens to see")


def test_declining_the_slice_never_renders_as_a_share_of_the_run():
    print("test_declining_the_slice_never_renders_as_a_share_of_the_run")
    whole = costs_mod.analyze(wf_dir=str(WF_DIR), whole_session=True)
    check(whole["driver"]["contextIntegral"] ==
          EXPECTED_WHOLE_SESSION_DRIVER["contextIntegral"],
          f"--driver-whole-session folds every session transcript whole "
          f"({whole['driver']['contextIntegral']} vs "
          f"{EXPECTED_WHOLE_SESSION_DRIVER['contextIntegral']})")
    check(whole["driverScope"] == "whole session",
          f"and says so (got {whole['driverScope']!r})")
    check(whole["driverWindow"] is None,
          "there is no window to report when none was applied")
    check("driverLaunchSeen" in whole and whole["driverLaunchSeen"] is None,
          f"and the launch flag is present-or-null like every other driver "
          f"field — None because none was looked for, which an absent KEY "
          f"cannot say (got {whole.get('driverLaunchSeen', '<absent>')!r})")
    check(whole["driver"]["owners"] == 2 and "agents" not in whole["driver"],
          f"the driver's owner count is named `owners`: they are SESSIONS, and "
          f"calling them agents beside a `sessions` field that counts "
          f"transcripts read is two numbers one word (got "
          f"{sorted(whole['driver'])})")
    rendered = costs_mod.render(whole)
    check("% of the run" not in rendered,
          f"the rendering never calls a whole-session numerator a share of the "
          f"run — that clause is the one a reader quotes (got {rendered!r})")
    check("WHOLE-SESSION figure" in rendered,
          "it says what the number actually is")
    check(RUN_ID2 in rendered,
          f"and names the other run(s) the transcript covers, so the reader "
          f"can see why summing would double-count (got {rendered!r})")
    check(costs_mod._parse(["--driver-whole-session"])["whole_session"] is True,
          "the flag is wired")
    check(costs_mod._parse([])["whole_session"] is False,
          "and the SLICE is the default — an escape hatch is not a default")


def test_an_undated_driver_turn_is_excluded_and_counted_never_silently_kept():
    print("test_an_undated_driver_turn_is_excluded_and_counted_never_silently_kept")
    # A record a window cannot PLACE is refused, not assumed into the run: the
    # module's floor direction. Silence here would be a turn billed to whichever
    # run happened to ask.
    import datetime
    start = datetime.datetime(2026, 7, 31, tzinfo=datetime.timezone.utc)
    end = start + datetime.timedelta(hours=1)
    fold = costs_mod.Fold()
    usage = {"input_tokens": 10, "output_tokens": 5,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    costs_mod._fold_record(
        {"type": "assistant", "timestamp": "2026-07-31T00:30:00.000Z",
         "message": {"id": "in", "model": "claude-opus-5", "usage": usage}},
        fold, window=(start, end))
    costs_mod._fold_record(
        {"type": "assistant", "timestamp": "2026-07-31T09:00:00.000Z",
         "message": {"id": "out", "model": "claude-opus-5", "usage": usage}},
        fold, window=(start, end))
    costs_mod._fold_record(
        {"type": "assistant",
         "message": {"id": "undated", "model": "claude-opus-5", "usage": usage}},
        fold, window=(start, end))
    check(set(fold.turns) == {"in"},
          f"only the turn inside the window is folded (got {sorted(fold.turns)})")
    check(fold.outside_window == 1 and fold.undated == 1,
          f"and the two exclusions are counted APART — 'another run's turn' and "
          f"'a turn I could not place' are different facts "
          f"(outside={fold.outside_window}, undated={fold.undated})")
    # Both are TURN counts, folded on the message id exactly as the tokens are.
    # A per-record count is inflated by however slowly a turn streamed, and it
    # is rendered beside a deduplicated turn count — so the ratio a reader
    # takes from the two numbers would be wrong by that factor. Measured on
    # this repo's own run before the fix: 44 "turns" excluded, 14 real ones.
    streamed = costs_mod.Fold()
    for _ in range(3):
        costs_mod._fold_record(
            {"type": "assistant", "timestamp": "2026-07-31T09:00:00.000Z",
             "message": {"id": "streamed-out", "model": "claude-opus-5",
                         "usage": usage}},
            streamed, window=(start, end))
    for _ in range(3):
        costs_mod._fold_record(
            {"type": "assistant",
             "message": {"id": "streamed-undated", "model": "claude-opus-5",
                         "usage": usage}},
            streamed, window=(start, end))
    check(streamed.outside_window == 1 and streamed.undated == 1,
          f"one streamed turn written three times is ONE excluded turn, not "
          f"three records (outside={streamed.outside_window}, "
          f"undated={streamed.undated})")
    anonymous = costs_mod.Fold()
    for _ in range(2):
        costs_mod._fold_record(
            {"type": "assistant", "timestamp": "2026-07-31T09:00:00.000Z",
             "message": {"model": "claude-opus-5", "usage": usage}},
            anonymous, window=(start, end))
    check(anonymous.outside_window == 2,
          f"a record with no id to deduplicate on is still counted rather than "
          f"lost — the floor direction (got {anonymous.outside_window})")
    # A turn that streamed ACROSS the window's own edge is folded IN, so
    # reporting it as excluded as well would be the report contradicting its
    # own totals.
    straddle = costs_mod.Fold()
    for stamp in ("2026-07-31T09:00:00.000Z", "2026-07-31T00:30:00.000Z"):
        costs_mod._fold_record(
            {"type": "assistant", "timestamp": stamp,
             "message": {"id": "edge", "model": "claude-opus-5", "usage": usage}},
            straddle, window=(start, end))
    check(set(straddle.turns) == {"edge"} and straddle.outside_window == 0,
          f"a turn with a record on each side of the edge is counted once, as "
          f"folded, never also as excluded (got {straddle.outside_window})")
    # Without a window nothing is excluded, and the fold reports the window the
    # records themselves describe — which is what bounds the driver half.
    open_fold = costs_mod.Fold()
    for stamp in ("2026-07-31T09:00:00.000Z", "2026-07-31T00:30:00.000Z"):
        costs_mod._fold_record(
            {"type": "assistant", "timestamp": stamp,
             "message": {"id": stamp, "model": "claude-opus-5", "usage": usage}},
            open_fold)
    check(open_fold.outside_window == 0 and len(open_fold.turns) == 2,
          "an unwindowed fold excludes nothing")
    got = open_fold.window
    check(got is not None and got[0] < got[1],
          f"and reports (first, last) regardless of the order they arrived in "
          f"(got {got})")


# --- the price table -------------------------------------------------------


def test_every_price_carries_a_provenance_and_the_source_is_named_honestly():
    print("test_every_price_carries_a_provenance_and_the_source_is_named_honestly")
    check(set(costs_mod.PRICE_PROVENANCE) == set(costs_mod.PRICES),
          f"every priced model says where its rate came from, and nothing else "
          f"does (symmetric difference: "
          f"{sorted(set(costs_mod.PRICE_PROVENANCE) ^ set(costs_mod.PRICES))})")
    blank = [name for name, text in costs_mod.PRICE_PROVENANCE.items()
             if not (text or "").strip()]
    check(not blank, f"no provenance is a blank string (got {blank})")
    # The rate that is NOT the sticker is the one a maintainer would 'correct'
    # from memory, so its entry has to say it is deliberate.
    sonnet = costs_mod.PRICE_PROVENANCE["claude-sonnet-5"]
    check(costs_mod.SONNET_5_INTRO_RATE_ENDS in sonnet and "intro" in sonnet.lower(),
          f"the introductory rate names itself and its lapse date "
          f"(got {sonnet!r})")
    check(costs_mod.PRICES["claude-sonnet-5"] == (2.0, 10.0),
          "and bills the introductory pair, because every figure here is a floor")
    doc = costs_mod.__doc__
    # The claim that cost one review cycle: the priced table is delivered in the
    # skill's instruction body, and the file a maintainer would reach for
    # (`shared/models.md`) carries a same-named table with NO price column. A
    # docstring that says only "the Current Models table" sends them there.
    check("shared/models.md" in doc,
          "the docstring warns which same-named table is NOT the price list")
    check("instruction body" in doc,
          "and says the real one is delivered at invocation, not on disk")


# --- the release gate ------------------------------------------------------


def test_release_sh_invokes_the_cost_reader_without_network():
    print("test_release_sh_invokes_the_cost_reader_without_network")
    if not RELEASE_SH.is_file():
        check(False, "scripts/release.sh exists")
        return
    text = RELEASE_SH.read_text(encoding="utf-8")
    check("aggregator.costs" in text,
          "the release checklist invokes the cost reader")
    # One invocation, one string: backslash continuations are folded first, or
    # the baseline arm's own `--baseline` (which sits on its second line) is
    # invisible to every assertion below. Comments are dropped — the block
    # explains itself in prose, and a comment quoting the command form is
    # documentation, not a process that runs.
    joined = re.sub(r"\\\n\s*", " ", text)
    invocations = [line for line in
                   re.findall(r"^.*python3[^\n]*aggregator\.costs.*$", joined, re.M)
                   if not line.lstrip().startswith("#")]
    check(len(invocations) >= 2,
          f"both arms are wired: the baseline gate AND D-21's own print "
          f"(found {len(invocations)})")
    check(all("PYTHONPATH=" in line for line in invocations),
          f"every invocation carries PYTHONPATH — the module-direct form, the "
          f"same one `aggregator.mirror` uses (bad: "
          f"{[l for l in invocations if 'PYTHONPATH=' not in l]})")
    bad = [line for line in invocations if "PYTHONDONTWRITEBYTECODE=1" not in line]
    check(not bad,
          f"and PYTHONDONTWRITEBYTECODE=1 — the flag does not survive "
          f"fork/exec, so without it a release drops a never-ship "
          f"aggregator/__pycache__ into the payload it just certified and the "
          f"NEXT cut is red for it (bad: {bad})")
    check("--baseline" in text and "--ceiling" in text,
          "the baseline gate passes a ceiling rather than trusting a default")
    check("RELEASE_CONTEXT_CEILING" in text,
          "the ceiling is overridable, deliberately and by name")
    # The whole (c)-half block, delimited by its own banner comments — not a
    # window spliced around the first match, which would leave a `curl` three
    # lines below the second invocation unseen.
    block = ""
    if "# The (c) half:" in text:
        block = text.split("# The (c) half:", 1)[1].split("# --- 3.", 1)[0]
    check(block.count("aggregator.costs") >= 2,
          f"both arms live inside the (c)-half block this scan delimits "
          f"(found {block.count('aggregator.costs')})")
    # Network: the gate reads files. A release that fetched a price table would
    # be a release that cannot be cut offline.
    found = re.findall(r"\b(curl|wget|pip install|npm )", block)
    check(not found,
          f"no network command anywhere in the cost gate (found {found})")
    # The report arm resolves the NEWEST run by walking up from its cwd, and
    # this script is invoked from anywhere — so the run it prices has to be
    # anchored to the repo rather than to the operator's shell.
    report_arm = [line for line in invocations if "--baseline" not in line]
    check(report_arm and all('cd "$REPO"' in line or '--repo' in line
                             or '--task' in line or '--wf-dir' in line
                             for line in report_arm),
          f"the report arm is anchored to $REPO, not to the ambient cwd "
          f"(got {report_arm})")
    check(re.search(r"skip \"\$COSTS_REL is not present", text) is not None,
          "an absent reader SKIPs — this script is run inside minimal trees")
    # Not an eighth wrapper: no `bin/touch-cost` exists, and release.sh never
    # names one. (`touch-cost:` is the reader's own output prefix, which the
    # script may legitimately quote.)
    check(not (REPO / "plugin" / "touch" / "bin" / "touch-cost").exists(),
          "no eighth bin/ wrapper was added — the reader is an operator tool "
          "on aggregator.mirror's footing, so the count stays seven")
    # A standing guard, not a measurement of today's file: `touch-cost` does
    # not appear in release.sh at all right now, so this assertion is here to
    # catch the edit that introduces it, and the lookahead exempts the reader's
    # own output prefix (`touch-cost:`) which the script may legitimately quote.
    named = re.findall(r"touch-cost(?!:)", text)
    check(not named,
          f"and release.sh invokes it as a module, never by a wrapper name "
          f"(got {named})")


def main():
    print("cost reader (D-21) tests\n")
    # Above the corpus guard on purpose: this one reads `scripts/release.sh`
    # and nothing else, so a tree without the fixture must not silence it.
    test_release_sh_invokes_the_cost_reader_without_network()
    if not CORPUS.is_dir():
        skip("tests/cost-corpus/ is absent — the frozen mini-corpus is the suite")
        print(f"\nskipped: {skips[0]}")
        if failures:
            print(f"FAILED ({len(failures)}):")
            for failure in failures:
                print(f"  - {failure}")
            sys.exit(1)
        return
    for test in (
        test_the_mini_corpus_is_frozen,
        test_exact_totals_over_the_frozen_corpus,
        test_the_driver_row_and_its_share_of_the_run,
        test_a_resumed_run_is_folded_across_every_session_directory,
        test_a_session_shared_by_two_runs_does_not_bill_both_for_the_same_turns,
        test_the_window_is_reported_and_reaches_back_to_the_launch_record,
        test_declining_the_slice_never_renders_as_a_share_of_the_run,
        test_an_undated_driver_turn_is_excluded_and_counted_never_silently_kept,
        test_every_price_carries_a_provenance_and_the_source_is_named_honestly,
        test_a_foreign_project_with_the_same_run_id_is_not_folded_in,
        test_a_wf_dir_outside_any_projects_tree_is_read_exactly_as_given,
        test_a_ttl_split_that_does_not_reconcile_is_not_a_measurement,
        test_a_refused_turns_reads_still_land_in_the_census,
        test_the_streaming_duplicate_is_max_folded_not_summed,
        test_a_synthetic_turn_is_non_billable_never_unpriced,
        test_an_unknown_model_is_counted_but_never_priced,
        test_a_dated_model_id_resolves_to_the_model_it_names,
        test_the_two_diagnostics_are_counted_per_turn_not_per_record,
        test_a_zero_context_turn_never_drags_an_agents_baseline,
        test_the_context_level_is_the_latest_turn_not_the_largest_or_the_last_one,
        test_an_unmeasured_context_level_is_absent_never_zero,
        test_the_level_refuses_what_the_money_reading_floors,
        test_the_level_reads_one_api_call_out_of_a_multi_iteration_usage,
        test_the_level_keys_are_additive_and_the_money_block_is_untouched,
        test_the_drivers_level_carries_the_scope_that_produced_it,
        test_the_rendering_reports_every_degradation_it_measured,
        test_the_cache_write_ttl_split_is_priced_at_two_different_rates,
        test_a_cache_write_with_no_ttl_split_is_assumed_and_counted,
        test_the_driver_transcript_resolves_from_the_wf_dir,
        test_the_run_is_found_through_the_task_folders_orch_config,
        test_no_corpus_is_a_clean_skip,
        test_an_archived_session_reports_the_driver_unavailable_not_zero,
        test_a_whole_analysis_writes_nothing_anywhere,
        test_the_baseline_measures_the_prefix_this_repo_owns,
        test_the_baseline_gates_and_says_which_number_it_used,
        test_the_budget_test_outranks_the_callers_ceiling_once_it_exists,
        test_a_folded_yaml_description_is_measured_not_read_as_zero,
        test_the_baseline_survives_a_torn_multibyte_read,
        test_the_cli_prints_json_and_refuses_what_it_cannot_parse,
    ):
        test()
    print()
    for reason in skips:
        print(f"skipped: {reason}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("all cost reader (D-21) tests passed")


if __name__ == "__main__":
    main()
