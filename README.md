# Touch

<p align="center">
  <img src="resources/touch_the_agent.png" alt="The Creation of Adam — touch the agent" width="820">
</p>

Touch is a Claude Code plugin for **watching your agents work**. When a session
spawns subagents, the terminal shows you almost nothing about them. Touch puts that run on a local web
page: which plans are running, what stage each loop is in, what the gates
decided, and what it all costs in tokens.

It is read-only for now. Future releases will "touch" the agent.

> **Alpha — v0.2.2.** Touch is early software: incomplete, moving, and rough in
> places. Interfaces, layout and command behaviour can change between releases
> without a migration path.

## Quick start

### 1. Install it in Claude Code

Worth a minute first: the plugin's own [README](plugin/touch/README.md) is the
full disclosure of what Touch reads, writes and serves.

Inside Claude Code, run:

```
/plugin marketplace add msdrx/touch
/plugin install touch@msdrx-tools
/reload-plugins
```

Then open `/plugin` and **enable** Touch — it installs disabled because it
carries a hook, and its commands land on your `PATH` only while it is enabled.

Two footnotes. The `msdrx/touch` shorthand clones over SSH; without a key on
the machine, use the HTTPS URL — `/plugin marketplace add
https://github.com/msdrx/touch.git` — or set
`CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1`. And updates never arrive on their own:
`/plugin marketplace update msdrx-tools` → `/plugin update touch@msdrx-tools`
→ `/reload-plugins`.

(To try Touch without installing anything, from a clone of this repository:
`claude --plugin-dir plugin/touch`.)

### 2. Check it works

```
touch-selfcheck
```

Ten PASS/FAIL checks, one line each — all green means Touch is installed,
importable, and able to serve. Optional but recommended:
`touch-selfcheck --init` maps this project's Claude Code auto memory into
`<project>/.touch/memory`, which is what the dashboard's `/memory` page reads.

### 3. Run a research → implement loop

The two loop skills are the heart of it. Ask for research first:

```
/touch:research  how should rate limiting be added to this API?
```

Read-only researchers fan out over your code in parallel, then one synthesizer
writes a single complete plan file and tells you where it is. When you are
happy with the plan:

```
/touch:implement  implement the plan it wrote
```

A divider splits the plan by file ownership, then each sub-plan runs a gated
loop — a fresh implementer, then a read-only test gate, then a read-only
adversarial critique — until green or the attempt cap.

### 4. Watch it

Runs driven by the skills start the dashboard for you — look for the
`http://127.0.0.1:8931/?token=…` URL in the run output. To start it by hand
for one task:

```bash
TASK=$PWD/.touch/local-orchestrators/<task-name>
ORCH_STATE_DIR="$TASK" touch-monitor &   # the dashboard
ORCH_STATE_DIR="$TASK" touch-watcher &   # feeds it from the run's journal
```

Every page needs the per-boot token the server prints (every route but
`/health` does), and everything binds `127.0.0.1` only — the wrappers refuse
to open a public bind on your behalf. To reach a page from another machine,
forward the port: `ssh -L 8931:127.0.0.1:8931 you@host`.

## The commands

Seven commands, on `PATH` while the plugin is enabled (in `plugin/touch/bin/`
otherwise):

| command | what it does |
|---|---|
| `touch-monitor` | the run dashboard (port 8931): live plan cards, stages, gate verdicts, token counters — plus the `/memory` editor |
| `touch-watcher` | daemon that turns a run's journal into dashboard events — what makes the page move |
| `touch-serve` | the Touch page (port 8932): read-only view over every session the aggregator sees |
| `touch-run` | `start / bind / close / verify / status` — lays out a run folder, starts and stops Touch's own daemons, settles the run's cards; not a session verb |
| `touch-status` | appends one progress event to a run's stream |
| `touch-cycle-reporter` | one readable report per implement → test → critique cycle, plus the final run report |
| `touch-selfcheck` | ten install checks; `--init` maps auto memory into the project |

`touch-monitor` is still the page most runs are watched on. `touch-serve`'s
page is shipped and read-only — the aggregator's ingest tick fills its read
model, and `/health` reports it. The two pages are meant to converge into one.

