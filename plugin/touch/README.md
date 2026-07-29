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
review, which are what those loops apply once they are running. Version 0.2.0
is **read-only**: it renders no button it cannot honestly honour, so nothing
here starts, stops or restarts anything (see `docs/control-semantics.md` for
the verb ladder that a later version would implement).

## Trust and data handling

Read this before you install. Touch reads your conversation history and
installs a hook, and both facts should be a decision, not a surprise.

**What it reads.** Your Claude Code transcripts and workflow journals under
`~/.claude/projects/`, and, for the run dashboard, the task folders under
`<project>/.claude/local-orchestrators/`. That is the product: the dashboard
is a rendering of those files. Touch never writes anywhere under
`~/.claude/` — it opens those files read-only and tails them. The session
dashboard only reads those task folders; the run dashboard reads them too
and writes one file of its own into the one it is anchored to (below).
Everything else in them is written by the run commands, and only in the
project you run them in.

**What it writes.** One directory, `.touch/`, inside the project you run it in
(`$TOUCH_STATE_DIR` overrides it). It holds Touch's own copy of the history —
the CLI's retention sweep deletes transcripts, so a dashboard that only read
the live files would lose your older runs — plus `server.json` (mode `0600`)
carrying the current URL and token. If you use the orchestration skills or the
`touch-*` run commands, Touch also writes one task folder per run under
`<project>/.claude/local-orchestrators/<task>/` — the `ACTIVE` sentinel, the
append-only `events.jsonl` stream, the watcher checkpoint, the cycle reports
and, while `touch-monitor` is running, `monitor.json` (mode `0600`) carrying
that dashboard's per-boot token, the counterpart of `.touch/server.json`.
Nothing is written outside those two project directories; in particular
nothing lands in the plugin's own directory, which is version-stamped,
replaced on every update and swept about two weeks later.

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

**The hook — the part that costs you something.** Touch registers one
`PreToolUse` hook, `hooks/orch_scope_guard.py`, on the matcher
`Read|Glob|Grep|Edit|Write|Bash`. Its job is to keep the subagents of one
orchestration run out of another run's folder, and it is **inert unless your
project contains `.claude/local-orchestrators/ACTIVE`** (or the `HALT`
emergency-stop sentinel beside it) — files only Touch's own orchestration
skills create. With neither file present it exits without reading its input.
The main terminal agent is never restricted; only subagents are.

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
`claude plugin details touch` reports **~1,257 tokens always-on** (measured
2026-07-29 against this payload) — the ten skill descriptions, and nothing
else — added to every session in which Touch is enabled. Ten, not four:
0.1.0 shipped the four orchestration skills at ~459 tokens, and 0.2.0 adds six
engineering-practice ones that cost the rest. Per skill, always-on and then
on invocation:

| skill | always-on | on invoke |
|---|---|---|
| `m-orchestrator` | ~130 | ~4.4k |
| `implement-plan` | ~120 | ~3.3k |
| `orchestrate` | ~120 | ~2k |
| `execute-research` | ~100 | ~1.8k |
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

**Auditing it.** The payload is Python 3 standard library, bash, two pages of
HTML/CSS/JS, the eleven Markdown files under `skills/` that the model reads as
instructions, and two JavaScript workflow templates the harness runs when a
skill fires. It has no runtime dependencies (the optional `pymongo` is
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

Eight checks in a healthy install, one `PASS`/`FAIL` line each — fewer when a
check is a full stop, since a 3.9 interpreter or a missing package ends the
report there and says so: the interpreter clears the 3.11 floor; the
aggregator imports from *this* tree rather than a same-named directory you
happen to be standing next to; the web assets came with it; the
project root resolves; task state resolves into your project and not into the
plugin's own directory; a loopback port can be bound; every `bin/` wrapper kept
its exec bit (the one thing a zip round trip silently destroys); and one event
survives a real write-and-read round trip. It refuses to print a green summary
from an incomplete report, exits non-zero on any failure, and ends with the
command to run next.

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

For orchestration runs there are four more commands: `touch-monitor` (the run
dashboard, port 8931, which prints its token to a terminal and otherwise
leaves it in `<task>/monitor.json`, mode `0600`), `touch-status` (append one
event to a task's `events.jsonl`), `touch-watcher` (derive
spawn/verdict/retry/advance events and token accounting from a workflow
journal) and `touch-cycle-reporter` (one report per implement→test→critique
cycle). The skills call them by name; you rarely need to.

The ten skills invoke under the plugin's namespace, in two groups.

**Orchestration** — the loops the dashboards render:

| skill | what it does |
|---|---|
| `/touch:execute-research` | parallel read-only researchers, then one synthesizer that writes a single complete plan |
| `/touch:implement-plan` | divide that plan by file ownership, then run each sub-plan through gated implement→test→critique loops |
| `/touch:orchestrate` | the naming, spawn-ledger and control-file standards that make subagents visible to the dashboard |
| `/touch:m-orchestrator` | wire live monitoring into any orchestrator you write yourself |

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
except, in the projects you used it in, the `.touch/` directory and any
`.claude/local-orchestrators/<task>/` folders; delete those if you want the
history gone too.
