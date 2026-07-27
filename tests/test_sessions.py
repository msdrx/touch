#!/usr/bin/env python3
"""Stdlib-only tests for aggregator/sessions.py (R-25 as amended, R-46). Run as
`python3 test_sessions.py`; exits non-zero on failure. No pytest, no runner.

The two items' own test lists, one function each:

* R-25 — "injectable fake `/proc` + registry; the 1-registry-entry vs
  6-transcripts case; `lost+found` fixture";
* R-46 — "mirror the fixture project dir ⇒ 6 session documents, exactly one
  `live:`; the four foreign slug dirs are NOT ingested; promotion leaves both
  docs queryable with no `_id` rewrite".

The four foreign slug directories and the single registry entry are the
**frozen fixtures** (`tests/fixtures/mirror/discovery/`, sp-02), copied into a
temporary `~/.claude` so a test can add the in-scope half without writing to
the frozen corpus. sp-02 froze no top-level `<sessionId>.jsonl` transcripts —
those are the one thing this module reads, and it never reads their *contents*
(a `.jsonl` line belongs to `ingest.py`, GD-15), so the six in-scope
transcripts are created as files with the right names. Their bytes are
irrelevant to every assertion here, and pretending otherwise by freezing
16 MB of transcript would test nothing extra.
"""

import ast
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from aggregator import mongo_store as ms                # noqa: E402
from aggregator import refs                             # noqa: E402
from aggregator import sessions as sess                 # noqa: E402
from aggregator.sessions import (                       # noqa: E402
    CLASSES,
    DEFAULT_CLASS,
    MIRROR_MAPPERS,
    MIRROR_SOURCES,
    PROVENANCE,
    Prior,
    PromotionObservation,
    SessionObservation,
    SessionsError,
    Source,
    claude_root,
    iter_promotion_observations,
    iter_session_observations,
    map_promotion,
    map_session,
    project_slugs,
    read_alias_slugs,
    read_history_sessions,
    read_proc_start,
    read_registry,
    scan,
    session_id_for_path,
    slug_for,
)

FIX = REPO / "tests" / "fixtures" / "mirror" / "discovery"
failures = []
skips = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def skip(msg):
    print(f"  skip: {msg}")
    skips.append(msg)


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:                                    # noqa: BLE001
        return False
    return False


# --- the world under test -------------------------------------------------

#: The project the frozen registry entry names (`sessions/15934.json`).
CWD = "/home/laniakea/Projects/touch"
SLUG = "-home-laniakea-Projects-touch"
PID = 15934
PROC_START = "4101211"
LIVE_ID = "a8d43bb1-0313-45d4-8784-4827af443ead"       # the registry's sessionId

#: Six transcripts, of which the registry names one — R-25's "1-registry-entry
#: vs 6-transcripts case", and R-46's "6 session documents, exactly one live:".
SIX = (
    "00b1a921-0c8f-47db-a74c-86145bd4ff3e",
    "0b1c07f4-1517-4d09-a174-1b245a337827",
    "292fc08c-923d-4ab4-8ff2-a9572417dbc8",
    LIVE_ID,
    "dd469822-2546-47d9-aaa3-31db4cb705e8",
    "e423cd3c-f859-45af-9afd-0d6bdec9b4ac",
)

#: In `history.jsonl`, never on disk — R-46's transcriptless seventh.
SEVENTH = "7f0b9961-a2da-4ac0-b4d5-7fb52b838d4b"

#: Every sessionId living in one of the four foreign slug dirs. A
#: `projects/*/*.jsonl` enumerator ingests all of them (SESSIONJSONL-11).
FOREIGN_IDS = (
    "d59f6015-9d08-4dc4-ac17-c23f10472595",
    "a994a689-a579-4b9f-a998-55006c5bc678",
    "08ffb13f-2e24-4c06-ac9b-f2e8d0a7d789",
    "385b3740-39fd-48f1-9ce7-2a40a588e5fb",
    "793bc57c-d56e-426a-8efc-2db0892ca411",
    "8084340e-a56b-499f-b54d-cec64e52da78",
    "26a625c4-7007-412a-a03d-4b72b473e298",
    "a7d4f0ce-1e30-4648-ad61-680c0233e2a7",
)


def fake_stat(tmp, pid=PID, start=PROC_START, comm="claude (code)"):
    """A `/proc/<pid>/stat` whose `comm` contains a space AND a parenthesis.

    Field 2 is `(comm)` and is not escaped by the kernel, so the realistic
    specimen is the one that breaks a naive `line.split()[21]`. Field 22 sits
    at index 19 of the tokens after the LAST `)`.
    """
    root = os.path.join(tmp, "proc", str(pid))
    os.makedirs(root, exist_ok=True)
    tail = " ".join(str(n) for n in range(4, 22))       # fields 4…21
    with open(os.path.join(root, "stat"), "w", encoding="utf-8") as fh:
        fh.write(f"{pid} ({comm}) S {tail} {start} 0 0 0\n")
    return os.path.join(tmp, "proc")


def build_root(tmp, *, transcripts=SIX, registry=True, history=True, aliases=None,
               foreign=True, extra_slug_files=()):
    """A temporary `~/.claude` — frozen fixtures plus the in-scope half."""
    root = os.path.join(tmp, "claude")
    projects = os.path.join(root, "projects")
    os.makedirs(projects, exist_ok=True)
    os.makedirs(os.path.join(root, "sessions"), exist_ok=True)
    if foreign:
        for entry in sorted(os.listdir(FIX / "projects")):
            shutil.copytree(FIX / "projects" / entry, os.path.join(projects, entry))
    slug_dir = os.path.join(projects, SLUG)
    os.makedirs(slug_dir, exist_ok=True)
    for session_id in transcripts:
        with open(os.path.join(slug_dir, f"{session_id}.jsonl"), "w", encoding="utf-8") as fh:
            fh.write('{"type":"user","sessionId":"%s"}\n' % session_id)
    # A session's own subdirectory: agent transcripts and journals live here
    # and are NOT sessions (they are keyed by agentId — SESSIONJSONL-3).
    deep = os.path.join(slug_dir, SIX[0], "subagents", "workflows", "wf_829e6f58-b2f")
    os.makedirs(deep, exist_ok=True)
    for name in ("agent-a2fc883c96ff7b837.jsonl", "journal.jsonl"):
        with open(os.path.join(deep, name), "w", encoding="utf-8") as fh:
            fh.write("{}\n")
    for name, body in extra_slug_files:
        with open(os.path.join(slug_dir, name), "w", encoding="utf-8") as fh:
            fh.write(body)
    if registry:
        shutil.copy2(FIX / "sessions" / "15934.json",
                     os.path.join(root, "sessions", "15934.json"))
        # Both tolerated shapes from R-25's test list, present on the real
        # machine: a directory that is not JSON, and a zero-byte file.
        os.makedirs(os.path.join(root, "sessions", "lost+found"), exist_ok=True)
        open(os.path.join(root, "sessions", "99999.json"), "w").close()
    if history:
        lines = [
            {"display": "a prompt whose text is nobody's business",
             "pastedContents": {"1": {"content": "secret paste"}},
             "timestamp": 1784987605035, "project": CWD, "sessionId": SEVENTH},
            {"display": "x", "pastedContents": {}, "timestamp": 1784987605036,
             "project": CWD, "sessionId": SIX[0]},
            {"display": "x", "pastedContents": {}, "timestamp": 1784987605037,
             "project": "/tmp/claude-1000/liveio", "sessionId": FOREIGN_IDS[2]},
            {"not": "a history record"},
        ]
        with open(os.path.join(root, "history.jsonl"), "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")
            fh.write("{not json at all\n")
    if aliases is not None:
        target = os.path.join(projects, SLUG, ".session-aliases")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(aliases)
    return root


def state_of(ops):
    return ms.apply_operations({}, ops)


def all_ops(scanned):
    out = []
    for kind, observation in scanned.observations():
        out.extend(MIRROR_MAPPERS[kind](observation))
    return out


# --- the slug rule --------------------------------------------------------
def test_slug_rule_reproduces_the_directory_names_on_disk():
    print("test_slug_rule_reproduces_the_directory_names_on_disk")
    check(slug_for(CWD) == SLUG, f"{CWD} -> {SLUG}")
    check(slug_for("/tmp/claude-1000/liveio") == "-tmp-claude-1000-liveio",
          "the second real (cwd, slug) pair on this machine")
    nested = ("/tmp/claude-1000/-home-laniakea-Projects-touch/"
              "dd469822-2546-47d9-aaa3-31db4cb705e8/scratchpad/castprobe")
    frozen = sorted(p.name for p in (FIX / "projects").iterdir())
    check(slug_for(nested) in frozen,
          "…and the doubled `--` of a nested slug is reproduced, not approximated: "
          f"{slug_for(nested)}")
    check(slug_for("/a/b.c_d") == "-a-b-c-d",
          "every non-alphanumeric collapses to `-` (`.` and `_` included)")


