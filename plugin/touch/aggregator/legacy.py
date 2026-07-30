"""Legacy `events.jsonl` adapter: read-time reduction, `legacy:` mirror arm,
artifact registry (R-27 + R-51 + R-58's read-time half).

The one module that reads the monitoring module's task folders
(`.touch/local-orchestrators/<task>/`). Everything it produces is *derived* —
no stream is ever rewritten, no file under a task folder is ever written, and
`.watcher-state.json` is never read at all (GD-14; RUNSTATE-5: it contradicts
its own stream and is never closed on kill).

Two halves, separated by the SD-1 line, exactly as `sessions.py`/`ingest.py`:

* the **reading** half (:func:`discover_tasks`, :func:`read_events`,
  :func:`reduce_task`) walks task folders, parses lines and reduces them into
  the plan/node/token model the UI renders;
* the **mapping** half (:data:`MIRROR_MAPPERS`) is pure: observations in,
  `(collection, _id, update)` triples out, built only from `refs.ref_key` and
  `mongo_store`'s op vocabulary — no I/O, no clock, no database driver. GD-21
  names the two files that may import the Mongo driver and this is not one of
  them, so that package's name does not appear in this file at all —
  `tests/test_mirror.py` greps every entity module for it.

R-51 states the relationship between the two in one line: **R-27's reduction IS
the input to the Mongo arm — there is no separate migration adapter**, and
there is nothing else to migrate (CUSTOMSTATE-12: zero `state/` dirs, zero
ledgers, zero control files, zero `.touch/` exist filesystem-wide). The parse
this module performs once feeds the reduction and the mirror both.

What the mirror stores, and why positional keys are legal here
--------------------------------------------------------------
`legacy_events._id` is `legacy:<task>#<line:08d>` — **one document per line**,
including byte-identical duplicate lines (the frozen `touch-mongo-live` stream
holds two) and duplicate timestamps (measured: up to 27 per file). Content and
`ts` keys are forbidden precisely because they collapse those into one
document, and a collapsed line is a lost event.

A positional key is only safe on an append-only file, so this schema **depends
on the never-delete rule** for `events.jsonl` (CLAUDE.md: "Never delete a
finished task folder or its `events.jsonl`", MONGOSCHEMA-7) — and on that rule
**alone**. An earlier version of this paragraph also leaned on "GD-16 keeps the
streams tracked in git"; that is false since the 2026-07-27 amendment ignored
the task folders outright — and it stays false at their new location, where
`/.touch/*` ignores them and the ONE re-included subtree is
`.touch/memory/*.md` (G9, guarded by `tests/test_bootstrap.py`). No committed
copy exists to recover a renumbered stream from. The dependency is recorded
here because it is load bearing:
renumbering a stream would silently re-point every `_id` after the edit, and
unlike `stream_meta` (GD-26's single legal scoped delete) this collection has
no repair path.

Provenance never guesses (GD-28/CUSTOMSTATE-3)
----------------------------------------------
`events.jsonl` is a multi-writer file whose writer used to be inferrable only
from an event's *shape*. R-39 fixes that forward with a one-key `w` field; for
the 130-plus lines already on disk without one the rule is fixed and refuses to
guess (:func:`provenance_of`):

* a line carrying `agent` or `tokens` ⇒ ``derived`` (watcher-only shapes);
* a line carrying `title` ⇒ ``asserted`` (only `status.sh` reads `ORCH_TITLE`);
* everything else ⇒ ``unknown``, rendered "writer unknown", excluded from
  authority-filtered queries. 41 of the 320 frozen `touch-mongo-live` lines
  take this arm (12 of its first 130) and that is the honest answer, not a
  defect to paper over.

`w`, when present, **wins** over the shape rules: an explicit attribution by
the writer is evidence, and the shape rules exist only in its absence.

The re-labels, and the one rule that keeps a green run green (SD-4/R-58)
------------------------------------------------------------------------
GD-14's read-time re-labels are applied by :func:`reduce_events`, every one of
them marked ``derived_from_legacy: True`` so a reader can always tell what the
stream said from what Touch concluded:

* ``plan failed`` whose detail matches ``loop exited ->`` **and** whose stage
  agents all reached an observed terminal ⇒ **"closed — no verdict"**. This is
  the fabricated FAILED badge (RUNSTATE-4 ≡ SKILLS-1 ≡ PRODUCT-7, live
  specimens in three runs of this repo);
* an agent left ``running`` with a later sibling spawn on the same stage ⇒
  **superseded** (RUNSTATE-9's two-wave respawn);
* a run with a terminal ``orchestrator|complete`` event closes every
  non-terminal node **stale** — never ``failed``, never left running.

And SD-4's rule, which the adapter must obey or it resurrects the badge the
watcher fix removed: **conflicting terminals on the same
`(task, plan, stage='plan')` resolve last-event-wins in file order.** A later
corrective ``done`` beats an earlier fabricated ``failed``; RUNSTATE-7's
watcher-wins dedup applies ONLY to same-state duplicates. Both frozen
correction lines (`touch-full-recon:276`, `touch-mongo-live:286`) therefore
render ``done``, while `touch-repo-recon`'s genuine ``failed`` terminals
("stopped by user before completion") stay ``failed`` — they carry no
``loop exited ->`` detail and no later correction, and honesty runs in both
directions (D13).

Identity: synthesized, because the stream carries none
------------------------------------------------------
No legacy event has a run id, a task id or any correlation field (RUNSTATE-2);
one folder's stream demonstrably spans two script invocations. So:

* ``taskId`` = the folder name; ``runId`` = `basename(orch-config["wf_dir"])`
  when that file exists, else ``legacy:<task>``;
* ``ordinal`` = a per-`(plan, stage)` counter that advances on each *new* spawn,
  so a respawn wave becomes a distinct node instead of one flickering node
  (`touch-repo-recon` is the only two-wave sample in existence);
* agent ids are namespaced ``legacy:<task>:<id8>`` (GD-14's exemption from the
  17-hex validator) because the events carry a truncated 8-char id while the
  checkpoint keys the full one (RUNSTATE-3). A stream that carries BOTH widths
  for one agent — a task whose watcher was restarted onto the widened code
  mid-run — is joined by unique prefix, the 8-char form treated as display
  only, and the join is counted so it is never invisible.

Nothing in the reduction is written to Mongo as a node or an agent: GD-24 gives
the legacy arm exactly one collection (`legacy_events`) plus the artifact
registry. The reduction is derived state, and derived state is the reducer's
(GD-23, R-54, `agents.py`) — this module hands it over in memory.

The artifact registry (R-51)
----------------------------
One `custom_state_events` document of kind ``artifact`` per task-folder file:
``{taskId, kind, path, sha256, size, mtime}`` — **paths and digests only, never
bodies**, `.watcher-state.json` excluded by name. Two properties are worth
stating because they are what make a re-scan safe:

* the `_id` is `<stream>#<slot:012d>` where the slot is derived from
  ``(path, sha256)``, so re-scanning an unchanged folder produces byte-identical
  ids (the insert is a tolerated duplicate and changes nothing — GD-25), a
  changed file appends a NEW event and keeps the old one (the collection is
  append-only), and adding a file never renumbers its neighbours the way a
  positional rank would;
* every field is written with `$setOnInsert`, so this module has no code path
  that updates or deletes a `custom_state_events` document (R-52's insert-only
  posture, GD-26's no-delete rule).

The slot is not a WAL sequence and this module does not pretend otherwise: the
artifact registry is a scan, not a journal, and GD-24 gives the collection a
`(stream, seq)` grammar and no other. Ordering artifacts by `seq` is therefore
meaningless — order them by `data.custom.path`, which is what a reader wants.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from dataclasses import dataclass, field

from . import mongo_store as ms
from . import paths
from . import refs
from . import sessions as sess
from . import store

__all__ = [
    "LegacyError",
    "STATES",
    "TERMINAL_STATES",
    "DERIVED_STATES",
    "RESERVED_PLAN",
    "RESERVED_STAGES",
    "BADGE_STAGES",
    "LOOP_EXITED",
    "CLOSED_NO_VERDICT",
    "TASK_ROOT",
    "EVENTS_FILE",
    "CONFIG_FILE",
    "WATCHER_STATE_FILE",
    "NEVER_REGISTERED",
    "ARTIFACT_KINDS",
    "ARCHIVE_STATES",
    "ARCHIVE_LABELS",
    "DEFAULT_TOKEN_WINDOW",
    "MAX_RAW_CHARS",
    "provenance_of",
    "LegacyEvent",
    "parse_line",
    "read_events",
    "ArchiveLabel",
    "archive_label",
    "TaskFolder",
    "orchestrator_root",
    "read_config",
    "discover_tasks",
    "task_folder",
    "NodeState",
    "PlanState",
    "TokenRecord",
    "Reduction",
    "reduce_events",
    "reduce_task",
    "scan",
    "artifact_kind_of",
    "artifact_stream",
    "task_of_artifact_stream",
    "artifact_slot",
    "iter_artifacts",
    "LegacyEventObservation",
    "ArtifactObservation",
    "map_legacy_event",
    "map_artifact",
    "MIRROR_MAPPERS",
    "MIRROR_SOURCES",
    "iter_legacy_event_observations",
    "iter_artifact_observations",
]


class LegacyError(ValueError):
    """A refusal this module makes.

    Reading is deliberately *not* a source of these: a line of history cannot be
    fixed retroactively, so a malformed line becomes an event carrying
    ``parse_error`` and is mirrored like any other (GD-26: data is never dropped
    quietly). The exceptions are for callers — an unusable observation, a task
    name this module cannot key, a mapper handed the wrong shape — where the
    fault is Touch's own and must surface before a wrong `_id` reaches a
    permanent store.
    """


# --- vocabulary -----------------------------------------------------------

#: `monitoring.md`'s state enum. Anything else maps to `info`, never dropped
#: (RUNSTATE-16: `status.sh` validates nothing, so out-of-enum states exist).
STATES = ("queued", "running", "done", "failed", "info", "stale")

#: An *observed* result: the stream said so. `stale`/`superseded`/`closed` are
#: Touch's conclusions and are never counted as results — the difference is what
#: makes "all stage agents resulted" a question about the file rather than about
#: the re-labeller's own output.
TERMINAL_STATES = ("done", "failed")

#: Derived closes, all marked `derived_from_legacy` wherever they are applied.
DERIVED_STATES = ("stale", "superseded", "closed")

#: `monitoring.md`'s reserved plan id: a wide card the watcher writes to. Its
#: rows are run-level logs, never agent nodes (RUNSTATE-8.3).
RESERVED_PLAN = "orchestrator"

#: Reserved stages: `plan`/`complete` set a card's badge, `tokens` updates a
#: counter. None of the three is an agent node.
RESERVED_STAGES = ("plan", "complete", "tokens")
BADGE_STAGES = ("plan", "complete")
TOKEN_STAGE = "tokens"

#: The fabricated-badge signature (RUNSTATE-4): the watcher writes this detail
#: whenever a phase advances without a `passed`-shaped verdict.
LOOP_EXITED = "loop exited ->"

#: What that badge is re-labelled to (GD-14). The em dash is deliberate and is
#: the string the frontend renders verbatim (R-32: every degraded state
#: labelled).
CLOSED_NO_VERDICT = "closed — no verdict"
CLOSED_STATE = "closed"
SUPERSEDED_STATE = "superseded"
STALE_STATE = "stale"

#: Task-folder layout (RUNSTATE-13: none of these can be assumed present).
#: The root is `paths.TASKS_REL` and not a second spelling of the same two
#: components: `server.default_tasks_root` used to re-join them inline, which is
#: exactly the CM-2 failure mode `paths.py`'s docstring was written about — four
#: wrong roots and one right one, indistinguishable (LAYOUT-8).
TASK_ROOT = paths.TASKS_REL
EVENTS_FILE = "events.jsonl"
CONFIG_FILE = "orch-config.json"

#: Watcher-private, and Touch never reads it — not for state, not for a digest.
#: GD-14 states the rule; the artifact registry excludes it by name so a folder
#: scan cannot smuggle it back in through a checksum.
WATCHER_STATE_FILE = ".watcher-state.json"
NEVER_REGISTERED = frozenset({WATCHER_STATE_FILE})

#: R-51's kinds, plus the open tail every closed-looking list in this codebase
#: keeps: a file the layout does not name is registered as `other`, never
#: skipped (RUNSTATE-13: the layout is not uniform).
ARTIFACT_KINDS = ("findings", "plan", "report", "script", "config", "log", "other")

#: Top-level directory ⇒ kind. Checked before the filename rules.
_ARTIFACT_DIRS = {
    "findings": "findings",
    "plan": "plan",
    "report": "report",
    "orch-scripts": "script",
    "context": "findings",
}

#: GD-14's derived archive label: three states, plus the honest fourth for a
#: folder whose config never recorded a `wf_dir` at all. T20's unconditional
#: "archived" label is superseded by this (PLANS-5).
ARCHIVE_STATES = ("present", "archived", "foreign", "unrecorded")
ARCHIVE_LABELS = {
    "present": "live source present",
    "archived": "archived — source transcripts unavailable",
    "foreign": "foreign source — outside this installation",
    "unrecorded": "no source recorded",
}

#: RUNSTATE-12: 91 % of a legacy stream is per-delta token noise (540 of 590
#: lines in `touch-aggregator`, 236 KB for a 27-minute run). Folding keeps at
#: most one record per agent per window, carrying the LAST cumulative
#: `agent.tokens` in it — lossless, because every delta line already carries the
#: cumulative copy. Sixty seconds is the throttle; callers may widen it.
DEFAULT_TOKEN_WINDOW = 60.0

#: A line that failed to parse is stored with its bytes, capped (GD-11's 1 KB
#: detail cap applied to the one field that carries arbitrary text).
MAX_RAW_CHARS = 1024

#: The four-key token record (GD-11); a missing key is 0, never absent.
TOKEN_KEYS = ("in", "out", "cached", "cache_write")

#: GD-28. `harness` is structurally impossible here — nothing in a task folder
#: is a harness fact — and `mongo_store`'s `legacy_events` pin says so too.
PROVENANCE_DERIVED = "derived"
PROVENANCE_ASSERTED = "asserted"
PROVENANCE_UNKNOWN = "unknown"

#: The artifact registry is authored by Touch (GD-28 class 4): the *files* are
#: asserted by agents, but this document — a path, a digest, a size — is Touch's
#: own observation of them. `custom_state_events` pins the field to
#: `{asserted, touch}` and there is no code path here emitting anything else.
ARTIFACT_PROVENANCE = "touch"
ARTIFACT_KIND = "artifact"
ARTIFACT_STREAM_PREFIX = "artifact"
ARTIFACT_AUTHOR = "local"

#: `_build_event` pads `seq` to 12 digits, so a slot must stay below 10^12 for
#: lexicographic `_id` order to equal numeric order (GD-24).
ARTIFACT_SLOT_MODULUS = 10 ** 12

_AGENT_ID8_RE = re.compile(r"^[0-9a-f]{8}$")
_AGENT_ID17_RE = re.compile(r"^[0-9a-f]{17}$")
#: The GD-14 namespace, read back: `refs.legacy_agent_id`'s output, and the one
#: place the 8-hex display id can be recovered from a namespaced one.
_LEGACY_AGENT_ID_RE = re.compile(r"^legacy:.*:(?P<id8>[0-9a-f]{8})$")

#: Characters a stream id may carry unescaped (`refs._STREAM_RE`, minus the
#: structural `:` and the `%` this encoding needs for itself).
_STREAM_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._+@=,-]")
_STREAM_PCT_RE = re.compile(r"%([0-9A-Fa-f]{2})")

#: `refs.escape_stream` caps a stream id at 200 characters; the prefix and the
#: separator take nine of them.
MAX_STREAM_TASK_CHARS = 190


def provenance_of(payload, writer=None) -> str:
    """GD-28's no-guess attribution for one legacy line.

    ``writer`` is R-39's `w` key and wins when present — an explicit
    attribution is evidence. Without it the shape rules apply, in this order:
    `agent`/`tokens` ⇒ derived (watcher-only shapes), `title` ⇒ asserted (only
    `status.sh` reads `ORCH_TITLE`), everything else ⇒ unknown.

    Verified against the frozen corpus: 33/590 `touch-aggregator`,
    20/103 `touch-repo-recon`, 35/276 `touch-full-recon`, 41/320
    `touch-mongo-live` lines are unattributable, and `tests/fixtures/legacy/
    anchors.json` holds those counts so a rule change cannot pass silently.
    """
    if writer == "watcher":
        return PROVENANCE_DERIVED
    if writer == "agent":
        return PROVENANCE_ASSERTED
    # A `w` naming a writer this reader has never heard of falls through to the
    # shape rules: the key was declared additive and open, the value is retained
    # on the event either way, and inventing a fifth provenance value would only
    # produce a document the `$jsonSchema` pin rejects.
    if not isinstance(payload, dict):
        return PROVENANCE_UNKNOWN
    if "agent" in payload or "tokens" in payload:
        return PROVENANCE_DERIVED
    if "title" in payload:
        return PROVENANCE_ASSERTED
    return PROVENANCE_UNKNOWN


# --- one line -------------------------------------------------------------


@dataclass(frozen=True)
class LegacyEvent:
    """One line of an `events.jsonl`, normalized but never corrected.

    `ts` is the parsed instant and `ts_raw` the file's own spelling: the two
    writers stamp differently (`…+00:00` from `status.sh`, `…Z` from the
    watcher, the latter backdated to the journal entry) and a stream is
    append-ordered but **not** timestamp-ordered — 2, 5 and 3 measured
    inversions in the three long streams (RUNSTATE-6). Order therefore comes
    from `line_no`, never from `ts`, everywhere in this module.
    """

    task: str
    line_no: int
    ts: object = None                 # datetime | None (an unparseable ts)
    ts_raw: str = ""
    plan: str = ""
    stage: str = ""
    state: str = ""
    state_raw: str = ""
    detail: str = ""
    title: object = None
    w: object = None                  # R-39's writer attribution, if present
    provenance: str = PROVENANCE_UNKNOWN
    agent: object = None              # the watcher's per-agent sub-object
    tokens: object = None             # the event's token delta
    quiet: bool = False
    plans_total: object = None
    extra: dict = field(default_factory=dict)
    parse_error: object = None
    ts_error: object = None           # a ts that would not parse; the line stands
    raw: object = None                # kept only for a line that failed to parse
    byte_offset: int = 0

    # --- derived, all cheap and all pure ---

    @property
    def ok(self) -> bool:
        """Did the line parse as a JSON object?

        Deliberately **not** "is everything about it well-formed": a line whose
        `ts` is unreadable still says which plan and stage did what, and
        dropping it from the reduction over a timestamp would lose a real
        result. The ts failure travels as :attr:`ts_error` and is mirrored.
        """
        return self.parse_error is None

    @property
    def by_watcher(self) -> bool:
        """Which of the two writers wrote it (RUNSTATE-7's dedup needs this).

        `w` when present; otherwise the shape: only the watcher attaches an
        `agent` sub-object. Deliberately *not* "has tokens" — `status.sh` can
        carry a `tokens` field too (`ORCH_TOKENS`), and mis-crediting an agent
        line to the watcher would make watcher-wins pick the wrong detail.
        """
        if self.w in ("watcher", "agent"):
            return self.w == "watcher"
        return isinstance(self.agent, dict)

    @property
    def agent_id_raw(self) -> object:
        agent = self.agent
        if isinstance(agent, dict):
            value = agent.get("id")
            if isinstance(value, str) and value:
                return value
        return None

    def agent_ref_id(self) -> object:
        """The namespaced agent id, or ``None`` when the line names no agent.

        8-hex ⇒ GD-14's `legacy:<task>:<id8>` (exempt from the 17-hex
        validator); 17-hex ⇒ the id itself, which is what the widened watcher
        writes (`monitoring.md`); anything else ⇒ ``None``, with the raw value
        preserved on the event for display.
        """
        raw = self.agent_id_raw
        if raw is None:
            return None
        if _AGENT_ID17_RE.match(raw):
            return raw
        if _AGENT_ID8_RE.match(raw):
            try:
                return refs.legacy_agent_id(self.task, raw)
            except refs.RefError:
                # A folder name `refs` will not key (control characters, or
                # longer than the component bound). Reading history never fails
                # over identity: the node keeps `agent_id_raw` for display and
                # is marked unconventional.
                return None
        return None

    @property
    def agent_label(self) -> object:
        agent = self.agent
        if isinstance(agent, dict):
            label = agent.get("label")
            if isinstance(label, str) and label:
                return label
        return None

    @property
    def agent_tokens(self) -> object:
        """The cumulative per-agent totals the watcher carries on every delta.

        This is what makes folding lossless (RUNSTATE-12): the last line of a
        window already states the total, so nothing needs summing — and summing
        deltas is exactly the double-count GD-25 forbids.
        """
        agent = self.agent
        if isinstance(agent, dict) and isinstance(agent.get("tokens"), dict):
            return _tokens(agent["tokens"])
        return None

    @property
    def is_badge(self) -> bool:
        return self.stage in BADGE_STAGES

    @property
    def is_token(self) -> bool:
        return self.stage == TOKEN_STAGE or self.tokens is not None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_node_event(self) -> bool:
        """Rows that describe an agent node, as opposed to a card or a log.

        `orchestrator` rows are run-level logs (RUNSTATE-8.3: they carry no
        agent at all and are unattributable to any of six identically-labelled
        siblings), and the three reserved stages are card furniture.
        """
        return (self.ok and self.plan != RESERVED_PLAN
                and self.stage not in RESERVED_STAGES)


def _tokens(value) -> dict:
    """A four-key token record with zero defaults (GD-11; RUNSTATE-14).

    `/tasks` returns two different token shapes today and `monitor.html` masks
    it with `|| 0`; Touch defines the shape once, here, so no consumer has to.
    """
    out = {}
    for key in TOKEN_KEYS:
        raw = value.get(key) if isinstance(value, dict) else None
        out[key] = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
    return out


_KNOWN_KEYS = frozenset(
    ("ts", "plan", "stage", "state", "detail", "title", "w", "agent", "tokens",
     "quiet", "plans_total")
)


def parse_line(task, line_no, line, *, byte_offset=0) -> LegacyEvent:
    """One raw line ⇒ one :class:`LegacyEvent`. Never raises on content.

    A line that is not JSON, or is JSON but not an object, becomes an event
    carrying ``parse_error`` and its (capped) bytes. It still gets a document in
    `legacy_events`, because "the file had a line here that Touch could not
    read" is a fact about the run, and dropping it would renumber nothing but
    would hide it forever (GD-26).
    """
    text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
    text = text.rstrip("\n")
    base = {"task": task, "line_no": line_no, "byte_offset": byte_offset}
    try:
        payload = json.loads(text)
    except ValueError as exc:
        return LegacyEvent(parse_error=f"not JSON: {exc}", raw=text[:MAX_RAW_CHARS],
                           provenance=PROVENANCE_UNKNOWN, **base)
    if not isinstance(payload, dict):
        return LegacyEvent(parse_error=f"not a JSON object: {type(payload).__name__}",
                           raw=text[:MAX_RAW_CHARS], provenance=PROVENANCE_UNKNOWN, **base)

    writer = payload.get("w") if isinstance(payload.get("w"), str) else None
    ts_raw = payload.get("ts") if isinstance(payload.get("ts"), str) else ""
    ts = None
    ts_error = None
    if ts_raw:
        try:
            # `store.normalize_ts` is the shared parser for RUNSTATE-6's two
            # formats and raises `StoreError`, not a bare ValueError, for
            # exactly this caller (store.py says so by name).
            ts = store.normalize_ts(ts_raw)
        except store.StoreError as exc:
            ts_error = str(exc)

    state_raw = _text_field(payload, "state")
    state = state_raw if state_raw in STATES else "info"
    extra = {name: value for name, value in payload.items() if name not in _KNOWN_KEYS}
    tokens = _tokens(payload["tokens"]) if isinstance(payload.get("tokens"), dict) else None
    plans_total = payload.get("plans_total")
    if not isinstance(plans_total, int) or isinstance(plans_total, bool):
        plans_total = None

    return LegacyEvent(
        ts=ts,
        ts_raw=ts_raw,
        plan=_text_field(payload, "plan"),
        stage=_text_field(payload, "stage"),
        state=state,
        state_raw=state_raw,
        detail=_text_field(payload, "detail"),
        title=payload.get("title") if isinstance(payload.get("title"), str) else None,
        w=writer,
        provenance=provenance_of(payload, writer),
        agent=payload.get("agent") if isinstance(payload.get("agent"), dict) else None,
        tokens=tokens,
        quiet=payload.get("quiet") is True,
        plans_total=plans_total,
        extra=extra,
        ts_error=ts_error,
        **base,
    )


def _text_field(payload, name) -> str:
    value = payload.get(name)
    return value if isinstance(value, str) else ""


def read_events(path, task=None) -> tuple:
    """Parse a whole `events.jsonl` in file order. Missing file ⇒ ``()``.

    Torn-tail handling is GD-20's, copied verbatim in intent: a trailing
    fragment with no newline is only kept if it parses as a complete JSON
    object. A writer killed mid-append leaves exactly that fragment, and
    mirroring half a line under a positional `_id` would make the *next* full
    read disagree with a permanent document.
    """
    path = os.fspath(path)
    if task is None:
        task = os.path.basename(os.path.dirname(path))
    try:
        with open(path, "rb") as handle:
            blob = handle.read()
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise LegacyError(f"cannot read {path}: {exc}") from None

    events = []
    offset = 0
    lines = blob.split(b"\n")
    # A file that ends with a newline splits into a final empty element; a file
    # that does not ends with its incomplete last line. Only the second case is
    # a torn tail.
    torn = lines.pop() if lines and lines[-1] == b"" else None
    for index, raw in enumerate(lines, 1):
        offset_here, offset = offset, offset + len(raw) + 1
        if not raw.strip():
            # A blank line carries nothing to lose; the line NUMBER still
            # advances, which is what the positional `_id` depends on.
            continue
        event = parse_line(task, index, raw, byte_offset=offset_here)
        if index == len(lines) and torn is None and not event.ok:
            break                      # a torn final line: skipped, never stored
        events.append(event)
    return tuple(events)


# --- task folders ---------------------------------------------------------


def orchestrator_root(root=None, env=None) -> str:
    """Where the task folders live — :func:`paths.tasks_root`, and nothing else.

    This function is the adapter's *name* for that root, not a second resolver:
    the ladder (arg > `$ORCH_TASKS_ROOT` > `$TOUCH_LEGACY_ROOT` >
    `<project>/.touch/local-orchestrators`) lives in `paths.py` so the daemons,
    the hook, `status.sh`, `server.default_tasks_root` and this file cannot
    disagree about which tree is live (PROTOCOL-8/G10). It used to read
    `$TOUCH_LEGACY_ROOT` and *not* `$ORCH_TASKS_ROOT`, which is how one cwd
    could give the dashboard and the API two different task lists.

    Nothing here is derived from this file's location (CM-2/GD-T5): the
    directory above an installed package holds no `local-orchestrators/`, so
    the task list would be silently empty rather than wrong-and-loud. An
    explicit argument still wins over every variable, so there remains no
    cwd-shaped way to point the adapter at another checkout's history behind a
    caller's back (GD-12's wrong-target invariant).
    """
    return paths.tasks_root(root, env=env)


@dataclass(frozen=True)
class ArchiveLabel:
    """GD-14's derived archive label. Never a constant (T20's is superseded)."""

    state: str
    label: str
    path: object = None

    def as_field(self) -> dict:
        out = {"state": self.state, "label": self.label}
        if self.path:
            out["path"] = self.path
        return out


def archive_label(wf_dir, *, claude_root=None, env=None) -> ArchiveLabel:
    """Stat the configured `wf_dir` and say what the source looks like now.

    Three states plus one: **present** (the directory is there — render the full
    path), **archived** (recorded but gone: "source transcripts unavailable"),
    **foreign** (recorded outside this installation's `~/.claude` — display the
    path, never glob it: PLANS-5, and globbing a foreign tree is how one
    project's UI starts listing another's sessions), and **unrecorded** for a
    task folder whose config never named one.

    Foreignness is decided from the path alone, before the stat: a path outside
    the configured root cannot be joined to any session this installation knows,
    whether or not it happens to exist.
    """
    if not wf_dir:
        return ArchiveLabel("unrecorded", ARCHIVE_LABELS["unrecorded"])
    path = os.fspath(wf_dir)
    root = os.path.abspath(sess.claude_root(env) if claude_root is None else claude_root)
    absolute = os.path.abspath(path)
    if absolute != root and not absolute.startswith(root + os.sep):
        return ArchiveLabel("foreign", ARCHIVE_LABELS["foreign"], path)
    state = "present" if os.path.isdir(absolute) else "archived"
    return ArchiveLabel(state, ARCHIVE_LABELS[state], path)


@dataclass(frozen=True)
class TaskFolder:
    """One directory under `local-orchestrators/`, as found on disk.

    ``kind`` is RUNSTATE-13's distinction, and it is the reason `controls` is a
    field rather than a rendering decision: `touch-monitor-spawn` holds a plan
    and nothing else — no stream, no config, no scripts — and today's
    `/tasks` lists it as a real task with `status:"empty"`. A folder with no
    stream is "plan only / never run" and offers no join/pause/stop.
    """

    task: str
    path: str
    kind: str                          # "run" | "plan-only"
    events_path: object = None
    config_path: object = None
    wf_dir: object = None
    run_id: str = ""
    archive: object = None
    config_error: object = None

    @property
    def controls(self) -> bool:
        return self.kind == "run"


def read_config(path):
    """`orch-config.json` ⇒ `(dict, error)`. Tolerant by design.

    A missing file is not an error (RUNSTATE-13: it is simply absent from some
    folders); an unreadable or malformed one yields `({}, "<why>")` so the task
    still renders with a synthesized `runId` and a visible reason.
    """
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8", "replace"))
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as exc:
        return {}, str(exc)
    if not isinstance(data, dict):
        return {}, f"not a JSON object: {type(data).__name__}"
    return data, None


def task_folder(path, *, claude_root=None, env=None) -> TaskFolder:
    """Describe one task directory without reading its stream."""
    path = os.path.abspath(os.fspath(path))
    task = os.path.basename(path)
    events_path = os.path.join(path, EVENTS_FILE)
    config_path = os.path.join(path, CONFIG_FILE)
    has_events = os.path.isfile(events_path)
    config, error = read_config(config_path) if os.path.isfile(config_path) else ({}, None)
    wf_dir = config.get("wf_dir") if isinstance(config.get("wf_dir"), str) else None
    return TaskFolder(
        task=task,
        path=path,
        kind="run" if has_events else "plan-only",
        events_path=events_path if has_events else None,
        config_path=config_path if os.path.isfile(config_path) else None,
        wf_dir=wf_dir,
        # RUNSTATE-2: the stream carries no run id, so one is synthesized —
        # from the config when it names a `wf_dir`, else from the folder name.
        run_id=os.path.basename(wf_dir.rstrip("/")) if wf_dir else f"legacy:{task}",
        archive=archive_label(wf_dir, claude_root=claude_root, env=env),
        config_error=error,
    )


def discover_tasks(root=None, *, env=None, claude_root=None) -> tuple:
    """Every task folder under the orchestrator root, sorted by name.

    Tolerant of the things that are actually there: a file among the
    directories, an unreadable directory, a `lost+found`. A missing root is
    ``()`` — a checkout with no orchestration history is not an error.
    """
    base = orchestrator_root(root, env)
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return ()
    folders = []
    for name in entries:
        path = os.path.join(base, name)
        if not os.path.isdir(path) or name.startswith("."):
            continue
        folders.append(task_folder(path, claude_root=claude_root, env=env))
    return tuple(folders)


# --- the reduction --------------------------------------------------------


@dataclass
class NodeState:
    """One agent node, keyed `(runId, <plan>/<stage>, ordinal)`.

    Never keyed on the label (all six parallel researchers in `touch-aggregator`
    carry `"research #1"`) and never on `(plan, stage)` alone (two agents share
    `v0task` in `touch-repo-recon`, 9 minutes apart) — RUNSTATE-8. The ordinal
    is what separates respawn waves; the label is display only.
    """

    task: str
    run_id: str
    plan: str
    stage: str
    ordinal: int
    agent_id: object = None
    agent_id_raw: object = None
    #: Every distinct namespaced id this node was ever observed under, in
    #: observation order. Normally one; two when a stream carries both id widths
    #: for one agent (RUNSTATE-3), which is what :func:`_join_id_widths` reads.
    agent_ids: tuple = ()
    label: str = ""
    agent_label: object = None
    state: str = "running"
    detail: str = ""
    agent_detail: object = None       # RUNSTATE-7: the agent's own words, kept
    started: object = None
    ended: object = None
    tokens: object = None
    line_nos: tuple = ()
    result_line: object = None
    derived_from_legacy: bool = False
    relabel: object = None
    unconventional: bool = False
    flags: tuple = ()

    @property
    def key(self) -> str:
        return f"{self.plan}/{self.stage}"

    @property
    def resulted(self) -> bool:
        """Did the *stream* say how this node ended? Derived closes do not count."""
        return self.result_line is not None

    def ref(self) -> dict:
        return {"runId": self.run_id, "key": self.key, "ordinal": self.ordinal}

    def ref_id(self) -> str:
        return refs.run_node_key(self.run_id, self.key, self.ordinal)


@dataclass
class PlanState:
    """One card. ``badge`` is what the UI paints; ``label`` is what it writes."""

    plan: str
    title: object = None
    badge: object = None
    label: object = None
    detail: str = ""
    agent_detail: object = None
    badge_line: object = None
    derived_from_legacy: bool = False
    relabel: object = None
    plans_total: object = None
    terminals: tuple = ()             # (line_no, state) in file order
    conflicting: tuple = ()           # (line_no, state) beaten by a later one
    duplicates: int = 0


@dataclass(frozen=True)
class TokenRecord:
    """One folded token observation — cumulative when it can be (GD-25).

    `absolute` says which of the two it is, because a reader cannot tell from
    the numbers and the two fold differently:

    * ``absolute=True`` — `tokens` is the agent's cumulative running total as
      of `line_no`. Two such records for the same agent are two observations of
      one quantity: fold them **latest-wins**; adding them double-counts.
    * ``absolute=False`` — no cumulative could be attributed to an agent, so
      `tokens` is that line's own **delta**, and nothing else *in*
      :class:`Reduction`.tokens states it (the fold only sees token-stage
      lines — see :func:`_fold_tokens`). Two such records are two separate
      quantities: **sum** them. Latest-wins collapses them (they share
      plan/stage/agent/label), which is exactly the 1.65 % shortfall
      `tests/test_legacy.py` now pins.

    `absolute` is `(cumulative is not None and agent_id is not None)`, so
    `absolute == (agent_id is not None)` holds by construction and a reader that
    can see only `agentId` — every browser client, because `server.py`'s token
    payload is an explicit seven-field dict — still gets the right answer.

    Recorded follow-up (M15/PRIOR-ART-TOUCH-5 style: note, do not implement
    here): that payload should gain ``"absolute": t.absolute``. It is
    schema-additive, old clients ignore an unknown key, and it would let
    `rollupList` branch on the fact instead of on a proxy for it.

    Additive and defaulted: an older caller that constructs a record without it
    gets the cumulative reading, which is what every folded record is.
    """

    task: str
    plan: str
    stage: str
    agent_id: object
    label: object
    ts: object
    line_no: int
    tokens: dict
    window: object = None
    folded: int = 1
    absolute: bool = True


@dataclass
class Reduction:
    """What one task folder means, derived at read time and never written back."""

    task: str
    task_id: str
    run_id: str
    kind: str = "run"
    archive: object = None
    events: tuple = ()
    plans: dict = field(default_factory=dict)
    nodes: tuple = ()
    logs: tuple = ()
    tokens: tuple = ()
    stats: dict = field(default_factory=dict)
    notes: tuple = ()

    def badge_of(self, plan):
        state = self.plans.get(plan)
        return None if state is None else state.badge

    def nodes_of(self, plan):
        return tuple(node for node in self.nodes if node.plan == plan)

    def observations(self, source_path=""):
        """`(kind, observation)` pairs — the shape `Mirror.rebuild` consumes.

        Only the verbatim lines: the reduction itself is derived state and
        belongs to the reducer (GD-23), not to a mirror collection.
        """
        for event in self.events:
            yield "legacyEvent", LegacyEventObservation.from_event(
                event, source_path=source_path)


def _new_stats() -> dict:
    return {
        "lines": 0,
        "parse_errors": 0,
        "unattributable": 0,
        "nodes": 0,
        "plans": 0,
        "token_lines": 0,
        "token_records": 0,
        "folded": 0,
        "deduped_terminals": 0,
        "conflicting_terminals": 0,
        "relabel_closed": 0,
        "relabel_superseded": 0,
        "relabel_stale": 0,
        "prefix_joins": 0,
        "orphan_terminals": 0,
    }


def reduce_events(events, *, task, run_id=None, task_id=None, kind="run",
                  archive=None, token_window=DEFAULT_TOKEN_WINDOW) -> Reduction:
    """The GD-14 rule set, applied in file order. Pure: no I/O, no clock.

    Purity matters beyond tidiness — "is this node stale" must be answerable
    from the stream alone here, because read-time liveness against `now()` is
    the reducer's (GD-23/R-54) and two independent clocks would let the page and
    the API disagree about the same run.
    """
    task_id = task or task_id
    run_id = run_id or f"legacy:{task}"
    stats = _new_stats()
    plans = {}
    nodes = []
    logs = []
    token_events = []
    open_nodes = {}                    # "<plan>/<stage>" -> NodeState
    ordinals = {}                      # "<plan>/<stage>" -> next ordinal
    run_terminals = []                 # every terminal `orchestrator|complete`
    events = tuple(events)

    for event in events:
        stats["lines"] += 1
        if event.provenance == PROVENANCE_UNKNOWN:
            stats["unattributable"] += 1
        if not event.ok:
            stats["parse_errors"] += 1
            continue

        plan = plans.get(event.plan)
        if plan is None:
            plan = plans[event.plan] = PlanState(plan=event.plan)
        if event.title:
            plan.title = event.title
        if event.plans_total is not None:
            # `monitoring.md`: a monotonic max, floored by the cards seen. A
            # resume re-declaring the same number is idempotent; a stray smaller
            # value never shrinks the denominator.
            plan.plans_total = max(plan.plans_total or 0, event.plans_total)

        if event.is_token:
            stats["token_lines"] += 1
            token_events.append(event)
            if event.stage == TOKEN_STAGE:
                continue               # a counter update, never a node or a log

        if event.is_badge:
            _badge_event(plan, event, stats)
            if event.plan == RESERVED_PLAN and event.stage == "complete" \
                    and event.state in TERMINAL_STATES:
                run_terminals.append(event)
            continue

        if event.plan == RESERVED_PLAN:
            logs.append(event)         # RUNSTATE-8.3: run-level, never an agent
            continue

        _node_event(event, task, run_id, nodes, open_nodes, ordinals, stats)

    alias = _join_id_widths(nodes, stats)
    _close_superseded(nodes, stats)
    _close_stale(nodes, run_terminals, stats)
    _resolve_badges(plans, nodes, stats)
    tokens = _fold_tokens(task, token_events, token_window, alias, stats)

    stats["nodes"] = len(nodes)
    stats["plans"] = len(plans)
    stats["token_records"] = len(tokens)
    return Reduction(
        task=task,
        task_id=task_id,
        run_id=run_id,
        kind=kind,
        archive=archive,
        events=events,
        plans=plans,
        nodes=tuple(nodes),
        logs=tuple(logs),
        tokens=tokens,
        stats=stats,
        notes=tuple(_notes(plans, nodes)),
    )


def _badge_event(plan, event, stats):
    """Collect a `plan`/`complete` row; resolution happens after the walk.

    Resolution is deferred because SD-4's rule is about the *last* terminal in
    file order, and "last" is not knowable while walking. Same-state duplicates
    are folded here (RUNSTATE-7: every stage completion is written twice, 38 s
    apart, by two writers) with the agent's own detail kept.
    """
    if event.state in TERMINAL_STATES:
        previous = plan.terminals[-1] if plan.terminals else None
        if previous is not None and previous[1] == event.state:
            stats["deduped_terminals"] += 1
            plan.duplicates += 1
            _merge_detail(plan, event)
            return
        if previous is not None:
            stats["conflicting_terminals"] += 1
            plan.conflicting += ((previous[0], previous[1]),)
        plan.terminals += ((event.line_no, event.state),)
        plan.detail = ""
        plan.agent_detail = None
        plan.badge_line = event.line_no
        _merge_detail(plan, event, primary=True)
    elif not plan.terminals:
        # queued/running before any terminal: the badge follows the stream.
        plan.badge = event.state
        plan.detail = event.detail
        plan.badge_line = event.line_no


def _merge_detail(state, event, *, primary=False):
    """Watcher-wins on the detail, the agent's own words kept beside it.

    RUNSTATE-7's recommendation verbatim: the watcher is the deterministic
    source, but "found 17 findings" is better UI copy than "research #1: 17
    findings", so neither is discarded. When only the agent ever wrote (the
    watcher was not running, or died first), its detail is all there is and it
    fills both fields rather than leaving the card blank.
    """
    if event.by_watcher:
        state.detail = event.detail
        return
    if primary or not state.agent_detail:
        state.agent_detail = event.detail
    if not state.detail:
        state.detail = event.detail


def _node_event(event, task, run_id, nodes, open_nodes, ordinals, stats):
    """Fold one non-reserved row into the node model."""
    key = f"{event.plan}/{event.stage}"
    node = open_nodes.get(key)
    agent_id = event.agent_ref_id()

    if event.state == "running":
        # A new node when there is none, when the last one already resulted, or
        # when this spawn names a *different* agent. The middle case is what
        # keeps `status.sh`'s "scanning models perspective" and the watcher's
        # "research attempt 1 spawned" — two running rows, one agent — from
        # becoming two nodes, while `touch-repo-recon`'s genuine second wave
        # (a different agent id on the same stage) becomes a second node with
        # ordinal 1, which is what GD-14's per-(plan,stage) counter means.
        fresh = (node is None or node.resulted
                 or (agent_id and node.agent_id and node.agent_id != agent_id))
        if fresh:
            ordinal = ordinals.get(key, 0)
            ordinals[key] = ordinal + 1
            node = NodeState(task=task, run_id=run_id, plan=event.plan,
                             stage=event.stage, ordinal=ordinal, label=event.stage,
                             state="running", detail=event.detail)
            nodes.append(node)
            open_nodes[key] = node
        _observe_agent(node, event, agent_id)
        if event.agent_label and not node.agent_label:
            node.agent_label = event.agent_label
        if node.started is None:
            node.started = _agent_started(event) or event.ts
        node.state = "running"
        _merge_detail(node, event)
        node.line_nos += (event.line_no,)
        return

    if node is None:
        # A terminal (or a queued/info row) with no spawn before it. The stream
        # is a multi-writer append log and a watcher restarted mid-run replays
        # results whose spawns predate its checkpoint, so this is normal history
        # rather than corruption: the node is created here, at the ordinal the
        # counter is up to.
        ordinal = ordinals.get(key, 0)
        ordinals[key] = ordinal + 1
        node = NodeState(task=task, run_id=run_id, plan=event.plan, stage=event.stage,
                         ordinal=ordinal, label=event.stage, state=event.state)
        nodes.append(node)
        open_nodes[key] = node
        if event.is_terminal:
            stats["orphan_terminals"] += 1

    _observe_agent(node, event, agent_id)
    if event.agent_label and not node.agent_label:
        node.agent_label = event.agent_label
    node.line_nos += (event.line_no,)

    if event.is_terminal:
        if node.resulted:
            if node.state == event.state:
                stats["deduped_terminals"] += 1     # RUNSTATE-7's twin write
                _merge_detail(node, event)
                return
            # Different terminals for one node: SD-4's last-event-wins, applied
            # to nodes for the same reason it is applied to cards — file order
            # is the only order these streams have.
            stats["conflicting_terminals"] += 1
        node.state = event.state
        node.result_line = event.line_no
        node.ended = event.ts
        node.derived_from_legacy = False
        node.relabel = None
        _merge_detail(node, event, primary=True)
        if event.agent_tokens:
            node.tokens = event.agent_tokens
        return

    # queued / info / stale / out-of-enum: recorded, never a result.
    if not node.resulted and event.state in ("queued", STALE_STATE):
        node.state = event.state
    _merge_detail(node, event)


def _observe_agent(node, event, agent_id):
    """Record what this row said about the node's agent, without overwriting.

    First-id-wins for the node's own `agent_id` (`$setOnInsert` semantics, one
    layer up: identity is not a field two writers may contest), but **every**
    distinct id is remembered. That is what makes the both-widths join possible
    at all: the widened watcher's `done` row names the full 17-hex id for a node
    whose spawn row named the 8-hex one, and a first-wins field alone would
    throw the evidence away.
    """
    if agent_id:
        if not node.agent_id:
            node.agent_id = agent_id
        if agent_id not in node.agent_ids:
            node.agent_ids += (agent_id,)
    raw = event.agent_id_raw
    if raw:
        if not node.agent_id_raw:
            node.agent_id_raw = raw
        if agent_id is None:
            # An id in neither width: the node exists, it just has no usable
            # identity (GD-7 permits nodes without a marker; D13 says so out
            # loud rather than inventing one).
            node.unconventional = True
            if "agent-id-unrecognized" not in node.flags:
                node.flags += ("agent-id-unrecognized",)


def _agent_started(event):
    agent = event.agent
    if isinstance(agent, dict) and isinstance(agent.get("started"), str):
        try:
            return store.normalize_ts(agent["started"])
        except store.StoreError:
            return None
    return None


def _join_id_widths(nodes, stats):
    """RUNSTATE-3: join an 8-hex display id to the full 17-hex one, by prefix.

    A stream can carry both widths for one agent — a task whose watcher was
    restarted onto the widened code mid-run — and `monitor.html`, which keys on
    `id` alone, renders that as two rows. Touch joins them here instead, and
    only when the prefix match is **unique**: 8 chars is `a` + 7 hex, about 2^28
    of entropy, and a prefix matching two full ids is exactly the case where
    joining is unsound. Ambiguity leaves both nodes alone rather than picking
    one; the join count is returned in `stats` so it is never invisible.

    Returns the alias map, which the token fold applies too — a rollup keyed on
    the pre-join id would report one agent's usage under two names.
    """
    full = {one for node in nodes for one in node.agent_ids
            if _AGENT_ID17_RE.match(one)}
    alias = {}
    if not full:
        return alias
    for node in nodes:
        for one in node.agent_ids:
            parsed = _LEGACY_AGENT_ID_RE.match(one)
            if not parsed:
                continue
            matches = [target for target in full
                       if target.startswith(parsed.group("id8"))]
            if len(matches) == 1:
                alias[one] = matches[0]
    for node in nodes:
        target = alias.get(node.agent_id)
        if target:
            node.agent_id = target
            node.agent_ids = tuple(alias.get(one, one) for one in node.agent_ids)
            node.flags += ("joined-by-prefix",)
            stats["prefix_joins"] += 1
    return alias


def _close_superseded(nodes, stats):
    """An agent left running with a later sibling on the same stage (GD-14).

    The watcher cannot do this itself: its stale-close is gated on the attempt
    strictly increasing, and a parallel fan-out spawns siblings at the SAME
    attempt (DRIVER-1), so `touch-repo-recon`'s 13:50 respawn reused attempt 1
    and the 13:41 agents stay `running` forever in the stream. Touch owns
    staleness instead of trusting the stream to close its own rows.
    """
    last_ordinal = {}
    for node in nodes:
        last_ordinal[node.key] = max(last_ordinal.get(node.key, -1), node.ordinal)
    for node in nodes:
        if node.resulted or node.ordinal >= last_ordinal[node.key]:
            continue
        node.state = SUPERSEDED_STATE
        node.relabel = SUPERSEDED_STATE
        node.derived_from_legacy = True
        stats["relabel_superseded"] += 1


def _close_stale(nodes, run_terminals, stats):
    """A terminal `orchestrator|complete` closes the open nodes **before it**.

    Never `failed` (D13: an abandoned agent did not fail, it was abandoned) and
    never left `running` (a run that ended has no running agents). This alone
    closes `touch-repo-recon`'s phantom rows, which is what R-37's phase-1
    acceptance arm asserts.

    "Before it" is load-bearing, and it is RUNSTATE-2 that makes it so: one task
    folder's stream spans several script invocations, so a `complete` in the
    middle of the file ends *that* invocation and says nothing about the agents
    a later one spawns. `touch-mongo-live` is the specimen — its research run
    completes at line 298 and the divide/implement phases keep appending to the
    same stream — and closing everything at EOF marked a node that was still
    running when the stream was frozen as stale nine lines before it started.
    """
    if not run_terminals:
        return
    for node in nodes:
        if node.resulted or node.relabel == SUPERSEDED_STATE or not node.line_nos:
            continue
        last_line = max(node.line_nos)
        closer = next((event for event in run_terminals if event.line_no > last_line),
                      None)
        if closer is None:
            continue                   # no run close after it: still open, honestly
        node.state = STALE_STATE
        node.relabel = STALE_STATE
        node.derived_from_legacy = True
        node.ended = node.ended or closer.ts
        stats["relabel_stale"] += 1


def _resolve_badges(plans, nodes, stats):
    """Last-event-wins in file order, then GD-14's re-label. In that order.

    The order is the whole point of SD-4. Re-labelling first would rewrite the
    fabricated `failed` on `touch-full-recon:255` into "closed — no verdict" and
    then let the corrective `done` on :276 win anyway — same answer by luck. But
    on a plan whose corrective line came *first* the two orders disagree, and
    "the last thing the file says is what happened" is the rule that generalises.
    """
    by_plan = {}
    for node in nodes:
        by_plan.setdefault(node.plan, []).append(node)

    for name, plan in plans.items():
        if plan.terminals:
            plan.badge = plan.terminals[-1][1]
        if plan.badge != "failed":
            continue
        if LOOP_EXITED not in (plan.detail or ""):
            continue                   # a genuine failure keeps its badge (D13)
        siblings = by_plan.get(name, ())
        if siblings and not all(node.resulted for node in siblings):
            continue                   # not all stage agents resulted: no relabel
        plan.badge = CLOSED_STATE
        plan.label = CLOSED_NO_VERDICT
        plan.relabel = CLOSED_NO_VERDICT
        plan.derived_from_legacy = True
        stats["relabel_closed"] += 1


def _fold_tokens(task, token_events, window, alias, stats):
    """RUNSTATE-12's fold: at most one record per agent per throttle window.

    Lossless by construction — every `quiet` delta line carries the cumulative
    `agent.tokens`, so the last line of a window states the total and nothing is
    summed (summing deltas is the double-count GD-25 forbids). Lines this cannot
    fold losslessly are kept whole: a non-quiet token line is the agent's final
    total, and a token line naming no agent has no cumulative copy to take.

    Those two kept arms are not the same thing, and :class:`TokenRecord`'s
    `absolute` flag says which is which: a kept line whose cumulative can be
    **attributed to an agent** is absolute (fold it latest-wins), any other kept
    line is its own delta and must be **summed** — nothing else *in*
    :class:`Reduction`.tokens states it, because the fold only ever sees
    token-stage lines. (The stream at large may state it elsewhere: an agent's
    terminal `done` carries a higher cumulative than its last delta line, which
    is exactly the 1.9 % PRIOR-ART-TOUCH-1 measures. Corollary: if this fold
    ever starts reading cumulatives off *any* event, the summation of the
    agent-less records must be dropped in the same change, or that 1.9 % is
    counted twice.)

    Losing the distinction is what under-reported this repo's own corpus by
    1.65 % (PRIOR-ART-TOUCH-2): every agent-less line of a plan shares the
    rollup key `plan|stage|None|None`, so latest-wins kept the last one and
    dropped the rest. Deriving `absolute` from `agent_id` rather than from the
    presence of a cumulative keeps `absolute == (agent_id is not None)` true by
    construction, which is what lets `touch-visual/app.js::rollupList` read the
    distinction off `agentId` alone (the wire carries no `absolute` key);
    `tests/test_legacy.py` pins it in both directions.

    One residual the flag cannot rescue: a kept line that names an agent but
    carries no cumulative is `absolute=False`, yet the page — reading `agentId`
    — will fold it latest-wins. Recorded, not fixed: it is unreachable today,
    since `status.sh` writes neither `agent` nor `tokens`, and every
    `decision_watcher.py` token emit carries `agent.tokens`. It becomes real
    only if a writer starts emitting `agent` without `agent.tokens` on a
    token-stage line, and the fix then is the one noted on
    :class:`TokenRecord` — put `absolute` on the wire.
    """
    if window is None or window <= 0:
        window = DEFAULT_TOKEN_WINDOW
    buckets = {}
    order = []
    kept = []
    for event in token_events:
        cumulative = event.agent_tokens
        agent_id = event.agent_ref_id()
        agent_id = alias.get(agent_id, agent_id)
        # A cumulative is only usable as one if it can be attributed: two
        # cumulatives of the SAME agent are one quantity (latest-wins), but two
        # cumulatives no reader can tell apart have no bucket to be latest in.
        # So `absolute` is structural, never observed — see the docstring.
        absolute = cumulative is not None and agent_id is not None
        if not event.quiet or not absolute:
            kept.append(TokenRecord(
                task=task, plan=event.plan, stage=event.stage, agent_id=agent_id,
                label=event.agent_label, ts=event.ts, line_no=event.line_no,
                tokens=(cumulative if absolute else (event.tokens or _tokens(None))),
                absolute=absolute))
            continue
        slot = None
        if event.ts is not None:
            slot = int(event.ts.timestamp() // window)
        key = (agent_id, slot)
        if key not in buckets:
            order.append(key)
        else:
            stats["folded"] += 1
        buckets[key] = TokenRecord(
            task=task, plan=event.plan, stage=event.stage, agent_id=agent_id,
            label=event.agent_label, ts=event.ts, line_no=event.line_no,
            tokens=cumulative, window=slot,
            folded=(buckets[key].folded + 1) if key in buckets else 1)
    folded = [buckets[key] for key in order]
    return tuple(sorted(folded + kept, key=lambda record: record.line_no))


def _notes(plans, nodes):
    """Short, honest sentences a card can render (D13: label every derivation)."""
    for name, plan in sorted(plans.items()):
        if plan.relabel:
            yield (f"plan {name}: {plan.relabel} (line {plan.badge_line}; "
                   f"derived from legacy)")
        for line_no, state in plan.conflicting:
            yield (f"plan {name}: {state} at line {line_no} superseded by the "
                   f"later terminal at line {plan.badge_line} (last-event-wins)")
    for node in nodes:
        if node.relabel:
            yield (f"node {node.key}#{node.ordinal}: {node.relabel} "
                   f"(derived from legacy)")


def reduce_task(folder, *, token_window=DEFAULT_TOKEN_WINDOW) -> Reduction:
    """Read one task folder's stream and reduce it. The I/O half of R-27."""
    if isinstance(folder, (str, os.PathLike)):
        folder = task_folder(folder)
    events = read_events(folder.events_path, folder.task) if folder.events_path else ()
    return reduce_events(events, task=folder.task, run_id=folder.run_id,
                         kind=folder.kind, archive=folder.archive,
                         token_window=token_window)


