"""Custom state: the `.touch/` WAL, the append-only event collection, the
derived head, and the `slots` name↔agentId hop (R-52 + R-53).

This is the one dataset Touch cannot rebuild from `~/.claude` (GD-22's single
exception), so it is **file-journaled first**: :class:`Writer` appends
touch-events-v2 records to `.touch/custom-state.jsonl` through `store.py`'s
existing append machinery — `store.py` itself is untouched by this sub-plan and
already carries the two rules that make it the right WAL (`custom-state` is an
fsync-per-append `DURABLE_STREAM`, and its provenance is pinned file-side to
`{asserted, touch}`). Mongo is then a projection of those lines, and a wipe is
a replay, not a loss.

Three halves, in the order the data moves:

* the **writing** half (:class:`Writer`) — the only code in Touch that authors
  durable state, and the reason GD-28 exists;
* the **reading** half (:func:`iter_custom_state_observations`,
  :func:`iter_slot_observations` and the path helpers they use) — foreign,
  agent-written files (spawn ledgers, control files) parsed into the same
  observation shape the WAL produces;
* the **mapping** half (:data:`MIRROR_MAPPERS`, :func:`head_write`,
  :func:`bind_write`) — pure: observations in, `(collection, _id, update)`
  triples and guarded writes out, built only from `refs.ref_key` and
  `mongo_store`'s op vocabulary. No I/O, no clock, no database driver; GD-21
  names the two files that may import the Mongo driver and this is not one of
  them, so that package's name does not appear in this file at all.

Two collections, one installation-wide (CUSTOMSTATE-17/G2)
-----------------------------------------------------------
`custom_state_events` is append-only and **insert-only** — every field is
`$setOnInsert`, so this module has no code path that updates or removes one.
`custom_state` is the derived head, `_id = <refId>#<stateKey>`, carrying
`derived:true` + `fromSeq`, fully rebuildable by replaying the events
(:func:`rebuild_heads`) and therefore droppable as a recovery procedure
(CUSTOMSTATE-14). Both are ONE collection for the whole installation,
discriminated by :data:`KINDS` and scoped by `refId` + `sessionKey` — never one
per task or session, which is the anti-pattern G2 already discarded and which
the per-scope file layout invites back under a new name.

Deletion is a **tombstone event**, never a `deleteOne` (GD-26): the head keeps
`tombstone: true` and its payload, readers hide it, and "when was this
annotation deleted, and what did it say" stays answerable.

Why the head is a guarded write and not a triple
------------------------------------------------
`mirror.py`'s queue drains `(collection, _id, update)` triples through
`bulk_upsert`, whose filter is `{_id: key}` and nothing else. The head needs
more than that: R-52 requires the payload to be written **only by a newer
event** (`require={"seq": {"$lt": newSeq}}`) so a late old write never clobbers
a fresher head. `mongo_store.guarded_update` — and `mirror.Backend`'s async
mirror of it — is exactly that call, and it already names R-52's head in its
docstring as one of its two reasons to exist. So this module emits two kinds of
work, declared separately and honestly:

* :data:`MIRROR_MAPPERS` — triples, insert-only or monotone, order-independent
  by construction (GD-25's shuffled/reversed pass); the mirror drives these
  today;
* :func:`head_write` / :func:`bind_write` / :func:`orphan_write` /
  :func:`conflict_write` — pure :class:`GuardedWrite` descriptions, driven by a
  backend's `guarded_update`. :func:`apply_guarded` is the in-memory twin (the
  same relationship `mongo_store.apply_update` has to the server), which is what
  lets a rebuild and the whole test suite run with no database at all.

What the head is ordered BY, and why it is not `seq` alone
-----------------------------------------------------------
`seq` is **per-stream and positional**: the WAL's own counter on
`custom-state`, and the *line number* on a `control:<scope>` or `ledger:<scope>`
file. The head, by R-52, is ONE space for the whole installation. So two events
from **different streams** can share `(refId, stateKey, seq)` — two control
files each carrying a stop for the same slot on their line 1 is the reachable
case, not a contrived one — and a bare `{seq: {$lt: newSeq}}` guard refuses
whichever of them arrives second, whichever that is. The log would be right
(both events stored, counts equal) and the head would be whatever arrived
first: silent, and invisible to the count assertion GD-25 pairs with its
fingerprint.

The head therefore carries :data:`HEAD_ORDER_FIELD` — `<seq padded to `refs`'
width>|<escaped stream>` — advanced by `$max` and used as the payload guard
(:func:`head_order`). Its primary component is the same zero-padded `seq` the
event `_id` already uses, so **within** one stream it orders exactly as
`{seq:{$lt:newSeq}}` does (R-52's literal contract, kept, alongside `seq` and
`fromSeq` on the document); across streams the stream id breaks the tie. Which
stream wins a tie is arbitrary and fixed; what may not stay is "arrival order
decides".

Ordering the *write* is only half of it, because `$set` overwrites the keys it
carries and nothing else, and GD-26 leaves no operator here that removes a
field: a key one event has and another does not would survive on the head under
the winner's payload, chosen by whichever inserted the document. So the head's
top-level key set is **fixed**, and everything that varies between the events of
one head — `sessionKey`, `sessionKeySource`, `attemptSource`, the clock — lives
in :data:`HEAD_EVENT_FIELD`, one sub-document `$set` replaces whole.

The refId rule, and its one documented widening
-----------------------------------------------
R-52: a custom-state `refId` is validated against the `agents`, `run_nodes` and
`slots` grammars, and a write for an unknown ref is **rejected** — a dangling
state card is worse than a rejected one. One kind widens that set, deliberately:
`topology` heads describe a *run*, and the reducer joins them by
`refs.run_key(runId)` and by nothing else (`agents.topology_index`, handed over
by name in sp-10's deviation record). Refusing a `runs` refId for `topology`
would leave every run silently on the "absent topology" arm forever, which is
the failure that handoff exists to prevent. So :data:`REF_KINDS` is the default
and :data:`REF_KINDS_BY_KIND` states the exception in one place, with the
grammar named rather than a special case buried in a branch.

Provenance can never be a mirrored-fact claim (GD-28)
------------------------------------------------------
`custom_state*` is pinned to `{asserted, touch}` by `mongo_store`'s
`$jsonSchema` and, file-side, by `store.stream_provenance`. This module adds the
third, structural leg: :func:`validate_provenance` is the only door, it accepts
:data:`PROVENANCE` and nothing else, and every write path goes through it —
there is no code path here that emits a mirror-class provenance value, which
`tests/test_custom_state.py` asserts both by call and by walking this file's
own AST for provenance literals.

Slots: the single name↔agentId hop (R-53)
------------------------------------------
Custom state addresses agents by **name**, because `agentId` is unknowable
before spawn; the mirror addresses them by `agentId`. `slots` is the one place
that hop happens, keyed
`slot:<sessionKey>|<root>|<name>|<attempt:03d>` — `sessionKey` first, because
two sessions in this repo pick `ROOT_NAME` from near-identical task names and a
key without it cross-links one session's custom state onto another session's
agents (CUSTOMSTATE-10).

`resolution` is an explicit, queryable state machine —
`pending | bound | orphaned | conflict` with `pendingSince` — and **orphaned is
a normal outcome**, rendered honestly and never hidden: GD-7 permits a node that
never gets a marker, and an orphaned stop intent is a stop that went nowhere
(D13). The transition is monotone in :data:`RESOLUTION_RANK` and every advance
is guarded on `{"resolutionRank": {"$lt": rank}}`, so replaying observations in
any order lands on the same document.

A bind that collides on the unique sparse `agentId` index writes a `conflict`
document recording **both** ids and **never raises**: the collision is caught,
counted and the tailer lives (CUSTOMSTATE-9 — agent-authored data must not be
able to kill the ingest process). :class:`SlotTable` enforces that same unique
constraint in memory, so the model and the server agree about which bind is the
loser rather than the test discovering it only against a live mongod.

Delivering "never raises" against a real server needed one deliberate split, and
it is the reason :func:`claim_op` exists. `mongo_store.guarded_update` funnels
every driver error on its **guarded** path into `MongoUnavailable`, carving out
only the `$jsonSchema` refusal — so a duplicate key there arrives as "the
database is gone", propagates out of the tick and trips GD-30's breaker on
healthy traffic. (Verified against mongod 7 with the real unique sparse index,
which is what turned this from a reading of the code into a fact.) So the one
index-touching write is isolated into :func:`claim_op` and driven through
`bulk_upsert`, the only write API here that answers a duplicate key with
`tolerated_dups` — exactly the count GD-29 asks to be exposed. Nothing else in
this module `$set`s `agentId`. A future pass that wants the guarded path to
carve out code 11000 the way it carves out 121 would be editing
`mongo_store.py`, which this sub-plan does not own.

Control intents and acks (SD-8)
-------------------------------
`control_intent`/`control_ack` ingest reads a **configured** path list from
`TOUCH_CONTROL_PATHS` and records `pathSource` on every document. R-20 (which
relocates the control file and makes it aggregator-owned) is not in this pass,
and this module deliberately **does not restate the control-file path**: the
skill file on disk and the base plan already disagree about it, and a third
statement would make it three (CUSTOMSTATE-11). With nothing configured the
source yields nothing and says so through its counters — the honest answer,
and a later relocation is a config change rather than a re-ingest.

The lines themselves are the ones `touch-orchestrate` actually tells an
orchestrator to write — `{"action":"stop","name":…}` and
`{"ack":"stop","name":…,"taskId":…,"result":…}` — and they carry **a name and
nothing else** of the address: no `root`, no `sessionKey`, no `attempt`. That
is not an omission to be patched by defaults; it is the reason `slots` exists.
:class:`SlotIndex` performs the name→slot hop against the slots actually
observed (the same hop the skill file describes as "resolve the name to its
`taskId` via the spawn ledger"), so :func:`read_control_file` addresses a real
spawn or addresses nothing:

* nothing observed under that name ⇒ `skipped_unaddressable`;
* the name observed under two different `(sessionKey, root)` identities ⇒
  `skipped_ambiguous` — Touch does not pick one;
* no `attempt` on the line ⇒ the **highest observed attempt** for that slot,
  recorded as `attemptSource: "resolved"`. Never a default of 1: a stop card
  attached to a stale attempt is worse than a skipped line (D13), and
  touch-orchestrate re-runs a stopped slot as `attempt` + 1.

Both foreign-file readers count what they drop (:func:`new_counters`) —
`read`, `parsed`, `skipped_malformed`, `skipped_unaddressable`,
`skipped_ambiguous`, `unreadable` — because an operator who cannot tell
"nothing happened yet" from "everything I wrote was rejected" is looking at the
quiet drop GD-26 and D13 both forbid.

Who must drive the head and the bind (a handoff, not a hope)
------------------------------------------------------------
`mirror.py` discovers :data:`MIRROR_MAPPERS` and :data:`MIRROR_SOURCES`, and
those two registries are the whole of what runs today: `custom_state_events`
and the *evidence* half of `slots`. Nothing in `aggregator/` yet calls
:func:`head_write`, :func:`bind_slot`, :meth:`SlotTable.sweep` or
:func:`rebuild_heads`, and this module cannot call them itself — they need a
database handle and GD-21 forbids this file from holding one. So, stated
plainly rather than left to be discovered: **until a driver lands,
`custom_state` is written by nothing and every slot stays
`resolution:"pending"`**, which keeps `agents.topology_index` permanently on
SD-9's absent-topology arm, and makes the head and every non-`pending`
resolution a test-only surface that the UI must not present as fact.

The missing driver is a few lines in whichever sub-plan owns the mirror tick
and the read API (sp-12 by elimination — sp-06's `mirror.py` is closed):
after each `map_custom_state` batch, drive `head_write(obs)` through
`Backend.guarded_update`; on each marker / ledger / description bind evidence,
call `bind_slot(db, key, agentId, by=…)`; and run `SlotTable.sweep` (or
:func:`orphan_write`) on the tick after a run reaches a terminal. The same
handoff is recorded, with the code to paste, in this run's findings folder as
`sp-custom-state-head-driver-deviation.md`.

The `slots` spec must declare this module's sets (the second handoff)
---------------------------------------------------------------------
GD-25's acceptance oracle is `mongo_store.fingerprint`, which sorts an array
only when the owning `CollectionSpec` names it in ``set_fields``; every other
array keeps its order so a real ordering regression still shows. This module
builds four `$addToSet` sets on `slots` (:data:`SLOT_SET_FIELDS`), and as of this
sub-plan `mongo_store.COLLECTIONS["slots"]` declares ``set_fields=()`` — the one
mirrored spec with sets and no declaration. Until it declares them, a shuffled
and a reversed replay of the *same* bind evidence fingerprint differently, purely
because `agentIds`/`evidence` land in arrival order.

`mongo_store.py` is sp-05's file and this sub-plan may not edit it, so the
one-line correction — `set_fields=SLOT_SET_FIELDS` on the `slots` row, and,
optionally, `accumulable=("taskId", "runNode", "pendingSince", "firstSeenTs",
"lastSeenTs") + SLOT_SET_FIELDS` to fence them structurally — is recorded in
this run's findings folder as
`sp-custom-state-slots-set-fields-deviation.md`. `tests/test_slots.py` asserts
the module's half (the four names, and that the documents differ in nothing but
those sets' element order) and installs the declaration for the duration of the
fingerprint assertion, so the day sp-05 pastes it the test simply stops
patching.

…and should fence the head's order field (the third handoff)
-------------------------------------------------------------
`custom_state` declares `accumulable=("seq",)` — the fence that makes `$set` on
`seq` an `OperatorError` rather than a code review. :data:`HEAD_ORDER_FIELD` is
the same kind of field and wants the same fence
(`accumulable=("seq", "order")`), and it is the same owner: sp-05's
`mongo_store.py`. Recorded as
`findings/sp-custom-state-head-order-deviation.md`. Until it lands the rule is
held here instead — nothing in this module reaches `order` through anything but
`$max`, and `tests/test_custom_state.py` walks this file's AST to keep it that
way rather than trusting the sentence.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from dataclasses import dataclass, field

from . import legacy
from . import mongo_store as ms
from . import refs
from . import store as store_mod

__all__ = [
    "CustomStateError",
    "RefRejected",
    "PayloadTooLarge",
    "SlotError",
    "COLLECTIONS",
    "KINDS",
    "ANNOTATION_KINDS",
    "CONTROL_KINDS",
    "PROVENANCE",
    "DEFAULT_PROVENANCE",
    "SLOT_PROVENANCE",
    "AUTHOR",
    "SOURCE",
    "WAL_STREAM",
    "HEAD_ORDER_FIELD",
    "HEAD_EVENT_FIELD",
    "REF_KINDS",
    "REF_KINDS_BY_KIND",
    "ANNOTATION_LIMIT",
    "RESOLUTIONS",
    "RESOLUTION_RANK",
    "PENDING_TTL_SECONDS",
    "BIND_CHANNELS",
    "SLOT_SET_FIELDS",
    "ADVANCE_MAX_FIELDS",
    "SESSION_KEY_SOURCES",
    "SESSION_KEY_SOURCE_RANK",
    "SESSION_PATH_PARENTS",
    "ATTEMPT_SOURCES",
    "CONTROL_PATHS_ENV",
    "LEDGER_PATHS_ENV",
    "LEDGER_FILE",
    "validate_kind",
    "validate_provenance",
    "validate_author",
    "validate_ref_id",
    "ref_id_of",
    "resolve_ref_id",
    "annotation_state_key",
    "head_id",
    "head_order",
    "slot_id",
    "session_key_source_rank",
    "session_key_source_of",
    "control_stream",
    "ledger_stream",
    "session_key_from_path",
    "GuardedWrite",
    "CustomStateObservation",
    "SlotObservation",
    "SlotAddress",
    "SlotIndex",
    "new_counters",
    "Writer",
    "map_custom_state",
    "map_slot",
    "MIRROR_MAPPERS",
    "MIRROR_SOURCES",
    "head_write",
    "bind_write",
    "claim_op",
    "bind_advance_write",
    "bind_slot",
    "orphan_write",
    "conflict_write",
    "conflict_evidence_op",
    "apply_guarded",
    "replay",
    "rebuild_heads",
    "resolution_of",
    "SlotTable",
    "BindResult",
    "control_paths",
    "ledger_paths",
    "slot_index",
    "read_control_file",
    "read_ledger_file",
    "iter_custom_state_observations",
    "iter_slot_observations",
]


# --- vocabulary -----------------------------------------------------------

#: GD-15's fence: the three collections this module writes, and no others.
COLLECTIONS = ("custom_state_events", "custom_state", "slots")

#: R-52's closed kind list — the discriminator that keeps ONE events collection
#: and ONE head collection installation-wide (CUSTOMSTATE-17). `artifact` is
#: here because R-51's registry writes documents of that kind; the *registry
#: mapper* is `legacy.py`'s (registered as `legacyArtifact`, so no two modules
#: register one kind), and this list is about the documents, not the mapper.
KINDS = ("ledger", "control_intent", "control_ack", "topology", "agent_state",
         "annotation", "tag", "artifact")

#: The kinds whose payload is **user prose**, and therefore the kinds that get
#: their own cap and the rejection instead of the truncation (CUSTOMSTATE-16).
ANNOTATION_KINDS = ("annotation", "tag")

#: Folded into `custom_state_events` rather than given a `control` collection of
#: their own (GD-24's table note): the audit is one log, queried by kind.
CONTROL_KINDS = ("control_intent", "control_ack")

#: GD-28, pinned identically by `mongo_store.COLLECTIONS["custom_state*"]` and
#: by `store.stream_provenance`. Two values, no third: this module authors
#: Touch's own state and records what agents assert, and it can do neither of
#: the other three things the enum names.
PROVENANCE = ("asserted", "touch")

#: What :class:`Writer` writes unless told otherwise: state Touch itself
#: authors. `asserted` is for lines that carry an *agent's* claim (a ledger
#: line, a control intent an orchestrator wrote).
DEFAULT_PROVENANCE = "touch"

#: `slots` is Touch's own join over other people's evidence, so GD-24 pins it to
#: `{derived, touch}` — a different enum from the two custom-state collections,
#: and the reason :func:`validate_provenance` takes the collection.
SLOT_PROVENANCE = "derived"

#: CUSTOMSTATE-16: Touch has **no user identity model**. GD-13's per-boot token
#: authenticates a browser, not a person, so `author` is this literal and never
#: a hostname, a username or an invented id. The field exists for a future
#: identity model; until there is one, writing anything else would be fabricated
#: data (D13).
AUTHOR = "local"

#: D4's channel for records this module writes. `store.KNOWN_SOURCES` is open at
#: the tail by design ("R-52's custom-state kinds are the designed-for case"),
#: so this is a new well-formed slug rather than an edit to `store.py`.
SOURCE = "touch"

#: The `.touch/` WAL stream (`store.Store.SINGLETON_STREAMS`). One of the two
#: streams `store.DURABLE_STREAMS` fsyncs per append, because this is the data a
#: rebuild from `~/.claude` cannot reconstruct.
WAL_STREAM = "custom-state"

#: The head's ordering field (see the module docstring). `seq` stays on the
#: document — R-52 names it, and within one stream it is still the whole of the
#: order — but the payload guard rides this composite, because the head is one
#: space and `seq` is per-stream. Declared as a name rather than spelled at the
#: two call sites so the guard, the `$max` and the test all mean one field.
HEAD_ORDER_FIELD = "order"

#: The head's per-event sub-document: every field that varies between the events
#: of one `(refId, stateKey)` — `sessionKey`, `sessionKeySource`,
#: `attemptSource`, `ts`/`tsRaw`. ONE container, because `$set` replaces a
#: sub-document **wholesale** while it merely overwrites the top-level keys it
#: carries — and GD-26 leaves this module no field-removing operator to clean up
#: the difference (the verb list is asserted absent from this file). An event
#: that states no session must not inherit the session of the event it
#: outranked, and which of them arrived first must not be able to decide.
#: Everything outside it is either identical for every event of the head
#: (`refId`, `stateKey`, `derived`) or accumulated (`seq`, `order`, `ts`).
HEAD_EVENT_FIELD = "event"

#: R-52's refId rule: a custom-state document may only point at an agent, a
#: workflow node or a slot. `refs.ref_id_kinds` answers with every grammar that
#: could have produced a key, and acceptance is a non-empty intersection with
#: this set — several kinds legitimately answer at once (GD-24's reason for
#: returning the whole set), and demanding a unique answer would reject every
#: 17-hex agentId, which is also a syntactically valid `runs` key.
REF_KINDS = ("agentId", "runNode", "slot")

#: The one documented widening (see the module docstring): a `topology` head
#: describes a run and MUST carry `refId = refs.run_key(runId)`, because that is
#: the only key `agents.topology_index` joins on.
REF_KINDS_BY_KIND = {"topology": REF_KINDS + ("run",)}

#: CUSTOMSTATE-16's cap, in bytes of the encoded payload. It **rejects**; it
#: never truncates. GD-11's 1 KB `detail` cap exists because machine detail
#: strings are embedded in shell and JS templates — a rule with no claim over a
#: human's prose, where silently eating the tail is data loss.
ANNOTATION_LIMIT = 16 * 1024

#: R-53's state machine. `orphaned` is a normal outcome, not an error state.
RESOLUTIONS = ("pending", "bound", "orphaned", "conflict")

#: Monotone severity. Every advance is guarded on `{"resolutionRank":
#: {"$lt": rank}}`, which is what makes the slot document order-independent
#: under GD-25's shuffled/reversed replay: `bound` cannot be undone by a
#: late-arriving `pending` observation, and `conflict` outranks everything
#: because a slot with two agentIds is not a slot anybody may act on.
RESOLUTION_RANK = {"pending": 0, "orphaned": 1, "bound": 2, "conflict": 3}

#: CUSTOMSTATE-9's TTL: 300 s, the 180 s idle threshold plus slack. A pending
#: slot older than this — or one whose run has reached a terminal — is
#: `orphaned`, which is a conclusion Touch renders, not one it hides.
PENDING_TTL_SECONDS = 300

#: R-53's three evidence channels, in the order they are trusted: the `[touch]`
#: marker is the orchestrator's own statement inside the prompt, the ledger line
#: is written immediately after the spawn, the Agent-tool `description` is the
#: only channel left when neither survived.
BIND_CHANNELS = ("marker", "ledger", "description")

#: The `slots` fields this module builds with `$addToSet` — i.e. **sets**, whose
#: element order is arrival order in Mongo and in `mongo_store`'s in-memory twin
#: alike. GD-25's oracle (`mongo_store.fingerprint`) sorts an array only when the
#: owning `CollectionSpec` declares it in ``set_fields``, so a set the spec does
#: not declare makes the shuffled/reversed acceptance pass compare arrival
#: orders and diverge. Declared here as one tuple; `tests/test_slots.py` derives
#: the `$addToSet` keys this module actually emits and asserts the two agree, so
#: the module cannot grow a fifth set without the assertion naming it.
#:
#: The matching declaration on `mongo_store.COLLECTIONS["slots"]` is sp-05's to
#: paste (this sub-plan owns no line of `mongo_store.py`); it is recorded, with
#: the exact tuple, in this run's
#: `findings/sp-custom-state-slots-set-fields-deviation.md`.
SLOT_SET_FIELDS = ("agentIds", "conflictAgentIds", "conflictWith", "evidence")

#: The advance fields that must ride `$max` rather than `$set`, because the
#: mapper already accumulates them with `$max`: a bind that `$set` them could
#: *lower* what an observation raised, and the stored value would then depend on
#: which of the two arrived last — GD-25's named failure, verbatim.
ADVANCE_MAX_FIELDS = ("taskId", "runNode")

#: Where a `sessionKey` came from. `path` is CUSTOMSTATE-10's rule for
#: pre-amendment ledger lines: derived from the containing directory, recorded
#: as such, and never presented as something the writer stated. `slots` is the
#: same honesty for a control line, whose session comes from the slot the name
#: resolved to rather than from anything the line said.
SESSION_KEY_SOURCES = ("ledger", "marker", "path", "slots")

#: How much each of those channels is worth, as an explicit rank. A slot
#: document accumulates evidence from several lines, so *something* has to
#: decide which attribution the document keeps — and on a bare `$max` over the
#: strings that decision is the alphabet (`path` > `marker` > `ledger`), which
#: reports a session the writer itself stated as one Touch guessed from a
#: directory name. The direction, stated once: **the most directly stated
#: channel wins**. An amended ledger line and a `[touch]` marker are the
#: orchestrating session's own statement of its `<pid>-<procStart>`; `slots` is
#: Touch's name→slot hop; `path` is a directory name Touch read. `$max` over the
#: rank is still order-independent (GD-25), and a weaker later observation can
#: no longer downgrade a stronger earlier one.
SESSION_KEY_SOURCE_RANK = {"path": 0, "slots": 1, "marker": 2, "ledger": 3}

#: A `<pid>-<procStart>` component is only read as a session identity when its
#: **parent** directory is one the session layout names. `^[1-9]\d*-\d+$` also
#: matches `2026-07` and `1-2`, and a date-named or version-named folder that
#: silently becomes a session produces an addressable slot that can never bind —
#: a phantom the sweep would later render as `orphaned`. Skipping is the
#: cheaper wrong answer (D13).
SESSION_PATH_PARENTS = ("sessions", "session", ".touch")

#: Where an `attempt` came from, recorded on every document whose address needed
#: one resolved. `stated` is the line's own number; `resolved` is the highest
#: attempt observed for that slot. There is deliberately no `default`: names are
#: logical and attempts are physical (touch-orchestrate), so an invented attempt
#: addresses a real, stale slot rather than failing loudly.
ATTEMPT_SOURCES = ("stated", "resolved")

#: SD-8. A configured list, `os.pathsep`-separated. Deliberately with no default
#: path: the skill file and the base plan already disagree about where the
#: control file lives, and a third statement here would make it three
#: (CUSTOMSTATE-11).
CONTROL_PATHS_ENV = "TOUCH_CONTROL_PATHS"

#: The same override for spawn ledgers. Unlike control files, ledgers *do* have
#: an undisputed location (`<task-dir>/state/spawn-ledger.jsonl`), so discovery
#: walks the orchestrator root and this variable only narrows it.
LEDGER_PATHS_ENV = "TOUCH_LEDGER_PATHS"

LEDGER_FILE = "spawn-ledger.jsonl"
STATE_DIR = "state"

#: Stream id prefixes for the two foreign, append-only file genres. Their
#: `(stream, seq)` is positional — `seq` is the line number — which is legal for
#: exactly the reason `legacy_events` is: the files are only ever appended to.
CONTROL_STREAM_PREFIX = "control"
LEDGER_STREAM_PREFIX = "ledger"

#: `refs.escape_stream` caps a stream id at 200 characters; the longest prefix
#: plus its separator takes eight.
MAX_STREAM_SCOPE_CHARS = 190

#: Characters a stream id may carry unescaped, and the percent-encoding used for
#: everything else. Byte-for-byte the outer layer `legacy._stream_safe` applies
#: to a task folder name — the same problem (a user-chosen name that
#: `refs.escape_stream` would reject outright rather than escape) needs the same
#: answer, and `tests/test_custom_state.py` asserts the two agree on the corpus
#: rather than trusting that they still do.
_STREAM_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._+@=,-]")
_STREAM_PCT_RE = re.compile(r"%([0-9A-Fa-f]{2})")

#: `<pid>-<procStart>`, the one composite `refs` validates as a `sessionKey`.
_SESSION_KEY_RE = re.compile(r"^[1-9]\d*-\d+$")


class CustomStateError(ValueError):
    """A caller-side misuse: state this module refuses to record.

    A `ValueError` subclass for `refs.RefError`'s reason — every caller is
    Touch's own code, so a malformed write is a programmer error that must
    surface before it reaches a permanent store. `mirror.Mapper` converts it
    into a `MapperError` naming this module.
    """


class RefRejected(CustomStateError):
    """R-52's rejection: a `refId` no allowed grammar could have produced.

    Named separately because it is the one refusal that is about the *caller's
    data model* rather than its syntax, and the API layer answers it with a
    400 rather than a 500. A dangling state card is worse than a rejected one:
    the UI would render a card for a node that does not exist.
    """


class PayloadTooLarge(CustomStateError):
    """CUSTOMSTATE-16's 413. Carries :attr:`status` so the API need not guess."""

    status = 413

    def __init__(self, message, *, size=None, limit=ANNOTATION_LIMIT):
        super().__init__(message)
        self.size = size
        self.limit = limit


