# Changelog

Versions here are the `version` field of `.claude-plugin/plugin.json`, which is
the only place Touch declares one. Third-party marketplaces do not auto-update,
and a release without a version bump delivers nothing — so every entry below
is a version users had to ask for. See the README's *Update / uninstall*
section for the two commands.

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