def scan(root=None, *, env=None, token_window=DEFAULT_TOKEN_WINDOW) -> tuple:
    """Every task folder under the root, reduced. Plan-only folders included.

    A plan-only folder reduces to an empty reduction that still carries its kind
    and archive label — it is a real thing the sidebar lists, with no controls
    (RUNSTATE-13), not an error and not an "empty task".
    """
    return tuple(reduce_task(folder, token_window=token_window)
                 for folder in discover_tasks(root, env=env))


# --- the artifact registry ------------------------------------------------


def artifact_kind_of(rel_path) -> str:
    """R-51's kind, from the path alone (a scan must not open a file to sort it)."""
    parts = rel_path.replace(os.sep, "/").split("/")
    if len(parts) > 1:
        kind = _ARTIFACT_DIRS.get(parts[0])
        if kind:
            return kind
    name = parts[-1]
    if name == CONFIG_FILE or name.endswith((".config.json", ".conf")):
        return "config"
    if name.endswith((".log", ".jsonl")):
        return "log"
    if name.endswith((".sh", ".js", ".py")):
        return "script"
    if name.endswith(".json"):
        return "config"
    return "other"


def _stream_safe(text: str) -> str:
    """Percent-encode what `refs`' stream grammar does not accept.

    A second, *outer* escaping layer over `refs.escape_stream`, and it has to be
    one: the grammar rejects a task name containing a space or a slash outright
    rather than escaping it, and task names are user-chosen folder names (GD-14
    says so, which is why `%`, `#`, `|` and `:` are escaped in the `legacy:`
    ids). Both layers are invertible and both are round-tripped in the tests, so
    the stream id is a lossless function of the folder name.
    """
    return _STREAM_UNSAFE_RE.sub(
        lambda match: "".join("%%%02X" % byte for byte in match.group(0).encode("utf-8")),
        text)