# --- R-25: injectable /proc, the registry, lost+found ---------------------
def test_proc_start_is_field_22_even_when_comm_has_spaces_and_parens():
    print("test_proc_start_is_field_22_even_when_comm_has_spaces_and_parens")
    with tempfile.TemporaryDirectory() as tmp:
        proc = fake_stat(tmp)
        check(read_proc_start(PID, proc_root=proc) == PROC_START,
              f"field 22 read past a comm containing ' ' and ')': {PROC_START}")
        # The naive implementation, for contrast: splitting the whole line.
        raw = Path(proc, str(PID), "stat").read_text().split()
        check(raw[21] != PROC_START,
              "…and a plain `line.split()[21]` really does read the wrong field here")
        check(read_proc_start(4242, proc_root=proc) is None,
              "no such process ⇒ None (not live), never an exception")
        check(read_proc_start("nonsense", proc_root=proc) is None,
              "a nonsense pid ⇒ None")
        check(read_proc_start(PID, proc_root=os.path.join(tmp, "no-procfs")) is None,
              "no procfs at all ⇒ None (a system without /proc is not a crash)")


def test_registry_tolerates_lost_found_and_zero_byte_files():
    print("test_registry_tolerates_lost_found_and_zero_byte_files")
    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp)
        proc = fake_stat(tmp)
        skipped = sess._skips()
        entries = read_registry(root, cwd=CWD, slugs=[SLUG], proc_root=proc, skipped=skipped)
        check(len(entries) == 1, f"one live entry survives, got {len(entries)}")
        check(entries[0].session_id == LIVE_ID and entries[0].pid == PID,
              "…and it is the frozen fixture's entry")
        check(skipped["registry_not_json"] == 1, "`lost+found` counted, not fatal")
        check(skipped["registry_unreadable"] == 1, "the zero-byte registry file counted")
        check(entries[0].fields.get("name") == "touch-36",
              "the allowlisted registry fields come through")
        check("status" not in entries[0].fields and "updatedAt" not in entries[0].fields,
              "…and `status` does not (GD-23 keeps no liveness in a mirror document)")


def test_a_reused_pid_is_not_a_live_session():
    print("test_a_reused_pid_is_not_a_live_session")
    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp)
        # Same pid, different start time: the process that owned this registry
        # entry is gone and something else holds its number. The filename is
        # the raw pid, so this is the failure the layout invites.
        proc = fake_stat(tmp, start="9999999")
        skipped = sess._skips()
        entries = read_registry(root, cwd=CWD, slugs=[SLUG], proc_root=proc, skipped=skipped)
        check(entries == [], "pid reuse ⇒ no live session")
        check(skipped["registry_stale_pid"] == 1, "…and it is counted as a stale pid")

        scanned = scan(cwd=CWD, root=root, proc_root=proc)
        keys = [s.key() for s in scanned.sessions]
        check(not any(k.startswith("live:") for k in keys),
              "…so the whole scan falls to the historical arm")
        check(refs.hist_session_key(LIVE_ID) in keys,
              "…and the session the stale entry named is a `hist:` document")


def test_a_registry_entry_for_another_project_is_out_of_scope():
    print("test_a_registry_entry_for_another_project_is_out_of_scope")
    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp)
        proc = fake_stat(tmp, pid=777)
        entry = json.loads((FIX / "sessions" / "15934.json").read_text())
        entry.update({"pid": 777, "cwd": "/tmp/claude-1000/liveio",
                      "sessionId": FOREIGN_IDS[2], "procStart": PROC_START})
        Path(root, "sessions", "777.json").write_text(json.dumps(entry))
        skipped = sess._skips()
        entries = read_registry(root, cwd=CWD, slugs=[SLUG], proc_root=proc, skipped=skipped)
        check([e.pid for e in entries] == [], "a live session of another project is skipped")
        check(skipped["registry_out_of_scope"] == 1,
              "…on the entry's own `cwd`, which needs no slug arithmetic at all")


# --- R-46: the acceptance scan -------------------------------------------
def test_the_project_dir_yields_six_documents_exactly_one_live():
    print("test_the_project_dir_yields_six_documents_exactly_one_live")
    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp, history=False)
        proc = fake_stat(tmp)
        scanned = scan(cwd=CWD, root=root, proc_root=proc)
        docs = state_of(all_ops(scanned))["sessions"]

        check(len(docs) == 6, f"six session documents, got {len(docs)}")
        live = [key for key in docs if key.startswith("live:")]
        check(live == [f"live:{PID}-{PROC_START}"],
              f"exactly one `live:` id, and it is (pid, procStart)-keyed: {live}")
        check(sum(1 for key in docs if key.startswith("hist:")) == 5,
              "…and the other five are the historical arm")
        check(all(doc["provenance"] == PROVENANCE for doc in docs.values()),
              "every document carries GD-28's mandatory provenance")
        check(all(doc["class"] == DEFAULT_CLASS for doc in docs.values()),
              "…and GD-6's class, which is `observed` for everything discovery finds")
        try:
            for key, doc in docs.items():
                ms.validate_document("sessions", doc)
            check(True, "…and every document validates against GD-24's row")
        except Exception as exc:                         # noqa: BLE001
            check(False, f"a document fails GD-24's row: {type(exc).__name__}: {exc}")

        # The live session's transcript is a SOURCE of the live document, not a
        # second document — which is why the count is six and not seven.
        live_doc = docs[live[0]]
        kinds = sorted(source["kind"] for source in live_doc["sources"])
        check(kinds == ["registry", "transcript"],
              f"the live document's sources are its registry entry and its transcript: {kinds}")
        check(live_doc["sessionIds"] == [LIVE_ID],
              "…and the sessionId is carried in `sessionIds`, never in the `_id`")
        check(refs.hist_session_key(LIVE_ID) not in docs,
              "…and no historical twin was conjured for it")


def test_the_four_foreign_slug_dirs_are_not_ingested():
    print("test_the_four_foreign_slug_dirs_are_not_ingested")
    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp)
        proc = fake_stat(tmp)
        scanned = scan(cwd=CWD, root=root, proc_root=proc)
        keys = set(state_of(all_ops(scanned))["sessions"])

        on_disk = sorted(p.name for p in Path(root, "projects").iterdir())
        check(len(on_disk) == 5, f"the tree really does hold four foreign slugs: {len(on_disk)}")
        check(scanned.slugs == (SLUG,), f"…and scope is one slug: {scanned.slugs}")
        leaked = [i for i in FOREIGN_IDS if refs.hist_session_key(i) in keys]
        check(not leaked, f"no foreign sessionId is ingested: {leaked}")
        check(len(keys) == 7, f"six transcripts + the transcriptless seventh: {len(keys)}")

        # A `projects/*/*.jsonl` enumerator — the version R-25 originally
        # specified — would have picked up every one of them.
        naive = sum(1 for p in Path(root, "projects").glob("*/*.jsonl"))
        check(naive == len(SIX) + len(FOREIGN_IDS),
              f"…where the unscoped enumerator would have found {naive}")


def test_the_transcriptless_seventh_session_is_sources_empty():
    print("test_the_transcriptless_seventh_session_is_sources_empty")
    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp)
        proc = fake_stat(tmp)
        scanned = scan(cwd=CWD, root=root, proc_root=proc)
        check(scanned.history_only == (SEVENTH,),
              f"one sessionId exists only in history.jsonl: {scanned.history_only}")
        check(scanned.skipped["history_bad_line"] == 1,
              "…and the unparseable history line is counted, not fatal")
        check(SEVENTH in [o.session_id for o in scanned.sessions],
              "…and a well-formed record that simply is not a history entry is "
              "passed over without poisoning the count (the format is open)")

        docs = state_of(all_ops(scanned))["sessions"]
        doc = docs[refs.hist_session_key(SEVENTH)]
        check(doc["sources"] == [],
              "the transcriptless session is recorded with `sources: []` (R-46)")
        check("sources" in doc, "…the field EXISTS — empty and missing are different facts")
        try:
            ms.validate_document("sessions", doc)
            check(True, "…and it is still a valid session document")
        except Exception as exc:                         # noqa: BLE001
            check(False, f"the transcriptless document is invalid: "
                         f"{type(exc).__name__}: {exc}")

        blob = json.dumps(docs, default=str)
        check("secret paste" not in blob and "nobody's business" not in blob,
              "…and history.jsonl's prompt text never reaches a document")


