# Contributing to Touch

How this repository works and what a change needs before it lands. The short
version: plain Python 3 stdlib, tests are standalone executables, the docs are
tested too, and `plugin/touch/` is partly a build product you regenerate
rather than edit.

## Prerequisites

- Python **3.11+**
- bash, git, coreutils
- nothing to `pip install` — the runtime and the test suite are stdlib-only
  (the one exception is described below)

## Repository layout

| path | what it is |
|---|---|
| `aggregator/` | the Python package: ingest, store, sessions, agents, WebSocket, server, optional Mongo mirror. One file, exactly one owner |
| `touch-visual/` | the web page (`index.html`, `app.js`, `style.css`) — v0 is read-only |
| `plugin/touch/` | **the shipping subtree** — the complete Claude Code plugin payload (manifests, `bin/` wrappers, skills, hook, and pinned copies of the trees above) |
| `tests/` | one standalone executable per module + `run_all.sh` + `fixtures/` |
| `docs/` | `control-semantics.md` (verb ladder), `mongo.md` (database recipe + security baseline) |
| `scripts/` | `sync_plugin.sh` (rebuild the pinned copies), `release.sh` (the release checklist, executable) |
| `.claude/shared/monitoring/` | the legacy run-monitor substrate Touch inherits — stateless, task-agnostic, never copied per task |
| `inception.md` | everything verified about the substrate (CLI 2.1.220), summarized |

## Ground rules

**Stdlib only on the critical path.** The ingest and serve path imports
nothing outside the Python standard library. The ONE permitted third-party
runtime dependency is `pymongo` (pinned `==4.17.0`), importable only from
`aggregator/mongo_store.py` and `aggregator/mirror.py`, lazily: its absence
degrades `/health` to `mirror: "absent"` and never fails startup, breaks an
agent, or blocks a test. Every Mongo test must skip cleanly with no reachable
mongod. `tests/test_stdlib_only.py` enforces all of this — do not add a second
dependency by analogy.

