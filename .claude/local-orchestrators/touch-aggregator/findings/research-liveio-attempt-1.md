# research-liveio — attempt 1

Perspective: **THE LIVE PATH** — how a browser can show/drive what a Claude Code
session is doing *right now*.

Everything below was verified against this machine: CLI `2.1.220`
(`/home/agent/.local/share/claude/versions/2.1.220`), the live session
`pid 622 / dd469822-2546-47d9-aaa3-31db4cb705e8`, and throwaway experiments under
`/tmp/claude-1000/liveio`. Where I quote binary internals I give the exact
`grep -a` that produces them. No file in the repo was modified.

## Summary of the channel inventory (measured)

| Channel | Granularity | Latency | Push? | Drives input? | Verdict |
|---|---|---|---|---|---|
| PTY host unix socket (`--bg-pty-host`) | raw ANSI bytes | immediate | yes | yes (keystrokes, resize, kill) | **the terminal page** |
| SDK stream-json (`-p --input/--output-format stream-json`) | token deltas + structured events | immediate | yes | yes (+ `control_request`) | **the structured/graph page** |
| Hooks (31 events) | per lifecycle event | immediate, **blocking** | yes | only via decisions (allow/deny/block) | **the control gate** |
| Transcript `.jsonl` tail | one message | 1–5 s bursts | no (poll) | no | history / reconciliation |
| `~/.claude/jobs/<id>/state.json` + `timeline.jsonl` | loop state | ~seconds | no (poll) | no | bg-agent status |
| `~/.claude/sessions/<pid>.json` / `claude agents --json` | session list | **no heartbeat** | no | no | sidebar list only |
| Remote Control (`--remote-control`) | everything | network | yes | yes | cloud relay — reject |

---

## LIVEIO-1 — There is no live channel into an *already running* interactive session; Touch must own the process

**Severity: blocker**
**Proof:**
```
ls -la /proc/622/fd            # 0,1,2 -> /dev/pts/0 ; no unix socket
find / -type s 2>/dev/null     # at 03:00, before I started anything: only
                               # /run/docker.sock and /run/ssh-agent.sock
cat /home/agent/.claude/daemon/roster.json   # lists ONLY daemon workers
```
`/tmp/cc-daemon-1000/` did not exist until I ran `claude --bg` at 03:03.

**Scenario.** README.md:3-4 says "main terminal is web view over claude code
session … left sidebar shows such terminal sessions list". Read literally, a user
starts `claude` in their own terminal and expects Touch to mirror it. That is
impossible: the process holds `/dev/pts/0` directly, nothing tees it, no IPC
socket is created, and the daemon roster never learns about it. Reading
`/dev/pts/0` from another process would *steal* the user's keystrokes, not
duplicate them.

**Recommendation.** Make the ownership model explicit in the product: **Touch
starts the sessions.** A session listed in the sidebar is one Touch spawned (or
one started as a background agent, LIVEIO-15). Sessions the user started by hand
in a foreign terminal can appear read-only at best (transcript tail, LIVEIO-11)
and must be visually marked "not attached". Do not promise a terminal view for
them.

---

## LIVEIO-2 — A real PTY multiplexer exists and is directly invocable: this is the terminal page

**Severity: major (primary architecture decision)**
**Proof — command that produced it:**
```
ps -o args -p 7066
  claude bg-pty-host --bg-pty-host /tmp/cc-daemon-1000/939665dd/pty/8084340e.sock 200 50 \
    -- /home/agent/.local/share/claude/versions/2.1.220 --session-id … --model haiku …
grep -aoE '.{0,60}Bun\.Terminal.{0,2500}' /home/agent/.local/share/claude/versions/2.1.220
grep -aoE 'function Gnn\(.{0,300}|function uwo\(.{0,700}' …2.1.220
```
Wire format, read out of `uwo`/`Gnn`/`x4`:
```
frame = uint32BE payloadLen | uint8 kind | payload
kind 0 = raw PTY bytes      kind 1 = JSON control object
P1t=0  Unn=1  ring=262144 (jnn)  header=5 (D1t)  maxFrame=1048576 (Odr)  maxCols/Rows=10000 (VBe)
```
Control objects seen/handled: server→client `{"t":"hello","replPid":N,"version":"2.1.220"}`,
`{"t":"live"}`, `{"t":"ping"}`, `{"t":"auth-required"}`, `{"t":"exit","code":…,"signal":…}`;
client→server `{"t":"auth","token":…}`, `{"t":"pong"}`, `{"t":"resize","cols":…,"rows":…}`,
`{"t":"kill","sig":"SIGTERM"|"SIGKILL"}` (SIGTERM auto-escalates to SIGKILL after 5 s).

