# Touch — complete implementation plan (aggregator + touch-visual)

Synthesized from six blind research reports plus the driver context. Findings
stay in the research files; this plan references them by id:

- SESSIONDATA-* → `findings/research-sessiondata-attempt-1.md`
- AGENTGRAPH-*  → `findings/research-agentgraph-attempt-1.md`
- LIVEIO-*      → `findings/research-liveio-attempt-1.md`
- CONTROL-*     → `findings/research-control-attempt-1.md`
- PRIORART-*    → `findings/research-priorart-attempt-1.md`
- STACK-*       → `findings/research-stack-attempt-1.md`

(all under `/home/laniakea/Projects/touch/.claude/local-orchestrators/touch-aggregator/`).
Driver context: `context/driver-context.md` (same root), cited as DRIVER §n.

---

## Part A — Conflict resolutions (research vs driver context)

Each was settled by re-opening the primary source during synthesis; the checks
were run against `/home/agent/.local/share/claude/versions/2.1.220` and
`~/.claude/projects/-home-laniakea-Projects-touch/`.

1. **Transcript write latency.** DRIVER §3.A claimed "≈5 s per completed
   message, no intra-turn flush, a 90 s turn is a 90 s void". SESSIONDATA-5
   measured a `tool_use` record landing **+0.10 s**, intra-turn, and cites
   `FLUSH_INTERVAL_MS = 100` (re-verified: the constant exists in the binary).
   **Decision: driver overridden.** Records land per *completed content block*
   on a ~100 ms flush timer; LIVEIO-11's 1–5 s inter-append gaps are block
   completion cadence, not flush cadence. A void only occurs inside one long
   uninterrupted thinking/text block. Consequence: transcript-fed panes are
   honest at block granularity and sub-second freshness; only token-streaming
   and the TUI require the PTY.
2. **Out-of-band tool results.** DRIVER §3.A claimed large tool results spill
   to a `tool-results/` dir (threshold ~50000); SESSIONDATA-12 observed only
   inline results (max 17 KB). **Decision: driver confirmed.** Re-grep of the
   binary shows `Aas="tool-results"`, `persistedOutputPath` ("Path to the
   persisted full output in tool-results dir (set when output is too large for
   inline)"), `persistedOutputSize`, and a `<persisted-output>` placeholder.
   Research simply never triggered it. The transcript renderer and ingester
   MUST handle pointer records (item T7).
3. **`--bg-pty-host` invocability.** DRIVER §3.B called it "daemon workers
   only"; LIVEIO-2 ran it standalone twice (verified argv string
   `bad argv: --bg-pty-host <sock> <cols> <rows> -- <file> [args...]` present
   in the binary). **Decision: driver overridden on invocability** — it can be
   spawned directly against the versioned binary. It remains **rejected as a
   build target** (LIVEIO-4, CONTROL-14, STACK-18: private, version-coupled,
   wrapper CLI rejects the flag); recorded in D14.
4. **`projects/<sid>/workflows/<runId>.json`.** SESSIONDATA said "does not
   exist on disk"; AGENTGRAPH-6/DRIVER say it is written once at run end. No
   real conflict: the writer (`OSd`, single call site on the completion path)
   is real, the file appears only after a run ends and only if the CLI
   survives. Treated as late, optional reconciliation input (D8).
5. **"Zero third-party dependencies" and "the firewall blocks installs".**
   DRIVER §5 states the precedent; STACK-8 proved npm/PyPI installs work
   through the proxy. **Decision: both partially right; the codified rule is
   D8's** — stdlib-only at runtime, zero network fetches from the page, npm
   allowed only as a build-time tool to vendor pinned, committed assets.
6. **Thinking persistence, session registry staleness, no-attach, pause
   nonexistence** — driver and research agree independently
   (SESSIONDATA-2/-7, LIVEIO-1 = CONTROL-2 = STACK-1, LIVEIO-9 = CONTROL-4).
   Treat all four as **confirmed by independent evidence**.

---

## Part B — Global decisions (binding for every item)

### D1. Process ownership: Touch hosts; it never attaches

There is no channel into an already-running interactive session — no socket,
no port, no TIOCSTI, no signal (LIVEIO-1, CONTROL-2, STACK-1; independently
confirmed by DRIVER §3.B). Therefore:

- **Owned session**: spawned by Touch under a PTY Touch holds. Full terminal,
  full input, full control surface.
