# research-monitoring-attempt-1

Perspective: the monitoring module `.claude/shared/monitoring/` — architecture
and semantics Touch inherits or copies, real defects/races/security gaps in the
current code, `monitoring.md` ↔ code divergence, test-coverage gaps, and what
Touch must copy verbatim vs. deliberately not inherit.

Everything below was read at the line numbers cited. Empirical checks were run
in throwaway dirs under `/tmp/claude-1000` only; the live task folder was
touched only by the two mandated `status.sh` calls.

All four test suites pass as of this run (`test_server.py` 16/16,
`test_watcher.py`, `test_shell.py`, `test_frontend.py`).

---

## MONITORING-1 — watcher dies with an unhandled traceback when `ORCH_STATE_DIR` does not exist yet

**file:line**: `.claude/shared/monitoring/decision_watcher.py:150` (and the
first caller, `:534`)
**severity**: major

### Scenario (verified)

`emit()` opens `EVENTS` with `open(EVENTS, "a")` and never creates `STATE_DIR`.
`status.sh:22` was fixed for exactly this (SHELL-6 — `mkdir -p "$STATE_DIR"`),
but the watcher never got the same treatment. Reproduced:

```
ORCH_STATE_DIR=/tmp/claude-1000/wtest/nonexistent-state \
ORCH_WF_DIR=/tmp/claude-1000/wtest/wf python3 decision_watcher.py
...
  File ".../decision_watcher.py", line 150, in emit
    with open(EVENTS, "a") as f:
FileNotFoundError: [Errno 2] No such file or directory:
  '/tmp/claude-1000/wtest/nonexistent-state/events.jsonl'
```

This is a launch-order race, not a hypothetical: `monitoring.md:117-122` and
`CLAUDE.md` both show the two daemons started "in any order", backgrounded with
`&`, before/around the seeding `status.sh` calls. If the watcher wins the race
against the first `status.sh` (which is what creates the folder), it dies on its
very first line — the `emit("watcher", "info", "decision watcher online")`
heartbeat — and because it is backgrounded, the traceback lands in a `.log`
nobody reads. The run then produces **zero deterministic events**: no spawns, no
verdicts, no token accounting. The dashboard shows only the best-effort
`status.sh` colour, which `monitoring.md:100-101` explicitly says an agent may
skip for free. Nothing in the system detects or reports the loss.

### Recommendation

Add `os.makedirs(STATE_DIR, exist_ok=True)` next to the `STATE_DIR` resolution
(`decision_watcher.py:40`), and wrap the `emit()` write in a `try/except OSError`
that logs to stderr instead of killing the process. Also add a `test_watcher.py`
case that points `ORCH_STATE_DIR` at a non-existent nested path and asserts the
first `emit()` succeeds — the existing suite never calls `emit()` at all.

---

## MONITORING-2 — no authentication, no `Origin` check, binds `0.0.0.0`; Touch must not inherit this posture once it adds controls

**file:line**: `.claude/shared/monitoring/monitor_server.py:519` (bind),
`:412-448` (the `/ws` upgrade path), `:449-506` (`/tasks`, `/artifacts`,
`/file`)
**severity**: blocker (as a Touch design decision)

### Scenario (verified)

The `/ws` upgrade validates only `Sec-WebSocket-Key` and
`Sec-WebSocket-Version`. There is no `Origin` check, no token, no `Host`
validation, and the listener is `0.0.0.0`. Verified against a local instance:

```
GET /ws  Origin: https://evil.example.com
-> HTTP/1.1 101 Switching Protocols        (upgrade accepted, full replay streams)

curl -H "Host: evil.com" /health           -> {"status": "ok"}
curl /tasks                                -> full task list + last event detail + token totals
```

WebSockets are not subject to the same-origin policy, so **any page the user
visits while the monitor is running can open `ws://localhost:8931/ws?task=…` and
read the entire event history** — plan names, agent labels, detail strings, and
(via `/tasks`) every task folder name. `/artifacts` + `/file` are same-origin
protected against `fetch` reads only by the absence of CORS headers; they are
fully open to anything that can reach the port directly, and `monitoring.md:135`
actively recommends publishing the port to the host without a word of warning.

