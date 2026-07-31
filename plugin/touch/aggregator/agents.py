"""Agents: the node/graph join (R-28), fragment + spawn assembly (R-48), and
the ONE server-side reducer (R-54).

Three items, one file, because they are one subject: what an agent *is*
(harness facts), what a marker *calls* it (labels), and what Touch *concludes*
about it (derived state). GD-15 gives `agents.py` one owner, and R-54 names it
"the reducer home" for exactly this reason — the conclusion must be computed
where the facts are assembled, or two places grow two answers.

Three halves, separated by hard lines
-------------------------------------
* **reading** (:func:`read_fragment`, :func:`read_meta`, :func:`find_spawns`,
  :func:`scan`) does the I/O and returns frozen dataclasses. It never parses a
  transcript itself: `ingest.read_transcript` owns the `~/.claude` record
  format (GD-15) and this module consumes its `TranscriptScan`. What it *does*
  own is the two formats nobody else reads — `agent-<id>.meta.json` and the
  `(tool_use, tool_result)` spawn pair — plus `os.stat`, for the perishable
  file hint.
* **mapping** (:data:`MIRROR_MAPPERS`) is pure: observations in,
  `(collection, _id, update)` triples out, built only from `refs.ref_key` and
  `mongo_store`'s op vocabulary. No I/O, no clock, no driver. `mirror.py`
  discovers and drives it, and it — with `mongo_store.py` — is the only module
  that may import the database driver (GD-21). The driver package's name does
  not appear in this file at all.
* **reducing** (:func:`reduce`) is pure too, over the mirror's memory model:
  observations in, `derived` documents out. It is the only place in Touch that
  turns a timestamp into a *state*.

Identity: harness facts create nodes, markers label them (GD-7)
---------------------------------------------------------------
An agent document exists because a file named `agent-<17-hex>.jsonl` exists,
or because a `tool_result` named an `agentId`. Neither needs a marker. A
`[monitor]`/`[touch]` marker adds `plan/stage/role/attempt/name/parent/root`
*labels*, and a missing one degrades the label (`unconventional: true`,
rendered as the agentId) and never the node. P6's name-only tree and
INTENT-13's "marker mandatory" are superseded by GD-7 and this file implements
the superseding version: there is no code path here in which the absence of a
marker prevents a document.

`sessionId` is never a grouping key (SESSIONJSONL-3 / R-48)
-----------------------------------------------------------
One agent's transcript is split across session directories when the user
`/clear`s mid-run: `a2fc883c96ff7b837` has 223 records under `dd469822…` and 2
under `e423cd3c…`, 17 minutes apart, with **zero uuid overlap** — they are
disjoint continuations, not two copies. Grouping by session would produce two
half-agents; grouping by `(session, agentId)` would produce two half-agents
with better names. The only grouping key is the agentId, and the fragments are
stitched by the `parentUuid → uuid` chain (`e423cd3c`'s first record's
`parentUuid` IS `dd469822`'s last record's `uuid` — verified exact on the live
pair), never by directory order, never by mtime.

Storage shape of `fragments[]`, and one stated deviation
--------------------------------------------------------
R-48 specifies `fragments:[{sessionId, path, firstUuid, lastUuid, lineCount}]`
and GD-24 makes `fragments` an `$addToSet` set. Those two are in tension on the
live path and the tension is not theoretical: `lastUuid` and `lineCount` change
on **every append**, so a 250 ms poll tick that re-observes a growing fragment
adds a *new* set element four times a second — an unbounded array on the one
collection that must stay small (GD-16), and a fragment list that shows the
same file eleven times.

So the split is by mutability, and the reader's contract is unchanged:

* `fragments[]` holds the fragment's **identity** —
  `{sessionId, path, firstUuid, firstParentUuid, firstTs}` — every member a
  property of the file's *first record*, which append cannot change. Observed
  once, added once, in any order, from any number of ticks. `firstTs` is that
  record's OWN timestamp (`Fragment.first_record_ts`) and emphatically not
  `TranscriptScan.first_ts`, which is the minimum over every line in the file:
  the harness writes non-monotonic timestamps (27 of 177 transcripts on this
  machine, 20 of them with `min(ts) != ts[0]`), so one later record stamped
  earlier than the current minimum would change the identity sub-document and
  `$addToSet` — which dedupes on exact, field-order-sensitive equality — would
  add a SECOND element for the same file, permanently (GD-26 forbids the delete
  that would repair it). An identity member has to be a property of one record,
  not an aggregate over all of them. The agent-level `firstTs` still wants the
  minimum, and gets it: that one is `$min` and merges across fragments.
  A fragment whose first record is not readable yet (an empty file, a torn
  first line) has NO identity, writes no element at all and is counted
  `no_first_record`: two different sub-documents for one file is exactly the
  duplicate this split exists to prevent, arriving through the live tail
  instead of the tick.
* `fragmentTips{<firstUuid>: {lineCount, records, lastTs, lastMark}}` holds the
  **tip**, written per fragment by the one writer that read that file, and
  every leaf of it is `$max`. `lineCount`/`records`/`lastTs` are monotone under
  *append* and so are `$max`-safe on their own; `lastUuid` is not monotone and
  is not stored on its own, because `$max` over random uuids would pair the
  *wrong* file's tip uuid with the right line count. It rides inside
  :func:`tip_mark`'s `lastMark` — `"<lineCount:012d>#<lastUuid>"` — a string
  whose ordering is dominated by the same monotone counter, so the winning mark
  and the winning `lineCount` are always the same observation's. There is no
  `$set` anywhere in this module's mapping half: `$set` does not commute, and
  `mirror.py` batches two updates of one `_id` unordered and re-queues unwritten
  operations at the tail on the explicit ground that everything here does.

  `$max` is a **high-water mark, and GD-26 says the transcript is not
  append-only**: `performRemoveByUuid` truncates and rewrites, and
  `performCompactTranscript` rewrites the whole file. After a shrink the stored
  tip keeps the pre-shrink `lineCount`/`records`/`lastMark` — `$max` cannot go
  down — until `mirror.py`'s generation sweep (SD-10, sp-06) supersedes the
  fragment's records at a higher generation. That is a stale *tip*, never a
  wrong identity or a duplicate element, and it is the price of an operator
  that commutes; the alternative ($set) is wrong in the case that happens four
  times a second instead of the one that happens on a rewrite. Recorded as D-5
  in `findings/sp-agents-reducer-storage-deviation.md`.
* :func:`fragments_of` recombines them into exactly R-48's shape, in chain
  order, one entry per file. Every *reader* — `/api/*`, the page, the tests —
  sees the specified list; only the storage knows about the split. It also
  collapses two elements that name one first record
  (:func:`_collapse_identities`), which is not redundancy: a document written
  before the `firstTs` correction above already holds both spellings, and GD-26
  forbids the delete that would remove one, so read time is the only place a
  duplicate can ever be repaired.

The same "no `$set`" rule governs `spawn` (R-48's
`{recordUuid, toolUseId, fileHint}` sub-document), whose *shape* is unchanged:
its immutable leaves are `$min` and every leaf of the perishable `fileHint` is
`$max`. `fileHint` cannot be written whole — `size` and `ts` are stat'd from
the parent session transcript, which grows while the session is alive, so two
observations of one spawn disagree and a whole-value `$set` stores whichever
mongod applied last. Per leaf it is coherent as well as order-free: `size` and
`ts` are the only churning leaves, both monotone under append and both read
from ONE `os.stat`, so `$max` on each independently reproduces the latest
observation. A file replaced under the same name can leave a `$max`-pinned
`ino`/`size` from the older file — which makes :func:`check_file_hint` answer
`stale`, the honest outcome for a cache whose identity is `spawn.recordUuid`.

The launch record's own `agentType` and `resolvedModel` stay **inside** that
sub-document (`spawn.agentType`, `spawn.resolvedModel`) rather than landing on
the top-level columns, because the two sources speak two vocabularies for one
column: `.meta.json` says `model: "opus"` where the same agent's launch result
says `resolvedModel: "claude-opus-5[1m]"`, and `run_nodes.model` (the journal's)
says `opus`/`fable` again. Written to one field under `$min` the winner is BSON
collation — `c` < `o`, so the resolved id wins in either order, silently
overriding R-48's stated precedence ("the fragment that HAS meta wins") with a
lexicographic accident. Namespaced, both facts are kept, `map_agent` owns
`agentType`/`model` alone, and R-48's precedence is structural again. The only
two columns BOTH mappers write are `toolUseId` and `description`, and those are
one harness fact stated twice — the `.meta.json` copies the launch record's
values verbatim (`agent-a342353f7b157760b`: meta `toolUseId` IS the `tool_use`
id and meta `description` IS `input.description`) — so `$min` over them is a
no-op rather than a race.

`.meta.json` is optional, and "the meta-bearing fragment wins" is structural
-----------------------------------------------------------------------------
`e423cd3c`'s 2-line continuation has no `.meta.json` (the live pair proves the
case), so :data:`META_FIELDS` — `agentType`, `model`, `spawnDepth`,
`description`, `toolUseId` — must survive a fragment that knows none of them.
All five are GD-24 `agents` columns (`toolUseId` carries a sparse index and
`description` is the human-readable string R-28's `unconventional` fallback
renders), and the `.meta.json` is their ONLY source for an agent whose launch
pair is not observable: a Workflow-profile agent has no `(tool_use,
tool_result)` pair at all, and an Agent-tool one that is still running, or
whose parent transcript was compacted, has no result yet. "The fragment that HAS meta wins on disagreement" is implemented
as *a fragment without meta writes no meta field at all* — not as a precedence
rule applied after the fact. A precedence rule needs both observations in hand,
which the per-file backfill arm never has; an absent write cannot lose a race
it never enters. Where two metas genuinely disagree, `$min` picks the
earliest-sorting value: arbitrary, deterministic, and order-free, which is
GD-25's actual requirement (the same reasoning `ingest.map_run` records for
duplicate launch records).

The same shape covers the marker: only a fragment whose first record carries a
marker writes `name`/`root`/`labels.*`, so the continuation that opens with an
`assistant` record cannot blank the prompt's labels. `unconventional` is
written by every observation and merged with `$min` — `False < True`, so "some
observation found a name" wins over "this one did not", again without either
side needing to see the other.

`assemble` and the mapper resolve a disagreement the SAME way (R-56)
---------------------------------------------------------------------
The two ingest arms see different inputs: a `--rebuild` hands :func:`assemble`
every fragment of an agent at once, while a `--backfill` walks files and hands
it one fragment at a time, letting the update algebra do the merging. Those two
must land on the same document — that is R-56's wipe/rebuild-equivalence arm and
GD-25's fingerprint property across ingest modes — so :func:`assemble` may not
have a conflict rule of its own. It has none: every scalar it merges
(`agentType`/`model`/`spawnDepth`, each label leaf, `runId`, `unconventional`)
is folded through :func:`_min_observed`, which is `mongo_store`'s `$min`
*itself* rather than a second spelling of BSON's order. A chain-first
precedence here — the shape attempt 1 shipped — silently dropped a `name=` the
harness had stated whenever the chain-first fragment's marker carried none, and
flagged a named agent `unconventional: true` on the rebuild path only.

Liveness is computed, never stored (GD-23/R-54)
-----------------------------------------------
No mirror document written here carries a `state`. `firstTs`/`lastTs`/
`resultSeen`/`resultTs` are observations; running / done / unknown is a
function of `now()` and lives in :func:`reduce`, which the API and the page
both serve from. `monitor.html`'s `freezePlan` — the UI-local rule that a card
closing with rows still "running" freezes those rows to stale — moves in here
(:func:`reduce`, the `frozen at run close` reason) so page and API cannot
disagree. `failed` is not in this module's state vocabulary at all: a verdict
is a separate field read off the node's own `result` (GD-10 — a plan whose
agents all resulted without a decisive verdict settles *done*, never `failed`).

Two rules of the derived half are stated here because a reader of a *stored*
derived document has to know them, and both were bugs before they were rules:

* **GD-10's session conjunct may only promote** (:func:`_session_activity`,
  D-7). `sessions.lastTs` is written by one arm of `sessions.py` from the
  registry entry's `updatedAt`, the historical arm writes none at all, and the
  registry heartbeat is measurably *hours* stale for a session that is running
  right now. Absence of a fresh heartbeat is therefore not an observation of an
  idle session: a sessionId is either known-busy (`True`) or unobserved
  (`None`), the node's own transcript decides otherwise, and a demotion would
  need a positive observation of session end that nothing writes yet.
* **Every optional key of a derived payload is always emitted**, `None`/`False`
  included (:meth:`Reduction.operations`). GD-26 forbids the `$unset` that would
  retract a key, and the live writer applies operations without dropping the
  collection, so a conditional key leaves conclusions the reducer no longer
  draws on the stored document. sp-12/sp-13 read "no value" as `null`, never as
  an absent field.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import dataclass, field, replace

from . import ingest
from . import legacy
from . import mongo_store as ms
from . import refs
from . import sessions as sess

__all__ = [
    "AgentsError",
    "PROVENANCE",
    "DERIVED_PROVENANCE",
    "COLLECTIONS",
    "DERIVED_COLLECTION",
    # marker layer (GD-9)
    "MARKER_WINDOW_LINES",
    "MONITOR_FIELDS",
    "TOUCH_FIELDS",
    "Labels",
    "marker_window",
    "marker_records",
    "parse_markers",
    "touch_marker_misplaced",
    "prompt_text_of",
    "labels_from_prompt",
    # identity (GD-7)
    "is_agent_id",
    "node_ref",
    "node_key",
    # reading (R-48)
    "SPAWN_TOOLS",
    "META_SUFFIX",
    "Fragment",
    "AgentObservation",
    "SpawnObservation",
    "AgentScan",
    "meta_path_for",
    "read_meta",
    "read_fragment",
    "assemble",
    "order_fragments",
    "TIP_MARK_SEPARATOR",
    "tip_mark",
    "tip_uuid",
    "fragments_of",
    "file_hint",
    "check_file_hint",
    "HintStatus",
    "spawn_record_filter",
    "find_spawns",
    "scan",
    # mapping (SD-1)
    "map_agent",
    "map_agent_spawn",
    "MIRROR_MAPPERS",
    "MIRROR_SOURCES",
    "iter_agent_observations",
    "iter_agent_spawn_observations",
    # the reducer (R-54)
    "REDUCER_VERSION",
    "IDLE_LIMIT_SECONDS",
    "RUNNING",
    "DONE",
    "UNKNOWN",
    "NODE_STATES",
    "PASSED",
    "FAILED",
    "VERDICT_KEYS",
    "CLOSED_NO_VERDICT",
    "Liveness",
    "liveness",
    "verdict_of",
    "Topology",
    "topology_index",
    "attempt_label",
    "Reduction",
    "reduce",
    "derived_id",
    "apply_derived",
    "needs_rebuild",
]


class AgentsError(ValueError):
    """A caller-side misuse: an observation this module cannot map.

    Reading never raises on content — a metaless fragment, an unparsable
    `.meta.json`, a spawn whose result never arrived and a broken stitch chain
    are all *counted* and carried (see :class:`AgentScan.skipped`). This exists
    for the mapping half, where a malformed observation is Touch's own bug and
    must surface before a wrong `_id` reaches a permanent store.
    `mirror.Mapper` converts it into a `MapperError` naming this module.
    """


# --- constants ------------------------------------------------------------

#: GD-28: everything the mapping half writes is a mirrored harness fact.
PROVENANCE = "harness"

#: GD-28: everything the reducer writes is derived, and droppable with it.
DERIVED_PROVENANCE = "derived"

#: The only collection a mapper here may target (GD-15/SD-1). `records`,
#: `run_nodes` and `usage` come off the same files and are `ingest.py`'s;
#: `sessions` is `sessions.py`'s. :func:`_only_ours` is the wall.
COLLECTIONS = ("agents",)

#: Written by :func:`reduce` alone (GD-23: "nothing outside the reducer writes
#: it"), and never through :data:`MIRROR_MAPPERS` — see :meth:`Reduction.operations`.
DERIVED_COLLECTION = "derived"

#: GD-9's window: markers are real only inside the first four physical lines of
#: the prompt, leading blank lines tolerated (a real prompt starts with `\n`).
#: Twelve files on this machine quote a marker in prose further down; a rule
#: without a window reads those as spawns.
MARKER_WINDOW_LINES = 4

#: Split on the marker TOKEN rather than matching it to end-of-line, so two
#: markers on ONE line (`[touch] name=a [monitor] plan=…`) both parse; each
#: payload is still cut at its own line end, so prose under a marker can never
#: be absorbed into it. Identical rule to `decision_watcher.MARKER_SPLIT` —
#: `tests/test_agents.py` proves the two agree on real prompts, because two
#: copies of one grammar that drift are two grammars.
MARKER_SPLIT = re.compile(r"\[(monitor|touch)\]")
MARKER_KV = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)=(\S+)")

#: Keys the `[monitor]` marker contributes. Unknown keys are kept in
#: :attr:`Labels.extra` rather than dropped, so `model=`/`phase=`/`ledger=` can
#: be added compatibly (GD-9).
MONITOR_FIELDS = ("plan", "stage", "role", "attempt")

#: Keys the `[touch]` identity marker contributes (touch-orchestrate SKILL.md:42).
TOUCH_FIELDS = ("name", "parent", "root", "role", "attempt")

#: Tool names whose `tool_use` block spawns an Agent-tool subagent. `Task` is
#: the older spelling and still appears in the corpus; `Agent` is 2.1.220's.
SPAWN_TOOLS = ("Agent", "Task")

META_SUFFIX = ".meta.json"

#: Fields copied off `agent-<id>.meta.json`. An allowlist, not the whole file:
#: the mirror must not grow a column because a future CLI version added a key.
#: Every name here is a GD-24 `agents` column, and the file is the only source
#: for all five when the launch pair is not observable (a Workflow-profile agent
#: has none; a `spawn_without_result` one has no result yet). `description` is
#: what R-28's `unconventional` fallback renders when no `[touch] name=` exists.
META_FIELDS = ("agentType", "model", "spawnDepth", "description", "toolUseId")

_AGENT_ID_RE = re.compile(r"^[0-9a-f]{17}$")

#: `~/.claude`'s own key for the spawned agent, on the launching tool_result.
_RESULT_AGENT_KEYS = ("agentId",)


# --- the marker layer (GD-9) ----------------------------------------------


def _split_window(text, lines=MARKER_WINDOW_LINES):
    """`(window lines, lines below it)`, leading blank lines skipped."""
    parts = (text or "").split("\n")
    index = 0
    while index < len(parts) and not parts[index].strip():
        index += 1
    return parts[index:index + lines], parts[index + lines:]


def marker_window(text, lines=MARKER_WINDOW_LINES) -> str:
    """The first ``lines`` physical lines of a prompt, leading blanks tolerated."""
    return "\n".join(_split_window(text, lines)[0])


def marker_records(text) -> list:
    """`(kind, payload)` for every marker in ``text``, in order (GD-9)."""
    parts = MARKER_SPLIT.split(text or "")
    return [(parts[i], parts[i + 1].split("\n", 1)[0])
            for i in range(1, len(parts) - 1, 2)]


def parse_markers(text) -> tuple:
    """`(monitor_fields, touch_fields)` from the marker window (GD-9).

    Fields are order-independent `key=value` pairs and unknown keys are kept.
    Last occurrence within the window wins — a prompt that restates its marker
    is stating the same thing twice, and the later statement is the one the
    orchestrator computed most recently.
    """
    monitor = touch = None
    for kind, rest in marker_records(marker_window(text)):
        fields = dict(MARKER_KV.findall(rest))
        if not fields:
            # Payload-less mention — prose quoting the token (e.g. a sub-plan
            # title naming "[monitor]"), not a marker; it must not clobber a
            # real marker's fields. Same rule touch_marker_misplaced applies
            # below the window.
            continue
        if kind == "monitor":
            monitor = fields
        else:
            touch = fields
    return monitor, touch


def touch_marker_misplaced(text) -> bool:
    """Is there a REAL `[touch]` marker BELOW the window? (GD-9)

    A prompt that merely mentions the token — a findings file quoted into a
    critique prompt, this docstring in a code review — is prose, not a
    misplaced marker: only a marker carrying a `key=value` payload counts, so
    the `markerMisplaced` flag stays a signal rather than noise.
    """
    for line in _split_window(text)[1]:
        for kind, rest in marker_records(line):
            if kind == "touch" and MARKER_KV.search(rest):
                return True
    return False


@dataclass(frozen=True)
class Labels:
    """The marker layer of GD-7 — labels only, never identity.

    Every field is optional by construction. :attr:`unconventional` is the
    R-28 flag: no `name=`, so the UI shows the agentId. It is the common case
    on this machine today (no `[touch]` marker has ever been written), which is
    why it is a *label* and not an error.
    """

    plan: object = None
    stage: object = None
    role: object = None
    attempt: object = None
    name: object = None
    parent: object = None
    root: object = None
    unconventional: bool = True
    marker_misplaced: bool = False
    extra: object = None

    @property
    def empty(self) -> bool:
        """True when no marker contributed anything (the node stands anyway)."""
        return not any((self.plan, self.stage, self.role, self.attempt,
                        self.name, self.parent, self.root))

    def fields(self) -> dict:
        """`{label: value}` for the labels that were actually observed."""
        out = {}
        for name in ("plan", "stage", "role", "attempt", "name", "parent", "root"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out


def prompt_text_of(body) -> str:
    """The prompt text of a transcript record, for marker matching.

    `message.content` is a string on a spawn prompt and a list of blocks on
    everything else; both shapes are joined the same way `decision_watcher`
    joins them, so the two parsers see the same bytes.
    """
    if not isinstance(body, dict):
        return ""
    message = body.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content
                        if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


def _attempt(value):
    """`attempt=` as an int, or the raw string when it is not one.

    Not silently defaulted to 1: `decision_watcher` defaults because it must
    emit an event either way, while a *stored* label that says `1` where the
    prompt said `two` is a fabricated fact. An unparsable attempt is kept
    verbatim and the denominator logic (:func:`attempt_label`) declines to
    render a fraction for it.
    """
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return value


def labels_from_prompt(text) -> Labels:
    """Parse both markers out of one prompt (GD-9), as a :class:`Labels`."""
    monitor, touch = parse_markers(text)
    monitor = monitor or {}
    touch = touch or {}
    known = set(MONITOR_FIELDS) | set(TOUCH_FIELDS)
    extra = {name: value for name, value in sorted(monitor.items()) if name not in known}
    extra.update({name: value for name, value in sorted(touch.items()) if name not in known})
    name = touch.get("name")
    return Labels(
        plan=monitor.get("plan"),
        stage=monitor.get("stage"),
        # `[touch] role=` is the same label as `[monitor] role=`; the monitor
        # marker is the one the orchestrator computes, so it wins.
        role=monitor.get("role") or touch.get("role"),
        attempt=_attempt(monitor.get("attempt") or touch.get("attempt")),
        name=name,
        parent=touch.get("parent"),
        root=touch.get("root"),
        unconventional=not name,
        marker_misplaced=touch_marker_misplaced(text),
        extra=extra or None,
    )


# --- identity (GD-7) ------------------------------------------------------


def is_agent_id(value) -> bool:
    """True for a full 17-hex agentId. 8-hex ids are legacy's, namespaced."""
    return isinstance(value, str) and bool(_AGENT_ID_RE.match(value))