- **Observed session**: discovered in `~/.claude/sessions/*.json`. Read-only
  semantic transcript view; **no** terminal pane, **no** control affordances in
  v1 (not even kill — least surprise; the registry file may describe a pid
  Touch shouldn't touch). UI must label the class on every pane ("read-only —
  not started by Touch").

Every UI affordance maps to exactly one class; an affordance that cannot be
honest for a class is not rendered for it.

### D2. The two views

- **Terminal page** (owned only): raw PTY bytes → WebSocket → vendored
  xterm.js. Byte-faithful, sub-frame latency, alt-screen/mouse/24-bit safe
  (STACK-9).
- **Transcript view** (observed sessions; also the detail panel everywhere): a
  *semantic re-render* of the JSONL — prompts, assistant text, tool cards with
  full untruncated results, turn dividers, diffs. Explicitly not a terminal
  (SESSIONDATA-12 option (a)). No thinking pane ever — thinking text is never
  persisted (SESSIONDATA-2); render "thought for N s" gap markers only.
- v1 sessions are PTY-hosted interactive only. The stream-json session mode
  (LIVEIO-5/CONTROL-3) is deferred: hooks + files already provide all graph
  data (LIVEIO-10), and one session type keeps the spawn/control matrix small.

### D3. Canonical identity and dedup keys (LIVEIO-18, DRIVER §7.3)

| Entity | Key | Rule |
|---|---|---|
| Session (sidebar unit) | `(pid, procStart)` | `procStart` = `/proc/<pid>/stat` field 22; `sessionId` is mutable, re-resolved every poll (SESSIONDATA-6, STACK-12) |
| Transcript record | `uuid` | upsert-by-uuid; re-ingest is a no-op (SESSIONDATA-3) |
| API response (tokens) | `message.id` | usage summed per distinct id, never per record (SESSIONDATA-1) |
| Tool invocation | `tool_use_id` | joins hook events, transcript, meta.json |
| Agent | full 17-hex `agentId` | never truncated (AGENTGRAPH-13) |
| Workflow run | `runId` (`wf_…`) | from the Workflow `toolUseResult` (AGENTGRAPH-11) |
| Graph node | `(runId, key, ordinal)` | journal `key` = logical node; each extra `started` for the same key = new ordinal, prior one `superseded` (AGENTGRAPH-3/-4, PRIORART-10) |

Late-arriving channels enrich the existing entity; they never create a second
one.

### D4. Canonical event model: "touch events v2" (DRIVER §7.3 decided)

One new stream format. Every record:

```json
{"v":2,"seq":184,"ts":"2026-07-25T03:20:00.000Z","source":"ingest|hook|control|pty|legacy",
 "kind":"session|agent|tool|run|node|token|control|log",
 "ref":{"uuid":"…"} | {"toolUseId":"…"} | {"agentId":"…"} | {"runId":"…","key":"…","ordinal":1} | {"pid":622,"procStart":"10028"},
 "data":{...}}
```

- Event log, not last-writer-wins; state is derived by reduction.
- `seq` is assigned by the single-writer aggregator process; multi-writer
  appends exist only on hook spools (short single-`write()` lines —
  PRIORART-14 invariant, documented and tested).
- Semantics live in `kind`, never in magic values of data fields
  (PRIORART-15). Readers treat missing `v` as v1 (legacy).
- **Relation to the existing `events.jsonl`**: the legacy stream is *not*
  extended and *not* subsumed destructively. `status.sh` and the monitoring
  module stay as-is for orchestration runs; the aggregator ingests legacy
  events.jsonl (and archived task folders) as one read-only `source:"legacy"`
  input. Exactly one new format ships.
- DRIVER §7.2 **confirmed**: Touch never appends to any file under
  `~/.claude/` — not transcripts, not journals (CONTROL-6), not
  `~/.claude.json` (SESSIONDATA-18). `~/.claude` is a read-only tap.

### D5. Touch-owned state: `<repo>/.touch/` (DRIVER §7.1 decided)

The CLI retention sweep deletes transcripts and whole subagent trees
(SESSIONDATA-13), so Touch owns its history. Root: `.touch/` at repo root
(gitignored; override `TOUCH_STATE_DIR`):

```
.touch/
  server.json                      # port, token fingerprint, pid
  control.jsonl                    # control audit, single writer (D7)
  hooks/<session_id>.jsonl         # hook spool (multi-writer, short lines)
  sessions/<pid>-<procStart>/
    meta.json                      # spawn record: argv, env hash, sessionIds seen
    events.jsonl                   # canonical v2 stream for this session
    pty.log + pty.idx              # raw PTY spool + {offset,ts} index (LIVEIO-20)
  runs/<runId>/
    events.jsonl                   # canonical v2 node stream for this run
    snapshot.json                  # copy of <runId>.json when it appears
```

Never under `.claude/local-orchestrators/` (that is monitoring history,
protected by CLAUDE.md).

### D6. Read pipeline

- **Poll, don't inotify** (STACK-14): 250 ms tick over
  `~/.claude/sessions/*.json` + open transcripts + open journals; stat-first
  (`st_mtime_ns, st_size`) before reads.
- **Tail checkpoints** are `(st_dev, st_ino, size, offset)`; inode change or
  `size < offset` ⇒ full idempotent re-ingest from 0 (transcripts are NOT
  append-only: `performRemoveByUuid` truncates, `performCompactTranscript`
  rewrites — SESSIONDATA-3). `.compact.tmp.*` beside a transcript ⇒ back off
  200 ms.
- **Torn tails**: cut at last `\n`, defer remainder, incremental UTF-8 decode
  (SESSIONDATA-5; same semantics as `decision_watcher.py:470-491`).
- **Record classification** by the CLI's own 4-bucket table (transcript /
  boundary-cleared / accumulate / last-wins — SESSIONDATA-4). Unknown types:
  retain raw, never render, never crash.
- **Liveness** is `/proc/<pid>` + `procStart` match, never
  `status`/`updatedAt` (SESSIONDATA-7, LIVEIO-12). Registry reads tolerate
  zero-byte/torn JSON: retry once, keep last good value. `claude agents
  --json` is the slow reconciliation path, TTL-cached, never per-request
  (STACK-12).
- **Node liveness is three-state**: `running` (<180 s idle), `finished`
  (journal `result` / parent `tool_result` only), `unknown/possibly stalled`
  (≥180 s idle, show the idle duration) (AGENTGRAPH-9, LIVEIO-19).
- **Hooks are the push channel** (SESSIONDATA-17, LIVEIO-6): opt-in per
  project, append-one-line-and-exit scripts with explicit `"timeout": 5`
  (LIVEIO-7), self-heartbeating (LIVEIO-17). Polling remains the fallback for
  sessions without hooks.
- **No journal auto-discovery ever**: a run is only attached via an observed
  Workflow `toolUseResult`; a missing `wf_dir` means "no live source", not
  "find one" (AGENTGRAPH-14).

### D7. Control semantics (the CONTROL-17 table, decided)

A loop is a JS closure inside the one CLI process; it has no OS identity
(CONTROL-1). All controls are per-verb intents recorded in `.touch/control.jsonl`
as `requested → sent → confirmed | failed | expired`, with confirmation always a
*derived observation*, never an assumption (CONTROL-16, STACK-18). The harness
records nothing about stops (AGENTGRAPH-16, PRIORART-9), so the control log is
the only truth about them and **wins** over quiet-timeout inference.

| Verb | Ships | Mechanism | Granularity | In-flight work | Deterministic? |
|---|---|---|---|---|---|
| Start | v1 | Touch spawns owned session (T9/T14); optionally types a skill invocation | session | n/a | yes |
| Terminate (session) | v1 | escalation ladder: type `/exit\r` → wait 3 s → `killpg SIGHUP` → 2 s → `SIGKILL`; reap; Touch writes the terminal event itself (STACK-7, CONTROL-13) | session | all loops die; `<runId>.json` never written | yes |
| Stop loop | v1 | typed instruction "call `TaskStop({taskId})`" into owned session, gated on registry `status:"idle"`; confirmed by `task_updated`/journal silence | run | running agent aborted; no disk marker — Touch's audit is the record | model-mediated |
| Restart loop | v1 | typed `Workflow({scriptPath, resumeFromRunId})` instruction, same gating; confirmed by a new Workflow tool_result + new journal; replayed nodes rendered "replayed from journal (not re-executed)" (CONTROL-5, AGENTGRAPH-4) | run | in-flight agents respawn from scratch onto a dirty tree — Touch records `git stash create` + `git status --porcelain` checkpoint first and offers restore as a separate action (CONTROL-7) | model-mediated |
| Pause | v1.5 (T15) | PreToolUse/SubagentStart hook gate per `agent_id`: **hold the response**, never `deny`; ≤120 s hold slices; UI states "pause requested → effective at next tool boundary" (CONTROL-8/-9, LIVEIO-9) | agent | pauses only at a tool boundary; an agent mid-reasoning cannot be paused | yes (owned sessions with the Touch hook pack) |

Rejected verbs, recorded so nobody re-hunts for them: suspend/resume of
inference (impossible — CONTROL-17), SIGSTOP "pause" (CONTROL-11),
harness "pause" (it is kill with a different label — CONTROL-4), per-agent
skip/retry (exists in-process, no transport — CONTROL-15), controlling foreign
sessions (CONTROL-2).

### D8. Stack

