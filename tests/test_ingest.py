#!/usr/bin/env python3
"""Stdlib-only tests for aggregator/ingest.py (R-26 as amended, R-47, R-49). Run
as `python3 test_ingest.py`; exits non-zero on failure. No pytest, no runner.

Token accounting has its own file (`test_usage.py`, R-50); everything else about
the ingest is here. The three items' own test lists, honoured one function each
where they are one claim and several where they are several:

* **R-26** — "the two `workflow_phase` null rows are ignored and all seven labels
  survive; the persisted-output regex fires on the real spill files; snapshot
  found despite cross-session split; both `result` arms; run rollup ≈ the deduped
  figure, not 1 089 990" (the last is `test_usage.py`'s);
* **R-47** — "ingest the fixture twice ⇒ identical counts both times; all `mode`
  occurrences present positionally; uuid coverage assertion (uuid-bearing count
  in `records`, uuid-less count in `stream_meta` — nothing collapsed)";
* **R-49** — "`wf_455b348c-e17` ⇒ 9 nodes across 6 keys with ordinals
  0/0/0/0/0/0,1,1,1; the killed run's resultless nodes render unknown/stale, never
  running; live-run fixture (no snapshot) ⇒ run doc exists, no error; snapshot
  arrival back-fills without clobbering; the taskId join resolves".

Everything reads the **frozen** corpus (`tests/fixtures/`, sp-02) — never
`~/.claude` — through a temporary `~/.claude`-shaped root whose `projects/`
entries are *symlinks* to the frozen directories. Symlinks rather than copies
because the corpus is 8 MB and read-only in both senses: no test here writes into
it, and `tests/test_fixtures.py` would fail if one did.

**Two acknowledged gaps, stated rather than papered over.**

1. sp-02 froze the run's session *subdirectories*, not the top-level session
   transcripts, so no fixture contains a launch `toolUseResult` record — the only
   deterministic main-session→run join (R-49/CONVO-12). Its test builds the
   record from the shape recorded in `ingest.read_launch`'s docstring
   (`w4hiywrt6` / `wf_930e210a-6da`, verbatim from `292fc08c…jsonl:57`), which
   proves the parser and does **not** prove the shape. A future fixture freeze
   should add one line.
2. R-47's "the 267-line fixture" and "all 17 `mode` occurrences" name a top-level
   session transcript that is likewise not frozen. The equivalent claim is made
   against the transcripts that *are* frozen — `8084340e…jsonl` carries `mode`,
   `permission-mode`, `ai-title`, `last-prompt`, `file-history-snapshot` and
   `system` in 13 lines — and the counts are computed from the bytes rather than
   hard-coded, so the assertion is the invariant and not the specimen.
"""

import ast
import json
import os
import random
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from aggregator import ingest                            # noqa: E402
from aggregator import mirror as mr                      # noqa: E402
from aggregator import mongo_store as ms                 # noqa: E402
from aggregator import refs                              # noqa: E402
from aggregator import sessions as sess                  # noqa: E402
from aggregator.ingest import (                          # noqa: E402
    COLLECTIONS,
    MIRROR_MAPPERS,
    MIRROR_SOURCES,
    NO_RENDER_TYPES,
    PROVENANCE,
    RECORD_TYPES,
    IngestError,
    bucket_of,
    find_persisted_output,
    find_run_dirs,
    find_snapshot,
    is_journal_path,
    is_transcript_path,
    link_spills,
    map_record,
    map_run,
    map_run_node,
    map_stream_meta,
    map_usage,
    read_journal,
    read_launch,
    read_run,
    read_snapshot,
    read_transcript,
    reset_read_cache,
    run_id_for_path,
    scan_tool_results,
    session_id_for_path,
    spill_containment,
)

FIX = REPO / "tests" / "fixtures"
RUN = FIX / "run-wf_829e6f58"
DD = "dd469822-2546-47d9-aaa3-31db4cb705e8"
E4 = "e423cd3c-f859-45af-9afd-0d6bdec9b4ac"
RUN_ID = "wf_829e6f58-b2f"
KILLED = FIX / "mirror" / "wf_455b348c-e17"
LIVE = FIX / "mirror" / "live-run-shape" / "a8d43bb1-0313-45d4-8784-4827af443ead"
LIVE_RUN = "wf_b297177a-d11"
DISCOVERY = FIX / "mirror" / "discovery" / "projects"
VARIETY = (DISCOVERY / "-tmp-claude-1000-liveio"
           / "8084340e-a56b-499f-b54d-cec64e52da78.jsonl")
RECORDS = FIX / "mirror" / "records"

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


def note(msg):
    """A remark about coverage that is NOT a skip.

    Distinct from :func:`skip` on purpose: a footer that says "skipped" for an
    arm whose eleven assertions all ran misreports coverage in the one direction
    that matters. `skip()` means *nothing below it executed*.
    """
    print(f"  note: {msg}")


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:                                            # noqa: BLE001
        return False
    return False


# --- helpers --------------------------------------------------------------


def linked_root(tmp, *pairs):
    """A `~/.claude`-shaped root whose project slugs symlink into the corpus."""
    root = os.path.join(tmp, "claude")
    os.makedirs(os.path.join(root, "projects"), exist_ok=True)
    for slug, target in pairs:
        os.symlink(os.fspath(target), os.path.join(root, "projects", slug))
    sess.reset_scope_cache()
    return root


def _mirror_tree(src, dst):
    """Recreate `src`'s directory tree at `dst` with the FILES symlinked.

    `linked_root`'s slug-level symlink is invisible to `os.walk`, which does not
    follow directory links — and `mirror.iter_backfill_sources` walks. So the
    backfill arm needs real directories; the 8 MB of file bytes stay shared.
    """
    os.makedirs(dst, exist_ok=True)
    for name in sorted(os.listdir(src)):
        source = os.path.join(src, name)
        target = os.path.join(dst, name)
        if os.path.isdir(source):
            _mirror_tree(source, target)
        else:
            os.symlink(source, target)


def walkable_root(tmp, *pairs):
    """`linked_root`, but with directories `os.walk` will actually descend into."""
    root = os.path.join(tmp, "claude")
    os.makedirs(os.path.join(root, "projects"), exist_ok=True)
    for slug, target in pairs:
        _mirror_tree(os.fspath(target), os.path.join(root, "projects", slug))
    sess.reset_scope_cache()
    return root


#: The cwd every rooted test claims to run in, and the slug the CLI would give
#: it. The per-path (`--backfill`) ownership test is `sessions.scoped_dirs`, so a
#: test that hands a source a path must put that path under the slug of the cwd
#: it passes — exactly as the real thing does.
OWNED_CWD = "/home/laniakea/Projects/touch"
OWNED_SLUG = sess.slug_for(OWNED_CWD)
FOREIGN_SLUG = "-tmp-claude-1000-liveio"


#: The `/clear`-mid-run shape the FROZEN corpus cannot express, built by
#: :func:`clear_split_root`. `run-wf_829e6f58/` does hold two session
#: directories, but no `message.id` is shared between them (checked: 0 of its
#: 328), so the acceptance property below would pass on it no matter what
#: operator `map_usage` chose. The fixtures are sp-02's and frozen, so the
#: adversarial shape is constructed in the test rather than added to them.
SPLIT_RUN = "wf_5ea70b12-c1e"
SPLIT_AGENT = "a" + "9" * 16
SPLIT_SESSIONS = ("0b6c1c2a-0000-4000-8000-00000000aaaa",
                  "1c7d2d3b-1111-4111-9111-11111111bbbb")
SPLIT_MESSAGE = "msg_01SpansTwoSessions"


def assistant_line(uuid, message_id, out, ts, agent_id=SPLIT_AGENT):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "agentId": agent_id, "timestamp": ts,
        "message": {"id": message_id, "role": "assistant",
                    "usage": {"input_tokens": 11, "output_tokens": out,
                              "cache_read_input_tokens": 3,
                              "cache_creation_input_tokens": 0}}})


def clear_split_root(tmp):
    """One agent's fragments under TWO session directories of one run.

    MONGOSCHEMA-9's topology: a `/clear` gives the process a new sessionId
    mid-run, so the *same* `agent-<id>.jsonl` continues under a second session
    directory — and a `message.id` split across the boundary is observed under
    two `sessionId`s. Three such ids exist on this machine's live corpus. The
    slug is the same in both, because a `/clear` changes the session and never
    the cwd the slug is derived from.
    """
    root = os.path.join(tmp, "claude")
    slug = os.path.join(root, "projects", OWNED_SLUG)
    counter = 0
    for index, session in enumerate(SPLIT_SESSIONS):
        directory = os.path.join(slug, session, "subagents", "workflows", SPLIT_RUN)
        os.makedirs(directory)
        lines = []
        for out, message in ((10 + 80 * index, SPLIT_MESSAGE),
                             (7, f"msg_only_in_fragment_{index}")):
            counter += 1
            lines.append(assistant_line(
                f"081b28a7-aee9-43dc-935d-{counter:012x}", message, out,
                f"2026-07-25T0{3 + index}:2{counter}:00.000Z"))
        Path(os.path.join(directory, f"agent-{SPLIT_AGENT}.jsonl")).write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
    sess.reset_scope_cache()
    return root


def raw_lines(path):
    """The parsed JSON objects of a `.jsonl`, straight from the bytes."""
    out = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def state_of(observations, registry=None):
    """Run observations through the real mapper registry into a memory state."""
    registry = registry or mr.discover_mappers(["ingest"])
    ops = []
    for kind, obs in observations:
        ops.extend(mr.map_observation(registry, kind, obs))
    return ms.apply_operations({}, ops), ops


def transcripts_of(directory):
    out = []
    for base, dirnames, filenames in os.walk(directory):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(base, name)
            if is_transcript_path(path):
                out.append(path)
    return out


# --- R-47: the bucket table ----------------------------------------------


def test_the_bucket_table_is_the_only_decider():
    print("test_the_bucket_table_is_the_only_decider")
    uuid = "081b28a7-aee9-43dc-935d-1586407f232e"
    for kind in RECORD_TYPES:
        check(bucket_of({"type": kind, "uuid": uuid}) == "records",
              f"{kind} with a uuid ⇒ records (R-47's uuid-bearing set)")
    for kind in ("mode", "permission-mode", "ai-title", "last-prompt",
                 "queue-operation", "file-history-snapshot", "file-history-delta",
                 "frame-link"):
        check(bucket_of({"type": kind, "uuid": uuid}) == "stream_meta",
              f"{kind} ⇒ stream_meta even carrying a uuid — the table is by TYPE")
    check(bucket_of({"type": "a-type-the-cli-has-not-invented-yet"}) == "stream_meta",
          "an unknown/future type ⇒ stream_meta positionally, never dropped (R-47)")
    check(bucket_of({"type": "user"}) == "stream_meta",
          "a user record with NO uuid cannot be uuid-keyed, so it goes positionally "
          "— the alternative is dropping it, which GD-26 forbids")
    check(bucket_of({"type": "user", "uuid": "081B28A7-AEEE-43DC-935D-1586407F232E"})
          == "stream_meta",
          "…and an UPPERCASE uuid is not a uuid refs will key: two spellings of one "
          "identity would key two documents (GD-24)")
    check(bucket_of("not a dict") == "stream_meta", "a non-object line is never a record")


def test_the_frozen_corpus_buckets_without_collapse():
    print("test_the_frozen_corpus_buckets_without_collapse")
    # The uuid coverage assertion R-47 asks for, computed from the BYTES and then
    # compared with the ingest — the shape that catches a silent collapse (a
    # content-hash key lost 142 of 333 records in MONGOSCHEMA-1's probe and every
    # count still looked plausible).
    expected_records = expected_meta = 0
    observations = []
    for path in sorted(transcripts_of(DISCOVERY)) + sorted(transcripts_of(RUN)):
        for record in raw_lines(path):
            if bucket_of(record) == "records":
                expected_records += 1
            else:
                expected_meta += 1
        scan = read_transcript(path, root=FIX)
        observations.extend(scan.observations())

    state, _ops = state_of(observations)
    counts = ms.counts(state)
    check(counts.get("records") == expected_records,
          f"every uuid-bearing record is in `records`: {counts.get('records')} "
          f"== {expected_records} counted from the bytes")
    check(counts.get("stream_meta") == expected_meta,
          f"every uuid-less line is in `stream_meta`: {counts.get('stream_meta')} "
          f"== {expected_meta}")
    check(expected_records > 1000 and expected_meta > 20,
          f"…and both buckets are non-trivial ({expected_records} / {expected_meta})")

    # "ingest the fixture twice ⇒ identical counts both times" (R-47).
    again, _ = state_of(observations)
    check(ms.counts(again) == counts, "a second identical pass changes no count")
    check(ms.fingerprint(again) == ms.fingerprint(state),
          "…and no byte: the upsert is idempotent, which is what 'deterministic "
          "persistence' means (GD-25)")