def node_ref(*, agent_id=None, run_id=None, key=None, ordinal=None) -> dict:
    """GD-7's node identity as a `refs` ref — the harness fact, never a name.

    Two profiles, two shapes, both harness-derived and both always present:
    `(runId, key, ordinal)` for a Workflow node and the full 17-hex `agentId`
    for an Agent-tool agent. Passing neither is a caller bug, not a node
    without an identity, so it raises.
    """
    if agent_id is not None:
        if not is_agent_id(agent_id):
            raise AgentsError(
                f"agentId {agent_id!r} is not the full 17-hex identity (GD-7); an "
                f"8-hex legacy id is namespaced legacy:<task>:<id8> (GD-11)")
        return {"agentId": agent_id}
    if run_id is not None and key is not None and ordinal is not None:
        return {"runId": run_id, "key": key, "ordinal": int(ordinal)}
    raise AgentsError(
        "a node needs either an agentId or a (runId, key, ordinal) — harness "
        "facts create nodes, markers only label them (GD-7)")


def node_key(**parts) -> str:
    """The `_id` of :func:`node_ref`'s node, through `refs.ref_key` (SD-11).

    On this module's own write path: both mappers key their `agents` upsert
    through here (`node_key(agent_id=…)`, identical to `refs.agent_key`), so the
    exported identity is the exercised one. The `(runId, key, ordinal)` profile
    is `run_nodes`' key — `ingest.py` writes that collection (GD-15) and sp-12
    reads it — and this is the one spelling of the grammar both halves share.
    """
    return refs.ref_key(node_ref(**parts))


# --- reading: fragments and meta ------------------------------------------

#: Separates the monotone counter from the uuid inside a `lastMark`. `#` is one
#: of the four characters `refs.escape_component` escapes, so it cannot occur in
#: a key component; a uuid cannot contain it either, which makes the split
#: unambiguous from the right or the left.
TIP_MARK_SEPARATOR = "#"

#: Width of the zero-padded counter in a `lastMark`. Wide enough that string
#: order and numeric order agree for any transcript this machine can hold
#: (SD-11's zero-padded-ints rule, applied to a value rather than to a key).
_TIP_MARK_WIDTH = 12


def tip_mark(line_count, last_uuid) -> str:
    """`"<lineCount:012d>#<lastUuid>"` — the `$max`-safe tip of a fragment.

    The one encoded value in this module, and it exists for one reason: the pair
    (line count, uuid of the line it counts to) must win or lose *together*.
    Stored as two fields under `$max` they do not — mongod applies an unordered
    bulk in whatever order it likes, and `mirror._requeue` appends unwritten
    operations at the tail on the stated ground that this module's operators
    commute — so the document ends up claiming that line 2 of a file has line
    1's uuid. As one string whose ordering is dominated by the zero-padded
    counter, the later observation wins as a unit and the tip is coherent in
    every order. :func:`tip_uuid` is the only reader that unpacks it.
    """
    return f"{int(line_count):0{_TIP_MARK_WIDTH}d}{TIP_MARK_SEPARATOR}{last_uuid}"


def tip_uuid(mark):
    """The uuid inside a `lastMark`, or None. Never raises on a foreign value."""
    if not isinstance(mark, str) or TIP_MARK_SEPARATOR not in mark:
        return None
    return mark.split(TIP_MARK_SEPARATOR, 1)[1] or None