Python **3.11+**, stdlib only at runtime, **one** asyncio process, **one**
port (default **8932** — 8931 is the live monitor; resolution
argv > `$TOUCH_PORT` > `.touch/server.json` > default), bind **0.0.0.0**
(sandbox publish requirement — LIVEIO-16 overrides STACK-3's 127.0.0.1
default; the compensating control is D9's unconditional token). PTY via
`pty.openpty()` + `Popen(start_new_session=True)` + `loop.add_reader` — never
`pty.fork()` (STACK-5/-6; node-pty cannot build: no g++). Frontend: no
bundler, ES modules + `<script>` tags; vendored xterm.js + fit addon committed
under `touch-visual/vendor/` with sha256 manifest (STACK-8/-9; note
`.gitignore` ignores `node_modules/` and `dist/`, so vendor lives outside
both). Graph: hand-rolled layered SVG, stable node ids, incremental patching
— no dagre/elk/cytoscape/mermaid (STACK-10). Zero network fetches from the
page, enforced by a static test.

**Graph data contract** (AGENTGRAPH-17, normative): harness-derived facts
(edges via `toolUseId` join, workflow directory containment,
`meta.parentAgentId`; spawn/last-activity times from transcript timestamps;
completion from journal `result`/parent `tool_result`; tokens by dedup-summed
`message.usage`) render **solid**; convention-derived facts (`[monitor]
plan/stage/role/attempt` marker, loop edges implied by the script) render
**dashed**; declared-not-observed nodes (seeded `queued`, parsed fan-out)
render as declarations. `toolUseResult.totalTokens` is ignored (last-call-only
— AGENTGRAPH-8); `totalDurationMs`/`toolStats`/`resolvedModel` are trusted.
`<runId>.json` is post-hoc reconciliation only (AGENTGRAPH-6). Journal order
is not spawn order (AGENTGRAPH-1); journal `result` strings are opaque display
text, never parsed as JSON (SESSIONDATA-10).

### D9. Security posture

The daemon reads credential-adjacent files and, once controls exist, its
socket is command execution in a `--dangerously-skip-permissions` repo
(PRIORART-2, STACK-3, SESSIONDATA-15, LIVEIO-3). Non-negotiables:

1. Per-boot random 256-bit token, printed once at startup and embedded in the
   publish instructions; required (via `hmac.compare_digest`) on the page
   load, every `/api/*`, `/ws`, and `/pty`. No unauthenticated route except
   `/health`.
2. `Origin`/`Host` allowlist enforced at WS upgrade; missing `Origin` rejected
   for control-capable sockets.
3. Typed, projected endpoints only over `~/.claude` — ids validated by regex
   (UUID, `wf_[a-z0-9-]{6,}`, 17-hex agentId); **no path parameters**
   (STACK-11). Hard denylist regardless of route: `.credentials.json`,
   `history.jsonl`, `~/.claude.json`, `shell-snapshots/`, `settings.json`.
4. Static serving: extension whitelist + realpath containment under
   `touch-visual/` (pattern of `monitor_server.py:199-212`).
5. PTY input is opt-in per connection (viewer vs driver); exactly one driver
   per session at a time (STACK-13 multi-writer decision); kill messages
   require the driver role + a confirm nonce.
6. Method-dispatching router with default 404; unknown session/run ids 404,
   never fall back to a different target (PRIORART-12/-13).

### D10. Spawn hygiene (owned sessions)

Child env is built from an **allowlist** (`PATH HOME USER LANG TERM COLUMNS
LINES SHELL TZ` + proxy vars + `SANDBOX_VM_ID`), never inherited —
`CLAUDE_CODE_CHILD_SESSION=1` silently disables transcript persistence
(STACK-2). Belt-and-braces `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`. Always
`--session-id <uuid>` so URLs are stable from the first byte (CONTROL-12).
Startup self-check: a transcript file must appear within N s of first prompt
or the UI shows a loud banner. ENOENT on a transcript = "no messages yet",
not an error (SESSIONDATA-6 lazy creation).

### D11. Prior art disposition

`.claude/shared/monitoring/` stays untouched and running except one additive
item (T21). Touch **copies** the battle-tested semantics with attribution
comments — torn-tail tailing, checkpoint-with-identity + atomic `os.replace`,
message-id token dedup with monotonic clamps, session-rotation glob union,
realpath containment, escape-first rendering (PRIORART-18) — rather than
importing across top-level dirs, keeping the module stateless and
task-agnostic. The watcher's process model (one global-ridden process per run
— PRIORART-11) is not inherited: Touch's ingester is multi-session,
incremental (PRIORART-7) and deferred-classification (PRIORART-16) by design.

### D12. Testing posture (PRIORART-17, STACK-16 — decided once)

Stdlib-only, no pytest; each `tests/test_*.py` executable, exits non-zero on
failure; new `tests/run_all.sh` loops them (and the four existing monitoring
tests). The network layer gets **socket-level integration tests** (real
`asyncio.start_server` on port 0). Browser JS stays thin (renders
server-computed models) and is guarded by static source tests, same genre as
`test_frontend.py`. PTY behaviors get real-process tests (spawn `cat`/`bash`).

### D13. Honesty rules baked into the UI

- Every model-mediated verb shows `requested/sent/confirmed` distinctly; a
  requested-but-unconfirmed action is the *normal* failure mode (CONTROL-16).
- Fan-out siblings are labelled by `stage` first (`agentgraph · research #1`)
  (AGENTGRAPH-12, PRIORART-5).
- A run whose journal went quiet shows "no activity for N min", never a
  fabricated verdict, whenever a control action is on record (PRIORART-9).
- Archived tasks render from legacy events only, labelled "archived — source
  transcripts unavailable" (AGENTGRAPH-14).
- Queued agents are declared, not observed; the concurrency cap is
  CPU-derived (`min(16, max(2, cpus-2))`), so declared ≠ started is normal
  (AGENTGRAPH-5).

### D14. Considered and rejected (record; do not re-propose)

`--remote-control` (Anthropic cloud relay — LIVEIO-14); `--bg-pty-host` and
`/tmp/cc-daemon-*` sockets as build targets (private, version-coupled —
LIVEIO-4, CONTROL-14); `MessageDisplay` as transport (sync exec on the render
path — LIVEIO-8); SIGSTOP as pause (CONTROL-11); `CLAUDE_CODE_TERMINAL_RECORDING`
/ `.cast` (inert in 2.1.220 — SESSIONDATA-12); `CLAUDE_PTY_RECORD` (private
binary format — LIVEIO-20); editing `journal.jsonl` (breaks watcher + resume —
CONTROL-6); appending to harness transcripts (DRIVER §7.2); graph layout
libraries (STACK-10); node-pty / installing g++ (STACK-6); reading
`~/.claude.json` for liveness or cost (SESSIONDATA-18); `progress` records as
a data source (zero exist on disk — SESSIONDATA-16, optional enrichment only).

---

## Part C — Ordered implementation items

Notation: **Files** lists every file the item creates (new) or changes
(existing, with line anchors). Items are ordered by dependency; the divider
may parallelize only disjoint file sets.

---

### T1. Repo scaffolding, gitignore, test runner, version floor