def test_every_uuidless_type_survives_positionally():
    print("test_every_uuidless_type_survives_positionally")
    scan = read_transcript(VARIETY, root=FIX)
    records = raw_lines(VARIETY)
    by_type = {}
    for record in records:
        if bucket_of(record) != "records":
            by_type[record.get("type")] = by_type.get(record.get("type"), 0) + 1
    seen = {}
    for obs in scan.stream_meta:
        seen[obs.type] = seen.get(obs.type, 0) + 1
    check(seen == by_type,
          f"every uuid-less type survives with its own count: {sorted(seen.items())}")
    for kind in ("mode", "permission-mode", "ai-title", "last-prompt",
                 "file-history-snapshot"):
        check(kind in seen, f"…including `{kind}`")

    # Positional means positional: the `_id` names the physical line, and the
    # line numbers are the file's, not the bucket's.
    lines = [obs.line_no for obs in scan.stream_meta]
    check(lines == sorted(lines) and len(set(lines)) == len(lines),
          "line numbers are unique and ascending — a positional key that repeated "
          "would alias two lines onto one document")
    state, _ = state_of(scan.observations())
    session = session_id_for_path(VARIETY)
    for obs in scan.stream_meta:
        key = refs.stream_meta_key(session, obs.line_no)
        if key not in state["stream_meta"]:
            check(False, f"missing positional document {key}")
            return
    check(True, "…and each one is stored under `<sessionId>#<line:08d>` (GD-24)")


def test_queue_operation_is_render_false_and_never_deduped():
    print("test_queue_operation_is_render_false_and_never_deduped")
    session = "292fc08c-923d-4ab4-8ff2-a9572417dbc8"
    path = RECORDS / "queue-operation-user-pair.jsonl"
    scan = read_transcript(path, session_id=session, root=FIX)
    state, _ = state_of(scan.observations())
    check(ms.counts(state) == {"records": 1, "stream_meta": 1},
          "the pair stays TWO documents — enqueue and delivery are different "
          "events, and their 70 ms gap is the only observable queue latency")
    meta = next(iter(state["stream_meta"].values()))
    check(meta["type"] == "queue-operation" and meta["render"] is False,
          "the queue-operation carries render:false (R-47)")
    record = next(iter(state["records"].values()))
    check(record["type"] == "user" and record["_id"] != meta["_id"],
          "…while its `user` twin is an ordinary uuid-keyed record")
    check(NO_RENDER_TYPES == ("queue-operation",),
          "and it is the only type marked render:false")


def test_dotted_snapshot_records_bucket_positionally_and_wrap():
    print("test_dotted_snapshot_records_bucket_positionally_and_wrap")
    session = "292fc08c-923d-4ab4-8ff2-a9572417dbc8"
    scan = read_transcript(RECORDS / "file-history-snapshot-dotted.jsonl",
                           session_id=session, root=FIX)
    check(len(scan.stream_meta) == 33 and not scan.records,
          f"all 33 dotted-key specimens are stream_meta (got {len(scan.stream_meta)})")
    state, _ = state_of(scan.observations())
    doc = state["stream_meta"][refs.stream_meta_key(session, 1)]
    wrapper = doc["body"]["snapshot"]["trackedFileBackups"]
    check(ms.is_raw_wrapper(wrapper),
          "the variable-key subtree is `_raw`-wrapped by prepare_document "
          "(R-44/MONGOSCHEMA-8) — dotted keys store but are not addressable")
    original = raw_lines(RECORDS / "file-history-snapshot-dotted.jsonl")[0]
    check(ms.unwrap_raw(wrapper) == original["snapshot"]["trackedFileBackups"],
          "…and round-trips byte-identically, so nothing is lost to the wrapper")
    check(doc.get("messageId") == original["messageId"],
          "`file-history-snapshot.messageId` is stored as the join field (R-47) — "
          "it equals the uuid of the snapshotted record")


def test_the_session_id_is_injected_from_the_path_and_says_so():
    print("test_the_session_id_is_injected_from_the_path_and_says_so")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "0b6c1c2a-0000-4000-8000-00000000abcd.jsonl")
        Path(path).write_text(
            json.dumps({"type": "user", "uuid": "081b28a7-aee9-43dc-935d-1586407f232e",
                        "session_id": "snake", "timestamp": "2026-07-25T03:20:00.000Z"})
            + "\n", encoding="utf-8")
        scan = read_transcript(path)
        obs = scan.records[0]
        check(obs.session_id == "0b6c1c2a-0000-4000-8000-00000000abcd",
              "a record with no `sessionId` takes the one its path names (R-47)")
        check(obs.session_id_source == "path", "…and records WHICH source it was")
        check(obs.dropped_keys == ("session_id",),
              "…and that the snake-case duplicate was dropped (SESSIONJSONL-16)")
        doc = ms.apply_operations({}, map_record(obs))["records"][obs.uuid]
        check(doc["_normalized"] == {"sessionIdSource": "path",
                                     "dropped": ["session_id"]},
              "…both of them on the stored document, not only in memory")
        check("session_id" not in doc and doc["body"]["session_id"] == "snake",
              "the duplicate is normalized off the document while the body keeps "
              "the source's own bytes")


def test_a_positional_key_belongs_to_the_file_it_numbers():
    print("test_a_positional_key_belongs_to_the_file_it_numbers")
    # `stream_meta._id` is `<sessionId>#<lineNo:08d>` (GD-24), so the session
    # component decides WHOSE line 5 this is. Two independent ways to get that
    # wrong, and the module refuses both rather than writing an aliasing key.
    session = "0b6c1c2a-0000-4000-8000-00000000abcd"
    other = "0b6c1c2a-0000-4000-8000-00000000ffff"
    line = json.dumps({"type": "mode", "mode": "plan", "sessionId": other,
                       "timestamp": "2026-07-25T03:20:00.000Z"}) + "\n"

    with tempfile.TemporaryDirectory() as tmp:
        # (1) The record's own claim never wins for a positional key.
        own = os.path.join(tmp, f"{session}.jsonl")
        Path(own).write_text(line, encoding="utf-8")
        scan = read_transcript(own)
        obs = scan.stream_meta[0]
        check(obs.session_id == session,
              "a uuid-less line is keyed by the session its PATH names, even when "
              "the line claims another — line 1 is a position in THIS file")
        check(obs.claimed_session_id == other,
              "…and the overruled claim is kept, not silently discarded")
        doc = ms.apply_operations({}, map_stream_meta(obs))["stream_meta"][
            refs.stream_meta_key(session, 1)]
        check(doc["_id"].startswith(session) and doc["sessionId"] == session,
              f"…so the stored _id is this file's ({doc['_id']})")
        check(doc["_normalized"] == {"sessionIdSource": "path",
                                     "claimedSessionId": other},
              "…with the disagreement auditable on the document itself, which is "
              "the only place a reader could ever find it")

        # (2) Several files share one session, so line 5 is ambiguous unless the
        # file IS the session's transcript. An agent transcript's uuid-less line
        # is reported, never mirrored: an aliasing key destroys the other file's
        # document and neither is recoverable.
        deep = os.path.join(tmp, session, "subagents", "workflows", "wf_x")
        os.makedirs(deep)
        agent = os.path.join(deep, "agent-a2ec106948f58d0c8.jsonl")
        Path(agent).write_text(line, encoding="utf-8")
        shadow = read_transcript(agent)
        check(session_id_for_path(agent) == session,
              "the agent transcript resolves to the SAME session as the file above")
        check(not shadow.stream_meta and shadow.skipped["unkeyable_positional"] == 1,
              "…so its uuid-less line is not mirrored — it would key that file's "
              "line 1, and one of the two documents would vanish")
        report = shadow.unkeyable[0]
        check(report.line_no == 1 and report.type == "mode" and "alias" in report.reason,
              "…it is REPORTED instead, with the reason: a counted gap beats a "
              "silent wrong-target write (GD-12) and beats dropping it (GD-26)")

        # (3) Naming the stream explicitly is the caller taking responsibility
        # for the numbering — the fixture/replay case.
        named = read_transcript(agent, session_id=session)
        check(len(named.stream_meta) == 1
              and named.skipped["unkeyable_positional"] == 0,
              "an explicit session_id= names the stream, so the line is keyed")

    # And the whole thing costs nothing on the real corpus: every record in every
    # frozen agent transcript is uuid-bearing, so the counter is 0 everywhere.
    stranded = {}
    for path in sorted(transcripts_of(DISCOVERY)) + sorted(transcripts_of(RUN)):
        count = read_transcript(path, root=FIX).skipped["unkeyable_positional"]
        if count:
            stranded[path] = count
    check(not stranded,
          f"and no line of the frozen corpus is stranded by the rule: {stranded}")


def test_positions_are_stored_on_every_document():
    print("test_positions_are_stored_on_every_document")
    path = RUN / DD / "subagents" / "workflows" / RUN_ID / "agent-a2ec106948f58d0c8.jsonl"
    scan = read_transcript(path, root=FIX)
    state, _ = state_of(scan.observations())
    raw = Path(path).read_bytes()
    bad = []
    for obs in scan.records:
        doc = state["records"][obs.uuid]
        if doc.get("lineNo") != obs.line_no or doc.get("byteOffset") != obs.byte_offset:
            bad.append(obs.uuid)
        elif not raw[obs.byte_offset:].startswith(b"{"):
            bad.append(obs.uuid)
    check(not bad, f"every record carries lineNo + byteOffset, and the offset points "
                   f"at the line's first byte ({len(scan.records)} checked)")
    check(scan.records[0].line_no == 1 and scan.records[0].byte_offset == 0,
          "line numbers are 1-based physical lines, offsets absolute (tailer.py)")


def test_an_unparsable_line_is_stored_not_dropped():
    print("test_an_unparsable_line_is_stored_not_dropped")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "0b6c1c2a-0000-4000-8000-00000000abcd.jsonl")
        Path(path).write_text('{"type":"user","uuid":"081b28a7-aee9-43dc-935d-1586407f232e"}\n'
                              '{not json at all\n'
                              '"a bare string"\n', encoding="utf-8")
        scan = read_transcript(path)
        check(scan.skipped["unparsable"] == 2, "both bad lines are COUNTED")
        metas = {obs.line_no: obs for obs in scan.stream_meta}
        check(set(metas) == {2, 3},
              "…and both are stored positionally rather than dropped (GD-26)")
        check(all(obs.type == ingest.UNPARSED_TYPE and obs.parse_error
                  for obs in metas.values()),
              "…with the parse error, so a run of bad lines is a visible fact")
        state, _ = state_of(scan.observations())
        check(len(state["stream_meta"]) == 2, "and both reach the mirror")

        # No session anywhere: a positional key cannot be invented, so the line
        # is counted twice and stored nowhere. That is the only honest answer.
        other = os.path.join(tmp, "not-a-session-name.jsonl")
        Path(other).write_text("{oops\n", encoding="utf-8")
        blind = read_transcript(other)
        check(blind.skipped["unparsable"] == 1 and blind.skipped["no_session_id"] == 1,
              "an unkeyable line is counted as both unparsable and unkeyable")
        check(not blind.stream_meta,
              "…and stored nowhere: a fabricated sessionId is a wrong-target write (GD-12)")


def test_the_oversize_line_is_stored_whole():
    print("test_the_oversize_line_is_stored_whole")
    session = "292fc08c-923d-4ab4-8ff2-a9572417dbc8"
    scan = read_transcript(RECORDS / "oversize-line.jsonl", session_id=session, root=FIX)
    state, _ = state_of(scan.observations())
    doc = next(iter(state["records"].values()))
    check("oversize" not in doc and ms.document_size(doc) > 800_000,
          f"the real 877 KB line is stored whole ({ms.document_size(doc)} bytes) — "
          f"5 % of the BSON limit, so the guard does not fire")
    check(doc["body"]["uuid"] == doc["_id"], "…with its body intact")


# --- R-26: spills, launches, containment ---------------------------------


