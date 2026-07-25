# Research findings — perspective: PRIOR ART (`.claude/shared/monitoring/` + the three skills)

Read the implementation, not the docs. Every claim below is anchored to a line
or to a command whose output I inspected. Empirical checks were run in
`/tmp/claude-1000/priorart/`; the live task folder was only read, never written
(except the two mandated `status.sh` calls).

## What the module already gets RIGHT (reuse verbatim — see PRIORART-18)

Before the defects: this is a genuinely well-built prototype. Torn-tail
tailing, checkpoint-with-identity, monotonic token deltas, message-id dedup,
session-id rotation survival, realpath containment and escape-first rendering
are all correct and tested. The findings below are about **scope**, not craft.

---

## PRIORART-1 — The transport is one-directional by construction; there is no place to put a control command

**File**: `.claude/shared/monitoring/monitor_server.py:393-514` (`handle`),
`:313-327` (`drain_client`), `:358-388` (`stream_events`)
**Severity**: blocker

**Scenario.** Touch's per-terminal page must "pause, restart, start and
terminate agent loops" (README.md:5-6) and the main page must accept keystrokes
into a live Claude Code session (README.md:3-4). Nothing in the prior art can
carry a byte from browser to backend:

- `handle()` parses the request line at `:403-404` but **never reads the HTTP
  method** and never reads a request body (it stops at `readuntil(b"\r\n\r\n")`,
  `:397`). A `POST /control` is indistinguishable from `GET /control`.
- The websocket is server→client only: `drain_client()` (`:313-327`) exists
  purely to *discard* client frames and detect CLOSE. `parse_client_frames()`
  (`:279-310`) deliberately skips over payloads without ever decoding them —
  it does not even unmask.
- Every route that is not `/ws`, `/health`, `/tasks`, `/artifacts`, `/file`
  falls through to the catch-all that returns `monitor.html` with **200**
  (`:495-506`).

The data model has the same shape: `events.jsonl` is append-only and every
consumer (`read_frames`, `task_status`, the browser) is a pure reader.

**Recommendation.** Do not "add a POST endpoint" to `monitor_server.py` — its
request loop has no method dispatch, no body reader, no routing table, and no
keep-alive (every response hard-codes `Connection: close`). Keep the *reader*
half (tail + replay + WS framing) as a component, and put commands on a
separate, explicitly-designed plane. Migration consequence: the reader half is
~120 lines of `monitor_server.py` (`ws_frame`, `parse_client_frames`,
`drain_client`, `read_frames`, `stream_events`) and lifts cleanly; the
`handle()` router does not and should be replaced.

---

## PRIORART-2 — Zero authentication, no Origin check, no Host check, binds 0.0.0.0 — safe for a read-only dashboard, fatal the moment controls exist

**File**: `monitor_server.py:519` (`asyncio.start_server(handle, "0.0.0.0", PORT)`),
`:412-441` (the entire `/ws` admission check)
**Severity**: blocker

**Scenario.** The `/ws` upgrade validates exactly two things:
`sec-websocket-key` present and `sec-websocket-version in (None, "13")`. There
is no `Origin` validation, no `Host` allowlist, no token, no cookie, no
same-origin requirement. WebSocket connections are **not** subject to CORS, so
any web page the user visits in the same browser can today do
`new WebSocket("ws://<sandbox-host>:8931/ws?task=<name>")` and read the entire
event stream, and `fetch("/file?task=…&path=…")` to read every `.md`/`.html`
under a task folder (opaque-response restrictions do not apply to the WS path).
A `Host:`-header-based DNS-rebinding attack is likewise unblocked.

Today the blast radius is "an attacker learns your agent logs". Once the same
origin accepts *pause/terminate/start* commands and terminal keystrokes, the
blast radius becomes **arbitrary command execution in the user's repo**, and it
is reachable by CSRF from any tab. The environment makes this worse, not
better: the module is documented to bind `0.0.0.0` and be published to the host
(`CLAUDE.md:112-114`).

**Recommendation.** Decide the auth model **before** writing the first write
endpoint, not after: a per-boot random token in the URL (`/?k=<token>`) that the
page echoes into the WS query + an `Origin`/`Host` allowlist enforced in the
upgrade handler is the minimum, and is ~20 lines. Migration consequence: the
token must be printed on stdout at startup (the module already prints its state
dir at `:523-525`, so the pattern exists) and every `sbx ports --publish`
instruction in the docs must carry it.

---

