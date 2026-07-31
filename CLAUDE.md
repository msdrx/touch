# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository. It is the session guide — the bottom of the authority
ladder. When it disagrees with a plan, the plan wins; fix this file.

It is also **always-on context**, so it is capped — see "The context budget";
the long form it used to carry is in `plugin/touch/docs/`, read on demand.

## Project status

Touch is a web page for visualizing and managing subagents in a Claude Code
session, with two components — **aggregator** (Python) and **touch-visual**
(the page). The read side's derivation modules are complete and tested, and the
ingest tick is what runs them: it drives the tailers, applies the derived
operations into the read model and the WAL, and reports itself in `/health`.
The page is shipped and read-only. **No session-control verb ships** — nothing
here starts, ends or re-invokes a session (GD-4), and `aggregator/server.py`'s
`CONTROL_ROUTES` stays `{}`.

One **write** plane does ship, and it is deliberately not a control verb: the
run dashboard on 8931 lists, reads and — behind an explicit
`--allow-memory-write` / `$TOUCH_ALLOW_MEMORY_WRITE`, **off by default** —
edits Claude Code's auto-memory files under `<project>/.touch/memory/`. Memory
CRUD acts on files, not sessions: no verb-table entry, no session-class
promotion, no `touch-status` event. `touch-run start|bind|close|verify|status`
is likewise **not** a control verb (GD-D8): it lays out a run folder and starts
and stops Touch's own daemons.

**`plugin/touch/` is the single canonical home** for everything Touch ships
(GD-U1): no second copy at the repo root, no sync script. Everything else is
development-only material that never ships — except the root
`.claude-plugin/marketplace.json`, a catalog *about* the payload.

