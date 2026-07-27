# sp-server-api — adversarial critique, attempt 4

**Verdict: REJECTED.** 0 blockers, 2 majors, 2 minors, 3 nits.
Depth: **in-scope** — both majors are fixed inside `aggregator/server.py` plus
a test each in `tests/test_api.py`. No other module, no other sub-plan, no new
research.
critical_defect: **false**.

Reviewed: full content of the three untracked owned files (`git status` shows
`?? aggregator/`, `?? tests/` — no diff base) against
`touch-mongo-live-subplans.md` §sp-12, R-30/R-31 (base plan), R-55 +
GD-21…GD-30 (amendment), and GD-11/GD-12/GD-13/GD-20/GD-22/GD-23.

Both owned suites re-run here: `tests/test_server_core.py` rc 0,
`tests/test_api.py` rc 0. Ownership clean — `find -newermt 20:05` returns
exactly `aggregator/server.py`, `tests/test_server_core.py`,
`tests/test_api.py`; `HEAD` is still `579446e`; nothing committed; `docs/` and
every other `aggregator/*.py` carry mtimes hours older.

## Attempt-3 findings: all five verified fixed

Re-checked by execution, not by reading:

| # | attempt-3 finding | status |
|---|---|---|
| M1 | a late-born stream's truncation recorded and never sent | fixed — the `anchors` frame (`:1596-1598`), on the contract table (`:70-71`, `:88-99`), and `test_a_late_streams_truncation_is_published_not_just_recorded` drives the 60-record/window-5 probe verbatim and asserts `oldest == 56` **and** frame order |
| M2 | `subscribe` acked a rewind and replayed nothing | fixed — `_resume` (`:1715-1738`) re-delivers `(seq, delivered]` as `live:false`, ack last, ahead-of-position refused by name; verified live: `subscribe({stream:3})` on a session at 10 emits seqs 4…10 then the ack, and `cursors` end at 10 |
| m1 | an unobserved `?stream=` became `currentRun` | fixed — `_observed()` + `_current_run_stream()` (`:1413-1467`), `streamsUnobserved` on hello (`:1541`), `currentRun is None` asserted |
| m2 | `tick()` emitted an unbounded number of frames | fixed *per stream* — `MAX_TICK_EVENTS` + `_carry` (`:1691-1696`), contiguity across the cap boundary asserted with real records (see m2 below for the residue) |
| m3 | a malformed `?from=` dropped without being echoed | fixed — `fromRejected` (`:1536`, `:2665`), three cases distinguishable |

So this rejection is again narrow, and again the same shape as the last two:
the file's own normative wire contract — the one sp-13 is instructed to restate
**verbatim** — is false about the code underneath it in two places, and both are
silent in exactly the way the module docstring says it refuses to be.

---

## MAJORS