@dataclass(frozen=True)
class Fragment:
    """One `agent-<id>.jsonl` file: a *piece* of an agent, never the agent.

    ``path`` is relative to the `~/.claude` root when one was given (so a
    rebuild's fingerprint does not embed this machine's home directory) and
    ``source_path`` is the absolute one the stat calls use.
    """

    agent_id: str
    session_id: object
    path: str
    first_uuid: object = None
    last_uuid: object = None
    first_parent_uuid: object = None
    line_count: int = 0
    record_count: int = 0
    #: The MINIMUM timestamp over the whole file (`TranscriptScan.first_ts`) —
    #: the agent-level `firstTs`, which is merged with `$min` across fragments.
    first_ts: object = None
    #: The FIRST record's own timestamp. Identity's member, and a different
    #: question: see :meth:`identity`.
    first_record_ts: object = None
    last_ts: object = None
    run_id: object = None
    meta: object = None
    has_meta: bool = False
    labels: object = None
    source_path: object = None

    def identity(self) -> dict:
        """The `$addToSet` element: the fragment's first record, and nothing else.

        Fixed key order, because BSON sub-document equality is field-order
        sensitive and `$addToSet` dedupes on exactly that (GD-24's opening law:
        `{s,n}` and `{n,s}` insert as two documents).

        Every member is therefore a property of ONE record — the first — and
        never an aggregate over the file. `firstTs` is :attr:`first_record_ts`,
        not :attr:`first_ts`: the latter is the minimum over every line, and the
        harness does write records out of timestamp order, so a single appended
        record stamped before the current minimum would mutate the identity and
        `$addToSet` would keep both spellings of one file forever (GD-26 forbids
        the delete). Appends can add lines; they cannot change the first record.
        """
        out = {"sessionId": self.session_id, "path": self.path,
               "firstUuid": self.first_uuid, "firstParentUuid": self.first_parent_uuid}
        if self.first_record_ts is not None:
            out["firstTs"] = ms.ts_fields(self.first_record_ts)["ts"]
        return {name: value for name, value in out.items() if value is not None}

    def tip(self) -> dict:
        """The mutable half, in **stored** shape — every leaf `$max`-safe.

        See the module docstring's stated deviation. `lastUuid` is absent on
        purpose: a uuid is not monotone, so `$max` on it would pair one
        observation's tip uuid with another's line count. It travels inside
        `lastMark`, and :func:`fragments_of` puts it back.

        `$max` is monotone under append only; after a `performRemoveByUuid` or
        a `performCompactTranscript` rewrite (GD-26) the stored tip is a stale
        high-water mark until sp-06's generation sweep supersedes the retracted
        records. Documented as D-5 rather than fixed here, because the fix keys
        the tip by generation and generation is the sweep's contract.
        """
        out = {"lineCount": self.line_count, "records": self.record_count}
        if self.last_ts is not None:
            out["lastTs"] = ms.ts_fields(self.last_ts)["ts"]
        if self.last_uuid:
            out["lastMark"] = tip_mark(self.line_count, self.last_uuid)
        return {name: value for name, value in out.items() if value is not None}


@dataclass(frozen=True)
class AgentObservation:
    """One agent, assembled from one or more fragments (R-48). Pure data."""

    agent_id: str
    fragments: tuple = ()
    sessions: tuple = ()
    files: tuple = ()
    run_id: object = None
    #: The five :data:`META_FIELDS`, in that order. `description`/`tool_use_id`
    #: are here because `.meta.json` is their only source for an agent whose
    #: launch pair Touch cannot see (MINOR 4 of attempt 2's critique).
    agent_type: object = None
    model: object = None
    spawn_depth: object = None
    description: object = None
    tool_use_id: object = None
    labels: object = None
    first_ts: object = None
    last_ts: object = None
    unconventional: bool = True
    marker_misplaced: bool = False
    #: Which fragment supplied the meta / the marker, for diagnostics only.
    meta_from: object = None
    labels_from: object = None


@dataclass(frozen=True)
class SpawnObservation:
    """The `(tool_use, tool_result)` pair that launched an Agent-tool agent.

    Identity is `recordUuid` + `toolUseId` (e423c:164, CONVO-9). The line
    number is in :attr:`file_hint` and nowhere else: offset-as-cursor is fine,
    offset-as-identity never — a `performRemoveByUuid` renumbers every line
    after the removal and an identity that moves is not an identity.
    """

    agent_id: str
    record_uuid: str
    tool_use_id: object = None
    session_id: object = None
    file_hint: object = None
    agent_type: object = None
    model: object = None
    description: object = None
    result_seen: bool = False
    result_ts: object = None
    result_uuid: object = None
    source_path: object = None


@dataclass
class AgentScan:
    """What one pass over a corpus produced. Counters, never exceptions."""

    agents: tuple = ()
    spawns: tuple = ()
    fragments: tuple = ()
    skipped: dict = field(default_factory=dict)

    def observations(self):
        """`(kind, observation)` pairs — the shape `Mirror.rebuild` consumes."""
        for obs in self.agents:
            yield "agent", obs
        for obs in self.spawns:
            yield "agentSpawn", obs


def _skips() -> dict:
    """The counter set, declared once so every scan has every key.

    A missing key and a zero are the same fact to a reader and different facts
    to a test; declaring the set makes "nothing was skipped" assertable.
    """
    return {
        "unreadable_meta": 0,        # .meta.json exists and is not a JSON object
        "meta_conflict": 0,          # two fragments' metas disagree on a field
        "marker_conflict": 0,        # more than one fragment carries a marker
        "unchained_fragment": 0,     # fragment reachable from no chain head (a cycle)
        "no_first_record": 0,        # file exists, first record not readable yet
        # A `read_fragment` direct-call counter ONLY: `scan` never reaches it,
        # because it tests `ingest.agent_id_for_path` before it calls in. Kept
        # because `read_fragment` is public and `mirror.py`'s per-path arm may
        # hand it anything the walk found.
        "not_an_agent_file": 0,      # asked to read a path with no 17-hex agentId
        "spawn_without_result": 0,   # tool_use seen, agentId not yet knowable
        "spawn_without_tool_use": 0, # result names an agent whose tool_use is gone
        "spawn_agent_conflict": 0,   # one toolUseId naming two agentIds
        "unstattable_hint": 0,       # the file vanished between read and stat
    }


def meta_path_for(path) -> str:
    """`agent-<id>.jsonl` ⇒ `agent-<id>.meta.json`."""
    text = os.fspath(path)
    return text[:-len(".jsonl")] + META_SUFFIX if text.endswith(".jsonl") else text + META_SUFFIX