def test_promotion_annotates_the_hist_doc_and_rewrites_no_id():
    print("test_promotion_annotates_the_hist_doc_and_rewrites_no_id")
    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp, history=False)
        proc = fake_stat(tmp)
        hist_key = refs.hist_session_key(LIVE_ID)
        live_key = refs.session_key(PID, PROC_START)

        # Pass 1 is a `--backfill`: transcripts only, no registry. The session
        # whose process is running right now therefore lands as `hist:`.
        backfilled = []
        for path in sorted(Path(root, "projects", SLUG).glob("*.jsonl")):
            backfilled.extend(iter_session_observations(str(path), cwd=CWD, root=root))
        state = state_of([op for obs in backfilled for op in map_session(obs)])
        check(hist_key in state["sessions"],
              "a backfill writes the live session as `hist:` — it has no registry to read")
        check(not any(k.startswith("live:") for k in state["sessions"]),
              "…and writes no `live:` document at all")

        # Pass 2 is a live scan that knows what the mirror already holds.
        prior = Prior(ids=frozenset(state["sessions"]))
        scanned = scan(cwd=CWD, root=root, proc_root=proc, prior=prior)
        check(len(scanned.promotions) == 1, "…so the next scan emits exactly one promotion")
        ms.apply_operations(state, all_ops(scanned))

        check(hist_key in state["sessions"] and live_key in state["sessions"],
              "both documents are queryable afterwards")
        check(state["sessions"][hist_key]["_id"] == hist_key,
              "…the historical `_id` is untouched (R-46: `_id` is immutable)")
        check(state["sessions"][hist_key]["promotedTo"] == live_key,
              "…it points at the live document")
        check(state["sessions"][live_key]["sessionIds"] == [LIVE_ID],
              "…and the live document reached the sessionId through `$addToSet`")
        check(state["sessions"][hist_key]["class"] == DEFAULT_CLASS
              and state["sessions"][hist_key]["provenance"] == PROVENANCE,
              "…with the identity `$setOnInsert` payload intact")

        # Without `prior`, nothing is promoted: a stateless pass never asserts
        # the existence of a document it has not seen.
        check(scan(cwd=CWD, root=root, proc_root=proc).promotions == (),
              "a first pass emits no promotion (and so writes one doc per session)")


def test_a_promotion_is_inert_on_the_wired_path():
    print("test_a_promotion_is_inert_on_the_wired_path")
    # THE HANDOFF, ASSERTED. `Prior` is the sole gate on promotions and on
    # GD-26's `present:false` sources, and the seam `mirror` declares —
    # `source(path=None)` — has no way to carry one: `iter_rebuild_observations`
    # calls `source()`, `iter_backfill_observations` calls `source(path)`, and
    # `prior` is keyword-only. So both features are implemented, unit-tested
    # above, and INERT until the component that owns a mirror handle supplies a
    # `Prior`. That component is not this sub-plan's file. A wrong green is
    # worse than a red, so the gap is a test, not a comment.
    import inspect

    from aggregator import mirror as mr

    for fn in (iter_session_observations, iter_promotion_observations):
        kinds = inspect.signature(fn).parameters
        check(kinds["prior"].kind is inspect.Parameter.KEYWORD_ONLY,
              f"{fn.__name__}: `prior` is keyword-only — the declared seam "
              f"`source(path=None)` cannot pass it")

    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp, history=False)
        proc = fake_stat(tmp)
        hist_key = refs.hist_session_key(LIVE_ID)

        # Directly, with a `Prior`: one promotion, and a vanished source is kept
        # as `present:false`. Both features work when someone supplies the input.
        gone = f"projects/{SLUG}/{SIX[1]}.jsonl"
        prior = Prior(ids=frozenset([hist_key]),
                      sources={refs.hist_session_key(SIX[0]): [gone]})
        promoted = iter_promotion_observations(cwd=CWD, root=root, proc_root=proc,
                                               prior=prior)
        absent = [s for o in iter_session_observations(cwd=CWD, root=root,
                                                       proc_root=proc, prior=prior)
                  for s in o.sources if not s.present]
        check(len(promoted) == 1 and len(absent) == 1,
              "with a `Prior`: one promotion and one `present:false` source")

        # Through `mirror.iter_sources`, which is the only path production has:
        # nothing. Same tree, same live process, no `Prior` to be had.
        saved = {k: os.environ.get(k) for k in ("TOUCH_CLAUDE_ROOT", "TOUCH_PROJECT_CWD")}
        os.environ["TOUCH_CLAUDE_ROOT"] = root
        os.environ["TOUCH_PROJECT_CWD"] = CWD
        try:
            wired = list(mr.iter_rebuild_observations(registry_modules=["sessions"]))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        kinds = sorted({kind for kind, _ in wired})
        check(kinds == ["session"],
              f"through the wired seam only `session` observations arrive: {kinds}")
        check(not [o for kind, o in wired if kind == "sessionPromotion"],
              "R-46's promotion never fires on the wired path today (no `Prior`)")
        check(not [s for _, o in wired for s in o.sources if not s.present],
              "…and no `present:false` element is ever written there either")


def test_two_session_ids_on_one_live_id_merge_via_add_to_set():
    print("test_two_session_ids_on_one_live_id_merge_via_add_to_set")
    # A UNIT TEST OF THE MAPPER'S ALGEBRA, not of a discovery behaviour:
    # `scan()` cannot produce this pair from any tree. `/clear` rewrites the
    # registry entry to name only the NEW sessionId, and nothing on disk records
    # the old one against the same pid (CONVO-4), so the pre-`/clear` session
    # stays an unlinked `hist:` document — see "What is deliberately NOT joined"
    # in the module docstring, and the assertion at the end of this test.
    first = SessionObservation(session_id=SIX[2], pid=PID, proc_start=PROC_START,
                               session_ids=(SIX[2],), cwd=CWD)
    second = SessionObservation(session_id=LIVE_ID, pid=PID, proc_start=PROC_START,
                                session_ids=(LIVE_ID,), cwd=CWD)
    forward = state_of(map_session(first) + map_session(second))
    backward = state_of(map_session(second) + map_session(first))
    key = refs.session_key(PID, PROC_START)
    check(sorted(forward["sessions"][key]["sessionIds"]) == sorted([SIX[2], LIVE_ID]),
          "both sessionIds survive on one live document")
    check(len(forward["sessions"]) == 1 and len(backward["sessions"]) == 1,
          "…as ONE document, not two ($addToSet, never $set — GD-25/MONGOSCHEMA-9)")
    # `sessionIds` is a *set* field, so mongod and the model both append in
    # arrival order and `fingerprint` sorts it — the fingerprint is what GD-25
    # compares, and it is the thing that must not depend on ingest order.
    check(ms.fingerprint(forward) == ms.fingerprint(backward),
          "…and the two orders fingerprint identically")

    # …and the gap the algebra is ready for. The registry names `a8d43bb1…`;
    # `292fc08c…` is the sessionId the same process had before its `/clear`, and
    # both transcripts are on disk. Discovery does not join them, and says so.
    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp, history=False)
        proc = fake_stat(tmp)
        scanned = scan(cwd=CWD, root=root, proc_root=proc,
                       prior=Prior(ids=frozenset([refs.hist_session_key(SIX[2])])))
        live = [o for o in scanned.sessions if o.live]
        check([o.session_ids for o in live] == [(LIVE_ID,)],
              f"`scan()` never puts two sessionIds on one live id: {[o.session_ids for o in live]}")
        check([p.session_id for p in scanned.promotions] == [],
              "…and the pre-`/clear` session is not promoted either — the "
              "registry entry names only the current sessionId (CONVO-4)")
        docs = state_of(all_ops(scanned))["sessions"]
        check("promotedTo" not in docs[refs.hist_session_key(SIX[2])],
              "…so it stays an unlinked `hist:` document, which is the honest answer")