class SlotError(CustomStateError):
    """A slot observation that cannot be keyed (R-53)."""


# --- validation (pure) ----------------------------------------------------


def validate_kind(kind) -> str:
    """One of :data:`KINDS`. Closed, unlike `store.KNOWN_KINDS`.

    The file-side kind list is open at the tail so later sub-plans need not edit
    `store.py`; this one is closed because it is a *collection discriminator*.
    An unrecognised kind here would be a document nothing queries and nothing
    renders — the silent-drop outcome GD-26 forbids, wearing a valid shape.
    """
    if kind not in KINDS:
        raise CustomStateError(
            f"custom-state kind must be one of {list(KINDS)}, got {kind!r} — the "
            f"kind is what discriminates ONE installation-wide collection (R-52)"
        )
    return kind


def validate_provenance(value, *, collection="custom_state_events") -> str:
    """GD-28's enum for ``collection``. The only door this module has.

    `custom_state*` accepts :data:`PROVENANCE`; `slots` accepts what GD-24 pins
    it to. Everything else — including any claim that a value came from the
    harness — is refused here, which is the structural half of CUSTOMSTATE-15's
    "never masquerade as fact": there is no branch in this file that reaches a
    write with a value this function did not return.
    """
    allowed = ms.spec_for(collection).provenance or PROVENANCE
    if value not in allowed:
        raise CustomStateError(
            f"{collection} accepts provenance {list(allowed)} only, got {value!r} "
            f"(GD-28: the custom-state writer has no code path to any other value)"
        )
    return value