## PRIORART-3 — The unit of aggregation is a repo-local task folder, not a Claude Code session

**File**: `monitor_server.py:21` (`TASKS_ROOT = .../local-orchestrators`),
`:49-62` (`discover_tasks`), `:148-151` (`resolve_task_dir`)
**Severity**: major

**Scenario.** Touch's sidebar is "a list of such terminal sessions"
(README.md:4). The prior art's list is `.claude/local-orchestrators/*/`
directories in **one repo**, discovered by `os.listdir` of a path computed
relative to the module's own location (`:21`). It never opens `~/.claude` at
all except in the watcher's journal auto-discovery.

Ground truth for what Touch actually needs is elsewhere and is richer.
`~/.claude/sessions/622.json` contains:

```json
{"pid":622,"sessionId":"dd469822-…","cwd":"/home/laniakea/Projects/touch",
 "startedAt":1784946693282,"procStart":"10028","version":"2.1.220",
 "peerProtocol":1,"kind":"interactive","entrypoint":"cli","name":"touch-2b",
 "nameSource":"derived","status":"busy","updatedAt":…,"statusUpdatedAt":…}
```

and the CLI binary reads additional per-session fields `tempo`
(`active|idle|blocked`), `needs`, and `tmux` from those same files (verified:
`grep -aoP '.{0,120}peerProtocol.{0,220}' /home/agent/.local/share/claude/versions/2.1.220`).
That is exactly the sidebar model — with a live status, a name, a cwd and a pid
— and the prior art has no concept of any of it. Note also that
`local-orchestrators/*/` in this repo is **carried-over history from a
different project** (`CLAUDE.md:127-130`), so its own discovery root is not
even describing this machine's runs.

**Recommendation.** Build session discovery as a new layer over
`~/.claude/sessions/*.json` + `~/.claude/projects/<slug>/<sessionId>.jsonl`, and
demote `events.jsonl` to a *per-run overlay* joined onto a session by id. Reuse
the shape of `tasks_payload()` (`:133-145`) — name/mtime/status/last/tokens per
entry is the right tile payload — but not its source. Migration consequence:
`?task=<name>` becomes `?session=<uuid>` (+ optionally `&run=<wfId>`), which
breaks every saved dashboard URL; do it now while there are none.

---

## PRIORART-4 — Only Workflow-tool agents carrying the `[monitor]` marker are visible; ordinary session subagents are invisible

**File**: `decision_watcher.py:115-135` (`MARKER`, `STAGE_HINT`,
`ROLE_PATTERNS`), `:330-367` (`classify`), `:53-73` (journal pinning)
**Severity**: major

**Scenario.** The watcher's only input is one `wf_*/journal.jsonl`, and an
agent only becomes anything but "unclassified" (`:664-666`) if the first line of
its transcript contains
`[monitor] plan=… role=… attempt=…`. That marker is authored by the
orchestrator workflow script — so the watcher sees **only** agents spawned by
an `execute-research`/`implement-plan` workflow.

But a Claude Code session spawns subagents outside workflows too, and they are
recorded in a completely different place with a completely different shape.
Verified in this very session:

```
~/.claude/projects/-home-laniakea-Projects-touch/<sessionId>/subagents/
  agent-a4e343a0f7d73268c.jsonl        (937 KB)
  agent-a4e343a0f7d73268c.meta.json -> {"agentType":"general-purpose",
      "description":"Assess control and UI feasibility",
      "toolUseId":"toolu_017UzEDnR28ARKERuMw2PGwX","spawnDepth":1,"model":"opus"}
  agent-a483cae616edffe81.jsonl        (421 KB)
```

Two real subagents, no journal, no marker, `spawnDepth` and `toolUseId` instead
— and the workflow variant's meta is *poorer*
(`{"agentType":"workflow-subagent","spawnDepth":1,"model":"opus"}`, no
description, no toolUseId). README.md:1 says Touch visualizes "subagents in a
Claude Code session"; the prior art can see roughly half of them, and the half
it can see is the half that needed a special prompt convention.

**Recommendation.** Make the **session transcript** the primary source: the
`Task` tool_use/tool_result pairs in `<sessionId>.jsonl` give you parent→child
edges, labels (`description`), and the `toolUseId` join key to
`subagents/agent-*.meta.json`, with zero prompt cooperation. Keep the
`[monitor]` marker as *optional enrichment* that adds plan/stage/attempt
semantics when a workflow authored it. Migration consequence: `classify()` stops
being the gate on visibility and becomes a decorator — the marker convention
survives, but nothing depends on it any more.

