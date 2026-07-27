# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository. It is the session guide — the bottom of the authority
ladder. When it disagrees with a plan, the plan wins; fix this file.

## Project status

Touch is a web page for visualizing and managing subagents in a Claude Code
session, with two components — **aggregator** (Python) and **touch-visual**
(the page). The read side is implemented; **no control verb ships yet**.

Repository layout:

| path | what it is |
|---|---|
| `aggregator/` | the Python package: `tailer.py`, `store.py`, `ws.py`, `sessions.py`, `ingest.py`, `legacy.py`, `agents.py`, `custom_state.py`, `refs.py`, `mongo_store.py`, `mirror.py`, `server.py`. One file, exactly one owner |
| `touch-visual/` | `index.html`, `app.js`, `style.css` — v0 is read-only; no control affordance renders |
| `tests/` | one standalone executable per module + `run_all.sh` + `fixtures/` (frozen corpora with a sha256 manifest) |
| `docs/` | `mongo.md` (database deployment/security), `control-semantics.md` (verb ladder, session classes) |
| `README.md` | intent, the honest verb table, how to run it |
| `inception.md` | everything verified about the substrate (CLI 2.1.220), summarized |
| `.claude/` | the orchestration skills, the shared monitoring module, and this repo's run history |

**Authority ladder (GD-3)** — highest first:

1. `.claude/local-orchestrators/touch-mongo-live/plan/touch-mongo-live-plan.md`
   — the amendment: Mongo + live flow (GD-21…GD-30, R-38…R-58).
2. `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md`
   — the normative plan (GD-1…GD-20, R-01…R-37).
3. `.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md`
   — design law D1–D14, as amended. **Not** an implementable plan any more.
4. `inception.md` → `README.md` → this file.

Cite **D8.1** (stack / stdlib-only, amended by GD-21) or **D8.2** (journal
`result` opaque, superseded) — a bare "D8" is ambiguous and means neither.

## Runtime dependency policy (GD-21)

Stdlib-only **on the ingest and serve critical path**. `pymongo` (pinned
`==4.17.0`, with `dnspython`) is the ONE permitted third-party runtime
dependency, importable **only** from `aggregator/mongo_store.py` and
`aggregator/mirror.py`, lazily. Its absence degrades the mirror to
`mirror: "absent"` in `/health`; it never fails startup, never breaks an agent,
never blocks a test. Every other module must import with no third-party
packages installed, and every Mongo test must skip cleanly with no reachable
mongod. `tests/test_stdlib_only.py` enforces this, exception included — do not
add a second dependency by analogy.

## What already exists in `.claude/` (and why it matters)

`.claude/shared/monitoring/` is a working, dependency-free (bash + Python 3
stdlib + browser) implementation of live orchestrator monitoring — the
prior-art prototype of Touch's visual half, and the substrate Touch inherits.
Full reference: `.claude/shared/monitoring/monitoring.md` (normative for its
event schema).

```
agents ──status.sh──┐
                    ├──> <task-dir>/events.jsonl ──> monitor_server.py ──ws──> monitor.html
Workflow journal ───┘        (append-only,          (HTTP + WebSocket)
  via decision_watcher.py     single source of truth)
```

- `status.sh <plan> <stage> <state> [detail]` — appends one JSON event line.
  Requires `ORCH_STATE_DIR`; falls back to the module dir with a stderr warning.
- `decision_watcher.py` — tails a Workflow run's `journal.jsonl` and derives
  spawn/verdict/retry/advance events plus per-agent token accounting from the
  `[monitor] plan=… stage=… role=… attempt=…` marker embedded in every agent
  prompt. That marker is the **deterministic** source — it works with zero LLM
  cooperation; `status.sh` calls inside agents are best-effort colour only.
  Checkpointed in `.watcher-state.json` (restart-safe, never double-counts).
- `monitor_server.py` — serves `monitor.html` at `/`, streams events at `/ws`
  (full replay on connect, then live tail, `?task=<name>`), plus `/tasks`,
  `/artifacts?task=`, `/file?task=&path=` (extension-whitelisted, realpath
  contained), `/health`. One server serves all tasks; one watcher per task.
- Both writers stamp `w` (`"agent"` / `"watcher"`) so every line's author is
  known; readers ignore unknown keys.

