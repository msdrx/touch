# Touch

## What it is

Touch shows you what the subagents in a Claude Code session are actually doing:
a session sidebar, the live agent tree, per-loop cards and running token
counters, served as a local web page over the transcripts the CLI already
writes. It also ships ten skills: the four orchestration skills that produce
those loops — deterministic research→plan and plan→implementation drivers
whose every stage reports to a dashboard Touch renders too (the run dashboard
below, and the same stream in the session view) — and six engineering-practice
skills for architecture, testing, refactoring, design patterns and code
review, which are what those loops apply once they are running. Version 0.2.4
renders no button it cannot honestly honour, so nothing here starts, stops or
restarts anything (see `docs/control-semantics.md` for the verb ladder that a
later version would implement). It is read-only over your sessions and your
runs, with exactly one carve-out, disclosed rather than buried: the run
dashboard can edit the Markdown files Claude Code loads as *memory* in the
project you run it in, and even that is off until you pass
`--allow-memory-write`.

## Trust and data handling

Read this before you install. Touch reads your conversation history and
installs two hooks, and both facts should be a decision, not a surprise.

**What it reads.** Your Claude Code transcripts and workflow journals under
`~/.claude/projects/`, and, for the run dashboard, the task folders under
`<project>/.touch/local-orchestrators/`. That is the product: the dashboard
is a rendering of those files. Touch never writes anywhere under
`~/.claude/` — it opens those files read-only and tails them, and that stays
true of the memory editor below: rather than reaching into `~/.claude/`, it asks
the CLI to keep this project's memory *in* this project, and it refuses any edit
whose target resolves under `~/.claude/`. The session dashboard only reads those
task folders; the run dashboard reads them too and writes one file of its own
into the one it is anchored to (below). Everything else in them is written by the
run commands, and only in the project you run them in.

**What it writes.** One directory, `.touch/`, inside the project you run it in
(`$TOUCH_STATE_DIR` overrides it). It holds Touch's own copy of the history —
the CLI's retention sweep deletes transcripts, so a dashboard that only read
the live files would lose your older runs — plus `server.json` (mode `0600`)
carrying the current URL and token. If you use the orchestration skills or the
`touch-*` run commands, Touch also writes one task folder per run under
`<project>/.touch/local-orchestrators/<task>/` — the `ACTIVE` sentinel, the
append-only `events.jsonl` stream, the watcher checkpoint, the cycle reports
and, while `touch-monitor` is running, `monitor.json` (mode `0600`) carrying
that dashboard's per-boot token, a sibling of `.touch/server.json`.
Nothing is written outside that project directory; in particular
nothing lands in the plugin's own directory, which is version-stamped,
replaced on every update and swept about two weeks later.

**The memory editor — the one thing Touch writes on your behalf.** Claude Code
keeps *auto memory* per project — a `MEMORY.md` index it loads at the start of
every conversation, plus topic notes it loads on demand. `touch-selfcheck
--init` maps this project's memory into the project itself, at
`<project>/.touch/memory`, by merging one documented key
(`autoMemoryDirectory`) into your project's `.claude/settings.local.json`; it
then verifies that the CLI really resolved it and says so, because a rejected
value is otherwise silent. The run dashboard serves a page at `/memory` that
lists and reads those files. Saving them is **off by default** and turned on
only by `touch-monitor --allow-memory-write` (or
`TOUCH_ALLOW_MEMORY_WRITE=1`), because their content becomes model
instructions in every later session in that project. With writes on, a save
must present the per-boot token in a *header* (never in the URL), carry an
`X-Touch-Write: 1` header and a same-origin `Origin`, name a plain
`<name>.md` inside the memory directory, and match the file's current
checksum; the previous bytes are kept in `.touch/memory/.history/`, a delete is
a move into `.touch/memory/.trash/`, and every change appends one line to
`.touch/memory-audit.jsonl`. Content that would quietly widen what the model
loads — an `@`-import, a hidden HTML comment, a credential-shaped line, a
`pinned:` front-matter key — is refused with a reason. Nothing outside that one
directory is ever written, and no other file kind is accepted.

