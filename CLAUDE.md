# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository. It is the session guide — the bottom of the authority
ladder. When it disagrees with a plan, the plan wins; fix this file.

## Project status

Touch is a web page for visualizing and managing subagents in a Claude Code
session, with two components — **aggregator** (Python) and **touch-visual**
(the page). The read side is implemented; **no session-control verb ships
yet** — nothing here starts, ends or re-invokes a session (GD-4).

One **write** plane does ship, and it is deliberately not a control verb
(GD-4 as amended): the run dashboard on 8931 lists, reads and — behind an
explicit `--allow-memory-write` / `$TOUCH_ALLOW_MEMORY_WRITE`, **off by
default** — edits Claude Code's auto-memory files under `<project>/.touch/memory/`.
Memory CRUD acts on files, not sessions: it never appears in the verb table,
never promotes a session class, and emits no `touch-status` event. See "The
memory home" below; `aggregator/server.py`'s `CONTROL_ROUTES` stays `{}`.

Repository layout. **`plugin/touch/` is the single canonical home** for
everything Touch ships (GD-U1): there is no second copy of any of it at the
repo root any more, and no sync script. What is not under `plugin/touch/` is
development-only material that deliberately never ships — with one exception,
`.claude-plugin/marketplace.json` at the repo root, which is not payload but a
catalog *about* the payload, and the substrate reads it only from there.

| path | what it is |
|---|---|
| `plugin/` | holds exactly one thing: `plugin/touch/`, the Claude Code plugin payload — and, since GD-U1, the canonical source tree |
| `plugin/touch/aggregator/` | the Python package: `tailer.py`, `store.py`, `ws.py`, `sessions.py`, `ingest.py`, `legacy.py`, `agents.py`, `custom_state.py`, `refs.py`, `paths.py`, `mongo_store.py`, `mirror.py`, `server.py`. One file, exactly one owner |
| `plugin/touch/touch-visual/` | `index.html`, `app.js`, `style.css` — v0 is read-only; no control affordance renders |
| `plugin/touch/docs/` | `mongo.md` (database deployment/security), `control-semantics.md` (verb ladder, session classes) |
| `plugin/touch/shared/monitoring/` | the six monitoring core files — `status.sh`, `monitor_server.py`, `decision_watcher.py`, `monitor.html`, `memory.html` (the memory editor page, G4), `monitoring.md`. The live daemons execute from here |
| `plugin/touch/skills/` | the ten skills (four orchestration + six engineering-practice), one directory each |
| `plugin/touch/bin/` | the six wrappers Touch puts on `PATH`: `touch-serve`, `touch-monitor`, `touch-watcher`, `touch-status`, `touch-cycle-reporter` — the five programs a session runs (GD-U4) — plus `touch-selfcheck`, which verifies an installation and is run by hand |
| `plugin/touch/hooks/` | `orch_scope_guard.py`, the run-scope `PreToolUse` hook, plus the `hooks/hooks.json` that registers it — the one and only registration (GD-U5) |
| `plugin/touch/.claude-plugin/` | `plugin.json` (the ONE place a version is declared) — exactly one file; neither the hook manifest nor the marketplace catalog is here |
| `.claude-plugin/` (repo root) | `marketplace.json`, the catalog: marketplace `msdrx-tools`, one entry, `"source": "./plugin/touch"`. It is at the ROOT because `/plugin marketplace add msdrx/touch` clones the repo and reads `<clone>/.claude-plugin/marketplace.json` and nothing else — a remote **marketplace** source has no subdirectory form (verified, CLI 2.1.220; a *plugin* source may be a `git-subdir`, a form weighed and declined under GD-C8 because it re-creates the two-repos-to-sync model GD-U1 abolished). This repo therefore IS the marketplace. The entry carries `displayName`/`category`/`tags` but no `version` and no `description` — those two are declared once, in `plugin.json` |
| `tests/` | one standalone executable per module + `run_all.sh` + `fixtures/` (frozen corpora with a sha256 manifest) + `_roots.py`, the one anchor every test names the canonical trees through |
| `tests/monitoring/` | the monitoring module's own dev-only suite and fixtures, moved out of the module so the payload boundary stays the directory boundary (GD-U6) |
| `scripts/` | `release.sh` — the release checklist, executable, and there is deliberately no RELEASE.md |
| `README.md` | intent, the honest verb table, how to run it |
| `CONTRIBUTING.md` | layout, ground rules, the test law, the release gate |
| `inception.md` | everything verified about the substrate (CLI 2.1.220), summarized — a dated snapshot, so its pre-plugin paths are history, not directions |
| `.claude/` | `settings.json` (exactly two keys: status line + `enabledPlugins: {"touch@inline": true}` — no `extraKnownMarketplaces`, no second enabled id, GD-C1), `statusline.sh`, two unrelated `shared/scripts/*-sox-installation.sh` helpers — those four files are all `git ls-files .claude` returns — plus the untracked `settings.local.json`, the ONE place `autoMemoryDirectory` may be written (G1). The directory itself is still the project MARKER every resolver walks up to; it no longer holds any run state |
| `.touch/` | project-local state, gitignored except one subtree, and **four trust classes in one directory**: (1) run history — `local-orchestrators/<task>/`, moved here from `.claude/` (G10/G11); (2) Touch's own history and secrets — `sessions/`, `server.json`, `mongo.json`, mode `0600`; (3) the memory audit log `memory-audit.jsonl` plus `memory/.history/`, `memory/.trash/`; (4) **the ONE tracked subtree, `.touch/memory/*.md`** — Claude Code's auto memory, editable from the dashboard, public the moment it is committed. Stage it as `git add .touch/memory`, **never** `git add .touch/` (GD-1/GD-16 as amended) |