Other `.claude/` files worth knowing: `.claude/settings.json` (committed,
session-wide — currently just the status line) and `.claude/statusline.sh`
(which shells out to `jq`; that is a **status-line-only** exception and is not
a licence for `jq` anywhere in Touch's own code or tests).

The module is **stateless and task-agnostic** — never copy or modify it per
task. Per-run state lives in `.claude/local-orchestrators/<task-name>/`
(`events.jsonl`, `orch-config.json`, `.watcher-state.json`, `orch-scripts/`,
`findings/`, `plan/`, `report/`).

## The run folders — what each one actually is

Five folders, **all produced by this repo's own runs**. Every
`orch-config.json` on disk names a `wf_dir` under
`~/.claude/projects/-home-laniakea-Projects-touch/…/subagents/workflows/`, so
`wf_dir` is the join key from a task folder to its harness journal. (An earlier
version of this file claimed these were carried-over examples from a different
project — that was false; verified again 2026-07-26.) A `wf_dir` that no longer
exists means "archived — source transcripts unavailable", never "wrong repo".

| folder | what it was | state | authoritative artifact |
|---|---|---|---|
| `touch-repo-recon` | first recon of the repo + skills | complete | `findings/` (51 findings) |
| `touch-aggregator` | 6-perspective research → design law | complete | `plan/touch-aggregator-plan.md` (D1–D14) |
| `touch-monitor-spawn` | a v0 slice planned from conversation | **plan only, never run** | `plan/touch-monitor-spawn-plan.md` (historical) |
| `touch-full-recon` | 6-perspective re-recon | complete | `plan/touch-full-recon-plan.md` (**normative**) + `report/probes.md` |
| `touch-mongo-live` | Mongo/live-flow research, then this implementation pass | research complete, implementation in flight | `plan/touch-mongo-live-plan.md` (**amendment**) + `plan/touch-mongo-live-subplans.md` |

A `plan/` or `report/` directory may legitimately be empty — that is a
recognized kind ("plan only / never run"), not a broken folder, and it is why
empty ones carry a `.gitkeep`.

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
- **Role → model (GD-5):** researcher / implementer / test-gate / critic =
  **Opus 5 at effort xhigh**; synthesizer, divider, main terminal agent, final
  review = **Fable**. Effort caps stay ≤ xhigh.
- Both skills' `templates/*.workflow.js` are the **normative protocol** (prompts,
  schemas, models, markers, status calls). Adapt a copy into the task folder's
  `orch-scripts/`; don't diverge from the invariants.
- Handoff between attempts is via `findings/<plan>-<gate>-attempt-<N>.md` file
  paths, not inlined text.
- Never resume/continue/`SendMessage` a prior agent — always a fresh subagent.
- `.claude/skills/touch-orchestrate/SKILL.md` is the companion standard for
  spawning agents Touch can see and stop (hierarchical names, `[touch]` marker,
  spawn ledger, control-file loop).