---

## PRIORART-5 — Parallel fan-out is unrepresentable: six live agents render as six identical rows and collapse into ONE graph node

**File**: `decision_watcher.py:636-638` and `:682-688` (label construction),
`monitor.html:287-293` (`upsertAgent` role derivation), `:300-321` (`renderFlow`)
**Proof**: `.claude/local-orchestrators/touch-aggregator/events.jsonl:12,15,17,19,21,23`
**Severity**: major

**Scenario.** The watcher builds every agent label as
`f"{info['role']} #{info['attempt']}"` — role and attempt only. It **has**
`info["stage"]` in hand (it uses it as the emitted event's stage on the very
same line) and throws it away. For a `parallel()` fan-out every sibling shares
role and attempt, so every label is identical. From the live run right now:

```
events.jsonl:12  "stage":"sessiondata" … "agent":{"id":"a2fc883c","label":"research #1"…}
events.jsonl:15  "stage":"liveio"      … "agent":{"id":"a74f0c93","label":"research #1"…}
events.jsonl:17  "stage":"control"     … "agent":{"id":"a9eabf26","label":"research #1"…}
events.jsonl:19  "stage":"agentgraph"  … "agent":{"id":"a82d2e25","label":"research #1"…}
events.jsonl:21  "stage":"stack"       … "agent":{"id":"a79fa2f4","label":"research #1"…}
events.jsonl:23  "stage":"priorart"    … "agent":{"id":"a2ec1069","label":"research #1"…}
```

The frontend renders `a.label || a.id` into `.alabel` (`monitor.html:278`) and
never shows the id, so the user sees six rows all reading `research #1`. Worse,
the flow strip derives its node key by `(a.label||a.id).split(" #")[0]`
(`:287`) → all six map to the single role `"research"` → `renderFlow` draws
**one** node. The orchestrator log is equally degenerate: six consecutive
identical `"spawn research research attempt 1"` lines (`events.jsonl:11,14,16,
18,20,22`).

This is precisely the case Touch's n8n-style graph exists to show.

**Recommendation.** Two-line fix in the watcher: label as
`f"{info['stage']}:{info['role']} #{info['attempt']}"`, and add explicit
`agent.stage` / `agent.role` / `agent.attempt` fields to the `agent` sub-object
rather than making the UI reverse-engineer them out of a display string.
Migration consequence: purely additive to the event schema — already-recorded
history (which the ops rules forbid deleting, `CLAUDE.md:118-121`) still renders,
just with the old collapsed labels.

---

## PRIORART-6 — The event schema is a flat state overlay; it cannot express a graph, and it has no version field

**File**: `.claude/shared/monitoring/monitoring.md:25-46` (normative schema),
`monitor.html:203-219` (`planEl` — one card per `plan` id)
**Severity**: major

**Scenario.** The whole model is `(plan, stage, state)` → one card per `plan`,
one chip per `stage`, plus a per-agent row list. There are **no node ids, no
edges, no parent/child, no ordering other than first-seen**. `renderFlow`
(`monitor.html:300-321`) fakes a topology by iterating `p.roles` in insertion
order and drawing `→` between consecutive entries — that is a rendering
accident, not data. An n8n-like UML graph needs an actual node/edge structure
(fan-out, barrier/join, retry back-edge, gate branch), and none of it can be
derived from this stream.

Separately: events carry **no schema version**. Backward compatibility so far
has been achieved by defaulting missing keys (`ev.tokens.cached || 0`,
`monitor.html:371`; the one-time backfill at `decision_watcher.py:538-561`).
Any *structural* change is unabsorbable, and the operating rule is that
completed runs' `events.jsonl` are permanent history that replays on connect
(`monitoring.md:183-187`).

**Recommendation.** (a) Derive the graph topology from a source that actually
has one — the workflow script's phase/`agent()` structure plus the journal's
spawn order and the session transcript's Task parent/child edges — and use
events only to *paint state onto* nodes. (b) Add `"v": 2` to every emitted
event now, and have readers treat a missing `v` as 1. Migration consequence:
one field in `emit()` (`decision_watcher.py:138-152`) and in `status.sh:34-40`;
doing it later means either a lossy history migration or a permanent branch in
the reader.

---

## PRIORART-7 — The watcher fully re-parses every running agent's entire transcript once per second (O(size) per tick, and size grows all run)