`LICENSE` is the one deliberate duplicate — repo root and plugin root, required
by the plugin spec, machine-checked byte-for-byte by `tests/test_plugin_tree.py`
(GD-U7). Nothing else in the tree is pinned to anything.

**Authority ladder (GD-3)** — highest first:

1. `.touch/local-orchestrators/touch-memory-home/plan/touch-memory-home-plan.md`
   — the memory/tasks-root amendment: auto memory mapped into
   `.touch/memory` (G1/G2), memory CRUD on the monitoring server (G3…G8),
   the `.gitignore` carve (G9) and the tasks-root move (G10…G14). It outranks
   everything below on where run state and memory live and on what the file
   plane may do.
2. `.touch/local-orchestrators/touch-plugin-compliance/plan/touch-plugin-compliance-plan.md`
   — the packaging amendment: one dev-loop identity, the catalog entry's card
   fields, the release gates, honest install docs (GD-C1…GD-C12, C-01…C-18).
   It outranks everything below on how Touch is packaged and published.
3. `.touch/local-orchestrators/touch-plugin-unify/plan/touch-plugin-unify-plan.md`
   — the layout amendment: `plugin/touch/` canonical, the six adopted skills
   (GD-U1…GD-U9). It outranks everything below on where a file lives.
4. `.touch/local-orchestrators/touch-mongo-live/plan/touch-mongo-live-plan.md`
   — the amendment: Mongo + live flow (GD-21…GD-30, R-38…R-58).
5. `.touch/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md`
   — the normative plan (GD-1…GD-20, R-01…R-37).
6. `.touch/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md`
   — design law D1–D14, as amended. **Not** an implementable plan any more.
7. `inception.md` → `README.md` → this file.

Cite **D8.1** (stack / stdlib-only, amended by GD-21) or **D8.2** (journal
`result` opaque, superseded) — a bare "D8" is ambiguous and means neither.

## Runtime dependency policy (GD-21)

Stdlib-only **on the ingest and serve critical path**. `pymongo` (pinned
`==4.17.0`, with `dnspython`) is the ONE permitted third-party runtime
dependency, importable **only** from `plugin/touch/aggregator/mongo_store.py`
and `plugin/touch/aggregator/mirror.py`, lazily. Its absence degrades the
mirror to `mirror: "absent"` in `/health`; it never fails startup, never
breaks an agent, never blocks a test. Every other module must import with no
third-party packages installed, and every Mongo test must skip cleanly with
no reachable mongod. `tests/test_stdlib_only.py` enforces this, exception
included — do not add a second dependency by analogy.

