# Changelog

Versions here are the `version` field of `.claude-plugin/plugin.json`, which is
the only place Touch declares one. Third-party marketplaces do not auto-update,
and a release without a version bump delivers nothing — so an entry below is a
version users have to ask for. See the README's *Update / uninstall* section
for the two commands.

**0.2.0 is the first version a marketplace serves.** 0.1.0 is kept below as the
development record of what existed before it; no marketplace ever served it and
no one could install it, so there is no upgrade path from it to document and
nothing to migrate. If you installed Touch at all, you installed 0.2.0 or later.

## 0.2.4

Still no session verb, and the memory editor's save side still ships disabled
until you start the dashboard with `--allow-memory-write`. This release is about
what a run HANDS OVER. A monitored run already showed you every stage as it
happened; when it ended you got a dashboard full of green cards and no answer to
the only question that outlives the run — **was the thing that was asked for
actually built, and where does what shipped differ from what was decided?** So
every report now leads with that comparison, derived from recorded results
only, and the run's end-of-run page is rendered by a program rather than
narrated by an agent.

**Added**

- **The requirement → implemented → Δ diagram**, on every cycle page and on the
  final report. For an implement run the requirement is the divider's
  `finding_ids` and the delivery is each implementer's per-item `items`; for a
  research run it is the researchers' finding board against the synthesizer's
  `coverage`. One row per item: what was required, what was built, and where the
  two read-only verdicts say the tree differs (`missing` | `differs` | `extra`).
  A plan item nobody reported renders **unreported**, and work beyond the
  requirement renders **extra** — silence about an item is the one thing a
  coverage diagram may not render as coverage. Every cell is glyph + word +
  colour, so it survives a colourblind reader, a greyscale print and a
  plain-text scrape.
- **`touch-cycle-reporter --final`** — the run's end-of-run page, written into
  the run folder under `.touch/local-orchestrators/<task>/report/`
  (`final-report.html`, or `research-report.html` for a research run) and
  diagram-first: the run shape, the coverage diagram(s), the timeline, the
  attempt-by-attempt cards behind a fold, then links to the plan and every
  findings file. Every number has ONE named source, the page carries no render
  timestamp, and rendering the same inputs twice is byte-identical. Exactly one
  slot is authored rather than derived — the narrative section, injected from a
  file with `--narrative`.
- **Per-surface report config (`reports`)** — three surfaces, `cycle`,
  `research` and `final`, each `{enabled, publish}`, set in the run spec or once
  per project in `.touch/run.json` (merged surface by surface AND key by key, so
  naming one never silently resets the others). `touch-run start` refuses a
  malformed map before anything is created, publishes the effective one whole
  into `orch-config.json` and prints it; the reporter re-reads it live, so a
  mid-run edit needs no restart, and `touch-run status` reads it back.
  **`enabled: false` stops PAGES and nothing else** — every loop close, protocol
  close and roster event still fires, so no card is left "running" because
  reporting was turned off.
- **New structured fields the diagram is derived from**: `items` on the
  implementer verdict, `deviations` on both read-only verdicts, and `coverage`
  on the research synthesizer — each an id, a status and one ≤120-char line. The
  argument and the evidence stay in the findings file; these are the diagram's
  labels, and they are schema-enforced by the templates rather than asked for in
  prose.

**Changed**

- **A publish destination NAMES its targets**: `local`, `public` or
  `local|public`, `|`-joined, replacing the opaque `both`. The vocabulary is
  readable off any single value, order and repetition are canonicalized
  (`public|local` is stored as `local|public`), and a future destination is one
  entry in the table rather than a new word to look up. The task-folder copy is
  written for **every** destination — `publish` chooses whether the Artifact
  step happens, never whether the durable copy exists.
- **The report step in both orchestration skills is render → narrate →
  publish**: the renderer writes the page under the task folder first, the agent
  writes only the narrative fragment, and publishing is the last step and a
  mirror. With the surface off, the render prints nothing, writes nothing and
  exits 0, so a driver's `path=$(… --final)` publishes nothing without needing
  to know why.

## 0.2.3

Still no session verb, and the memory editor's save side still ships disabled
until you start the dashboard with `--allow-memory-write`. This release adds the
number that belongs beside the spend: **how full each subagent's context window
is**. It is a read, not a verb — derived from transcripts Touch already tails,
carried on the event stream it already writes, starting and stopping nothing.

**Added**

- **Per-agent context occupancy (`agent.ctx`)** on the run dashboard's agent
  rows and in the session view — the prompt the model re-read on its most recent
  request (`input + cache_creation + cache_read` of the last qualifying
  assistant record, output excluded: the arithmetic Claude Code's own status
  line documents, so a card and your status line agree digit-for-digit). It is a
  **level at an instant**, not a total, and it is deliberately unlike the token
  counters beside it: nothing sums it, deltas it, maxes it across agents or
  clamps it, because a compaction legitimately moves it *down*. A missing
  reading is spelled by rendering nothing — never a `0`, which would claim an
  empty window no live agent has. Scope is Workflow subagents; the orchestrator
  card shows none, because a session-scoped figure would be a false statement
  about the run.
