# Research findings — THE CONTROL PLANE

Perspective: *can an external web page start, pause, restart or terminate agent
loops?* Everything below was verified against primary sources on this machine
(running processes, `~/.claude` on-disk state, the 2.1.220 CLI binary, and two
throwaway live experiments under `/tmp/claude-1000`). Where a claim rests on an
experiment, the exact command/script path is given.

---

## Part 0 — What a "loop" IS at runtime (the premise everything else rests on)

Established by reading the skill templates plus the live run of this very
research task:

1. **A loop is a JavaScript program, not a process.**
   `.claude/skills/implement-plan/templates/implement.workflow.js:163-210`
   (`runLoop`) is a `while (!success && attempt < MAX_ATTEMPTS)` loop that
   `await`s three `agent()` calls. `execute-research`'s "loop" is flatter —
   `research.workflow.js:139-144` is one `parallel()` fan-out plus a barrier,
   then one synthesizer (`:150`). There is no daemon, no queue, no scheduler.
2. **The script runs inside the CLI process, in a `vm` context.**
   Binary: `tEd=S(()=>{…Msn=x(require("vm"))})` and
   `let d; try { p = e.runInContext(l.vmContext…` — the workflow source is
   compiled and run with `vm.runInContext` inside the same Node process.
3. **Its agents are also in-process.** Verified: `ps -eo pid,ppid,stat,args`
   while six workflow researchers were concurrently live showed exactly **one**
   `claude` process (`PID 622`) and no child `claude`/`node` processes; every
   `agent-*.jsonl` under
   `~/.claude/projects/-home-laniakea-Projects-touch/dd469822-…/subagents/workflows/wf_829e6f58-b2f/`
   is written by PID 622, and every Bash call an agent makes has `PPID 622`.
4. **The only durable, resumable boundary is one `agent()` call.** The run dir
   holds `journal.jsonl` with one line per agent lifecycle event:
   `{"type":"started","key":"v2:<sha256>","agentId":"a2fc883c96ff7b837"}` and a
   later `{"type":"result",…}`. The Workflow tool's own doc string in the binary
   states it exactly:

   > "To resume after a pause, kill, or script edit, relaunch with
   > `Workflow({scriptPath, resumeFromRunId})` — the longest unchanged prefix of
   > `agent()` calls returns cached results instantly; the first edited/new call
   > and everything after it runs live. Same script + same args → 100% cache hit."

   and `resumeFromRunId` is documented as *"Completed agent() calls with
   unchanged (prompt, opts) return their cached results instantly; only edited
   or new calls re-run. **Same-session only.** Stop the prior run first before
   resuming."*
5. **In-flight agents are re-run, not resumed.** The binary emits telemetry
   `tengu_workflow_journal_started_hit_respawn` when replay finds a `started`
   key with no matching `result` — i.e. an agent that was aborted mid-flight is
   spawned again from scratch. Our live journal proves the shape: at the time of
   writing it contains six `started` lines and zero `result` lines.
6. **Loop state on disk is split three ways**: journal (`journal.jsonl`, the
   resume cache), the working tree (whatever implementers already wrote), and
   the task folder (`findings/*.md`, `plan/*.md`, `events.jsonl`). Only the
   first is understood by the harness; the other two are ours.

Everything below follows from (2)+(3): **a loop has no OS identity of its own.**

---

## CONTROL-1 — A loop cannot be signalled, killed or niced individually; the process is the whole session
**Evidence:** `ps -eo pid,ppid,stat,etime,args` (one `claude`, PID 622, six live
agents); binary `runInContext` + `vm`; `implement.workflow.js:163` (`runLoop` is
a JS function).
**Severity: blocker** (for the README as written).