# --- .session-aliases -----------------------------------------------------
def test_session_aliases_widen_scope_in_both_plausible_formats():
    print("test_session_aliases_widen_scope_in_both_plausible_formats")
    other = "-tmp-claude-1000-liveio"
    for label, body in (("json array", json.dumps([other])),
                        ("one per line", f"# written by recordSessionAlias\n{other}\n\n"),
                        ("a path, slugified", "/tmp/claude-1000/liveio\n")):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp, aliases=body)
            slugs = project_slugs(CWD, root)
            check(slugs == [SLUG, other], f"{label}: scope widens to {slugs}")
            scanned = scan(cwd=CWD, root=root, proc_root=os.path.join(tmp, "nope"))
            keys = set(state_of(all_ops(scanned))["sessions"])
            check(refs.hist_session_key(FOREIGN_IDS[2]) in keys,
                  f"{label}: …and the aliased slug's transcripts are now in scope")

    # An alias file in no format we know, containing a NUL. `project_slugs`
    # joins alias entries straight into a filesystem path, so an unfiltered
    # entry raised `ValueError: embedded null byte` out of `open()` and killed
    # the whole scan — a discovery pass destroyed by a file it was tolerating.
    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp, aliases="\x00\x01\n..\n")
        skipped = sess._skips()
        aliases = read_alias_slugs(root, SLUG, skipped=skipped)
        check(aliases == [], f"a NUL-bearing or `..` alias entry is refused: {aliases}")
        check(skipped["alias_rejected"] == 2, "…and counted, so the refusal is visible")
        scanned = scan(cwd=CWD, root=root, proc_root=os.path.join(tmp, "nope"))
        check(scanned.slugs == (SLUG,) and len(scanned.sessions) == len(SIX) + 1,
              "…and the scan completes with scope unchanged")

    # An entry that is all punctuation. `/` slugifies to `-` and `//` to `--`:
    # both match the character class, neither is a slug any `claude` run wrote,
    # and admitting them widens scope to `projects/-`.
    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp, aliases="/\n//\n-\n")
        skipped = sess._skips()
        check(read_alias_slugs(root, SLUG, skipped=skipped) == [],
              "an alias entry with no alphanumeric character is not a slug")
        check(project_slugs(CWD, root) == [SLUG], "…so scope does not widen to `projects/-`")

    # The unknown-format counter is per FILE, not scan-wide. Two alias files:
    # the first rejects an entry, the second is in no format we know (it opens
    # `{`, announcing JSON, and is not a JSON array). Counting the second
    # against the scan-wide rejection total silently loses it — precisely in
    # the multi-slug case the transitive closure exists for.
    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp, foreign=False, aliases=json.dumps(["..", "-b"]))
        Path(root, "projects", "-b").mkdir()
        Path(root, "projects", "-b", ".session-aliases").write_text(
            json.dumps({"aliases": ["-c"]}))
        Path(root, "projects", "-c").mkdir()
        skipped = sess._skips()
        slugs = project_slugs(CWD, root, skipped=skipped)
        check(slugs == [SLUG, "-b"], f"the closure is what the readable entries say: {slugs}")
        check(skipped["alias_rejected"] == 1, "the `..` entry is counted as rejected")
        check(skipped["alias_unreadable"] == 1,
              "…and the SECOND file's unknown format is still counted, though an "
              "earlier file had already rejected an entry")
        check("-c" not in slugs,
              "…and a JSON document we do not recognise widens scope to nothing — "
              "line-parsing it would invent entries the file never stated")

    # An UNDERSTOOD file with no aliases in it is not an unreadable one. `[]` is
    # the obvious spelling of "this project has none" (and what
    # `recordSessionAlias` leaves after the last alias is removed); so is a file
    # of nothing but the comment its own second format uses. Counting either as
    # unreadable corrupts the distinction the counters exist for, in the
    # direction that manufactures alarm.
    for label, body in (("an empty JSON array", "[]"),
                        ("comments only", "# written by recordSessionAlias\n"),
                        ("an empty file", "   \n\n"),
                        ("blank lines only", "\n\n\n")):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp, foreign=False, aliases=body)
            skipped = sess._skips()
            check(read_alias_slugs(root, SLUG, skipped=skipped) == [],
                  f"{label}: no aliases")
            check(skipped["alias_unreadable"] == 0 and skipped["alias_rejected"] == 0,
                  f"{label}: …and nothing is counted — the file was understood, "
                  f"it simply named none ({skipped['alias_unreadable']} unreadable)")

    # A JSON array whose members are not strings: the CONTAINER is understood,
    # so the file is not unreadable — its entries are refused, one count each.
    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp, foreign=False, aliases=json.dumps([1, 2, "-b"]))
        Path(root, "projects", "-b").mkdir()
        skipped = sess._skips()
        check(read_alias_slugs(root, SLUG, skipped=skipped) == ["-b"],
              "a JSON array's string members are still read")
        check(skipped["alias_rejected"] == 2 and skipped["alias_unreadable"] == 0,
              f"…and its two non-string members are rejected entries, not an "
              f"unreadable file: {skipped['alias_rejected']}/{skipped['alias_unreadable']}")


def test_alias_closure_is_cycle_safe_and_bounded():
    print("test_alias_closure_is_cycle_safe_and_bounded")
    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp, foreign=False, aliases=json.dumps(["-b"]))
        projects = Path(root, "projects")
        (projects / "-b").mkdir(exist_ok=True)
        (projects / "-b" / ".session-aliases").write_text(json.dumps([SLUG, "-c"]))
        (projects / "-c").mkdir(exist_ok=True)
        (projects / "-c" / ".session-aliases").write_text(json.dumps(["-b"]))
        check(project_slugs(CWD, root) == [SLUG, "-b", "-c"],
              "a cycle terminates and every slug appears once")

    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp, foreign=False, aliases=json.dumps([f"-s{n}" for n in range(80)]))
        skipped = sess._skips()
        slugs = project_slugs(CWD, root, skipped=skipped)
        check(len(slugs) == sess.MAX_SLUGS, f"the closure is capped at {sess.MAX_SLUGS}")
        check(skipped["slug_cap"] == 1, "…and hitting the cap is counted, not silent")


def test_the_per_path_seam_does_not_reread_the_alias_closure():
    print("test_the_per_path_seam_does_not_reread_the_alias_closure")
    # `mirror.iter_backfill_observations` calls EVERY registered source once per
    # `.jsonl` in the corpus and states the contract in its own docstring:
    # "returning `()` for a path you do not own … must cost one `str`
    # comparison". Deriving the transitive alias closure per call `open()`s one
    # `.session-aliases` per slug per file — owned and foreign alike.
    other = "-tmp-claude-1000-liveio"
    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp, aliases=json.dumps([other]))
        sess.reset_scope_cache()
        calls = []
        real = sess.project_slugs

        def counted(*args, **kwargs):
            calls.append(args[:2])
            return real(*args, **kwargs)

        sess.project_slugs = counted
        try:
            paths = sorted(str(p) for p in Path(root, "projects").glob("*/*.jsonl"))
            owned = [o for path in paths
                     for o in iter_session_observations(path, cwd=CWD, root=root)]
        finally:
            sess.project_slugs = real
        check(len(paths) == len(SIX) + len(FOREIGN_IDS),
              f"the walk really does hand over every transcript in the corpus: {len(paths)}")
        check(len(calls) == 1,
              f"…and the closure is derived ONCE for the whole walk, not per file: {len(calls)}")
        in_scope = sorted(str(p) for slug in (SLUG, other)
                          for p in Path(root, "projects", slug).glob("*.jsonl"))
        check(sorted(str(o.sources[0].path) for o in owned) ==
              sorted(path[len(root) + 1:] for path in in_scope),
              f"…while ownership is unchanged: exactly the two in-scope slugs' "
              f"{len(in_scope)} transcripts, never the three foreign slugs'")

        # The ownership test is ROOTED, not a basename match. A transcript in a
        # directory merely NAMED like an in-scope slug, somewhere else entirely,
        # is not this project's file — otherwise the scope rule stops scoping the
        # moment a caller's walk is rooted elsewhere.
        decoy = Path(tmp, "elsewhere", SLUG)
        decoy.mkdir(parents=True)
        (decoy / f"{SIX[0]}.jsonl").write_text("{}\n")
        check(iter_session_observations(str(decoy / f"{SIX[0]}.jsonl"),
                                        cwd=CWD, root=root) == [],
              "a lookalike directory outside <root>/projects is not owned")
        check(len(iter_session_observations(
            os.path.join(root, "projects", SLUG, f"{SIX[0]}.jsonl"),
            cwd=CWD, root=root)) == 1,
            "…while the real one, under the root, still is")

        # m2: the per-path seam is the ONLY path a `--backfill` takes, so the
        # closure's refusals must be visible somewhere. `scan()` reports them on
        # `Scan.skipped`; here they live on the memo, read back rather than
        # accumulated per call so asking twice cannot double-count.
        sess.reset_scope_cache()
        Path(root, "projects", SLUG, ".session-aliases").write_text(
            json.dumps([other, "..", "-\x00-"]))
        owned = iter_session_observations(
            os.path.join(root, "projects", SLUG, f"{SIX[0]}.jsonl"), cwd=CWD, root=root)
        check(len(owned) == 1 and bool(sess._SCOPE_CACHE),
              "the per-path seam — and nothing else — computed this closure")
        skips = sess.scope_skips(CWD, root)
        check(skips["alias_rejected"] == 2,
              f"a rejected alias entry seen ONLY through the per-path seam is still "
              f"counted, though a backfill never builds a `Scan`: "
              f"{skips['alias_rejected']}")
        check(sess.scope_skips(CWD, root) == skips,
              "…and reading the counters twice reports the same numbers, not double")
        check(sum(skips.values()) == 2,
              f"…with nothing else silently skipped on that closure: {skips}")

        # The memo is per `(cwd, root)` and explicitly resettable, because an
        # alias file written *during* a run is a real (if rare) event — and a
        # full `scan()` never consults it at all.
        sess.reset_scope_cache()
        Path(root, "projects", "-b").mkdir()
        Path(root, "projects", SLUG, ".session-aliases").write_text(json.dumps([other]))
        check(sess.scoped_slugs(CWD, root) == (SLUG, other),
              "the memo answers with the closure it computed")
        Path(root, "projects", SLUG, ".session-aliases").write_text(json.dumps([other, "-b"]))
        check(sess.scoped_slugs(CWD, root) == (SLUG, other),
              "…and keeps answering with it after the alias file changes underneath")
        check(scan(cwd=CWD, root=root,
                   proc_root=os.path.join(tmp, "nope")).slugs == (SLUG, other, "-b"),
              "…while a full scan reads the new alias file immediately (it never caches)")
        sess.reset_scope_cache()
        check(sess.scoped_slugs(CWD, root) == (SLUG, other, "-b"),
              "…and `reset_scope_cache()` is how the seam catches up")

        # The memo is bounded: a long-lived server handed many (cwd, root) pairs
        # must not grow a module-level dict forever.
        for n in range(sess.MAX_SCOPE_KEYS * 3):
            sess.scoped_slugs(f"{CWD}/sub{n}", root)
        check(len(sess._SCOPE_CACHE) <= sess.MAX_SCOPE_KEYS,
              f"the scope memo is capped at {sess.MAX_SCOPE_KEYS} keys, "
              f"not unbounded: {len(sess._SCOPE_CACHE)}")
        sess.reset_scope_cache()


