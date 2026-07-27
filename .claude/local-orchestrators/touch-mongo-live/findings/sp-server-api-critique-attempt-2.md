# sp-server-api — adversarial critique, attempt 2

**Verdict: REJECTED.** 0 blockers, 2 majors, 4 minors, 3 nits.
Depth: **in-scope** — every fix lands inside the three owned files
(`aggregator/server.py`, `tests/test_server_core.py`, `tests/test_api.py`);
none of them needs another module, another sub-plan, or new research.
critical_defect: **false**.

Reviewed: full content of the three untracked files (no diff base) against
`touch-mongo-live-subplans.md` §sp-12, R-30/R-31 (base), R-55/GD-21…GD-30
(amendment), GD-11/GD-12/GD-13/GD-20/GD-23.

## Attempt-1 findings: all thirteen verified fixed

Re-checked each by execution, not by reading the diff:

| # | attempt-1 finding | status |
|---|---|---|
| B1 | `27017` literal in `_usage()` | fixed — numeral gone (`server.py:2396`), the owned assertion now pins the *prose* (`test_server_core.py:509-515`), and `python3 tests/test_mongo_deploy.py` exits **0** including the live `mongo:7` Docker arm |
| B2 | timeline lost records on a paged round-trip | fixed — the cursor is the whole sort key `(lineNo, _id)` (`_row_key`, `server.py:1016-1024`), and `test_the_timeline_cursor_is_the_whole_sort_key…` asserts **set equality** with the unpaged answer over the frozen 671-record corpus at limits 100 and 7 |
| B3 | header-injection + `UnicodeEncodeError` via `X-Touch-Basename` | fixed in one place — `header_value()` (`server.py:253-270`) scrubs CR/LF/NUL and non-latin-1 for *every* header, the name is validated and raises `ServerError`, and the basename is percent-encoded at `:1841` |
| M1 | unknown `run=`/`stream=` answered 200 + empty list | fixed — `server.py:1690-1692` 404s, with a test that names the run |
| M2 | "current run" was the alphabetically largest | fixed — `_current_run_stream` orders by stream-file mtime (`:1262-1291`) with a test whose fixture is deliberately anti-alphabetical |
| M3 | handlers iterated the shared state dict on a worker thread | fixed — `_snapshot()` + `ReadModel.bucket/lookup/sizes/state_snapshot` (`:736-831`), and a 120-request test under `setswitchinterval(1e-6)` with a churning writer, all 200 |
| M4 | `/health` published `~/.claude` paths on the open route | fixed — `target_hash()` (`:909-924`), `store: {configured, streamCount}`, and a test asserting *nothing path-shaped* is on the open route |
| m1–m5, n1, n2 | flag/limit=0/413/`?from=`/late-stream backfill/route groups/dir mode | all fixed, each with a behavioural assertion |

Both owned suites are green (`test_server_core.py` rc 0, `test_api.py` rc 0),
ownership is clean (only the three files carry 19:12–19:15 mtimes, `HEAD` is
still `579446e`, no commit), and the checklist items I could falsify by
execution — GD-21 (no `pymongo`/`bson`/`dns`/`mirror` import; every absolute
import stdlib), GD-22 (no DB call on any liveness path; `/health` reports a
raising mirror as `down` rather than 500), GD-24/25/26 (no `_id` construction,
no `$`-verb, no `$unset`, no TTL index, no subtraction on a token field —
AST- and tokenizer-guarded), GD-27 (no credential in any response; `.touch/`
is gitignored so `server.json` cannot be committed), GD-29, GD-30 — are clean.
`store.validate_stream`/`stream_path` were re-attacked with `..`, `.`, `#` and
`|` payloads through `?run=`/`?stream=`; containment holds.

So this rejection is narrow: two of them are contract statements that are
false about the code they document, in the one docstring the plan says sp-13
copies verbatim, plus one silent-drop that is the exact defect attempt 2 just
fixed for a *different* parameter.

---

## MAJORS

### M1 — the hello frame's `oldest`/`truncated` are structurally always empty, and the normative frame table says otherwise
`aggregator/server.py:54-76` (the wire contract), `:1320-1340` (`hello`),
`:1395-1406` (`switch`), `:1369-1393` (`_emit_backfill`)