## The monitoring module (and why it matters)

`plugin/touch/shared/monitoring/` is a working, dependency-free (bash + Python 3
stdlib + browser) implementation of live orchestrator monitoring — the
prior-art prototype of Touch's visual half, and the substrate Touch inherits.
It shipped from `.claude/shared/monitoring/` until GD-U1; that path is gone,
and the daemons already executed from the plugin copy before the move, which
is why moving it cost nothing. Full reference:
`plugin/touch/shared/monitoring/monitoring.md` (normative for its event
schema). Its own dev-only suite and fixtures live at `tests/monitoring/`, out
of the payload (GD-U6).

```
agents ──status.sh──┐
                    ├──> <task-dir>/events.jsonl ──> monitor_server.py ──ws──> monitor.html
Workflow journal ───┘        (append-only,          (HTTP + WebSocket)
  via decision_watcher.py     single source of truth)
```

- `status.sh <plan> <stage> <state> [detail]` — appends one JSON event line.
  Requires `ORCH_STATE_DIR`. Without it, it resolves the project's tasks root
  (`$ORCH_TASKS_ROOT` > `$CLAUDE_PROJECT_DIR/.touch/local-orchestrators` > a
  cwd walk-up to a `.claude/` marker, then join `.touch/local-orchestrators`
  onto it — the same **three** rungs both daemons use, and yes, the marker dir
  and the state dir are deliberately different names) and writes to the NEWEST
  task folder there, warning loudly; if that fails too it exits 2 rather than
  spooling into the module directory — an installed payload is a
  version-stamped cache that gets swept, so a write there is data loss with
  extra steps. The old fourth rung (a module-relative `../../local-orchestrators`)
  is **deleted**: GD-U1 left it with nothing to resolve to, and inside a
  packaged copy it would glob sibling plugins (GD-T5 as amended).
- `decision_watcher.py` — tails a Workflow run's `journal.jsonl` and derives
  spawn/verdict/retry/advance events plus per-agent token accounting from the
  `[monitor] plan=… stage=… role=… attempt=…` marker embedded in every agent
  prompt. That marker is the **deterministic** source — it works with zero LLM
  cooperation; `status.sh` calls inside agents are best-effort colour only.
  Checkpointed in `.watcher-state.json` (restart-safe, never double-counts).
- `monitor_server.py` — serves `monitor.html` at `/`, streams events at `/ws`
  (full replay on connect, then live tail, `?task=<name>`), plus `/tasks`,
  `/artifacts?task=`, `/file?task=&path=` (extension-whitelisted, realpath
  contained), `/health`. Since the file plane it also serves `memory.html` at
  `/memory` and the JSON group `/api/memory/{list,file}` — the ONLY route group
  on either server that parses an HTTP method, dispatches on `(method, route)`,
  reads a request body and answers `405 Allow:` / a JSON `404` under its own
  prefix. One server serves all tasks; one watcher per task.
- Both writers stamp `w` (`"agent"` / `"watcher"`) so every line's author is
  known; readers ignore unknown keys.