def validate_author(value) -> str:
    """CUSTOMSTATE-16's literal — on the READ door as much as the write one.

    :data:`AUTHOR` is an invariant of the *module*, not of :meth:`Writer.append`:
    GD-29 explicitly contemplates agent-side file appends, and
    :meth:`CustomStateObservation.from_record` reads `author` straight off a WAL
    line, so a line this module did not write can otherwise carry
    `author: "someone@host"` into `custom_state_events` and into the head. Touch
    has no user identity model — GD-13's token authenticates a browser, not a
    person — so a name there is fabricated data (D13), and fabricated data is
    worse than a refused line.

    Missing is normalised (an old or terse record simply did not say, and the
    module's answer for "who" is the same literal either way); anything else is a
    :class:`CustomStateError`, which `mirror.Mapper` turns into a counted
    rejection rather than a silent store.
    """
    if value is None:
        return AUTHOR
    if value != AUTHOR:
        raise CustomStateError(
            f"author is the literal {AUTHOR!r}: Touch has no user identity model, "
            f"and GD-13's token authenticates a browser rather than a person "
            f"(CUSTOMSTATE-16) — got {value!r}")
    return AUTHOR


def allowed_ref_kinds(kind=None) -> tuple:
    """The grammars a `refId` may belong to for ``kind`` (see the docstring)."""
    return REF_KINDS_BY_KIND.get(kind, REF_KINDS)


def validate_ref_id(ref_id, *, kind=None) -> str:
    """R-52's refId rule, as a function. Raises :class:`RefRejected`.

    Acceptance is "some allowed grammar canonically produces this key", not
    "exactly one does": `refs.ref_id_kinds` answers with the whole set because
    several grammars legitimately overlap (a 17-hex agentId is also a
    syntactically valid `runs` key), and GD-24's rule is that the *caller*
    decides between them — which is what ``kind`` does here.
    """
    if not isinstance(ref_id, str) or not ref_id:
        raise RefRejected("a custom-state refId must be a non-empty string")
    allowed = allowed_ref_kinds(kind)
    kinds = refs.ref_id_kinds(ref_id)
    if not set(kinds) & set(allowed):
        raise RefRejected(
            f"refId {ref_id!r} is not an {' / '.join(allowed)} key "
            f"(it parses as {list(kinds) or 'nothing'}) — a state card pointing at a "
            f"node that does not exist is worse than a rejected write (R-52)"
        )
    return ref_id


def resolve_ref_id(ref, ref_id, *, kind=None) -> str:
    """The validated `refId` for a write that may carry a ref, an id, or both.

    When both are given they must **agree**: GD-24 stores the structured `ref{}`
    beside the scalar `refId` ("flat + denormalized"), and a document whose two
    halves point at different entities is undetectable downstream — every reader
    joins on one of them and none of them compares. One comparison here is the
    whole cost of making that shape unrepresentable.
    """
    if ref_id is None:
        return ref_id_of(ref, kind=kind)
    validate_ref_id(ref_id, kind=kind)
    if isinstance(ref, dict) and ref:
        try:
            derived = refs.ref_id(ref)
        except refs.RefError as exc:
            raise RefRejected(f"unusable custom-state ref: {exc}") from None
        if derived and derived != ref_id:
            raise RefRejected(
                f"ref {sorted(ref)} names {derived!r} but refId says {ref_id!r} — a "
                f"document whose ref{{}} and refId point at different entities joins "
                f"two ways to two answers (GD-24)")
    return ref_id


def ref_id_of(ref, *, kind=None) -> str:
    """`refId` for a structured ref, validated (:func:`validate_ref_id`).

    The ref itself goes on the document as `ref{}` in `refs.canonical_ref`'s
    fixed field order; this is GD-24's "flat + denormalized" scalar beside it.
    A ref shape with no key — an unknown shape, or one that names a grouping
    rather than a document — is rejected here rather than stored with a null
    `refId`, because a custom-state document that joins to nothing is the exact
    dangling card R-52 refuses.
    """
    if not isinstance(ref, dict) or not ref:
        raise RefRejected("a custom-state write needs a ref or a refId")
    try:
        key = refs.ref_id(ref)
    except refs.RefError as exc:
        raise RefRejected(f"unusable custom-state ref: {exc}") from None
    if not key:
        raise RefRejected(
            f"ref {sorted(ref)} names no document — custom state must point at an "
            f"{' / '.join(allowed_ref_kinds(kind))} (R-52)"
        )
    return validate_ref_id(key, kind=kind)


def check_payload(kind, custom) -> int:
    """Size of ``custom`` in bytes; raises :class:`PayloadTooLarge` over the cap.

    Only :data:`ANNOTATION_KINDS` are capped, and they are capped by
    **rejection**. Machine payloads ride `store.MAX_RECORD_BYTES` and
    `mongo_store.guard_oversize`, both of which stub rather than raise — the
    right answer for a tool result, and the wrong one for something a person
    typed (CUSTOMSTATE-16).
    """
    if custom is None:
        return 0
    if not isinstance(custom, dict):
        raise CustomStateError(
            f"a custom-state payload must be a dict (it is stored under "
            f"data.custom), got {type(custom).__name__}"
        )
    size = ms.document_size(custom)
    if kind in ANNOTATION_KINDS and size > ANNOTATION_LIMIT:
        raise PayloadTooLarge(
            f"{kind} payload is {size} bytes, over the {ANNOTATION_LIMIT}-byte cap — "
            f"rejected rather than truncated: a machine detail string may lose its "
            f"tail, a person's prose may not (CUSTOMSTATE-16)",
            size=size, limit=ANNOTATION_LIMIT)
    return size


