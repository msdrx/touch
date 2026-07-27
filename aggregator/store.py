"""`.touch/` — Touch's own append-only event store, "touch-events-v2" (R-24).

The CLI's retention sweep deletes transcripts and whole subagent trees, so
Touch owns its history (D5). This module is that history: the **system of
record** and the crash-durable WAL. Mongo does **not** replace it (amendment
§2, R-24 row) — the mirror is a derived, rebuildable projection of these files
(GD-22), and every Mongo-side concern (scalar `stream` field, `_id` grammar,
`explain()`/IXSCAN assertions) lives in `mongo_store.py`, not here. Nothing in
this file imports pymongo, and nothing in this file may.

Record shape (D4/GD-11, one line of JSON, keys in this fixed order so a stream
is byte-stable across writers):

    {"v":2,"seq":184,"ts":"2026-07-25T03:20:00.000Z","source":"ingest",
     "provenance":"harness","kind":"agent","ref":{"agentId":"a2fc883c96ff7b837"},
     "data":{...}}

Invariants this module owns, each one testable in isolation:

* **single writer per stream**, appends `flock`'d and one `write()` per batch
  (GD-20 do-not-inherit: unlocked appends without a length cap). Concurrent
  writers are still *correct* — `seq` is re-derived inside the lock whenever
  the file grew behind our back — but they are out of contract, and the
  duplicate-key counter GD-29 exposes is how you notice one.
* **`seq` is per event-log file** and resumes from the file at boot; a cursor
  is therefore `(stream, seq)` and a bare seq is never a valid cursor (GD-11).
* **one ts format** on the wire and on disk: `…Z`, milliseconds. Readers
  normalize `Z → +00:00`; order is file line order, never a ts sort (GD-11).
* **ref union, open at the tail** (GD-11 + GD-11(d)): malformed instances of
  *known* shapes are rejected loudly; unknown shapes are retained verbatim and
  passed through. `legacy:<task>:<id8>` agent refs are exempt from the 17-hex
  rule (GD-14).
* **token records always carry all four keys** `{in,out,cached,cache_write}`,
  defaulting to 0. Deltas exist only on the WS wire (GD-25) — never here.
* **mandatory `provenance`** (GD-28), a closed five-value enum, orthogonal to
  D4's `source` channel.
* **no reduction.** There is exactly one reducer and it is server-side (GD-23,
  R-54). This module appends and replays; it never folds a stream into "current
  state". `tests/test_store.py` guards that boundary by name.

`kind` and `source` are validated as *well-formed slugs* against a documented
known-value list, not as closed enums: R-52's custom-state WAL rides this exact
append machinery with kinds like `control_intent`/`annotation`/`topology` and
`store.py` must stay unchanged for it (sub-plan sp-11). `provenance` is the one
closed enum, and the `custom-state` stream additionally refuses `harness`/
`derived` (GD-28's `$jsonSchema` pin, applied file-side where the WAL is
written).
"""

from __future__ import annotations

import datetime
import errno
import fcntl
import json
import os
import re
import threading

from . import SCHEMA_VERSION
from .tailer import Checkpoint, tail_once

__all__ = [
    "Store",
    "StoreError",
    "RefError",
    "StreamError",
    "SchemaError",
    "KNOWN_KINDS",
    "KNOWN_SOURCES",
    "PROVENANCE",
    "TOKEN_KEYS",
    "RECORD_KEYS",
    "REF_SHAPES",
    "DURABLE_STREAMS",
    "MAX_RECORD_BYTES",
    "classify_ref",
    "validate_ref",
    "normalize_tokens",
    "now_ts",
    "is_wire_ts",
    "normalize_ts",
    "cursor_key",
    "parse_cursor_key",
    "validate_stream",
    "state_root",
]

# --- vocabulary -----------------------------------------------------------

#: D4's channel enum. Open at the tail (see module docstring): unknown but
#: well-formed values are accepted so later sub-plans need not edit this file.
KNOWN_SOURCES = ("ingest", "hook", "control", "pty", "legacy")

#: D4's kinds. Also open at the tail — R-52's custom-state kinds are the
#: designed-for case.
KNOWN_KINDS = ("session", "agent", "tool", "run", "node", "token", "control", "log")

#: GD-28. Closed: five values, no sixth, orthogonal to `source`.
PROVENANCE = ("harness", "derived", "asserted", "touch", "unknown")

#: GD-11: a token record always carries all four, defaulting to 0.
TOKEN_KEYS = ("in", "out", "cached", "cache_write")

#: Fixed serialization order (D4). `stream` is deliberately absent: it is the
#: file's identity, and the scalar copy is `mongo_store.py`'s addition.
RECORD_KEYS = ("v", "seq", "ts", "source", "provenance", "kind", "ref", "data")

