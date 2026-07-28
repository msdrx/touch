# Touch

<p align="center">
  <img src="https://github.com/msdrx/touch/blob/main/resources/touch_the_agent.png" alt="The Creation of Adam — touch the agent" width="820">
</p>

Touch shows you what the subagents in a Claude Code session are actually
doing: a session sidebar, the live agent tree, per-loop cards and running
token counters, served as a local web page over the transcripts the CLI
already writes. It ships as a Claude Code plugin, together with four
orchestration skills whose research → plan → implementation loops report to
the same dashboards that render them.

Touch never writes to `~/.claude`. It tails it, keeps its own history under
`.touch/` in your project (the CLI's retention sweep deletes transcripts), and
optionally mirrors that history into a local MongoDB.

Version 0.1.0 is **read-only**: it renders no button it cannot honestly
honour, so nothing here starts, stops or restarts anything yet.

## Install

From any Claude Code session:

```
/plugin marketplace add msdrx/touch-plugin
/plugin install touch@msdrx-tools
```

Then `/reload-plugins`. Touch installs **disabled** (`defaultEnabled: false`)
because it carries a `PreToolUse` hook — enable it from `/plugin` after
reading the plugin's own [README](plugin/touch/README.md), which discloses
what it reads, what it writes, what it serves, and what the hook costs. The
same file ships inside the plugin.

Want to read the code before registering anything? Run it straight from a
clone of this repository, for one session, with nothing installed:

```
claude --plugin-dir plugin/touch
```

### Verify the install

From the project you want to use it in:

```
touch-selfcheck
```

Eight checks, one `PASS`/`FAIL` line each — the Python 3.11 floor, the right
aggregator on the import path, the web assets, the project root, task state
resolving into your project (never into the plugin's own directory), a
bindable loopback port, intact exec bits, and one event surviving a real
write-and-read round trip. It exits non-zero on any failure and ends with the
command to run next.

## Use

```
touch-serve
```

It prints a loopback URL carrying a per-boot token —
`http://127.0.0.1:8932/?token=<per-boot token>` — and writes the same URL to
`.touch/server.json`. Open it and you get the sessions of the project you
started it in. Every route except `/health` requires the token; the server
binds `127.0.0.1` only, and the wrappers refuse to open a public bind on your
behalf. To reach the page from another machine, forward the port over SSH
(`ssh -L 8932:127.0.0.1:8932 you@host`) instead of exposing it.

The four skills invoke under the plugin's namespace:

| skill | what it does |
|---|---|
| `/touch:execute-research` | parallel read-only researchers, then one synthesizer that writes a single complete plan |
| `/touch:implement-plan` | divide a plan by file ownership, then run gated implement → test → critique loops per sub-plan |
| `/touch:orchestrate` | the naming, spawn-ledger and control-file standards that make subagents visible to the dashboard |
| `/touch:m-orchestrator` | wire live monitoring into an orchestrator you write yourself |

Orchestration runs get their own dashboard: `touch-monitor` (port 8931, same
loopback-and-token posture), plus `touch-status`, `touch-watcher` and
`touch-cycle-reporter`. The skills call these by name; you rarely need to.

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

The aggregator is plain Python 3 stdlib — nothing to install:

```bash
python3 -m aggregator.server                 # binds 127.0.0.1:8932
# prints:  open: http://127.0.0.1:8932/?token=<per-boot token>
#          token written to .touch/server.json (0600)
```

Every route except `/health` requires that per-boot token, and the WebSocket
upgrade enforces an Origin/Host allowlist.

Tests — stdlib only, no pytest; every file is a standalone executable:

```bash
tests/run_all.sh              # both suites; --keep-going to see every failure
python3 tests/test_docs.py    # any single file runs alone
```

Development details — repository layout, the shipping subtree, the release
gate, how to add a test — live in [CONTRIBUTING.md](CONTRIBUTING.md).

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

## Optional: the Mongo mirror

Touch works with **no database at all**. Mongo is a write-behind projection of
data that already lives in files, fully rebuildable from them; when it is
absent, down, or `pymongo` is not installed, the live view is unaffected and
`/health` says `mirror: absent | down | degraded`. Only history and backfill
degrade.

If you want it, `docs/mongo.md` has the exact recipe. Two rules from it, here
so nobody has to go looking: the database binds **loopback only**
(`-p 127.0.0.1:27017:27017`, `--auth`, a named volume) — Touch refuses to
mirror into a mongod with zero configured users — and the database port is
**never** published (no `sbx ports … 27017`, not "just for a minute"); the
mirror holds the same unredacted transcripts the token posture exists to
protect, so use `docker exec touch-mongo mongosh …` instead.

**"Separate collections for separate session datas" — asked, and declined.**
What you get instead is per-session *isolation*: one collection per entity type
(sessions, records, agents, runs, usage, …), each document carrying an indexed
`sessionId`/`sessionKey`, and per-session filtered queries. The reason is this
machine's own numbers: 6 transcripts and 7 session ids in one project already
means 7+ collections, the sidebar's "all sessions, newest first" becomes an
N-collection scan, and every collection duplicates every index. Nothing is
lost — the isolation you asked for is a filter, not a namespace.

## Where the design lives

- `docs/control-semantics.md` — the verb ladder and session classes.
- `docs/mongo.md` — the database recipe and its security baseline.
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