def test_the_persisted_output_regex_fires_on_the_real_spills():
    print("test_the_persisted_output_regex_fires_on_the_real_spills")
    real_root = sess.claude_root()
    session = os.path.basename(LIVE)
    found = []
    for path in sorted(transcripts_of(LIVE)):
        for record in raw_lines(path):
            pointer = find_persisted_output(record, root=real_root,
                                            session_id=session)
            if pointer is not None:
                found.append(pointer)
    check(found, f"the regex fires on the frozen spill pointers ({len(found)} found)")
    check(all(p.tool_use_id and p.basename.endswith(".txt") for p in found),
          "…and each names its tool_use_id and its spill file")

    # The regression guard for the containment tightening: the frozen pointers
    # are verbatim copies of paths under THIS machine's `~/.claude`, so a
    # correct realpath-containment must still say True for all of them. If the
    # corpus is ever replayed somewhere those paths do not belong, the arm is
    # honestly not runnable rather than quietly weakened.
    if found and all(p.path.startswith(real_root + os.sep) for p in found):
        check(all(p.contained for p in found),
              f"…and every real pointer resolves INSIDE {real_root}/projects/*/*/"
              f"tool-results/ (R-26's realpath containment, the shape unchanged)")
    else:
        skip(f"containment arm: the frozen pointers do not name {real_root} on this "
             f"machine, so there is nothing to contain them")

    # `toolUseResult.persistedOutputPath` has zero occurrences on disk (R-26):
    # the pointer is agent-authored text, so the match is ANCHORED.
    quoted = {"type": "user", "uuid": "081b28a7-aee9-43dc-935d-1586407f232e",
              "message": {"role": "user", "content": [
                  {"type": "tool_result", "tool_use_id": "toolu_x",
                   "content": "see the docs: <persisted-output> Full output saved to: /x/y"}]}}
    check(find_persisted_output(quoted) is None,
          "a quoted marker mid-string does NOT fire — 12 false-positive files exist")


def test_containment_is_rooted_and_resolved_not_a_directory_name():
    print("test_containment_is_rooted_and_resolved_not_a_directory_name")
    # R-26: "realpath-contain under ~/.claude/projects/*/*/tool-results/ only".
    # The boolean is PERSISTED beside the agent-authored path, so the first
    # consumer that trusts the pair inherits whatever this predicate lets past.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "claude")
        session = "a8d43bb1-0313-45d4-8784-4827af443ead"
        spills = os.path.join(root, "projects", "-p", session, "tool-results")
        os.makedirs(spills)
        good = os.path.join(spills, "bkw8r4iwk.txt")
        Path(good).write_text("spilled output", encoding="utf-8")
        check(spill_containment(good, root=root) is True,
              "the real shape — <root>/projects/<slug>/<session>/tool-results/<file> "
              "— is contained")
        check(spill_containment(good) is False,
              "…and with NO root there is nothing to be contained BY, so the answer "
              "is False and counted: 'unknown' must not read as 'contained'")

        escape = os.path.join(spills, "escape.txt")
        os.symlink("/etc/passwd", escape)
        check(spill_containment(escape, root=root) is False,
              "a symlink out of the tree is refused — the rule is realpath, not a "
              "lexical prefix, and only resolution can see this")

        evil = os.path.join(tmp, "evil", "tool-results", "passwd.txt")
        os.makedirs(os.path.dirname(evil))
        check(spill_containment(evil, root=root) is False,
              "a same-named tool-results/ OUTSIDE the root is refused (the parent-"
              "directory-name test said True for exactly this)")
        check(spill_containment("tool-results/x.txt", root=root) is False,
              "…and so is a bare relative pointer, which that test also accepted")
        check(spill_containment(os.path.join(root, "projects", "tool-results", "x.txt"),
                                root=root) is False,
              "…and a tool-results/ at the wrong depth INSIDE the root: the shape is "
              "projects/*/*/tool-results/, five components, not 'somewhere below'")
        check(spill_containment(os.path.join(spills, "..", "..", "..", "..", "..",
                                             "etc", "passwd"), root=root) is False,
              "`..` is normalized and resolved away before deciding (untrusted text)")

        link = os.path.join(tmp, "link-to-claude")
        os.symlink(root, link)
        check(spill_containment(os.path.join(link, "projects", "-p", session,
                                             "tool-results", "bkw8r4iwk.txt"),
                                root=link) is True,
              "…while BOTH sides are resolved, so a symlinked root still contains "
              "its own files (a resolved path against an unresolved root would not)")

        # And the counter the caller keeps: unrooted is its own fact, not a
        # corpus that suddenly looks full of escaping pointers.
        record = {"type": "user", "uuid": "081b28a7-aee9-43dc-935d-1586407f232e",
                  "timestamp": "2026-07-25T03:20:00.000Z",
                  "message": {"role": "user", "content": [
                      {"type": "tool_result", "tool_use_id": "toolu_x",
                       "content": "<persisted-output>\nOutput too large (32KB). "
                                  "Full output saved to: " + good}]}}
        transcript = os.path.join(root, "projects", "-p", f"{session}.jsonl")
        Path(transcript).write_text(json.dumps(record) + "\n", encoding="utf-8")
        rooted = read_transcript(transcript, root=root)
        check(rooted.records[0].spill.contained is True
              and rooted.skipped["uncontained_spill"] == 0
              and rooted.skipped["unrooted_spill"] == 0,
              "read_transcript threads its root through, so a real pointer under it "
              "is contained and nothing is counted")
        blind = read_transcript(transcript)
        check(blind.records[0].spill.contained is False
              and blind.skipped["unrooted_spill"] == 1
              and blind.skipped["uncontained_spill"] == 0,
              "…and the same read with no root counts `unrooted_spill`, which is a "
              "different fact from a pointer that escaped")
        doc = ms.apply_operations({}, map_record(blind.records[0]))["records"][
            blind.records[0].uuid]
        check(doc["persistedOutput"]["contained"] is False
              and doc["persistedOutput"]["path"] == good,
              "…and the path is still stored verbatim: refusing containment is a "
              "label on the document, never a drop (GD-26)")


def test_the_tool_results_scan_surfaces_unlinked_spills():
    print("test_the_tool_results_scan_surfaces_unlinked_spills")
    spills = scan_tool_results(LIVE)
    check(len(spills) == 1 and spills[0].basename == "bkw8r4iwk.txt",
          f"the directory scan finds the session's spill files ({len(spills)})")
    check(spills[0].session_id == os.path.basename(LIVE) and spills[0].bytes > 0,
          "…keyed (sessionId, basename), with the file's real size")

    session = os.path.basename(LIVE)
    pointers = [find_persisted_output(record, root=sess.claude_root(),
                                      session_id=session)
                for path in sorted(transcripts_of(LIVE)) for record in raw_lines(path)]
    pointers = [p for p in pointers if p]
    linked = link_spills(spills, pointers)
    check(linked[0].linked_tool_use_id is not None,
          "a spill whose pointer is in the frozen set is linked to its toolUseId")

    orphan = link_spills(spills, [])
    check(orphan[0].linked_tool_use_id is None,
          "…and one with no pointer stays `linkedToolUseId: None` — 'unlinked "
          "spilled output' is a state to render, not an error (R-26)")

    # SESSIONJSONL-14's key is `(sessionId, basename)`, and it has to BE the key:
    # spill basenames are 9-char random ids drawn per session, so a basename-only
    # map lets one session's pointer claim another session's file the first time
    # two collide. Same basename, different session ⇒ no link.
    other = ingest.SpillPointer(tool_use_id="toolu_other",
                                path=f"/x/{spills[0].basename}",
                                basename=spills[0].basename, contained=True,
                                session_id="ffffffff-0000-4000-8000-000000000000")
    crossed = link_spills(spills, [other])
    check(crossed[0].linked_tool_use_id is None,
          "a pointer from ANOTHER session with the same basename does not link — "
          "the key is (sessionId, basename), not basename")
    check(link_spills(spills, [ingest.SpillPointer(
        tool_use_id="toolu_nosession", path=f"/x/{spills[0].basename}",
        basename=spills[0].basename, contained=True)])[0].linked_tool_use_id is None,
        "…and a pointer with no session links nothing rather than everything: the "
        "join is a fact or it is absent")

    check(scan_tool_results(os.path.join(os.fspath(LIVE), "nope")) == (),
          "an absent tool-results/ is no spills, never an exception (poll loop)")


def test_the_launch_tool_use_result_is_the_taskid_join():
    print("test_the_launch_tool_use_result_is_the_taskid_join")
    # Verbatim shape from `292fc08c…jsonl:57`. No FIXTURE carries it (sp-02 froze
    # the run's subdirectories, not the top-level session transcripts), so this
    # proves the parser and is explicit that it does not prove the shape. It is a
    # `note`, not a `skip`: every assertion below runs and is green, and a footer
    # that called this arm skipped would misreport coverage.
    note("no frozen fixture carries a launch toolUseResult — shape taken from "
         "292fc08c…jsonl:57 (w4hiywrt6 / wf_930e210a-6da), verbatim in read_launch; "
         "the assertions below all run against that shape")
    record = {
        "type": "user", "uuid": "f8e32e35-e4ff-42df-8ddc-ed88a48a59de",
        "sessionId": "292fc08c-923d-4ab4-8ff2-a9572417dbc8",
        "timestamp": "2026-07-25T14:14:58.000Z",
        "message": {"role": "user", "content": [
            {"tool_use_id": "toolu_01CUgTdcY4yUDvS1D3WmSHWz", "type": "tool_result",
             "content": "Workflow launched in background. Task ID: w4hiywrt6"}]},
        "toolUseResult": {
            "status": "async_launched", "taskId": "w4hiywrt6",
            "taskType": "local_workflow", "workflowName": "touch-full-recon-research",
            "runId": "wf_930e210a-6da", "summary": "Full recon of the Touch repo",
            "transcriptDir": "/home/agent/.claude/projects/-x/292fc08c/subagents/"
                             "workflows/wf_930e210a-6da",
            "scriptPath": "/home/x/orch-scripts/research.workflow.js"},
    }
    launch = read_launch(record)
    check(launch is not None and launch.run_id == "wf_930e210a-6da",
          "the launch record parses into a run join")
    check(launch.task_id == "w4hiywrt6",
          "…carrying the taskId — the run-level stop handle (amended GD-8)")

    with tempfile.TemporaryDirectory() as tmp:
        session = "292fc08c-923d-4ab4-8ff2-a9572417dbc8"
        root = os.path.join(tmp, "claude")
        slug = os.path.join(root, "projects", OWNED_SLUG)
        os.makedirs(slug)
        path = os.path.join(slug, f"{session}.jsonl")
        Path(path).write_text(json.dumps(record) + "\n", encoding="utf-8")
        sess.reset_scope_cache()
        scan = read_transcript(path)
        check(len(scan.launches) == 1, "read_transcript collects it")
        reset_read_cache()
        runs = MIRROR_SOURCES["run"](path, root=root, cwd=OWNED_CWD)
        check(len(runs) == 1 and runs[0].run_id == "wf_930e210a-6da",
              "…and the `run` source emits a run document from a transcript path")
        doc = ms.apply_operations({}, map_run(runs[0]))["runs"][refs.run_key(
            "wf_930e210a-6da")]
        launch_doc = doc["launch"]
        check(launch_doc["taskId"] == "w4hiywrt6"
              and launch_doc["transcriptDir"].endswith("wf_930e210a-6da")
              and launch_doc["scriptPath"].endswith(".workflow.js")
              and launch_doc["workflowName"] == "touch-full-recon-research",
              "…with taskId, transcriptDir, scriptPath and workflowName persisted on "
              "the `runs` document (R-49), namespaced under `launch` so the "
              "snapshot — the other observer of those same field names — cannot "
              "make the document depend on which file the walk reached first")
        check(launch_doc["status"] == "async_launched" and "status" not in doc,
              "…including the launch's own `status`, which is now SAFE to carry: it "
              "is 'how the run started' and never collides with the snapshot's "
              "'how it ended'")
        check(doc["sessionIds"] == [session],
              "…and the session it was launched from — that IS the join")
        nodes = MIRROR_SOURCES["runNode"](path, root=root, cwd=OWNED_CWD)
        check(nodes == [], "a launch record names no node; nodes come from the journal")

    check(read_launch({"toolUseResult": {"taskId": "x"}}) is None,
          "a toolUseResult with no runId names no run and is not a launch")
    check(read_launch({"toolUseResult": "a string"}) is None,
          "…and a non-object toolUseResult is not one either (0 records on disk have "
          "persistedOutputPath; the shape is not assumed)")


# --- R-49: runs and run nodes --------------------------------------------


def test_journal_ordinals_are_position_derived():
    print("test_journal_ordinals_are_position_derived")
    journal = read_journal(KILLED / "journal.jsonl", run_id="wf_455b348c-e17")
    nodes = journal.nodes
    check(len(nodes) == 9, f"wf_455b348c-e17 ⇒ 9 nodes (got {len(nodes)})")
    keys = {node["key"] for node in nodes}
    check(len(keys) == 6, f"…across 6 distinct keys (got {len(keys)})")
    check([node["ordinal"] for node in nodes] == [0, 0, 0, 0, 0, 0, 1, 1, 1],
          "…with ordinals 0/0/0/0/0/0,1,1,1 — R-49's acceptance, verbatim")
    check([node["journal_seq"] for node in nodes] == [1, 2, 3, 4, 5, 8, 9, 10, 11],
          "…and each node's journalSeq is its physical line (2 result lines between)")

    # GD-7 as amended: the ordinal is a position in THIS journal, so re-reading a
    # prefix must not renumber. (A DB counter would: MONGOSCHEMA-18.)
    check(len({(n["key"], n["ordinal"]) for n in nodes}) == 9,
          "every (key, ordinal) pair is distinct — a retry is its own node")
    by_agent = {n["agent_id"] for n in nodes}
    check(len(by_agent) == 9,
          "…and agentId → (runId,key,ordinal) is 1:1 (GD-7's live 3-key specimen)")