**Files (new):** `aggregator/__init__.py`, `aggregator/util.py`,
`tests/run_all.sh`, `touch-visual/.gitkeep` (placeholder until T16);
**(changed):** `.gitignore` (additive only — preserve the two monitoring
lines asserted by `.claude/shared/monitoring/tests/test_shell.py:155`).
**Resolves:** STACK-17, STACK-16 (runner + floor), PRIORART-14 (documented invariant).
**Approach:** create `aggregator/`, `touch-visual/`, `tests/` at repo root.
`.gitignore` gains `.touch/` and `__pycache__/`. `aggregator/util.py` holds
shared primitives copied (with attribution comments, per D11) from prior art:
`tail_lines(path, checkpoint) -> (lines, checkpoint)` implementing
`(st_dev, st_ino, size, offset)` + torn-tail deferral + truncation sentinel
(semantics of `monitor_server.py:330-356` and `decision_watcher.py:470-491`);
`contained_path(base, rel, exts)` (from `monitor_server.py:199-212`);
`atomic_write_json` (from `decision_watcher.py:513-520`); a startup assert for
Python ≥ 3.11 with a clear message. `tests/run_all.sh` runs every
`tests/test_*.py` plus the four existing monitoring tests and fails on first
non-zero exit.
**Tests:** `tests/test_util.py` — torn tail deferred; shrink → sentinel; inode
swap → sentinel; containment rejects `..`, symlink escape, sibling-prefix dir;
atomic write survives a concurrent reader; the one-`write()`-per-record
concurrency proof (4 writers × 200 records, 0 corrupted — PRIORART-14).

### T2. Vendor xterm.js

**Files (new):** `touch-visual/vendor/xterm.js`, `touch-visual/vendor/xterm.css`,
`touch-visual/vendor/addon-fit.js`, `touch-visual/vendor/VERSIONS.txt`.
**Resolves:** STACK-8, STACK-9.
**Approach:** `npm pack`/install `@xterm/xterm` + `@xterm/addon-fit` in a temp
dir (installs verified working through the proxy), copy the UMD builds
(`lib/xterm.js` ≈489 KB, `css/xterm.css` ≈7 KB, fit addon) into
`touch-visual/vendor/`, record `package@version` + sha256 per file in
`VERSIONS.txt`, commit. No bundler, global `Terminal` via plain `<script>`.
**Tests:** extend `tests/test_touch_frontend.py` (T22): `VERSIONS.txt` sha256
entries match the files on disk; no `http(s)://` asset URL anywhere under
`touch-visual/`.

### T3. WebSocket codec

**Files (new):** `aggregator/ws.py`.
**Resolves:** STACK-4, PRIORART-1 (transport half); informs LIVEIO-3-style
input gating.
**Approach:** a full RFC-6455 codec as pure functions/classes over bytes —
client-frame **unmasking** (the prior art deletes payloads unread,
`monitor_server.py:279-310`), fragmentation/continuation reassembly, ping/pong,
close codes, text/binary opcodes, a max-message cap (64 KB control, 1 MB data),
plus the server-side accept-key handshake helper. No socket I/O in this module.
**Tests:** `tests/test_ws_codec.py` — masked client frame round-trip; a
fragmented 3-frame message; interleaved ping during fragmentation; oversized
frame rejected with close 1009; handshake accept-key vector from RFC 6455.

### T4. Server core: routing, auth, static, health

**Files (new):** `aggregator/server.py`, `aggregator/routes.py`;
**(reference only):** `monitor_server.py:225-241` (port pattern), `:199-212`
(containment), `:519-525` (bind/startup print).
**Resolves:** PRIORART-2, PRIORART-13, STACK-3, STACK-15, SESSIONDATA-15,
LIVEIO-16; PRIORART-12 (404 discipline).
**Approach:** one asyncio process. `(method, route) → handler` table with
default 404 and 405; HEAD support. Per-boot `secrets.token_urlsafe(32)`
printed once with the exact `sbx ports $SANDBOX_VM_ID --publish 8932:8932/tcp`
line and tokened URL; token checked with `hmac.compare_digest` on `/`,
`/static/*`, `/api/*`, `/ws`, `/pty` (only `/health` is open). Origin/Host
allowlist derived from bind host/port, enforced at WS upgrade; missing Origin
rejected on `/pty` and any control-capable socket. Static serving via
`contained_path` under `touch-visual/`, extension whitelist
(html/js/css/svg/txt). Bind `0.0.0.0`, port resolution
argv > `$TOUCH_PORT` > `.touch/server.json` > 8932. CSP + `nosniff` headers.
**Tests:** `tests/test_server_integration.py` — real server on port 0:
tokenless `/api/*` → 401; bad-Origin WS upgrade → 403 (the evil.example probe
that today gets 101); unknown route → 404; unknown session id → 404 not
fallback; `/health` open; static containment refuses `/static/../../etc`;
HEAD works.

### T5. Touch-owned store and event log

**Files (new):** `aggregator/store.py`.
**Resolves:** D4/D5 decisions, SESSIONDATA-13, PRIORART-6 (version field),
PRIORART-14, PRIORART-15, DRIVER §7.1/7.2/7.3.
**Approach:** implements the `.touch/` layout of D5 and the v2 record schema of
D3/D4. Single-writer append with `seq` (resumes from line count at boot);
`ref` union validation; reducers that fold an event stream into current
session/run/node state (pure functions, unit-testable); readers tolerate
missing `v` (=1) and unknown `kind`. Retention: Touch never deletes; the
store is the history the CLI sweep would otherwise destroy.
**Tests:** `tests/test_store.py` — seq continuity across reopen; reducer
idempotence under replay; unknown kinds preserved; ref validation rejects
malformed ids; v1 legacy record accepted.

### T6. Session discovery and liveness

**Files (new):** `aggregator/sessions.py`.
**Resolves:** SESSIONDATA-6, SESSIONDATA-7, SESSIONDATA-8, LIVEIO-12,
STACK-12, PRIORART-3, AGENTGRAPH-15.
**Approach:** 250 ms scan of `~/.claude/sessions/*.json`. Key `(pid,
procStart)`; liveness = `/proc/<pid>` exists AND stat field 22 ==
`procStart`; `status`/`updatedAt` displayed as "last known state", never as
liveness. Tolerate torn/zero-byte registry JSON (retry once, keep last good).
Re-resolve `sessionId` every tick; on rotation (`/clear`) close the old tail,
open the new file, emit a `session` event ("context cleared") so panes render
a divider instead of freezing. Ended = process gone → emit inferred
"ended — process gone" (a precise reason arrives only via the SessionEnd hook,
T10). Ghost files (dead pid) render as historical. Slow path: `claude agents
--json` with ≥10 s TTL cache for reconciliation + bg-agent state. Sidebar
payload: name, nameSource, cwd, kind, liveness, ownership class (from Touch's
spawn registry, T9), needs-attention badge (from hooks, T10).
**Tests:** `tests/test_sessions.py` — fixture registry dir + fake `/proc`
root injected: dead-pid ghost not live; pid-reuse (procStart mismatch) not
live; torn JSON keeps last good; sessionId rotation detected and evented;
ENOENT transcript = empty-session state.

### T7. Transcript ingestion

