# Contributing to Touch

How this repository works and what a change needs before it lands. The short
version: plain Python 3 stdlib, tests are standalone executables, the docs are
tested too, and `plugin/touch/` is the canonical home of everything Touch
ships — you edit it in place, because there is nowhere else to edit.

## Prerequisites

- Python **3.11+**
- bash, git, coreutils
- nothing to `pip install` — the runtime and the test suite are stdlib-only
  (the one exception is described below)

## Repository layout

`plugin/touch/` is **the shipping subtree and the single canonical home** for
everything Touch ships. Everything outside it is development-only material
that deliberately never ships — which is the whole reason the payload is a
subdirectory: the plugin manifest schema has no `files`/`exclude` field, so
the directory boundary is the only way to keep `tests/` out of what a consumer
downloads — ~10 MB of fixtures and frozen transcripts, of which ~8 MB is
`tests/fixtures/` alone (`du -sb`, apparent size, which is what a download
costs; `du -sh` says 11M because it reports blocks allocated, 10.03 MiB, and
rounds that up to a whole unit — measured 2026-07-29).

| path | what it is |
|---|---|
| `plugin/touch/aggregator/` | the Python package: ingest, store, sessions, agents, WebSocket, server, optional Mongo mirror. One file, exactly one owner |
| `plugin/touch/touch-visual/` | the web page (`index.html`, `app.js`, `style.css`) — v0 is read-only |
| `plugin/touch/docs/` | `control-semantics.md` (verb ladder), `mongo.md` (database recipe + security baseline) |
| `plugin/touch/shared/monitoring/` | the run-monitor substrate Touch inherits — stateless, task-agnostic, never copied per task. Exactly five files; its tests live outside the payload |
| `plugin/touch/skills/` | ten skills: four orchestration, six engineering-practice |
| `plugin/touch/bin/` | the six wrappers Touch puts on `PATH` — `touch-serve`, `touch-monitor`, `touch-watcher`, `touch-status`, `touch-cycle-reporter` (the five programs a session runs, GD-U4) plus `touch-selfcheck`, which verifies an installation and is run by hand |
| `plugin/touch/hooks/` | `orch_scope_guard.py` and the `hooks/hooks.json` that registers it — one registration, nowhere else |
| `plugin/touch/.claude-plugin/` | `plugin.json` (the one place a version is declared) and `marketplace.json` — the hook manifest is not here |
| `tests/` | one standalone executable per module + `run_all.sh` + `fixtures/` + `_roots.py` (the anchor every test names the canonical trees through) |
| `tests/monitoring/` | the monitoring module's own suite and fixtures, kept out of the payload |
| `scripts/` | `release.sh` — the release checklist, executable |
| `inception.md` | everything verified about the substrate (CLI 2.1.220), summarized |

`LICENSE` is the one deliberate duplicate (repo root and plugin root, required
by the plugin spec) and the only file in the tree pinned byte-for-byte to
another — `tests/test_plugin_tree.py` checks the pair.

## Ground rules

**Stdlib only on the critical path.** The ingest and serve path imports
nothing outside the Python standard library. The ONE permitted third-party
runtime dependency is `pymongo` (pinned `==4.17.0`), importable only from
`plugin/touch/aggregator/mongo_store.py` and
`plugin/touch/aggregator/mirror.py`, lazily: its absence
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

`touch-serve`, `touch-monitor`, `touch-watcher`, `touch-status` and
`touch-cycle-reporter` are the supported entry points. They are on `PATH` in
any session with the plugin enabled; from a bare checkout, run them out of
`plugin/touch/bin/`.

```bash
plugin/touch/bin/touch-serve        # Touch: binds 127.0.0.1:8932, prints the tokened URL
```

The run-monitor (port 8931) is what live orchestration runs report to:

```bash
TASK=$PWD/.claude/local-orchestrators/<task-name>
ORCH_STATE_DIR="$TASK" plugin/touch/bin/touch-monitor &
ORCH_STATE_DIR="$TASK" plugin/touch/bin/touch-watcher &
```