**Honesty is a feature.** The UI renders no control it cannot honestly
perform, and the docs promise nothing the code does not do. Degraded or
derived states are labelled as such ("closed — no verdict", "archived —
source transcripts unavailable"). If you add a claim to the docs, expect to
add a guard for it (see "The docs are tested" below).

**Loopback by default.** Both servers bind `127.0.0.1` and mint a per-boot
token; the plugin's `bin/` wrappers refuse to open a non-loopback bind on the
user's behalf. The optional database binds loopback only, with auth, and its
port (27017) is never published off the machine.

## Running from a checkout

```bash
python3 -m aggregator.server        # Touch: binds 127.0.0.1:8932, prints the tokened URL
```

The legacy run-monitor (port 8931) is what live orchestration runs report to:

```bash
TASK=$PWD/.claude/local-orchestrators/<task-name>
ORCH_STATE_DIR="$TASK" python3 .claude/shared/monitoring/monitor_server.py &
ORCH_STATE_DIR="$TASK" python3 .claude/shared/monitoring/decision_watcher.py &
```

To dogfood the plugin exactly as a consumer would get it, from the repo root:

```bash
claude --plugin-dir plugin/touch
```

## Tests

`tests/run_all.sh` runs **both** suites — Touch's `tests/test_*.py` and the
monitoring module's `.claude/shared/monitoring/tests/test_*.py` — because a
green Touch suite over a red substrate would be a lie.

```bash
tests/run_all.sh                # fail-fast
tests/run_all.sh --keep-going   # run everything, report every failure
tests/run_all.sh --list         # what would run, in order
python3 tests/test_store.py     # any single file runs alone
```

- No pytest, no runner library. Every test file is executable, prints
  `ok:`/`FAIL:` lines, and exits non-zero on failure.
- **Adding a test** is dropping an executable `tests/test_<thing>.py` into the
  tree — registration is by glob, nothing else changes. The corollary: a test
  *helper* must NOT be named `test_*.py`, or it will be run as a suite.
- **Skips are reported, not swallowed.** Suites legitimately skip when
  something they read is absent (no mongod, no node, the gitignored run
  history). The runner prints skip counts so green never quietly means "the
  files vanished".
- **The clean-checkout gate.** Before any wide change and before a release,
  run the suite over tracked bytes only — what a fresh clone or a packaged
  copy actually looks like:

  ```bash
  d=$(mktemp -d) && git archive HEAD | tar -x -C "$d" && (cd "$d" && tests/run_all.sh --keep-going)
  ```

## The docs are tested

`tests/test_docs.py` pins claims in `README.md`, `CLAUDE.md`, `inception.md`
and `docs/` — including *negative* guards (the absence of a claim that was
once wrong is load-bearing). If you edit prose, run:

```bash
python3 tests/test_docs.py
```

If a guard fights you, it usually knows something: each one exists because the
claim it pins was once wrong in this repo. Change a guard only with the reason
in hand, and keep its negative half.

Two related gates: `tests/test_publish_hygiene.py` scans every tracked file
for token-shaped blobs and credentialed Mongo URIs, and keeps an allowlist of
tracked repo-root files — a new root file is added there deliberately, by
someone who looked at it. Keep real tokens in untracked scratch files (the
`mytok*` and `*.token` patterns are gitignored) and write passwords in docs as
`<password>`.

## The shipping subtree (`plugin/touch/`)

The subtree is the complete plugin payload, and it holds two kinds of content:

- **Canonical there** — edit in place: `.claude-plugin/` (manifests),
  `bin/` (wrappers), `skills/`, `hooks/`, the plugin's own `README.md`,
  `CHANGELOG.md`.
- **Pinned copies** — canonical elsewhere in the repo, byte-equal copies here:
  `aggregator/`, `touch-visual/`, `docs/`, `shared/monitoring/`, `LICENSE`.
  **Never hand-edit a pinned copy.** After changing a canonical file:

  ```bash
  scripts/sync_plugin.sh          # rebuild the pinned copies (delete-then-copy)
  scripts/sync_plugin.sh --check  # report drift, change nothing
  ```

  `tests/test_plugin_tree.py` fails on any drift, in both directions — a
  stray file sitting in the payload is as red as a stale copy.

The plugin's `README.md` is its trust surface — the `/plugin` UI never renders
it, so its install/update command lines must be correct verbatim, and its
disclosures (what is read, what is written, what the hook costs) are the only
ones a stranger gets. `test_docs.py` guards that file too.

## Releasing

`scripts/release.sh` **is** the checklist — there is deliberately no
RELEASE.md. What to know before touching it:

- The version lives in exactly one place:
  `plugin/touch/.claude-plugin/plugin.json`. `CHANGELOG.md`'s top entry must
  name that same version — a guard enforces it, because that bump is the only
  thing that delivers an update to installed users.
- The payload is built by `git archive` from a **committed** tree into a
  separate, flat release repository. The working tree is never copied, and
  the dev repo is never an install source.
- `scripts/release.sh --check` runs every gate up to the point of no return
  and changes nothing. Run it early and often.

## If you run the orchestration skills here

The `.claude/` skills spawn real subagent runs that write into
`.claude/local-orchestrators/<task>/`. Etiquette that bites:

- When a run ends, stop its watcher; leave its state files in place.
- Never delete a finished task folder or its `events.jsonl` — completed runs
  are dashboard history, and the Mongo mirror's key space depends on them.
- Don't commit while a watcher is writing inside the paths being committed.
- Every `status.sh` call sets `ORCH_STATE_DIR`, or it dribbles a stray
  `events.jsonl` into the shared module directory.

## Where design decisions live

`CLAUDE.md` carries the authority ladder over the full design record. The run
folders it cites (under `.claude/local-orchestrators/`) are local history and
gitignored — absent from a clean checkout **by design**, which is why doc
guards that read them skip with a reason instead of failing. `inception.md` is
the tracked summary of everything verified about the substrate; when in doubt,
start there.