**What it serves.** `touch-serve` binds `127.0.0.1:8932` and prints a URL
carrying a token minted fresh at every boot; every route except `/health`
requires it, and the WebSocket upgrade additionally checks `Origin` and `Host`
against an allowlist. `touch-monitor` (the run dashboard, port 8931) has the
same posture. Neither wrapper will open a non-loopback bind on your
behalf — `--open` is refused by both, by design, and you have to invoke the
underlying module yourself to override that.

**Network guidance.** Leave it on loopback and publish nothing. If you need to
reach the page from another machine, forward the port over SSH
(`ssh -L 8932:127.0.0.1:8932 you@host`) rather than binding a public interface:
the token protects the page, but the transcripts behind it are the whole point
of not exposing it. Container users: a port publish from the host
(`sbx ports <sandbox> --publish 8932:8932/tcp`) additionally requires binding
a non-loopback address yourself, which is why the tunnel is the better answer.

**The optional database.** Touch can mirror its history into a local MongoDB so
it survives longer than `.touch/`. It is **off by default**, needs `pymongo`
you install yourself, and a database that is absent, down or unreachable is a
non-event — the live view is memory-authoritative and unaffected. Recipe and
security baseline: `docs/mongo.md`. Never publish the database port.

**The hooks — the part that costs you something.** Touch registers two hooks
in `hooks/hooks.json`. The one that can say no is `hooks/orch_scope_guard.py`,
a `PreToolUse` hook on the matcher `Read|Glob|Grep|Edit|Write|Bash`. Its job
is to keep the subagents of one orchestration run out of another run's folder,
and it is **inert unless your project contains
`.touch/local-orchestrators/ACTIVE`** (or the `HALT` emergency-stop sentinel
beside it) — files only Touch's own orchestration skills create. With neither
file present it exits without reading its input. The main terminal agent is
never restricted; only subagents are. While it is armed it also refuses
subagent writes to `.touch/memory/`, so a loop agent cannot edit the
instructions your next session loads. The second hook,
`hooks/agent_lifecycle.py` (SubagentStart, SubagentStop, and PostToolUse on
`Workflow|Artifact`), denies nothing, ever: it records agent starts and stops
into the active run's folder so the dashboard sees them without waiting for
the journal, is likewise inert when no run is active, and has its own off
switch (the `agent_lifecycle` plugin option, or `TOUCH_AGENT_LIFECYCLE=0`).

- **Cost, measured 2026-07-28** (six independent 20-run loops, one `python3`
  subprocess per call, median): about **22 ms per matched tool call** against a
  **~22 ms** bare-`python3 -c pass` floor — so 0–3 ms of it is Touch,
  inside the run-to-run noise of process start — while no orchestration run
  is active in the project, and **~33–38 ms** while one is (four rounds;
  33 ms in the run above). You pay the interpreter, not the guard, unless you
  use the orchestration skills.
- **The off switch** is the `run_scope_guard` plugin option (default `true`);
  set it to `false` and the hook returns immediately. Note that it also
  disables the `HALT` emergency brake, since that brake is a feature of this
  same hook.
- **Bash coverage is textual**: a Bash command whose *string* names another
  run's folder is denied, even if it would never have touched it. That is a
  deliberate trade — the threat model is loop agents drifting, not an
  attacker — and it means this is not an adversarial sandbox. Matching is
  first-segment-only and nothing is path-normalized, so traversal through a
  permitted segment is not detected.
- **Two deliberate holes**, disclosed for the same reason: a wildcard task
  segment (`*/findings`) is passed for `Read`/`Glob`/`Grep`, and another run's
  `plan/` directory is exempt for every tool except `Edit`/`Write` — so a
  `Bash` command can write there. The guard bounds drift; it does not contain
  a caller who means it.
- **This plugin starts no background process on install.** No daemon, no
  listener, no watcher: nothing runs until you run one of the `bin/` commands
  yourself. (The plugin format has an `experimental.monitors` feature that
  would auto-start one; Touch deliberately does not use it.)

**Context cost — the biggest thing this plugin charges you.**
`claude plugin details touch` reports **~1,261 tokens always-on** (measured
2026-07-31 against this payload) — the ten skill descriptions, and nothing
else — added to every session in which Touch is enabled. Ten, not four:
0.1.0 shipped the four orchestration skills at ~459 tokens, and 0.2.0 adds six
engineering-practice ones that cost the rest. Per skill, always-on and then
on invocation:

| skill | always-on | on invoke |
|---|---|---|
| `monitor` | ~140 | ~6.2k |
| `implement` | ~110 | ~5.7k |
| `orchestrate` | ~120 | ~2.9k |
| `research` | ~100 | ~3.3k |
| `architecture-tradeoffs` | ~170 | ~2.6k |
| `pattern-selection` | ~140 | ~2.8k |
| `architecture-boundaries` | ~130 | ~2.4k |
| `code-quality-review` | ~120 | ~2.3k |
| `refactoring-pass` | ~120 | ~1.6k |
| `testing-discipline` | ~120 | ~1.6k |

The on-invoke column is paid only when that skill actually fires. The hook is
harness-only and adds no model context at all. If you want the dashboard
without the skills' bill, leave the plugin disabled except in the projects
where you orchestrate — it installs disabled anyway.

**Auditing it.** The payload is Python 3 standard library, bash, three pages of
HTML/CSS/JS, the eleven Markdown files under `skills/` that the model reads as
instructions, and two JavaScript workflow templates the harness runs when a
skill fires — copied byte-for-byte per run and parameterized only by a JSON
run-spec, never edited. It has no runtime dependencies (the optional `pymongo` is
imported lazily by two modules and by nothing else), and it ships no test
suite, no fixtures and no build step, so what you read is what runs. A
future release may add `claude plugin eval` as a published pre-release gate;
today the gate is the source in front of you plus a release-time scan of this
payload that refuses to publish one carrying secrets, caches or fixtures.

One disclosure the source in front of you cannot make on its own: the
marketplace Touch is served from is its own development repository, so
installing it clones that repository's **whole history** — and that history
carries a burned API token and credentialed `mongodb://` URIs from the days
before the leak gates existed. Every credential this repository has ever seen
has been rotated and should be treated as burned; nothing there opens anything
today. Say so out loud anyway, because a clone puts those objects on your disk,
and `--sparse` is no defence: it limits the checkout, not the objects, which
arrive either way. (A plugin source form that fetches only the payload subtree
does exist and would avoid the history transfer entirely — but there is no such
form for a *marketplace* source, and adopting it would mean maintaining a second
repository in sync with this one. That trade was weighed and declined; the
contributor guide in the repository records why.)

## Install

```
/plugin marketplace add msdrx/touch
/plugin install touch@msdrx-tools
```

The `owner/repo` shorthand clones over SSH, so it fails on a machine with no
key loaded. Either escape works: give the HTTPS URL instead —
`/plugin marketplace add https://github.com/msdrx/touch.git` — or set
`CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1` and keep the shorthand.

Then `/reload-plugins`, and **enable Touch from `/plugin`**. It ships
`defaultEnabled: false` because it carries a hook: installing it does not
enable it, so enable it deliberately once you have read the section above —
and note that the `bin/` wrappers are only on your `PATH` while the plugin is
enabled, which is why the verification below comes after this step and not
before it.

If the clone times out — this is a whole development repository, ~15 MB —
raise the budget with `CLAUDE_CODE_PLUGIN_GIT_TIMEOUT_MS` (the default every
plugin git operation gets is `120000`, i.e. 120 s) and try again.

If working-tree **disk** is the constraint, a sparse clone you then add as a
local marketplace is smaller — `git clone --sparse <url>`, then
`git sparse-checkout set .claude-plugin plugin`. It saves checked-out files and
nothing else: the objects still transfer, so it shortens no download and, as
above, is not a privacy boundary.

One alternative, for the record: clone the repository and run against the
working copy with `claude --plugin-dir <clone-path>/plugin/touch` — the plugin
is that subdirectory, and the flag also takes a `.zip`. Zero infrastructure,
nothing registered, good for reading the code first. Submitting Touch to the
community marketplace is deliberately deferred until after the first
self-hosted release.

## Verify

With the plugin enabled, from the project you want to use it in:

```
touch-selfcheck
```