The one sanctioned module-direct invocation *of the server*, for hacking on the
module itself, is `PYTHONPATH=plugin/touch python3 -m aggregator.server`. (The
Mongo mirror is the other module you can run directly — an operator tool with
no `bin/` wrapper by design; CLAUDE.md carries its line.) There is no longer an
`aggregator/` at the repo root to run without it — which is also why the docs
omit the `python3 -P` that `touch-serve` prints: `-P` keeps the cwd off
`sys.path`, and here there is nothing in the cwd left to shadow the package.

To dogfood the plugin exactly as a consumer would get it, from the repo root:

```bash
claude --plugin-dir plugin/touch
```

## Tests

`tests/run_all.sh` runs **both** suites — Touch's `tests/test_*.py` and the
monitoring module's `tests/monitoring/test_*.py` — because a green Touch suite
over a red substrate would be a lie.

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

`tests/test_docs.py` pins claims in `README.md`, `CLAUDE.md`, `CONTRIBUTING.md`,
`inception.md`, `plugin/touch/docs/` and the two payload documents — including
*negative* guards (the absence of a claim that was once wrong is load-bearing).
If you edit prose, run:

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

The subtree is the complete plugin payload **and** the canonical source tree:
everything in it is edited in place, and nothing in it is a copy of anything
else. There used to be a second set of these trees at the repo root, kept
byte-equal by `scripts/sync_plugin.sh`; that arrangement is gone, along with
the script. The reasons it went, recorded so nobody rebuilds it: the tests
imported the root copy while the daemons executed the payload copy, so the
tested file was not the shipped file; the sync's delete-then-copy left a window
in which a live `touch-status` call found nothing; and it had already produced
one resolver in shipped code that preferred the wrong copy.

The one exception is `LICENSE`, which the plugin spec requires at the repo root
*and* at the plugin root. That pair is machine-checked byte-for-byte in
`tests/test_plugin_tree.py`, and it is labelled there as the one deliberate
duplicate. If you find yourself adding a second such pair, that is the signal
to move the file instead.

`tests/test_plugin_tree.py` also fails on a stray file sitting in the payload,
and `tests/test_package.py` builds a real `git archive` stage and scans it —
the payload boundary is enforced, not documented.

**No symlinks anywhere under `plugin/touch/`.** Not because `--plugin-dir`
skips them (it does not — an escaping symlink is honoured there, measured on
CLI 2.1.220) but because `release.sh` builds the payload with `git archive`,
which preserves a symlink verbatim into a stage where its target does not
exist. The component then silently vanishes from the published zip while every
`find -type f` count still looks right. `release.sh` gates on
`find "$STAGE" -type l` being empty, and `test_package.py` carries the same arm.

The plugin's `README.md` is its trust surface — the `/plugin` UI never renders
it, so its install/update command lines must be correct verbatim, and its
disclosures (what is read, what is written, what the hook costs, what the
skills cost in context) are the only ones a stranger gets. `test_docs.py`
guards that file too. It is deliberately a **different document** from the
root `README.md`: one addresses a stranger deciding whether to install, the
other a reader of this repository. Never pin them to each other, and never
merge them.

Every number in either README is a measured claim, not an estimate. Re-measure
with `claude --plugin-dir plugin/touch plugin details touch` and paste what it
actually printed.

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

The orchestration skills spawn real subagent runs that write into
`.claude/local-orchestrators/<task>/`. Etiquette that bites:

- When a run ends, stop its watcher; leave its state files in place.
- Never delete a finished task folder or its `events.jsonl` — completed runs
  are dashboard history, and the Mongo mirror's key space depends on them.
- Don't commit while a watcher is writing inside the paths being committed.
- Every `touch-status` call sets `ORCH_STATE_DIR`, or the event lands in
  whatever task folder the writer resolves instead.

## Where design decisions live

`CLAUDE.md` carries the authority ladder over the full design record. The run
folders it cites (under `.claude/local-orchestrators/`) are local history and
gitignored — absent from a clean checkout **by design**, which is why doc
guards that read them skip with a reason instead of failing. `inception.md` is
the tracked summary of everything verified about the substrate; when in doubt,
start there.