def _stream_unsafe(text: str) -> str:
    """The inverse of :func:`_stream_safe`, decoded as UTF-8 bytes.

    Byte-wise on purpose: a non-ASCII character escapes to several `%XX` pairs
    and decoding them one at a time would produce mojibake instead of the folder
    name that went in.
    """
    out = bytearray()
    index = 0
    while index < len(text):
        match = _STREAM_PCT_RE.match(text, index)
        if match:
            out.append(int(match.group(1), 16))
            index = match.end()
        else:
            out.extend(text[index].encode("utf-8"))
            index += 1
    return out.decode("utf-8", "replace")


def artifact_stream(task) -> str:
    """`artifact:<task>` — the registry stream id for one task folder."""
    if not isinstance(task, str) or not task:
        raise LegacyError("a task name must be a non-empty string")
    safe = _stream_safe(task)
    if len(safe) > MAX_STREAM_TASK_CHARS:
        raise LegacyError(
            f"task name too long to key an artifact stream ({len(safe)} > "
            f"{MAX_STREAM_TASK_CHARS} characters after escaping): {task!r}")
    return f"{ARTIFACT_STREAM_PREFIX}:{safe}"


def task_of_artifact_stream(stream) -> str:
    """The exact inverse of :func:`artifact_stream` (a grammar with no inverse
    is a grammar nobody can audit — `refs` makes the same argument)."""
    prefix, sep, rest = str(stream).partition(":")
    if prefix != ARTIFACT_STREAM_PREFIX or not sep:
        raise LegacyError(f"not an artifact stream id: {stream!r}")
    return _stream_unsafe(rest)