**I ran it standalone, twice.** Wrapping `/bin/bash`:
```
CLAUDE_BG_PTY_AUTH=secret123 /home/agent/.local/share/claude/versions/2.1.220 \
  --bg-pty-host /tmp/claude-1000/liveio/t.sock 100 30 -- /bin/bash -i
→ CTRL {"t":"hello","replPid":7865,...}
→ DATA b'bash: no job control…\x1b[?2004h\x1b[01;32magent@claude-touch…'
→ CTRL {"t":"live"}  CTRL {"t":"ping"}
→ (after writing kind-0 frame b"echo TOUCH_PTY_WORKS\n")
  DATA b'echo TOUCH_PTY_WORKS\r\n…TOUCH_PTY_WORKS\r\n…$ '
```
And wrapping a **real interactive Claude REPL** (`--bg-pty-host … -- <binary>
--settings … --model haiku --permission-mode bypassPermissions`), typing
`"Count from 1 to 12 in words…\r"` into the socket: 36 data frames / 4599 bytes,
first frame at t+0.00 s, output contained `twelve`. That is the whole product
requirement of README.md:3 ("main terminal is web view over claude code session")
satisfied by one unix socket.

Semantics that matter for the UI: on connect the host replays the **entire
256 KB scrollback ring** and then sends `{"t":"live"}` — so a browser tab that
opens late gets correct history and a precise "you are now live" boundary, and
multiple browsers can watch the same terminal (`p` is a `Set` of subscribers).
Slow subscribers are dropped, not backpressured: `if(G.writableLength>X6_){G.destroy()}`.

**Recommendation.** Build the terminal page on this: Touch spawns
`<versioned-binary> --bg-pty-host <sock> <cols> <rows> -- <versioned-binary> <claude args>`
per session, bridges the socket to a WebSocket, and renders with xterm.js.
Replay-then-`live` maps directly onto the monitoring module's existing
"full replay on connect, then live tail" contract
(`.claude/shared/monitoring/monitoring.md:19`), so the mental model is already in
the repo. Handle `writableLength` drops by reconnecting (and re-replaying), and
treat `{"t":"exit"}` as the session-ended event for the sidebar.

---

## LIVEIO-3 — The PTY socket streams output to *anyone* who connects; only input is token-gated

**Severity: major (security)**
**Proof:** `/tmp/claude-1000/liveio/ptytest2.py`, run against a live socket:
```
NOAUTH ctrl frames: [hello, live, ping, {"t":"auth-required"}]   # ← full scrollback
                                                                  #   arrived before this
AUTH  (token from roster.json ptyAuth) → input echoed back
```
`auth-required` is only emitted **after** the client tries to write a DATA frame;
`hello` + scrollback + live data are sent unconditionally. The binary also
contains `tokens-file unreadable; DATA gate fail-open`, i.e. a missing token file
disables the input gate entirely rather than closing it.

**Scenario.** Touch bridges this socket to a WebSocket on `0.0.0.0:<port>`
(mandatory in this sandbox — CLAUDE.md:112-114). Anyone who can reach that port
now has (a) the full transcript of the user's session rendered as text, and (b)
if Touch forwards keystrokes, an unauthenticated shell inside the sandbox with
`--dangerously-skip-permissions` on. The published-port model means "reachable"
is not just localhost.

**Recommendation.** Non-negotiables for the bridge: create the socket in a
`0700` directory Touch owns; always set `CLAUDE_BG_PTY_AUTH` to a fresh random
secret per session and keep it server-side (never send it to the browser);
require a Touch-issued session token on the WebSocket **before** relaying any
frame in either direction; make input relaying opt-in per connection so a
read-only viewer cannot type. Also decide explicitly whether the browser may
send `{"t":"kill"}` — that is a remote process kill.

---

## LIVEIO-4 — The PTY channel is a private, version-coupled interface

**Severity: major**
**Proof:** `--bg-pty-host` is absent from `claude --help` (full output read);
the wrapper `/home/agent/.local/bin/claude` rejects it —
`error: unknown option '--bg-pty-host'` — you must exec the versioned binary
`/home/agent/.local/share/claude/versions/2.1.220` directly. The frame
constants (`P1t/Unn/D1t/Odr/jnn`) and the argv shape
(`bad argv: --bg-pty-host <sock> <cols> <rows> -- <file> [args...]`) exist only
inside that binary. `~/.local/share/claude/versions/` already holds three
versions (2.1.141, 2.1.162, 2.1.220) — the CLI self-updates.

