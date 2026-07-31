"""Harness ingest: transcripts, journals, snapshots, spills (R-26 as amended by
the six §2 clauses, plus R-47, R-49, R-50).

This module owns **the `~/.claude` file formats** (GD-15: the module that owns a
format owns its parser) and turns them into observations for five of GD-24's
collections and nothing else:

    records      one document per uuid-bearing transcript record   (R-47)
    stream_meta  one positional document per uuid-less line        (R-47)
    usage        one $max-accumulated document per message.id      (R-50)
    runs         one document per Workflow runId                   (R-49)
    run_nodes    one document per (runId, key, ordinal)            (R-49)

`agents` is deliberately absent: assembling an agent from its fragments is
R-48's, in `agents.py`, and this module must not touch it (SESSIONJSONL-3 —
`sessionId` is never a grouping key for agent records, and the only way to keep
that structurally true is for the module that *reads sessions' files* to have no
code path that writes an agent document). :func:`_only_ours` is that wall.

Two halves, separated by the SD-1 line
--------------------------------------
* **reading** (:func:`read_transcript`, :func:`read_journal`,
  :func:`read_snapshot`, :func:`scan_tool_results`) does the I/O and returns
  frozen dataclasses — pure data, no database vocabulary;
* **mapping** (:data:`MIRROR_MAPPERS`) is pure: observations in,
  `(collection, _id, update)` triples out, built only from `refs.ref_key` and
  `mongo_store`'s op vocabulary. No I/O, no clock, no driver. `mirror.py`
  discovers and drives it, and it — with `mongo_store.py` — is the only module
  that may import the database driver (GD-21). The driver package's name does
  not appear in this file at all, and `tests/test_mirror.py` greps for it.

There is no clock here, at all (R-26's third amendment)
-------------------------------------------------------
`journal.jsonl` records carry **no timestamp** (SESSIONJSONL-5), and the
temptation is to stamp a node with `now()` when it is first seen. That is
forbidden: `now()` is the ingest's clock, not the run's, so it makes a
`--rebuild` produce different documents from the live pass that preceded it, and
it makes `--backfill` stamp a 2026-07-25 run with today's date (`Mirror.backfill`
refuses any `ts` newer than the source's mtime, so it would not even be stored —
it would be *dropped*). Node times therefore come from the agent transcript's
first and last record timestamps, and from the snapshot's own epoch fields, and
from nowhere else. `tests/test_ingest.py` asserts the absence statically: no
`now(`, no `time.time`, no `utcnow` in this file.

The "ingested-at" field R-49 names as "kept separate and never displayed" is
*aggregator bookkeeping*, and it lives where the aggregator's own bookkeeping
lives — `cursors.updatedTs` (mirror.py, GD-24's table). Writing it here would
put a clock in the one module whose rule is that it has none, and
`tests/test_ingest.py` greps this file for the field name to keep it out.

The bucket table (R-47), stated once, here
------------------------------------------
    user | assistant | system | attachment   ->  `records`,  `_id = <uuid>`
    every other type, known or not           ->  `stream_meta`, positional

with one refinement the table implies and this module makes explicit: the four
types go to `records` **only when the record actually carries a lowercase-uuid
`uuid`**. A `user` line without one cannot be keyed by uuid at all, and the two
honest options are "drop it" and "key it positionally". GD-26 forbids the first.
So the predicate is *type ∧ uuid*, and :func:`bucket_of` is the single place it
is decided (`tests/test_ingest.py` asserts the uuid-bearing and uuid-less counts
of the frozen corpus separately, so a change to this predicate cannot silently
collapse one bucket into the other — the failure MONGOSCHEMA-1's content-hash
probe made, losing 142 of 333 records).

**The one deviation from R-47's table, stated as a deviation (handoff to
sp-15).** R-47 says *every* other type ⇒ `stream_meta`, positionally. This
module does that for every line of the session's own transcript and **does not
mirror** a uuid-less line found in an agent transcript, because
`stream_meta._id` is `<sessionId>#<line:08d>` and a session directory holds many
files: line 5 of an agent transcript and line 5 of the session transcript would
key one document and one of them would vanish. Such lines are reported on
:attr:`TranscriptScan.unkeyable` and counted `unkeyable_positional` — zero on
the whole frozen corpus, asserted by `tests/test_ingest.py`. Making the table
true again is a GD-24 amendment, not a local improvisation: the `stream_meta`
row needs `<sessionId>#<fileDiscriminator>#<line:08d>` (or a `filePath` key
member) before those lines can be stored. Until then a future CLI that writes a
`mode` line into an agent transcript is counted, not mirrored, and sp-14's
acceptance should know to look for the counter.

`queue-operation` gets `render:false` and is **not** deduped against the `user`
record it becomes: they are two events (enqueue at `…374Z`, delivery at
`…444Z` in the frozen pair), and that 70 ms is the only observable queue latency
in the whole corpus. `sessionId` is injected from the path when the line has
none, and which of the two it was is recorded in `_normalized.sessionIdSource`
rather than guessed at later. `session_id` (the snake-case duplicate) is dropped
at the boundary and named in `_normalized.dropped` (SESSIONJSONL-16).

Positions are stored, not implied: every mirrored document carries `lineNo` and
`byteOffset`, because "order" in a rewritable file is a fact about a generation,
not an append order (GD-26/SD-10 — `tailer.py` signals the re-ingest, `mirror.py`
runs the sweep, and neither is this module's job).

Tokens are documents, not counters (R-50/GD-25)
-----------------------------------------------
`output_tokens` **grows** across the split records of one `message.id` (571 of
901 corpus message.ids differ between their first and last split), so first-wins
under-reports by 2.8× and `$set` is write-order dependent. One document per
`message.id`, four `$max` fields, ids `$setOnInsert`. That upsert *is* the
message-id dedup the monitoring module does in memory (GD-20's copy-verbatim
list) — expressed as a key instead of as a set.

A `message.id` never spans agents, so the stored `agentId` is written once and
never overwritten. When a second observation of the same id names a *different*
agent, that is an anomaly to count, not a value to swap: :func:`usage_conflicts`
counts them over an observation stream (a mapper is pure and stateless, so it
cannot), and :func:`read_transcript` raises `skipped["usage_agent_conflict"]`
for the within-one-file case so the anomaly has a runtime path and not only a
callable. **Handoff (sp-12/sp-15):** the CROSS-file case still has no caller —
`mirror.py` maps observations one at a time and accumulates none — so the
`/health` mirror block should call :func:`usage_conflicts` over an ingest pass
(or sum these counters across scans) before R-50's conflict count can be claimed
as surfaced installation-wide.

**And it DOES span sessions** — the sentence R-50's justification is missing,
and a stated deviation from its `$setOnInsert:{agentId, sessionId, runId}`.
Three of the live corpus's 4 738 message ids are observed under two `sessionId`s
(one agent's fragments, split across two session directories by a `/clear`
mid-run — MONGOSCHEMA-9's shape). `$setOnInsert` is first-writer-wins, so with
`sessionId` in it the stored document depends on ingest order and GD-25's
acceptance property fails on real data. `usage.sessionId` is therefore `$min`;
`agentId` and `runId` stay immutable, where R-50's "never overwrite" is the
specified semantics and the corpus agrees (0 divergent ids each). The three
counters say which is which: `usage_agent_conflict` and `usage_run_conflict` are
anomalies, `usage_session_span` is expected topology. See :func:`map_usage` for
the `$addToSet:{sessionIds}` handoff that would keep every session instead of
the earliest.

Every "N of the live corpus" figure in this file is a **measurement of a moving
corpus**: `~/.claude` is append-only and this machine keeps using the CLI, so
the denominator grows between one reading and the next (4 607, then 4 648, then
4 738 over three hours of one afternoon). The load-bearing halves are the ones that do
not drift — the sessionId numerator is ≥ 1 and the agentId/runId numerators are
0 — and no test asserts a live-corpus figure. The frozen fixtures are where
exact counts are asserted, and they are exactly why sp-02 froze them.

Deliberately not stored: `tsRaw` on a `usage` document. GD-11(g) pairs every
`ts` with the source's own spelling, and a usage document has many sources —
storing one of their spellings would name one split record as *the* source of a
value accumulated from all of them. The `ts` itself is `$min`, so it is the
earliest observation of that message and is order-independent.

Runs and run nodes (R-49)
-------------------------
The run document is created from the **first journal `started`** — a live run
has no `<runId>.json` at all (`mirror/live-run-shape/` is a frozen specimen of
exactly that), so a snapshot-first design cannot see a running run. The snapshot,
when it appears, is **back-fill**: it never overwrites an observed value with
null, its `workflowProgress` is filtered to `type=="workflow_agent"` and keyed by
`agentId` **never by `index`**, and its `agentCount` lands as
`harnessTotals.nodeCount` — display-only, never summed, never a count check
(`wf_455b348c-e17` reports `agentCount: 6` over 8 progress rows and 9 `started`
records; `len(agents) == agentCount` is never a valid assertion).

`ordinal` is GD-7 as amended: the 0-based count of preceding `started` records
with the same `key`, **in file line order** — stored, never recomputed from a
database counter (which is restart-unsafe — MONGOSCHEMA-18). The line number
itself is stored as `journalSeq`.

**One word of GD-7 is deviated from, to keep the rest of it true.** GD-7 scopes
that count to "the same `journal.jsonl`" *and* declares `agentId →
(runId,key,ordinal)` 1:1. A runId with two journals satisfies only the first,
and one exists: `wf_1a3ffcdd-c60` was killed and resumed under a new sessionId
with the same runId, so the harness opened a second journal under the new
session directory and both number their `started` records from 0. Two different
agents then key one `run_nodes` document, one is lost, and the walk order picks
which — a GD-25 failure reproduced on the live corpus, not imagined. The count
is therefore scoped to the **run**: journals ordered by path, each continuing
the previous one's count per key (:func:`_ordinal_offsets`, which states the
cost and the amendment). Single-journal runs — six of the seven live ones, and
every frozen fixture — are numbered exactly as before.

A `result` record carries `key` and `agentId` but no ordinal, and GD-7 makes
`agentId → (runId, key, ordinal)` 1:1, so a result is matched by **agentId**
first. The fallback — the oldest `started` of that `key` still without a result —
exists for the 3-key retry shape (`wf_455b348c-e17`) where a `result` could in
principle arrive with no agentId; a result that matches neither is counted
(`skipped["unmatched_result"]`), never attached to an arbitrary node.

The launch `toolUseResult` (R-49/CONVO-12)
------------------------------------------
`{status, taskId, taskType, workflowName, runId, summary, transcriptDir,
scriptPath}` on the main session's tool-result record is the ONLY deterministic
main-session→run join, and per amended GD-8 its `taskId` is the run-level stop
handle. It is stored under a namespaced `launch{}` sub-document on the `runs`
document, **and every field of it is `$min`** — see :func:`map_run` for both
halves: the snapshot is an independent observer of the same field names (so the
namespace), and one `runId` can have two launch records (so the operator —
`wf_455b348c-e17` has exactly that, taskIds `wgm4nvzgk` and `wzd027fky`, and
`$set` would let the walk order pick the stop handle).
:func:`read_launch` parses it from any transcript record;
:func:`read_transcript` collects them, and the `run` source emits them as run
observations. **No frozen fixture contains one** — sp-02 froze the two session
*subdirectories* of the run, not the top-level session transcripts that hold the
launch records — so `tests/test_ingest.py` builds the record from the verbatim
shape recorded here (`w4hiywrt6` / `wf_930e210a-6da`) rather than pretending the
corpus covers it. That gap is stated rather than hidden.

Spilled tool output (R-26, SESSIONJSONL-14)
-------------------------------------------
`toolUseResult.persistedOutputPath` does not exist (0 records on disk). The
pointer is *agent-authored text* inside a `tool_result` block::

    <persisted-output>
    Output too large (32KB). Full output saved to: <path>

so it is matched by :data:`PERSISTED_OUTPUT_RE` + :data:`PERSISTED_PATH_RE` and
then **contained**: R-26's rule is *realpath*-containment under
`<root>/projects/*/*/tool-results/`, and :func:`spill_containment` implements
that rule — root-anchored, symlinks resolved, exact shape — because the boolean
is persisted next to the agent-authored path and the next reader treats the pair
as a resolved location. An uncontained pointer is recorded with
`contained:false` and its file is never opened; with no root to resolve against
the answer is also `false` (counted `unrooted_spill`), never "probably fine".

`tool-results/` is additionally scanned as a directory, keyed
`(sessionId, basename)` (:func:`scan_tool_results`), and linked to its pointer by
:func:`link_spills`; a spill with `linkedToolUseId: None` is the "unlinked
spilled output" R-26 names — a real state (`mirror/live-run-shape/` has one
`tool-results/*.txt` whose pointer record is in a transcript the freeze cut).

**Where those spills are *not* stored, and why.** GD-24's collection table is
closed and has no `tool_results` row, and `refs.py`/`mongo_store.py` belong to
sp-05 — inventing a grammar for `(sessionId, basename)` here would be exactly the
one-file-one-owner violation this partition exists to prevent. So the pointer is
stored where it belongs (`persistedOutput` on the pointing `records` document,
which is queryable by `toolUseId`) and the directory inventory is *returned* by
:func:`scan_tool_results` — a per-*session* scan, deliberately not a field on the
per-*file* :class:`TranscriptScan`, where it would be either duplicated across
every transcript of the session or silently empty on most of them.
A future pass that wants the inventory mirrored needs one new row in GD-24's
table, and that is a plan amendment, not a local improvisation.
"""

from __future__ import annotations

import datetime
import glob as globmod
import json
import os
import re
from dataclasses import dataclass, field

from . import mongo_store as ms
from . import refs
from . import sessions as sess
from . import tailer

__all__ = [
    "IngestError",
    "PROVENANCE",
    "RECORD_TYPES",
    "KNOWN_META_TYPES",
    "NO_RENDER_TYPES",
    "COLLECTIONS",
    "TOOL_RESULTS_DIR",
    "PROJECTS_DIR",
    "PERSISTED_OUTPUT_RE",
    "PERSISTED_PATH_RE",
    "USAGE_FIELDS",
    "USAGE_IDENTITY",
    "SNAPSHOT_MAX_FIELDS",
    "PROGRESS_MAX_FIELDS",
    "STATUS_COMPANIONS",
    "RecordObservation",
    "StreamMetaObservation",
    "UsageObservation",
    "RunObservation",
    "RunNodeObservation",
    "Spill",
    "SpillPointer",
    "Launch",
    "TranscriptScan",
    "JournalScan",
    "RunScan",
    "parse_line",
    "bucket_of",
    "session_id_for_path",
    "agent_id_for_path",
    "run_id_for_path",
    "is_transcript_path",
    "is_journal_path",
    "usage_from_message",
    "tool_use_ids_of",
    "find_persisted_output",
    "spill_containment",
    "read_launch",
    "read_transcript",
    "read_journal",
    "read_snapshot",
    "read_snapshots",
    "fold_snapshots",
    "find_snapshot",
    "find_snapshots",
    "read_run",
    "scan_tool_results",
    "link_spills",
    "agent_times",
    "dedup_usage",
    "rollup",
    "rollup_pipeline",
    "usage_conflicts",
    "map_record",
    "map_stream_meta",
    "map_usage",
    "map_run",
    "map_run_node",
    "MIRROR_MAPPERS",
    "MIRROR_SOURCES",
    "iter_record_observations",
    "iter_stream_meta_observations",
    "iter_usage_observations",
    "iter_run_observations",
    "iter_run_node_observations",
    "iter_transcript_paths",
    "iter_journal_paths",
    "reset_read_cache",
]