def artifact_slot(rel_path, digest, *, taken=()) -> int:
    """The `(stream, seq)` slot for one artifact observation.

    Derived from `(path, sha256)` so that re-scanning an unchanged folder
    reproduces the same `_id` (the insert is a tolerated duplicate — GD-25's
    idempotency), a changed file appends a new event beside the old one
    (append-only), and adding a file renumbers nothing. A positional rank would
    have all three properties backwards: it renumbers on insertion, which under
    a positional `_id` silently re-points existing documents at other files.

    ``taken`` resolves the (astronomically unlikely, but not impossible) slot
    collision within one scan by probing upward. Probing is scan-local and the
    scan iterates in sorted-path order, so it is deterministic — and a collision
    across scans cannot corrupt anything, it merely re-probes.
    """
    seed = f"{rel_path}\0{digest}".encode("utf-8")
    slot = int(hashlib.sha256(seed).hexdigest()[:16], 16) % ARTIFACT_SLOT_MODULUS
    taken = set(taken)
    while slot in taken:
        slot = (slot + 1) % ARTIFACT_SLOT_MODULUS
    return slot


def _digest(path):
    """`(sha256, size)` — streamed, never a whole-file read into memory."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def iter_artifacts(folder, *, root=None) -> tuple:
    """Register every file in one task folder: paths + digests, never bodies.

    `.watcher-state.json` is excluded by name (GD-14) — a digest of a file Touch
    must not read is still a read of it. Everything else is registered,
    including the stream itself: a reader asking "what did this run produce"
    should not have to know which files the layout happens to name.
    """
    if isinstance(folder, (str, os.PathLike)):
        folder = task_folder(folder)
    stream = artifact_stream(folder.task)
    found = []
    for directory, dirnames, filenames in os.walk(folder.path):
        dirnames.sort()
        for name in sorted(filenames):
            if name in NEVER_REGISTERED:
                continue
            path = os.path.join(directory, name)
            rel = os.path.relpath(path, folder.path).replace(os.sep, "/")
            try:
                sha256, size = _digest(path)
                mtime = os.stat(path).st_mtime
            except OSError:
                continue               # vanished or unreadable mid-walk
            found.append((rel, sha256, size, mtime, path))

    out = []
    taken = set()
    for rel, sha256, size, mtime, path in sorted(found):
        slot = artifact_slot(rel, sha256, taken=taken)
        taken.add(slot)
        out.append(ArtifactObservation(
            task=folder.task, stream=stream, slot=slot, path=rel,
            kind=artifact_kind_of(rel), sha256=sha256, size=size,
            mtime=_utc(mtime), source_path=_rel(root, path)))
    return tuple(out)


def _utc(epoch) -> datetime.datetime:
    moment = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return moment.replace(microsecond=(moment.microsecond // 1000) * 1000)


# --- observations ---------------------------------------------------------


@dataclass(frozen=True)
class LegacyEventObservation:
    """One verbatim line, ready for the mapper. Positional identity (R-51)."""

    task: str
    line_no: int
    ts: object = None
    ts_raw: str = ""
    plan: str = ""
    stage: str = ""
    state: str = ""
    state_raw: str = ""
    detail: str = ""
    title: object = None
    w: object = None
    provenance: str = PROVENANCE_UNKNOWN
    agent: object = None
    tokens: object = None
    quiet: bool = False
    plans_total: object = None
    extra: object = None
    parse_error: object = None
    raw: object = None
    byte_offset: int = 0
    source_path: str = ""

    @classmethod
    def from_event(cls, event, *, source_path=""):
        return cls(
            task=event.task, line_no=event.line_no, ts=event.ts, ts_raw=event.ts_raw,
            plan=event.plan, stage=event.stage, state=event.state,
            state_raw=event.state_raw, detail=event.detail, title=event.title,
            w=event.w, provenance=event.provenance, agent=event.agent,
            tokens=event.tokens, quiet=event.quiet, plans_total=event.plans_total,
            extra=dict(event.extra) if event.extra else None,
            parse_error=event.parse_error, raw=event.raw,
            byte_offset=event.byte_offset, source_path=source_path)


@dataclass(frozen=True)
class ArtifactObservation:
    """One task-folder file: path + digest + size + mtime. Never its body."""

    task: str
    stream: str
    slot: int
    path: str
    kind: str
    sha256: str
    size: int
    mtime: object
    source_path: str = ""


# --- the mapping half (SD-1: pure, no I/O, no driver) ---------------------


COLLECTIONS = ("legacy_events", "custom_state_events")


def _only_ours(ops):
    """Structural GD-15: this module writes two collections and no others.

    The same fence `sessions.py` and `ingest.py` put around themselves. Legacy
    nodes and legacy agents are *derived* (GD-23) — there is no code path here
    by which one becomes an `agents` or a `run_nodes` document, which is what
    keeps a synthesized 8-hex identity out of the harness mirror.
    """
    for collection, _key, _update in ops:
        if collection not in COLLECTIONS:
            raise LegacyError(
                f"legacy.py may only write {list(COLLECTIONS)}, not {collection!r} — "
                f"the legacy reduction is derived state and belongs to the reducer "
                f"(GD-23), never to a harness collection")
    return ops


def _as_observation(observation, cls):
    """Accept a dataclass or the plain dict a replay/fixture hands back."""
    if isinstance(observation, cls):
        return observation
    if isinstance(observation, dict):
        try:
            return cls(**observation)
        except TypeError as exc:
            raise LegacyError(f"unusable {cls.__name__}: {exc}") from None
    raise LegacyError(
        f"expected a {cls.__name__} or a dict, got {type(observation).__name__}")


def _ts_pair(ts, ts_raw) -> dict:
    """`{"ts": Date, "tsRaw": "<the file's own spelling>"}` (GD-11(g)).

    The spelling matters here more than anywhere else in the mirror: the two
    legacy writers disagree about the format (RUNSTATE-6) and a normalized Date
    is a lossy answer to "what did the line say".
    """
    fields = ms.ts_fields(ts)
    if isinstance(ts_raw, str) and ts_raw:
        fields["tsRaw"] = ts_raw
    return fields


def _split_ops(collection, doc, key, *, immutable=()):
    """GD-25's operators for a document whose fields are a function of one line.

    `provenance` and the `_id`'s own components are `$setOnInsert` — identical
    in every operation that can target this `_id`, which is the one operator
    whose payload must not vary. Everything else is `$set`, and it is
    order-independent because the source line is immutable: `events.jsonl` is
    append-only, so two observations of line N are the same bytes.
    """
    doc = dict(doc)
    doc.pop("_id", None)
    on_insert = {}
    for name in ("provenance",) + tuple(immutable):
        if name in doc:
            on_insert[name] = doc.pop(name)
    ops = [ms.op_set_on_insert(on_insert)] if on_insert else []
    setters = {name: value for name, value in doc.items() if value is not None}
    if setters:
        ops.append(ms.op_set(setters))
    if not ops:
        raise LegacyError(f"{collection} operation for {key!r} would be empty")
    return ms.merge_ops(*ops, collection=collection)


def map_legacy_event(observation):
    """`legacyEvent` ⇒ one positional `legacy_events` upsert. Pure (SD-1).

    `_id = legacy:<task>#<line:08d>`. Two lines with identical bytes and
    identical timestamps are two documents because they are two events —
    the frozen `touch-mongo-live` stream holds exactly that pair, and a content
    or `ts` key would silently merge them (measured: up to 27 identical
    timestamps in one file).
    """
    obs = _as_observation(observation, LegacyEventObservation)
    if not obs.task:
        raise LegacyError("a legacy event observation needs its task folder name")
    key = refs.legacy_event_key(obs.task, obs.line_no)
    doc = {
        "_id": key,
        "task": obs.task,
        "lineNo": obs.line_no,
        "provenance": obs.provenance,
        "plan": obs.plan or None,
        "stage": obs.stage or None,
        "state": obs.state or None,
        "detail": obs.detail or None,
        "title": obs.title,
        "w": obs.w,
        "agent": obs.agent,
        "tokens": obs.tokens,
        "plansTotal": obs.plans_total,
        "byteOffset": obs.byte_offset,
        "sourcePath": obs.source_path or None,
        "extra": obs.extra or None,
        "parseError": obs.parse_error,
        "raw": obs.raw,
    }
    if obs.quiet:
        doc["quiet"] = True
    if obs.state_raw and obs.state_raw != obs.state:
        # RUNSTATE-16: `status.sh` validates nothing, so out-of-enum states
        # exist on disk. They map to `info` and the original is kept — mapped,
        # never dropped, and auditable.
        doc["stateRaw"] = obs.state_raw
        doc["_normalized"] = {"stateMapped": True}
    if obs.ts is not None:
        doc.update(_ts_pair(obs.ts, obs.ts_raw))
    elif obs.ts_raw:
        doc["tsRaw"] = obs.ts_raw
    doc = {name: value for name, value in doc.items() if value is not None}

    prepared, _report = ms.prepare_document("legacy_events", doc)
    kept, _size = ms.guard_oversize("legacy_events", prepared,
                                    source_path=obs.source_path or None,
                                    byte_offset=obs.byte_offset)
    return _only_ours([("legacy_events", key,
                        _split_ops("legacy_events", kept, key,
                                   immutable=("task", "lineNo")))])


def map_artifact(observation):
    """`legacyArtifact` ⇒ one insert-only `custom_state_events` document.

    Every field is `$setOnInsert`, so this mapper has no code path that updates
    or removes a document (R-52's append-only posture; GD-26's no-delete rule).
    A re-scan of an unchanged file therefore costs one tolerated duplicate key
    and changes nothing at all — which is what GD-25's shuffled/reversed
    acceptance pass measures.

    The observation kind is `legacyArtifact`, not `artifact`: `discover_mappers`
    refuses two modules registering one kind (GD-15), and `artifact` is also one
    of R-52's *document* kinds, which `custom_state.py` owns. The document this
    writes carries `kind: "artifact"` as R-51 specifies; only the registry name
    is namespaced.
    """
    obs = _as_observation(observation, ArtifactObservation)
    if obs.kind not in ARTIFACT_KINDS:
        raise LegacyError(
            f"artifact kind {obs.kind!r} is not one of R-51's {ARTIFACT_KINDS}")
    stream = obs.stream or artifact_stream(obs.task)
    key = refs.custom_state_event_key(stream, obs.slot)
    # R-51's field list lives under `artifact`, not under `data.custom`: GD-24
    # declares `data.custom` a variable-key subtree, so `mongo_store` stores it
    # `_raw`-wrapped (a stable shape for arbitrary user keys) and a wrapped
    # subtree is unqueryable. These six fields are fixed, and "every findings
    # file in this task, by digest" has to be a query. The class is
    # `artifact.kind`; the document's own `kind` is the collection's
    # discriminator and stays `"artifact"` (R-52 keys the collection on it).
    doc = {
        "_id": key,
        "kind": ARTIFACT_KIND,
        "provenance": ARTIFACT_PROVENANCE,
        "author": ARTIFACT_AUTHOR,     # Touch has no user identity to fabricate
        "stream": stream,
        "seq": obs.slot,
        "taskId": obs.task,
        "artifact": {
            "kind": obs.kind,
            "path": obs.path,
            "sha256": obs.sha256,
            "size": obs.size,
        },
    }
    if obs.mtime is not None:
        # The file's own mtime, never `now()`: it keeps the document a pure
        # function of the file (so a rebuild reproduces it byte for byte) and
        # gives `Mirror.backfill`'s "no ts newer than the source" guard
        # something true to compare against.
        doc.update(_ts_pair(obs.mtime, None))
        doc["artifact"]["mtime"] = doc["tsRaw"]

    prepared, _report = ms.prepare_document("custom_state_events", doc)
    kept, _size = ms.guard_oversize("custom_state_events", prepared,
                                    source_path=obs.source_path or None)
    body = dict(kept)
    body.pop("_id", None)
    return _only_ours([("custom_state_events", key,
                        ms.op_set_on_insert(body))])


#: SD-1's registry. `mirror.discover_mappers` finds it by name.
MIRROR_MAPPERS = {
    "legacyEvent": map_legacy_event,
    "legacyArtifact": map_artifact,
}


# --- sources (the rebuild/backfill seam) ---------------------------------
#
# `--rebuild` calls each source with `path=None`: the whole orchestrator root.
# `--backfill` calls every registered source once per `.jsonl` under
# `~/.claude/projects/**`, and **no legacy file lives there** — the streams are
# repo history under `.touch/local-orchestrators/`. Both sources therefore
# answer `()` for any path they do not own, decided from the path alone (one
# `basename` comparison, per `iter_backfill_observations`' contract), and the
# legacy arm contributes nothing to a backfill. That is the correct answer, not
# a gap: R-51's "there is nothing else to migrate" is about this exact seam.


def is_legacy_stream_path(path) -> bool:
    """Does this path name a task folder's `events.jsonl`? From the path alone."""
    path = os.fspath(path)
    if os.path.basename(path) != EVENTS_FILE:
        return False
    folder = os.path.dirname(path)
    return os.path.basename(os.path.dirname(folder)) == os.path.basename(TASK_ROOT)


