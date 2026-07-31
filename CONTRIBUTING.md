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

Ten PASS/FAIL lines — interpreter version, the package importing *from this
tree*, the web assets, project-root resolution, task-state resolution, the
auto-memory mapping, a leftover pre-mapping memory directory (the one check
that can WARN instead of fail), a loopback bind, the exec bits on `bin/`, and
one event through a real write/read round trip. It exits non-zero on any
failure and ends with the command to run next:

```
PASS  python3 3.13.7 (needs 3.11+) at /usr/bin/python3
PASS  aggregator 0.1.0 imported from this tree (…/plugin/touch/aggregator/__init__.py)
…
10 checks: all passed
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
TASK=$PWD/.touch/local-orchestrators/<task-name>
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

The seven wrappers are then on `PATH` as bare command names, and the skills are
available as `/touch:<name>`.

## Hacking on the module itself

`touch-serve`, `touch-monitor`, `touch-watcher`, `touch-status`,
`touch-cycle-reporter` and `touch-run` are the supported entry points — one
per program (`touch-selfcheck`, the seventh wrapper, is run by hand). The one
sanctioned module-direct invocation *of the server* is:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=plugin/touch python3 -m aggregator.server
```

There is no `aggregator/` at the repo root, which is why the `PYTHONPATH` is
required and why the docs omit the `python3 -P` that `touch-serve` prints
(`-P` keeps the cwd off `sys.path`; here there is nothing in the cwd left to
shadow the package). Keep the first env var: every wrapper exports it, so a
hand-typed run without it is the only way an `aggregator/__pycache__` lands in
the shipping subtree — which `tests/test_package.py` fails by name. The Mongo
mirror is the other directly-runnable module — an operator tool with no
wrapper by design; `CLAUDE.md` carries its line.

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
**What.** Renders one report per implement → test → critique cycle, emits each
loop-terminal verdict, and renders the final run report — for both workflows.
**Why.** So a finished run leaves something readable behind instead of a log
you have to reconstruct — and so verdicts are written by a deterministic
renderer rather than a model asked to summarize itself. `failed` requires a
real verdict: a loop with no decisive result settles *done* ("closed — no
verdict"), never *failed*.
**How.** `ORCH_STATE_DIR=<task-dir> touch-cycle-reporter <wf_dir> [--once]
[--interval=N]`. It fronts `cycle_reporter.py`, which ships inside the
`implement` skill and serves both loop skills; `--settle` is the
idempotent one-shot `touch-run close` uses to emit only the closes the record
implies but the stream is missing — it never invents a verdict.

### `touch-run` — the run envelope
**What.** `start | bind | close | verify | status`: lays out a run's task
folder, copies the workflow template byte-for-byte, preflights the run spec,
seeds the plan cards, starts and stops Touch's own daemons, and settles the
cards at close.
**Why.** The blocks of shell a driver used to retype for every run were
mechanical bookkeeping, measured at real token cost — exactly what a script
should own. It acts on Touch's folders and daemons only: it runs no agent and
is not a session verb.
**How.** `touch-run start <task> --spec <file>` — per-project constants merge
in from the tracked `.touch/run.json`, per-run values winning — then `bind`
records the `wf_dir` and renders `plan/RESUME.md`, then `close` settles what
the record implies and stops the pid-verified daemons it started.

### `touch-selfcheck` — does it work here
**What.** Ten PASS/FAIL checks of an installation (one of them — the
legacy-memory check — can WARN instead).
**Why.** The plugin ships no test suite (fixtures and a git checkout do not
belong in a payload), but the ten things that actually break on someone
else's machine are cheap to check — so "it doesn't work" turns into one failing
line you can act on.
**How.** `cd <your project> && touch-selfcheck`. It writes nothing except one
throwaway event in a temp directory it removes — except `--init`, its one
writing mode, which maps auto memory into `<project>/.touch/memory`. Its
report ends with a sentinel so a crash halfway through can never be summarized
as a pass.

### `touch-serve` — the aggregator and the Touch page
**What.** The read/serve side: harness ingest, the ingest tick, the event
store, the read API, the WebSocket, and the `touch-visual` page on port 8932.
**Why.** One page over everything the CLI writes — sessions included — rather
than one dashboard per run.
**How.** `touch-serve [--port P] [--tasks-root DIR] [--assets DIR]`. The page
is shipped and read-only; the ingest tick (`aggregator/tick.py`) is what fills
it — it drives the tailers, applies the derived operations into the read model
and the WAL, and reports itself in `/health`. `touch-monitor` is still the
page most runs are watched on; the two pages are meant to converge into one.

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
**What.** Six files — `status.sh`, `monitor_server.py`, `decision_watcher.py`,
`monitor.html`, `memory.html`, `monitoring.md` — the working prototype Touch's
visual half inherits, plus the memory editor page the same server hosts.
**Why.** It already solved live orchestration monitoring with bash, stdlib
Python and a browser. `monitoring.md` is normative for its event schema.
**How.** Stateless and task-agnostic: never copy or modify it per task. All
per-run state lives in `.touch/local-orchestrators/<task-name>/`.

### The memory editor (`/memory` on port 8931)
**What.** A second page, `memory.html`, plus the JSON group `/api/memory/*` on
the monitoring server: list, read, create, update and delete the `*.md` files
Claude Code loads as auto memory from `<project>/.touch/memory/`.
**Why.** The memory index is loaded into every session in this project, so it
is the one file set worth editing from a page instead of by hand — and it is on
*this* server because the monitor page holds only this server's per-boot token.
**How.** Writes are **off by default** (`--allow-memory-write` /
`TOUCH_ALLOW_MEMORY_WRITE`), take the token from a header only, require
`X-Touch-Write: 1` and a same-origin `Origin`, and go through a flat-name
regex, a symlink refusal, realpath containment, an `~/.claude` refusal, an
`O_EXCL|O_NOFOLLOW` temp file plus `os.replace`, a required `ifMatch` and the
content-hygiene rules — in that order, because the order is the security
property. Memory is now public; write it as if it ships.

## What ships alongside

### The ten skills
**What.** Four orchestration skills (`research`, `implement`,
`orchestrate`, `monitor`) and six engineering-practice skills, all
invoked as `/touch:<name>`.
**Why.** The orchestration pair is the loop the dashboard renders:
`research` → ONE complete plan → `implement` → gated
implement/test/critique loops divided by file ownership. The other six are what
the agents inside those loops are asked to do well.
**How.** The two loop skills carry a `templates/*.workflow.js` that is the
normative protocol — prompts, schemas, models, markers. The templates are
generic and spec-driven: `touch-run start` copies one **byte-for-byte** into
the task folder's `orch-scripts/`, never edited, and every per-run value
arrives through `args` from a run-spec JSON merged over the tracked
`.touch/run.json`. The scripts emit no events and no prompt mandates a
self-tracing status call — the watcher, the cycle reporter and `touch-run`
derive every event from the record. All ten skills cost ~1,261 tokens of
always-on context between them, a measured figure.

### The two hooks
**What.** `orch_scope_guard.py`, a `PreToolUse` hook, and
`agent_lifecycle.py`, an additive SubagentStart/SubagentStop/PostToolUse
recorder.
**Why.** While a run is active, a subagent that wanders into another run's
folder can read or overwrite work it knows nothing about — and a dashboard
that waits for the journal learns about agent starts later than a hook does.
**How.** While `.touch/local-orchestrators/ACTIVE` lists task names, the scope
guard denies subagent access to every unlisted task's folder except its
`plan/`, and denies subagent writes to `.touch/memory/**` outright. The main
terminal agent is never restricted, and with no ACTIVE file the guard is
inert. Both roots are consulted during the transition (`.touch/` first, then
the legacy `.claude/` one), so no flip order can disarm `HALT`.
`agent_lifecycle.py` denies nothing, ever: it records agent lifecycle lines
and merges `wf_dir`/`run_id` into the run config, and is likewise inert with
no active run. Both are registered exactly once, by the plugin's own
`hooks/hooks.json`, which sits beside the scripts.

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
directory boundary is the only way to keep `tests/` out of the payload.

Be precise about what that boundary buys, because it is easy to over-claim.
It controls **what lands in a user's plugin cache** (the install copies
`./plugin/touch` and nothing else) and **what the release gates scan** (step 5
builds the stage with `git archive HEAD:plugin/touch`, so `tests/fixtures/` —
~8 MB on its own — is not in the scanned bytes). It does **not** control what
is transferred: this repository *is* the marketplace, so
`/plugin marketplace add msdrx/touch` clones the whole repo, roughly 15 MB of
it (re-measure with `gh api repos/msdrx/touch --jq .size`, which reports KB),
history included. That is the same decision the Releasing section below
records, seen from the layout side — the two paragraphs are one decision, and
neither may be edited to disagree with the other.