# --- GD-26: sources ------------------------------------------------------
def test_a_disappeared_source_is_a_field_not_a_removal():
    print("test_a_disappeared_source_is_a_field_not_a_removal")
    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp, transcripts=SIX[:1], registry=False, history=False)
        gone = f"projects/{SLUG}/{SIX[1]}.jsonl"
        key = refs.hist_session_key(SIX[0])
        prior = Prior(sources={key: [f"projects/{SLUG}/{SIX[0]}.jsonl", gone]})
        scanned = scan(cwd=CWD, root=root, proc_root=os.path.join(tmp, "nope"), prior=prior)
        doc = state_of(all_ops(scanned))["sessions"][key]
        by_path = {source["path"]: source for source in doc["sources"]}
        check(by_path[f"projects/{SLUG}/{SIX[0]}.jsonl"]["present"] is True,
              "the file still on disk is present")
        check(gone in by_path and by_path[gone]["present"] is False,
              "…and the one that vanished is retained with `present:false` (GD-26)")
        check(all(not source["path"].startswith("/") for source in doc["sources"]),
              "source paths are root-relative — a fingerprint must not depend on $HOME")


def test_source_elements_have_a_pinned_field_order():
    print("test_source_elements_have_a_pinned_field_order")
    element = Source("projects/x/y.jsonl").as_element()
    check(list(element) == ["path", "kind", "present"],
          f"fixed BSON field order: {list(element)}")
    check(raises(SessionsError, Source("p", "invented").as_element),
          "an unknown source kind is refused (a second spelling is a second set element)")
    # `$addToSet` is BSON-identity based, so the pin is what makes it a set.
    obs = SessionObservation(session_id=SIX[0], sources=(Source("p"), Source("p")))
    doc = state_of(map_session(obs))["sessions"][refs.hist_session_key(SIX[0])]
    check(len(doc["sources"]) == 1, "…and re-observing one source adds one element, not two")
    # A `sources` element handed over as a plain dict (a replay, a fixture) goes
    # through the same door as the observation itself, so a malformed one is
    # this module's `SessionsError` and not a bare `TypeError` from `Source(**…)`.
    check(map_session(dict(session_id=SIX[0],
                           sources=[dict(path="p", kind="registry")])) ==
          map_session(SessionObservation(session_id=SIX[0],
                                         sources=(Source("p", "registry"),))),
          "a dict source element maps identically to the dataclass")
    check(raises(SessionsError, map_session,
                 dict(session_id=SIX[0], sources=[dict(path="p", invented=1)])),
          "…and a malformed one raises SessionsError, the contract this module states")
    # GD-26's element set is keyed by (path, kind): the same path seen as two
    # kinds is two facts, and `_kind_for` derives the kind of a vanished source.
    prior_paths = ["sessions/15934.json", f"projects/{SLUG}/{SIX[1]}.jsonl"]
    absent = sess._with_absent([Source("sessions/15934.json", "registry")], prior_paths)
    check([(s.path, s.kind, s.present) for s in absent] ==
          [("sessions/15934.json", "registry", True),
           (f"projects/{SLUG}/{SIX[1]}.jsonl", "transcript", False)],
          f"a still-present source is not duplicated as absent: {absent}")


# --- SD-1 / SD-11: the mapping half --------------------------------------
def test_mappers_are_registered_pure_and_write_only_sessions():
    print("test_mappers_are_registered_pure_and_write_only_sessions")
    from aggregator import mirror as mr

    registry = mr.discover_mappers()
    check(set(MIRROR_MAPPERS) <= set(registry),
          f"mirror.discover_mappers finds this module's kinds: {sorted(MIRROR_MAPPERS)}")
    check(registry["session"].module == "sessions", "…attributed to `sessions`")

    source = (REPO / "aggregator" / "sessions.py").read_text(encoding="utf-8")
    check("pymongo" not in source,
          "no pymongo here (GD-21: only mongo_store and mirror may import it)")

    tree = ast.parse(source)
    impure = {"open", "listdir", "walk", "stat", "getcwd", "expanduser", "realpath",
              "now", "utcnow", "today", "monotonic", "time", "getenv"}

    # Every function AND method defined in the module, so the walk below can
    # follow a mapper into its helpers. Inspecting only the four named entry
    # points would pass a module whose `_as_observation` opened a file.
    defs = {node.name: node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def calls_of(node):
        out = set()
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Name):
                out.add(call.func.id)
            elif isinstance(call.func, ast.Attribute):
                out.add(call.func.attr)
        return out

    for name in ("map_session", "map_promotion", "_identity_on_insert", "_only_sessions"):
        reached, queue, called = set(), [name], set()
        while queue:                                   # transitive, not one level
            current = queue.pop()
            if current in reached:
                continue
            reached.add(current)
            here = calls_of(defs[current])
            called |= here
            queue.extend(sorted(here & set(defs)))
        check(not (called & impure),
              f"{name} does no I/O and reads no clock, transitively through "
              f"{len(reached) - 1} module-local helpers (SD-1): {sorted(called & impure)}")
    check("as_element" in defs and "as_element" in calls_of(defs["map_session"]),
          "…and the walk really does reach the helpers: `map_session` → `as_element`")

    # Every operation validates at mirror's own boundary — the `_id` really did
    # come from `refs.ref_key`, and the update is inside GD-25's algebra.
    obs = SessionObservation(session_id=SIX[0], pid=PID, proc_start=PROC_START,
                             session_ids=(SIX[0],), cwd=CWD, slugs=(SLUG,),
                             sources=(Source("sessions/15934.json", "registry"),))
    try:
        for op in map_session(obs) + map_promotion(
                PromotionObservation(SIX[1], f"live:{PID}-{PROC_START}")):
            mr.validate_op(op, source="sessions")
        check(True, "…and mirror.validate_op accepts every operation both mappers emit")
    except Exception as exc:                             # noqa: BLE001
        check(False, f"mirror.validate_op refused an operation this module emits: "
                     f"{type(exc).__name__}: {exc}")

    # SESSIONJSONL-3, structurally: nothing here may key an agent record.
    check(raises(SessionsError, sess._only_sessions, [("agents", "a" * 17, {})]),
          "an operation for any collection but `sessions` is refused in code, not in review")
    check(all(op[0] == "sessions" for op in map_session(obs)),
          "…and the mapper only ever emits `sessions` operations")