**File**: `decision_watcher.py:154-197` (`agent_tokens`), `:86-100`
(`agent_paths`), called from `:764-795` (every ~1s) and `:678`, `:705`
**Severity**: major

**Scenario.** `agent_tokens(aid)` globs
`~/.claude/projects/*/*/subagents/workflows/<wf>/agent-<id>.jsonl` (a filesystem
glob, per agent, per tick) and then reads and `json.loads`-es **every line of
every copy** to rebuild `usage_by_msg` from scratch. It is called for every
still-running agent on every 1-second poll, and again on every result.

Measured on the live run (`/tmp/claude-1000/priorart/bench.py`):

```
6 transcripts, 2.26 MB, 456 usage rows, one full pass = 16 ms   [6 minutes in]
```

16 ms/s is nothing — but the cost is linear in transcript size, which grows for
the whole run. Extrapolating from the measured rate (~0.38 MB per agent per
6 min), a 2-hour six-agent implement loop reaches ~45 MB, i.e. **~300+ ms of
JSON parsing every single second**, on the *same single thread* that tails the
journal, classifies spawns, and runs the completion debounce. Touch wants to
watch whole sessions, whose transcripts are larger still (this session's
`<sessionId>.jsonl` is 777 KB after ~30 minutes).

**Recommendation.** Apply the module's own checkpoint pattern one level down:
store `{path: (byte_offset, partial_usage_totals)}` per transcript in
`.watcher-state.json` and only parse the appended tail — `read_new_lines()`
(`:470-491`) is already exactly the right primitive and is already tested.
Message-id dedup still works because you keep the accumulated id set (or, since
ids are per-message and monotonic, just the running sums plus a small
recently-seen id window). Migration consequence: `.watcher-state.json` gains a
key; `load_state()`'s journal-identity reset (`:494-512`) already handles a
stale/incompatible checkpoint by discarding it.

---

## PRIORART-8 — Full replay from byte 0 on every connect, one WS frame per event, no log trimming — and the UI offers a 500 ms full-resync button

**File**: `monitor_server.py:358-378` (`stream_events`, `offset = 0`, one
`writer.write(ws_frame(line))` per line), `monitor.html:699-708` (clear + rebuild
on every `onopen`), `:390-391` (unbounded `log.insertBefore` + `render()` on
every event), `:172-181` and `:714-732` (`rateSel` / `forceResync`)
**Severity**: major

**Scenario.** There is no resume cursor: every `/ws` connection replays the file
from offset 0 as individual text frames, and the page throws away all DOM and
accumulators and rebuilds. Then:

- `render()` runs on **every** event and re-`appendChild`s **every** card
  (`monitor.html:220-238`) — O(events × plans) DOM moves during replay.
- log `<li>`s are never trimmed (`:390`), so replay materialises one DOM node
  per historical event.
- the refresh dropdown offers **500 ms**, and `forceResync()` implements it by
  *closing and reopening the socket* (`:717-724`) — i.e. a full history replay
  twice per second.

Measured on the live run: **375 events / 150 871 bytes in ~6 minutes** for a
single six-agent research phase — ~1.5 MB/h, dominated by the quiet 1 Hz token
ticks (`decision_watcher.py:781-791`). One hour of history on the 500 ms setting
is ≈3 MB/s of replay per open tab, and ≈21 600 log DOM nodes. Touch's terminal
view (which must replay a session transcript, already 777 KB at 30 min) makes
this the dominant cost of the whole product.

**Recommendation.** Add `?from=<byte-offset>` (or `?after=<seq>`) to `/ws`,
batch replay into multi-line frames, emit an explicit `{"kind":"replay-end"}`
marker, cap the client log with a ring buffer, and coalesce `render()` into a
`requestAnimationFrame`. Migration consequence: the client must persist a
cursor and keep its accumulators across reconnects — which means giving up the
"full replay is safe against double counting" invariant that the current design
leans on (`monitoring.md:172-175`); the replacement invariant is
"events are ordered and idempotent by seq", which is stronger and is what a
control plane needs anyway.

---

## PRIORART-9 — The watcher is structurally blind to control actions, so a paused/stopped run will be reported as a completed one

**File**: `decision_watcher.py:110-113` (`QUIET_SECS`), `:450-467`
(`run_outcome`), `:731-762` (the completion sweep)
**Proof**: `grep -aoP 'resumeFromRunId.{0,400}' /home/agent/.local/share/claude/versions/2.1.220`
**Severity**: major

