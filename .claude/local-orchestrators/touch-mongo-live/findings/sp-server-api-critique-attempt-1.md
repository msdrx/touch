# sp-server-api — adversarial critique, attempt 1

**Verdict: REJECTED.** 3 blockers, 4 majors, 5 minors/nits.
Depth: **in-scope** — every fix lands inside the three owned files
(`aggregator/server.py`, `tests/test_server_core.py`, `tests/test_api.py`).
critical_defect: **false**.

Reviewed: full content of the three new files (untracked tree, no diff base),
against `touch-mongo-live-subplans.md` §sp-12, R-30/R-31 (base plan),
R-55/GD-21…GD-30 (amendment), GD-12/GD-13/GD-20 (base).

What is genuinely right, so the rejection is not read as a verdict on the whole
file: the static `(method, route)` table with no prefix match and no default
handler; the malformed-400 / unknown-404 split living in ONE validator; the
constant-time token with three carriers and the `?token=` rationale;
`safe_artifact_path` containment (symlink case included) and the CSP-sandbox +
nosniff headers; the absolute-token wire and the AST/tokenizer source guards
that pin it; zero driver imports (GD-21 clean); no write verb, no `$unset`, no
TTL, no `_id` construction anywhere (GD-25/GD-26/GD-24 untouched by this file);
`/health` never 500s over a raising mirror (GD-22). The socket state machine
being testable without a socket is the right decomposition.

---

## BLOCKERS

### B1 — `_usage()` reintroduces the literal `27017`; the owned test *asserts* the violation
`aggregator/server.py:2124` and `tests/test_server_core.py:354`

```python
# server.py:2124
"                   Never publish 27017 (GD-27: Mongo stays loopback).\n"
```
```python
# test_server_core.py:354
check("sbx ports" in server_mod._usage() and "27017" in server_mod._usage(), ...)
```