**Files (new):** `aggregator/transcript.py`.
**Resolves:** SESSIONDATA-1, SESSIONDATA-3, SESSIONDATA-4, SESSIONDATA-5,
SESSIONDATA-14, SESSIONDATA-19, SESSIONDATA-20, PRIORART-7 (incremental
design), conflict-resolution A2 (`tool-results` spill).
**Approach:** per-transcript tailer using `tail_lines` (T1) with the
checkpoint/rewrite rules of D6. Classification by the 4-bucket table:
`last-wins` types keep only the last occurrence (keyed `type:sessionId`,
`summary` by `type:leafUuid`); `boundary-cleared` kept but never rendered as
timeline events; only user/assistant/system/attachment are timeline. Upsert
by `uuid`. Group assistant records into turns by `message.id` (1:1 with
`requestId`); missing `is_error` = false; `stop_reason` + `system/turn_duration`
close turns. Token accounting: per-`message.id` map (fallback `uuid`, then
`path+lineno`), input = `input + cache_creation + cache_read` with r/w
breakout, monotonic clamps — semantics of `decision_watcher.py:154-197`.
Handle `toolUseResult.persistedOutputPath`/`persistedOutputSize` +
`<persisted-output>` placeholder: store the pointer, expose the content
through a typed endpoint that validates containment under the CLI's
`tool-results` dir (read-only). Extract diffs from `structuredPatch`
(primary) with `file-history/` as optional enrichment keyed to the record's
own sessionId (SESSIONDATA-19). Surface the prompt queue from the live tail
only (SESSIONDATA-20). Emit v2 events into the session store.
**Tests:** `tests/test_transcript.py` — synthetic transcripts: split usage
over 3 records counts once (the 2.09x case); re-ingest after simulated
`performRemoveByUuid` shrink converges to identical state; 20×`mode` records
collapse to one state value; torn 46 KB line deferred then completed;
persisted-output pointer record produces a pointer, not inline content; turn
grouping renders one turn for text+2×tool_use sharing a `message.id`.

### T8. Workflow runs and the agent graph model

**Files (new):** `aggregator/graph.py`;
**(reference only):** `decision_watcher.py:76-100` (rotation glob),
`:305-327` (completion timestamp rule), `:370-447` (verdict mapping).
**Resolves:** AGENTGRAPH-1..12, AGENTGRAPH-14..17, SESSIONDATA-9,
SESSIONDATA-10, SESSIONDATA-11, LIVEIO-13, LIVEIO-19, PRIORART-4, PRIORART-5,
PRIORART-10.
**Approach:** run attach happens **only** when T7 sees a `Workflow`
toolUseResult (`runId, taskId, workflowName, summary, scriptPath,
transcriptDir` — persist all six; scriptPath is the restart pointer). Journal
tailed as an unordered fact set `(key→agentId*, key→result)`; node identity
`(runId, key, ordinal)`; second `started` on a key supersedes the prior
ordinal (attempt badge, tokens rolled up); empty `agentId` on result rows
tolerated. Resume detection: `results` already populated at attach ⇒ those
nodes marked "replayed from journal (not re-executed)", greyed. All timing
from agent transcripts (first/last parseable timestamp; glob across session
dirs for `/clear` splits, oldest-first for prompt extraction —
AGENTGRAPH-7); when a `result` lands live and the transcript tail is stale
>30 s, the read moment is the completion timestamp. Labels: parse
`^\[monitor\] plan=… stage=… role=… attempt=…` from the oldest transcript's
first user record; fallback `agentType` + first 60 chars of prompt; display
`stage · role #attempt`. Edges: (1) parent `tool_use.id` ↔ `meta.toolUseId`
(Agent tool), (2) `subagents/workflows/<runId>/` containment, (3)
`meta.parentAgentId` (depth ≥2); `spawnDepth` is a hint only. Ordinary
session subagents (no journal, no marker) are first-class nodes via source
(1) — visibility is not marker-gated (PRIORART-4). Declared nodes come from
seeded legacy `queued` events or a parsed fan-out and stay visually
"declared". Three-state liveness per D6. Tokens per D3; `totalDurationMs`,
`toolStats`, `resolvedModel` trusted from `toolUseResult`. When
`<sessionId>/workflows/<runId>.json` appears, copy to
`.touch/runs/<runId>/snapshot.json` and back-fill authoritative
label/phase/index/attempt/durations/status as optional late fields.
**Tests:** `tests/test_graph.py` — fixtures: 6-way fan-out labelled uniquely
by stage (the "research #1"×6 collapse must not reproduce); duplicate
`started` per key yields one node with ordinal 2 and a superseded attempt;
resume journal with pre-populated results marks replayed nodes; killed run
(started, no result, quiet) reports `unknown/stalled` with idle duration —
never `finished`; journal order shuffled vs transcript timestamps orders by
timestamp; node with no marker gets fallback label; `totalTokens` never read.

### T9. PTY host and owned-session spawner

