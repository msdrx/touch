#!/usr/bin/env python3
"""Stdlib-only tests for sp-fixtures-freeze (R-03 base, R-41 amendment,
R-58:fixtures) and sp-fixtures (LC-01). Run as `python3 test_fixtures.py`;
exits non-zero on failure. No pytest, no runner.

The manifest test is the test (per the sub-plan), but a sha256 list alone only
proves "these bytes did not change" — it cannot tell a reviewer that the corpus
still contains the *shapes* the plan froze it for. So each fixture group also
gets a shape assertion naming the finding it exists to serve. Together they mean:
the bytes are frozen, and the bytes are still the right bytes.

Everything here is read-only. See tests/fixtures/PROVENANCE.md for what each
fixture is, where it came from, and how to regenerate the manifest deliberately.

Six of the seven groups are verbatim captures. The seventh, `context/` (LC-01),
is CONSTRUCTED — the compaction boundary, the >1 `usage.iterations` array and
the refused-usage row do not exist in any real subagent transcript on this
machine, and without them GD-LC-2's greatest-timestamp rule and the
tempting-and-wrong `max`-over-turns rule are indistinguishable. Synthetic
fixtures therefore get MORE shape assertions than captured ones, not fewer: a
capture is evidence of itself, whereas a construction is only worth what its
assertions pin.

That group is also the one whose filenames are deliberately NOT `agent-*.jsonl`:
`tests/test_token_crosscheck.py`'s corpus arm globs `tests/fixtures/**/agent-*.jsonl`
and its domain is real harness bytes, one of these specimens is malformed BY
CONSTRUCTION, and a labelled synthetic must not silently join a corpus that is
evidence about the harness. `test_context_group_stays_out_of_the_corpus_glob`
pins that boundary so a sixth specimen cannot re-open it by accident.

The fixtures are TRACKED, so every hash/shape arm runs in a clean checkout too.
The one exception is `test_fixtures_are_trackable`, which asks git whether any
fixture is ignored: outside a git checkout (an unpacked `git archive`, a
packaged copy) git answers rc=128 and the check SKIPs with a printed reason
rather than reporting a fixture problem it did not find (RENAME-SCOPE-15).
"""
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIX = REPO / "tests" / "fixtures"
MANIFEST = FIX / "MANIFEST.sha256"

# Not frozen: the manifest cannot hash itself, and PROVENANCE.md is prose that
# may be improved without re-freezing the corpus.
UNMANIFESTED = {"MANIFEST.sha256", "PROVENANCE.md"}

DD = "dd469822-2546-47d9-aaa3-31db4cb705e8"      # first session of wf_829e6f58-b2f
E4 = "e423cd3c-f859-45af-9afd-0d6bdec9b4ac"      # session after the /clear
N2 = "292fc08c-923d-4ab4-8ff2-a9572417dbc8"      # R-58 replay session
A8 = "a8d43bb1-0313-45d4-8784-4827af443ead"      # the still-running session

RUN = FIX / "run-wf_829e6f58"
WF = "subagents/workflows/wf_829e6f58-b2f"
LEGACY = FIX / "legacy"
MIRROR = FIX / "mirror"
CONTEXT = FIX / "context"          # LC-01, the one labelled-synthetic group

# Provenance arms of CUSTOMSTATE-3 / GD-28: `agent`/`tokens` => derived,
# `title` => asserted, anything else => unknown (unattributable).
ATTRIBUTING_KEYS = {"agent", "tokens", "title", "w"}

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


def is_git_checkout():
    """True when REPO is the working tree of a git repo (not just inside one).

    The toplevel is compared against REPO rather than merely asking "did git
    find *a* repo": an archive unpacked inside some other checkout would
    otherwise interrogate that stranger's history and answer about its
    `.gitignore`, not ours.
    """
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(REPO),
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return False
    try:
        return Path(proc.stdout.strip()).resolve() == REPO
    except OSError:
        return False


GIT = is_git_checkout()


def records(path):
    """Parse a .jsonl fixture into records, skipping blank lines."""
    return [json.loads(l) for l in path.read_bytes().split(b"\n") if l.strip()]


def _ms(ts):
    """Harness ISO timestamp -> epoch ms. `Z` is spelled out because
    datetime.fromisoformat rejected it before Python 3.11 (RUNSTATE-6)."""
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


def raw_lines(path):
    return [l for l in path.read_bytes().split(b"\n") if l.strip()]


def fixture_files():
    return sorted(p for p in FIX.rglob("*") if p.is_file())


def rel(p):
    return str(p.relative_to(FIX))


# --- the freeze itself: every byte accounted for, in both directions ---------
def test_manifest_complete_and_stable():
    print("test_manifest_complete_and_stable")
    check(MANIFEST.is_file(), "MANIFEST.sha256 exists")
    if not MANIFEST.is_file():
        return
    listed = {}
    malformed = []
    for line in MANIFEST.read_text().splitlines():
        digest, sep, path = line.partition("  ")
        if not sep or not re.fullmatch(r"[0-9a-f]{64}", digest) or not path:
            malformed.append(line)
            continue
        listed[path] = digest
    check(not malformed, f"every manifest line is `<sha256>  <path>` (bad: {malformed[:3]})")
    check(len(listed) >= 60, f"manifest is a corpus, not a stub ({len(listed)} entries)")

    on_disk = {rel(p) for p in fixture_files()} - UNMANIFESTED
    missing = sorted(set(listed) - on_disk)
    extra = sorted(on_disk - set(listed))
    check(not missing, f"no manifested fixture is missing from disk ({missing[:5]})")
    check(not extra, f"no fixture on disk is unmanifested ({extra[:5]})")

    changed = []
    for path, digest in sorted(listed.items()):
        p = FIX / path
        if not p.is_file():
            continue
        if hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            changed.append(path)
    check(not changed, f"every fixture is byte-identical to its manifest hash ({changed[:5]})")


# --- the corpus must survive a clone: nothing here may be gitignored --------
# Only a git checkout can answer this: `git check-ignore` exits 128 in a tree
# with no `.git` (an unpacked `git archive`, a packaged copy), which is not a
# finding about the fixtures at all. Skip with a printed reason there — the
# manifest, newline and JSONL arms below need no git and keep running
# unconditionally, so the freeze itself is still asserted on tracked bytes.
def test_fixtures_are_trackable():
    print("test_fixtures_are_trackable")
    if not GIT:
        skip(f"test_fixtures_are_trackable: {REPO} is not a git checkout "
             f"(unpacked archive / packaged copy) — git check-ignore cannot answer")
        return
    paths = [str(p.relative_to(REPO)) for p in fixture_files()]
    proc = subprocess.run(["git", "check-ignore", "--stdin"], cwd=str(REPO),
                          input="\n".join(paths), capture_output=True, text=True)
    # rc 1 = nothing matched = nothing ignored; rc 0 = at least one ignored path
    ignored = [l for l in proc.stdout.splitlines() if l.strip()]
    check(proc.returncode in (0, 1), f"git check-ignore ran (rc={proc.returncode})")
    check(not ignored, f"no fixture is gitignored ({ignored[:5]})")