## The skills

Four orchestration skills drive the loops the dashboard renders:

| skill | what it does |
|---|---|
| `/touch:research` | parallel read-only researchers → one complete plan |
| `/touch:implement` | divide the plan by file ownership → gated implement / test / critique loops |
| `/touch:orchestrate` | naming and control-file standards that make subagents visible to the dashboard |
| `/touch:monitor` | wire live monitoring into an orchestrator you write yourself |

Six more are engineering practice, for the agents inside those loops:
`/touch:architecture-boundaries`, `/touch:architecture-tradeoffs`,
`/touch:code-quality-review`, `/touch:pattern-selection`,
`/touch:refactoring-pass` and `/touch:testing-discipline` — condensed guidance
derived from the books on each one's `Sources:` line, not the works
themselves.

All ten cost ~1,261 tokens of always-on context between them — a measured
figure, re-read with `claude --plugin-dir plugin/touch plugin details touch`.
The two hooks that ship alongside cost no model context at all: a scope guard
that keeps a run's subagents in their own folder, and an additive recorder
that lets the dashboard see agent starts sooner. Both are inert when no run is
active.

## What it will not do

- **It never writes to `~/.claude`.** It reads what the CLI already wrote, and
  keeps its own history in `.touch/` inside your project. The one thing it
  writes for you is Claude Code's auto memory — and it does that by pointing
  the CLI at `<project>/.touch/memory`, never by reaching into `~/.claude`.
- **One write plane, off by default.** The dashboard can edit those memory
  files, but only when started with `--allow-memory-write`; everything else it
  serves is read-only.
- **No control it cannot honour.** No session verb ships — the table below is
  the planned vocabulary, and none of it exists yet.

## Control verbs — planned, none shipped

Managing agents is the end goal, not just watching them. This is the whole
planned vocabulary — every document, skill and UI element uses these words with
these meanings — and **none of it ships in v0**:

| verb | what it would mean | determinism |
|---|---|---|
| **start** | Touch spawns the session it will own | deterministic |
| **terminate / kill** | escalation on an owned session: `/exit` → SIGHUP the process group → SIGKILL (SIGTERM does not move the TUI) | deterministic, owned sessions only |
| **stop (graceful)** | ask a session to stop a loop; rendered `requested / pending / sent / confirmed` | model-mediated — a request, never an assumption |
| **restart** | re-invoke the workflow script with the stored partition and `only:[ids]`: fresh agents, the divide step skipped. `Workflow({resumeFromRunId})` is **not** restart — it replays agents without re-executing them | model-mediated |
| **pause** | no CLI channel exists for it; the only honest form is a hook gate, per-agent and effective at the next tool boundary — probed and working, but **not shipped** | deferred |

A run-level stop and a per-agent stop are different things and are never
conflated. Full ladder and session classes:
`plugin/touch/docs/control-semantics.md`.

## Optional: the Mongo mirror

Touch works with **no database at all**. The mirror is a write-behind copy of
history that already lives in files and is fully rebuildable from them; when
Mongo is absent, down, or `pymongo` is not installed, the live view is
unaffected and `/health` says so.

The recipe is in `plugin/touch/docs/mongo.md`: loopback bind
(`127.0.0.1:27017`), auth on, and the port never exposed off the machine — the
mirror holds the same unredacted transcripts the token posture exists to
protect.

"Separate collections for separate session datas" was asked for, and
**declined**. Each entity type gets one collection (sessions, records, agents,
runs, usage) and every document carries an indexed `sessionId`, so per-session
isolation is a filter rather than a namespace — otherwise "all sessions, newest
first" becomes an N-collection scan with every index duplicated.

## Where the design lives

- `plugin/touch/docs/control-semantics.md` — the verb ladder and session classes
- `plugin/touch/docs/mongo.md` — the database recipe and its security baseline
- `inception.md` — everything verified about the substrate, summarized
- `CLAUDE.md` — the session guide and the authority ladder over the full design
  record (whose run folders are local history, absent from a clean checkout)
