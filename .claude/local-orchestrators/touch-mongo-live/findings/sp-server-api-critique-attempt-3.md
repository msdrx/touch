# sp-server-api — adversarial critique, attempt 3

**Verdict: REJECTED.** 0 blockers, 2 majors, 3 minors, 3 nits.
Depth: **in-scope** — every fix lands inside the three owned files
(`aggregator/server.py`, `tests/test_server_core.py`, `tests/test_api.py`).
No other module, no other sub-plan, no new research.
critical_defect: **false**.

Reviewed: full content of the three untracked files (no diff base — `git
status` shows `?? aggregator/`, `?? tests/`) against
`touch-mongo-live-subplans.md` §sp-12, R-30/R-31 (base plan), R-55 +
GD-21…GD-30 (amendment), and GD-11/GD-12/GD-13/GD-20/GD-22/GD-23.

Both owned suites re-run here: `tests/test_server_core.py` rc 0,
`tests/test_api.py` rc 0. Ownership clean — only the three files carry
19:37–19:43 mtimes, `HEAD` is still `579446e`, nothing committed.

## Attempt-2 findings: all nine verified fixed

Re-checked by execution, not by reading:

| # | attempt-2 finding | status |
|---|---|---|
| M1 | hello's `oldest`/`truncated` structurally always empty while the contract table said otherwise | fixed — hello carries neither key (`:1409-1425`), the table moved them onto `mode` (`:65-80`), and `test_the_load_older_anchors_are_on_the_frame_that_can_know_them` reads the docstring table *as data* (`_contract_frame_keys`) and asserts the real seq on the real frame |
| M2 | one malformed `?cursor=` discarded every resume position | fixed — `parse_cursor_params` is per-entry and returns `(accepted, rejected)` (`:1591-1615`); `cursorsRejected` on hello; `test_one_malformed_cursor_costs_only_itself` |
| m1 | a malformed `?stream=` served **every** stream | fixed — `asked and not selected` ⇒ serve nothing (`:2409-2421`), `streamsRejected` published, named test |
| m2 | resume deeper than `MAX_REPLAY_EVENTS` promised "no gap" | fixed — clause reworded (`:99-107`) and `test_a_resume_deeper_than_the_cap_is_declared_not_silently_gapped` drives a 6000-record stream |
| m3 | `tick()` advanced the cursor past a held token record | fixed — `_advance()` clamps to `pending_floor - 1` (`:1495-1511`); `test_a_held_token_frame_holds_the_cursor_behind_it` builds a *second* session from the published cursor and proves the held record still arrives |
| m4 | `Content-Type` bypassed `header_value` | fixed (`:349`) |
| n1 | `query_source.find(...)` seam undocumented | fixed — exact signature stated in `h_query`'s docstring (`:2048-2064`) |
| n2 | `inject_token` docstring true of one arm only | fixed — both arms described separately (`:733-745`) |
| n3 | `Api.hits` collected and never published | fixed — replaced by `self.requests` (handled/notFound/failed), published on `/health`, and `test_the_open_route_counts_requests_without_publishing_the_route_table` pins that it is totals-only |

So this rejection is again narrow, and again the same shape: two places where
the file's own normative wire contract — the one sp-13 is instructed to
restate **verbatim** — is false about the code underneath it, and both are
silent in exactly the way the module docstring says it refuses to be.

---

## MAJORS