# --- verbatim-bytes conventions, exactly as the harness wrote them ----------
def test_newline_conventions():
    print("test_newline_conventions")
    bad_jsonl, bad_json = [], []
    for p in fixture_files():
        data = p.read_bytes()
        if not data:
            continue
        if p.suffix == ".jsonl" and not data.endswith(b"\n"):
            bad_jsonl.append(rel(p))
        # Single-object harness artifacts are written WITHOUT a trailing newline.
        # anchors.json / *.index.json are ours and do end with one, so they are
        # excluded from the "no trailing newline" half.
        if (p.suffix == ".json" and not p.name.endswith((".index.json", "anchors.json"))
                and data.endswith(b"\n")):
            bad_json.append(rel(p))
    check(not bad_jsonl, f"every .jsonl ends with a newline ({bad_jsonl[:3]})")
    check(not bad_json,
          f"harness .json/.meta.json keep their missing trailing newline ({bad_json[:3]})")


def test_every_jsonl_line_parses():
    print("test_every_jsonl_line_parses")
    bad = []
    total = 0
    for p in sorted(FIX.rglob("*.jsonl")):
        for i, line in enumerate(p.read_bytes().split(b"\n"), 1):
            if not line.strip():
                continue
            total += 1
            try:
                json.loads(line)
            except Exception as exc:
                bad.append(f"{rel(p)}:{i} {exc}")
    check(total > 3000, f"the corpus holds real volume ({total} records)")
    check(not bad, f"no truncated/torn record in any .jsonl ({bad[:3]})")
    for p in sorted(FIX.rglob("*.json")):
        try:
            json.loads(p.read_text())
        except Exception as exc:
            check(False, f"{rel(p)} parses as JSON ({exc})")


# --- R-03: the completed multi-session run ---------------------------------
def test_run_wf_829e6f58_shape():
    print("test_run_wf_829e6f58_shape")
    tx = sorted(RUN.glob(f"*/{WF}/agent-*.jsonl"))
    wf_meta = sorted(RUN.glob(f"*/{WF}/agent-*.meta.json"))
    task_meta = sorted(RUN.glob("*/subagents/agent-*.meta.json"))
    check(len(tx) == 8, f"8 agent transcript files (found {len(tx)})")
    ids = {p.name[len("agent-"):-len(".jsonl")] for p in tx}
    check(len(ids) == 7, f"over 7 distinct agentIds — one id appears twice (found {len(ids)})")
    check(len(wf_meta) == 7, f"7 workflow .meta.json, per R-03 (found {len(wf_meta)})")
    check(len(task_meta) == 2, f"2 Task-tool .meta.json (the other shape) (found {len(task_meta)})")
    for p in wf_meta:
        m = json.loads(p.read_text())
        check(m.get("agentType") == "workflow-subagent" and "model" in m,
              f"{p.name} is the workflow-subagent meta shape")
    for p in task_meta:
        m = json.loads(p.read_text())
        check("description" in m and "toolUseId" in m,
              f"{p.name} is the Task-tool meta shape (description + toolUseId)")

    journal = records(RUN / DD / WF / "journal.jsonl")
    kinds = [r.get("type") for r in journal]
    check(kinds.count("started") == 7 and kinds.count("result") == 7,
          f"journal is 7 started + 7 result (got {kinds.count('started')}/{kinds.count('result')})")

    snap_path = RUN / E4 / "workflows" / "wf_829e6f58-b2f.json"
    check(snap_path.is_file(), "the terminal snapshot sits in the OTHER session dir")
    check(not (RUN / DD / "workflows").exists(),
          "the session holding the journal has no workflows/ dir at all")
    snap = json.loads(snap_path.read_text())
    check(snap.get("status") == "completed", "snapshot status is completed")
    # SESSIONJSONL-7: agentCount is the distinct NODE count, never len(agents).
    check(snap.get("agentCount") == 7, f"snapshot agentCount is 7 (got {snap.get('agentCount')})")
    check(len(snap.get("workflowProgress") or []) == 9,
          "snapshot workflowProgress has 9 node rows — the '9' in R-03's prose")
    check(len(list((RUN / E4 / "tool-results").glob("*.txt"))) == 4,
          "4 persisted-output spill bodies are present so pointer records resolve")


def test_cross_session_disjoint_continuations():
    print("test_cross_session_disjoint_continuations")
    a = RUN / DD / WF / "agent-a2fc883c96ff7b837.jsonl"
    b = RUN / E4 / WF / "agent-a2fc883c96ff7b837.jsonl"
    check(a.is_file() and b.is_file(), "the same agentId exists under BOTH session dirs")
    ra, rb = records(a), records(b)
    check(len(ra) == 223, f"first fragment has 223 records (got {len(ra)})")
    check(len(rb) == 2, f"continuation has 2 records (got {len(rb)})")
    check(not b.with_suffix(".meta.json").exists(),
          "the continuation has NO .meta.json (nothing spawned it)")
    ua = {r.get("uuid") for r in ra if r.get("uuid")}
    ub = {r.get("uuid") for r in rb if r.get("uuid")}
    # MONGOSCHEMA-9: disjoint continuations, not two copies => _id=agentId unions.
    check(ub and not (ua & ub), "zero uuid overlap: these are continuations, not copies")
    # SESSIONJSONL-3: the ONLY thread between the fragments is parentUuid, and it
    # lands on the very last record of fragment 1 — nothing else joins them.
    check(rb[0].get("parentUuid") == ra[-1].get("uuid"),
          "the continuation's first parentUuid IS fragment 1's last uuid")
    check(rb[1].get("parentUuid") == rb[0].get("uuid"),
          "and the continuation chains internally from there")
    check({r.get("sessionId") for r in ra} == {DD},
          "fragment 1 carries the first sessionId")
    check({r.get("sessionId") for r in rb} == {E4},
          "fragment 2's sessionId was rewritten to the new session")
    # MONGOSCHEMA-9 measures the two files 17 min apart — that is where each
    # fragment BEGINS. The seam itself is tight (fragment 1's last record to
    # fragment 2's first is under a minute), which is why only parentUuid stitches
    # them and why a time-window heuristic would merge the wrong agents.
    gap = (_ms(rb[0]["timestamp"]) - _ms(ra[0]["timestamp"])) / 60000.0
    check(15 < gap < 20, f"the two fragments START ~17 min apart (got {gap:.1f} min)")
    seam = (_ms(rb[0]["timestamp"]) - _ms(ra[-1]["timestamp"])) / 60000.0
    check(0 < seam < 5, f"but the seam across the /clear is under a minute ({seam:.1f} min)")