Today this is read-only leakage of a dev dashboard — genuinely low stakes. The
blocker is what Touch adds on top. `README.md` requires **pause, restart, start
and terminate** controls, and `.claude/skills/touch-orchestrate/SKILL.md` §4
specifies the mechanism: the UI appends `{"action":"stop","name":"…"}` lines to
`.touch/control.jsonl`, which the orchestrator polls and turns into `TaskStop`
calls. Bolting that write path onto this server design gives every page in the
user's browser — and every host that can reach the published port — the ability
to terminate the user's agents mid-run, with no CSRF barrier whatsoever
(WebSockets bypass SOP outright; a plain `<form>` POST bypasses it for HTTP).

### Recommendation

Make this an explicit, up-front decision in the Touch plan rather than an
inherited default:

1. Bind the control-capable server to `127.0.0.1` by default; require an
   explicit opt-in flag for `0.0.0.0` (and document the sandbox `sbx ports`
   flow as that opt-in).
2. Enforce an `Origin` allowlist on the `/ws` upgrade (reject anything not
   matching the served origin with `403`) — this is ~5 lines in `handle()` and
   is the single highest-value change.
3. Put every mutating endpoint behind a per-process bearer token minted at
   startup and injected into `monitor.html` at serve time, and require it on
   both the socket and any POST.
4. Keep the read-only monitor and the control plane as separate route groups so
   the "just show me the run" mode stays deployable without the control risk.

Copy verbatim from the current server: `safe_artifact_path()` (extension
whitelist + realpath containment, `monitor_server.py:199-212`) and the
`Content-Security-Policy: sandbox allow-scripts` + `X-Content-Type-Options:
nosniff` headers on `/file` (`:480-493`). Those parts are correct and
well-tested.

---

## MONITORING-3 — the watcher's only event source is the Workflow journal, which `touch-orchestrate` runs do not produce

**file:line**: `.claude/shared/monitoring/decision_watcher.py:53-73`
(`resolve_wf_dir` / `JOURNAL`), `:569-729` (the whole tail loop)
**severity**: major

### Scenario (verified)

The watcher is hard-wired to `<wf_dir>/journal.jsonl` and auto-discovers via
`~/.claude/projects/*/*/subagents/workflows/wf_*/journal.jsonl`. Every event it
emits is derived from `type: "started"` / `type: "result"` entries in that
journal (`:587`, `:667`), and every per-agent read globs
`.../subagents/workflows/<WF_NAME>/agent-<id>.jsonl` (`:86-100`).

`.claude/skills/touch-orchestrate/SKILL.md` §2 mandates the opposite spawn
mechanism: "**Background spawns.** Spawn via the Agent tool with
`run_in_background` so each agent is a harness-tracked task that `TaskStop` can
kill individually", with the spawn recorded in
`<task-dir>/state/spawn-ledger.jsonl`. Those spawns produce no workflow journal.
Verified on this machine — `subagents/` contains nothing but `workflows/`:

```
$ ls -d ~/.claude/projects/*/*/subagents/*/
.../292fc08c-.../subagents/workflows/
.../dd469822-.../subagents/workflows/
.../e423cd3c-.../subagents/workflows/
```

So a run executed to `touch-orchestrate` standards yields an **empty
`decision_watcher.py`** — it either `sys.exit`s ("no workflow journal found",
`:67`) or latches onto an unrelated older `wf_*` run and narrates that instead.
Stoppability (the `run_in_background` + `TaskStop` requirement, which is the
whole point of Touch's control half) and deterministic monitoring (the journal,
which is the whole point of the watcher) are currently **mutually exclusive**.
The `touch-orchestrate` SKILL.md hand-waves this at the end of §2 — "keep its
`[monitor] plan=… …` marker and `status.sh` calls … the two markers coexist" —
but `status.sh` alone is the best-effort layer `monitoring.md:100` says costs
nothing to skip. There is no deterministic source left.

### Recommendation

The reconciled plan must pick one and write it down as a global decision:

- **(a)** Touch's aggregator replaces `decision_watcher.py` for
  `touch-orchestrate` runs: tail `state/spawn-ledger.jsonl` (name → `taskId`)
  plus the harness task list, and read per-agent transcripts from wherever
  background Agent-tool spawns land, emitting the same `events.jsonl` schema so
  `monitor.html` and `monitor_server.py` keep working unchanged; or
- **(b)** `touch-orchestrate` drops `run_in_background` in favour of Workflow
  `agent()` spawns and finds another stop mechanism (which contradicts its own
  §4), or
- **(c)** the two modes are declared separate and Touch supports both
  explicitly, with the watcher reserved for Workflow-driven runs.

Whichever is chosen, an implementer must first empirically confirm where a
`run_in_background` Agent-tool spawn writes its transcript and whether token
usage is recoverable from it — `agent_tokens()`'s entire contract
(`:154-197`) depends on `message.usage` rows being present and on a stable
per-agent file path.

---

## MONITORING-4 — the event model is flat; it carries no parent/child edges, so Touch's graph view is not derivable from it

**file:line**: `.claude/shared/monitoring/decision_watcher.py:121` (`MARKER`),
`.claude/shared/monitoring/monitoring.md:30-46` (event schema)
**severity**: major

### Scenario

The schema is two levels deep and no deeper: `plan` (one card) → `agent`
sub-object (`{id, label, state, tokens, started, runtime}`). `monitor.html`
groups agent rows into a "flow strip" by splitting `label` on `" #"`
(`monitor.html:287-288`) and orders roles by first-seen — the comment at
`:266-271` is explicit that "that order IS the loop topology". That is a
heuristic over a display string, not a data model.

`README.md` asks for "n8n-like UML diagrams/graphs" per terminal session, and
`touch-orchestrate/SKILL.md` §1 defines exactly the missing structure — a
`ROOT_NAME`, a `<parent>_<role><N>` derivation, arbitrary nesting
(`auth_refactor_subagent1_subagent1`), and a first-line marker that states the
edges explicitly:

```
[touch] name=<name> parent=<parent_name> root=<ROOT_NAME> role=<role> attempt=<N>
```

`decision_watcher.py`'s `MARKER` regex matches only `[monitor] plan=… stage=…
role=… attempt=…`. It has no `name`, no `parent`, no `root` — an agent carrying
only the `[touch]` marker classifies as `None` and gets the generic "spawn
unclassified agent" line (`:664-666`). Consequently there is nothing in
`events.jsonl` from which a parent→child graph can be reconstructed, and the
`agent.id` field is a truncated 8-char harness id (`:636`), not a Touch name, so
even the join key to the spawn ledger is lossy.

### Recommendation

Extend the event schema (and `monitoring.md`, which is normative) with an
optional agent identity block carrying `name`, `parent`, `root` — sourced from
the `[touch]` marker — and teach the classifier to parse both markers into one
record. Emit the full `agentId`, not `agentId[:8]`, so the ledger join is exact.
Keep `plan` as-is for backward compatibility with the existing dashboard: the
graph view should be an additive consumer of the same append-only stream, not a
replacement schema, or the ~800 events of history already in
`.claude/local-orchestrators/` stop rendering.

---

## MONITORING-5 — an unknown `?task=` silently streams a different task's data instead of erroring

**file:line**: `.claude/shared/monitoring/monitor_server.py:148-155`
(`resolve_task_dir` / `resolve_events_path`)
**severity**: major

### Scenario (verified)

```python
return discover_tasks().get(task, STATE_DIR)
```

An unrecognised task name falls back to the server's startup `STATE_DIR`. This
is not traversal-unsafe — it is worse in a subtler way: the client gets a
**successful** stream of somebody else's events. Verified against a server whose
default task held a sentinel plan:

```
GET /ws?task=totally-bogus
-> HTTP/1.1 101 Switching Protocols
-> {"ts":"…","plan":"SECRETPLAN","stage":"plan","state":"running",
    "detail":"default-task-data"}