Terminal events are part of the protocol, not a nicety: each plan ends with
`status.sh <plan> plan done` and the run ends with
`status.sh <run> orchestrator complete done "<summary>"`. A plan whose agents
all returned without a decisive verdict settles **done** ("closed — no
verdict"), **never `failed`** — the fabricated FAILED badge was a real defect
(R-58) and the rule that killed it must not be re-broken.

## Commands

Tests — stdlib only, no pytest, no runner; every file is executable and exits
non-zero on failure:

```bash
tests/run_all.sh                     # BOTH suites (Touch + monitoring), fail-fast
tests/run_all.sh --keep-going        # run everything, report every failure
tests/run_all.sh --list              # what would run, in order
python3 tests/test_docs.py           # or run any single file directly
```

`tests/run_all.sh` also runs the four monitoring-module tests
(`test_server.py`, `test_watcher.py`, `test_shell.py`, `test_frontend.py`),
because a green Touch suite over a red substrate would be a lie.
`test_frontend.py` and `test_touch_frontend.py` assert on **source text** (the
fixed pattern present, the vulnerable one absent) — the HTML/JS is never
executed by Python. `test_bootstrap.py` guards `.gitignore` and the git
bootstrap; `test_docs.py` guards the claims in the docs you are reading.

**Serve blocks — two different programs on reserved ports.** "Reserved" means
by convention, not occupied: start what you need.

```bash
# Touch (port 8932) — aggregator + touch-visual
python3 -m aggregator.server                   # binds 127.0.0.1:8932 (GD-13 default)
# every route but /health needs the per-boot token it prints; it is also
# written to .touch/server.json (0600). WS upgrade enforces an Origin/Host
# allowlist. To expose it, opt in AND publish from the host:
python3 -m aggregator.server --open --allow-origin http://<host>:8932
sbx ports "$SANDBOX_VM_ID" --publish 8932:8932/tcp      # on the host
```

```bash
# Legacy orchestrator monitor (port 8931) — read-only dashboard, this is what
# live orchestration runs report to
TASK=$PWD/.claude/local-orchestrators/<task-name>
ORCH_STATE_DIR="$TASK" python3 .claude/shared/monitoring/monitor_server.py &    # port: argv > $ORCH_PORT > config > 8931
ORCH_STATE_DIR="$TASK" python3 .claude/shared/monitoring/decision_watcher.py &  # wf_dir: argv > $ORCH_WF_DIR > config > newest wf_*
sbx ports "$SANDBOX_VM_ID" --publish 8931:8931/tcp      # on the host
```

Optional Mongo mirror (see `docs/mongo.md` for the recipe and the security
baseline):

```bash
python3 -c "import aggregator.mirror as m; raise SystemExit(m.main(['--check']))"
# --health / --rebuild / --backfill also exist; all print redacted JSON
```

## Rules that bite

- **When a run ends, stop its watcher; leave its state files in place.** The
  watcher also self-exits after the journal goes quiet AND a terminal
  `orchestrator complete` event lands, and the driver epilogue stops the
  daemons — but check. Orphaned watchers are why the commit gate is scoped:
  **no commit while a watcher whose `ORCH_STATE_DIR` is inside the paths being
  committed is writing** (GD-1 as amended). A watcher writing some *other*
  task's stream never blocks a commit. The mirror daemon follows the same
  lifecycle.
- **Every generated deliverable is stored in the repo, not only the claude.ai
  artifact store.** Any HTML artifact (report, diagram, dossier) and any
  research/analysis `.md` produced while working on a task must ALSO be written
  under `.claude/local-orchestrators/<task>/report/` (HTML) or `findings/`
  (`.md` notes) of the task it belongs to. The monitor's artifacts strip lists
  them automatically (`/artifacts`, depth ≤ 3, reports first) — that local copy
  is the durable record; publishing to claude.ai is a share mirror, never the
  storage. Workflow: write the file, `cp` it into the task folder, then publish.
- **Never delete a finished task folder or its `events.jsonl`** — completed runs
  are monitor history and replay on connect, and the Mongo `legacy:` key space
  is positional (`legacy:<task>#<line>`), so it *depends* on that rule. There is
  no cleanup step. Wiping is only for a task you are actively re-running (stop
  daemons, delete `events.jsonl` + `.watcher-state.json`, re-seed, restart).
- **Run scope guard**: while `.claude/local-orchestrators/ACTIVE` lists task
  names (one per line), the PreToolUse hook `.claude/hooks/orch_scope_guard.py`
  (registered in `.claude/settings.json`) denies SUBAGENT access to every
  unlisted task's folder except its `plan/` (the authority ladder lives
  there). The main terminal agent is never restricted; no ACTIVE file means
  the guard is inert. Drivers append their task's line at daemon start and
  delete only that line at close-out (m-orchestrator §4); a stale line only
  over-restricts — delete it.
- Every `status.sh` call must set `ORCH_STATE_DIR`; a forgotten one dribbles a
  stray `events.jsonl` into the shared module dir.
- Never `pkill -f` these scripts from a command line that spells the script name
   — bracket the first letter: `pkill -f "[m]onitor_server"`.
- Keep event `detail` strings short, single-line, and free of double quotes.
  The reason is **shell and JS-template embedding** — the detail travels through
  a bash argument and a JS template literal before it is ever JSON — plus the
  1 KB writer cap (GD-11). JSON itself would survive the quotes; the pipeline
  will not.
- **Never write under `~/.claude/`.** It is a read-only tap: not transcripts,
  not journals, not settings.
- **Never publish the mongod port.** No `sbx ports … 27017`, not "just for a
  minute" — the mirror holds the same unredacted transcripts the token posture
  protects. `docker exec touch-mongo mongosh …` from inside the sandbox instead.
- Mongo being down, absent, or unreachable is a **non-event**: the live view is
  memory-authoritative and unaffected; only history/backfill degrade, and
  `/health` says so.
