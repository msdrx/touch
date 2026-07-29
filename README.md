# Touch

<p align="center">
  <img src="https://github.com/msdrx/touch/blob/main/resources/touch_the_agent.png" alt="The Creation of Adam — touch the agent" width="820">
</p>

Touch is a Claude Code plugin for **watching your agents work**. A session
that spawns subagents — a research pass, an implementation loop, a review
fleet — shows you almost nothing about them in the terminal. Touch puts that
activity on a local web page: which plans are running, what stage each loop
is in, what the gates decided, and what everything costs in tokens.

Touch never writes to `~/.claude` — it only reads what the CLI already
writes — and it keeps its own history under `.touch/` in your project,
optionally mirrored into a local MongoDB.

Version 0.2.0 is **read-only**: it renders no button it cannot honestly
honour, so nothing here starts, stops or restarts anything yet.

## What the plugin gives you

Six commands land on your `PATH` when the plugin is enabled:

| command | what it does | why it exists | status |
|---|---|---|---|
| `touch-monitor` | serves the monitoring page (port 8931): live plan cards, stages, gate verdicts and token counters for an orchestration run | a multi-agent run is unreadable from a terminal | works |
| `touch-watcher` | daemon that turns a Workflow run's journal into dashboard events | progress must not depend on agents remembering to report — the journal is deterministic | works |
| `touch-status` | appends one progress event from a script or an agent | the human-readable colour on top of the journal's facts | works |
| `touch-cycle-reporter` | writes one report per implement → test → critique cycle | so a finished run leaves a readable record, not just a log | works |
| `touch-selfcheck` | eight PASS/FAIL checks of an installation | so "it doesn't work" turns into one failing line you can act on | works |
| `touch-serve` | the Touch page (port 8932) | one page over everything the CLI writes, sessions included | **not implemented** |

**About `touch-serve`: not implemented.** The backend behind it (transcript
ingest, read API, the Mongo mirror) exists and is tested, but the page it
serves today is a bare v0 placeholder, not a dashboard anyone would use. The
plan is for `touch-serve` to serve **the same page as `touch-monitor`**,
extended over everything the aggregator sees, so Touch ends up with one
dashboard instead of two. Until then, `touch-monitor` is the page you
actually use.