`tests/test_mongo_deploy.py:283-286` (sp-06's, PASSING in the prior baseline)
runs a static guard over `aggregator/*.py` rejecting any line containing
`27017` whose escape is a preceding `#`. This is a string, not a comment, so
the escape does not apply. Reproduced: `python3 tests/test_mongo_deploy.py`
exits 1 with `server.py hardcodes no mongod port — lines [2124]`; every other
assertion in that file, including the live `mongo:7` Docker arm, passes. Sole
attribution to this sub-plan.

The second half is what makes this a blocker rather than a one-line typo: the
owned test at `test_server_core.py:354` *requires* the numeral to be present.
The two suites now assert contradictory things about the same string, so the
next implementer who "fixes" only `server.py` turns a red suite into a
different red suite.

**Fix:** drop the numeral from the help text (`"Never publish the mongod
port (GD-27: Mongo stays loopback)."`) **and** change the assertion at
`test_server_core.py:354` to `"sbx ports" in _usage()` plus
`re.search(r"(?i)never publish the mongod port", _usage())`. Do **not** add a
`#` to dodge the guard — the guard's escape exists for prose about the port,
not for making a literal invisible.

### B2 — `/api/session/timeline` silently **loses records** on a pagination round-trip
`aggregator/server.py:1379-1384` (the `> since` filter) and `1393` (`nextSince`)

`since` is an exclusive **`lineNo`**, and `nextSince` is the last `lineNo` on
the page. That is only sound if `lineNo` is unique per session. It is not:
`records` for one `sessionId` are ingested from **every** file in the session
directory (the session transcript **plus** `subagents/**/agent-*.jsonl`), each
numbered from line 1, and `ingest.read_transcript` keys `records` by uuid with
the record's own `sessionId` — so nine files contribute nine documents with
`lineNo == 1` under one sessionId. (`stream_meta` is safe — R-47 restricts
positional keys to the session's own transcript — but `records` is not.)

Reproduced against the **frozen fixture corpus**, not a synthetic case:

```
tests/fixtures/mirror/live-run-shape/a8d43bb1-.../   (10 .jsonl files)
  total records for that sessionId : 671
  100 distinct lineNo values carry 9 documents each
  full page  (limit=1000)          : 671
  paged round-trip (limit=100)     : 640 unique, 0 duplicates, 31 LOST
```

Every page boundary that lands inside a `lineNo` group discards the rest of
that group forever — the client has no cursor that can reach it. R-31's own
test line is "pagination round-trip without duplicates"; this passes that
letter while breaking the stronger property nobody wrote down because it was
assumed.

`tests/test_api.py:232-248` does not catch it because its fixture is five
hand-built records with five distinct `lineNo`s — the assertion
`len(seen) == 5` is a tautology w.r.t. the real corpus.

**Fix:** make the cursor the full sort key, not a prefix of it. The page is
already sorted by `(lineNo, str(_id))` at `:1384`, so page on that pair:
accept `?since=<lineNo>&sinceId=<_id>` (or one opaque `<lineNo:08d>#<_id>`
token, mirroring the `(stream,seq)` grammar the rest of the file uses), filter
`(lineNo, _id) > (since, sinceId)`, and return it as `nextSince`/`nextCursor`.
Then extend `test_timeline_pages_without_duplicates` to page the
`live-run-shape` fixture and assert **set equality** with the unpaged answer,
not just "no duplicates" — the current assertion cannot fail.

### B3 — HTTP response-header injection (and a hard crash) via `X-Touch-Basename`
`aggregator/server.py:1594-1597`, sink at `aggregator/server.py:286-288`

`h_toolresult` copies `spill["basename"]` — derived from an **agent-authored
path string** in a transcript (`ingest.py:677`,
`os.path.basename(os.path.normpath(path))`) — straight into a response header.
`Response.head_bytes` joins headers with `\r\n` and does no validation.
POSIX filenames may contain CR and LF. Reproduced:

```
basename = "evil.txt\r\nX-Injected: yes\r\nSet-Cookie: a=b"
-> HTTP/1.1 200 OK
   ...
   X-Touch-Basename: evil.txt
   X-Injected: yes
   Set-Cookie: a=b
```

A basename containing `\r\n\r\n` splits the response: the attacker-controlled
half is served from this origin **without** the `Content-Security-Policy:
sandbox` header this file adds two lines above, which is precisely the
protection GD-13 requires on served files. That the route needs the token is
mitigation, not a defence — the page holds the token by design (`inject_token`).

Same sink, second failure mode, no attacker needed: a non-latin-1 basename
(`"文件.txt"`) makes `head_bytes()` raise `UnicodeEncodeError`. That happens in
`HttpServer.handle` at `:1929`, **outside** `Api.handle`'s try/except and not
among the caught `(ConnectionError, OSError, asyncio.CancelledError)` — the
connection is dropped with no response and a traceback in the asyncio log.
Reproduced:
`Response(headers={"X-Touch-Basename": "文件.txt"}).to_bytes()` →
`UnicodeEncodeError: 'latin-1' codec can't encode characters in position 163-164`.

**Fix:** sanitize in `Response.head_bytes` (one place, protects every future
header): drop any header whose name or value fails
`^[!-~ \t]*$` after stripping, or `value.encode("latin1", "replace")` with
CR/LF/NUL removed — and raise `ServerError` for a name that fails, since a bad
name is a code bug. Additionally percent-encode the basename at `:1597`
(`urllib.parse.quote(spill.get("basename") or "", safe="")`), which is what
`Content-Disposition` does for the same reason. Add a test in
`test_server_core.py` asserting a CRLF-bearing and a non-ASCII basename both
produce a single well-formed response.

---

## MAJORS

### M1 — unknown `run=` / `stream=` answer **200 with an empty list** instead of 404
`aggregator/server.py:1444-1445`, `1450-1474`

```
GET /api/events?run=wf_totally-unknown
 -> 200 {"stream":"run:wf_totally-unknown","records":[],"count":0,
         "hasMore":false,"cursor":null,"head":"run:wf_totally-unknown#000000000000"}
GET /api/events?stream=run:nope            -> 200, same shape
```

R-31's test line is "unknown session/run/id ⇒ 404"; GD-12 is "Unknown ids →
404". The `session=` arm gets this right (`:1437-1438`) and
`/api/run/graph?run=wf_nope` gets it right (404, verified). Only the
`run=`/`stream=` arms of `/api/events` do not — they synthesize a stream id
and report a `head` cursor for a stream that has never existed, which is a
made-up fact about a made-up run.

This is also the exact failure this file's own `HttpError` docstring
(`:218-223`) names: *"the eighth would eventually get it wrong and answer 200
with an empty list — a wrong-target answer wearing a success code"*. The
eighth got it wrong.

**Fix:** after resolving `stream`, require it to exist — `if stream not in
model.store.streams(): raise HttpError(404, f"no stream {stream!r} has been
observed")` (and for the `run=` arm, phrase it as the unknown run). Add the
case to `test_api.py::test_a_bare_after_is_not_a_cursor` or its own test; note
that "unknown run ⇒ 404" is currently asserted only for `/api/run/graph`.

### M2 — "current run" is the **lexicographically largest** run id, not the newest
`aggregator/server.py:1118-1126`

```python
def _current_run_stream(self):
    """... Newest by the store's own order (a run stream is created when its
    run starts), not by a timestamp ..."""
    runs = [s for s in self.streams() if s.startswith("run:")]
    return runs[-1] if runs else None
```

The premise is false. `store.streams()` ends with `return sorted(found)`
(`aggregator/store.py:545-560`) — an alphabetical sort of `os.listdir`, which
carries no creation order at all. Run ids are `wf_<random hex>`. Reproduced,
appending in chronological order:

```
appended: wf_829e6f58-b2f, wf_b297177a-d11, wf_455b348c-e17   (newest last)
store.streams()      -> ['run:wf_455b348c-e17','run:wf_829e6f58-b2f','run:wf_b297177a-d11']
_current_run_stream() -> run:wf_b297177a-d11        # actual newest: wf_455b348c-e17
```

Consequences: R-55's "current run replays whole, others capped" replays the
wrong run in full and truncates the one the operator is watching; and
`hello()["currentRun"]` (`:1157`) publishes the wrong run id to sp-13, which
restates this contract verbatim and will render it.

**Fix:** order by an observed fact, not by name. The store already knows the
file — sort candidate `run:` streams by `os.stat(store.stream_path(s)).st_mtime`
(or by the first record's `seq`/ingest order), or take the run id from the
`runs` collection in `model.state` (which carries `startedAt`, an observed
harness field, and is already the reduction's input). Then correct the
docstring — it currently justifies a property `store.streams()` does not have.
Add a test that creates three run streams out of alphabetical order and asserts
the newest is the one replayed whole.

### M3 — API handlers iterate the shared state dict on a worker thread ⇒ intermittent 500s
`aggregator/server.py:1928` (`asyncio.to_thread(self.api.handle, ...)`) against
`ReadModel`'s contract at `:686-690`

`ReadModel` states the design explicitly: *"one dict, shared with whatever is
ingesting, so a tick's writes are visible to the next request without a copy or
a notification."* `mirror.py` / the ingest tick run on the asyncio event loop;
every handler runs in a `to_thread` worker. `.values()` iteration in
`records_of`, `stream_meta_of`, `nodes_of`, `h_sessions`, `h_toolresult`,
`h_query`, `h_health` and `agents.reduce` therefore races a concurrent
`dict.__setitem__`/`pop`. Reproduced (20 000 records, one mutating thread,
`sys.setswitchinterval(1e-6)`):

```
60 requests to /api/session/timeline -> 50 responses of
{"error":"Internal Server Error","status":500,
 "message":"handler failed: RuntimeError"}     # dict changed size during iteration
```

The blanket `except Exception` at `:1831` turns it into a 500 rather than a
crash, which is why nothing else notices — but GD-22's promise is that the live
view is *fully functional*, and a page whose sidebar 500s under load is not.

**Fix (any one, cheapest first):** (a) snapshot inside `ReadModel.bucket` —
`return dict(got)` — which bounds the race to one atomic-ish copy and is O(n)
only for the collection actually asked for; or (b) give `ReadModel` a
`threading.RLock` the writer also takes; or (c) have the ingest side publish an
immutable replacement dict per tick (`model.state = new_state`) so readers
always see a consistent generation — this is the option that also matches
GD-26's generation model. Whichever is chosen, say so in the `ReadModel`
docstring, because it currently documents the unsafe sharing as the design, and
add a test that mutates the state from a thread while requests run.

### M4 — the one unauthenticated route publishes `~/.claude` transcript paths, contradicting its own docstring
`aggregator/server.py:1300-1306` (docstring), `:812-816` (tailer `path`),
`:1323` (`store.root`)

`h_health`'s docstring: *"Deliberately says nothing about what is being observed
— no session ids, no paths from `~/.claude`, no token, no URI (GD-27)."*
`tailer_health()` emits `"path": getattr(entry, "path", None)`, and a real
`tailer.Tailer` (`aggregator/tailer.py:508`) holds exactly such a path.
Reproduced with **no token**:

```json
"tailers":[{"name":"session",
  "path":"/home/agent/.claude/projects/-home-laniakea-Projects-touch/a8d43bb1-0313-45d4-8784-4827af443ead.jsonl", ...}]
"store":{"root":"/tmp/tmpvang7n5z"}
```

That is the session uuid, the machine's home directory and the cwd-derived
project slug, to any unauthenticated caller. GD-13 puts a token on every route
*but* `/health` on the understanding that `/health` carries operational facts,
not observations; `test_health_never_carries_a_credential`
(`test_server_core.py:274-299`) only checks for the token and `mongodb://`, so
it passes.

Not a credential, so not a GD-27 deny-list breach — but it is a real
information disclosure on the deliberately-open route, and the docstring
asserting the opposite is the "docs match implemented behavior" clause failing
inside the file itself.

**Fix:** publish `os.path.basename(path)` plus a stable hash, or nothing at all
— `name`, `alive`, `missing`, `stalled`, `resets`, and the counters are what
AUDIT-15 asks for, and none of them needs the path. Same for `store.root`
(`{"configured": true}` is the operational fact). Extend the existing test to
assert no `/` -prefixed absolute path and no uuid appears in `/health` when a
real `Tailer` is registered.

---

## MINORS / NITS

### m1 — `flag()` contradicts its docstring for a valueless parameter
`aggregator/server.py:410-415`. Docstring: *"present-and-not-`0`/`false` is
True"*; the implementation puts `""` in the falsy set, so `?full` (the natural
hand-typed form) returns **False** — verified: `/api/session/timeline?...&full`
answers `"bodies": false`. Since `parse_head` deliberately uses
`keep_blank_values=True` so "given and empty" is visible, pick one: either drop
`""` from the falsy tuple, or reword the docstring to
*"present with a non-empty, non-`0`/`false` value"*.

### m2 — `?limit=0` yields an empty page that claims `hasMore: true`
`aggregator/server.py:1421`, `1467-1471`. `positive_int` accepts `0`
(`_SEQ_RE` matches `"0"`), so `/api/events?run=…&limit=0` returns
`count: 0, hasMore: true, cursor: null` — a client looping on `hasMore` never
terminates and has no cursor to advance with. Clamp to a minimum of 1, or 400
on `limit=0`.

### m3 — the 413 branch is unreachable
`aggregator/server.py:1917-1920` checks `len(raw) > MAX_HEAD_BYTES` (64 KiB),
but `reader.readuntil` hits asyncio's own 64 KiB stream limit first and raises
`LimitOverrunError`, which `:1914` swallows into a silent close. Either pass an
explicit larger `limit=` to `asyncio.start_server` so the check can fire, or
delete the branch and say in the comment that an oversized head is dropped
without a response.

### m4 — `?from=` is silently ignored unless exactly one stream is being served
`aggregator/server.py:1181-1183`: `elif self.from_seq is not None and
len(self.streams()) == 1`. R-55 specifies `?from=` "with a single stream
selector", but a client that sends `?from=` without `?stream=` gets the default
window with no indication its parameter was dropped. Either require the pairing
(and report the mismatch in the `hello` frame, the only channel left after the
101), or apply `from_seq` to the selected stream when `?stream=` is given
regardless of how many streams exist.

### m5 — a run stream that appears *after* the switch replays its backlog as `live:true`
`aggregator/server.py:1231-1244`: `tick()` calls `self.streams()` fresh each
time, and a stream with no cursor entry gets `since = 0`, so everything already
in it goes out with `live: true`. R-55's rule is that backfill paints once
without animation. Today the backlog is small (a run stream is created empty),
so this is a latent correctness issue rather than a visible one — but sp-13's
source guard keys off the `live` flag alone and cannot tell. Seed
`self.cursors[stream]` for a newly-seen stream from a bounded `live:false`
burst, as `replay()` does.

### n1 — R-30's "read-only vs control route groups" is not represented
`aggregator/server.py:1732-1747`. `ROUTES` is one flat dict; the only grouping
is `OPEN_ROUTES`. v0 ships no control route (correct — sp-13 renders no control
affordance), but R-30 names the *group* as part of the posture, and the point of
declaring it before any control endpoint exists is that the split is already
there when one arrives. A `READ_ROUTES` / `CONTROL_ROUTES` pair whose union is
`ROUTES`, with `CONTROL_ROUTES` empty and a test asserting it is empty, costs
four lines and makes the invariant enforceable.

### n2 — `write_server_json` does not repair an existing directory's mode
`aggregator/server.py:2104`: `os.makedirs(..., mode=0o700, exist_ok=True)` is a
no-op on mode when `.touch/` already exists (e.g. created by `store.Store` or a
prior run with a different umask). The file itself is correctly opened 0600, so
this is defence-in-depth only — but `test_server_json_is_0600:340` asserts the
directory mode and only passes because the temp dir is fresh. Add
`os.chmod(directory, 0o700)`.

---

## Checklist items verified clean

- **GD-21**: no `pymongo`/`bson`/`dns` import, no lazy import, no `mirror`
  import; every import is stdlib (asserted structurally by
  `test_server_core.py:399-413`, re-verified by hand).
- **GD-22**: every route reads `model.state` / `model.store`; `/api/query`'s
  Mongo arm is injected and optional and labels its source; `/health` reports
  `absent`/`down` without raising. No blocking DB I/O anywhere.
- **GD-24 / GD-25 / GD-26 / GD-28 / GD-29**: this file constructs no `_id`
  (only `store.cursor_key`, which round-trips through `parse_cursor_key`),
  writes no document, emits no delete verb, differences no token field (AST
  guard), and holds no Mongo client. `provenance` is projected, never invented.
- **GD-27**: no credential/URI in any response, prompt or file; `server.json`
  opened 0600 from the start.
- **GD-30**: `tick()` is checkpoint-incremental via `store.follow`;
  `REDUCE_TTL_SECONDS` bounds reduction cost; token coalescing is ≥1 s.
- **GD-15 / ownership**: only the three owned files exist as new work; no
  commits; no edits to other sub-plans' files.
