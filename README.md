# Touch

A web page for visualizing and managing subagents in a Claude Code session.

Two components: **aggregator** (Python, reads `~/.claude` and this repo's run
folders, serves HTTP + WebSocket) and **touch-visual** (the page: session
sidebar, agent/loop views, n8n-like run graphs). The "loops" Touch renders are
exactly the ones the `execute-research` and `implement-plan` skills in
`.claude/skills/` define — task, plan, sub-plan, agent, attempt, gate.

Touch never writes to `~/.claude`. It tails it, keeps its own history under
`.touch/` (because the CLI's retention sweep deletes transcripts), and
optionally mirrors that history into a local MongoDB.

## What works today

| area | state |
|---|---|
| session discovery, transcript/journal ingest, agent + run graph, token rollups | implemented (`aggregator/`) |
| read API + WebSocket with bounded replay and `(stream, seq)` resume | implemented (`aggregator/server.py`) |
| touch-visual v0 — sidebar, agent tree, loop cards, live token counters | implemented, **read-only** |
| Mongo mirror (optional, write-behind, rebuildable) | implemented (`aggregator/mirror.py`, see `docs/mongo.md`) |
| control plane — start / stop / restart / terminate | **not shipped.** No control affordance renders in v0 |
| terminal-fidelity PTY view | not shipped (the transcript supports a semantic re-render, not a terminal) |

"Implemented" here means the module and its tests exist in this tree — the
suite is the authority, not this table: run `tests/run_all.sh` and believe it.

Nothing in the UI shows a control it cannot honestly perform. A degraded or
derived state is always labelled as one ("closed — no verdict", "archived —
source transcripts unavailable", "unknown — idle 7 m").

## Control verbs — the honest table

This is the whole vocabulary. Every document, skill, and UI element uses these
words with these meanings (GD-4); nothing here is shipped in v0.

| verb | how it would work | determinism |
|---|---|---|
| **start** | Touch spawns the session it will own | deterministic |
| **terminate / kill** | escalation ladder on an owned session: `/exit` → SIGHUP the process group → SIGKILL (SIGTERM does not move the TUI) | deterministic, owned sessions only |
| **stop (graceful)** | ask the session to stop a loop; rendered `requested / pending — orchestrator busy / sent / confirmed` | model-mediated — a request, never an assumption |
| **restart** | re-invoke the workflow script with the stored partition (`subplans_file`) and `only:[ids]`: fresh agents, attempt numbering continues, the divide step skipped. `Workflow({resumeFromRunId})` is **not** restart — it replays agents without re-executing them | model-mediated |
| **pause** | does not exist as a CLI channel. The only honest form is a hook gate (a `PreToolUse` hook that holds its response), which is per-agent and takes effect at the next tool boundary. Probed and working (2026-07-26) but **not shipped**, and not rendered until it is | deferred |

Two stop granularities, never conflated (GD-8): a **run-level** stop exists for
Workflow runs via the launch `toolUseResult.taskId` and stops the whole loop; a
**per-agent** stop exists only for Agent-tool spawns, where the task id is the
agent's own 17-hex id. A Workflow agent renders its per-agent stop disabled,
with that reason. Full ladder and session classes: `docs/control-semantics.md`.

## Running it

Two servers exist in this repo. They are different programs on **reserved**
ports — reserved by convention, not occupied by default; start what you need.

**Touch (port 8932)** — the aggregator + touch-visual:

```bash
python3 -m aggregator.server                 # binds 127.0.0.1:8932
# prints:  open: http://127.0.0.1:8932/?token=<per-boot token>
#          token written to .touch/server.json (0600)
```

Every route except `/health` requires that per-boot token, and the WebSocket
upgrade enforces an Origin/Host allowlist. To reach it from outside the
sandbox, opt in explicitly and publish the port from the **host**:

```bash
python3 -m aggregator.server --open --allow-origin http://<host>:8932
sbx ports "$SANDBOX_VM_ID" --publish 8932:8932/tcp     # run on the host
```

**Legacy orchestrator monitor (port 8931)** — the older, read-only dashboard in
`.claude/shared/monitoring/`, which is what live orchestration runs report to:

```bash
TASK=$PWD/.claude/local-orchestrators/<task-name>
ORCH_STATE_DIR="$TASK" python3 .claude/shared/monitoring/monitor_server.py &
ORCH_STATE_DIR="$TASK" python3 .claude/shared/monitoring/decision_watcher.py &
sbx ports "$SANDBOX_VM_ID" --publish 8931:8931/tcp     # run on the host
```

When a run ends, stop its watcher; leave its state files in place — a finished
task folder is history the dashboard replays.

Tests (stdlib only, no pytest — each file is a standalone executable):

```bash
tests/run_all.sh              # both suites; --keep-going to see every failure
python3 tests/test_docs.py    # just one file, like any other
```

## Optional: the Mongo mirror

Touch works with **no database at all**. Mongo is a write-behind projection of
data that already lives in files, fully rebuildable from them; when it is
absent, down, or `pymongo` is not installed, the live view is unaffected and
`/health` says `mirror: absent | down | degraded`. Only history and backfill
degrade.

If you want it, `docs/mongo.md` has the exact recipe. Two rules from it, here
so nobody has to go looking: the database binds **loopback only**
(`-p 127.0.0.1:27017:27017`, `--auth`, a named volume) — Touch refuses to
mirror into a mongod with zero configured users — and **`sbx ports` must never
publish 27017**. The mirror holds the same unredacted transcripts the token
posture exists to protect; use `docker exec touch-mongo mongosh …` instead.

**"Separate collections for separate session datas" — asked, and declined.**
What you get instead is per-session *isolation*: one collection per entity type
(sessions, records, agents, runs, usage, …), each document carrying an indexed
`sessionId`/`sessionKey`, and per-session filtered queries. The reason is this
machine's own numbers: 6 transcripts and 7 session ids in one project already
means 7+ collections, the sidebar's "all sessions, newest first" becomes an
N-collection scan, and every collection duplicates every index. Nothing is
lost — the isolation you asked for is a filter, not a namespace.

Storage is kept forever in v0 (no TTL index anywhere, by rule): sessions,
agents, runs, run_nodes, usage and custom state are all small; measured
baseline is 15.7 MB / 3 936 records (≈4 KB per record, ≈1.3 MB h⁻¹ per active
session), and the mirror costs about 0.53× the raw text on disk. Pruning is
revisited at the growth threshold, not before.

## Where the design lives

Authority ladder — read them in this order when they disagree:

1. `.claude/local-orchestrators/touch-mongo-live/plan/touch-mongo-live-plan.md`
   — the amendment (Mongo, live flow; GD-21…GD-30, R-38…R-58).
2. `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md`
   — the normative plan (GD-1…GD-20, R-01…R-37).
3. `.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md`
   — design law D1–D14, as amended above.
4. `inception.md` — the summary of everything verified about the substrate.
5. this README — intent and how to run it.
6. `CLAUDE.md` — the session guide for working in this repo.

Also: `docs/control-semantics.md` (verb ladder, session classes),
`docs/mongo.md` (database), `.claude/shared/monitoring/monitoring.md`
(legacy monitor event schema), and
`.claude/local-orchestrators/touch-full-recon/report/probes.md` (what was
probed, when, and with which command).

## Original intent (verbatim, 2026-07-25)

Kept unedited as the source of the requirement. Where its wording and the verb
table above differ, the table wins — it is the same intent with each verb's
honesty attached.

> This is Touch, a web page for visualizing and managing subagents in a Claude Code session.
> Touch have 2 main components, aggregator and touch-visual.
> main page shows terminal with terminal design. main terminal is web view over claude code
> session. that is main user interface. left sidebar shows such terminal sessions list, where we
> can click and windows opens that terminal. also there is page for current terminal, where we can see n8n like UML diagrams and graphs. but with one addition, we must have control in which
> we can pause, restart, start and terminate agents loops. about loops you can find in
> /execute-research and /implement-plan skills.
