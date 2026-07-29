# Touch

<p align="center">
  <img src="https://github.com/msdrx/touch/blob/main/resources/touch_the_agent.png" alt="The Creation of Adam — touch the agent" width="820">
</p>

Touch is a Claude Code plugin for **watching your agents work**. When a session
spawns subagents — a research pass, an implementation loop, a review fleet — the
terminal shows you almost nothing about them. Touch puts that run on a local web
page: which plans are running, what stage each loop is in, what the gates
decided, and what it all costs in tokens.

> **Alpha — v0.2.0.** Touch is early software: incomplete, moving, and rough in
> places. Interfaces, layout and command behaviour can change between releases
> without a migration path.

- **Local only.** Both servers bind `127.0.0.1` and print a URL carrying a
  per-boot token; every route but `/health` needs it.
- **Read-only on your machine.** Touch never writes to `~/.claude` — it only
  reads what the CLI already wrote. Its own history lives in `.touch/`.
- **It renders no button it cannot honour.** Nothing here starts, stops or
  restarts anything yet.

## The six commands

They land on your `PATH` when the plugin is enabled, and live in
`plugin/touch/bin/` otherwise.

| command | what it does |
|---|---|
| `touch-monitor` | serves the dashboard (port 8931): live plan cards, stages, gate verdicts, token counters |
| `touch-watcher` | daemon that turns a run's journal into dashboard events — this is what makes the page move |
| `touch-status` | appends one progress event; the line a script or an agent writes to say where it is |
| `touch-cycle-reporter` | writes one report per implement → test → critique cycle, so a finished run leaves a readable record |
| `touch-selfcheck` | eight PASS/FAIL checks of an installation, so "it doesn't work" becomes one failing line |
| `touch-serve` | the Touch page (port 8932) — **not implemented yet**: the backend behind it works and is tested, the page it serves is a placeholder |

`touch-monitor` is the page you actually use today. The plan is for
`touch-serve` to serve that same page over everything the aggregator sees, so
Touch ends up with one dashboard instead of two.

The plugin also ships **ten skills** ([listed below](#the-skills)), costing
~1,257 tokens of always-on context between them — a measured figure — and **one
hook**, `orch_scope_guard.py`, which keeps subagents inside their own run's
folder while a run is active and is inert when none is.

## Install

```
/plugin marketplace add msdrx/touch
/plugin install touch@msdrx-tools
```

Then `/reload-plugins`, and `touch-selfcheck` to verify. Touch installs
**disabled** because it carries a hook — enable it from `/plugin` after reading
the plugin's own [README](plugin/touch/README.md), which discloses what it
reads, writes and serves.

To try it without installing anything, from a clone of this repository:
`claude --plugin-dir plugin/touch`.

New releases do not arrive on their own: `/plugin marketplace update
msdrx-tools` → `/plugin update touch@msdrx-tools` → `/reload-plugins`.

## Use

**1. Run a loop.**

```
/touch:execute-research  <what you want researched>
/touch:implement-plan    <the plan it wrote>
```

**2. Watch it.** Runs driven by the skills start the daemons for you; by hand,
for one task:

```bash
TASK=$PWD/.claude/local-orchestrators/<task-name>
ORCH_STATE_DIR="$TASK" touch-monitor &   # the dashboard: http://127.0.0.1:8931
ORCH_STATE_DIR="$TASK" touch-watcher &   # feeds it from the run's journal
```

The wrappers refuse to open a public bind on your behalf. To reach a page from
another machine, forward the port: `ssh -L 8931:127.0.0.1:8931 you@host`.

### The skills

Four drive the loops the dashboard renders:

| skill | what it does |
|---|---|
| `/touch:execute-research` | parallel read-only researchers, then one synthesizer that writes a single complete plan |
| `/touch:implement-plan` | divide a plan by file ownership, then run gated implement → test → critique loops |
| `/touch:orchestrate` | the naming and control-file standards that make subagents visible to the dashboard |
| `/touch:m-orchestrator` | wire live monitoring into an orchestrator you write yourself |

Six are engineering practice, for the agents inside those loops:
`/touch:architecture-boundaries` (layering and dependency direction),
`/touch:architecture-tradeoffs` (a decision analysed as a trade-off, then
recorded), `/touch:code-quality-review` (`file:line` findings with fixes),
`/touch:pattern-selection` (the right design pattern — or the case against one),
`/touch:refactoring-pass` (behaviour-preserving cleanup with a test net), and
`/touch:testing-discipline` (tests, and testability read as an architecture
signal). They are condensed guidance derived from the books on each one's
`Sources:` line — not the works themselves.

## From this repository

Plain Python 3 stdlib, nothing to install. The code lives in `plugin/touch/`,
the shipping subtree and the only copy:

```bash
plugin/touch/bin/touch-monitor   # or any of the six wrappers
tests/run_all.sh                 # the tests; --keep-going reports every failure
```

For hacking on the aggregator itself there is one module-direct form:
`PYTHONPATH=plugin/touch python3 -m aggregator.server`.

Layout, ground rules, the release gate: [CONTRIBUTING.md](CONTRIBUTING.md).

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