#: A record line is `flock`'d, so length is not an atomicity concern; the cap
#: is a memory bound on readers (GD-20 "no unlocked appends *without a length
#: cap*"). Above it, oversize fields are replaced by stubs — never dropped,
#: never raised, mirroring R-44's >8 MB rule for the Mongo side. 1 MiB leaves
#: real headroom over the 872 KB largest line in the frozen corpus.
#:
#: The cap bounds the **written line**, not one field of it: `_encode` measures
#: the encoded blob after every reduction and keeps reducing until it fits, so
#: `tailer.DEFAULT_READ_CAP` (8 MiB) is provably never reached by a line this
#: store wrote. That is what makes a `.touch/` stream safe to tail live — an
#: over-cap line cannot advance a tailer's offset, so one would take the stream
#: dark for the live view while replay still worked (GD-22/GD-30).
MAX_RECORD_BYTES = 1024 * 1024

#: Stub bounds. A stub names the keys it replaced, and key strings are
#: caller-supplied, so both the count and each key's length are capped —
#: otherwise "the stub" is just a smaller unbounded thing.
STUB_MAX_KEYS = 64
STUB_KEY_CHARS = 64

#: Streams whose loss is unrecoverable get an fsync per append. Custom state is
#: the one dataset not rebuildable from `~/.claude` (R-52), and the control
#: audit is a legal record of intents (D7); everything else can be rebuilt from
#: the harness files, so paying fsync on every ingest tick would be a pure
#: latency tax (GD-30).
DURABLE_STREAMS = ("custom-state", "control")

# Grammars. `#` is excluded from stream ids because `<stream>#<seq:012d>` is
# the event `_id`/cursor grammar (GD-24) and must stay unambiguous; `|` because
# it separates GD-24 key components; `/` and NUL because a stream id becomes a
# path component.
_STREAM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+@=,%-]{0,199}$")
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_AGENT_ID_RE = re.compile(r"^[0-9a-f]{17}$")          # full 17-hex, GD-7
_LEGACY_AGENT_RE = re.compile(r"^legacy:[^:]+:[0-9a-f]{8}$")   # GD-14 exemption
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_CURSOR_RE = re.compile(r"^(?P<stream>.+)#(?P<seq>\d{12})$")

#: The GD-11 ref union plus GD-11(d)'s two promoted members. Value =
#: (required keys, optional keys). Matching is by exact key set, so shapes
#: cannot silently absorb each other's members.
REF_SHAPES = {
    "uuid": (("uuid",), ()),
    "toolUseId": (("toolUseId",), ()),
    "agentId": (("agentId",), ()),
    "runNode": (("runId", "key", "ordinal"), ()),
    "session": (("pid", "procStart"), ()),
    # GD-11(d): the two validated additions (CUSTOMSTATE-7).
    "orchAgent": (("root", "name", "attempt"), ()),
    "legacyPlan": (("task", "plan"), ("stage", "attempt")),
}


class StoreError(Exception):
    """Base for every rejection this module makes."""


class RefError(StoreError):
    """A malformed instance of a *known* ref shape (GD-11 hard-rejection half)."""


class StreamError(StoreError):
    """An unusable stream id (unsafe as a path component or as an `_id` prefix)."""


class SchemaError(StoreError):
    """A record that violates the touch-events-v2 shape."""


# --- timestamps -----------------------------------------------------------