**Files (new):** `aggregator/ptyhost.py`, `aggregator/spawn.py`.
**Resolves:** LIVEIO-1/-2 (decision executed with own PTY), LIVEIO-20,
STACK-2, STACK-5, STACK-6, STACK-7, STACK-13 (PTY half), CONTROL-12, D10.
**Approach:** `pty.openpty()` + `subprocess.Popen(argv, stdin=slave,
stdout=slave, stderr=slave, start_new_session=True)`;
`os.set_blocking(master, False)` + `loop.add_reader` (no polling); reads
coalesced ≤16 KB / ≤60 fps. Env allowlist + `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`
+ explicit `--session-id` per D10; spawn record into
`.touch/sessions/<pid>-<procStart>/meta.json` (this registry defines the
"owned" class). Server-side 256 KB scrollback ring, replayed on connect then
`{"t":"live"}` marker (the module's replay-then-live contract); every byte
also appended to `pty.log` with a `pty.idx` line per flush so closed
terminals replay beyond the ring. Resize via
`fcntl.ioctl(master, TIOCSWINSZ, …)`. Terminate ladder per D7 with deadlines
and `waitpid` reaping; transcript-appears self-check with UI banner on
failure.
**Tests:** `tests/test_ptyhost.py` — spawn `cat`: echo round-trip; resize
observed via `stty size`; spawn `bash -i`: SIGTERM survives (documented),
ladder (`exit\r` → SIGHUP → SIGKILL) reaps within deadline; ring replay
returns last N bytes; pty.log + idx grow consistently; env allowlist strips
`CLAUDE_CODE_CHILD_SESSION` and injects the force-persistence flag (assert on
the constructed env dict, no claude binary needed).

### T10. Hook pack (opt-in push channel)

**Files (new):** `aggregator/hooks.py`, `aggregator/hookpack/touch-hook.sh`,
`aggregator/hookpack/settings-template.json`.
**Resolves:** SESSIONDATA-8, SESSIONDATA-17, LIVEIO-6, LIVEIO-7, LIVEIO-10,
LIVEIO-17, CONTROL-10.
**Approach:** `touch-hook.sh` is append-one-line-and-exit: reads the hook JSON
from stdin, `printf '%s\n'` of one compact line into
`.touch/hooks/<session_id>.jsonl` (single `write()`, no network, no Python
startup), exits 0 always. Registered events: `SessionStart, SessionEnd,
UserPromptSubmit, SubagentStart, SubagentStop, Stop, Notification,
PreToolUse, PostToolUse, TaskCreated, TaskCompleted`; every entry carries
explicit `"timeout": 5`. The settings template is **static** (matcher +
command only, all policy server-side — CONTROL-10) and is passed via
`--settings` at spawn for owned sessions; installing into a project's
`.claude/settings.json` for foreign sessions is a separate, explicit,
reversible user action (documented, not automatic). `aggregator/hooks.py`
tails the spools, assigns seq into the canonical stream, and joins by
`session_id`/`agent_id`/`tool_use_id` (enrich-don't-duplicate per D3).
Hook self-heartbeat: the ingester emits "no hook line for N s while session
busy" as a health event (hook failures are invisible in PTY mode —
LIVEIO-17). `SubagentStart/Stop` (+70 ms, carries `agent_transcript_path`)
becomes the preferred node-creation trigger, upgraded by the marker when the
transcript is readable (LIVEIO-13). `Notification`
(`permission_prompt`/`agent_needs_input`) drives the sidebar
"needs you" badge. `SessionEnd.reason` supplies the precise end cause.
Matcher trap encoded: the spawn tool matcher is `Agent`, not `Task`.
**Tests:** `tests/test_hooks.py` — spool line is one write and parseable;
ingester joins a `PreToolUse`+`PostToolUse` pair to one tool entity by
`tool_use_id`; hook event for an agent already known from the journal
enriches, not duplicates; heartbeat event after silence; settings template
declares timeout 5 on every entry and matches `Agent`.

### T11. Typed read API

**Files (new):** `aggregator/api.py`.
**Resolves:** STACK-11, PRIORART-3 (payload shape), SESSIONDATA-15 (route
discipline), AGENTGRAPH-13 (full ids in payloads).
**Approach:** JSON endpoints, all tokened, all id-validated: `/api/sessions`
(sidebar list per T6); `/api/session/<uuid>/timeline?since=<seq>` (turn
objects from T7); `/api/session/<uuid>/queue`; `/api/runs/<runId>/graph`
(nodes+edges+states from T8, full agentIds + transcript paths);
`/api/runs/<runId>/node/<agentId>` (detail incl. dedup token rollup);
`/api/toolresult/<tool_use_id>` (inline or persisted-output content,
contained); `/api/archive` (T20). Responses carry `seq` cursors for
incremental pulls. No endpoint accepts a filesystem path.
**Tests:** covered in `tests/test_server_integration.py` (T4) — id regex
rejection (`../`, non-UUID, short agentId), 404 on unknown ids, cursor
semantics (`since` returns only newer), denylist files unreachable via any
route.

### T12. Live event WebSocket

**Files (new):** `aggregator/wschannel.py`.
**Resolves:** PRIORART-8, PRIORART-12 (WS half), STACK-13 (events half),
PRIORART-6 (graph painted by events).
**Approach:** `/ws?session=…` or `?run=…` + token: replay from
`?after=<seq>` (default 0) in **batched multi-event frames**, explicit
`{"kind":"replay-end","seq":N}` marker, then live tail at 250–500 ms. The
client keeps its cursor and accumulators across reconnects; events are
ordered and idempotent by seq (the invariant replacing full-replay safety).
Server drops slow consumers (writableLength-style bound) and the client
reconnects with its cursor. 404-equivalent close code for unknown ids.
**Tests:** integration (T4 file): connect-replay-live sequence; reconnect
with `after` receives no duplicates; batch frames parse; unknown id closes
with the defined code; slow-consumer disconnect path.

### T13. PTY WebSocket channel

**Files (new):** `aggregator/ptychannel.py`.
**Resolves:** STACK-5 (latency), LIVEIO-3 (as design input for our own
bridge), STACK-13 (multi-viewer), D9.5.
**Approach:** `/pty?session=…&role=viewer|driver` + token + Origin check.
On connect: scrollback-ring replay, `{"t":"live"}`, then event-driven frames
from `loop.add_reader`. Input frames accepted only from the single driver
connection (explicit takeover endpoint swaps the driver); resize frames
driver-only; kill/terminate not on this channel at all (it lives in the
control API, T14, with confirm). Binary WS opcode for PTY bytes; JSON text
frames for control markers.
**Tests:** integration — viewer cannot inject (input frame ignored + logged);
driver echo round-trip against a `cat` session; second driver rejected until
takeover; replay-then-live ordering; token/Origin enforced (reuses T4
security harness).

### T14. Control plane: start / terminate / stop-loop / restart

**Files (new):** `aggregator/control.py`.
**Resolves:** CONTROL-1, CONTROL-3 (decision recorded), CONTROL-5, CONTROL-6,
CONTROL-7, CONTROL-13, CONTROL-16, CONTROL-17 (table shipped), STACK-7,
STACK-18, PRIORART-9, AGENTGRAPH-16, LIVEIO-9 (verbs half).
**Approach:** intents API (`POST /api/control`) writing
`.touch/control.jsonl` (audit: ts, actor, verb, target
`(session|runId|key|agentId)`, state, detail). Verbs per D7:
**start** = spawn via T9 (optional initial prompt/skill invocation typed into
the PTY); **terminate session** = T9 ladder, after which Touch itself writes
the terminal run/session events (the harness won't — CONTROL-13);
**stop loop** = typed `TaskStop({taskId})` instruction, **restart loop** =
typed `Workflow({scriptPath, resumeFromRunId})` instruction — both queued
until the session registry shows `status:"idle"`, sent as one PTY write,
then confirmed only by observed evidence (new Workflow tool_result / journal
lines / `task_updated`), with visible timeout → `expired`. Before any
stop/restart affecting an implement loop, record a tree checkpoint
(`git stash create` sha + `git status --porcelain` scoped to the sub-plan's
owned files) and expose "restore checkpoint" as a separate explicit action
(CONTROL-7). Foreign sessions: every control 403s server-side (not just
hidden in UI). The derived-state reducer treats a recorded control action as
overriding quiet-timeout inference (PRIORART-9). Journal files are never
edited (CONTROL-6).
**Tests:** `tests/test_control.py` — audit line per state transition; gating
holds an intent while fixture registry says busy and releases on idle;
confirmation only flips on observed evidence (fake journal append), else
expires; foreign-session control rejected; checkpoint recorded before
restart; reducer: control-stopped run never reported "completed".

### T15. Pause gate (the only honest pause)

**Files (new):** `aggregator/pausegate.py`;
**(changed):** `aggregator/hookpack/settings-template.json` (adds the gate
hook entry), `aggregator/control.py` (pause/resume/step verbs).
**Resolves:** LIVEIO-9, CONTROL-8, CONTROL-9, CONTROL-17.1/.2.
**Approach:** an HTTP hook endpoint (`POST /hook/gate`) registered as
`PreToolUse` (and `SubagentStart`) in the hook pack for owned sessions. A
per-`agent_id` gate table (set via control verbs) makes the handler **hold
the response** in ≤120 s slices (respond `allow`, re-gate on the next call;
never approach the 600 s ceiling; never `deny` — the model must not see
errors). Step = release exactly one call. UI states: "pause requested →
effective at next tool boundary → paused (held N s)"; a forgotten pause
auto-expires to resumed with an audit line. Works per-agent while siblings
run — the only mechanism that does (CONTROL-8). Documented limits in the UI:
an agent that never calls another tool cannot be paused.
**Tests:** `tests/test_control.py` additions — gate holds a fake hook POST
until released; slice release+re-gate produces exactly one tool execution
path (no deny ever sent); step releases one; expiry resumes and audits;
per-agent isolation (agent B's POST passes while A is held).

### T16. Frontend shell, routing, sidebar

**Files (new):** `touch-visual/index.html`, `touch-visual/app.js`,
`touch-visual/style.css`.
**Resolves:** PRIORART-3 (UI half), README sidebar; D1 class split surfaced;
PRIORART-18 (URL-as-state routing, escape-first discipline).
**Approach:** single page, query-string routing
(`?session=<uuid>` / `?run=<wf…>` / `?view=archive`), token carried from the
initial URL into WS/API calls. Sidebar from `/api/sessions`: name, cwd,
kind, liveness dot (derived server-side), ownership chip
(owned / read-only), "needs you" badge (hook notifications). Escape-first
rendering everywhere (`textContent`/`createElement`; no interpolated
`innerHTML`), whitelisted class names. Panels: terminal (T17, owned only),
transcript (T18), graph (T19). Log/DOM ring buffers — nothing unbounded
(PRIORART-8 client half).
**Tests:** `tests/test_touch_frontend.py` (static guards): no `innerHTML`
with interpolation; no external URL; token never written into
`localStorage`; ring-buffer constant present; the owned/observed chip
rendered from a server field, not inferred client-side.

### T17. Terminal page

**Files (new):** `touch-visual/terminal.js`.
**Resolves:** SESSIONDATA-12 (option (a)+(b) split executed), STACK-9,
README "terminal-styled main page".
**Approach:** xterm.js + fit addon against `/pty`. Renders replay, then
live; viewer/driver role switch in UI; `term.onData` → input frames
(driver only), `term.onResize` → resize frames. Terminal identification
queries (DA1 etc.) are answered by xterm.js itself. Exit marker renders an
"ended" overlay; closed sessions replay from `pty.log` via a ranged API.
**Tests:** static guards (T16 file): input send is gated on driver role;
no ANSI parsing hand-rolled in our code.

### T18. Semantic transcript view

**Files (new):** `touch-visual/transcript.js`.
**Resolves:** SESSIONDATA-2 (no fake thinking pane), SESSIONDATA-14,
SESSIONDATA-19, SESSIONDATA-20, LIVEIO-11 (role of transcript view).
**Approach:** renders T7 turn objects: prompt bubbles (incl. slash/bash-mode
forms), assistant text, tool cards (full input, full result, is_error,
duration), "thought for N s" collapsed markers, turn dividers with
`durationMs`, per-turn tokens, diff cards from `structuredPatch`,
persisted-output cards fetch on demand, queued-prompt strip, "context
cleared" divider on session rotation. Labeled "transcript view — not a
terminal" for observed sessions.
**Tests:** static guards: thinking pane absent; escape-first; pointer-result
card fetches via `/api/toolresult/` only.

### T19. Graph page (n8n-style)

**Files (new):** `touch-visual/graph.js`.
**Resolves:** STACK-10, AGENTGRAPH-17 (visual contract), AGENTGRAPH-2/-5/-12
(labels/declared), PRIORART-5, PRIORART-6 (graph from model, painted by
events), LIVEIO-9 (pending-state rendering), D13.
**Approach:** hand-rolled layered SVG (~200 lines): rank = phase, y = index
in rank, bezier edges, one back-edge style for retry loops. Stable node ids
(`runId:key:ordinal`) so live updates patch attributes — never re-layout on
paint. Visual grammar: solid = harness-derived, dashed = convention-derived,
outlined = declared; three-state liveness colors + idle-duration text;
attempt badges; "replayed from journal" grey; control buttons render intent
state (`requested/sent/confirmed/expired`) inline on the node/run they
target. Loops (impl→test→critique) drawn from marker plan/stage per the two
skill templates' known shapes (`research.workflow.js:136-153`,
`implement.workflow.js:163-210,255-349`).
**Tests:** static guards: NODE-STATE class whitelist; stable-id pattern
present; no layout library reference; plus `tests/test_graph.py` asserts the
server model contains everything the page needs (no client-side derivation
of harness facts).

### T20. Archived runs (legacy events.jsonl)

**Files (new):** `aggregator/legacy.py`;
**(reference only):** `.claude/local-orchestrators/*/orch-config.json`,
`monitoring.md` schema.
**Resolves:** AGENTGRAPH-14, PRIORART-12 (unknown → 404), PRIORART-15
(magic names interpreted on read), D4 (legacy as read-only source).
**Approach:** `/api/archive` lists task folders under
`.claude/local-orchestrators/`; each renders from its `events.jsonl` only,
mapped into v2 events (`source:"legacy"`, magic plan/stage names interpreted
into `kind` at read time), labelled "archived — source transcripts
unavailable". A missing/foreign `wf_dir` in `orch-config.json` is displayed,
never globbed for. Nothing under the task folders is ever written or deleted
(CLAUDE.md rule).
**Tests:** `tests/test_legacy.py` — omnigent-style fixture: renders without
any `~/.claude` data; dead `wf_dir` does not attach the live journal; magic
names map to kinds; unknown archive name 404s.

### T21. Additive fixes to the shared monitoring module

**Files (changed):** `.claude/shared/monitoring/decision_watcher.py:636-638`
and `:682-688` (labels), `:138-152` (`emit`),
`.claude/shared/monitoring/status.sh:34-40`,
`.claude/shared/monitoring/monitor.html:287` (flow key),
`.claude/shared/monitoring/monitoring.md` (schema note),
`.claude/shared/monitoring/tests/test_watcher.py`,
`tests` counterparts as needed.
**Resolves:** PRIORART-5, PRIORART-6 (v field), AGENTGRAPH-12, AGENTGRAPH-13,
PRIORART-14 (documented invariant).
**Approach:** strictly additive, old history must still render: label becomes
`f"{stage}:{role} #{attempt}"` when stage exists; `agent` sub-object gains
`stage`, `role`, `attempt`, `agent_id` (full id; short `id` kept for
compatibility); every emitted event gains `"v": 2`; `monitor.html` flow key
uses `agent.stage||role` so fan-outs stop collapsing; `monitoring.md`
documents the one-`write()`-per-record invariant and the `v` field. Do not
touch tailing, checkpoints, or the journal reader. Must not run while a live
orchestration is mid-run.
**Tests:** existing four monitoring tests still pass; `test_watcher.py`
gains: fan-out of two stages yields distinct labels and stage fields; events
carry `v:2`; a v1 event line still renders (reader tolerance).

### T22. Test suite completion and security regression file

**Files (new):** `tests/test_touch_frontend.py`, plus any test files from
T1–T21 not yet created; **(changed):** `tests/run_all.sh` (final list).
**Resolves:** PRIORART-17, STACK-16, D12.
**Approach:** consolidate: every aggregator module has a unit test file; the
integration file covers the full auth matrix (token × Origin × route class ×
method) as regression tests for D9; frontend static guards per T16–T19;
`run_all.sh` runs everything including the four monitoring tests, exits
non-zero on first failure.
**Tests:** this item is tests; acceptance = `bash tests/run_all.sh` green.

### T23. Documentation and honest-semantics surfaces

**Files (new):** `README.md` expansion (root), `docs/control-semantics.md`;
**(changed):** `CLAUDE.md` (commands section: how to run Touch, port 8932,
publish line with token; test runner).
**Resolves:** CONTROL-17 (published table), D14 (rejected alternatives
recorded), LIVEIO-16 (publish instructions), SESSIONDATA-15 (token in
docs), driver §6.3 (honest verb definitions).
**Approach:** README gains: architecture (aggregator/touch-visual), the
owned/observed model, the D7 control table verbatim (what each verb honestly
does to in-flight work), the D14 rejected list with one-line reasons, run
instructions (`python3 aggregator/server.py`, `sbx ports … 8932`, token
usage), and the data-honesty grammar (solid/dashed/declared). CLAUDE.md
updated per its own standing instruction to keep commands current.
**Tests:** `tests/test_touch_frontend.py` static guard: control table rows
for all four verbs present in `docs/control-semantics.md`; publish line
mentions 8932 and the token.

---

## Part D — Merged/discarded findings register

**Merged (same item, ids kept as aliases):**
no-attach = LIVEIO-1 ≡ CONTROL-2 ≡ STACK-1 (→ D1);
token dedup = SESSIONDATA-1 ≡ AGENTGRAPH-8 (→ T7/T8);
registry staleness = SESSIONDATA-7 ≡ LIVEIO-12 ≡ STACK-12 (→ T6);
marker-only labels = SESSIONDATA-11 ≡ AGENTGRAPH-2 ≡ LIVEIO-13 (→ T8);
journal timestamps = SESSIONDATA-10 ≡ AGENTGRAPH-1 (→ T8);
async-run invisibility = SESSIONDATA-9 ≡ AGENTGRAPH-11 (→ T8);
fan-out collapse = AGENTGRAPH-12 ≡ PRIORART-5 (→ T8/T21);
resume replay = AGENTGRAPH-4 ≡ PRIORART-10 ≡ CONTROL-5 (→ T8/T14);
no-auth-0.0.0.0 = PRIORART-2 ≡ STACK-3 ≡ SESSIONDATA-15 ≡ LIVEIO-3 (→ T4);
one-way transport = PRIORART-1 ≡ STACK-4 (→ T3/T12);
pause-is-kill = LIVEIO-9 ≡ CONTROL-4 ≡ STACK-7(pause) ≡ STACK-18 (→ D7/T15);
hooks-as-push = SESSIONDATA-17 ≡ LIVEIO-6 ≡ CONTROL-8 (→ T10/T15);
stops-invisible = AGENTGRAPH-16 ≡ PRIORART-9 (→ T14).

**Discarded as work items (decision recorded instead):**
- AGENTGRAPH-13 in the *legacy* stream: moot for Touch (reads harness files
  directly); fixed additively in T21 anyway.
- PRIORART-7/-8/-11/-16 as *watcher fixes*: the monitoring module stays at its
  own scale (16 ms/tick measured); Touch's ingester avoids each by design
  (T7/T12); no watcher refactor ships.
- LIVEIO-15 (bg-jobs plane): deferred to v2 — `kind:bg` sessions appear
  read-only via the registry; `~/.claude/jobs/` ingestion adds a second
  private-layout coupling for no v1 requirement.
- LIVEIO-8 (MessageDisplay), CONTROL-11 (SIGSTOP), LIVEIO-14
  (remote control), CONTROL-14/LIVEIO-4 (`bg-pty-host`/daemon sockets),
  CONTROL-15 (unreachable TUI verbs), SESSIONDATA-16 (progress records),
  SESSIONDATA-18 (`~/.claude.json`), LIVEIO-20 (`CLAUDE_PTY_RECORD`),
  SESSIONDATA-12's `.cast` recorder: all rejection records in D14/D7 —
  nothing to build.
- STACK-6's "apt install g++" branch: rejected; Python PTY chosen.

---

## Part E — UNVERIFIED items and the cheapest settling experiments

1. **`tool-results` spill trigger + exact threshold.** Writer confirmed in
   the binary; never observed on disk. Experiment: probe session runs a Bash
   command emitting >64 KB; inspect the transcript for
   `persistedOutputPath` and locate the dir (`xke()`); T7 handles both
   outcomes regardless.
2. **Hook hot-reload for already-running sessions** (CONTROL-10). Experiment:
   start a probe session, add a `PreToolUse` hook to project settings, invoke
   a tool; fires or not decides whether foreign-session hook install is ever
   worth documenting. v1 assumes NO (hooks only at spawn).
3. **`Notification` hook under non-bypass permission modes** (the "needs you"
   badge path). Experiment: probe session with `--permission-mode default`
   triggers a permissioned tool; observe hook spool.
4. **Gate-hook slice release/re-gate loop** (T15's ≤120 s slices; the
   hold-was-verified, the slicing loop is designed). Experiment: PreToolUse
   HTTP hook holds 3×10 s slices; assert exactly one tool execution and no
   duplicate `tool_use`.
5. **`CLAUDE_CODE_TERMINAL_RECORDING`** inert in 2.1.220 (three probes).
   Re-probe once per CLI upgrade; not planned around.
6. **`messagingSocketPath` for interactive sessions** — emitted `undefined`
   in 2.1.220. Re-grep per upgrade; would unlock foreign-session control if
   it ever ships.
7. **`journal.agentId` ↔ `agentControllers` key identity** (DRIVER §4) —
   irrelevant while no transport reaches skip/retry (CONTROL-15); settle only
   if a control_request subtype for it appears.
8. **Owned-session registry behavior**: does a Touch-spawned interactive
   `claude` under a PTY write `sessions/<pid>.json` with `kind:"interactive"`
   promptly? Expected yes; asserted by T9's self-check on first real spawn.
9. **`~/.claude/todos/` format** — dir empty on this machine; not needed v1.
10. **Long-SIGSTOP TLS survival** — moot (SIGSTOP rejected), do not test.

---

## Part F — Acceptance shape

v1 is done when: `python3 aggregator/server.py` prints one tokened URL; the
sidebar lists this machine's real sessions with honest liveness and class
chips; an owned session spawned from the UI shows a byte-faithful terminal
with sub-frame echo; the touch-aggregator research run of 2026-07-25 renders
as a graph with six *distinctly labelled* researcher nodes, correct token
rollups (deduped), three-state liveness, and solid/dashed provenance;
terminate/stop/restart flow through recorded intents with observed
confirmations; `bash tests/run_all.sh` is green including the four
pre-existing monitoring tests.