def task_folder_of(path):
    """The task directory containing ``path``, or ``None``. Path arithmetic only."""
    path = os.path.abspath(os.fspath(path))
    marker = os.path.basename(TASK_ROOT)
    parts = path.split(os.sep)
    if marker not in parts:
        return None
    index = parts.index(marker)
    if index + 1 >= len(parts) - 1:
        return None                    # the root itself, or a file directly in it
    return os.sep.join(parts[:index + 2])


def iter_legacy_event_observations(path=None, *, root=None, env=None):
    """`MIRROR_SOURCES["legacyEvent"]` — one observation per line, in file order.

    ``root`` is the orchestrator root (:func:`orchestrator_root`'s argument),
    and it is also what stored `sourcePath`s are relative to.

    The same parser the reduction uses (R-51: R-27's reduction IS the input,
    there is no second adapter).
    """
    base = orchestrator_root(root, env)
    if path is not None:
        if not is_legacy_stream_path(path):
            return []
        task = os.path.basename(os.path.dirname(os.fspath(path)))
        return [LegacyEventObservation.from_event(event, source_path=_rel(base, path))
                for event in read_events(path, task)]
    out = []
    for folder in discover_tasks(base, env=env):
        if not folder.events_path:
            continue
        source = _rel(base, folder.events_path)
        out.extend(LegacyEventObservation.from_event(event, source_path=source)
                   for event in read_events(folder.events_path, folder.task))
    return out