```

The frontend's FRONTEND-5 fix (`monitor.html:592-613`) already documents this
exact hazard — "the socket still streams the server's STATE_DIR fallback for the
unknown task — URL, crumb and (blank) dropdown then all imply something else" —
but it only patches the *dropdown label*. The server was never fixed, so a
bookmarked or shared `?task=<renamed-or-deleted>` URL still renders another
run's progress under the requested run's name and crumb.

For Touch this stops being cosmetic. Once task names route control actions
(`stop`/`pause`/`terminate`, per `README.md` and `touch-orchestrate` §4), a
silent name→wrong-directory fallback means a control command aimed at a typo'd
or stale task lands on **whatever run happens to be the server default** —
plausibly a live one.

### Recommendation

Make `resolve_task_dir` return `None` for unknown names. `/ws` should reply
`404` (or accept and immediately send one `{"error":"unknown task"}` frame then
CLOSE); `/tasks`, `/artifacts`, `/file` should return `404`. Never fall back to
a different task's state. Add a `test_server.py` case asserting
`resolve_task_dir("nope")` does not return `STATE_DIR` — the current suite tests
`safe_artifact_path` containment thoroughly but never tests task resolution at
all.

---

## MONITORING-6 — concurrent appends to `events.jsonl` are unlocked and unbounded; corrupted lines are then silently dropped

**file:line**: `.claude/shared/monitoring/status.sh:28-49`,
`.claude/shared/monitoring/decision_watcher.py:150-151`
**severity**: minor

### Scenario (verified)

Both writers append with no `flock`, relying on a single `O_APPEND` `write()`
being atomic. That holds only while the serialized event fits in one buffer
flush. Python's block buffer is 8 KiB, so a long `detail` splits into several
`write()` calls and interleaves with another writer. Measured with 20 concurrent
`status.sh` invocations:

| detail size | lines written (expected 20) | unparseable |
|---|---|---|
| 6 KB  | 30/30 (separate run) | 0 |
| 20 KB | 20 | 0 |
| 60 KB | **19** | **1** |

The corruption is then invisible. `task_status()` swallows it
(`monitor_server.py:96-99`, `except (json.JSONDecodeError, UnicodeDecodeError):
continue`); `read_frames()` forwards the broken bytes verbatim; and the browser
drops them in `ws.onmessage = (m) => { … try { onEvent(JSON.parse(m.data)) }
catch (e) {} }` (`monitor.html:706`). Nothing anywhere counts a parse failure,
so an event that vanishes leaves no trace at all — including the *other*
writer's event that got shredded alongside it.

The informal mitigation lives only in `CLAUDE.md` ("Keep event `detail` strings
short, single-line, and free of double quotes"). `monitoring.md` — the normative
spec — states no length constraint anywhere, and the schema calls `detail` just
`"<short text>"` (`monitoring.md:30`). Touch will multiply the writer count
(one aggregator plus every agent's `status.sh`) and is likely to relax the
"short detail" convention as soon as it wants to surface tool output.

### Recommendation

Wrap both append sites in `flock` (`flock "$STATE_DIR/events.jsonl" -c …` in
`status.sh`; `fcntl.flock` around the `emit()` write). Truncate `detail` to an
explicit documented cap (~1 KB) at the writer, and state that cap in
`monitoring.md`'s schema section. Add a parse-failure counter to
`monitor_server.py` surfaced via `/health`, and a `console.warn` in
`ws.onmessage`'s catch, so silent loss becomes visible loss.

---

## MONITORING-7 — the dashboard re-renders every card on every event and never bounds the log, so replay cost is quadratic

**file:line**: `.claude/shared/monitoring/monitor.html:391` (`render()` at the
end of `onEvent`), `:220-238` (`render`), `:390` (unbounded `log.insertBefore`)
**severity**: minor

### Scenario

`onEvent` ends with an unconditional `render()`. `render()` walks every plan,
re-`appendChild`s every card into `#cards`, calls `placeArtifacts()`, and
recomputes both the per-plan and grand-total token sums. The server replays the
**entire** history on every connect (`monitor_server.py:363-378` starting at
`offset = 0`; `monitor.html:699-705` clears and rebuilds), so opening a task page
runs `render()` once per historical event. Each of those events also appends a
permanent `<li>` to `p.log`, which is never trimmed.