# --- R-03/R-41/R-58: the legacy streams and their anchors ------------------
def test_legacy_anchors():
    print("test_legacy_anchors")
    doc = json.loads((LEGACY / "anchors.json").read_text())
    streams = doc["streams"]
    check(len(streams) == 4, f"all four real streams are frozen (got {len(streams)})")
    for name, spec in sorted(streams.items()):
        path = LEGACY / name
        check(path.is_file(), f"{name} exists")
        if not path.is_file():
            continue
        recs = records(path)
        check(len(recs) == spec["lines"], f"{name}: {spec['lines']} lines (got {len(recs)})")
        unattr = sum(1 for r in recs if not (ATTRIBUTING_KEYS & set(r)))
        check(unattr == spec["unattributable"],
              f"{name}: {spec['unattributable']} unattributable lines (got {unattr})")
        check(all("w" not in r for r in recs),
              f"{name}: no line carries the R-39 `w` field — that is why it is the legacy specimen")
        for a in spec["anchors"]:
            r = recs[a["line"] - 1]
            ok = (r["plan"] == a["plan"] and r["stage"] == a["stage"]
                  and r["state"] == a["state"]
                  and r["detail"].startswith(a["detail_startswith"]))
            check(ok, f"{name}:{a['line']} is still {a['what']}")
        for pair in spec["duplicate_stage_terminals"]:
            keys = {(recs[i - 1]["plan"], recs[i - 1]["stage"], recs[i - 1]["state"])
                    for i in pair}
            check(len(keys) == 1,
                  f"{name}: lines {pair} are the same (plan,stage,state) written twice")
        for i in spec["ts_inversions"]:
            prev, cur = (recs[i - 2]["ts"].replace("Z", "+00:00"),
                         recs[i - 1]["ts"].replace("Z", "+00:00"))
            check(cur < prev, f"{name}:{i} is still a ts inversion (append order != ts order)")


def test_legacy_terminal_conflicts():
    print("test_legacy_terminal_conflicts")
    doc = json.loads((LEGACY / "anchors.json").read_text())
    # SD-4: last-event-wins in FILE ORDER on (plan, stage='plan').
    seen_conflict = False
    for name, spec in sorted(doc["streams"].items()):
        recs = records(LEGACY / name)
        for conflict in spec.get("conflicting_plan_terminals", []):
            failed, done = conflict["failed"], conflict["corrective_done"]
            f = recs[failed - 1]
            check(f["stage"] == "plan" and f["state"] == "failed"
                  and f["detail"].startswith("loop exited ->"),
                  f"{name}:{failed} is a fabricated `plan failed \"loop exited -> ...\"`")
            if done is None:
                # the re-label arm: failed, never corrected => "closed - no verdict"
                later = [i for i, r in enumerate(recs[failed:], failed + 1)
                         if r["plan"] == f["plan"] and r["stage"] == "plan"
                         and r["state"] == "done"]
                check(not later,
                      f"{name}: {f['plan']} plan failed at {failed} was never corrected")
                continue
            seen_conflict = True
            d = recs[done - 1]
            check(d["plan"] == f["plan"] and d["stage"] == "plan" and d["state"] == "done",
                  f"{name}:{done} is the corrective `plan done` for {f['plan']}")
            check(done > failed,
                  f"{name}: the correction is LATER in file order ({done} > {failed})")
            # File order is the authority, and it has to be: no terminal event
            # between the two shares the plan, so ONLY the ordering rule decides.
            between = [i for i, r in enumerate(recs[failed:done - 1], failed + 1)
                       if r["plan"] == f["plan"] and r["stage"] == "plan"]
            check(not between,
                  f"{name}: nothing else touches {f['plan']}/plan between {failed} and {done}")
            check(_ms(d["ts"]) > _ms(f["ts"]),
                  f"{name}: the correction is later by ts too, so both rules agree here")
    check(seen_conflict, "at least one failed-then-done correction pair is frozen")

    # The two-wave respawn: the only sample in existence (RUNSTATE-2).
    spec = doc["streams"]["touch-repo-recon-events.jsonl"]
    recs = records(LEGACY / "touch-repo-recon-events.jsonl")
    waves = spec["respawn_waves"]
    check(len(waves) >= 4, f"four research stages were respawned ({len(waves)} recorded)")
    for stage, lines in sorted(waves.items()):
        states = {recs[i - 1]["state"] for i in lines}
        check(len(lines) >= 2 and states == {"running"},
              f"{stage}: {len(lines)} spawn waves into one folder {lines}")
    # RUNSTATE-2 control: a user-killed run's `plan failed` is GENUINE, not the
    # fabricated kind, and must never be re-labelled away.
    for i in (101, 102):
        r = recs[i - 1]
        check(r["stage"] == "plan" and r["state"] == "failed"
              and not r["detail"].startswith("loop exited ->"),
              f"repo-recon:{i} is a genuine failure (killed run), not the fabricated shape")


def test_unattributable_twelve_of_first_130():
    print("test_unattributable_twelve_of_first_130")
    doc = json.loads((LEGACY / "anchors.json").read_text())
    spec = doc["streams"]["touch-mongo-live-events.jsonl"]
    recs = records(LEGACY / "touch-mongo-live-events.jsonl")
    n = sum(1 for r in recs[:130] if not (ATTRIBUTING_KEYS & set(r)))
    check(n == 12, f"the amendment's '12 of 130' unattributable lines are frozen (got {n})")
    check(spec["unattributable_in_first_130"] == 12, "anchors.json records the same 12")
    titled = [r for r in recs if "title" in r]
    check(bool(titled), "at least one `title` line (provenance `asserted`) is present")
    derived = [r for r in recs if {"agent", "tokens"} & set(r)]
    check(bool(derived), "watcher-shaped lines (provenance `derived`) are present")