- **`context_window`** in a run's `orch-config.json` under
  `.touch/local-orchestrators/<task>/` (an int, or a `{model: int}` map; pinned
  by `ORCH_CONTEXT_WINDOW`) — the ONLY source of the meter's denominator. There
  is no built-in model→window table and no fallback, so an undeclared window is
  a normal state: the reading then renders as an absolute figure with no bar and
  no percentage, and nothing invents one.
- **`/health.context`** on the aggregator — a rung name (`events` | `absent`)
  and four counts, so "no number on the card" has a cause an operator can act
  on. Counts and one enum only: no agent ids, no paths, no task names. The
  server measures none of it; `decision_watcher.py` is the sole producer.
- **Context peak and final in the cost reader** — `contextPeak`,
  `contextFinal`, `contextFinalTs` and `contextByOwner`, outside the money block
  and never mixed into it.

**Changed**

- **The snapshot fold generation is now 3.** Agent rows fold `agent.ctx`
  last-wins, whole-object replace, no merge — never the neighbouring token
  line's monotonic treatment, and never a per-key merge that could keep a stale
  `cap` alive across a model switch. A page holding generation 2 discards its
  snapshot and replays.
- **The reading rides the existing token tick** — same poll, same
  `token_tick_secs` ceiling, zero new event kinds, zero new event lines, no
  heartbeat. `--no-tokens` suppresses it along with the tokens, because it is a
  by-product of exactly the transcript read that flag turns off.
- **No Mongo field, no migration.** Occupancy is a read-time projection over the
  `usage` collection that is already mirrored (`sort ts desc, limit 1`), so
  history gains the reading retroactively and no collection gains a key nobody
  wrote.