Current scale is survivable — `touch-aggregator/events.jsonl` is 590 events /
236 KB — but Touch's stated scope is a live view over a **whole Claude Code
session** with a sidebar of many such sessions, and the quiet 1 Hz token ticks
(`decision_watcher.py:764-795`) are per-running-agent per-second, so event count
grows linearly in agents × runtime. A multi-hour parallel run reaching tens of
thousands of events makes the initial paint visibly slow and the DOM unbounded,
and the `refresh` dropdown's `forceResync` (`:716-724`) re-pays that cost on
every interval the user selects.

### Recommendation

Coalesce `render()` behind a `requestAnimationFrame` dirty flag (it is already
idempotent, so this is a safe mechanical change). Cap `p.log` to a rolling
window (~500 rows) by removing `lastChild` after insert — the full stream stays
on disk and is the source of truth. If Touch keeps the replay-on-connect
contract (it should — `monitoring.md:172-175` is right that it is the thing that
makes double-counting impossible), add a server-side `?since=<offset>` so a
reconnecting client can resume rather than rebuild.

---

## MONITORING-8 — the watcher has no journal-truncation detection, so a shrunk journal stalls it forever and silently

**file:line**: `.claude/shared/monitoring/decision_watcher.py:575`
(`if size > state["offset"]`), `:494-512` (`load_state`)
**severity**: minor

### Scenario

`monitor_server.read_frames` handles this case explicitly — `if size < offset:
return [], -1`, the D10 truncation sentinel, covered by
`test_server.py:69-80`. The watcher has no equivalent. Its loop only ever acts
when `size > state["offset"]`; if the journal shrinks below the checkpointed
offset (rotation, a re-created `wf_dir`, a partially-written file after a crash)
the condition is false forever and the watcher spins at 1 Hz emitting nothing,
with no log line and no `stale`/`failed` event. From the dashboard this is
indistinguishable from "the orchestrator is thinking".

`load_state()` guards only the *path* changing (`state.get("journal") !=
JOURNAL`, D8, tested at `test_watcher.py:126-140`). A journal at the **same
path** that got shorter passes that check and keeps the stale offset. This is a
genuine spec/implementation divergence: `monitoring.md:23` promises
`.watcher-state.json` is "restart-safe, never duplicates events" but says
nothing about the failure mode where it never *emits* events either.

### Recommendation

Mirror the server: in the main loop, if `size < state["offset"]`, reset
`offset = 0`, clear derived run state the same way `load_state`'s `fresh` dict
does, emit one `watcher info "journal truncated — rebuilding"` event, and
re-backfill. Document the behaviour in `monitoring.md` next to the existing
"Resetting a run" note (`:188-191`).

---

## MONITORING-9 — token accounting re-parses every running agent's full transcript once per second

**file:line**: `.claude/shared/monitoring/decision_watcher.py:154-197`
(`agent_tokens`), `:764-795` (the 1 Hz live-tick loop), `:86-100`
(`agent_paths`)
**severity**: minor

### Scenario (measured)

`agent_tokens()` opens every transcript copy of an agent and `json.loads` every
line, from byte zero, every call. The live-tick block calls it for **each**
entry in `state["running"]` on **every** poll (1 s). `agent_paths()` runs a
filesystem glob over `~/.claude/projects/*/*/subagents/workflows/<WF>/` on each
of those calls, and `first_ts` / `last_ts` each call it again — so a single
result handler does the glob-and-read work three times over
(`:678` and `:705` even call `agent_tokens()` twice for the same agent in the
same branch).

Measured on the largest real transcript in this repo's history: 1.0 MB / 50
lines / 16 ms to parse. Current runs are ~6 parallel agents at ~0.5-1 MB each,
i.e. roughly 100 ms of CPU per second, sustained — noticeable but tolerable. It
scales as O(agents × transcript bytes) per second, and the `/clear`-rotation
handling deliberately *multiplies* the file count per agent (`:75-84`). Touch's
ambition (a whole session, many concurrent agents, a sidebar of sessions) puts
this well past comfortable.

