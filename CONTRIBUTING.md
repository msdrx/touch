# Contributing to Touch

How to get Touch running from a checkout, what each piece of it does and why,
and what a change needs before it lands.

The short version: plain Python 3 stdlib, nothing to install, no build step,
tests are standalone executables, the docs are tested too, and `plugin/touch/`
is the canonical home of everything Touch ships — you edit it in place, because
there is nowhere else to edit.

---

# Part 1 — Getting it running

## Step 1. Check the prerequisites

| you need | why |
|---|---|
| Python **3.11+** | the aggregator and every daemon; older versions are refused with a clear message, not a traceback |
| bash, git, coreutils | the wrappers, the event writer and the test runner are shell |
| Claude Code CLI | only if you want to use Touch as a plugin or run the skills; the servers work without it |

Optional, only for the Mongo mirror: a local `mongod` (or Docker) and
`pymongo==4.17.0`. Skip it — everything works without a database.

## Step 2. Get the code

```bash
git clone https://github.com/msdrx/touch.git
cd touch
```

## Step 3. Install dependencies — there are none

This is not a shortcut in the instructions; it is the design:

- **No `pip install`.** The runtime and the test suite import the standard
  library only. The single exception is `pymongo`, imported lazily by two files
  and never on the ingest or serve path.
- **No build step.** The Python runs from source, and the web page is three
  static files (`index.html`, `app.js`, `style.css`) served as they are — no
  bundler, no transpiler, no `node_modules`.
- **No virtualenv needed.** Nothing is installed, so there is nothing to
  isolate. Use one if you like.