class IngestError(ValueError):
    """A caller-side misuse: an observation this module cannot map.

    Reading never raises on content — an unparsable line, an unusable timestamp
    and a missing snapshot are all *counted* and carried (see
    :class:`TranscriptScan.skipped`). This exists for the mapping half, where a
    malformed observation is Touch's own bug and must surface before a wrong
    `_id` reaches a permanent store. `mirror.Mapper` converts it into a
    `MapperError` naming this module.
    """


# --- constants ------------------------------------------------------------

#: GD-28: everything this module writes is a mirrored harness fact.
PROVENANCE = "harness"

#: R-47's uuid-bearing set — the harness's own. Closed on purpose: a new type
#: added by a future CLI lands in `stream_meta` (positional, full fidelity)
#: rather than in a uuid-keyed collection it may have no uuid for.
RECORD_TYPES = ("user", "assistant", "system", "attachment")

#: The uuid-less types observed in the corpus, listed for readers only. This is
#: **not** a gate: R-47's rule is "every other type, plus any unknown/future
#: type", and :func:`bucket_of` implements that rule, not this tuple.
KNOWN_META_TYPES = (
    "mode", "permission-mode", "ai-title", "last-prompt", "queue-operation",
    "file-history-snapshot", "file-history-delta", "frame-link",
)

#: Mirrored with `render:false` (R-47). A `queue-operation` is the enqueue half
#: of a pair whose delivery half is an ordinary `user` record; rendering both
#: would double every queued prompt, and deduping them would destroy the only
#: observable queue latency in the corpus.
NO_RENDER_TYPES = ("queue-operation",)

#: `type` given to a line this module could not parse as a JSON object. It is
#: stored, positionally, with the parse error — GD-26: data is never dropped
#: quietly, and a run of unparsable lines is a fact about the file.
UNPARSED_TYPE = "_unparsed"

#: The only collections a mapper here may target (GD-15/SD-1). `agents` is
#: R-48's and `sessions` is R-46's; both are reachable from the same files this
#: module reads, which is exactly why the wall is structural.
COLLECTIONS = ("records", "stream_meta", "usage", "runs", "run_nodes")

TOOL_RESULTS_DIR = "tool-results"
#: The one directory under a `~/.claude` root that holds sessions. Taken from
#: `sessions.py` rather than re-spelled, so the containment anchor and the
#: discovery scope can never disagree about where projects live.
PROJECTS_DIR = sess.PROJECTS_DIR
SUBAGENTS_DIR = "subagents"
WORKFLOWS_DIR = "workflows"
JOURNAL_NAME = "journal.jsonl"

#: The four token fields, always all four, default 0 (GD-11).
USAGE_FIELDS = ("in", "out", "cached", "cache_write")

#: `message.usage` -> Touch's four names. Anything else in `usage`
#: (`service_tier`, `cache_creation{}`, `inference_geo`) is display trivia the
#: mirror does not accumulate.
_USAGE_SOURCE = {
    "in": "input_tokens",
    "out": "output_tokens",
    "cached": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
}