| path | what it is |
|---|---|
| `plugin/touch/aggregator/` | the Python package: ingest, the ingest tick, store, sessions, agents, WebSocket, server, the cost reader, optional Mongo mirror. One file, exactly one owner |
| `plugin/touch/touch-visual/` | the web page (`index.html`, `app.js`, `style.css`) — read-only; no control affordance renders |
| `plugin/touch/docs/` | `control-semantics.md` (verb ladder), `mongo.md` (database recipe + security baseline), and the long form the session guide points at: `memory-home.md`, `run-folders.md`, `dev-loop.md` |
| `plugin/touch/shared/monitoring/` | the run-monitor substrate — stateless, task-agnostic, exactly six files; its tests live outside the payload |
| `plugin/touch/skills/` | ten skills: four orchestration, six engineering-practice |
| `plugin/touch/bin/` | the seven wrappers Touch puts on `PATH` |
| `plugin/touch/hooks/` | `orch_scope_guard.py`, `agent_lifecycle.py` and the `hooks/hooks.json` that registers them — one registration, nowhere else |
| `plugin/touch/.claude-plugin/` | `plugin.json`, the one place a version is declared — and nothing else |
| `.claude-plugin/` (repo root) | `marketplace.json`, the catalog. It sits at the ROOT because a cloned marketplace is only ever read from `<repo>/.claude-plugin/marketplace.json`, and it names the payload with `"source": "./plugin/touch"` |
| `tests/` | one standalone executable per module + `run_all.sh` + `fixtures/` and `cost-corpus/` (frozen corpora) + `_roots.py` (the anchor every test names the canonical trees through) |
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
  files vanished". A suite that needs an absent thing must SKIP, never crash —
  a `SystemExit` out of an imported module is a red file, not a skip line, and
  that distinction is what keeps a clean checkout green.