# --- R-41: the killed run ---------------------------------------------------
def test_killed_run_shape():
    print("test_killed_run_shape")
    d = MIRROR / "wf_455b348c-e17"
    journal = records(d / "journal.jsonl")
    kinds = [r.get("type") for r in journal]
    check(kinds.count("started") == 9, f"9 agents started (got {kinds.count('started')})")
    check(kinds.count("result") == 2, f"only 2 returned — the run was killed (got {kinds.count('result')})")
    # SESSIONJSONL-4 / MONGOSCHEMA-18: (type,key) repeats => `ordinal` is required
    # and must come from journal line order, not an in-memory counter.
    seen = {}
    for r in journal:
        seen.setdefault((r.get("type"), r.get("key")), 0)
        seen[(r.get("type"), r.get("key"))] += 1
    repeats = sorted(v for v in seen.values() if v > 1)
    check(len(repeats) == 3, f"3 distinct (type,key) pairs occur twice — the retry specimen (got {len(repeats)})")

    snap = json.loads((d / "wf_455b348c-e17.json").read_text())
    check(snap.get("status") == "killed", f"snapshot status is killed (got {snap.get('status')})")
    check("error" in snap and snap["error"], "snapshot carries the abort error")
    check(snap.get("agentCount") == 6,
          f"agentCount 6 against 9 started: it counts nodes (got {snap.get('agentCount')})")
    check(len(snap.get("workflowProgress") or []) == 8, "8 workflowProgress rows")


# --- R-41 / SESSIONJSONL-6: a live run has no snapshot --------------------
def test_live_run_shape_has_no_snapshot():
    print("test_live_run_shape_has_no_snapshot")
    session = MIRROR / "live-run-shape" / A8
    run = session / "subagents/workflows/wf_b297177a-d11"
    check(run.is_dir(), "the in-flight run dir is frozen")
    # The ABSENCE is the fixture: no <runId>.json anywhere under this session.
    check(not (session / "workflows").exists(),
          "the session has NO workflows/ dir — a live run has no terminal snapshot")
    check(not list(session.rglob("wf_b297177a-d11.json")),
          "no wf_b297177a-d11.json exists anywhere in the fixture")
    journal = records(run / "journal.jsonl")
    kinds = [r.get("type") for r in journal]
    check(kinds.count("started") == 9, f"9 started (got {kinds.count('started')})")
    check(kinds.count("result") == 7,
          f"fewer results than starts — agents still running (got {kinds.count('result')})")
    check(kinds[0] == "started",
          "the run document must be derivable from the FIRST started record")
    check(len(list(run.glob("agent-*.jsonl"))) == 9, "all 9 agent transcripts are frozen")
    check(len(list(run.glob("agent-*.meta.json"))) == 9, "all 9 metas are frozen")


# --- R-58: the streams that made the watcher fabricate a FAILED badge -----
def test_r58_replay_journals_have_no_verdict():
    print("test_r58_replay_journals_have_no_verdict")
    base = MIRROR / "r58-replay" / N2
    verdict = re.compile(r'"(passed|approved)"', re.I)
    for run, (started, result) in (("wf_930e210a-6da", (7, 7)),
                                   ("wf_cca84d59-933", (6, 6))):
        jpath = base / "subagents/workflows" / run / "journal.jsonl"
        journal = records(jpath)
        kinds = [r.get("type") for r in journal]
        check(kinds.count("started") == started and kinds.count("result") == result,
              f"{run}: {started} started / {result} result (got {kinds.count('started')}/{kinds.count('result')})")
        # This is the input that triggered the defect: a research fan-out whose
        # agents return findings, never a passed/approved-shaped verdict.
        check(not verdict.search(jpath.read_text()),
              f"{run}: no passed/approved verdict anywhere in the journal")
        snap = json.loads((base / "workflows" / f"{run}.json").read_text())
        check(snap.get("status") == "completed", f"{run}: snapshot says completed")
        # SESSIONJSONL-6: the snapshot's own `timestamp` is the run's END time
        # (startTime + durationMs), not its start — so it can never be used as a
        # "run created at" value, and its absence means "still running".
        skew = abs(_ms(snap["timestamp"]) - (snap["startTime"] + snap["durationMs"]))
        check(skew < 100,
              f"{run}: snapshot timestamp == startTime + durationMs (skew {skew} ms)")
        check(_ms(snap["timestamp"]) - snap["startTime"] > 600_000,
              f"{run}: and that is ~{(_ms(snap['timestamp']) - snap['startTime'])//60000} min after the start")


# --- R-41: single-record specimens ---------------------------------------
def test_record_specimens():
    print("test_record_specimens")
    rec_dir = MIRROR / "records"

    big = rec_dir / "oversize-line.jsonl"
    data = big.read_bytes()
    check(data.count(b"\n") == 1, "the oversize specimen is exactly ONE record")
    check(len(data) == 877395, f"the oversize record is 877395 bytes (got {len(data)})")
    check(800_000 < len(data) < 16 * 1024 * 1024,
          "under the 16 MiB BSON limit but far over any inline-it-blindly budget")
    check(json.loads(data).get("type") == "user", "the oversize record parses as a user record")

    dotted = rec_dir / "file-history-snapshot-dotted.jsonl"
    idx = json.loads((rec_dir / "file-history-snapshot-dotted.index.json").read_text())
    recs = records(dotted)
    check(len(recs) == len(idx) == 33,
          f"33 dotted-key records, one index row each (got {len(recs)}/{len(idx)})")
    check(all({"source", "line", "bytes"} <= set(row) for row in idx),
          "every index row names its source file and line")

    def dotted_keys(obj):
        found = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if "." in k or k.startswith("$"):
                    found.append(k)
                found += dotted_keys(v)
        elif isinstance(obj, list):
            for v in obj:
                found += dotted_keys(v)
        return found

    check(all(r.get("type") == "file-history-snapshot" for r in recs),
          "every specimen is a file-history-snapshot record")
    check(all(dotted_keys(r) for r in recs),
          "every specimen really does carry a dotted/`$`-prefixed key (MONGOSCHEMA-8)")
    check(all("uuid" not in r and "timestamp" not in r for r in recs),
          "and none has a uuid or a timestamp — they cannot be keyed or ordered (SESSIONJSONL-1)")

    pair = records(rec_dir / "queue-operation-user-pair.jsonl")
    check(len(pair) == 2, f"the pair is 2 records (got {len(pair)})")
    check(pair[0].get("type") == "queue-operation" and "uuid" not in pair[0],
          "record 1 is a queue-operation with NO uuid")
    check(pair[1].get("type") == "user" and pair[1].get("uuid"),
          "record 2 is the user record it became, which HAS a uuid")


# --- R-41 / SESSIONJSONL-11: negative discovery --------------------------
def test_discovery_fixtures():
    print("test_discovery_fixtures")
    projects = MIRROR / "discovery" / "projects"
    slugs = sorted(p.name for p in projects.iterdir() if p.is_dir())
    check(len(slugs) == 4, f"four FOREIGN project slugs are frozen (got {len(slugs)})")
    check(all(s.startswith("-tmp-claude-1000") for s in slugs),
          "every one is a /tmp slug, i.e. not this project")
    check(all(list((projects / s).rglob("*.jsonl")) for s in slugs),
          "each foreign slug holds at least one transcript a naive enumerator would ingest")

    reg = sorted((MIRROR / "discovery" / "sessions").glob("*.json"))
    check(len(reg) == 1, f"the registry holds exactly ONE entry (got {len(reg)})")
    entry = json.loads(reg[0].read_text())
    check(reg[0].stem == str(entry["pid"]),
          "the registry filename is the raw pid — pid reuse overwrites it")
    check("procStart" in entry,
          "so session identity needs (pid, procStart), not pid alone")