#: R-26's persisted-output detection, in two parts because the marker and the
#: path are on different physical lines of one agent-authored string.
PERSISTED_OUTPUT_RE = re.compile(r"^<persisted-output>")
PERSISTED_PATH_RE = re.compile(r"Full output saved to:\s*(?P<path>\S+)")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_AGENT_FILE_RE = re.compile(r"^agent-(?P<agentId>[0-9a-f]{17})\.jsonl$")
_SESSION_FILE_RE = re.compile(
    r"^(?P<sessionId>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)


def _skips() -> dict:
    """The counter set, declared once so every scan has every key.

    A missing key and a zero are the same fact to a reader and different facts
    to `dict.get`; declaring them makes "nothing was skipped" printable —
    sessions.py's rule, applied to the other half of the corpus.
    """
    return {
        "unparsable": 0,          # a line that is not a JSON object
        "no_session_id": 0,       # neither the record nor the path names one
        "bad_uuid": 0,            # a uuid-typed record whose uuid refs rejects
        "bad_ts": 0,              # an unparseable `timestamp`
        "bad_usage": 0,           # an assistant record whose usage is unusable
        "uncontained_spill": 0,   # a pointer path outside the root's tool-results/
        "unrooted_spill": 0,      # a pointer judged with no root: unknown, not safe
        "usage_agent_conflict": 0,  # one message.id under two agentIds (R-50)
        "usage_run_conflict": 0,  # …or two runIds — the other $setOnInsert id
        "usage_session_span": 0,  # …or two sessionIds: EXPECTED, see :func:`map_usage`
        "duplicate_launch": 0,    # two launch records naming ONE runId (real: 1 run)
        "multi_journal_run": 0,   # a runId with TWO journal.jsonl files (real: 1 run)
        "unmatched_result": 0,    # a journal `result` naming no started node
        "unkeyed_progress": 0,    # a snapshot row whose agentId has no node
        "no_snapshot": 0,         # a run with no <runId>.json (normal!)
        "duplicate_snapshot": 0,  # a resumed run's extra <runId>.json copies (D-02)
        "snapshot_times": 0,      # nodes whose clock came from the snapshot, not a scan
        "unkeyable_positional": 0,  # a uuid-less line in a NON-session file
    }


# --- path grammar ---------------------------------------------------------
#
# Every ownership decision a `MIRROR_SOURCES` callable makes must be made from
# the path alone (`mirror.iter_backfill_observations`' contract: five entity
# modules × N transcripts, so opening a file to find out you do not own it turns
# the walk into five full reads of the corpus).


def session_id_for_path(path):
    """The sessionId a file belongs to, from its path alone, or None.

    Two shapes, both real:

    * `…/projects/<slug>/<sessionId>.jsonl` — the session's own transcript;
    * `…/projects/<slug>/<sessionId>/subagents/**/agent-<id>.jsonl` — an agent's
      transcript, which lives *under* a directory named for the session.

    The directory form is searched from the deepest component outwards, so a
    path that contains two uuid-shaped directories resolves to the innermost —
    the one that actually names the file's owner.
    """
    text = os.fspath(path)
    match = _SESSION_FILE_RE.match(os.path.basename(text))
    if match:
        return match.group("sessionId")
    parts = os.path.normpath(text).split(os.sep)
    for name in reversed(parts[:-1]):
        if _UUID_RE.match(name):
            return name
    return None


def agent_id_for_path(path):
    """The 17-hex agentId of an `agent-<id>.jsonl` transcript, or None."""
    match = _AGENT_FILE_RE.match(os.path.basename(os.fspath(path)))
    return match.group("agentId") if match else None


def run_id_for_path(path):
    """The runId of a file under `…/subagents/workflows/<runId>/`, or None.

    Anchored on the `subagents/workflows` pair rather than on `workflows` alone:
    `<session>/workflows/<runId>.json` is the *snapshot* directory, and a rule
    that matched it too would call a snapshot's parent a runId.
    """
    parts = os.path.normpath(os.fspath(path)).split(os.sep)
    for index in range(len(parts) - 3, -1, -1):
        if parts[index] == SUBAGENTS_DIR and parts[index + 1] == WORKFLOWS_DIR:
            return parts[index + 2]
    return None


def is_transcript_path(path) -> bool:
    """True for a `.jsonl` this module ingests as *records* (never a journal)."""
    name = os.path.basename(os.fspath(path))
    if name == JOURNAL_NAME:
        return False
    return bool(_SESSION_FILE_RE.match(name) or _AGENT_FILE_RE.match(name))


def is_journal_path(path) -> bool:
    """True for a Workflow `journal.jsonl` under a `<runId>` directory."""
    return (os.path.basename(os.fspath(path)) == JOURNAL_NAME
            and run_id_for_path(path) is not None)


def _rel(root, path) -> str:
    """Root-relative, POSIX-separated — the form a stored path may take.

    An absolute path would make a rebuild's fingerprint depend on the home
    directory it ran in, and GD-25's acceptance test compares fingerprints across
    passes (sessions.py's rule, and the same reason).
    """
    if root is None:
        return os.fspath(path)
    try:
        rel = os.path.relpath(os.fspath(path), os.fspath(root))
    except ValueError:                                           # pragma: no cover
        return os.fspath(path)
    return rel.replace(os.sep, "/")


# --- line parsing ---------------------------------------------------------


def parse_line(text):
    """`(record, error)` — a dict and None, or None and a short reason.

    Never raises. A transcript is written by another process while we read it,
    and a half-written or foreign line must cost one counter, not the file.
    """
    if not text or not text.strip():
        return None, "blank"
    try:
        value = json.loads(text)
    except ValueError as exc:
        return None, f"json: {exc}"
    if not isinstance(value, dict):
        return None, f"not an object: {type(value).__name__}"
    return value, None


def bucket_of(record) -> str:
    """R-47's two-arm rule: `"records"` or `"stream_meta"`. The only decider."""
    if not isinstance(record, dict):
        return "stream_meta"
    if record.get("type") not in RECORD_TYPES:
        return "stream_meta"
    uuid = record.get("uuid")
    return "records" if isinstance(uuid, str) and _UUID_RE.match(uuid) else "stream_meta"


def tool_use_ids_of(record) -> tuple:
    """Every tool-use id this record joins on, in document order.

    Both directions of the join are collected, because both exist in the corpus
    and both are how "jump to the other half" is answered:

    * an `assistant` record's `tool_use` blocks carry `id`;
    * a `user` record's `tool_result` blocks carry `tool_use_id`.

    GD-24 indexes a single sparse `toolUseId`; a record with several gets the
    first there (so the index still finds it) and the whole list in `toolUseIds`.
    """
    message = record.get("message")
    if not isinstance(message, dict):
        return ()
    content = message.get("content")
    if not isinstance(content, list):
        return ()
    out = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        value = None
        if kind == "tool_result":
            value = block.get("tool_use_id")
        elif kind == "tool_use":
            value = block.get("id")
        if isinstance(value, str) and value and value not in out:
            out.append(value)
    return tuple(out)


def _content_texts(record):
    """Every plain-text string inside `message.content`, in document order."""
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if isinstance(content, str):
        yield None, content
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        tool_use_id = block.get("tool_use_id") if isinstance(block.get("tool_use_id"), str) else None
        value = block.get("content")
        if isinstance(value, str):
            yield tool_use_id, value
        elif isinstance(value, list):
            for inner in value:
                if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                    yield tool_use_id, inner["text"]
        elif isinstance(block.get("text"), str):
            yield tool_use_id, block["text"]


def spill_containment(path, *, root=None) -> bool:
    """True when ``path`` **realpath-resolves** inside `<root>/projects/*/*/tool-results/`.

    R-26, verbatim: *"the recorded path is agent-authored text — realpath-contain
    under `~/.claude/projects/*/*/tool-results/` only"*. Three things that rule
    demands, and all three are here:

    * a **root** — without one there is nothing to be contained *by*. A predicate
      named `contained` that is really "the parent directory happens to be called
      `tool-results`" says True for `/tmp/evil/tool-results/passwd.txt`, and the
      boolean is *persisted* on the `records` document beside the agent-authored
      path, where the next reader (sp-12's spill viewer) reads it as "resolved,
      safe to open". A security predicate weaker than its name is the defect
      class GD-27 exists to pre-empt. No root ⇒ **False**, counted by the caller
      as `unrooted_spill`: "unknown" must never read as "contained".
    * a **realpath**, not a lexical prefix: `<session>/tool-results/x` may be a
      symlink to `/etc/passwd`, and only resolution sees that. Both sides are
      resolved, so a symlinked `$TOUCH_CLAUDE_ROOT` (or a symlinked `$HOME`)
      still contains its own files.
    * the **exact shape** `projects/<slug>/<sessionId>/tool-results/<name>` —
      five components under the root, decided on the relative path's parts, so a
      deeper or shallower `tool-results/` inside the root does not qualify either.

    No file is ever opened here (`realpath` does not read the target), and the
    path is still stored verbatim: refusing containment is a label, not a drop.
    """
    if not isinstance(path, str) or not path or root is None:
        return False
    try:
        resolved = os.path.realpath(os.path.normpath(path))
        base = os.path.realpath(os.fspath(root))
        relative = os.path.relpath(resolved, base)
    except (OSError, ValueError):
        return False
    parts = relative.split(os.sep)
    return (len(parts) == 5 and parts[0] == PROJECTS_DIR
            and parts[3] == TOOL_RESULTS_DIR and ".." not in parts
            and all(parts))


@dataclass(frozen=True)
class SpillPointer:
    """A record's claim that its output was spilled to a file (R-26).

    ``session_id`` is the session of the record that *carried* the pointer, and
    it is half of :func:`link_spills`' key: SESSIONJSONL-14 keys a spill
    `(sessionId, basename)`, and a basename alone lets session A's pointer claim
    session B's file.
    """

    tool_use_id: object
    path: str
    basename: str
    contained: bool
    session_id: object = None

    def as_field(self) -> dict:
        """The `persistedOutput` sub-document, in fixed field order."""
        return {"path": self.path, "basename": self.basename,
                "contained": bool(self.contained)}


def find_persisted_output(record, *, root=None, session_id=None):
    """The :class:`SpillPointer` this record carries, or None.

    Matched on the *text* of a `tool_result` block, anchored at its start —
    `toolUseResult.persistedOutputPath` has zero occurrences on disk, and a
    substring match anywhere in a body would fire on this very docstring being
    quoted back inside a transcript (12 such false-positive files exist).

    ``root`` is the containment anchor (:func:`spill_containment`); it is
    threaded from :func:`read_transcript`, which already holds it. ``session_id``
    is the carrying record's session, kept for :func:`link_spills`' key.
    """
    for tool_use_id, text in _content_texts(record):
        if not PERSISTED_OUTPUT_RE.match(text):
            continue
        match = PERSISTED_PATH_RE.search(text)
        if not match:
            continue
        path = match.group("path")
        return SpillPointer(tool_use_id=tool_use_id, path=path,
                            basename=os.path.basename(os.path.normpath(path)),
                            contained=spill_containment(path, root=root),
                            session_id=session_id)
    return None


def usage_from_message(message):
    """`{in,out,cached,cache_write}` from a `message.usage`, or None.

    All four keys always, defaulting to 0 (GD-11): a token record with three
    keys makes "no cache reads" and "cache reads unknown" the same document.
    Non-integer values (a `null`, a float, a bool) are refused for the whole
    record rather than coerced — `$max` over a coerced 0 is a silently wrong
    total, and the caller counts the refusal.
    """
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    out = {}
    for name, source in _USAGE_SOURCE.items():
        value = usage.get(source, 0)
        if value is None:
            value = 0
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        out[name] = value
    return out


def read_launch(record):
    """The Workflow launch join on a tool-result record, or None (R-49/CONVO-12).

    Shape, verbatim from `292fc08c…jsonl:57` (`w4hiywrt6` / `wf_930e210a-6da`)::

        toolUseResult = {"status": "async_launched", "taskId": …,
                         "taskType": "local_workflow", "workflowName": …,
                         "runId": …, "summary": …, "transcriptDir": …,
                         "scriptPath": …}

    `runId` is required — without it the record names no run and there is
    nothing to join. Everything else is optional and omitted when absent, so a
    future CLI that drops a field degrades the document rather than the ingest.
    """
    result = record.get("toolUseResult")
    if not isinstance(result, dict):
        return None
    run_id = result.get("runId")
    if not isinstance(run_id, str) or not run_id:
        return None
    return Launch(
        run_id=run_id,
        task_id=_str_or_none(result.get("taskId")),
        task_type=_str_or_none(result.get("taskType")),
        workflow_name=_str_or_none(result.get("workflowName")),
        transcript_dir=_str_or_none(result.get("transcriptDir")),
        script_path=_str_or_none(result.get("scriptPath")),
        summary=_str_or_none(result.get("summary")),
        status=_str_or_none(result.get("status")),
    )


def _str_or_none(value):
    return value if isinstance(value, str) and value else None


def _int_or_none(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _epoch_ms(value):
    """A harness epoch-millisecond field as a UTC datetime, or None.

    The harness's clock, never ours (see the module docstring): `startTime`,
    `startedAt` and `queuedAt` are recorded by the CLI at the moment they
    describe. Out-of-range values are refused rather than clamped — a
    `datetime` this module cannot build is a value it does not know.
    """
    value = _int_or_none(value)
    if value is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(value / 1000.0, datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


# --- observations ---------------------------------------------------------


@dataclass(frozen=True)
class RecordObservation:
    """One uuid-bearing transcript record. Pure data (R-47)."""

    uuid: str
    session_id: str
    type: str
    line_no: int
    byte_offset: int
    body: dict
    ts: object = None
    #: The source's own spelling of :attr:`ts`, carried rather than re-derived
    #: from the parsed Date (GD-11(g); see :func:`_record_ts`).
    ts_raw: object = None
    parent_uuid: object = None
    agent_id: object = None
    tool_use_ids: tuple = ()
    session_id_source: str = "record"
    dropped_keys: tuple = ()
    spill: object = None
    source_path: object = None


@dataclass(frozen=True)
class StreamMetaObservation:
    """One uuid-less line, keyed positionally (R-47)."""

    session_id: str
    line_no: int
    byte_offset: int
    type: str
    body: object = None
    ts: object = None
    #: The source's own spelling of :attr:`ts` (GD-11(g); :func:`_record_ts`).
    ts_raw: object = None
    render: object = None
    message_id: object = None
    session_id_source: str = "record"
    #: The `sessionId` the LINE claimed, when it differs from the one the key
    #: uses. A positional `_id` is a position in *a file's* numbering, so the
    #: key always takes the path's session (see :func:`read_transcript`); the
    #: claim is preserved rather than dropped, because "this line says it
    #: belongs elsewhere" is a fact no reader can recover from the document.
    claimed_session_id: object = None
    dropped_keys: tuple = ()
    parse_error: object = None
    source_path: object = None


@dataclass(frozen=True)
class Unkeyable:
    """A uuid-less line in a file `stream_meta`'s grammar cannot address.

    Reported rather than mirrored — see :func:`read_transcript`. It carries
    everything a reader needs to show the line and everything a future fixture
    or plan amendment needs to key it.
    """

    path: str
    line_no: int
    byte_offset: int
    type: str
    reason: str
    parse_error: object = None


@dataclass(frozen=True)
class UsageObservation:
    """One `message.id`'s absolute token counts (R-50)."""

    message_id: str
    session_id: str
    tokens: dict
    agent_id: object = None
    run_id: object = None
    ts: object = None


@dataclass(frozen=True)
class RunObservation:
    """A Workflow run: journal-created, snapshot-back-filled, launch-joined (R-49).

    Two *independent* sources observe one `runId` — the journal+snapshot
    (:func:`_run_observation`) and the launch `toolUseResult`
    (:func:`_launch_scan`) — and GD-25 requires the stored document to be the
    same whichever arrives first. The fields below are the **snapshot/journal**
    half only; the launch's copy lives in :attr:`launch`, a namespaced
    sub-document, so the two sources write disjoint field sets and cannot
    contradict each other at all. See :func:`map_run`.
    """

    run_id: str
    session_ids: tuple = ()
    task_id: object = None
    workflow_name: object = None
    transcript_dir: object = None
    script_path: object = None
    status: object = None
    summary: object = None
    harness_totals: object = None
    phases: object = None
    started_at: object = None
    ended_at: object = None
    #: The launch `toolUseResult`'s own fields, stored under `launch` (R-49).
    launch: object = None
    source_path: object = None


@dataclass(frozen=True)
class RunNodeObservation:
    """One `(runId, key, ordinal)` node (R-49). `state` is deliberately absent.

    The five `harness_*`/`last_*`/`queued_at` fields are D-03's widening: they
    are the snapshot's OWN liveness statement about the agent
    (`workflowProgress[].state`, `queuedAt`, `lastProgressAt`, `lastToolName`,
    `lastToolSummary`). They are observations like every other field here —
    `harness_state` is what the harness *said*, never a verdict, and the one
    reducer still computes running/finished/unknown from `result_seen` and the
    clocks (GD-23/R-54). `last_tool_summary` is **truncated at the source** and
    is display text only: nothing may parse a marker out of it (GD-D4/
    SUBSTRATE-10).
    """

    run_id: str
    key: str
    ordinal: int
    journal_seq: object = None
    agent_id: object = None
    result_seen: bool = False
    result: object = None
    started_at: object = None
    ended_at: object = None
    label: object = None
    model: object = None
    attempt: object = None
    phase_index: object = None
    phase_title: object = None
    harness_totals: object = None
    harness_state: object = None
    queued_at: object = None
    last_progress_at: object = None
    last_tool_name: object = None
    last_tool_summary: object = None
    source_path: object = None


@dataclass(frozen=True)
class Launch:
    """The launch `toolUseResult` — the only main-session→run join (CONVO-12)."""

    run_id: str
    task_id: object = None
    task_type: object = None
    workflow_name: object = None
    transcript_dir: object = None
    script_path: object = None
    summary: object = None
    status: object = None


@dataclass(frozen=True)
class Spill:
    """One file in a session's `tool-results/`, keyed `(sessionId, basename)`."""

    session_id: str
    basename: str
    path: str
    bytes: int
    linked_tool_use_id: object = None


@dataclass
class TranscriptScan:
    """What one transcript read produced. Counters, never exceptions."""

    path: str
    session_id: object = None
    agent_id: object = None
    run_id: object = None
    records: tuple = ()
    stream_meta: tuple = ()
    usage: tuple = ()
    launches: tuple = ()
    #: Lines this scan could see but could not key — see :func:`read_transcript`.
    #: Empty on the whole frozen corpus; non-empty is a fact to surface, not an
    #: error to raise.
    unkeyable: tuple = ()
    first_ts: object = None
    last_ts: object = None
    lines: int = 0
    skipped: dict = field(default_factory=_skips)

    def observations(self):
        """`(kind, observation)` pairs — the shape `Mirror.rebuild` consumes."""
        for obs in self.records:
            yield "record", obs
        for obs in self.stream_meta:
            yield "streamMeta", obs
        for obs in self.usage:
            yield "usage", obs


@dataclass
class JournalScan:
    """A `journal.jsonl`, reduced to nodes. No timestamps live here (GD-7)."""

    path: str
    run_id: object = None
    session_id: object = None
    nodes: tuple = ()
    agent_ids: dict = field(default_factory=dict)
    lines: int = 0
    skipped: dict = field(default_factory=_skips)


@dataclass
class RunScan:
    """One run directory: the run document plus its nodes.

    ``extra_runs`` exists for the launch-record arm only: one *file* can carry
    several launch `toolUseResult`s (a session may start several runs), while a
    run *directory* is one run by construction. Keeping them in a declared field
    rather than attaching an attribute means `observations()` yields all of them
    and nothing depends on a dataclass's `__dict__`.
    """

    run: object = None
    nodes: tuple = ()
    snapshot: object = None
    extra_runs: tuple = ()
    skipped: dict = field(default_factory=_skips)

    def observations(self):
        if self.run is not None:
            yield "run", self.run
        for run in self.extra_runs:
            yield "run", run
        for node in self.nodes:
            yield "runNode", node


# --- reading: transcripts -------------------------------------------------


def read_transcript(path, *, session_id=None, root=None, lines=None) -> TranscriptScan:
    """Bucket one transcript into records / stream_meta / usage (R-47, R-50).

    ``lines`` accepts the :class:`tailer.TailLine` list a poll tick already read,
    so the live path costs O(bytes appended) and never re-reads the file
    (GD-30). With ``lines=None`` the whole file is read through
    `tailer.read_complete_lines`, which is the `--rebuild`/`--backfill` call:
    the torn tail is deferred there exactly as it is on a live tick, so a
    transcript being appended to while it is backfilled loses no line and
    fabricates none.

    ``root`` is the `~/.claude` root the file lives under. It does two jobs and
    both need it: stored paths are made relative to it (a rebuild's fingerprint
    must not depend on the home directory it ran in), and it is the anchor
    :func:`spill_containment` resolves persisted-output pointers against. With
    no ``root`` a pointer is `contained:false` and counted `unrooted_spill` —
    the honest answer, since there is nothing to be contained by.

    ``session_id`` overrides the path-derived one; the *record's* own
    `sessionId` still wins over both **for a `records` document**, and which
    source was used is recorded on every observation
    (`_normalized.sessionIdSource`). A line with neither is counted and skipped —
    GD-24 keys both harness collections by session, and a fabricated session id
    is a wrong-target write (GD-12).

    Positional keys are the exception, and the exception is load-bearing.
    ---------------------------------------------------------------------
    `stream_meta._id` is `<sessionId>#<line:08d>` (GD-24, sp-05's grammar), so it
    addresses **a line number inside one session-scoped stream**. Two things
    follow, and both are enforced here rather than left to luck:

    * the session component is the one the **path** names, never the one the
      record claims. A line's number is a position in the file it is in; keying
      it by a `sessionId` the record carries would file line 5 of one file under
      another file's numbering. A record that claims a different session keeps
      that claim in `claimedSessionId`, noted in `_normalized`.
    * a session directory holds **several** files (`<sessionId>.jsonl` plus every
      `subagents/**/agent-*.jsonl`), and they all resolve to the same session.
      Line 5 of two of them would therefore key ONE document. So a uuid-less line
      in a file that is *not* the session's own transcript is **not mirrored**:
      it is reported on :attr:`TranscriptScan.unkeyable` and counted under
      `skipped["unkeyable_positional"]`. Writing an aliasing document is strictly
      worse than reporting an unkeyable one — the alias silently destroys the
      other file's line, and neither is recoverable from the store.

      This costs nothing on the real corpus: every record in every frozen agent
      transcript is uuid-bearing, so the counter is 0 and `tests/test_ingest.py`
      asserts that it is. Storing them needs one thing this sub-plan does not
      own — a `stream_meta` grammar that includes the file — and that is a plan
      amendment to GD-24's table, not a local improvisation.

    An explicit ``session_id`` is read as the caller *naming the stream* (a
    fixture or a replay asserting which numbering these lines belong to), so it
    permits positional keys for any file.
    """
    path = os.fspath(path)
    from_path = session_id_for_path(path)
    default_session = session_id or from_path
    agent_id = agent_id_for_path(path)
    run_id = run_id_for_path(path)
    scan = TranscriptScan(path=path, session_id=default_session,
                          agent_id=agent_id, run_id=run_id)
    rel = _rel(root, path)
    positional_ok = bool(session_id) or bool(
        _SESSION_FILE_RE.match(os.path.basename(path)))

    if lines is None:
        lines = tailer.read_complete_lines(path)

    records = []
    metas = []
    usage = []
    launches = []
    unkeyable = []
    usage_ids = {}
    first_ts = None
    last_ts = None

    def positional(line, kind, body=None, ts=None, ts_raw=None, record=None,
                   parse_error=None, dropped=()):
        """Emit one `stream_meta` observation, or report it as unkeyable."""
        if not default_session:
            scan.skipped["no_session_id"] += 1
            return
        if not positional_ok:
            scan.skipped["unkeyable_positional"] += 1
            unkeyable.append(Unkeyable(
                path=rel, line_no=line.line_no, byte_offset=line.byte_offset,
                type=kind, parse_error=parse_error,
                reason="a positional _id is <sessionId>#<line>, and this file shares "
                       "its session with the session transcript and every other agent "
                       "transcript beside it — the key would alias their lines"))
            return
        claimed = (record or {}).get("sessionId")
        metas.append(StreamMetaObservation(
            session_id=default_session,
            line_no=line.line_no,
            byte_offset=line.byte_offset,
            type=kind,
            body=body,
            ts=ts,
            ts_raw=ts_raw,
            render=False if kind in NO_RENDER_TYPES else None,
            message_id=_str_or_none((record or {}).get("messageId")),
            claimed_session_id=(claimed if isinstance(claimed, str)
                                and claimed != default_session else None),
            session_id_source="path",
            dropped_keys=tuple(dropped),
            parse_error=parse_error,
            source_path=rel,
        ))

    for line in lines:
        scan.lines += 1
        record, error = parse_line(line.text)
        if record is None:
            if error == "blank":
                continue
            # Counted twice when it is also unkeyable, on purpose: "the line is
            # not JSON" and "the line cannot be keyed" are different facts and an
            # operator needs to see both.
            scan.skipped["unparsable"] += 1
            positional(line, UNPARSED_TYPE, parse_error=error[:200])
            continue

        own = record.get("sessionId")
        dropped = ("session_id",) if "session_id" in record else ()
        if isinstance(own, str) and _UUID_RE.match(own):
            record_session, source = own, "record"
        else:
            record_session, source = default_session, "path"
        if not record_session:
            scan.skipped["no_session_id"] += 1
            continue

        ts, ts_raw, ts_error = _record_ts(record)
        if ts_error:
            scan.skipped["bad_ts"] += 1
        if ts is not None:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts

        launch = read_launch(record)
        if launch is not None:
            launches.append(launch)

        if bucket_of(record) == "records":
            spill = find_persisted_output(record, root=root,
                                          session_id=record_session)
            if spill is not None and not spill.contained:
                # Two different facts, counted apart: "the pointer resolves
                # outside the root" and "there was no root to resolve against".
                # Collapsing them would make an unrooted read look like a corpus
                # full of escaping pointers, and vice versa.
                scan.skipped["unrooted_spill" if root is None
                             else "uncontained_spill"] += 1
            records.append(RecordObservation(
                uuid=record["uuid"],
                session_id=record_session,
                type=record["type"],
                line_no=line.line_no,
                byte_offset=line.byte_offset,
                body=record,
                ts=ts,
                ts_raw=ts_raw,
                parent_uuid=_str_or_none(record.get("parentUuid")),
                agent_id=_str_or_none(record.get("agentId")) or agent_id,
                tool_use_ids=tool_use_ids_of(record),
                session_id_source=source,
                dropped_keys=dropped,
                spill=spill,
                source_path=rel,
            ))
        else:
            positional(line, str(record.get("type") or UNPARSED_TYPE), body=record,
                       ts=ts, ts_raw=ts_raw, record=record, dropped=dropped)

        if record.get("type") == "assistant":
            observation = _usage_observation(record, record_session,
                                             agent_id, run_id, ts)
            if observation is None:
                if isinstance(record.get("message"), dict) and \
                        record["message"].get("usage") is not None:
                    scan.skipped["bad_usage"] += 1
            else:
                # R-50's conflict, at the one place a *stream* is visible. The
                # mapper is pure and stateless so it cannot see the second
                # observation of a `message.id`; :func:`usage_conflicts` can, but
                # nothing in the live path accumulates observations to hand it.
                # A counter no code path can raise is a silent anomaly, so the
                # in-scan case is raised here and travels on `scan.skipped`.
                #
                # All THREE identity fields are watched, not just `agentId`: the
                # counter that existed watched the one field that never diverges
                # on the corpus (agentId 0 / 4 738 ids) while the one that does
                # (sessionId, 3) was uncounted. Their meanings differ and the
                # names say so — `usage_session_span` is expected topology (an
                # agent's fragments span sessions after a `/clear`), the other
                # two are anomalies (see :func:`map_usage`).
                seen = usage_ids.setdefault(observation.message_id, {})
                for field_name, counter, value in (
                        ("agentId", "usage_agent_conflict", observation.agent_id),
                        ("runId", "usage_run_conflict", observation.run_id),
                        ("sessionId", "usage_session_span", observation.session_id)):
                    if value is None:
                        continue
                    if field_name in seen and seen[field_name] != value:
                        scan.skipped[counter] += 1
                    seen.setdefault(field_name, value)
                usage.append(observation)

    scan.records = tuple(records)
    scan.stream_meta = tuple(metas)
    scan.usage = tuple(usage)
    scan.launches = tuple(launches)
    scan.unkeyable = tuple(unkeyable)
    scan.first_ts = first_ts
    scan.last_ts = last_ts
    return scan


def _record_ts(record):
    """`(datetime | None, tsRaw | None, error | None)` from a record's `timestamp`.

    The **string** is carried out alongside the Date, not re-derived from it.
    GD-11(g) pairs `ts` with `tsRaw` precisely so the source's own spelling
    survives normalization; deriving `tsRaw` back from the parsed datetime makes
    the field claim to be the original while being this module's rendering of
    it. Lossless for the one shape the frozen corpus carries
    (`2026-07-25T14:14:59.374Z`) and lossy for every other the CLI could emit
    (`…:59Z`, `+00:00`, microseconds) — which is exactly the case the pair
    exists for.
    """
    raw = record.get("timestamp")
    if raw is None:
        return None, None, None
    try:
        return ms.ts_fields(raw)["ts"], (raw if isinstance(raw, str) else None), None
    except ms.MongoStoreError as exc:
        return None, None, str(exc)


def _usage_observation(record, session_id, agent_id, run_id, ts):
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    message_id = _str_or_none(message.get("id"))
    if message_id is None:
        return None
    tokens = usage_from_message(message)
    if tokens is None:
        return None
    return UsageObservation(
        message_id=message_id,
        session_id=session_id,
        tokens=tokens,
        agent_id=_str_or_none(record.get("agentId")) or agent_id,
        run_id=run_id,
        ts=ts,
    )


# --- reading: spills ------------------------------------------------------


def scan_tool_results(session_dir, *, session_id=None) -> tuple:
    """Every file in `<session_dir>/tool-results/`, keyed `(sessionId, basename)`.

    A directory scan rather than a record walk, because the two disagree in both
    directions and both disagreements are real: a spill whose pointer record was
    compacted away still exists on disk, and a pointer whose file the retention
    sweep removed still names it. An unreadable directory is "no spills", never
    an exception — this runs on a poll tick.
    """
    session_dir = os.fspath(session_dir)
    if session_id is None:
        name = os.path.basename(os.path.normpath(session_dir))
        session_id = name if _UUID_RE.match(name) else None
    directory = os.path.join(session_dir, TOOL_RESULTS_DIR)
    out = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return ()
    for name in names:
        full = os.path.join(directory, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        if not os.path.isfile(full):
            continue
        out.append(Spill(session_id=session_id, basename=name, path=full,
                         bytes=st.st_size))
    return tuple(out)


def link_spills(spills, pointers) -> tuple:
    """Attach each spill to the `tool_use_id` that named it (SESSIONJSONL-14).

    A spill nobody points at keeps ``linked_tool_use_id = None`` — R-26's
    "unlinked spilled output", a state to render honestly rather than an error:
    the pointer may live in a record a rewrite removed, or in a transcript
    outside the scanned set (the frozen `mirror/live-run-shape/` corpus has
    exactly that shape).

    The key is `(sessionId, basename)` — SESSIONJSONL-14's, and the one
    :func:`scan_tool_results` and :class:`Spill` both declare. A basename-only
    map is not that key: spill basenames are 9-character random ids drawn per
    session, so session A's pointer would silently claim session B's file the
    first time two sessions collide. A pointer whose session is unknown
    (`session_id=None`, a caller that built it without one) links nothing rather
    than linking everything — the join is a fact or it is absent.
    """
    by_key = {}
    for pointer in pointers:
        if pointer is None or not pointer.contained or pointer.session_id is None:
            continue
        by_key.setdefault((pointer.session_id, pointer.basename), pointer.tool_use_id)
    return tuple(
        Spill(session_id=spill.session_id, basename=spill.basename, path=spill.path,
              bytes=spill.bytes,
              linked_tool_use_id=by_key.get((spill.session_id, spill.basename)))
        for spill in spills
    )


# --- reading: journals, snapshots, runs -----------------------------------


def read_journal(path, *, run_id=None) -> JournalScan:
    """Reduce a `journal.jsonl` to nodes (R-49, GD-7 as amended).

    `ordinal` is the 0-based count of preceding `started` records with the same
    `key`, in file line order; `journalSeq` is the 1-based physical line. Both
    are *stored*: a database counter is restart-unsafe, and recomputing an
    ordinal from a partially-ingested journal is how a retry silently becomes the
    first attempt (MONGOSCHEMA-18).

    A `result` attaches to a node by **agentId** (GD-7 makes the mapping 1:1),
    falling back to the oldest un-resulted `started` of the same key. One that
    matches neither is counted, never attached: three of `wf_455b348c-e17`'s keys
    occur twice, and guessing between two ordinals is how a killed run's second
    attempt inherits the first's verdict.
    """
    path = os.fspath(path)
    run_id = run_id or run_id_for_path(path)
    scan = JournalScan(path=path, run_id=run_id,
                       session_id=session_id_for_path(path))
    nodes = {}
    order = []
    counts = {}
    by_agent = {}

    for line in tailer.read_complete_lines(path):
        scan.lines += 1
        record, error = parse_line(line.text)
        if record is None:
            if error != "blank":
                scan.skipped["unparsable"] += 1
            continue
        kind = record.get("type")
        key = _str_or_none(record.get("key"))
        agent_id = _str_or_none(record.get("agentId"))
        if kind == "started" and key is not None:
            ordinal = counts.get(key, 0)
            counts[key] = ordinal + 1
            ident = (key, ordinal)
            nodes[ident] = {
                "key": key, "ordinal": ordinal, "journal_seq": line.line_no,
                "agent_id": agent_id, "result_seen": False, "result": None,
            }
            order.append(ident)
            if agent_id:
                by_agent[agent_id] = ident
        elif kind == "result" and key is not None:
            ident = by_agent.get(agent_id) if agent_id else None
            if ident is None or ident not in nodes:
                ident = next((i for i in order
                              if i[0] == key and not nodes[i]["result_seen"]), None)
            if ident is None:
                scan.skipped["unmatched_result"] += 1
                continue
            nodes[ident]["result_seen"] = True
            nodes[ident]["result"] = record.get("result")
            if agent_id and not nodes[ident]["agent_id"]:
                nodes[ident]["agent_id"] = agent_id
                by_agent[agent_id] = ident
        elif kind == "result":
            # A `result` with no `key` at all. Every record of all five frozen
            # journals carries one, so this is unobservable today — which is
            # exactly why it needs a counter rather than a silent `continue`: a
            # lost verdict that increments nothing is invisible to `/health` and
            # to the operator, and a future CLI that stops emitting `key` on
            # results would look like a run where nothing ever finished.
            scan.skipped["unmatched_result"] += 1

    scan.nodes = tuple(nodes[ident] for ident in order)
    scan.agent_ids = {node["agent_id"]: (node["key"], node["ordinal"])
                      for node in scan.nodes if node["agent_id"]}
    return scan


def _within_scope(path, scope) -> bool:
    """Is a globbed path under one of the slug directories ``scope`` allows?

    ``scope=None`` is "no scope applied" and is the raw R-26 glob — legal for a
    tool or a test calling the finder directly, never for the ingest path, which
    always passes one (:func:`_run_scope`). The anchor is
    :func:`_scope_anchor`'s, so this is the *same* rooted test the per-path
    source arm applies: the slug directory must BE one this project owns, not
    merely be named like one.
    """
    if scope is None:
        return True
    return _scope_anchor(path) in scope


def find_snapshots(run_id, root, *, scope=None) -> tuple:
    """EVERY `<root>/projects/*/*/workflows/<runId>.json`, in path order (D-02).

    Globbed across *every session* directory on purpose (R-26): the snapshot
    lands under whichever session was current when the run **ended**, which for
    `wf_829e6f58-b2f` is not the session its journal lives in — the `/clear`
    between them moved it. Looking in the launching session finds nothing.

    R-26's clause is about **sessions**, and the `*/*` pattern spans project
    *slugs* as well — which R-25 as amended names as the one thing discovery
    must never do. The two are not in tension, because a `/clear` changes the
    sessionId and never the slug (the slug is a function of the cwd): scoping to
    the slug directories this project owns keeps R-26's cross-session reach
    intact and drops the cross-project reach nothing asked for. ``scope``
    carries those directories; :func:`read_run` always supplies it.

    **Why the plural is the primary function now.** A *resumed* run writes one
    snapshot per observing session, and the two disagree: `wf_617adbe5-42a` is
    recorded `failed`/37 agents/3.66 M tokens by the earlier copy and
    `killed`/59/4.32 M by the authoritative later one. Picking `sorted()[0]` —
    which is an ordering on session UUID, a value with no relation to time —
    served the wrong one. One of 27 on-disk run ids is duplicated today (the
    run-2 census; run-1's "7 of 27" was a miscount), and the *mechanism* recurs
    on every resume, so the count is a floor and not a corner case.
    :func:`fold_snapshots` is the answer; this function only finds them.
    """
    pattern = os.path.join(globmod.escape(os.fspath(root)), "projects", "*", "*",
                           WORKFLOWS_DIR, globmod.escape(run_id) + ".json")
    return tuple(sorted(path for path in globmod.glob(pattern)
                        if _within_scope(path, scope)))


def find_snapshot(run_id, root, *, scope=None):
    """The FIRST matching snapshot path, or None — a locator, never a choice.

    Kept because "where does this run's snapshot live" is a real question with
    a path-shaped answer (a test naming the cross-session directory asks it).
    It is deliberately NOT what :func:`read_run` calls any more: with two
    snapshots on disk the first path is an arbitrary one, and choosing between
    their *contents* is :func:`fold_snapshots`' job.
    """
    matches = find_snapshots(run_id, root, scope=scope)
    return matches[0] if matches else None


def find_run_dirs(run_id, root, *, scope=None) -> tuple:
    """Every `…/subagents/workflows/<runId>/` directory, across sessions (R-49).

    A run is **not** confined to the session that launched it: `/clear` gives the
    process a new sessionId mid-run, and the run's later agent transcripts land
    under the new session directory while the journal stays under the old one
    (`wf_829e6f58-b2f` is exactly that, and its snapshot is under the *second*
    session). Globbing the plural is what makes `sessionIds[]` a set rather than
    a guess, and what lets a node's `endedAt` come from a transcript fragment the
    journal's own directory does not contain.

    ``scope`` is the same R-25 fence :func:`find_snapshot` explains, and this
    finder has even less claim to reach past it: its results become
    `runs.sessionIds` (`$addToSet` — permanent, GD-26 forbids the delete that
    would undo it) and node `startedAt`/`endedAt`. A foreign project that
    happens to hold a directory of the same `wf_<12hex>` runId would otherwise
    contribute its sessionIds and its transcripts' clocks to this project's run.
    """
    pattern = os.path.join(globmod.escape(os.fspath(root)), "projects", "*", "*",
                           SUBAGENTS_DIR, WORKFLOWS_DIR, globmod.escape(run_id))
    return tuple(sorted(path for path in globmod.glob(pattern)
                        if os.path.isdir(path) and _within_scope(path, scope)))


def _run_scope(run_dir, root, cwd, env):
    """The slug directories a :func:`read_run` may look in, or None with no root.

    `sessions.scoped_dirs` (R-25 as amended) **plus the anchor's own slug**. The
    anchor is added rather than assumed to be in the set because ownership of it
    was already established by whoever handed the path over — the scoped walk
    (`iter_journal_paths`) or the per-path test (:func:`_in_scope`) — and a run
    directory must not become unreadable just because the caller passed a `cwd`
    that names a different project (a tool reading one specific run, a test
    rooted on a fixture slug). Every *other* slug the glob finds still has to be
    one this project owns.
    """
    if root is None:
        return None
    allowed = set(sess.scoped_dirs(sess.project_cwd(cwd, env), root))
    anchor = _scope_anchor(os.path.join(os.fspath(run_dir), JOURNAL_NAME))
    if anchor is not None:
        allowed.add(anchor)
    return allowed


#: Per-journal `{key: number of `started` records}`, memoized on the file's
#: identity. Only read for a run that has more than one `journal.jsonl`, which is
#: one run of seven on the live corpus — the cap exists so a long-lived process
#: cannot grow the memo without bound, and dropping it costs one re-read.
_JOURNAL_KEYS = {}
_JOURNAL_KEYS_CAP = 32


def _journal_key_counts(path) -> dict:
    """`{key: count of `started` records}` for one journal (see :func:`_ordinal_offsets`)."""
    ident = _identity(path)
    hit = _JOURNAL_KEYS.get(os.path.abspath(path))
    if ident is not None and hit is not None and hit[0] == ident:
        return hit[1]
    counts = {}
    for node in read_journal(path).nodes:
        counts[node["key"]] = counts.get(node["key"], 0) + 1
    if ident is not None:
        if len(_JOURNAL_KEYS) >= _JOURNAL_KEYS_CAP:
            _JOURNAL_KEYS.clear()
        _JOURNAL_KEYS[os.path.abspath(path)] = (ident, counts)
    return counts


def _ordinal_offsets(run_dir, directories, scan) -> dict:
    """Per-key ordinal offset for `run_dir`'s journal — GD-7's 1:1 clause, kept true.

    **The shape.** GD-7 as amended says two things: `ordinal` is "the 0-based
    count of preceding `started` records with the same `key` **in the same
    `journal.jsonl`**", and "`agentId → (runId,key,ordinal)` is 1:1". Those two
    clauses agree only while a runId has exactly one journal. One runId on this
    machine's live corpus has **two** — `wf_1a3ffcdd-c60`, whose driver was
    killed and resumed under a new sessionId with the same runId, so the harness
    opened a second `subagents/workflows/<runId>/journal.jsonl` under the new
    session directory. Both journals number their own `started` records from 0,
    so the two executions of one stage key collide on one `_id` and the stored
    node's `agentId`, `resultSeen` and `result` are decided by walk order: two
    different agents (`ab4eefd9d57343b46`, `a45a5c78def2f3576`), one document,
    one of them silently lost, and the `runs`/`run_nodes` fingerprint differs
    between a live tail and a `--rebuild` (GD-25, reproduced).

    **The rule.** The count's scope is the **run**, not the file: journals of one
    runId are ordered by path and each one's keys continue the count of the
    journals that sort before it. `agentId → (runId,key,ordinal)` becomes 1:1
    again, the two executions read as attempt 0 and attempt 1 of that key — which
    is what GD-7's ordinal is *for* — and no node is lost. `journalSeq` still
    names the physical line inside its own journal, and :func:`read_journal` is
    unchanged: it emits GD-7's per-file count verbatim, and the run-level
    composition happens here, where the run's file set is known.

    **Stated as a deviation (handoff to sp-15).** This departs from GD-7's "in
    the same `journal.jsonl`" wording in order to keep GD-7's own 1:1 clause and
    GD-25; the amendment the plan needs is one word — the count is per `(runId,
    key)`, over the run's journals in path order. Every single-journal run is
    numbered exactly as before (the offset is `{}`), so the `wf_455b348c-e17`
    acceptance (`0,0,0,0,0,0,1,1,1`) is untouched.

    **The honest cost.** Path order is not arrival order, and a journal that
    appears later under a lexically *earlier* session directory shifts the
    ordinals of the journals after it — the documents at the old ordinals then
    describe a numbering that no longer exists. That is the generation
    mark-and-sweep case SD-10 assigns to `mirror.py`, not something this module
    may delete (GD-26); it is counted here as `multi_journal_run` so the
    condition is visible to `/health` rather than inferred. Ordering by anything
    less brittle would need a clock (forbidden) or the file's mtime (which a copy
    or a restore changes, so a `--rebuild` could disagree with the live pass —
    the exact property this fixes).
    """
    journals = sorted({os.path.abspath(os.path.join(os.fspath(one), JOURNAL_NAME))
                       for one in directories})
    journals = [one for one in journals if os.path.isfile(one)]
    if len(journals) < 2:
        return {}
    scan.skipped["multi_journal_run"] += 1
    mine = os.path.abspath(os.path.join(os.fspath(run_dir), JOURNAL_NAME))
    offsets = {}
    for one in journals:
        if one == mine:
            break
        for key, count in _journal_key_counts(one).items():
            offsets[key] = offsets.get(key, 0) + count
    return offsets


def read_snapshot(path):
    """Parse a `<runId>.json`, or None if it is unreadable/unparsable.

    Missing is **never an error** (R-26's fourth amendment): a live run has no
    snapshot at all, and `mirror/live-run-shape/` is the frozen proof. The
    caller counts `skipped["no_snapshot"]` and carries on with the journal.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


#: The snapshot totals folded with `$max` (D-02). Monotonic by construction —
#: a resumed run only ever did *more* work than the copy written before it —
#: so the later copy losing a race with a bigger earlier one is impossible and
#: an earlier copy that saw more is still the truth.
SNAPSHOT_MAX_FIELDS = ("totalTokens", "totalToolCalls", "agentCount", "durationMs")

#: Per-`workflowProgress`-row numbers, same reasoning one level down.
PROGRESS_MAX_FIELDS = ("tokens", "toolCalls", "durationMs", "attempt")

#: Scalars that are companions of `status` and mean nothing without the copy
#: that produced them. A run recorded `failed` + `error: "…"` by its first
#: observing session and `completed` (no `error`) by the authoritative later
#: one must NOT fold to `completed` with the earlier error string still
#: attached — that is D-02's own defect, one field over. So when the winning
#: copy carries `status` and not the companion, the companion is dropped.
STATUS_COMPANIONS = ("error", "summary", "failureReason")


def _snapshot_order(snapshot, index):
    """The sort key that decides "later" for :func:`fold_snapshots`.

    `timestamp` first (the snapshot's own clock, the only recorded statement
    about when it was written), then the two totals D-02 names as the
    tie-break, then the caller's own order so the result is deterministic when
    a snapshot carries no timestamp at all.
    """
    raw = snapshot.get("timestamp")
    stamp = raw if isinstance(raw, str) else ""
    return (stamp,
            _int_or_none(snapshot.get("agentCount")) or 0,
            _int_or_none(snapshot.get("durationMs")) or 0,
            index)


def _fold_progress_rows(older, newer):
    """Merge two `workflowProgress` lists: latest-wins per row, `$max` on counts.

    Keyed by `agentId` where there is one (R-26: never by `index`, which is a
    display position that restarts per phase), and by `(type, index)` for the
    `workflow_phase` rows that have no agent. A row only the older snapshot
    knows about is KEPT: a resume can drop an agent from the live board, and
    losing its record would be the same defect as picking the wrong file.
    """
    def key_of(row):
        agent = _str_or_none(row.get("agentId"))
        if agent:
            return ("agent", agent)
        # `(type, index)` and NOT the row's position in its own list: the two
        # copies of a resumed run hold different numbers of agent rows (37 vs
        # 59 in the frozen fixture), so one phase row sits at different
        # positions in the two lists and a position-bearing key would merge it
        # as two. `fold_snapshots` is exported and its result IS `scan.snapshot`,
        # so those duplicates would outlive `_progress_by_agent`'s type filter.
        return ("row", row.get("type"), row.get("index"))

    merged = {}
    for source in (older, newer):
        rows = source if isinstance(source, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = key_of(row)
            prior = merged.get(key)
            if prior is None:
                merged[key] = dict(row)
                continue
            folded = dict(prior)
            folded.update(row)
            for name in PROGRESS_MAX_FIELDS:
                left, right = _int_or_none(prior.get(name)), _int_or_none(row.get(name))
                if left is not None and right is not None:
                    folded[name] = max(left, right)
                elif left is not None and name in row:
                    # `row` CARRIES the field and it did not parse as an int
                    # (`null`, a string, a float NaN) — `folded.update(row)`
                    # above has already replaced a good earlier number with it,
                    # so restoring is not redundant. A field simply absent from
                    # `row` needs no branch: the update leaves `prior`'s value.
                    folded[name] = left
            merged[key] = folded
    return list(merged.values())


def fold_snapshots(snapshots):
    """Several `<runId>.json` copies ⇒ ONE snapshot document (D-02).

    The rule, decided by the item and applied here and nowhere else: sort by
    :func:`_snapshot_order`, let the **latest** copy win every scalar (`status`
    above all — `killed` must not be overwritten by an earlier `failed`), and
    take `$max` over :data:`SNAPSHOT_MAX_FIELDS` so a later copy that was
    written before some accounting landed cannot shrink a total.

    Returns the single dict unchanged when there is one (so the common path is
    provably untouched), and `None` when there is nothing to fold — a run with
    no snapshot is normal, never an error (R-26's fourth amendment).
    """
    usable = [s for s in (snapshots or ()) if isinstance(s, dict)]
    if not usable:
        return None
    if len(usable) == 1:
        return usable[0]
    ordered = sorted(enumerate(usable), key=lambda pair: _snapshot_order(pair[1], pair[0]))
    folded = {}
    for _index, snapshot in ordered:
        progress = _fold_progress_rows(folded.get("workflowProgress"),
                                       snapshot.get("workflowProgress"))
        maxima = {}
        for name in SNAPSHOT_MAX_FIELDS:
            left, right = _int_or_none(folded.get(name)), _int_or_none(snapshot.get(name))
            if left is not None and right is not None:
                maxima[name] = max(left, right)
            elif left is not None and name in snapshot:
                # Present but unparseable in the later copy — see the same
                # branch in :func:`_fold_progress_rows`.
                maxima[name] = left
        # A `status` that CHANGED takes its companions with it: whatever the
        # winning copy says about `error`/`summary` is the whole truth about
        # them, including saying nothing (:data:`STATUS_COMPANIONS`).
        restated = ("status" in snapshot
                    and _str_or_none(snapshot.get("status")) != _str_or_none(folded.get("status")))
        folded.update(snapshot)
        folded.update(maxima)
        if restated:
            for name in STATUS_COMPANIONS:
                if name not in snapshot:
                    folded.pop(name, None)
        if progress:
            folded["workflowProgress"] = progress
    return folded


def read_snapshots(paths):
    """`[dict, …]` for the paths that parsed — the unparsable ones drop out."""
    out = []
    for path in paths or ():
        parsed = read_snapshot(path)
        if parsed is not None:
            out.append(parsed)
    return out


def read_run(run_dir, *, root=None, run_id=None, snapshot=None, times=True,
             cwd=None, env=None) -> RunScan:
    """One run directory ⇒ a `runs` document and its `run_nodes` (R-49).

    ``times`` reads each `agent-*.jsonl` in the directory for its first and last
    record timestamps, because the journal has none. That is O(run) I/O and it is
    the honest price of a node time: the alternative is `now()`, which is
    forbidden. Pass ``times=False`` when the caller already holds the transcripts
    (a live poll does) and will supply node times another way.

    ``snapshot`` may be a parsed snapshot dict; otherwise one is looked up under
    ``root`` and its absence is counted, not raised.

    With a ``root``, the run's **other** session directories are found too
    (:func:`find_run_dirs`) and contribute their agent transcripts' times and
    their sessionIds — scoped to this project's slugs by ``cwd``/``env`` (see
    :func:`_run_scope`). Only ``run_dir``'s own `journal.jsonl` is *reduced to
    nodes*: GD-7's ordinal is a position within one file, and merging two
    journals' records into one line order would invent an interleaving neither
    file states. The anchor is therefore the journal, always — which is exactly
    what the `runNode` source keys on, one scan per journal.

    A runId with **two** journals is real (`wf_1a3ffcdd-c60`: killed, resumed
    under a new sessionId with the same runId), and per-file numbering makes the
    two executions of a stage key collide on one `_id`. The count's scope is
    therefore the run — :func:`_ordinal_offsets` continues each journal's count
    past the journals that sort before it, so `agentId → (runId,key,ordinal)`
    stays 1:1 as GD-7 requires. Read that function for the deviation and its
    cost.

    The run's top-level `$set` fields (`status`, `summary`, `phases`, `taskId`,
    `harnessTotals`) have exactly ONE writer whatever the journal count is: they
    all come from the snapshot (:func:`_run_observation`), and both journals of a
    runId resolve the same `<runId>.json` **copies** through the same scoped
    :func:`find_snapshots` + :func:`fold_snapshots`. (D-02 made the plural the
    reader: `find_snapshot` is a locator this path deliberately no longer
    calls, because with two copies on disk the first path is an arbitrary
    one.) What the journal contributes to `runs` is
    `sessionIds` (`$addToSet`, sorted by `fingerprint`) and the node-derived
    `startedAt`/`endedAt` (`$min`/`$max`) — order-free operators, all three.
    """
    run_dir = os.fspath(run_dir)
    journal_path = os.path.join(run_dir, JOURNAL_NAME)
    run_id = run_id or run_id_for_path(journal_path) or os.path.basename(run_dir)
    journal = read_journal(journal_path, run_id=run_id)
    scan = RunScan(skipped=dict(journal.skipped))
    scope = _run_scope(run_dir, root, cwd, env)

    if snapshot is None and root is not None:
        # D-02: every copy, folded — never `matches[0]`, which orders on a
        # session UUID and so answers "which session observed the end first"
        # rather than "which copy is the truth".
        found = find_snapshots(run_id, root, scope=scope)
        if len(found) > 1:
            scan.skipped["duplicate_snapshot"] += len(found) - 1
        snapshot = fold_snapshots(read_snapshots(found))
    if snapshot is None:
        scan.skipped["no_snapshot"] += 1
    scan.snapshot = snapshot

    directories = [run_dir]
    if root is not None:
        for other in find_run_dirs(run_id, root, scope=scope):
            if os.path.abspath(other) != os.path.abspath(run_dir):
                directories.append(other)
    offsets = _ordinal_offsets(run_dir, directories, scan)
    session_ids = []
    for directory in directories:
        sid = session_id_for_path(os.path.join(directory, JOURNAL_NAME))
        if sid and sid not in session_ids:
            session_ids.append(sid)

    progress = _progress_by_agent(snapshot)
    # D-03: a snapshot states each agent's `startedAt`/`lastProgressAt` itself,
    # so when it covers every node the O(run) transcript scan buys nothing but
    # I/O. It is skipped only when the cover is TOTAL — one uncovered node
    # would otherwise silently lose its clock, and "cheaper" is not a reason to
    # publish a node with no `endedAt`. A live run has no snapshot at all,
    # which is why `agent_times` stays the live path (GD-D4) rather than
    # becoming a fallback nobody exercises.
    #
    # **Both** clocks are required, because the scan supplies both: `first` is
    # the only source of `started_at` when a `workflowProgress` row carries no
    # `startedAt`, so gating on `lastProgressAt` alone would publish a node
    # with `startedAt: null` where the pre-D-03 code had the transcript's
    # first-seen time. No live row has one without the other today; the
    # predicate does not depend on that staying true.
    covered = bool(progress) and all(
        _snapshot_clocks_cover(progress.get(node["agent_id"]))
        for node in journal.nodes)
    times_by_agent = {}
    if times and not covered:
        for directory in directories:
            for agent_id, (first, last) in agent_times(directory).items():
                known_first, known_last = times_by_agent.get(agent_id, (None, None))
                if first is not None and (known_first is None or first < known_first):
                    known_first = first
                if last is not None and (known_last is None or last > known_last):
                    known_last = last
                times_by_agent[agent_id] = (known_first, known_last)
    elif times and covered:
        scan.skipped["snapshot_times"] += len(journal.nodes)

    nodes = []
    for node in journal.nodes:
        agent_id = node["agent_id"]
        first, last = times_by_agent.get(agent_id, (None, None))
        row = progress.get(agent_id) or {}
        started = _epoch_ms(row.get("startedAt"))
        if first is not None and (started is None or first < started):
            started = first
        progressed = _epoch_ms(row.get("lastProgressAt"))
        if progressed is not None and (last is None or progressed > last):
            last = progressed
        totals = _node_totals(row)
        nodes.append(RunNodeObservation(
            run_id=run_id,
            key=node["key"],
            # GD-7's per-file count, continued across the run's other journals
            # when there are any (:func:`_ordinal_offsets`). `{}` — the only
            # shape six of the seven live runs and every frozen fixture produce —
            # leaves it exactly as `read_journal` counted it.
            ordinal=node["ordinal"] + offsets.get(node["key"], 0),
            journal_seq=node["journal_seq"],
            agent_id=agent_id,
            result_seen=node["result_seen"],
            result=node["result"],
            started_at=started,
            ended_at=last,
            label=_str_or_none(row.get("label")),
            model=_str_or_none(row.get("model")),
            attempt=_int_or_none(row.get("attempt")),
            phase_index=_int_or_none(row.get("phaseIndex")),
            phase_title=_str_or_none(row.get("phaseTitle")),
            harness_totals=totals,
            # D-03. `harness_state` is the snapshot's own word, stored beside
            # the observations and never in place of them; the two `last_*`
            # fields are the free deterministic replacement for a hand-typed
            # detail string, and `last_tool_summary` is truncated at the source.
            harness_state=_str_or_none(row.get("state")),
            queued_at=_epoch_ms(row.get("queuedAt")),
            last_progress_at=progressed,
            last_tool_name=_str_or_none(row.get("lastToolName")),
            last_tool_summary=_str_or_none(row.get("lastToolSummary")),
            source_path=_rel(root, journal_path),
        ))
    for agent_id in progress:
        if agent_id not in journal.agent_ids:
            # A snapshot row for an agent no `started` record names. It cannot
            # be keyed — `(runId, key, ordinal)` comes from the journal — so it
            # is counted rather than keyed by `index`, which is the one thing
            # R-26 says never to do (the index is a display position and is
            # reused across phases).
            scan.skipped["unkeyed_progress"] += 1

    scan.nodes = tuple(nodes)
    scan.run = _run_observation(run_id, session_ids, snapshot, nodes, root, journal_path)
    return scan


def _progress_by_agent(snapshot) -> dict:
    """`workflowProgress` rows of `type=="workflow_agent"`, keyed by agentId.

    The two `workflow_phase` rows in `wf_829e6f58-b2f`'s snapshot are phases, not
    agents; filtering on the type is what makes "all seven labels survive" true
    while the phase rows are ignored. Keyed by `agentId` and never by `index`
    (R-26): `index` is a display position, restarts at 1 per phase, and is
    reused.
    """
    out = {}
    if not isinstance(snapshot, dict):
        return out
    rows = snapshot.get("workflowProgress")
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "workflow_agent":
            continue
        agent_id = _str_or_none(row.get("agentId"))
        if agent_id:
            out[agent_id] = row
    return out


def _snapshot_clocks_cover(row) -> bool:
    """Does one `workflowProgress` row state BOTH clocks the transcript scan gives?

    D-03's short-circuit replaces `agent_times`, and that scan supplies two
    values per agent — `first` (the node's `started_at` whenever the row has no
    `startedAt` of its own) and `last` (its `ended_at`). A row that states only
    `lastProgressAt` covers one of them, so skipping the scan for it trades a
    real `startedAt` for `null`. Both, or the scan runs.
    """
    if not isinstance(row, dict):
        return False
    return (_epoch_ms(row.get("startedAt")) is not None
            and _epoch_ms(row.get("lastProgressAt")) is not None)


def _node_totals(row):
    """A node's harness-reported numbers, namespaced and display-only (GD-11(f))."""
    totals = {}
    for name, source in (("tokens", "tokens"), ("toolCalls", "toolCalls"),
                         ("durationMs", "durationMs")):
        value = _int_or_none(row.get(source))
        if value is not None:
            totals[name] = value
    return totals or None


def _run_observation(run_id, session_ids, snapshot, nodes, root, journal_path):
    """The `runs` document: journal-created, snapshot-back-filled (R-49)."""
    started = None
    ended = None
    for node in nodes:
        if node.started_at is not None and (started is None or node.started_at < started):
            started = node.started_at
        if node.ended_at is not None and (ended is None or node.ended_at > ended):
            ended = node.ended_at

    task_id = workflow_name = script_path = status = summary = None
    totals = None
    phases = None
    if isinstance(snapshot, dict):
        task_id = _str_or_none(snapshot.get("taskId"))
        workflow_name = _str_or_none(snapshot.get("workflowName"))
        script_path = _str_or_none(snapshot.get("scriptPath"))
        status = _str_or_none(snapshot.get("status"))
        summary = _str_or_none(snapshot.get("summary"))
        snap_started = _epoch_ms(snapshot.get("startTime"))
        if snap_started is not None and (started is None or snap_started < started):
            started = snap_started
        snap_ended = _snapshot_end(snapshot)
        if snap_ended is not None and (ended is None or snap_ended > ended):
            ended = snap_ended
        totals = _run_totals(snapshot)
        if isinstance(snapshot.get("phases"), list):
            phases = snapshot["phases"]

    return RunObservation(
        run_id=run_id,
        session_ids=tuple(session_ids),
        task_id=task_id,
        workflow_name=workflow_name,
        script_path=script_path,
        status=status,
        summary=summary,
        harness_totals=totals,
        phases=phases,
        started_at=started,
        ended_at=ended,
        source_path=_rel(root, journal_path),
    )


def _snapshot_end(snapshot):
    """A terminal snapshot's own `timestamp` — the run's END time, or None.

    Verified on the two `r58-replay` snapshots: a snapshot is written when the
    run finishes and carries only end-state fields, so its `timestamp` is an
    `endedAt` observation, never a `startedAt` one (that is `startTime`, an
    epoch).
    """
    raw = snapshot.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return ms.ts_fields(raw)["ts"]
    except ms.MongoStoreError:
        return None


def _run_totals(snapshot):
    """`harnessTotals` — display-only, never summed, never a count check.

    `agentCount` becomes **`nodeCount`** (SESSIONJSONL-7/GD-11(f)): the field
    counts nodes, `wf_455b348c-e17` reports 6 over 8 progress rows and 9 started
    agents, and every reading of it as "how many agents ran" is wrong. Renaming
    it at the boundary is the cheapest way to make that unreadable-as-agents.
    """
    totals = {}
    for name, source in (("totalTokens", "totalTokens"),
                         ("totalToolCalls", "totalToolCalls"),
                         ("nodeCount", "agentCount")):
        value = _int_or_none(snapshot.get(source))
        if value is not None:
            totals[name] = value
    duration = _int_or_none(snapshot.get("durationMs"))
    if duration is not None:
        totals["durationMs"] = duration
    return totals or None


def agent_times(run_dir) -> dict:
    """`{agentId: (firstTs, lastTs)}` from the transcripts in a run directory.

    The journal carries no timestamps, so this is where a node's `startedAt` and
    `endedAt` come from (R-49). Both ends are taken over *every parsable*
    timestamp rather than over the first and last lines: a torn or foreign line
    at either end would otherwise silently move a node's clock.
    """
    out = {}
    try:
        names = sorted(os.listdir(run_dir))
    except OSError:
        return out
    for name in names:
        agent_id = agent_id_for_path(name)
        if not agent_id:
            continue
        first = last = None
        for line in tailer.read_complete_lines(os.path.join(run_dir, name)):
            record, _error = parse_line(line.text)
            if record is None:
                continue
            ts, _raw, _error = _record_ts(record)
            if ts is None:
                continue
            if first is None or ts < first:
                first = ts
            if last is None or ts > last:
                last = ts
        if first is not None or last is not None:
            out[agent_id] = (first, last)
    return out


# --- rollups (R-50): sums over documents, never counters ------------------


def dedup_usage(observations) -> dict:
    """`{message_id: {in,out,cached,cache_write}}` — `max` per field.

    The in-memory twin of the `$max` upsert, and the reason both exist: the
    mirror's totals and the live view's totals must be the same number, computed
    the same way, whether or not Mongo is reachable (GD-22). `output_tokens`
    grows across the split records of one message, so `max` — not first, not
    last, not a sum.
    """
    out = {}
    for obs in observations:
        current = out.setdefault(obs.message_id, dict.fromkeys(USAGE_FIELDS, 0))
        for name in USAGE_FIELDS:
            value = obs.tokens.get(name, 0)
            if value > current[name]:
                current[name] = value
    return out


def rollup(observations, by="agentId") -> dict:
    """Token totals grouped by `agentId` / `sessionId` / `runId`.

    Deduped by `message.id` **first**, then summed — the corpus's own trap:
    summing split records naively over-counts 2.09× (115 605 vs 55 396 measured
    on one agent). Never `$inc`, never a stored counter, never
    `harnessTotals.totalTokens` (which is the harness's own display figure and
    is not substituted for a computed one — GD-11).
    """
    attribute = {"agentId": "agent_id", "sessionId": "session_id",
                 "runId": "run_id"}.get(by)
    if attribute is None:
        raise IngestError(
            f"rollup key {by!r} is not one of the three indexed grouping fields "
            f"(agentId, sessionId, runId) — GD-24 indexes exactly those")
    seen = {}
    for obs in observations:
        group = getattr(obs, attribute)
        bucket = seen.setdefault(group, {})
        current = bucket.setdefault(obs.message_id, dict.fromkeys(USAGE_FIELDS, 0))
        for name in USAGE_FIELDS:
            value = obs.tokens.get(name, 0)
            if value > current[name]:
                current[name] = value
    out = {}
    for group, messages in seen.items():
        totals = dict.fromkeys(USAGE_FIELDS, 0)
        for tokens in messages.values():
            for name in USAGE_FIELDS:
                totals[name] += tokens[name]
        out[group] = totals
    return out


def rollup_pipeline(by="agentId", match=None) -> list:
    """The `$group` aggregation that computes :func:`rollup` server-side (R-50).

    A pipeline, never `$inc` counters: the documents are already absolute and
    already deduped by `_id`, so the sum is derivable at read time and cannot
    drift from them. Returned as data (no driver call) so it is unit-testable
    with no database driver installed at all, which is GD-21's whole posture.
    """
    if by not in ("agentId", "sessionId", "runId"):
        raise IngestError(f"rollup key {by!r} is not an indexed grouping field")
    stages = []
    if match:
        stages.append({"$match": dict(match)})
    stages.append({"$group": dict(
        {"_id": f"${by}"},
        **{name: {"$sum": f"${name}"} for name in USAGE_FIELDS},
        messages={"$sum": 1},
    )})
    return stages


#: The three identity fields R-50 attaches to a `usage` document, in the order a
#: report reads best. `sessionId` is here because it is the one that actually
#: diverges (see :func:`map_usage`); leaving it out is what made the counter
#: watch only fields that never move.
USAGE_IDENTITY = ("agentId", "sessionId", "runId")


def usage_conflicts(observations) -> dict:
    """`{message_id: {field: (v1, v2, …)}}` — ids observed under two identities.

    R-50's conflict counter, per FIELD rather than per message, because the three
    fields mean three different things:

    * **agentId** — an anomaly. A `message.id` never spans agents, the stored
      value is `$setOnInsert` and is never overwritten, and this is the only
      place the disagreement is visible at all (0 of 4 738 ids on the live
      in-scope corpus).
    * **runId** — an anomaly for the same reason and by the same operator (0 of
      4 738).
    * **sessionId** — **expected**, and benign since :func:`map_usage` writes it
      with `$min` rather than `$setOnInsert`: an agent's fragments legitimately
      span two session directories when a `/clear` lands mid-run (3 of 4 738
      ids, all of them one agent under two sessions of one run). Reported so the
      topology is visible, not because something is wrong.

    Values are listed in the order they were first observed, and `None` is not a
    value — a field absent from one observation is silence, not a claim. Ids
    with a single identity are absent from the result, so `{}` means "nothing
    diverged" and is printable as such.
    """
    seen = {}
    for obs in observations:
        row = seen.setdefault(obs.message_id, {})
        for name, value in (("agentId", obs.agent_id),
                            ("sessionId", obs.session_id),
                            ("runId", obs.run_id)):
            if value is None:
                continue
            values = row.setdefault(name, [])
            if value not in values:
                values.append(value)
    out = {}
    for message, row in seen.items():
        diverged = {name: tuple(row[name]) for name in USAGE_IDENTITY
                    if len(row.get(name, ())) > 1}
        if diverged:
            out[message] = diverged
    return out


# --- mappers (SD-1: pure — no I/O, no clock, no driver) -------------------


def _only_ours(ops):
    """GD-15's wall, enforced structurally rather than by review.

    This module reads the same files `agents.py` and `sessions.py` read, so
    "don't write their collections" cannot be a convention: it is a gate every
    mapper returns through. Grouping agent records by session (SESSIONJSONL-3)
    and assembling cross-session fragments (R-48) are both unreachable from here
    because there is no code path that emits an `agents` or `sessions`
    operation.
    """
    for collection, _key, _update in ops:
        if collection not in COLLECTIONS:
            raise IngestError(
                f"ingest.py may only write {list(COLLECTIONS)}, not {collection!r} — "
                f"agent assembly is R-48's (agents.py) and session identity is "
                f"R-46's (sessions.py)"
            )
    return ops


def _as_observation(observation, cls):
    """Accept a dataclass or the plain dict a replay/fixture hands back."""
    if isinstance(observation, cls):
        return observation
    if isinstance(observation, dict):
        try:
            return cls(**observation)
        except TypeError as exc:
            raise IngestError(f"unusable {cls.__name__}: {exc}") from None
    raise IngestError(
        f"expected a {cls.__name__} or a dict, got {type(observation).__name__}")


def _normalized(obs) -> dict:
    """`_normalized` — what the boundary changed, said out loud.

    Three facts, none of them recoverable from the stored document otherwise:
    whether the `sessionId` came from the record or was injected from the path
    (8 records in one live session carry none — R-47); that the snake-case
    `session_id` duplicate was dropped (SESSIONJSONL-16, Discard 8); and, for a
    positionally-keyed line only, that the line itself claimed a *different*
    session than the one its `_id` is built from.

    That third one is an overrule, not an injection, which is why it is not
    folded into `sessionIdSource`: `stream_meta._id` is `<sessionId>#<lineNo>`,
    so the session component has to be the file's or the number means nothing
    (:func:`read_transcript`). Recording the discarded claim keeps the
    disagreement auditable — a reader can find every line whose own `sessionId`
    contradicts its stream without re-reading the transcripts.
    """
    out = {"sessionIdSource": obs.session_id_source}
    claimed = getattr(obs, "claimed_session_id", None)
    if claimed is not None:
        out["claimedSessionId"] = claimed
    if obs.dropped_keys:
        out["dropped"] = list(obs.dropped_keys)
    return out


def _split_ops(collection, doc, key, *, immutable=(), accumulate=()):
    """Turn a prepared document into GD-25's operators.

    `provenance` and the `_id`'s own components are `$setOnInsert` — they are
    the identity, they are identical in every operation that can target this
    `_id`, and `$setOnInsert` is the one operator whose payload must not vary
    (`mongo_store.op_set_on_insert`). Everything else is `$set`, whose value is a
    pure function of the record's own bytes, so it is order-independent by
    construction; `$min`/`$max` carry the fields that accumulate across
    observations.
    """
    doc = dict(doc)
    doc.pop("_id", None)
    on_insert = {}
    for name in ("provenance",) + tuple(immutable):
        if name in doc:
            on_insert[name] = doc.pop(name)
    accumulators = {}
    for name, operator in accumulate:
        if name in doc and doc[name] is not None:
            accumulators.setdefault(operator, {})[name] = doc.pop(name)
    ops = [ms.op_set_on_insert(on_insert)] if on_insert else []
    setters = {name: value for name, value in doc.items() if value is not None}
    if setters:
        ops.append(ms.op_set(setters))
    for operator, fields in accumulators.items():
        ops.append({operator: fields})
    if not ops:
        raise IngestError(f"{collection} operation for {key!r} would be empty")
    return ms.merge_ops(*ops, collection=collection)


def _ts_pair(obs) -> dict:
    """`{"ts": Date, "tsRaw": "<the source's own spelling>"}` (GD-11(g)).

    `mongo_store.ts_fields` synthesizes a `tsRaw` when handed a datetime, which
    is the right default for a caller that has only a datetime — but this module
    read the string off the line and kept it (:func:`_record_ts`), so the stored
    spelling is the file's and not this module's rendering of the file's.
    """
    fields = ms.ts_fields(obs.ts)
    raw = getattr(obs, "ts_raw", None)
    if isinstance(raw, str) and raw:
        fields["tsRaw"] = raw
    return fields


def map_record(observation):
    """`record` ⇒ one `records` upsert, `_id = <uuid>`. Pure (SD-1)."""
    obs = _as_observation(observation, RecordObservation)
    key = refs.record_key(obs.uuid)
    doc = {
        "_id": key,
        "sessionId": obs.session_id,
        "type": obs.type,
        "provenance": PROVENANCE,
        "lineNo": obs.line_no,
        "byteOffset": obs.byte_offset,
        "parentUuid": obs.parent_uuid,
        "agentId": obs.agent_id,
        "_normalized": _normalized(obs),
        "body": obs.body,
    }
    if obs.tool_use_ids:
        doc["toolUseId"] = obs.tool_use_ids[0]
        if len(obs.tool_use_ids) > 1:
            doc["toolUseIds"] = list(obs.tool_use_ids)
    if obs.ts is not None:
        doc.update(_ts_pair(obs))
    if obs.spill is not None:
        doc["persistedOutput"] = obs.spill.as_field()
    doc = {name: value for name, value in doc.items() if value is not None}

    prepared, _report = ms.prepare_document("records", doc)
    kept, _size = ms.guard_oversize("records", prepared,
                                    source_path=obs.source_path,
                                    byte_offset=obs.byte_offset)
    return _only_ours([("records", key, _split_ops("records", kept, key))])


def map_stream_meta(observation):
    """`streamMeta` ⇒ one positional `stream_meta` upsert. Pure (SD-1).

    Positional keys are legal here and nowhere else in the harness mirror: a
    renumbered file's stale documents are aliasing garbage rather than history,
    which is why `stream_meta` is GD-26's single legal scoped delete — a delete
    `mirror.py` performs, never this module.
    """
    obs = _as_observation(observation, StreamMetaObservation)
    key = refs.stream_meta_key(obs.session_id, obs.line_no)
    doc = {
        "_id": key,
        "sessionId": obs.session_id,
        "lineNo": obs.line_no,
        "type": obs.type,
        "provenance": PROVENANCE,
        "byteOffset": obs.byte_offset,
        "_normalized": _normalized(obs),
    }
    if obs.render is not None:
        doc["render"] = bool(obs.render)
    if obs.message_id is not None:
        doc["messageId"] = obs.message_id
    if obs.parse_error is not None:
        doc["parseError"] = obs.parse_error
    if obs.body is not None:
        doc["body"] = obs.body
    if obs.ts is not None:
        doc.update(_ts_pair(obs))
    doc = {name: value for name, value in doc.items() if value is not None}

    prepared, _report = ms.prepare_document("stream_meta", doc)
    kept, _size = ms.guard_oversize("stream_meta", prepared,
                                    source_path=obs.source_path,
                                    byte_offset=obs.byte_offset)
    return _only_ours([("stream_meta", key, _split_ops("stream_meta", kept, key))])


def map_usage(observation):
    """`usage` ⇒ `$max` on four fields, ids `$setOnInsert`/`$min` (R-50/GD-25).

    No `$inc` and no `$set` on the four: re-ingest is mandatory after every
    `performRemoveByUuid`, and a summed delta doubles. No `tsRaw` either — see
    the module docstring.

    **`sessionId` is `$min`, and that is a stated deviation from R-50.**
    ------------------------------------------------------------------
    R-50 says `$setOnInsert:{agentId, sessionId, runId}`, justified by "a
    `message.id` never spans **agents**". That justification is true of agents
    and says nothing about sessions — and `sessionId` demonstrably *does* span:
    over the 4 738 distinct message ids of the in-scope live corpus, three are
    observed under two different `sessionId`s, each one agent's fragments split
    across two session directories by a `/clear` mid-run (the same MONGOSCHEMA-9
    topology :func:`find_run_dirs` globs the plural for).

    `$setOnInsert` is the one operator whose payload must not vary
    (:func:`_split_ops`): it is first-writer-wins, so a varying payload makes the
    stored document depend on ingest order. With `sessionId` in it, a live tail
    (file-arrival order) and a `--rebuild` (sorted-path order) store *different*
    documents for those three ids, which fails GD-25's acceptance property
    (R-44's identical fingerprint on every pass) and R-55's wipe/rebuild
    equivalence — reproduced on real data, not hypothesised.

    So `sessionId` is written with `$min`: the earliest-sorting of the observed
    ids. Arbitrary, deterministic, order-free — which is the whole requirement.
    `agentId` and `runId` stay `$setOnInsert` because for them R-50's "never
    overwrite" IS the specified semantics and the corpus agrees (0 divergent ids
    each); :func:`usage_conflicts` reports all three, and
    `read_transcript` counts them (`usage_agent_conflict` / `usage_run_conflict`
    are anomalies, `usage_session_span` is expected).

    **Handoff (sp-05/sp-12).** Keeping *every* session a message was seen in
    wants `$addToSet:{sessionIds: …}`, and that needs `sessionIds` added to
    `SPECS["usage"].set_fields` in `mongo_store.py` so :func:`fingerprint` sorts
    the array — sp-05's file, so it is requested here rather than reached into.
    Until then `usage.sessionId` is "a session this message was seen in", and
    the full set is derivable from `records`.
    """
    obs = _as_observation(observation, UsageObservation)
    key = refs.usage_key(obs.message_id)
    on_insert = {"provenance": PROVENANCE}
    if obs.agent_id is not None:
        on_insert["agentId"] = obs.agent_id
    if obs.run_id is not None:
        on_insert["runId"] = obs.run_id
    tokens = {name: int(obs.tokens.get(name, 0)) for name in USAGE_FIELDS}
    ops = [ms.op_set_on_insert(on_insert), ms.op_max(tokens)]
    order_free = {}
    if obs.session_id is not None:
        order_free["sessionId"] = obs.session_id
    if obs.ts is not None:
        order_free["ts"] = ms.ts_fields(obs.ts)["ts"]
    if order_free:
        ops.append(ms.op_min(order_free))
    return _only_ours([("usage", key, ms.merge_ops(*ops, collection="usage"))])


def map_run(observation):
    """`run` ⇒ one `runs` upsert (R-49).

    `startedAt`/`endedAt` are `$min`/`$max` because three independent sources
    observe them (the journal's transcripts, the snapshot's `startTime`, the
    snapshot's `timestamp`) and the earliest start with the latest end is the
    only order-independent answer. `sessionIds` is `$addToSet` because a run
    genuinely spans sessions — `wf_829e6f58-b2f`'s journal is under one and its
    snapshot under another.

    **Why the launch record is namespaced, and why its fields are `$min`.**
    `runs` is the collection this module writes from the most writers, and there
    are three of them, not two:

    1. the journal+snapshot scan (:func:`_run_observation`);
    2. a launch `toolUseResult` on a main-session transcript;
    3. **another launch record naming the same `runId`.**

    (1) vs (2) is closed by the namespace: both can name `taskId`,
    `workflowName`, `scriptPath` and `summary`, so the launch's copy lands under
    `launch{}` and the two sources write disjoint field sets. `status` is safe to
    carry there for the same reason — `launch.status` is "how it started"
    (`async_launched`) and `status` is "how it ended" (`killed`), and they are no
    longer one field two writers disagree about.

    (3) is real and the namespace does not close it: `wf_455b348c-e17` has **two**
    launch records, in one transcript, with taskIds `wgm4nvzgk` and `wzd027fky`.
    Written with `$set` the stored `launch.taskId` — amended GD-8's run-level
    stop handle — would be whichever line the walk read last, so "stop this run"
    would target a taskId chosen by walk order. Each launch field is therefore
    written with `$min` on its own dotted path: the earliest-sorting observed
    value, order-free by construction and identical in the memory model and on
    mongod (a scalar comparison, never a whole-sub-document one, which is the
    comparison the two engines could disagree about). :func:`_launch_scan`
    counts the duplicates (`skipped["duplicate_launch"]`).

    **Handoff (sp-05/sp-12).** `$min` keeps one of two genuine taskIds; keeping
    both wants `$addToSet:{launchTaskIds: …}`, which needs `launchTaskIds` in
    `SPECS["runs"].set_fields` in `mongo_store.py` so :func:`fingerprint` sorts
    the array (an unsorted `$addToSet` array is itself order-dependent, so
    adding it without that entry would re-break the property this fixes).
    `mongo_store.py` is sp-05's file — requested, not reached into.
    """
    obs = _as_observation(observation, RunObservation)
    key = refs.run_key(obs.run_id)
    doc = {
        "_id": key,
        "provenance": PROVENANCE,
        "taskId": obs.task_id,
        "workflowName": obs.workflow_name,
        "transcriptDir": obs.transcript_dir,
        "scriptPath": obs.script_path,
        "status": obs.status,
        "summary": obs.summary,
        "harnessTotals": obs.harness_totals,
        "phases": obs.phases,
        "startedAt": obs.started_at,
        "endedAt": obs.ended_at,
    }
    doc = {name: value for name, value in doc.items() if value is not None}
    prepared, _report = ms.prepare_document("runs", doc)
    update = _split_ops("runs", prepared, key,
                        accumulate=(("startedAt", "$min"), ("endedAt", "$max")))
    launch = _launch_paths(obs.launch)
    if launch:
        update = ms.merge_ops(update, ms.op_min(launch), collection="runs")
    ids = [sid for sid in obs.session_ids if sid]
    update = ms.merge_ops(update, ms.op_add_to_set({"sessionIds": {"$each": ids}}),
                          collection="runs")
    return _only_ours([("runs", key, update)])


def _launch_paths(launch) -> dict:
    """`{"launch.<field>": value}` for a launch `toolUseResult` — see :func:`map_run`.

    Dotted **leaf** paths rather than one `launch` sub-document, because the
    operator is `$min` and `$min` over two whole sub-documents is a BSON
    document comparison: field-by-field in insertion order on the server, and
    canonical-text in `mongo_store`'s model. Those two orders can disagree, and
    the acceptance test compares the model's fingerprint against the server's.
    Per-leaf `$min` is a string comparison, which they cannot disagree about.
    """
    if not isinstance(launch, dict) or not launch:
        return {}
    out = {}
    for name, value in launch.items():
        if value is None:
            continue
        if not isinstance(name, str) or not name or "." in name or name.startswith("$"):
            raise IngestError(
                f"launch field {name!r} is not a plain field name — it becomes the "
                f"dotted path `launch.{name}` and mongod would read the dot as a "
                f"nesting level (GD-24: the stored shape of a field is stable)")
        out[f"launch.{name}"] = value
    return out


def map_run_node(observation):
    """`runNode` ⇒ one `run_nodes` upsert, `_id = <runId>|<key>|<ordinal>` (R-49).

    What is deliberately **not** here: `state`. GD-23 keeps no liveness and no
    verdict in a mirror document — `resultSeen` plus the two timestamps are the
    observations, and "running / finished / unknown" is computed at read time by
    the one reducer (R-54, `agents.py`). A killed run's seven resultless nodes
    are therefore stored as exactly what was seen, and render unknown/stale
    rather than running.
    """
    obs = _as_observation(observation, RunNodeObservation)
    key = refs.run_node_key(obs.run_id, obs.key, obs.ordinal)
    doc = {
        "_id": key,
        "runId": obs.run_id,
        "key": obs.key,
        "ordinal": obs.ordinal,
        "provenance": PROVENANCE,
        "journalSeq": obs.journal_seq,
        "agentId": obs.agent_id,
        "resultSeen": bool(obs.result_seen),
        "label": obs.label,
        "model": obs.model,
        "attempt": obs.attempt,
        "phaseIndex": obs.phase_index,
        "phaseTitle": obs.phase_title,
        "harnessTotals": obs.harness_totals,
        "startedAt": obs.started_at,
        "endedAt": obs.ended_at,
        # D-03's five. `$set`, not accumulators: they are absolute statements
        # of the snapshot that produced them, and the snapshot itself is
        # already folded newest-wins (D-02) before it gets here — a second
        # `$max` at this level would be folding a fold.
        "harnessState": obs.harness_state,
        "queuedAt": obs.queued_at,
        "lastProgressAt": obs.last_progress_at,
        "lastToolName": obs.last_tool_name,
        "lastToolSummary": obs.last_tool_summary,
    }
    if obs.result is not None:
        doc["result"] = obs.result
    doc = {name: value for name, value in doc.items() if value is not None}
    prepared, _report = ms.prepare_document("run_nodes", doc)
    update = _split_ops("run_nodes", prepared, key,
                        immutable=("runId", "key", "ordinal"),
                        accumulate=(("startedAt", "$min"), ("endedAt", "$max")))
    return _only_ours([("run_nodes", key, update)])


#: SD-1's registry. `mirror.discover_mappers` finds it by name.
MIRROR_MAPPERS = {
    "record": map_record,
    "streamMeta": map_stream_meta,
    "usage": map_usage,
    "run": map_run,
    "runNode": map_run_node,
}


# --- sources (the rebuild/backfill seam) ---------------------------------
#
# TWO call patterns, and they need two different memos — a single memo sized for
# one of them silently never hits on the other.
#
# `--backfill` (`mirror.iter_backfill_observations`) calls EVERY registered
# source once per `.jsonl` under `projects/**` — five entity modules × N
# transcripts — so the three transcript sources here would read each file three
# times. Those calls are CONSECUTIVE for one path, so a one-entry memo keyed on
# the file's identity collapses them back to one read. It is one entry on
# purpose: a cache that outlives its path is a cache that answers about a file
# that has since been rewritten.
#
# `--rebuild` (`mirror.iter_rebuild_observations`) calls each source over the
# WHOLE corpus before moving to the next, so the one-entry memo is always
# holding the previous file's scan and never hits. The walk-level memos below
# are the ones that serve it: one keyed on the identity of every transcript in
# scope, one on every journal. They make a rebuild read each transcript once
# (three record/streamMeta/usage sources plus the launch arm of the two run
# sources all share it) instead of five times.
#
# What is still read twice on a rebuild: `read_run(times=True)` opens each
# `agent-*.jsonl` again for its first/last record timestamp, because the journal
# has none (R-49) and the alternative is `now()`, which is forbidden. Feeding it
# the walk's timestamps instead is tempting and wrong: the walk exists only in
# the `--rebuild` arm (`path=None`), and a `--backfill` reaching `read_run` with
# one journal path has no walk at all — so the two modes would derive a node's
# `startedAt` from different file sets and R-55's wipe/rebuild equivalence would
# be false by construction. One honest extra read beats two divergent answers.

_LAST_TRANSCRIPT = {"key": None, "scan": None}
_LAST_RUN = {"key": None, "scan": None}
_TRANSCRIPT_WALK = {"key": None, "scans": None}
_RUN_WALK = {"key": None, "scans": None}


def reset_read_cache():
    """Drop the read memos (tests, and any caller that rewrote a file)."""
    _LAST_TRANSCRIPT.update({"key": None, "scan": None})
    _LAST_RUN.update({"key": None, "scan": None})
    _TRANSCRIPT_WALK.update({"key": None, "scans": None})
    _RUN_WALK.update({"key": None, "scans": None})
    _JOURNAL_KEYS.clear()


def _identity(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (os.path.abspath(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _cached_transcript(path, root):
    key = _identity(path)
    if key is not None and _LAST_TRANSCRIPT["key"] == key:
        return _LAST_TRANSCRIPT["scan"]
    scan = read_transcript(path, root=root)
    if key is not None:
        _LAST_TRANSCRIPT.update({"key": key, "scan": scan})
    return scan


def _cached_run(path, root, cwd=None, env=None):
    # The project cwd is part of the memo key, not just of the read: it decides
    # which OTHER slug directories the run may draw sessionIds and node times
    # from (:func:`_run_scope`), so two callers with different cwds are asking
    # two different questions about the same journal.
    key = (_identity(path), sess.project_cwd(cwd, env))
    if key[0] is not None and _LAST_RUN["key"] == key:
        return _LAST_RUN["scan"]
    scan = read_run(os.path.dirname(path), root=root, cwd=cwd, env=env)
    if key[0] is not None:
        _LAST_RUN.update({"key": key, "scan": scan})
    return scan


def iter_transcript_paths(root, cwd=None, *, env=None) -> list:
    """Every transcript this project owns, in a stable order.

    Scope is `sessions.scoped_dirs` — the cwd slug plus the slugs named in
    `.session-aliases`, never `projects/*` (R-25 as amended: four foreign slug
    directories exist on this machine right now, and an enumerator ingests all
    of them). Session transcripts and the agent transcripts beneath them are one
    list because both are `records` sources; `journal.jsonl` is excluded here and
    owned by :func:`iter_journal_paths`.
    """
    out = []
    for directory in sorted(sess.scoped_dirs(sess.project_cwd(cwd, env), root)):
        for base, dirnames, filenames in os.walk(directory):
            dirnames.sort()
            for name in sorted(filenames):
                path = os.path.join(base, name)
                if is_transcript_path(path):
                    out.append(path)
    return out


def iter_journal_paths(root, cwd=None, *, env=None) -> list:
    """Every `journal.jsonl` under this project's sessions, in a stable order."""
    out = []
    for directory in sorted(sess.scoped_dirs(sess.project_cwd(cwd, env), root)):
        for base, dirnames, filenames in os.walk(directory):
            dirnames.sort()
            if JOURNAL_NAME in filenames:
                path = os.path.join(base, JOURNAL_NAME)
                if is_journal_path(path):
                    out.append(path)
    return out


def _scope_anchor(path):
    """The `<root>/projects/<slug>` directory a transcript/journal path sits in.

    Two shapes, the same two :func:`session_id_for_path` knows:
    `…/<slug>/<sessionId>.jsonl` (the parent) and
    `…/<slug>/<sessionId>/subagents/**/agent-*.jsonl` or `…/journal.jsonl` (the
    parent of the innermost uuid-shaped directory). Absolute, never resolved:
    `sessions.scoped_dirs` builds its set with `abspath`, and realpath-ing one
    side of a set-membership test and not the other is how a symlinked corpus
    stops being owned by anybody.
    """
    text = os.path.abspath(os.fspath(path))
    if _SESSION_FILE_RE.match(os.path.basename(text)):
        return os.path.dirname(text)
    parts = text.split(os.sep)
    for index in range(len(parts) - 2, -1, -1):
        if _UUID_RE.match(parts[index]):
            return os.sep.join(parts[:index]) or os.sep
    return None


def _in_scope(path, cwd, root, env) -> bool:
    """Is this path inside a slug directory this project owns? (R-25 amended.)

    The per-path (`--backfill`) arm of every source below goes through here, and
    it is not an optimisation — it is the *same* ownership rule the `path=None`
    (`--rebuild`) arm gets for free from :func:`iter_transcript_paths`.
    `mirror.iter_backfill_sources` deliberately walks the whole of
    `<root>/projects` with no slug filter, so nothing upstream applies it: a
    basename-grammar test alone ("is this `<uuid>.jsonl`?") hands this module
    every foreign project's transcripts. Four such slug directories exist on
    this machine right now, and sp-02 froze them as negative fixtures.

    Getting it wrong is not undoable: the documents land in
    `touch_<sha1(repo)>`, GD-26 forbids deleting them, they are other projects'
    unredacted transcripts inside the one database GD-27's posture fences, and
    `--backfill` and `--rebuild` stop agreeing about which files exist (R-55's
    wipe/rebuild equivalence). `sessions.py`'s per-path arm applies exactly this
    test, so a scoped `records`/`usage` row also always has a `sessions`
    document to belong to.

    The test is ROOTED (`sessions.py`'s reasoning, verbatim in effect): the
    anchor must BE `<root>/projects/<slug>`, not merely be *named* like one.
    """
    anchor = _scope_anchor(path)
    if anchor is None:
        return False
    return anchor in sess.scoped_dirs(sess.project_cwd(cwd, env), root)


def _walk_key(paths):
    """A cheap identity for a whole walk: one `stat` per file, no reads.

    Every entry carries `(dev, ino, size, mtime_ns)`, so an appended-to or
    rewritten file invalidates the memo it belongs to. Stat-ing N files to avoid
    re-parsing N files is the trade this makes.
    """
    return tuple((os.fspath(one), _identity(one)) for one in paths)


def _transcript_walk(root, cwd, env):
    """Every in-scope transcript, scanned once per generation (the memo, m5)."""
    paths = iter_transcript_paths(root, cwd, env=env)
    key = (os.fspath(root), _walk_key(paths))
    if _TRANSCRIPT_WALK["key"] == key:
        return _TRANSCRIPT_WALK["scans"]
    scans = [_cached_transcript(one, root) for one in paths]
    _TRANSCRIPT_WALK.update({"key": key, "scans": scans})
    return scans


def _transcript_scans(path, cwd, root, env):
    root = sess.claude_root(env) if root is None else os.fspath(root)
    if path is not None:
        if not is_transcript_path(path):
            return []
        if not _in_scope(path, cwd, root, env):
            return []
        return [_cached_transcript(path, root)]
    return _transcript_walk(root, cwd, env)


def iter_record_observations(path=None, *, cwd=None, root=None, env=None):
    """`MIRROR_SOURCES["record"]` — see `mirror.iter_sources` for the contract."""
    return [obs for scan in _transcript_scans(path, cwd, root, env) for obs in scan.records]


def iter_stream_meta_observations(path=None, *, cwd=None, root=None, env=None):
    """`MIRROR_SOURCES["streamMeta"]`."""
    return [obs for scan in _transcript_scans(path, cwd, root, env)
            for obs in scan.stream_meta]


def iter_usage_observations(path=None, *, cwd=None, root=None, env=None):
    """`MIRROR_SOURCES["usage"]`."""
    return [obs for scan in _transcript_scans(path, cwd, root, env) for obs in scan.usage]


def _run_scans(path, cwd, root, env):
    root = sess.claude_root(env) if root is None else os.fspath(root)
    if path is not None:
        # Same rooted ownership test as the transcript arm: a journal under a
        # foreign slug is another project's run, and mirroring it writes a
        # permanent `runs`/`run_nodes` row nothing may delete (:func:`_in_scope`).
        if is_journal_path(path):
            if not _in_scope(path, cwd, root, env):
                return []
            return [_cached_run(path, root, cwd, env)]
        # A transcript may still carry the launch join, and that join is the
        # only deterministic main-session -> run edge there is (CONVO-12). It
        # produces a run document with no nodes, which is correct: the launch
        # record proves the run exists and names its taskId; the journal names
        # its nodes.
        if is_transcript_path(path):
            if not _in_scope(path, cwd, root, env):
                return []
            return [_launch_scan(_cached_transcript(path, root), root)]
        return []
    journals = iter_journal_paths(root, cwd, env=env)
    key = (os.fspath(root), sess.project_cwd(cwd, env), _walk_key(journals))
    if _RUN_WALK["key"] != key:
        _RUN_WALK.update({"key": key,
                          "scans": [_cached_run(one, root, cwd, env)
                                    for one in journals]})
    # The launch arm reuses the transcript walk's scans rather than re-reading
    # the corpus: `iter_run_observations` and `iter_run_node_observations` both
    # land here, and before the two memos a `--rebuild` walked and re-parsed
    # every transcript for each of them.
    return list(_RUN_WALK["scans"]) + [
        _launch_scan(scan, root) for scan in _transcript_walk(root, cwd, env)]


def _launch_scan(scan, root=None):
    """Run documents implied by the launch records in one transcript (R-49).

    A launch record proves a run exists and names its `taskId` — the run-level
    stop handle per amended GD-8 — without naming a single node. Several launches
    in one file is normal (a session may start several runs), so the first is the
    scan's `run` and the rest are its `extra_runs`.

    **The two invariants this function must not break**, both of them GD-25 and
    both observed on the corpus rather than imagined:

    1. *Nothing emitted here may leave the `launch{}` sub-document.* The snapshot
       is an independent observer of the same `runId` and the same field names,
       so a promoted field would be `$set` from two sources. That is also why
       `status` is safe to carry here: `launch.status` ("how it started",
       `async_launched`) and `status` ("how it ended", `killed`) are two
       questions, not one contested field.
    2. *Several launches may name ONE `runId`.* Not several runIds — that is the
       `extra_runs` case above and it is harmless. `wf_455b348c-e17` has two
       launch records in one transcript, taskIds `wgm4nvzgk` and `wzd027fky`, so
       `launch{}` is not a single-writer field either and the namespace alone
       would still leave the stop handle chosen by walk order. :func:`map_run`
       writes every launch field with `$min` for that reason; the duplicates are
       counted here so a run that was launched twice is a visible fact and not
       just a silently discarded taskId.
    """
    runs = tuple(RunObservation(
        run_id=launch.run_id,
        session_ids=(scan.session_id,) if scan.session_id else (),
        launch={
            "taskId": launch.task_id,
            "taskType": launch.task_type,
            "workflowName": launch.workflow_name,
            "transcriptDir": launch.transcript_dir,
            "scriptPath": launch.script_path,
            "summary": launch.summary,
            "status": launch.status,
        },
        source_path=_rel(root, scan.path),
    ) for launch in scan.launches)
    skipped = _skips()
    seen = set()
    for run in runs:
        if run.run_id in seen:
            skipped["duplicate_launch"] += 1
        seen.add(run.run_id)
    return RunScan(run=runs[0] if runs else None, nodes=(), extra_runs=runs[1:],
                   skipped=skipped)


def iter_run_observations(path=None, *, cwd=None, root=None, env=None):
    """`MIRROR_SOURCES["run"]` — journals, plus the launch join on transcripts."""
    out = []
    for scan in _run_scans(path, cwd, root, env):
        if scan.run is not None:
            out.append(scan.run)
        out.extend(scan.extra_runs)
    return out


def iter_run_node_observations(path=None, *, cwd=None, root=None, env=None):
    """`MIRROR_SOURCES["runNode"]` — journals only; a launch names no node."""
    if path is not None and not is_journal_path(path):
        return []
    return [node for scan in _run_scans(path, cwd, root, env) for node in scan.nodes]


#: The rebuild/backfill seam declared beside the mappers (`mirror.iter_sources`).
MIRROR_SOURCES = {
    "record": iter_record_observations,
    "streamMeta": iter_stream_meta_observations,
    "usage": iter_usage_observations,
    "run": iter_run_observations,
    "runNode": iter_run_node_observations,
}