### Recommendation

Keep the message-id dedup — it is the correct primitive and `monitoring.md:179-182`
is right to call it normative — but make the read incremental: store per
transcript path a `(byte offset, partial sums, seen message ids)` tuple in
`.watcher-state.json` and only parse the appended tail each tick. Hoist the
`agent_paths()` glob to once per tick per agent instead of three times per call
site, and reuse the single `agent_tokens()` result in the result branch instead
of recomputing it at `:678` and `:705`.

---

## MONITORING-10 — several watcher "tests" are tautologies that re-implement the logic instead of calling it

**file:line**: `.claude/shared/monitoring/tests/test_watcher.py:147-151`,
`:159-175`, `:181-187`, `:266-271`
**severity**: minor

### Scenario

These blocks assert on expressions written inline in the test file, not on
anything the module exports:

```python
st_close = "done" if {"decisive": {}}["decisive"].get("sp1") else "failed"
check("sequenced-close: missing decisive -> failed", st_close == "failed")
```

```python
d_in = max(0, cur_in - prev.get("in", 0))
check("token clamp: shrunk in -> delta 0", d_in == 0)
```

```python
def should_stale(new_attempt, old_attempt):
    return not (new_attempt <= old_attempt)
check("DRIVER-1: equal attempt (parallel siblings) -> no stale", should_stale(1, 1) is False)
```

Every one of these passes whether or not `decision_watcher.py` still contains
the corresponding logic — they test Python's ternary operator and `max()`. The
sweep test at `:159-164` likewise copies the main-loop body into the test rather
than invoking it. The only thing tying them to the source is the one grep at
`:273-275` (`'info["attempt"] <= oinfo["attempt"]' in main_src`), which is a
string match that any refactor of the same logic breaks and any comment
containing that text satisfies.

The genuine coverage gaps behind this: `emit()` is never called (which is why
MONITORING-1 shipped), `main()`'s tail loop is never exercised end-to-end
(spawn → result → plan close → run-complete), and on the server side only pure
helpers are tested — `handle()`, the `/ws` handshake, `resolve_task_dir`, and
`stream_events` have no tests at all (which is why MONITORING-5 shipped).

### Recommendation

Replace each tautology with a call into the module. The plan-close and
stale-close decisions are worth extracting from `main()` into small pure
functions (`close_state_for(plan, decisive)`, `should_stale(new, old)`) so they
become testable for real. Add one end-to-end test that writes a synthetic
`journal.jsonl` + agent transcripts into a temp `WF_DIR`, runs a bounded number
of main-loop iterations, and asserts on the resulting `events.jsonl` lines —
that single test would have caught MONITORING-1 and MONITORING-8. Add a
`test_server.py` case for unknown-task resolution.

---

## MONITORING-11 — malformed config/env values kill the watcher at import; the server handles the same class of error cleanly

**file:line**: `.claude/shared/monitoring/decision_watcher.py:106-113`
**severity**: minor

### Scenario

```python
MAX_PLAN_ATTEMPTS = int(_CAPS_CFG.get("max_plan_attempts", 4))
MAX_GATE_ATTEMPTS = int(_CAPS_CFG.get("max_gate_attempts", 3))
MAX_E2E_ATTEMPTS  = int(_CAPS_CFG.get("max_e2e_attempts", 3))
QUIET_SECS = int(os.environ.get("ORCH_QUIET_SECS", "60"))
```

All four are module-level, unguarded. A hand-edited `orch-config.json` with
`"max_gate_attempts": "three"` (or `null`, or a float string), or an
`ORCH_QUIET_SECS` typo, raises `ValueError` **at import** — before the
`emit("watcher", "info", "decision watcher online")` heartbeat — so the watcher
dies without a single event, exactly like MONITORING-1.

This is a straight divergence from the server, which got the SERVER-2 treatment
for the identical hazard (`monitor_server.py:225-241`: non-integer argv/env →
`sys.exit(f"invalid port from {label}: {source!r}")`, tested at
`test_server.py:87-116`). `monitoring.md:22` documents these keys as
user-editable config without noting that a bad value is fatal.

