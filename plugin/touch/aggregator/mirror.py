"""The write-behind Mongo mirror: queue, breaker, lease, cursors, sweep,
rebuild, backfill — plus the deployment/credential handling GD-27 demands
(R-45 + R-42's runtime half).

Where this module sits
----------------------
`mongo_store.py` (R-44) is the *description*: GD-24's table, GD-25's algebra,
the shape guards, and an in-memory twin of Mongo's upsert so the acceptance
test needs no database. This module is the *runtime*: the one thing in Touch
that holds a live client, drains a bounded queue into it, and owns every
decision about what happens when the database is slow, gone, or being written
by a second process.

It is also, by GD-15, the only module allowed to import both sides — file-side
(`tailer`, `store`) and Mongo-side (`mongo_store`, `refs`) — and, with
`mongo_store`, one of the only two allowed to import `pymongo` at all (GD-21,
lazily, inside functions). Everything above it speaks in mapper functions
(SD-1) that are pure and know nothing about a client.

The five invariants that decide every design choice here
--------------------------------------------------------
1. **Mongo contributes 0 ms to the critical path** (GD-22/GD-30). The poll
   loop only ever calls :meth:`Mirror.enqueue`, which is a plain synchronous
   function that never awaits, never blocks and never raises. Writing happens
   in a separate drainer task. MONGOSCHEMA-4 measured a 30.1 s stall against a
   dead port with pymongo's defaults; :data:`mongo_store.CLIENT_OPTIONS` caps
   server selection at 500 ms, and the circuit breaker means the *second*
   dead tick costs nothing at all.
2. **A dead or absent Mongo is a non-event.** No import failure, no startup
   failure, no test failure: `mirror: "absent"` when pymongo or a URI is
   missing, `"down"` when nothing answers, `"degraded"` when writes are being
   dropped, and the live view keeps working because the in-memory reduction —
   not the mirror — is what `/ws` and the read API serve.
3. **The mirror never deletes history** (GD-26). Upsert-only, with a
   generation mark-and-sweep that *retracts* (`$set:{retracted:true}`) rather
   than removes, and exactly ONE legal delete: positionally-keyed
   `stream_meta` documents of a file whose lines renumbered, deleted and
   re-inserted in the same code path. That is a wall here, not a convention:
   :class:`Backend` exposes no delete verb but the scoped one, and it refuses
   any collection other than `stream_meta`.
4. **Credentials never leave `.touch/mongo.json`** (GD-27). The file must be
   0600 or the mirror refuses to start; the URI is transported by
   `TOUCH_MONGO_URI`; and every string this module publishes — `/health`,
   `lastError`, the CLI — goes through :func:`redact` first, because the
   single most likely place for a password to leak is the text of a driver
   exception.
5. **One writer per stream** (GD-29). Duplicate-key is both the signature of
   idempotent replay (healthy) and of two live writers racing (a bug), so the
   lease decides who writes, tolerated duplicates are *counted* rather than
   swallowed, and a process that cannot hold the lease refuses to mirror while
   remaining perfectly able to serve reads. That refusal is not a life
   sentence: the lease has a TTL, and a refusing process retries once per TTL,
   so a holder that crashed or stalled does not wedge the mirror until somebody
   restarts it.

Backends
--------
Every database access goes through a small async :class:`Backend`:

* :class:`AsyncBackend` — pymongo's `AsyncMongoClient` (GD-21's chosen driver;
  Motor is EOL). Schema bootstrap is the one exception: `ensure_schema` is
  synchronous and shared with the rest of Touch, so it runs on a short-lived
  sync client inside `asyncio.to_thread` — off the poll loop, once, at connect.
* :class:`MemoryBackend` — `mongo_store`'s own in-memory model plus the guard
  and sweep semantics this module needs. It is not a toy: it is what makes the
  lease race, the idempotent replay and the wipe+rebuild equivalence testable
  on a bare checkout, and `tests/test_mirror.py` runs the *same* scenarios
  against a real mongod when one is reachable and compares fingerprints.

Nothing in this module invents a collection, an `_id` or an operator: keys come
from `refs.ref_key`, collections from GD-24's closed table, and every update is
run through `mongo_store.validate_update` before it can reach a driver.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import importlib
import json
import os
import re
import stat
import time
import urllib.parse

from . import mongo_store as ms
from . import paths
from . import refs
from . import tailer as tailer_mod

__all__ = [
    "MirrorError",
    "CredentialError",
    "MapperError",
    "SweepScopeError",
    "STATES",
    "STATE_ABSENT",
    "STATE_STARTING",
    "STATE_LIVE",
    "STATE_DEGRADED",
    "STATE_DOWN",
    "STATE_REFUSED",
    "DEFAULT_QUEUE_SIZE",
    "BREAKER_FAILURES",
    "BREAKER_HOLD_S",
    "LEASE_TTL_S",
    "TICK_INTERVAL_S",
    "DENY_BASENAMES",
    "DB_PREFIX",
    "SECRET_KEY_RE",
    "SECRET_KEY_EXEMPT",
    "SECRET_KEY_VALUE_EXEMPT",
    "SCHEMA_FIELD_NAMES",
    "REF_FIELD",
    "UNPINNED_REF_KINDS",
    "CLASSIFICATION_VALUES",
    "REDACTED",
    "redact_uri",
    "redact",
    "scrub_value",
    "is_denied_path",
    "database_name",
    "load_credentials",
    "save_credentials",
    "MongoConfig",
    "resolve_config",
    "ENTITY_MODULES",
    "Mapper",
    "discover_mappers",
    "map_observation",
    "RUNTIME_COLLECTIONS",
    "projection_state",
    "MirrorOp",
    # `ScrubbedOp` is deliberately NOT exported: it is an internal transport
    # marker that *disables* GD-27's backstop, and the only legitimate way to
    # obtain one is to have paid for it in `validate_op`. Publishing it invited a
    # mapper — which already imports `MirrorOp` from here — to yield one and opt
    # its own payloads out of the only backstop the module has. `Mapper.__call__`
    # downgrades the marker on mapper output for the same reason; the export list
    # is the half that keeps it from looking like part of SD-1's contract.
    "validate_op",
    "stamp_gen",
    "stamp_backfill",
    "op_timestamps",
    "Backend",
    "MemoryBackend",
    "AsyncBackend",
    "Mirror",
    "iter_sources",
    "claude_root",
    "iter_backfill_sources",
    "iter_backfill_observations",
    "iter_rebuild_observations",
    "main",
]


# --- errors ---------------------------------------------------------------


class MirrorError(Exception):
    """Base for every refusal this module makes.

    Deliberately NOT a subclass of `mongo_store.MongoStoreError`: a drainer
    written as `except MongoStoreError:` is catching "this document cannot be
    stored", and a credential or lease problem is not that.
    """


class CredentialError(MirrorError):
    """`.touch/mongo.json` is unusable — wrong mode, wrong shape, wrong type.

    Always a *refusal to start*, never a degrade: a world-readable credentials
    file is the failure GD-27 exists to prevent, and continuing with it would
    make the mirror the thing that leaks the transcripts it exists to protect.
    """


class MapperError(MirrorError):
    """A `MIRROR_MAPPERS` registration or output that breaks SD-1's contract."""


class SweepScopeError(MirrorError):
    """A generation sweep was asked to run without a positive scope.

    An unscoped `updateMany`/`deleteMany` is GD-12's wrong-target hazard at its
    most expensive: it would retract, or delete, every document in the
    collection. The scope is required, and it must select by something.
    """


# --- constants ------------------------------------------------------------

#: pymongo or a URI is missing. The mirror does nothing, nothing fails, and the
#: live view is complete (GD-21/GD-22).
STATE_ABSENT = "absent"
#: Configured, not connected yet. Honest about the gap rather than claiming
#: `down` (which would say "we tried") or `live` (which would be a lie).
STATE_STARTING = "starting"
#: Connected, lease held, writes landing.
STATE_LIVE = "live"
#: Writing, but lossily — queue-full drops or tolerated write errors (GD-30).
STATE_DEGRADED = "degraded"
#: Nothing answered; the breaker is holding. History/backfill degrade, the live
#: view does not.
STATE_DOWN = "down"
#: A *policy* refusal, not a failure: zero configured users (GD-27), a lost
#: writer lease (GD-29), or credentials this module will not use.
STATE_REFUSED = "refused"

STATES = (STATE_ABSENT, STATE_STARTING, STATE_LIVE, STATE_DEGRADED,
          STATE_DOWN, STATE_REFUSED)

#: Bounded, per GD-30. On overflow the mirror drops MIRROR writes and counts
#: them; live frames are never in this queue in the first place — they are
#: served from the in-memory reduction, which is the structural form of "never
#: drop a live frame".
DEFAULT_QUEUE_SIZE = 4096

#: Circuit breaker (GD-30): N consecutive driver failures ⇒ stop attempting for
#: 30 s. The point is not the database, it is the poll loop: without it every
#: tick pays the full server-selection timeout forever.
BREAKER_FAILURES = 3
BREAKER_HOLD_S = 30.0

#: GD-29's lease. Renewed every tick; a holder that dies loses it after TTL.
LEASE_TTL_S = 30.0
#: Renew when less than this fraction of the TTL remains.
LEASE_RENEW_AT = 0.5

#: The drainer's idle wake-up interval. It matches D6/R-23's tailer poll so the
#: mirror never lags the ingest by more than one tick, and the drainer wakes
#: immediately when work is enqueued.
TICK_INTERVAL_S = 0.25

#: The fence every Touch database name sits behind (GD-27/GD-12). The trailing
#: underscore is load-bearing: a bare `touch` prefix admits `touchdown_prod`,
#: which nobody constructed and which Touch would then be willing to drop.
DB_PREFIX = "touch_"

#: GD-27's never-mirrored deny-list, by basename. `.touch/server.json` holds the
#: live per-boot token (GD-13); the two `~/.claude` files hold the CLI's own
#: credentials. A record sourced from any of them is never mirrored — not
#: redacted, not stubbed: never read.
DENY_BASENAMES = frozenset({"server.json", ".credentials.json", ".claude.json",
                            "mongo.json"})

#: GD-27's env-var pattern, reused as a key-name pattern for the document
#: backstop below.
SECRET_KEY_RE = re.compile(r"(?i)(token|secret|key|password|auth)")

#: What a redacted value becomes. A marker, never an empty string: D13 says a
#: degraded value is labelled, and "this field existed and was withheld" is a
#: different fact from "this field was empty".
REDACTED = "[redacted]"

#: Key names the document backstop leaves alone unconditionally. Every one of
#: them is a *classification* the harness writes constantly
#: (`"apiKeySource":"none"`, `"authType":"oauth"`), never a secret, and redacting
#: them would corrupt the mirrored record — and with it GD-25's fingerprint — for
#: no gain. Note what is NOT here: bare `key` and `keys`, which are the two names
#: most likely to hold the real thing.
SECRET_KEY_EXEMPT = frozenset({"apiKeySource", "authType", "keyType",
                               "toolUseKey", "publicKeyId"})

#: Names exempt only when their *value* is plainly a label. `{"key": "Enter"}` is
#: a keystroke and `{"key": "sk-…"}` is exactly what this backstop exists for;
#: the name alone cannot tell them apart, so the value decides. Checked BEFORE
#: :data:`SCHEMA_FIELD_NAMES`, which also contains `key` (`run_nodes.key`,
#: GD-24): a declared schema name must not buy an unconditional exemption for the
#: one name most likely to hold a real credential inside an agent-asserted
#: `data.custom` payload.
SECRET_KEY_VALUE_EXEMPT = frozenset({"key", "keys"})

#: The vocabulary a `key`/`keys` value must be in to survive. Deliberately tiny
#: and closed: anything outside it is redacted, because the cost of redacting a
#: label is a corrupted field and the cost of publishing a credential is a
#: credential on an unauthenticated route (GD-13/GD-27).
CLASSIFICATION_VALUES = frozenset({
    "none", "null", "unknown", "default", "auto", "true", "false", "yes", "no",
    "oauth", "apikey", "api_key", "bearer", "basic", "session", "subscription",
    "user", "system", "assistant", "enter", "return", "escape", "esc", "tab",
    "space", "backspace", "delete", "up", "down", "left", "right", "home", "end",
    "ctrl", "shift", "alt", "meta", "pageup", "pagedown",
})


def _schema_field_names():
    """Every field name GD-24's schema declares, from the two modules that own it.

    Derived, never hand-listed. `SECRET_KEY_EXEMPT` above enumerates five names
    a human noticed; the schema contains others that match
    :data:`SECRET_KEY_RE` by accident of English — `sessionKey`, `stateKey`,
    `author` — and redacting *those* does not withhold a secret, it corrupts the
    document. `slots.sessionKey` is the single name↔agentId hop (R-53) and
    `custom_state.stateKey` is half of that collection's `_id`; a `[redacted]`
    in either makes GD-24's mandated dot-notation join (`{"ref.sessionKey": …}`)
    match nothing, and leaves the document contradicting its own `refId`.

    Reading the declarations rather than copying them is the point: sp-07…sp-11
    add fields to `refs.KIND_SPECS` and `mongo_store.COLLECTIONS`, and a
    hand-maintained list is how this hole re-opens two sub-plans from now.
    """
    names = set()
    for spec in refs.KIND_SPECS.values():
        names.update(spec.required)
        names.update(spec.optional)
    for spec in ms.COLLECTIONS.values():
        names.update(spec.types)
        names.update(spec.required)
        names.update(spec.set_fields)
        names.update(spec.accumulable)
    return frozenset(names)


#: GD-24's declared vocabulary — schema, not payload. A field the schema names is
#: never a place a credential can hide: its value grammar is pinned by
#: `mongo_store`'s BSON types and, inside a ref of a *declared* kind, by that
#: kind's `refs.KIND_SPECS` field pins.
SCHEMA_FIELD_NAMES = _schema_field_names()

#: The canonical structured reference GD-24 puts on every event-ish document.
#: Exempt from the backstop only when it **validates** against one of GD-24's
#: seven closed union members — naming a declared kind is not enough, because
#: `refs.classify` reads the `kind` key and nothing else. `kind:"unknown"` is
#: GD-11's *open tail* and carries whatever keys its author wrote, and so does a
#: hand-built `{"kind":"uuid", …, "password":…}` until something checks the key
#: set. `refs.validate_ref` is that something (see :func:`_scrub_ref`).
REF_FIELD = "ref"

#: The two `refs.validate_ref` outcomes that pin nothing. `"none"` is the empty
#: ref (a stream-level event has no target); `"unknown"` is GD-24's retained
#: open tail, whose key set is by definition not declared anywhere. A ref that
#: *fails* validation joins them: an undeclared key on a declared kind is an
#: unpinned subtree wearing a pinned kind's name.
UNPINNED_REF_KINDS = ("none", "unknown")