# --- LC-01: the context-occupancy specimens ------------------------------
# The five shapes GD-LC-1/2/3 turn on and the real corpus does not contain: a
# compaction boundary (0 in 689 subagent transcripts), a `usage.iterations`
# array longer than 1 (0 in 7,256 sampled rows), a refused-usage row, and a
# retry pair's two independent windows. They are SYNTHETIC and PROVENANCE.md
# says so — an unlabelled synthetic would violate the freeze.
#
# The predicates below deliberately restate GD-LC-2's rule instead of importing
# any implementation: GD-LC-11 forbids the two implementations importing each
# other, and a fixture test that borrowed one of them could not fail when that
# one drifted. This is the third, independent copy, and it is the one that
# describes the BYTES.
CTX_MSG_ID = re.compile(r"^msg_")

# The five specimens and the `[monitor]` stage each one's prompt carries. The
# stages are distinct on purpose (except the retry pair, which MUST share one):
# `(plan, stage, role, attempt)` is the key the watcher's marker join uses, so
# two specimens staged into one `wf_*` tree with the same marker would collide
# on a single card. tests/fixtures/ is frozen, so this is fixable here or never.
CTX_SPECIMENS = {
    "ctx-agent-compaction-boundary.jsonl": "ctxcompact",
    "ctx-agent-iterations-multi.jsonl": "ctxiter",
    "ctx-agent-retry-attempt1.jsonl": "ctxretry",
    "ctx-agent-retry-attempt2.jsonl": "ctxretry",
    "ctx-agent-no-usable-turn.jsonl": "ctxnousable",
}

# Verbatim from the frozen corpus: 667/667 real assistant records under
# run-wf_829e6f58/ carry all three, and these are real values of each.
CTX_ATTRIBUTION = {"attributionAgent": "workflow-subagent",
                   "attributionSkill": "implement-plan", "effort": "xhigh"}


def _ctx_marker(recs):
    """The `[monitor]` marker line of the first user prompt, or None.

    Returns None rather than raising on any shape that is not a str prompt:
    real transcripts routinely carry list-valued `message.content` (651 such
    records in this corpus), and a caller that indexed blindly would abort the
    whole file instead of reporting a named failure.
    """
    first = next((r for r in recs if r.get("type") == "user"), None)
    content = ((first or {}).get("message") or {}).get("content")
    if not isinstance(content, str):
        return None
    return next((l for l in content.splitlines() if l.strip()), None)


def _ctx_components(usage):
    """GD-LC-1's three prompt-side fields, in order. output_tokens excluded."""
    return [usage.get("input_tokens"),
            usage.get("cache_creation_input_tokens"),
            usage.get("cache_read_input_tokens")]


def _ctx_strict_sum(usage):
    """The sum, or None when any component is not a plain int.

    `type(v) is int`, never `isinstance`: bool IS an int subclass in Python, and
    one of the specimens carries `cache_creation_input_tokens: true` precisely
    so a lenient check is caught here rather than on a card.
    """
    vs = _ctx_components(usage)
    if not all(type(v) is int for v in vs):
        return None
    return sum(vs)


def _ctx_usage_source(usage):
    """GD-LC-2's iterations rule: top level, except iterations[-1] when len > 1."""
    its = usage.get("iterations")
    if isinstance(its, list) and len(its) > 1:
        return its[-1]
    return usage


def _ctx_qualifying(path, agent_id):
    """Every qualifying row as (timestamp, line, occupancy), per GD-LC-2."""
    rows = []
    for i, line in enumerate(path.read_bytes().split(b"\n"), 1):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("type") != "assistant" or r.get("agentId") != agent_id:
            continue
        m = r.get("message") or {}
        if not CTX_MSG_ID.match(str(m.get("id") or "")):
            continue
        if m.get("model") == "<synthetic>":
            continue
        total = _ctx_strict_sum(_ctx_usage_source(m.get("usage") or {}))
        if not total or total <= 0:
            continue
        rows.append((r["timestamp"], i, total))
    return rows


def _ctx_agent_id(recs):
    return next((r["agentId"] for r in recs if r.get("agentId")), None)


def _ctx_qualifying_prefix(recs, upto_line):
    """Qualifying rows among the first `upto_line` records (GD-LC-3's arm)."""
    rows = []
    aid = _ctx_agent_id(recs)
    for i, r in enumerate(recs[:upto_line], 1):
        if r.get("type") != "assistant" or r.get("agentId") != aid:
            continue
        m = r.get("message") or {}
        if not CTX_MSG_ID.match(str(m.get("id") or "")) or m.get("model") == "<synthetic>":
            continue
        total = _ctx_strict_sum(_ctx_usage_source(m.get("usage") or {}))
        if total and total > 0:
            rows.append((r["timestamp"], i, total))
    return rows


def test_context_group_stays_out_of_the_corpus_glob():
    """No specimen here may be named `agent-*.jsonl`. (Attempt-2 correction.)

    `tests/test_token_crosscheck.py:145` is
    `sorted(FIX.glob("**/agent-*.jsonl"))` — recursive, unfiltered, and its
    stated domain is *real harness bytes*. This group is labelled-synthetic and
    `ctx-agent-no-usable-turn.jsonl` cannot satisfy that arm's GD-M2.2
    invariant by construction: it carries a float, a null and a bool where the
    harness only ever writes ints, which is the entire specimen.

    The boundary is therefore drawn in the FILENAMES rather than by a filter in
    an unowned test — it is structural, needs no maintenance, and covers the
    whole group instead of just the one file that happens to go red. This arm
    exists so a sixth specimen named `agent-…` cannot silently re-open it.
    """
    print("test_context_group_stays_out_of_the_corpus_glob")
    if not CONTEXT.is_dir():
        check(False, "tests/fixtures/context/ exists")
        return
    intruders = sorted(p.name for p in CONTEXT.rglob("agent-*.jsonl"))
    check(not intruders,
          f"no context/ specimen matches test_token_crosscheck's `agent-*.jsonl` "
          f"corpus glob — a labelled synthetic never joins a real-bytes corpus "
          f"({intruders})")
    # And the reverse direction: the glob's own corpus is exactly the captured
    # groups, so this arm fails loudly if the crosscheck's pattern is widened.
    corpus = sorted(rel(p) for p in FIX.glob("**/agent-*.jsonl"))
    check(corpus and not any(p.startswith("context/") for p in corpus),
          f"and the crosscheck corpus ({len(corpus)} files) holds no context/ file")


