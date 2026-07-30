"""`aggregator/server.py` — the HTTP/WS front door (R-30, R-31, R-55).

Three items, one file, because they are one feature: a route table nobody can
fall through (R-30), the read endpoints that table points at (R-31), and the
socket contract those endpoints share a cursor grammar with (R-55).

What this module is NOT
----------------------
**It never derives state.** There is exactly one reducer and it is
`agents.reduce` (GD-23/R-54); every liveness verdict, badge, attempt label and
run close on the wire came out of it. This file joins, projects, paginates and
serializes — a search for "if idle >" or "state =" here should find nothing but
the reducer's output being copied. `tests/test_server_core.py` guards that by
name, together with the token rule below.

**It never computes a token delta.** GD-25 puts deltas on the wire only, and
R-55 makes the *absolute* model a hard precondition of `(stream, seq)` resume:
a client that reconnects mid-stream and replays a partial run of deltas ends up
with silently low counters, and nothing in the UI can notice. So Touch's socket
carries the store's absolute four-key token records verbatim, coalesced but
never differenced — the two halves ("resume from a cursor" and "absolute
tokens") are a package and neither ships alone. The static guard in
`tests/test_server_core.py` asserts no subtraction on a token field ever
appears in this file.

**It never reaches for Mongo.** GD-22 makes the in-memory reduction the single
source for `/ws` and every `/api/*` route, so the page keeps working with
`mirror: absent|down`. The one Mongo-flavoured route, `/api/query` (R-55),
takes an *injected* query source and, with none, answers from the same memory
model and says so in `source`. This file imports no driver, lazily or
otherwise (GD-21: only `mongo_store.py` and `mirror.py` may).

Security posture (GD-13, extended by GD-27)
-------------------------------------------
* binds **127.0.0.1** by default; `0.0.0.0` is an explicit opt-in flag
  (`--open`) documented with the `sbx ports` flow;
* a **per-boot 256-bit token** on every route except `/health`, compared with
  `hmac.compare_digest`, accepted as `Authorization: Bearer`, `X-Touch-Token`
  or `?token=` (a browser cannot set a header on a WS handshake or a
  navigation, so the query-string arm is not a convenience — it is the only
  way the page can authenticate at all). The token is injected into the page at
  serve time so it need not stay in the address bar;
* an **Origin/Host allowlist enforced at the WS upgrade**, 403 otherwise;
* a static `(method, route)` dict with a default 404 — **never** a fallback to
  another task/session/stream (GD-12; the monitor's silent `STATE_DIR` fallback
  is the wrong-target hazard this rule exists to kill). A path segment after a
  registered route is a different route and 404s;
* `safe_artifact_path` containment, the CSP sandbox and `nosniff` copied
  verbatim from `monitor_server.py` (GD-20's copy-verbatim list);
* nothing here prints, serves or logs a credential (GD-27). `/health` is the
  one unauthenticated route and publishes `mirror.health()` — already redacted
  at the source — plus tailer liveness and parse-failure counters.

The wire contract (R-55), restated here because sp-13 restates it verbatim
-------------------------------------------------------------------------
Frames are JSON text messages. Every one carries `live`:

    {"type":"hello","live":false,"mode":"replay","streams":[...],
     "currentRun":"run:wf_x","window":500,"reducerVersion":"1",
     "cursors":{"<stream>":<seq>},"resumed":true,
     "from":12,"fromApplied":false,"fromRejected":null,
     "cursorsRejected":[...],"streamsRejected":[...],
     "streamsUnobserved":[...]}
    {"type":"event","live":false,"stream":"run:wf_x","seq":12,
     "cursor":"run:wf_x#000000000012","record":{...}}
    {"type":"mode","live":true,"mode":"tail",         <- the ONE boundary
     "cursors":{"<stream>":<seq>},"oldest":{"<stream>":<seq>},
     "truncated":{"<stream>":true}}
    {"type":"event","live":true, ...}
    {"type":"anchors","live":true,"stream":"<stream>",
     "oldest":<seq>,"truncated":true}      <- a backfill AFTER the boundary
    {"type":"subscribed","live":true,"cursors":{"<stream>":<seq>},
     "accepted":{"<stream>":<seq>},"rejected":[...],
     "backfilled":{"<stream>":<count>}}    <- follows the frames it re-sent
    {"type":"tick","live":true,"ts":"…Z"}            <- idle keepalive marker

**The load-older anchors are on `mode`, and are absent from `hello`.**
`oldest`/`truncated` are facts *about a replay*, so they cannot exist before
one: hello goes out first, each stream's window is chosen as the replay runs,
and only the boundary frame knows what was cut. Publishing them on hello
anyway would publish `{}` — and a page that reads an always-empty
`hello.truncated`, concludes nothing was cut and never renders the "load
older" affordance is a silently wrong UI, where a missing key is a loud one.
So hello carries neither. hello's `cursors` is the *client-supplied* position
(`?cursor=`/`subscribe`), empty on a fresh connect; `mode`'s `cursors` is
where the replay actually ended, and it is the pair a client resumes from.

**A backfill that happens after `mode` carries its own `anchors` frame.**
`mode` is sent exactly once — sp-13 keys the replay→tail transition off it, so
it may never be repeated or revised — but a backfill can happen later: a
stream born after the boundary (an ingest pass discovering a new transcript)
and a `subscribe` rewind are both painted `live:false` by the same code path.
Their window is bounded by the same rules, so they cut records off the wire
too, and an anchor recorded in session state that no frame publishes is the
`hello.truncated` failure reached through a later door: a page rendering a
stream whose first 55 records were never sent, with nothing to click. Every
post-boundary backfill therefore publishes `{"type":"anchors"}` naming its
stream, its `oldest`, and whether anything was cut, immediately before the
frames it describes.

* **bounded default replay**: the newest run's stream in full, every other
  stream's last `window` records (default 500) — "current run or last N
  events, whichever larger", made concrete. `?from=<seq>` (with a single
  stream selector) replays explicitly from there, still bounded by
  `MAX_REPLAY_EVENTS`; whatever the window cut off is reported per stream as
  `oldest`/`truncated` **on the `mode` frame**, so the page's "load older"
  button knows it has work and where to start (`/api/events?stream=&before=`);
* **a handshake parameter is never silently dropped**: after the 101 there is
  no status code left to refuse with, so every parameter that could not be
  used is *named on hello* instead — `fromApplied:false` plus the raw
  `fromRejected` for a `?from=` that did not parse or did not pair with
  exactly one stream (three cases the client can tell apart),
  `cursorsRejected:[raw,…]` for a malformed `?cursor=` (the well-formed ones
  in the same handshake still apply — one typo may not cost a client its other
  resume positions), and `streamsRejected:[raw,…]` for a malformed `?stream=`.
  If `?stream=` was given and *nothing* survived validation the socket serves
  **no** stream rather than every stream: widening a failed selector into "all
  targets" is GD-12's wrong-target fallback arriving through the query parser.
  A selector that is well-formed but names a stream this store has never seen
  *is* served — that is how a client watches a run before it starts — but it
  is named in `streamsUnobserved` and it can never become `currentRun`:
  labelling the header of the page with an id nobody has observed is the
  made-up fact `/api/events` answers 404 to, wearing a socket instead;
* **resume**: the client sends its last `(stream, seq)` pair(s) — as
  `?cursor=<stream>#<seq:012d>` (repeatable) on the handshake, or as a
  `{"type":"subscribe","cursors":{...}}` message — and gets exactly the
  records after them: no duplicates and no gap for the newest
  `MAX_REPLAY_EVENTS` (5000) records of each stream. A cursor further back
  than that is *not* served in full — the replay is capped and the shortfall
  is declared as `truncated`/`oldest`, on the `mode` frame during the replay
  and on an `anchors` frame after it, which is what the page walks with
  `/api/events?stream=&before=`. The counters after a reconnect equal a full
  replay's because the tokens are absolute. `subscribe` is the same mechanism
  and not a weaker one: a pair *behind* the session's position re-delivers
  that range as `live:false` backfill before the ack, and a pair *ahead* of it
  is refused with a reason on the ack rather than adopted — an ack that
  reports a position the socket never sent is how a client is talked into
  skipping records, and the cursor it publishes afterwards would carry that
  lie into every reconnect;
* **replayed frames paint once**: `live:false` is on the frame, so sp-13 never
  animates a backfill burst (its source guard asserts the class is not
  attached to a non-live frame);
* **the tail is bounded as well as the replay**: one tick emits at most
  `MAX_TICK_EVENTS` frames per stream and carries the rest into the next tick,
  so an ingest catching up after a restart — the normal way a burst is born —
  arrives as a sequence of bounded writes rather than one synchronous storm
  per connected client (GD-30's bounded queue: the replay was capped from the
  start and the tail was the one queue in this file with no bound). The cursor
  stops where the cap fell, so the remainder continues with no gap and no
  duplicate;
* **token frames coalesce ≥1 s** even though ingest ticks at 250 ms
  (GD-30) — the *last* absolute record per `(stream, ref)` in the window wins
  and the ones it superseded are dropped, which is only safe because they are
  absolute. While a frame is held, the published cursor for its stream stays
  *behind* it: a client that adopted a cursor past a held record and then
  reconnected would skip it permanently (the hold dies with the session), and
  for a finished agent's last token record that is a stale count forever. The
  cost is that a reconnect inside the ≤1 s window may repaint the frames after
  the held one — visible, and dedupable by `(stream, seq)`, which is the trade
  this whole file is built on.

Recorded handoffs, and which of them this file takes
----------------------------------------------------
Two earlier sub-plans named "the read API (sp-12)" as the home of work they
could not do themselves. Stated plainly rather than left to be discovered:

* **taken** — `ingest.py`'s cross-file `usage_conflicts` counter. `ReadModel`
  carries a `counters` dict a caller raises into, and `/health` publishes it,
  so an ingest pass's conflict count has somewhere installation-wide to land.
* **not taken** — `custom_state.py`'s head/slot **driver** (`head_write` →
  `Backend.guarded_update`, `bind_slot`, `SlotTable.sweep`). That work needs a
  database handle and a *write* tick; this file is a read surface with neither
  (it holds no `Backend`, runs no ingest loop, and GD-21 keeps the driver out
  of every module but two). Putting a write path here would also break GD-22's
  direction of travel — the API reads the memory model, it does not feed it.
  The handoff therefore stands open for whichever sub-plan owns the ingest
  tick, exactly as `custom_state.py` records it: until then `custom_state` is
  written by nothing, every slot stays `pending`, and this API serves that
  honestly (`/api/query?collection=slots` returns what is there, which is
  nothing) rather than presenting an unwritten head as fact.

Composition
-----------
`ReadModel` is the injected read side: the mirror's memory model
(`{collection: {_id: doc}}` — `mongo_store.apply_operations`' shape, which is
also `mirror.MemoryBackend`'s state), a `store.Store` for `.touch/` streams, an
optional object with a `health()` (the mirror), and the tailers whose liveness
`/health` reports. `Api` turns a `(method, route, query, headers)` into a
`Response` with no socket in sight; `WsSession` is the wire state machine with
no socket either; `HttpServer` is the only part that owns a transport, and it
is deliberately the thinnest of the three.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from dataclasses import dataclass, field

from . import SCHEMA_VERSION, __version__
from . import agents as agents_mod
from . import ingest as ingest_mod
from . import legacy as legacy_mod
from . import mongo_store as ms
from . import paths
from . import refs
from . import store as store_mod
from . import ws

__all__ = [
    "DEFAULT_HOST", "OPEN_HOST", "DEFAULT_PORT", "LEGACY_PORT", "TOKEN_BYTES",
    "DEFAULT_REPLAY_EVENTS", "MAX_REPLAY_EVENTS", "MAX_TICK_EVENTS",
    "TOKEN_COALESCE_SECONDS",
    "TICK_SECONDS", "KEEPALIVE_SECONDS", "ARTIFACT_EXTS", "ID_PATTERNS",
    "ServerError", "HttpError", "Response", "header_value",
    "FILE_CSP", "NO_REFERRER", "STATUS_TEXT", "status_text",
    "valid_id", "optional_id", "one", "flag", "positive_int",
    "Auth", "OriginPolicy", "safe_artifact_path", "artifact_listing",
    "inject_token", "json_default", "json_body",
    "ReadModel", "Api", "ROUTES", "READ_ROUTES", "CONTROL_ROUTES",
    "route_table", "OPEN_ROUTES",
    "WsSession", "TokenCoalescer", "replay_window",
    "HttpServer", "main",
]

# --- constants ------------------------------------------------------------

#: GD-13: loopback by default, `0.0.0.0` only behind `--open`.
DEFAULT_HOST = "127.0.0.1"
OPEN_HOST = "0.0.0.0"

#: GD-13's reserved ports: 8931 is the legacy monitor's, 8932 is Touch's.
#: Reserved, not occupied — this server binds 8932 and never 8931.
DEFAULT_PORT = 8932
LEGACY_PORT = 8931

#: 256 bits, per boot (GD-13). `token_urlsafe` takes *bytes* of entropy.
TOKEN_BYTES = 32

#: R-55's bounded default replay window, per stream.
DEFAULT_REPLAY_EVENTS = 500
#: The ceiling an explicit `?from=` may not exceed — "explicit" bounds the
#: *start*, not the size, and an unbounded replay is a memory bug with a
#: query string in front of it.
MAX_REPLAY_EVENTS = 5000
#: The same ceiling on the *tail*: a bulk append (an ingest catching up after a
#: restart) is drained over several ticks instead of becoming one unbounded
#: write storm on every connected socket. GD-30 asks for a bounded queue and
#: this is the bound on the only queue the wire owns.
MAX_TICK_EVENTS = MAX_REPLAY_EVENTS

#: GD-30: token frames coalesce to ≥1 s even though ingest ticks at 250 ms.
TOKEN_COALESCE_SECONDS = 1.0

#: The socket's poll interval. GD-30's tailer budget, so the wire cannot be
#: fresher than the ingest that feeds it and pretending otherwise just burns
#: CPU.
TICK_SECONDS = 0.25
#: WS keepalive ping period (the monitor's ~20 s, kept).
KEEPALIVE_SECONDS = 20.0

#: Bounded request head. A header block larger than this is a client bug or an
#: attack; either way it is not a Touch request.
MAX_HEAD_BYTES = 64 * 1024
HEAD_TIMEOUT_SECONDS = 10.0

#: Page sizes. `limit` is clamped, never trusted.
DEFAULT_PAGE = 200
MAX_PAGE = 1000

#: A spilled tool result is served whole up to here, then truncated with the
#: flag set (the largest real one is 872 KB).
MAX_TOOLRESULT_BYTES = 4 * 1024 * 1024

#: Copied verbatim from `monitor_server.py` (GD-20).
ARTIFACT_EXTS = {".md", ".html", ".htm"}

#: How long a computed reduction may be reused. Read-time liveness (GD-23) is
#: a function of `now()`, so the honest bound on staleness is one ingest tick —
#: recomputing the whole reduction per request on a 40-agent run is the
#: 320 ms/tick mistake GD-30 records, one layer up. Tests pass `reduce_ttl=0`.
REDUCE_TTL_SECONDS = 0.25


# --- errors ---------------------------------------------------------------


class ServerError(Exception):
    """A server-side misconfiguration — never raised in response to a request."""


class HttpError(Exception):
    """A response with a status. Handlers raise it; :class:`Api` renders it.

    Carrying the status on the exception is what keeps the "unknown id ⇒ 404,
    malformed id ⇒ 400" distinction (R-31) in the *validator* rather than
    duplicated in eight handlers, where the eighth would eventually get it
    wrong and answer 200 with an empty list — a wrong-target answer wearing a
    success code (GD-12).
    """

    def __init__(self, status: int, message: str, *, headers=None):
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)
        self.headers = dict(headers or {})


# --- responses ------------------------------------------------------------

#: Reason phrase per status. Deliberately longer than what this server's routes
#: emit today (201/204/405/409/411/412/415/422 have no route here yet): the
#: monitoring server's memory routes need exactly those, keeping one complete
#: table is cheaper than two half-tables with two fallbacks, and a table that
#: lists only what today's caller happens to need is how the bug in
#: :func:`status_text` was written in the first place. No machine check ties the
#: two servers' tables together, so this is not claimed as a GD-20 twin — the
#: only pinned twin in this pair is `FILE_CSP`.
STATUS_TEXT = {
    200: "OK", 201: "Created", 204: "No Content",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
    411: "Length Required", 412: "Precondition Failed",
    413: "Payload Too Large", 415: "Unsupported Media Type",
    422: "Unprocessable Content", 426: "Upgrade Required",
    500: "Internal Server Error", 503: "Service Unavailable",
}

#: Status class ⇒ what an unnamed status in that class is called.
_STATUS_CLASS = {1: "Informational", 2: "Success", 3: "Redirection",
                 4: "Client Error", 5: "Server Error"}


def status_text(status: int) -> str:
    """The reason phrase for `status` — and **never** `"OK"` for one we do not
    name (SERVER-8).

    The table used to be short enough to lie. Both readers spelled their own
    fallback: `head_bytes` wrote `STATUS_TEXT.get(status, "OK")`, so the first
    route to answer `409` would have sent `HTTP/1.1 409 OK` — a conflict wearing
    a success reason phrase — and `Response.error` wrote `"error": "Error"`,
    which says nothing. A reason phrase is advisory to every HTTP client, so
    neither breaks a parser; both mislead the human reading a capture, which is
    the only audience a reason phrase has. An unnamed status therefore falls
    back to its **class**, which is derived from the code itself and so cannot
    contradict it.
    """
    status = int(status)
    named = STATUS_TEXT.get(status)
    if named:
        return named
    return _STATUS_CLASS.get(status // 100, "Unknown Status")


#: RFC 7230's `token` — the only shape a header *name* may have. A name is
#: always written by this file, so one that fails is a code bug and raises.
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
#: Everything a header *value* may legally contain: visible ASCII, space, tab.
_HEADER_VALUE_BAD = re.compile(r"[^\t\x20-\x7e]")


def header_value(value) -> str:
    """One header value, made safe to concatenate into a response head.

    A value here can come from a transcript — `X-Touch-Basename` is derived
    from an agent-authored path — and a POSIX filename may contain CR, LF or a
    non-latin-1 character. Concatenated unchecked, the first splits the
    response (an attacker-authored body served from this origin *without* the
    CSP sandbox two lines above it) and the second raises `UnicodeEncodeError`
    in `head_bytes`, outside every handler's try, dropping the connection with
    no answer at all.

    So the sanitizer lives here rather than at any one call site: every header
    this server will ever add passes through it, including the ones a later
    change adds without reading this docstring. CR/LF/NUL are removed and any
    other byte outside visible-ASCII+SP+TAB becomes `?`.
    """
    text = "" if value is None else str(value)
    return _HEADER_VALUE_BAD.sub("?", text.replace("\r", "").replace("\n", "")).strip()


@dataclass
class Response:
    """One HTTP response, headers included, ready to serialize.

    `nosniff` is unconditional and `Cache-Control: no-store` is too: every body
    this server produces is a live observation, and a cached one is a lie with
    a timestamp on it.
    """

    status: int = 200
    body: bytes = b""
    content_type: str = "application/json"
    headers: dict = field(default_factory=dict)

    @classmethod
    def json(cls, payload, status: int = 200, headers=None):
        return cls(status=status, body=json_body(payload),
                   content_type="application/json", headers=dict(headers or {}))

    @classmethod
    def text(cls, message, status: int = 200, headers=None):
        body = message.encode("utf-8") if isinstance(message, str) else bytes(message)
        return cls(status=status, body=body,
                   content_type="text/plain; charset=utf-8", headers=dict(headers or {}))

    @classmethod
    def error(cls, status: int, message: str, headers=None):
        """A JSON error body. The message is the *reason*, never a traceback.

        Every 4xx this server produces says which rule was broken, because the
        alternative — a bare status — sends a reader to read this file instead
        of the message.
        """
        return cls.json({"error": status_text(status),
                         "status": status, "message": message},
                        status=status, headers=headers)

    def head_bytes(self) -> bytes:
        # `Content-Type` goes through `header_value` like every other value.
        # Every content type is server-authored today, so this is
        # defence-in-depth — but the point of putting the sanitizer in this
        # method was that *every* header the server will ever emit passes
        # through it, and one exempt field is how that stops being true.
        lines = [f"HTTP/1.1 {self.status} {status_text(self.status)}",
                 f"Content-Type: {header_value(self.content_type)}",
                 f"Content-Length: {len(self.body)}",
                 "X-Content-Type-Options: nosniff",
                 "Cache-Control: no-store",
                 "Connection: close"]
        for name, value in self.headers.items():
            if not _HEADER_NAME_RE.match(str(name)):
                raise ServerError(f"illegal response header name {name!r}")
            lines.append(f"{name}: {header_value(value)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin1")

    def to_bytes(self) -> bytes:
        return self.head_bytes() + self.body


# --- json -----------------------------------------------------------------


def json_default(value):
    """The one serializer for shapes `json` refuses.

    `datetime` renders in GD-11's single wire format (`…Z`, milliseconds) — the
    same one `store.now_ts` writes, so a ts that made a round trip through the
    mirror (stored as a BSON Date beside its `tsRaw`) comes back off the API
    spelled exactly as it went in. `set`/`tuple` become arrays, `bytes` become
    their UTF-8 text with replacement (a body that is not text is still
    renderable, and a 500 in a JSON encoder is the worst possible place to
    learn about one).
    """
    if isinstance(value, datetime.datetime):
        moment = value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        moment = moment.astimezone(datetime.timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def json_body(payload) -> bytes:
    """Compact JSON bytes. No `ensure_ascii`: the page decodes UTF-8."""
    return json.dumps(payload, default=json_default, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


# --- id validation (ONE shared helper — R-31) -----------------------------

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_AGENT_RE = re.compile(r"^[0-9a-f]{17}$")
_LEGACY_AGENT_RE = re.compile(r"^legacy:[^:|#%]{1,120}:[0-9a-f]{8}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@=+-]{0,127}$")
_TOOL_USE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SESSION_KEY_RE = re.compile(r"^[0-9]{1,10}-[0-9]{1,20}$")
_SEQ_RE = re.compile(r"^[0-9]{1,12}$")
#: The same bound as a number, for the socket's JSON `subscribe` (where a seq
#: arrives already parsed and `store.cursor_key`'s `%012d` has no ceiling of its
#: own). A seq above this cannot be spelled as a `(stream, seq)` cursor at all.
MAX_SEQ = 10 ** 12 - 1
#: How much of an unusable, client-supplied cursor is echoed back on the ack.
MAX_REJECT_ECHO = 120
#: The `_id` half of the timeline's `(lineNo, _id)` cursor. Deliberately wide —
#: it is echoed back from a previous page and only ever compared, never used to
#: build a path or a key — but bounded and control-character-free all the same.
_CURSOR_ID_RE = re.compile(r"^[!-~]{1,256}$")

#: kind -> (predicate, human description). Every id on every route goes through
#: :func:`valid_id`; adding a route means naming its id kind here, which is the
#: point — an endpoint that validates its ids inline is an endpoint that
#: validates them differently.
ID_PATTERNS = {
    "session": (lambda v: bool(_UUID_RE.match(v)), "a session uuid"),
    "uuid": (lambda v: bool(_UUID_RE.match(v)), "a record uuid"),
    "agent": (lambda v: bool(_AGENT_RE.match(v)) or bool(_LEGACY_AGENT_RE.match(v)),
              "a 17-hex agentId (or a legacy:<task>:<id8> ref)"),
    "run": (lambda v: bool(_NAME_RE.match(v)), "a runId"),
    "task": (lambda v: bool(_NAME_RE.match(v)), "a task folder name"),
    "toolUseId": (lambda v: bool(_TOOL_USE_RE.match(v)), "a toolUseId"),
    "sessionKey": (lambda v: bool(_SESSION_KEY_RE.match(v)), "a <pid>-<procStart> key"),
    "stream": (lambda v: _is_stream(v), "a .touch/ stream id"),
    "seq": (lambda v: bool(_SEQ_RE.match(v)), "a non-negative seq"),
    "collection": (lambda v: v in ms.collection_names(), "a known collection"),
}


def _is_stream(value) -> bool:
    try:
        store_mod.validate_stream(value)
    except store_mod.StoreError:
        return False
    return True


def valid_id(kind: str, value, *, what=None) -> str:
    """Validate ``value`` as an id of ``kind``; raise 400 if it is malformed.

    **Malformed is 400, unknown is 404** (R-31), and the split lives here so it
    cannot drift: this function never looks anything up, so it can only ever
    answer the syntactic question. A handler that gets a value back from here
    still has to find it, and 404s when it cannot — never falling back to
    another id (GD-12).
    """
    check = ID_PATTERNS.get(kind)
    if check is None:                                   # a routing bug, not a request
        raise ServerError(f"no id pattern registered for {kind!r}")
    predicate, described = check
    label = what or kind
    if not isinstance(value, str) or not value or not predicate(value):
        raise HttpError(400, f"malformed {label}: expected {described}")
    return value


def optional_id(kind: str, value, *, what=None):
    """:func:`valid_id` for a parameter that may be absent (None passes)."""
    if value is None or value == "":
        return None
    return valid_id(kind, value, what=what)


def one(query: dict, name: str, default=None):
    """The single value of a query parameter; a repeat is a 400.

    `?session=a&session=b` has no correct answer and the tempting ones (first
    wins, last wins) are both a silent wrong target — the same hazard GD-12
    names, arriving through the parser instead of the router.
    """
    values = query.get(name)
    if not values:
        return default
    if len(values) > 1:
        raise HttpError(400, f"{name} given {len(values)} times; it takes exactly one value")
    return values[0]


def flag(query: dict, name: str) -> bool:
    """A boolean query parameter: present-and-not-`0`/`false`/`no` is True.

    The bare `?full` (no `=`) is the hand-typed form and it means True, which
    is why `parse_head` keeps blank values: "given and empty" has to reach this
    function to be answered, and answering it False would make the natural
    spelling of the parameter a silent no-op.
    """
    value = one(query, name)
    if value is None:
        return False
    return value.strip().lower() not in ("0", "false", "no")


def positive_int(query: dict, name: str, default: int, *, maximum: int, minimum=0):
    """A clamped integer parameter in `[minimum, maximum]`; a non-number is 400.

    ``minimum`` is 1 for every page size: `limit=0` parses, and an empty page
    that still reports `hasMore: true` with no cursor is a client loop that
    never terminates — a wrong answer with a 200 on it, which is the same
    hazard GD-12 names one layer up.
    """
    raw = one(query, name)
    if raw is None:
        return default
    if not _SEQ_RE.match(raw.strip()):
        raise HttpError(400, f"{name} must be a non-negative integer, got {raw!r}")
    return max(int(minimum), min(int(raw), maximum))


# --- auth (GD-13) ---------------------------------------------------------

#: The only routes served without a token. `/health` is on the list because an
#: operator must be able to ask "is it up and is the mirror down?" without
#: holding a secret; nothing on it is a credential (GD-27) and nothing on it is
#: a transcript.
OPEN_ROUTES = frozenset({"/health"})


class Auth:
    """A per-boot 256-bit bearer token, compared in constant time.

    Three carriers, and the query string is not a shortcut: a browser cannot
    set a header on a `new WebSocket(...)` handshake or on a top-level
    navigation, so `?token=` is the only way the page can authenticate the two
    requests that matter most. The trade — a token in a URL is a token in the
    shell history — is bounded by the token being **per boot**: it is worthless
    the moment the process exits, and it is never written anywhere but
    `.touch/server.json` (0600, GD-27's handling parity with `mongo.json`).
    """

    HEADER = "x-touch-token"
    QUERY = "token"

    def __init__(self, token=None, *, open_routes=OPEN_ROUTES):
        self.token = token or secrets.token_urlsafe(TOKEN_BYTES)
        self.open_routes = frozenset(open_routes)
        self.rejections = 0

    def is_open(self, route: str) -> bool:
        return route in self.open_routes

    @staticmethod
    def presented(headers: dict, query: dict):
        """The token the request carries, from any of the three carriers."""
        auth = (headers or {}).get("authorization", "")
        if auth[:7].lower() == "bearer ":
            return auth[7:].strip()
        header = (headers or {}).get(Auth.HEADER)
        if header:
            return header.strip()
        values = (query or {}).get(Auth.QUERY) or []
        return values[0] if values else None

    def check(self, route: str, headers: dict, query: dict) -> bool:
        """True when the request may proceed. Constant-time, always.

        The comparison runs even when no token was presented — against the
        empty string rather than short-circuiting — so a missing token and a
        wrong one take the same path. `compare_digest` on `str` requires ASCII
        on both sides, and a token from a query string is arbitrary user text,
        so both sides are encoded first.
        """
        if self.is_open(route):
            return True
        presented = self.presented(headers, query) or ""
        ok = hmac.compare_digest(presented.encode("utf-8", "replace"),
                                 self.token.encode("utf-8"))
        if not ok:
            self.rejections += 1
        return ok

    @staticmethod
    def challenge() -> Response:
        return Response.error(401, "a per-boot token is required on every route but /health",
                              headers={"WWW-Authenticate": 'Bearer realm="touch"'})


# --- Origin/Host allowlist (GD-13, enforced at the WS upgrade) ------------


class OriginPolicy:
    """The WS upgrade's Origin/Host allowlist. Everything else is 403.

    Three rules, in order, and the third is the one that makes a default
    install safe without configuration:

    1. an Origin on the configured allowlist passes;
    2. a **missing** Origin passes when ``allow_missing_origin`` (the default):
       browsers always send one on a WS handshake, so an absent Origin means a
       non-browser client — which still had to present the token. Refusing it
       would break `curl`/tests without closing anything a browser could open;
    3. otherwise the Origin must be **same-origin with the Host header** — the
       page served by this server, talking back to it. That rule needs no
       configuration and is exactly the set of pages that could have been
       handed the token at serve time.

    The Host header is checked against ``hosts`` when that set is non-empty
    (the loopback default), which is the DNS-rebinding half: a page on
    `http://evil.example` resolving to 127.0.0.1 arrives with
    `Host: evil.example` and fails rule 3 anyway, but failing it *by name* is
    the check an operator can read in a log.
    """

    def __init__(self, *, hosts=(), origins=(), allow_missing_origin=True):
        self.hosts = frozenset(h.lower() for h in hosts)
        self.origins = frozenset(o.rstrip("/").lower() for o in origins)
        self.allow_missing_origin = bool(allow_missing_origin)
        self.rejections = 0

    @classmethod
    def default(cls, host: str, port: int, *, origins=(), hosts=(),
                allow_missing_origin=True):
        """The policy for a bind. An open bind has no host allowlist by default.

        Binding `0.0.0.0` is the explicit opt-in of GD-13 and the operator
        reaches it through whatever address the sandbox published, so a
        derived host allowlist would just be a list of guesses. Rule 3 still
        applies: an open bind is not an open Origin policy.
        """
        names = set(hosts)
        if host not in (OPEN_HOST, "::", ""):
            for candidate in (host, "localhost", "127.0.0.1", "[::1]"):
                names.add(candidate)
                names.add(f"{candidate}:{port}")
        derived = set(origins)
        for name in list(names):
            if ":" in name and not name.startswith("["):
                derived.add(f"http://{name}")
        return cls(hosts=names, origins=derived,
                   allow_missing_origin=allow_missing_origin)

    @staticmethod
    def _authority(origin: str):
        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
        return parsed.netloc.lower()

    def refusal(self, headers: dict):
        """None when the upgrade may proceed, else a one-line reason (403)."""
        headers = headers or {}
        host = (headers.get("host") or "").strip().lower()
        origin = (headers.get("origin") or "").strip()
        if self.hosts and host not in self.hosts:
            self.rejections += 1
            return f"Host {host or '(absent)'!s} is not on the allowlist"
        if not origin or origin.lower() == "null":
            if self.allow_missing_origin:
                return None
            self.rejections += 1
            return "an Origin header is required"
        if origin.rstrip("/").lower() in self.origins:
            return None
        authority = self._authority(origin)
        if authority is not None and host and authority == host:
            return None
        self.rejections += 1
        return f"Origin {origin!r} is not allowed"


# --- artifacts: copied verbatim from monitor_server.py (GD-20) ------------


def artifact_listing(state_dir: str) -> list:
    """List report HTMLs + .md notes under a task folder, report(s) first.

    Verbatim from `monitor_server.task_artifacts` (GD-20's copy-verbatim list),
    including the bounds: hidden entries and `__pycache__` skipped, paths
    task-relative with forward slashes, walk capped at depth 4 / 300 files so a
    runaway folder cannot stall the endpoint.
    """
    out = []
    base = os.path.realpath(state_dir)
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(dirpath, base)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth >= 4:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d != "__pycache__")
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if fn.startswith(".") or ext not in ARTIFACT_EXTS:
                continue
            try:
                st = os.stat(os.path.join(dirpath, fn))
            except OSError:
                continue
            rel = fn if rel_dir == "." else os.path.join(rel_dir, fn)
            out.append({"path": rel.replace(os.sep, "/"),
                        "kind": "note" if ext == ".md" else "report",
                        "size": st.st_size, "mtime": st.st_mtime})
            if len(out) >= 300:
                return sorted(out, key=lambda a: (a["kind"] != "report", a["path"]))
    out.sort(key=lambda a: (a["kind"] != "report", a["path"]))
    return out


def safe_artifact_path(state_dir: str, rel: str):
    """Absolute path for a task-relative artifact, or None if not servable.

    Verbatim from `monitor_server.safe_artifact_path` (GD-20): extension
    whitelist + realpath containment in the task dir, so a hostile ``?path=``
    (.. traversal, absolute path, or a symlink pointing outside) can never read
    beyond the task folder.
    """
    if not rel or os.path.splitext(rel)[1].lower() not in ARTIFACT_EXTS:
        return None
    base = os.path.realpath(state_dir)
    full = os.path.realpath(os.path.join(base, rel))
    if not full.startswith(base + os.sep):
        return None
    return full if os.path.isfile(full) else None


#: The CSP every served file gets. `sandbox` with **no** `allow-scripts`, the
#: same value the monitoring server sends — today as an inline
#: `b"Content-Security-Policy: sandbox\r\n"` literal on its `/` response, not as
#: a constant this one could import. GD-20 calls the two servers verbatim twins,
#: so the equality is meant to be machine-checked; that check belongs to the
#: memory-feature test pass and does not exist yet (`tests/monitoring/` today
#: only asserts the monitor's own literal and that no `b"…"` line carries
#: `allow-scripts`). Until it lands, this is two files agreeing on a value
#: separately — which is why the value is stated here as a value, in one place a
#: use site cannot re-spell (SECURITY-4).
#:
#: The sandbox alone puts a report in an opaque origin, cut off from this server
#: and from same-origin reads; it does **not** stop a script in the report from
#: reading its own `location.search` and POSTing the token somewhere — and
#: agent-generated reports are exactly the documents nobody audited. The monitor
#: refused `allow-scripts` for that reason from the start and said so in a
#: comment; this file was copied from it without the CSP and kept the flag for no
#: recorded reason. The cost is honest and small: a report whose interactivity
#: needs script no longer gets it here. The gain is that one opened report can no
#: longer become token-exfil → memory write → persistent injection.
#:
#: `nosniff` (in `head_bytes`, on every response) stops a `.md` from being
#: executed as HTML; `Referrer-Policy: no-referrer` travels with this constant at
#: every use site, because the URL that fetched the document carries the token in
#: its query string and a `Referer` header would hand it to whatever the document
#: links to (SECURITY-5).
FILE_CSP = "sandbox"

#: The second header of that pair. Named once so a use site cannot set the CSP
#: and forget this — they are one decision, not two.
NO_REFERRER = "no-referrer"


def inject_token(html: bytes, token: str) -> bytes:
    """Inject the per-boot token into the page at serve time (GD-13).

    Two arms, and they escape differently — sp-13 needs to know which one it is
    coding against:

    * **the placeholder arm** (the contract): `__TOUCH_TOKEN__` anywhere in the
      document is replaced with the **raw** token, verbatim and unquoted. So
      sp-13 must put the placeholder only where a raw URL-safe token is already
      valid — inside a JS string literal, a query string, an attribute value —
      and never where the surrounding syntax would have to escape it. That is
      sound because `secrets.token_urlsafe` emits `[A-Za-z0-9_-]` only, which
      needs no escaping in any of those positions;
    * **the fallback arm** (a page that predates the convention, or a
      hand-written one): a `<script>` defining `window.TOUCH_TOKEN` is inserted
      directly after `<head>`, or prepended when there is no head. Here the
      token *is* JSON-encoded, so a value containing a quote or a `<` cannot
      break out of the string — this arm builds the syntax itself, so it does
      not get to lean on the alphabet.
    """
    literal = json.dumps(token)
    if b"__TOUCH_TOKEN__" in html:
        return html.replace(b"__TOUCH_TOKEN__", token.encode("utf-8"))
    tag = f"<script>window.TOUCH_TOKEN={literal};</script>".encode("utf-8")
    lowered = html.lower()
    head = lowered.find(b"<head")
    if head >= 0:
        close = lowered.find(b">", head)
        if close >= 0:
            return html[:close + 1] + tag + html[close + 1:]
    return tag + html


# --- the read model -------------------------------------------------------


def _ts_key(value):
    """A sortable, type-safe key for a ts field that may be a Date, str or None."""
    if isinstance(value, datetime.datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
        return (1, moment.astimezone(datetime.timezone.utc).isoformat())
    if isinstance(value, str) and value:
        return (1, value.replace("Z", "+00:00"))
    return (0, "")


def _int_or(value, default=0):
    return value if isinstance(value, int) and not isinstance(value, bool) else default


#: How many times a snapshot retries a mapping that changed under it.
SNAPSHOT_ATTEMPTS = 8


def _snapshot(mapping) -> dict:
    """A shallow copy of a dict another thread may be writing to.

    Every handler runs on a `to_thread` worker while the ingest tick writes the
    same state on the event loop, so a plain `.values()` walk races
    `dict.__setitem__` and raises *"dict changed size during iteration"* — a
    500 on the sidebar under exactly the load GD-22 promises to survive. A
    copy is the cheapest fix that needs no cooperation from the writer:
    `dict(d)` on an exact dict is one C-level copy that cannot observe a
    mutation, and the retry covers the case where ``state`` holds a mapping
    that is not an exact dict. Exhausting the retries is a 503 with a reason,
    never a 500 with a `RuntimeError` in it.
    """
    if not isinstance(mapping, dict):
        return {}
    for _ in range(SNAPSHOT_ATTEMPTS):
        try:
            return dict(mapping)
        except RuntimeError:
            time.sleep(0)
    raise HttpError(503, "the read model is being rewritten faster than it can be "
                         "read; retry")


@dataclass
class ReadModel:
    """Everything the API reads, injected — GD-22's memory-authoritative side.

    ``state`` is `{collection: {_id: doc}}`, `mongo_store.apply_operations`'
    model and `mirror.MemoryBackend`'s state: one dict, shared with whatever is
    ingesting, so a tick's writes are visible to the next request without a
    copy or a notification. Nothing here writes it.

    That sharing is deliberate and it has one consequence a reader must not
    have to discover: the writer runs on the event loop, every handler runs on
    a `to_thread` worker, and **no reader iterates the shared mapping
    directly**. :meth:`bucket` hands out a snapshot (:func:`_snapshot`) and
    :meth:`lookup` does the single-key reads that would otherwise pay for a
    copy they do not need. The bound this buys is one *generation*: a response
    reflects the state as of the moment it was copied, which is what the
    250 ms ingest tick already made the honest bound anyway.

    ``store`` is the `.touch/` event store the socket replays and tails.
    ``mirror`` is anything with a `health()` (the real `mirror.Mirror`, or
    None ⇒ `mirror: absent`). ``tailers`` maps a name to a `tailer.Tailer` (or
    to a plain dict, for a caller whose tailing lives elsewhere) and is what
    `/health` reports per-stream liveness from.
    """

    state: dict = field(default_factory=dict)
    store: object = None
    mirror: object = None
    tailers: dict = field(default_factory=dict)
    claude_root: str = None
    tasks_root: str = None
    query_source: object = None
    idle_limit: int = agents_mod.IDLE_LIMIT_SECONDS
    reduce_ttl: float = REDUCE_TTL_SECONDS
    #: Counters a caller can raise (`usage_conflicts` from an ingest pass —
    #: `ingest.py`'s recorded sp-12 handoff) so `/health` can publish them.
    counters: dict = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    _cached: object = None
    _cached_at: float = -1.0

    # --- collections ------------------------------------------------------

    def bucket(self, collection: str) -> dict:
        """One collection, as a snapshot safe to iterate off the loop."""
        return _snapshot(self.state.get(collection))

    def lookup(self, collection: str, key):
        """One document by `_id`, without copying the collection to find it.

        `dict.get` on the shared mapping is a single atomic read, so the O(1)
        path stays O(1): copying a 100 000-record collection to answer "is this
        uuid present" would be the cure being worse than the race.
        """
        got = self.state.get(collection)
        if not isinstance(got, dict) or not isinstance(key, str):
            return None
        return got.get(key)

    def sizes(self) -> dict:
        """`{collection: len}` — counts only, no walk over any document."""
        out = {}
        for name, value in _snapshot(self.state).items():
            if isinstance(value, dict):
                out[name] = len(value)
        return dict(sorted(out.items()))

    def state_snapshot(self) -> dict:
        """One generation of the whole state, for the reducer to walk safely."""
        return {name: (_snapshot(value) if isinstance(value, dict) else value)
                for name, value in _snapshot(self.state).items()}

    def reduction(self, *, now=None):
        """The reducer's output, recomputed at most once per `reduce_ttl`.

        The reduction is *the* derived state (GD-23) and this is the only place
        the API obtains it, so "the server never derives" is checkable by
        reading the callers of this method. The reducer walks the state deeply,
        so it is handed a generation snapshot rather than the live mapping.
        """
        if now is not None or self.reduce_ttl <= 0:
            return agents_mod.reduce(self.state_snapshot(), now=now,
                                     idle_limit=self.idle_limit)
        age = time.monotonic() - self._cached_at
        if self._cached is None or age >= self.reduce_ttl:
            self._cached = agents_mod.reduce(self.state_snapshot(),
                                             idle_limit=self.idle_limit)
            self._cached_at = time.monotonic()
        return self._cached

    # --- joins the handlers share ----------------------------------------

    def session_doc(self, session_id: str):
        """The `sessions` document naming ``session_id``, or None.

        Both arms of R-46's tagged union are searched by *field*, never by
        reconstructing an `_id`: a sessionId may live in a `hist:` document, in
        a `live:` document's `sessionIds[]` after promotion, or in both (the
        promotion leaves the historical document queryable, `promotedTo` set).
        Preferring the live document is not a guess — it is the promotion's
        stated direction.
        """
        historical = None
        for key, doc in self.bucket("sessions").items():
            if not isinstance(doc, dict):
                continue
            if session_id in (doc.get("sessionIds") or []):
                if key.startswith("live:"):
                    return doc
                historical = historical or doc
        return historical

    def session_stream(self, session_id: str):
        """The `.touch/` stream id carrying ``session_id``'s events, or None.

        A historical session has none: `.touch/` streams are keyed
        `session:<pid>-<procStart>` (D5) and a session Touch only ever read
        from disk has no process. That is a fact to report, not an error and
        not a fallback to some other stream (GD-12).
        """
        doc = self.session_doc(session_id)
        if doc is None:
            return None
        key = doc.get("_id") or ""
        if isinstance(key, str) and key.startswith("live:"):
            return "session:" + key[len("live:"):]
        return None

    def records_of(self, session_id: str) -> list:
        return [doc for doc in self.bucket("records").values()
                if isinstance(doc, dict) and doc.get("sessionId") == session_id]

    def stream_meta_of(self, session_id: str) -> list:
        return [doc for doc in self.bucket("stream_meta").values()
                if isinstance(doc, dict) and doc.get("sessionId") == session_id]

    def run_doc(self, run_id: str):
        return self.lookup("runs", refs.run_key(run_id))

    def nodes_of(self, run_id: str) -> list:
        return [doc for doc in self.bucket("run_nodes").values()
                if isinstance(doc, dict) and doc.get("runId") == run_id]

    def agent_doc(self, agent_id: str):
        return self.lookup("agents", agent_id)

    # --- health -----------------------------------------------------------

    @staticmethod
    def target_hash(path):
        """A stable, opaque name for a tailer's target — never the path itself.

        `/health` is the one unauthenticated route, and a tailer's real path is
        `~/.claude/projects/<cwd-slug>/<sessionId>.jsonl`: the machine's home
        directory, the directory Claude was started in, and the session uuid,
        to anybody who can reach the port. None of that is what AUDIT-15 asks
        for. A hash keeps the only property an operator actually uses — "these
        two rows are the same target, that one changed" — and publishes no
        observation. The basename is *not* published either: for a session
        transcript the basename **is** the session uuid.
        """
        if not isinstance(path, str) or not path:
            return None
        return hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()[:12]

    def tailer_health(self) -> list:
        """Per-tailer liveness + parse-failure counters (R-30 / AUDIT-15).

        AUDIT-15's rule is "a tailer whose target is gone exits, never polls
        forever", so the interesting field is `missing`: a tailer reporting it
        is one whose owner should have retired it, and publishing the fact is
        how an operator sees a poll loop that did not. What is *not* here is
        where the target lives — see :meth:`target_hash`.
        """
        out = []
        for name in sorted(_snapshot(self.tailers)):
            entry = self.tailers.get(name)
            if isinstance(entry, dict):
                row = dict(entry)
                row.setdefault("name", name)
                row["target"] = self.target_hash(row.pop("path", None))
                out.append(row)
                continue
            result = getattr(entry, "last_result", None)
            out.append({
                "name": name,
                "target": self.target_hash(getattr(entry, "path", None)),
                "reason": getattr(entry, "last_reason", None),
                "missing": bool(getattr(result, "missing", False)),
                "alive": not bool(getattr(result, "missing", False)),
                "stalled": bool(getattr(entry, "stalled", False)),
                "resets": _int_or(getattr(entry, "resets", 0)),
                "linesRead": _int_or(getattr(entry, "lines_read", 0)),
                "bytesRead": _int_or(getattr(entry, "bytes_read", 0)),
                "oversizeLines": _int_or(getattr(entry, "oversize_lines", 0)),
                "escalations": _int_or(getattr(entry, "escalations", 0)),
            })
        return out

    def parse_failures(self) -> int:
        """Every "we saw a line and could not use it" counter, summed.

        The store's `bad_lines` plus whatever the caller raised into
        ``counters`` (an ingest pass's `skipped` totals). One number on
        `/health` beside the per-tailer detail, because the question an
        operator asks first is "is anything being dropped".
        """
        total = _int_or((getattr(self.store, "stats", {}) or {}).get("bad_lines"))
        for name, value in self.counters.items():
            if name.startswith("parse") or name.endswith("_errors"):
                total += _int_or(value)
        return total

    def mirror_health(self) -> dict:
        """R-45's block, served verbatim; `absent` when there is no mirror.

        `mirror.Mirror.health()` is the authority and this does not reshape it
        (`docs/mongo.md` publishes that exact field list, and
        `tests/test_mirror.py` asserts the two are equal — a field renamed here
        would make the documented contract false). A mirror that raises while
        being asked is reported as `down` with the reason, because `/health`
        answering 500 over an optional database is the GD-22 violation the
        block exists to prevent.
        """
        if self.mirror is None:
            return {"state": "absent", "lastError": None,
                    "notes": ["no mirror configured (GD-22: the live view does not need one)"],
                    "queued": 0, "dropped": 0, "tolerated_dups": 0,
                    "lease": {}, "backend": None, "db": None, "counters": {}}
        try:
            payload = self.mirror.health()
        except Exception as exc:                                    # noqa: BLE001
            return {"state": "down", "lastError": f"{type(exc).__name__}",
                    "notes": ["mirror.health() raised"], "queued": 0, "dropped": 0,
                    "tolerated_dups": 0, "lease": {}, "backend": None, "db": None,
                    "counters": {}}
        return payload if isinstance(payload, dict) else {"state": "unknown"}


# --- projections ----------------------------------------------------------

#: Fields of a `records` document the timeline serves without `?full=1`. The
#: body is excluded by *size*, not by policy: the corpus holds an 872 KB line
#: and a page of 200 of those is a 170 MB response.
RECORD_FIELDS = ("_id", "sessionId", "type", "ts", "tsRaw", "lineNo", "byteOffset",
                 "parentUuid", "agentId", "toolUseId", "toolUseIds", "persistedOutput",
                 "retracted", "retractedGen", "gen", "provenance", "oversize", "bytes")

STREAM_META_FIELDS = ("_id", "sessionId", "lineNo", "type", "render", "ts", "tsRaw",
                      "messageId", "byteOffset", "parseError", "provenance")

SESSION_FIELDS = ("_id", "sessionIds", "cwd", "slugs", "class", "firstTs", "lastTs",
                  "pid", "procStart", "promotedTo", "sources", "registry")


def _row_key(doc) -> tuple:
    """The timeline's sort key, which is also its cursor.

    One function, called by both, because a cursor that is a *prefix* of the
    sort key is a cursor that skips whatever the prefix cannot distinguish —
    which is exactly how a `lineNo`-only cursor lost records that shared a line
    number with a subagent file's.
    """
    return (_int_or(doc.get("lineNo"), 0), str(doc.get("_id")))


def _project(doc, fields, *, extra=None):
    out = {name: doc[name] for name in fields if name in doc}
    if extra:
        out.update(extra)
    return out


def _session_payload(doc) -> dict:
    """One sidebar row. `kind` is the `_id` prefix, not a re-derivation."""
    key = doc.get("_id") or ""
    payload = _project(doc, SESSION_FIELDS)
    payload["id"] = key
    payload["kind"] = "live" if key.startswith("live:") else "historical"
    payload["transcriptless"] = not (doc.get("sources") or [])
    return payload


def _node_payload(doc, derived) -> dict:
    """A `run_nodes` observation joined to the reducer's verdict for it.

    The observation and the conclusion stay in separate sub-objects on purpose:
    sp-13 renders `derived` and shows `observed` on demand, and a page that
    cannot tell the two apart is the one that eventually re-derives (GD-23).
    """
    return {
        "id": doc.get("_id"),
        "observed": _project(doc, ("_id", "runId", "key", "ordinal", "journalSeq",
                                   "agentId", "resultSeen", "result", "startedAt",
                                   "endedAt", "attempt", "label", "provenance")),
        "derived": derived,
    }


def _agent_payload(doc, derived) -> dict:
    payload = {
        "id": doc.get("_id"),
        "observed": _project(doc, ("_id", "agentType", "model", "spawnDepth",
                                   "description", "toolUseId", "runId", "sessions",
                                   "files", "firstTs", "lastTs", "resultSeen",
                                   "resultTs", "labels", "name", "root", "parent",
                                   "unconventional", "provenance")),
        "derived": derived,
        # R-48's reader contract: fragments are recombined and chain-ordered by
        # the module that owns the storage split, never by a reader.
        "fragments": [dict(item) for item in agents_mod.fragments_of(doc)],
    }
    spawn = doc.get("spawn")
    if isinstance(spawn, dict):
        payload["spawn"] = dict(spawn)
    return payload


def _task_payload(reduction) -> dict:
    """One legacy task folder (GD-14 kinds), from `legacy.Reduction`.

    Every re-label the legacy reducer applied travels with the row
    (`derivedFromLegacy`, the relabel reason, "closed — no verdict"), because
    D13 is satisfied by *rendering* the derivation, and a row that lost its
    provenance on the way through the API cannot render it.
    """
    archive = reduction.archive
    plans = {}
    for name, plan in (reduction.plans or {}).items():
        plans[name] = {
            "plan": plan.plan, "title": plan.title, "badge": plan.badge,
            "label": plan.label, "detail": plan.detail,
            "agentDetail": plan.agent_detail,
            "derivedFromLegacy": bool(plan.derived_from_legacy),
            "relabel": plan.relabel, "plansTotal": plan.plans_total,
            "duplicates": plan.duplicates,
            "conflictingTerminals": [list(item) for item in (plan.conflicting or ())],
        }
    nodes = [{
        "key": node.key, "plan": node.plan, "stage": node.stage,
        "ordinal": node.ordinal, "agentId": node.agent_id, "label": node.label,
        "state": node.state, "detail": node.detail, "started": node.started,
        "ended": node.ended, "tokens": node.tokens,
        "derivedFromLegacy": bool(node.derived_from_legacy),
        "relabel": node.relabel, "unconventional": bool(node.unconventional),
        "flags": list(node.flags or ()),
    } for node in (reduction.nodes or ())]
    return {
        "task": reduction.task, "taskId": reduction.task_id,
        "runId": reduction.run_id, "kind": reduction.kind,
        "archive": ({"label": archive.label, "state": archive.state,
                     "path": archive.path} if archive is not None else None),
        "plans": plans, "nodes": nodes,
        "tokens": [{"plan": t.plan, "stage": t.stage, "agentId": t.agent_id,
                    "label": t.label, "ts": t.ts, "tokens": t.tokens,
                    "folded": t.folded} for t in (reduction.tokens or ())],
        "stats": dict(reduction.stats or {}),
        "notes": list(reduction.notes or ()),
    }


# --- the wire (R-55) ------------------------------------------------------


def replay_window(records, *, limit=DEFAULT_REPLAY_EVENTS, from_seq=None, whole=False):
    """R-55's bounded window over ONE stream's records. Pure.

    ``from_seq`` (the explicit `?from=`) is exclusive and still bounded by
    ``limit``: "explicit" bounds where a replay *starts*, never how big it is.
    ``whole`` is the "current run" arm — a run's own stream replays entire, so
    the freshest thing on the page is never half-drawn.

    Returns ``(window, truncated, oldest)``: the records to send, whether
    anything older was cut, and the seq of the oldest record actually sent —
    the "load older" anchor, published on the `mode` frame for a replay and on
    an `anchors` frame for a backfill that happens after it. A page that shows
    a truncation marker without a cursor to click is a dead end, so the two are
    always published together and by the frame that can know them.
    """
    ordered = [r for r in records if isinstance(r, dict)]
    if from_seq is not None:
        ordered = [r for r in ordered if _int_or(r.get("seq"), -1) > from_seq]
    limit = max(0, min(int(limit), MAX_REPLAY_EVENTS))
    if whole and from_seq is None:
        window = ordered[-MAX_REPLAY_EVENTS:] if len(ordered) > MAX_REPLAY_EVENTS else ordered
    else:
        window = ordered[-limit:] if limit and len(ordered) > limit else (ordered if limit else [])
    truncated = len(window) < len(ordered)
    oldest = _int_or(window[0].get("seq"), None) if window else None
    return window, truncated, oldest


class TokenCoalescer:
    """GD-30's ≥1 s token coalescing. Absolute records only — never a delta.

    Holds the newest token record per `(stream, ref)` and releases it when the
    window has elapsed for that key. Dropping the superseded ones is only sound
    because each is the *whole* count (GD-25): dropping one delta would lose
    tokens permanently, which is the failure R-55 pairs with resume.

    Nothing is ever lost by holding: :meth:`due` releases whatever is pending
    once the window passes, so the last observation of a stream that went quiet
    still lands one second later rather than at the next append.
    """

    def __init__(self, window=TOKEN_COALESCE_SECONDS):
        self.window = float(window)
        self._pending = {}          # key -> record
        self._sent_at = {}          # key -> monotonic-ish timestamp of last release
        self.coalesced = 0          # records superseded while pending

    @staticmethod
    def key_of(stream, record):
        ref = record.get("ref")
        if isinstance(ref, dict):
            token = "|".join(f"{name}={ref[name]}" for name in sorted(ref))
        else:
            token = ""
        return (stream, token)

    def offer(self, stream, record, now):
        """Take one token record; return it when it may go out now, else None."""
        key = self.key_of(stream, record)
        last = self._sent_at.get(key)
        if last is None or (now - last) >= self.window:
            self._sent_at[key] = now
            if key in self._pending:
                self._pending.pop(key)
            return record
        if key in self._pending:
            self.coalesced += 1
        self._pending[key] = record
        return None

    def due(self, now) -> list:
        """Every held record whose window has now elapsed, in stream order."""
        out = []
        for key in sorted(self._pending, key=lambda k: (k[0], k[1])):
            last = self._sent_at.get(key)
            if last is None or (now - last) >= self.window:
                out.append((key[0], self._pending.pop(key)))
                self._sent_at[key] = now
        return out

    @property
    def pending(self) -> int:
        return len(self._pending)

    def pending_floor(self, stream):
        """The lowest seq still held for one stream, or None if nothing is.

        A published cursor may not pass this: the hold lives in *this*
        session's coalescer and dies with the socket, so a client that adopted
        a cursor past a held record and reconnected would never be sent it
        again. For the last token record of a finished agent that is a stale
        count on the page forever, and nothing in the UI can notice — the
        failure this module's docstring opens with.

        A held record with no usable seq is *skipped* rather than floored to 0:
        defaulting it to 0 gave a floor of -1, which no cursor can ever be
        below, so one malformed record from a foreign writer would freeze the
        stream's published cursor until the coalescer released it. Store-written
        records always carry a seq, which is what makes this defensive rather
        than load-bearing — and is exactly why it must not be the arm that
        stops the cursor.
        """
        seqs = []
        for (held_stream, _), record in self._pending.items():
            if held_stream != stream:
                continue
            seq = _int_or(record.get("seq"), None)
            if seq is not None:
                seqs.append(seq)
        return min(seqs) if seqs else None


def _is_token_record(record) -> bool:
    return isinstance(record, dict) and record.get("kind") == "token"


class WsSession:
    """The socket contract as a state machine over records — no socket here.

    Life cycle, and the mode switch happens exactly once:

        hello()            -> the handshake frame (mode "replay", live false)
        replay()           -> the bounded window, every frame live:false
        switch()           -> {"type":"mode","live":true} — the boundary
        tick(now)          -> live frames since the cursors, tokens coalesced
        subscribe(msg)     -> a resume: backfill frames, then the ack, at any
                              point after the handshake

    ``cursors`` is the client's `(stream, seq)` state and is the whole of the
    session's position: it means "everything up to here has been *delivered*",
    so a reconnect that hands the same pairs back gets the records after them.
    It is advanced only by a frame that actually went out, and never past a
    record the coalescer is still holding. A bare seq is never accepted —
    GD-11 — which is why this is a dict and not an int.

    ``streams`` is a *selection*, and `None` (no selector) is a different thing
    from `[]` (a selector that matched nothing): the first serves the whole
    store, the second serves nothing. Collapsing them would turn a client's
    failed target into every target — GD-12's wrong-target fallback.
    ``cursors_rejected``/``streams_rejected`` carry the raw handshake values
    that could not be used, so `hello` can name them (there is no status code
    left after the 101).
    """

    def __init__(self, model, *, cursors=None, from_seq=None, streams=None,
                 window=DEFAULT_REPLAY_EVENTS, coalesce=TOKEN_COALESCE_SECONDS,
                 cursors_rejected=None, streams_rejected=None, from_rejected=None):
        self.model = model
        self.window = max(0, min(int(window), MAX_REPLAY_EVENTS))
        self.cursors = dict(cursors or {})
        self.from_seq = from_seq
        self.selected = None if streams is None else tuple(streams)
        self.cursors_rejected = list(cursors_rejected or [])
        self.streams_rejected = list(streams_rejected or [])
        self.from_rejected = from_rejected
        self.coalescer = TokenCoalescer(coalesce)
        self.live = False
        self.sent = 0
        self.truncated = {}
        self.oldest = {}
        self.resets = 0
        #: How many times a tick hit `MAX_TICK_EVENTS` and carried the rest.
        self.capped = 0
        self._resumed = bool(cursors)
        #: Per-stream tail checkpoints, so a live tick reads only what was
        #: appended (GD-30: per-tick cost is O(bytes appended), never a
        #: re-parse of the stream). The first tick after the switch still does
        #: one full pass — it has no checkpoint yet — and every tick after it
        #: is incremental.
        self._checkpoints = {}
        #: Records a capped tick read but did not emit, per stream. They are
        #: held rather than re-read: `follow` has already parsed them and
        #: already moved the checkpoint past them, so dropping them would need
        #: the checkpoint rewound and the same bytes parsed again — and the
        #: re-read would re-offer records the cursor cannot filter (a token
        #: record held by the coalescer keeps the cursor behind it), turning a
        #: bounded tick into duplicate frames.
        self._carry = {}

    # --- streams ---------------------------------------------------------

    def streams(self) -> list:
        """The streams this session serves, newest run last (replay order)."""
        if self.selected is not None:
            return list(self.selected)
        store = self.model.store
        if store is None:
            return []
        found = list(store.streams())
        return sorted(found, key=lambda s: (s.startswith("run:"), s))

    def _observed(self) -> set:
        """The streams the store has actually seen.

        A `?stream=` selector is only checked for *grammar*, so a client may
        name a stream nobody has ever written — a typo, or a run that has not
        started yet. Serving it is right (that is how a page watches for a run
        to appear, and the late-stream backfill in :meth:`tick` is built for
        it); presenting it as an observation is not, which is why this set
        exists and why `hello` publishes the difference.
        """
        store = self.model.store
        if store is None:
            return set()
        try:
            return set(store.streams())
        except OSError:
            return set()

    def _current_run_stream(self):
        """The most recently *written* `run:` stream — R-55's "current run".

        By the mtime of the stream file, which is an observed fact about the
        run, and explicitly **not** by the order `store.streams()` returns:
        that ends in `sorted(found)` over a `listdir`, so it is alphabetical,
        and run ids are `wf_<random hex>`. Taking its last element would pick
        the alphabetically-largest run — which replays some other run whole,
        truncates the one the operator is watching, and publishes the wrong id
        as `currentRun` to the page.

        A file mtime is not a record timestamp, so this is not the ts-ordering
        GD-11 forbids: nothing is *sequenced* by it, one stream is *selected*
        by it, and the tie-break is the name so the choice stays deterministic.
        A stream that cannot be stat'ed sorts oldest rather than raising.

        Only an **observed** stream can be the current run. A `?stream=` that
        is well-formed but names nothing this store has ever written would
        otherwise become `currentRun` and label the page's header with a run
        that does not exist — the made-up fact `h_events` refuses with a 404,
        arriving through the socket instead. Such a selector is served (a run
        may start later) and named in `hello.streamsUnobserved`; it is simply
        not presented as an observation.
        """
        observed = self._observed()
        runs = [s for s in self.streams() if s.startswith("run:") and s in observed]
        if not runs:
            return None
        store = self.model.store

        def written_at(stream):
            try:
                return os.stat(store.stream_path(stream)).st_mtime
            except (OSError, store_mod.StoreError, AttributeError):
                return -1.0

        return max(runs, key=lambda s: (written_at(s), s))

    def _from_applies(self) -> bool:
        """Whether `?from=` selects anything — R-55 pairs it with ONE stream.

        `?from=<seq>` against three streams has no meaning (seq is per stream),
        and silently dropping the parameter is the failure mode: after the 101
        there is no status code left, so the hello frame publishes `fromApplied`
        and the client can see its parameter was not used.
        """
        return self.from_seq is not None and len(self.streams()) == 1

    def _records(self, stream) -> list:
        store = self.model.store
        if store is None:
            return []
        try:
            return store.read_all(stream)
        except store_mod.StoreError:
            return []

    # --- frames ----------------------------------------------------------

    @staticmethod
    def frame(stream, record, *, live) -> dict:
        seq = _int_or(record.get("seq"), 0)
        return {"type": "event", "live": bool(live), "stream": stream, "seq": seq,
                "cursor": store_mod.cursor_key(stream, seq), "record": record}

    def hello(self) -> dict:
        """The handshake: the mode, the window, and what the parameters did.

        It does **not** carry `oldest`/`truncated`. Those describe a replay
        that has not happened yet when this frame goes out — `HttpServer.stream`
        sends hello, *then* replays — so hello could only ever publish `{}`, and
        a page that reads an empty `truncated`, concludes nothing was cut and
        drops the "load older" affordance is wrong without saying so. The
        anchors are on the `mode` frame, which is the first moment they exist.

        `cursors` here is the position the *client* supplied (`?cursor=`), so
        it is `{}` on a fresh connect; every parameter that could not be used
        is named (`fromApplied`, `fromRejected`, `cursorsRejected`,
        `streamsRejected`) because after the 101 there is no status code left
        to refuse with. `from` and `fromRejected` are separate fields so the
        three cases stay distinguishable: no `?from=` at all is
        `{"from": null, "fromRejected": null}`, an unusable-but-valid one is
        `{"from": 12, "fromApplied": false}`, and `?from=abc` is
        `{"from": null, "fromRejected": "abc"}` — one `false` shared by all
        three would be the silent drop this frame exists to end.

        `streamsUnobserved` is the other half of that rule for `?stream=`:
        those selectors *are* served, so they are not in `streamsRejected`,
        but the client is told which of its targets this store has never seen
        rather than being left to infer it from an empty replay.
        """
        streams = self.streams()
        observed = self._observed()
        current = self._current_run_stream()
        return {
            "type": "hello",
            "live": False,
            "mode": "replay",
            "protocol": SCHEMA_VERSION,
            "reducerVersion": agents_mod.REDUCER_VERSION,
            "window": self.window,
            "streams": streams,
            "currentRun": current,
            "from": self.from_seq,
            "fromApplied": self._from_applies(),
            "fromRejected": self.from_rejected,
            "resumed": self._resumed,
            "cursors": dict(self.cursors),
            "cursorsRejected": list(self.cursors_rejected),
            "streamsRejected": list(self.streams_rejected),
            "streamsUnobserved": [s for s in streams if s not in observed],
            "tokenCoalesceSeconds": self.coalescer.window,
        }

    def replay(self) -> list:
        """The bounded backfill: every frame `live:false`, painted once.

        Resume wins over the window: a stream the client holds a cursor for is
        replayed from that cursor (that is the *point* of the cursor), and only
        a stream it has never seen falls back to "current run whole, others
        capped".
        """
        out = []
        current = self._current_run_stream()
        from_applies = self._from_applies()
        for stream in self.streams():
            records = self._records(stream)
            cursor = self.cursors.get(stream)
            if cursor is not None:
                window, truncated, oldest = replay_window(
                    records, limit=MAX_REPLAY_EVENTS, from_seq=int(cursor))
            elif from_applies:
                window, truncated, oldest = replay_window(
                    records, limit=MAX_REPLAY_EVENTS, from_seq=int(self.from_seq))
            else:
                window, truncated, oldest = replay_window(
                    records, limit=self.window, whole=(stream == current))
            out.extend(self._emit_backfill(stream, window, truncated, oldest))
        self.sent += len(out)
        return out

    def _emit_backfill(self, stream, window, truncated, oldest) -> list:
        """Turn one stream's replay window into `live:false` frames.

        Shared with :meth:`tick` and :meth:`subscribe` so a stream that appears
        *after* the mode switch, and a range re-delivered for a rewound cursor,
        get the same treatment as a stream that was there at the handshake:
        their backlog is painted once as backfill. Sending it `live:true` —
        which is what "no cursor ⇒ since 0" did — would animate a burst sp-13
        cannot tell apart from real activity, because the `live` flag is the
        only thing it has to go on (R-55).

        **After the boundary it publishes its own anchors.** `switch()` sends
        `oldest`/`truncated` exactly once and can never be re-sent, so a
        backfill that happens later would record its anchors into session state
        that nothing reads again: 55 of a new stream's 60 records cut off the
        wire, `truncated` set, and a page that is never told. The `anchors`
        frame is that publication, and it goes out *before* the frames it
        describes so a client sees the window's edge before its contents.
        """
        out = []
        if truncated:
            self.truncated[stream] = True
        if oldest is not None:
            self.oldest[stream] = oldest
        if self.live and (truncated or oldest is not None):
            out.append({"type": "anchors", "live": True, "stream": stream,
                        "oldest": oldest, "truncated": bool(truncated)})
        for record in window:
            out.append(self.frame(stream, record, live=False))
            self.cursors[stream] = _int_or(record.get("seq"),
                                           self.cursors.get(stream, 0))
        if stream not in self.cursors:
            # An empty stream still gets a position, so the first live
            # record after it is "after the cursor" rather than "before an
            # absent one" (which would replay it twice on reconnect).
            self.cursors[stream] = 0
        return out

    def switch(self) -> dict:
        """The single replay→tail boundary frame, and the only load-older anchor.

        Carries the cursors the replay ended on plus what it cut off
        (`oldest`/`truncated`), because all three are only known *after* the
        replay and a page that has to guess where "load older" starts will
        guess wrong. This is the frame the wire contract names for them; hello
        deliberately carries none of the three (see :meth:`hello`), so a client
        that only reads hello sees no anchor rather than an empty one.
        """
        self.live = True
        return {"type": "mode", "live": True, "mode": "tail",
                "cursors": dict(self.cursors), "oldest": dict(self.oldest),
                "truncated": dict(self.truncated)}

    def _advance(self, stream, seq) -> None:
        """Move a stream's published cursor forward — never past a held record.

        The cursor means "delivered", and it is published (`mode`, `subscribe`
        ack) for the client to reconnect from. Advancing it for a record the
        coalescer is *holding* would publish a position that was never sent:
        the hold dies with the socket, so a client that adopted it and
        reconnected inside the ≤1 s window would skip the record permanently.
        Clamping to `pending_floor - 1` trades that for at most a repaint of
        the frames after the held one, which is visible and dedupable by
        `(stream, seq)` — the direction this file always chooses.
        """
        floor = self.coalescer.pending_floor(stream)
        if floor is not None:
            seq = min(seq, floor - 1)
        if seq > _int_or(self.cursors.get(stream), 0):
            self.cursors[stream] = seq

    def tick(self, now=None) -> list:
        """Everything appended since the cursors, coalescing token frames.

        Incremental by checkpoint (`store.follow`), and de-duplicated by cursor
        regardless: a rotation or an in-place truncation makes `follow` re-read
        the file from zero (`reset=True`, R-23/SD-10), and without the cursor
        filter that re-read would replay the whole stream to a live client as
        if it were new. The cursor is the client's position and it is the one
        thing that must never go backwards.

        ``since`` is snapshotted per stream *before* the loop, so the emitted
        cursor lagging behind a held token record (see :meth:`_advance`) still
        cannot re-deliver a record inside one tick.

        **Bounded.** At most ``MAX_TICK_EVENTS`` frames per stream leave one
        tick; whatever `follow` returned beyond that is carried to the next one
        in file order. Without the cap a 5000-record bulk append — an ingest
        catching up after a restart, which is exactly how a burst is born —
        became 5000 frames written and drained back-to-back on every connected
        socket before the loop could sleep. Carrying the remainder rather than
        rewinding the checkpoint is what keeps "no gap and no duplicate" true:
        the cursor stops where the cap fell, and the records after it are the
        ones this session has not sent, not the ones it must re-read to find.
        """
        now = time.monotonic() if now is None else now
        store = self.model.store
        if store is None:
            return []
        out = []
        for stream in self.streams():
            if stream not in self.cursors:
                # A stream that came into existence after the mode switch: its
                # backlog is backfill, not live traffic, so it is painted once
                # (live:false, with its own anchors frame) and only then tailed.
                records = self._records(stream)
                window, truncated, oldest = replay_window(records, limit=self.window)
                out.extend(self._emit_backfill(stream, window, truncated, oldest))
            since = _int_or(self.cursors.get(stream), 0)
            fresh = self._carry.pop(stream, None)
            if fresh is None:
                try:
                    fresh, checkpoint, reset = store.follow(
                        stream, self._checkpoints.get(stream))
                except store_mod.StoreError:
                    continue
                self._checkpoints[stream] = checkpoint
                if reset:
                    self.resets += 1
            emitted = 0
            for index, record in enumerate(fresh):
                if emitted >= MAX_TICK_EVENTS:
                    self._carry[stream] = fresh[index:]
                    self.capped += 1
                    break
                seq = _int_or(record.get("seq"), -1)
                if seq <= since:
                    continue
                if _is_token_record(record):
                    released = self.coalescer.offer(stream, record, now)
                    if released is None:
                        # Held, not sent: the cursor stays behind it.
                        continue
                    record = released
                out.append(self.frame(stream, record, live=True))
                emitted += 1
                self._advance(stream, _int_or(record.get("seq"), seq))
        for stream, record in self.coalescer.due(now):
            out.append(self.frame(stream, record, live=True))
            self._advance(stream, _int_or(record.get("seq"), 0))
        self.sent += len(out)
        return out

    def _resume(self, stream, seq) -> list:
        """Re-deliver one stream from ``seq`` as backfill. Returns the frames.

        The range is `(seq, delivered]` when this session already has a
        position for the stream — the client is asking to see again what it
        lost — and `(seq, head]` when it does not, which is the handshake's
        `?cursor=` semantics arriving over the socket instead of the query
        string. Bounded by ``MAX_REPLAY_EVENTS`` like every other backfill, and
        painted `live:false` with its own anchors frame, so a rewind past the
        cap is declared rather than silently short.
        """
        current = self.cursors.get(stream)
        records = [r for r in self._records(stream)
                   if _int_or(r.get("seq"), -1) > seq]
        if current is None:
            # Adopt the client's position first: `_emit_backfill` floors an
            # unknown stream at 0, which would make the next tick re-deliver
            # everything the client just told us it already has.
            self.cursors[stream] = seq
        else:
            ceiling = _int_or(current, 0)
            records = [r for r in records if _int_or(r.get("seq"), -1) <= ceiling]
        window, truncated, oldest = replay_window(records, limit=MAX_REPLAY_EVENTS)
        return self._emit_backfill(stream, window, truncated, oldest)

    def subscribe(self, message) -> list:
        """Apply a client `{"type":"subscribe","cursors":{...}}` message.

        Returns **the frames to send, in order, ending with the ack** — because
        a subscribe is a resume, not a bookkeeping update. It used to
        `self.cursors.update(accepted)` and answer `{"accepted": …}`: the tail
        reads `store.follow` from a checkpoint already at EOF, so a rewound
        cursor replayed nothing at all while the ack reported it applied, and
        the session's own published position was left naming a seq behind
        records it had already delivered — every later ack and every reconnect
        built on it re-sending them as duplicates. R-55's named test
        ("reconnect mid-stream ⇒ no duplicate events") failing through the one
        API that exists to prevent it.

        So each accepted pair is acted on:

        * **behind** the session's position ⇒ that range is re-delivered as
          `live:false` backfill (:meth:`_resume`) before the ack, and the
          cursor ends where it started — everything the client is told it has,
          it has been sent;
        * **at** it ⇒ nothing to do;
        * **ahead** of it ⇒ *refused*, with the reason and this session's real
          position on the ack. Adopting it would publish a cursor for records
          this socket never sent, and the client that trusted the ack would
          skip them for good.

        Malformed pairs are refused the same way rather than raising — a socket
        is not a place to die over a typo — and every refusal is named, which
        is the handshake's `cursorsRejected` policy (:func:`parse_cursor_params`)
        on the frame that has an answer slot of its own.
        """
        accepted, rejected = {}, []
        cursors = message.get("cursors") if isinstance(message, dict) else None
        if isinstance(cursors, dict):
            for stream, seq in cursors.items():
                # The echo is truncated because it is attacker-supplied: a
                # socket message is not length-bounded the way a query string
                # is, and "we name what we could not use" may not become "we
                # mirror a megabyte back at every client".
                raw = f"{stream!r}#{seq!r}"[:MAX_REJECT_ECHO]
                if not isinstance(stream, str) or not _is_stream(stream):
                    rejected.append({"cursor": raw, "reason": "not a stream id"})
                    continue
                if (isinstance(seq, bool) or not isinstance(seq, int)
                        or not 0 <= seq <= MAX_SEQ):
                    # `MAX_SEQ` is the cursor grammar's own bound (12 digits):
                    # a larger number cannot be spelled as a `(stream, seq)`
                    # cursor at all, so accepting it would create a position no
                    # client could ever hand back.
                    rejected.append({"cursor": raw, "reason": "not a seq"})
                    continue
                if self.selected is not None and stream not in self.selected:
                    # Adopting it would backfill a stream `streams()` never
                    # tails: served once, then silently frozen.
                    rejected.append({"cursor": store_mod.cursor_key(stream, seq),
                                     "reason": "outside this socket's ?stream= selection"})
                    continue
                held = self.cursors.get(stream)
                if held is not None and seq > _int_or(held, 0):
                    rejected.append({
                        "cursor": store_mod.cursor_key(stream, seq),
                        "reason": f"ahead of this socket at seq {_int_or(held, 0)}"})
                    continue
                accepted[stream] = seq
        frames, backfilled = [], {}
        for stream in sorted(accepted):
            replayed = self._resume(stream, accepted[stream])
            events = [f for f in replayed if f["type"] == "event"]
            if events:
                backfilled[stream] = len(events)
            frames.extend(replayed)
        self.sent += len(frames)
        self._resumed = self._resumed or bool(accepted)
        frames.append({"type": "subscribed", "live": self.live,
                       "cursors": dict(self.cursors), "accepted": accepted,
                       "rejected": rejected, "backfilled": backfilled})
        return frames


def parse_cursor_params(query: dict) -> tuple:
    """`?cursor=<stream>#<seq:012d>` (repeatable) → `({stream: seq}, [rejected])`.

    The cursor grammar is `store.cursor_key`'s, byte-identical to the `events`
    `_id` (GD-24), so a client can hand back the `_id` it saw.

    Parsing is **per entry**: one malformed cursor rejects itself and nothing
    else. Raising on the first bad one — with the sole caller being a handshake
    that has already returned 101 and can only swallow it — meant a client that
    got two of three cursors right silently lost all three and replayed records
    it already held, which is precisely R-55's "reconnect mid-stream ⇒ no
    duplicate events" failing for a typo. The rejected raw strings come back so
    `hello` can name them, the same way `fromApplied` names an unusable
    `?from=` and `subscribe`'s ack names an unusable pair: after the upgrade,
    naming it on a frame is the only refusal left.
    """
    accepted, rejected = {}, []
    for raw in query.get("cursor") or []:
        try:
            stream, seq = store_mod.parse_cursor_key(raw)
        except store_mod.StoreError:
            rejected.append(raw)
            continue
        accepted[stream] = seq
    return accepted, rejected


# --- handlers -------------------------------------------------------------


def h_health(api, query, headers) -> Response:
    """The one unauthenticated route. Liveness, counters, and the mirror block.

    Deliberately says nothing about *what* is being observed — no session ids,
    no run ids, no paths from `~/.claude` or from the store, no token, no URI
    (GD-27). What it does say is everything an operator needs to answer "is it
    up, is anything being dropped, and is the mirror at fault": per-tailer
    liveness with parse-failure counters (R-30) and `mirror.health()` verbatim
    (R-45). Counts and states are operational facts; names and paths are
    observations, and observations are what the token is for.

    `requests` is that rule applied to the server's own traffic: served /
    not-found / failed as three totals, never a per-route breakdown, because a
    route name is a path and the line above is not negotiable just because the
    path happens to be one of ours.
    """
    model = api.model
    payload = {
        "ok": True,
        "service": "touch-aggregator",
        "version": __version__,
        "schema": SCHEMA_VERSION,
        "reducerVersion": agents_mod.REDUCER_VERSION,
        "uptimeSeconds": round(time.monotonic() - model.started, 3),
        "bind": dict(api.bind),
        "ws": dict(api.ws_stats),
        "requests": dict(api.requests),
        "auth": {"required": True, "openRoutes": sorted(api.auth.open_routes),
                 "rejections": api.auth.rejections},
        "origin": {"rejections": api.policy.rejections if api.policy else 0},
        "tailers": model.tailer_health(),
        "parseFailures": model.parse_failures(),
        "store": {"configured": model.store is not None,
                  "streamCount": len(model.store.streams()) if model.store else 0,
                  "stats": dict(getattr(model.store, "stats", {}) or {})},
        "collections": model.sizes(),
        "counters": dict(model.counters),
        "mirror": model.mirror_health(),
    }
    return Response.json(payload)


def h_sessions(api, query, headers) -> Response:
    """`/api/sessions` — the sidebar's rows, newest last-activity first.

    Both session classes are listed (GD-6/R-46's tagged union): a `hist:`
    document is a real session with no process, not a degraded `live:` one, and
    it is labelled rather than hidden. `transcriptless` is the seventh
    sessionId of this machine's registry — observed, `sources: []` — and it is
    published because "we know about it and have nothing to show" is a
    different answer from "it does not exist".
    """
    model = api.model
    rows = [_session_payload(doc) for doc in model.bucket("sessions").values()
            if isinstance(doc, dict)]
    if flag(query, "live"):
        rows = [r for r in rows if r["kind"] == "live"]
    rows.sort(key=lambda r: (_ts_key(r.get("lastTs")), r.get("id") or ""), reverse=True)
    return Response.json({"sessions": rows, "count": len(rows)})


def h_timeline(api, query, headers) -> Response:
    """`/api/session/timeline?session=&since=&sinceId=` — one session, paged.

    The cursor is the **whole sort key**, `(lineNo, _id)`, and that is the
    correction a `lineNo`-only cursor needs rather than a nicety: `lineNo` is
    unique per *file*, not per session. One sessionId's `records` are ingested
    from the session transcript **and** from every `subagents/**/agent-*.jsonl`
    beside it, each numbered from line 1, so in the frozen corpus a single
    session has 100 distinct `lineNo` values carrying nine documents each. An
    exclusive `> lineNo` cursor discards whatever is left of the group a page
    boundary lands inside — 31 records of 671, silently, with no cursor the
    client could reach them with. Paging on the pair cannot skip: it is the
    same order the rows are sorted in.

    ``since``/``sinceId`` are one exclusive *position* and come straight back
    off the previous page as `nextSince`/`nextSinceId`, so a client never
    constructs a cursor itself. A missing `sinceId` is the empty string, which
    sorts before every `_id`: `?since=3` alone therefore resumes at the
    **start** of line 3 rather than after it. That direction is chosen on
    purpose — a client that forgets half the cursor re-reads a line (visibly,
    same `_id`s) instead of losing the rest of a group (invisibly, forever),
    which is the failure this cursor exists to end.

    R-47 stores `lineNo` on every mirrored record precisely so order is an
    explicit field rather than append order; paging on a timestamp would drop
    records whenever two share one (which the corpus does, up to 27 times in a
    file). Bodies are omitted unless `?full=1`: the corpus holds an 872 KB
    line, and a 200-record page of those is not a response, it is an outage.
    """
    model = api.model
    session_id = valid_id("session", one(query, "session"), what="session id")
    doc = model.session_doc(session_id)
    if doc is None:
        raise HttpError(404, f"no session {session_id!r} has been observed")
    since = positive_int(query, "since", 0, maximum=2 ** 40)
    since_id = one(query, "sinceId", "") or ""
    if since_id and not _CURSOR_ID_RE.match(since_id):
        raise HttpError(400, "malformed sinceId: expected an _id from a previous "
                             "page's nextSinceId")
    limit = positive_int(query, "limit", DEFAULT_PAGE, maximum=MAX_PAGE, minimum=1)
    full = flag(query, "full")
    cursor = (since, since_id)

    # Carried as `(bucket, doc)` pairs rather than sniffed afterwards: a
    # `records` document and a `stream_meta` one are different collections with
    # different field sets (R-47's 12-type table), and recovering that from the
    # `_id` shape would be a second, weaker copy of the bucket rule.
    rows = [("records", d) for d in model.records_of(session_id)]
    if flag(query, "meta"):
        rows += [("stream_meta", d) for d in model.stream_meta_of(session_id)]
    rows = [pair for pair in rows if _row_key(pair[1]) > cursor]
    rows.sort(key=lambda pair: _row_key(pair[1]))
    page = rows[:limit]
    out = []
    for bucket, record in page:
        fields = RECORD_FIELDS if bucket == "records" else STREAM_META_FIELDS
        projected = _project(record, fields, extra={"collection": bucket})
        if full and "body" in record:
            projected["body"] = record["body"]
        out.append(projected)
    last = _row_key(page[-1][1]) if page else cursor
    return Response.json({
        "session": session_id,
        "sessionDoc": _session_payload(doc),
        "records": out,
        "count": len(out),
        "since": since,
        "sinceId": since_id,
        "nextSince": last[0],
        "nextSinceId": last[1],
        "hasMore": len(rows) > len(page),
        "bodies": bool(full),
    })


def h_events(api, query, headers) -> Response:
    """`/api/events?session=|stream=|run=&after=` — the `.touch/` stream, paged.

    The cursor is `(stream, seq)` and this endpoint enforces the half of that
    rule an HTTP API can get wrong: **`after=` without a stream selector is a
    400**, never "after seq 12 of whichever stream we felt like" (GD-11/GD-12).
    `before=` is R-55's "load older" arm, paging backwards from the window the
    socket cut off.

    A `run=`/`stream=` naming something never observed is a **404**, not an
    empty list: `run:<anything>` is a well-formed stream id, so answering 200
    with `records: []` and a `head` cursor would be publishing a made-up fact
    about a made-up run — the wrong-target answer wearing a success code that
    :class:`HttpError`'s docstring names.

    `cursor` means *"the pair to continue from"* in **both** directions, which
    is why the backwards arm's is `page[0]` and not `page[-1]`: walking older
    continues from the oldest record on the page, so its seq is the next
    `before=`. Naming the newest one `cursor` while `oldest` sat beside it gave
    one field two opposite meanings on one endpoint — the sort of thing a page
    gets right by luck and wrong on the second page.

    **Recorded cost.** Every request parses the whole stream file
    (`store.read_all`) and pages in memory, so walking a 20 MB stream backwards
    re-parses it once per page. The *wire* is bounded (`limit`, `MAX_PAGE`) and
    the live path is incremental (`store.follow` by checkpoint, GD-30's budget
    is written about ticks), so this is outside the letter of that budget — but
    it is the loop a page runs to walk a truncation, and it is a seek by
    `byteOffset`/checkpoint the day a stream gets big enough to notice. Stated
    here rather than discovered there.
    """
    model = api.model
    session_id = optional_id("session", one(query, "session"), what="session id")
    stream = optional_id("stream", one(query, "stream"))
    run_id = optional_id("run", one(query, "run"), what="runId")
    after = one(query, "after")
    before = one(query, "before")
    limit = positive_int(query, "limit", DEFAULT_PAGE, maximum=MAX_PAGE, minimum=1)

    selectors = [s for s in (session_id, stream, run_id) if s]
    if len(selectors) > 1:
        raise HttpError(400, "give exactly one of session=, stream= or run=")
    if not selectors:
        if after is not None or before is not None:
            raise HttpError(400, "a bare after=/before= is not a cursor; "
                                 "a cursor is (stream, seq) — add session=, stream= or run=")
        raise HttpError(400, "one of session=, stream= or run= is required")
    if after is not None:
        valid_id("seq", after, what="after cursor")
    if before is not None:
        valid_id("seq", before, what="before cursor")

    store = model.store
    if store is None:
        raise HttpError(503, "no .touch/ store is configured on this server")

    if session_id:
        # This arm's existence check is the session document itself: a live
        # session whose stream has no records yet is observed, and an empty
        # answer for it is the truth.
        if model.session_doc(session_id) is None:
            raise HttpError(404, f"no session {session_id!r} has been observed")
        stream = model.session_stream(session_id)
        if stream is None:
            return Response.json({"stream": None, "records": [], "count": 0,
                                  "note": "historical session: no .touch/ event stream "
                                          "(D5 keys streams by <pid>-<procStart>)"})
        observed = True
    else:
        if run_id:
            stream = f"run:{run_id}"
        observed = False
    # Malformed first, unknown second — the same order, and the same split, as
    # `valid_id`'s: a stream id this store could never open is a 400.
    try:
        store.stream_path(stream)
    except store_mod.StoreError as exc:
        raise HttpError(400, str(exc)) from None
    if not observed and stream not in store.streams():
        raise HttpError(404, f"no run {run_id!r} has been observed" if run_id
                        else f"no stream {stream!r} has been observed")
    records = store.read_all(stream)
    if after is not None:
        records = [r for r in records if _int_or(r.get("seq"), -1) > int(after)]
    if before is not None:
        older = [r for r in records if 0 <= _int_or(r.get("seq"), -1) < int(before)]
        page = older[-limit:]
        return Response.json({
            "stream": stream, "records": page, "count": len(page),
            "before": int(before), "hasOlder": len(older) > len(page),
            "oldest": _int_or(page[0].get("seq"), None) if page else None,
            # Backwards: continue from the OLDEST record on this page — its seq
            # is the next `before=`. (Forwards, `cursor` is the newest; both
            # mean "hand this back to get the next page".)
            "cursor": store_mod.cursor_key(stream, _int_or(page[0].get("seq"), 0))
            if page else None,
        })
    page = records[:limit]
    return Response.json({
        "stream": stream, "records": page, "count": len(page),
        "after": int(after) if after is not None else None,
        "hasMore": len(records) > len(page),
        "cursor": store_mod.cursor_key(stream, _int_or(page[-1].get("seq"), 0)) if page else None,
        "head": store.cursor(stream),
    })


def h_run_graph(api, query, headers) -> Response:
    """`/api/run/graph?run=` — the run's nodes and agents, observations + verdicts.

    Every derived value on this response came out of `agents.reduce` at read
    time, which is what makes liveness honest: an agent idle for ten minutes is
    `unknown` here for the same reason and by the same code as on the page
    (R-54 moved `monitor.html`'s freeze-to-stale into the reducer precisely so
    these two cannot disagree).
    """
    model = api.model
    run_id = valid_id("run", one(query, "run"), what="runId")
    run_key = refs.run_key(run_id)
    nodes = model.nodes_of(run_id)
    run_doc = model.lookup("runs", run_key)
    if run_doc is None and not nodes:
        raise HttpError(404, f"no run {run_id!r} has been observed")

    reduction = model.reduction()
    node_rows = []
    agent_ids = []
    for doc in sorted(nodes, key=lambda d: (_int_or(d.get("journalSeq"), 0), str(d.get("_id")))):
        derived = reduction.nodes.get(doc.get("_id"))
        node_rows.append(_node_payload(doc, derived))
        if doc.get("agentId"):
            agent_ids.append(doc["agentId"])
    agent_rows = []
    for agent_id in dict.fromkeys(agent_ids):
        agent_doc = model.agent_doc(agent_id)
        if agent_doc is not None:
            agent_rows.append(_agent_payload(agent_doc, reduction.agents.get(agent_id)))
    return Response.json({
        "run": run_id,
        "runId": run_id,
        "observed": _project(run_doc or {}, ("_id", "taskId", "workflowName",
                                             "transcriptDir", "scriptPath", "sessionIds",
                                             "status", "harnessTotals", "startedAt",
                                             "endedAt", "ingestMode", "provenance")),
        "derived": reduction.runs.get(run_key),
        "nodes": node_rows,
        "agents": agent_rows,
        "counts": {"nodes": len(node_rows), "agents": len(agent_rows)},
    })


def h_run_node(api, query, headers) -> Response:
    """`/api/run/node?run=&agent=` — one node, its agent, fragments and spawn.

    The spawn locator is R-48's, unchanged: identity is `recordUuid` +
    `toolUseId`, and the `fileHint` is a perishable cache whose validity is
    re-checked here against `(st_dev, st_ino, size)`. A stale hint is reported
    stale and the jump still resolves — through `records`, never by re-reading
    the transcript.
    """
    model = api.model
    run_id = valid_id("run", one(query, "run"), what="runId")
    agent_id = valid_id("agent", one(query, "agent"), what="agentId")
    match = [doc for doc in model.nodes_of(run_id) if doc.get("agentId") == agent_id]
    if not match:
        raise HttpError(404, f"run {run_id!r} has no node for agent {agent_id!r}")
    reduction = model.reduction()
    doc = match[0]
    agent_doc = model.agent_doc(agent_id)
    payload = {
        "run": run_id,
        "node": _node_payload(doc, reduction.nodes.get(doc.get("_id"))),
        "agent": (_agent_payload(agent_doc, reduction.agents.get(agent_id))
                  if agent_doc is not None else None),
    }
    spawn = (agent_doc or {}).get("spawn")
    if isinstance(spawn, dict):
        hint = spawn.get("fileHint")
        status = agents_mod.check_file_hint(hint, root=model.claude_root) if hint else None
        payload["spawn"] = {
            "recordUuid": spawn.get("recordUuid"),
            "toolUseId": spawn.get("toolUseId"),
            "fileHint": hint,
            "hint": ({"valid": status.valid, "reason": status.reason}
                     if status is not None else None),
            # The jump is a document lookup, never a file read (R-48).
            "record": model.lookup("records", spawn.get("recordUuid")) is not None,
        }
    return Response.json(payload)


def h_toolresult(api, query, headers) -> Response:
    """`/api/toolresult?id=<toolUseId>` — a spilled tool result, contained.

    Containment is re-checked **at serve time** (`ingest.spill_containment`),
    not trusted from the stored `persistedOutput.contained` flag: the flag is a
    fact about the moment of ingest, the request is now, and the path came from
    agent-authored text. Not contained ⇒ 403 with the reason, never a read.
    """
    model = api.model
    tool_use_id = valid_id("toolUseId", one(query, "id"), what="toolUseId")
    hit = None
    for doc in model.bucket("records").values():
        if not isinstance(doc, dict):
            continue
        ids = doc.get("toolUseIds") or ([doc["toolUseId"]] if doc.get("toolUseId") else [])
        if tool_use_id in ids and isinstance(doc.get("persistedOutput"), dict):
            hit = doc
            break
    if hit is None:
        raise HttpError(404, f"no spilled output is recorded for toolUseId {tool_use_id!r}")
    spill = hit["persistedOutput"]
    path = spill.get("path")
    if not isinstance(path, str) or not ingest_mod.spill_containment(path, root=model.claude_root):
        raise HttpError(403, "the recorded spill path is not contained under "
                             "<claude root>/projects/*/*/tool-results/")
    try:
        with open(path, "rb") as fh:
            body = fh.read(MAX_TOOLRESULT_BYTES + 1)
    except OSError:
        raise HttpError(404, "the spill file named by this record is gone") from None
    truncated = len(body) > MAX_TOOLRESULT_BYTES
    if truncated:
        body = body[:MAX_TOOLRESULT_BYTES]
    # The basename came from agent-authored text and is percent-encoded for the
    # same reason `Content-Disposition` percent-encodes a filename: a header is
    # a line in a protocol, and a filename may contain anything a filesystem
    # allows. `Response.head_bytes` sanitizes as well — this is the encoding
    # that keeps the value *recoverable* rather than merely safe.
    return Response(status=200, body=body, content_type="text/plain; charset=utf-8",
                    headers={"Content-Security-Policy": FILE_CSP,
                             "Referrer-Policy": NO_REFERRER,
                             "X-Touch-Truncated": "1" if truncated else "0",
                             "X-Touch-Basename": urllib.parse.quote(
                                 spill.get("basename") or "", safe="")})


def h_tasks(api, query, headers) -> Response:
    """`/api/tasks` — the legacy `events.jsonl` task folders (GD-14 kinds).

    Plan-only folders are listed with their kind and no controls (RUNSTATE-13):
    a folder with a plan and no run is a real thing the sidebar shows, not an
    error and not an empty task.

    Two shapes of "nothing to list" answer 200 **with a `note`**, because the
    panel that renders this cannot tell an empty answer from a lost one (UI-13):
    no root configured at all, and a configured root that is not there. The
    second is the one the tasks-root move creates — `--tasks-root` is a computed
    default, so "not configured" is now nearly unreachable, while a resolver
    that has moved ahead of the directory (or a checkout that has simply never
    run an orchestration) is ordinary. `legacy.scan` answers `()` for a missing
    root, correctly and silently, which is precisely why the note has to be here
    and not inferred from the count.
    """
    model = api.model
    if not model.tasks_root:
        return Response.json({"tasks": [], "count": 0,
                              "note": "no local-orchestrators root configured"})
    if not os.path.isdir(model.tasks_root):
        # The note names no path: it is rendered on the page, and the root is
        # already its own field for a reader that wants it. `exists` is the
        # machine-readable half of the same fact, so a caller that wants to
        # branch on it does not have to parse English.
        return Response.json({"tasks": [], "count": 0, "root": model.tasks_root,
                              "exists": False,
                              "note": "local-orchestrators root does not exist yet"})
    reductions = legacy_mod.scan(model.tasks_root)
    rows = [_task_payload(r) for r in reductions]
    rows.sort(key=lambda r: r["task"])
    return Response.json({"tasks": rows, "count": len(rows), "root": model.tasks_root})


def _task_dir(model, query) -> str:
    """The task folder a `?task=` names — validated, contained, never a fallback.

    The monitor's silent fallback to its own `STATE_DIR` when a task name does
    not resolve is exactly the wrong-target hazard GD-12 forbids, so an unknown
    task is a 404 here and nothing else.
    """
    if not model.tasks_root:
        raise HttpError(404, "no local-orchestrators root is configured")
    task = valid_id("task", one(query, "task"), what="task folder name")
    base = os.path.realpath(model.tasks_root)
    full = os.path.realpath(os.path.join(base, task))
    if not full.startswith(base + os.sep) or not os.path.isdir(full):
        raise HttpError(404, f"no task folder {task!r}")
    return full


def h_artifacts(api, query, headers) -> Response:
    """`/api/artifacts?task=` — the task folder's reports and notes."""
    return Response.json({"task": one(query, "task"),
                          "artifacts": artifact_listing(_task_dir(api.model, query))})


def h_file(api, query, headers) -> Response:
    """`/file?task=&path=` — one artifact, extension-whitelisted and contained."""
    directory = _task_dir(api.model, query)
    full = safe_artifact_path(directory, one(query, "path", ""))
    if full is None:
        raise HttpError(404, "no such artifact (or its extension is not servable)")
    try:
        with open(full, "rb") as fh:
            body = fh.read()
    except OSError:
        raise HttpError(404, "artifact disappeared between listing and read") from None
    if full.lower().endswith(".md"):
        # Served as plain text: the page renders the preview with its own
        # escape-first mini renderer (GD-20), so the server never hands a
        # browser markdown-shaped HTML. `Referrer-Policy` still applies — a
        # browser navigated straight to this URL renders the text in a document
        # of this origin, and the URL carries the token (SECURITY-5).
        return Response(status=200, body=body, content_type="text/plain; charset=utf-8",
                        headers={"Referrer-Policy": NO_REFERRER})
    return Response(status=200, body=body, content_type="text/html; charset=utf-8",
                    headers={"Content-Security-Policy": FILE_CSP,
                             "Referrer-Policy": NO_REFERRER})


def h_query(api, query, headers) -> Response:
    """`/api/query?collection=&…` — R-55's optional Mongo read, with the fallback.

    The fallback is not a degraded mode, it is the documented normal one: with
    no injected query source this answers from the same in-memory model every
    other route reads and reports `source: "memory"`. That is what "the UI
    never depends on Mongo" (R-55) means operationally — the route exists, the
    answers are the same shape, and `mirror: absent` changes only the label.

    The filter is deliberately tiny: equality on declared fields, no `$`
    operators, no dotted paths, no regexes. A general query language on an
    authenticated-but-local server is a search engine nobody asked for and a
    dotted-`_id` COLLSCAN generator (LIVEFLOW-3) besides.

    **The seam, stated so the next implementer matches it.** GD-21 forbids this
    file a driver import, so the Mongo arm arrives as an injected
    ``ReadModel(query_source=…)``. Nothing in the repo supplies one yet — today
    the only implementation is `FakeQuerySource` in `tests/test_api.py`, and an
    interface with a fake and no producer drifts. So, exactly: whichever
    sub-plan wires the Mongo read must pass an object with

        find(collection: str, criteria: dict, limit: int) -> iterable[dict]

    — `limit` passed by keyword, `criteria` already validated to be flat
    equalities, the return re-truncated to `limit` here because a provider is
    not trusted to honour it. Not `query()`, not `find(collection, **kwargs)`.
    A provider that raises `MongoStoreError` is a 400 through `Api.handle`'s
    mapping and anything else is a 500 naming the type — but never a quiet
    fallback to memory: `source` says which side answered and it has to stay
    true, or `mirror: down` becomes invisible on the one route that is about
    Mongo.
    """
    model = api.model
    collection = valid_id("collection", one(query, "collection"), what="collection name")
    limit = positive_int(query, "limit", DEFAULT_PAGE, maximum=MAX_PAGE, minimum=1)
    raw = one(query, "filter")
    criteria = {}
    if raw:
        try:
            criteria = json.loads(raw)
        except ValueError as exc:
            raise HttpError(400, f"filter is not JSON: {exc}") from None
        if not isinstance(criteria, dict):
            raise HttpError(400, "filter must be a JSON object of field=value equalities")
        for name, value in criteria.items():
            if not isinstance(name, str) or name.startswith("$") or "." in name:
                raise HttpError(400, f"filter field {name!r}: no operators, no dotted paths")
            if isinstance(value, (dict, list)):
                raise HttpError(400, f"filter field {name!r}: equality on scalars only")

    source = model.query_source
    if source is not None:
        docs = list(source.find(collection, criteria, limit=limit))[:limit]
        origin = "mongo"
    else:
        docs = []
        for doc in model.bucket(collection).values():
            if not isinstance(doc, dict):
                continue
            if all(doc.get(name) == value for name, value in criteria.items()):
                docs.append(doc)
            if len(docs) >= limit:
                break
        origin = "memory"
    return Response.json({"collection": collection, "filter": criteria,
                          "documents": docs, "count": len(docs),
                          "source": origin,
                          "note": None if origin == "mongo" else
                          "served from the in-memory reduction (GD-22); "
                          "Mongo is a rebuildable mirror and is never on this path"})


def h_page(api, query, headers) -> Response:
    """`/` — `touch-visual/index.html` with the token injected (GD-13)."""
    return api.asset("index.html", "text/html; charset=utf-8", inject=True)


def h_app_js(api, query, headers) -> Response:
    return api.asset("app.js", "application/javascript; charset=utf-8")


def h_style_css(api, query, headers) -> Response:
    return api.asset("style.css", "text/css; charset=utf-8")


#: R-30 names two route *groups*, read-only and control, and the split is
#: declared here **before** a control route exists rather than after: the point
#: of the posture is that a control endpoint arrives into a group that already
#: has a name, not into a flat table where "is this route a control?" is
#: answered by reading the handler. v0 ships none (sp-13 renders no control
#: affordance), so `CONTROL_ROUTES` is empty and a test asserts it.
READ_ROUTES = {
    ("GET", "/health"): h_health,
    ("GET", "/api/sessions"): h_sessions,
    ("GET", "/api/session/timeline"): h_timeline,
    ("GET", "/api/events"): h_events,
    ("GET", "/api/run/graph"): h_run_graph,
    ("GET", "/api/run/node"): h_run_node,
    ("GET", "/api/toolresult"): h_toolresult,
    ("GET", "/api/tasks"): h_tasks,
    ("GET", "/api/artifacts"): h_artifacts,
    ("GET", "/api/query"): h_query,
    ("GET", "/file"): h_file,
    ("GET", "/"): h_page,
    ("GET", "/app.js"): h_app_js,
    ("GET", "/style.css"): h_style_css,
}

#: The control plane is out of scope for this pass (RUNSTATE: pause/restart/
#: terminate is a later item), and an empty group is the honest way to say so.
CONTROL_ROUTES = {}

#: GD-12's static `(method, route) -> handler` dict — the union of the two
#: groups. There is no prefix match, no regex route and no default handler: a
#: request whose exact pair is absent is a 404, including `/api/sessions/extra`
#: (a different route) and `POST /api/sessions` (a different pair). `/ws` is
#: dispatched before this table, at the upgrade.
ROUTES = {**READ_ROUTES, **CONTROL_ROUTES}

#: Routes reachable only through the WS upgrade path. Kept out of `ROUTES` so
#: an ordinary GET of `/ws` cannot be answered with a body (the monitor's
#: "serve the HTML on a malformed upgrade" behaviour, refused — SERVER-3).
WS_ROUTE = "/ws"


def route_table() -> dict:
    """A copy of the route table, for tests and for `/health`-adjacent tooling."""
    return dict(ROUTES)


# --- the API --------------------------------------------------------------


class Api:
    """The routes, with no socket: `(method, route, query, headers) -> Response`.

    Every property of R-30 and R-31 that matters is decidable here — no port,
    no event loop, no sleep — which is why `tests/test_api.py` never opens a
    connection and `tests/test_server_core.py` opens exactly one.
    """

    def __init__(self, model, *, auth=None, policy=None, assets=None,
                 bind=None, ws_stats=None):
        self.model = model
        self.auth = auth if auth is not None else Auth()
        self.policy = policy if policy is not None else OriginPolicy.default(
            DEFAULT_HOST, DEFAULT_PORT)
        self.assets = assets
        self.bind = dict(bind or {"host": DEFAULT_HOST, "port": DEFAULT_PORT,
                                  "loopback": True})
        self.ws_stats = ws_stats if ws_stats is not None else {
            "clients": 0, "active": 0, "framesSent": 0, "rejected": 0}
        #: Request counters, published by `/health`. Aggregate on purpose, and
        #: **not** per route: a route name is path-shaped, `/health` is the one
        #: unauthenticated route, and its rule is that nothing path-shaped
        #: appears on it (`test_server_core.py` asserts that with a regex over
        #: the whole body). Totals answer the operational question — is it
        #: serving, is it being asked for routes that do not exist, is a
        #: handler failing — without publishing the URL table to an
        #: unauthenticated caller. A counter that is collected and never served
        #: is worse than none: it reads as an observability feature and is not.
        self.requests = {"handled": 0, "notFound": 0, "failed": 0}

    # --- assets ----------------------------------------------------------

    def asset(self, name, content_type, *, inject=False) -> Response:
        """One `touch-visual/` file, or a 503 naming what is missing.

        503 rather than 404 on a missing asset: the route exists and the
        server is fine, the *page* has not been written yet (it is sp-13's
        file). A 404 there would read as "wrong URL" and send the reader
        looking in the wrong place.
        """
        if not self.assets:
            raise HttpError(503, "no touch-visual/ directory is configured")
        base = os.path.realpath(self.assets)
        full = os.path.realpath(os.path.join(base, name))
        if not full.startswith(base + os.sep):
            raise HttpError(404, "not found")
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            raise HttpError(503, f"touch-visual/{name} is not present yet") from None
        if inject:
            body = inject_token(body, self.auth.token)
        return Response(status=200, body=body, content_type=content_type)

    # --- dispatch --------------------------------------------------------

    def handle(self, method: str, route: str, query=None, headers=None) -> Response:
        """The whole request path: auth, static lookup, handler, error mapping."""
        query = query or {}
        headers = headers or {}
        if not self.auth.check(route, headers, query):
            return Auth.challenge()
        handler = ROUTES.get((method.upper(), route))
        if handler is None:
            self.requests["notFound"] += 1
            return Response.error(404, f"no route {method.upper()} {route}")
        self.requests["handled"] += 1
        try:
            return handler(self, query, headers)
        except HttpError as exc:
            return Response.error(exc.status, exc.message, headers=exc.headers)
        except (store_mod.StoreError, refs.RefError, ms.MongoStoreError,
                agents_mod.AgentsError, legacy_mod.LegacyError) as exc:
            # A malformed id that got past the syntactic validator, or a
            # document the store refuses: a 400 with the reason, never a 500 —
            # and never a traceback on the wire.
            return Response.error(400, f"{type(exc).__name__}: {exc}")
        except Exception as exc:                                    # noqa: BLE001
            # The last line of the same rule: a handler bug is a 500 naming the
            # exception *type* and nothing else. A traceback on the wire from a
            # server holding unredacted transcripts is a disclosure, and a
            # dropped connection would look like the socket died.
            self.requests["failed"] += 1
            return Response.error(500, f"handler failed: {type(exc).__name__}")

    def get(self, path: str, headers=None) -> Response:
        """Convenience: `get("/api/events?stream=x&after=1")`, for tests."""
        route, _, raw = path.partition("?")
        return self.handle("GET", route, urllib.parse.parse_qs(raw, keep_blank_values=True),
                           headers)


# --- the transport --------------------------------------------------------


class HttpServer:
    """asyncio HTTP/1.1 + WebSocket transport. The only part that owns a socket.

    Everything decidable without a connection lives in :class:`Api` and
    :class:`WsSession`; what is left here is: read a bounded head, parse it,
    hand it over, write the answer — plus the upgrade, which is the one place
    GD-13's Origin/Host allowlist is enforced.
    """

    def __init__(self, model, *, host=DEFAULT_HOST, port=DEFAULT_PORT, auth=None,
                 policy=None, assets=None, window=DEFAULT_REPLAY_EVENTS,
                 tick=TICK_SECONDS, coalesce=TOKEN_COALESCE_SECONDS,
                 keepalive=KEEPALIVE_SECONDS):
        self.model = model
        self.host = host
        self.port = int(port)
        self.auth = auth if auth is not None else Auth()
        self.policy = policy if policy is not None else OriginPolicy.default(host, port)
        self.window = window
        self.tick = float(tick)
        self.coalesce = coalesce
        #: The ping/`tick`-frame period. A parameter rather than the constant
        #: so a test can watch the idle marker the contract advertises without
        #: waiting 20 s for it — an unobservable frame is an unasserted one.
        self.keepalive = float(keepalive)
        #: Whether the Origin/Host allowlist was derived rather than given. An
        #: ephemeral bind (`port=0`) does not know its own authority until
        #: `start()`, and a derived allowlist built around port 0 would refuse
        #: the very page this server serves — so a derived policy is rebuilt
        #: once the real port is known and a caller-supplied one never is.
        self._derived_policy = policy is None
        self.ws_stats = {"clients": 0, "active": 0, "framesSent": 0, "rejected": 0}
        self.api = Api(model, auth=self.auth, policy=self.policy, assets=assets,
                       bind={"host": host, "port": self.port,
                             "loopback": host not in (OPEN_HOST, "::", "")},
                       ws_stats=self.ws_stats)
        self.connections = set()
        self._server = None

    # --- request parsing --------------------------------------------------

    @staticmethod
    def parse_head(raw: bytes):
        """`(method, route, query, headers)` from a request head. Never raises.

        `latin1` for the head (RFC 7230's byte range) and `parse_qs` with
        `keep_blank_values` so `?after=` arrives as an empty value rather than
        vanishing — "the parameter was given and is empty" is a 400 the API
        must be able to see.
        """
        head = raw.decode("latin1")
        first, _, rest = head.partition("\r\n")
        parts = first.split(" ")
        method = parts[0].upper() if parts else "GET"
        target = parts[1] if len(parts) > 1 else "/"
        route, _, raw_query = target.partition("?")
        headers = {}
        for line in rest.split("\r\n"):
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        return method, route, urllib.parse.parse_qs(raw_query, keep_blank_values=True), headers

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            try:
                raw = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_SECONDS)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                    asyncio.TimeoutError, ConnectionError, OSError):
                return
            if len(raw) > MAX_HEAD_BYTES:
                writer.write(Response.error(413, "request head too large").to_bytes())
                await writer.drain()
                return
            method, route, query, headers = self.parse_head(raw)
            if route == WS_ROUTE:
                await self.upgrade(reader, writer, query, headers)
                return
            # Handlers touch the filesystem (legacy scan, artifact walk, spill
            # read), so they run off the loop: a multi-MB read must never stall
            # a live socket (SERVER-5's precedent, GD-30's budget).
            response = await asyncio.to_thread(self.api.handle, method, route, query, headers)
            writer.write(response.to_bytes())
            await writer.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            self.connections.discard(task)
            try:
                writer.close()
            except Exception:                                       # noqa: BLE001
                pass

    # --- the upgrade ------------------------------------------------------

    async def upgrade(self, reader, writer, query, headers):
        """GD-13's WS gate: version, token, Origin/Host — then 101.

        Order matters and is deliberate: a malformed upgrade is answered as a
        malformed upgrade (400/426) before authentication, because telling a
        broken client it is broken leaks nothing; the token check comes next so
        an unauthenticated peer learns nothing about the Origin policy; the
        403 is last.
        """
        key = headers.get("sec-websocket-key")
        version = headers.get("sec-websocket-version")
        if version not in (None, "13"):
            writer.write(b"HTTP/1.1 426 Upgrade Required\r\nSec-WebSocket-Version: 13\r\n"
                         b"Connection: close\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return
        if not key:
            writer.write(Response.error(400, "not a WebSocket upgrade").to_bytes())
            await writer.drain()
            return
        if not self.auth.check(WS_ROUTE, headers, query):
            self.ws_stats["rejected"] += 1
            writer.write(Auth.challenge().to_bytes())
            await writer.drain()
            return
        refusal = self.policy.refusal(headers)
        if refusal is not None:
            self.ws_stats["rejected"] += 1
            writer.write(Response.error(403, refusal).to_bytes())
            await writer.drain()
            return

        writer.write(("HTTP/1.1 101 Switching Protocols\r\n"
                      "Upgrade: websocket\r\n"
                      "Connection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {ws.accept_key(key)}\r\n\r\n").encode("latin1"))
        await writer.drain()
        self.ws_stats["clients"] += 1
        self.ws_stats["active"] += 1
        try:
            await self.stream(reader, writer, query)
        finally:
            self.ws_stats["active"] -= 1

    async def stream(self, reader, writer, query):
        """Serve one socket: hello, bounded replay, the mode switch, then tail."""
        # Nothing here is fatal and nothing here is silent: the upgrade has
        # already succeeded, so there is no status code left to answer with,
        # and every parameter that could not be used is named on the hello
        # frame instead (`cursorsRejected`, `streamsRejected`, `fromApplied`).
        cursors, cursors_rejected = parse_cursor_params(query)
        from_raw = (query.get("from") or [None])[0]
        from_seq = int(from_raw) if from_raw and _SEQ_RE.match(from_raw) else None
        # `?from=abc` and no `?from=` at all both leave `from_seq` None, and
        # `fromApplied:false` is the answer to a third question ("did it pair
        # with one stream?"). The raw value comes along so the three cases stay
        # apart on the frame — the same rule `?cursor=`/`?stream=` follow.
        from_rejected = from_raw if from_raw and from_seq is None else None
        asked = list(query.get("stream") or [])
        selected = [s for s in asked if _is_stream(s)]
        streams_rejected = [s for s in asked if not _is_stream(s)]
        # `asked and not selected` serves NOTHING — an empty selection, not the
        # absent one. Widening a client's failed selector into "every stream in
        # the store" is GD-12's never-fall-back-to-another-target rule broken
        # by the query parser, and it is the same class of silence the
        # `?from=`/`?cursor=` reports above exist to end.
        session = WsSession(self.model, cursors=cursors, from_seq=from_seq,
                            streams=selected if asked else None,
                            window=self.window, coalesce=self.coalesce,
                            cursors_rejected=cursors_rejected,
                            streams_rejected=streams_rejected,
                            from_rejected=from_rejected)
        decoder = ws.FrameDecoder("server")
        closed = asyncio.Event()
        # `tick` runs on a worker thread while `pump` may be handling a
        # subscribe on the loop, and both move the same cursors and emit from
        # the same coalescer. One lock keeps a resume's backfill and a tick's
        # tail from interleaving into a frame order neither wrote.
        turn = asyncio.Lock()

        async def send(payload):
            writer.write(ws.encode_text(json.dumps(payload, default=json_default)))
            await writer.drain()
            self.ws_stats["framesSent"] += 1

        async def pump():
            """Read client frames: subscribe messages, pongs, and the close."""
            try:
                while not closed.is_set():
                    chunk = await reader.read(65536)
                    if not chunk:
                        break
                    for message in decoder.feed(chunk):
                        if message.opcode == ws.OP_CLOSE:
                            closed.set()
                            return
                        if message.opcode == ws.OP_PING:
                            writer.write(ws.encode_pong(message.data))
                            await writer.drain()
                        elif message.is_text:
                            try:
                                body = json.loads(message.text)
                            except ValueError:
                                continue
                            if isinstance(body, dict) and body.get("type") == "subscribe":
                                # Frames first, ack last: the ack publishes the
                                # cursors, and a cursor must never be announced
                                # before the records it names have gone out.
                                async with turn:
                                    for frame in await asyncio.to_thread(
                                            session.subscribe, body):
                                        await send(frame)
            except ws.ProtocolError as exc:
                try:
                    writer.write(ws.encode_close(exc.code))
                    await writer.drain()
                except (ConnectionError, OSError):
                    pass
                closed.set()
            except (ConnectionError, OSError, asyncio.CancelledError):
                closed.set()

        pump_task = asyncio.create_task(pump())
        try:
            # hello -> bounded replay (live:false) -> ONE mode switch -> tail.
            # Under the same lock the tail uses: `pump` is already reading, so a
            # `subscribe` arriving mid-replay would otherwise interleave its own
            # backfill with the handshake's and land its ack before the mode
            # frame — a boundary sp-13 keys off arriving after frames that
            # claim to precede it.
            async with turn:
                await send(session.hello())
                for frame in await asyncio.to_thread(session.replay):
                    await send(frame)
                await send(session.switch())
            last_ping = time.monotonic()
            while not closed.is_set():
                async with turn:
                    for frame in await asyncio.to_thread(session.tick):
                        await send(frame)
                now = time.monotonic()
                if now - last_ping >= self.keepalive:
                    # A protocol ping the browser answers in the network stack,
                    # and a `tick` frame the page can actually see: `onmessage`
                    # never fires for a pong, so a JS client watching a quiet
                    # run has no way to tell "idle" from "socket died" without
                    # one. Both, because they answer different questions.
                    writer.write(ws.encode_ping(b""))
                    await writer.drain()
                    await send({"type": "tick", "live": True,
                                "ts": datetime.datetime.now(datetime.timezone.utc)})
                    last_ping = now
                await asyncio.sleep(self.tick)
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            closed.set()
            pump_task.cancel()
            try:
                writer.write(ws.encode_close(ws.CLOSE_GOING_AWAY))
                await writer.drain()
            except (ConnectionError, OSError, asyncio.CancelledError):
                pass

    # --- lifecycle --------------------------------------------------------

    async def start(self):
        # `limit=` above MAX_HEAD_BYTES on purpose: with asyncio's default 64 KiB
        # stream limit — the same number — `readuntil` raises `LimitOverrunError`
        # first and the head-size check below it could never fire, so an
        # oversized head was answered with a silent close instead of the 413 the
        # code claimed to send. Room for one over-large head means the branch is
        # reachable and the client is told what happened.
        self._server = await asyncio.start_server(
            self.handle, self.host, self.port, limit=MAX_HEAD_BYTES * 2)
        if self.port == 0:                      # ephemeral port, for tests
            self.port = self._server.sockets[0].getsockname()[1]
            self.api.bind["port"] = self.port
            if self._derived_policy:
                self.policy = OriginPolicy.default(self.host, self.port)
                self.api.policy = self.policy
        return self._server

    async def close(self):
        for task in list(self.connections):
            task.cancel()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in (OPEN_HOST, "::", "") else self.host
        return f"http://{host}:{self.port}/"


# --- entry point ----------------------------------------------------------


def write_server_json(root, payload) -> str:
    """`.touch/server.json`, mode 0600 — the token's only resting place (GD-27).

    Same handling as `mongo.json`: created with 0600 from the start (never
    written then chmod'ed, which leaves a window where the token is
    world-readable), and never containing anything else that is secret.
    """
    directory = os.path.join(root)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    # `makedirs(mode=…)` is a no-op on a directory that already exists (the
    # store may have created `.touch/` first, under a different umask), so the
    # mode is asserted rather than merely requested. Defence in depth: the file
    # itself is opened 0600 below.
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    path = os.path.join(directory, "server.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.chmod(path, 0o600)
    return path


def default_tasks_root() -> str:
    """`--tasks-root` default: the served **project**'s task folders (GD-T5).

    Named, not inlined in :func:`main`, so the provenance of each root is
    assertable on its own — the two defaults here used to share one `repo`
    variable derived from the package's own location, which is precisely why
    one of them being wrong (CM-2) was undetectable.

    It **delegates** rather than re-joining the two components: this line used
    to spell the same root as `legacy.TASK_ROOT` a second time, so a move had to
    be made twice and a half-landed move showed up as the dashboard and the API
    listing different tasks for one cwd (LAYOUT-8, PROTOCOL-8).
    """
    return paths.tasks_root()


def default_assets() -> str:
    """`--assets` default: `touch-visual/` beside the **package** (CM-2).

    The one root that genuinely belongs to the package: the page ships with the
    code and is read-only, so it follows the code into the plugin cache. Do not
    "fix" this one onto the project root along with the others.
    """
    return os.path.join(paths.plugin_root(), "touch-visual")


def _usage() -> str:
    return (
        "usage: python3 -m aggregator.server [--host H] [--port P] [--open]\n"
        "                                    [--touch-root DIR] [--tasks-root DIR]\n"
        "                                    [--claude-root DIR] [--assets DIR]\n"
        "                                    [--allow-origin O] [--allow-host H]\n"
        "\n"
        "  --open           bind 0.0.0.0 (GD-13 opt-in). In a sandbox, publish the\n"
        "                   port from the host with:\n"
        "                     sbx ports $SANDBOX_VM_ID --publish 8932:8932/tcp\n"
        "                   Never publish the mongod port (GD-27: Mongo stays\n"
        "                   loopback-only; its port comes from .touch/mongo.json,\n"
        "                   never from source).\n"
        "  --allow-origin   add an allowed Origin for the WS upgrade (repeatable)\n"
        "  --allow-host     add an allowed Host header value (repeatable)\n"
    )


def main(argv=None) -> int:
    """Run the server. Prints the URL **with the token**, once, on stdout.

    The token has to reach a human somehow and this is the least-bad channel:
    it is per-boot, the process is local, and the alternative (a token the user
    types) is a token that gets reused. It is also written to
    `.touch/server.json` (0600) so a local client can find it without scraping
    a log.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    host, port = DEFAULT_HOST, DEFAULT_PORT
    touch_root = tasks_root = claude_root = assets = None
    origins, hosts = [], []
    while argv:
        arg = argv.pop(0)
        if arg in ("-h", "--help"):
            print(_usage())
            return 0
        if arg == "--open":
            host = OPEN_HOST
        elif arg == "--host" and argv:
            host = argv.pop(0)
        elif arg == "--port" and argv:
            try:
                port = int(argv.pop(0))
            except ValueError:
                print("--port takes an integer", file=sys.stderr)
                return 2
        elif arg == "--touch-root" and argv:
            touch_root = argv.pop(0)
        elif arg == "--tasks-root" and argv:
            tasks_root = argv.pop(0)
        elif arg == "--claude-root" and argv:
            claude_root = argv.pop(0)
        elif arg == "--assets" and argv:
            assets = argv.pop(0)
        elif arg == "--allow-origin" and argv:
            origins.append(argv.pop(0))
        elif arg == "--allow-host" and argv:
            hosts.append(argv.pop(0))
        else:
            print(f"unknown argument {arg!r}\n\n{_usage()}", file=sys.stderr)
            return 2

    store = store_mod.Store(touch_root)
    model = ReadModel(
        state={},
        store=store,
        tasks_root=tasks_root or default_tasks_root(),
        claude_root=claude_root,
    )
    server = HttpServer(model, host=host, port=port,
                        policy=OriginPolicy.default(host, port, origins=origins, hosts=hosts),
                        assets=assets or default_assets())

    async def run():
        try:
            await server.start()
        except OSError as exc:
            print(f"cannot bind {host}:{port} ({exc})", file=sys.stderr)
            return 1
        url = f"{server.url}?token={server.auth.token}"
        print(f"touch aggregator listening on {host}:{server.port}")
        print(f"open: {url}")
        try:
            path = write_server_json(store.root, {
                "token": server.auth.token, "url": url,
                "host": host, "port": server.port, "pid": os.getpid(),
            })
            print(f"token written to {path} (0600)")
        except OSError as exc:
            print(f"could not write .touch/server.json ({exc}); the URL above is the token",
                  file=sys.stderr)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await server.close()
        return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":                                          # pragma: no cover
    raise SystemExit(main())