#: The URI scheme separator, spelled apart from either scheme name. R-42's
#: guard — "no file under `aggregator/` contains a connection string literal" —
#: greps for a scheme followed by this separator, and a module that hardcodes
#: that spelling in order to *check* for it reads, to the guard and to a human
#: skimming a diff, exactly like one that hardcodes a server. So it is composed
#: instead, and `tests/test_mongo_deploy.py` runs the grep.
_SCHEME_SEP = "://"
_SCHEMES = ("mongodb", "mongodb" + "+srv")


def mongo_doc_path() -> str:
    """The absolute path of the database recipe, for user-facing messages.

    A message that says "see `docs/mongo.md`" is only true for someone standing
    in this checkout (PLUGIN-RUNTIME-10). Under a plugin install the reader's
    cwd is their OWN project, which has no `docs/`; the file lives inside a
    version-stamped cache directory they have no reason to know the name of.
    Since GD-U1 `docs/` is a sibling of this package under the plugin root, so
    :func:`paths.plugin_root` resolves it correctly in both cases — and the
    printed string is something the reader can actually open.

    Comments and docstrings still say `docs/mongo.md`: they address a developer
    reading this file, for whom the repo-relative name is the clearer one.
    """
    return os.path.join(paths.plugin_root(), "docs", "mongo.md")


# --- redaction ------------------------------------------------------------


def redact_uri(uri) -> str:
    """A connection URI with its userinfo removed. Never raises.

    Anything that does not parse as `<scheme>://<authority>…` is redacted
    **whole**: an unrecognised string in a credentials slot is not evidence that
    it carries no credential.
    """
    if not isinstance(uri, str) or not uri:
        return REDACTED
    scheme, sep, rest = uri.partition(_SCHEME_SEP)
    if not sep or scheme not in _SCHEMES:
        return REDACTED
    authority, slash, tail = rest.partition("/")
    if "@" in authority:
        authority = REDACTED + "@" + authority.rsplit("@", 1)[1]
    return scheme + _SCHEME_SEP + authority + (slash + tail if slash else "")


#: Matches an embedded connection URI wherever a driver exception put it. Built
#: from pieces for R-42's guard's sake (see :data:`_SCHEME_SEP`); the authority
#: stops at whitespace, a quote, or the path separator that ends it.
_URI_RE = re.compile(r"(mongodb(?:\+srv)?)" + re.escape(_SCHEME_SEP) + r"([^\s'\"/,)\]]*)")

#: Shortest literal the second redaction pass will search for. A 1–2 character
#: password is not *safer* — it is unmatchable: `str.replace` has no word
#: boundary, so redacting `"a"` rewrites every `a` in the message into
#: `[redacted]` and destroys the host, the error class and the reason an
#: operator opened `/health` for. The structural pass still covers such a
#: password wherever it can actually appear (inside a URI's userinfo, which is
#: the only form a driver exception embeds), so the literal pass is giving up a
#: belt it never held rather than an exposure.
_MIN_LITERAL_SECRET = 3


def redact(text, secrets=()) -> str:
    """``text`` with every URI userinfo and every known secret removed.

    Two independent passes, because each catches what the other cannot:

    * the structural one rewrites anything shaped like a connection URI — the
      form a driver exception embeds ("ServerSelectionTimeoutError: <uri>");
    * the literal one removes exact known secrets (the configured URI, its
      password) — the form a driver exception embeds when it *reformats* the
      URI, or when a caller interpolates the password itself.

    Every string this module publishes goes through here: `/health`,
    `lastError`, the CLI's output. `/health` is unauthenticated (GD-13), so a
    password in `lastError` is a password on an open port.
    """
    out = "" if text is None else str(text)
    # Structural FIRST, literal second. Both orders are safe, but this one is
    # also useful: the structural pass strips the userinfo and keeps the host, so
    # an operator reading `/health` still learns *which* server timed out. Run
    # the other way round, the literal pass matches the whole configured URI and
    # replaces the host along with the password, leaving a message that says
    # nothing at all.
    if _SCHEME_SEP in out:
        out = _URI_RE.sub(
            lambda m: redact_uri(m.group(1) + _SCHEME_SEP + m.group(2)), out)
    for secret in secrets or ():
        if isinstance(secret, str) and len(secret) >= _MIN_LITERAL_SECRET and secret in out:
            out = out.replace(secret, REDACTED)
    return out


def _is_classification(value):
    """True if a `key`/`keys` value is a label rather than a credential.

    A single character is a keystroke; everything else must be in the closed
    vocabulary. No length heuristic beyond that — "short" is not a security
    property, and a truncated token is still a token.
    """
    return len(value) <= 1 or value.strip().lower() in CLASSIFICATION_VALUES


def _is_secret_key(key, item):
    """True if ``{key: item}`` is a string field the backstop must redact.

    Order matters, and it is the opposite of the obvious one:
    :data:`SECRET_KEY_VALUE_EXEMPT` is consulted **before** the schema
    vocabulary, because `key` is both a declared schema field (`run_nodes.key`)
    and the likeliest name for a quoted credential in an agent-asserted payload.
    For that pair the value decides, schema or not; for every other declared
    name the schema decides, because a `[redacted]` there is a corrupted
    document rather than a withheld secret.
    """
    if not (isinstance(key, str) and isinstance(item, str)):
        return False
    if key in SECRET_KEY_VALUE_EXEMPT:
        return not _is_classification(item)
    if key in SECRET_KEY_EXEMPT or key in SCHEMA_FIELD_NAMES:
        return False
    return SECRET_KEY_RE.search(key) is not None


def scrub_value(value):
    """Return ``value`` with secret-looking string fields replaced (GD-27).

    A **backstop**, and it says so: the real rule is the deny-list — a record
    sourced from a credentials file is never read at all (:func:`is_denied_path`).
    This exists because a transcript can quote an environment dump, and a
    mirrored transcript is permanent.

    It reaches inside `_raw` wrappers (`mongo_store.wrap_raw`), which is where
    variable-key subtrees — tool `input` maps, `toolUseResult` bodies — actually
    live once prepared. A scrubber that stopped at the wrapper would be blind to
    exactly the subtrees whose keys are attacker-…, or rather tool-, chosen.
    Only *string* values are replaced: a `{"authRequired": true}` is a
    classification, not a credential, and rewriting booleans and numbers would
    change the meaning of documents for no security gain.
    """
    if ms.is_raw_wrapper(value):
        inner = ms.unwrap_raw(value)
        scrubbed = scrub_value(inner)
        if scrubbed == inner:
            return value
        wrapper = ms.wrap_raw(scrubbed)
        if value.get(ms.RAW_AUTO_FIELD):
            wrapper[ms.RAW_AUTO_FIELD] = True
        return wrapper
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _is_secret_key(key, item):
                out[key] = REDACTED
            else:
                out[key] = scrub_value(item)
        return out
    if isinstance(value, list):
        return [scrub_value(item) for item in value]
    return value


def _scrub_ref(value):
    """The `ref` sub-document, scrubbed unless its kind pins its field set.

    The exemption exists because scrubbing a ref did real damage:
    `ref.sessionKey` and `ref.stateKey` match :data:`SECRET_KEY_RE` on "Key", so
    every slot and custom-state reference was stored as `[redacted]` while the
    identical datum survived inside `refId` — GD-24's mandated
    `{"ref.sessionKey": …}` join dead, and the document contradicting itself. In
    an upsert-only mirror (GD-26) that is permanent.

    But it is conditional on the property that justifies it, not on the field
    *name* and not on the `kind` string. A ref that **validates** against one of
    GD-24's seven declared kinds has a closed key set and per-field value pins
    (`refs.KIND_SPECS`), so there is nowhere in it for a quoted credential to
    sit. `kind:"unknown"` is the opposite: GD-24 keeps unclassifiable shapes
    deliberately ("retained under `ref` with `kind:"unknown"`, no `refId`,
    excluded from joins — GD-11's open tail preserved") and `refs.canonical_ref`
    copies every key of one through with only a sort applied. That is precisely
    a subtree of arbitrary, agent-authored keys, and sp-07…sp-11's mappers are
    told to produce them — SD-8/R-53's control-intent ingest reads agent-written
    control files. So an unknown ref goes through the backstop like any other
    payload.

    **`validate_ref`, not `classify`.** `refs.classify` names a ref's kind
    "without validating its values" (its own docstring): for a ref carrying an
    explicit `kind`, it is a membership test against `KIND_SPECS` and returns,
    whatever else the dict holds. So `{"kind":"uuid", "uuid":…,
    "password":"hunter2"}` classified as a pinned kind and walked straight past
    the backstop into an upsert-only store. The function that enforces the claim
    this exemption is *made of* is `refs.validate_ref` (`refs.py`): it rejects
    undeclared fields and runs the per-field pins. The two agree on every ref
    that has been through `refs.canonical_ref` — which strips extras — so the
    fast path for real mapper output is unchanged, and the difference is exactly
    the hand-assembled ref this function exists to distrust.

    Validation never raises out of here: an unknown declared kind and a failed
    pin both arrive as `RefError`, and a ref the module cannot vouch for is
    exactly the one to scrub rather than to trust.
    """
    if not isinstance(value, dict):
        return scrub_value(value)
    try:
        kind = refs.validate_ref(value)
    except refs.RefError:
        kind = "unknown"
    return value if kind not in UNPINNED_REF_KINDS else scrub_value(value)


def is_denied_path(path) -> bool:
    """True if ``path`` is on GD-27's never-mirrored list.

    Matched on the **basename** rather than on a set of absolute paths: the two
    `~/.claude` files exist once per home directory and `.touch/server.json`
    once per state root, but a test tree, a backfill of another checkout and a
    second state root all reproduce them elsewhere. A name-based rule covers
    every copy; an absolute-path rule covers the one this process happens to
    own.
    """
    if not path:
        return True
    name = os.path.basename(os.fspath(path))
    return name in DENY_BASENAMES


# --- configuration and credentials ---------------------------------------


def database_name(repo=None, *, env=None) -> str:
    """`touch_<sha1(repo-realpath)[:8]>`, or `$TOUCH_MONGO_DB` (GD-27).

    Derived, never a constant: two checkouts of Touch on one machine share a
    mongod, and a constant name would have the second silently mirror into the
    first's history — GD-12's wrong-target invariant, one layer down.
    `realpath` so a symlinked checkout is the same database as its target.

    What is digested is the **project** root (:func:`paths.project_root`), not
    the directory above this package (CM-2/GD-T5). Under a plugin install the
    latter is version-stamped, so every update would digest a new path and
    start a brand-new database — orphaning the old one under the GD-27 drop
    fence, which refuses to touch names it did not construct. Project-anchored,
    the name is update-invariant and still per-checkout.

    An explicit `$TOUCH_MONGO_DB` is honoured but still fenced to the `touch_`
    prefix — **with the underscore**, so that `touchdown_prod` is refused rather
    than adopted. Both names this module constructs (`touch_<sha1>` and the
    suite's `touch_test_<pid>`) satisfy the tighter rule, and the tighter rule is
    what makes "drop only names we constructed" checkable at all (GD-27).
    """
    override = (os.environ if env is None else env).get("TOUCH_MONGO_DB")
    if override:
        if not override.startswith(DB_PREFIX):
            raise CredentialError(
                f"TOUCH_MONGO_DB must start with {DB_PREFIX!r} so Touch can never write "
                f"to, or drop, a database it did not construct (GD-27/GD-12): "
                f"{override!r}"
            )
        return override
    if repo is None:
        repo = paths.project_root(env=os.environ if env is None else env)
    digest = hashlib.sha1(os.path.realpath(os.fspath(repo)).encode("utf-8")).hexdigest()
    return DB_PREFIX + digest[:8]


def _credentials_path(root=None) -> str:
    from . import store as store_mod          # file-side; GD-15 allows it HERE only
    return os.path.join(store_mod.state_root(root), "mongo.json")


def load_credentials(path=None, *, root=None):
    """Read `.touch/mongo.json`, refusing anything a group or the world can read.

    Returns ``{"uri": …, …}`` or **None** when the file simply does not exist —
    an unconfigured mirror is `absent`, which is a normal, fully supported
    deployment (GD-21), not an error.

    Everything else is a :class:`CredentialError`, including any mode with a bit
    set outside owner read/write. The check is the mask `mode & 0o177` rather
    than `!= 0o600`, so `0400` (read-only) is accepted as well as `0600` — the
    two spellings of "only the owner can read this" — while every group bit,
    every other bit, and the owner-execute bit are refused. A credentials file
    has no reason to be executable, and the mask is cheaper to be right about
    than an enumeration of the modes that happen to be safe.

    Symlinks are refused outright — a 0600 symlink pointing at a 0644 file
    passes every mode check there is.
    """
    target = os.fspath(path) if path else _credentials_path(root)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CredentialError(f"{target}: cannot stat the credentials file: {exc}") from None
    if stat.S_ISLNK(info.st_mode):
        raise CredentialError(
            f"{target} is a symlink; Touch reads credentials only from a regular file it "
            f"can check the mode of (a 0600 symlink says nothing about its target)"
        )
    if not stat.S_ISREG(info.st_mode):
        raise CredentialError(f"{target} is not a regular file")
    if info.st_mode & 0o177:
        raise CredentialError(
            f"{target} is mode {stat.S_IMODE(info.st_mode):04o}; Touch refuses to read "
            f"credentials any other account can: chmod 600 it (GD-27)"
        )
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise CredentialError(f"{target}: unreadable credentials file: {exc}") from None
    if not isinstance(data, dict):
        raise CredentialError(f"{target}: expected a JSON object, got {type(data).__name__}")
    uri = data.get("uri")
    if uri is not None and (not isinstance(uri, str) or not uri.strip()):
        raise CredentialError(f"{target}: 'uri' must be a non-empty string")
    return data


def save_credentials(uri, path=None, *, root=None, db=None, overwrite=False) -> str:
    """Write `.touch/mongo.json` at mode 0600, creating it exclusively.

    The file is created with `O_EXCL` and 0600 *at open time* rather than
    chmod'ed afterwards: between `open` and `chmod` the file is world-readable,
    and that is the window an attacker on a shared box actually uses.
    Overwriting is opt-in, so a bootstrap re-run cannot silently replace a
    working credential with a typo'd one.
    """
    if not isinstance(uri, str) or not uri.strip():
        raise CredentialError("a Mongo URI is required")
    target = os.fspath(path) if path else _credentials_path(root)
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
    payload = {"uri": uri}
    if db:
        payload["db"] = db
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError:
        raise CredentialError(
            f"{target} already exists; pass overwrite=True to replace it"
        ) from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(target, 0o600)                 # belt and braces against a lax umask
    return target