**Scenario.** Verified against the CLI binary: a Workflow run is a background
task in a task registry — the binary contains
`i.type==="local_workflow" && i.status==="running" && i.workflowRunId===e.resumeFromRunId`
and the user-facing string *"Workflow ${runId} is still running (task ${id}).
Stop it first with TaskStop({taskId: …}) before resuming."* And, decisively:

> *"It may have been stopped (via the UI or TaskStop — **these leave no
> transcript marker**), or it may have been running when the previous Claude
> Code process exited."*

So a stopped workflow and a stalled workflow produce **byte-identical**
journals. The watcher's only terminal signal is the 60-second quiet debounce
(`:738-760`), which will confidently emit
`orchestrator complete done|failed … "(watcher-detected end)"` and settle every
open plan card by `state["decisive"]` (`:748-753`) — i.e. it will fabricate a
verdict for a run the user *paused on purpose*, and the fabricated verdict will
usually be `failed` (because `decisive` is unset for a plan mid-loop, `:751`).

**Recommendation.** Once Touch owns the control actions it must record them
itself (an append-only control log: `paused|resumed|stopped|started`, with
runId, taskId and actor) and *join* that onto the derived state, with the
control log winning. Concretely: gate the quiet-debounce close behind "no
pause/stop recorded for this run". Migration consequence: `run_outcome()` grows
an input it currently cannot see; the 60 s heuristic stays only as the fallback
for runs Touch did not initiate.

---

## PRIORART-10 — "Restart" is `resumeFromRunId` cache replay into a NEW journal, and the checkpoint model turns that into duplicated history

**File**: `decision_watcher.py:494-512` (`load_state` journal-identity reset),
`:72-74` (`JOURNAL`/`WF_NAME` fixed at import)
**Proof**: binary text *"To resume after a pause, kill, or script edit, relaunch
with Workflow({scriptPath, resumeFromRunId}) — the longest unchanged prefix of
agent() calls returns cached results instantly; the first edited/new call and
everything after it runs live."*
**Severity**: major

**Scenario.** The harness's restart primitive replays cached `agent()` results
into a **new run**, with a new runId and (per `nEp()` in the binary, which
symlinks a canonical `workflowRunId` path at the `transcriptDir`) a new
transcript dir and journal. `load_state()` sees `state["journal"] != JOURNAL`
and — correctly, for its own assumptions — resets `offset=0` and discards all
derived state. The watcher then re-emits the *entire* spawn/result/token history
of the resumed run into the **same** `events.jsonl`, on top of the original
run's events. The dashboard replays both: duplicate agent rows (different agent
ids), duplicated token deltas on the card counters (`monitor.html:370-372`
accumulates unconditionally), and a card whose badge flips through two runs'
worth of transitions.

Note the harness *does* hand you the dedup key and the prior art ignores it —
every journal `started` line carries a content hash:

```
{"type":"started","key":"v2:c13a866b3bd1e7129557fc996a218a044553d0115ac84c64a6184c41e4042639","agentId":"a2fc883c96ff7b837"}
```

(verified: `~/.claude/projects/…/subagents/workflows/wf_829e6f58-b2f/journal.jsonl`).
`key` is stable across a cached resume; `agentId` is not. The watcher reads only
`type` and `agentId` (`:586-587`).

**Recommendation.** Key run state by `(runId, key)` rather than by journal path
+ agentId: a `started` whose `key` was already emitted is a cache replay and
must update the existing node, not create a second one. Migration consequence:
`.watcher-state.json` gains a `keys` map and `load_state`'s hard reset becomes a
merge; this is the single change that makes the README's "restart" control
truthful rather than duplicative.

---

## PRIORART-11 — One watcher process per run, with all run identity in module-level globals — the module cannot grow to N sessions