**Scenario.** `claude update` lands 2.1.221, the framing or flag changes, and
every terminal in Touch goes blank with no error the user can act on.

**Recommendation.** (1) Resolve the binary through the versions dir at spawn
time and record the version with the session; (2) capability-probe at startup —
spawn a throwaway `--bg-pty-host … -- /bin/echo probe` and require
`{"t":"hello","version":…}` within a timeout, and refuse to advertise the
terminal feature if the probe fails; (3) keep LIVEIO-5 (SDK stream-json, a
documented flag surface) as the declared fallback renderer so a CLI upgrade
degrades the terminal to a structured transcript rather than to nothing;
(4) pin the tested version in the repo and make the probe a test.

---

## LIVEIO-5 — The supported live channel is SDK streaming; it also carries a real control protocol

**Severity: major (second architecture decision)**
**Proof:** measured run,
`/tmp/claude-1000/liveio/stream.jsonl` (55 lines) produced by
```
claude -p --output-format stream-json --verbose --include-partial-messages \
       --include-hook-events --permission-mode bypassPermissions
```
Observed message shapes: `system/init` (full tool list, session_id),
`system/status` (`requesting`), `stream_event/{message_start,content_block_start,
content_block_delta,content_block_stop,message_delta,message_stop}` (token-level
`thinking_delta` / `text_delta`), `system/thinking_tokens`
(`estimated_tokens_delta`), `assistant`, `user` (tool_result + `tool_use_result`),
`rate_limit_event` (`five_hour` window + `resetsAt`), `system/hook_started`,
`system/hook_response`, `result/success` (`total_cost_usd`, full `usage`).
Every row carries `uuid`, `session_id`, `parent_tool_use_id`.

Input side (`--input-format stream-json`) additionally accepts `control_request`.
Subtypes enumerated from the binary:
```
grep -aoE 'subtype:v\.literal\("[a-z_]{3,40}"\)' 2.1.220 | sed 's/.*("//;s/")//' | sort -u
→ interrupt, stop_task, set_permission_mode, set_model, set_max_thinking_tokens,
  set_cwd, can_use_tool, agents_killed, get_context_usage, get_session_cost,
  get_plan, get_usage, get_workspace_diff, rewind_files, hook_callback,
  mcp_*, permission_retry, rename_session, …
```
`--forward-subagent-text` (help text) forwards subagent text/thinking as
messages with `parent_tool_use_id` set — i.e. live per-subagent output without
touching files.

**Scenario / trade-off.** This is the only channel Anthropic documents, it gives
token-level deltas *and* structured tool/usage/cost events, and `can_use_tool`
lets the browser answer permission prompts. It does **not** give the TUI: no
box-drawing, no `/`-command UI, no `Esc` semantics. A terminal built on it is a
Touch-authored renderer, not "a web view over the terminal".

**Recommendation.** Use both, for different pages: PTY (LIVEIO-2) for the
terminal page README.md:3 asks for; SDK stream-json for the per-terminal
graph/UML page README.md:5 asks for, because it is the only channel that hands
you tool boundaries, `parent_tool_use_id`, token deltas and cost without
parsing ANSI. Note both cannot wrap the *same* process — decide per session
which mode it runs in, and show the mode in the UI.

---

## LIVEIO-6 — 31 hook events exist; they are the only push channel that works for sessions Touch did not spawn

**Severity: major**
**Proof:**
```
grep -aoE 'lB=\["PreToolUse".{0,600}' 2.1.220
lB=["PreToolUse","PostToolUse","PostToolUseFailure","PostToolBatch","Notification",
 "UserPromptSubmit","UserPromptExpansion","SessionStart","SessionEnd","Stop","StopFailure",
 "SubagentStart","SubagentStop","PreCompact","PostCompact","PermissionRequest",
 "PermissionDenied","Setup","TeammateIdle","TaskCreated","TaskCompleted","Elicitation",
 "ElicitationResult","ConfigChange","WorktreeCreate","WorktreeRemove","InstructionsLoaded",
 "CwdChanged","FileChanged","DirectoryAdded","MessageDisplay"]
```
Base payload on every event (`function Kf(`):
`session_id, transcript_path, cwd, prompt_id, permission_mode, agent_id, agent_type, effort`.
Notable extras (verified live, `/tmp/claude-1000/liveio/hooks.log`):
`PreToolUse{tool_name,tool_input,tool_use_id}`,
`PostToolUse{…,tool_response,duration_ms}`,
`SubagentStart{agent_id,agent_type}`,
`SubagentStop{agent_id,agent_type,agent_transcript_path,last_assistant_message,background_tasks,session_crons}`,
`Stop{stop_hook_active,last_assistant_message,background_tasks,session_crons}`,
`MessageDisplay{turn_id,message_id,index,final,delta}`,
`Notification{message,title,notification_type}` where notification_type ∈
`{permission_prompt, agent_needs_input, agent_completed, idle_prompt, worker_permission_prompt, …}`
(`grep -aoE 'notificationType:"[a-z_]+"'`).