The module docstring is introduced as *"The wire contract (R-55), restated here
because sp-13 restates it verbatim"*, and its frame table reads:

```
{"type":"hello","live":false,"mode":"replay","streams":{...},
 "cursors":{"<stream>":<seq>},"oldest":{"<stream>":<seq>},
 "truncated":{"<stream>":true},"window":500,"reducerVersion":"1"}
{"type":"mode","live":true,"mode":"tail"}        <- the ONE boundary
```

Both lines are wrong, in opposite directions. `self.oldest` and
`self.truncated` are populated **only** in `_emit_backfill` (`:1380-1383`),
which runs during `replay()`; `HttpServer.stream` sends `hello()` *before*
`replay()` (`:2300-2303`). Reproduced against a store with a 50-record run
stream, a 50-record session stream and `window=5`:

```
HELLO  oldest: {}  truncated: {}
(55 replay frames)
SWITCH oldest: {'session:1234-1700000000': 46, 'run:wf_aaaaaaaa-aaa': 1}
       truncated: {'session:1234-1700000000': True}
```

`hello()`'s own docstring — *"Declares the mode, the window and where to load
older"* — is false for the same reason, and the docstring bullet at `:74-76`
(*"whatever the window cut off is reported per stream as `oldest`/`truncated`
so the page's 'load older' button knows it has work and where to start"*)
attaches the fact to the wrong frame.

Why this is major rather than cosmetic: the keys are **present and empty**,
not absent. sp-13 is instructed to restate this contract verbatim, so the
page it writes will read `hello.truncated` (`{}` — falsy), conclude nothing
was cut, and never render the load-older affordance R-55 requires — while the
real anchors sail past on an undocumented `mode` frame. A missing key would
have crashed loudly in development; an empty one renders a wrong UI silently.
`switch()`'s docstring already knows the truth (*"both are only known after
the replay"*) — the contract at the top of the file was simply not updated to
match it.

**Fix:** make the table describe the code. Move `cursors`/`oldest`/`truncated`
onto the `mode` frame in the docstring at `:58-65`, drop them from the hello
example (or keep them and say explicitly that hello's copies are the
*client-supplied* position and are empty on a fresh connect), and reword
`hello()`'s docstring so it stops promising load-older anchors it cannot have.
Add a test to `test_api.py` that walks hello → replay → switch and asserts the
anchors are on the frame the contract names, with the truncated stream's real
`oldest` — nothing in either suite currently asserts `oldest`/`truncated` on
any frame, which is why the drift survived.

### M2 — one malformed `?cursor=` on the WS handshake silently discards **every** resume position
`aggregator/server.py:2245-2248` (`stream`), `:1477-1492`
(`parse_cursor_params`)

```python
try:
    cursors = parse_cursor_params(query)
except HttpError:
    cursors = {}
```

`parse_cursor_params` raises on the *first* bad entry, so a handshake carrying
three cursors of which one is malformed loses all three. Reproduced with a
valid `run:wf_aaaaaaaa-aaa#000000000005` plus a junk `"garbage"`:

```
parse_cursor_params -> HttpError 400 "malformed cursor 'garbage'"
stream() swallows it -> cursors {} -> replay() emits 10 frames
                                      (records 1-10, all already held)
hello.resumed = False
```

That is R-55's named failure — *"reconnect mid-stream ⇒ no duplicate events"* —
reached by a client that got 2 of 3 cursors right. It is also the **same defect
class attempt 2 just fixed for `?from=`**: after the 101 there is no status code
left, so the fix was to publish `fromApplied` on the hello frame
(`:1293-1301`, `:1334`) rather than drop the parameter silently. `?cursor=`
never got the same treatment, and it is the parameter with the larger blast
radius.

Second half, and the reason this is a contract defect and not just a
robustness one: `parse_cursor_params`' docstring says *"A malformed one is a
400: unlike the socket's `subscribe` (where ignoring is kinder than dropping
the connection), a handshake has nothing to recover to."* The function has
exactly one caller (`stream`) and that caller catches the 400 and continues, so
**no client can ever observe the documented 400**. The docstring describes a
policy the code does not implement.