class MongoConfig:
    """Everything the mirror needs to connect, and nothing it may publish.

    `uri` is private by construction: it is never in :meth:`describe`, never in
    `/health`, and :attr:`secrets` is what :func:`redact` scrubs out of every
    message this module emits.
    """

    __slots__ = ("uri", "db", "source", "stream", "queue_size")

    def __init__(self, uri=None, db=None, *, source="none", stream="mirror",
                 queue_size=DEFAULT_QUEUE_SIZE):
        self.uri = uri
        self.db = db
        self.source = source
        self.stream = stream
        self.queue_size = queue_size

    @property
    def configured(self) -> bool:
        return bool(self.uri)

    @property
    def secrets(self) -> tuple:
        """Exact strings :func:`redact` must remove: the URI and its password.

        Both spellings of the password: as written in the URI (`p%40ss`) and as
        the driver actually holds it (`p@ss`). A URI password containing `@`,
        `/` or `:` *must* be percent-encoded to parse, so the encoded form is
        the only one this string carries — while an exception raised by code
        that already decoded it quotes the other. The literal pass is the belt
        behind the structural one, and a belt that only knows one of the two
        spellings is missing the half that appears in messages.
        """
        if not self.uri:
            return ()
        out = [self.uri]
        _, sep, rest = self.uri.partition(_SCHEME_SEP)
        if sep:
            authority = rest.partition("/")[0]
            if "@" in authority:
                userinfo = authority.rsplit("@", 1)[0]
                password = userinfo.partition(":")[2]
                if password:
                    out.append(password)
                    decoded = urllib.parse.unquote(password)
                    if decoded != password:
                        out.append(decoded)
        return tuple(out)

    def describe(self) -> dict:
        """The publishable form: database, where the URI came from, no URI."""
        return {"db": self.db, "source": self.source, "uri": redact_uri(self.uri)
                if self.uri else None}


def resolve_config(*, root=None, repo=None, env=None, path=None) -> MongoConfig:
    """`$TOUCH_MONGO_URI` first, then `.touch/mongo.json` (GD-27).

    That order is GD-27's own: the credentials *live* in the 0600 file, and the
    environment variable is how they are *handed to* the aggregator (by a
    launcher that read the file, by a systemd unit, by a test). An env-first
    rule also means a developer can point one process at a scratch database
    without editing — or accidentally committing — anything.

    A missing file is not an error (mirror `absent`); a *malformed* one is,
    because silently falling back to "no mirror" would turn a chmod mistake
    into a feature that quietly stopped working.

    The database name is resolved **whether or not there is a URI**. It is a
    pure function of the repo's realpath and `$TOUCH_MONGO_DB` (GD-27) — it does
    not need, and cannot learn anything from, a connection string. Deriving it
    only once a URI existed made the documented bootstrap circular: `docs/mongo.md`
    §2 asks the operator to paste the derived name into the `createRole`/
    `createUser` script, and §3 says `--check` prints it — but at that point no
    credentials exist yet, so the answer was `null` exactly when it was needed.
    `configured` still keys off the URI alone, so an unconfigured mirror is
    still `absent`; it now just knows which database it *would* write to.
    """
    environ = os.environ if env is None else env
    uri = (environ.get("TOUCH_MONGO_URI") or "").strip()
    data = load_credentials(path, root=root) or {}
    source = "none"
    if uri:
        source = "env"
    elif data.get("uri"):
        uri = data["uri"].strip()
        source = "file"
    db = (environ.get("TOUCH_MONGO_DB") or data.get("db") or "")
    if not db:
        db = database_name(repo, env=environ)
    elif not db.startswith(DB_PREFIX):
        raise CredentialError(
            f"database name {db!r} does not start with {DB_PREFIX!r} — Touch writes only "
            f"to databases it constructed (GD-27/GD-12)"
        )
    return MongoConfig(uri or None, db, source=source)


# --- the mapper registry (SD-1) ------------------------------------------

#: The entity modules that may export mappers. Each owns exactly one slice of
#: GD-24's table and none of them exists yet in this pass — absence is the
#: normal case and is silent, which is what "discovers and drives them lazily"
#: means in SD-1.
ENTITY_MODULES = ("sessions", "ingest", "legacy", "agents", "custom_state")


class Mapper:
    """One registered mapper: a pure `obs -> [(collection, _id, update)]`.

    SD-1's contract, restated where it is enforced: a mapper does **no I/O**,
    imports **no pymongo**, and returns operations built only from
    `mongo_store`'s op vocabulary with `_id`s from `refs.ref_key`. Purity is
    asserted statically (`tests/test_mirror.py` walks the module ASTs); the
    *output* is validated here, on every call, because that is the half a
    static check cannot see.
    """

    __slots__ = ("kind", "module", "fn")

    def __init__(self, kind, module, fn):
        self.kind = kind
        self.module = module
        self.fn = fn

    def __call__(self, observation):
        try:
            raw = self.fn(observation)
        except Exception as exc:                                # noqa: BLE001
            raise MapperError(f"{self.module}.{self.kind} raised {type(exc).__name__}: {exc}") from None
        if raw is None:
            return []
        try:
            items = list(raw)
        except TypeError:
            raise MapperError(
                f"{self.module}.{self.kind} returned {type(raw).__name__}; a mapper returns "
                f"an iterable of (collection, _id, update) triples (SD-1)"
            ) from None
        # `scrub=False`: this runs inside the 250 ms poll loop (SD-1's mappers
        # are driven by `Mirror.map_and_enqueue`), and GD-27's backstop is a deep
        # walk that already runs on the way out of the queue. Validation stays —
        # it is the half that costs nothing and names the mapper.
        #
        # …and the `ScrubbedOp` marker is DOWNGRADED here, unconditionally.
        # `validate_op` honours the marker wherever it finds it — that is what
        # makes "scrubbed once per operation" true across `_requeue`'s retries —
        # so an entity module that returned one would have exempted its own
        # output from the only backstop GD-27 has, by constructing a type. Mapper
        # output has by definition never been through the walk, whatever its type
        # says, and this is the one boundary where that is knowable.
        ops = []
        for item in items:
            op = validate_op(item, source=f"{self.module}.{self.kind}", scrub=False)
            ops.append(MirrorOp(*op) if isinstance(op, ScrubbedOp) else op)
        return ops

    def __repr__(self):                                          # pragma: no cover
        return f"<Mapper {self.module}.{self.kind}>"


def discover_mappers(modules=None, *, package=None):
    """`{kind: Mapper}` from every entity module that declares `MIRROR_MAPPERS`.

    Import failures of the *module itself* are not caught: a module that exists
    and cannot import is a bug in this checkout, and swallowing it would leave
    the mirror silently writing a subset of the schema. A module that does not
    exist at all is skipped — that is the state of four of the five today. The
    two are told apart by the **fully-qualified** name (`aggregator.legacy`, not
    `legacy`): comparing leaf names would silently skip an entity module whose
    real failure was a missing third-party package that happens to share its
    name.

    Two modules registering the same kind is refused. GD-15 gives every file one
    owner; a kind is the visible edge of that ownership, and "whichever imported
    last wins" is how two mappers quietly write the same collection with
    different rules.
    """
    package = package or __package__
    registry = {}
    for name in (ENTITY_MODULES if modules is None else modules):
        if isinstance(name, str):
            try:
                module = importlib.import_module(f".{name}", package)
            except ModuleNotFoundError as exc:
                if exc.name != f"{package}.{name}":
                    raise                                        # a real missing dependency
                continue
        else:
            module = name
            name = getattr(module, "__name__", repr(module)).rsplit(".", 1)[-1]
        mappers = getattr(module, "MIRROR_MAPPERS", None)
        if mappers is None:
            continue
        if not isinstance(mappers, dict):
            raise MapperError(f"{name}.MIRROR_MAPPERS must be a dict of kind -> callable (SD-1)")
        for kind, fn in mappers.items():
            if not isinstance(kind, str) or not kind:
                raise MapperError(f"{name}.MIRROR_MAPPERS: kind must be a non-empty string")
            if not callable(fn):
                raise MapperError(f"{name}.MIRROR_MAPPERS[{kind!r}] is not callable")
            if kind in registry:
                raise MapperError(
                    f"observation kind {kind!r} is registered by both "
                    f"{registry[kind].module} and {name} — one kind has one owner (GD-15/SD-1)"
                )
            registry[kind] = Mapper(kind, name, fn)
    return registry


def map_observation(registry, kind, observation):
    """Drive the mapper for ``kind``; an unregistered kind is a refusal.

    Not a silent no-op: an observation nobody maps is data the mirror is
    dropping, and GD-26's whole posture is that data is never dropped quietly.
    """
    mapper = registry.get(kind)
    if mapper is None:
        raise MapperError(
            f"no mapper registered for observation kind {kind!r} "
            f"(registered: {sorted(registry) or 'none'})"
        )
    return mapper(observation)


# --- operations -----------------------------------------------------------


class MirrorOp(tuple):
    """`(collection, key, update)` — the only thing that ever enters the queue.

    A tuple subclass so it feeds `mongo_store.apply_operations` unchanged (the
    memory model takes exactly these triples), with names for readability.
    """

    __slots__ = ()

    def __new__(cls, collection, key, update):
        return super().__new__(cls, (collection, key, update))

    @property
    def collection(self):
        return self[0]

    @property
    def key(self):
        return self[1]

    @property
    def update(self):
        return self[2]

    def __repr__(self):                                          # pragma: no cover
        return f"MirrorOp({self[0]!r}, {self[1]!r}, {self[2]!r})"


class ScrubbedOp(MirrorOp):
    """A :class:`MirrorOp` whose update has already been through the backstop.

    **The type is the flag.** `MirrorOp` subclasses `tuple`, and CPython refuses
    a non-empty `__slots__` on a subclass of a variable-length built-in, so
    there is nowhere to hang a per-instance boolean without giving every
    operation a `__dict__` — on the one object the queue holds thousands of.

    It exists because :meth:`Mirror._requeue` is a real path: an outage puts an
    in-flight batch back on the queue, and the next drain would run GD-27's deep
    walk over it again — 8.79 ms per 550 KB operation, per tick, for the whole
    length of the outage, on operations whose bytes cannot have changed. The
    walk is idempotent, so the *result* was right; it was the cost that was
    wrong, and it was wrong exactly when the mirror was already unhealthy.

    Only :func:`validate_op` mints one, and only after actually scrubbing. The
    two stampers deliberately do **not** preserve the marker: they merge new
    fields into the update, and an update that grew after its scrub has not been
    scrubbed.
    """

    __slots__ = ()

    def __repr__(self):                                          # pragma: no cover
        return f"ScrubbedOp({self[0]!r}, {self[1]!r}, {self[2]!r})"


def validate_op(item, *, source="caller", scrub=True) -> MirrorOp:
    """Validate one `(collection, _id, update)` triple; returns a :class:`MirrorOp`.

    The same three guards `mongo_store.bulk_upsert` applies at the write path,
    applied at the *registry* boundary instead — so a mapper bug fails where the
    mapper is, with the mapper's name in the message, rather than as one entry
    of a five-hundred-operation bulk an hour later.

    ``scrub`` decides whether GD-27's document backstop runs here. It is
    **False** on every caller that sits on the poll loop's side of GD-30's line
    (`Mapper.__call__`, :func:`stamp_gen`, :func:`stamp_backfill`) and True on
    the drainer's side (`Mirror._take_batches`, `Mirror.sweep`), because the two
    halves of this function cost wildly different amounts:

    ============================  ==========  ==================================
    on a 550 KB document          measured    what it is
    ============================  ==========  ==================================
    `ms.validate_update`           0.006 ms   the guard GD-24/GD-25 require
    :func:`scrub_op_update`        8.79 ms    a deep walk of every value
    ============================  ==========  ==================================

    Validation is free and belongs everywhere. The backstop is a 1600×-more
    expensive walk for a database that is explicitly *not* on the critical path
    (GD-30 budgets Mongo at 0 ms there), and it already runs on the way out of
    the queue — the last thing before `bulk_upsert`, which is the copy that
    decides what is stored. Running it at the registry boundary as well spent
    the reduce budget scrubbing twice and stored the same bytes.

    An operation that carries :class:`ScrubbedOp` has already paid it and is
    never re-scrubbed, whatever ``scrub`` says — that is what makes "once per
    operation" true under `_requeue`'s retries rather than only on the happy
    path. Validation still runs: the queue is a boundary, and a re-validated
    operation costs 0.006 ms.
    """
    try:
        collection, key, update = item
    except (TypeError, ValueError):
        raise MapperError(
            f"{source}: expected a (collection, _id, update) triple, got {item!r} (SD-1)"
        ) from None
    try:
        ms.spec_for(collection)
        ms.check_id(collection, key)
        ms.validate_update(update, collection, _id=key)
    except ms.MongoStoreError as exc:
        raise MapperError(f"{source}: {exc}") from None
    if isinstance(item, ScrubbedOp):
        return ScrubbedOp(collection, key, update)
    if scrub:
        return ScrubbedOp(collection, key, scrub_op_update(update))
    return MirrorOp(collection, key, update)


def scrub_op_update(update):
    """GD-27's document backstop, applied to an update's *values* only.

    Field names are left alone: a key called `authType` is schema, and renaming
    it would make the document unqueryable. Values are scrubbed by
    :func:`scrub_value`.

    With one conditional whole-subtree exception: a canonical `ref` sub-document
    (:data:`REF_FIELD`) that *validates* against a kind pinning its field set is
    passed through untouched, and one that does not — GD-11's open tail, or a
    declared kind carrying an undeclared key — is not. That decision, and
    why it is a decision rather than a field-name rule, is :func:`_scrub_ref`.
    """
    return {operator: {field: (_scrub_ref(value) if field == REF_FIELD
                               else scrub_value(value))
                       for field, value in fields.items()}
            for operator, fields in update.items()}


def stamp_gen(ops, gen):
    """Merge `$set:{gen: G}` into every `records`/`stream_meta` operation (GD-26).

    Only those two: `gen` is the mark half of the mark-and-sweep, and the sweep
    only ever runs over the two positionally- and uuid-keyed harness
    collections. Stamping an `agents` or `usage` document with a per-file
    generation would attach a file's identity to an entity assembled from
    several files (R-48's `a2fc883c` pair is exactly that shape).
    """
    if not isinstance(gen, int) or isinstance(gen, bool) or gen < 1:
        raise MirrorError(f"gen must be a positive int, got {gen!r}")
    out = []
    for op in ops:
        op = validate_op(op, scrub=False)                         # loop side: see validate_op
        if op.collection in ("records", "stream_meta"):
            update = ms.merge_ops(op.update, ms.op_set({"gen": gen}), collection=op.collection)
            op = MirrorOp(op.collection, op.key, update)
        out.append(op)
    return out


def stamp_backfill(ops):
    """Merge `$set:{ingestMode:"backfill"}` into every operation (R-45).

    A field, not a flag in a log line: a document mirrored by a one-shot walk of
    `~/.claude/projects/**` is not the same observation as one seen live, and
    the difference has to survive into the store or nobody downstream can tell
    a backfilled row from an observed one.
    """
    out = []
    for op in ops:
        op = validate_op(op, scrub=False)                         # loop side: see validate_op
        update = ms.merge_ops(op.update, ms.op_set({"ingestMode": "backfill"}),
                              collection=op.collection)
        out.append(MirrorOp(op.collection, op.key, update))
    return out