| path | what it is |
|---|---|
| `plugin/` | holds exactly one thing: `plugin/touch/`, the payload and the canonical source tree |
| `plugin/touch/aggregator/` | the Python package — `tailer`, `store`, `ws`, `sessions`, `ingest`, `tick`, `legacy`, `agents`, `custom_state`, `costs`, `refs`, `paths`, `mongo_store`, `mirror`, `server`. One file, exactly one owner |
| `plugin/touch/touch-visual/` | `index.html`, `app.js`, `style.css` — read-only; no control affordance renders |
| `plugin/touch/docs/` | `mongo.md`, `control-semantics.md` (verb ladder, session classes), `memory-home.md`, `run-folders.md`, `dev-loop.md` |
| `plugin/touch/shared/monitoring/` | the six monitoring core files — `status.sh`, `monitor_server.py`, `decision_watcher.py`, `monitor.html`, `memory.html`, `monitoring.md`. The live daemons execute from here |
| `plugin/touch/skills/` | the ten skills, one directory each |
| `plugin/touch/bin/` | the seven wrappers on `PATH` (GD-U4 as amended by GD-D8): `touch-serve`, `touch-monitor`, `touch-watcher`, `touch-status`, `touch-cycle-reporter`, `touch-run` — the six a session runs — plus `touch-selfcheck`, run by hand |
| `plugin/touch/hooks/` | `orch_scope_guard.py` (run-scope `PreToolUse`), `agent_lifecycle.py` (additive Subagent/PostToolUse recording) and the `hooks/hooks.json` that registers them — the one and only registration (GD-U5) |
| `plugin/touch/.claude-plugin/` | `plugin.json`, the ONE place a version is declared — one file; neither the hook manifest nor the catalog is here |
| `.claude-plugin/` (repo root) | `marketplace.json`: marketplace `msdrx-tools`, one entry, `"source": "./plugin/touch"`. At the ROOT because a remote marketplace source has no subdirectory form — an add clones the repo and reads `<clone>/.claude-plugin/marketplace.json`, nothing else |
| `tests/` | one standalone executable per module + `run_all.sh` + `fixtures/` (frozen corpora, sha256 manifest) + `_roots.py`, the one anchor every test names the canonical trees through |
| `tests/monitoring/` | the monitoring module's dev-only suite, out of the module so the payload boundary stays the directory boundary (GD-U6) |
| `scripts/` | `release.sh` — the release checklist, executable; deliberately no RELEASE.md |
| `README.md` / `CONTRIBUTING.md` / `inception.md` | intent + the honest verb table; ground rules and the release gate; a dated substrate snapshot (CLI 2.1.220) whose pre-plugin paths are history, not directions |
| `.claude/` | `.claude/settings.json` (exactly two keys: status line + `enabledPlugins: {"touch@inline": true}`, GD-C1 — `plugin/touch/docs/dev-loop.md`), `statusline.sh` (shells out to `jq`: a **status-line-only** exception, never a licence for `jq` in Touch's own code or tests), two `shared/scripts/` helpers, and the untracked `settings.local.json`, the ONE place `autoMemoryDirectory` may be written (G1). Still the project MARKER every resolver walks up to; holds no run state |
| `.touch/` | project-local state, gitignored except ONE carve, **five trust classes in one directory**: (1) run history, `local-orchestrators/<task>/`; (2) Touch's own history and secrets — `sessions/`, `server.json`, `mongo.json`, mode `0600`; (3) `memory-audit.jsonl` plus `memory/.history/` and `memory/.trash/`; (4) `memory/*.md`, Claude Code's auto memory — local state, **not tracked** (the G9 carve was withdrawn 2026-07-31 and the committed copies purged from history); (5) **the tracked FILE `.touch/run.json`**, the per-project run constants `touch-run start` merges under a run spec (D-12) — configuration, not state. One carve, staged by NAME: `git add .touch/run.json`, **never** `git add .touch/` (GD-1/GD-16 as re-amended) |

`LICENSE` is the one deliberate duplicate — repo root and plugin root, required
by the plugin spec, machine-checked by `tests/test_plugin_tree.py` (GD-U7).

**Authority ladder (GD-3)** — highest first, each a
`.touch/local-orchestrators/<run>/plan/<run>-plan.md`:

1. `touch-determinism-plan.md` — determinism (GD-D1…GD-D15, D-01…D-26): what
   stops being an LLM chore and becomes a script. Its run-2 addendum
   `touch-determinism-modules-plan.md` carries a corrections register that
   outranks run-1's measured numbers.
2. `touch-memory-home-plan.md` — auto memory in `.touch/memory` (G1…G14):
   where run state and memory live, and what the file plane may do.
3. `touch-plugin-compliance-plan.md` — packaging (GD-C1…GD-C12).
4. `touch-plugin-unify-plan.md` — layout (GD-U1…GD-U9): where a file lives.
5. `touch-mongo-live-plan.md` — Mongo + live flow (GD-21…GD-30, R-38…R-58).
6. `touch-full-recon-plan.md` — the normative plan (GD-1…GD-20, R-01…R-37).
7. `touch-aggregator-plan.md` — design law D1–D14, as amended. **Not** an
   implementable plan any more.
8. `inception.md` → `README.md` → this file.

Cite **D8.1** (stack / stdlib-only, amended by GD-21) or **D8.2** (journal
`result` opaque, superseded) — a bare "D8" is ambiguous and means neither.
What each run folder was: `plugin/touch/docs/run-folders.md`.

## Runtime dependency policy (GD-21)

Stdlib-only **on the ingest and serve critical path**. `pymongo` (pinned
`==4.17.0`, with `dnspython`) is the ONE permitted third-party runtime
dependency, importable **only** from `plugin/touch/aggregator/mongo_store.py`
and `plugin/touch/aggregator/mirror.py`, lazily. Its absence shows as
`mirror: "absent"` in `/health` and breaks nothing else — not startup, not an
agent, not a test. Every other module must import with no third-party packages
installed, and every Mongo test must skip cleanly with no reachable mongod.
`tests/test_stdlib_only.py` enforces this, exception included — do not add a
second dependency by analogy.

## The monitoring module

`plugin/touch/shared/monitoring/` is a working, dependency-free (bash +
Python 3 stdlib + browser) implementation of live orchestrator monitoring — the
substrate Touch inherits. Normative for its event schema:
`plugin/touch/shared/monitoring/monitoring.md`.

```
agents ──status.sh──┐
                    ├──> <task-dir>/events.jsonl ──> monitor_server.py ──ws──> monitor.html
Workflow journal ───┘        (append-only,          (HTTP + WebSocket)
  via decision_watcher.py     single source of truth)
```

- `status.sh <plan> <stage> <state> [detail]` appends one JSON event line and is
  the ONLY write path into `events.jsonl` (GD-D5). It wants `ORCH_STATE_DIR`;
  without it it resolves the tasks root in **three** rungs — `$ORCH_TASKS_ROOT`
  > `$CLAUDE_PROJECT_DIR/.touch/local-orchestrators` > a cwd walk-up to a
  `.claude/` marker, then join `.touch/local-orchestrators` onto it (marker dir
  and state dir are deliberately different names) — and writes the NEWEST task
  folder there, warning loudly. If that fails it **exits 2** rather than
  spooling into the module directory, which an update sweeps. The old fourth
  rung (module-relative `../../local-orchestrators`) is **deleted**; no wrapper
  may describe it.
- `decision_watcher.py` tails a run's `journal.jsonl` and derives
  spawn/verdict/retry/advance events plus per-agent token accounting from the
  `[monitor] plan=… stage=… role=… attempt=…` marker in every agent prompt.
  That marker is the **deterministic** source and is fenced (GD-D1a): never
  trimmed, renamed or deleted; `status.sh` calls inside agents are best-effort
  colour only. Checkpointed in `.watcher-state.json` (restart-safe); it closes
  the run from the snapshot or the task notification, then self-exits.
- `monitor_server.py` serves `monitor.html` at `/` and `memory.html` at
  `/memory`, streams events at `/ws` (full replay on connect, then live tail,
  `?task=`), plus `/tasks`, `/artifacts`, `/file` (extension-whitelisted,
  realpath contained), `/health`, and `/api/memory/{list,file}` — the ONLY route
  group on either server that parses an HTTP method and answers `405 Allow:`.
  One server serves all tasks; one watcher per task. Both writers stamp `w`
  (`"agent"` / `"watcher"`); readers ignore unknown keys.

The module is **stateless and task-agnostic** — never copy or modify it per
task. Per-run state lives in `.touch/local-orchestrators/<task-name>/`; its
anatomy and the `wf_dir` join: `plugin/touch/docs/run-folders.md`.

## The memory home (`.touch/memory`)

Claude Code's **auto memory** — the `MEMORY.md` index loaded at every
conversation start plus its topic notes — is mapped into this project at
`<project>/.touch/memory` by one documented key, **`autoMemoryDirectory`**,
merged into `.claude/settings.local.json` by `touch-selfcheck --init` (G1).
That is a program's job, not a hand edit: a relative or `$VAR`-interpolated
value is **silently rejected**, and three undocumented env vars outrank every
settings layer, so `--init` writes the key and then verifies it.

Four things to know before touching that directory: **these bytes are model
instructions** (they load into future sessions, so the write path refuses
`@`-imports, block HTML comments, token-shaped lines and unconfirmed `pinned:`
frontmatter); **memory is local, not published** (the tracked-subtree carve was
withdrawn 2026-07-31 — still write it as if it could be read, but it is no
longer committed); **subagents may not write it** (G14, the scope guard); and
the aggregator's WAL and the run history stay out of it.

Full account — the scope table of every memory kind, what does and does not
move, the `~/.claude` refusal, GD-13's three planes:
`plugin/touch/docs/memory-home.md`, with
`plugin/touch/docs/control-semantics.md` §5 normative for the file plane.

## The skills — four orchestration, six engineering practice

Ten skill directories under `plugin/touch/skills/`, invoked as `/touch:<name>`.
Four drive the loops (`research`, `implement`, `orchestrate`,
`monitor`); six are the engineering-practice set adopted under GD-U3
(architecture, patterns, refactoring, testing, code review — listed in
`README.md`). The six are advisory and defer to this repo's settled law where
they disagree, each saying so in its own preamble. All ten cost **~1,261 tok
always-on** — measured 2026-07-31 with `claude --plugin-dir plugin/touch plugin
details touch`, the command to re-run before changing that figure anywhere.

The orchestration pair:
`research` → ONE complete plan file → `implement` → implementation.

- `research`: parallel read-only researchers (one per perspective) with
  a barrier, then ONE synthesizer that writes `plan/<name>-plan.md` (global
  decisions + ordered items). Never partitions, never edits source.
- `implement`: a divider derives isolated sub-plans by **file ownership**
  (one file, exactly one owner), then per sub-plan runs a gated loop — brand-new
  implementer each attempt → read-only test gate → read-only adversarial
  critique — until green or MAX_ATTEMPTS, then a final aggregate gate over the
  merged change-set. **Serial by default**; parallel only when explicitly asked
  and only for disjoint file ownership.
- **Role → model (GD-5):** researcher / implementer / test-gate / critic =
  **Opus 5 at effort xhigh**; synthesizer, divider, main terminal agent, final
  review = **Fable**. Effort caps stay ≤ xhigh.
- Both `templates/*.workflow.js` are the **normative protocol** and are
  spec-driven: a run supplies a JSON run-spec and the template is copied
  byte-for-byte, never edited. Handoff between attempts is via
  `findings/<plan>-<gate>-attempt-<N>.md` paths, not inlined text. Never
  resume/continue/`SendMessage` a prior agent — always a fresh subagent.
- `plugin/touch/skills/orchestrate/SKILL.md` is the companion standard for
  spawning agents Touch can see and stop (hierarchical names, `[touch]` marker,
  spawn ledger, control-file loop).

Terminal events are protocol, not a nicety: each plan ends with a `plan done`
event and the run ends with `orchestrator complete done "<summary>"` — both
emitted deterministically by the watcher, the reporter or `touch-run close`,
never mandated of an agent. A plan whose agents all returned without a decisive
verdict settles **done** ("closed — no verdict"), **never `failed`** — the
fabricated FAILED badge was a real defect (R-58) and that rule must not be
re-broken.

## The context budget

The always-on prefix this repo owns is three things — this file,
`.touch/memory/MEMORY.md`, and the ten skills' `description:` lines — and every
agent of every run pays for all three on every turn. So they are capped, not
merely tidied:

| source | budget | measured 2026-07-31 |
|---|---|---|
| `CLAUDE.md` | ≤ 6,000 tok | ~5,851 |
| `.touch/memory/MEMORY.md` | ≤ 800 tok | ~251 |
| Σ ten skill `description:` values | ≤ 1,400 tok | ~914 |

The skills row and the ~1,261 quoted above measure the same ten skills
differently — chars/4 over the `description:` bytes here, the real tokenizer
plus per-skill metadata there. The ceiling is set on this repo's estimator,
chars/4 over bytes, calibrated once and pinned, because that is the one every
consumer of it shares. `tests/test_context_budget.py` declares the three
numbers and fails the build the way `tests/test_stdlib_only.py` does. To
measure:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=plugin/touch python3 -m aggregator.costs --baseline --repo .
```

`scripts/release.sh` runs the same reader against `RELEASE_CONTEXT_CEILING`, and
prefers the budget test's numbers once all three are declared. The `CLAUDE.md`
in the directory *above* this repository is out of this repo's write scope: the
reader reports it, nothing gates on it.

When a section here grows past a paragraph, move it into `plugin/touch/docs/`
and leave a pointer — that is what `memory-home.md`, `run-folders.md` and
`dev-loop.md` are.

## Commands

Tests — stdlib only, no pytest, no runner; every file is executable and exits
non-zero on failure:

```bash
tests/run_all.sh                     # BOTH suites (Touch + monitoring), fail-fast
tests/run_all.sh --keep-going        # run everything, report every failure
tests/run_all.sh --list              # what would run, in order
python3 tests/test_docs.py           # or run any single file directly
```

`run_all.sh` also runs the monitoring suite under `tests/monitoring/`, because
a green Touch suite over a red substrate would be a lie. The frontend tests
assert on **source text** — the HTML/JS is never executed by Python, with
`test_memory_ui.py` the one exception (`node` + `vm`, because a source guard
cannot see a clobber). `test_memory_hygiene.py` guards what may be committed
under `.touch/memory/`; `test_context_budget.py` guards the always-on prefix;
`test_docs.py` guards the claims in the docs you are reading.

**Serve blocks — two different programs on reserved ports.** "Reserved" means
by convention, not occupied: start what you need.

Every route but `/health` needs the per-boot token the server prints (also
written to `.touch/server.json`, 0600); the WS upgrade enforces an Origin/Host
allowlist; both wrappers REFUSE a non-loopback bind, so exposing one means
invoking the module yourself and publishing the port from the host.

```bash
# Touch (port 8932) — aggregator + touch-visual
touch-serve                                    # binds 127.0.0.1:8932 (GD-13 default)
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=plugin/touch python3 -m aggregator.server --open --allow-origin http://<host>:8932
sbx ports "$SANDBOX_VM_ID" --publish 8932:8932/tcp      # on the host
```

```bash
# Orchestrator run monitor (port 8931) — read-only over orchestration STATE
TASK=$PWD/.touch/local-orchestrators/<task-name>
ORCH_STATE_DIR="$TASK" touch-monitor &   # port: argv > $ORCH_PORT > config > 8931
ORCH_STATE_DIR="$TASK" touch-watcher &   # wf_dir: argv > $ORCH_WF_DIR > config > newest wf_*

# memory editor at /memory: reads need only the token; WRITES are off until
# asked for, and then the token must arrive in a header, never in ?token=
ORCH_STATE_DIR="$TASK" touch-monitor --allow-memory-write &   # or TOUCH_ALLOW_MEMORY_WRITE=1
```

A whole run, driven: `touch-run start <task> --spec <file>` lays out the folder,
seeds the cards and starts the daemons; `bind` records the `wf_dir` and renders
`plan/RESUME.md`; `close` settles the cards and stops what it started.

Those wrappers are THE entry points — the six a session runs, plus
`touch-selfcheck` by hand — on `PATH` when the plugin is enabled, otherwise out
of `plugin/touch/bin/`. The
`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=plugin/touch python3 -m aggregator.…`
form is the one sanctioned module-direct invocation: for hacking on the module,
for the bind the wrapper will not open, and for the two operator tools that get
no wrapper because no session runs them. Keep the first env var: every wrapper
exports it, so a hand-typed run is the only way an `aggregator/__pycache__`
reaches the payload and reddens `tests/test_package.py` and the next cut.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=plugin/touch python3 -c "import aggregator.mirror as m; raise SystemExit(m.main(['--check']))"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=plugin/touch python3 -m aggregator.costs --top 5   # what a run cost
```

Mongo recipe and security baseline: `plugin/touch/docs/mongo.md`.

## Rules that bite

- **When a run ends, stop its watcher; leave its state files in place.** The
  watcher self-exits after the journal goes quiet AND a terminal
  `orchestrator complete` event lands, and `touch-run close` stops the daemons —
  but check. Orphaned watchers are why the commit gate is scoped: **no commit
  while a watcher whose `ORCH_STATE_DIR` is inside the paths being committed is
  writing** (GD-1 as amended) — "the paths being committed" means the
  **pathspec-resolved tracked paths**, which makes the gate largely structural
  now that run state is gitignored. What replaces it operationally: **never
  `git add .touch/`; always `git add .touch/run.json` by name** — the one
  tracked path, staged by name, so a stray token file, a memory note or a
  `.history/` copy can never ride along.
  A watcher writing some *other* task's stream never blocks a commit.
  The mirror daemon follows the same lifecycle.
- **Every generated deliverable is stored in the repo, not only the claude.ai
  artifact store.** Any HTML artifact or research `.md` produced for a task must
  ALSO be written under `.touch/local-orchestrators/<task>/report/` or
  `findings/`. That local copy is the durable record; publishing is a mirror.
- **Never delete a finished task folder or its `events.jsonl`, and never
  REWRITE one either.** Completed runs are monitor history, they replay on
  connect, and the Mongo `legacy:` key space is positional
  (`legacy:<task>#<line>`), so it *depends* on the rule. A finished folder is
  dated record; the one sanctioned exception is its `plan/`, where the authority
  ladder lives. Details: `plugin/touch/docs/run-folders.md`.
- **Run scope guard**: while `.touch/local-orchestrators/ACTIVE` lists task
  names (one per line), the PreToolUse hook
  `plugin/touch/hooks/orch_scope_guard.py` denies SUBAGENT access to every
  unlisted task's folder except its `plan/`, and denies subagent
  `Write`/`Edit`/`NotebookEdit` on `.touch/memory/**` (G14). What it is: a
  **name-based speed bump — it bounds drift; it is not a containment
  boundary**. The Bash arm matches command TEXT, so the same access spelled
  through a `cd` and a relative path walks past it, an unexpanded `$var` in a
  matched segment is undecidable and warns rather than accusing, and a deny
  message says "an argument mentions task X", never "this call accessed task X".
  Over-restriction is the safe direction; the hook's docstring enumerates every
  gap. The main terminal agent is never restricted; no ACTIVE file means the
  guard is inert, and a stale line only over-restricts — delete it.
  **The hooks are registered EXACTLY ONCE, by the plugin's own
  `hooks/hooks.json`** — beside the hook scripts, not in `.claude-plugin/`, and
  `plugin.json` carries no `hooks` key (GD-U5). `.claude/settings.json` no
  longer carries a `hooks` block: the two registrations had the same matcher and
  fired the hook twice per tool call (measured 2 vs 1). Accepted consequence: a
  session started WITHOUT the plugin has no guard. Do not "restore" the
  settings.json form (`plugin/touch/docs/dev-loop.md`).
- Every `touch-status` call must set `ORCH_STATE_DIR`; a forgotten one writes
  into whatever task folder the writer resolves instead.
- Never `pkill -f` these scripts from a command line that spells the script name
   — bracket the first letter: `pkill -f "[m]onitor_server"`.
- Keep event `detail` strings short, single-line, and free of double quotes:
  the detail travels through a bash argument and a JS template literal before
  it is ever JSON, plus the 1 KB writer cap (GD-11).
- **Never write under `~/.claude/`.** It is a read-only tap: not transcripts,
  not journals, not settings. The memory feature does not bend this — the
  relocation is the escape hatch, so the files come to the project instead of
  Touch reaching into `~/.claude/`. The write plane refuses any target under
  `~/.claude` with a named 4xx, and a symlink out of the memory root is refused
  rather than followed.
- **Never publish the mongod port.** No `sbx ports … 27017`, not "just for a
  minute" — the mirror holds the same unredacted transcripts the token posture
  protects. `docker exec touch-mongo mongosh …` from inside the sandbox instead.
  Mongo being down or absent is otherwise a **non-event**: the live view is
  memory-authoritative; only history/backfill degrade, and `/health` says so.