**Fix:** parse per-entry — keep the well-formed pairs, collect the rejected raw
strings, and publish them on the hello frame the way `fromApplied` is published
(`"cursorsRejected": ["garbage"]`, or `"cursorsAccepted"/"cursorsRejected"`).
Then correct the docstring to say the handshake reports rejects on the hello
frame while `subscribe` reports them in its ack (which it already does, at
`:1473-1474` — the two paths should read the same). Add a test: two cursors,
one junk, assert the good one still resumes and the junk one is named in
hello.

---

## MINORS

### m1 — a malformed `?stream=` makes the socket serve **every** stream
`aggregator/server.py:2251-2254`

```python
selected = [s for s in (query.get("stream") or []) if _is_stream(s)] or None
```

If the only `?stream=` given is malformed, `selected` is `None`, which
`WsSession.streams()` reads as "no selection" and serves the whole store.
Reproduced: `?stream=run:nonexistent#bad` →
`['session:1234-1700000000', 'run:wf_aaaaaaaa-aaa']`. The client asked for one
target and got a superset — the "never a fallback to another target" rule
(GD-12) that this file's own module docstring cites, arriving through the
query parser. `hello.streams` does publish what is being served, so it is
observable, but so was `?from=` before it got `fromApplied`.
**Fix:** keep the same shape as M2's — if any `?stream=` was given and none
survived validation, serve nothing and publish `streamsRejected` on hello (or
refuse the upgrade with a 400 *before* the 101, which is still available at
`upgrade()`).

### m2 — a resume further back than `MAX_REPLAY_EVENTS` produces a real gap while the docstring promises none
`aggregator/server.py:77-81` (contract), `:1356-1358` (resume arm)

The contract says resume yields *"exactly the records after them. No
duplicates, no gap"*. The resume arm caps at `MAX_REPLAY_EVENTS` (5000), so a
client whose cursor is 6000 records back is silently advanced to the newest
5000 — a gap. It *is* reported (`truncated`/`oldest`, via the `mode` frame —
see M1), which is the right behaviour; only the sentence is wrong.
**Fix:** finish the sentence — "…no gap, up to `MAX_REPLAY_EVENTS`; a deeper
backlog is truncated and reported as `truncated`/`oldest`, and `?before=` is
how the page walks the rest." One test with a 6000-record stream and a cursor
at 1 pins it.

### m3 — `tick()` advances the cursor past a token record the coalescer is still holding
`aggregator/server.py:1439-1448`

```python
self.cursors[stream] = seq
if _is_token_record(record):
    released = self.coalescer.offer(stream, record, now)
    if released is None:
        continue
```

The cursor moves before the hold decision, so `self.cursors` can name a seq
that was never sent — and `switch()` (`:1405`) and `subscribe()` (`:1473`)
publish that dict to the client. Reproduced: three absolute token records for
one agent; after the third is held, the `subscribe` ack returns
`{'run:wf_tok00000-aaa': 3}` while seq 3 has not gone out and
`coalescer.pending == 1`. A client that adopts the ack cursor and reconnects
inside the ≤1 s window skips the record permanently — and if it was the last
token record of a finished agent, the page shows a stale count forever. Narrow,
but it is exactly the "silently low counters, and nothing in the UI can notice"
failure the module docstring opens with.
**Fix:** advance the cursor only for records actually emitted, and let
`due()`'s release set it (`self.cursors[stream] = max(current, seq)` at the
point of `out.append`). Test: hold a token frame, take the ack cursors, build a
new `WsSession` from them, assert the held record still arrives.

### m4 — `Content-Type` bypasses the header sanitizer the same method just installed
`aggregator/server.py:311-321`

`head_bytes` routes `self.headers` through `header_value()` and validates every
name, but interpolates `self.content_type` directly into the head. Every
content type is server-authored today, so this is defence-in-depth only — but
the whole point of B3's fix was *"the sanitizer lives here rather than at any
one call site: every header this server will ever add passes through it,
including the ones a later change adds without reading this docstring."* One
field in the same method is exempt from that sentence.
**Fix:** `f"Content-Type: {header_value(self.content_type)}"`, and extend
`test_a_header_value_can_never_split_the_response` with a CRLF-bearing
`content_type`.