def test_the_algebra_is_order_independent():
    print("test_the_algebra_is_order_independent")
    import random

    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp)
        proc = fake_stat(tmp)
        hist_key = refs.hist_session_key(LIVE_ID)
        scanned = scan(cwd=CWD, root=root, proc_root=proc,
                       prior=Prior(ids=frozenset([hist_key])))
        pairs = list(scanned.observations())
        check(len(pairs) == 8, f"seven sessions plus one promotion: {len(pairs)}")

        def replay(order):
            ops = []
            for kind, observation in order:
                ops.extend(MIRROR_MAPPERS[kind](observation))
            return state_of(ops)

        normal = replay(pairs)
        shuffled = list(pairs)
        random.Random(58).shuffle(shuffled)
        prints = {label: ms.fingerprint(replay(order))
                  for label, order in (("normal", pairs),
                                       ("shuffled", shuffled),
                                       ("reversed", list(reversed(pairs))))}
        check(len(set(prints.values())) == 1,
              f"normal/shuffled/reversed fingerprint identically (GD-25): {prints}")
        check(ms.counts(normal) == {"sessions": 8},
              f"…and the counts are the expected ones, not a silent collapse: "
              f"{ms.counts(normal)}")
        check(len(replay(pairs + pairs)["sessions"]) == 8,
              "a double ingest lands on its own output (upsert-only)")

        # `$setOnInsert` is the one order-dependent operator in the algebra, so
        # the promotion and the session op must agree on their payload —
        # verbatim, with nothing filtered out of the comparison.
        promotion_op = map_promotion(scanned.promotions[0])[0][2]
        promo = promotion_op["$setOnInsert"]
        hist = SessionObservation(session_id=LIVE_ID, cwd=CWD)
        check(promo == map_session(hist)[0][2]["$setOnInsert"],
              f"the two writers of one `_id` carry the same immutables: {promo}")
        check("sources" not in promo,
              "…and `sources` is in neither payload: `$setOnInsert:{sources:[]}` next to "
              "`$addToSet:{sources:…}` is 'would create a conflict at sources' on mongod 7, "
              "so the empty case cannot live in the immutables")
        check(promotion_op["$addToSet"]["sources"] == {"$each": []},
              "the promotion carries the empty `$addToSet` instead — a no-op on an "
              "existing document, and it creates `sources: []` if the promotion is the "
              "operation that inserts")
        promotion_first = state_of(map_promotion(scanned.promotions[0])
                                   + map_session(hist))
        check(promotion_first["sessions"][refs.hist_session_key(LIVE_ID)]["sources"] == [],
              "…so a document created by a promotion still has the field (R-46)")


def test_class_and_provenance_are_immutables_not_verdicts():
    print("test_class_and_provenance_are_immutables_not_verdicts")
    obs = SessionObservation(session_id=SIX[0])
    update = map_session(obs)[0][2]
    check("class" in update["$setOnInsert"] and "provenance" in update["$setOnInsert"],
          "both are `$setOnInsert` — the mirror stores observations, not verdicts (GD-23)")
    check("$set" not in update or "class" not in update.get("$set", {}),
          "…never `$set`, which would make the stored class a race between writers")
    check(update["$setOnInsert"]["class"] == DEFAULT_CLASS
          and DEFAULT_CLASS in CLASSES,
          f"…and the value is one of GD-6's {CLASSES}")
    check(raises(SessionsError, map_session,
                 SessionObservation(session_id=SIX[0], session_class="invented")),
          "a class outside GD-6's three is refused")
    check(raises(SessionsError, map_session, SessionObservation()),
          "an observation with no sessionId is refused (a wrong `_id` never reaches a store)")
    check(raises(SessionsError, map_promotion,
                 PromotionObservation(SIX[0], refs.hist_session_key(SIX[1]))),
          "`promotedTo` must name the live arm — promoting to a `hist:` id is a bug")
    check(map_session(dict(session_id=SIX[0])) == map_session(obs),
          "a mapper accepts the plain dict a replay hands back, identically")


def test_the_union_is_gd24s_and_refs_owns_both_grammars():
    print("test_the_union_is_gd24s_and_refs_owns_both_grammars")
    live = SessionObservation(session_id=SIX[0], pid=PID, proc_start=PROC_START).key()
    hist = SessionObservation(session_id=SIX[0]).key()
    check(live == f"live:{PID}-{PROC_START}" and hist == f"hist:{SIX[0]}",
          f"the two GD-24 grammars: {live} | {hist}")
    check(ms.COLLECTIONS["sessions"].id_kinds == ("session", "histSession"),
          "…both declared on the collection row")
    for key in (live, hist):
        check(ms.check_id("sessions", key) == key, f"mongo_store accepts {key}")
    check(raises(refs.RefError, SessionObservation(session_id="NOT-A-UUID").key),
          "a malformed sessionId cannot become an `_id`")
    check(raises(refs.RefError, SessionObservation(session_id=SIX[0], pid=0,
                                                   proc_start=PROC_START).key),
          "…nor can pid 0")
    check(session_id_for_path(f"/x/{SIX[0]}.jsonl") == SIX[0],
          "a transcript basename names its session")
    check(session_id_for_path("/x/agent-a2fc883c96ff7b837.jsonl") is None
          and session_id_for_path("/x/journal.jsonl") is None,
          "…and an agent transcript or a journal names none")


# --- the mirror seam -----------------------------------------------------
def test_mirror_sources_answer_only_for_paths_they_own():
    print("test_mirror_sources_answer_only_for_paths_they_own")
    from aggregator import mirror as mr

    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp, history=False)
        proc = fake_stat(tmp)
        check(set(MIRROR_SOURCES) == set(MIRROR_MAPPERS),
              "every source has a mapper and vice versa")
        check(dict(mr.iter_sources())["session"] is iter_session_observations,
              "mirror.iter_sources finds the seam by name")

        owned = os.path.join(root, "projects", SLUG, f"{SIX[0]}.jsonl")
        got = iter_session_observations(owned, cwd=CWD, root=root)
        check(len(got) == 1 and got[0].key() == refs.hist_session_key(SIX[0]),
              "an in-scope transcript yields exactly one `hist:` observation")
        check(not got[0].live,
              "…the per-path mode never consults the registry, so never the live arm")

        for label, path in (
                ("a foreign slug's transcript",
                 os.path.join(root, "projects", "-tmp-claude-1000-liveio",
                              f"{FOREIGN_IDS[2]}.jsonl")),
                ("an agent transcript", os.path.join(
                    root, "projects", SLUG, SIX[0], "subagents", "workflows",
                    "wf_829e6f58-b2f", "agent-a2fc883c96ff7b837.jsonl")),
                ("a journal", os.path.join(
                    root, "projects", SLUG, SIX[0], "subagents", "workflows",
                    "wf_829e6f58-b2f", "journal.jsonl"))):
            check(iter_session_observations(path, cwd=CWD, root=root) == [],
                  f"{label} is not this source's file")
        check(iter_promotion_observations(owned, cwd=CWD, root=root) == [],
              "a promotion is a statement about a live process; no file attests to one")

        full = iter_session_observations(None, cwd=CWD, root=root, proc_root=proc)
        check(len(full) == 6 and sum(1 for o in full if o.live) == 1,
              f"`path=None` is the full scan: {len(full)} observations")


def test_backfill_observations_carry_no_timestamp():
    print("test_backfill_observations_carry_no_timestamp")
    from aggregator import mirror as mr

    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp, history=False)
        path = os.path.join(root, "projects", SLUG, f"{SIX[0]}.jsonl")
        obs = iter_session_observations(path, cwd=CWD, root=root)[0]
        stamps = [ts for op in map_session(obs) for ts in mr.op_timestamps(op)]
        check(stamps == [],
              "a historical session operation stores no datetime at all — so a "
              "backfill can never stamp history with the import's clock")

        # The live arm's two timestamps come from the registry's own clock,
        # through $min/$max, so they are order-independent (GD-25).
        live = SessionObservation(session_id=SIX[0], pid=PID, proc_start=PROC_START,
                                  first_ts=sess._epoch_ms(1784987605035),
                                  last_ts=sess._epoch_ms(1784991967695))
        update = map_session(live)[0][2]
        check(set(update["$min"]) == {"firstTs"} and set(update["$max"]) == {"lastTs"},
              "…and the live arm uses $min/$max, never $set, on the two accumulables")
        check(sess._epoch_ms(1784987605035).microsecond == 35000,
              "epoch ms → a millisecond-precision UTC Date (BSON has no finer)")
        check(sess._epoch_ms(0) is None and sess._epoch_ms("x") is None
              and sess._epoch_ms(True) is None,
              "…and a missing/absurd registry timestamp is None, never `now()`")


def test_an_absurd_registry_timestamp_cannot_kill_the_pass():
    print("test_an_absurd_registry_timestamp_cannot_kill_the_pass")
    # A registry file is a file. `json.load` accepts bare `Infinity`/`NaN`, and
    # `1e18` is a plain number — each of which crashed the conversion
    # (`OverflowError`, `ValueError`, "year 31690708 is out of range"). The
    # exception escaped `read_registry` (whose `try` covers `json.load` only),
    # then `scan`, then `mirror.iter_rebuild_observations`, which calls its
    # sources with no handler: ONE corrupt file, every session lost.
    for label, raw in (("1e18", "1e18"), ("Infinity", "Infinity"),
                       ("NaN", "NaN"), ("10**19", str(10 ** 19)),
                       ("negative", "-1"), ("a string", '"1784987605035"')):
        check(sess._epoch_ms(json.loads(raw)) is None,
              f"{label} converts to None, not an exception")

    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp, history=False)
        proc = fake_stat(tmp)
        path = Path(root, "sessions", "15934.json")
        entry = json.loads(path.read_text())
        entry["startedAt"] = float("inf")
        entry["updatedAt"] = 10 ** 19
        path.write_text(json.dumps(entry))            # json.dump writes bare Infinity

        skipped = sess._skips()
        entries = read_registry(root, cwd=CWD, slugs=[SLUG], proc_root=proc, skipped=skipped)
        check(len(entries) == 1 and entries[0].session_id == LIVE_ID,
              "the entry itself survives — a bad clock is not a bad session")
        check(entries[0].started_at is None and entries[0].updated_at is None,
              "…it simply carries no timestamp")
        check(skipped["registry_bad_timestamp"] == 2,
              f"…and both refusals are counted, per the module's counted-never-silent "
              f"rule: {skipped['registry_bad_timestamp']}")

        scanned = scan(cwd=CWD, root=root, proc_root=proc)
        docs = state_of(all_ops(scanned))["sessions"]
        live = refs.session_key(PID, PROC_START)
        check(len(docs) == 6 and live in docs,
              f"the whole discovery pass completes: {len(docs)} documents")
        check("firstTs" not in docs[live] and "lastTs" not in docs[live],
              "…with no fabricated timestamp on the live document")