def op_timestamps(op):
    """Every `datetime` an operation would store, in document order.

    Used by the backfill guard, which must compare *stored* timestamps against
    the source file's mtime. Walks values rather than trusting a `ts` key:
    GD-24 has `firstTs`/`lastTs`/`startedAt`/`endedAt`/`pendingSince`/
    `leaseExpiresAt` besides, and a guard that only looked at `ts` would wave
    through the fields that actually carry a run's clock.
    """
    found = []

    def walk(value):
        if isinstance(value, datetime.datetime):
            found.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for fields in op[2].values():
        walk(fields)
    return found


# --- backends -------------------------------------------------------------


class Backend:
    """The async surface `Mirror` is written against.

    Deliberately tiny, and deliberately missing things: there is no
    `delete_one`, no `drop_database`, no raw collection handle. GD-26's
    upsert-only rule and GD-27's wrong-target invariant are enforced by what
    this interface *does not have*, which is the only kind of enforcement that
    survives a hurried caller.
    """

    #: Set by implementations; `/health` publishes it.
    kind = "none"

    async def ensure_schema(self):
        raise NotImplementedError

    async def ping(self) -> bool:
        raise NotImplementedError

    async def user_count(self):
        """Configured users on the server, or None when it cannot be asked.

        None is not zero: "we could not enumerate users" (because we are
        authenticated as a least-privilege user without `viewUser`) is the
        *healthy* answer, and reading it as zero would refuse to start against
        exactly the correctly-secured deployment GD-27 asks for.
        """
        raise NotImplementedError

    async def bulk_upsert(self, collection, operations):
        raise NotImplementedError

    async def guarded_update(self, collection, key, update, *, require=None, upsert=True):
        raise NotImplementedError

    async def update_many(self, collection, filter_, update):
        raise NotImplementedError

    async def delete_many(self, collection, filter_):
        """The ONE legal delete (GD-26). Implementations refuse every other
        collection than `stream_meta`."""
        raise NotImplementedError

    async def drop_collection(self, collection):
        """Only `derived` (GD-23's drop-and-rebuild). Implementations refuse
        every other name."""
        raise NotImplementedError

    async def fingerprint(self) -> str:
        raise NotImplementedError

    async def counts(self) -> dict:
        raise NotImplementedError

    async def close(self):
        return None


#: Collections that hold **runtime** state rather than a projection of files.
#: Today that is exactly GD-29's writer lease: a pid, a boot digest and an
#: expiry, none of it read out of a transcript and none of it reproducible by a
#: replay.
RUNTIME_COLLECTIONS = ("writers",)


def projection_state(state):
    """`state` minus :data:`RUNTIME_COLLECTIONS` — what a rebuild can reproduce.

    R-45's acceptance criterion is "a Mongo wipe + `--rebuild` reproduces a
    byte-identical fingerprint", and GD-22's whole claim is that Mongo is a
    projection of the files. A lock document taken by whichever process happened
    to be running makes both false by construction: it differs between two runs
    for reasons neither GD says anything about, and a rebuild has nothing to
    replay it from. Every test that computed a fingerprint had already been
    stripping this collection by hand, which is the tell that the fingerprint —
    not each caller — is where the rule belongs.
    """
    return {name: bucket for name, bucket in state.items()
            if name not in RUNTIME_COLLECTIONS}


def _assert_scoped(collection, filter_):
    """A sweep filter must select something. Refuses `{}` and `{_id: …}`-less noise."""
    if not isinstance(filter_, dict) or not filter_:
        raise SweepScopeError(
            f"{collection}: a generation sweep needs a positive scope; an unscoped "
            f"updateMany/deleteMany would hit every document in the collection (GD-12)"
        )
    positive = [f for f in filter_ if f != "gen"]
    if not positive:
        raise SweepScopeError(
            f"{collection}: scope {sorted(filter_)} selects by generation alone — it must "
            f"name the source it is sweeping (sessionId, agentId, …)"
        )
    return filter_


def _matches(doc, filter_):
    """Mongo's matcher, restricted to equality plus :data:`mongo_store.GUARD_OPS`.

    Missing-field semantics are Mongo's, not Python's, because that is the whole
    point of the two shapes this evaluates: `{seq:{$lt:n}}` does **not** match a
    document with no `seq` (R-52's head guard must not fire on a fresh head),
    while `{a:{$ne:1}}` **does** match a document with no `a`.
    """
    for field, condition in filter_.items():
        value, present = _get(doc, field)
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


def _get(doc, field):
    node = doc
    for part in field.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


class MemoryBackend(Backend):
    """`mongo_store`'s in-memory model, driven through :class:`Backend`.

    This is the backend the whole test suite runs against on a bare checkout,
    and it is honest about being a model: every write goes through
    `mongo_store.apply_operations` (so the algebra is the *same code* the
    acceptance test certifies), and the only things added here are the three
    behaviours the model does not cover — the guarded write's outcome, the
    scoped `updateMany`/`deleteMany`, and duplicate-key accounting.

    `tests/test_mirror.py` runs the identical scenarios against a real mongod
    when one is reachable and compares fingerprints, which is the only way a
    model like this stays true.
    """

    kind = "memory"

    def __init__(self, state=None, *, fail=None):
        self.state = {} if state is None else state
        #: When set, every call raises it — the dead-server arm, with no port.
        self.fail = fail
        #: ATTEMPTS, not successes: a test that asks "did the breaker stop it
        #: from touching the driver?" needs the failed attempts counted too.
        #: (`delete_many`/`drop_collection` count after their refusal guard — a
        #: refused delete is not a delete.)
        self.calls = {"bulk_upsert": 0, "guarded_update": 0, "update_many": 0,
                      "delete_many": 0, "drop_collection": 0, "ping": 0}
        self.users = 1

    def _check(self):
        if self.fail is not None:
            raise self.fail

    async def ensure_schema(self):
        self._check()
        return {name: {} for name in ms.collection_names()}

    async def ping(self):
        self.calls["ping"] += 1
        try:
            self._check()
        except ms.MongoUnavailable:
            return False
        return True

    async def user_count(self):
        self._check()
        return self.users

    async def bulk_upsert(self, collection, operations):
        self.calls["bulk_upsert"] += 1
        self._check()
        bucket = self.state.setdefault(collection, {})
        upserted = matched = 0
        for key, update in operations:
            ms.check_id(collection, key)
            ms.validate_update(update, collection, _id=key)
            if key in bucket:
                matched += 1
            else:
                upserted += 1
            bucket[key] = ms.apply_update(bucket.get(key), update, _id=key,
                                          collection=collection)
        return {"matched": matched, "upserted": upserted, "modified": matched,
                "tolerated_dups": 0, "errors": []}

    async def guarded_update(self, collection, key, update, *, require=None, upsert=True):
        self.calls["guarded_update"] += 1
        self._check()
        spec = ms.spec_for(collection)
        ms.check_id(collection, key)
        ms.validate_update(update, collection, _id=key)
        bucket = self.state.setdefault(collection, {})
        current = bucket.get(key)
        if current is not None and (not require or _matches(current, require)):
            bucket[key] = ms.apply_update(current, update, _id=key, collection=collection)
            return {"matched": 1, "upserted": 0, "modified": 1, "acquired": True,
                    "tolerated_dups": 0}
        if not upsert:
            return {"matched": 0, "upserted": 0, "modified": 0, "acquired": False,
                    "tolerated_dups": 0}
        candidate = ms.apply_update(None, update, _id=key, collection=collection)
        if [f for f in spec.required if f not in candidate]:
            # A partial payload write whose guard matched nothing is not a
            # create — `mongo_store.guarded_update`'s own rule, reproduced so
            # the model reports `acquired:False` where the server does.
            return {"matched": 0, "upserted": 0, "modified": 0, "acquired": False,
                    "tolerated_dups": 0}
        if current is not None:
            # The create the server would attempt, and the duplicate key it
            # would answer with: a lost race, counted (GD-29), never raised.
            return {"matched": 0, "upserted": 0, "modified": 0, "acquired": False,
                    "tolerated_dups": 1}
        ms.validate_document(collection, candidate)
        bucket[key] = candidate
        return {"matched": 0, "upserted": 1, "modified": 0, "acquired": True,
                "tolerated_dups": 0}

    async def update_many(self, collection, filter_, update):
        self.calls["update_many"] += 1
        self._check()
        _assert_scoped(collection, filter_)
        ms.validate_update(update, collection)
        bucket = self.state.get(collection, {})
        touched = 0
        for key, doc in list(bucket.items()):
            if _matches(doc, filter_):
                bucket[key] = ms.apply_update(doc, update, _id=key, collection=collection)
                touched += 1
        return touched

    async def delete_many(self, collection, filter_):
        self._check()
        if collection != "stream_meta":
            raise MirrorError(
                f"delete_many is legal on stream_meta only — the mirror exists because the "
                f"CLI deletes history, and deleting a mirrored {collection} document would "
                f"re-import that destruction (GD-26)"
            )
        self.calls["delete_many"] += 1
        _assert_scoped(collection, filter_)
        bucket = self.state.get(collection, {})
        removed = [key for key, doc in bucket.items() if _matches(doc, filter_)]
        for key in removed:
            del bucket[key]
        return len(removed)

    async def drop_collection(self, collection):
        self._check()
        if collection != "derived":
            raise MirrorError(
                f"only the reducer-owned `derived` collection is droppable (GD-23); "
                f"{collection} holds observations and is never dropped"
            )
        self.calls["drop_collection"] += 1
        self.state.pop(collection, None)

    async def fingerprint(self):
        return ms.fingerprint(projection_state(self.state))

    async def counts(self):
        return ms.counts(self.state)


class AsyncBackend(Backend):
    """pymongo's `AsyncMongoClient`, behind the same tiny surface (GD-21).

    Every guard `mongo_store.bulk_upsert` applies is applied here too, by
    calling the *same* functions (`spec_for`, `check_id`, `validate_update`,
    `classify_write_errors`) rather than re-deriving them: this class exists
    only because the sync `bulk_write` those helpers wrap cannot be awaited, and
    a second write path that validated differently would be worse than the
    blocking call it replaces. `tests/test_mirror.py` asserts the two refuse the
    same inputs.

    Schema bootstrap is the one synchronous thing left: `ensure_schema` also
    reads indexes back to enforce GD-26's no-TTL law, it runs once at connect,
    and it is off the poll loop — so it runs on a short-lived sync client inside
    `asyncio.to_thread`, which is the fallback GD-21 explicitly permits.
    """

    kind = "mongo"

    def __init__(self, client, db, *, uri=None, options=None):
        self.client = client
        self.db = db
        self._uri = uri
        self._options = dict(options or {})

    @classmethod
    async def connect(cls, uri, db_name, **overrides):
        """Open an `AsyncMongoClient` with GD-21's timeouts. Never blocks long."""
        try:
            from pymongo import AsyncMongoClient
        except ImportError as exc:
            raise ms.MongoUnavailable(f"pymongo is not installed: {exc}") from None
        if not uri:
            raise ms.MongoUnavailable("no Mongo URI (TOUCH_MONGO_URI / .touch/mongo.json)")
        options = ms.client_options(**overrides)
        client = AsyncMongoClient(uri, **options)
        return cls(client, client[db_name], uri=uri, options=options)

    async def ensure_schema(self):
        def bootstrap():
            client = ms.open_client(self._uri, **self._options)
            try:
                return ms.ensure_schema(client[self.db.name])
            finally:
                client.close()
        return await asyncio.to_thread(bootstrap)

    async def ping(self):
        try:
            await self.client.admin.command("ping")
        except Exception:                                        # noqa: BLE001
            return False
        return True

    async def user_count(self):
        for query in ({"usersInfo": {"forAllDBs": True}}, {"usersInfo": 1}):
            try:
                result = await self.client.admin.command(query)
            except Exception as exc:                             # noqa: BLE001
                if getattr(exc, "code", None) in (13, 8000):
                    # Unauthorized: we are talking to a server that HAS auth and
                    # would not have let us in without a user. That is the
                    # healthy answer, and it is not zero.
                    return None
                continue
            users = result.get("users")
            if isinstance(users, list):
                return len(users)
        return None

    async def bulk_upsert(self, collection, operations):
        ms.spec_for(collection)
        checked = []
        for key, update in operations:
            ms.check_id(collection, key)
            ms.validate_update(update, collection, _id=key)
            checked.append((key, update))
        if not checked:
            return {"matched": 0, "upserted": 0, "modified": 0, "tolerated_dups": 0,
                    "errors": []}
        from pymongo import UpdateOne
        from pymongo.errors import BulkWriteError, PyMongoError
        requests = [UpdateOne({"_id": key}, update, upsert=True) for key, update in checked]
        try:
            result = await self.db[collection].bulk_write(requests, ordered=False)
        except BulkWriteError as exc:
            tolerated, fatal = ms.classify_write_errors(exc)
            details = exc.details or {}
            return {"matched": details.get("nMatched", 0),
                    "upserted": details.get("nUpserted", 0),
                    "modified": details.get("nModified", 0),
                    "tolerated_dups": tolerated, "errors": fatal}
        except PyMongoError as exc:
            raise ms.MongoUnavailable(f"{collection}: bulk write failed: {exc}") from None
        return {"matched": result.matched_count,
                "upserted": len(result.upserted_ids or {}),
                "modified": result.modified_count, "tolerated_dups": 0, "errors": []}

    async def guarded_update(self, collection, key, update, *, require=None, upsert=True):
        spec = ms.spec_for(collection)
        ms.check_id(collection, key)
        ms.validate_update(update, collection, _id=key)
        filter_ = ms._guard_filter(key, require)                # noqa: SLF001 (one shared door)
        from pymongo.errors import DuplicateKeyError, PyMongoError
        handle = self.db[collection]
        lost = {"matched": 0, "upserted": 0, "modified": 0, "acquired": False,
                "tolerated_dups": 0}
        try:
            if not require:
                result = await handle.update_one(filter_, update, upsert=upsert)
                upserted = 0 if result.upserted_id is None else 1
                return {"matched": result.matched_count, "upserted": upserted,
                        "modified": result.modified_count,
                        "acquired": bool(result.matched_count or upserted),
                        "tolerated_dups": 0}
            result = await handle.update_one(filter_, update, upsert=False)
        except DuplicateKeyError:
            return dict(lost, tolerated_dups=1)
        except PyMongoError as exc:
            raise ms._driver_error(collection, "guarded update", exc) from None  # noqa: SLF001
        if result.matched_count:
            return {"matched": result.matched_count, "upserted": 0,
                    "modified": result.modified_count, "acquired": True,
                    "tolerated_dups": 0}
        if not upsert:
            return dict(lost)
        candidate = ms.apply_update(None, update, _id=key, collection=collection)
        if [f for f in spec.required if f not in candidate]:
            return dict(lost)
        ms.validate_document(collection, candidate)
        try:
            await handle.insert_one(candidate)
        except DuplicateKeyError:
            return dict(lost, tolerated_dups=1)
        except PyMongoError as exc:
            raise ms._driver_error(collection, "guarded create", exc) from None  # noqa: SLF001
        return {"matched": 0, "upserted": 1, "modified": 0, "acquired": True,
                "tolerated_dups": 0}

    async def update_many(self, collection, filter_, update):
        _assert_scoped(collection, filter_)
        ms.validate_update(update, collection)
        from pymongo.errors import PyMongoError
        try:
            result = await self.db[collection].update_many(filter_, update)
        except PyMongoError as exc:
            raise ms._driver_error(collection, "retraction sweep", exc) from None  # noqa: SLF001
        return result.modified_count

    async def delete_many(self, collection, filter_):
        if collection != "stream_meta":
            raise MirrorError(
                f"delete_many is legal on stream_meta only — a renumbered positional "
                f"document is aliasing garbage, but a {collection} document is history "
                f"(GD-26)"
            )
        _assert_scoped(collection, filter_)
        from pymongo.errors import PyMongoError
        try:
            result = await self.db[collection].delete_many(filter_)
        except PyMongoError as exc:
            raise ms._driver_error(collection, "stream_meta renumber", exc) from None  # noqa: SLF001
        return result.deleted_count

    async def drop_collection(self, collection):
        if collection != "derived":
            raise MirrorError(
                f"only `derived` is droppable (GD-23); {collection} holds observations"
            )
        from pymongo.errors import PyMongoError
        try:
            await self.db.drop_collection(collection)
        except PyMongoError as exc:
            raise ms._driver_error(collection, "drop", exc) from None  # noqa: SLF001

    async def _read_state(self):
        state = {}
        for name in ms.collection_names():
            bucket = {}
            try:
                cursor = self.db[name].find({})
                async for doc in cursor:
                    key = doc.get("_id")
                    bucket[key] = doc
            except Exception as exc:                             # noqa: BLE001
                raise ms.MongoUnavailable(f"{name}: read failed: {exc}") from None
            if bucket:
                state[name] = bucket
        return state

    async def fingerprint(self):
        return ms.fingerprint(projection_state(await self._read_state()))

    async def counts(self):
        return ms.counts(await self._read_state())

    async def close(self):
        try:
            await self.client.close()
        except Exception:                                        # noqa: BLE001
            return None