---

## NITS

### n1 — `query_source.find(collection, criteria, limit=)` is a seam nothing in the repo implements
`aggregator/server.py:1937-1939`. `grep -rn "def find" aggregator/` finds only
`find_spawns`, `find_persisted_output`, `find_snapshot`, `find_run_dirs` — no
`find(collection, criteria, limit=)` on `mongo_store`, `mirror` or anything
else; the only implementation is `FakeQuerySource` in `tests/test_api.py:600`.
The injection is the right shape (GD-22 keeps Mongo optional and this file may
not import a driver), but an interface with one fake and no producer drifts.
**Fix:** state the expected provider and the exact signature in `h_query`'s
docstring ("whatever sub-plan wires the Mongo read supplies
`find(collection, criteria, limit) -> iterable[dict]`"), so the next
implementer matches it instead of inventing `query()`.

### n2 — `inject_token`'s docstring is true of only one of its two arms
`aggregator/server.py:690-712`. *"The token is JSON-encoded, so a value
containing a quote or a `<` cannot break out of the string"* describes the
`<script>` fallback; the `__TOUCH_TOKEN__` arm (`:704`) substitutes the raw
token. Harmless for `token_urlsafe`, and the docstring even says the safe
alphabet is "not a reason to build the string by hand" — which is exactly what
the placeholder arm does.
**Fix:** say the placeholder arm substitutes the raw token and that sp-13 must
therefore place `__TOUCH_TOKEN__` where a raw URL-safe token is valid.

### n3 — `Api.hits` is collected and never published
`aggregator/server.py:2039`, `:2077`. Per-route hit counts would be a natural
`/health` operational field (counts, not observations — exactly what the open
route is for). Either publish them or drop the dict.

---

## Checklist items verified clean (re-run, not inherited)

- **GD-21** — `ast`-walked imports: `{asyncio, datetime, hashlib, hmac, json,
  os, re, secrets, sys, time, urllib, dataclasses, __future__}`; no `pymongo`,
  `bson`, `dns`, no `mirror`. `tests/test_mongo_deploy.py` rc 0.
- **GD-22** — every route reads `model.state`/`model.store`; `/api/query`'s
  Mongo arm is injected, optional and labelled; `/health` over a raising mirror
  returns `state: down`, `ok: true`.
- **GD-24/25/26** — no `_id` built here (only `store.cursor_key`, which
  round-trips through `parse_cursor_key`); grep for `$set`/`$inc`/`$unset`/
  `deleteMany`/`expireAfter` finds nothing (the only `TTL` is
  `REDUCE_TTL_SECONDS`, a reduction cache); the AST guard on token subtraction
  and the tokenizer guard on the word "delta" both hold.
- **GD-27** — no token/URI/password on any response; `.touch/` is gitignored
  (`.gitignore:20-21`) so `server.json` cannot be committed; `/health` carries
  no path, no uuid, no run id (asserted with a real `tailer.Tailer` and a real
  `store.Store`).
- **GD-29/GD-30** — no client held; `tick()` is `store.follow`-incremental with
  a per-stream checkpoint; `REDUCE_TTL_SECONDS` bounds reduction cost; token
  coalescing is ≥1 s and wired to the store's real `kind: "token"`
  (`store.KNOWN_KINDS`).
- **GD-15 / ownership** — only `aggregator/server.py`,
  `tests/test_server_core.py`, `tests/test_api.py` changed; `git status`
  otherwise unchanged; `HEAD` still `579446e`.
- **Tests assert behaviour** — the two suites I could most easily dismiss as
  tautologies are not: the timeline paging test runs the frozen 671-record
  corpus and asserts set equality plus order at two page sizes, and the
  current-run test builds an anti-alphabetical fixture with real `os.utime`
  mtimes. The concurrency test drives 120 real requests against a churning
  writer. No skip-without-mongod arm is needed here — this file touches no
  database.