Ten checks in a healthy install, one `PASS`/`FAIL` line each (one of them can
`WARN` instead) — fewer when a check is a full stop, since a 3.9 interpreter
or a missing package ends the report there and says so: the interpreter clears
the 3.11 floor; the aggregator imports from *this* tree rather than a
same-named directory you happen to be standing next to; the web assets came
with it; the project root resolves; task state resolves into your project and
not into the plugin's own directory; auto memory resolves to the directory the
memory page serves, or the mismatch is named; a leftover pre-mapping memory
directory under `~/.claude` is detected and named — the one check that WARNs
rather than fails, since only you can decide what to rescue from it; a
loopback port can be bound; every `bin/` wrapper kept its exec bit (the one
thing a zip round trip silently destroys); and one event survives a real
write-and-read round trip. It refuses to print a green summary from an
incomplete report, exits non-zero on any failure, and ends with the command to
run next.

`touch-selfcheck --init` is the same program's one writing mode: it maps this
project's auto memory to `<project>/.touch/memory` (one key in
`.claude/settings.local.json`), verifies the result, and refuses rather than
guesses when it cannot be sure — it moves no memory content of its own.

## Use

```
touch-serve
```

It prints a loopback URL carrying the per-boot token —
`http://127.0.0.1:8932/?token=<per-boot token>` — and writes the same URL to
`.touch/server.json`. Open it and you get the sessions of the project you
started it in. `touch-serve --help` shows the module's own usage, including
the two bind flags this wrapper holds back — `--open` outright, and `--host`
unless it names a loopback address.

For orchestration runs there are five more commands: `touch-monitor` (the run
dashboard, port 8931, which prints its token to a terminal and otherwise
leaves it in `<task>/monitor.json`, mode `0600`, and whose header links to the
`/memory` editor described above), `touch-status` (append one
event to a task's `events.jsonl`), `touch-watcher` (derive
spawn/verdict/retry/advance events and token accounting from a workflow
journal), `touch-cycle-reporter` (one report per implement→test→critique
cycle, plus the final run report) and `touch-run` (`start | bind | close |
verify | status` — the run envelope: it lays out the run folder, starts and
stops Touch's own daemons and settles the run's cards; it runs no agent and is
not a session verb). The skills call them by name; you rarely need to.

The ten skills invoke under the plugin's namespace, in two groups.

**Orchestration** — the loops the dashboards render:

| skill | what it does |
|---|---|
| `/touch:research` | parallel read-only researchers, then one synthesizer that writes a single complete plan |
| `/touch:implement` | divide that plan by file ownership, then run each sub-plan through gated implement→test→critique loops |
| `/touch:orchestrate` | the naming, spawn-ledger and control-file standards that make subagents visible to the dashboard |
| `/touch:monitor` | wire live monitoring into any orchestrator you write yourself |

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

One plugin carries both because the second group is what the first group's
agents are for: a review loop with no review standard produces loops, not
quality. Each of the six is condensed guidance derived from the books named on
its own `Sources:` line — not the works themselves, and no substitute for
reading them.

## Update / uninstall

Third-party marketplaces have auto-update **disabled** by default, so a new
release reaches you only when you ask for it:

```
/plugin marketplace update msdrx-tools
/plugin update touch@msdrx-tools
/reload-plugins
```

To stop doing that by hand, turn on `/plugin` → Marketplaces → **Enable
auto-update** for `msdrx-tools`. Know what you are turning on: the marketplace
is the project repository itself, so auto-update re-syncs a ~15 MB development
repo on every push to it — commits about tests, plans and run history included.
Either way, only a version bump in `plugin.json` delivers anything — pushing
commits alone changes nothing for installed users, which is why `CHANGELOG.md`
tracks the version you have.

To remove it, uninstall `touch` from `/plugin` → installed plugins, then
`/reload-plugins`. Nothing of Touch's survives outside the plugin cache
except, in the projects you used it in, the `.touch/` directory — its history,
its run folders under `.touch/local-orchestrators/<task>/`, and, if you ran
`--init`, your memory files under `.touch/memory/`. Delete it if you want the
history gone too, and drop the `autoMemoryDirectory` line from that project's
`.claude/settings.local.json` if you want the CLI's own default memory location
back (move the files first — uninstalling Touch does not move them for you).