Note that the `orch-config.json` files already in
`.claude/local-orchestrators/*/` are carried over from a different project (per
`CLAUDE.md`), so they are exactly the kind of file someone will hand-edit.

### Recommendation

Give the watcher the same treatment: a small `_int_cfg(value, default, label)`
helper that falls back to the default with a stderr warning, or `sys.exit`s with
a one-line message. Move the resolution below the first `emit()` so a
configuration mistake still produces a visible event on the dashboard.

---

## MONITORING-12 — `/file` buffers whole artifacts in memory with no size cap

**file:line**: `.claude/shared/monitoring/monitor_server.py:471`
**severity**: minor

### Scenario

```python
body = await asyncio.to_thread(lambda: open(full, "rb").read())
```

The listing side is carefully bounded — `task_artifacts()` stops at depth 4 and
300 files with the docstring "so a runaway folder cannot stall the endpoint"
(`:164-196`) — but the serving side has no equivalent limit. Any whitelisted
`.md`/`.html` under a task folder is read fully into memory per request, and
`monitor.html` polls `/artifacts` every 5 s while the user can click any chip.
Task folders legitimately accumulate large agent-written notes (the ones in this
repo already run to 35 KB findings files; a report embedding base64 images or a
gate dumping full test output is unbounded). Several concurrent `/file` requests
for a large report multiply that.

### Recommendation

`os.stat` the resolved path first; refuse above a documented cap (say 8 MB) with
`413`, and stream the body in chunks below it. Document the cap alongside the
extension whitelist in `monitoring.md:158-169`, which currently describes the
containment guarantees but says nothing about size.

---

## MONITORING-13 — the markdown link whitelist admits protocol-relative `//host` URLs

**file:line**: `.claude/shared/monitoring/monitor.html:473-475`
**severity**: nit

### Scenario

```js
.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, txt, href) =>
  /^(https?:\/\/|#|\.{0,2}\/)/.test(href) ? '<a href="' + href + '" …' : m);
```

The third alternative `\.{0,2}\/` matches zero dots followed by `/`, so
`//evil.example.com/x` passes as a "relative path" and renders as a live link to
an arbitrary external origin. The stated intent in the comment above is
"whitelisted to `https?://`, `#anchor`, and relative paths".

The escape-first discipline itself is sound and I could not break it: `esc()`
runs before `mdInline()`, `"` is already `&quot;` by the time `href` is
interpolated (so no attribute breakout), `javascript:` is rejected, and the
anchor carries `target="_blank" rel="noopener"`. Impact is therefore limited to
a user-initiated navigation to a host chosen by whoever wrote the `.md` — i.e.
an agent. Low, but it is a hole in a whitelist that the code and
`monitoring.md:158-161` both advertise as closed.

### Recommendation

Tighten to `/^(https?:\/\/|#|\.{0,2}\/(?!\/))/`. Add a `test_frontend.py`
assertion for the negative-lookahead — the current frontend suite checks only
that `"https?:"` appears somewhere in `mdInline` (`test_frontend.py:109`), which
this regex satisfies today and would still satisfy after the fix.

---

## MONITORING-14 — `monitoring.md` is silent on the security posture, the `detail` length constraint, and the `watcher` stage

**file:line**: `.claude/shared/monitoring/monitoring.md:30-46` (schema),
`:135-138` (deployment), `:41-46` (reserved ids/stages)
**severity**: nit

### Scenario