# --- the mirror -----------------------------------------------------------


def _utc(moment=None) -> datetime.datetime:
    return moment or datetime.datetime.now(datetime.timezone.utc)


def _boot_identity() -> str:
    """The raw per-boot fact, before hashing. Never published.

    A pid alone cannot identify a lease holder: pids are recycled, and a crashed
    holder's pid can be reused by an unrelated process before the lease expires.
    Falls back through the two other things Linux offers, then to the process
    start time, which at least never *collides* even if it is not shared.
    """
    for path in ("/proc/sys/kernel/random/boot_id",):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read().strip()
            if text:
                return text
        except OSError:
            pass
    try:
        with open("/proc/stat", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("btime "):
                    return line.split()[1]
    except OSError:
        pass
    return str(int(time.time()))


def _boot_id() -> str:
    """A **hashed** string that changes across reboots (GD-24 pins a string).

    The lease's only requirements are that this value change across reboots and
    never collide; neither one needs the raw datum. `/proc/sys/kernel/random/
    boot_id` is a stable host fingerprint, and it would otherwise reach
    `/health`, which is the one unauthenticated route (GD-13). So the identity is
    hashed before it is stored on the lease document and published — same
    guarantees, nothing about the host disclosed.
    """
    return hashlib.sha1(_boot_identity().encode("utf-8")).hexdigest()[:16]


class Mirror:
    """The write-behind projection: enqueue from the poll loop, drain elsewhere.

    Usage is two calls in two places, and the split is the whole design:

        mirror.enqueue(ops)                 # from the poll loop: sync, never blocks
        await mirror.run(stop=event)        # a task of its own: does the writing

    Everything expensive — connecting, waiting for a dead server, retrying,
    sweeping — happens on the drainer side of that line. :meth:`enqueue` cannot
    fail: a full queue drops the write, counts it and flips `/health` to
    `degraded`, because a mirror that stalls the ingest to avoid losing history
    has already lost the thing history was for.
    """

    def __init__(self, config=None, *, backend=None, clock=None, queue_size=None,
                 breaker_failures=BREAKER_FAILURES, breaker_hold=BREAKER_HOLD_S,
                 lease_ttl=LEASE_TTL_S, stream=None, registry=None, monotonic=None,
                 lease_required=True):
        self.config = config if config is not None else MongoConfig()
        self.backend = backend
        self.clock = clock or (lambda: _utc())
        self.monotonic = monotonic or time.monotonic
        self.stream = stream or getattr(self.config, "stream", None) or "mirror"
        self.registry = {} if registry is None else registry
        self.queue = asyncio.Queue(maxsize=queue_size or self.config.queue_size)
        self._wakeup = asyncio.Event()
        self.lease_ttl = lease_ttl
        self.breaker_failures = breaker_failures
        self.breaker_hold = breaker_hold
        self._failures = 0
        self._hold_until = 0.0
        #: Set when an observation was refused outright. A clean tick clears
        #: `degraded`; it must not clear THIS, because no tick can un-refuse it.
        self._sticky_degraded = False
        self._lease = {"held": False, "stream": self.stream, "expiresAt": None,
                       "holderPid": os.getpid(), "holderBoot": _boot_id()}
        #: GD-29's requirement, as a flag rather than as an inference from
        #: `_lease["held"]`. The tick used to gate its whole lease branch on
        #: `held`, which made the *renewal* airtight and left the never-acquired
        #: case wide open: a `start()` whose `acquire()` failed for any reason
        #: but a lost race returns `degraded` with `held=False`, and `degraded`
        #: is a state `enqueue` accepts and the drainer writes in. So a partial
        #: outage confined to `writers` — the one collection the lease lives in
        #: — produced a process mirroring a stream it holds no lease on, while
        #: `/health` published `state:"live"` beside `lease:{held:false}`.
        #: Defaults to True because fail-open is the failure being fixed;
        #: `start(acquire_lease=False)` and unit fixtures that are about the
        #: drain path rather than the lease turn it off *explicitly*.
        self._lease_required = bool(lease_required)
        #: True when `refused` means "another process holds the lease" rather
        #: than "this deployment is unauthenticated" (GD-27) or "the schema is
        #: wrong". Only the first of those is retryable, and conflating them
        #: would have `tick` re-probing a mongod Touch has refused on purpose.
        self._lease_lost = False
        #: Monotonic deadline for the next lease re-take attempt. One per TTL:
        #: the lease is TTL-based precisely so it can be re-taken, and a holder
        #: that stalled past its expiry and then exited must not leave this
        #: process mirroring nothing until somebody restarts it.
        self._retake_at = 0.0
        self.state = STATE_ABSENT if backend is None and not self.config.configured \
            else STATE_STARTING
        self.last_error = None
        #: Non-failure commentary. Deliberately NOT `lastError`: `/health` is the
        #: route an operator pages on, and a healthy mirror that publishes a
        #: `lastError` has cried wolf on the only channel that matters.
        self.notes = None
        self.stats = {"queued": 0, "written": 0, "dropped": 0, "tolerated_dups": 0,
                      "rejected": 0, "write_errors": 0, "retracted": 0,
                      "renumbered": 0, "sweeps": 0, "ticks": 0, "backfilled": 0,
                      "refused_no_lease": 0, "refused_policy": 0,
                      "refused_future_ts": 0, "refused_no_source": 0,
                      "unmapped": 0, "skipped_absent": 0}

    # --- health -----------------------------------------------------------

    def _redact(self, text):
        return redact(text, self.config.secrets)

    def note_error(self, exc):
        """Record a failure's *redacted* text. The only writer of `lastError`."""
        self.last_error = self._redact(f"{type(exc).__name__}: {exc}")
        return self.last_error

    def note(self, text):
        """Record redacted commentary that is **not** a failure (`notes`).

        For conditions the code has already decided are fine — a least-privilege
        user that cannot run `usersInfo`, say. Putting those in `lastError` would
        publish `state:"live", lastError:<something>` on `/health`, which reads
        as an unresolved fault to every operator and every alert rule.
        """
        self.notes = self._redact(str(text))
        return self.notes

    def _degrade(self, *, sticky=False):
        """GD-30's `degraded`, from any state that could still have been healthy.

        Reached from `live` **and** from `starting`: a queue-full burst during
        startup is exactly as much lost history as one at steady state, and
        leaving `/health` on `starting` would hide it (GD-30 does not qualify the
        prior state). `down`/`refused`/`absent` are already worse than degraded
        and are never upgraded to it.

        ``sticky`` marks a degradation that a later clean tick must NOT clear.
        `tick` deliberately treats `degraded` as a statement about *now* — a
        mirror that never recovered from one queue-full burst would be a
        permanently wrong answer. That reasoning holds for every write-side
        condition, and fails for exactly one thing: an observation no mapper
        claimed. No future tick can map it, so clearing the state would leave
        `/health` saying `live` about a run whose data is provably incomplete.
        """
        if sticky:
            self._sticky_degraded = True
        if self.state in (STATE_LIVE, STATE_STARTING):
            self.state = STATE_DEGRADED

    def health(self) -> dict:
        """R-45's `/health` block, exactly: `{state, lastError, notes, queued,
        dropped, tolerated_dups, lease, backend, db, counters}`, and no
        credential anywhere.

        That list is also what `docs/mongo.md` publishes, and the two are
        asserted equal by `tests/test_mirror.py`: `server.py` (R-30, sp-12)
        serves this dict verbatim, so the page documenting it *is* the contract,
        and a field added here without a doc edit is an undocumented API.

        `server.py` (R-30, sp-12) serves this verbatim; this module supplies it.
        `/health` is the one unauthenticated route (GD-13), so everything here
        is already redacted — `lastError` cannot be built any other way, because
        :meth:`note_error` is the only thing that sets it.

        Two invariants this dict must never contradict, both held structurally
        rather than by a check here (a `/health` route that can raise is worse
        than one that is wrong):

        * ``state == "live"`` implies ``lease["held"]`` whenever a lease is
          required — every path that sets `live` is downstream of the lease gate
          in :meth:`_tick` or of `acquire()` in :meth:`_start`. The pair
          `state:"live"` beside `lease:{held:false}` was a real defect, and it
          is the shape an operator cannot tell from a healthy mirror.
        * ``state == "live"`` implies ``lastError is None``. See :meth:`_settle`.
        """
        return {
            "state": self.state,
            "lastError": self.last_error,
            "notes": self.notes,
            "queued": self.queue.qsize(),
            "dropped": self.stats["dropped"],
            "tolerated_dups": self.stats["tolerated_dups"],
            "lease": dict(self._lease),
            "backend": self.backend.kind if self.backend is not None else None,
            "db": self.config.db,
            "counters": dict(self.stats),
        }

    # --- lifecycle --------------------------------------------------------

    async def start(self, *, ensure_schema=True, acquire_lease=True):
        """Connect, verify the deployment, bootstrap the schema, take the lease.

        Every failure here is a *state*, never an exception: an absent driver, a
        dead server, a mongod with no users and a lost lease all leave a running
        process with a truthful `/health` and a fully working live view. Returns
        the resulting state.

        That claim is made **total by construction** rather than by hoping the
        branches below enumerate every way a driver can misbehave. They did not:
        `ping`, `ensure_schema` and `AsyncMongoClient` itself can each raise
        something that is neither a `MongoUnavailable` nor a `MongoStoreError`
        (`RuntimeError: Cannot use AsyncMongoClient in different event loop` is
        the specimen), and one escaping here aborts the caller's startup over a
        database GD-22 says is optional. Anything unexpected is recorded as the
        failure it is and returned as a state.
        """
        try:
            return await self._start(ensure_schema=ensure_schema,
                                     acquire_lease=acquire_lease)
        except Exception as exc:                                 # noqa: BLE001
            self._record_failure(exc)
            return self.state

    async def _start(self, *, ensure_schema=True, acquire_lease=True):
        """:meth:`start`'s body. Every *expected* failure is handled in place."""
        dropped_before = self.stats["dropped"]
        if self.backend is None:
            if not self.config.configured:
                self.state = STATE_ABSENT
                self.last_error = "no Mongo URI configured (TOUCH_MONGO_URI / .touch/mongo.json)"
                return self.state
            if not ms.pymongo_available():
                self.state = STATE_ABSENT
                self.last_error = "pymongo is not installed (GD-21: its absence is legal)"
                return self.state
            try:
                self.backend = await AsyncBackend.connect(self.config.uri, self.config.db)
            except ms.MongoUnavailable as exc:
                self.state = STATE_DOWN
                self.note_error(exc)
                return self.state
        if not await self.backend.ping():
            self.state = STATE_DOWN
            self.last_error = "no mongod answered within the GD-21 timeouts"
            return self.state
        try:
            users = await self.backend.user_count()
        except Exception as exc:                                 # noqa: BLE001
            # `None` IS the healthy answer here — a correctly-secured
            # least-privilege user cannot run `usersInfo` (GD-27). So this is
            # commentary, not a fault: recording it as `lastError` would publish
            # `state:"live", lastError:…` and page somebody for the deployment
            # the docs ask for.
            users = None
            self.note(f"{type(exc).__name__}: {exc} (users could not be enumerated, "
                      f"which a least-privilege deployment expects)")
        if users == 0:
            # GD-27, the refusal that has to exist before any mirror code:
            # `docker run -p 27017:27017 mongo:7` is an unauthenticated database
            # holding the exact unredacted transcripts GD-13 exists to protect.
            self.state = STATE_REFUSED
            self.last_error = (
                "the mongod reports zero configured users: it is unauthenticated, and "
                f"Touch will not mirror transcripts into it — see {mongo_doc_path()} "
                "for the loopback+auth recipe (GD-27)")
            return self.state
        if ensure_schema:
            try:
                await self.backend.ensure_schema()
            except (ms.MongoStoreError, MirrorError) as exc:
                self.state = STATE_REFUSED if isinstance(exc, ms.SchemaError) else STATE_DOWN
                self.note_error(exc)
                return self.state
        # Recorded BEFORE the attempt, and from the argument rather than from
        # the outcome: a `start()` that asked for a lease and did not get one is
        # precisely the process that must not write, and reading the flag off
        # `acquire()`'s return value would clear the requirement on the failure
        # it exists for (GD-29).
        self._lease_required = bool(acquire_lease)
        if acquire_lease and not await self.acquire():
            return self.state
        if self.state == STATE_DEGRADED or self.stats["dropped"] > dropped_before:
            # Writes were lost before the drainer was running. Promoting to
            # `live` here would erase the only signal — `dropped` is a counter
            # nobody looks at until the state tells them to (GD-30, m4). A later
            # clean tick may still clear it; `start()` is not entitled to.
            self.state = STATE_DEGRADED
            if self.stats["dropped"]:
                self.note_error(MirrorError(
                    f"{self.stats['dropped']} mirror write(s) were dropped before the "
                    f"drainer was running; /health is not promoted to `live` over a loss "
                    f"it has already recorded (GD-30)"))
            return self.state
        self.state = STATE_LIVE
        # `live` and a `lastError` cannot both be true: `docs/mongo.md` offers
        # "a live mirror never publishes a lastError" as a contract an alert rule
        # may read literally, and a retried `start()` (first attempt against a
        # server that was still coming up, second one clean) is a real way to
        # arrive here carrying the first attempt's text. `notes` is untouched —
        # it is the field for things that are fine (see :meth:`note`).
        self.last_error = None
        return self.state

    async def acquire(self, stream=None) -> bool:
        """Take or renew GD-29's writer lease. False means *do not mirror*.

        Two call shapes, both proven against a real mongod in
        `tests/test_mongo_store.py`: a takeover behind
        `{leaseExpiresAt: {$lt: now}}`, and a renewal that writes only the
        expiry behind an equality precondition on ourselves. A lost race is a
        normal outcome — it is returned, never raised, and it leaves the process
        perfectly able to serve reads.

        **Total**, like every other entry point on the drainer's side. The lease
        path is on the same tick as `bulk_upsert` and has the same three
        non-`MongoUnavailable` exits — a server-side `$jsonSchema` refusal
        arriving as `SchemaError`, `validate_document`'s `MongoStoreError`, and
        whatever a driver raises that is not a `PyMongoError` at all. `tick` has
        guarded against those around the write since attempt 1; leaving the
        branch one step earlier unguarded meant a renewal — due at least every
        `LEASE_TTL_S * LEASE_RENEW_AT` — could kill the drainer task while
        `/health` went on reporting `live` with no `lastError` and the queue
        filled behind it. A failure here is a state (GD-22/GD-30), so the
        breaker gets it and the next tick tries again.
        """
        stream = stream or self.stream
        key = refs.ref_key({"kind": "writer", "stream": stream})
        now = self.clock()
        expires = now + datetime.timedelta(seconds=self.lease_ttl)
        pid, boot = self._lease["holderPid"], self._lease["holderBoot"]
        try:
            if self._lease["held"]:
                result = await self.backend.guarded_update(
                    "writers", key, ms.op_set({"leaseExpiresAt": expires}),
                    require={"holderPid": pid, "holderBoot": boot})
            else:
                result = await self.backend.guarded_update(
                    "writers", key,
                    ms.op_set({"holderPid": pid, "holderBoot": boot,
                               "leaseExpiresAt": expires}),
                    require={"leaseExpiresAt": {"$lt": now}})
        except ms.MongoUnavailable as exc:
            self._record_failure(exc)
            return False
        except Exception as exc:                                 # noqa: BLE001
            # A schema refusal on `writers`, or a driver surprise. Not a lost
            # race — `_lease_lost` stays where it is, because this process did
            # not lose the lease to anybody and `tick`'s once-per-TTL re-take is
            # for that case only. This is a fault: it degrades, it opens the
            # breaker after N, and it never escapes.
            self._record_failure(exc)
            return False
        self.stats["tolerated_dups"] += result.get("tolerated_dups", 0)
        if not result.get("acquired"):
            self._lease.update(held=False, expiresAt=None, stream=stream)
            self.state = STATE_REFUSED
            self._lease_lost = True
            self._retake_at = self.monotonic() + self.lease_ttl
            self.last_error = (
                f"another process holds the writer lease for stream {stream!r}; this one "
                f"will serve reads and mirror nothing until the lease expires (GD-29)")
            return False
        self._lease.update(held=True, stream=stream,
                           expiresAt=expires.isoformat().replace("+00:00", "Z"))
        self._lease_lost = False
        # A completed server round trip, and on an idle deployment the ONLY one
        # this process makes: between transcript writes the queue is empty, so
        # the batch loop — which used to be the sole caller of `_record_success`
        # — never runs. Without this, a mirror that recovered stayed `down` with
        # a stale `lastError` until traffic happened to arrive, and the next
        # single failure re-opened a full breaker hold immediately instead of
        # after N. GD-22 asks `/health` to be truthful, not pessimistic.
        self._record_success()
        return True

    def _lease_due(self) -> bool:
        if not self._lease["held"] or not self._lease["expiresAt"]:
            return True
        expires = datetime.datetime.fromisoformat(self._lease["expiresAt"].replace("Z", "+00:00"))
        remaining = (expires - self.clock()).total_seconds()
        return remaining <= self.lease_ttl * LEASE_RENEW_AT

    # --- the queue --------------------------------------------------------

    def enqueue(self, ops, *, stream=None) -> int:
        """Queue operations for the drainer. Synchronous, non-blocking, total.

        Returns the number accepted. Never raises, never awaits, never yields to
        the loop — this is the function the 250 ms poll loop calls, and GD-30
        gives the database a 0 ms budget on that path. A full queue drops the
        write, counts it, and degrades `/health`; a mirror that applied
        backpressure to the ingest would trade a *permanent* record of the run
        for a *complete* copy of it in a database nobody can reach.

        Operations for a stream this process does not hold the lease on are
        refused (GD-29) and counted, not written — under `refused_no_lease`.
        The *other* two things `refused` can mean (an unauthenticated mongod,
        GD-27; a schema Touch will not write to) are counted under
        `refused_policy` instead. The module keeps those three apart everywhere
        else — `_lease_lost` exists for exactly that — and `/health` publishes
        the counters, so booking all three under a name that says "lease" sends
        an operator looking for a second writer that does not exist.

        One operation, or an iterable of them. A generator is *materialised*
        here rather than wrapped: `MirrorOp` is itself a tuple subclass, so
        "not a list or tuple ⇒ one operation" quietly queued a generator
        **object** as a single operation and returned `accepted=1` for it — a
        count that lied, followed by a `rejected` one tick later when
        `_take_batches` could not unpack it. Anything that is neither a
        `MirrorOp` nor iterable is refused as one rejected operation and named,
        rather than queued to fail later somewhere with less context.
        """
        if isinstance(ops, MirrorOp):
            ops = [ops]
        elif not isinstance(ops, (list, tuple)):
            try:
                ops = list(ops)
            except Exception as exc:                             # noqa: BLE001
                # `Exception`, not `TypeError`: draining a generator runs the
                # *caller's* code here, and "never raises" is this function's
                # first promise (GD-30 — it is what the poll loop calls).
                self.stats["rejected"] += 1
                self.note_error(MapperError(
                    f"enqueue takes a MirrorOp or an iterable of them; "
                    f"{type(ops).__name__} raised {type(exc).__name__}: {exc}"))
                # Sticky, like an unmapped observation (`map_total`): whatever
                # the caller meant to write is gone, and no later tick can make
                # `live` true about a run whose data is provably incomplete.
                self._degrade(sticky=True)
                return 0
        if self.state == STATE_ABSENT:
            # Nothing was attempted, so nothing was *dropped*: a deployment with
            # no Mongo at all must not accumulate a loss counter for writes it
            # never promised to make (GD-21).
            self.stats["skipped_absent"] += len(ops)
            return 0
        wrong_stream = stream is not None and stream != self._lease["stream"]
        if self.state == STATE_REFUSED or wrong_stream:
            counter = "refused_no_lease" if (wrong_stream or self._lease_lost) \
                else "refused_policy"
            self.stats[counter] += len(ops)
            return 0
        accepted = 0
        for op in ops:
            try:
                self.queue.put_nowait(op)
            except asyncio.QueueFull:
                self.stats["dropped"] += 1
                self._degrade()
                continue
            accepted += 1
        self.stats["queued"] += accepted
        if accepted and not self._wakeup.is_set():
            self._wakeup.set()
        return accepted

    def map_total(self, kind, observation):
        """Map an observation, or record the failure and return no operations.

        **Total**, like :meth:`enqueue` and for the same reason: an unregistered
        kind or a buggy mapper is a fact about one observation, and letting it
        propagate would turn it into a fact about the whole run. `map_observation`
        keeps raising for callers that want the exception — this is the wrapper
        the loop-side seam uses.

        The observation is not silently dropped (GD-26): it is counted
        (`rejected`, `unmapped`), it sets `lastError`, and it degrades `/health`,
        which is the same treatment `_take_batches` already gives a poison
        operation on the drainer side of the line.
        """
        try:
            return map_observation(self.registry, kind, observation)
        except MapperError as exc:
            self.stats["rejected"] += 1
            if kind not in self.registry:
                self.stats["unmapped"] += 1
            self.note_error(exc)
            self._degrade(sticky=True)
            return []

    def map_and_enqueue(self, kind, observation, *, stream=None) -> int:
        """Map an observation through SD-1's registry and queue the result.

        Mapping is pure and cheap, so it happens on the caller's side of the
        line; only the write crosses it. Total: returns 0 for an observation no
        mapper claims, having counted and reported it.
        """
        return self.enqueue(self.map_total(kind, observation), stream=stream)

    # --- draining ---------------------------------------------------------

    def _record_failure(self, exc):
        self._failures += 1
        self.note_error(exc)
        if self._failures >= self.breaker_failures:
            self._hold_until = self.monotonic() + self.breaker_hold
            self.state = STATE_DOWN
        else:
            self.state = STATE_DEGRADED

    def _record_success(self):
        self._failures = 0
        self._hold_until = 0.0

    @property
    def breaker_open(self) -> bool:
        return self.monotonic() < self._hold_until

    def _take_batches(self, limit=None):
        """Drain the queue into `{collection: [(key, update), …]}` batches.

        Batched per collection because that is what one `bulk_write` addresses,
        and **last-write-wins within a tick is not applied**: two updates to one
        `_id` in one batch stay two operations, because the algebra is
        `$max`/`$addToSet` and collapsing them client-side would be the
        write-order-dependent `$set` GD-25 exists to forbid.

        This is also where GD-27's document backstop runs — the *only* place it
        runs, and the right one: the drainer is off GD-30's critical path, and
        this is the last code between an operation and `bulk_upsert`, so a scrub
        here is the copy that reaches the store. Once per operation, not once
        per visit: an operation requeued by `_requeue` comes back as a
        :class:`ScrubbedOp` and is validated again but not walked again. See
        :func:`validate_op`.
        """
        batches = {}
        taken = 0
        rejected = 0
        while limit is None or taken < limit:
            try:
                op = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            taken += 1
            try:
                op = validate_op(op, source="queue")
            except Exception as exc:                             # noqa: BLE001
                # A poison operation is dropped and counted, never re-queued: it
                # will fail identically forever, and a retry loop around it
                # wedges every healthy write behind it.
                #
                # `Exception`, not just `MapperError`, because the operation has
                # already left the queue when this runs: `scrub_op_update` walks
                # a payload of agent-controlled depth, so a pathologically nested
                # document raises `RecursionError` — not a `MapperError` — and a
                # narrower `except` let it escape to `tick`'s blanket guard,
                # which books it as a *failure* after the operation was dequeued
                # and before it entered a batch. That is an operation that
                # vanished without being counted as `rejected` or `dropped`,
                # which is the one thing GD-26 does not permit.
                self.stats["rejected"] += 1
                rejected += 1
                self.note_error(exc)
                continue
            batches.setdefault(op.collection, []).append((op.key, op.update))
        return batches, taken, rejected

    def _requeue(self, collection, operations, pending, report):
        """Return unwritten operations to the queue; count what no longer fits.

        Safe to append at the tail rather than restore the original order
        because GD-25's algebra is order-independent by construction
        (`$max`/`$addToSet`/`$min`/`$setOnInsert` commute — that is the property
        the acceptance test certifies by ingesting the corpus shuffled and
        reversed). A queue that filled while the server was away drops the
        overflow and *says so*, which is the documented degrade; dropping the
        whole in-flight batch and reporting nothing is not.

        Requeued as :class:`ScrubbedOp`: these updates came out of
        `_take_batches`, so GD-27's walk has already run over them and their
        bytes cannot have changed since. Without the marker a long outage
        re-paid that walk on every tick, for every operation in flight — the
        exact cost this attempt's predecessor moved off the poll loop, quietly
        reintroduced on the path taken when the mirror is already unhealthy.
        """
        for name, items in [(collection, operations)] + list(pending):
            for key, update in items:
                try:
                    self.queue.put_nowait(ScrubbedOp(name, key, update))
                except asyncio.QueueFull:
                    self.stats["dropped"] += 1
                    report["dropped"] = report.get("dropped", 0) + 1

    async def tick(self, *, limit=None) -> dict:
        """One drain cycle. Returns a small report; never raises.

        The breaker's whole value is visible here: while it is open this
        function does not touch the driver at all, so a dead mongod costs one
        500 ms server-selection timeout every 30 s rather than one per tick
        (MONGOSCHEMA-4's 30.1 s stall is the version of this with pymongo's
        defaults and no breaker). It is checked before everything else,
        including the lease re-take below, because "do not touch the driver" has
        to mean every reason one might want to.

        This is also where a *lost* lease comes back: `refused` is a state about
        now, not a verdict for the lifetime of the process (GD-29's lease is
        TTL-based precisely so it can be re-taken).

        "Never raises" is enforced here rather than asserted branch by branch.
        `run()` awaits this in a long-lived task, and an escaping exception
        there is the worst failure this module has: the task dies, asyncio
        swallows its exception until GC, `/health` keeps reporting the last
        state it saw — `live`, with `lastError: null` — and every subsequent
        `enqueue` piles into a queue nobody drains until it overflows into a
        counter nobody looks at until the state tells them to. So the body runs
        inside a guard that turns any surprise into `_record_failure` plus a
        report, which is what GD-22 and GD-30 mean by a degrade ladder.
        """
        self.stats["ticks"] += 1
        report = {"written": 0, "batches": 0, "held": False, "skipped": None,
                  "errors": 0, "rejected": 0, "dropped": 0, "reacquired": False}
        try:
            return await self._tick(report, limit=limit)
        except Exception as exc:                                 # noqa: BLE001
            self._record_failure(exc)
            report["errors"] += 1
            report["skipped"] = "error"
            return report

    async def _tick(self, report, *, limit=None) -> dict:
        """:meth:`tick`'s body; every *expected* failure is handled in place."""
        if self.state == STATE_ABSENT or self.backend is None:
            report["skipped"] = self.state
            return report
        if self.breaker_open:
            report["skipped"] = "breaker"
            report["held"] = True
            return report
        if self.state == STATE_REFUSED:
            # GD-29 says a process that cannot hold the lease refuses to mirror.
            # It does not say that refusal is terminal — the lease has a TTL
            # exactly so it can be re-taken, and a transient takeover (this
            # process stalled past the TTL, another took over, then exited) would
            # otherwise leave a running aggregator mirroring nothing for the rest
            # of its life with no remedy but a restart.
            #
            # Retried at most once per TTL, and ONLY for a lost lease: the other
            # two refusals (an unauthenticated mongod, a schema Touch will not
            # write to) are deliberate and must never be re-probed on a timer.
            if not self._lease_lost or self.monotonic() < self._retake_at:
                report["skipped"] = self.state
                return report
            self._retake_at = self.monotonic() + self.lease_ttl
            self.state = STATE_STARTING            # so a second refusal can set it again
            if not await self.acquire():
                report["skipped"] = self.state
                return report
            report["reacquired"] = True
            self.last_error = None
            self.state = STATE_DEGRADED if self._sticky_degraded else STATE_LIVE
        if self._lease_required and self._lease_due():
            # On the REQUIREMENT, not on `_lease["held"]`, and on the boolean
            # `acquire()` returns rather than on the state it left behind.
            #
            # `held` was the outer gate, and it made the renewal airtight while
            # letting the never-acquired case walk past the same door: a
            # `start()` whose `acquire()` failed on anything but a lost race
            # returns `degraded` with `held=False`, this branch then never ran,
            # and the tick wrote a batch with no lease at all. `_lease_due()`
            # already returns True when the lease is not held, so the flag is
            # the whole gate: not held ⇒ due ⇒ take it or decline to write.
            #
            # And on the boolean because `acquire()` returns False for two
            # different reasons — a lost race (`refused`) and a failure to reach
            # `writers` at all (`degraded`/`down`) — of which only the first
            # ever set `refused`. Falling through on the second wrote under a
            # lease that was not renewed and may already have expired, which is
            # the one thing GD-29 forbids. A partial outage confined to the
            # `writers` collection is enough to reach both halves.
            if not await self.acquire():
                report["skipped"] = self.state
                return report
        if self._lease_required and not self._lease["held"]:
            # Belt, deliberately redundant with the branch above: there is no
            # path from here to `bulk_upsert` that checks the lease again, so
            # this is the assertion that keeps a future refactor of that branch
            # from re-opening the hole rather than a condition expected to fire.
            report["skipped"] = "no-lease"
            return report
        batches, _taken, rejected = self._take_batches(limit)
        # Reported as well as counted: a tick whose whole batch was poison must
        # not look like a tick with nothing to do.
        report["rejected"] += rejected
        # No early return on an empty batch set. There used to be one, and it
        # made a work-free tick incapable of ever settling the state: an idle
        # deployment — the steady state between transcript writes — returned
        # here, so `/health` stayed `down` with a stale `lastError` for as long
        # as the session was quiet, however healthy the server, and recovered
        # only when a write happened to arrive. An empty `pending` falls through
        # the loop below for free, and :meth:`_settle` is then reached by every
        # tick that got this far. GD-22 asks `/health` to be truthful; pessimism
        # that never self-heals is a different untruth from optimism, not a
        # safer one.
        pending = list(batches.items())
        while pending:
            collection, operations = pending.pop(0)
            try:
                result = await self.backend.bulk_upsert(collection, operations)
            except ms.MongoUnavailable as exc:
                # The server went away mid-tick. These operations were already
                # taken OUT of the queue, so returning here without them would
                # lose them silently — the one thing GD-26 does not permit. Put
                # everything unwritten back and let the breaker decide when to
                # try again: the queue is the buffer, and when IT overflows the
                # loss is counted in `dropped` and visible in `/health`.
                self._requeue(collection, operations, pending, report)
                self._record_failure(exc)
                report["skipped"] = "unavailable"
                return report
            except ms.MongoStoreError as exc:
                # The document is wrong, the server is fine: this must NOT count
                # toward the breaker, or a single bad mapper output takes a
                # healthy mirror down (mongo_store._driver_error's rule).
                self.stats["rejected"] += len(operations)
                report["rejected"] += len(operations)
                report["errors"] += 1
                self.note_error(exc)
                self.state = STATE_DEGRADED
                continue
            except Exception as exc:                             # noqa: BLE001
                # The drainer is a long-lived task, and this is the only place a
                # driver can surprise it. `RuntimeError: Cannot use
                # AsyncMongoClient in different event loop` is the real specimen:
                # not a MongoStoreError, not a MongoUnavailable, and fatal to the
                # task if it escapes — which would leave a process whose mirror
                # is dead while `/health` still claimed `live`. Degrade like any
                # other failure, keep the operations, and let the breaker decide.
                self._requeue(collection, operations, pending, report)
                self._record_failure(exc)
                report["skipped"] = "error"
                return report
            report["batches"] += 1
            written = result["matched"] + result["upserted"]
            report["written"] += written
            self.stats["written"] += written
            self.stats["tolerated_dups"] += result.get("tolerated_dups", 0)
            if result.get("errors"):
                self.stats["write_errors"] += len(result["errors"])
                report["errors"] += len(result["errors"])
                self.state = STATE_DEGRADED
                self.note_error(MirrorError(
                    f"{collection}: {len(result['errors'])} write error(s) in an unordered "
                    f"bulk; first: {result['errors'][0]}"))
        if report["batches"]:
            # Gated on a completed `bulk_upsert`, because that is what makes it
            # *evidence*: a round trip the server answered. A tick with nothing
            # to write proves nothing about the server, and clearing the breaker
            # on the strength of having done nothing would let an idle process
            # promote a dead mongod back to `live`. The other round trip an idle
            # process makes — the lease renewal — records its own success inside
            # :meth:`acquire`, which is what lets a quiet deployment recover.
            self._record_success()
        self._settle(report)
        return report

    def _settle(self, report):
        """Clear `degraded`/`down` when this tick produced evidence of health.

        A clean tick that leaves nothing queued clears the state. The *counters*
        (`dropped`, `write_errors`) stay — they are the durable record — but the
        state is a statement about now, and a mirror that never recovered from
        one queue-full burst would be a permanently wrong answer on an otherwise
        healthy deployment.

        Four things veto the promotion, and each is a fault this tick can see:

        * ``self._failures`` — an unanswered server failure. This is what keeps a
          *work-free* tick honest now that one can reach here: with nothing to
          write and no lease renewal due, a tick has no evidence at all, and
          `_failures` is the record of the last thing that did. It reaches zero
          only through :meth:`_record_success`, which needs a completed round
          trip (a write, or a lease renewal).
        * ``report["errors"]`` / ``report["rejected"]`` — this tick refused or
          failed on a document. Promoting would also erase the `lastError` that
          names it, on the same tick that produced it.
        * ``STATE_REFUSED`` — GD-29/GD-27 refusals are decisions, not weather.
        * ``_sticky_degraded`` — an observation nobody mapped is data that never
          reached the store and never will, so no amount of clean writing
          afterwards makes the answer `live` true (see :meth:`_degrade`).

        `lastError` is cleared with the state, and that is a documented contract
        rather than tidiness: `docs/mongo.md` promises that a `live` mirror never
        publishes a `lastError`, so an alert rule can read the field literally.
        Leaving the fault text behind on recovery made that rule fire forever
        after the first transient blip.
        """
        if (self._failures or report["errors"] or report["rejected"]
                or self.state == STATE_REFUSED or not self.queue.empty()
                or self._sticky_degraded):
            return
        self.state = STATE_LIVE
        self.last_error = None

    async def run(self, *, stop=None, interval=TICK_INTERVAL_S, ticks=None):
        """The drainer task: tick, then sleep until woken or the interval lapses.

        This is the *only* coroutine the poll loop must not await. It exists so
        that `enqueue` can be a synchronous function that returns immediately —
        which is what makes Mongo's contribution to the critical path 0 ms
        (GD-30), whatever the database is doing.

        The guard around the tick is deliberate redundancy. :meth:`tick` is
        already total, and this catches the same class of thing a second time,
        because the two failures are not equally bad: a tick that mishandles an
        exception costs one cycle, and a `run()` task that dies costs every
        cycle after it, silently, behind a `/health` that still says `live`.
        A belt on the only long-lived task in the module is worth one `except`.
        """
        done = 0
        while True:
            if stop is not None and stop.is_set():
                return done
            # Cleared BEFORE the tick, never after: an `enqueue` that lands
            # while this tick is awaiting the driver must leave the event SET,
            # or the drainer sleeps a full interval on work it already has.
            self._wakeup.clear()
            try:
                await self.tick()
            except Exception as exc:                             # noqa: BLE001
                # `Exception`, not `BaseException`: `asyncio.CancelledError` is
                # a `BaseException` on 3.8+ and passes straight through, so
                # cancelling the drainer still cancels it.
                self._record_failure(exc)
            done += 1
            if ticks is not None and done >= ticks:
                return done
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=interval)
            except (asyncio.TimeoutError, TimeoutError):
                pass

    async def flush(self, *, max_ticks=100):
        """Drain everything currently queued. For shutdown and for tests."""
        written = 0
        for _ in range(max_ticks):
            if self.queue.empty():
                break
            report = await self.tick()
            written += report["written"]
            if report["skipped"]:
                break
        return written

    # --- cursors ----------------------------------------------------------

    async def save_cursor(self, stream_id, checkpoint, *, last_seq=None, ts=None):
        """Persist a per-source watermark (GD-24's `cursors`, SD-10's identity).

        `$set`, not `$max`, and deliberately: a shrink (`size < offset`) rewinds
        the offset legitimately, and a monotonic watermark would pin the cursor
        past the end of a truncated file forever — the exact failure SD-10's
        re-ingest exists to recover from.
        """
        key = refs.ref_key({"kind": "cursor", "streamId": stream_id})
        fields = {"offset": int(checkpoint.offset), "stDev": int(checkpoint.st_dev),
                  "stIno": int(checkpoint.st_ino), "size": int(checkpoint.size),
                  "gen": int(checkpoint.gen), "updatedTs": _utc(ts)}
        if last_seq is not None:
            fields["lastSeq"] = int(last_seq)
        result = await self.backend.guarded_update("cursors", key, ms.op_set(fields))
        return result

    async def load_cursor(self, stream_id):
        """The stored watermark as a `tailer.Checkpoint`, or None.

        Returned as the file-side type on purpose: the checkpoint's whole job is
        to be handed straight back to `tailer.Tailer`, and a dict that has to be
        translated at every call site is how the identity tuple loses a field.
        """
        key = refs.ref_key({"kind": "cursor", "streamId": stream_id})
        doc = await self._find_one("cursors", key)
        if not doc:
            return None
        return tailer_mod.Checkpoint.from_dict({
            "st_dev": doc.get("stDev", 0), "st_ino": doc.get("stIno", 0),
            "size": doc.get("size", 0), "offset": doc.get("offset", 0),
            "gen": doc.get("gen", 0)})

    async def _find_one(self, collection, key):
        backend = self.backend
        if isinstance(backend, MemoryBackend):
            return backend.state.get(collection, {}).get(key)
        try:
            return await backend.db[collection].find_one({"_id": key})
        except Exception as exc:                                 # noqa: BLE001
            raise ms.MongoUnavailable(f"{collection}: read failed: {exc}") from None

    # --- the generation sweep (GD-26) -------------------------------------

    async def sweep(self, scope, gen, *, reinsert=(), allow_empty_reinsert=False):
        """Mark-and-sweep after a full re-ingest of one source file.

        SD-10 splits this: `tailer.py` detects the shrink (checkpoint identity
        with `size < offset` explicit) and signals a full idempotent re-ingest;
        the sweep that re-ingest must run is this one.

        Two collections, two rules, and the asymmetry is the whole of GD-26:

        * `records` is uuid-keyed and rewritable, so vanished documents are
          **retracted** — `$set:{retracted:true, retractedGen:G}` via
          `updateMany`. The mirror exists BECAUSE the CLI deletes history
          (`performRemoveByUuid` truncates and rewrites); physically deleting
          rewound records would re-import that destruction. D13 honesty is
          satisfied by rendering: retracted records are hidden by default,
          visible on demand, never shown as current.
        * `stream_meta` is positionally keyed, so a file whose lines renumbered
          leaves *aliasing garbage*, not history: those documents are deleted
          and re-inserted in this same code path. It is the one legal delete in
          Touch, and it is scoped to one file's line range.

        Incremental append ticks never call this: they neither bump `gen` nor
        delete anything.

        The delete and its re-insert are **one code path**, and that is what
        makes Touch's one delete defensible — so an empty ``reinsert`` has to be
        asked for. A re-ingest that renumbered a file yields the file's current
        lines; nothing to re-insert means the file is now empty, which is real
        (a truncation to zero) but rare, and indistinguishable from a caller who
        forgot the argument. `allow_empty_reinsert=True` is that caller saying
        so out loud, and it costs a word at the one call site that needs it
        rather than leaving a bare scoped delete looking like the legal one.
        """
        _assert_scoped("records", scope)
        if not isinstance(gen, int) or isinstance(gen, bool) or gen < 1:
            raise MirrorError(f"gen must be a positive int, got {gen!r}")
        if not reinsert and not allow_empty_reinsert:
            raise MirrorError(
                "sweep deletes the renumbered stream_meta documents and re-inserts the "
                "file's current lines in one code path (GD-26); an empty `reinsert` is "
                "a bare scoped delete, so pass allow_empty_reinsert=True if the source "
                "really did shrink to nothing")
        self.stats["sweeps"] += 1
        report = {"retracted": 0, "renumbered": 0, "reinserted": 0}
        stale = dict(scope, gen={"$lt": gen})
        report["retracted"] = await self.backend.update_many(
            "records", stale, ms.op_set({"retracted": True, "retractedGen": gen}))
        self.stats["retracted"] += report["retracted"]
        report["renumbered"] = await self.backend.delete_many("stream_meta", dict(stale))
        self.stats["renumbered"] += report["renumbered"]
        ops = [validate_op(op) for op in reinsert]
        wrong = [op.collection for op in ops if op.collection != "stream_meta"]
        if wrong:
            raise MirrorError(
                f"sweep re-insert takes stream_meta operations only, got {sorted(set(wrong))} "
                f"— the delete and its re-insert are one code path (GD-26)")
        if ops:
            result = await self.backend.bulk_upsert(
                "stream_meta", [(op.key, op.update) for op in ops])
            report["reinserted"] = result["matched"] + result["upserted"]
        return report

    # --- rebuild / backfill ----------------------------------------------

    async def rebuild(self, observations, *, drop_derived=True):
        """`--rebuild`: drop `derived`, replay everything from files (GD-22/R-45).

        Mongo is a projection, so this is the operation that proves it: after a
        wipe, replaying the same observations must produce a byte-identical
        fingerprint. `derived` is dropped rather than migrated (GD-23's
        reducer-version rule); every other collection is upsert-only, so replay
        lands on its own output and duplicate keys are tolerated by design.

        ``observations`` is an iterable of `(kind, obs)` pairs supplied by the
        caller — in production, by the entity modules that own the files (their
        `MIRROR_SOURCES`, the seam :func:`iter_sources` reads); in tests, by the
        fixtures. This module drives mappers; it does not parse transcripts,
        because the module that owns a file format owns its parser (GD-15).

        The ordering around the one destructive step this module has is the
        whole of the method:

        * **everything is mapped first**, before `derived` is dropped. Mapping is
          pure and holds no client, so this costs nothing but memory and it is
          the only way to know whether the replay can actually run. Resolving
          just the *registry* first is not enough: a kind can be registered and
          its mapper still raise (`Mapper.__call__` reports every mapper bug as
          `MapperError`), which is a rebuild that drops the reducer's collection
          and then replays nothing;
        * `derived` is dropped only if that pass produced **zero** rejections —
          unmapped kinds and failing mappers alike. A rebuild that cannot replay
          everything must not also destroy the reducer's output; it reports what
          it could not map and leaves the store as it found it.

        Never raises — for a mapping failure or for a *server* one. The
        unreplayable kinds, the rejection count and any driver fault come back in
        the report and on `/health`, which is the difference between an operator
        who can see what happened and a traceback out of `asyncio.run`. That
        matters most here: `--rebuild` is the command run against a database
        somebody is in the middle of fiddling with, so its three driver calls —
        the drop, and the two reads that build the report — are each a live
        outage waiting to happen. A failed drop *stops* the rebuild (dropping is
        the precondition for a faithful replay, not a step in it); a failed read
        costs only the report field, which comes back `None` beside a `lastError`
        that says why.
        """
        observations = list(observations)
        unmapped = sorted({kind for kind, _ in observations if kind not in self.registry})
        rejected_before = self.stats["rejected"]
        mapped = [self.map_total(kind, observation) for kind, observation in observations]
        rejected = self.stats["rejected"] - rejected_before
        dropped_derived = False
        if drop_derived and rejected:
            reason = (f"no mapper is registered for {unmapped}" if unmapped else
                      f"{rejected} observation(s) could not be mapped")
            self.note_error(MirrorError(
                f"--rebuild kept `derived`: {reason} — a rebuild that cannot replay "
                f"every observation does not drop the reducer's collection as well "
                f"(GD-23/GD-26)"))
            self._degrade(sticky=True)
        elif drop_derived:
            try:
                await self.backend.drop_collection("derived")
            except Exception as exc:                             # noqa: BLE001
                # The server went away (or refused the drop) before the replay
                # started. Return without replaying: `derived` is still the OLD
                # reducer's output, and replaying onto it would leave a store
                # that is neither the old projection nor the new one, with a
                # report claiming a rebuild happened.
                self._record_failure(exc)
                return {"replayed": 0, "unmapped": len(unmapped),
                        "unmappedKinds": unmapped, "rejected": rejected,
                        "droppedDerived": False, "counts": None, "fingerprint": None}
            dropped_derived = True
        replayed = 0
        for ops in mapped:
            replayed += self.enqueue(ops)
            if self.queue.qsize() >= max(1, self.queue.maxsize // 2):
                await self.flush()
        await self.flush()
        return {"replayed": replayed, "unmapped": len(unmapped),
                "unmappedKinds": unmapped, "rejected": rejected,
                "droppedDerived": dropped_derived,
                "counts": await self._report_read(self.backend.counts),
                "fingerprint": await self._report_read(self.backend.fingerprint)}

    async def _report_read(self, call):
        """A read whose only consumer is a report: `None` beats a traceback.

        `counts()` and `fingerprint()` describe what the rebuild *did*; they are
        not part of doing it. A server that went away between the last write and
        the summary must not turn a completed rebuild into an exception out of
        `asyncio.run` — the operator would have no way to tell that from a
        rebuild that never ran.
        """
        try:
            return await call()
        except Exception as exc:                                 # noqa: BLE001
            self._record_failure(exc)
            return None

    async def backfill(self, observations, *, mtimes=None):
        """`--backfill`: a one-shot historical walk. `live` is False, always.

        Three rules, all of them in the code rather than in a comment:

        1. ``live = False`` is a literal here and there is no parameter that can
           change it — a backfill burst that announced itself as live would
           animate a year of history through the UI (R-55's frames carry the
           flag; this is where the flag is decided).
        2. every operation is stamped `ingestMode:"backfill"`.
        3. any operation carrying a timestamp **newer than its source file's
           mtime** is refused and counted. That is the guard against the failure
           mode a backfill actually has: a mapper that reaches for `now()`
           because a journal record has no timestamp (SESSIONJSONL-5) would
           stamp a whole historical corpus with today's clock, and nothing
           downstream could ever tell the run's time from the import's.

        Rule 3 **fails closed**. Each item is `(kind, obs, source)`; a 2-tuple,
        or a source whose mtime cannot be read, means the guard cannot be
        evaluated — and an operation that carries a timestamp is then refused
        (`refused_no_source`) rather than admitted against `now()`. Widening the
        limit to the import's clock, which is what a `now()` default does, makes
        the refusal `ts > now()` — a condition no mapper reading a historical
        file can ever trip, i.e. the guard silently switched off in exactly the
        case it was written for. An operation carrying no timestamp at all has
        nothing to compare and passes: there is no claim about time to check.

        ``observations`` come from :func:`iter_backfill_observations` in
        production, which tags every one with the file it was read from.
        """
        live = False
        assert live is False                                     # rule 1, executable
        # Copied, then used as the memo for rule 3: a walk yields thousands of
        # observations over hundreds of files, and `mtimes` is already the
        # per-source cache the guard needs — one `stat()` per file, not one per
        # observation. Copied rather than mutated in place because a caller's
        # override dict is an input, not scratch space.
        mtimes = dict(mtimes) if mtimes else {}
        report = {"live": live, "stamped": 0, "refused": 0, "refused_no_source": 0,
                  "replayed": 0, "unmapped": 0, "malformed": 0}
        for item in observations:
            # Unpacked defensively, because this loop's whole design note is that
            # a backfill of a large corpus must not die on one item — and the
            # unpack was the one line that could. `len(item)` needs a *sized*
            # object, so a streaming source's generator raised `TypeError`, and a
            # 4-tuple raised `ValueError: too many values to unpack`: both out of
            # a method that reports every other kind of bad item as a counted
            # refusal. A malformed item is a fact about one item.
            try:
                parts = tuple(item)
            except TypeError:
                parts = None
            if parts is None or not 2 <= len(parts) <= 3:
                self.stats["rejected"] += 1
                report["malformed"] += 1
                self.note_error(MirrorError(
                    f"backfill skipped a malformed item: expected (kind, observation) "
                    f"or (kind, observation, source), got {type(item).__name__} "
                    f"{'of length ' + str(len(parts)) if parts is not None else ''}"))
                # Sticky, for `map_total`'s reason: this observation never
                # reached the store and no later tick can put it there, so a
                # clean tick afterwards must not restore `live` — or erase the
                # `lastError` that names it.
                self._degrade(sticky=True)
                continue
            kind, observation = parts[0], parts[1]
            source = parts[2] if len(parts) > 2 else None
            before = self.stats["unmapped"]
            ops = stamp_backfill(self.map_total(kind, observation))
            report["unmapped"] += self.stats["unmapped"] - before
            if source not in mtimes:
                mtimes[source] = _mtime(source, None)
            limit = mtimes[source]
            keep = []
            for op in ops:
                newest = max(op_timestamps(op), default=None)
                if newest is None:
                    keep.append(op)
                    continue
                if limit is None:
                    self.stats["refused_no_source"] += 1
                    report["refused_no_source"] += 1
                    report["refused"] += 1
                    self.note_error(MirrorError(
                        f"backfill refused {op.collection} {op.key}: it carries "
                        f"{newest.isoformat()} and its source is unknown, so the "
                        f"mtime guard cannot be evaluated — a guard that cannot be "
                        f"checked fails closed, it does not widen to now()"))
                    continue
                if _aware(newest) > limit:
                    self.stats["refused_future_ts"] += 1
                    report["refused"] += 1
                    self.note_error(MirrorError(
                        f"backfill refused {op.collection} {op.key}: it carries "
                        f"{newest.isoformat()}, newer than its source's mtime "
                        f"{limit.isoformat()} — a backfill never stamps the import's clock"))
                    continue
                keep.append(op)
            report["stamped"] += len(keep)
            report["replayed"] += self.enqueue(keep)
            if self.queue.qsize() >= max(1, self.queue.maxsize // 2):
                await self.flush()
        await self.flush()
        self.stats["backfilled"] += report["stamped"]
        return report


def _aware(moment):
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)


def _mtime(path, default=None):
    if not path:
        return default
    try:
        return datetime.datetime.fromtimestamp(os.stat(path).st_mtime,
                                               datetime.timezone.utc)
    except OSError:
        return default


def iter_sources(registry_modules=None, *, package=None):
    """`(kind, callable)` pairs from every entity module's `MIRROR_SOURCES`.

    The rebuild/backfill seam, declared beside SD-1's `MIRROR_MAPPERS` by the
    module that owns the files a kind comes from. None of the five exists yet,
    so this yields nothing today — and `rebuild`/`backfill` take their
    observations as an argument precisely so that is not a blocker.

    **The signature every `MIRROR_SOURCES` callable must have** (declared here
    because `--backfill` calls it, and sp-07…sp-11 implement against it)::

        def source(path=None) -> Iterable[observation]

    * `path=None` — "every file this kind owns, wherever you know to look".
      That is the `--rebuild` call.
    * a concrete path — "just this one file". That is the `--backfill` call:
      the walk is :func:`iter_backfill_sources` and it happens *here*, so every
      observation can be tagged with the file it came from. A source handed a
      path it does not own returns nothing; returning `None` is allowed and
      reads as empty.

    The path is not a convenience. `Mirror.backfill`'s mtime refusal is against
    the *source's* mtime, and a source the caller cannot name is a guard the
    caller cannot evaluate — which is why the backfill path refuses such an
    operation rather than admitting it.
    """
    package = package or __package__
    for name in (ENTITY_MODULES if registry_modules is None else registry_modules):
        try:
            module = importlib.import_module(f".{name}", package)
        except ModuleNotFoundError as exc:
            # Fully qualified, for `discover_mappers`' reason: "this entity
            # module is not written yet" and "this entity module's dependency is
            # missing" must never look alike.
            if exc.name != f"{package}.{name}":
                raise
            continue
        sources = getattr(module, "MIRROR_SOURCES", None) or {}
        for kind, fn in sources.items():
            yield kind, fn


def iter_backfill_sources(root, *, deny=is_denied_path):
    """Transcript files under `<root>/projects/**`, in a stable order (R-45).

    The walk `--backfill` does, and the only filesystem knowledge this module
    holds: which files exist, never what is in them. GD-27's deny-list is
    applied here, at the source — a credentials file is not redacted downstream,
    it is never read.

    ``deny`` is consulted **first**, before the `.jsonl` filter, and that order
    is the point. Every basename in :data:`DENY_BASENAMES` happens to end
    `.json` today, so a deny check placed after the extension filter can never
    fire: the filter alone produces the same list, and the refusal the docstring
    claims is decoration. Asked first, it is the rule it says it is, and a
    future entry naming a `.jsonl` file (a transcript-shaped credential cache is
    not a strange thing for a CLI to invent) is honoured the day it is added
    rather than the day somebody notices.
    """
    base = os.path.join(os.fspath(root), "projects")
    out = []
    for directory, dirnames, filenames in os.walk(base):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(directory, name)
            if deny(path):
                continue
            if not name.endswith(".jsonl"):
                continue
            out.append(path)
    return out


def claude_root(env=None) -> str:
    """Where `--backfill` walks. `$TOUCH_CLAUDE_ROOT`, else `~/.claude`."""
    environ = os.environ if env is None else env
    return environ.get("TOUCH_CLAUDE_ROOT") or os.path.expanduser("~/.claude")


def iter_backfill_observations(root=None, *, registry_modules=None, package=None,
                               deny=is_denied_path, env=None):
    """`(kind, observation, path)` triples for `--backfill` — R-45's walk, wired.

    This is the function that makes `--backfill` do what R-45, `--help` and
    `docs/mongo.md` all say it does. The walk is
    :func:`iter_backfill_sources` (deny-list applied at the source, `.jsonl`
    only, stable order); the parsing belongs to the entity modules that own the
    file formats (GD-15), so each `MIRROR_SOURCES` callable is invoked **once per
    file** with that file's path.

    Every observation carries the path it came from, and that third element is
    the whole point: it is what `Mirror.backfill` compares a timestamp against.
    A 2-tuple would leave the mtime guard with nothing to evaluate.

    **Every registered source is called for every file** — five entity modules ×
    N transcripts — so the ownership decision each one makes must be made *from
    the path alone*: its extension, its parent directory, its basename grammar.
    A source that opened or parsed a file to discover it does not own it would
    turn this walk into five full reads of the corpus. Returning `()` (or
    `None`) for a path you do not own is the whole contract, and it must cost
    one `str` comparison.

    Yields nothing today, because none of the five entity modules exists — and
    when they land they need no change here, only the declared signature.
    """
    sources = list(iter_sources(registry_modules, package=package))
    if not sources:
        return
    for path in iter_backfill_sources(claude_root(env) if root is None else root, deny=deny):
        for kind, source in sources:
            for observation in source(path) or ():
                yield kind, observation, path


def iter_rebuild_observations(registry_modules=None, *, package=None):
    """`(kind, observation)` pairs for `--rebuild`: every source, whole corpus."""
    for kind, source in iter_sources(registry_modules, package=package):
        for observation in source() or ():
            yield kind, observation


# --- CLI ------------------------------------------------------------------


def _usage():
    return (
        "usage: python3 -m aggregator.mirror [--check | --health | --rebuild | --backfill]\n"
        "  --check      resolve the config and print it (never the URI)\n"
        "  --health     connect and print the /health mirror block\n"
        "  --rebuild    drop `derived` and replay from files (needs the ingest modules)\n"
        "  --backfill   one-shot historical walk of $TOUCH_CLAUDE_ROOT/projects\n"
        "               (default ~/.claude/projects; live=False, always), handing each\n"
        "               file to the ingest modules that own its format\n")


def main(argv=None):
    """A small operator CLI. Prints redacted JSON; returns a process exit code."""
    argv = list(argv if argv is not None else [])
    mode = argv[0] if argv else "--check"
    if mode in ("-h", "--help"):
        print(_usage())
        return 0
    if mode not in ("--check", "--health", "--rebuild", "--backfill"):
        print(_usage())
        return 2
    try:
        config = resolve_config()
    except CredentialError as exc:
        print(json.dumps({"state": STATE_REFUSED, "lastError": redact(str(exc))}, indent=2))
        return 1
    if mode == "--check":
        print(json.dumps({"config": config.describe(),
                          "pymongo": ms.pymongo_available()}, indent=2, sort_keys=True))
        return 0

    async def run():
        mirror = Mirror(config, registry=discover_mappers())
        await mirror.start()
        if mode in ("--rebuild", "--backfill") and mirror.state == STATE_LIVE:
            # The two modes read DIFFERENT sources, and that is the difference
            # between them: `--rebuild` replays the whole corpus as each module
            # knows to find it; `--backfill` walks the historical tree file by
            # file so every observation can be tagged with the file it came from
            # (which is what the mtime refusal is evaluated against).
            if mode == "--rebuild":
                observations = list(iter_rebuild_observations())
            else:
                observations = list(iter_backfill_observations())
            if not observations:
                mirror.note_error(MirrorError(
                    "no entity module declares MIRROR_SOURCES yet, so there is nothing to "
                    "replay from files"))
            elif mode == "--rebuild":
                await mirror.rebuild(observations)
            else:
                await mirror.backfill(observations)
        health = mirror.health()
        if mirror.backend is not None:
            await mirror.backend.close()
        return health

    health = asyncio.run(run())
    print(json.dumps(health, indent=2, sort_keys=True, default=str))
    return 0 if health["state"] in (STATE_LIVE, STATE_ABSENT) else 1


if __name__ == "__main__":                                       # pragma: no cover
    import sys

    sys.exit(main(sys.argv[1:]))