### M1 — a stream the client *asked for* by name is the one stream born after the boundary that gets no backfill treatment
`aggregator/server.py:1672-1679` (`tick`'s late-stream arm), `:1603-1607`
(`_emit_backfill`'s cursor floor), `:1545-1570` (`replay`), contract at
`:88-99` and `:119-123`

The contract states this unconditionally:

> **A backfill that happens after `mode` carries its own `anchors` frame.** …
> a stream born after the boundary (an ingest pass discovering a new
> transcript) and a `subscribe` rewind are both painted `live:false` by the
> same code path. … Every post-boundary backfill therefore publishes
> `{"type":"anchors"}` naming its stream, its `oldest`, and whether anything
> was cut, immediately before the frames it describes.

and it names the flow that breaks it two clauses later:

> A selector that is well-formed but names a stream this store has never seen
> *is* served — that is how a client watches a run before it starts

The late-stream arm is gated on `if stream not in self.cursors`. But `replay()`
runs `_emit_backfill` over **every** stream in `self.streams()`, and
`_emit_backfill` floors an empty stream at `self.cursors[stream] = 0`
(`:1603-1607`). A `?stream=` selector for a not-yet-written run is in
`streams()` from the handshake, so it is in `cursors` from the handshake — and
when the run finally starts, `tick()` skips the backfill arm entirely and tails
it from seq 0 as live traffic.

Reproduced. Same store, same 6000-record append after `switch()`,
`window=5`, the only difference being how the client named the stream:

```
?stream= preselected            frames=5000  events=5000  live=[True]  anchors=0  firstSeq=1
?cursor= for the unborn stream  frames=5000  events=5000  live=[True]  anchors=0  firstSeq=1
no selector (documented door)   frames=6     events=5     live=[False] anchors=1  firstSeq=5996
```

Three consequences, and each is one the file argues against in its own prose.
(a) `live:true` on a 5000-frame backlog is precisely what `_emit_backfill`'s
docstring forbids — *"would animate a burst sp-13 cannot tell apart from real
activity, because the `live` flag is the only thing it has to go on"*.
(b) `window` is not applied at all: the bound on this path is `MAX_TICK_EVENTS`
(5000), a thousand times the window the same session honours on every other
stream, and `session.capped` ticks to 1 — the GD-30 write storm the cap was
added *this attempt* to stop, delivered to the one client that asked politely.
(c) No `anchors`, `mode.truncated == {}` and `session.truncated == {}`, so a
page that reads the contract literally is told nothing was cut on a stream
where the window says otherwise.

`test_a_selector_for_a_run_that_has_not_started_is_labelled_unobserved`
(`tests/test_api.py:972-998`) stops one line short of this: it asserts
`session.replay() == []` and never ticks after the run starts, so the half that
matters — what a client actually *sees* when the run it was waiting for begins
— is unasserted.

**Fix:** make the late-stream arm key off "has this session ever emitted a
frame for this stream", not off cursor presence. Concretely: track the streams
`_emit_backfill` has actually painted (`self._painted = set()`, added when a
window is emitted) and change `:1673` to
`if stream not in self._painted and stream not in self._checkpoints:` — an
unborn stream floored at 0 is not painted, so its first real backlog goes
through `_emit_backfill` and gets the window, the `live:false` flag and the
`anchors` frame, exactly as the unselected door does. `_resume`'s adopted
cursor must count as painted so a `subscribe` rewind is not re-backfilled.
Test: the probe above verbatim — `WsSession(model, streams=[ghost], window=5)`,
`replay/switch/tick`, append 6000 records, assert the next tick yields 5
`live:false` frames led by an `anchors` frame naming `oldest == 5996`, and that
it is byte-identical to the no-selector session's output.

### M2 — `?from=` is overridden by a `?cursor=` for the same stream and hello reports it applied
`aggregator/server.py:1469-1477` (`_from_applies`), `:1556-1568` (`replay`'s
precedence), `:1534-1536` (hello), contract at `:108-115`

The rule is the one this file is built around:

> **a handshake parameter is never silently dropped**: after the 101 there is
> no status code left to refuse with, so every parameter that could not be
> used is *named on hello* instead — `fromApplied:false` plus the raw
> `fromRejected` for a `?from=` that did not parse or did not pair with
> exactly one stream (**three cases the client can tell apart**)

There is a fourth case and it is reported as success. `_from_applies()` tests
only `from_seq is not None and len(self.streams()) == 1`; `replay()` checks the
cursor arm *first* (`:1558-1561`) and never reaches the `elif from_applies`
arm when a cursor exists for that stream. Reproduced against a 10-record
stream:

```
?stream=run:wf_probe0001-a&from=2&cursor=run:wf_probe0001-a#000000000005
  hello: {'from': 2, 'fromApplied': True, 'fromRejected': None,
          'cursors': {'run:wf_probe0001-a': 5}}
  replayed seqs: [6, 7, 8, 9, 10]        <- from=2 was ignored
  mode: truncated={}  oldest={stream: 6}
```

The client asked to replay explicitly from seq 2 — the documented purpose of
the parameter (`:101-107`: *"`?from=<seq>` … replays explicitly from there"*) —
received records 6…10, and was told on the handshake frame that its `from` was
applied. `truncated` is `{}` because `replay_window(from_seq=5)` genuinely cut
nothing relative to *5*, so the one field a page could notice a shortfall with
also says all is well. This is the attempt-2 M1 failure mode (a hello field
that structurally cannot be false when the client needs it true) inverted: a
hello field that is structurally `true` when the client needs it false, and
sp-13 will restate this table verbatim and code `if (!hello.fromApplied)`
against it.

`test_from_is_applied_or_reported_never_silently_dropped`
(`tests/test_api.py:824-851`) covers the no-parse arm and the many-streams arm.
It never sends `?from=` and `?cursor=` together, which is how this survived.

**Fix:** pick a precedence and make hello say it. Either (i) `?from=` wins for
the stream it names — move the `from_applies` check above the cursor check in
`replay()` and document that an explicit jump overrides a stale resume — or
(ii) the cursor wins and `_from_applies()` returns False when
`self.cursors.get(the single stream) is not None`, so hello publishes
`fromApplied:false` and the client sees its parameter was not used. (ii) is the
smaller change and matches `replay()`'s existing docstring. Test: the probe
above, asserting the replayed seqs and `hello["fromApplied"]` agree — whichever
way the precedence falls.

---

## MINORS

### m1 — two `?cursor=` values for one stream: last wins, silently
`aggregator/server.py:1835-1843` (`parse_cursor_params`)

```
?cursor=run:X#000000000003&cursor=run:X#000000000009
  -> accepted {'run:X': 9}   rejected []
```

The seq-3 pair is discarded and named nowhere. Every other repeated parameter
on this server is a refusal with a reason (`one()` at `:521-533`: *"`?session=a&session=b` has no correct answer and the tempting ones (first wins, last wins) are both a silent wrong target"*), and `?cursor=` is deliberately
repeatable so per-entry handling is the whole point of the function. Last-wins
is exactly the tempting answer `one()` refuses.

**Fix:** on a second cursor for a stream already accepted, keep the *lower*
seq (the conservative resume — a repaint beats a skip, the direction this file
always chooses) and push the discarded raw string onto `rejected` with a
reason, so hello names it. Extend
`test_one_malformed_cursor_costs_only_itself` with the duplicate arm.

### m2 — the per-tick cap is per *stream*; the socket's queue is still N × 5000
`aggregator/server.py:1691-1696`, `:253-257`, drained at `:2745-2747`

Measured with four streams and `MAX_TICK_EVENTS + 3` records appended to each:

```
frames emitted in ONE tick: 20000   capped counter: 4
```

The contract is honest about the shape (*"one tick emits at most
`MAX_TICK_EVENTS` frames per stream"*, `:143-144`), so this is not a false
statement — but GD-30's bounded queue is a property of the socket, and the
justification written beside the constant is about the socket
(*"one unbounded write storm on every connected socket before the loop could
sleep"*). A restarted ingest catching up writes to every stream at once, which
is the same scenario, multiplied by the number of streams.

**Fix:** carry the budget across the stream loop — one `budget =
MAX_TICK_EVENTS` decremented per emitted frame, with the remainder of every
stream pushed into `_carry` when it hits zero — and change `:143-144` to say
"per tick" rather than "per stream". The `_carry` machinery already does the
work; only the counter's scope changes. Test: the four-stream probe above,
asserting one tick emits at most `MAX_TICK_EVENTS` frames in total and the
remainder arrives contiguously.

---

## NITS

### n1 — a request that 500s is counted twice
`aggregator/server.py:2481`, `:2497`. `requests["handled"]` is incremented
before the handler runs and `requests["failed"]` after it raises, so
`handled + notFound` is the request total but `handled` is not "served". The
`/health` label reads as served/not-found/failed.
**Fix:** increment `handled` only on the success path, or rename it `dispatched`.

### n2 — hello mirrors a rejected raw `?cursor=`/`?stream=` untruncated
`aggregator/server.py:1539-1540`, `:1835-1843`. `subscribe`'s ack truncates its
echo at `MAX_REJECT_ECHO` for a stated reason (`:1775-1778`); the handshake path
echoes whatever the query string held, bounded only by `MAX_HEAD_BYTES`
(64 KiB) and repeatable. Not a vulnerability — the head is capped and the
socket is authenticated — but the same rule, applied on one of two doors.
**Fix:** run the hello echoes through the same `[:MAX_REJECT_ECHO]` slice.

### n3 — `anchors` frames carry `live:true` while the frames they introduce are `live:false`
`aggregator/server.py:1596-1598`. Consistent with `mode` (also `live:true`
while it closes a `live:false` replay), so this is a naming observation rather
than a defect — but sp-13's source guard is written about the `live` flag, and
an anchors frame is the one frame whose flag describes neither the records
before it nor the records after it.
**Fix:** state in the contract table that `live` on `mode`/`anchors` marks the
session's mode, not the frames they bracket.

---

## Checklist items verified clean (re-run this attempt, not inherited)

- **GD-21** — `ast`-walked imports of `server.py`:
  `{__future__, asyncio, dataclasses, datetime, hashlib, hmac, json, os, re,
  secrets, sys, time, urllib}` plus relative siblings. No `pymongo`, `bson`,
  `dns`; no `mirror` import; the strings `pymongo`/`bson` do not occur anywhere
  in the file, prose included.
- **GD-22** — every route reads `model.state`/`model.store`; `/api/query`'s
  Mongo arm is injected and labelled (`source`); `/health` over a store whose
  root does not exist answers 200 with `streamCount: 0` (verified live), and
  over a raising mirror answers `state: down` rather than 500.
- **GD-24/25/26** — no `_id` is constructed here (only `store.cursor_key`,
  which round-trips through `parse_cursor_key`); the strings `$inc`, `$set`,
  `$max`, `$unset`, `deleteMany`, `expireAfterSeconds` do not occur; tokens are
  carried verbatim, never differenced — the `ast.BinOp`/tokenizer guards in
  `test_server_core.py` are real (they strip comments and strings with
  `tokenize`, so the "no delta in executable code" assertion cannot be
  satisfied by prose).
- **GD-27** — no token, URI, password or path on any response; `target_hash()`
  keeps `/health` path-free; `.touch/server.json` is written 0600 via `os.open`
  with the mode in the call, its directory chmod'ed 0700, and `.touch*/` is
  covered by `.gitignore` (`git check-ignore -v .touch/server.json` →
  `.gitignore:21`), so the per-boot token can never reach a commit.
- **GD-13** — 127.0.0.1 default, `--open` opt-in with the `sbx ports` recipe,
  `hmac.compare_digest` on three carriers with no short-circuit,
  Origin/Host allowlist at the upgrade only (403 last, after 426/400/401),
  static `(method, route)` dict with a default 404 and no prefix match.
- **GD-28/29/30 (partial)** — no Mongo client held, no write path, no
  provenance manufactured (`_node_payload`/`_agent_payload` project `provenance`
  through); `tick()` is `store.follow`-incremental per stream; token coalescing
  is ≥1 s and keyed on the store's real `kind: "token"`. The GD-30 residue is
  m2 above.
- **GD-15 / ownership** — `find -newermt` over the attempt window returns only
  the three owned files; `docs/mongo.md` untouched; `HEAD` still `579446e`;
  no commit.
- **Tests assert behaviour, not tautologies** — spot-checked the ones easiest
  to dismiss: `_contract_frame_keys` parses the normative docstring table as
  data and asserts against it; `test_one_tick_cannot_write_an_unbounded_burst`
  drives `MAX_TICK_EVENTS + 7` real records and asserts contiguity across the
  cap boundary plus `session.tick() == []` after the drain;
  `test_subscribe_resumes_and_never_acks_a_position_it_did_not_send` builds
  four different subscribes on one live session and pins the tail afterwards.
  No mongod-dependent arm is needed in this file and none is present.