def test_two_journals_of_one_run_do_not_collide_on_one_node():
    print("test_two_journals_of_one_run_do_not_collide_on_one_node")
    # GD-7 as amended says BOTH "count within the same journal.jsonl" AND
    # "agentId → (runId,key,ordinal) is 1:1". A runId with two journals satisfies
    # only the first, and one exists on this machine: `wf_1a3ffcdd-c60` was
    # killed and resumed under a new sessionId with the SAME runId, so the
    # harness opened a second journal under the new session directory. Both
    # number their `started` records from 0, so per-file numbering keys the two
    # executions of one stage to one document and the walk order picks which
    # agent survives. Built here rather than added to sp-02's frozen fixtures.
    run_id = "wf_7c0ffee0-a11"
    key = "v2:409b77485f46ce71"
    other = "v2:0e1d2c3b4a596877"
    # The FIRST session sorts LAST, so a test that merely concatenated the two
    # journals in discovery order would still pass; path order is the claim.
    resumed, first = ("f" * 8 + "-2222-4222-8222-22222222cccc",
                      "0" * 8 + "-1111-4111-9111-11111111bbbb")
    agents = {first: ("ab4eefd9d57343b46", "a1111111111111111"),
              resumed: ("a45a5c78def2f3576", "a2222222222222222")}

    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "claude")
        slug = os.path.join(root, "projects", OWNED_SLUG)
        for session in (first, resumed):
            directory = os.path.join(slug, session, "subagents", "workflows", run_id)
            os.makedirs(directory)
            stage, extra = agents[session]
            lines = [json.dumps({"type": "started", "key": key, "agentId": stage}),
                     json.dumps({"type": "started", "key": other, "agentId": extra})]
            if session == resumed:                      # only the retry finished
                lines.append(json.dumps({"type": "result", "key": key,
                                         "agentId": stage, "result": {"ok": True}}))
            Path(os.path.join(directory, "journal.jsonl")).write_text(
                "\n".join(lines) + "\n", encoding="utf-8")
        sess.reset_scope_cache()
        reset_read_cache()

        raw = [read_journal(os.path.join(slug, s, "subagents", "workflows", run_id,
                                         "journal.jsonl"), run_id=run_id)
               for s in (first, resumed)]
        check(all(n["ordinal"] == 0 for scan in raw for n in scan.nodes),
              "each journal on its own numbers BOTH its keys from 0 — GD-7's "
              "per-file count, which `read_journal` still emits verbatim")

        scans = [read_run(os.path.join(slug, s, "subagents", "workflows", run_id),
                          root=root, cwd=OWNED_CWD, times=False)
                 for s in (first, resumed)]
        nodes = [n for scan in scans for n in scan.nodes]

    ids = [refs.run_node_key(n.run_id, n.key, n.ordinal) for n in nodes]
    check(len(set(ids)) == len(ids) == 4,
          f"the run's four nodes get four distinct `_id`s ({len(set(ids))}/4) — "
          f"per-file numbering gave two of them the same one")
    pairs = {(n.key, n.ordinal): n.agent_id for n in nodes}
    check(len(pairs) == len({n.agent_id for n in nodes}) == 4,
          "…so agentId → (runId,key,ordinal) is 1:1 again, which is GD-7's own "
          "second clause and the one per-file numbering breaks")
    # `.get`, not `[]`: a regression here must report a FAILURE, not raise out of
    # the runner and take the remaining tests with it.
    check(pairs.get((key, 0)) == "ab4eefd9d57343b46"
          and pairs.get((key, 1)) == "a45a5c78def2f3576",
          f"…and the two executions of one stage key read as attempt 0 and attempt "
          f"1 in PATH order, not discovery order: {pairs.get((key, 0))} then "
          f"{pairs.get((key, 1))}")
    resulted = [n for n in nodes if n.result_seen]
    check(len(resulted) == 1 and resulted[0].ordinal == 1,
          "…so the resumed attempt's verdict stays on the resumed attempt, and the "
          "killed one is not silently overwritten by it (nor it by the killed one)")
    check(all(n.journal_seq in (1, 2, 3) for n in nodes),
          "journalSeq is still the physical line inside its OWN journal — the "
          "offset moves the ordinal, never the line number it came from")
    check(all(scan.skipped["multi_journal_run"] == 1 for scan in scans),
          "both scans count the run as multi-journal, so the renumbering condition "
          "`_ordinal_offsets` documents is visible rather than inferred")

    observations = [("runNode", n) for n in nodes]
    forward, _ = state_of(observations)
    backward, _ = state_of(list(reversed(observations)))
    shuffled = list(observations)
    random.Random(20260726).shuffle(shuffled)
    tossed, _ = state_of(shuffled)
    check(ms.fingerprint(forward) == ms.fingerprint(backward) == ms.fingerprint(tossed),
          "normal / reversed / shuffled ingest of the two journals fingerprint "
          "IDENTICALLY (GD-25) — before the fix `run_nodes` differed between a live "
          "tail and a --rebuild on exactly this shape")
    check(ms.counts(forward)["run_nodes"] == 4,
          f"…with all four documents present in every order "
          f"({ms.counts(forward)['run_nodes']}), which is the half that catches a "
          f"collapse the fingerprint alone would call agreement")


def test_one_journal_per_run_is_numbered_exactly_as_before():
    print("test_one_journal_per_run_is_numbered_exactly_as_before")
    # The offset is a no-op for every single-journal run, so R-49's acceptance
    # (`wf_455b348c-e17` ⇒ 0/0/0/0/0/0,1,1,1) is asserted through the FULL
    # `read_run` path and not only through `read_journal`, where no offset is
    # ever applied and the regression could hide.
    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp, (OWNED_SLUG, KILLED.parent))
        run_dir = os.path.join(root, "projects", OWNED_SLUG, "wf_455b348c-e17")
        reset_read_cache()
        scan = read_run(run_dir, root=root, cwd=OWNED_CWD, run_id="wf_455b348c-e17",
                        times=False)
    check([n.ordinal for n in scan.nodes] == [0, 0, 0, 0, 0, 0, 1, 1, 1],
          f"R-49's ordinals survive the run-scoped count unchanged "
          f"({[n.ordinal for n in scan.nodes]})")
    check(scan.skipped["multi_journal_run"] == 0,
          "…and a one-journal run is not counted as multi-journal, so the counter "
          "means what it says")
    check(len({(n.key, n.ordinal) for n in scan.nodes}) == 9,
          "…with nine distinct (key, ordinal) pairs, as the frozen journal states")

    # The distinction the offset turns on is *journals*, not *directories*.
    # `wf_829e6f58-b2f` occupies TWO session directories (the `/clear` moved its
    # later agent transcripts) and has exactly ONE journal.jsonl — the common
    # cross-session shape, and the one a directory count would misread.
    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp, (OWNED_SLUG, RUN))
        run_dir = os.path.join(root, "projects", OWNED_SLUG, DD, "subagents",
                               "workflows", RUN_ID)
        reset_read_cache()
        spanning = read_run(run_dir, root=root, cwd=OWNED_CWD, times=False)
        dirs = find_run_dirs(RUN_ID, root)
    check(len(dirs) == 2 and spanning.skipped["multi_journal_run"] == 0,
          f"a run spanning two session DIRECTORIES with one journal is not "
          f"multi-journal ({len(dirs)} dirs, "
          f"{spanning.skipped['multi_journal_run']} counted) — the offset turns on "
          f"journal files, and only the second one that exists renumbers anything")
    check([n.ordinal for n in spanning.nodes] == [0] * len(spanning.nodes),
          "…so every node of the cross-session run keeps the ordinal its single "
          "journal gave it")


def test_a_result_attaches_by_agent_id_and_never_guesses():
    print("test_a_result_attaches_by_agent_id_and_never_guesses")
    journal = read_journal(KILLED / "journal.jsonl", run_id="wf_455b348c-e17")
    resulted = [n for n in journal.nodes if n["result_seen"]]
    check(len(resulted) == 2, f"the killed run has 2 results over 9 nodes "
                              f"(got {len(resulted)})")
    check(all(n["ordinal"] == 0 for n in resulted),
          "…and both attach to the FIRST attempt of their key, by agentId")
    check(journal.skipped["unmatched_result"] == 0, "nothing was left unmatched")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "journal.jsonl")
        Path(path).write_text(
            json.dumps({"type": "started", "key": "k", "agentId": "a" * 17}) + "\n"
            + json.dumps({"type": "result", "key": "k", "agentId": "b" * 17,
                          "result": {"passed": True}}) + "\n"
            + json.dumps({"type": "result", "key": "zz", "result": "orphan"}) + "\n",
            encoding="utf-8")
        scan = read_journal(path, run_id="wf_x")
        check(scan.nodes[0]["result_seen"] is True,
              "a result whose agentId names no started node falls back to the oldest "
              "un-resulted started of the same key")
        check(scan.skipped["unmatched_result"] == 1,
              "…while one matching NO key at all is counted, never attached to an "
              "arbitrary node (a killed run's second attempt must not inherit a verdict)")

        keyless = os.path.join(tmp, "keyless", "journal.jsonl")
        os.makedirs(os.path.dirname(keyless))
        Path(keyless).write_text(
            json.dumps({"type": "started", "key": "k", "agentId": "a" * 17}) + "\n"
            + json.dumps({"type": "result", "agentId": "a" * 17,
                          "result": {"passed": True}}) + "\n",
            encoding="utf-8")
        bare = read_journal(keyless, run_id="wf_x")
        check(bare.skipped["unmatched_result"] == 1,
              "a `result` with no `key` at all is counted too — it matched no branch "
              "and incremented nothing, so a lost verdict was invisible")
        check(bare.nodes[0]["result_seen"] is False,
              "…and it is still not attached to a node by guesswork")

    # All five frozen journals carry `key` on every record, so the arm above is
    # unobservable on the corpus — which is exactly why it needs a counter.
    for journal in sorted(FIX.rglob("journal.jsonl")):
        counts = read_journal(journal).skipped["unmatched_result"]
        if counts:
            check(False, f"{journal} left {counts} results unmatched")
            return
    check(True, "…and no frozen journal has an unmatched result of either kind")


def test_the_killed_runs_resultless_nodes_carry_no_state():
    print("test_the_killed_runs_resultless_nodes_carry_no_state")
    snapshot = read_snapshot(KILLED / "wf_455b348c-e17.json")
    scan = read_run(KILLED, snapshot=snapshot)
    state, _ = state_of(scan.observations())
    docs = list(state["run_nodes"].values())
    check(len(docs) == 9, f"9 node documents (got {len(docs)})")
    unresolved = [d for d in docs if not d["resultSeen"]]
    check(len(unresolved) == 7, f"7 of them are resultless (got {len(unresolved)})")
    check(all("state" not in d for d in docs),
          "NO document carries a `state` field — liveness is computed at read time "
          "by the one reducer, never stored (GD-23)")
    check(all("result" not in d for d in unresolved),
          "…and a resultless node stores no result, so nothing can read a verdict "
          "into it (the killed run's nodes render unknown/stale, never running)")
    resulted = [d for d in docs if d["resultSeen"]]
    check(all(ms.is_raw_wrapper(d["result"]) for d in resulted),
          "a result that IS present is stored `_raw`-wrapped, per mongo_store's "
          "declared run_nodes raw path")
    check(snapshot["status"] == "killed" and
          state["runs"][refs.run_key("wf_455b348c-e17")]["status"] == "killed",
          "the run's own recorded status is mirrored — an observation, not a verdict")


def test_a_live_run_has_no_snapshot_and_that_is_not_an_error():
    print("test_a_live_run_has_no_snapshot_and_that_is_not_an_error")
    run_dir = LIVE / "subagents" / "workflows" / LIVE_RUN
    check(not (LIVE / "workflows").exists(),
          "the frozen live-run fixture has no workflows/ directory at all")
    scan = read_run(run_dir)
    check(scan.snapshot is None and scan.skipped["no_snapshot"] == 1,
          "a missing snapshot is COUNTED, never raised (R-26's fourth amendment)")
    check(scan.run is not None and scan.run.run_id == LIVE_RUN,
          "…and the run document exists anyway, created from the first journal "
          "`started` — a snapshot-first design cannot see a running run")
    check(len(scan.nodes) == 9, f"…with all 9 of its nodes (got {len(scan.nodes)})")
    state, _ = state_of(scan.observations())
    doc = state["runs"][refs.run_key(LIVE_RUN)]
    check("status" not in doc and "harnessTotals" not in doc,
          "…and no fabricated status or totals: what the snapshot would have said "
          "is absent, not guessed")
    check(doc["startedAt"] is not None,
          "startedAt still exists — it comes from the transcripts, not from the "
          "snapshot and not from now()")