Hooks are configured from `.claude/settings.json` in the project, so Touch can
install them for *this repo's* sessions without editing the user's global config.

**Recommendation.** Ship a single tiny hook script (`touch-hook.sh`) registered
for `SessionStart, SessionEnd, UserPromptSubmit, PreToolUse, PostToolUse,
SubagentStart, SubagentStop, Stop, Notification, TaskCreated, TaskCompleted`
that appends one JSON line to the task's `events.jsonl` — the exact shape the
existing monitor already streams (`monitoring.md:19`). `Notification` with
`notification_type=permission_prompt`/`agent_needs_input` is the "this terminal
needs you" badge for the sidebar; nothing else in the system pushes that.

---

## LIVEIO-7 — Hooks are strictly blocking; a slow hook stalls the user's session (default timeout 600 s)

**Severity: major (cost)**
**Proof — measured.** Same prompt, with an extra `sleep 3` command added to the
`PreToolUse` matcher (`/tmp/claude-1000/liveio/settings2.json`):
```
0.00 SessionStart
0.47 UserPromptSubmit
2.75 PreToolUse   Bash
5.85 PostToolUse  Bash   toolms=67      ← 3.10 s wall for a 67 ms tool
7.33 MessageDisplay
7.39 Stop
```
Default timeout: `var Hm=600000` (10 minutes), used as `timeoutMs:i=Hm` in the
hook executor `lM`. Hooks under one event run and are all awaited before the
turn continues; when no hook matches the executor returns immediately
(`if(y.length===0)return`), so unconfigured events cost nothing.

**Scenario.** Touch's hook does an HTTP POST to its own aggregator. The
aggregator is down / the sandbox proxy 403s / the port moved. Every tool call in
the user's session now blocks for the socket timeout, and with the default the
session can appear frozen for ten minutes with no UI explanation.

**Recommendation.** The hook must be `append one line to a local file and exit` —
no network, no python startup if avoidable (`printf` + `>>` under `flock`), and
an explicit small `"timeout": 5` on every hook entry rather than the 600 s
default. Touch's server tails the file. This is exactly the design the existing
module already uses (`status.sh` → `events.jsonl` → server), and it is the
correct one; do not "improve" it into a push to the server.

---

## LIVEIO-8 — `MessageDisplay` is a real streaming-text hook but runs `forceSyncExecution` on the render path

**Severity: major**
**Proof:**
```
grep -aoE '.{100}hook_event_name:"MessageDisplay".{400}' 2.1.220
… hook_event_name:"MessageDisplay",turn_id:…,message_id:…,index:…,final:…,delta:…
  … forceSyncExecution:!0,suppressPerInvocationTelemetry:!0
```
Measured in a real interactive REPL (driven through my own PTY host), asking for
12 lines of output:
```
MessageDisplay index=0 final=False delta='One\nTwo\nThree\nFour\nFive\nSix\nSeven\nEight\n'
MessageDisplay index=1 final=True  delta='Nine\nTen\nEleven\nTwelve'
```
So: incremental, chunk-granular (not per token), and **one process spawn per
chunk, synchronously, on the display path**.

**Scenario.** Someone chooses MessageDisplay as "the cheap way to stream
assistant text to the browser without a PTY". On a long answer that is dozens of
synchronous `fork+exec`s interleaved with rendering; the user sees their own
terminal get choppy, and the cause is invisible to them.

**Recommendation.** Do not use `MessageDisplay` as a transport. Use it, if at
all, only as a low-rate "assistant is producing text" liveness ping, and even
then guard it. The PTY channel already carries the same bytes at zero
per-message process cost.

---

## LIVEIO-9 — "Pause" does not exist in any channel; the README control set must be redefined before it is built

