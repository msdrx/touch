# Changelog

Versions here are the `version` field of `.claude-plugin/plugin.json`, which is
the only place Touch declares one. Third-party marketplaces do not auto-update,
and a release without a version bump delivers nothing — so every entry below
is a version users had to ask for. See the README's *Update / uninstall*
section for the two commands.

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
- **A different install command.** The marketplace `msdrx-tools` is now served
  from the project repository itself, so the first line is
  `/plugin marketplace add msdrx/touch` (it was `msdrx/touch-plugin`, a
  separate payload-only repo). Everything after it is unchanged —
  `/plugin install touch@msdrx-tools`, then `/reload-plugins` — and so is the
  update path. The catalog has to sit at that repository's root because a
  cloned marketplace is read from `<repo>/.claude-plugin/marketplace.json` and
  nowhere else; it names this payload with `"source": "./plugin/touch"`, and
  what lands in your plugin cache is still exactly this directory.
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