def test_the_snapshot_backfills_without_clobbering():
    print("test_the_snapshot_backfills_without_clobbering")
    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp, ("-fixture", RUN))
        run_dir = os.path.join(root, "projects", "-fixture", DD, "subagents",
                               "workflows", RUN_ID)
        snapshot_path = find_snapshot(RUN_ID, root)
        check(snapshot_path is not None and E4 in snapshot_path,
              "the snapshot is found in the OTHER session's directory — the glob "
              "spans sessions because the /clear moved it (R-26)")

        without = read_run(run_dir, root=root, snapshot={})
        with_snap = read_run(run_dir, root=root)
        check(len(without.nodes) == len(with_snap.nodes) == 7,
              "the node set is the journal's either way — the snapshot adds no node")

        labels = [n.label for n in with_snap.nodes]
        check(all(labels) and len(labels) == 7,
              f"all seven labels survive the back-fill: {labels}")
        raw = read_snapshot(snapshot_path)
        phases = [r for r in raw["workflowProgress"] if r["type"] == "workflow_phase"]
        check(len(phases) == 2 and len(raw["workflowProgress"]) == 9,
              "…and the two `workflow_phase` rows of the nine are ignored (R-26)")

        state, _ = state_of(with_snap.observations())
        run_doc = state["runs"][refs.run_key(RUN_ID)]
        check(run_doc["harnessTotals"]["nodeCount"] == raw["agentCount"],
              "`agentCount` lands as `harnessTotals.nodeCount` — display-only, "
              "namespaced, never a count check (SESSIONJSONL-7/GD-11(f))")
        check("agentCount" not in run_doc and "totalTokens" not in run_doc,
              "…and neither name survives at the top level, where something would "
              "read it as a computed total")

        # "never overwrites an observed non-null": the node's startedAt is the
        # EARLIEST of the transcript's first record and the snapshot's own epoch,
        # so applying the two observations in either order is the same document.
        node = next(n for n in with_snap.nodes if n.agent_id == "a2fc883c96ff7b837")
        bare = next(n for n in without.nodes if n.agent_id == "a2fc883c96ff7b837")
        check(node.started_at <= bare.started_at,
              "the snapshot may only move a node's start EARLIER (it is $min)")
        both = ms.apply_operations({}, map_run_node(bare) + map_run_node(node))
        reverse = ms.apply_operations({}, map_run_node(node) + map_run_node(bare))
        check(ms.fingerprint(both) == ms.fingerprint(reverse),
              "…so a snapshot arriving before or after the journal is the same "
              "document — back-fill, never clobber (R-49)")


def test_the_runs_document_is_order_independent_across_its_two_sources():
    print("test_the_runs_document_is_order_independent_across_its_two_sources")
    # `runs` is the ONE collection this module writes from two independent
    # sources: the journal+snapshot scan and a launch `toolUseResult` in a
    # main-session transcript. Both can name taskId / workflowName / scriptPath /
    # summary / status. Routed to the same top-level fields they would be `$set`
    # from two writers, and GD-25's rule is that the document must not depend on
    # which one the walk reached first. The two agree on all 7 live runs today —
    # which is luck, not a construction — so the property is asserted against a
    # DELIBERATELY disagreeing pair.
    run_id = "wf_930e210a-6da"
    launch = read_launch({"toolUseResult": {
        "runId": run_id, "taskId": "w4hiywrt6", "taskType": "local_workflow",
        "workflowName": "launch-said-this", "summary": "launch summary",
        "status": "async_launched",
        "transcriptDir": "/x/subagents/workflows/" + run_id,
        "scriptPath": "/x/launch.workflow.js"}})
    from_launch = ingest.RunObservation(
        run_id=run_id, session_ids=("292fc08c-923d-4ab4-8ff2-a9572417dbc8",),
        launch={"taskId": launch.task_id, "taskType": launch.task_type,
                "workflowName": launch.workflow_name, "summary": launch.summary,
                "status": launch.status, "transcriptDir": launch.transcript_dir,
                "scriptPath": launch.script_path})
    from_snapshot = ingest.RunObservation(
        run_id=run_id, session_ids=("e423cd3c-f859-45af-9afd-0d6bdec9b4ac",),
        task_id="a-different-task-id", workflow_name="snapshot-said-this",
        summary="snapshot summary", status="killed",
        script_path="/x/snapshot.workflow.js")

    forward = ms.apply_operations({}, map_run(from_launch) + map_run(from_snapshot))
    backward = ms.apply_operations({}, map_run(from_snapshot) + map_run(from_launch))
    check(ms.fingerprint(forward) == ms.fingerprint(backward),
          "a launch and a snapshot that CONTRADICT each other on every shared "
          "field still fingerprint identically in either order (GD-25)")

    doc = forward["runs"][refs.run_key(run_id)]
    check(doc["workflowName"] == "snapshot-said-this"
          and doc["launch"]["workflowName"] == "launch-said-this",
          "…because the two sources write DISJOINT field sets: the snapshot's "
          "reading at the top level, the launch's under `launch{}`")
    check(doc["status"] == "killed" and doc["launch"]["status"] == "async_launched",
          "…so 'how it ended' and 'how it started' are both readable, instead of "
          "one silently winning by arrival order")
    check(doc["launch"]["taskId"] == "w4hiywrt6" and doc["taskId"] ==
          "a-different-task-id",
          "…and R-49's session→run join / GD-8 stop handle is still on the "
          "document, at `launch.taskId`")
    check(sorted(doc["sessionIds"]) == sorted(
        ["292fc08c-923d-4ab4-8ff2-a9572417dbc8",
         "e423cd3c-f859-45af-9afd-0d6bdec9b4ac"]),
        "…while the genuinely-accumulating field is still $addToSet over both")

    # And the launch arm alone must not resurrect a top-level collision by
    # accident: nothing it emits may leave the sub-document.
    only_launch = ms.apply_operations({}, map_run(from_launch))["runs"][
        refs.run_key(run_id)]
    check(set(only_launch) <= {"_id", "provenance", "launch", "sessionIds"},
          f"a launch-only run document carries nothing but its namespaced fields: "
          f"{sorted(only_launch)}")


def test_two_launch_records_of_one_run_do_not_race_for_the_stop_handle():
    print("test_two_launch_records_of_one_run_do_not_race_for_the_stop_handle")
    # The namespace closes launch-vs-SNAPSHOT. It does not close
    # launch-vs-LAUNCH, and the live corpus has that shape: `wf_455b348c-e17` is
    # named by two launch records in ONE transcript with two different taskIds.
    # `launch.taskId` is amended GD-8's run-level stop handle, so with `$set` the
    # "stop this run" control would target whichever line the walk read last.
    # The pair below is the real one, verbatim.
    run_id = "wf_455b348c-e17"
    session = "e423cd3c-f859-45af-9afd-0d6bdec9b4ac"

    def launch_record(uuid, task_id, summary):
        return {
            "type": "user", "uuid": uuid, "sessionId": session,
            "timestamp": "2026-07-25T14:14:58.000Z",
            "message": {"role": "user", "content": [
                {"tool_use_id": "toolu_" + task_id, "type": "tool_result",
                 "content": f"Workflow launched in background. Task ID: {task_id}"}]},
            "toolUseResult": {
                "status": "async_launched", "taskId": task_id,
                "taskType": "local_workflow", "runId": run_id,
                "workflowName": "touch-repo-recon-research", "summary": summary,
                "transcriptDir": f"/x/{session}/subagents/workflows/{run_id}",
                "scriptPath": "/x/orch-scripts/research.workflow.js"},
        }

    first = launch_record("f8e32e35-e4ff-42df-8ddc-ed88a48a59de", "wzd027fky",
                          "the first launch's summary")
    second = launch_record("c1a2b3d4-e5f6-47a8-99b0-c1d2e3f4a5b6", "wgm4nvzgk",
                           "the second launch's summary")

    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "claude")
        slug = os.path.join(root, "projects", OWNED_SLUG)
        os.makedirs(slug)
        path = os.path.join(slug, f"{session}.jsonl")
        Path(path).write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n",
                              encoding="utf-8")
        sess.reset_scope_cache()
        reset_read_cache()
        scan = ingest._launch_scan(read_transcript(path, root=root), root)
        check(scan.skipped["duplicate_launch"] == 1,
              "two launch records naming ONE runId raise `duplicate_launch` — a run "
              "launched twice is a fact, not a silently discarded taskId")
        reset_read_cache()
        runs = MIRROR_SOURCES["run"](path, root=root, cwd=OWNED_CWD)

    check(len(runs) == 2 and {r.run_id for r in runs} == {run_id},
          f"…and BOTH observations are emitted, not deduped away: {len(runs)}")
    forward = ms.apply_operations({}, map_run(runs[0]) + map_run(runs[1]))
    backward = ms.apply_operations({}, map_run(runs[1]) + map_run(runs[0]))
    check(ms.fingerprint(forward) == ms.fingerprint(backward),
          "two launches that disagree on taskId and summary fingerprint identically "
          "in either order — every `launch.*` field is $min, so the stop handle is "
          "not chosen by walk order (GD-25 by construction)")
    doc = forward["runs"][refs.run_key(run_id)]
    check(doc["launch"]["taskId"] == "wgm4nvzgk",
          f"…and the stored handle is the deterministic minimum of the two "
          f"({doc['launch']['taskId']}), the same one on every pass and in every "
          f"mode — `$min` is per LEAF, never a whole-sub-document comparison, which "
          f"is the comparison mongod and the memory model could disagree about")
    check(doc["launch"]["summary"] == "the first launch's summary",
          "…each field independently, so nothing is lost that $min can keep")

    # The honest cost, asserted so it cannot drift into a silent loss: the OTHER
    # taskId is not stored anywhere. Keeping both wants
    # $addToSet:{launchTaskIds}, which needs a `set_fields` entry in
    # mongo_store.py (sp-05's file) or the array itself becomes order-dependent.
    check("wzd027fky" not in json.dumps(doc),
          "the second taskId is NOT stored — recorded as the handoff `map_run` "
          "names (SPECS['runs'].set_fields += launchTaskIds), never as an unsorted "
          "$addToSet array that would re-break the property")

    check(raises(IngestError, map_run, ingest.RunObservation(
        run_id=run_id, launch={"task.id": "x"})),
        "a launch field name with a dot is refused: it would become the dotted "
        "path `launch.task.id` and mongod would read the dot as a nesting level")


def test_node_times_come_from_transcripts_and_span_sessions():
    print("test_node_times_come_from_transcripts_and_span_sessions")
    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp, ("-fixture", RUN))
        run_dir = os.path.join(root, "projects", "-fixture", DD, "subagents",
                               "workflows", RUN_ID)
        dirs = find_run_dirs(RUN_ID, root)
        check(len(dirs) == 2,
              f"the run occupies TWO session directories (found {len(dirs)}) — "
              f"`/clear` split it mid-run (R-49: glob the plural)")
        scan = read_run(run_dir, root=root)
        check(scan.run.session_ids == (DD, E4),
              "…so `sessionIds[]` is a union of both, via $addToSet")

        node = next(n for n in scan.nodes if n.agent_id == "a2fc883c96ff7b837")
        check(node.started_at.isoformat().startswith("2026-07-25T02:59:29.846"),
              f"firstTs is the transcript's first record, 02:59:29.846Z "
              f"(got {node.started_at})")
        one_dir = read_run(run_dir)
        alone = next(n for n in one_dir.nodes if n.agent_id == "a2fc883c96ff7b837")
        check(node.ended_at > alone.ended_at,
              "…and endedAt comes from the CONTINUATION fragment in the other "
              "session, which a single-directory read cannot see")

        synth = next(n for n in scan.nodes if n.agent_id == "a2ed16d57db0e9887")
        check(synth.started_at is not None and synth.ended_at is not None,
              "the synthesizer ran after the /clear and still gets both times")
        # The journal has NO timestamps; every time above is a transcript's or a
        # snapshot epoch's. This is the assertion that says so.
        for record in raw_lines(run_dir + "/journal.jsonl"):
            if "timestamp" in record or "ts" in record:
                check(False, "a journal record carried a timestamp after all")
                return
        check(True, "…and no journal record carries a timestamp at all (SESSIONJSONL-5)")