def test_context_fixture_group():
    print("test_context_fixture_group")
    expected = set(CTX_SPECIMENS)
    check(CONTEXT.is_dir(), "tests/fixtures/context/ exists")
    if not CONTEXT.is_dir():
        return
    found = {p.name for p in CONTEXT.iterdir() if p.is_file()}
    check(found == expected,
          f"exactly the five LC-01 specimens, no strays (extra {sorted(found - expected)}, "
          f"missing {sorted(expected - found)})")
    # The freeze arm above hashes every file; this one pins that the NEW group
    # is inside it, so adding a sixth specimen without re-freezing goes red.
    listed = {line.partition("  ")[2] for line in MANIFEST.read_text().splitlines()}
    unmanifested = sorted(f"context/{n}" for n in expected
                          if f"context/{n}" not in listed)
    check(not unmanifested, f"every specimen has a manifest row ({unmanifested})")

    for name in sorted(found & expected):
        recs = records(CONTEXT / name)
        aid = _ctx_agent_id(recs)
        check(bool(aid) and all(r.get("agentId") == aid for r in recs),
              f"{name}: every record carries the one agentId {aid}")
        # GD-LC-2 keys on the record's own timestamp, so every record needs one.
        check(all(r.get("timestamp") for r in recs), f"{name}: every record is timestamped")
        assistants = [r for r in recs if r.get("type") == "assistant"]
        stamps = [r["timestamp"] for r in assistants]
        check(stamps == sorted(stamps) and len(set(stamps)) == len(stamps),
              f"{name}: assistant timestamps strictly increase — a total order (GD-LC-2)")
        ids = [(r.get("message") or {}).get("id") for r in assistants]
        check(len(ids) == len(set(ids)), f"{name}: distinct message ids")

        # The [monitor] marker is FENCED (GD-D1a). It heads the first user
        # prompt but is NOT on line 1: every workflow template opens the prompt
        # with a template-literal newline (implement.workflow.js:221 and five
        # more sites, research.workflow.js:186/216), and 16/16 marker-carrying
        # user records in the captured corpus start with "\n". Pinning line 1
        # here would freeze a fact about the harness that is false.
        marker = _ctx_marker(recs)
        if marker is None:
            check(False, f"{name}: the first user prompt is a str carrying a marker")
            continue
        first_user = next(r for r in recs if r.get("type") == "user")
        check(first_user["message"]["content"].startswith("\n"),
              f"{name}: the prompt opens with the templates' leading newline")
        check(marker.startswith("[monitor] "),
              f"{name}: the [monitor] marker heads the first user prompt (GD-D1a)")
        check(" attempt=" in marker, f"{name}: and that marker names its attempt")
        check(f" stage={CTX_SPECIMENS[name]} " in marker,
              f"{name}: its stage is {CTX_SPECIMENS[name]} ({marker.split(' role=')[0]})")

        # GD-LC-2's row selection reads `message.model` and `message.usage`; the
        # attribution keys beside them are never read, and are carried only so
        # the envelope matches the 667/667 real assistant records that have
        # them. The <synthetic> 529 row is the measured exception: a real one
        # carries none of the three.
        for r in assistants:
            mid = (r.get("message") or {}).get("id")
            synthetic = (r.get("message") or {}).get("model") == "<synthetic>"
            want = {} if synthetic else CTX_ATTRIBUTION
            got = {k: r[k] for k in CTX_ATTRIBUTION if k in r}
            what = ("the <synthetic> row carries NO attribution keys" if synthetic
                    else "carries the real attribution envelope")
            check(got == want, f"{name}: {mid} {what} ({got})")


def test_context_markers_do_not_collide():
    """Distinct `(plan, stage, role, attempt)` per specimen — except the pair."""
    print("test_context_markers_do_not_collide")
    if not CONTEXT.is_dir():
        check(False, "tests/fixtures/context/ exists")
        return
    marks = {}
    for name in sorted(CTX_SPECIMENS):
        p = CONTEXT / name
        if p.is_file():
            marks[name] = _ctx_marker(records(p))
    check(len(marks) == 5, f"all five specimens readable (got {sorted(marks)})")
    check(all(m for m in marks.values()), f"all five carry a marker ({marks})")
    # The retry pair MUST share plan/stage/role and differ only in attempt
    # (GD-LC-7: same role, retried, two independent windows). Every other pair
    # must differ, or a downstream test staging two specimens into one wf_*
    # tree gets two agents claiming one card identity.
    keys = {n: m for n, m in marks.items() if m}
    dupes = sorted(n for n in keys
                   if sum(1 for o in keys if keys[o] == keys[n]) > 1)
    check(not dupes, f"no two specimens share a whole marker ({dupes})")
    stages = {n: (m.split(" stage=")[1].split(" ")[0] if m and " stage=" in m else None)
              for n, m in keys.items()}
    retry = {n for n in stages if "retry" in n}
    check(len({stages[n] for n in retry}) == 1,
          f"the retry pair shares one stage ({[stages[n] for n in sorted(retry)]})")
    others = [stages[n] for n in stages if n not in retry]
    check(len(set(others)) == len(others) and not (set(others) & {stages[n] for n in retry}),
          f"and the other three stages are distinct from it and each other ({others})")


