# research / intent — attempt 1

Perspective: **product intent and documentation truth**. Every claim in
`README.md`, `CLAUDE.md`, `inception.md`, `.gitignore` checked against what is
on disk and running **now** (2026-07-25, CLI 2.1.220, pid 622 session
`e423cd3c-…`).

## Verification log (what I actually ran)

- `git status` / `git log`: branch `master`, **zero commits**, everything
  untracked — CLAUDE.md:9-10 still true.
- Full file inventory of the repo (49 files); `ls` of
  `.claude/local-orchestrators/*`.
- All four monitoring test files executed: `test_server.py` (16 tests),
  `test_watcher.py`, `test_shell.py`, `test_frontend.py` — **all exit 0**.
  CLAUDE.md:97 "All four pass" is true.
- `cat ~/.claude/sessions/622.json`; compared `procStart` (`10028`) to
  `/proc/622/stat` field 22 (`10028`) — **match**, inception §3 liveness rule
  verified.
- Measured registry staleness live: `updatedAt` **214.9 s old** with
  `status:"busy"` while this very subagent was running — inception:62-64
  ("not a heartbeat", 863 s observed) **confirmed, same failure class**.
- `curl http://127.0.0.1:8931/health` → `{"status":"ok"}`;
  `curl .../tasks` → three tasks with derived statuses.
- `ps` for `[m]onitor_server` / `[d]ecision_watcher`: server pid 4614 and
  watcher pid 4929 have been up since **02:59** (>10 h).
- Parsed `touch-aggregator/events.jsonl` (590 events) for states, stages,
  agent ids and labels.
- Confirmed both `orch-config.json` `wf_dir` paths exist on disk.

Result: **the plan (`touch-aggregator-plan.md`) and `inception.md` are broadly
accurate about the substrate; `CLAUDE.md` is the stale artifact**, and
`README.md` promises one verb the research proved impossible.

---

## INTENT-1 — README promises "pause", which the research proved cannot exist; CLAUDE.md repeats it verbatim