`monitoring.md` is treated as normative by `CLAUDE.md` ("treat that file as
normative") and is what a Touch implementer will read first. Three things it
does not say, each of which caused or hides a finding above:

1. **Security.** No mention that the server binds `0.0.0.0`, performs no
   authentication, and does not check `Origin`. Worse, `:135-138` recommends
   running it on the host — "In a sandbox whose ports the host can't reach, run
   `monitor_server.py` on the host instead" — with no caveat. (MONITORING-2.)
2. **`detail` length.** The schema calls it `"<short text>"` and stops there.
   The real constraint ("short, single-line, free of double quotes") exists only
   in `CLAUDE.md`, and the reason for it (unlocked concurrent appends, buffer
   splitting) is written down nowhere. (MONITORING-6.)
3. **The `watcher` stage.** `:42` enumerates reserved stages as `plan`,
   `complete`, `tokens`. `decision_watcher.py` also emits `stage: "watcher"` for
   its own lifecycle and unclassified-agent lines (`:534`, `:665`, `:724`),
   which renders as an ordinary chip on the orchestrator card. A Touch
   implementer reading the reserved list will not know to special-case it.

Also undocumented: `/tasks` now returns a per-task `status`/`last`/`tokens`
payload driving a home grid (`monitor_server.py:70-145`,
`monitor.html:636-669`), while `monitoring.md:19` still describes `/tasks` as
merely "lists every discovered task folder".

### Recommendation

Add a short "Security posture" section stating the current guarantees honestly
(local dev tool, no auth, read-only) and what must change before any write
endpoint exists. Put the `detail` cap in the schema table with its rationale.
Add `watcher` to the reserved-stage list. Refresh the `/tasks` row. Because
`test_shell.py:131-151` already static-asserts on `monitoring.md` content, the
matching guards are cheap to add there.

---

## Appendix — what Touch should copy verbatim vs. deliberately not inherit

Not findings; the reconciled plan needs this split stated explicitly.

**Copy verbatim (these are correct and earned):**

- **Append-only `events.jsonl` as the single source of truth**, with full replay
  on connect and a page that rebuilds from scratch. `monitoring.md:172-175` is
  right that this is what makes double-counting structurally impossible.
- **Torn-tail handling** on both readers: cut at the last `\n`, never advance the
  offset past an incomplete line (`monitor_server.read_frames:330-355`,
  `decision_watcher.read_new_lines:470-491`), decode with `errors="replace"`.
  Well tested (`test_server.py:36-85`, `test_watcher.py:96-121`).
- **Monotonic token deltas** clamped `>= 0` with a never-lowered baseline (D7,
  `decision_watcher.py:714-722`) and **dedup by API message id** across
  session-rotated transcript copies (`:165-186`). The `/clear`-rotation search
  across sibling session dirs (`:75-100`) is the non-obvious part and is right.
- **Path containment for served files**: extension whitelist + `realpath` +
  `startswith(base + os.sep)` (`safe_artifact_path:199-212`), plus
  `CSP: sandbox allow-scripts` and `nosniff` on `/file`. Thoroughly tested
  (`test_server.py:235-260`).
- **Escape-first rendering discipline**: `esc()` before any inline transform,
  `createElement`/`textContent` over `innerHTML`, the `NODE_STATES` whitelist
  (`monitor.html:295-321`, `:463-543`). Treat any new renderer in Touch as
  bound by the same rule.
- **Checkpoint keyed to its source** (`.watcher-state.json` storing the journal
  path, D8) so a source switch resets rather than silently mis-seeks.
- **Statelessness of the module + `ORCH_STATE_DIR` as the only state selector**
  (D6). Touch should keep code and per-task state disjoint the same way.

**Deliberately do not inherit:**

- The **network posture** (`0.0.0.0`, no auth, no `Origin` check) — see
  MONITORING-2. This is the one that must be decided before any control
  endpoint is written, not after.
- The **unknown-task fallback to the default state dir** — MONITORING-5. Touch
  routes actions by task name; silent fallback is a wrong-target bug.
- The **1 Hz full-transcript re-parse** — MONITORING-9. Fine for one workflow,
  not for a session-wide aggregator.
- The **render-everything-per-event frontend loop and unbounded log** —
  MONITORING-7.
- **Unlocked appends with no length cap** — MONITORING-6. Touch adds writers.
- The **flat plan→agent schema** as the graph model — MONITORING-4. Extend it
  additively; do not treat `label`-string parsing as topology.
- The **Workflow-journal-only event source** — MONITORING-3. Resolve the
  conflict with `touch-orchestrate`'s background-spawn mandate before building
  on either.