**Severity: blocker (product/spec)**
**Proof:** README.md:5-6 requires "pause, restart, start and terminate agents
loops". The complete control vocabulary available is:
`interrupt`, `stop_task`, `agents_killed`, `set_permission_mode`, `set_model`,
`set_cwd`, `rewind_files`, … (`subtype:v.literal(...)` enumeration above), plus
PTY `{"t":"kill"}` and hook decisions
(`permissionDecision: allow|deny|ask` for PreToolUse; `decision: approve|block`
+ `continue:false` + `stopReason` for Stop/SubagentStop —
`grep -aoE 'decision:v\.enum\(\["approve","block"\]'`). There is **no** suspend
or resume anywhere. The loops themselves are plain `while` loops in a Workflow
script (`implement.workflow.js:168`, `research.workflow.js`) — the runtime has no
notion of a pausable step.

What each verb can honestly mean:
- **start** — Touch spawns the session/workflow (LIVEIO-2). Real.
- **terminate** — PTY `{"t":"kill","sig":"SIGTERM"}`, or `control_request
  interrupt` in SDK mode, or `claude stop <short>` for a bg agent (I ran it:
  `stopped 8084340e`). Real, but coarse: it kills the whole run, not one loop.
- **restart** — real, and better than expected: the Workflow runtime memoises
  each `agent()` call by prompt hash. `journal.jsonl` lines are
  `{"type":"started","key":"v2:<sha256>","agentId":…}` and the binary says
  *"completed agents return cached results … 100% cache hit"*. Re-running the
  same script with the same `runId` replays finished agents and re-runs only what
  is missing.
- **pause** — does not exist. `SIGSTOP` on the process is not a substitute: it
  freezes mid-HTTP-stream and the API request will time out, so "resume" resumes
  into a broken turn.

**Recommendation.** Two honest implementations, pick one and label the button
accordingly:
1. **Gate hook (works for any session, no script change).** A `SubagentStart`
   (or `PreToolUse`) hook that blocks while a control file says "paused". This is
   a *real* pause with a real resume, bounded by the hook `timeout` — set it
   explicitly (e.g. 300 s) and have the hook release-and-report when it expires,
   so a forgotten pause degrades to "resumed" rather than to a wedged session.
   UI must say **"pause at next agent boundary"**, not "pause".
2. **Cooperative check in the orchestrator script.** `implement.workflow.js:74`
   already does `const fs = await import('node:fs')`, so the loop can poll a
   control file between `agent()` calls and also honour an "abort" flag. This
   gives pause/abort at a semantically meaningful boundary and needs no hook, but
   only works for loops Touch generated.
   (Note the workflow sandbox forbids `Date.now()/Math.random()/new Date()` —
   the control file must carry any timestamp the script needs.)