**File**: `decision_watcher.py:19-113` (module-level `ROOT`, `STATE_DIR`,
`WF_DIR`, `JOURNAL`, `WF_NAME`, `EVENTS`, `STATE`, `MAX_*`, `QUIET_SECS` all
resolved at import), `:523-796` (`main()` = one infinite loop over one journal);
`monitoring.md:143-145` ("`decision_watcher.py` still watches one task per
process — start one per concurrent orchestration")
**Severity**: major

**Scenario.** Everything that identifies *which run* is being watched is a
module global computed at import time. Two runs = two OS processes, started by
hand, with no supervisor, no restart, no health check, and nothing that notices
a new run appearing. Touch's sidebar implies N sessions × M runs, discovered
and attached **live**. The server half already solved the multiplicity problem
(one `monitor_server.py` serves every task, rescanning per request,
`:49-62`); the watcher half did not.

**Recommendation.** Refactor the watcher into a `RunWatcher` class holding what
are now globals, plus a supervisor loop that reconciles a watch-set against
disk (new `wf_*/journal.jsonl` appears → attach; journal quiet + run closed →
detach). Migration consequence: this is a real refactor of ~800 lines, not a
wrapper — every helper (`agent_paths`, `agent_tokens`, `prompt_text`,
`first_ts`, `last_ts`, `emit`, `classify`) closes over `WF_DIR`/`WF_NAME`/
`EVENTS` today. Budget it explicitly; the alternative (spawning a process per
run from Touch) inherits a process-lifecycle problem the prior art never had to
solve.

---

## PRIORART-12 — An unknown `?task=` silently streams a different task's data

**File**: `monitor_server.py:148-151` (`discover_tasks().get(task, STATE_DIR)`),
used by `/ws` (`:446`), `/artifacts` (`:457`) and `/file` (`:467`)
**Severity**: minor

**Scenario.** A renamed, deleted, or mistyped task name does not 404 — it
resolves to the process's startup `STATE_DIR` and the client gets a **200/101
with someone else's events, artifacts and files**. The frontend has a
band-aid for the display side (`selectTask` injects a disabled
`"(unknown task)"` option, `monitor.html:598-613`) but the socket is still
streaming the wrong run underneath it: URL, breadcrumb and dropdown say one
thing, the cards show another.

For Touch, where the identifier is a session and the page carries controls,
"silently act on a different target" graduates from confusing to dangerous.

**Recommendation.** Return 404 for an unresolvable id on `/ws`, `/artifacts` and
`/file`; keep the default-selection behaviour only for the *absent* parameter.
Migration consequence: `monitor.html`'s `route()` must handle a failed WS
upgrade as "unknown session" rather than as a retry loop (`:707-708` currently
reconnects forever).

---

## PRIORART-13 — Catch-all route returns the dashboard with 200 for every unknown path; HTTP method is never parsed

**File**: `monitor_server.py:403-404` (request line parsed for path only),
`:495-506` (catch-all)
**Severity**: minor

**Scenario.** `GET /favicon.ico`, `GET /api/anything`, `DELETE /file` all return
the full `monitor.html` body with `200 OK` and `Content-Type: text/html`. There
is no 404, no 405, no method dispatch, and no `HEAD` handling. A browser
requesting `/favicon.ico` downloads the whole dashboard.

**Recommendation.** Introduce a real `(method, route) → handler` table with a
default 404 before adding any endpoint. Migration consequence: trivial now
(five routes); painful once Touch has thirty.

---

## PRIORART-14 — `events.jsonl` is multi-writer and its safety rests on an undocumented "one `write()` per record" invariant

**File**: `status.sh:28-45` (`python3 … >> "$STATE_DIR/events.jsonl"`),
`decision_watcher.py:150-151` (`open(EVENTS,"a")` + one `f.write(line+"\n")`)
**Proof**: `/tmp/claude-1000/priorart/interleave.py` and `interleave2.py`
**Severity**: minor (today) / blocker (if Touch reuses the file as a transport)

**Scenario.** N concurrent agents' `status.sh` calls plus the watcher all append
to the same file with no locking. I tested this directly with 4 concurrent
writers × 200 records of ~6 KB each:

```
one write() per record   -> lines=800  corrupted(mixed-writer)=0
record split over 2 write() calls -> lines=800  corrupted(mixed-writer)=206
```

So the current design is genuinely safe — Linux serialises a single `O_APPEND`
`write()` on a regular file — but *only* because every writer today happens to
emit a whole record in exactly one syscall (`print()` on a buffered stdout that
is flushed once at process exit; a single `f.write()`). Nothing documents this,
nothing tests it, and it is exactly the invariant a new writer breaks: a Node.js
`stream.write()` of a large payload, an incrementally-built line, or anything
streaming terminal output in chunks produces the 206/800 case.

**Recommendation.** Write the invariant down next to the schema in
`monitoring.md`, and add it to `test_shell.py` as a concurrency test (it is a
20-line stdlib test). For Touch: do **not** carry terminal I/O over a shared
append-only file — use one file per writer, or a socket. Migration consequence:
none for the existing event stream; it decides Touch's terminal transport.

---

## PRIORART-15 — Reserved magic names live in the same flat namespace as user data

**File**: `monitoring.md:41-42`; enforced by string comparison in three places:
`monitor_server.py:100` and `:112-113`, `monitor.html:352` and `:360-361`,
`decision_watcher.py:139` (`plan="orchestrator"` default)
**Severity**: minor

**Scenario.** `plan == "orchestrator"` means "the wide card pinned last";
`stage in ("plan","complete")` means "this event sets the badge instead of
adding a chip"; `stage == "tokens"` means "this is a counter delta, not a log
line". These are ordinary values of ordinary data fields. The plan ids in
`implement-plan` are generated at runtime by a Fable divider from the plan text
(`implement.workflow.js:219-228`, `id: "sp-<slug>"`), and stage names are
free-form; a sub-plan or stage that happens to be called `plan`, `complete`,
`tokens` or `orchestrator` silently hijacks the rendering, and nothing in
`status.sh` or the watcher rejects it.

**Recommendation.** Move the control semantics into a dedicated field (`kind:
"badge" | "chip" | "tokens" | "log"`) and let `plan`/`stage` be pure data.
Migration consequence: readers must accept both forms for one release — which
is exactly what the `v` field from PRIORART-6 is for.

---

## PRIORART-16 — `classify()` sleeps inside the single poll thread; a fan-out can stall the whole watcher for seconds

**File**: `decision_watcher.py:330-367` (up to `retries=3` × `time.sleep(0.5)`),
called synchronously at `:595` and `:670`
**Severity**: minor

**Scenario.** When an agent's transcript has not flushed yet, `classify()`
blocks the poll loop for up to 1.5 s. The code comments acknowledge this and
call it a deliberate bound (WATCHER-5, `:331-337`) — but the bound is per agent,
and a `parallel()` fan-out delivers all `started` entries in one journal chunk:
the six spawns in this run are consumed in a single iteration, so the worst case
is ~9 s during which journal tailing, live token ticks and the completion
debounce are all frozen.

Acceptable for a dashboard that lags a second. Not acceptable for Touch, whose
same-loop responsibilities would include a live terminal.

**Recommendation.** Make classification deferred: on `started`, register the
agent as pending and return immediately; retry classification on subsequent
ticks (the code already re-attempts on the `result` entry, `:670`, so the
fallback path exists). Migration consequence: `state["agents"]` gains a
"pending" state; the `stale`-close guard at `:610-616` must tolerate a sibling
with no classification yet.

---

## PRIORART-17 — The network layer has no tests at all, and the frontend is tested by grepping its own source

**File**: `tests/test_server.py:36-271` (covers only `read_frames`,
`resolve_port`, `task_status`, `ws_frame`, `parse_client_frames`,
`task_artifacts`, `safe_artifact_path`), `tests/test_frontend.py:1-9` and
`:30-134`
**Severity**: minor

**Scenario.** `handle()`, `stream_events()`, `drain_client()` and `main()` — the
entire request lifecycle, the upgrade handshake, the CSP/`nosniff` headers, the
404 path, shutdown — are never executed by any test; `test_server.py` explicitly
notes "No server is started (main() is never called)" (`:19-21`). And
`test_frontend.py` asserts on **`monitor.html` source text** ("the fixed pattern
present, the vulnerable one absent", `CLAUDE.md:97-99`) because no JS runtime is
available under the zero-dependency rule. That yields assertions like
`assert "innerHTML" not in flow` and
`assert 'rel = "noopener"' in arts` — which are refactor-fragile and prove
nothing about behaviour.

The approach is *right for its size*. It does not survive Touch, which has a
terminal emulator, a graph renderer, routing and a control plane in the browser.

**Recommendation.** Decide the testing posture as a global decision in the plan,
not per sub-plan. Two coherent options: (a) stay zero-dependency and add
**stdlib socket-level integration tests** (`asyncio.open_connection` against a
real `start_server` on port 0 — ~50 lines, no third-party anything) — this
closes the network gap without breaking the constraint, and should be done
regardless; (b) admit a JS test runner for the frontend, which breaks the
zero-dependency property the module was explicitly built around
(`monitoring.md:11`) and the sandbox's default-deny egress makes installing it a
user action. Migration consequence: (a) is additive; (b) changes the project's
identity and must be decided once, up front.

---

## PRIORART-18 — What to reuse verbatim, and what to generalize (consolidated recommendation)

**File**: various (below)
**Severity**: nit (it is a decision record, not a defect)

**Reuse verbatim — these are correct, tested, and hard to re-derive:**

- **Torn-tail tailing + truncation sentinel**: `monitor_server.py:330-356`
  (`read_frames`, `-1` on `size < offset`) and `decision_watcher.py:470-491`
  (`read_new_lines`, `errors="replace"` on a torn multibyte tail). Tested by
  `test_server.py:36-85`. Every log-tailing feature in Touch needs exactly this.
- **Checkpoint with identity + atomic replace**: `decision_watcher.py:494-520`
  (`load_state` refuses a checkpoint whose `journal` differs; `save_state` writes
  `.tmp` then `os.replace`).
- **Monotonic token deltas + message-id dedup**: `:154-197`, `:705-722`
  (`max(0, …)` clamps and never-lowered baselines). This is the reason restarts
  and 1 Hz ticks do not double-count, and it is subtle.
- **Session-id rotation survival**: `agent_paths()` (`:86-100`) unions every
  session dir holding the same `wf_*` name. `/clear` and `/compact` relocate
  transcripts mid-run; Touch will hit this on day one.
- **Path containment**: `safe_artifact_path()` (`monitor_server.py:199-212`) —
  extension whitelist + `realpath` + `startswith(base + os.sep)`. The
  `+ os.sep` correctly excludes both the base itself and sibling-prefix dirs.
  Tested (`test_server.py:235-262`). Keep it exactly as written.
- **Escape-first rendering discipline**: `monitor.html:463-543` (`esc` before
  every inline transform, href protocol whitelist) and the `NODE_STATES`
  whitelist pattern at `:299-311`. Agent-written text is untrusted input; this
  is the right default and the tests enforce it.
- **`status.sh` as the agent trace point** (49 lines, `set -u`, `mkdir -p` up
  front, non-zero exit on a failed append). Take it as-is — but note it forks a
  Python interpreter per event (~30-50 ms), so it must never become a hot path.
- **The `?task=` URL-as-state routing model** (`monitor.html:196-200, 562-591`):
  query string is the single source of truth, back/forward re-route, refresh and
  share both work. Generalize the identifier, keep the pattern.

**Generalize:** `tasks_payload()`/`task_status()` (the tile payload shape is
right, the source must become sessions — PRIORART-3); the `agent` sub-object
(add `stage`/`role`/`attempt` — PRIORART-5); the watcher's globals (→ per-run
objects — PRIORART-11); `agent_tokens` (→ incremental — PRIORART-7).

**Replace:** `handle()`'s router (PRIORART-1, -13); the auth posture, i.e. add
one (PRIORART-2); the full-replay-only WS protocol (PRIORART-8); the flat event
schema as the graph source (PRIORART-6); the marker-gated visibility model
(PRIORART-4).

---

## Summary of severities

| id | severity | one line |
|----|----------|----------|
| PRIORART-1 | blocker | transport is read-only by construction; no place for a command |
| PRIORART-2 | blocker | no auth / Origin / Host check on a 0.0.0.0 bind — fatal once controls exist |
| PRIORART-3 | major | unit is a repo task folder, not a Claude Code session |
| PRIORART-4 | major | only marker-carrying Workflow agents are visible; session subagents are not |
| PRIORART-5 | major | parallel fan-out renders as identical rows and one collapsed graph node |
| PRIORART-6 | major | flat schema cannot express a DAG; no version field |
| PRIORART-7 | major | full transcript re-parse every second, O(size) and growing |
| PRIORART-8 | major | replay-from-zero on every connect, no cursor, unbounded log DOM |
| PRIORART-9 | major | stops leave no journal marker → paused runs reported as completed |
| PRIORART-10 | major | `resumeFromRunId` cache replay duplicates the whole history |
| PRIORART-11 | major | one watcher process per run, run identity in module globals |
| PRIORART-12 | minor | unknown `?task=` silently streams a different task |
| PRIORART-13 | minor | catch-all 200 for every unknown path; method never parsed |
| PRIORART-14 | minor | multi-writer append safety rests on an undocumented invariant |
| PRIORART-15 | minor | reserved magic names share the namespace with user data |
| PRIORART-16 | minor | `classify()` sleeps in the poll thread; fan-out stalls it |
| PRIORART-17 | minor | zero tests on the network layer; frontend tested by grep |
| PRIORART-18 | nit | reuse/generalize/replace decision record |