The plugin also ships **ten skills** (invoked as `/touch:<name>`): four
orchestration skills that drive research → plan → implement loops and report
them to the dashboard, and six engineering-practice skills the agents inside
those loops draw on. They are listed under [Use](#use) below, and cost
~1,257 tokens of always-on context across all ten — a measured figure
(`claude plugin details touch`); the plugin's own
[README](plugin/touch/README.md) has the per-skill breakdown.

And **one hook**: `orch_scope_guard.py`, a `PreToolUse` guard that keeps
subagents inside their own run's folder while an orchestration run is active.
It is inert when no run is active.

## Install

From any Claude Code session:

```
/plugin marketplace add msdrx/touch-plugin
/plugin install touch@msdrx-tools
```

Then `/reload-plugins`. Touch installs **disabled** because it carries a
`PreToolUse` hook — enable it from `/plugin` after reading the plugin's own
[README](plugin/touch/README.md), which discloses what it reads, what it
writes, what it serves, and what the hook costs.

Want to try it without installing anything? Run it straight from a clone of
this repository, for one session:

```
claude --plugin-dir plugin/touch
```

### Verify the install

From the project you want to use it in:

```
touch-selfcheck
```

Eight checks, one `PASS`/`FAIL` line each. It exits non-zero on any failure
and ends with the command to run next.

## Use

The page you use today is the monitoring dashboard.

**1. Run an orchestration loop.** Ask for a plan, then have it implemented:

```
/touch:execute-research  <what you want researched>
/touch:implement-plan    <the plan it wrote>
```

**2. Watch it.** Runs driven by the skills start the dashboard daemons for
you; to start them by hand for a task:

```bash
TASK=$PWD/.claude/local-orchestrators/<task-name>
ORCH_STATE_DIR="$TASK" touch-monitor &   # the dashboard: http://127.0.0.1:8931
ORCH_STATE_DIR="$TASK" touch-watcher &   # feeds it from the run's journal
```

**Local only, by design.** Both servers bind `127.0.0.1` and print a URL
carrying a per-boot token; every route except `/health` requires it, and the
wrappers refuse to open a public bind on your behalf. To reach a page from
another machine, forward the port over SSH
(`ssh -L 8931:127.0.0.1:8931 you@host`) instead of exposing it.

### The skills

**Orchestration** — the loops the dashboard renders:

| skill | what it does |
|---|---|
| `/touch:execute-research` | parallel read-only researchers, then one synthesizer that writes a single complete plan |
| `/touch:implement-plan` | divide a plan by file ownership, then run gated implement → test → critique loops per sub-plan |
| `/touch:orchestrate` | the naming, spawn-ledger and control-file standards that make subagents visible to the dashboard |
| `/touch:m-orchestrator` | wire live monitoring into an orchestrator you write yourself |

**Engineering practice** — what the agents inside those loops are asked to do
well:

| skill | what it does |
|---|---|
| `/touch:architecture-boundaries` | module boundaries, layering and dependency direction |
| `/touch:architecture-tradeoffs` | a significant decision analysed as an explicit trade-off, then recorded |
| `/touch:code-quality-review` | review a diff, file or module and report `file:line` findings with fixes |
| `/touch:pattern-selection` | match a problem to the right design pattern — or argue against one |
| `/touch:refactoring-pass` | safe, incremental, behaviour-preserving cleanup with a test safety net |
| `/touch:testing-discipline` | write or restructure tests, and read testability pain as an architecture signal |

The six are condensed guidance derived from the books named on each one's
`Sources:` line — not the works themselves.

## Update / uninstall

Third-party marketplaces do **not** auto-update by default, so a new release
reaches you when you ask for it:

```
/plugin marketplace update msdrx-tools
/plugin update touch@msdrx-tools
/reload-plugins
```

To remove it, uninstall `touch` from `/plugin` → installed plugins, then
`/reload-plugins`. Beyond the plugin cache, the only things Touch leaves
behind are `.touch/` and any `.claude/local-orchestrators/<task>/` run
folders in the projects you used it in; delete those if you want the history
gone too.

## Running from this repository

The code is plain Python 3 stdlib — nothing to install. It lives in
`plugin/touch/`, the shipping subtree and the only copy, so run the wrappers
that know where that is:

```bash
plugin/touch/bin/touch-serve                 # binds 127.0.0.1:8932, prints the tokened URL
```

All six commands are on `PATH` in any session that has the plugin enabled,
and in `plugin/touch/bin/` otherwise. The one module-direct form, for hacking
on the aggregator itself, is
`PYTHONPATH=plugin/touch python3 -m aggregator.server`.

Tests — stdlib only, no pytest; every file is a standalone executable:

```bash
tests/run_all.sh              # both suites; --keep-going to see every failure
python3 tests/test_docs.py    # any single file runs alone
```

Development details — repository layout, the shipping subtree, the release
gate, how to upload a release to the marketplace — live in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Control verbs — planned, none shipped

Touch's end goal is managing agents, not just watching them. This table is
the whole planned vocabulary — every document, skill and UI element uses
these words with these meanings — and **nothing in it is shipped in v0**:

| verb | how it would work | determinism |
|---|---|---|
| **start** | Touch spawns the session it will own | deterministic |
| **terminate / kill** | escalation ladder on an owned session: `/exit` → SIGHUP the process group → SIGKILL (SIGTERM does not move the TUI) | deterministic, owned sessions only |
| **stop (graceful)** | ask the session to stop a loop; rendered `requested / pending — orchestrator busy / sent / confirmed` | model-mediated — a request, never an assumption |
| **restart** | re-invoke the workflow script with the stored partition (`subplans_file`) and `only:[ids]`: fresh agents, attempt numbering continues, the divide step skipped. `Workflow({resumeFromRunId})` is **not** restart — it replays agents without re-executing them | model-mediated |
| **pause** | does not exist as a CLI channel. The only honest form is a hook gate (a `PreToolUse` hook that holds its response), which is per-agent and takes effect at the next tool boundary. Probed and working (2026-07-26) but **not shipped**, and not rendered until it is | deferred |

A run-level stop (a whole Workflow loop) and a per-agent stop are different
things and are never conflated. The full ladder and the session classes live
in `plugin/touch/docs/control-semantics.md`.

## Optional: the Mongo mirror

Touch works with **no database at all**. The mirror is a write-behind copy of
history that already lives in files, fully rebuildable from them; when Mongo
is absent, down, or `pymongo` is not installed, the live view is unaffected
and `/health` says so. If you want it, `plugin/touch/docs/mongo.md` has the
exact recipe: the database binds loopback only (`127.0.0.1:27017`), with
auth, and its port is never exposed off the machine — the mirror holds the
same unredacted transcripts the token posture exists to protect.

**"Separate collections for separate session datas" — asked, and declined.**
Instead each entity type has one collection (sessions, records, agents, runs,
usage, …) and every document carries an indexed `sessionId`, so per-session
isolation is a filter, not a namespace. Per-session collections would turn
"all sessions, newest first" into an N-collection scan and duplicate every
index.

## Where the design lives

- `plugin/touch/docs/control-semantics.md` — the verb ladder and session
  classes.
- `plugin/touch/docs/mongo.md` — the database recipe and its security
  baseline.
- `inception.md` — everything verified about the substrate, summarized.
- `CLAUDE.md` — the session guide, and the authority ladder over the full
  design record (whose run folders are local history, absent from a clean
  checkout by design).
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to work on the code.

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