**Scenario.** Touch's per-terminal page shows three sub-plan loops, `sp-a`
running, `sp-b` and `sp-c` queued behind it. The user clicks "terminate" on
`sp-a`. There is no PID, no thread, no cgroup, no socket that corresponds to
`sp-a`: it is a stack frame inside PID 622's event loop. Any OS-level action
(`kill`, `SIGSTOP`) hits **every** loop, the driver conversation, and the user's
own terminal simultaneously. The only per-loop identifiers that exist anywhere
are `workflowRunId` (`wf_829e6f58-b2f`), `agentId`
(`a2fc883c96ff7b837`), and the `[monitor] plan=…` marker the *script* embeds in
each prompt.

**Recommendation.** Model Touch's control plane as
`(sessionId, workflowRunId, agentId, plan)` addressing over **in-band**
channels, never as process control. Reserve OS signals for one and only one
verb — "kill this whole session" — and label that button accordingly.

---

## CONTROL-2 — An already-running interactive session has **no** external control transport at all
**Evidence (all four verified on this box):**
- `/proc/622/fd/0 -> /dev/pts/0`, `fd/1 -> /dev/pts/0` — stdin is a PTY *slave*.
  Writing to the slave paints the screen; it does not inject input.
- `cat /proc/sys/dev/tty/legacy_tiocsti` → **`0`**. `TIOCSTI` keystroke
  injection is disabled by the kernel, so the classic "type into someone else's
  terminal" trick is unavailable.
- `/proc/net/tcp` + `/proc/net/tcp6` listening sockets: `8931`
  (`monitor_server.py`) and `8999` (my test server) only. **`claude` listens on
  no port.**
- `/tmp/cc-daemon-$(id -u)/…/control.sock` does not exist (binary:
  `GBe=…path.join(tmp, "cc-daemon-"+uid, sha256(cwd).slice(0,8))`,
  `jse()=…join(GBe(),"control.sock")`). That socket only exists when the
  background-session daemon is running; a plain interactive `claude` does not
  create one.
- No custom signal handler exists beyond shutdown: the only `process.on("SIG…")`
  registrations in the binary are `SIGINT`/`SIGTERM`/`SIGHUP` → exit,
  `SIGCONT`/`SIGWINCH` → TTY redraw. `SIGUSR1/2` appear only inside a
  vendored signal-name table. There is no "control" signal.

