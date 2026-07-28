#!/usr/bin/env python3
"""Stdlib-only tests for sp-fixtures-freeze (R-03 base, R-41 amendment,
R-58:fixtures). Run as `python3 test_fixtures.py`; exits non-zero on failure.
No pytest, no runner.

The manifest test is the test (per the sub-plan), but a sha256 list alone only
proves "these bytes did not change" — it cannot tell a reviewer that the corpus
still contains the *shapes* the plan froze it for. So each fixture group also
gets a shape assertion naming the finding it exists to serve. Together they mean:
the bytes are frozen, and the bytes are still the right bytes.

Everything here is read-only. See tests/fixtures/PROVENANCE.md for what each
fixture is, where it came from, and how to regenerate the manifest deliberately.

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