Other `.claude/` files worth knowing: `.claude/settings.local.json` (untracked,
per-checkout — the one file `autoMemoryDirectory` may be written into, by
`touch-selfcheck --init`, G1) and `.claude/settings.json` (committed,
session-wide — exactly two keys, the status line and
`"enabledPlugins": {"touch@inline": true}` so every
`--plugin-dir plugin/touch` session auto-enables Touch, and **nothing else**;
it registers NO hooks, GD-U5) and `.claude/statusline.sh` (which shells out to
`jq`; that is a **status-line-only** exception and is not a licence for `jq`
anywhere in Touch's own code or tests).

**Why settings.json is that short, and must stay that way (GD-C1).** It once
also carried an `extraKnownMarketplaces` entry pointing `msdrx-tools` at this
checkout, plus `"touch@msdrx-tools": true`. Both are gone:

- Marketplace registration is keyed by catalog **name** and stored **per user,
  globally**, so a same-name add silently REPLACES the previous registration.
  Anyone who had installed the published Touch would have their real
  `msdrx-tools` repointed at a working tree the moment they trusted this
  folder — in every project on that machine. That hijack is the whole reason.
- An `enabledPlugins` entry at any scope overrides `defaultEnabled: false`,
  which is the manifest's entire consent posture for a hook-carrying plugin.
  One id is one deliberate opt-in for the dev loop; two ids is two.
- The key bought nothing anyway: the `claude plugin install`/`marketplace`
  subcommands do not read `extraKnownMarketplaces` (reproduced twice), and the
  dev loop is `claude --plugin-dir plugin/touch`, which `touch@inline` already
  serves. To exercise the marketplace install path, `claude plugin marketplace
  add <checkout>` in a throwaway `CLAUDE_CONFIG_DIR` — never via committed
  settings.

Measured, so the GD-U5 double-fire fear is not re-litigated: with the same
plugin present both as `--plugin-dir` and as an installed copy, a hook fires
**once** — the `--plugin-dir` copy shadows the installed one (probe plugins,
one appended line per invocation, 1 fire not 2). That shadowing rule is
unwritten upstream, so nothing may depend on it; the fix above removes the
dependency instead of documenting a reliance on it.

The module is **stateless and task-agnostic** — never copy or modify it per
task. Per-run state lives in `.touch/local-orchestrators/<task-name>/`
(`events.jsonl`, `orch-config.json`, `.watcher-state.json`, `orch-scripts/`,
`findings/`, `plan/`, `report/`).

## The memory home (`.touch/memory`)

Claude Code's **auto memory** — the `MEMORY.md` index it loads at every
conversation start plus its topic notes — is mapped into this project at
`<project>/.touch/memory` by exactly one documented key,
**`autoMemoryDirectory`**, merged into `.claude/settings.local.json` by
`touch-selfcheck --init` (G1). Two things make that a program's job and not a
hand edit: the value must be **absolute** (or `~/`-prefixed) — a relative or
`$VAR`-interpolated path is **silently** rejected, the validator returns
`undefined` and the CLI falls back to the default with no error and no warning —
and three **undocumented** env overrides
(`CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`, `CLAUDE_CODE_REMOTE_MEMORY_DIR`,
`CLAUDE_MEMORY_STORES`) outrank every settings layer. So `--init` writes the
key and then *verifies*: it prints the effective directory, reports any of those
three that is set (it reports them and refuses to read them — they are a
diagnosis trap, never a mechanism), and fails loudly when the answer is the
default rather than the directory it just configured. It never writes
`.claude/settings.json` — GD-C1's exactly-two-keys rule stands.

What moves, and what does not (this table is the scope; `test_docs.py` guards
that every kind is listed):

| kind | location | this plan |
|---|---|---|
| Auto memory (`MEMORY.md` + topic notes) | `~/.claude/projects/<key>/memory/` | **moves** → `.touch/memory` via `autoMemoryDirectory` |
| Project `CLAUDE.md` content | `./CLAUDE.md` | stays; may optionally `@import` a file from `.touch/memory` |
| User `~/.claude/CLAUDE.md`, managed-policy CLAUDE.md | fixed | out of scope, read-only |
| Enclosing-directory CLAUDE.md above the repo | outside repo | out of scope, read-only |
| Subagent memory (`~/.claude/agent-memory/`, `.claude/agent-memory*/`) | fixed | **no relocation mechanism exists** — out of scope |

The editor writes **only** `<project>/.touch/memory/*.md`. Every other tier is
read-only, and a `~/.claude/**` target is refused with a named 4xx rather than
silently resolved — that refusal is what keeps the "`~/.claude/` is a read-only
tap" promise literally true while a write plane exists.

Four consequences worth internalising before you touch that directory:

- **These bytes are model instructions.** Anything in there is loaded into
  future sessions of this project — the index always, a topic note on demand,
  and a file carrying `pinned:` frontmatter into *every* session, unasked. The
  write path therefore refuses `@`-imports outside code spans, block HTML
  comments, token-shaped and credentialed-URI lines (by category, never
  quoting the match), and `pinned:` without an explicit confirmation.
- **Memory is public now.** `.touch/memory/*.md` is the one tracked subtree of
  `.touch/`; write it as if it ships, because it does.
- **Subagents may not write it.** The scope guard denies subagent
  `Write`/`Edit`/`NotebookEdit` on `.touch/memory/**` (G14). Edits come from the
  main terminal agent or the flag-gated HTTP plane, and the audit log is
  `.touch/memory-audit.jsonl` — never `events.jsonl`, never a plan badge.
- **The two trees stay apart, both ways** (`store.state_root` as amended): the
  aggregator's WAL never lives under run history, and run history never lives
  under a tracked subtree. `.touch/` holds both, which is exactly why the
  ignore carve is an allowlist of one pattern and not a directory.

**GD-13, as amended.** There are three planes now, not two: **read** (the
transcripts, journals and event streams both servers tail), **control** (the
verb ladder — unshipped), and **file** (this section). "Read-only" is scoped to
orchestration *state*: the monitoring server writes no event, and its per-boot
token — which the original GD-13 wording had declined for it — has been
implemented for some time. `plugin/touch/docs/control-semantics.md` §5 is the
normative account of the file plane; `plugin/touch/shared/monitoring/monitoring.md`
documents the routes.

## The run folders — what each one actually is

One folder per run, **all produced by this repo's own runs** — the table below
is the index, so no count here can fall behind it. Every
`orch-config.json` on disk names a `wf_dir` under
`~/.claude/projects/-home-laniakea-Projects-touch/…/subagents/workflows/`, so
`wf_dir` is the join key from a task folder to its harness journal. (An earlier
version of this file claimed these were carried-over examples from a different
project — that was false; verified again 2026-07-26.) A `wf_dir` that no longer
exists means "archived — source transcripts unavailable", never "wrong repo".

| folder | what it was | state | authoritative artifact |
|---|---|---|---|
| `touch-repo-recon` | first recon of the repo + skills | complete | `findings/` (51 findings) |
| `touch-aggregator` | 6-perspective research → design law | complete | `plan/touch-aggregator-plan.md` (D1–D14) |
| `touch-monitor-spawn` | a v0 slice planned from conversation | **plan only, never run** | `plan/touch-monitor-spawn-plan.md` (historical) |
| `touch-full-recon` | 6-perspective re-recon | complete | `plan/touch-full-recon-plan.md` (**normative**) + `report/probes.md` |
| `touch-mongo-live` | Mongo/live-flow research, then its implementation pass | complete | `plan/touch-mongo-live-plan.md` (**amendment**) + `plan/touch-mongo-live-subplans.md` |
| `touch-monitor-perf` | monitoring-module performance/robustness pass | complete, with follow-up work recorded | `plan/touch-monitor-perf-plan.md` + `plan/POST-RUN.md` |
| `touch-plugin-pack` | packaging Touch as a Claude Code plugin | packaging items complete; items 01/06/13 still open | `plan/touch-plugin-pack-plan.md` + `plan/RESUME.md` |
| `reflection-plugin` | a superseded 33-item plan; its implementation landed 3 items and was halted | **halted** | `plan/reflection-plugin-plan.md` (historical) |
| `touch-plugin-unify` | `plugin/touch/` made canonical + the six skills adopted | complete | `plan/touch-plugin-unify-plan.md` (**amendment**, GD-U1…GD-U9) + `plan/touch-plugin-unify-subplans.md` |
| `touch-plugin-compliance` | alignment with the official plugin/marketplace standard: one dev identity, catalog entry card fields, release gates, honest install docs | complete | `plan/touch-plugin-compliance-plan.md` (**amendment**, GD-C1…GD-C12, C-01…C-18) + `plan/touch-plugin-compliance-subplans.md` + `report/endgame-acceptance.md` |
| `touch-memory-home` | auto memory mapped into `.touch/memory`, memory CRUD on the monitoring server, and the tasks-root move out of `.claude/` | plan complete, this implementation pass | `plan/touch-memory-home-plan.md` (**amendment**, G1…G14, I1…I17) + `plan/touch-memory-home-subplans.md` |

A `plan/` or `report/` directory may legitimately be empty — that is a
recognized kind ("plan only / never run"), not a broken folder, and it is why
empty ones carry a `.gitkeep`.

## The skills — four orchestration, six engineering practice

Ten skill directories under `plugin/touch/skills/`, all invoked as
`/touch:<name>`. Four drive the loops (`execute-research`, `implement-plan`,
`orchestrate`, `m-orchestrator`); six are the engineering-practice set adopted
under GD-U3 — `architecture-boundaries`, `architecture-tradeoffs`,
`code-quality-review`, `pattern-selection`, `refactoring-pass`,
`testing-discipline`. The six are advisory and defer to this repo's settled
law where they disagree (each says so in its own preamble); they cost
~1,257 tokens always-on across all ten, and that figure is a **measured**
claim — re-measure with `claude --plugin-dir plugin/touch plugin details touch`
before changing it anywhere.

The orchestration pair, in detail:
`execute-research` → ONE complete plan file → `implement-plan` → implementation.

- `execute-research`: parallel read-only researchers (one per perspective,
  `opus`) with a barrier, then ONE `fable` synthesizer that writes
  `plan/<name>-plan.md` (global decisions + ordered items). Never partitions,
  never edits source.
- `implement-plan`: a `fable` divider derives isolated sub-plans by **file
  ownership** (one file, exactly one owner), then per sub-plan runs a gated
  loop — brand-new implementer each attempt → read-only test gate → read-only
  adversarial critique — until green or MAX_ATTEMPTS, then a final aggregate
  gate over the merged change-set. **Serial by default**; parallel only when
  explicitly asked and only for disjoint file ownership.
- **Role → model (GD-5):** researcher / implementer / test-gate / critic =
  **Opus 5 at effort xhigh**; synthesizer, divider, main terminal agent, final
  review = **Fable**. Effort caps stay ≤ xhigh.
- Both skills' `templates/*.workflow.js` are the **normative protocol** (prompts,
  schemas, models, markers, status calls). Adapt a copy into the task folder's
  `orch-scripts/`; don't diverge from the invariants.