def now_ts() -> str:
    """The one wire/disk format: `2026-07-25T03:20:00.000Z` (GD-11).

    The aggregator supplies every `ts` (GD-11(g)); no server handler and no
    Mongo default ever invents one, and `$natural`/ObjectId time orders
    nothing.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def is_wire_ts(value) -> bool:
    return isinstance(value, str) and bool(_TS_RE.match(value))


def normalize_ts(value) -> datetime.datetime:
    """Parse a ts for comparison: `Z → +00:00`, tz-aware result (GD-11).

    Accepts the writer format and the wider ISO-8601 set found in legacy
    streams (RUNSTATE-6: mixed formats exist on disk); naive input is read as
    UTC rather than rejected, because a legacy line is history and cannot be
    fixed retroactively.

    Tolerance has a floor: a ts this function genuinely cannot parse is a
    :class:`SchemaError` like every other rejection this module makes, never a
    bare `ValueError` leaking out of `fromisoformat`. The legacy adapter (R-27)
    reads RUNSTATE-6's mixed-format streams and catches `StoreError`; a
    different exception type there is an unhandled crash on a line of history.
    """
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(text)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"unparseable ts {value!r}: {exc}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


# --- refs -----------------------------------------------------------------


def classify_ref(ref) -> str:
    """Name the GD-11 union member ``ref`` belongs to, without validating it.

    Returns ``"none"`` for an empty/absent ref (a stream-level event such as a
    `log` line), the shape name for a known key set, or ``"unknown"`` — which
    is a *retained* outcome, not an error (GD-11's open tail; GD-24 keeps such
    refs with ``kind:"unknown"`` and no ``refId``).
    """
    if not ref:
        return "none"
    if not isinstance(ref, dict):
        return "unknown"
    keys = set(ref)
    for name, (required, optional) in REF_SHAPES.items():
        req = set(required)
        if keys == req or (req <= keys and keys <= req | set(optional)):
            return name
    return "unknown"


def _require(cond, message):
    if not cond:
        raise RefError(message)


def validate_ref(ref) -> str:
    """Validate ``ref`` and return its classification.

    Hard rejection is limited to malformed instances of known shapes — the
    non-17-hex agentId and non-UUID uuid GD-11 names, plus the BSON type pins
    GD-24 fixes (``pid`` int, ``procStart`` **string**, ``ordinal`` int).
    Everything unknown passes through untouched. These rejections are
    programmer errors, not data errors: every caller is Touch's own code
    building a ref it just derived, so failing loudly here is how a key bug is
    found before it reaches a permanent store.
    """
    kind = classify_ref(ref)
    if kind in ("none", "unknown"):
        return kind
    if kind == "uuid":
        _require(_UUID_RE.match(str(ref["uuid"])), f"ref.uuid is not a UUID: {ref['uuid']!r}")
    elif kind == "toolUseId":
        _require(isinstance(ref["toolUseId"], str) and ref["toolUseId"].strip(),
                 "ref.toolUseId must be a non-empty string")
    elif kind == "agentId":
        agent = ref["agentId"]
        _require(isinstance(agent, str), "ref.agentId must be a string")
        _require(
            bool(_AGENT_ID_RE.match(agent)) or bool(_LEGACY_AGENT_RE.match(agent)),
            "ref.agentId must be 17 hex chars or a legacy:<task>:<id8> id: %r" % (agent,),
        )
    elif kind == "runNode":
        _require(isinstance(ref["runId"], str) and ref["runId"].strip(),
                 "ref.runId must be a non-empty string")
        _require(isinstance(ref["key"], str) and ref["key"].strip(),
                 "ref.key must be a non-empty string")
        # GD-24 pins ordinal to a BSON int; bool is an int in Python and is not
        # an ordinal, so it is excluded explicitly.
        _require(isinstance(ref["ordinal"], int) and not isinstance(ref["ordinal"], bool)
                 and ref["ordinal"] >= 0,
                 "ref.ordinal must be a non-negative int (GD-7/GD-24)")
    elif kind == "session":
        _require(isinstance(ref["pid"], int) and not isinstance(ref["pid"], bool) and ref["pid"] > 0,
                 "ref.pid must be a positive int")
        _require(isinstance(ref["procStart"], str) and ref["procStart"].strip(),
                 "ref.procStart must be a STRING clock-tick value (GD-24 type pin)")
    elif kind == "orchAgent":
        for field_ in ("root", "name"):
            _require(isinstance(ref[field_], str) and ref[field_].strip(),
                     f"ref.{field_} must be a non-empty string")
        _require(isinstance(ref["attempt"], int) and not isinstance(ref["attempt"], bool)
                 and ref["attempt"] >= 0,
                 "ref.attempt must be a non-negative int")
    elif kind == "legacyPlan":
        for field_ in ("task", "plan"):
            _require(isinstance(ref[field_], str) and ref[field_].strip(),
                     f"ref.{field_} must be a non-empty string")
        if "stage" in ref:
            _require(isinstance(ref["stage"], str) and ref["stage"].strip(),
                     "ref.stage must be a non-empty string when present")
        if "attempt" in ref:
            _require(isinstance(ref["attempt"], int) and not isinstance(ref["attempt"], bool)
                     and ref["attempt"] >= 0,
                     "ref.attempt must be a non-negative int when present")
    return kind


# --- tokens ---------------------------------------------------------------


def normalize_tokens(data) -> dict:
    """Fill the four token keys, keep any extra fields (GD-11).

    Missing keys default to 0 so a reader never has to distinguish "absent"
    from "zero"; non-int values are a schema error rather than a silent 0,
    because a stringly-typed token count is how an under-report starts.
    """
    out = dict(data or {})
    for key in TOKEN_KEYS:
        value = out.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaError(f"token data.{key} must be an int, got {value!r}")
        if value < 0:
            raise SchemaError(f"token data.{key} must be >= 0, got {value!r}")
        out[key] = value
    return out


# --- streams and cursors --------------------------------------------------


def validate_stream(stream) -> str:
    """Return ``stream`` if it is usable as an `_id` prefix and a path component.

    Rejects `#` (the `<stream>#<seq>` separator — GD-24), `|` (GD-24's key
    separator), path separators, `..`, NUL/control characters and leading dots.
    Percent-escaping of user-chosen components (`% # | :`) happens *before* an
    id reaches this store — that is `refs.ref_key`'s grammar (R-43), and this
    validator is what makes an unescaped one fail fast instead of writing a
    file called `run%3Alegacy` in the wrong place.

    Every `:`-separated component is checked, not just the whole string: `run:.`
    passes a naive `..` test yet names the *directory root* rather than a
    per-id directory, so two ids would collide there and `streams()` — hence any
    GD-26 rebuild that enumerates streams — would never see it. A dot-only or
    empty component is rejected loudly instead.
    """
    if not isinstance(stream, str) or not stream:
        raise StreamError("stream id must be a non-empty string")
    if not _STREAM_RE.match(stream):
        raise StreamError(f"unusable stream id: {stream!r}")
    if ".." in stream:
        raise StreamError(f"stream id may not contain '..': {stream!r}")
    for part in stream.split(":"):
        if part in ("", ".", ".."):
            raise StreamError(
                f"stream id component {part!r} is not a usable name: {stream!r}"
            )
    return stream


#: GD-24 percent-escapes `% # | :` in **user-chosen** key components. A stream
#: id can never carry `#` or `|` (`validate_stream` rejects both, because they
#: are GD-24's own separators), so the two that can still reach a key are `%`
#: and `:`. Escaping happens in a single pass in both directions, so `%2525`
#: decodes to `%25` and not to `%`.
_KEY_ESCAPES = {"%": "%25", ":": "%3A"}
_KEY_ESCAPE_RE = re.compile(r"%(25|3A)")
_KEY_UNESCAPES = {"25": "%", "3A": ":"}


def _escape_key_component(text: str) -> str:
    return "".join(_KEY_ESCAPES.get(ch, ch) for ch in text)


def _unescape_key_component(text: str) -> str:
    return _KEY_ESCAPE_RE.sub(lambda m: _KEY_UNESCAPES[m.group(1)], text)


def cursor_key(stream, seq) -> str:
    """`<stream>#<seq:012d>` — the WS cursor token, in GD-24's `_id` grammar.

    Zero padding makes lexicographic order equal numeric order, so `_id`-range
    scans and `(stream, seq)` cursors agree and both IXSCAN (LIVEFLOW-3).

    The **normative** `_id` producer is `refs.ref_key` (R-43, sub-plan sp-05);
    SD-11 is unconditional that every `_id` comes from there. This function is
    the file side's cursor token, and it emits the same grammar on purpose so
    the two cannot mean different things — including GD-24's escaping rule,
    which `validate_stream` deliberately does not apply (it *permits* a raw `:`
    or `%` inside a stream id, e.g. the user-chosen folder name in
    `run:legacy:<task>`). Only the first `:` is structural — it separates the
    Touch-owned stream kind from the id — so everything else is escaped:

        run:wf_829e6f58-b2f       -> run:wf_829e6f58-b2f      (unchanged)
        run:legacy:touch-recon    -> run:legacy%3Atouch-recon
        custom-state              -> custom-state             (no id part)

    # SD-11: refs.ref_key must round-trip this. sp-05 owns the proof (build the
    # same stream id through both and assert equality); this file owns the
    # grammar it hands over.
    """
    validate_stream(stream)
    seq = int(seq)
    if seq < 0:
        raise StoreError("seq must be >= 0")
    prefix, sep, rest = stream.partition(":")
    key = _escape_key_component(prefix) + sep + _escape_key_component(rest)
    return f"{key}#{seq:012d}"


def parse_cursor_key(cursor):
    """Inverse of :func:`cursor_key`. A bare seq is not a cursor (GD-11)."""
    match = _CURSOR_RE.match(cursor or "")
    if not match:
        raise StoreError(
            f"not a (stream, seq) cursor: {cursor!r} — a bare seq is never a valid cursor"
        )
    prefix, sep, rest = match.group("stream").partition(":")
    stream = _unescape_key_component(prefix) + sep + _unescape_key_component(rest)
    return validate_stream(stream), int(match.group("seq"))


def state_root(root=None) -> str:
    """`.touch/` root: explicit arg > `$TOUCH_STATE_DIR` > `<repo>/.touch` (D5).

    Never under `.claude/local-orchestrators/` — that is monitoring history
    protected by CLAUDE.md — and `.gitignore` already ignores `.touch/` plus
    `.touch*/` so a `TOUCH_STATE_DIR` variant cannot be committed either.
    """
    if root:
        return os.path.abspath(os.fspath(root))
    env = os.environ.get("TOUCH_STATE_DIR")
    if env:
        return os.path.abspath(env)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo, ".touch")


# Path escaping is a *filesystem* concern, independent of the `_id` grammar: a
# stream id that is already legal as an `_id` may still contain `:` or `%`,
# which we percent-encode on disk so the reverse mapping in `streams()` is
# exact.
_SAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _escape_component(text: str) -> str:
    return _SAFE_PATH_CHARS.sub(lambda m: "%%%02X" % ord(m.group(0)), text)


def _unescape_component(text: str) -> str:
    return re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), text)


class Store:
    """The `.touch/` event store. One instance per process is the intent.

    Stream id → file (D5):

    ==========================  =========================================
    ``session:<pid>-<procStart>``  ``.touch/sessions/<key>/events.jsonl``
    ``run:<runId>``                ``.touch/runs/<runId>/events.jsonl``
    ``custom-state``               ``.touch/custom-state.jsonl``
    ``control``                    ``.touch/control.jsonl``
    ==========================  =========================================

    Everything else is rejected: a stream nobody named is a wrong-target
    hazard, and GD-12's "never fall back to another task/session/stream" is the
    same rule one layer down.
    """

    #: prefix -> (relative directory, filename). A prefix-less id maps to a
    #: single file at the root.
    STREAM_KINDS = {
        "session": ("sessions", "events.jsonl"),
        "run": ("runs", "events.jsonl"),
    }
    SINGLETON_STREAMS = {
        "custom-state": "custom-state.jsonl",
        "control": "control.jsonl",
    }

    def __init__(self, root=None, *, dir_mode=0o700):
        self.root = state_root(root)
        self.dir_mode = dir_mode
        # These four caches are keyed by stream id and are never evicted. One
        # entry is a lock, three ints and a bool — a few hundred bytes — and the
        # key space is the number of streams this process has touched (sessions
        # + runs seen since boot), not the number of records. A long-lived
        # aggregator over a machine's whole `~/.claude` history is the intended
        # deployment, so the honest statement of the bound is: O(streams
        # touched), and evicting one is always safe (every entry is re-derivable
        # from the file, which is exactly what a cold `Store` does).
        self._locks = {}                    # stream -> threading.Lock
        self._next_seq = {}                 # stream -> next seq to assign
        self._last_size = {}                # stream -> file size at our last scan/append
        # stream -> "the file's last line has no trailing newline". Cached
        # alongside `_last_size` because the two are one observation: caching the
        # size without the flag is how a `cursor()`-then-`append()` sequence used
        # to skip the in-lock rescan and concatenate a record onto a killed
        # writer's partial line (silent loss in the WAL).
        self._needs_nl = {}
        self.stats = {
            "appended": 0,
            "bytes_written": 0,
            "oversize": 0,
            "torn_repairs": 0,
            "reseeks": 0,
            "bad_lines": 0,
        }

    # --- paths ------------------------------------------------------------

    def stream_path(self, stream) -> str:
        validate_stream(stream)
        if stream in self.SINGLETON_STREAMS:
            return os.path.join(self.root, self.SINGLETON_STREAMS[stream])
        prefix, _, rest = stream.partition(":")
        if not rest or prefix not in self.STREAM_KINDS:
            raise StreamError(
                f"unknown stream {stream!r}; expected one of "
                f"{sorted(self.SINGLETON_STREAMS)} or "
                f"{sorted(k + ':<id>' for k in self.STREAM_KINDS)}"
            )
        directory, filename = self.STREAM_KINDS[prefix]
        escaped = _escape_component(rest)
        if escaped in ("", ".", ".."):
            # Belt and braces beside `validate_stream`: the on-disk name is what
            # actually decides which file we open, so it gets its own check.
            raise StreamError(
                f"stream {stream!r} escapes to the directory root ({escaped!r})"
            )
        return os.path.join(self.root, directory, escaped, filename)

    def streams(self):
        """Every stream that exists on disk, sorted; the exact inverse mapping."""
        found = []
        for stream, filename in self.SINGLETON_STREAMS.items():
            if os.path.exists(os.path.join(self.root, filename)):
                found.append(stream)
        for prefix, (directory, filename) in self.STREAM_KINDS.items():
            base = os.path.join(self.root, directory)
            try:
                entries = sorted(os.listdir(base))
            except OSError:
                continue
            for entry in entries:
                if os.path.exists(os.path.join(base, entry, filename)):
                    found.append(f"{prefix}:{_unescape_component(entry)}")
        return sorted(found)

    def _lock(self, stream):
        return self._locks.setdefault(stream, threading.Lock())

    def _ensure_dir(self, path):
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, mode=self.dir_mode, exist_ok=True)

    # --- seq bookkeeping --------------------------------------------------

    def _scan_seq(self, fh):
        """``(next_seq, ends_with_newline, size)`` derived from the open file.

        Cheap on purpose: newlines are counted chunk-wise with no JSON parsing,
        and only the tail is parsed for the highest `seq` (seqs are monotonic in
        file order). ``next_seq`` is ``max(line count, highest stored seq) + 1``
        so a torn or garbage line still consumes a number and can never cause a
        duplicate — the "resumes from line count at boot" rule of R-24, made
        collision-proof.
        """
        size = os.fstat(fh.fileno()).st_size
        if size == 0:
            return 1, True, 0
        lines = 0
        fh.seek(0)
        last_byte = b""
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            lines += chunk.count(b"\n")
            last_byte = chunk[-1:]
        ends_with_newline = last_byte == b"\n"
        if not ends_with_newline:
            # A torn tail becomes a physical line the moment the repair newline
            # lands, so count it now: `seq` then stays aligned with the line
            # number even across a killed writer, and no new record can reuse
            # the number the torn line already claims.
            lines += 1
        tail_start = max(0, size - 256 * 1024)
        fh.seek(tail_start)
        tail = fh.read()
        highest = 0
        for line in reversed([l for l in tail.split(b"\n") if l.strip()]):
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            seq = record.get("seq")
            if isinstance(seq, int):
                highest = seq
                break
        return max(lines, highest) + 1, ends_with_newline, size

    def next_seq(self, stream) -> int:
        """The seq the next append to ``stream`` will use (1-based).

        Re-stats every call and rescans when the size moved, so a **reader**
        instance (a server answering "resume from here") is never stuck at its
        first observation. The cached value is only trusted while the file is
        byte-for-byte the size we last scanned — the same signal
        :meth:`append_many` uses.
        """
        validate_stream(stream)
        path = self.stream_path(stream)
        try:
            size_now = os.stat(path).st_size
        except OSError:
            size_now = 0
        cached = self._next_seq.get(stream)
        if cached is not None and self._last_size.get(stream) == size_now:
            return cached
        try:
            with open(path, "rb") as fh:
                seq, ends_with_newline, size = self._scan_seq(fh)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
            seq, ends_with_newline, size = 1, True, 0
        self._next_seq[stream] = seq
        # Remember the whole observation — size *and* newline state — so the next
        # append can skip the rescan of a file nobody touched without losing the
        # torn-tail repair it owes the next record.
        self._last_size[stream] = size
        self._needs_nl[stream] = not ends_with_newline
        return seq

    # --- writing ----------------------------------------------------------

    def _build_record(self, seq, *, kind, ref, data, source, provenance, ts, stream):
        if not isinstance(kind, str) or not _SLUG_RE.match(kind):
            raise SchemaError(
                f"kind must be a lower_snake slug, got {kind!r} "
                f"(known: {', '.join(KNOWN_KINDS)}; the set is open at the tail)"
            )
        if not isinstance(source, str) or not _SLUG_RE.match(source):
            raise SchemaError(
                f"source must be a lower_snake slug, got {source!r} "
                f"(known: {', '.join(KNOWN_SOURCES)}; the set is open at the tail)"
            )
        if provenance not in PROVENANCE:
            raise SchemaError(
                f"provenance is mandatory and one of {PROVENANCE}, got {provenance!r} (GD-28)"
            )
        allowed = self.stream_provenance(stream)
        if provenance not in allowed:
            raise SchemaError(
                f"stream {stream!r} accepts provenance {sorted(allowed)} only, "
                f"got {provenance!r} (GD-28)"
            )
        if ts is None:
            ts = now_ts()
        if not is_wire_ts(ts):
            raise SchemaError(
                f"ts must be the single writer format YYYY-MM-DDTHH:MM:SS.mmmZ, got {ts!r}"
            )
        if ref is None:
            ref = {}
        if not isinstance(ref, dict):
            raise RefError(f"ref must be a dict (possibly empty), got {type(ref).__name__}")
        validate_ref(ref)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise SchemaError(f"data must be a dict, got {type(data).__name__}")
        if kind == "token":
            data = normalize_tokens(data)
        record = {
            "v": SCHEMA_VERSION,
            "seq": seq,
            "ts": ts,
            "source": source,
            "provenance": provenance,
            "kind": kind,
            "ref": dict(ref),
            "data": data,
        }
        if tuple(record) != RECORD_KEYS:
            # Serialization order is part of the contract (a stream must be
            # byte-stable across writers), so this is a raise, not an `assert`
            # that evaporates under `python -O`.
            raise SchemaError(
                f"record key order {tuple(record)} != {RECORD_KEYS} (touch-events-v2)"
            )
        return record

    @staticmethod
    def stream_provenance(stream):
        """Which provenance values a stream accepts (GD-28's pin, file-side).

        `custom_state*` is pinned to `{asserted, touch}`: the WAL is the only
        place Touch authors state, and a `harness` claim written there would be
        a forged harness fact. Mirror-fed streams keep the full enum (they carry
        `harness`/`derived` facts and, for legacy lines that cannot be
        attributed, `unknown`).
        """
        if stream in ("custom-state", "control"):
            return frozenset({"asserted", "touch"})
        return frozenset(PROVENANCE)

    @staticmethod
    def _dumps(value) -> bytes:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    @classmethod
    def _stub_for(cls, value) -> dict:
        """R-44's stub shape for one oversize field: what it was, not its bytes.

        Both dimensions are bounded — at most ``STUB_MAX_KEYS`` keys, each cut
        to ``STUB_KEY_CHARS`` characters — because the keys are caller-supplied
        strings of arbitrary length, and an unbounded stub is not a stub.
        """
        stub = {"oversize": True, "bytes": len(cls._dumps(value))}
        if isinstance(value, dict):
            stub["keys"] = [str(k)[:STUB_KEY_CHARS] for k in sorted(value)[:STUB_MAX_KEYS]]
        return stub

    @classmethod
    def _encode(cls, record):
        """``(record, blob, oversize)`` with ``len(blob) + 1 <= MAX_RECORD_BYTES``.

        Reduces the **encoded blob**, not one field of the payload, and
        re-measures after every step. Measuring `data` alone was a hole with a
        designed-for carrier: GD-11's open tail requires unknown `ref` shapes to
        be retained verbatim, so an ingest arm passing a harness subtree through
        as a `ref` is the intended path, and it could write a line larger than
        the tailer's read cap — permanently blinding the live tail for that
        stream while replay still worked.

        The two reducible fields are stubbed **biggest first**, re-measuring
        after each, so the field that was not the problem survives: a 10 MB
        `ref` no longer destroys a two-key `data` beside it, and vice versa. On
        a tie the payload (`data`) goes first — identity is worth more than
        content. Identity fields (`v`, `seq`, `ts`, `source`, `provenance`,
        `kind`) are never touched, so the record stays in the stream and stays
        attributable.
        """
        blob = cls._dumps(record)
        if len(blob) + 1 <= MAX_RECORD_BYTES:
            return record, blob, False

        # Never drop, never raise: keep identity + provenance, stub the bulk
        # (the same shape R-44 uses for >8 MB documents).
        record = dict(record)
        order = sorted(
            ("data", "ref"),
            key=lambda name: (-len(cls._dumps(record.get(name, {}))), name != "data"),
        )
        for name in order:
            record[name] = cls._stub_for(record.get(name, {}))
            blob = cls._dumps(record)
            if len(blob) + 1 <= MAX_RECORD_BYTES:
                return record, blob, True

        # Both stubs' `keys` lists are bounded but not tiny; drop them and the
        # remainder is identity plus two fixed-shape stubs — a few hundred bytes,
        # so this branch always fits.
        for name in ("data", "ref"):
            record[name] = {"oversize": True, "bytes": record[name]["bytes"]}
        blob = cls._dumps(record)
        if len(blob) + 1 > MAX_RECORD_BYTES:
            # Unreachable with any sane MAX_RECORD_BYTES (the fields left are a
            # slug, an enum, a fixed-width ts and two ints). It is a raise rather
            # than a silent over-cap write because `follow()`/`read_all()` and
            # every live tailer are entitled to the bound this method promises.
            raise SchemaError(
                f"record cannot be reduced under MAX_RECORD_BYTES={MAX_RECORD_BYTES}: "
                f"{len(blob) + 1} bytes with identity fields only"
            )
        return record, blob, True

    def append(self, stream, *, kind, provenance, data=None, ref=None,
               source="ingest", ts=None, durable=None) -> dict:
        """Append one record; return it (with its assigned `seq`)."""
        return self.append_many(
            stream,
            [{"kind": kind, "provenance": provenance, "data": data, "ref": ref,
              "source": source, "ts": ts}],
            durable=durable,
        )[0]

    def append_many(self, stream, specs, *, durable=None) -> list:
        """Append several records in one `flock`'d `write()`.

        Batching is how the 250 ms ingest tick stays inside its budget: one
        lock, one syscall, one optional fsync for a whole tick's events.

        The batch is all-or-nothing: `_build_record` rejects a malformed spec
        before any byte reaches the file, and nothing in ``self.stats`` moves
        until `write()` has returned. Counters that describe *the file* — the
        torn-tail repair, the oversize stubs, the re-seeks — are accumulated
        locally and folded in afterwards, so a rejected batch cannot leave
        behind a repair that was never written (GD-29 leans on these numbers to
        notice a second writer; a double-counting counter lies).
        """
        validate_stream(stream)
        specs = list(specs)
        if not specs:
            return []
        path = self.stream_path(stream)
        existed = os.path.exists(path)
        self._ensure_dir(path)
        if durable is None:
            durable = stream in DURABLE_STREAMS
        written = []
        with self._lock(stream):
            with open(path, "a+b") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    delta = {"reseeks": 0, "torn_repairs": 0, "oversize": 0}
                    size = os.fstat(fh.fileno()).st_size
                    seq = self._next_seq.get(stream)
                    # Trust the cached newline state only for the file we cached
                    # it from; the branch below re-derives both together.
                    ends_with_newline = not self._needs_nl.get(stream, False)
                    if seq is None or self._last_size.get(stream) != size:
                        # First append of this process, or the file grew behind
                        # our back (another writer — out of contract, still
                        # handled): re-derive inside the lock so `seq` cannot
                        # collide.
                        if seq is not None:
                            delta["reseeks"] += 1
                        seq, ends_with_newline, _ = self._scan_seq(fh)
                    payload = bytearray()
                    if not ends_with_newline:
                        # Torn tail from a killed writer: terminate the partial
                        # line instead of concatenating onto it. The garbage
                        # line stays — this store never deletes history; readers
                        # skip it and count it (`stats["bad_lines"]`).
                        payload += b"\n"
                        delta["torn_repairs"] += 1
                    for spec in specs:
                        record = self._build_record(
                            seq,
                            kind=spec["kind"],
                            ref=spec.get("ref"),
                            data=spec.get("data"),
                            source=spec.get("source", "ingest"),
                            provenance=spec["provenance"],
                            ts=spec.get("ts"),
                            stream=stream,
                        )
                        record, blob, oversize = self._encode(record)
                        if oversize:
                            delta["oversize"] += 1
                        payload += blob + b"\n"
                        written.append(record)
                        seq += 1
                    fh.seek(0, os.SEEK_END)
                    fh.write(payload)
                    fh.flush()
                    if durable:
                        os.fsync(fh.fileno())
                        if not existed:
                            self._fsync_dir(os.path.dirname(path))
                    self._next_seq[stream] = seq
                    self._last_size[stream] = os.fstat(fh.fileno()).st_size
                    self._needs_nl[stream] = False   # we always end on a newline
                    for name, value in delta.items():
                        self.stats[name] += value
                    self.stats["appended"] += len(written)
                    self.stats["bytes_written"] += len(payload)
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return written

    def _fsync_dir(self, directory):
        """Commit the directory entry that names a just-created durable file.

        `os.fsync(file)` makes the *contents* durable; the entry created by
        `open(..., "a+b")`/`os.makedirs` is a separate metadata write, so
        without this the first `custom-state`/`control` record can be lost with
        the file after a power cut — and that stream is the one thing a rebuild
        from `~/.claude` cannot reconstruct (R-52/D7).

        Called only when the file did **not** exist before this append, which is
        the only moment a new entry appears — so this costs one extra fsync per
        durable file, ever, not one per append. Deliberately *not* memoized per
        directory: `custom-state.jsonl` and `control.jsonl` share `.touch/`, and
        a set of "directories already synced" would skip the second file's
        entry, which is precisely the entry at risk. Walks up to the state root
        so a nested durable stream's freshly created parent is committed too.

        Best-effort by design: some filesystems refuse `O_RDONLY` fsync on a
        directory, and a durable append must not fail because the *extra*
        guarantee is unavailable.
        """
        root = os.path.normpath(self.root)
        current = os.path.normpath(directory) if directory else ""
        while current:
            self._fsync_one_dir(current)
            if current == root:
                break
            parent = os.path.dirname(current)
            if parent == current or not current.startswith(root + os.sep):
                break
            current = parent

    @staticmethod
    def _fsync_one_dir(directory):
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    # --- reading ----------------------------------------------------------

    def _parse(self, lines):
        out = []
        for line in lines:
            text = line.text.strip() if hasattr(line, "text") else str(line).strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except ValueError:
                self.stats["bad_lines"] += 1     # torn/garbage line: skip, count, never crash
                continue
            if isinstance(record, dict):
                out.append(record)
            else:
                self.stats["bad_lines"] += 1
        return out

    def read_all(self, stream) -> list:
        """Every parseable record, in file line order (never a ts sort — GD-11)."""
        path = self.stream_path(stream)
        return self._parse(self._read_lines(path))

    def _read_lines(self, path):
        # `escalate_oversize_line`: a replay must return every complete record,
        # so a line longer than the tailer's read cap is read whole rather than
        # silently truncating the replay to zero records.
        ck = Checkpoint()
        lines = []
        while True:
            res = tail_once(path, ck, skip_while_compacting=False,
                            escalate_oversize_line=True)
            lines.extend(res.lines)
            if res.missing or not res.more:
                return lines
            ck = res.checkpoint

    def read_since(self, stream, seq: int) -> list:
        """Records with `seq` strictly greater than ``seq`` — cursor resume.

        The cursor is `(stream, seq)`; this is the per-stream half. A caller
        resuming a multi-stream subscription holds one of these per stream
        (GD-11: a bare seq is never a valid cursor).

        Reads the stream and filters: correct, and O(stream) rather than
        O(remaining). A bounded default replay window and a seek-to-cursor fast
        path are R-55's (`server.py`, sub-plan sp-12) — they need the wire
        contract this store deliberately does not know about.
        """
        return [r for r in self.read_all(stream) if isinstance(r.get("seq"), int)
                and r["seq"] > seq]

    def read_from_cursor(self, cursor) -> list:
        stream, seq = parse_cursor_key(cursor)
        return self.read_since(stream, seq)

    def follow(self, stream, checkpoint: Checkpoint = None):
        """Incremental read: ``(records, checkpoint, reset)``.

        Same checkpoint semantics as `tailer.tail_once` (rotation and in-place
        truncation both force a re-ingest, `reset=True`), which is what makes a
        `.touch/` stream cheap to serve to a live WS client: per tick the server
        reads only what was appended (GD-30).

        Escalates an oversize line for the same reason :meth:`read_all` does:
        this signature reports records, not tail reasons, so a stalled line would
        be indistinguishable from "nothing appended". Store-written lines are
        capped at ``MAX_RECORD_BYTES`` (1 MiB, well under the tailer's read cap)
        — :meth:`_encode` measures the *encoded blob* after every reduction and
        raises rather than write over the cap, which is what makes that sentence
        true — so the escalation can only ever fire on a foreign writer's line.
        """
        res = tail_once(self.stream_path(stream), checkpoint, skip_while_compacting=False,
                        escalate_oversize_line=True)
        return self._parse(res.lines), res.checkpoint, res.reset

    def cursor(self, stream) -> str:
        """The cursor naming the last record currently in ``stream``.

        Re-derived from the file whenever it grew (see :meth:`next_seq`), so a
        non-writing instance reports the stream's real end rather than its own
        first observation of it.
        """
        return cursor_key(stream, max(0, self.next_seq(stream) - 1))