- **`hooks/agent_lifecycle.py` documents the launch race honestly** — the
  `PostToolUse` `Workflow` event and the first `SubagentStart` land within
  ~±35 ms **in either order** (measured against CLI 2.1.220; the earlier "3 ms
  before" was one sample). When the config write loses, that agent gets no
  lifecycle line rather than a guessed one, and the watcher derives the same
  card from the journal a beat later.

## 0.2.2

Still no session verb, and the memory editor's save side still ships disabled
until you start the dashboard with `--allow-memory-write`. This release makes
the run loop deterministic end to end: one wrapper now owns the run-folder
lifecycle, and the aggregator gains its ingest tick and a cost reader.

**Added**

- **`touch-run` — the seventh wrapper** (`start | bind | close | verify |
  status`). `start` lays out the task folder under
  `.touch/local-orchestrators/<task>/` — where run folders live — seeds the
  plan cards and starts Touch's own daemons; `bind` records the `wf_dir` and
  renders `plan/RESUME.md`; `close` settles the cards and stops what it
  started. It acts on Touch's folders and daemons only, so it is not a control
  verb and the verb table is unchanged (GD-D8).
- **`.touch/run.json`** — tracked per-project run constants that `touch-run
  start` merges under a run spec (D-12): configuration, not state, and the
  second (and last) tracked path under `.touch/`.
- **The ingest tick** (`aggregator/tick.py`) — drives the tailers, applies the
  derived operations into the read model and the WAL, and reports itself in
  `/health`.
- **A cost reader** (`aggregator/costs.py`) — `--top` prices what a run cost
  per agent; `--baseline` measures the always-on context prefix this repo
  charges every session, the number `tests/test_context_budget.py` now caps.
- **An agent-lifecycle hook** (`hooks/agent_lifecycle.py`) — additive
  Subagent/PostToolUse recording, registered once in the plugin's
  `hooks/hooks.json` beside the scope guard.
- **Three docs** — `docs/dev-loop.md`, `docs/memory-home.md`,
  `docs/run-folders.md` — the long form the session guide now points at.

**Changed**

- **Three skills renamed** — `execute-research` → `research`,
  `implement-plan` → `implement`, `m-orchestrator` → `monitor`; invoke as
  `/touch:research`, `/touch:implement`, `/touch:monitor`. Same skills, shorter
  names; entries below use the names current at their release.
- **Terminal events are protocol, emitted deterministically** — by the
  watcher, the cycle reporter or `touch-run close`, never mandated of an
  agent. A plan whose agents all returned without a decisive verdict settles
  `done` ("closed — no verdict"), never `failed` (R-58).

## 0.2.1

Still no session verb — nothing starts, stops, restarts or terminates anything —
but this release adds Touch's first **write** surface, and it is deliberately a
narrow one: the Markdown files Claude Code loads as *memory* in the project you
run it in. It also moves every run folder out of `.claude/` and into `.touch/`,
so all of Touch's project state now lives under one directory.

**Added**

- **Auto memory, kept in your project.** `touch-selfcheck --init` maps this
  project's Claude Code memory to `<project>/.touch/memory` by merging one
  documented key, `autoMemoryDirectory`, into the project's
  `.claude/settings.local.json` — then *verifies* that the CLI resolved it,
  because a value the CLI rejects (a relative path, a `$VAR`) is dropped
  silently with no error and no warning. It also reports the three undocumented
  environment overrides that would outrank the key. It moves no memory content
  and refuses rather than guesses when the answer would be a mapping it cannot
  stand behind. Auto memory only: project and user instruction files, and
  subagent memory, have no relocation mechanism and do not move.
- **A memory editor on the run dashboard.** `touch-monitor` serves a second
  page at `/memory` — same port, same per-boot token — that lists and reads
  those files, shows the resolved directory, says whether the CLI agrees with
  it, and discloses the size caps and the 200-line / 25 KB budget the index is
  loaded under. Saving is **off by default**: pass `--allow-memory-write` (or
  set `TOUCH_ALLOW_MEMORY_WRITE=1`) to enable it, and rows render disabled with
  the reason until you do. Reads are `text/plain`, previews are escape-first,
  and the page has no `innerHTML` in it.
- **A write path built around the ways this could go wrong.** With writes
  enabled, a save takes the token from a request *header* only (never the URL,
  so a mutation cannot be bookmarked, prefetched or embedded in an `<img>`),
  requires an `X-Touch-Write: 1` header and a present same-origin `Origin`,
  emits no CORS header ever, and answers `405` with `Allow:` for a wrong method
  and a JSON `404` — never a page — for an unknown route under its prefix. Names
  are flat `<name>.md`; a symlink is refused rather than followed; containment is
  checked on the resolved path both sides; a target under `~/.claude` or inside
  an installed plugin cache is refused by name; the file is written through an
  `O_EXCL|O_NOFOLLOW` temp file, `fsync`, `os.replace` and a directory `fsync`;
  and an `ifMatch` checksum is required, so two editors cannot silently overwrite
  each other. Previous bytes go to `.touch/memory/.history/`, a delete is a move
  into `.touch/memory/.trash/`, directories are `0700` and files `0600`, and
  every change appends one line to `.touch/memory-audit.jsonl`. No dashboard
  event is emitted for a memory edit — it is not a plan card and must not
  fabricate one.
- **Content hygiene, because these bytes become model instructions.** A save is
  refused, with a named reason and never a quote of the offending text, when it
  carries an `@`-import outside a code span (the CLI expands those
  transitively), a block-level HTML comment, a credential-shaped line or a
  `pinned:` front-matter key without an explicit confirmation — `pinned` files
  are loaded into *every* session, not just the ones that ask.

**Changed**

- **Run state moved: `<project>/.claude/local-orchestrators/` is now
  `<project>/.touch/local-orchestrators/`.** One directory for everything Touch
  keeps in a project. The folder name is unchanged, so nothing inside a run
  folder is rewritten, and the state-dir ladder every writer shares
  (`$ORCH_TASKS_ROOT`, then `$CLAUDE_PROJECT_DIR`, then a walk up to the
  project marker) now joins the new path. The old fourth rung — a path relative
  to the module's own directory — is gone; it could only ever have written into
  the plugin cache. Existing projects: move the directory yourself, or start
  fresh; the scope-guard hook reads both locations during the transition, so its
  `ACTIVE` and `HALT` sentinels keep working either way.
- **`.gitignore` guidance for the one tracked subtree.** Everything under
  `.touch/` stays ignored except `.touch/memory/*.md`, which is meant to be
  committed — so stage it by name (`git add .touch/memory`) and never `git add
  .touch/`, which would sweep up tokens, transcripts and the history/trash
  copies.
- **The scope-guard hook additionally refuses subagent writes to
  `.touch/memory/`.** Co-locating memory with run state would otherwise have
  handed a loop agent a larger capability than the guard exists to withhold.
- **`touch-selfcheck` reports nine checks**, up from eight: the new one asks
  whether auto memory actually resolves to the directory the memory page serves,
  and names the mismatch when it does not.
- The pre-install description no longer says "read-only" without qualification;
  it names the memory write plane and the fact that it ships disabled.

## 0.2.0

Still read-only — no start / stop / restart / terminate verb ships — but the
payload is now the *only* copy of everything it carries, and it carries six
more skills than 0.1.0 did.

**Added**

- **Six engineering-practice skills**, invoked under the same namespace as the
  orchestration four: `/touch:architecture-boundaries`,
  `/touch:architecture-tradeoffs`, `/touch:code-quality-review`,
  `/touch:pattern-selection`, `/touch:refactoring-pass` and
  `/touch:testing-discipline`. They are condensed guidance derived from the
  named books, not the works themselves, and each carries a path-free
  `Sources:` line naming them.
- **A measured context bill for that choice.** `claude plugin details touch`
  now reports **~1,257 tokens always-on** across ten skills, up from the ~459
  measured at 0.1.0 — the six add ~120–170 tokens each to every session,
  whether or not you use them. On invocation they cost ~1.6k–2.8k, paid only
  when one fires. If you want the dashboard without that bill, keep the plugin
  installed and disabled except in the projects where you orchestrate.

**Changed**

- **One canonical copy of every shipped file.** Through 0.1.0 the development
  repository kept `aggregator/`, `touch-visual/`, `docs/` and the monitoring
  module at its root and pinned byte-equal copies into the payload, kept in
  step by a sync script. The payload is now the canonical home and the sync
  script is gone: the code that is tested is the code that ships, and there is
  no second copy to go stale. Nothing about the *layout* an installed plugin
  presents changes — the directories a consumer receives are the same ones this
  release ships from (the six new skills above are the release's only addition
  to what lands on disk).
- **The install command, stated once.** The marketplace `msdrx-tools` is served
  from the project repository itself: `/plugin marketplace add msdrx/touch`,
  then `/plugin install touch@msdrx-tools`, then `/reload-plugins`, then enable
  Touch from `/plugin`. The catalog has to sit at that repository's root
  because a cloned marketplace is read from
  `<repo>/.claude-plugin/marketplace.json` and nowhere else; it names this
  payload with `"source": "./plugin/touch"`, and what lands in your plugin
  cache is exactly this directory. Earlier development notes named a separate
  payload-only repository as the source — that repository never published a
  version, so nothing was ever installed from it and no migration exists.
- **Compliance pass over the packaging.** The catalog lives only at the
  repository root — the payload no longer carries a second
  `.claude-plugin/marketplace.json` of its own, which would have been a second
  catalog under the same marketplace name — and the catalog entry gained the
  card fields the listing UI shows (`displayName`, a category, tags) while the
  description and the version stay declared once, in `plugin.json`. The release
  gates now refuse to publish a version whose tag already exists, prove the
  catalog entry's `source` actually resolves to this payload, and run the test
  suite over a clean checkout rather than the author's working tree.
- The pre-install description and keywords now name the second skill family,
  so nothing in the skill list is a surprise after enabling.

## 0.1.0

First release. Read-only by design: Touch renders no control it cannot
honestly perform, so no start / stop / restart / terminate verb ships.

**Added**

- **Dashboard** — `touch-serve` binds `127.0.0.1:8932` and serves the session
  sidebar, the live agent tree, per-loop cards and running token counters over
  HTTP and a WebSocket with bounded replay and `(stream, seq)` resume. Every
  route but `/health` needs a per-boot token; the WebSocket upgrade also checks
  `Origin` and `Host` against an allowlist.
- **History that outlives the CLI's** — ingest of transcripts and workflow
  journals under `~/.claude/projects/`, kept in `.touch/` inside the project
  you run Touch in, because the CLI's retention sweep rewrites and deletes
  those files.
- **Optional MongoDB mirror** — off by default, `pymongo` supplied by you,
  and a database that is absent or down is a non-event: only history and
  backfill degrade, and `/health` says so. See `docs/mongo.md`.
- **Four skills** — `/touch:execute-research`, `/touch:implement-plan`,
  `/touch:orchestrate` and `/touch:m-orchestrator`, costing ~459 tokens
  always-on in total and more only when one of them fires.
- **Orchestration monitoring** — `touch-monitor` (run dashboard, port 8931,
  same loopback + token posture), `touch-status`, `touch-watcher` and
  `touch-cycle-reporter`, writing one task folder per run under
  `<project>/.claude/local-orchestrators/<task>/`.
- **Run-scope guard** — one `PreToolUse` hook on
  `Read|Glob|Grep|Edit|Write|Bash` that keeps one orchestration run's subagents
  out of another run's folder. Inert unless the project holds
  `.claude/local-orchestrators/ACTIVE`, off entirely via the `run_scope_guard`
  plugin option, and measured at ~22 ms per matched call against a ~22 ms bare
  interpreter floor while inert. Full disclosure in the README.
- **`touch-selfcheck`** — eight `PASS`/`FAIL` checks of an installation,
  which refuse to summarize an incomplete report.

**Notes**

- Ships `defaultEnabled: false`: a plugin that carries a hook should be enabled
  deliberately, not by installing it.
- Starts no background process on install; the `experimental.monitors` feature
  is deliberately unused.
- Nothing is written into the plugin directory, which is version-stamped and
  replaced on every update. All state is project-anchored.