The "build", when you package a release, is `git archive` of the committed
tree — see [Releasing](#releasing).

## Step 4. Verify the checkout

```bash
plugin/touch/bin/touch-selfcheck
```

Eight PASS/FAIL lines — interpreter version, the package importing *from this
tree*, the web assets, project-root resolution, task-state resolution, a
loopback bind, the exec bits on `bin/`, and one event through a real write/read
round trip. It exits non-zero on any failure and ends with the command to run
next:

```
PASS  python3 3.13.7 (needs 3.11+) at /usr/bin/python3
PASS  aggregator 0.1.0 imported from this tree (…/plugin/touch/aggregator/__init__.py)
…
8 checks: all passed
```

## Step 5. Run the tests

```bash
tests/run_all.sh                # fail-fast
tests/run_all.sh --keep-going   # run everything, report every failure
tests/run_all.sh --list         # what would run, in order
python3 tests/test_store.py     # any single file runs alone
```

Both suites run — Touch's `tests/test_*.py` and the monitoring module's
`tests/monitoring/test_*.py` — because a green Touch suite over a red substrate
would be a lie. Note that `--keep-going` reports failures in its summary; read
the summary rather than trusting the exit status alone.

## Step 6. Run Touch locally

```bash
plugin/touch/bin/touch-serve
```

It binds `127.0.0.1:8932` and prints a URL carrying a per-boot token:

```
http://127.0.0.1:8932/?token=<per-boot token>
```

Open that URL. Every route except `/health` needs the token, so a URL without
one gets you a 401 rather than a page. The same URL is written to
`.touch/server.json` (mode 0600) in your project, which is where a script
should read it from:

```bash
curl -s http://127.0.0.1:8932/health | head -c 200        # no token needed
python3 -c "import json;print(json.load(open('.touch/server.json'))['url'])"
```

`--port`, `--tasks-root` and `--assets` are accepted; `touch-serve --help`
lists everything. The wrapper **refuses** `--open` and any non-loopback
`--host`, so exposing the port is something you do deliberately, by invoking
the module yourself.

## Step 7. Watch a real orchestration run

The dashboard on port 8931 is the page that works today. Point it at one task
folder:

```bash
TASK=$PWD/.claude/local-orchestrators/<task-name>
ORCH_STATE_DIR="$TASK" plugin/touch/bin/touch-monitor &   # http://127.0.0.1:8931
ORCH_STATE_DIR="$TASK" plugin/touch/bin/touch-watcher &   # feeds it the run's journal
```

To stop them, bracket the first letter of the pattern so `pkill` does not match
its own command line:

```bash
pkill -f "[m]onitor_server"
pkill -f "[d]ecision_watcher"
```

## Step 8. Use it as a plugin

To dogfood exactly what a consumer installs, from the repo root:

```bash
claude --plugin-dir plugin/touch
```

The six wrappers are then on `PATH` as bare command names, and the skills are
available as `/touch:<name>`.

## Hacking on the module itself

`touch-serve`, `touch-monitor`, `touch-watcher`, `touch-status` and
`touch-cycle-reporter` are the supported entry points — one per program. The
one sanctioned module-direct invocation *of the server* is:

```bash
PYTHONPATH=plugin/touch python3 -m aggregator.server
```

There is no `aggregator/` at the repo root, which is why the `PYTHONPATH` is
required and why the docs omit the `python3 -P` that `touch-serve` prints
(`-P` keeps the cwd off `sys.path`; here there is nothing in the cwd left to
shadow the package). The Mongo mirror is the other directly-runnable module —
an operator tool with no wrapper by design; `CLAUDE.md` carries its line.

---

# Part 2 — The features, and why each one exists

## The commands

### `touch-monitor` — the dashboard
**What.** One HTTP + WebSocket server for every task under the project's tasks
root, serving the live page: plan cards, stages, gate verdicts, token counters.
**Why.** A multi-agent run is unreadable from a terminal — the output of ten
agents interleaves into noise, and the one thing you want ("which loop is on
which attempt, and what did the gate say") is exactly what scrolls away.
**How.** `touch-monitor [port]`; the port resolves argv → `$ORCH_PORT` → the
task's config → 8931. It replays the whole event stream on connect, then tails
it live. It binds loopback always: `--open` and a non-loopback `$ORCH_BIND` are
both refused, because no wrapper opens a port on your behalf.

### `touch-watcher` — the deterministic feed
**What.** A daemon that tails a Workflow run's `journal.jsonl` and derives
spawn / verdict / retry / advance events plus per-agent token accounting.
**Why.** Progress must not depend on agents remembering to report. The journal
is a fact the harness writes whether or not a model cooperates; a `[monitor]
plan=… stage=… role=… attempt=…` marker in every agent prompt is what joins a
journal entry to a plan card.
**How.** `ORCH_STATE_DIR=<task-dir> touch-watcher [<wf_dir>]` — one watcher per
task. It checkpoints in `.watcher-state.json`, so a restart never double-counts,
and it self-exits once the journal goes quiet and a terminal orchestrator event
lands.

### `touch-status` — one event
**What.** Appends exactly one JSON line to a task's `events.jsonl`.
**Why.** Human-readable colour on top of the journal's facts: the stage names
and short details a person actually reads. It is a bare command name rather
than a path so a PATH lookup survives a plugin update — a baked cache path does
not.
**How.** `ORCH_STATE_DIR=<task-dir> touch-status <plan> <stage> <state>
[detail…]`. Keep `detail` short, single-line and free of double quotes: it
travels through a bash argument and a JS template literal before it is ever
JSON, and there is a 1 KB cap. Always set `ORCH_STATE_DIR`, or the event lands
in whatever task folder the writer resolves instead.

### `touch-cycle-reporter` — the per-cycle record
**What.** Renders one report per implement → test → critique cycle and emits
each loop-terminal verdict.
**Why.** So a finished run leaves something readable behind instead of a log
you have to reconstruct — and so the verdict is written by a deterministic
renderer rather than a model asked to summarize itself.
**How.** `ORCH_STATE_DIR=<task-dir> touch-cycle-reporter <wf_dir> [--once]
[--interval=N]`. It fronts `cycle_reporter.py`, which ships inside the
`implement-plan` skill it serves.

### `touch-selfcheck` — does it work here
**What.** Eight PASS/FAIL checks of an installation.
**Why.** The plugin ships no test suite (fixtures and a git checkout do not
belong in a payload), but the eight things that actually break on someone
else's machine are cheap to check — so "it doesn't work" turns into one failing
line you can act on.
**How.** `cd <your project> && touch-selfcheck`. It writes nothing except one
throwaway event in a temp directory it removes. Its report ends with a sentinel
so a crash halfway through can never be summarized as a pass.

### `touch-serve` — the aggregator, and the page that isn't finished
**What.** The read/serve side: harness ingest, the event store, the read API,
the WebSocket, and the `touch-visual` page on port 8932.
**Why.** One page over everything the CLI writes — sessions included — rather
than one dashboard per run.
**How.** `touch-serve [--port P] [--tasks-root DIR] [--assets DIR]`. The
backend is implemented and tested; **the page it serves is a v0 placeholder**.
Use `touch-monitor` for real work. The plan is for `touch-serve` to serve that
same page, extended over everything the aggregator sees.

## Under the hood

### Harness ingest
**What.** `ingest.py` owns the `~/.claude` file formats — transcripts,
Workflow journals, snapshots, spills — and turns them into records, usage,
runs and run-nodes.
**Why.** Touch shows you what the CLI already wrote; it never asks a model for
its own status.
**How.** Everything is read through `tailer.py`: incremental, restart-safe,
cutting at the last newline so a torn tail is deferred rather than parsed, with
checkpoints keyed to their source. **Touch never writes under `~/.claude`** —
it is a read-only tap.

### The event store (`.touch/`)
**What.** Touch's own append-only stream, `touch-events-v2` — the system of
record.
**Why.** The CLI's retention sweep deletes transcripts and whole subagent
trees. History Touch does not own is history Touch loses.
**How.** One JSON line per event with a fixed key order, `flock`'d appends, one
`write()` per batch, and a per-file `seq` — so a cursor is `(stream, seq)` and
a resume has neither a gap nor a duplicate.

### The read API and the WebSocket
**What.** A static `(method, route) → handler` table: `/health`,
`/api/sessions`, `/api/session/timeline`, `/api/events`, `/api/run/graph`,
`/api/run/node`, `/api/toolresult`, `/api/tasks`, `/api/artifacts`,
`/api/query`, `/file`, plus the three asset routes and `/ws`.
**Why.** The routes are split into read and control *groups* even though the
control group is empty: a control endpoint should arrive into a group that
already has a name, not into a flat table where "is this a control?" is
answered by reading the handler.
**How.** No prefix match, no regex, no default handler — an exact pair or a
404. `CONTROL_ROUTES` is empty in v0 and a test asserts it.

### The monitoring substrate (`plugin/touch/shared/monitoring/`)
**What.** Five files — `status.sh`, `monitor_server.py`, `decision_watcher.py`,
`monitor.html`, `monitoring.md` — the working prototype Touch's visual half
inherits.
**Why.** It already solved live orchestration monitoring with bash, stdlib
Python and a browser. `monitoring.md` is normative for its event schema.
**How.** Stateless and task-agnostic: never copy or modify it per task. All
per-run state lives in `.claude/local-orchestrators/<task-name>/`.

## What ships alongside

### The ten skills
**What.** Four orchestration skills (`execute-research`, `implement-plan`,
`orchestrate`, `m-orchestrator`) and six engineering-practice skills, all
invoked as `/touch:<name>`.
**Why.** The orchestration pair is the loop the dashboard renders:
`execute-research` → ONE complete plan → `implement-plan` → gated
implement/test/critique loops divided by file ownership. The other six are what
the agents inside those loops are asked to do well.
**How.** The two loop skills carry a `templates/*.workflow.js` that is the
normative protocol — prompts, schemas, models, markers, status calls. Adapt a
copy into the task folder's `orch-scripts/`; do not diverge from the
invariants. All ten skills cost ~1,257 tokens of always-on context between
them, a measured figure.

### The scope-guard hook
**What.** `orch_scope_guard.py`, a `PreToolUse` hook.
**Why.** While a run is active, a subagent that wanders into another run's
folder can read or overwrite work it knows nothing about.
**How.** While `.claude/local-orchestrators/ACTIVE` lists task names, the hook
denies subagent access to every unlisted task's folder except its `plan/`. The
main terminal agent is never restricted, and with no ACTIVE file the guard is
inert. It is registered exactly once, by the plugin's own `hooks/hooks.json`,
which sits beside the script.

### The loopback + token posture
**What.** Both servers bind `127.0.0.1` and mint a per-boot token; every route
but `/health` requires it, and the WebSocket upgrade enforces an Origin/Host
allowlist.
**Why.** These pages show unredacted transcripts.
**How.** The `bin/` wrappers refuse to open a non-loopback bind at all. To
reach a page from another machine, forward the port
(`ssh -L 8931:127.0.0.1:8931 you@host`) rather than exposing it.

### The optional Mongo mirror
**What.** A write-behind copy of history that already lives in files.
**Why.** Queryable history, without ever becoming load-bearing: the live view
is memory-authoritative, so Mongo being absent, down, or missing `pymongo` is a
non-event that `/health` reports as `mirror: "absent"`.
**How.** `plugin/touch/docs/mongo.md` has the recipe and the security baseline:
loopback bind, auth on, and the port never published off the machine. One
collection per entity type with an indexed `sessionId` — never one collection
per session.

---

# Part 3 — Working on the code

## Repository layout

`plugin/touch/` is **the shipping subtree and the single canonical home** for
everything Touch ships. Everything outside it is development-only material that
deliberately never ships — which is the whole reason the payload is a
subdirectory: the plugin manifest schema has no `files`/`exclude` field, so the
directory boundary is the only way to keep `tests/` out of what a consumer
downloads (~10 MB, of which ~8 MB is `tests/fixtures/` alone).

| path | what it is |
|---|---|
| `plugin/touch/aggregator/` | the Python package: ingest, store, sessions, agents, WebSocket, server, optional Mongo mirror. One file, exactly one owner |
| `plugin/touch/touch-visual/` | the web page (`index.html`, `app.js`, `style.css`) — v0 is read-only |
| `plugin/touch/docs/` | `control-semantics.md` (verb ladder), `mongo.md` (database recipe + security baseline) |
| `plugin/touch/shared/monitoring/` | the run-monitor substrate — stateless, task-agnostic, exactly five files; its tests live outside the payload |
| `plugin/touch/skills/` | ten skills: four orchestration, six engineering-practice |
| `plugin/touch/bin/` | the six wrappers Touch puts on `PATH` |
| `plugin/touch/hooks/` | `orch_scope_guard.py` and the `hooks/hooks.json` that registers it — one registration, nowhere else |
| `plugin/touch/.claude-plugin/` | `plugin.json`, the one place a version is declared — and nothing else |
| `.claude-plugin/` (repo root) | `marketplace.json`, the catalog. It sits at the ROOT because a cloned marketplace is only ever read from `<repo>/.claude-plugin/marketplace.json`, and it names the payload with `"source": "./plugin/touch"` |
| `tests/` | one standalone executable per module + `run_all.sh` + `fixtures/` + `_roots.py` (the anchor every test names the canonical trees through) |
| `tests/monitoring/` | the monitoring module's own suite and fixtures, kept out of the payload |
| `scripts/` | `release.sh` — the release checklist, executable |
| `inception.md` | everything verified about the substrate (CLI 2.1.220), summarized |

`LICENSE` is the one deliberate duplicate (repo root and plugin root, required
by the plugin spec) and the only file pinned byte-for-byte to another —
`tests/test_plugin_tree.py` checks the pair.

## Ground rules

**Stdlib only on the critical path.** The ingest and serve path imports nothing
outside the standard library. The ONE permitted third-party runtime dependency
is `pymongo` (pinned `==4.17.0`), importable only from `mongo_store.py` and
`mirror.py`, lazily: its absence degrades `/health` and never fails startup,
breaks an agent, or blocks a test. Every Mongo test must skip cleanly with no
reachable mongod. `tests/test_stdlib_only.py` enforces all of this — do not add
a second dependency by analogy.

**Honesty is a feature.** The UI renders no control it cannot honestly perform,
and the docs promise nothing the code does not do. Degraded or derived states
are labelled as such ("closed — no verdict", "archived — source transcripts
unavailable"). A plan whose agents all returned without a decisive verdict
settles *done*, never *failed* — the fabricated FAILED badge was a real defect.

**Loopback by default.** Described above, and it is not negotiable per-feature.

## Tests

- No pytest, no runner library. Every test file is executable, prints
  `ok:`/`FAIL:` lines, and exits non-zero on failure.
- **Adding a test** is dropping an executable `tests/test_<thing>.py` into the
  tree — registration is by glob. The corollary: a test *helper* must NOT be
  named `test_*.py`, or it will be run as a suite.
- **Skips are reported, not swallowed.** Suites legitimately skip when
  something they read is absent (no mongod, no node, the gitignored run
  history). The runner prints skip counts, so green never quietly means "the
  files vanished".
- **The clean-checkout gate.** Before any wide change and before a release, run
  the suite over tracked bytes only — what a fresh clone actually looks like:

  ```bash
  d=$(mktemp -d) && git archive HEAD | tar -x -C "$d" && (cd "$d" && tests/run_all.sh --keep-going)
  ```

## The docs are tested

`tests/test_docs.py` pins claims in `README.md`, `CLAUDE.md`, `CONTRIBUTING.md`,
`inception.md`, `plugin/touch/docs/` and the two payload documents — including
*negative* guards, where the absence of a claim that was once wrong is
load-bearing. If you edit prose, run:

```bash
python3 tests/test_docs.py
```

If a guard fights you, it usually knows something: each one exists because the
claim it pins was once wrong in this repo. Change a guard only with the reason
in hand, and keep its negative half.

Two related gates: `tests/test_publish_hygiene.py` scans every tracked file for
token-shaped blobs and credentialed Mongo URIs, and keeps an allowlist of
tracked repo-root files — a new root file is added there deliberately, by
someone who looked at it. Keep real tokens in untracked scratch files (the
`mytok*` and `*.token` patterns are gitignored) and write passwords in docs as
`<password>`.

## The shipping subtree

Everything in `plugin/touch/` is edited in place; nothing in it is a copy of
anything else. There used to be a second set of these trees at the repo root,
kept byte-equal by a sync script; that arrangement is gone, along with the
script. The reasons, recorded so nobody rebuilds it: the tests imported the
root copy while the daemons executed the payload copy, so the tested file was
not the shipped file; the sync's delete-then-copy left a window in which a live
`touch-status` call found nothing; and it had already produced one resolver in
shipped code that preferred the wrong copy.

`tests/test_plugin_tree.py` fails on a stray file sitting in the payload, and
`tests/test_package.py` builds a real `git archive` stage and scans it — the
payload boundary is enforced, not documented.

**No symlinks anywhere under `plugin/touch/`.** Not because `--plugin-dir`
skips them (it does not — measured on CLI 2.1.220) but because `release.sh`
builds the payload with `git archive`, which preserves a symlink verbatim into
a stage where its target does not exist. The component then silently vanishes
from the published zip while every `find -type f` count still looks right.
`release.sh` gates on `find "$STAGE" -type l` being empty, and
`test_package.py` carries the same arm.

The plugin's own `README.md` is its trust surface — the `/plugin` UI never
renders it, so its install/update command lines must be correct verbatim, and
its disclosures are the only ones a stranger gets. It is deliberately a
**different document** from the root `README.md`: one addresses a stranger
deciding whether to install, the other a reader of this repository. Never pin
them to each other, and never merge them. Every number in either README is a
measured claim — re-measure with
`claude --plugin-dir plugin/touch plugin details touch` and paste what it
printed.

## Releasing

**This repository is the marketplace.** `.claude-plugin/marketplace.json` at
the root names the marketplace `msdrx-tools` and lists one plugin with
`"source": "./plugin/touch"`, so `/plugin marketplace add msdrx/touch` clones
this repo and `/plugin install touch@msdrx-tools` copies that subtree into the
user's plugin cache. The manifest has to be at the root: a cloned marketplace
is read from `<repo>/.claude-plugin/marketplace.json` and nowhere else, and
there is no subdirectory form of a remote marketplace source — `--sparse`
limits a checkout, not where the catalog is looked for.

Two consequences worth naming, because an earlier model existed to avoid them
and publishing from here accepts them deliberately:

- **An install clones this repo's whole history**, which carries a burned token
  blob and credentialed `mongodb://` URIs. Purging it (`git filter-repo`) is
  the fix; every credential this repo has ever seen should be treated as
  burned either way. `release.sh`'s preflight makes you confirm this.
- **Every commit is a marketplace update.** Users who have auto-update on
  re-clone this repo, dev noise included. Only a `version` bump in
  `plugin.json` actually delivers a new payload to them.

`scripts/release.sh` **is** the checklist; there is deliberately no RELEASE.md.

1. Bump the version in `plugin/touch/.claude-plugin/plugin.json` — the only
   place a version is declared — and give `CHANGELOG.md` a top entry naming the
   same version (a guard enforces the agreement). That bump is the only thing
   that delivers an update to installed users.
2. Commit. The gates read the **committed** tree (`git archive HEAD:plugin/touch`
   builds the payload they scan); the working tree is never what ships.
3. Dry-run every gate: `scripts/release.sh --check`. It stops before the point
   of no return, pushes nothing, and reports every failure rather than the
   first. Run it early and often.
4. `scripts/release.sh`. A green run pushes this repo — that push is the
   publish, because the marketplace *is* the repo.
5. Users pick it up with `/plugin marketplace update msdrx-tools` then
   `/plugin update touch@msdrx-tools`; third-party marketplaces do not
   auto-update by default.

## If you run the orchestration skills here

They spawn real subagent runs that write into
`.claude/local-orchestrators/<task>/`. Etiquette that bites:

- When a run ends, stop its watcher; leave its state files in place.
- Never delete a finished task folder or its `events.jsonl` — completed runs
  are dashboard history, and the Mongo mirror's key space depends on them.
- Don't commit while a watcher is writing inside the paths being committed.
- Every `touch-status` call sets `ORCH_STATE_DIR`.

## Where design decisions live

`CLAUDE.md` carries the authority ladder over the full design record. The run
folders it cites (under `.claude/local-orchestrators/`) are local history and
gitignored — absent from a clean checkout **by design**, which is why doc
guards that read them skip with a reason instead of failing. `inception.md` is
the tracked summary of everything verified about the substrate; when in doubt,
start there.