def test_a_nul_in_a_path_cannot_kill_the_pass():
    print("test_a_nul_in_a_path_cannot_kill_the_pass")
    # THE THIRD INSTANCE of one bug class, after the alias entry and the absurd
    # timestamp: a value read off disk reaching a stdlib call that raises
    # something the guard does not catch. `os.path.realpath` wraps its `lstat`
    # in `except OSError`, but an embedded NUL raises **ValueError** and escapes
    # — out of `read_registry`/`read_history_sessions`, then `scan`, then
    # `mirror.iter_rebuild_observations`, which calls its sources with no
    # handler. One byte in one line, and every session is lost, not one.
    check(sess._realpath("\x00") == "\x00",
          "a NUL-bearing path resolves to itself instead of raising ValueError")
    check(sess._realpath("/no/such/path/anywhere").startswith("/"),
          "…and an ordinary unresolvable path still answers (ENOENT is not an error here)")

    with tempfile.TemporaryDirectory() as tmp:
        # Arm A — the NUL is in a `history.jsonl` record's `project`.
        root = build_root(tmp)
        proc = fake_stat(tmp)
        history = Path(root, "history.jsonl")
        history.write_text(
            json.dumps({"display": "x", "project": CWD + "\x00", "sessionId": SEVENTH,
                        "timestamp": 1784987605035}) + "\n" + history.read_text())
        scanned = scan(cwd=CWD, root=root, proc_root=proc)
        check(len(scanned.sessions) == len(SIX) + 1,
              f"a NUL in a history `project` costs that line and nothing else: "
              f"{len(scanned.sessions)} sessions")

        # Arm B — the NUL is in the registry entry's own `cwd`.
        entry_path = Path(root, "sessions", "15934.json")
        entry = json.loads(entry_path.read_text())
        entry["cwd"] = CWD + "\x00"
        entry_path.write_text(json.dumps(entry))
        skipped = sess._skips()
        entries = read_registry(root, cwd=CWD, slugs=[SLUG], proc_root=proc, skipped=skipped)
        check(entries == [], "a NUL-bearing registry `cwd` costs that entry")
        check(skipped["registry_unusable"] == 1,
              f"…counted as unusable, which is what it is — not as another "
              f"project's session: {skipped}")
        scanned = scan(cwd=CWD, root=root, proc_root=proc)
        keys = [s.key() for s in scanned.sessions]
        check(len(keys) == len(SIX) + 1 and not any(k.startswith("live:") for k in keys),
              f"…and the pass completes, with that session on the historical arm: {len(keys)}")

        # Arm C — the same tree through the ONLY seam production has. This is
        # the arm that matters: `iter_rebuild_observations` has no `try`.
        saved = {k: os.environ.get(k) for k in ("TOUCH_CLAUDE_ROOT", "TOUCH_PROJECT_CWD")}
        os.environ["TOUCH_CLAUDE_ROOT"] = root
        os.environ["TOUCH_PROJECT_CWD"] = CWD
        try:
            from aggregator import mirror as mr
            wired = list(mr.iter_rebuild_observations(registry_modules=["sessions"]))
        except ValueError as exc:
            wired = []
            check(False, f"the wired rebuild died on a NUL: {exc}")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        check(len(wired) == len(SIX) + 1,
              f"`--rebuild` survives it too, with every other session intact: {len(wired)}")

        # Arm D — an ENAMETOOLONG path, to show the OSError half still works and
        # the fix widened the guard rather than replacing it.
        long_cwd = "/" + "x" * 5000
        check(sess._realpath(long_cwd) == long_cwd or sess._realpath(long_cwd).startswith("/"),
              "an absurdly long path is an OSError, which was already absorbed")


def test_a_source_path_must_be_root_relative():
    print("test_a_source_path_must_be_root_relative")
    # `Source.path` is root-relative BY CONSTRUCTION at the four `scan`-site
    # calls to `_rel()` — but `map_session` accepts the plain dicts a replay or
    # a fixture hands back, so the construction sites are not the boundary. An
    # absolute path stored in `sources[]` makes that document's fingerprint
    # depend on the $HOME it was built in, and GD-25's acceptance test compares
    # fingerprints across passes.
    for label, path in (("absolute", f"/home/someone/.claude/projects/{SLUG}/x.jsonl"),
                        ("escaping the root", "projects/../../etc/passwd"),
                        ("a bare `..` segment", "../x.jsonl"),
                        ("backslash-separated", "projects\\x\\y.jsonl"),
                        ("NUL-bearing", "projects/x\x00.jsonl"),
                        ("empty", "")):
        check(raises(SessionsError, Source(path).as_element),
              f"a {label} source path is refused at the mapping boundary")
        check(raises(SessionsError, map_session,
                     dict(session_id=SIX[0], sources=[dict(path=path)])),
              f"…and so is the same path arriving as a replayed dict ({label})")
    check(Source(f"projects/{SLUG}/{SIX[0]}.jsonl").as_element()["path"]
          == f"projects/{SLUG}/{SIX[0]}.jsonl",
          "…while a root-relative POSIX path passes through untouched")
    check(Source("projects/a..b/x.jsonl").as_element()["path"] == "projects/a..b/x.jsonl",
          "…and `..` INSIDE a segment is a legal directory name, not an escape")


def test_a_rebuild_through_mirror_reproduces_the_scan():
    print("test_a_rebuild_through_mirror_reproduces_the_scan")
    from aggregator import mirror as mr

    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp)
        proc = fake_stat(tmp)
        scanned = scan(cwd=CWD, root=root, proc_root=proc)
        backend = mr.MemoryBackend({})
        mirror = mr.Mirror(mr.MongoConfig("uri-placeholder", "touch_test"),
                           backend=backend, registry=mr.discover_mappers())
        mirror.state = mr.STATE_LIVE
        report = asyncio.run(mirror.rebuild(list(scanned.observations())))
        check(report["unmapped"] == 0 and report["rejected"] == 0,
              f"the mirror maps every observation this module emits: {report['unmappedKinds']}")
        check(report["counts"].get("sessions") == 7,
              f"…into seven session documents: {report['counts']}")

        before = report["fingerprint"]
        backend.state.clear()
        after = asyncio.run(mirror.rebuild(list(scanned.observations())))["fingerprint"]
        check(after == before, "wipe + --rebuild reproduces a byte-identical fingerprint")


