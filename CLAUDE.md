# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

**Touch has no application source yet.** The repo currently contains only
`README.md` (the product intent) and `.claude/` (orchestration skills + a
working live-monitoring module). The repo was just `git init`-ed on `master`
and has no commits yet — everything is untracked.

Per `README.md`, Touch is a web page for visualizing and managing subagents in a
Claude Code session, with two main components — **aggregator** and
**touch-visual**:

- main page: a terminal-styled web view over a Claude Code session (primary UI)
- left sidebar: list of such terminal sessions, click to open one
- a per-terminal page with n8n-like UML diagrams/graphs, plus controls to
  **pause, restart, start and terminate agent loops**

The "loops" it must control are exactly the ones defined by the
`execute-research` and `implement-plan` skills in `.claude/skills/`. Read those
before designing anything in Touch — they define the entities (task, plan,
sub-plan, agent, attempt, gate) the UI is meant to render and drive.

## What already exists in `.claude/` (and why it matters)

`.claude/shared/monitoring/` is a working, dependency-free (bash + Python 3
stdlib + browser) implementation of live orchestrator monitoring — effectively a
prior-art prototype of Touch's visual half. Full reference:
`.claude/shared/monitoring/monitoring.md`.

Data flow:

```
agents ──status.sh──┐
                    ├──> <task-dir>/events.jsonl ──> monitor_server.py ──ws──> monitor.html
Workflow journal ───┘        (append-only,          (HTTP + WebSocket)
  via decision_watcher.py     single source of truth)
```

- `status.sh <plan> <stage> <state> [detail]` — appends one JSON event line.
  Requires `ORCH_STATE_DIR`; falls back to the module dir with a stderr warning.
- `decision_watcher.py` — tails a Workflow run's `journal.jsonl`, derives
  spawn/verdict/retry/advance events and per-agent token accounting from the
  `[monitor] plan=… stage=… role=… attempt=…` marker embedded in every agent
  prompt. This marker is the **deterministic** event source — it works with zero
  LLM cooperation; `status.sh` calls inside agents are best-effort color only.
  Checkpointed in `.watcher-state.json` (restart-safe, never double-counts).
- `monitor_server.py` — serves `monitor.html` at `/`, streams events at `/ws`
  (full replay on connect, then live tail, `?task=<name>`), plus `/tasks`,
  `/artifacts?task=`, `/file?task=&path=` (extension-whitelisted, realpath
  contained), `/health`. One server serves all tasks; one watcher per task.
- Event schema, reserved ids (`plan` id `orchestrator`; stages `plan`,
  `complete`, `tokens`), and token-delta semantics are specified in
  `monitoring.md` — treat that file as normative.

State layout: the module is **stateless and task-agnostic** — never copy or
modify it per task. Per-run state lives in
`.claude/local-orchestrators/<task-name>/` (`events.jsonl`, `orch-config.json`,
`.watcher-state.json`, `orch-scripts/`, `findings/`, `plan/`, `report/`).

## The orchestration skill pair

`execute-research` → ONE complete plan file → `implement-plan` → implementation.

- `execute-research`: parallel read-only researchers (one per perspective,
  `opus`) with a barrier, then ONE `fable` synthesizer that writes
  `plan/<name>-plan.md` (global decisions + ordered items). Never partitions,
  never edits source.
- `implement-plan`: a `fable` divider derives isolated sub-plans by **file
  ownership** (one file, exactly one owner), then per sub-plan runs a gated
  loop — brand-new implementer each attempt → read-only test gate → read-only
  adversarial critique — until green or MAX_ATTEMPTS, then a final aggregate
  gate over the merged change-set. **Serial by default**; parallel only when
  explicitly asked and only for disjoint file ownership.
- Both skills' `templates/*.workflow.js` are the **normative protocol** (prompts,
  schemas, models, markers, status calls). Adapt a copy into the task folder's
  `orch-scripts/`; don't diverge from the invariants.
- Handoff between attempts is via `findings/<plan>-<gate>-attempt-<N>.md` file
  paths, not inlined text.
- Never resume/continue/`SendMessage` a prior agent — always a fresh subagent.

## Commands

Tests (stdlib only, no pytest, no runner — each file is executable and exits
non-zero on failure):

```bash
cd .claude/shared/monitoring/tests
python3 test_server.py      # monitor_server.py unit tests
python3 test_watcher.py     # decision_watcher.py unit tests
python3 test_shell.py       # status.sh + template/doc static guards
python3 test_frontend.py    # static source guards on monitor.html
```

All four pass. `test_frontend.py` asserts on `monitor.html` **source text** (the
fixed pattern present, the vulnerable one absent) because the HTML is never
executed by Python. `test_shell.py` includes a repo-root `.gitignore` check —
it must keep ignoring `.claude/shared/monitoring/events.jsonl` and
`.claude/shared/monitoring/.watcher-state.json`; per-task state under
`.claude/local-orchestrators/` stays tracked.

Run the monitoring stack:

```bash
TASK=$PWD/.claude/local-orchestrators/<task-name>
ORCH_STATE_DIR="$TASK" python3 .claude/shared/monitoring/monitor_server.py &    # port: argv > $ORCH_PORT > config > 8931
ORCH_STATE_DIR="$TASK" python3 .claude/shared/monitoring/decision_watcher.py &  # wf_dir: argv > $ORCH_WF_DIR > config > newest wf_*
```

Dashboard at `http://<host>:8931/`. This is a sandbox — ask the user to run
`sbx ports $SANDBOX_VM_ID --publish 8931:8931/tcp` on the host to reach it, and
bind any Touch dev server to `0.0.0.0`, not `127.0.0.1`.

## Rules that bite

- **Never delete a finished task folder or its `events.jsonl`** — completed runs
  are monitor history and replay on connect. There is no cleanup step. Wiping is
  only for a task you are actively re-running (stop daemons, delete
  `events.jsonl` + `.watcher-state.json`, re-seed, restart).
- Every `status.sh` call must set `ORCH_STATE_DIR`; a forgotten one dribbles a
  stray `events.jsonl` into the shared module dir.
- Never `pkill -f` these scripts from a command line that spells the script name
   — bracket the first letter: `pkill -f "[m]onitor_server"`.
- Keep event `detail` strings short, single-line, and free of double quotes.
- The `orch-config.json` files under `.claude/local-orchestrators/*/` point at
  `wf_dir` paths from a **different, earlier project** (`omnigent`). They are
  carried-over history — read them as examples, don't assume they describe this
  repo.