def iter_artifact_observations(path=None, *, root=None, env=None):
    """`MIRROR_SOURCES["legacyArtifact"]` — the registry, one document per file.

    A concrete path registers only that file, and only when it is inside a task
    folder; the slot is content-addressed, so a single-file registration and a
    whole-folder scan agree on the `_id` without sharing any state.
    """
    base = orchestrator_root(root, env)
    if path is not None:
        folder_path = task_folder_of(path)
        if folder_path is None or os.path.basename(os.fspath(path)) in NEVER_REGISTERED:
            return []
        folder = task_folder(folder_path)
        rel = os.path.relpath(os.path.abspath(os.fspath(path)),
                              folder.path).replace(os.sep, "/")
        try:
            sha256, size = _digest(path)
            mtime = os.stat(path).st_mtime
        except OSError:
            return []
        return [ArtifactObservation(
            task=folder.task, stream=artifact_stream(folder.task),
            slot=artifact_slot(rel, sha256), path=rel, kind=artifact_kind_of(rel),
            sha256=sha256, size=size, mtime=_utc(mtime),
            source_path=_rel(base, path))]
    out = []
    for folder in discover_tasks(base, env=env):
        out.extend(iter_artifacts(folder, root=base))
    return out


def _rel(root, path) -> str:
    """A root-relative POSIX path, or the basename when there is no root.

    Stored paths stay machine-independent: an absolute path would make a
    document's fingerprint depend on the checkout it was built in, which is the
    one thing GD-25's cross-machine comparison cannot tolerate.
    """
    path = os.fspath(path)
    if not root:
        return os.path.basename(path)
    try:
        return os.path.relpath(os.path.abspath(path),
                               os.path.abspath(os.fspath(root))).replace(os.sep, "/")
    except ValueError:
        return os.path.basename(path)


#: The rebuild/backfill seam declared beside the mappers (`mirror.iter_sources`).
MIRROR_SOURCES = {
    "legacyEvent": iter_legacy_event_observations,
    "legacyArtifact": iter_artifact_observations,
}