def test_a_foreign_slug_holding_the_same_run_id_contributes_nothing():
    print("test_a_foreign_slug_holding_the_same_run_id_contributes_nothing")
    # `_in_scope` fences the per-path entry seam. `find_snapshot` and
    # `find_run_dirs` glob `<root>/projects/*/*/…` from INSIDE that fence, and
    # their results feed `runs.sessionIds` ($addToSet — permanent, GD-26 forbids
    # the delete that would undo it) and every node's startedAt/endedAt. R-26
    # justifies spanning SESSIONS; nothing justifies spanning projects, and a
    # `/clear` never changes the slug, so the two are not in tension.
    foreign_slug = "-a-foreign-project"          # sorts BEFORE the owned slug
    foreign_session = "9f9f9f9f-2222-4222-8222-22222222cccc"
    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp, (OWNED_SLUG, RUN))
        run_dir = os.path.join(root, "projects", OWNED_SLUG, DD, "subagents",
                               "workflows", RUN_ID)
        impostor = os.path.join(root, "projects", foreign_slug, foreign_session,
                                "subagents", "workflows", RUN_ID)
        os.makedirs(impostor)
        Path(os.path.join(impostor, "agent-a2fc883c96ff7b837.jsonl")).write_text(
            assistant_line("081b28a7-aee9-43dc-935d-fffffffffff1", "msg_foreign",
                           5, "2020-01-01T00:00:00.000Z",
                           agent_id="a2fc883c96ff7b837") + "\n", encoding="utf-8")
        snapshot_dir = os.path.join(root, "projects", foreign_slug,
                                    foreign_session, "workflows")
        os.makedirs(snapshot_dir)
        Path(os.path.join(snapshot_dir, f"{RUN_ID}.json")).write_text(json.dumps({
            "taskId": "foreign-task", "status": "foreign", "agentCount": 999,
            "summary": "another project's run of the same id"}), encoding="utf-8")
        sess.reset_scope_cache()

        unscoped = find_run_dirs(RUN_ID, root)
        check(len(unscoped) == 3,
              f"the raw glob DOES see all three directories ({len(unscoped)}) — the "
              f"scope is what excludes one, not the pattern")
        check(os.path.basename(os.path.dirname(os.path.dirname(
            os.path.dirname(find_snapshot(RUN_ID, root))))) == foreign_slug,
            "…and the raw snapshot glob would even PREFER the impostor, because it "
            "sorts first: an unscoped lookup is decided by slug alphabetics")

        scan = read_run(run_dir, root=root, cwd=OWNED_CWD)

    check(scan.run.session_ids == (DD, E4),
          f"the scoped read sees exactly this project's two sessions "
          f"({scan.run.session_ids}) — the foreign project's session is not "
          f"$addToSet-ed onto our run document, permanently (R-25 amended/GD-26)")
    check(scan.run.status != "foreign" and scan.run.task_id != "foreign-task",
          f"…and the back-fill comes from OUR snapshot, not from the one that "
          f"sorted first (status={scan.run.status})")
    node = next(n for n in scan.nodes if n.agent_id == "a2fc883c96ff7b837")
    check(node.started_at.year == 2026,
          f"…and no node's clock is moved by a foreign transcript that happens to "
          f"name the same agentId ({node.started_at})")

    check(find_run_dirs(RUN_ID, root, scope=frozenset()) == (),
          "an empty scope owns nothing — the fence is a real argument, not a "
          "default nobody passes")


def test_tsraw_is_the_sources_own_spelling_not_ours():
    print("test_tsraw_is_the_sources_own_spelling_not_ours")
    # GD-11(g) pairs `ts` with `tsRaw` so the file's own spelling survives
    # normalization. A `tsRaw` re-derived from the parsed Date is this module's
    # rendering wearing the source's name — lossless only for the single shape
    # the frozen corpus happens to carry.
    session = "0b6c1c2a-0000-4000-8000-00000000abcd"
    spellings = ["2026-07-25T14:14:59.374Z",      # the corpus's own shape
                 "2026-07-25T14:14:59Z",          # second precision
                 "2026-07-25T14:14:59.374812Z",   # microseconds
                 "2026-07-25T14:14:59.374+00:00"]  # offset instead of Z
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, f"{session}.jsonl")
        lines = []
        for index, spelling in enumerate(spellings):
            lines.append(json.dumps({
                "type": "user", "timestamp": spelling,
                "uuid": f"081b28a7-aee9-43dc-935d-15864{index:07d}"}))
            lines.append(json.dumps({"type": "mode", "mode": "plan",
                                     "timestamp": spelling}))
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        scan = read_transcript(path)

    check([o.ts_raw for o in scan.records] == spellings,
          "every record carries the timestamp string as the FILE spelled it")
    state, _ = state_of(scan.observations())
    stored = [state["records"][o.uuid]["tsRaw"] for o in scan.records]
    check(stored == spellings,
          f"…and that exact string reaches the document, all four shapes: {stored}")
    metas = [state["stream_meta"][refs.stream_meta_key(session, o.line_no)]["tsRaw"]
             for o in scan.stream_meta]
    check(metas == spellings,
          "…positional documents too — a stream_meta line has one source, so its "
          "spelling is not ambiguous either")
    dates = {state["records"][o.uuid]["ts"] for o in scan.records}
    check(len(dates) == 2,
          f"…while `ts` is the normalized Date, and three of the four spellings are "
          f"the same instant ({len(dates)} distinct Dates)")
    derived = ms.ts_fields(scan.records[1].ts)["tsRaw"]
    check(derived != spellings[1] and stored[1] == spellings[1],
          f"and the re-derived form differs ({derived!r} vs {spellings[1]!r}), which "
          f"is the whole reason the string is carried instead of recomputed")


def test_the_module_has_no_clock():
    print("test_the_module_has_no_clock")
    source = (REPO / "aggregator" / "ingest.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned = {"now", "utcnow", "time", "monotonic", "time_ns", "perf_counter",
              "today", "fromtimestamp"}
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned:
            # `datetime.datetime.fromtimestamp` converts a HARNESS epoch and is
            # the one allowed member: it reads a number the CLI wrote, it does
            # not ask the machine what time it is.
            if node.attr == "fromtimestamp":
                continue
            hits.append(node.attr)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + ([node.module] if
                                                    isinstance(node, ast.ImportFrom) else [])
            if any(name == "time" for name in names if name):
                hits.append("import time")
    check(not hits,
          f"ingest.py calls no clock — R-26's third amendment, enforced on the AST "
          f"rather than on a promise (hits: {sorted(set(hits))})")
    check("fromtimestamp" in source,
          "…while harness epochs (startTime, startedAt) are still converted, which "
          "is a different thing entirely")
    check("ingestedAt" not in source,
          "and no `ingestedAt` is written here: an ingest clock is the mirror's "
          "bookkeeping (cursors.updatedTs), and --backfill would refuse it anyway")


# --- SD-1: the mapper contract -------------------------------------------


def test_mappers_are_registered_pure_and_write_only_our_collections():
    print("test_mappers_are_registered_pure_and_write_only_our_collections")
    registry = mr.discover_mappers(["ingest"])
    check(sorted(registry) == ["record", "run", "runNode", "streamMeta", "usage"],
          f"all five kinds are registered with mirror (got {sorted(registry)})")
    check(set(MIRROR_MAPPERS) == set(MIRROR_SOURCES),
          "every mapped kind has a source and vice versa — a kind with no source "
          "never rebuilds, and one with no mapper is data the mirror drops")

    source = (REPO / "aggregator" / "ingest.py").read_text(encoding="utf-8")
    check("pymongo" not in source,
          "the package name does not appear at all (GD-21: only mongo_store and "
          "mirror may import it)")

    # SD-1's purity, on the half a static check CAN see: the mapper functions
    # touch no filesystem call.
    tree = ast.parse(source)
    mapper_names = {"map_record", "map_stream_meta", "map_usage", "map_run",
                    "map_run_node", "_split_ops", "_normalized", "_only_ours",
                    "_launch_paths"}
    impure = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in mapper_names:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute) and inner.attr in (
                        "open", "listdir", "stat", "walk", "glob", "read_complete_lines"):
                    impure.append(f"{node.name}.{inner.attr}")
                if isinstance(inner, ast.Name) and inner.id == "open":
                    impure.append(f"{node.name}.open")
    check(not impure, f"no mapper does I/O (SD-1): {impure}")

    check(raises(IngestError, ingest._only_ours, [("agents", "x", {})]),
          "…and a mapper that tried to write `agents` is refused structurally — "
          "agent assembly is R-48's, and this module reads the same files")
    check(tuple(COLLECTIONS) == ("records", "stream_meta", "usage", "runs", "run_nodes"),
          "the allowed set is exactly the five GD-24 rows this sub-plan owns")


def test_every_id_comes_from_refs():
    print("test_every_id_comes_from_refs")
    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp, ("-fixture", RUN))
        run_dir = os.path.join(root, "projects", "-fixture", DD, "subagents",
                               "workflows", RUN_ID)
        observations = list(read_run(run_dir, root=root).observations())
        for path in sorted(transcripts_of(os.path.join(root, "projects", "-fixture"))):
            observations.extend(read_transcript(path, root=root).observations())
    _state, ops = state_of(observations)
    check(len(ops) > 1000, f"a real operation set ({len(ops)} ops)")
    bad = []
    for collection, key, _update in ops:
        try:
            ms.check_id(collection, key)
        except ms.MongoStoreError as exc:
            bad.append(f"{collection}/{key}: {exc}")
    check(not bad, f"every `_id` parses back through refs.ref_key (SD-11): {bad[:2]}")
    check({collection for collection, _k, _u in ops} <= set(COLLECTIONS),
          "…and every operation targets one of this module's five collections")
    check(PROVENANCE == "harness" and all(
        update.get("$setOnInsert", {}).get("provenance") == PROVENANCE for _c, _k, update in ops),
        "…and every one carries `provenance: harness` as an immutable — GD-28 makes "
        "the field mandatory, and a document without one answers no provenance "
        "filter at all, not even the 'writer unknown' bucket")


def test_the_algebra_is_order_independent():
    print("test_the_algebra_is_order_independent")
    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp, ("-fixture", RUN))
        run_dir = os.path.join(root, "projects", "-fixture", DD, "subagents",
                               "workflows", RUN_ID)
        observations = list(read_run(run_dir, root=root).observations())
        for path in sorted(transcripts_of(os.path.join(root, "projects", "-fixture"))):
            observations.extend(read_transcript(path, root=root).observations())

    normal, _ = state_of(observations)
    reverse, _ = state_of(list(reversed(observations)))
    shuffled = list(observations)
    random.Random(47).shuffle(shuffled)
    mixed, _ = state_of(shuffled)

    prints = {ms.fingerprint(normal), ms.fingerprint(reverse), ms.fingerprint(mixed)}
    check(len(prints) == 1,
          "normal / reversed / shuffled ingest fingerprint IDENTICALLY (GD-25's "
          "acceptance property)")
    counts = ms.counts(normal)
    check(ms.counts(reverse) == counts == ms.counts(mixed),
          f"…AND the counts are equal, which is the half that catches a silent "
          f"collapse: {counts}")
    check(counts["runs"] == 1 and counts["run_nodes"] == 7,
          "…including exactly one run and its seven nodes")

    twice, _ = state_of(observations + observations)
    check(ms.fingerprint(twice) == ms.fingerprint(normal),
          "and ingesting the whole corpus twice changes nothing — GD-25's "
          "double-ingest arm, in memory")

    # The arm above is the regression floor, and on its own it is not the
    # invariant: the frozen corpus contains NEITHER shape that can break the
    # property (one message.id under two sessions, two launch records of one
    # runId), so it would report green whatever operator the mappers chose. Both
    # shapes are real on the live corpus, so both are built here.
    with tempfile.TemporaryDirectory() as tmp:
        root = clear_split_root(tmp)
        reset_read_cache()
        adversarial = []
        for kind in MIRROR_SOURCES:
            adversarial.extend((kind, obs) for obs in
                               MIRROR_SOURCES[kind](None, root=root, cwd=OWNED_CWD))
    check(len({obs.session_id for kind, obs in adversarial if kind == "usage"
               and obs.message_id == SPLIT_MESSAGE}) == 2,
          "the built corpus really does observe one message.id under two "
          "sessionIds — the shape being asserted about exists")
    a_normal, _ = state_of(adversarial)
    a_reverse, _ = state_of(list(reversed(adversarial)))
    a_shuffled = list(adversarial)
    random.Random(48).shuffle(a_shuffled)
    a_mixed, _ = state_of(a_shuffled)
    check(len({ms.fingerprint(a_normal), ms.fingerprint(a_reverse),
               ms.fingerprint(a_mixed)}) == 1,
          "…and the `/clear`-split corpus fingerprints identically in all three "
          "orders too: `usage.sessionId` is $min, not $setOnInsert (GD-25 on the "
          "shape the fixtures cannot express)")
    doc = a_normal["usage"][refs.usage_key(SPLIT_MESSAGE)]
    check(doc["sessionId"] == min(SPLIT_SESSIONS) and doc["out"] == 90,
          f"…storing the earliest-sorting session and the $max of the counts "
          f"({doc['sessionId'][:8]}…, out={doc['out']}) — arbitrary but the same "
          f"on a live tail and on a --rebuild, which is the whole requirement")