- Handoff between attempts is via `findings/<plan>-<gate>-attempt-<N>.md` file
  paths, not inlined text.
- Never resume/continue/`SendMessage` a prior agent — always a fresh subagent.
- `plugin/touch/skills/orchestrate/SKILL.md` is the companion standard for
  spawning agents Touch can see and stop (hierarchical names, `[touch]` marker,
  spawn ledger, control-file loop).

Terminal events are part of the protocol, not a nicety: each plan ends with
`touch-status <plan> plan done` and the run ends with
`touch-status <run> orchestrator complete done "<summary>"`. A plan whose agents
all returned without a decisive verdict settles **done** ("closed — no
verdict"), **never `failed`** — the fabricated FAILED badge was a real defect
(R-58) and the rule that killed it must not be re-broken.

## Commands

Tests — stdlib only, no pytest, no runner; every file is executable and exits
non-zero on failure:

```bash
tests/run_all.sh                     # BOTH suites (Touch + monitoring), fail-fast
tests/run_all.sh --keep-going        # run everything, report every failure
tests/run_all.sh --list              # what would run, in order
python3 tests/test_docs.py           # or run any single file directly
```

`tests/run_all.sh` also runs the monitoring-module suite under
`tests/monitoring/` (`test_server.py`, `test_watcher.py`, `test_shell.py`,
`test_frontend.py`, `test_memory_api.py`, `test_memory_ui.py`, plus the perf and
WebSocket-e2e files), because a green Touch suite over a red substrate would be
a lie.
`test_frontend.py` and `test_touch_frontend.py` assert on **source text** (the
fixed pattern present, the vulnerable one absent) — the HTML/JS is never
executed by Python; `test_memory_ui.py` is the exception that runs `memory.html`
for real, under `node` + `vm`, because a source guard cannot see a clobber.
`test_bootstrap.py` guards `.gitignore` and the git bootstrap;
`test_memory_hygiene.py` guards what may be committed under `.touch/memory/`;
`test_docs.py` guards the claims in the docs you are reading.

**Serve blocks — two different programs on reserved ports.** "Reserved" means
by convention, not occupied: start what you need.

```bash
# Touch (port 8932) — aggregator + touch-visual
touch-serve                                    # binds 127.0.0.1:8932 (GD-13 default)
# every route but /health needs the per-boot token it prints; it is also
# written to .touch/server.json (0600). WS upgrade enforces an Origin/Host
# allowlist. The wrapper REFUSES a non-loopback bind, so exposing it means
# invoking the module yourself, then publishing from the host:
PYTHONPATH=plugin/touch python3 -m aggregator.server --open --allow-origin http://<host>:8932
sbx ports "$SANDBOX_VM_ID" --publish 8932:8932/tcp      # on the host
```

```bash
# Orchestrator run monitor (port 8931) — read-only over orchestration STATE
# (it never writes an event), this is what live orchestration runs report to
TASK=$PWD/.touch/local-orchestrators/<task-name>
ORCH_STATE_DIR="$TASK" touch-monitor &   # port: argv > $ORCH_PORT > config > 8931
ORCH_STATE_DIR="$TASK" touch-watcher &   # wf_dir: argv > $ORCH_WF_DIR > config > newest wf_*
sbx ports "$SANDBOX_VM_ID" --publish 8931:8931/tcp      # on the host

# The same server hosts the memory editor at /memory. Reads need only the
# per-boot token; WRITES are off until you ask for them, and then the token
# must arrive in a header, never in ?token=:
ORCH_STATE_DIR="$TASK" touch-monitor --allow-memory-write &   # or TOUCH_ALLOW_MEMORY_WRITE=1
```

`touch-serve` / `touch-monitor` / `touch-watcher` / `touch-status` /
`touch-cycle-reporter` are THE entry points a session runs (GD-U4);
`touch-selfcheck` is the sixth wrapper, a by-hand check of an installation.
They are on `PATH` in any session with the plugin enabled; otherwise run them
out of `plugin/touch/bin/`. The
`PYTHONPATH=plugin/touch python3 -m aggregator.server` form above is the one
sanctioned module-direct invocation *of the server* — for hacking on the
module, and for the bind the wrapper will not open for you. `touch-serve`
prints the same line with `python3 -P` because it may be run from any cwd on a
consumer's machine; the docs omit `-P` deliberately, because after GD-U1 there
is no `aggregator/` at the repo root for the cwd to shadow. Add it back if you
run this from somewhere that has one.

Optional Mongo mirror (see `plugin/touch/docs/mongo.md` for the recipe and the
security baseline). The mirror has no `bin/` wrapper by design — it is an
operator tool, not a program a session runs — so the module form below is its
only entry point, and GD-U4's one-wrapper-per-program rule has nothing to say
about it:

```bash
PYTHONPATH=plugin/touch python3 -c "import aggregator.mirror as m; raise SystemExit(m.main(['--check']))"
# --health / --rebuild / --backfill also exist; all print redacted JSON
```

## Rules that bite

- **When a run ends, stop its watcher; leave its state files in place.** The
  watcher also self-exits after the journal goes quiet AND a terminal
  `orchestrator complete` event lands, and the driver epilogue stops the
  daemons — but check. Orphaned watchers are why the commit gate is scoped:
  **no commit while a watcher whose `ORCH_STATE_DIR` is inside the paths being
  committed is writing** (GD-1 as amended) — and "the paths being committed"
  means the **pathspec-resolved tracked paths**, which since the move makes the
  gate largely structural: run state under `.touch/local-orchestrators/` is
  gitignored, so no pathspec resolves to it. What replaces it operationally:
  **never `git add .touch/`; always `git add .touch/memory`** — the one tracked
  subtree, staged by name, so a stray token file or a `.history/` copy can never
  ride along. A watcher writing some *other* task's stream never blocks a
  commit. The mirror daemon follows the same lifecycle.
- **Every generated deliverable is stored in the repo, not only the claude.ai
  artifact store.** Any HTML artifact (report, diagram, dossier) and any
  research/analysis `.md` produced while working on a task must ALSO be written
  under `.touch/local-orchestrators/<task>/report/` (HTML) or `findings/`
  (`.md` notes) of the task it belongs to. The monitor's artifacts strip lists
  them automatically (`/artifacts`, depth ≤ 3, reports first) — that local copy
  is the durable record; publishing to claude.ai is a share mirror, never the
  storage. Workflow: write the file, `cp` it into the task folder, then publish.
- **Never delete a finished task folder or its `events.jsonl`** — completed runs
  are monitor history and replay on connect, and the Mongo `legacy:` key space
  is positional (`legacy:<task>#<line>`), so it *depends* on that rule. There is
  no cleanup step. Wiping is only for a task you are actively re-running (stop
  daemons, delete `events.jsonl` + `.watcher-state.json`, re-seed, restart).
  **Never REWRITE one either**: a finished folder — its `orch-scripts/`,
  `orch-config.json`, `RESUME.md`, findings — is dated record, so a path
  migration deliberately stops at the payload, `tests/` and the top-level docs
  and leaves the old absolute paths inside finished runs exactly as they were.
  The one sanctioned exception is a finished run's `plan/` state, which the
  authority ladder lives in.
- **Run scope guard**: while `.touch/local-orchestrators/ACTIVE` lists task
  names (one per line), the PreToolUse hook
  `plugin/touch/hooks/orch_scope_guard.py` denies SUBAGENT access to every
  unlisted task's folder except its `plan/` (the authority ladder lives
  there). It reads BOTH roots during the transition — `.touch/` first, then the
  legacy `.claude/` one — so the flip order never disarms it and `HALT` stays
  live; it also denies subagent `Write`/`Edit`/`NotebookEdit` on
  `.touch/memory/**` (G14), because co-locating memory with run history would
  otherwise hand a subagent a bigger capability than the guard exists to
  withhold. The main terminal agent is never restricted; no ACTIVE file means
  the guard is inert. Drivers append their task's line at daemon start and
  delete only that line at close-out (m-orchestrator §4); a stale line only
  over-restricts — delete it.
  **It is registered EXACTLY ONCE, by the plugin's own `hooks/hooks.json`** —
  the file sits beside the hook script, not in `.claude-plugin/`, and
  `plugin.json` carries no `hooks` key (GD-U5). `.claude/settings.json` no
  longer carries a `hooks` block either: the two registrations had the same
  matcher and fired the hook twice per tool call (measured 2 vs 1). The
  consequence, accepted deliberately: a session started WITHOUT the plugin has
  no guard — fine, because the guard is inert without an ACTIVE file and every
  orchestration run needs the plugin's `bin/` anyway. Do not "restore" the
  settings.json form.