**Severity: blocker** (for README lines 3-5, "main terminal is web view over
claude code session").

**Scenario.** The user has `claude` open in their own terminal (exactly the
PID-622 case here). They open Touch, click that session in the sidebar, and
press "pause". Touch has literally nothing to write to: no port, no socket, no
FIFO, no injectable stdin. It can *read* everything (transcripts, journal,
`~/.claude/sessions/<pid>.json`) and can *kill* the PID. Nothing in between.

**Recommendation.** Split the sidebar into two classes and make the split
visible in the UI:
- **Adopted sessions** — started *by* Touch (Touch owns stdin): full control.
- **Foreign sessions** — discovered via `~/.claude/sessions/*.json` /
  `claude agents --json`: **read-only**, plus a "kill" that is honestly labelled
  as killing the process. Do not render pause/restart affordances on these.
Re-word README line 3 from "web view over claude code session" to "terminal that
Touch hosts", or accept that the primary UI only works for Touch-started
sessions.

---

## CONTROL-3 — The one real, documented control channel is `--input-format stream-json`, and it supports exactly four verbs
**Evidence:** binary switch on `request.subtype` accepts `initialize`,
`interrupt`, `set_model`, `set_permission_mode` (and outbound-only
`can_use_tool` / `hook_callback` / `request_user_dialog` / `mcp_message`).
**Verified live** with `/tmp/claude-1000/ctl/t.py`: started
`claude -p --input-format stream-json --output-format stream-json --verbose`,
wrote a user turn, then wrote
`{"type":"control_request","request_id":"req-1","request":{"subtype":"interrupt"}}`
on stdin. The process replied
`{"type":"control_response","response":{"subtype":"success","request_id":"req-1","response":{"still_queued":[]}}}`
and immediately emitted
`{"type":"system","subtype":"task_updated","task_id":"bicegqgn1","patch":{"status":"killed",…}}`
— the in-flight background Bash task was killed.
**Severity: major** (this is the load-bearing design decision).

**Scenario.** Touch spawns each session as
`claude -p --input-format stream-json --output-format stream-json --verbose
--include-partial-messages --replay-user-messages --forward-subagent-text
--include-hook-events --session-id <uuid>`. The browser gets a rich structured
event feed (verified message types seen in the experiment: `system/init` with
the full tool list, `rate_limit_event`, `assistant`, `user`, `system/task_started`,
`system/background_tasks_changed`, `system/task_notification`,
`system/task_updated`, `system/thinking_tokens`, `result`), and Touch can send
user turns + the four control verbs. What Touch **cannot** send is any form of
pause, resume, spawn-agent, retry-agent or skip-agent — those subtypes do not
exist in the parser.

**Recommendation.** Adopt stream-json as the aggregator's session transport.
Then be explicit in the plan that of the README's four verbs, this channel
natively provides **start** (write a user turn) and **terminate**
(`interrupt`), and provides **neither pause nor restart** — those must be
synthesized (CONTROL-8, CONTROL-5). Note also that `-p` mode gives you a
structured feed but **not** a real terminal; if you also want authentic ANSI/TUI
output you need a second, PTY-hosted process — one process cannot be both.

---

## CONTROL-4 — Claude Code's own "pause" is abort-plus-checkpoint, not suspend; in-flight agent work is destroyed
**Evidence:** binary,
`function jxo(e,t,r,n){…i.abortController?.abort(); return {…, status:r, endTime:Date.now(), abortController:void 0, agentControllers:void 0}}`
with `kft(e,t) = jxo(e,t,"paused",{notified:!0})` and
`tve(e,t) = jxo(e,t,"killed",…)`. Pause and kill are **the same function** with
a different status string; pause additionally produces the message
*"Resume the paused workflow by calling: `Workflow({scriptPath, resumeFromRunId})` — completed agents return cached results."*
**Severity: major.**

**Scenario.** A user pauses `sp-payments` while its attempt-2 implementer is 8
minutes into editing four files. The AbortController fires, the agent dies, no
`result` line is journalled, and the agent's partial edits stay on disk. On
resume the implementer is respawned **from scratch** (see
`tengu_workflow_journal_started_hit_respawn`) — same prompt, fresh context,
dirty tree, and ~8 minutes of tokens already burned and unrecoverable. If Touch
labels this "pause" the user will reasonably expect the opposite.

**Recommendation.** In Touch, never expose a verb called "pause" that maps to
this. Call it **"Stop & checkpoint"**, show the cost ("the running agent will be
discarded and restarted from its prompt"), and put the true low-cost pause
(CONTROL-8) on a separate control.

---

## CONTROL-5 — Restart/resume exists but is *same-session only* and is a **tool call**, not an API
**Evidence:** `resumeFromRunId:v.string().regex(/^wf_[a-z0-9-]{6,}$/)…"Same-session only. Stop the prior run first before resuming."`;
the resume instruction strings in the binary are all of the form
`Workflow({scriptPath: '…', resumeFromRunId: 'wf_…'})`. Verified in our own
session transcript (`…/dd469822-….jsonl` line 294): the run was launched by the
**model** calling `Workflow({scriptPath})`, and the tool result (line 295) reads
`Workflow launched in background. Task ID: wpbwj76b3`.
**Severity: major.**

**Scenario.** Touch's "restart loop" button must ultimately cause the *model in
that session* to emit a `Workflow` tool call with the right `resumeFromRunId`.
An HTTP handler cannot do it directly. The only mechanism is: write a user turn
into that session's stdin containing the resume instruction, and hope the model
complies. That is (a) impossible for foreign sessions (CONTROL-2), (b)
non-deterministic even for adopted ones, and (c) impossible across sessions —
resuming `wf_829e6f58-b2f` from a *new* session is rejected outright.

**Recommendation.** Make restart a **Touch-owned** operation rather than a
harness-owned one:
- Touch keeps `{scriptPath, workflowRunId, args}` per run (all three are in the
  Workflow tool result and in the session transcript — parse them at launch).
- "Restart" = send the adopted session a scripted user turn whose entire content
  is the literal `Workflow({scriptPath:'…', resumeFromRunId:'wf_…', args:…})`
  instruction, then watch `journal.jsonl` for new `started` lines to confirm the
  model actually did it. Surface "restart requested / restart confirmed" as two
  distinct UI states — a requested-but-unconfirmed restart is the normal failure
  mode and must not be shown as success.
- Disable the restart control entirely for foreign sessions.

---

## CONTROL-6 — `journal.jsonl` is the restart state, and it is shared with the monitoring watcher; editing it breaks the watcher
**Evidence:** journal contents (above); replay behaviour (CONTROL-5);
`decision_watcher.py:470-491` (`read_new_lines`) and `:494-520`
(`load_state`/`save_state`) keep a **byte offset** into that exact file in
`.watcher-state.json`, and `load_state` only resets when the journal *path*
changes — not when its *content* changes.
**Severity: major.**

**Scenario.** Touch offers "re-run just the critique of `sp-api` attempt 2". The
obvious implementation is to drop that `result` line from `journal.jsonl` so
replay respawns it. The moment you rewrite the file shorter, `decision_watcher`
(offset 41 kB, file now 38 kB) silently stops emitting: `size > state["offset"]`
is false forever, the dashboard freezes mid-run, and no error is logged. If you
rewrite it *longer*, the watcher re-parses garbage from a mid-line offset.

**Recommendation.** Treat `journal.jsonl` as **append-only and harness-owned**.
To force a specific stage to re-run, change the *script* instead (the cache key
is `(prompt, opts)` — the tool doc is explicit: "the first edited/new call and
everything after it runs live"), e.g. by bumping a per-stage nonce that Touch
writes into the workflow script's constants. If you ever do need to truncate the
journal, the plan must also specify deleting `.watcher-state.json` in the same
operation and restarting the watcher.

---

## CONTROL-7 — Nothing in the harness rolls back the working tree; every restart resumes onto a dirty tree
**Evidence:** `jxo` (CONTROL-4) clears controllers and status only — no
filesystem action. `implement.workflow.js:66` explicitly instructs agents
"Working tree may hold unrelated in-flight changes — never revert/commit/stash".
`implement.workflow.js:172-176` starts each attempt with a brand-new implementer
against whatever is on disk.
**Severity: major.**

**Scenario.** User terminates `sp-parser` attempt 3 mid-edit. `parser.py` is
half-rewritten and does not import. User then clicks "restart". A fresh
implementer reads the sub-plan, reads the findings files, and now also inherits
a syntactically broken file it never wrote, with no record that the breakage is
an artifact of the abort. Its test gate fails for reasons unrelated to the plan,
burning another attempt out of `MAX_ATTEMPTS = 4`
(`implement.workflow.js:30`).

**Recommendation.** Touch must own the checkpoint the harness doesn't:
before issuing any pause/terminate, record `git stash create` / a throwaway
commit sha / `git status --porcelain` for the loop's owned file set (the
partition gives Touch exactly that list — `implement.workflow.js:264-268`), show
it in the UI, and offer "restore tree to checkpoint" as an explicit, separate
action from "restart loop". Never bundle them silently.

---

## CONTROL-8 — The real external control plane is **hooks**, and an HTTP hook works (verified end to end)
**Evidence — live experiment**, `/tmp/claude-1000/hooktest/`:
- `settings.json`:
  `{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"http","url":"http://127.0.0.1:8999/","timeout":300}]}]}}`
- a stdlib `ThreadingHTTPServer` that **slept 20 s** then returned
  `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"touch-approved"}}`
- `claude -p --settings … "Run the bash command: echo HELLO_TOUCH"`
- Result: wall clock **28 s** vs `duration_api_ms` **4428** — the tool call was
  held for the full 20 s and then proceeded. The POST body received was:
  `{"session_id":"470dd028-…","transcript_path":"/home/agent/.claude/projects/-tmp-claude-1000-hooktest/470dd028-….jsonl","cwd":"/tmp/claude-1000/hooktest","prompt_id":"b48b28c8-…","permission_mode":"bypassPermissions","hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"echo HELLO_TOUC…`

**Additional evidence from the binary** — the hook input builder is
`function Kf(e,t,r){…return {session_id, transcript_path, cwd, prompt_id, permission_mode, agent_id: r?.agentId, agent_type, effort}}`,
i.e. **`agent_id` is in every hook payload when the call comes from a subagent**
(absent on the main thread, which is why our single-threaded test shows no
`agent_id`). Hook events include `PreToolUse`, `PostToolUse`, `SubagentStart`
(`hook_event_name:"SubagentStart",agent_id…`), `SubagentStop`, `Stop`,
`UserPromptSubmit`, `SessionStart`, `SessionEnd`, `PreCompact`, `Notification`.
`PreToolUse` decisions are `allow | deny | ask | defer`. Hook types are
`command | http | prompt | agent`.
**Severity: major (this is the opportunity, and it changes the architecture).**

**Scenario.** Touch's aggregator exposes `POST /hook`. A one-line static hook
config points every session at it. Now Touch can, per **`agent_id`**:
- **pause** — hold the HTTP response (the agent blocks at its next tool call),
- **step** — hold, then release exactly one call,
- **terminate a single agent** — return `permissionDecision:"deny"` repeatedly,
- **observe deterministically** — `SubagentStart`/`SubagentStop` give exact
  spawn/stop with `agent_id`, strictly better than tailing `journal.jsonl`
  (which has no timestamps at all — `decision_watcher.py:305-327` exists purely
  to work around that).

This is the only mechanism found that pauses **one** loop while its siblings
keep running, and it needs no cooperation from the model.

**Recommendation.** Make the hook endpoint a first-class component of the
aggregator (stdlib `http.server` — no new dependency), and make the visual
page's pause/step/kill controls write into the per-`agent_id` gate table it
reads. Emit hook events into `events.jsonl` alongside the existing watcher
events so one stream still drives the UI.

---

## CONTROL-9 — Hook-based pause has hard limits: tool boundaries, a timeout ceiling, and `deny` is visible to the model
**Evidence:** binary — default hook dispatch timeout `var Hm=600000` (10 min);
per-hook `timeout:v.number().positive()` (seconds) for command/http/prompt hooks
and `timeout:v.number().min(0).max(600000)` elsewhere; timeout paths log
`"PreToolUse hook timed out (per-hook abort)"` and
`"…output discarded. Raise the hook's \"timeout\" to allow more time."`;
`deny` sets `u.permissionBehavior="deny"` and
`u.blockingError={blockingError:…}` — i.e. the model receives a tool error.
**Severity: major.**

**Scenario A (latency).** A user pauses an agent that is 90 s into a long
reasoning turn. Nothing happens until the model next calls a tool — the UI shows
"pausing…" for an unbounded time. Worse: an agent whose *final* act is to return
structured output may never call another tool, so the pause never lands and the
loop advances anyway.

**Scenario B (timeout).** Touch holds a hook response for 15 minutes. At 10
minutes the harness aborts the hook, discards its output, and the tool call
proceeds unpaused — the "pause" silently expires.

**Scenario C (semantics).** Touch uses `deny` to stop an agent. The agent sees a
tool error, apologises, tries a different command, and keeps burning tokens; the
critique agent later reviews a change-set shaped by Touch's denials.

**Recommendation.** Specify precisely: pause = *hold the response*, never
`deny`; hold in ≤120 s slices with immediate re-POST so the 10-minute ceiling is
never approached; show pause state as `requested → effective at <first gated
tool call>` in the UI, with the pending duration visible; and for "terminate one
agent", prefer the workflow-level abort (CONTROL-3/4) over a deny loop.

---

## CONTROL-10 — Hooks are session-scoped configuration: Touch must install them before the session starts
**Evidence:** hooks are resolved from settings sources (`--settings`,
`--setting-sources`); the only reload path found in the binary is
`"loadPluginHooks: plugin-affecting settings changed"` / `"reloading due to
plugin-affecting settings change"` (plugin-scoped), and plugin changes prompt
`Run /reload-plugins to apply`.
**Severity: minor** (easy to design around; expensive to discover late).

**Scenario.** Touch is opened while a session is already running and tries to
gain control by writing `.claude/settings.json`. Whether the running process
picks that up is unverified and version-dependent; if it doesn't, the pause
controls appear functional but do nothing.

**Recommendation.** Touch launches every adopted session with an explicit
`--settings <touch-hooks.json>` whose content is **static** (a URL and a
matcher, nothing else) so it never needs reloading; **all** policy lives in
Touch's HTTP handler. Never render control affordances for a session whose
launch args Touch did not set — Touch can check this from
`~/.claude/sessions/<pid>.json` (which records `pid`, `sessionId`, `cwd`,
`kind`, `status`, `name`) plus its own launch registry.

---

## CONTROL-11 — `SIGSTOP` is a real whole-session freeze, and it is a trap
**Evidence — live experiment** `/tmp/claude-1000/ctl/t2.py`: started a
stream-json session, sent a long-generation user turn, `SIGSTOP` at t+4 s,
`SIGCONT` at t+74 s. The process resumed and the turn completed with
`duration_api_ms: 74097` — the in-flight HTTPS stream survived a 70-second stop.
Binary confirms no application-level handling: `SIGSTOP` appears only in a
signal-description table (`{name:"SIGSTOP",number:19,action:"pause"}`).
**Severity: major.**

**Scenario.** It works, right up until it doesn't. `SIGSTOP` (a) freezes **all**
loops plus the user's own conversation, since they share PID 622 (CONTROL-1);
(b) does **not** stop processes an agent already spawned — a `Bash`
`run_in_background` job keeps writing files while the "paused" session cannot
observe it; (c) leaves an idle TLS stream to the API and through this sandbox's
egress proxy, which will eventually be reaped — the 70 s survival above is not
evidence that 10 minutes survives; (d) makes the session unresponsive to its own
stdin, so a stopped session cannot even be sent `interrupt`.

**Recommendation.** Do not build the README's "pause" on `SIGSTOP`. If it is
offered at all, offer it as a distinct, clearly-labelled **"freeze process"**
emergency control, scoped to the whole session, with an explicit maximum
duration enforced by Touch (auto-`SIGCONT`), and disabled by default.

---

## CONTROL-12 — "Start" is fully achievable; three routes, and the choice is architectural
**Evidence:** `claude --help` — `-p/--print`, `--input-format stream-json`,
`--output-format stream-json`, `--session-id <uuid>`, `--bg/--background`
("Start the session as a background agent and return immediately (manage with
`claude agents`)"), `--permission-mode`, `--model`, `--settings`, `--add-dir`.
`claude agents --json` verified working (returned both live sessions on this box
with `pid`, `cwd`, `kind`, `sessionId`, `name`, `status`).
**Severity: minor** (it works — the risk is picking the wrong one).

The three routes and what each costs:

| route | control | terminal fidelity | discovery |
|---|---|---|---|
| `-p --input-format stream-json` child | 4 control verbs + user turns, Touch owns stdin | none (no ANSI/TUI) | Touch's own registry |
| PTY-hosted `claude` (Touch calls `os.openpty`) | keystrokes (incl. Esc = interrupt), resize | full, byte-exact | Touch's own registry |
| `claude --bg` + daemon | via `claude agents` / private sockets | via private PTY socket | `claude agents --json` |

**Recommendation.** Run **both** of the first two, not one: a PTY-hosted process
for the README's "terminal with terminal design" main page (stdlib `pty` +
`select` — zero dependencies, matching the module's constraint), and, for
sessions where structured control matters more than looks, a stream-json child.
Do not attempt to get both from a single process. Always pass an explicit
`--session-id` so Touch's URL for a session is stable and joinable to
`~/.claude/projects/<slug>/<sessionId>.jsonl` from the first byte.

---

## CONTROL-13 — "Terminate" has two very different meanings; only one of them is safe
**Evidence:** `interrupt` verified to kill in-flight work while the session
survives (CONTROL-3). Process kill: binary registers
`process.on("SIGHUP",…Ds(129))`, `process.on("SIGTERM",…)`, `SIGINT → exit`; our
first experiment ended with `EXIT 143` (SIGTERM) with no cleanup output. On the
next start the harness reports
`"N background workflow(s) orphaned by previous process exit"` and
`"No completion record was found for background workflow … from the previous
session. It may have been stopped (via the UI or TaskStop — these leave no
transcript marker)"`.
**Severity: minor.**

**Scenario.** Touch's "terminate" kills PID 622 to stop one loop. Every other
loop dies with it; the completion record `…/<sessionId>/workflows/<runId>.json`
is never written (verified: that directory **does not exist** for our live run —
`OSd()` writes it only at run end), so Touch's own history for that run is
permanently incomplete; and the driver conversation that was supposed to close
the `orchestrator` badge is gone, leaving the dashboard card "running" forever
(the exact failure `decision_watcher.py:731-760` was written to paper over).

**Recommendation.** Wire the UI's "terminate" to `control_request/interrupt`
for adopted sessions, and expose process-kill only as a separate, confirmed
"kill session" action. Whenever Touch kills or interrupts, **it** must write the
terminal record itself — both a `status.sh <plan> plan failed …` /
`orchestrator complete …` event (per `m-orchestrator/SKILL.md:72-75`) and its own
run record — because the harness will not.

---

## CONTROL-14 — The private background-session IPC exists but must not be built on
**Evidence:** binary — sock dir
`GBe() = /tmp/cc-daemon-<uid>/<sha256(realpath(cwd)).slice(0,8)>`; endpoints
`control.sock`, `pty/<id>.sock`, `spare/<id>.pty.sock`, `spare/<id>.claim.sock`,
pid files under `pty-pids/<id>.pid`, a shared secret in `pipe.key`; framed
messages `{t:"hello",replPid,version}`, `{t:"auth",token}`, `{t:"ping"}`,
`{t:"pong"}`, `{t:"live"}`, `{t:"resize",cols,rows}`,
`{t:"kill",sig:"SIGTERM"|"SIGKILL"}`, `{t:"exit",code,signal}`; roster in
`roster.json`; diagnostics print `bg sessions: / sock dir: / control.sock:
reachable|unreachable`.
**Severity: minor** (tempting shortcut, high decay risk).

**Scenario.** Touch attaches to a `--bg` session's PTY socket to render a real
terminal for a session it did not spawn. It works on 2.1.220, then a point
release renames a frame type or rotates `pipe.key` handling, and Touch's
headline feature dies with no error the user can act on. The binary itself warns
about mixed versions ("from a different CLI version…").

**Recommendation.** Use only the **supported** surface of this subsystem —
`claude agents --json` for discovery — and host PTYs Touch created itself for
everything interactive. If the private socket is used at all, isolate it behind
one adapter module, version-gate it on the CLI version string from
`~/.claude/sessions/<pid>.json` (`"version":"2.1.220"`), and degrade to
read-only when it doesn't match.

---

## CONTROL-15 — Per-agent skip/retry already exist — with no transport out of the TUI
**Evidence:** binary —
`function ISd(e,t,r,n){…let s=i.agentControllers?.get(t); if(s&&!s.signal.aborted) s.abort(new DOMException(r,"AbortError"))…}`,
`Vfr = ISd(…,"user-skip")`, `zfr = ISd(…,"user-retry")`, wired only into the
Ink component:
`jsx(FRr,{workflow:Z2, onDone, onKill:()=>tve(…), onPause:()=>kft(…), onResume:(Fo)=>{…}, onSkipAgent:(Fo)=>Vfr(…), onRetryAgent:(Fo)=>zfr(…)})`.
**Severity: minor** (a "so close" item worth recording as a decision).

**Scenario.** The exact four buttons the README asks for already exist inside
Claude Code — kill, pause, skip-agent, retry-agent, addressed by
`(taskId, agentId)` — and they are reachable **only** by a human pressing keys
in the TUI. There is no control_request subtype, no socket command and no CLI
flag that reaches `ISd`. Note also `onResume` is not a resume at all: it calls
the session's submit function with the `Workflow({scriptPath, resumeFromRunId})`
text, i.e. it types the resume instruction for you (consistent with CONTROL-5).

**Recommendation.** Record this explicitly in the plan so nobody spends a day
hunting for the missing endpoint. Touch's per-agent control must be synthesized
from CONTROL-8 (hook gate keyed on `agent_id`) plus CONTROL-3 (whole-run
interrupt); per-agent *retry* is only reachable via the script-edit + resume
path of CONTROL-5/6.

---

## CONTROL-16 — `TaskStop` and `Workflow` are model-facing tools; any Touch verb built on them is advisory, not deterministic
**Evidence:** `TaskStop` schema (in-session tool, `task_id`); the current run's
task id `wpbwj76b3` appears only in the tool result inside the session
transcript; `Workflow` is likewise a tool. Both require an assistant turn.
Additionally `Workflow` can be disabled outright: `disableWorkflows` managed
setting, `CLAUDE_CODE_DISABLE_WORKFLOWS`, the `/config` "Dynamic workflows"
toggle, and a named-workflows-only mode that rejects `script`/`scriptPath`/
`resumeFromRunId`/`remote`.
**Severity: minor.**

**Scenario.** Touch's "terminate loop" sends the adopted session a turn saying
"call TaskStop with wpbwj76b3". The model is mid-tool-call, or decides to ask a
clarifying question, or the session has workflows disabled by policy — and the
loop keeps running while the UI shows "terminating".

**Recommendation.** Every model-mediated verb in Touch must be modelled as a
**request with an observable confirmation**: issue → watch the deterministic
signal (journal `started`/`result`, `SubagentStop` hook, `task_updated` event)
→ only then flip the UI state, with a visible timeout and a fallback to the
deterministic path (`interrupt`). Also probe workflow availability at session
start and hide loop controls when the feature is disabled.

---

## CONTROL-17 — What is semantically impossible (state this in the plan, don't discover it in sprint 3)
**Evidence:** the union of CONTROL-1/2/4/5/9/11.
**Severity: major** (scope-defining).

1. **Suspend/resume of in-flight model inference.** No primitive exists at any
   layer. The nearest approximations are: freeze the OS process (CONTROL-11,
   whole session, risky) or gate at tool boundaries (CONTROL-8, granular,
   delayed). Neither preserves an in-progress turn against a real abort.
2. **Pausing one loop while its siblings continue — at process level.** They are
   the same process (CONTROL-1). Only the hook gate achieves it, and only at
   tool boundaries.
3. **Resuming a workflow into a different session.** `resumeFromRunId` is
   same-session only. If the CLI process dies, that run's remaining `agent()`
   calls can never be resumed by the harness — a new session can only re-launch
   the script (the journal *cache* is keyed by prompt, but the resume entry
   point is session-scoped).
4. **Controlling a session Touch did not start.** No writable channel exists
   (CONTROL-2). Read-only + `kill(pid)` is the complete achievable set.
5. **Undoing an aborted agent's file edits.** Not a harness concern at all
   (CONTROL-7); if Touch doesn't checkpoint, nobody does.

**Recommendation.** Put a "Control semantics" table in the plan's global
decisions section with exactly four rows — start / pause / restart / terminate —
each stating: the mechanism, the granularity (session vs run vs agent), what
happens to in-flight work, what happens on disk, and whether it is deterministic
or model-mediated. Every UI control must map to one row; anything that maps to
none gets cut from the README rather than half-built.