def test_claude_root_agrees_with_mirrors():
    print("test_claude_root_agrees_with_mirrors")
    from aggregator import mirror as mr

    env = {"TOUCH_CLAUDE_ROOT": "/somewhere/else"}
    check(claude_root(env) == mr.claude_root(env) == "/somewhere/else",
          "$TOUCH_CLAUDE_ROOT is honoured identically by both copies")
    check(claude_root({}) == mr.claude_root({}),
          f"…and so is the default: {claude_root({})}")
    # Why the four lines are duplicated rather than imported, asserted on the
    # IMPORTS rather than on the file's prose: `mirror` reaches this module only
    # through `importlib` inside `discover_mappers`/`iter_sources`, so a
    # module-scope `from .mirror import claude_root` here would close a cycle.
    # (Grepping mirror.py for the string "sessions" reddens on any future
    # comment that mentions the word.)
    tree = ast.parse((REPO / "aggregator" / "mirror.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[-1] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {alias.name for alias in node.names}
            if node.module:
                imported.add(node.module.split(".")[-1])
    check("sessions" not in imported,
          f"mirror.py never imports `sessions` statically — it discovers it "
          f"(imports: {sorted(imported)})")


def test_history_scope_is_the_project_field_not_a_slug_guess():
    print("test_history_scope_is_the_project_field_not_a_slug_guess")
    with tempfile.TemporaryDirectory() as tmp:
        root = build_root(tmp)
        ids = read_history_sessions(root, CWD)
        check(ids == [SEVENTH, SIX[0]],
              f"only this project's sessionIds, in first-seen order: {ids}")
        check(FOREIGN_IDS[2] not in ids,
              "…and a record whose `project` is another cwd is excluded on that field alone")
        check(read_history_sessions(os.path.join(tmp, "no-such-root"), CWD) == [],
              "a missing history.jsonl is empty, never an error")


# --- live arm (skips cleanly) --------------------------------------------
def live_database():
    """(db, client, name) against `TOUCH_MONGO_URI`, or (None, None, reason).

    Same shape as `tests/test_mongo_store.py`'s: R-42's loopback+auth recipe,
    a database named `touch_test_<pid>` (GD-27 — the only name this file will
    ever drop), and every absence is a clean skip, never a failure.
    """
    uri = os.environ.get("TOUCH_MONGO_URI")
    if not uri:
        return None, None, "TOUCH_MONGO_URI is not set (R-42's loopback+auth recipe)"
    if not ms.pymongo_available():
        return None, None, "pymongo is not installed (GD-21: absence is legal)"
    try:
        client = ms.open_client(uri)
    except ms.MongoUnavailable as exc:
        return None, None, str(exc)
    if not ms.ping(client):
        client.close()
        return None, None, "no mongod answered within the GD-21 timeouts"
    return client[f"touch_test_{os.getpid()}"], client, f"touch_test_{os.getpid()}"


def test_live_mongod_arm():
    print("test_live_mongod_arm")
    # The one claim in `sessions.py` that a memory model CANNOT settle: `sources`
    # is in neither mapper's `$setOnInsert` because mongod refuses
    # `$setOnInsert:{sources:[]}` beside `$addToSet:{sources:…}`, so R-46's
    # "transcriptless seventh ⇒ sources: []" has to come from the empty
    # `$addToSet` instead. Both halves are asserted here against a real server;
    # without one the arm skips and the in-memory tests still stand.
    db, client, name = live_database()
    if db is None:
        skip(f"live Mongo arm: {name}")
        return
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _live_session_checks(db, tmp)
    finally:
        check(name.startswith("touch_test_"),
              f"dropping only the database this test constructed: {name} (GD-27)")
        if name.startswith("touch_test_"):
            client.drop_database(name)
        client.close()


def _live_session_checks(db, tmp):
    import random

    from pymongo.errors import PyMongoError

    ms.ensure_schema(db)
    root = build_root(tmp)
    proc = fake_stat(tmp)
    hist_key = refs.hist_session_key(LIVE_ID)
    scanned = scan(cwd=CWD, root=root, proc_root=proc,
                   prior=Prior(ids=frozenset([hist_key])))
    ops = all_ops(scanned)
    memory = ms.fingerprint(state_of(ops))

    orders = {"normal": ops, "reversed": list(reversed(ops))}
    shuffled = list(ops)
    random.Random(46).shuffle(shuffled)
    orders["shuffled"] = shuffled

    # GD-26's no-delete rule governs the mirror's code, not a test fixture reset
    # — but the reset below must be unable to reach anything but the database
    # this test constructed, so the guard sits immediately above it (GD-27/GD-12).
    check(db.name.startswith("touch_test_"),
          f"the per-collection wipe can only reach the constructed database: {db.name}")
    if not db.name.startswith("touch_test_"):
        return

    seen, first_state = {}, None
    for label, sequence in orders.items():
        db["sessions"].delete_many({})              # fixture reset, not mirror code
        result = ms.bulk_upsert(db, "sessions",
                                [(key, update) for _c, key, update in sequence])
        if result["errors"]:
            check(False, f"{label}: mongod refused an operation: {result['errors'][:1]}")
            return
        state = {"sessions": {doc["_id"]: doc for doc in db["sessions"].find({})}}
        seen[label] = ms.fingerprint(state)
        first_state = first_state or state
    check(len(set(seen.values())) == 1,
          f"normal / shuffled / reversed through a real mongod ⇒ ONE fingerprint "
          f"({', '.join(f'{k}={v[:8]}' for k, v in seen.items())})")
    check(seen["normal"] == memory,
          "…and the in-memory model this module's other tests use agrees with the "
          "server byte for byte (GD-25)")
    check(len(first_state["sessions"]) == len(SIX) + 2,
          f"…over R-46's seven sessions plus the historical twin the promotion "
          f"annotates: {len(first_state['sessions'])}")

    # R-46's transcriptless seventh, read back OFF THE SERVER: the field exists
    # and is empty, which is a different stored fact from a missing field.
    seventh = first_state["sessions"][refs.hist_session_key(SEVENTH)]
    check(seventh.get("sources") == [] and "sources" in seventh,
          f"the empty `$addToSet` really does create `sources: []` on insert: "
          f"{seventh.get('sources', '<missing>')!r}")

    # `hist:<LIVE_ID>` is claimed by the live document, so NO session operation
    # targets it — the promotion is the only writer, and therefore the operation
    # that inserts it. That is the order-dependence GD-25 forbids, exercised
    # against a real server in all three orders above.
    promoted = first_state["sessions"][hist_key]
    check(promoted.get("promotedTo") == refs.session_key(PID, PROC_START)
          and promoted["_id"] == hist_key,
          "…and the promoted historical document keeps its `_id` and gains the pointer")
    check(promoted.get("sources") == [] and promoted.get("provenance") == PROVENANCE
          and promoted.get("class") == DEFAULT_CLASS,
          f"…even though a PROMOTION inserted it, it is a complete, valid session "
          f"row with the field present: {promoted.get('sources', '<missing>')!r}")
    try:
        ms.validate_document("sessions", promoted)
        check(True, "…and it validates against GD-24's row read back off the server")
    except Exception as exc:                             # noqa: BLE001
        check(False, f"the promotion-inserted document is invalid: "
                     f"{type(exc).__name__}: {exc}")

    # The other half of the rationale: the shape the mappers DO NOT emit is one
    # a real server rejects outright. If this ever starts passing, `sources`
    # could live in the immutables and the comment in `sessions.py` is stale.
    conflict_key = refs.hist_session_key(FOREIGN_IDS[0])
    try:
        db["sessions"].update_one(
            {"_id": conflict_key},
            {"$setOnInsert": {"sources": [], "class": DEFAULT_CLASS,
                              "provenance": PROVENANCE, "sessionId": FOREIGN_IDS[0]},
             "$addToSet": {"sources": {"$each": [
                 {"path": "projects/x/y.jsonl", "kind": "transcript", "present": True}]}}},
            upsert=True)
        check(False, "mongod ACCEPTED `$setOnInsert:{sources:[]}` beside "
                     "`$addToSet:{sources:…}` — the rationale in sessions.py is stale")
    except PyMongoError as exc:
        check("conflict" in str(exc).lower(),
              f"mongod refuses `$setOnInsert:{{sources:[]}}` beside "
              f"`$addToSet:{{sources:…}}` — which is WHY `sources` is in neither "
              f"mapper's immutables: {str(exc).splitlines()[0][:80]}")
        check(db["sessions"].find_one({"_id": conflict_key}) is None,
              "…and the refused operation left no half-built document behind")


def main():
    for t in (test_slug_rule_reproduces_the_directory_names_on_disk,
              test_proc_start_is_field_22_even_when_comm_has_spaces_and_parens,
              test_registry_tolerates_lost_found_and_zero_byte_files,
              test_a_reused_pid_is_not_a_live_session,
              test_a_registry_entry_for_another_project_is_out_of_scope,
              test_the_project_dir_yields_six_documents_exactly_one_live,
              test_the_four_foreign_slug_dirs_are_not_ingested,
              test_the_transcriptless_seventh_session_is_sources_empty,
              test_promotion_annotates_the_hist_doc_and_rewrites_no_id,
              test_two_session_ids_on_one_live_id_merge_via_add_to_set,
              test_a_promotion_is_inert_on_the_wired_path,
              test_session_aliases_widen_scope_in_both_plausible_formats,
              test_alias_closure_is_cycle_safe_and_bounded,
              test_a_disappeared_source_is_a_field_not_a_removal,
              test_source_elements_have_a_pinned_field_order,
              test_mappers_are_registered_pure_and_write_only_sessions,
              test_the_algebra_is_order_independent,
              test_class_and_provenance_are_immutables_not_verdicts,
              test_the_union_is_gd24s_and_refs_owns_both_grammars,
              test_mirror_sources_answer_only_for_paths_they_own,
              test_backfill_observations_carry_no_timestamp,
              test_an_absurd_registry_timestamp_cannot_kill_the_pass,
              test_a_nul_in_a_path_cannot_kill_the_pass,
              test_a_source_path_must_be_root_relative,
              test_the_per_path_seam_does_not_reread_the_alias_closure,
              test_a_rebuild_through_mirror_reproduces_the_scan,
              test_claude_root_agrees_with_mirrors,
              test_history_scope_is_the_project_field_not_a_slug_guess,
              test_live_mongod_arm):
        t()
    print()
    for message in skips:
        print(f"skipped: {message}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all sessions tests passed")


if __name__ == "__main__":
    main()