- Every `touch-status` call must set `ORCH_STATE_DIR`; a forgotten one writes
  into whatever task folder the writer resolves instead, which is rarely the
  one you meant.
- Never `pkill -f` these scripts from a command line that spells the script name
   — bracket the first letter: `pkill -f "[m]onitor_server"`.
- Keep event `detail` strings short, single-line, and free of double quotes.
  The reason is **shell and JS-template embedding** — the detail travels through
  a bash argument and a JS template literal before it is ever JSON — plus the
  1 KB writer cap (GD-11). JSON itself would survive the quotes; the pipeline
  will not.
- **Never write under `~/.claude/`.** It is a read-only tap: not transcripts,
  not journals, not settings. The memory feature does not bend this: the
  relocation is the escape hatch — a key in the project's own
  `.claude/settings.local.json` points the CLI at `.touch/memory`, so the files
  come to the project instead of Touch reaching into `~/.claude/`. The write
  plane refuses any target under `~/.claude` with a named 4xx, and a symlink out
  of the memory root is refused rather than followed.
- **Never publish the mongod port.** No `sbx ports … 27017`, not "just for a
  minute" — the mirror holds the same unredacted transcripts the token posture
  protects. `docker exec touch-mongo mongosh …` from inside the sandbox instead.
- Mongo being down, absent, or unreachable is a **non-event**: the live view is
  memory-authoritative and unaffected; only history/backfill degrade, and
  `/health` says so.