### M1 — a stream born after the mode switch has its truncation computed, recorded, and never sent
`aggregator/server.py:1532-1539` (`tick`'s late-stream arm), `:1454-1478`
(`_emit_backfill`), `:1480-1493` (`switch`), contract at `:82-88`

The contract is unconditional:

> **bounded default replay**: … whatever the window cut off is reported per
> stream as `oldest`/`truncated` **on the `mode` frame**, so the page's "load
> older" button knows it has work and where to start

`switch()` is the only emitter of those two dicts and it runs exactly once,
before the tail. But `tick()` calls `_emit_backfill` for every stream that
appears *after* the switch (`:1533-1539`), and `_emit_backfill` writes
`self.truncated[stream] = True` / `self.oldest[stream] = …` (`:1465-1468`)
into session state that nothing will ever publish again.

Reproduced (window=5, a `session:` stream created after the switch with 60
records — an ingest pass backfilling a newly discovered transcript, which is
the normal way a stream is born):

```
switch anchors:        oldest={'run:wf_first0001-a': 1} truncated={}
late backfill frames:  5   live flags: {False}
first seq sent:        56   (of 60 records)
session.truncated:     {'session:9999-123456': True}     <- recorded
session.oldest:        {'session:9999-123456': 56}       <- recorded
frames carrying oldest/truncated: []                     <- never sent
any 'mode' frame from tick(): []
```

55 of 60 records are cut off the wire and the client is told nothing. It sees
five `live:false` frames beginning at seq 56, `hasMore`-equivalent nowhere,
and no anchor to call `/api/events?stream=&before=` with. This is precisely
the failure attempt 2's M1 was rejected for — *"a page that reads an
always-empty `truncated`, concludes nothing was cut and never renders the
'load older' affordance is a silently wrong UI"* — only reached through the
late-stream door instead of the hello door. sp-13 will restate the contract
verbatim, read the one `mode` frame, and have no way to learn that a stream it
is actively rendering is missing its first 55 records.

`test_a_stream_born_after_the_switch_is_backfilled_not_animated`
(`tests/test_api.py:870-891`) covers exactly one half of this path — the
`live:false` flag — with a 3-record backlog that cannot truncate. The
truncation half has no test, which is how it survived.

**Fix:** publish the anchors when they change after the switch. The `mode`
frame must stay the ONE boundary (sp-13 keys the replay→tail transition off
it), so add a distinct frame — e.g.
`{"type":"anchors","live":true,"stream":…,"oldest":…,"truncated":true}` —
emitted from `_emit_backfill` whenever it is called post-switch, and add it to
the contract table at `:56-69` plus the `:82-88` clause. Test: the probe above
verbatim (window=5, 60 records appended after `switch()`), asserting the
client receives an anchor naming `oldest == 56` for that stream.

### M2 — `subscribe` accepts a rewound cursor, acknowledges it, and replays nothing
`aggregator/server.py:1566-1588` (`subscribe`), `:1513-1564` (`tick`),
contract at `:99-102`

The contract makes `subscribe` a co-equal arm of resume:

> **resume**: the client sends its last `(stream, seq)` pair(s) — as
> `?cursor=<stream>#<seq:012d>` (repeatable) on the handshake, **or as a
> `{"type":"subscribe","cursors":{...}}` message** — and gets exactly the
> records after them

`subscribe` does `self.cursors.update(accepted)` and returns
`{"accepted": …, "cursors": …}`. It never replays. `tick()` reads
`store.follow(stream, self._checkpoints[stream])` (`:1542`), and that
checkpoint is already at EOF, so the records between the rewound cursor and
the checkpoint are never re-read and never sent. Reproduced on a live session
holding 10 records:

```
cursor after replay:  {'run:wf_sub0000001-a': 10}
subscribe ack:        {'type':'subscribed','live':True,
                       'cursors':{'run:wf_sub0000001-a':3},
                       'accepted':{'run:wf_sub0000001-a':3}}   <- says applied
frames re-delivered:  []                                       <- nothing
frames on next append: [11]
```

Two consequences, and the second is worse than the first. (a) The documented
mechanism is a no-op that reports success — the same "a parameter that could
not be used is named" rule this file enforces everywhere else, broken on the
one path that has a whole ack frame to name it in. (b) The session's own
position is now corrupted: `self.cursors[stream]` is 3 while seq 11 has been
delivered, so every subsequent `subscribe` ack publishes 3, and a client that
adopts it and reconnects replays 4…11 as duplicates — R-55's named test
("reconnect mid-stream ⇒ no duplicate events") failing by way of the API that
exists to prevent it.

`test_subscribe_updates_the_cursor_and_ignores_junk`
(`tests/test_api.py:1044-1054`) builds a session that has never replayed or
ticked, so it only ever moves the cursor *forward from nothing* and cannot see
either consequence.

**Fix:** pick one and make the ack say it. Either (i) treat a rewind as a real
resume — for each accepted cursor below the current one, clear
`self._checkpoints[stream]` and emit the `live:false` backfill for that range
before the ack (the `_emit_backfill` machinery already exists) — or (ii)
refuse a rewind: accept only cursors ≥ the current position, put the rest in a
`rejected` list on the ack with the reason, and leave `self.cursors`
untouched. Whichever is chosen, correct `:99-102` and `subscribe`'s docstring
to describe it. Test: a session that has replayed, switched and ticked to seq
10, then `subscribe({stream: 3})` — assert either the 4…10 backfill arrives or
the ack rejects the pair, and in both cases that `self.cursors` never names a
seq the client was not sent.

---

## MINORS

### m1 — a well-formed but unobserved `?stream=` is served silently *and* published as `currentRun`
`aggregator/server.py:1324-1332` (`streams`), `:1334-1363`
(`_current_run_stream`), `:2409-2421`

`streams()` returns the selection verbatim when one was given, and
`_current_run_stream()` maxes over `self.streams()` — so a typo'd but
syntactically valid run id becomes the socket's *current run*:

```
?stream=run:wf_doesnotexist ->
  hello: streams=['run:wf_doesnotexist']  streamsRejected=[]
         currentRun='run:wf_doesnotexist'
  replay frames: []
```

`h_events` refuses exactly this on the HTTP side and says why
(`:1819-1821`, docstring `:1766-1770`): *"answering 200 with `records: []` and
a `head` cursor would be publishing a made-up fact about a made-up run — the
wrong-target answer wearing a success code"*. The socket does the made-up-fact
version, and `currentRun` is the field sp-13 will label the page header with.

Serving a not-yet-existing stream is deliberate and correct (that is the
late-stream path M1 is about), so the fix is not a refusal — it is a label.
**Fix:** compute `currentRun` only over streams that exist in
`store.streams()`, and add a `streamsUnobserved: [...]` list to hello beside
`streamsRejected`, so "watching a run that has not started" is visible as such
rather than presented as an observation. Test: the probe above, asserting
`currentRun is None` and the id named in `streamsUnobserved`.

### m2 — `tick()` emits an unbounded number of frames per tick (GD-30's bounded queue)
`aggregator/server.py:1513-1564`, drained at `:2469-2477`

`replay()` is capped at `MAX_REPLAY_EVENTS` per stream; the tail has no cap at
all. Everything `store.follow` returns in one 250 ms tick becomes one frame
each, and `HttpServer.stream` writes and drains all of them before the next
`await asyncio.sleep(self.tick)`:

```
frames emitted in ONE tick after a 5000-record bulk append: 5000 | all live:true
```

A bulk append into an already-followed stream — an ingest catching up after a
restart, exactly the scenario that produces a burst — is one synchronous write
storm on the socket, per connected client. GD-30 asks for a bounded queue and
this is the one queue in the file that has no bound. (Secondary: those 5000
frames are `live:true`, so sp-13 will animate a catch-up burst it cannot
distinguish from real activity — the concern `_emit_backfill`'s docstring
raises for new streams, unaddressed for existing ones.)

**Fix:** cap `tick()` at `MAX_REPLAY_EVENTS` frames per stream per tick,
leave the cursor where the cap fell so the remainder arrives on the next tick,
and say so in the contract's tail clause. Test: append `MAX_REPLAY_EVENTS + 1`
records between two ticks and assert the first tick's frame count is capped
and the next tick delivers the remainder with no gap and no duplicate.

### m3 — a malformed `?from=` is dropped without being echoed
`aggregator/server.py:2407-2408`, `:1418-1419`

```python
from_seq = int(from_raw) if from_raw and _SEQ_RE.match(from_raw) else None
```

`?from=abc` yields `{"from": null, "fromApplied": false}` — indistinguishable
from `?from=12` against three streams, and from no `?from=` at all except for
the `false`. The rule at `:89-95` is that *every* parameter that could not be
used is named on hello; `?cursor=` and `?stream=` both carry their raw
rejects, `?from=` does not carry its own.

**Fix:** echo the raw value (`"from": "abc"` or a `fromRejected: "abc"`
field) so the three cases are distinguishable. Extend
`test_from_is_applied_or_reported_never_silently_dropped` with the malformed
arm — it currently covers only the multi-stream arm.

---

## NITS

### n1 — the backwards page's `cursor` names the position the client already had
`aggregator/server.py:1826-1834`. In the `after=` arm `cursor` is "where to
continue"; in the `before=` arm it is `page[-1]` — the *newest* record of an
older page, i.e. `before - 1`. The field a client must continue backwards with
is `oldest`, published right beside it. Same name, opposite meanings, one
endpoint. **Fix:** in the `before` arm either drop `cursor` or set it to
`cursor_key(stream, page[0].seq)` and say in the docstring that it is the next
`before=`.

### n2 — every `/api/events` request and every replay reads the whole stream file
`aggregator/server.py:1375-1382` (`_records`), `:1822` (`store.read_all`).
The *wire* is bounded (`replay_window`, `limit`), the read is not: paging
backwards through a 20 MB stream re-parses all of it per page. GD-30's budget
is written about ticks, and ticks are incremental (`store.follow`), so this is
outside the letter — but `h_events`'s "load older" is the exact loop a page
runs to walk a truncation. **Fix:** at minimum record the choice in
`h_events`' docstring; better, seek by `byteOffset`/checkpoint the way
`follow` does.

### n3 — `_advance` freezes a cursor on a seq-less held record
`aggregator/server.py:1507-1511`. `pending_floor` defaults a missing `seq` to
0, so `floor - 1 == -1` and `seq > cursor` can never hold: the stream's
published cursor stops moving until the coalescer releases. Store-written
records always carry a seq, so this is defensive-only. **Fix:** skip records
with no seq in `pending_floor` rather than defaulting them to 0.

---

## Checklist items verified clean (re-run this attempt, not inherited)

- **GD-21** — `ast`-walked imports of `server.py`:
  `{__future__, asyncio, dataclasses, datetime, hashlib, hmac, json, os, re,
  secrets, sys, time, urllib}` plus relative siblings. No `pymongo`, `bson`,
  `dns`; no `mirror` import. `mongo_store.py` keeps its `import pymongo` inside
  a function (`:1353`).
- **GD-22** — every route reads `model.state`/`model.store`; `/api/query`'s
  Mongo arm is injected and labelled (`source`); `/health` over a store whose
  root does not exist answers 200 with `streamCount: 0`, and over a raising
  mirror answers `state: down` rather than 500.
- **GD-24/25/26** — no `_id` is constructed here (only `store.cursor_key`,
  which round-trips through `parse_cursor_key`); no `$`-verb, no `$unset`, no
  `deleteMany`, no TTL index (the only `TTL` is `REDUCE_TTL_SECONDS`, a
  reduction cache); tokens are carried verbatim, never differenced — the AST
  guard in `test_server_core.py` still holds.
- **GD-27** — no token, URI, password or path on any response;
  `target_hash()` keeps `/health` path-free; `.touch/server.json` is written
  0600 via `os.open` with the mode in the call, and its directory is chmod'ed
  0700.
- **GD-13** — 127.0.0.1 default, `--open` opt-in, `hmac.compare_digest` on
  three carriers, Origin/Host allowlist at the upgrade only, static
  `(method, route)` dict with a default 404 and no prefix match.
- **GD-29/GD-30 (partial)** — no Mongo client held; `tick()` is
  `store.follow`-incremental per stream; token coalescing is ≥1 s and keyed on
  the store's real `kind: "token"`. The one GD-30 gap found is m2 above.
- **GD-15 / ownership** — only the three owned files are new/changed;
  `HEAD` still `579446e`; no commit.
- **Tests assert behaviour, not tautologies** — spot-checked the ones easiest
  to dismiss: `_contract_frame_keys` parses the normative docstring table as
  data and asserts against it; the deep-resume test drives 6000 real records;
  the held-token test builds a *second* `WsSession` from the published cursor
  and proves the held record is still delivered. No mongod-dependent arm is
  needed in this file and none is present.