def test_the_set_on_insert_payload_never_varies_for_one_id():
    print("test_the_set_on_insert_payload_never_varies_for_one_id")
    # The single property that catches the whole `$setOnInsert` failure class on
    # ANY corpus, adversarial or not: that operator is first-writer-wins, so two
    # operations on one `_id` carrying different payloads make the stored
    # document depend on ingest order. `mongo_store.op_set_on_insert` states the
    # rule; this asserts it over every operation five real sources produce.
    def payloads(observations):
        seen = {}
        varying = []
        for collection, key, update in state_of(observations)[1]:
            payload = update.get("$setOnInsert")
            if payload is None:
                continue
            previous = seen.setdefault((collection, key), payload)
            if previous != payload:
                varying.append((collection, key, previous, payload))
        return seen, varying

    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp, ("-fixture", RUN))
        run_dir = os.path.join(root, "projects", "-fixture", DD, "subagents",
                               "workflows", RUN_ID)
        observations = list(read_run(run_dir, root=root).observations())
        for path in sorted(transcripts_of(os.path.join(root, "projects", "-fixture"))):
            observations.extend(read_transcript(path, root=root).observations())
    seen, varying = payloads(observations)
    check(len(seen) > 1000 and not varying,
          f"over {len(seen)} distinct `_id`s of the frozen corpus, no two "
          f"operations disagree about their $setOnInsert payload: {varying[:1]}")

    with tempfile.TemporaryDirectory() as tmp:
        root = clear_split_root(tmp)
        reset_read_cache()
        adversarial = []
        for kind in MIRROR_SOURCES:
            adversarial.extend((kind, obs) for obs in
                               MIRROR_SOURCES[kind](None, root=root, cwd=OWNED_CWD))
    _seen, varying = payloads(adversarial)
    check(not varying,
          f"…nor on the `/clear`-split corpus, where the same message.id is "
          f"observed under two sessions — the case that made this property fail: "
          f"{varying[:1]}")

    # And the property is a real detector, not a tautology: the pre-fix shape —
    # `sessionId` back inside the payload — has to make it fire.
    spanning = [ingest.UsageObservation(
        message_id=SPLIT_MESSAGE, session_id=session,
        tokens=dict.fromkeys(ingest.USAGE_FIELDS, 1), agent_id=SPLIT_AGENT)
        for session in SPLIT_SESSIONS]
    _seen, clean = payloads([("usage", obs) for obs in spanning])
    check(not clean,
          "two observations of one message.id under two sessions agree about their "
          "payload today, because sessionId left it")
    pre_fix = [dict(update["$setOnInsert"], sessionId=obs.session_id)
               for obs in spanning for _c, _k, update in map_usage(obs)]
    check(pre_fix[0] != pre_fix[1],
          f"…and R-50's literal `$setOnInsert:{{agentId, sessionId, runId}}` would "
          f"give them DIFFERENT payloads ({pre_fix[0]['sessionId'][:8]}… vs "
          f"{pre_fix[1]['sessionId'][:8]}…), so the property above detects the real "
          f"defect rather than restating the fix")


def test_mirror_sources_answer_only_for_paths_they_own():
    print("test_mirror_sources_answer_only_for_paths_they_own")
    journal = str(KILLED / "journal.jsonl")
    transcript = str(RUN / DD / "subagents" / "workflows" / RUN_ID
                     / "agent-a2ec106948f58d0c8.jsonl")
    session_file = str(VARIETY)

    check(is_journal_path(journal) is False,
          "a journal outside a subagents/workflows/<runId>/ tree is not a journal "
          "path — the anchor is the pair, so a snapshot's `workflows/` dir cannot "
          "be mistaken for a run dir")
    real_journal = str(LIVE / "subagents" / "workflows" / LIVE_RUN / "journal.jsonl")
    check(is_journal_path(real_journal) and not is_transcript_path(real_journal),
          "…while the real one is a journal and never a transcript")
    check(is_transcript_path(transcript) and is_transcript_path(session_file),
          "agent and session transcripts are both records sources")

    reset_read_cache()
    for kind in ("record", "streamMeta", "usage"):
        check(MIRROR_SOURCES[kind](real_journal, root=FIX) == [],
              f"the `{kind}` source returns nothing for a journal path")
    check(MIRROR_SOURCES["runNode"](transcript, root=FIX) == [],
          "the `runNode` source returns nothing for a transcript path")
    check(MIRROR_SOURCES["run"](str(FIX / "PROVENANCE.md"), root=FIX) == [],
          "…and every source returns nothing for a path nobody owns")
    check(run_id_for_path(str(RUN / E4 / "workflows" / f"{RUN_ID}.json")) is None,
          "a snapshot's path names no runId directory (it is a FILE named for one)")

    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp, (OWNED_SLUG, RUN), (FOREIGN_SLUG,
                                                    DISCOVERY / FOREIGN_SLUG))
        owned = os.path.join(root, "projects", OWNED_SLUG, DD, "subagents",
                             "workflows", RUN_ID, "agent-a2ec106948f58d0c8.jsonl")
        reset_read_cache()
        records = MIRROR_SOURCES["record"](owned, root=root, cwd=OWNED_CWD)
        check(len(records) == 104,
              f"an owned path returns its observations ({len(records)})")

        # R-25 as amended, on the PER-PATH arm: `mirror.iter_backfill_sources`
        # walks all of `<root>/projects` with no slug filter, so this test is the
        # only thing between `--backfill` and four other projects' transcripts.
        foreign = os.path.join(root, "projects", FOREIGN_SLUG,
                               os.path.basename(VARIETY))
        check(is_transcript_path(foreign),
              "the foreign file passes the basename grammar — which is exactly why "
              "the grammar alone is not an ownership test")
        reset_read_cache()
        for kind in ("record", "streamMeta", "usage", "run"):
            got = MIRROR_SOURCES[kind](foreign, root=root, cwd=OWNED_CWD)
            check(got == [],
                  f"…and the `{kind}` source still returns nothing for it: a slug "
                  f"this project does not own is not this project's data (R-25)")
        reset_read_cache()
        check(len(MIRROR_SOURCES["record"](foreign, root=root,
                                           cwd="/tmp/claude-1000/liveio")) > 0,
              "…while the project that DOES own that slug reads it perfectly well — "
              "the rule is ownership, not a blocklist")


def test_the_one_entry_read_memo_does_not_outlive_its_file():
    print("test_the_one_entry_read_memo_does_not_outlive_its_file")
    # `mirror.iter_backfill_observations` calls EVERY source once per file, so the
    # three transcript sources would read each file three times without a memo —
    # and a memo that outlived a rewrite would answer about bytes that are gone.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "claude")
        slug = os.path.join(root, "projects", OWNED_SLUG)
        os.makedirs(slug)
        sess.reset_scope_cache()
        path = os.path.join(slug, "0b6c1c2a-0000-4000-8000-00000000abcd.jsonl")
        first = {"type": "user", "uuid": "081b28a7-aee9-43dc-935d-1586407f232e"}
        Path(path).write_text(json.dumps(first) + "\n", encoding="utf-8")
        reset_read_cache()
        check(len(MIRROR_SOURCES["record"](path, root=root, cwd=OWNED_CWD)) == 1,
              "one record before the rewrite")
        second = dict(first, uuid="1ec9c5c1-3921-443e-82c2-f15e372d237a")
        Path(path).write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n",
                              encoding="utf-8")
        check(len(MIRROR_SOURCES["record"](path, root=root, cwd=OWNED_CWD)) == 2,
              "…and two after it: the memo is keyed on (dev, ino, size, mtime)")


def test_the_rebuild_walk_is_read_once_not_once_per_source():
    print("test_the_rebuild_walk_is_read_once_not_once_per_source")
    # `--rebuild` calls each source over the WHOLE corpus before moving to the
    # next, so the one-entry memo (sized for `--backfill`'s consecutive calls)
    # never hits: without a walk-level memo the five sources read the corpus five
    # times, and `_run_scans` was walked twice on top of that.
    reads = []
    original = ingest.read_transcript

    def counting(path, **kwargs):
        reads.append(os.fspath(path))
        return original(path, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp, (OWNED_SLUG, RUN))
        transcripts = ingest.iter_transcript_paths(root, OWNED_CWD)
        check(len(transcripts) > 5, f"a real corpus to walk ({len(transcripts)} files)")
        ingest.read_transcript = counting
        try:
            reset_read_cache()
            for kind in ("record", "streamMeta", "usage", "run", "runNode"):
                MIRROR_SOURCES[kind](None, root=root, cwd=OWNED_CWD)
        finally:
            ingest.read_transcript = original
    check(sorted(reads) == sorted(transcripts),
          f"all five rebuild sources together read each transcript EXACTLY once "
          f"({len(reads)} reads over {len(transcripts)} files)")

    # …and the memo is still a memo, not a stale cache: a rewritten file
    # invalidates the whole walk, because its key carries every file's identity.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "claude")
        slug = os.path.join(root, "projects", OWNED_SLUG)
        os.makedirs(slug)
        sess.reset_scope_cache()
        path = os.path.join(slug, "0b6c1c2a-0000-4000-8000-00000000abcd.jsonl")
        one = {"type": "user", "uuid": "081b28a7-aee9-43dc-935d-1586407f232e"}
        Path(path).write_text(json.dumps(one) + "\n", encoding="utf-8")
        reset_read_cache()
        check(len(MIRROR_SOURCES["record"](None, root=root, cwd=OWNED_CWD)) == 1,
              "the walk memo answers with what is on disk")
        two = dict(one, uuid="1ec9c5c1-3921-443e-82c2-f15e372d237a")
        Path(path).write_text(json.dumps(one) + "\n" + json.dumps(two) + "\n",
                              encoding="utf-8")
        check(len(MIRROR_SOURCES["record"](None, root=root, cwd=OWNED_CWD)) == 2,
              "…and a rewrite invalidates it: the key is every file's "
              "(dev, ino, size, mtime), so a cache can never outlive its bytes")


def test_a_rebuild_through_mirror_reproduces_the_scan():
    print("test_a_rebuild_through_mirror_reproduces_the_scan")
    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp, ("-home-laniakea-Projects-touch", RUN))
        env = {"TOUCH_CLAUDE_ROOT": root, "HOME": tmp}
        reset_read_cache()
        direct = []
        for path in sorted(transcripts_of(os.path.join(root, "projects",
                                                       "-home-laniakea-Projects-touch"))):
            direct.extend(read_transcript(path, root=root).records)

        reset_read_cache()
        through = MIRROR_SOURCES["record"](None, cwd="/home/laniakea/Projects/touch",
                                           root=root, env=env)
        check(len(through) == len(direct) and len(direct) > 1000,
              f"the rebuild seam (`path=None`) finds every transcript the direct "
              f"walk does: {len(through)} == {len(direct)}")
        check({o.uuid for o in through} == {o.uuid for o in direct},
              "…the same records, not merely the same number")

        # And nothing outside this project's slug: R-25's amended scope.
        root2 = linked_root(os.path.join(tmp, "second"),
                            ("-home-laniakea-Projects-touch", RUN),
                            ("-tmp-claude-1000-liveio",
                             DISCOVERY / "-tmp-claude-1000-liveio"))
        reset_read_cache()
        scoped = MIRROR_SOURCES["record"](None, cwd="/home/laniakea/Projects/touch",
                                          root=root2,
                                          env={"TOUCH_CLAUDE_ROOT": root2})
        check(len(scoped) == len(direct),
              "a foreign slug directory beside it is NOT ingested (R-25 as amended: "
              "four such directories exist on this machine)")


def test_a_backfill_stamps_nothing_newer_than_its_source():
    print("test_a_backfill_stamps_nothing_newer_than_its_source")
    # `Mirror.backfill` refuses any stored `ts` newer than the source file's
    # mtime. Since this module has no clock, every timestamp it emits comes from
    # the file's own bytes — so the guard has nothing to refuse. Asserted rather
    # than assumed, because an `ingestedAt` would silently break --backfill.
    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp, (OWNED_SLUG, RUN))
        path = os.path.join(root, "projects", OWNED_SLUG, DD, "subagents",
                            "workflows", RUN_ID, "agent-a2ec106948f58d0c8.jsonl")
        reset_read_cache()
        observations = [("record", o) for o in
                        MIRROR_SOURCES["record"](path, root=root, cwd=OWNED_CWD)]
        state, _ = state_of(observations)
        newest = max(doc["ts"] for doc in state["records"].values() if "ts" in doc)
        check(newest.year == 2026 and newest.month == 7 and newest.day == 25,
              f"every stored ts is the transcript's own 2026-07-25 clock ({newest})")
        mtime = os.stat(path).st_mtime
        check(newest.timestamp() < mtime,
              "…and none is newer than the source file's mtime, which is exactly the "
              "guard `--backfill` applies (R-45)")