def test_context_compaction_separates_latest_from_max():
    print("test_context_compaction_separates_latest_from_max")
    p = CONTEXT / "ctx-agent-compaction-boundary.jsonl"
    if not p.is_file():
        check(False, "ctx-agent-compaction-boundary.jsonl exists")
        return
    recs = records(p)
    rows = _ctx_qualifying(p, _ctx_agent_id(recs))
    check(len(rows) == 5, f"5 billed turns (got {len(rows)})")
    occ = [r[2] for r in rows]
    check(occ == [40000, 80000, 120000, 12000, 18000],
          f"three rising then two an order of magnitude lower (got {occ})")

    # THE POINT OF THE WHOLE FIXTURE: greatest-timestamp and max-over-turns
    # disagree here, and nowhere else in the corpus (`max` coincides with
    # `latest` on 100 % of the real bytes). Without this file the correct
    # implementation and the tempting-and-wrong one are indistinguishable.
    latest = max(rows)[2]
    check(latest == 18000, f"greatest-timestamp reading is 18000 (got {latest})")
    check(max(occ) == 120000, f"max-over-turns would say 120000 (got {max(occ)})")
    check(latest < max(occ),
          "so occupancy went DOWN: non-monotonic by design, and the D7 monotone "
          "clamp must never touch it")

    # The boundary must be a parsed RECORD. The known false positive is prose:
    # research transcripts quote `compact_boundary`/`isCompactSummary` inside
    # message content, so a grep-shaped test would pass on a file with no record
    # at all. Pinning the byte-occurrence count to the record count closes that.
    bounds = [(i, r) for i, r in enumerate(recs, 1)
              if r.get("type") == "system" and r.get("subtype") == "compact_boundary"]
    check(len(bounds) == 1, f"exactly ONE compact_boundary record (got {len(bounds)})")
    raw = p.read_bytes()
    check(raw.count(b'"compact_boundary"') == 1,
          "and exactly one byte-occurrence of the token — the record IS the grep hit, "
          "so this can never pass on prose")
    check(raw.count(b'"isCompactSummary"') == 1,
          "one isCompactSummary occurrence, likewise a record and not prose")
    if not bounds:
        return
    bline, b = bounds[0]
    cm = b.get("compactMetadata") or {}
    check({"trigger", "preTokens", "postTokens", "cumulativeDroppedTokens"} <= set(cm),
          f"compactMetadata carries the four fields GD-LC-3 names (got {sorted(cm)})")
    check(b.get("timestamp"), "the boundary carries its OWN timestamp — the `at` stamp")
    # A real boundary's preserved uuids name records that EXIST (verified on a
    # captured 2.1.220 boundary: head/tail/anchor all resolve). Touch reads none
    # of them, but a fixture whose uuids dangle is a shape claim that is false.
    present = {r.get("uuid") for r in recs}
    seg, kept = cm.get("preservedSegment") or {}, cm.get("preservedMessages") or {}
    referenced = ([seg.get(k) for k in ("headUuid", "anchorUuid", "tailUuid")]
                  + [kept.get("anchorUuid")]
                  + list(kept.get("uuids") or []) + list(kept.get("allUuids") or []))
    dangling = sorted({u for u in referenced if u not in present})
    check(referenced and not dangling,
          f"every preservedSegment/preservedMessages uuid names a record in the "
          f"file (dangling: {dangling})")
    check(seg.get("tailUuid") == b.get("logicalParentUuid"),
          "and the segment tail is the boundary's logicalParentUuid — the last "
          "record before the compaction, exactly as the real capture has it")
    check(cm["preTokens"] - cm["postTokens"] == cm["cumulativeDroppedTokens"],
          "pre - post == cumulativeDropped, so the metadata is self-consistent")
    # CC-STORES-3: preTokens is a DIFFERENT estimator from the usage-row sum and
    # must never be mixed into GD-LC-1's arithmetic. The 30-token gap is the
    # measured one, reproduced here so a "preTokens == last row" shortcut fails.
    check(cm["preTokens"] != 120000 and cm["preTokens"] - 120000 == 30,
          f"preTokens is 30 above the last pre-compaction usage row (got {cm['preTokens']})")
    check(cm["postTokens"] != 12000,
          f"and postTokens differs from the next usage row too (got {cm['postTokens']})")

    # The summary line FOLLOWS the boundary in file order while carrying an
    # EARLIER timestamp — measured verbatim on the real pair (2 ms). File order
    # and ts order genuinely disagree at the seam; neither line is a candidate
    # row, so GD-LC-2 is unaffected, but a whole-file `sorted(by ts)[-1]` is.
    summary = [(i, r) for i, r in enumerate(recs, 1) if r.get("isCompactSummary")]
    check(len(summary) == 1, f"one isCompactSummary user line (got {len(summary)})")
    if not summary:
        return
    sline, s = summary[0]
    check(s.get("type") == "user", "the compact summary is a user record")
    check(sline == bline + 1, f"it immediately follows the boundary ({sline} vs {bline})")
    check(_ms(s["timestamp"]) < _ms(b["timestamp"]),
          "yet its timestamp is EARLIER — file order != ts order at the seam")

    # The provisional branch (GD-LC-3): truncate after the summary and the
    # newest boundary is newer than the newest qualifying row, so the reading
    # becomes postTokens stamped with the boundary's own ts.
    head = _ctx_qualifying_prefix(recs, sline)
    check(bool(head) and max(head)[0] < b["timestamp"],
          "truncated at the summary, every billed row predates the boundary — "
          "this prefix is the src:\"compact\" specimen")
    check(bool(head) and max(head)[2] == 120000 and cm["postTokens"] < 120000,
          "and there the reading DROPS from 120000 to postTokens without any new "
          "usage row: a naive last-row reader would overstate 10x for the gap")


def test_context_iterations_multi():
    print("test_context_iterations_multi")
    p = CONTEXT / "ctx-agent-iterations-multi.jsonl"
    if not p.is_file():
        check(False, "ctx-agent-iterations-multi.jsonl exists")
        return
    recs = records(p)
    usages = [(r.get("message") or {}).get("usage") or {}
              for r in recs if r.get("type") == "assistant"]
    lens = [len(u["iterations"]) for u in usages if isinstance(u.get("iterations"), list)]
    check(lens == [1, 3], f"one len==1 row then one len==3 row (got {lens})")
    if 3 not in lens:
        return
    multi = next(u for u in usages if isinstance(u.get("iterations"), list)
                 and len(u["iterations"]) == 3)
    its = multi["iterations"]
    # The pessimistic assumption GD-LC-2 is written against: the top level is
    # the SUM of the iterations, so reading it reports a prompt that never
    # existed. Reading iterations[-1] is unambiguously one API call.
    for field in ("input_tokens", "cache_creation_input_tokens",
                  "cache_read_input_tokens", "output_tokens"):
        check(multi[field] == sum(i[field] for i in its),
              f"top-level {field} is the SUM of the three iterations")
    top = _ctx_strict_sum(multi)
    last = _ctx_strict_sum(its[-1])
    check(top == 65690, f"a top-level read would say 65690 (got {top})")
    check(last == 22131, f"iterations[-1] — the correct read — says 22131 (got {last})")
    check(last < top, "so the two rules are separable on these bytes")
    check(all(_ctx_strict_sum(i) and _ctx_strict_sum(i) > 20000 for i in its),
          "each iteration is a realistic prompt, not a filler row")
    # len == 1: the top level EQUALS the single iteration, so the top level is
    # what is read there (all 522 + 6,734 measured rows behave this way).
    single = next(u for u in usages if isinstance(u.get("iterations"), list)
                  and len(u["iterations"]) == 1)
    check(_ctx_strict_sum(single) == _ctx_strict_sum(single["iterations"][0]),
          "on the len==1 row the top level and the single iteration agree")