Whatever is chosen, the graph page must render the *pending* state ("pause
requested — will take effect at the next agent boundary"), because the gap
between click and effect is one whole agent, i.e. minutes.

---

## LIVEIO-10 — Live per-subagent attribution is fully solved by hooks (and only by hooks)

**Severity: major (this is the graph page's data source)**
**Proof — measured**, one interactive-equivalent run that spawned a subagent
(`/tmp/claude-1000/liveio/hooks.log`):
```
t+0.00 SessionStart
t+0.46 UserPromptSubmit
t+3.07 PreToolUse    tool=ToolSearch  agent_id=None
t+15.26 PreToolUse   tool=Agent       agent_id=None      ← spawn, tool_name is "Agent"
t+15.33 SubagentStart                 agent_id=a342353f7b157760b agent_type=general-purpose
t+17.37 PreToolUse   tool=Bash        agent_id=a342353f7b157760b
t+17.50 PostToolUse  tool=Bash        agent_id=a342353f7b157760b
t+19.22 SubagentStop                  agent_id=a342353f7b157760b
        agent_transcript_path=…/subagents/agent-a342353f7b157760b.jsonl
```
Spawn→`SubagentStart` latency **70 ms**. Every nested tool event carries
`agent_id`. `SubagentStop` hands you the transcript path — the exact join to the
on-disk file, with no guessing.

Two traps worth writing down: the spawn tool's `tool_name` in `PreToolUse` is
**`Agent`**, not `Task` (`Task` is the name in `system/init`'s tool list), so a
matcher on `Task` silently never fires; and `agent_id` is absent (not `null`) on
main-thread events, so treat missing as "main".

**Recommendation.** Make `SubagentStart`/`SubagentStop` + `agent_id`-tagged
`PreToolUse`/`PostToolUse` the canonical node/edge source for the n8n-style
graph. It gives node creation at +70 ms, per-node activity, and node completion
with a transcript link, with no polling. Reconcile against the transcript files
(LIVEIO-11) only for content and token counts.

---

## LIVEIO-11 — Transcript tailing is message-granular and 1–5 s late; it cannot back a terminal view

**Severity: major**
**Proof — measured** over the six live workflow subagents of the current run,
sampling sizes every 200 ms for 25 s:
```
20 append events in 25 s across 4 actively-writing agents
appends of 779 … 10852 bytes, inter-append gaps 0.2 s … 5.0 s per agent
```
The parent session file `dd469822….jsonl` did not change at all for a full
minute while six subagents worked. Writes land at message boundaries; there is
no per-token flush.

**Scenario.** Touch renders "the terminal" by tailing the transcript. The user
types a prompt and stares at a dead screen for 3–5 s until the first assistant
message lands whole, then it appears instantly in full. That is not a terminal.

**Recommendation.** Transcript tail is for (a) history/replay of sessions Touch
did not spawn, (b) token/content reconciliation, (c) the graph's detail panel —
never for the live terminal. Poll at 200–500 ms, and **defer torn tails**: the
existing watcher already does exactly this
(`.claude/shared/monitoring/decision_watcher.py:471`, "read whole journal lines
appended since offset; defer a torn tail") — reuse that code rather than
reinventing a line reader that will one day parse half a JSON object.

---

## LIVEIO-12 — The session registry has no heartbeat: `status` is stale by design

**Severity: major**
**Proof — measured:**
```
/home/agent/.claude/sessions/622.json : kind=interactive status=busy
                                        updatedAt age = 854.1 s
/home/agent/.claude/sessions/7095.json: kind=bg          status=idle
                                        updatedAt age = 409.6 s
```
pid 622 was genuinely busy the whole time (it is the session running this
research). The file is written on *status transitions*, not on a timer. Fields
present: `pid, sessionId, cwd, startedAt, procStart, version, peerProtocol, kind,
entrypoint, name, nameSource, status, updatedAt, statusUpdatedAt` and, per the
binary, optionally `tempo (active|idle|blocked), needs, tmux, logPath, agent,
jobId`. Liveness is recomputed on read against `/proc/<pid>` plus `procStart`
(the `/proc/pid/stat` start-time, a PID-reuse guard) — `claude agents --json`
does this and correctly dropped the exited session `e144bb01` from its output.

**Scenario.** The sidebar (README.md:4) shows a green "running" dot driven by
`status`. A session is SIGKILLed; its `<pid>.json` is never rewritten; the dot
stays green forever. Or worse, the pid is reused by an unrelated process and
Touch shows a live session that is someone else's `sleep`.

**Recommendation.** Never trust `status`/`updatedAt` as liveness. Either shell
out to `claude agents --json` (it already implements the pid+`procStart` check,
and it is a documented flag: *"does not require a TTY"*) or replicate that check
verbatim, comparing `procStart` against field 22 of `/proc/<pid>/stat`. For
sessions Touch spawned, prefer its own child-process table — that is
authoritative and instant.

---

## LIVEIO-13 — Subagent content is not in the parent transcript, and workflow agents carry no role in any on-disk record

**Severity: major**
**Proof:**
```
python3 … dd469822….jsonl → sidechain rows: 0   (isSidechain exists on 219 rows, all false)
subagents/agent-a4e343a0f7d73268c.meta.json
  {"agentType":"general-purpose","description":"Assess control and UI feasibility",
   "toolUseId":"toolu_017Uz…","spawnDepth":1,"model":"opus"}
subagents/workflows/wf_829e6f58-b2f/agent-a2ec106948f58d0c8.meta.json
  {"agentType":"workflow-subagent","spawnDepth":1,"model":"opus"}      ← no toolUseId,
                                                                       ← no role, no description
subagents/workflows/wf_829e6f58-b2f/journal.jsonl
  {"type":"started","key":"v2:<sha256>","agentId":"a2fc883c96ff7b837"}  ← ×6, nothing else
```
So for a *Task*-spawned agent the parent's `tool_use` block joins to the agent
file via `meta.json.toolUseId`. For a *workflow* agent there is no such join and
no human-meaningful name anywhere on disk — which is precisely why
`decision_watcher.py:121` has to scrape
`\[monitor\]\s+plan=(\S+)\s+(?:stage=(\S+)\s+)?role=(\S+)\s+attempt=(\d+)` out of
the agent's own prompt text.

**Scenario.** Touch's graph shows six anonymous `a2fc883…` boxes for a research
fan-out instead of `research/liveio`, `research/dataflow`, … — the run is
unreadable exactly when it matters.

**Recommendation.** Keep the `[monitor] plan=… stage=… role=… attempt=…` prompt
marker as a hard invariant of every Touch-generated workflow (it is already the
protocol — `monitoring.md`, `m-orchestrator/SKILL.md`), and additionally capture
`SubagentStart{agent_id,agent_type}` so the node exists at +70 ms with a
provisional label that the marker upgrades once the prompt is readable. Do not
build the graph on `meta.json` alone; for workflow agents it is nearly empty.

---

## LIVEIO-14 — Remote Control is a cloud relay, not a local channel; do not build on it

**Severity: minor (but it will look attractive, so record the rejection)**
**Proof:** `claude --remote-control [name]` exists in `--help`. Its transport is
```
grep -aoE '.{80}/v1/sessions/.{200}' 2.1.220
  GET  api.anthropic.com/v1/sessions/<id>/events/stream?beta=true
  POST api.anthropic.com/v1/sessions/<id>/events?beta=true
  anthropic-beta: managed-agents-2026-04-01
  "[bridge:attestation] DROPPING unverified control_request subtype=…"
```
**Scenario.** It does prove an interactive session can accept remote input — but
via Anthropic's servers, with attestation, an authenticated account, and working
egress. In this sandbox egress is default-deny (CLAUDE.md network section) and
the browser reaching Touch is inside the sandbox boundary anyway. Adding a cloud
round-trip to move bytes between two processes on the same machine is strictly
worse than LIVEIO-2 on latency, availability and privacy.

**Recommendation.** Explicitly out of scope. Note it in the design doc as
"considered and rejected: cloud relay" so it does not get re-proposed.

---

## LIVEIO-15 — Background agents already expose a structured loop-state plane Touch can read for free

**Severity: major (opportunity)**
**Proof — from a bg agent I started and stopped:**
```
~/.claude/jobs/8084340e/state.json
 {"state":"done","detail":"replied with BGOK as requested","tempo":"idle",
  "inFlight":{"tasks":0,"queued":0,"kinds":[]},"tokens":94,
  "output":{"result":"BGOK"},"children":null,
  "linkScanPath":"…/projects/-tmp-claude-1000-liveio/<sessionId>.jsonl",
  "sessionId":…,"resumeSessionId":…,"cwd":…,"respawnFlags":["--model","haiku"],
  "createdAt":…,"updatedAt":…,"firstTerminalAt":…}
~/.claude/jobs/8084340e/timeline.jsonl      ← append-only, one line per state change
~/.claude/daemon/roster.json                ← {pid, procStart, sessionId, ptySock,
                                               ptyAuth, rvAuth, cwd, attempt,
                                               dispatch{launch.args, respawnFlags}}
claude agents --json / claude stop <short>  ← supported list & terminate
```
`state`, `tempo`, `inFlight.tasks/queued`, `tokens`, `children` and an
append-only `timeline.jsonl` are exactly the fields a loop card needs, and
`roster.json` hands you the PTY socket + its auth token for the same session.

**Scenario.** Touch reimplements process supervision, restart-on-crash
(`RESPAWN_REASON_STALL/UPGRADE` exist in the binary) and status tracking that
the daemon already does.

**Recommendation.** Seriously evaluate running each Touch-managed loop as
`claude --bg` and treating `~/.claude/jobs/*/state.json` + `timeline.jsonl` +
`roster.json` as the read model, with `claude stop` as terminate and the roster's
`ptySock`/`ptyAuth` as the terminal feed. Cost: it couples Touch to the same
private layout as LIVEIO-4 (mitigate the same way), and `~/.claude/jobs/` is
global, not per-project — filter by `cwd`.

---

## LIVEIO-16 — The live channels are unix sockets and localhost files; the browser is on the other side of a sandbox boundary

**Severity: major**
**Proof:** CLAUDE.md:112-114 and the environment brief — ports are unreachable
until `sbx ports $SANDBOX_VM_ID --publish <p>:<p>/tcp`, and services must bind
`0.0.0.0`. The existing server already does this
(`monitor_server.py:519` `asyncio.start_server(handle,"0.0.0.0",PORT)`), and its
failure message names the port collision case (`monitor_server.py:521`).

**Scenario.** Touch's aggregator binds `127.0.0.1` (the natural default for
"local dev"), the user publishes the port, and gets connection-refused with no
diagnostic. Or Touch picks 8931 and collides with the running monitor
(`ps` shows `monitor_server.py` live on 8931 right now).

**Recommendation.** Bind `0.0.0.0`, take the port from argv > env > config >
default exactly as the existing module does, pick a default that is **not** 8931
so both can run side by side, and expose `/health` so the publish step can be
verified in one curl. Document the one `sbx ports` line in the README; it is the
single step nothing in the sandbox can do for the user.

---

## LIVEIO-17 — Hook observability differs by mode, which will make Touch's two pages disagree

**Severity: minor**
**Proof:** `--include-hook-events` is documented as "only works with
`--output-format=stream-json`", and in my print-mode run it produced
`system/hook_started` and `system/hook_response` rows (with `exit_code`,
`stdout`, `stderr`, `outcome`). Driving a real interactive REPL through the PTY
with the identical settings file produced no such rows anywhere — the hooks ran
(they wrote `hooks.log`) but nothing in the session reported them.

**Scenario.** A hook fails (bad path, non-zero exit). In SDK mode Touch shows
"hook failed"; in PTY mode it shows nothing and the gate from LIVEIO-9 silently
stops gating.

**Recommendation.** Do not rely on the CLI to report hook health. Have the hook
script itself emit a heartbeat line (start + exit code) into `events.jsonl`, and
have the server flag "no hook heartbeat for N seconds" — that works identically
in both modes.

---

## LIVEIO-18 — The same event arrives on up to three channels at different times; define the dedup key now

**Severity: minor**
**Proof:** a single Bash call produced (a) `PreToolUse`/`PostToolUse` hook lines
with `tool_use_id=toolu_01P6vd…` and `duration_ms=61`, (b) `stream_event` /
`assistant` / `user` rows carrying the same `toolu_01P6vd…` plus their own
`uuid`, and (c) a transcript line with `toolUseResult`, written seconds later
(LIVEIO-11). Hook `PostToolUse` fires *before* the result reaches the file.

**Recommendation.** Canonical identity: `tool_use_id` for tool events,
`agent_id` for agents, `uuid` for messages, `session_id` for the session. Make
the aggregator idempotent on those keys and let late channels enrich an existing
node rather than create a second one — otherwise the graph grows duplicate boxes
whenever two channels are enabled at once. The existing `events.jsonl` schema
(`monitoring.md:19`) has no id field for this; add one deliberately rather than
by accident.

---

## LIVEIO-19 — A killed run leaves `started` with no `result` forever; liveness must be inferred

**Severity: minor**
**Proof:** the current run's `journal.jsonl` contains six
`{"type":"started",…}` lines and nothing else; the watcher only ever handles
`started` (`decision_watcher.py:587`) and `result`
(`decision_watcher.py:667`). There is no `failed`/`cancelled` record. If the
session is killed mid-run, those six agents stay "running" in the file for ever.

**Recommendation.** Keep the existing quiet/stale timeout logic
(`decision_watcher.py:451`, "terminal run state implied by the journal so far,
or None while live") and surface it in the UI as an explicit **stale** state
distinct from **running** — a graph that shows six perpetually-spinning nodes
after a crash is worse than one that says "no activity for 5 min".

---

## LIVEIO-20 — `CLAUDE_PTY_RECORD` writes a private binary format; tee the frames yourself instead

**Severity: nit**
**Proof:** the PTY host honours `CLAUDE_PTY_RECORD` (`C=Q6_(process.env.CLAUDE_PTY_RECORD,cols,rows)`
then `C?.write(chunk)`); I set it and got a 27-byte file whose first bytes are
`\x00…Z…5\xNN REC_PROBE` — a length-prefixed binary log, not asciinema JSON.
Related knobs exist (`CLAUDE_CODE_TERMINAL_RECORDING`, `CLAUDE_CODE_SESSION_LOG`,
`CLAUDE_CODE_TEE_SDK_STDOUT`).

**Scenario.** Touch relies on it for "replay a closed terminal", then a version
bump changes the framing and all history becomes unreadable.

**Recommendation.** Touch already receives every frame on the socket; write its
own append-only `<session>/pty.log` (raw bytes + a sidecar index of
`offset,timestamp`) so the sidebar can replay terminals beyond the host's 256 KB
scrollback ring, in a format Touch owns. One line of code, no version coupling.

---

## Artifacts of this research (throwaway, safe to delete)

`/tmp/claude-1000/liveio/` — `settings.json` / `settings2.json` (hook matrices),
`hook.sh`, `hooks.log`, `stream.jsonl` / `stream2.jsonl` (stream-json captures),
`ptydump.py`, `ptytest2.py`, `probe.py`, `drive.py` (PTY socket clients).
All spawned processes (bg agent `8084340e`, its daemon, three `--bg-pty-host`
hosts) were stopped; `ps -C claude` is back to the user's session only.