def _state_key(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CustomStateError("stateKey must be a non-empty string (it is half of the head _id)")
    return value


def annotation_state_key(annotation_id) -> str:
    """`annotation:<id>` — one head per annotation, so an edit supersedes.

    CUSTOMSTATE-16's edit rule needs a key finer than the kind: edits are new
    events superseding by `(target ref, annotationId)`, and the head shows the
    latest non-tombstoned version. A kind-wide key would make a second
    annotation on one agent an *edit* of the first.
    """
    if not isinstance(annotation_id, str) or not annotation_id.strip():
        raise CustomStateError("an annotationId must be a non-empty string")
    return f"annotation:{annotation_id}"


def head_id(ref_id, state_key) -> str:
    """`<refId>#<stateKey>` — the head's `_id` (GD-24), through `refs`."""
    return refs.custom_state_key(ref_id, _state_key(state_key))


def head_order(stream, seq) -> str:
    """`<seq padded>|<escaped stream>` — the head's TOTAL order (GD-25).

    The value :data:`HEAD_ORDER_FIELD` carries. Padded with `refs`' own width so
    the primary component sorts exactly as the event `_id`'s does: lexicographic
    order equals numeric order, one stream's events order by `seq` alone (R-52's
    `{seq:{$lt:newSeq}}`, unchanged in meaning), and two streams that happen to
    collide on `seq` are separated by the stream id instead of by whichever the
    tick happened to read first. A `seq` wider than the pad widens rather than
    truncates, exactly as `refs._pad` does — order then holds within a width
    class, which for 10^12 events on one stream is a bridge to cross later.

    The stream is required: a head with no stream has no order, and inventing
    one ("" sorts below everything) would quietly make one nameless stream win
    every tie.
    """
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise CustomStateError(
            f"a head order needs the event's seq, got {seq!r}")
    try:
        escaped = refs.escape_stream(stream)
    except refs.RefError as exc:
        raise CustomStateError(
            f"a head order needs the event's own stream: {exc}") from None
    return "%s|%s" % (str(seq).zfill(refs.PADDED_INTS["seq"]), escaped)


def slot_id(session_key, root, name, attempt) -> str:
    """`slot:<sessionKey>|<root>|<name>|<attempt:03d>` (GD-24), through `refs`."""
    try:
        return refs.slot_key(session_key, root, name, attempt)
    except refs.RefError as exc:
        raise SlotError(f"unusable slot identity: {exc}") from None


def session_key_source_rank(value) -> int:
    """The rank of one `sessionKeySource` label (:data:`SESSION_KEY_SOURCE_RANK`).

    An unknown label is refused rather than ranked 0: a channel nobody declared
    is not "the weakest evidence", it is a caller writing a field this module
    does not understand, and silently filing it under `path` would attribute a
    real statement to a directory name.
    """
    if value not in SESSION_KEY_SOURCE_RANK:
        raise SlotError(
            f"sessionKeySource must be one of {list(SESSION_KEY_SOURCES)}, got {value!r}")
    return SESSION_KEY_SOURCE_RANK[value]


def session_key_source_of(doc):
    """The `sessionKeySource` label a slot document's rank stands for.

    The slot document stores `sessionKeySourceRank` (an int, `$max`-accumulated)
    rather than the label, so read-time is where the label comes back. The
    individual `custom_state_events` documents keep the *literal* channel of
    their own line — this answers the different question "of everything observed
    about this slot, how directly was its session stated?".
    """
    if not isinstance(doc, dict):
        raise SlotError("a slot document must be a dict")
    rank = doc.get("sessionKeySourceRank")
    if isinstance(rank, int) and not isinstance(rank, bool):
        for label, value in SESSION_KEY_SOURCE_RANK.items():
            if value == rank:
                return label
    return doc.get("sessionKeySource")


# --- stream ids for foreign, append-only files ----------------------------


def _stream_safe(text: str) -> str:
    """Percent-encode what `refs`' stream grammar rejects outright.

    An outer layer over `refs.escape_stream`, for the same reason
    `legacy._stream_safe` is one: the grammar *rejects* a space or a slash
    rather than escaping it, and these scopes are user-chosen folder names. Both
    layers are invertible, and both round-trips are asserted in the tests.
    """
    return _STREAM_UNSAFE_RE.sub(
        lambda match: "".join("%%%02X" % byte for byte in match.group(0).encode("utf-8")),
        text)


def _stream_unsafe(text: str) -> str:
    """Inverse of :func:`_stream_safe`, decoded as UTF-8 **bytes**.

    Byte-wise because a non-ASCII character escapes to several `%XX` pairs, and
    decoding them one at a time produces mojibake instead of the name that went
    in.
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


def _scoped_stream(prefix, scope) -> str:
    if not isinstance(scope, str) or not scope:
        raise CustomStateError(f"a {prefix} stream needs a non-empty scope")
    safe = _stream_safe(scope)
    if len(safe) > MAX_STREAM_SCOPE_CHARS:
        raise CustomStateError(
            f"scope too long to key a {prefix} stream ({len(safe)} > "
            f"{MAX_STREAM_SCOPE_CHARS} characters after escaping): {scope!r}")
    return f"{prefix}:{safe}"


def control_stream(scope) -> str:
    """`control:<scope>` — one stream per control file (positional `seq`)."""
    return _scoped_stream(CONTROL_STREAM_PREFIX, scope)


def ledger_stream(scope) -> str:
    """`ledger:<scope>` — one stream per spawn ledger (positional `seq`)."""
    return _scoped_stream(LEDGER_STREAM_PREFIX, scope)


def scope_of_stream(stream) -> str:
    """Inverse of :func:`control_stream`/:func:`ledger_stream`."""
    prefix, sep, rest = str(stream).partition(":")
    if prefix not in (CONTROL_STREAM_PREFIX, LEDGER_STREAM_PREFIX) or not sep:
        raise CustomStateError(f"not a control/ledger stream id: {stream!r}")
    return _stream_unsafe(rest)


def session_key_from_path(path):
    """`<pid>-<procStart>` read out of a containing directory, or None.

    CUSTOMSTATE-10's fallback for ledger lines written before the amendment: the
    session attribution is the path the line was read from, and the moment those
    lines become documents the path is gone. Derived here, recorded as
    `sessionKeySource: "path"`, and never presented as something the writer
    said.

    Only under a directory the session layout names (:data:`SESSION_PATH_PARENTS`):
    the grammar alone also matches `2026-07` and `1-2`, so a ledger sitting under
    a date- or version-named folder would otherwise be attributed to an invented
    session and become a slot that can never bind. The surrounding code prefers
    to skip rather than to fabricate an address, and so does this.
    """
    if not path:
        return None
    parts = os.path.abspath(os.fspath(path)).split(os.sep)
    for index in range(len(parts) - 1, 0, -1):
        if _SESSION_KEY_RE.match(parts[index]) and parts[index - 1] in SESSION_PATH_PARENTS:
            return parts[index]
    return None


def _path_scope(path, human) -> str:
    """`<human>-<sha1(realpath)[:8]>` — a stream scope that cannot collide.

    Two configured files whose *containing folders* share a name (a
    `<taskA>/state/…` and a `<taskB>/state/…`, both scoping to `state`) would
    otherwise produce one stream id, and line 1 of each would map to the same
    `custom_state_events` `_id`. That collection is `$setOnInsert`-only, so the
    second file's line would be swallowed as a tolerated duplicate: the silent
    collapse GD-25's count assertion exists to catch. The digest is of the
    resolved path, so the same file reached by two routes still scopes to one
    stream.
    """
    real = os.path.realpath(os.fspath(path))
    digest = hashlib.sha1(real.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    return f"{human}-{digest}"


# --- observations ---------------------------------------------------------


@dataclass(frozen=True)
class CustomStateObservation:
    """One custom-state event, from the WAL or from a foreign control file.

    `(stream, seq)` is preserved from the source file so the file stays a
    replayable WAL (CUSTOMSTATE-14) and the `_id` is positional over an
    append-only source — the same argument that makes `legacy_events` safe.
    """

    kind: str
    stream: str
    seq: int
    state_key: str
    ref_id: object = None
    ref: object = None
    custom: object = None
    provenance: str = DEFAULT_PROVENANCE
    author: str = AUTHOR
    ts: object = None
    ts_raw: object = None
    session_key: object = None
    session_key_source: object = None
    tombstone: bool = False
    task_id: object = None
    source_path: object = None
    path_source: object = None
    #: `stated` when the line carried the attempt of the slot it addresses,
    #: `resolved` when the name→slot hop supplied it (:class:`SlotIndex`). On
    #: the document, so a reader can say "this stop card was addressed by
    #: inference" instead of presenting the address as the writer's own.
    attempt_source: object = None

    @classmethod
    def from_record(cls, record, *, stream=WAL_STREAM, source_path=None):
        """Build from one touch-events-v2 WAL record (`store.Store` output)."""
        if not isinstance(record, dict):
            raise CustomStateError(
                f"a WAL record must be a dict, got {type(record).__name__}")
        data = record.get("data")
        data = data if isinstance(data, dict) else {}
        ref = record.get("ref") or None
        ref_id = data.get("refId")
        if not ref_id and ref:
            # A WAL line is agent-adjacent data, so a malformed `ref` must leave
            # this module through its own hierarchy: `mirror.Mapper` converts
            # `CustomStateError` into a counted rejection and lets a bare
            # `refs.RefError` escape the tick.
            try:
                ref_id = refs.ref_id(ref)
            except refs.RefError as exc:
                raise CustomStateError(
                    f"a WAL record's ref is not addressable: {exc}") from None
        return cls(
            kind=record.get("kind"),
            stream=stream,
            seq=record.get("seq"),
            state_key=data.get("stateKey"),
            ref_id=ref_id,
            ref=ref,
            custom=data.get("custom"),
            provenance=record.get("provenance"),
            author=data.get("author", AUTHOR),
            ts=record.get("ts"),
            ts_raw=record.get("ts"),
            session_key=data.get("sessionKey"),
            session_key_source=data.get("sessionKeySource"),
            tombstone=bool(data.get("tombstone")),
            task_id=data.get("taskId"),
            source_path=source_path,
            path_source=data.get("pathSource"),
            attempt_source=data.get("attemptSource"),
        )

    @classmethod
    def from_document(cls, doc):
        """Rebuild an observation from a stored `custom_state_events` document.

        The other direction of :func:`map_custom_state`, and the thing that
        makes "drop `custom_state`, rebuild, document-for-document equal" a
        one-line test rather than a second parser.
        """
        if not isinstance(doc, dict):
            raise CustomStateError("a custom_state_events document must be a dict")
        payload = doc.get("data")
        custom = payload.get("custom") if isinstance(payload, dict) else None
        if ms.is_raw_wrapper(custom):
            custom = ms.unwrap_raw(custom)
        return cls(
            kind=doc.get("kind"),
            stream=doc.get("stream"),
            seq=doc.get("seq"),
            state_key=doc.get("stateKey"),
            ref_id=doc.get("refId"),
            ref=doc.get("ref"),
            custom=custom,
            provenance=doc.get("provenance"),
            author=doc.get("author", AUTHOR),
            ts=doc.get("ts"),
            ts_raw=doc.get("tsRaw"),
            session_key=doc.get("sessionKey"),
            session_key_source=doc.get("sessionKeySource"),
            tombstone=bool(doc.get("tombstone")),
            task_id=doc.get("taskId"),
            source_path=doc.get("sourcePath"),
            path_source=doc.get("pathSource"),
            attempt_source=doc.get("attemptSource"),
        )


@dataclass(frozen=True)
class SlotObservation:
    """One piece of evidence about a `(sessionKey, root, name, attempt)` slot.

    Every bind channel produces this shape: the `[touch]` marker (via
    :func:`slot_from_labels`), a ledger line, an Agent-tool `description`. The
    slot document is the *accumulation* of them, which is why the mapper writes
    only monotone evidence and the conclusion is a guarded write.
    """

    session_key: str
    root: str
    name: str
    attempt: int
    parent: object = None
    role: object = None
    agent_id: object = None
    task_id: object = None
    run_node: object = None
    bound_by: object = None
    ts: object = None
    ts_raw: object = None
    session_key_source: str = "ledger"
    stream: object = None
    seq: object = None
    source_path: object = None
    terminal: bool = False
    line: object = None

    @property
    def key(self) -> str:
        return slot_id(self.session_key, self.root, self.name, self.attempt)


@dataclass(frozen=True)
class SlotAddress:
    """The slot a name resolved to, and how much of it had to be inferred."""

    key: str
    session_key: str
    root: str
    name: str
    attempt: int
    attempt_source: str = "stated"
    session_key_source: str = "slots"


class SlotIndex:
    """name → the slots observed under it: R-53's hop, used to *address* a line.

    A control line names an agent by `name` and by nothing else — no `root`, no
    `sessionKey`, no `attempt` (touch-orchestrate §4). Demanding those fields on
    the line would re-implement this join by fiat and then fail it, which is how
    an ingest arm ends up reading zero of the only format that exists. So the
    resolution happens where R-53 says it happens: against the slots the ledger
    and marker channels actually produced.

    Deliberately conservative, in both directions:

    * a name observed under two different `(sessionKey, root)` identities is
      **ambiguous** — two sessions picking one `ROOT_NAME` from near-identical
      task names is the very case CUSTOMSTATE-10 exists for, and guessing here
      would bind one session's stop onto the other session's agent;
    * with no `attempt` on the line, the **highest observed** attempt wins,
      because touch-orchestrate re-runs a stopped slot as `attempt` + 1 and the
      newest attempt is the only one a stop can still affect.
    """

    def __init__(self, items=()):
        self._entries = []
        for item in items or ():
            self.add(item)

    def __len__(self):
        return len(self._entries)

    def add(self, item) -> bool:
        """Index one slot: a key string, a :class:`SlotObservation`, or a doc."""
        parsed = self._parts(item)
        if parsed is None:
            return False
        if parsed not in self._entries:
            self._entries.append(parsed)
        return True

    def entries(self) -> list:
        """`[(sessionKey, root, name, attempt)]`, in the order first observed."""
        return list(self._entries)

    @staticmethod
    def _parts(item):
        if isinstance(item, SlotObservation):
            fields = (item.session_key, item.root, item.name, item.attempt)
        elif isinstance(item, str):
            try:
                parsed = refs.parse_ref_key("slot", item)
            except refs.RefError:
                return None
            fields = (parsed["sessionKey"], parsed["root"], parsed["name"],
                      parsed["attempt"])
        elif isinstance(item, dict):
            if "sessionKey" in item:                     # a stored `slots` document
                fields = (item.get("sessionKey"), item.get("root"), item.get("name"),
                          item.get("attempt"))
            else:                                        # an observation as a dict
                fields = (item.get("session_key"), item.get("root"), item.get("name"),
                          item.get("attempt"))
        else:
            return None
        session, root, name, attempt = fields
        if not all(isinstance(part, str) and part for part in (session, root, name)):
            return None
        if not isinstance(attempt, int) or isinstance(attempt, bool):
            return None
        return (session, root, name, attempt)

    def resolve(self, name, *, session_key=None, root=None, attempt=None) -> tuple:
        """`(SlotAddress | None, reason)` — `resolved` / `unknown` / `ambiguous`."""
        candidates = [entry for entry in self._entries
                      if entry[2] == name
                      and (session_key is None or entry[0] == session_key)
                      and (root is None or entry[1] == root)
                      and (attempt is None or entry[3] == attempt)]
        if not candidates:
            return None, "unknown"
        identities = {(entry[0], entry[1]) for entry in candidates}
        if len(identities) > 1:
            return None, "ambiguous"
        chosen = max(candidates, key=lambda entry: entry[3])
        try:
            key = slot_id(chosen[0], chosen[1], chosen[2], chosen[3])
        except SlotError:
            return None, "unknown"
        return SlotAddress(
            key=key, session_key=chosen[0], root=chosen[1], name=chosen[2],
            attempt=chosen[3],
            attempt_source="stated" if attempt is not None else "resolved",
            session_key_source="ledger" if session_key else "slots"), "resolved"


def new_counters() -> dict:
    """The counter shape both foreign-file readers keep (M2/D13).

    Same vocabulary as :attr:`Writer.counters` and :attr:`SlotTable.counters`:
    a reader that drops a line silently leaves an operator unable to tell
    "nothing happened yet" from "everything I wrote was rejected", which is the
    quiet failure GD-26's posture and D13's honesty rule both forbid.
    """
    return {"read": 0, "parsed": 0, "skipped_malformed": 0,
            "skipped_unaddressable": 0, "skipped_ambiguous": 0, "unreadable": 0}


def slot_from_labels(labels, *, session_key, agent_id=None, ts=None,
                     bound_by="marker", source_path=None):
    """A :class:`SlotObservation` from an `agents.Labels`-shaped object.

    Duck-typed on purpose: `agents.py` owns the marker parser and this module
    owns the hop, and a hard import in this direction would make the two files
    one. The fields read are exactly GD-9's label layer.
    """
    attempt = getattr(labels, "attempt", None)
    if not isinstance(attempt, int) or isinstance(attempt, bool):
        # GD-9 keeps an unparsable `attempt=` verbatim rather than defaulting it;
        # a slot _id needs an int, so a label Touch cannot read is not evidence.
        raise SlotError(
            f"a slot needs an integer attempt (the marker said {attempt!r}) — "
            f"names are logical, attempts are physical (touch-orchestrate)")
    name = getattr(labels, "name", None)
    root = getattr(labels, "root", None)
    if not name or not root:
        raise SlotError(
            "a slot needs both `name=` and `root=` from the [touch] marker; a "
            "node without them is unconventional, not a slot (GD-7/R-28)")
    return SlotObservation(
        session_key=session_key, root=root, name=name, attempt=attempt,
        parent=getattr(labels, "parent", None), role=getattr(labels, "role", None),
        agent_id=agent_id, bound_by=bound_by if agent_id else None, ts=ts,
        session_key_source="marker", source_path=source_path)


# --- the WAL (the writing half) -------------------------------------------


class Writer:
    """Appends custom state to `.touch/custom-state.jsonl`, WAL-first (R-52).

    Thin on purpose: `store.Store` already owns single-writer-per-stream,
    `flock`'d appends, per-file `seq`, the torn-tail repair and the fsync this
    stream gets for being unrebuildable. What this class adds is R-52's rules —
    the closed kind list, the refId validation, the annotation cap that rejects,
    and the provenance door — applied *before* a byte reaches the file, so the
    WAL never contains a record the mirror would have to refuse.
    """

    stream = WAL_STREAM

    def __init__(self, store=None, *, root=None):
        self.store = store if store is not None else store_mod.Store(root)
        self.counters = {"appended": 0, "rejected": 0, "tombstones": 0}

    # -- writing -----------------------------------------------------------

    def append(self, kind, *, state_key, ref=None, ref_id=None, custom=None,
               provenance=DEFAULT_PROVENANCE, author=AUTHOR, session_key=None,
               session_key_source=None, task_id=None, tombstone=False,
               path_source=None, attempt_source=None, ts=None) -> dict:
        """Append one custom-state record; return it, with its assigned `seq`.

        ``ref`` (a GD-11 ref shape) or ``ref_id`` (a `refs` key) — one of them
        is required, and both end up on the record: the structured ref for
        GD-24's `ref{}`, the scalar for the join.
        """
        try:
            record = self._append(
                kind, state_key=state_key, ref=ref, ref_id=ref_id, custom=custom,
                provenance=provenance, author=author, session_key=session_key,
                session_key_source=session_key_source, task_id=task_id,
                tombstone=tombstone, path_source=path_source,
                attempt_source=attempt_source, ts=ts)
        except CustomStateError:
            self.counters["rejected"] += 1
            raise
        self.counters["appended"] += 1
        if tombstone:
            self.counters["tombstones"] += 1
        return record

    def _append(self, kind, *, state_key, ref, ref_id, custom, provenance, author,
                session_key, session_key_source, task_id, tombstone, path_source,
                attempt_source, ts):
        validate_kind(kind)
        validate_provenance(provenance)
        state_key = _state_key(state_key)
        ref_id = resolve_ref_id(ref, ref_id, kind=kind)
        check_payload(kind, custom)
        author = validate_author(author)
        if session_key is not None:
            _validate_session_key(session_key)
        if session_key_source is not None and session_key_source not in SESSION_KEY_SOURCES:
            raise CustomStateError(
                f"sessionKeySource must be one of {list(SESSION_KEY_SOURCES)}, "
                f"got {session_key_source!r}")
        if attempt_source is not None and attempt_source not in ATTEMPT_SOURCES:
            raise CustomStateError(
                f"attemptSource must be one of {list(ATTEMPT_SOURCES)} — there is no "
                f"'default', because an invented attempt addresses a real stale slot "
                f"(R-53) — got {attempt_source!r}")
        data = {"stateKey": state_key, "refId": ref_id, "author": author,
                "custom": dict(custom or {})}
        for name, value in (("sessionKey", session_key),
                            ("sessionKeySource", session_key_source),
                            ("taskId", task_id), ("pathSource", path_source),
                            ("attemptSource", attempt_source)):
            if value is not None:
                data[name] = value
        if tombstone:
            # GD-26: a delete is an event, never a `deleteOne`. The payload rides
            # along so "what did it say when it was deleted" stays answerable.
            data["tombstone"] = True
        return self.store.append(self.stream, kind=kind, provenance=provenance,
                                 source=SOURCE, ref=dict(ref) if ref else None,
                                 data=data, ts=ts)

    def annotate(self, ref, text, *, annotation_id, ts=None, session_key=None):
        """An `annotation` event: user prose, capped by rejection, author `local`."""
        return self.append("annotation", state_key=annotation_state_key(annotation_id),
                           ref=ref, custom={"text": text, "annotationId": annotation_id},
                           ts=ts, session_key=session_key)

    def tombstone(self, kind, *, state_key, ref=None, ref_id=None, ts=None,
                  session_key=None):
        """Retract a state key by appending, never by deleting (GD-26)."""
        return self.append(kind, state_key=state_key, ref=ref, ref_id=ref_id,
                           custom={}, tombstone=True, ts=ts, session_key=session_key)

    # -- reading back ------------------------------------------------------

    def records(self) -> list:
        """Every parseable WAL record, in file line order."""
        return self.store.read_all(self.stream)

    def observations(self) -> list:
        """The WAL as observations — the input to a replay."""
        return [CustomStateObservation.from_record(record, stream=self.stream)
                for record in self.records()]


def _validate_session_key(value) -> str:
    if not isinstance(value, str) or not _SESSION_KEY_RE.match(value):
        raise CustomStateError(
            f"sessionKey must be <pid>-<procStart> as the session grammar emits it "
            f"(GD-24), got {value!r}")
    return value


# --- the mapping half (pure) ----------------------------------------------


def _only_ours(ops):
    """Structural GD-15: this module writes three collections and no others."""
    for collection, _key, _update in ops:
        if collection not in COLLECTIONS:
            raise CustomStateError(
                f"custom_state.py may only write {list(COLLECTIONS)}, not "
                f"{collection!r} — a harness collection is the mirror's, and a "
                f"derived one is the reducer's (GD-15/GD-23)")
    return ops


def _as_observation(observation, cls):
    """Accept a dataclass or the plain dict a replay/fixture hands back."""
    if isinstance(observation, cls):
        return observation
    if isinstance(observation, dict):
        try:
            return cls(**observation)
        except TypeError as exc:
            raise CustomStateError(f"unusable {cls.__name__}: {exc}") from None
    raise CustomStateError(
        f"expected a {cls.__name__} or a dict, got {type(observation).__name__}")


def _ts_pair(ts, ts_raw) -> dict:
    fields = ms.ts_fields(ts)
    if isinstance(ts_raw, str) and ts_raw:
        fields["tsRaw"] = ts_raw
    return fields


def _event_document(obs) -> tuple:
    """`(key, document)` for one `custom_state_events` row. Shared by both halves."""
    validate_kind(obs.kind)
    validate_provenance(obs.provenance)
    if not isinstance(obs.seq, int) or isinstance(obs.seq, bool) or obs.seq < 0:
        raise CustomStateError(
            f"a custom-state event needs its source (stream, seq); got seq={obs.seq!r}")
    ref_id = resolve_ref_id(obs.ref, obs.ref_id, kind=obs.kind)
    check_payload(obs.kind, obs.custom)
    key = refs.custom_state_event_key(obs.stream, obs.seq)
    doc = {
        "_id": key,
        "kind": obs.kind,
        "provenance": obs.provenance,
        "author": validate_author(obs.author),
        "stream": obs.stream,
        "seq": obs.seq,
        "stateKey": _state_key(obs.state_key),
        "refId": ref_id,
        "sessionKey": obs.session_key,
        "sessionKeySource": obs.session_key_source,
        "taskId": obs.task_id,
        "sourcePath": obs.source_path,
        "pathSource": obs.path_source,
        "attemptSource": obs.attempt_source,
        "data": {"custom": dict(obs.custom or {})},
    }
    if obs.ref:
        try:
            doc["ref"] = refs.canonical_ref(obs.ref)
        except refs.RefError as exc:
            raise CustomStateError(f"unusable ref on a custom-state event: {exc}") from None
    if obs.tombstone:
        doc["tombstone"] = True
    if obs.ts is not None:
        doc.update(_ts_pair(obs.ts, obs.ts_raw))
    elif obs.ts_raw:
        doc["tsRaw"] = obs.ts_raw
    return key, {name: value for name, value in doc.items() if value is not None}


def map_custom_state(observation):
    """`customState` ⇒ ONE insert-only `custom_state_events` document.

    Every field is `$setOnInsert`, so this mapper has no code path that updates
    or removes a document — R-52's append-only posture and GD-26's no-delete
    rule, made structural rather than remembered. Re-ingesting a line therefore
    costs one tolerated duplicate key and changes nothing, which is exactly what
    GD-25's shuffled/reversed pass measures.

    The head is **not** written here: it needs the `{seq: {$lt: newSeq}}` guard
    a triple cannot express, and it is :func:`head_write` (see the module
    docstring).
    """
    obs = _as_observation(observation, CustomStateObservation)
    key, doc = _event_document(obs)
    prepared, _report = ms.prepare_document("custom_state_events", doc)
    kept, _size = ms.guard_oversize("custom_state_events", prepared,
                                    source_path=obs.source_path or None)
    body = dict(kept)
    body.pop("_id", None)
    return _only_ours([("custom_state_events", key, ms.op_set_on_insert(body))])


def map_slot(observation):
    """`slot` ⇒ the slot's monotone evidence, plus the ledger line it came from.

    Only order-independent operators appear here — `$setOnInsert` for identity,
    `$min`/`$max` for scalars, `$addToSet` for the evidence sets — so replaying
    the same observations shuffled or reversed lands on the same document
    (GD-25). The **conclusion** (`agentId`, `boundBy`, `resolution`) is written
    by the guarded writes below, because it is a decision about the whole
    accumulation and not about the one line in hand; `resolution` is
    `$setOnInsert:"pending"` here, which is the honest state of a slot nobody has
    resolved yet.

    A ledger observation carries its own `(stream, seq)`, and then it is also an
    event of kind `ledger` — one line, one immutable document, exactly like every
    other custom-state event.
    """
    obs = _as_observation(observation, SlotObservation)
    key = obs.key
    on_insert = {
        "sessionKey": obs.session_key,
        "root": obs.root,
        "name": obs.name,
        "attempt": obs.attempt,
        "provenance": validate_provenance(SLOT_PROVENANCE, collection="slots"),
        "resolution": "pending",
    }
    maxes, mins, sets = {}, {}, {}
    for name, value in (("parent", obs.parent), ("role", obs.role),
                        ("taskId", obs.task_id), ("runNode", obs.run_node),
                        ("sourcePath", obs.source_path)):
        if value is not None:
            # `$max` on a scalar is GD-25's "scalars-first/last are $min/$max":
            # deterministic under shuffle, where `$set` is write-order dependent.
            maxes[name] = value
    if obs.session_key_source is not None:
        # …but NOT the source label itself: `$max` over the three strings settles
        # by alphabet (`path` > `marker` > `ledger`), which would report a
        # session the writer stated as one Touch derived from a directory name.
        # The rank carries the trust order explicitly (SESSION_KEY_SOURCE_RANK);
        # `session_key_source_of` reads the label back.
        maxes["sessionKeySourceRank"] = session_key_source_rank(obs.session_key_source)
    maxes["resolutionRank"] = RESOLUTION_RANK["pending"]
    if obs.agent_id is not None:
        # Evidence, not the binding: the unique sparse `agentId` index is only
        # ever touched by `bind_write`, so a colliding *observation* can never
        # make the ingest tick fail (CUSTOMSTATE-9).
        sets["agentIds"] = obs.agent_id
    if obs.bound_by is not None:
        if obs.bound_by not in BIND_CHANNELS:
            raise SlotError(
                f"boundBy must be one of {list(BIND_CHANNELS)}, got {obs.bound_by!r}")
        sets["evidence"] = obs.bound_by
    if obs.ts is not None:
        stamps = _ts_pair(obs.ts, obs.ts_raw)
        mins["pendingSince"] = stamps["ts"]
        mins["firstSeenTs"] = stamps["ts"]
        maxes["lastSeenTs"] = stamps["ts"]
    ops = [ms.op_set_on_insert(on_insert), ms.op_max(maxes)]
    if mins:
        ops.append(ms.op_min(mins))
    if sets:
        ops.append(ms.op_add_to_set(sets))
    out = [("slots", key, ms.merge_ops(*ops, collection="slots"))]
    if obs.stream and isinstance(obs.seq, int) and not isinstance(obs.seq, bool):
        out.extend(map_custom_state(_ledger_event(obs, key)))
    return _only_ours(out)


def _ledger_event(obs, slot_key) -> CustomStateObservation:
    """The `ledger` event a ledger line also is (R-52's kind list)."""
    payload = {"name": obs.name, "root": obs.root, "attempt": obs.attempt}
    for name, value in (("parent", obs.parent), ("role", obs.role),
                        ("taskId", obs.task_id), ("agentId", obs.agent_id)):
        if value is not None:
            payload[name] = value
    if obs.line:
        payload["raw"] = obs.line
    return CustomStateObservation(
        kind="ledger", stream=obs.stream, seq=obs.seq, state_key="ledger",
        ref_id=slot_key, custom=payload, provenance="asserted", ts=obs.ts,
        ts_raw=obs.ts_raw, session_key=obs.session_key,
        session_key_source=obs.session_key_source, task_id=obs.task_id,
        source_path=obs.source_path)


#: SD-1's registry. `mirror.discover_mappers` finds it by name; the two kinds
#: are unique across the five entity modules (one kind, one owner — GD-15).
MIRROR_MAPPERS = {
    "customState": map_custom_state,
    "slot": map_slot,
}


# --- guarded writes (the reduction) ---------------------------------------


@dataclass(frozen=True)
class GuardedWrite:
    """`(collection, key, update, require)` — one conditional upsert.

    The shape `mongo_store.guarded_update` and `mirror.Backend.guarded_update`
    both take. A dataclass rather than a bare tuple because ``require`` is the
    load-bearing part and a fourth positional would read as an afterthought.
    """

    collection: str
    key: str
    update: dict
    require: object = None

    def as_call(self) -> tuple:
        """`(collection, key, update)` + `{"require": …}` — the driver's two args."""
        return (self.collection, self.key, self.update), {"require": self.require}


def head_write(observation) -> GuardedWrite:
    """The `custom_state` head for one event — `$max` on the order, guarded payload.

    `seq` and :data:`HEAD_ORDER_FIELD` both advance by `$max`, which is
    idempotent and needs no filter of its own; the head's *payload* rides
    `require={"order": {"$lt": newOrder}}` so a late old write never clobbers a
    fresher head (R-52, and `mongo_store.guarded_update` names this call in its
    own docstring). `custom_state` fences `$set` off `seq` for the same reason,
    so the two operators cannot be confused, and nothing here `$set`s the order
    either.

    The guard is the composite and not `{"seq": {"$lt": newSeq}}` because the
    head is ONE space and `seq` is per-stream — see :func:`head_order` and the
    module docstring. Within a stream the two are the same predicate, so R-52's
    literal semantics are unchanged; across streams the composite is what makes
    "applying the events of one `(refId, stateKey)` in any order lands on one
    document" true rather than merely intended. `$max` on `seq` still stores the
    highest `seq` seen, and it is always the winning event's own: the order's
    primary component IS that padded `seq`.

    The payload's key set is **fixed**, which is the other half of the same
    rule. A `$set` only overwrites the keys it carries, and GD-26 leaves no
    field-removing operator to drop the rest, so a field one event has and
    another does not (`sessionKey` on a control line, `ts` on a line that
    carried one) would otherwise survive on the head after a stronger event
    replaced everything around it — arrival order deciding a field again, one
    layer down. So everything that varies between the events of one head lives
    inside :data:`HEAD_EVENT_FIELD`, a sub-document `$set` replaces **wholesale**
    (mongod and `mongo_store.apply_update` agree on that).

    The head's clock lives there too, and deliberately not as a top-level `$max`
    beside `seq`: a guarded update applies **nothing** when its guard loses, so
    such a `$max` would only ever see the events that won — and an event with a
    higher `seq` but an earlier `ts` (clocks and line numbers do not agree across
    files) would then leave a different `ts` depending on which arrived first.
    The head is the projection of ONE event; `event.ts` is that event's own, and
    "when was anything last said about this key" is a question for the log, which
    keeps every line.
    """
    obs = _as_observation(observation, CustomStateObservation)
    validate_kind(obs.kind)
    validate_provenance(obs.provenance, collection="custom_state")
    ref_id = resolve_ref_id(obs.ref, obs.ref_id, kind=obs.kind)
    check_payload(obs.kind, obs.custom)
    if not isinstance(obs.seq, int) or isinstance(obs.seq, bool) or obs.seq < 0:
        raise CustomStateError(f"a head needs the event's seq, got {obs.seq!r}")
    order = head_order(obs.stream, obs.seq)
    key = head_id(ref_id, obs.state_key)
    payload = {
        "refId": ref_id,
        "kind": obs.kind,
        "provenance": obs.provenance,
        "author": validate_author(obs.author),
        "stateKey": _state_key(obs.state_key),
        "stream": obs.stream,
        "derived": True,
        "fromSeq": obs.seq,
        "tombstone": bool(obs.tombstone),
        "data": {"custom": dict(obs.custom or {})},
        # Always present, always replaced as a whole: the winning event's own
        # attribution and clock, and nothing a losing event left behind.
        HEAD_EVENT_FIELD: _head_event(obs),
    }
    prepared, _report = ms.prepare_document("custom_state", dict(payload, _id=key))
    prepared.pop("_id", None)
    update = ms.merge_ops(ms.op_max({"seq": obs.seq, HEAD_ORDER_FIELD: order}),
                          ms.op_set(prepared), collection="custom_state")
    return GuardedWrite("custom_state", key, update,
                        {HEAD_ORDER_FIELD: {"$lt": order}})


def _head_event(obs) -> dict:
    """The per-event half of a head: what THIS event said, and nothing else.

    Kept as one sub-document because that is the only shape `$set` can replace
    completely — see :func:`head_write`. Empty is a legal, meaningful value: an
    event that stated no session, no inferred address and no timestamp.
    """
    event = {}
    if obs.session_key is not None:
        event["sessionKey"] = obs.session_key
    if obs.session_key_source is not None:
        event["sessionKeySource"] = obs.session_key_source
    if obs.attempt_source is not None:
        # "Is this card's address inferred?" — answerable without a join back to
        # the event that produced it.
        event["attemptSource"] = obs.attempt_source
    if obs.ts is not None:
        event.update(_ts_pair(obs.ts, obs.ts_raw))
    elif obs.ts_raw:
        event["tsRaw"] = obs.ts_raw
    return event


def _slot_advance(key, resolution, fields, *, at=None, add_to_set=None) -> GuardedWrite:
    """One rank advance. The transition's own timestamp is `resolvedTs`.

    Not `ts`: `firstSeenTs`/`lastSeenTs` are the *evidence* clock (`$min`/`$max`
    from the mapper) and a bare `ts` beside them reads as the document's, when
    it is only ever "when this transition was concluded". Naming it for what it
    is also keeps a later sweep from looking like it rewrote the bind's time.

    `resolution`/`resolutionRank` and the conclusion fields (`agentId`,
    `boundBy`, `orphanReason`) are `$set` — legitimately, because the
    `{"resolutionRank": {"$lt": rank}}` guard is what makes them monotone: the
    write only fires when it strictly advances. :data:`ADVANCE_MAX_FIELDS` are
    the exception, and they leave through `$max`: the mapper already accumulates
    `taskId`/`runNode` that way, and a `$set` beside a `$max` on one field means
    whichever arrived last decides the stored value — GD-25's named failure.
    """
    rank = RESOLUTION_RANK[resolution]
    payload = {"resolution": resolution, "resolutionRank": rank}
    maxes = {}
    for name, value in fields.items():
        if value is None:
            continue
        (maxes if name in ADVANCE_MAX_FIELDS else payload)[name] = value
    if at is not None:
        stamps = _ts_pair(at, None)
        payload["resolvedTs"] = stamps["ts"]
    ops = [ms.op_set(payload)]
    if maxes:
        ops.append(ms.op_max(maxes))
    if add_to_set:
        ops.append(ms.op_add_to_set(add_to_set))
    return GuardedWrite("slots", key, ms.merge_ops(*ops, collection="slots"),
                        {"resolutionRank": {"$lt": rank}})


def bind_write(key, agent_id, *, by, at=None, task_id=None, run_node=None) -> GuardedWrite:
    """`pending → bound`, agentId included — the whole-bind form.

    The guard is `resolutionRank < 2`, so a bind cannot demote a `conflict` and
    a late `pending` observation cannot undo it.

    Legal against a server only when the caller already knows no *other* slot
    holds ``agent_id`` — which :class:`SlotTable` does know, because it carries
    the unique index in memory. The driver path does not, and it uses
    :func:`claim_op` + :func:`bind_advance_write` instead; see :func:`bind_slot`
    for why that split exists.
    """
    _check_channel(by)
    if not isinstance(agent_id, str) or not agent_id:
        raise SlotError("a bind needs an agentId")
    return _slot_advance(key, "bound",
                         {"agentId": agent_id, "boundBy": by, "taskId": task_id,
                          "runNode": run_node}, at=at)


def claim_op(key, agent_id) -> tuple:
    """The one write that touches the unique sparse `agentId` index, alone.

    A `(collection, _id, update)` triple, shaped for `mongo_store.bulk_upsert` —
    which is the **only** write API in this codebase that answers a duplicate key
    with `tolerated_dups` instead of an exception (GD-29 requires the count, and
    `classify_write_errors` produces it). `guarded_update` translates every
    driver error on its guarded path into `MongoUnavailable`, carving out only
    the `$jsonSchema` refusal, so a collision there arrives as "the database is
    gone" and would trip GD-30's breaker on perfectly healthy traffic *and*
    propagate out of the tick.

    R-53 is explicit that a colliding bind must never raise — the data that
    causes it is agent-authored, and an ingest loop a subagent can kill is not
    an ingest loop — so the index-touching write is isolated here and driven
    through the tolerant API. Nothing else in this module `$set`s `agentId`.
    """
    if not isinstance(agent_id, str) or not agent_id:
        raise SlotError("a claim needs an agentId")
    return ("slots", key, ms.op_set({"agentId": agent_id}))


def bind_advance_write(key, *, by, at=None, task_id=None, run_node=None) -> GuardedWrite:
    """The rank advance to `bound`, **without** `agentId` (see :func:`claim_op`)."""
    _check_channel(by)
    return _slot_advance(key, "bound",
                         {"boundBy": by, "taskId": task_id, "runNode": run_node}, at=at)


def _check_channel(by):
    if by not in BIND_CHANNELS:
        raise SlotError(f"boundBy must be one of {list(BIND_CHANNELS)}, got {by!r}")


def orphan_write(key, *, reason, at=None) -> GuardedWrite:
    """`pending → orphaned`: a normal outcome, recorded with its reason (D13).

    GD-7 permits a node that never gets a marker, so a slot that never binds is
    not a defect to hide — an orphaned stop intent is a stop that went nowhere,
    and the UI is required to say so.
    """
    return _slot_advance(key, "orphaned", {"orphanReason": reason}, at=at)


def _conflict_ids(agent_ids, conflict_with=None) -> dict:
    ids = [i for i in dict.fromkeys(agent_ids or ()) if i]
    if len(ids) < 1:
        raise SlotError("a conflict records the agentIds that collided")
    fields = {"conflictAgentIds": {"$each": ids}}
    others = [i for i in dict.fromkeys(conflict_with or ()) if i]
    if others:
        fields["conflictWith"] = {"$each": others}
    return fields


def conflict_write(key, agent_ids, *, at=None, conflict_with=None) -> GuardedWrite:
    """`→ conflict`, recording **both** agentIds. Outranks every other state.

    Written when one slot is claimed by two agentIds, or when a bind loses the
    unique sparse index to another slot already holding that id. Deliberately
    does **not** write `agentId`: the losing bind must not touch the index
    again, and a slot with two claims has no single answer to "which agent is
    this".

    The ids ride `$addToSet`, not `$set`, so a slot that collides a third time
    accumulates the third id instead of replacing the pair that is already
    there — and so this write stays order-independent under GD-25 like every
    other operator in the module. The rank guard still governs the *state*, and
    :func:`conflict_evidence_op` is the unguarded half that records ids when the
    guard has nothing left to advance.
    """
    return _slot_advance(key, "conflict", {}, at=at,
                         add_to_set=_conflict_ids(agent_ids, conflict_with))


def conflict_evidence_op(key, agent_ids, *, conflict_with=None) -> tuple:
    """The colliding ids as an **unguarded** `$addToSet` triple.

    `conflict_write`'s guard is `resolutionRank < 3`, so a slot that is already
    `conflict` refuses a further advance — correctly, since there is no higher
    state — but that would also discard the *third* colliding id, leaving a
    document that says "two agents collided here" when three did. The state
    machine stays guarded and monotone; the evidence is append-only and needs no
    guard at all, which is exactly the split GD-25 draws.
    """
    return ("slots", key,
            ms.merge_ops(ms.op_add_to_set(_conflict_ids(agent_ids, conflict_with)),
                         collection="slots"))


# --- the in-memory twin of a guarded write --------------------------------


def _guard_value(doc, field):
    node = doc
    for part in field.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def _guard_matches(doc, require) -> bool:
    """Mongo's matcher over :data:`mongo_store.GUARD_OPS`, missing-field semantics
    included: `{seq: {$lt: n}}` does **not** match a document with no `seq`
    (R-52's head guard must not fire on a fresh head), while `{a: {$ne: 1}}`
    does. Identical in behaviour to `mirror._matches`, which the tests assert
    rather than assume — two copies of one matcher that drift are two matchers.
    """
    for field_name, condition in (require or {}).items():
        value, present = _guard_value(doc, field_name)
        if isinstance(condition, dict):
            for operator, operand in condition.items():
                if operator == "$exists":
                    if bool(present) != bool(operand):
                        return False
                    continue
                if operator == "$ne":
                    if present and value == operand:
                        return False
                    continue
                if operator in ("$in", "$nin"):
                    inside = present and any(value == item for item in operand)
                    if (operator == "$in") != inside:
                        return False
                    continue
                if not present:
                    return False
                try:
                    ok = {"$lt": value < operand, "$lte": value <= operand,
                          "$gt": value > operand, "$gte": value >= operand}[operator]
                except (TypeError, KeyError):
                    return False
                if not ok:
                    return False
        elif not present or value != condition:
            return False
    return True


def apply_guarded(state, write) -> bool:
    """Apply a :class:`GuardedWrite` to `{collection: {_id: doc}}`. Returns
    ``acquired``.

    The in-memory twin of `mongo_store.guarded_update`, down to its two refusals:
    a guard that matches nothing is **not** an insert, and an update that cannot
    stand alone as a document is a payload write rather than a create. Pure and
    stdlib-only, which is what makes a full rebuild — and every test in this
    sub-plan — run with no database (GD-21/GD-22).
    """
    spec = ms.spec_for(write.collection)
    ms.check_id(write.collection, write.key)
    ms.validate_update(write.update, write.collection, _id=write.key)
    bucket = state.setdefault(write.collection, {})
    current = bucket.get(write.key)
    if current is not None:
        if not _guard_matches(current, write.require):
            return False
        bucket[write.key] = ms.apply_update(current, write.update, _id=write.key,
                                            collection=write.collection)
        return True
    candidate = ms.apply_update(None, write.update, _id=write.key,
                                collection=write.collection)
    if [name for name in spec.required if name not in candidate]:
        return False
    ms.validate_document(write.collection, candidate)
    bucket[write.key] = candidate
    return True


def replay(observations, state=None) -> dict:
    """Replay the WAL into both collections. Order-independent (GD-25).

    R-52's acceptance in one call: the events collection gets one insert-only
    document per line, the head gets the highest-ordered event per
    `(refId, stateKey)` — highest `seq`, ties between streams broken by
    :func:`head_order` and not by arrival — and a Mongo wipe followed by this is
    byte-identical to the mirror that was lost, whatever order the observations
    are handed over in. That is what makes Mongo optional even for the one
    dataset files cannot rebuild.
    """
    state = {} if state is None else state
    items = [_as_observation(o, CustomStateObservation) for o in observations]
    ms.apply_operations(state, [op for obs in items for op in map_custom_state(obs)])
    for obs in items:
        apply_guarded(state, head_write(obs))
    return state


def rebuild_heads(state, *, drop=True) -> dict:
    """Rebuild `custom_state` from `custom_state_events` alone (CUSTOMSTATE-14).

    The recovery procedure the derived head exists to make possible: drop it,
    replay the log, and get the same documents back. Nothing else writes the
    head, so nothing else can make this untrue.

    The iteration below is in dict insertion order — i.e. arrival order — and it
    does not matter: :func:`head_write`'s guard is a total order over
    `(seq, stream)`, so every enumeration of the same log lands on the same head
    (`tests/test_custom_state.py` shuffles the log and asserts it).
    """
    if drop:
        state["custom_state"] = {}
    for doc in list((state.get("custom_state_events") or {}).values()):
        apply_guarded(state, head_write(CustomStateObservation.from_document(doc)))
    return state


# --- slots: the state machine (R-53) --------------------------------------


def resolution_of(doc, *, now=None, ttl=PENDING_TTL_SECONDS, terminal=False) -> str:
    """The resolution a slot document *should* carry, from its evidence.

    Read-time and pure, so the same rule answers a query and drives the guarded
    write that materializes it. The order is the severity order: a conflict is
    never softened, a bind is never undone, and a pending slot becomes
    `orphaned` once its run has ended or it has been waiting past the TTL — the
    two ways a marker can turn out never to be coming (CUSTOMSTATE-9).

    A conflict is read from the **evidence**, not only from the stored word.
    Both conflict paths (:meth:`SlotTable._conflict`, :func:`_drive_conflict`)
    write the unguarded evidence first and the guarded state second, so a
    process that dies between them — or a guard with nothing left to advance —
    leaves a document carrying `conflictAgentIds`/`conflictWith` and
    `resolution: "pending"`. Nothing but a conflict path ever writes those two
    fields (the mapper accumulates `agentIds`, which is a different field for
    exactly this reason), so their presence IS the collision: reading such a
    slot as `pending` would let the sweep promote a contested stop to
    `orphaned` — "went nowhere" said about a stop that went to two agents, which
    is the one shape D13 forbids.
    """
    if not isinstance(doc, dict):
        raise SlotError("a slot document must be a dict")
    if (doc.get("resolution") == "conflict" or doc.get("conflictAgentIds")
            or doc.get("conflictWith")):
        return "conflict"
    if doc.get("agentId"):
        return "bound"
    if terminal:
        return "orphaned"
    since = doc.get("pendingSince")
    if now is not None and isinstance(since, datetime.datetime):
        if (now - since).total_seconds() > ttl:
            return "orphaned"
    return "pending"


@dataclass
class BindResult:
    """What a bind attempt did: the outcome, the write, and why.

    ``acquired`` is the honest half: `resolution` is the state the slot is *in*,
    which is not the same as what this call *wrote*. A conflict on a slot that
    is already `conflict`, or a bind on a slot already at the same rank, comes
    back with `resolution` set and ``acquired`` false — the guard did not fire,
    and a caller that counts writes must not count that one.
    """

    resolution: str
    write: object = None
    agent_ids: tuple = ()
    conflict_with: object = None
    duplicate_key: bool = False
    acquired: bool = False


@dataclass
class SlotTable:
    """The name↔agentId hop, in memory, with the unique index it has in Mongo.

    Holding the unique sparse `agentId` constraint here — not only in the
    database — is the point of the class: the model and the server must agree
    about *which* bind is the loser, and a test that can only discover the
    disagreement against a live mongod is a test that does not run on most
    machines (GD-21). Every collision is counted and none of them raises: the
    ingest process staying alive is R-53's stated requirement, because the data
    that causes a collision is agent-authored.
    """

    ttl: int = PENDING_TTL_SECONDS
    state: dict = field(default_factory=dict)
    counters: dict = field(default_factory=lambda: {
        "observed": 0, "bound": 0, "conflict": 0, "orphaned": 0,
        "duplicate_key": 0, "rejected": 0})
    _by_agent: dict = field(default_factory=dict)

    # -- evidence ----------------------------------------------------------

    def observe(self, observation) -> list:
        """Apply one observation's monotone evidence; returns the ops applied."""
        obs = _as_observation(observation, SlotObservation)
        ops = map_slot(obs)
        ms.apply_operations(self.state, ops)
        # A second agentId for one slot lands in the `agentIds` evidence array
        # and is settled by `bind`, never here: an observation must not be able
        # to change a slot's conclusion, only to add to what is known about it.
        self.counters["observed"] += 1
        return ops

    def slot(self, key):
        return (self.state.get("slots") or {}).get(key)

    def resolution(self, key, *, now=None, terminal=False):
        doc = self.slot(key)
        return None if doc is None else resolution_of(doc, now=now, ttl=self.ttl,
                                                      terminal=terminal)

    # -- the conclusion ----------------------------------------------------

    def bind(self, key, agent_id, *, by, at=None, task_id=None, run_node=None) -> BindResult:
        """Bind ``agent_id`` to a slot, or record the conflict. Never raises.

        Two collisions exist and both end as one `conflict` document carrying
        both ids:

        * **within a slot** — the slot already names a different agentId (a
          copy-pasted marker; "exactly one agentId per (name, attempt)" is
          enforced by nothing but an LLM's care);
        * **across slots** — this agentId is already bound elsewhere, which in
          Mongo is a `DuplicateKeyError` on the unique sparse index. Caught,
          counted (`duplicate_key`), and the tailer lives.
        """
        doc = self.slot(key)
        if doc is None:
            self.counters["rejected"] += 1
            return BindResult("unknown")
        current = doc.get("agentId")
        holder = self._by_agent.get(agent_id)
        if current and current != agent_id:
            return self._conflict(key, [current, agent_id], at=at)
        if holder is not None and holder != key:
            self.counters["duplicate_key"] += 1
            return self._conflict(key, [agent_id], at=at, conflict_with=[holder],
                                  duplicate_key=True)
        write = bind_write(key, agent_id, by=by, at=at, task_id=task_id, run_node=run_node)
        acquired = apply_guarded(self.state, write)
        if (self.slot(key) or {}).get("agentId") == agent_id:
            # Registered whenever this slot really holds the id, guard or no
            # guard. The rank guard legitimately does not fire for an idempotent
            # re-bind, and `state` is a public field a caller may seed from
            # stored documents — so tying registration to the guard would leave
            # the in-memory unique index blind to a holder the document already
            # names, and the NEXT slot claiming that agentId would be bound
            # rather than answered with the conflict this class exists to catch.
            self._by_agent[agent_id] = key
        if not acquired:
            # The guard lost: the slot is already at this rank or higher (a
            # conflict, or an identical bind). Not an error, and not a retry.
            return BindResult(self.resolution(key), write=write, agent_ids=(agent_id,))
        self.counters["bound"] += 1
        return BindResult("bound", write=write, agent_ids=(agent_id,), acquired=True)

    def _conflict(self, key, agent_ids, *, at=None, conflict_with=None,
                  duplicate_key=False) -> BindResult:
        # Evidence first and unguarded, so a third colliding id is recorded even
        # though the state machine has nowhere left to advance (n4).
        ms.apply_operations(self.state, [conflict_evidence_op(
            key, agent_ids, conflict_with=conflict_with)])
        write = conflict_write(key, agent_ids, at=at, conflict_with=conflict_with)
        applied = apply_guarded(self.state, write)
        if applied:
            self.counters["conflict"] += 1
        return BindResult("conflict", write=write, agent_ids=tuple(agent_ids),
                          conflict_with=conflict_with, duplicate_key=duplicate_key,
                          acquired=applied)

    def sweep(self, *, now, terminal=(), reason=None) -> list:
        """Orphan every pending slot past the TTL or in a terminated run.

        ``terminal`` is a set of slot keys whose run has reached a terminal
        event — the run-scoped half of CUSTOMSTATE-9's rule, which the caller
        knows and this table does not.
        """
        terminal = set(terminal or ())
        writes = []
        for key, doc in sorted((self.state.get("slots") or {}).items()):
            is_terminal = key in terminal
            if resolution_of(doc, now=now, ttl=self.ttl,
                             terminal=is_terminal) != "orphaned":
                continue
            write = orphan_write(
                key, at=now,
                reason=reason or ("run terminal" if is_terminal else "no marker within TTL"))
            if apply_guarded(self.state, write):
                self.counters["orphaned"] += 1
                writes.append(write)
        return writes


# --- the driver path (a live database, and still no exceptions) -----------


def bind_slot(db, key, agent_id, *, by, at=None, task_id=None, run_node=None,
              counters=None) -> BindResult:
    """:meth:`SlotTable.bind` against a real database. Never raises on a collision.

    Three steps, in this order, and the order is the contract:

    1. **read** the slot. A slot nobody observed is refused rather than created —
       a bind is evidence about a spawn Touch saw, not a spawn it invents — and a
       slot already in `conflict` is left alone.
    2. **claim** `agentId` through :func:`claim_op` and `bulk_upsert`, the write
       API that answers a duplicate key with `tolerated_dups`. A collision here
       means another slot holds this agentId, and it becomes a `conflict`
       document naming both the id and the slot that holds it.
    3. **advance** the resolution with :func:`bind_advance_write`, guarded on the
       rank so it cannot demote a conflict.

    The claim precedes the advance so that a crash between them leaves a document
    whose stored `resolution` merely *lags* :func:`resolution_of` (which reads
    the agentId), and the next reconcile materializes it. The other order would
    leave a document claiming `bound` with no agentId — a state the read-time
    function would contradict, which is the one shape D13 forbids.

    ``db`` is any object with mapping access to collections; the driver is never
    imported here (GD-21). Every outcome is counted rather than raised, because
    every one of them is normal traffic.
    """
    counters = counters if counters is not None else {}

    def bump(name):
        counters[name] = counters.get(name, 0) + 1

    current = db["slots"].find_one({"_id": key})
    if current is None:
        bump("rejected")
        return BindResult("unknown")
    if current.get("resolution") == "conflict":
        # Nothing left to advance (rank 3 is the top), but the claim is still
        # evidence: recording it keeps the driver's document identical to
        # `SlotTable`'s, and keeps "three agents collided here" from rendering as
        # two. `acquired` stays false, because no transition was written (n4).
        collection, _key, evidence = conflict_evidence_op(key, [agent_id])
        ms.bulk_upsert(db, collection, [(key, evidence)])
        bump("conflict_evidence")
        refreshed = db["slots"].find_one({"_id": key}) or current
        return BindResult("conflict",
                          agent_ids=tuple(refreshed.get("conflictAgentIds") or ()))
    existing = current.get("agentId")
    if existing and existing != agent_id:
        bump("conflict")
        return _drive_conflict(db, key, [existing, agent_id], at=at)
    if not existing:
        collection, _key, update = claim_op(key, agent_id)
        result = ms.bulk_upsert(db, collection, [(key, update)])
        if result["errors"]:
            bump("write_errors")
            return BindResult("pending")
        if result["tolerated_dups"]:
            bump("duplicate_key")
            bump("conflict")
            holder = db["slots"].find_one({"agentId": agent_id})
            holder_id = holder.get("_id") if holder else None
            return _drive_conflict(db, key, [agent_id], at=at,
                                   conflict_with=[holder_id] if holder_id else None,
                                   duplicate_key=True)
    write = bind_advance_write(key, by=by, at=at, task_id=task_id, run_node=run_node)
    outcome = ms.guarded_update(db, "slots", write.key, write.update,
                                require=write.require)
    if outcome["acquired"]:
        bump("bound")
    return BindResult("bound", write=write, agent_ids=(agent_id,),
                      acquired=bool(outcome["acquired"]))


def _drive_conflict(db, key, agent_ids, *, at=None, conflict_with=None,
                    duplicate_key=False) -> BindResult:
    # The unguarded evidence first, for the same reason `SlotTable._conflict`
    # writes it: the rank guard cannot advance a slot that is already
    # `conflict`, and a third colliding id must still be recorded.
    collection, _key, evidence = conflict_evidence_op(key, agent_ids,
                                                      conflict_with=conflict_with)
    ms.bulk_upsert(db, collection, [(key, evidence)])
    write = conflict_write(key, agent_ids, at=at, conflict_with=conflict_with)
    outcome = ms.guarded_update(db, "slots", write.key, write.update,
                                require=write.require)
    return BindResult("conflict", write=write, agent_ids=tuple(agent_ids),
                      conflict_with=conflict_with, duplicate_key=duplicate_key,
                      acquired=bool(outcome["acquired"]))


# --- sources (the rebuild/backfill seam) ----------------------------------
#
# `mirror.iter_sources`' contract: `source(path=None) -> Iterable[observation]`,
# where `path=None` means "every file this kind owns" (`--rebuild`) and a
# concrete path means "just this one file" (`--backfill`, which walks
# `~/.claude/projects/**`). No custom-state file lives under that root — the WAL
# is `.touch/`'s, ledgers are the task folders' and control files are wherever
# `TOUCH_CONTROL_PATHS` says — so both sources answer `()` for any path they do
# not own, decided from the path alone, and contribute nothing to a backfill.


def _configured_paths(env_name, env=None) -> list:
    environ = os.environ if env is None else env
    raw = environ.get(env_name) or ""
    return [os.path.abspath(part) for part in raw.split(os.pathsep) if part.strip()]


def control_paths(env=None) -> list:
    """`[(path, "env")]` from `TOUCH_CONTROL_PATHS` — SD-8's configured list.

    Empty is a legitimate answer, and it is the answer today: R-20 has not
    landed, the skill file and the base plan disagree about where the control
    file lives, and this module states no third location (CUSTOMSTATE-11). Every
    document ingested from here carries `pathSource`, so a later relocation is a
    config change rather than a re-ingest.
    """
    return [(path, "env") for path in _configured_paths(CONTROL_PATHS_ENV, env)]


def ledger_paths(root=None, env=None) -> list:
    """Every `state/spawn-ledger.jsonl` under the orchestrator root, sorted.

    `TOUCH_LEDGER_PATHS` overrides the walk entirely. Unlike control files, the
    ledger's location is undisputed (touch-orchestrate SKILL.md), so discovery
    is legitimate here — it is reading a configured project root, not
    auto-discovery (D6/SESSIONJSONL-3).
    """
    configured = _configured_paths(LEDGER_PATHS_ENV, env)
    if configured:
        return [path for path in configured if os.path.isfile(path)]
    base = legacy.orchestrator_root(root, env)
    out = []
    for folder in legacy.discover_tasks(base, env=env):
        path = os.path.join(folder.path, STATE_DIR, LEDGER_FILE)
        if os.path.isfile(path):
            out.append(path)
    return sorted(out)


def is_ledger_path(path) -> bool:
    """Does this path name a spawn ledger? From the path alone."""
    path = os.fspath(path)
    if os.path.basename(path) != LEDGER_FILE:
        return False
    return os.path.basename(os.path.dirname(path)) == STATE_DIR


def _read_lines(path, counters=None) -> list:
    """`[(lineNo, text)]`, 1-based, blank lines skipped but still numbered.

    The line number IS the `seq`, so a skipped blank line must not renumber its
    successors: the `_id` is positional and these files are append-only. A file
    that cannot be opened is `unreadable` in ``counters`` rather than an
    exception — a foreign file is agent-written, and an ingest tick a subagent
    can kill by `chmod` is not an ingest tick.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = [(number, line.strip())
                     for number, line in enumerate(handle, start=1) if line.strip()]
    except OSError:
        if counters is not None:
            counters["unreadable"] = counters.get("unreadable", 0) + 1
        return []
    if counters is not None:
        counters["read"] = counters.get("read", 0) + len(lines)
    return lines


def _bump(counters, name):
    if counters is not None:
        counters[name] = counters.get(name, 0) + 1


def _ledger_scope(path) -> str:
    """The task folder a ledger belongs to — `<task>/state/spawn-ledger.jsonl`.

    Disambiguated by the realpath digest (:func:`_path_scope`) because two
    orchestrator roots may hold task folders of the same name, and one stream id
    for two files silently collapses line 1 of the second into line 1 of the
    first (`custom_state_events` is `$setOnInsert`-only).
    """
    folder = os.path.dirname(os.path.dirname(os.path.abspath(os.fspath(path))))
    return _path_scope(path, os.path.basename(folder) or "unknown")


def read_ledger_file(path, *, session_key=None, counters=None) -> list:
    """Parse one spawn ledger into :class:`SlotObservation`s.

    The ledger line as amended by R-53 carries `root` and `sessionKey`; a
    pre-amendment line carries neither, and then the session is derived from the
    containing path and recorded as `sessionKeySource: "path"` (CUSTOMSTATE-10).
    A line with no derivable session is **skipped, not guessed**: a slot `_id`
    without a real sessionKey is the cross-session collision the key exists to
    prevent.

    ``counters`` (:func:`new_counters`) records every drop by reason, so "this
    ledger produced nothing" is distinguishable from "this ledger is not there".
    """
    counters = new_counters() if counters is None else counters
    stream = ledger_stream(_ledger_scope(path))
    from_path = session_key or session_key_from_path(path)
    out = []
    for number, text in _read_lines(path, counters):
        try:
            payload = json.loads(text)
        except ValueError:
            _bump(counters, "skipped_malformed")
            continue
        if not isinstance(payload, dict):
            _bump(counters, "skipped_malformed")
            continue
        stated = payload.get("sessionKey")
        key = stated if isinstance(stated, str) and _SESSION_KEY_RE.match(stated) else None
        source = "ledger" if key else ("path" if from_path else None)
        key = key or from_path
        attempt = payload.get("attempt")
        if not key or not source or not payload.get("name") or not payload.get("root"):
            _bump(counters, "skipped_unaddressable")
            continue
        if not isinstance(attempt, int) or isinstance(attempt, bool):
            # GD-9's rule again: an attempt Touch cannot read is not evidence,
            # and defaulting it would address a real, stale slot (R-53).
            _bump(counters, "skipped_malformed")
            continue
        _bump(counters, "parsed")
        out.append(SlotObservation(
            session_key=key, root=payload["root"], name=payload["name"],
            attempt=attempt, parent=payload.get("parent"), role=payload.get("role"),
            task_id=payload.get("taskId"), ts=payload.get("ts"),
            ts_raw=payload.get("ts") if isinstance(payload.get("ts"), str) else None,
            session_key_source=source, stream=stream, seq=number,
            source_path=os.fspath(path), line=text[:512]))
    return out


def read_control_file(path, path_source="env", *, session_key=None, slots=None,
                      counters=None) -> list:
    """Parse one control file into `control_intent` / `control_ack` observations.

    The two shapes touch-orchestrate writes: `{"action":"stop","name":…}` and
    `{"ack":"stop","name":…,"taskId":…,"result":…}`. Both address the agent by
    **name and nothing else**, which is why the refId is a slot key and why
    `slots` exists at all: ``slots`` (a :class:`SlotIndex`, or anything it can
    index — slot keys, :class:`SlotObservation`s, stored `slots` documents)
    performs the name→slot hop, exactly as the skill file's own control loop
    resolves a name through the spawn ledger.

    A line that states its whole address (`sessionKey`/path + `root` +
    `attempt`) is taken at its word and needs no index. Everything else is
    resolved, or skipped and counted — never defaulted: `attempt` in particular
    is the difference between stopping the agent that is running and attaching a
    stop card to a slot that ended two attempts ago (touch-orchestrate §4).

    `pathSource` records where the path came from, and this function never
    states a default path (SD-8/CUSTOMSTATE-11).
    """
    counters = new_counters() if counters is None else counters
    index = slots if isinstance(slots, SlotIndex) else SlotIndex(slots or ())
    from_path = session_key or session_key_from_path(path)
    stream = control_stream(_path_scope(path, from_path or os.path.basename(
        os.path.dirname(os.path.abspath(os.fspath(path)))) or "unknown"))
    out = []
    for number, text in _read_lines(path, counters):
        try:
            payload = json.loads(text)
        except ValueError:
            _bump(counters, "skipped_malformed")
            continue
        if not isinstance(payload, dict):
            _bump(counters, "skipped_malformed")
            continue
        kind = "control_ack" if payload.get("ack") else (
            "control_intent" if payload.get("action") else None)
        name = payload.get("name")
        if not kind or not isinstance(name, str) or not name:
            _bump(counters, "skipped_malformed")
            continue
        stated = payload.get("sessionKey")
        if not (isinstance(stated, str) and _SESSION_KEY_RE.match(stated)):
            stated = None
        session = stated or from_path
        root = payload.get("root") if isinstance(payload.get("root"), str) else None
        attempt = payload.get("attempt")
        if attempt is not None and (
                not isinstance(attempt, int) or isinstance(attempt, bool)):
            _bump(counters, "skipped_malformed")
            continue

        if session and root and attempt is not None:
            try:
                ref_id = slot_id(session, root, name, attempt)
            except SlotError:
                _bump(counters, "skipped_unaddressable")
                continue
            attempt_source = "stated"
            key_source = "ledger" if stated else "path"
        else:
            address, reason = index.resolve(name, session_key=session, root=root,
                                            attempt=attempt)
            if address is None:
                # An intent Touch cannot address is not an intent it may act on,
                # and a fabricated address is worse than none (D13).
                _bump(counters, "skipped_ambiguous" if reason == "ambiguous"
                      else "skipped_unaddressable")
                continue
            ref_id = address.key
            session = address.session_key
            attempt_source = address.attempt_source
            key_source = "ledger" if stated else ("path" if from_path else "slots")
        _bump(counters, "parsed")
        out.append(CustomStateObservation(
            kind=kind, stream=stream, seq=number,
            state_key=f"{kind}:{payload.get('action') or payload.get('ack')}",
            ref_id=ref_id, custom=payload, provenance="asserted",
            ts=payload.get("ts"),
            ts_raw=payload.get("ts") if isinstance(payload.get("ts"), str) else None,
            session_key=session, session_key_source=key_source,
            task_id=payload.get("taskId"), source_path=os.fspath(path),
            path_source=path_source, attempt_source=attempt_source))
    return out


#: One-entry memo for :func:`slot_index`, keyed by the ledger set's
#: `(path, st_mtime_ns, st_size)` signature — cheap to compute (one `stat` per
#: ledger) and impossible to serve stale, since any append changes both numbers.
_SLOT_INDEX_MEMO = {"signature": None, "index": None}


def _ledger_signature(paths) -> tuple:
    out = []
    for path in paths:
        try:
            info = os.stat(path)
        except OSError:
            out.append((path, None, None))
        else:
            out.append((path, info.st_mtime_ns, info.st_size))
    return tuple(out)


def slot_index(root=None, env=None, *, memo=True) -> SlotIndex:
    """Every slot the ledger channel has observed, indexed by name (M1's hop).

    Built from the same source `MIRROR_SOURCES["slot"]` drives, so a control
    line resolves against exactly the slots the mirror knows about. Built only
    when a control path is configured — with SD-8's list empty there is nothing
    to address, and walking the orchestrator root for nobody is work the
    liveness path must not pay for (GD-22).

    Memoised on the ledger set's stat signature, because the caller that asks
    most often is a per-file backfill: rebuilding the index for each control
    file it is handed makes an O(n) walk O(n·ledgers). ``memo=False`` forces a
    rebuild for a caller that would rather pay than reason about the cache.
    """
    paths = ledger_paths(root, env)
    signature = (os.path.abspath(os.fspath(root)) if root is not None else None,
                 _ledger_signature(paths))
    if memo and _SLOT_INDEX_MEMO["index"] is not None \
            and _SLOT_INDEX_MEMO["signature"] == signature:
        return _SLOT_INDEX_MEMO["index"]
    index = SlotIndex()
    for ledger in paths:
        for obs in read_ledger_file(ledger):
            index.add(obs)
    if memo:
        _SLOT_INDEX_MEMO["signature"] = signature
        _SLOT_INDEX_MEMO["index"] = index
    return index


def iter_custom_state_observations(path=None, *, root=None, env=None, counters=None,
                                   slots=None):
    """`MIRROR_SOURCES["customState"]` — the `.touch/` WAL plus control files.

    ``slots`` lets a backfill driver build the name→slot index once and hand it
    to every file it walks; left `None`, :func:`slot_index` supplies it (and
    memoises it, so the per-file call is not O(ledgers) each time).
    """
    if path is not None:
        path = os.path.abspath(os.fspath(path))
        for configured, source in control_paths(env):
            if os.path.abspath(configured) == path:
                index = slots if slots is not None else slot_index(root, env)
                return read_control_file(path, source, slots=index, counters=counters)
        wal = store_mod.Store(root)
        if os.path.abspath(wal.stream_path(WAL_STREAM)) == path:
            return _wal_observations(wal)
        return []
    out = list(_wal_observations(store_mod.Store(root)))
    configured_paths = control_paths(env)
    if slots is not None:
        index = slots
    else:
        index = slot_index(root, env) if configured_paths else SlotIndex()
    for configured, source in configured_paths:
        out.extend(read_control_file(configured, source, slots=index, counters=counters))
    return out


def _wal_observations(store):
    try:
        records = store.read_all(WAL_STREAM)
    except OSError:
        return []
    return [CustomStateObservation.from_record(record, stream=WAL_STREAM)
            for record in records]


def iter_slot_observations(path=None, *, root=None, env=None, counters=None):
    """`MIRROR_SOURCES["slot"]` — every spawn ledger, or just the named one."""
    if path is not None:
        if not is_ledger_path(path):
            return []
        return read_ledger_file(path, counters=counters)
    out = []
    for ledger in ledger_paths(root, env):
        out.extend(read_ledger_file(ledger, counters=counters))
    return out


#: The rebuild/backfill seam, declared beside the mappers (`mirror.iter_sources`).
MIRROR_SOURCES = {
    "customState": iter_custom_state_observations,
    "slot": iter_slot_observations,
}