**file:line** `README.md:6` (`we can pause, restart, start and terminate agents
loops`); restated at `CLAUDE.md:19`. Contradicted by `inception.md:141-148`
("**\"Pause\" does not exist** in any CLI channel — the harness's own pause is
kill with a different status label") and by
`.claude/skills/touch-orchestrate/SKILL.md:95-96` ("do not promise it").

**severity** blocker

**scenario** README is the only user-authored statement of product intent and
is therefore the acceptance criterion a reader falls back on. It is unamended
since inception. An implementer (or the synthesizer of the new plan) who takes
README literally ships a Pause button in v1 with nothing behind it; the honest
answer (`D7`: pause = a PreToolUse/SubagentStart **hook gate** that holds its
response, per-agent, effective only at the next tool boundary, owned sessions
only, **v1.5**) is buried 250 lines into a file README never references. This
is the single largest scope contradiction in the repo, and it is the one a
non-technical reader is most likely to insist on.

**recommendation** The new plan must open with an explicit
**intent-reconciliation table** mapping each README verb to its honest
implementation, deferral, or impossibility:
`start` → deterministic v1; `terminate` → deterministic v1 (escalation ladder
`/exit` → SIGHUP pgroup → SIGKILL); `stop loop` → model-mediated v1;
`restart` → see INTENT-9; `pause` → hook gate, v1.5, owned sessions only, "next
tool boundary" latency stated in the UI. Add a plan item that **edits
`README.md`** to record the reconciliation (README is 7 lines and pre-dates all
research; leaving it uncorrected guarantees the argument recurs). Until it is
edited, no UI element may be labelled "Pause".

---

## INTENT-2 — CLAUDE.md points a fresh session at the skills and never at the plan, inception, or the two new skills

**file:line** `CLAUDE.md:21-24` ("Read those before designing anything in
Touch") and the whole of `CLAUDE.md:26-82`. Nothing in `CLAUDE.md` mentions
`inception.md`, `.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md`
(903 lines, called "**the normative design document**" at `inception.md:10`),
`touch-monitor-spawn/plan/touch-monitor-spawn-plan.md` (229 lines), or the
skills `m-orchestrator` and `touch-orchestrate` — both of which exist on disk.

**severity** blocker

**scenario** `CLAUDE.md` is auto-loaded into every session; the plan is not.
A new session asked to "start building Touch" reads CLAUDE.md, is told the repo
holds only README + `.claude/`, and re-derives an architecture from scratch —
silently violating D1 (host, never attach), D4 (never write under `~/.claude/`),
D5 (`.touch/` state root), D8 (stdlib-only runtime, port 8932). This is not
hypothetical: CLAUDE.md's project-status section is written as if the
`touch-aggregator` research had never happened.

**recommendation** A plan item must rewrite `CLAUDE.md` with a **"Read this
first" pointer block**: the normative plan path, `inception.md` as the summary,
the v0 scoped plan, and the four skills with one line each. State the precedence
explicitly — **plan D1–D14 > inception.md > CLAUDE.md > README.md** — so future
conflicts resolve without re-litigation. Do this as an early item (alongside
T1/P1 scaffolding), not as the T23/P12 docs item at the end; it is a
correctness control on every agent spawned after it.

---

## INTENT-3 — CLAUDE.md's inventory of the repo is factually wrong

**file:line** `CLAUDE.md:7-10`

**scenario** It says "The repo currently contains only `README.md` (the product
intent) and `.claude/`". On disk right now the repo root also holds
`inception.md` (271 lines), `.gitignore` (37 lines) and `CLAUDE.md` itself. An
agent that trusts this sentence will not `ls` and will never open `inception.md`
— which is the fastest available briefing on the substrate.

**severity** major

**recommendation** Replace the sentence with a real root inventory
(`README.md`, `CLAUDE.md`, `inception.md`, `.gitignore`, `.claude/`) and keep
the still-true parts (no application source; `master`; zero commits — both
verified). Add a note that the inventory changes the moment T1/P1 lands
(`aggregator/`, `touch-visual/`, `tests/`).

---

## INTENT-4 — Both CLAUDE.md and inception.md claim the task folders are carried-over `omnigent` history; they are this repo's own runs

**file:line** `CLAUDE.md:127-130`; same claim at `inception.md:54-56`
("per-task run history (mostly carried-over `omnigent` examples)").

**severity** major

**scenario** Verified contents of both configs:

```
touch-aggregator/orch-config.json → wf_dir …/projects/-home-laniakea-Projects-touch/dd469822-…/subagents/workflows/wf_829e6f58-b2f
touch-repo-recon/orch-config.json → wf_dir …/projects/-home-laniakea-Projects-touch/e423cd3c-…/subagents/workflows/wf_455b348c-e17
```

Both paths **exist on disk** and both are under this repo's own project slug.
There is no `omnigent` directory anywhere in `.claude/local-orchestrators/`
(the only entries are `touch-aggregator`, `touch-monitor-spawn`,
`touch-repo-recon`). CLAUDE.md actively instructs the reader to *discount* these
files ("don't assume they describe this repo") — the inverse of the truth. An
agent following that instruction will not reuse the working config, will
re-derive `wf_dir`, and may re-seed a live task folder.

**recommendation** Delete the omnigent warning from `CLAUDE.md` and the
"mostly carried-over omnigent examples" clause from `inception.md:55`. Replace
with a short table of the three real task folders and what each is (completed
research + plan / plan-only from conversation / this recon run). Note that
`wf_dir` is per-run and must be re-pointed for a new run — that is the real
caveat, not provenance.

---

## INTENT-5 — inception says the research run is "complete"; the only machine record says it FAILED, and the cause is systemic

**file:line** `inception.md:235-246` ("The `execute-research` run for Touch is
**complete**"). Contradicted by the live monitor:

```
GET /tasks → {"name":"touch-aggregator", …, "status":"failed", …}
events.jsonl → 2026-07-25T03:16:40.028Z research plan failed "loop exited -> synthesis"
```

Root cause `.claude/shared/monitoring/decision_watcher.py:646`:
`st = "done" if state["decisive"].get(prev) else "failed"`.

**severity** major

**scenario** A research plan never produces a *gate verdict*, so
`state["decisive"]["research"]` is never set; when the synthesis plan's first
agent spawns, the watcher closes the research loop as **failed** by
construction. `monitor_server.py:114-117` then reports the whole task failed
because no driver ever emitted the final `orchestrator complete done` that
monitoring.md:103-108 says the driver SHOULD emit — and **no workflow script in
the repo emits it** (grepped: neither skill template nor either task copy
contains a `status.sh orchestrator complete` call). So *every* `execute-research`
run in this repo will be permanently recorded as failed, and Touch — whose
whole job is to render this history — will show a red run for the very research
that produced its plan. That is a D13 honesty violation baked into the data.

**recommendation** Two separate fixes, both belong in the new plan.
(a) **Doc**: `inception.md:235` must state the run completed *and* that its
monitor record reads `failed` for the reason above, so nobody "fixes" the plan
by re-running it. (b) **Code**: either the research templates emit
`status.sh orchestrator complete done "<summary>"` on the success path, or the
watcher must treat a barrier-driven plan transition with all agents resulted as
`done`, not `failed`. Touch's ingestion must additionally **not** inherit this
derivation: per D13, a plan that ended with every agent resulted and no verdict
renders as *"closed, no verdict"* — never as a fabricated failure.

---

## INTENT-6 — `.gitignore` has no `.touch/` entry, in a repo with zero commits

**file:line** `.gitignore:1-37` (no `.touch/`); required by
`touch-aggregator-plan.md:143-147` (D5) and scheduled only inside
`touch-aggregator-plan.md:339-347` (T1) / `touch-monitor-spawn-plan.md:71-73`
(P1).

**severity** major

**scenario** `git log` confirms **no commits exist**. The first real commit will
almost certainly be `git add -A`. If any code, test, or manual probe creates
`.touch/` before T1/P1's additive `.gitignore` edit lands, the commit swallows
`control.jsonl`, hook spools, per-session stores and (later) the PTY spool.
`inception.md:117` states plainly that this material contains **unredacted
secrets** from transcripts. The window is real because P3/P6 tests and the P12
e2e simulation all touch the store.

**recommendation** Make "`.gitignore` gains `.touch/`" the **first action of the
first implementation item**, before any module is created — and state in the
item that the edit is strictly additive (`test_shell.py:155-161` asserts the two
monitoring lines are present; verified it only checks substring presence, so
appending is safe). Consider also ignoring `.touch*` to catch
`TOUCH_STATE_DIR` variants. Independently, the new plan should record a
"no commit before `.gitignore`" gate, since the repo's zero-commit state makes
the first commit unusually dangerous.

---

## INTENT-7 — CLAUDE.md tells the reader to bind Touch to 0.0.0.0 on port 8931, with no token and no mention of 8932

**file:line** `CLAUDE.md:112-114` ("Dashboard at `http://<host>:8931/` … bind
any Touch dev server to `0.0.0.0`, not `127.0.0.1`"). Contradicted by
`inception.md:187-194` (Touch = **8932**, 8931 is the live monitor; the
0.0.0.0 bind is compensated by a per-boot 256-bit token on every route +
Origin/Host allowlist at WS upgrade) and `touch-aggregator-plan.md:406`.

**severity** major

**scenario** Verified live: `monitor_server.py` pid 4614 is serving 8931 right
now (`/health` → ok, `/tasks` → 3 tasks). A reader following CLAUDE.md binds
Touch to 8931 and either fails to start or shadows the monitor the docs also
tell them to keep running. Worse, CLAUDE.md gives the "bind 0.0.0.0" instruction
**without** the compensating control, and Touch's surface — unlike the
monitor's — serves transcripts (secrets) and executes control verbs.
`inception.md:192-193` notes the existing monitor already accepted a
cross-origin WS handshake; repeating the bind advice without the token
propagates exactly that bug class.

**recommendation** Correct `CLAUDE.md`: monitor = 8931, Touch = 8932
(`argv > $TOUCH_PORT > .touch/server.json > 8932`), publish with
`sbx ports $SANDBOX_VM_ID --publish 8932:8932/tcp`, and state that the 0.0.0.0
bind is **only** permissible together with the per-boot token + Origin/Host
allowlist. The new plan should make "no route serves anything before the token
check" an invariant of the server item rather than a later hardening item, and
should include a socket-level test that a tokenless request and a cross-origin
WS upgrade are both rejected.

---

## INTENT-8 — touch-orchestrate's stop mechanism cannot stop the loops README exists to control

**file:line** `.claude/skills/touch-orchestrate/SKILL.md:49-56` (spawn via the
Agent tool with `run_in_background`, record `taskId` in
`<task-dir>/state/spawn-ledger.jsonl`, stop via `TaskStop`) vs the loops it
layers on: `execute-research/templates/research.workflow.js:140,150` and
`implement-plan/templates/implement.workflow.js:172,179,189,256,333,343` — all
spawns are Workflow `agent()` calls, which are **not** background harness tasks
and expose **no `taskId`**.

**severity** major

**scenario** README:5-7 defines the controllable unit as "agents loops … you
can find in /execute-research and /implement-plan skills". Those loops are
Workflow-spawned. `touch-orchestrate` says it "layers on top of
`execute-research` / `implement-plan` (their invariants still apply)" (SKILL.md
line 9-10) but its only stop path is `TaskStop(taskId)` — which no Workflow
`agent()` spawn can supply. So a Stop button wired per the skill works for
ad-hoc Agent-tool spawns and silently does nothing for the exact loops the
product is about. This also compounds `inception.md:93-95`: Workflow spawns get
a 63-byte stub `.meta.json` with no description, so the `[touch] name=` marker
is their *only* identity channel.

**recommendation** The new plan must state, per spawn mechanism, which control
verbs are real:
*Agent-tool background spawn* → per-agent stop via `TaskStop` (deterministic
once the ledger exists); *Workflow `agent()` spawn* → **no per-agent stop**;
only the model-mediated whole-loop stop (typed instruction, `requested → sent →
confirmed`) and session terminate. Either amend `touch-orchestrate` to say its
ledger/stop path applies to Agent-tool spawns only, or add a plan item that
converts the skill templates to background Agent-tool spawns — do not leave the
skill implying coverage it does not have. Whichever way, the UI must gray out
per-agent Stop on Workflow-spawned nodes.

---

## INTENT-9 — "restart" is defined two incompatible ways

**file:line** `README.md:6` (one verb, "restart"). Definition A:
`inception.md:159-163` / plan D7 — typed
`Workflow({scriptPath, resumeFromRunId})`, same-session only, replayed agents
are *not* re-executed, preceded by a `git stash create` checkpoint.
Definition B: `touch-orchestrate/SKILL.md:83` and `:96` — "A stopped slot may be
re-run only as a fresh spawn with `attempt` + 1" / "stop + fresh-attempt restart
is the honest cycle".

**severity** major

**scenario** The two produce different observable outcomes for the same button:
A replays a whole run (cheap, nothing re-executes, needs a dirty-tree
checkpoint); B re-executes one slot at attempt N+1 (costly, new agentId, new
graph node `(name, attempt)`). Since `execute-research`/`implement-plan`
explicitly forbid resuming a prior agent, definition B is the one consistent
with the skill invariants — but D7 is the binding plan decision. Left
unreconciled, the implementer picks one and the UI's "restart" means whatever
that sub-plan's owner assumed.

**recommendation** Split the verb in the plan and the UI: **"Restart run"**
(Workflow resume, D7, with the stash checkpoint and a "replayed, not
re-executed" badge) and **"Retry agent"** (fresh spawn, `attempt`+1, new node).
Amend README's single "restart" accordingly in the same doc item as INTENT-1.

---

## INTENT-10 — The session registry lists only LIVE sessions, so the sidebar's "list of sessions" cannot come from it alone

**file:line** `inception.md:60-61` ("one file per CLI process"); README:4
("left sidebar shows such terminal sessions list").

**severity** minor

**scenario** Verified: `~/.claude/sessions/` contains exactly **one** file
(`622.json`, this session) while
`~/.claude/projects/-home-laniakea-Projects-touch/` holds **three** transcripts
(`dd469822-…`, `e144bb01-…`, `e423cd3c-…`). Ended sessions are removed from the
registry. D3 (`inception.md:223`) keys sessions on `(pid, procStart)` — a key
that **does not exist** for a session discoverable only through its transcript.
A sidebar built on the registry shows one row and hides all history; a sidebar
built on transcripts alone has no liveness and no pid.

**recommendation** The plan's session-discovery item must specify the **union**
explicitly: live sessions from the registry keyed `(pid, procStart)`, historical
sessions from `projects/<cwd-slug>/*.jsonl` keyed by `sessionId`, with the two
reconciled when a registry entry names a sessionId. It must also state that
`sessionId` is *not* a stable session key (`/clear` mints a new one under the
same pid, `inception.md:65-67`), so a historical row may represent a fragment of
a session, and that historical rows carry no liveness and therefore no controls.

---

## INTENT-11 — `~/.claude` root and the repo live under different users; the sessions dir contains a non-JSON entry

**file:line** `inception.md:60` / `:69` (`~/.claude/sessions/<pid>.json`,
`~/.claude/projects/<cwd-slug>/`)

**severity** minor

**scenario** Verified in this sandbox: `HOME=/home/agent`, `whoami=agent`, but
the repo is `/home/laniakea/Projects/touch` and the project slug is therefore
`-home-laniakea-Projects-touch`. The slug derives from **cwd**, not from `$HOME`
— an implementation that builds the slug from the home path (an easy mistake
when the two usually coincide) finds nothing. Separately,
`ls ~/.claude/sessions/` shows a **`lost+found` directory** (mode 0700, 16 KB)
alongside `622.json` — the dir is a mount point. A naive `glob("*")` +
`json.load` over the registry raises on it.

**recommendation** State both facts in the discovery item: slug is derived from
the **session's `cwd`** (registry field) or from the scanned project directory
name, never from `$HOME`; and registry enumeration must be
`glob("*.json")` + `is_file()` + tolerate torn/partial JSON (retry once, keep
last good — already in P3, but the non-JSON-entry case is not). Add a fixture
with a `lost+found` directory to the discovery test.

---

## INTENT-12 — The per-task layout in CLAUDE.md is described as fixed; one task folder does not match it and the monitor reports it "empty"

**file:line** `CLAUDE.md:58-61`

**severity** minor

**scenario** CLAUDE.md lists the layout as `events.jsonl`, `orch-config.json`,
`.watcher-state.json`, `orch-scripts/`, `findings/`, `plan/`, `report/`.
Actual: `touch-monitor-spawn/` contains **only** `plan/` (it was written from a
conversation, not by a workflow), and the live monitor consequently reports
`{"name":"touch-monitor-spawn","events":false,"status":"empty"}`.
`touch-aggregator/` has no `report/`. An agent asked to "run the monitor over
the v0 plan task" will find nothing and may conclude the plan is invalid, or
may re-seed the folder.

**recommendation** Reword the layout as "may contain"; add one line that
`touch-monitor-spawn/` is plan-only by design and has no run history. Touch's
own task list must render a plan-only folder as a *plan artifact*, not as an
empty/failed run — same D13 honesty rule as INTENT-5.

---

## INTENT-13 — The "deterministic" marker loses parallel-sibling identity: six researchers rendered as one agent

**file:line** `.claude/shared/monitoring/decision_watcher.py:636-638`
(`"id": agent_id[:8], "label": f"{info['role']} #{info['attempt']}"`);
doc claim at `CLAUDE.md:46-48` and `inception.md:44-47` that the `[monitor]`
marker is the deterministic naming source.

**severity** minor

**scenario** Measured over `touch-aggregator/events.jsonl`: label
`'research #1'` covers **six distinct agent ids** — `a2ec1069`, `a2fc883c`,
`a74f0c93`, `a79fa2f4`, `a82d2e25`, `a9eabf26` — because all six perspectives
share `role=research, attempt=1`. Per-perspective identity survived **only** in
the best-effort `status.sh` stream (stages `sessiondata`, `agentgraph`,
`liveio`, `control`, `priorart`, `stack`, 4 events each) — i.e. exactly the
channel the docs call "best-effort color". So the deterministic source is
*not* sufficient for the node naming Touch's graph needs.

**recommendation** This is the empirical justification for making the
`[touch] name=` marker (`touch-orchestrate/SKILL.md:39-43`) **mandatory rather
than advisory** in the new plan: `name` is per-slot and unique, `role+attempt`
is not. The plan should say so with this evidence, and Touch's graph must key
nodes on `name` (falling back to `agentId`), never on `role#attempt`. Also
worth a line in `monitoring.md` so the limitation is not rediscovered.

---

## INTENT-14 — inception's token figure for the research run understates it by ~27x

**file:line** `inception.md:235-236` ("7 agents, **~1.09M tokens**")

**severity** minor

**scenario** The live monitor's rollup for the same task reads
`tokens: {in: 29,540,374, out: 316,233, cached: 28,313,811,
cache_write: 1,220,054}`. The synthesizer **alone** consumed 1.14M in / 44.3k
out (last `tokens` event). "~1.09M" appears to be one agent's figure presented
as the whole run's. Anyone sizing the cost of re-running research — a decision
the new plan may well have to make — will be off by more than an order of
magnitude.

**recommendation** Correct the figure to the rollup (≈29.5M in, of which
≈28.3M cached read, 316k out) and say which number it is. Generally: whenever a
doc quotes a metric, quote the source event/endpoint alongside it — this is the
same class of error as `toolUseResult.totalTokens` under-reporting 14x
(`inception.md:111-113`), which the research already caught once.

---

## INTENT-15 — A watcher daemon for a finished run has been live for 10+ hours; nothing in the docs says to stop one

**file:line** `CLAUDE.md:116-125` ("Rules that bite" — covers never-delete and
safe `pkill`, but not shutdown)

**severity** nit

**scenario** `ps` shows `decision_watcher.py` pid 4929
(`ORCH_STATE_DIR=…/touch-aggregator`) running since **02:59**, against a run
that ended at 03:26 — alongside pid 13643 for the current recon run. Because
the docs (correctly) forbid deleting task folders and say nothing about
stopping daemons, watchers accumulate one per run for the life of the sandbox,
each tailing a dead journal. Harmless today; not harmless once Touch adds a
per-session tailer with the same lifecycle.

**recommendation** Add a "when a run ends, stop its watcher (`pkill -f
'[d]ecision_watcher'` scoped by `ORCH_STATE_DIR`), never delete its state" line
to CLAUDE.md. For Touch, make daemon/tailer lifecycle an explicit plan concern:
a tailer whose session is gone (`/proc/<pid>` absent or `procStart` mismatch)
must exit rather than poll forever, and the plan should say so in the discovery
/ tailing item.

---

## INTENT-16 — CLAUDE.md omits the two `.claude/` files that shape the session itself

**file:line** `CLAUDE.md:26-31` (inventory of `.claude/`)

**severity** nit

**scenario** `.claude/settings.json` (configures a `statusLine` command) and
`.claude/statusline.sh` exist and are untracked like everything else, but
CLAUDE.md's `.claude/` inventory lists only `skills/` and `shared/monitoring/`.
An agent auditing "what runs in this repo" misses a command the harness executes
on every render — and Touch, which will read `~/.claude/settings.json` and ship
its own hook pack (`inception.md:270`, plan T10 / P10), needs to know a
project-level settings file is already in play so its hook installer merges
rather than overwrites.

**recommendation** Add both files to the CLAUDE.md inventory, and add an
explicit invariant to the hook-pack plan item: **merge into existing
`.claude/settings.json`, never replace it**, with a test that an unrelated
pre-existing key (`statusLine`) survives installation.

---

## Summary of what is authoritative vs stale

**Authoritative (verified true now)**
- `touch-aggregator-plan.md` D1–D14 and its substrate facts — every claim I
  spot-checked (registry non-heartbeat, `procStart` = `/proc` field 22, CLI
  2.1.220, transcript layout, subagent meta split) held up.
- `inception.md` §§1–6 as an accurate summary of that plan, and all of its
  cross-file pointers (`driver-context.md`, both plan files, the skill) resolve.
- `CLAUDE.md`'s monitoring section (data flow, `status.sh` contract, endpoints,
  reserved stages) and its test claims — all four suites pass.
- The "never delete a finished task folder / never write under `~/.claude/`"
  rules.

**Stale or wrong — must be corrected by the new plan**
- `CLAUDE.md:7-10` repo inventory (INTENT-3); `CLAUDE.md:127-130` +
  `inception.md:54-56` omnigent provenance (INTENT-4);
  `CLAUDE.md:112-114` port/bind guidance (INTENT-7); CLAUDE.md's silence on
  the plan, inception, and the two newer skills (INTENT-2);
  `inception.md:235` run status (INTENT-5) and token figure (INTENT-14).

**Unreconciled contradictions the synthesizer must decide, not inherit**
- pause (INTENT-1), restart (INTENT-9), which spawn mechanism the stop path
  actually covers (INTENT-8), and whether node identity comes from
  `[touch] name=` or `role#attempt` (INTENT-13).

**Binding constraints an implementation must honor** (all re-verified):
port 8931 is occupied by the live monitor → Touch takes 8932; bind `0.0.0.0`
**only** with the per-boot token + Origin/Host allowlist; `.touch/` must be
gitignored **before** anything creates it (zero-commit repo); never write under
`~/.claude/`; never delete a finished task folder or its `events.jsonl`; every
`status.sh` call sets `ORCH_STATE_DIR`; additive-only `.gitignore` edits
(`test_shell.py` guards two lines); stdlib-only runtime, no network from the
page.