def read_meta(path, *, skipped=None):
    """The `.meta.json` beside a transcript, or None. Never raises (R-48).

    Optional by specification, and empirically: `e423cd3c`'s 2-line
    continuation of `a2fc883c96ff7b837` has none. Absence is not an error and
    not a counter — it is the normal shape of a continuation. A file that
    exists and is *unusable* IS counted, because that is a fact about the
    harness rather than about this run.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            body = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        if skipped is not None:
            skipped["unreadable_meta"] = skipped.get("unreadable_meta", 0) + 1
        return None
    if not isinstance(body, dict):
        if skipped is not None:
            skipped["unreadable_meta"] = skipped.get("unreadable_meta", 0) + 1
        return None
    out = {name: body[name] for name in META_FIELDS if body.get(name) is not None}
    return out or None


def read_fragment(path, *, root=None, scan=None, skipped=None) -> Fragment:
    """Read one `agent-<id>.jsonl` into a :class:`Fragment`.

    The transcript itself is parsed by `ingest.read_transcript` — this module
    owns agents, not the record format (GD-15). ``scan`` lets a caller that
    already read the file hand its `TranscriptScan` over rather than pay for a
    second read.

    First/last uuid come from the records **in line order**, not from the
    first and last physical lines: a torn tail is deferred by the tailer and an
    unparsable line is bucketed elsewhere, so "the last record" and "the last
    line" are different questions and only the first one stitches a chain.

    Two timestamps come back for the head of the file and they are two
    different facts: ``first_record_ts`` is the first record's own stamp (the
    identity member — append-invariant) and ``first_ts`` is the scan's minimum
    over the file (the agent-level `$min`). They differ exactly when the
    harness writes a record out of order, which it does.
    """
    text = os.fspath(path)
    agent_id = ingest.agent_id_for_path(text)
    if not agent_id:
        if skipped is not None:
            skipped["not_an_agent_file"] = skipped.get("not_an_agent_file", 0) + 1
        raise AgentsError(
            f"{text!r} is not an agent transcript: an agents document is keyed by the "
            f"full 17-hex agentId, and this path carries none (GD-7)")
    scan = scan if scan is not None else ingest.read_transcript(text, root=root)
    records = sorted(scan.records, key=lambda obs: obs.line_no)
    first = records[0] if records else None
    last = records[-1] if records else None
    prompt = prompt_text_of(first.body) if first is not None and first.type == "user" else ""
    labels = labels_from_prompt(prompt) if prompt else None
    if labels is not None and labels.empty and not labels.marker_misplaced:
        labels = None
    meta = read_meta(meta_path_for(text), skipped=skipped)
    return Fragment(
        agent_id=agent_id,
        session_id=scan.session_id,
        path=(first.source_path if first is not None else _relative(text, root)),
        first_uuid=(first.uuid if first is not None else None),
        last_uuid=(last.uuid if last is not None else None),
        first_parent_uuid=(first.parent_uuid if first is not None else None),
        line_count=scan.lines,
        record_count=len(records),
        first_ts=scan.first_ts,
        first_record_ts=(first.ts if first is not None else None),
        last_ts=scan.last_ts,
        run_id=scan.run_id,
        meta=meta,
        has_meta=meta is not None,
        labels=labels,
        source_path=os.path.abspath(text),
    )


def _relative(path, root):
    if not root:
        return os.fspath(path)
    try:
        return os.path.relpath(os.fspath(path), os.fspath(root))
    except ValueError:                                          # different drive
        return os.fspath(path)


def _sort_key(fragment):
    """Deterministic tie-break for fragments the chain cannot order.

    `(firstTs, path)`: the timestamp is the honest answer and the path is what
    makes two same-instant fragments still order the same way on every pass —
    without it, a rebuild and a live tail could emit them in different orders
    and GD-25's fingerprint would differ on a corpus nobody changed.

    The timestamp read here is the first *record's*, on both paths: the stored
    element's `firstTs` IS `Fragment.first_record_ts` (:meth:`Fragment.identity`),
    so a live :class:`Fragment` must not tie-break on the scan minimum or
    :func:`assemble` and :func:`fragments_of` could order one pair two ways.
    """
    ts = ((fragment.first_record_ts if fragment.first_record_ts is not None
           else fragment.first_ts) if isinstance(fragment, Fragment)
          else fragment.get("firstTs"))
    path = fragment.path if isinstance(fragment, Fragment) else fragment.get("path") or ""
    return (ts is None, ts or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), path)


def _chain_fields(item):
    if isinstance(item, Fragment):
        return item.first_uuid, item.last_uuid, item.first_parent_uuid
    return item.get("firstUuid"), item.get("lastUuid"), item.get("firstParentUuid")


def order_fragments(fragments, *, skipped=None) -> tuple:
    """Order fragments by the `parentUuid → uuid` stitch chain (R-48).

    The chain, on the live pair: `dd469822`'s last record is
    `22745683-c12a-…`, and `e423cd3c`'s first record's `parentUuid` IS
    `22745683-c12a-…`. So a fragment's successor is the fragment whose first
    record's parent is this one's last record — a link the *files* attest to,
    unlike directory order (alphabetical: `dd…` before `e4…`, right here by
    luck) and unlike mtime (a copied fixture has none of the original's).

    A fragment whose parent was compacted away by `performCompactTranscript`
    becomes its own chain head and is ordered by `(firstTs, path)` — a hole in
    the middle leaves two honest chains, which is not an anomaly and is not
    counted. `unchained_fragment` counts only what the walk over heads cannot
    reach at all, i.e. a cycle: those fragments are appended in the same order
    and never dropped, because an agent with a broken chain is still an agent
    and hiding the tail would hide the break.
    """
    items = list(fragments)
    by_parent = {}
    last_uuids = set()
    for item in items:
        first_uuid, last_uuid, first_parent = _chain_fields(item)
        if last_uuid:
            last_uuids.add(last_uuid)
        if first_parent:
            by_parent.setdefault(first_parent, []).append(item)
    heads = [item for item in items
             if not _chain_fields(item)[2] or _chain_fields(item)[2] not in last_uuids]
    ordered = []
    seen = set()
    for head in sorted(heads, key=_sort_key):
        cursor = head
        while cursor is not None and id(cursor) not in seen:
            ordered.append(cursor)
            seen.add(id(cursor))
            successors = sorted(by_parent.get(_chain_fields(cursor)[1] or "", []),
                                key=_sort_key)
            cursor = next((item for item in successors if id(item) not in seen), None)
    leftovers = [item for item in items if id(item) not in seen]
    if leftovers and skipped is not None:
        skipped["unchained_fragment"] = skipped.get("unchained_fragment", 0) + len(leftovers)
    ordered.extend(sorted(leftovers, key=_sort_key))
    return tuple(ordered)


def _identity_key(element):
    """What makes two stored elements the SAME fragment: its first record."""
    return element.get("firstUuid") or f"path:{element.get('path')}"


def _collapse_identities(elements) -> list:
    """One element per fragment, whatever the collection actually holds.

    Belt and braces over :meth:`Fragment.identity`'s append-invariance, and the
    only repair available: `$addToSet` dedupes on exact field-order-sensitive
    equality, so an identity member that turns out to vary — attempt 2's
    `firstTs`, which was the whole-file minimum rather than the first record's
    own stamp — leaves TWO permanent elements for one file, and GD-26 forbids
    the delete that would remove one. Documents written before that fix exist;
    they are repaired here, at read time, because they cannot be repaired at
    rest. Fields are merged with the store's own `$min` so two readers of one
    document cannot disagree about which spelling won.
    """
    grouped = {}
    order = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        key = _identity_key(element)
        if key not in grouped:
            grouped[key] = dict(element)
            order.append(key)
            continue
        base = grouped[key]
        for name, value in element.items():
            if value is None:
                continue
            if base.get(name) is None or base[name] == value:
                base[name] = value
            else:
                base[name] = _min_observed([base[name], value])
    return [grouped[key] for key in order]


def fragments_of(doc) -> tuple:
    """R-48's `fragments[]` — recombined and chain-ordered — from a stored doc.

    This is the reader's contract the module docstring promises: identity
    elements plus `fragmentTips` back into
    `{sessionId, path, firstUuid, lastUuid, lineCount, …}`, in chain order, ONE
    entry per file. The API (sp-12) and the page (sp-13) call this and never
    touch either stored field, so the storage split is invisible above this
    line — including the repair :func:`_collapse_identities` performs.
    """
    if not isinstance(doc, dict):
        return ()
    tips = doc.get("fragmentTips") or {}
    merged = []
    for element in _collapse_identities(doc.get("fragments") or []):
        item = dict(element)
        tip = tips.get(element.get("firstUuid")) if isinstance(tips, dict) else None
        if isinstance(tip, dict):
            item.update({name: value for name, value in tip.items() if name != "lastMark"})
            # `lastMark` is storage, `lastUuid` is R-48's field name: the mark
            # is unpacked here and never leaves this function, so no reader
            # above this line knows the encoding exists.
            last_uuid = tip_uuid(tip.get("lastMark"))
            if last_uuid:
                item["lastUuid"] = last_uuid
        merged.append(item)
    return order_fragments(merged)


def _min_observed(values):
    """`$min` over ``values`` — computed by the store's own operator, not a copy.

    :func:`assemble` (the `--rebuild` arm) and :func:`map_agent` folding one
    fragment at a time (the `--backfill` arm) must resolve a disagreement
    identically, or the two ingest modes store different documents for the same
    agent and R-56's wipe/rebuild-equivalence arm compares two different
    corpora. Re-spelling BSON's canonical type order here would be a second
    answer waiting to drift from the first, so the fold runs through
    `mongo_store.apply_update` on a throwaway one-field document: whatever
    `$min` means there, it means here, including the cases nobody enumerated
    (a bool against an int, a `null` against a Date).

    Values are expected non-None; callers filter, because "unobserved" is an
    absent write and not a low value.
    """
    doc = None
    for value in values:
        doc = ms.apply_update(doc, ms.op_min({"v": value}))
    return None if doc is None else doc["v"]


def _merge_labels(marker_bearing, unconventional) -> object:
    """One :class:`Labels` from every marker-bearing fragment, leaf by leaf.

    Per-leaf `$min`, matching :func:`map_agent`'s `$min` on `labels.<leaf>`
    exactly: a continuation whose marker states `name=` supplies the name even
    when the chain-first fragment's marker did not. `marker_misplaced` is `$max`
    (`any`), mirroring the mapper's `$max` on `markerMisplaced`.
    """
    if not marker_bearing:
        return None
    leaves = {}
    extra = {}
    for item in marker_bearing:
        for name, value in item.labels.fields().items():
            leaves.setdefault(name, []).append(value)
        for name, value in (item.labels.extra or {}).items():
            extra.setdefault(name, []).append(value)
    merged = {name: _min_observed(values) for name, values in leaves.items()}
    return Labels(
        plan=merged.get("plan"), stage=merged.get("stage"), role=merged.get("role"),
        attempt=merged.get("attempt"), name=merged.get("name"),
        parent=merged.get("parent"), root=merged.get("root"),
        unconventional=unconventional,
        marker_misplaced=any(item.labels.marker_misplaced for item in marker_bearing),
        extra=({name: _min_observed(values) for name, values in sorted(extra.items())}
               or None),
    )


def assemble(fragments, *, skipped=None) -> AgentObservation:
    """Fragments ⇒ one :class:`AgentObservation` (R-48's union write).

    One agentId, one document, however many session directories the transcript
    was split across. Every scalar that two fragments can disagree about is
    merged with :func:`_min_observed` — the store's own `$min` — because the
    `--backfill` arm reaches the same document by folding one fragment at a time
    through that operator and the two arms must agree (see the module
    docstring). A disagreement is counted as well as resolved: `meta_conflict`
    per field, `marker_conflict` per extra marker-bearing fragment.

    A fragment whose first record is not readable yet has no identity, so it
    contributes nothing but the document's existence — no `fragments[]` element,
    no `files[]`/`sessions[]` entry — and is counted `no_first_record`. It is
    still carried on :attr:`AgentObservation.fragments`, because "a file we can
    see but not yet read" is a fact a diagnostic wants.
    """
    items = list(fragments)
    if not items:
        raise AgentsError("an agent is assembled from at least one fragment")
    agent_ids = {item.agent_id for item in items}
    if len(agent_ids) != 1:
        raise AgentsError(
            f"fragments of {sorted(agent_ids)} cannot be one agent — the agentId is "
            f"the ONLY grouping key (R-48/SESSIONJSONL-3)")
    ordered = order_fragments(items, skipped=skipped)
    agent_id = ordered[0].agent_id
    identified = [item for item in ordered if item.first_uuid]
    if len(identified) != len(ordered) and skipped is not None:
        skipped["no_first_record"] = (skipped.get("no_first_record", 0)
                                      + len(ordered) - len(identified))

    meta_bearing = [item for item in ordered if item.has_meta]
    fields = {}
    for item in meta_bearing:
        for name, value in (item.meta or {}).items():
            if name in fields and value not in fields[name] and skipped is not None:
                skipped["meta_conflict"] = skipped.get("meta_conflict", 0) + 1
            fields.setdefault(name, []).append(value)
    meta = {name: _min_observed(values) for name, values in fields.items()}

    marker_bearing = [item for item in ordered if item.labels is not None]
    if len(marker_bearing) > 1 and skipped is not None:
        skipped["marker_conflict"] = skipped.get("marker_conflict", 0) + len(marker_bearing) - 1
    # `$min` over EVERY fragment's flag, not over the marker-bearing ones: the
    # mapper writes `unconventional` on every observation, so a markerless
    # fragment contributes `True` on the backfill path and must contribute it
    # here too. `False` still wins, which is R-28's precedence.
    unconventional = bool(_min_observed(
        [bool(item.labels.unconventional) if item.labels is not None else True
         for item in ordered]))
    labels = _merge_labels(marker_bearing, unconventional)

    times = [item.first_ts for item in ordered if item.first_ts is not None]
    ends = [item.last_ts for item in ordered if item.last_ts is not None]
    return AgentObservation(
        agent_id=agent_id,
        fragments=ordered,
        sessions=tuple(sorted({item.session_id for item in identified if item.session_id})),
        files=tuple(sorted({item.path for item in identified if item.path})),
        run_id=_min_observed([item.run_id for item in ordered if item.run_id]),
        agent_type=meta.get("agentType"),
        model=meta.get("model"),
        spawn_depth=meta.get("spawnDepth"),
        description=meta.get("description"),
        tool_use_id=meta.get("toolUseId"),
        labels=labels,
        first_ts=min(times) if times else None,
        last_ts=max(ends) if ends else None,
        unconventional=unconventional,
        marker_misplaced=any(item.labels is not None and item.labels.marker_misplaced
                             for item in ordered),
        meta_from=meta_bearing[0].path if meta_bearing else None,
        labels_from=marker_bearing[0].path if marker_bearing else None,
    )


# --- reading: the spawn locator (R-48 / CONVO-9) --------------------------


@dataclass(frozen=True)
class HintStatus:
    """The answer :func:`check_file_hint` gives. `stale` is not an error."""

    valid: bool
    reason: str
    observed: object = None


def file_hint(path, line, *, root=None, ts=None, skipped=None):
    """The perishable `{path, line, stDev, ino, size, ts}` cache (R-48).

    A *cache*, explicitly: "which line the subagent is on" was adopted at
    e423c:164 as a hint validated against `(st_dev, st_ino, size)` and
    invalidated on mismatch — never as identity. The identity is
    `spawn.recordUuid`, which survives every rewrite the CLI performs.

    ``st_dev`` is stored even though R-48's field list names only `ino`,
    because the validation triple R-48 *also* specifies is
    `(st_dev, st_ino, size)`: an inode number is unique within a filesystem and
    the corpus legitimately spans two (`$HOME` and `/tmp`), so a hint that
    stored only the inode could not evaluate its own rule.
    """
    text = os.fspath(path)
    try:
        stat = os.stat(text)
    except OSError:
        if skipped is not None:
            skipped["unstattable_hint"] = skipped.get("unstattable_hint", 0) + 1
        return None
    moment = ts if ts is not None else datetime.datetime.fromtimestamp(
        stat.st_mtime, datetime.timezone.utc)
    return {
        "path": _relative(text, root),
        "line": int(line),
        "stDev": int(stat.st_dev),
        "ino": int(stat.st_ino),
        "size": int(stat.st_size),
        "ts": ms.ts_fields(moment)["ts"],
    }


def check_file_hint(hint, *, root=None) -> HintStatus:
    """Is a stored hint still usable? (R-48)

    The reading half's function, not the reducer's: it stats a file, and
    :func:`reduce` is pure. Callers are the "jump to spawn" path (sp-12) and
    the tests. A stale answer is a normal outcome — the hint is kept for
    diagnostics and the *record* is still reachable by uuid.
    """
    if not isinstance(hint, dict) or not hint.get("path"):
        return HintStatus(False, "no hint recorded")
    path = hint["path"]
    if root and not os.path.isabs(path):
        path = os.path.join(os.fspath(root), path)
    try:
        stat = os.stat(path)
    except OSError as exc:
        return HintStatus(False, f"source unreadable: {exc.strerror or exc}")
    observed = {"stDev": int(stat.st_dev), "ino": int(stat.st_ino), "size": int(stat.st_size)}
    for name in ("stDev", "ino", "size"):
        if name in hint and hint[name] != observed[name]:
            return HintStatus(
                False,
                f"{name} changed ({hint[name]} -> {observed[name]}) — the line number is "
                f"a cache of a file that moved; resolve by spawn.recordUuid instead",
                observed)
    return HintStatus(True, "matches (stDev, ino, size)", observed)


def spawn_record_filter(spawn) -> dict:
    """The `records.findOne` filter that resolves "jump to spawn" (R-48).

    A *filter*, returned rather than executed, and that is the whole point:
    the spawn record is fetched from the mirror by uuid, never by re-reading
    the transcript at a stored offset. This module contains no code path that
    opens a file to answer "where was this agent spawned".
    """
    if isinstance(spawn, dict) and spawn.get("recordUuid"):
        return {"_id": refs.record_key(spawn["recordUuid"])}
    raise AgentsError("this spawn has no recordUuid; there is nothing to resolve")


def _tool_uses(body):
    """`(toolUseId, name, input)` for every tool_use block of a record."""
    message = body.get("message") if isinstance(body, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
            out.append((block["id"], block.get("name"), block.get("input")))
    return out


def _tool_results(body):
    """`toolUseId` for every tool_result block of a record."""
    message = body.get("message") if isinstance(body, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block["tool_use_id"] for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result"
            and isinstance(block.get("tool_use_id"), str)]


def find_spawns(scan, *, root=None, skipped=None) -> tuple:
    """The Agent-tool spawn pairs in one session transcript (R-48/CONVO-9).

    Two records, one fact:

    1. an `assistant` record carrying `tool_use{id, name in SPAWN_TOOLS}` —
       this is the record a user means by "where was it spawned", so it is
       `spawn.recordUuid`;
    2. the following `user` record whose `tool_result.tool_use_id` matches and
       whose `toolUseResult` carries the **agentId** — the only place the
       harness ever states the link between a launch and a 17-hex id.

    A tool_use with no result yet is a *running* agent whose id is not
    knowable from this file (it is knowable from its own transcript, which is
    :func:`read_fragment`'s job) — counted, not guessed. A Workflow run has no
    such pair at all: it launches once and its agents are named only by their
    filenames, which is why the fragment arm creates documents on its own.
    """
    pending = {}
    spawns = []
    seen = {}
    for obs in sorted(scan.records, key=lambda item: item.line_no):
        for tool_use_id, name, arguments in _tool_uses(obs.body):
            if name in SPAWN_TOOLS:
                pending[tool_use_id] = (obs, arguments if isinstance(arguments, dict) else {})
        result = obs.body.get("toolUseResult") if isinstance(obs.body, dict) else None
        if not isinstance(result, dict):
            continue
        agent_id = next((result[key] for key in _RESULT_AGENT_KEYS
                         if is_agent_id(result.get(key))), None)
        if agent_id is None:
            continue
        ids = _tool_results(obs.body)
        tool_use_id = next((item for item in ids if item in pending), ids[0] if ids else None)
        # `seen` is consulted BEFORE the launch is popped, and before the
        # early return: the conflict this counts — one toolUseId naming two
        # agentIds — is by construction a *second* result for a toolUseId whose
        # launch the first result already consumed, so a check placed after the
        # pop can never fire and miscounts the case as `spawn_without_tool_use`.
        prior = seen.get(tool_use_id) if tool_use_id else None
        if prior is not None and prior != agent_id and skipped is not None:
            skipped["spawn_agent_conflict"] = skipped.get("spawn_agent_conflict", 0) + 1
        launch = pending.pop(tool_use_id, None) if tool_use_id else None
        if launch is None:
            # Only a result whose launch was never seen at all is "gone". A
            # second result for a consumed launch is a duplicate or a conflict,
            # already counted above, and calling it a missing tool_use would
            # report one anomaly as two.
            if prior is None and skipped is not None:
                skipped["spawn_without_tool_use"] = skipped.get("spawn_without_tool_use", 0) + 1
            continue
        record, arguments = launch
        seen[tool_use_id] = agent_id
        spawns.append(SpawnObservation(
            agent_id=agent_id,
            record_uuid=record.uuid,
            tool_use_id=tool_use_id,
            session_id=record.session_id,
            file_hint=file_hint(_absolute(record.source_path, root, scan.path),
                                record.line_no, root=root, skipped=skipped),
            agent_type=result.get("agentType") or arguments.get("subagent_type"),
            model=result.get("resolvedModel"),
            description=arguments.get("description"),
            result_seen=True,
            result_ts=obs.ts,
            result_uuid=obs.uuid,
            source_path=record.source_path,
        ))
    if pending and skipped is not None:
        skipped["spawn_without_result"] = skipped.get("spawn_without_result", 0) + len(pending)
    return tuple(spawns)


def _absolute(source_path, root, fallback):
    """The on-disk path of a record whose stored path may be root-relative."""
    if source_path and os.path.isabs(source_path):
        return source_path
    if source_path and root:
        return os.path.join(os.fspath(root), source_path)
    return fallback


# --- reading: the corpus pass ---------------------------------------------


def _corpus_scans(cwd, root, env, paths):
    """The `TranscriptScan` for every in-scope file, read ONCE per generation.

    A whole-corpus pass (``paths is None``, i.e. a `--rebuild`) goes through
    `ingest._transcript_walk`, which is the memo `ingest.py`'s own sources
    already fill. That matters because SD-1 registers this module's two sources
    separately: `mirror.iter_sources` calls :func:`iter_agent_observations` and
    :func:`iter_agent_spawn_observations` one after the other, and each used to
    walk and re-parse **every** transcript — the agent files for one and the
    session files for the other, both reading both. Sharing ingest's walk makes
    a rebuild read the corpus once for records, usage, runs, agents and spawns
    together.

    Reused rather than re-implemented, for the same reason `ingest._in_scope`
    is: a second memo is a second answer about when a file is stale. Its key is
    the walk's `(path, dev, ino, size, mtime_ns)` tuples, so an appended,
    replaced or removed file busts it — a live tail cannot be served a stale
    scan, which is the property that makes the memo invisible.

    An explicit ``paths`` list shares the memo too, through
    `ingest._cached_transcript` — the same entry `ingest.py`'s own per-path arm
    fills (`ingest._transcript_scans`), and the one `_transcript_walk` is built
    out of. Calling `ingest.read_transcript` directly here would have made this
    function's advertised property true of half of it: a caller naming a file
    ingest has already read this generation would re-parse it (NIT 7 of attempt
    3's critique). The memo is single-slot, so a long list still re-reads —
    what it cannot do is re-read a file whose scan is in hand.
    """
    if paths is None:
        return ingest._transcript_walk(root, cwd, env)
    return [ingest._cached_transcript(one, root) for one in paths
            if ingest.agent_id_for_path(one) or ingest.is_transcript_path(one)]


def scan(*, cwd=None, root=None, env=None, paths=None) -> AgentScan:
    """Every agent this project owns, grouped by agentId (never by session).

    Scope is `ingest.iter_transcript_paths` — `sessions.scoped_dirs`, i.e. the
    cwd slug plus the slugs named in `.session-aliases`, never `projects/*`
    (R-25 as amended). The four foreign `/tmp` slug directories on this machine
    are not this project's agents.

    Agent transcripts become fragments and then agents; session transcripts are
    read for their spawn pairs only. Both passes read files this module does
    not own the format of, through `ingest.read_transcript` — and both read
    them from the one shared pass :func:`_corpus_scans` describes.
    """
    root = sess.claude_root(env) if root is None else os.fspath(root)
    skipped = _skips()
    by_agent = {}
    spawns = []
    fragments = []
    for one in _corpus_scans(cwd, root, env, paths):
        if ingest.agent_id_for_path(one.path):
            fragment = read_fragment(one.path, root=root, scan=one, skipped=skipped)
            fragments.append(fragment)
            by_agent.setdefault(fragment.agent_id, []).append(fragment)
        elif ingest.is_transcript_path(one.path):
            spawns.extend(find_spawns(one, root=root, skipped=skipped))
    agents = tuple(assemble(items, skipped=skipped)
                   for _agent_id, items in sorted(by_agent.items()))
    return AgentScan(agents=agents, spawns=tuple(spawns),
                     fragments=tuple(fragments), skipped=skipped)


# --- mapping (SD-1): pure, no I/O, no clock -------------------------------


def _only_ours(ops):
    """GD-15's wall, enforced structurally rather than by review.

    This module reads the same files `ingest.py` and `sessions.py` read. "Don't
    write their collections" therefore cannot be a convention: it is a gate
    every mapper returns through, and `derived` is on the far side of it too —
    the reducer writes that one, through :meth:`Reduction.operations`, and a
    mapper that could emit a derived document would be a second derivation
    site (GD-23 allows exactly one).
    """
    for collection, _key, _update in ops:
        if collection not in COLLECTIONS:
            raise AgentsError(
                f"agents.py may only write {list(COLLECTIONS)}, not {collection!r} — "
                f"records/run_nodes/usage are R-47/R-49's (ingest.py), sessions are "
                f"R-46's (sessions.py), and `derived` is the reducer's alone (GD-23)")
    return ops


def _plain_field(value, what):
    """A dotted-path component mongod will read as ONE field name.

    `fragmentTips.<firstUuid>.<leaf>` builds an update path out of harness
    text, and `ingest._launch_paths` refuses exactly this hazard by name: a `.`
    in the component silently becomes a nesting level and a leading `$` an
    operator, so the stored shape of a field stops being stable (GD-24). The
    live path cannot produce one — `ingest.bucket_of` routes a record whose
    `uuid` fails its UUID pattern to `stream_meta`, so a fragment head is
    always a uuid — but :func:`_fragment_of` accepts "the dict a replay
    carries", where the value is whatever the caller wrote. Refused here as an
    :class:`AgentsError`, which is the exception `mirror.Mapper` attributes to
    this module (NIT 4 of attempt 3's critique).
    """
    text = value if isinstance(value, str) else str(value)
    if not text or "." in text or text.startswith("$") or "\x00" in text:
        raise AgentsError(
            f"{what} {value!r} is not a plain field name — it becomes the dotted path "
            f"`fragmentTips.{text}.…` and mongod would read the dot as a nesting level "
            f"(GD-24: the stored shape of a field is stable)")
    return text


def _labels_of(value):
    """A :class:`Labels` from a `Labels`, from a dict, or None.

    The dict arm is not decoration: a serialized observation can only carry its
    labels as a mapping, and the mappers call `labels.fields()` — so without the
    coercion the documented "or the plain dict a replay hands back" path raises
    `AttributeError` and escapes the :class:`AgentsError` funnel that
    `mirror.Mapper` turns into a `MapperError` naming this module.

    `unconventional` is *derived* rather than defaulted when the mapping does
    not state it: :func:`labels_from_prompt` computes it as `not name`, and
    every `Labels` this module builds satisfies that invariant, so a replay dict
    that carries a `name` and omits the flag means "named", not "unnamed". Left
    to the dataclass default it meant the opposite, and R-28's precedence — a
    named agent is not unconventional — was inverted on the replay path alone
    (NIT 5 of attempt 3's critique).
    """
    if value is None or isinstance(value, Labels):
        return value
    if isinstance(value, dict):
        try:
            built = Labels(**value)
        except TypeError as exc:
            raise AgentsError(f"unusable Labels: {exc}") from None
        return built if "unconventional" in value else replace(
            built, unconventional=not built.name)
    raise AgentsError(f"unusable Labels: {type(value).__name__}")


def _as_observation(observation, cls):
    """Accept a dataclass or the plain dict a replay/fixture hands back.

    Two coercions, both about the same thing — a serialized observation carries
    its labels as a mapping, and the fields derived from those labels have to be
    derived again on the way back in. :attr:`AgentObservation.unconventional`
    defaults to `True` (the honest default for an observation that says
    nothing), so a dict stating `labels.name` and no top-level flag stored
    `unconventional: true` beside the very name that disproves it. The coerced
    labels answer whenever the dict itself did not (NIT 5, attempt 3).
    """
    if isinstance(observation, cls):
        return observation
    if isinstance(observation, dict):
        try:
            built = cls(**observation)
        except TypeError as exc:
            raise AgentsError(f"unusable {cls.__name__}: {exc}") from None
        labels = getattr(built, "labels", None)
        coerced = _labels_of(labels)
        changes = {} if coerced is labels else {"labels": coerced}
        if (isinstance(coerced, Labels) and hasattr(built, "unconventional")
                and "unconventional" not in observation):
            changes["unconventional"] = bool(coerced.unconventional)
        return replace(built, **changes) if changes else built
    raise AgentsError(
        f"expected a {cls.__name__} or a dict, got {type(observation).__name__}")


def _fragment_of(item):
    """A :class:`Fragment` from a dataclass or from the dict a replay carries."""
    if isinstance(item, Fragment):
        return item
    if isinstance(item, dict):
        try:
            built = Fragment(**item)
        except TypeError as exc:
            raise AgentsError(f"unusable fragment: {exc}") from None
        return replace(built, labels=_labels_of(built.labels))
    raise AgentsError(f"unusable fragment: {type(item).__name__}")


def map_agent(observation):
    """`agent` ⇒ one `agents` upsert, `_id = <agentId>` (R-28/R-48).

    Every operator here is chosen so that the stored document does not depend
    on the order the fragments were observed in — GD-25's property, which the
    two `a2fc883c` files exist to test:

    * `sessions` / `files` / `fragments` are `$addToSet` (a union, by
      definition order-free); the fragment elements are identity-only, so a
      growing file adds one element and not one per tick, and a fragment with
      no readable first record adds none at all (its identity would be a
      *different* sub-document, which `$addToSet` would add a second time);
    * every leaf of `fragmentTips.<firstUuid>` is `$max`: `lineCount`,
      `records` and `lastTs` are monotone under append, and the tip uuid rides
      inside :func:`tip_mark`'s `lastMark` so that it wins or loses together
      with the counter it belongs to. There is no `$set` in this function —
      `$set` does not commute, and `mirror.py` reorders and re-queues these
      operations on the stated ground that they do;
    * `firstTs` is `$min` and `lastTs` is `$max` — the earliest start with the
      latest end is the only order-independent answer when three sources
      observe the same agent;
    * the scalars a *fragment* can disagree about (the five
      :data:`META_FIELDS`; `name`, `root` and the labels from the marker)
      are `$min` per leaf, and are written **only** by an observation that
      actually has them. That combination is what makes "the meta-bearing
      fragment wins" true without either observation seeing the other; see the
      module docstring;
    * `unconventional` is `$min` on a bool, so `False` — "some observation
      found a name" — wins over `True`, which is the precedence R-28 wants;
      `markerMisplaced` is `$max`, so an observed GD-9 violation is not
      forgotten by the next clean observation.

    What is deliberately absent: `state`, `liveness`, `status`, and any
    verdict. GD-23 keeps a mirror document to observations; running/done/
    unknown is :func:`reduce`'s, at read time, from `now()`.
    """
    obs = _as_observation(observation, AgentObservation)
    if not is_agent_id(obs.agent_id):
        raise AgentsError(
            f"agentId {obs.agent_id!r} is not 17-hex; a legacy 8-hex id belongs to the "
            f"legacy: namespace and never to `agents` (GD-11/R-48)")
    # Through :func:`node_key`, not `refs.agent_key` directly: GD-7's identity
    # function is the one this module exports, and an exported identity nothing
    # calls is an identity no test can pin (NIT 6 of attempt 3's critique). The
    # check above stays because it carries the *message* — `node_ref`'s own
    # raise is correct and generic, this one names the legacy namespace the
    # 8-hex id belongs to. Same key either way, asserted in tests/test_agents.py.
    key = node_key(agent_id=obs.agent_id)
    fragments = [_fragment_of(item) for item in obs.fragments]

    ops = [ms.op_set_on_insert({"provenance": PROVENANCE})]

    scalars = {"unconventional": bool(obs.unconventional)}
    for name, value in (("runId", obs.run_id), ("agentType", obs.agent_type),
                        ("model", obs.model), ("spawnDepth", obs.spawn_depth),
                        ("description", obs.description),
                        ("toolUseId", obs.tool_use_id)):
        if value is not None:
            scalars[name] = value
    labels = obs.labels
    if labels is not None:
        for name, value in labels.fields().items():
            # `name` and `root` are top-level in GD-24's table (they carry the
            # `{root:1,name:1}` index); the rest are the label layer and stay
            # namespaced, so a label can never be mistaken for a harness fact.
            scalars[name if name in ("name", "root") else f"labels.{name}"] = value
        if labels.extra:
            for name, value in sorted(labels.extra.items()):
                scalars[f"labels.extra.{name}"] = value
    ops.append(ms.op_min(scalars))
    if obs.marker_misplaced:
        ops.append(ms.op_max({"markerMisplaced": True}))

    # A fragment with no `firstUuid` has no identity yet — the file exists but
    # its first line is not readable (empty, or torn mid-write and deferred by
    # the tailer). Emitting an element for it writes `{path: …}` now and
    # `{sessionId, path, firstUuid, …}` one tick later, and `$addToSet` dedupes
    # on exact field-order-sensitive equality: those are two elements for one
    # file, permanently (GD-26 forbids the delete that would fix it).
    identified = [item for item in fragments if item.first_uuid]

    sets = {}
    if obs.sessions:
        sets["sessions"] = {"$each": list(obs.sessions)}
    if obs.files:
        sets["files"] = {"$each": list(obs.files)}
    if identified:
        sets["fragments"] = {"$each": [item.identity() for item in identified]}
    if sets:
        ops.append(ms.op_add_to_set(sets))

    tips = {}
    for item in identified:
        head = _plain_field(item.first_uuid, "a fragment's firstUuid")
        for name, value in item.tip().items():
            tips[f"fragmentTips.{head}.{name}"] = value
    if tips:
        ops.append(ms.op_max(tips))

    if obs.first_ts is not None:
        ops.append(ms.op_min({"firstTs": ms.ts_fields(obs.first_ts)["ts"]}))
    if obs.last_ts is not None:
        ops.append(ms.op_max({"lastTs": ms.ts_fields(obs.last_ts)["ts"]}))

    return _only_ours([("agents", key, ms.merge_ops(*ops, collection="agents"))])


def map_agent_spawn(observation):
    """`agentSpawn` ⇒ the `spawn` sub-document on the same `agents` doc (R-48).

    A second writer of one document, and its field set is stated exactly
    because "disjoint by construction" was once claimed and was not true. This
    mapper writes `spawn.*` (its own sub-document), `resultSeen`, `resultTs`,
    and the two columns `toolUseId` and `description`; :func:`map_agent` writes
    `fragments`, `fragmentTips`, `files`, `sessions`, `firstTs`, `lastTs`,
    `runId`, `agentType`, `model`, `spawnDepth`, the label layer — and the same
    two columns.

    Those two are the deliberate overlap and they are safe because they are one
    harness fact stated twice: `agent-<id>.meta.json` copies the launch
    record's own values (`a342353f7b157760b`: meta `toolUseId` IS the
    `tool_use` id, meta `description` IS `input.description`), so `$min` over
    the pair is a no-op and neither arm needs the other to be present.

    What is deliberately NOT written top-level here is `agentType` and `model`.
    The launch result spells them in a different vocabulary from `.meta.json`
    (`resolvedModel: "claude-opus-5[1m]"` against `model: "opus"`), so one
    column fed by both would be decided by BSON collation instead of by R-48's
    stated precedence. They are kept as `spawn.agentType` /
    `spawn.resolvedModel`, where they are the launch's account of the agent and
    cannot contradict the meta file's.

    In particular this mapper does **not** add the launching session to
    `sessions[]`: that array means "session directories this agent's own
    transcript lives in", and the parent's session is emphatically not one of
    them (SESSIONJSONL-3).

    `spawn` keeps R-48's shape and is written **leaf by leaf**, never whole. Its
    immutable half (`recordUuid`, `toolUseId`, `sessionId`) is `$min`; every
    leaf of `fileHint` is `$max`. A whole-value `$set` would be order-dependent
    in the case that actually happens: `fileHint.size` and `.ts` are stat'd from
    the *parent session transcript*, which grows continuously while the session
    is alive, so two observations of one spawn carry different hints and an
    unordered bulk stores whichever mongod applied last. Per leaf the answer is
    the same in every order AND coherent, because `size` and `ts` are the only
    churning leaves, both monotone under append and both taken from one
    `os.stat` — see the module docstring for the file-replaced case, which
    resolves to `stale` rather than to a wrong answer.

    `resultSeen` is written **only when true** — never as `False` — so a later
    observation cannot un-see a result and the field's absence honestly means
    "no result observed", which is what :func:`reduce` reads it as.
    """
    obs = _as_observation(observation, SpawnObservation)
    if not is_agent_id(obs.agent_id):
        raise AgentsError(
            f"agentId {obs.agent_id!r} is not 17-hex; `agents` is keyed by the harness's "
            f"own identity (GD-7)")
    if not obs.record_uuid:
        raise AgentsError(
            "a spawn without a recordUuid is a line number and nothing else — "
            "offset-as-identity is exactly what R-48 forbids")
    # The same GD-7 identity function :func:`map_agent` keys through, so the two
    # writers of one `agents` document cannot disagree about what its `_id` is.
    key = node_key(agent_id=obs.agent_id)
    immutable = {"spawn.recordUuid": obs.record_uuid}
    if obs.tool_use_id:
        immutable["spawn.toolUseId"] = obs.tool_use_id
    if obs.session_id:
        immutable["spawn.sessionId"] = obs.session_id
    # The launch's own vocabulary, kept in the launch's own namespace — see the
    # docstring. Immutable per spawn (one launch record states them once), so
    # `$min` with the rest rather than an operator of their own.
    if obs.agent_type is not None:
        immutable["spawn.agentType"] = obs.agent_type
    if obs.model is not None:
        immutable["spawn.resolvedModel"] = obs.model

    ops = [ms.op_set_on_insert({"provenance": PROVENANCE}), ms.op_min(immutable)]
    if obs.file_hint:
        ops.append(ms.op_max({f"spawn.fileHint.{name}": value
                              for name, value in sorted(dict(obs.file_hint).items())
                              if value is not None}))
    scalars = {}
    for name, value in (("toolUseId", obs.tool_use_id),
                        ("description", obs.description)):
        if value is not None:
            scalars[name] = value
    if scalars:
        ops.append(ms.op_min(scalars))
    if obs.result_seen:
        ops.append(ms.op_max({"resultSeen": True}))
        if obs.result_ts is not None:
            ops.append(ms.op_max({"resultTs": ms.ts_fields(obs.result_ts)["ts"]}))
    return _only_ours([("agents", key, ms.merge_ops(*ops, collection="agents"))])


#: SD-1's registry. `mirror.discover_mappers` finds it by name. `derived` is
#: not here on purpose (GD-23) — see :meth:`Reduction.operations`.
MIRROR_MAPPERS = {
    "agent": map_agent,
    "agentSpawn": map_agent_spawn,
}


# --- sources (the rebuild/backfill seam) ---------------------------------


def iter_agent_observations(path=None, *, cwd=None, root=None, env=None):
    """`MIRROR_SOURCES["agent"]` — see `mirror.iter_sources` for the contract.

    With ``path=None`` (a `--rebuild`) the whole corpus is grouped by agentId
    and each agent is emitted once, fully assembled. With a concrete path (a
    `--backfill`, which walks the files and needs every observation attributable
    to one of them) the single fragment is emitted as an agent of its own —
    which is precisely the case the `$min`/`$addToSet`/`$max` algebra above
    exists to make identical to the assembled one.

    **Both arms apply the same scope.** `path=None` gets R-25's ownership rule
    for free from `ingest.iter_transcript_paths`; the per-path arm has to ask
    for it, because `mirror.iter_backfill_sources` deliberately walks all of
    `<root>/projects` with no slug filter. Without the ask, `--backfill` mirrors
    four foreign projects' agents that `--rebuild` excludes, the two modes stop
    being comparable (R-56's wipe/rebuild-equivalence arm), and GD-26 forbids
    deleting what landed. `ingest._in_scope` is called rather than re-spelled:
    it is the same rooted `sessions.scoped_dirs` test `ingest.py`'s and
    `sessions.py`'s per-path arms apply, and a third copy of a path grammar is
    a third answer.

    The two whole-corpus arms share one read of the corpus — see
    :func:`_corpus_scans`.
    """
    if path is None:
        return list(scan(cwd=cwd, root=root, env=env).agents)
    if not ingest.agent_id_for_path(path):
        return []
    root = sess.claude_root(env) if root is None else os.fspath(root)
    if not ingest._in_scope(path, cwd, root, env):
        return []
    return [assemble([read_fragment(path, root=root)])]


def iter_agent_spawn_observations(path=None, *, cwd=None, root=None, env=None):
    """`MIRROR_SOURCES["agentSpawn"]`. Session transcripts only.

    Same scope rule as :func:`iter_agent_observations`, for the same reason: a
    foreign project's session transcript names foreign agentIds, and a spawn
    document creates the `agents` row it points at.
    """
    if path is None:
        return list(scan(cwd=cwd, root=root, env=env).spawns)
    if ingest.agent_id_for_path(path) or not ingest.is_transcript_path(path):
        return []
    root = sess.claude_root(env) if root is None else os.fspath(root)
    if not ingest._in_scope(path, cwd, root, env):
        return []
    return list(find_spawns(ingest.read_transcript(path, root=root), root=root))


#: The rebuild/backfill seam declared beside the mappers (`mirror.iter_sources`).
MIRROR_SOURCES = {
    "agent": iter_agent_observations,
    "agentSpawn": iter_agent_spawn_observations,
}


# =========================================================================
# The reducer (R-54). Observations in, derived out. One derivation site.
# =========================================================================

#: Bumped whenever the *meaning* of a derived document changes. GD-23: on a
#: mismatch `derived` is dropped and rebuilt by replay, never migrated — a
#: migration would carry an old rule's conclusion under a new rule's version.
REDUCER_VERSION = "1"

#: GD-23/R-54: an agent with no result and no activity for longer than this is
#: `unknown`. Not "failed", not "running": five same-attempt siblings of this
#: very run are the specimen — the driver was killed, they never resulted, and
#: the page ticked them as running for hours.
IDLE_LIMIT_SECONDS = 180

RUNNING = "running"
DONE = "done"
UNKNOWN = "unknown"

#: The reducer's whole vocabulary. GD-23's three states, with `finished` spelled
#: `done` because that is the badge word `monitoring.md`'s enum, `legacy.STATES`
#: and the page already use — one spelling, one meaning.
#:
#: `failed` is deliberately NOT here. A failure is a *verdict* read off a node's
#: own `result` (:func:`verdict_of`); it is never a liveness state, and no code
#: path in this module can produce one. That is the fabricated-FAILED-badge
#: defect (LIVEFLOW-5/CONVO-10) made structurally unreachable on the read side.
NODE_STATES = (RUNNING, DONE, UNKNOWN)

#: Verdict vocabulary, driven from result dict keys exactly as
#: `decision_watcher.py:961-1025` drives it (GD-11: `result` is polymorphic; a
#: string result is opaque and yields no verdict). Exported alongside
#: :data:`NODE_STATES` for the same reason `CLOSED_NO_VERDICT` is imported from
#: `legacy.py` rather than re-typed: sp-12's API and sp-13's page render these
#: three strings, and a second spelling of a user-visible label is a second label.
PASSED = "passed"
FAILED = "failed"
VERDICT_KEYS = ("passed", "approved")

#: What a closed-without-a-verdict card is called. Imported rather than
#: re-spelled: `legacy.py` owns the string, and two copies of one user-visible
#: label drift (GD-10/R-58 — "closed — no verdict", never `failed`).
CLOSED_NO_VERDICT = legacy.CLOSED_NO_VERDICT

_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def verdict_of(result):
    """`"passed"` / `"failed"` / None from a node's `result` (GD-11).

    None is the common and *legitimate* answer: GD-10 says a plan whose agents
    all resulted without a decisive verdict settles **done**, "closed, no
    verdict", never failed. The whole R-58 defect was a code path that read
    "no verdict" as "failed"; there is no such path here.
    """
    if not isinstance(result, dict):
        return None
    for key in VERDICT_KEYS:
        if key in result:
            return PASSED if result[key] else FAILED
    return None


def _aware(moment):
    if isinstance(moment, datetime.datetime):
        return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)
    return None


def _latest(*moments):
    values = [m for m in (_aware(x) for x in moments) if m is not None]
    return max(values) if values else None


def _idle_text(seconds) -> str:
    """`45s` / `3m` / `1h05m` — the string the page prints verbatim (D13)."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


@dataclass(frozen=True)
class Liveness:
    """One node's computed state. Never stored on a mirror document (GD-23)."""

    state: str
    reason: str
    label: str
    idle_seconds: object = None


def liveness(*, now, last_activity=None, result_seen=False, result_ts=None,
             session_active=None, idle_limit=IDLE_LIMIT_SECONDS) -> Liveness:
    """GD-10 as amended, three states, computed from `now()` (R-54).

        result observed                                   -> done
        no result, activity inside the window,
            owning session not known-idle                 -> running
        anything else                                     -> unknown

    The third arm is the one that matters and the one that was missing: a node
    with no result and no recent activity is **unknown**, it leaves the running
    set for run-close purposes, and it renders "unknown — idle 10m". It is
    never `running` (the page ticked five dead siblings for hours) and never
    `failed` (nothing observed a failure; inventing one is R-58's defect).

    ``session_active`` is GD-10's "owning session busy" conjunct, three-valued
    on purpose: `None` means *not observed*, and an unobserved session must not
    demote a node whose own transcript is warm.
    """
    now = _aware(now)
    if now is None:
        raise AgentsError("reduce needs a clock: pass now=datetime (R-54 computes at read time)")
    if result_seen:
        moment = _aware(result_ts)
        return Liveness(DONE, "result observed", DONE,
                        None if moment is None else max(0, int((now - moment).total_seconds())))
    last = _aware(last_activity)
    if last is None:
        return Liveness(UNKNOWN, "no activity observed", f"{UNKNOWN} — never observed")
    idle = max(0, int((now - last).total_seconds()))
    if idle > idle_limit:
        return Liveness(UNKNOWN, f"idle {_idle_text(idle)} (> {idle_limit}s)",
                        f"{UNKNOWN} — idle {_idle_text(idle)}", idle)
    if session_active is False:
        return Liveness(UNKNOWN, "owning session idle",
                        f"{UNKNOWN} — session idle", idle)
    return Liveness(RUNNING, f"active {_idle_text(idle)} ago", RUNNING, idle)


# --- topology (SD-9): a shape, never a code dependency --------------------


@dataclass(frozen=True)
class Topology:
    """Attempt denominators and stage order, from `custom_state` (SD-9/R-19).

    Read strictly per GD-24's schema — kind `topology`, payload under
    `data.custom` — and **optional**. Every run recorded before R-19 exists
    takes the absent arm: "attempt 3", no denominator, no next-stage arrow
    (D13: a number Touch cannot substantiate is not rendered). That is what
    lets this sub-plan land before the one that writes topology at all.
    """

    ref_id: object = None
    max_attempts: object = None
    stages: tuple = ()
    stage_attempts: object = None

    def denominator(self, stage=None):
        if stage and isinstance(self.stage_attempts, dict) and stage in self.stage_attempts:
            return self.stage_attempts[stage]
        return self.max_attempts

    def next_stage(self, stage):
        """The stage after ``stage``, or None — never a guess."""
        if not stage or stage not in self.stages:
            return None
        index = self.stages.index(stage)
        return self.stages[index + 1] if index + 1 < len(self.stages) else None


def _topology_from(doc) -> Topology:
    payload = doc.get("data") if isinstance(doc, dict) else None
    custom = payload.get("custom") if isinstance(payload, dict) else None
    custom = custom if isinstance(custom, dict) else {}
    stages = custom.get("stages")
    attempts = custom.get("maxAttempts", custom.get("max_attempts"))
    per_stage = custom.get("stageAttempts")
    return Topology(
        ref_id=doc.get("refId"),
        max_attempts=attempts if isinstance(attempts, int) else None,
        stages=tuple(str(s) for s in stages) if isinstance(stages, list) else (),
        stage_attempts=per_stage if isinstance(per_stage, dict) else None,
    )


def topology_index(state) -> dict:
    """`{refId: Topology}` from the `custom_state` heads of kind `topology`.

    A read of a *shape*, not an import: nothing here depends on
    `custom_state.py` existing (SD-9). When it does not, this returns `{}` and
    every attempt label loses its denominator, which is the documented arm.

    **The refId space is the contract, and it is `refs.run_key(runId)`.**
    SD-9 fixes the shape (kind `topology`, payload under `data.custom`) but not
    the key, and :func:`reduce` joins a topology to a run by that key alone
    (:func:`_run_ref`). A writer that keys its head by a `{task, plan, stage}`
    ref instead — legal under amended GD-11 — produces an index this reduction
    never hits, so every run silently takes the "absent topology" arm forever
    rather than failing loudly. Stated for sp-11 in the deviation file's
    handoff, beside the `fragments_of()` rule that binds sp-12/sp-13.
    """
    out = {}
    for doc in (state.get("custom_state") or {}).values():
        if isinstance(doc, dict) and doc.get("kind") == "topology":
            topology = _topology_from(doc)
            if topology.ref_id:
                out[topology.ref_id] = topology
    return out


def attempt_label(attempt, topology=None, *, stage=None):
    """`"attempt 2 of 4"` with a topology, `"attempt 2"` without, None if unknown."""
    if attempt is None:
        return None
    if not isinstance(attempt, int):
        return f"attempt {attempt}"
    total = topology.denominator(stage) if isinstance(topology, Topology) else None
    return f"attempt {attempt} of {total}" if isinstance(total, int) else f"attempt {attempt}"


# --- the reduction --------------------------------------------------------


def derived_id(kind, ref_id) -> str:
    """`<kind>:<refId>` — the `derived` collection's key grammar.

    `derived` is the one row of GD-24's table whose `_id` is *reducer-owned*
    (`mongo_store.COLLECTIONS["derived"].id_kinds` is empty, by declaration and
    not by omission), so this is the grammar. `refId` is itself a `refs` key,
    and it is stored as a field as well, so every join is a field query and
    nothing ever parses an `_id` (LIVEFLOW-3: dotted-`_id` queries COLLSCAN).
    """
    if not kind or not isinstance(ref_id, str) or not ref_id:
        raise AgentsError(f"a derived document needs a kind and a refId, got {kind!r}/{ref_id!r}")
    return f"{kind}:{ref_id}"


@dataclass
class Reduction:
    """The reducer's whole output: derived documents, plus why (R-54)."""

    reducer_version: str = REDUCER_VERSION
    derived_from_seq: int = 0
    now: object = None
    agents: dict = field(default_factory=dict)
    nodes: dict = field(default_factory=dict)
    runs: dict = field(default_factory=dict)
    counters: dict = field(default_factory=dict)

    def documents(self) -> dict:
        """`{_id: document}` — exactly what the `derived` collection holds."""
        out = {}
        for kind, bucket in (("agentState", self.agents), ("nodeState", self.nodes),
                             ("runState", self.runs)):
            for ref_id, payload in bucket.items():
                key = derived_id(kind, ref_id)
                doc = {"_id": key, "kind": kind, "refId": ref_id,
                       "provenance": DERIVED_PROVENANCE,
                       "reducerVersion": self.reducer_version,
                       "derivedFromSeq": int(self.derived_from_seq)}
                doc.update(payload)
                out[key] = doc
        return out

    def operations(self):
        """`(collection, _id, update)` triples for the `derived` collection.

        Deliberately not reachable through :data:`MIRROR_MAPPERS`: GD-23 gives
        derived state exactly one writer, and a registered mapper is by
        definition callable by anything holding an observation. A caller
        enqueues these the same way it enqueues a mapper's output.

        **The `$set` is a TOTAL overwrite of the payload, and that is a
        property of the payload rather than of this method** (MAJOR 2 of attempt
        3's critique). GD-26 forbids the `$unset` that would remove a key which
        stopped being emitted, and `mirror.py` drops `derived` only on
        `--rebuild`, never on a live tick — so a conditionally-emitted key
        leaves a *stale conclusion* on the server document: `frozen: true`
        surviving beside a reason that is no longer the freeze, an
        `idleSeconds` from ten minutes ago beside `state: "done"`, an
        `attemptLabel` from a topology that has since been retracted. The
        in-memory :func:`apply_derived` never showed it, because it clears the
        bucket first, so the memory model that is `mongo_store`'s declared
        oracle for the server was the *more* correct of the two. :func:`reduce`
        therefore emits every optional key on every payload, `None`/`False`
        included, which makes folding these operations into a state
        byte-identical to :func:`apply_derived` for the same `_id` set.

        The one thing per-`_id` totality cannot express is an `_id` that stops
        being produced at all, which is why the drop still exists. On the live
        path that cannot happen — the reduction's keys come from `agents`,
        `run_nodes` and `runs`, and the mirror is upsert-only (GD-26) — so the
        drop is needed exactly where `mirror.Mirror.rebuild` already performs
        it: a wipe, a rebuild, or a `reducerVersion` bump
        (:func:`needs_rebuild`).

        **Handoff (sp-06/sp-12).** The *server-side* half of "dropped and
        rebuilt, never migrated" is a `drop` of one collection, and the only
        module allowed to speak to the driver is `mirror.py` (sp-06). The
        memory-model half is :func:`apply_derived`, here, and
        :func:`needs_rebuild` is the predicate both sides share.
        """
        ops = []
        for key, doc in sorted(self.documents().items()):
            body = {name: value for name, value in doc.items()
                    if name not in ("_id", "provenance")}
            ops.append((DERIVED_COLLECTION, key, ms.merge_ops(
                ms.op_set_on_insert({"_id": key, "provenance": DERIVED_PROVENANCE}),
                ms.op_set(body),
                collection=DERIVED_COLLECTION)))
        return ops


def _session_activity(state, now, idle_limit):
    """`{sessionId: True}` — the sessions Touch has POSITIVE evidence are busy.

    GD-10's conjunct, and it may only ever **promote**. A sessionId absent from
    this map is *unobserved*: :func:`liveness` is passed `None` for it and the
    node's own transcript decides alone. That is the whole rule, and it is a
    rule about evidence rather than a convenience — see D-7 in
    `findings/sp-agents-reducer-storage-deviation.md`.

    Why `False` is not available here (MAJOR 1 of attempt 3's critique). The
    field this function reads, `sessions.lastTs`, has exactly one writer —
    `sessions.map_session`, live-registry arm only, from the registry entry's
    `updatedAt` — and it is not a liveness clock:

    * the **historical** arm writes no timestamp at all (`sessions.py`'s "What
      this module does not timestamp"), and `ingest.COLLECTIONS` does not
      include `sessions`, so nothing accumulates record stamps into it either.
      Every transcript on disk therefore yields a `sessions` document with no
      `lastTs`;
    * the **registry** heartbeat is not refreshed at anything like this
      granularity: the one live entry on this machine while attempt 3 was
      reviewed belonged to the session that was running at that moment and its
      `updatedAt` was six hours old.

    Scored as "not fresh ⇒ idle", both cases demote every warm agent of a real
    session to `unknown — session idle` — a label that blames the session, so
    the row looks explained while the reducer's whole subject (a live agent
    renders running) is inverted. Absence of a fresh heartbeat is not an
    observation of an idle session. A genuine demotion needs a positive
    observation of session *end*, which nothing writes today; when something
    does, it belongs here as its own arm and `liveness` already takes `False`.
    """
    out = {}
    for doc in (state.get("sessions") or {}).values():
        if not isinstance(doc, dict):
            continue
        last = _aware(doc.get("lastTs"))
        if last is None or (now - last).total_seconds() > idle_limit:
            continue
        for session_id in (doc.get("sessionIds") or []):
            if isinstance(session_id, str):
                out[session_id] = True
    return out


def _session_conjunct(session_ids, active):
    """GD-10's conjunct for one node: `True` (promoted) or `None` (unobserved).

    Never `False` — :func:`_session_activity` records only positive evidence, so
    "no fresh session" and "no session observed" are the same answer and it is
    the one that lets the node's own transcript decide.
    """
    for session_id in session_ids or ():
        if isinstance(session_id, str) and active.get(session_id):
            return True
    return None


def _max_seq(state) -> int:
    """The highest `seq` the reduction saw — GD-23's `derivedFromSeq`.

    Informational provenance, **not** a resume cursor. `events` and
    `custom_state_events` are independent per-stream counters (GD-24 keys both
    `<stream>#<seq:012d>`), so one maximum across the two is not a position
    either stream can be resumed from; R-55's resume is a `(stream, seq)` pair
    the client holds, and it neither reads this field nor could. What the value
    answers is "how far had the mirror got when this conclusion was drawn",
    which is what makes a derived document comparable against a later one.
    """
    best = 0
    for collection in ("events", "custom_state_events"):
        for doc in (state.get(collection) or {}).values():
            seq = doc.get("seq") if isinstance(doc, dict) else None
            if isinstance(seq, int) and not isinstance(seq, bool) and seq > best:
                best = seq
    return best


def _run_is_terminal(doc) -> bool:
    """Did the harness itself say this run ended? (the freeze trigger)"""
    if not isinstance(doc, dict):
        return False
    if doc.get("endedAt") is not None:
        return True
    status = doc.get("status")
    return isinstance(status, str) and status not in ("running", "started", "async_launched")


def _run_ref(run_id):
    """The `runs` `_id` a raw `runId` field refers to, or None.

    Every run lookup in :func:`reduce` goes through here, and it is not
    decoration. `runs` and the topology index are keyed by `refs.run_key`
    (escaped, GD-24); `agents.runId` and `run_nodes.runId` carry the runId
    **raw**. Those two agree only while a runId contains none of `% # | :`,
    which today's `wf_<hex>` happens not to. A runId that carries one splits a
    single run into two `Reduction.runs` entries — the escaped one with no
    nodes, the raw one with no `startedAt` — and `terminal.get(...)` starts
    missing, which silently switches the freeze-to-stale rule off. Since
    `derived`'s `refId` is specified to be a `refs` key anyway, the reduction
    is keyed by the escaped form throughout and the raw id is carried as a
    field.
    """
    return refs.run_key(run_id) if run_id else None


def _raw_run_id(run_ref):
    """The raw `runId` inside a `runs` `_id`, or None. The grammar's inverse.

    The last resort behind `runState.runId`, and it is load-bearing in the arm
    :func:`reduce` documents as normal: `ingest.map_run` stores the runId **as
    the `_id`** and not as a field (`COLLECTIONS["runs"].types` declares no
    `runId` either), so a run whose nodes have not been observed yet — the
    journal's first `started`, before any node exists — has nobody to name it
    and its payload carried `runId: null`. That is the freshest run on the page.

    `refs.parse_ref_key` rather than a local unescape: D-4's promise is that no
    reader has to unescape an `_id`, and the module that owns the grammar owns
    its inverse too (a second unescaper is a second grammar). A key from a
    foreign shape raises `RefError` and yields None rather than a guess
    (MINOR 3 of attempt 3's critique).
    """
    if not run_ref:
        return None
    try:
        return refs.parse_ref_key("run", run_ref).get("runId")
    except refs.RefError:
        return None


def reduce(state, *, now=None, reducer_version=REDUCER_VERSION, derived_from_seq=None,
           idle_limit=IDLE_LIMIT_SECONDS, topology=None) -> Reduction:
    """The ONE reducer (GD-23/R-54): mirror observations in, `derived` out.

    ``state`` is `mongo_store.apply_operations`' memory model,
    `{collection: {_id: doc}}` — the same shape a read back from the server
    produces, so the reduction is identical whether it runs over a live tick's
    accumulated state or over a `--rebuild`'s.

    ``now`` is the clock, and it is an argument rather than a call so that the
    derivation is *provable*: the same fixture reduced with a `now` inside the
    window is `running` and with a `now` ten minutes later is `unknown`, which
    is exactly R-54's test and is only expressible if the caller holds the
    clock. Omitted, it defaults to the real one — the API (sp-12) passes
    nothing and gets read-time liveness for free.

    Three passes, in this order, because each needs the one before it:

    1. **runs** — is there a terminal observation? (the freeze trigger);
    2. **nodes and agents** — liveness, then the freeze: a run that closed with
       rows still "running" freezes them to `unknown`, reason
       "frozen at run close". This is `monitor.html`'s `freezePlan`, moved into
       the reducer so the page and `/api/*` cannot disagree about a row (R-54);
    Every run-shaped key in the output — `Reduction.runs`, `derived`'s
    `runState:<refId>` — is `refs.run_key(runId)`, never the raw id; see
    :func:`_run_ref` for why mixing the two switches the freeze rule off.

    3. **run close** — a run closes when nothing is left in the running set.
       `unknown` nodes have already left it, which is the run-close rule R-54
       states and the reason a killed driver's run closes at all. The close
       carries a reason and a verdict tally; the run's own state is never
       `failed`, and "all resulted, none decisive" is
       `"closed — no verdict"` (GD-10), the same string `legacy.py` renders.
       A close WITH failing verdicts renders `"done — N failed verdict(s)"`:
       the label is the page's render string (R-54), so a run whose nodes
       decided against it may not read exactly like a clean one — but the
       tally is a tally, and `state` stays `done`.
    """
    now = _aware(now) or datetime.datetime.now(datetime.timezone.utc)
    version = str(reducer_version)
    seq = _max_seq(state) if derived_from_seq is None else int(derived_from_seq)
    agents_docs = state.get("agents") or {}
    node_docs = state.get("run_nodes") or {}
    run_docs = state.get("runs") or {}
    topologies = topology_index(state) if topology is None else dict(topology)
    active = _session_activity(state, now, idle_limit)
    # Flat, and prefixed by population on purpose: one `done` counter over
    # agents AND nodes reads as a total and is not one (an agent and its node
    # are the same fact seen twice), so a reader comparing it against
    # `nodeCount` would find a discrepancy that is not there.
    counters = {"agents": 0, "nodes": 0, "runs": 0, "frozen": 0,
                "agent_" + RUNNING: 0, "agent_" + DONE: 0, "agent_" + UNKNOWN: 0,
                "node_" + RUNNING: 0, "node_" + DONE: 0, "node_" + UNKNOWN: 0,
                "verdict_passed": 0, "verdict_failed": 0, "no_verdict": 0,
                "closed": 0, "open": 0, "topology_missing": 0}

    # Keyed by the `runs` `_id`, which IS `refs.run_key(runId)`; every lookup
    # below goes through :func:`_run_ref` so the two key spaces cannot drift.
    terminal = {key: _run_is_terminal(doc) for key, doc in run_docs.items()}
    # `run_key -> raw runId`, so a payload can still name the run the way the
    # harness spells it without any reader having to unescape an `_id`.
    raw_run = {key: doc.get("runId") for key, doc in run_docs.items()
               if isinstance(doc, dict) and doc.get("runId")}

    # --- agents ----------------------------------------------------------
    result_by_agent = {}
    for doc in node_docs.values():
        agent_id = doc.get("agentId")
        if agent_id and doc.get("resultSeen"):
            result_by_agent[agent_id] = _latest(result_by_agent.get(agent_id),
                                                doc.get("endedAt"))
    agents = {}
    for key, doc in sorted(agents_docs.items()):
        run_id = doc.get("runId")
        run_ref = _run_ref(run_id)
        if run_ref and run_id:
            raw_run.setdefault(run_ref, run_id)
        session_ids = [sid for sid in (doc.get("sessions") or []) if isinstance(sid, str)]
        session_active = _session_conjunct(session_ids, active)
        result_seen = bool(doc.get("resultSeen")) or key in result_by_agent
        live = liveness(now=now, last_activity=doc.get("lastTs"), result_seen=result_seen,
                        result_ts=doc.get("resultTs") or result_by_agent.get(key),
                        session_active=session_active, idle_limit=idle_limit)
        frozen = False
        if live.state == RUNNING and terminal.get(run_ref):
            live = Liveness(UNKNOWN, "frozen at run close",
                            f"{UNKNOWN} — frozen at run close", live.idle_seconds)
            frozen = True
        labels = doc.get("labels") if isinstance(doc.get("labels"), dict) else {}
        topo = topologies.get(run_ref) if run_ref else None
        # Every optional key is emitted on every payload, `None`/`False`
        # included — see :meth:`Reduction.operations`. A key that appears only
        # sometimes cannot be retracted from a stored document (GD-26 forbids
        # `$unset`), so `frozen`/`idleSeconds`/`attemptLabel`/`nextStage` would
        # survive as conclusions the reducer no longer draws.
        payload = {
            "state": live.state,
            "reason": live.reason,
            "label": live.label,
            "display": doc.get("name") or key,
            "unconventional": bool(doc.get("unconventional", True)),
            "runId": run_id,
            "sessions": session_ids,
            "lastActivityTs": doc.get("lastTs"),
            "resultSeen": result_seen,
            "idleSeconds": live.idle_seconds,
            "frozen": frozen,
            "attemptLabel": attempt_label(labels.get("attempt"), topo,
                                          stage=labels.get("stage")),
            "nextStage": (topo.next_stage(labels.get("stage"))
                          if topo is not None else None),
        }
        if frozen:
            counters["frozen"] += 1
        agents[key] = payload
        counters["agents"] += 1
        counters["agent_" + live.state] += 1

    # --- run nodes -------------------------------------------------------
    nodes = {}
    per_run = {}
    for key, doc in sorted(node_docs.items()):
        run_id = doc.get("runId")
        run_ref = _run_ref(run_id)
        if run_ref and run_id:
            raw_run.setdefault(run_ref, run_id)
        agent = agents_docs.get(doc.get("agentId")) if doc.get("agentId") else None
        last = _latest(doc.get("endedAt"), (agent or {}).get("lastTs"), doc.get("startedAt"))
        session_active = _session_conjunct((agent or {}).get("sessions"), active)
        live = liveness(now=now, last_activity=last, result_seen=bool(doc.get("resultSeen")),
                        result_ts=doc.get("endedAt"), session_active=session_active,
                        idle_limit=idle_limit)
        frozen = False
        if live.state == RUNNING and terminal.get(run_ref):
            live = Liveness(UNKNOWN, "frozen at run close",
                            f"{UNKNOWN} — frozen at run close", live.idle_seconds)
            frozen = True
        verdict = verdict_of(doc.get("result"))
        topo = topologies.get(run_ref) if run_ref else None
        payload = {
            "state": live.state,
            "reason": live.reason,
            "label": live.label,
            "display": doc.get("label") or doc.get("key") or key,
            "runId": run_id,
            "key": doc.get("key"),
            "ordinal": doc.get("ordinal"),
            "agentId": doc.get("agentId"),
            "resultSeen": bool(doc.get("resultSeen")),
            "verdict": verdict,
            "lastActivityTs": last,
            # Total, for the reason the agent payload above states.
            "idleSeconds": live.idle_seconds,
            "frozen": frozen,
            "attemptLabel": attempt_label(doc.get("attempt"), topo, stage=doc.get("key")),
            "nextStage": topo.next_stage(doc.get("key")) if topo is not None else None,
        }
        if frozen:
            counters["frozen"] += 1
        nodes[key] = payload
        counters["nodes"] += 1
        counters["node_" + live.state] += 1
        counters["verdict_passed" if verdict == PASSED else
                 "verdict_failed" if verdict == FAILED else "no_verdict"] += 1
        if run_ref:
            per_run.setdefault(run_ref, []).append(payload)

    # --- runs ------------------------------------------------------------
    runs = {}
    for run_ref in sorted(set(run_docs) | set(per_run)):
        doc = run_docs.get(run_ref) or {}
        members = per_run.get(run_ref, [])
        tally = {state: sum(1 for m in members if m["state"] == state) for state in NODE_STATES}
        verdicts = {
            PASSED: sum(1 for m in members if m["verdict"] == PASSED),
            FAILED: sum(1 for m in members if m["verdict"] == FAILED),
        }
        # A run with no nodes yet is OPEN, not closed: the journal's first
        # `started` creates the run document before it creates any node
        # (R-49 — a live run with no `<runId>.json` is on disk right now), and
        # "closed because we have not seen anything" is the same fabrication in
        # the other direction. Only the harness's own terminal closes an empty
        # run.
        closed = (not tally[RUNNING]) if members else bool(terminal.get(run_ref))
        if members and tally[RUNNING]:
            reason = f"{tally[RUNNING]} node(s) active"
        elif terminal.get(run_ref):
            reason = "terminal observation"
        elif not members:
            reason = "no nodes observed yet"
        elif tally[DONE] == len(members):
            reason = "every node resulted"
        else:
            reason = f"quiet — {tally[UNKNOWN]} node(s) idle past {idle_limit}s"
        label = None
        if closed and members and not verdicts[PASSED] and not verdicts[FAILED]:
            # GD-10, verbatim in effect: resulted without a decisive verdict is
            # DONE, "closed — no verdict". Never `failed`; that badge is what
            # R-58 exists to stop fabricating.
            label = CLOSED_NO_VERDICT
        elif closed and verdicts[FAILED]:
            # R-54 makes the label the render string, so a run that closed with
            # failing verdicts may not render as the bare word `done`,
            # indistinguishable from a clean one. The tally is reported, and it
            # stays a TALLY: `state` is still DONE and `failed` is still absent
            # from NODE_STATES — R-58 forbids inventing the state, not stating
            # what the nodes actually decided.
            label = f"{DONE} — {verdicts[FAILED]} failed verdict(s)"
        topo = topologies.get(run_ref)
        if topo is None:
            # Counted once per run, not once per node: "this run has no
            # topology" is one fact, and SD-9 makes it the *normal* one (every
            # pre-R-19 run takes this arm). A per-node count would read like a
            # per-node defect.
            counters["topology_missing"] += 1
        runs[run_ref] = {
            "state": DONE if closed else RUNNING,
            "closed": closed,
            "reason": reason,
            "label": label or (DONE if closed else RUNNING),
            "nodes": tally,
            "verdicts": verdicts,
            "nodeCount": len(members),
            # The harness's own spelling, carried as a field so no reader has to
            # unescape the `_id` this bucket is keyed by (LIVEFLOW-3). The
            # `runs` document does not store one (the runId IS its `_id`), so an
            # agent or a node normally supplies it and :func:`_raw_run_id`
            # answers for a run that has neither yet — which is the live arm.
            "runId": doc.get("runId") or raw_run.get(run_ref) or _raw_run_id(run_ref),
            "terminalObserved": bool(terminal.get(run_ref)),
            "startedAt": doc.get("startedAt"),
            "endedAt": doc.get("endedAt"),
        }
        counters["runs"] += 1
        counters["closed" if closed else "open"] += 1

    return Reduction(reducer_version=version, derived_from_seq=seq, now=now,
                     agents=agents, nodes=nodes, runs=runs, counters=counters)


def needs_rebuild(state, reducer_version=REDUCER_VERSION) -> bool:
    """Is any stored derived document from a different reducer? (GD-23)

    The predicate both halves of "drop and rebuild" share — this module's
    :func:`apply_derived` and, on a live deployment, `mirror.py`'s drop.
    """
    for doc in (state.get(DERIVED_COLLECTION) or {}).values():
        if isinstance(doc, dict) and doc.get("reducerVersion") != str(reducer_version):
            return True
    return False


def apply_derived(state, reduction):
    """GD-23's drop-and-rebuild, in the memory model. Returns ``state``.

    The bucket is **replaced**, not merged: a derived document is a
    conclusion, and a conclusion the current reducer no longer draws must
    disappear rather than linger under a version that no longer produced it.
    That is the one legitimate "delete" in Touch and it is confined to the one
    collection GD-23 declares droppable — the mirror collections are
    upsert-only and this function cannot touch them (it names exactly one).
    """
    state[DERIVED_COLLECTION] = {}
    return ms.apply_operations(state, reduction.operations())