def test_context_retry_pair_has_two_windows():
    print("test_context_retry_pair_has_two_windows")
    a = CONTEXT / "ctx-agent-retry-attempt1.jsonl"
    b = CONTEXT / "ctx-agent-retry-attempt2.jsonl"
    if not (a.is_file() and b.is_file()):
        check(False, "both retry specimens exist")
        return
    ra, rb = records(a), records(b)
    ida, idb = _ctx_agent_id(ra), _ctx_agent_id(rb)
    # GD-LC-7: each retry row is its OWN agent with its own fresh window — which
    # is why no card-level tie-break is needed and no cross-agent aggregate but
    # `peak` is sanctioned.
    check(ida != idb, f"the two attempts are DIFFERENT agentIds ({ida} vs {idb})")
    marks = [_ctx_marker(ra), _ctx_marker(rb)]
    if not all(marks):
        check(False, f"both prompts carry a [monitor] marker ({marks})")
        return
    check("attempt=1" in marks[0] and "attempt=2" in marks[1],
          f"each prompt's marker names its attempt ({marks})")
    check(marks[0].split("attempt=")[0] == marks[1].split("attempt=")[0],
          "and the two markers agree on plan/stage/role — same role, retried")

    qa, qb = _ctx_qualifying(a, ida), _ctx_qualifying(b, idb)
    check(len(qa) == 3 and len(qb) == 2,
          f"3 billed turns then 2 (got {len(qa)}/{len(qb)})")
    end_a, start_b = max(qa)[2], min(qb)[2]
    check(end_a == 148900, f"attempt 1 ends high, at 148900 (got {end_a})")
    check(start_b < end_a / 4,
          f"attempt 2 starts on a FRESH window, far lower ({start_b} vs {end_a})")
    check(max(r[2] for r in qb) < end_a,
          "attempt 2 never reaches attempt 1's level — summing or merging the "
          "two would fabricate a level neither agent ever held")
    # HOOK-PLANE-7: a fresh window is never empty (measured min 21,641 over 610
    # agents), so a spawn-time `ctx 0` would understate by 21k-45k.
    check(start_b > 20000,
          f"and it is not near zero either — a fresh window already holds the "
          f"system prompt and CLAUDE.md ({start_b})")


def test_context_no_usable_turn_resolves_unknown():
    print("test_context_no_usable_turn_resolves_unknown")
    p = CONTEXT / "ctx-agent-no-usable-turn.jsonl"
    if not p.is_file():
        check(False, "ctx-agent-no-usable-turn.jsonl exists")
        return
    recs = records(p)
    aid = _ctx_agent_id(recs)
    rows = _ctx_qualifying(p, aid)
    # THE DEFINING DISCIPLINE: this file must resolve to the KEY BEING ABSENT.
    # Not 0, not null — 0 is the lie (R-58's defect class).
    check(not rows, f"ZERO qualifying rows: occupancy is unknown, never 0 (got {rows})")

    assistants = [r for r in recs if r.get("type") == "assistant"]
    check(len(assistants) == 4, f"4 assistant records, none usable (got {len(assistants)})")
    usages = [(r.get("message") or {}).get("usage") or {} for r in assistants]
    refused = [u for u in usages if _ctx_strict_sum(u) is None]
    check(len(refused) == 3, f"3 rows are refused on TYPE alone (got {len(refused)})")
    kinds = {type(v).__name__ for u in refused for v in _ctx_components(u)
             if type(v) is not int}
    check(kinds == {"bool", "float", "NoneType"},
          f"the three refusals are a float, a null and a bool (got {sorted(kinds)})")

    # bool is an int subclass in Python: a lenient `isinstance(v, int)` accepts
    # `True` and a lenient `v or 0` swallows the null. Both would yield a
    # plausible-looking number from bytes that say nothing.
    def lenient(u):
        return sum(int(v or 0) for v in _ctx_components(u)
                   if isinstance(v, (int, float)) or v is None)
    check(all(lenient(u) > 0 for u in refused),
          "every refused row WOULD produce a positive number under a lenient "
          "reader — that is what makes this fixture bite")

    synth = [r for r in assistants if (r.get("message") or {}).get("model") == "<synthetic>"]
    check(len(synth) == 1, f"exactly one <synthetic> row (got {len(synth)})")
    if synth:
        m = synth[0]["message"]
        check(all(m["usage"][k] == 0 for k in ("input_tokens", "output_tokens",
                                               "cache_creation_input_tokens",
                                               "cache_read_input_tokens")),
              "the <synthetic> row is all-zero — 30 of 649 real transcripts END on one")
        check(not CTX_MSG_ID.match(str(m.get("id"))),
              "and its id is not ^msg_ either, so it fails two of GD-LC-2's tests")
        check(synth[0].get("apiErrorStatus") == 529,
              "it is the 529 shape, copied from a real capture")
        check(max(r["timestamp"] for r in assistants) == synth[0]["timestamp"],
              "and it is the LAST assistant row — the killed-agent shape exactly")


# --- R-03's sanitisation condition, codified so a re-copy cannot slip -----
def test_no_credentials():
    print("test_no_credentials")
    # Deliberately narrow: only shapes that are real secrets by construction.
    # Loose `password=...`-style patterns match this corpus's *documentation*
    # text ("apiKey: 'your-api-key'", "$ANTHROPIC_API_KEY") and are excluded on
    # purpose — see PROVENANCE.md for the full scan that was run before freezing.
    patterns = {
        "anthropic key": re.compile(rb"sk-ant-[A-Za-z0-9_\-]{16,}"),
        "aws key id": re.compile(rb"(?:AKIA|ASIA)[0-9A-Z]{16}"),
        "github token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
        "private key block": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    }
    hits = []
    for p in fixture_files():
        data = p.read_bytes()
        for label, rx in patterns.items():
            if rx.search(data):
                hits.append(f"{label} in {rel(p)}")
    check(not hits, f"no real credential shape anywhere in the corpus ({hits[:3]})")


def main():
    for t in (test_manifest_complete_and_stable,
              test_fixtures_are_trackable,
              test_newline_conventions,
              test_every_jsonl_line_parses,
              test_run_wf_829e6f58_shape,
              test_cross_session_disjoint_continuations,
              test_legacy_anchors,
              test_legacy_terminal_conflicts,
              test_unattributable_twelve_of_first_130,
              test_killed_run_shape,
              test_live_run_shape_has_no_snapshot,
              test_r58_replay_journals_have_no_verdict,
              test_record_specimens,
              test_discovery_fixtures,
              test_context_group_stays_out_of_the_corpus_glob,
              test_context_fixture_group,
              test_context_markers_do_not_collide,
              test_context_compaction_separates_latest_from_max,
              test_context_iterations_multi,
              test_context_retry_pair_has_two_windows,
              test_context_no_usable_turn_resolves_unknown,
              test_no_credentials):
        t()
    print()
    for message in skips:
        print(f"skipped: {message}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all sp-fixtures-freeze tests passed ({len(skips)} skipped)")


if __name__ == "__main__":
    main()