- **`test_package.py` gates on what git has, not on your disk.** It never reads
  `plugin/touch/` directly: it builds the release stage with `git archive`, on
  `HEAD:plugin/touch` *and* on a throwaway-index preview of the next commit
  (`git add -A -- plugin/touch` written to a scratch index, so your own index is
  untouched). The preview is why most payload edits are checked *before* you
  commit them; an arm asserting a file is **absent** from the payload still
  reads the `HEAD` side, so a deletion (a stowaway `marketplace.json`, say) stays
  red until the deletion is committed. `test_plugin_tree.py` reads the working
  tree, with one exception: it asserts the root catalog is **tracked**
  (`git ls-files --error-unmatch`), so a catalog nobody ever `git add`ed is red
  however good it looks on disk. That is the intended shape, not a bug to route
  around — those two suites answer "what would ship", and what ships is what git
  has. Do not weaken an arm to make an uncommitted tree green.
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
  burned either way. `release.sh` step 0 **gates** on it: a reachable token
  blob or a credentialed URI is a red gate — under `--check` too — and the only
  way past it is `RELEASE_HISTORY_ACCEPTED=yes`, said out loud, per run.
  `RELEASE_CONFIRM` does not imply it and never will: confirming a checklist
  and accepting a leaked-credential publish are different decisions.
- **Every commit is a marketplace update.** Users who have auto-update on
  re-clone this repo — roughly 15 MB, dev noise included (the same number the
  layout section above states, for the same reason). Only a `version` bump in
  `plugin.json` actually delivers a new payload to them.

**The source form that was declined.** A `git-subdir` plugin source, pointed at
a small clean marketplace repository, would avoid both consequences above: no
history transfer and a tiny clone. It was weighed and declined, because it
re-creates the two-repositories-to-keep-in-sync model this project abolished
when `plugin/touch/` became the single canonical home — a second repo to sync
is exactly the cost this model refused to keep paying, and a payload that is
canonical in one place is worth more than a smaller clone. Recorded here so the
next reader does not re-litigate it from scratch; if the trade ever flips, it
flips on that cost, not on the clone size.

`scripts/release.sh` **is** the checklist; there is deliberately no RELEASE.md.

1. Bump the version in `plugin/touch/.claude-plugin/plugin.json` — the only
   place a version is declared — and give `CHANGELOG.md` a top entry naming the
   same version (a guard enforces the agreement). That bump is the only thing
   that delivers an update to installed users.
2. Commit. `release.sh`'s gates read the **committed** tree
   (`git archive HEAD:plugin/touch` builds the payload they scan); the working
   tree is never what ships.
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
`.touch/local-orchestrators/<task>/`. Etiquette that bites:

- When a run ends, stop its watcher; leave its state files in place.
- Never delete a finished task folder or its `events.jsonl` — completed runs
  are dashboard history, and the Mongo mirror's key space depends on them. Never
  rewrite one either: a finished run is dated record, paths included.
- Don't commit while a watcher is writing inside the paths being committed.
- **Never `git add .touch/`; always `git add .touch/memory` (and
  `.touch/run.json` if you changed it)** — those are the two tracked paths,
  staged by name; the rest of `.touch/` is transcripts and tokens.
- Every `touch-status` call sets `ORCH_STATE_DIR`.

## Where design decisions live

`CLAUDE.md` carries the authority ladder over the full design record. The run
folders it cites (under `.touch/local-orchestrators/`) are local history and
gitignored — absent from a clean checkout **by design**, which is why doc
guards that read them skip with a reason instead of failing. `inception.md` is
the tracked summary of everything verified about the substrate; when in doubt,
start there.