def test_backfill_and_rebuild_see_exactly_the_same_files():
    print("test_backfill_and_rebuild_see_exactly_the_same_files")
    # The two modes read the corpus through different seams — `--rebuild` calls
    # each source once with `path=None` (scoped by `iter_transcript_paths`),
    # `--backfill` calls every source once per file found by
    # `mirror.iter_backfill_sources`, which walks ALL of `<root>/projects` with
    # no slug filter and is documented to do so. If only one of them scopes, the
    # other writes another project's transcripts into this repo's database —
    # permanently (GD-26 forbids the delete that would undo it) — and R-55's
    # "wipe + rebuild equivalence" acceptance becomes unsatisfiable.
    with tempfile.TemporaryDirectory() as tmp:
        root = walkable_root(tmp, (OWNED_SLUG, RUN), (FOREIGN_SLUG,
                                                      DISCOVERY / FOREIGN_SLUG))
        saved = dict(os.environ)
        os.environ.update({"TOUCH_CLAUDE_ROOT": root,
                           "TOUCH_PROJECT_CWD": OWNED_CWD})
        try:
            sess.reset_scope_cache()
            reset_read_cache()
            rebuilt = []
            for kind in MIRROR_SOURCES:
                rebuilt.extend((kind, obs) for obs in
                               MIRROR_SOURCES[kind](None, root=root, cwd=OWNED_CWD))

            walked = mr.iter_backfill_sources(root)
            foreign_files = [p for p in walked
                             if f"/projects/{FOREIGN_SLUG}/" in p]
            check(foreign_files,
                  f"the backfill walk does see the foreign slug's files "
                  f"({len(foreign_files)}) — nothing upstream filters them")

            reset_read_cache()
            backfilled = []
            emitted_paths = set()
            for path in walked:
                for kind in MIRROR_SOURCES:
                    for obs in MIRROR_SOURCES[kind](path) or ():
                        backfilled.append((kind, obs))
                        emitted_paths.add(path)
        finally:
            os.environ.clear()
            os.environ.update(saved)
            sess.reset_scope_cache()

    check(not [p for p in emitted_paths if f"/projects/{FOREIGN_SLUG}/" in p],
          "…and NOT ONE observation comes out of them: the per-path arm applies "
          "the same rooted `sessions.scoped_dirs` test the rebuild arm gets for "
          "free (R-25 as amended, sessions.py's rule)")

    rebuilt_state, _ = state_of(rebuilt)
    backfilled_state, _ = state_of(backfilled)
    check(ms.counts(rebuilt_state) == ms.counts(backfilled_state),
          f"--backfill and --rebuild produce the same document COUNTS: "
          f"{ms.counts(rebuilt_state)}")
    check(ms.fingerprint(rebuilt_state) == ms.fingerprint(backfilled_state),
          "…and the same bytes, which is R-55's wipe/rebuild equivalence stated as "
          "a property of the two seams rather than of one run")


def test_live_mongod_arm():
    print("test_live_mongod_arm")
    # The one claim a memory model cannot settle: `run_nodes` puts `runId`/`key`/
    # `ordinal` under $setOnInsert while the `_id` filter carries the same three
    # components, and mongod is strict about $setOnInsert vs the upsert filter.
    # Without a server the arm skips and the in-memory tests still stand.
    uri = os.environ.get("TOUCH_MONGO_URI")
    if not uri:
        skip("live Mongo arm: TOUCH_MONGO_URI is not set (R-42's loopback+auth recipe)")
        return
    if not ms.pymongo_available():
        skip("live Mongo arm: pymongo is not installed (GD-21: absence is legal)")
        return
    try:
        client = ms.open_client(uri)
    except ms.MongoUnavailable as exc:
        skip(f"live Mongo arm: {exc}")
        return
    if not ms.ping(client):
        client.close()
        skip("live Mongo arm: no mongod answered within the GD-21 timeouts")
        return
    name = f"touch_test_{os.getpid()}"
    db = client[name]
    try:
        _live_checks(db)
    finally:
        check(name.startswith("touch_test_"),
              f"dropping only the database this test constructed: {name} (GD-27)")
        if name.startswith("touch_test_"):
            client.drop_database(name)
        client.close()


def _live_checks(db):
    ms.ensure_schema(db)
    snapshot = read_snapshot(KILLED / "wf_455b348c-e17.json")
    scan = read_run(KILLED, snapshot=snapshot)
    _state, ops = state_of(scan.observations())
    memory = ms.fingerprint(ms.apply_operations({}, ops))

    if not db.name.startswith("touch_test_"):
        return
    orders = {"normal": ops, "reversed": list(reversed(ops))}
    shuffled = list(ops)
    random.Random(49).shuffle(shuffled)
    orders["shuffled"] = shuffled

    seen = {}
    for label, sequence in orders.items():
        for collection in ("runs", "run_nodes"):
            db[collection].delete_many({})            # fixture reset, not mirror code
        for collection in ("runs", "run_nodes"):
            batch = [(key, update) for coll, key, update in sequence
                     if coll == collection]
            result = ms.bulk_upsert(db, collection, batch)
            if result["errors"]:
                check(False, f"{label}/{collection}: mongod refused "
                             f"{result['errors'][:1]}")
                return
        state = {collection: {doc["_id"]: doc for doc in db[collection].find({})}
                 for collection in ("runs", "run_nodes")}
        seen[label] = ms.fingerprint(state)
    check(len(set(seen.values())) == 1,
          f"mongod stores the same bytes in every ingest order: {seen}")
    check(memory and seen["normal"],
          "…and the memory model and the server both produced a fingerprint")
    check(db["run_nodes"].count_documents({}) == 9,
          "9 node documents survive a real $setOnInsert on the _id's own components")

    # The namespaced launch sub-document is new shape on `runs`, and GD-24
    # installs a `$jsonSchema` validator on the server: "the memory model liked
    # it" is not evidence that mongod will. Written in BOTH orders against the
    # same `_id` as a snapshot observation that contradicts it on every shared
    # field name.
    run_id = "wf_930e210a-6da"
    from_launch = ingest.RunObservation(
        run_id=run_id, session_ids=("292fc08c-923d-4ab4-8ff2-a9572417dbc8",),
        launch={"taskId": "w4hiywrt6", "taskType": "local_workflow",
                "workflowName": "launch-said-this", "summary": "launch summary",
                "status": "async_launched", "scriptPath": "/x/launch.workflow.js",
                "transcriptDir": "/x/subagents/workflows/" + run_id})
    from_snapshot = ingest.RunObservation(
        run_id=run_id, session_ids=("e423cd3c-f859-45af-9afd-0d6bdec9b4ac",),
        task_id="a-different-task-id", workflow_name="snapshot-said-this",
        summary="snapshot summary", status="killed")
    stored = []
    for pair in ([from_launch, from_snapshot], [from_snapshot, from_launch]):
        db["runs"].delete_many({"_id": refs.run_key(run_id)})   # fixture reset
        batch = [(key, update) for obs in pair
                 for _c, key, update in map_run(obs)]
        result = ms.bulk_upsert(db, "runs", batch)
        if result["errors"]:
            check(False, f"mongod refused the namespaced launch document: "
                         f"{result['errors'][:1]}")
            return
        stored.append(db["runs"].find_one({"_id": refs.run_key(run_id)}))
    check(stored[0]["launch"]["taskId"] == "w4hiywrt6"
          and stored[0]["workflowName"] == "snapshot-said-this",
          "mongod stores the launch sub-document beside the snapshot's own fields, "
          "and its $jsonSchema accepts the shape")
    check(ms.fingerprint({"runs": {d["_id"]: d for d in [stored[0]]}})
          == ms.fingerprint({"runs": {d["_id"]: d for d in [stored[1]]}}),
          "…and the two arrival orders read back as the same document on the "
          "server, not only in the model (GD-25)")

    # Launch-vs-LAUNCH, the shape the namespace does NOT close: `$min` on a
    # dotted leaf path is the one operator claim in this file that the memory
    # model settles by string comparison and mongod settles in BSON. They agree
    # about strings — but "they agree" is a claim about the server, so it is
    # made against the server. The pair is `wf_455b348c-e17`'s real one.
    contested = "wf_455b348c-e17"
    launches = [ingest.RunObservation(
        run_id=contested, session_ids=("e423cd3c-f859-45af-9afd-0d6bdec9b4ac",),
        launch={"taskId": task_id, "taskType": "local_workflow",
                "workflowName": "touch-repo-recon-research", "summary": summary,
                "status": "async_launched",
                "scriptPath": "/x/orch-scripts/research.workflow.js",
                "transcriptDir": "/x/subagents/workflows/" + contested})
        for task_id, summary in (("wzd027fky", "the first launch's summary"),
                                 ("wgm4nvzgk", "the second launch's summary"))]
    twice = []
    for pair in (launches, list(reversed(launches))):
        db["runs"].delete_many({"_id": refs.run_key(contested)})   # fixture reset
        result = ms.bulk_upsert(db, "runs", [(key, update) for obs in pair
                                             for _c, key, update in map_run(obs)])
        if result["errors"]:
            check(False, f"mongod refused two launches of one runId: "
                         f"{result['errors'][:1]}")
            return
        twice.append(db["runs"].find_one({"_id": refs.run_key(contested)}))
    check(twice[0] == twice[1] and twice[0]["launch"]["taskId"] == "wgm4nvzgk",
          f"two launch records of ONE runId store the same stop handle on the "
          f"server in either arrival order ({twice[0]['launch']['taskId']}) — the "
          f"$min-per-leaf claim, settled by mongod rather than by the model")
    check(twice[0]["launch"]["summary"] == "the first launch's summary",
          "…field by field, so the server keeps the same minimum the model does "
          "and a --rebuild cannot disagree with the live tail that preceded it")


def main():
    print("test_ingest.py — R-26 (as amended), R-47, R-49\n")
    for test in (test_the_bucket_table_is_the_only_decider,
                 test_the_frozen_corpus_buckets_without_collapse,
                 test_every_uuidless_type_survives_positionally,
                 test_queue_operation_is_render_false_and_never_deduped,
                 test_dotted_snapshot_records_bucket_positionally_and_wrap,
                 test_the_session_id_is_injected_from_the_path_and_says_so,
                 test_a_positional_key_belongs_to_the_file_it_numbers,
                 test_positions_are_stored_on_every_document,
                 test_an_unparsable_line_is_stored_not_dropped,
                 test_the_oversize_line_is_stored_whole,
                 test_the_persisted_output_regex_fires_on_the_real_spills,
                 test_containment_is_rooted_and_resolved_not_a_directory_name,
                 test_the_tool_results_scan_surfaces_unlinked_spills,
                 test_the_launch_tool_use_result_is_the_taskid_join,
                 test_journal_ordinals_are_position_derived,
                 test_two_journals_of_one_run_do_not_collide_on_one_node,
                 test_one_journal_per_run_is_numbered_exactly_as_before,
                 test_a_result_attaches_by_agent_id_and_never_guesses,
                 test_the_killed_runs_resultless_nodes_carry_no_state,
                 test_a_live_run_has_no_snapshot_and_that_is_not_an_error,
                 test_the_snapshot_backfills_without_clobbering,
                 test_the_runs_document_is_order_independent_across_its_two_sources,
                 test_two_launch_records_of_one_run_do_not_race_for_the_stop_handle,
                 test_node_times_come_from_transcripts_and_span_sessions,
                 test_a_foreign_slug_holding_the_same_run_id_contributes_nothing,
                 test_tsraw_is_the_sources_own_spelling_not_ours,
                 test_the_module_has_no_clock,
                 test_mappers_are_registered_pure_and_write_only_our_collections,
                 test_every_id_comes_from_refs,
                 test_the_algebra_is_order_independent,
                 test_the_set_on_insert_payload_never_varies_for_one_id,
                 test_mirror_sources_answer_only_for_paths_they_own,
                 test_the_one_entry_read_memo_does_not_outlive_its_file,
                 test_the_rebuild_walk_is_read_once_not_once_per_source,
                 test_a_rebuild_through_mirror_reproduces_the_scan,
                 test_a_backfill_stamps_nothing_newer_than_its_source,
                 test_backfill_and_rebuild_see_exactly_the_same_files,
                 test_live_mongod_arm):
        test()
        reset_read_cache()
    print()
    for message in skips:
        print(f"skipped: {message}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for one in failures:
            print(f"  - {one}")
        sys.exit(1)
    print("all ingest tests passed")


if __name__ == "__main__":
    main()
