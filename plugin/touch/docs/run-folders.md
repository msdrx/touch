# Run folders — what is in one, and how it joins to a harness run

One folder per run, under a project's tasks root
(`<project>/.touch/local-orchestrators/<task-name>/`). `touch-run start` lays
one out; the daemons write into it; nothing ever deletes one.

## Anatomy

| entry | written by | what it is |
|---|---|---|
| `events.jsonl` | `status.sh` only (GD-D5) | the append-only event stream: one JSON line per plan/stage/state transition. Replayed in full on every dashboard connect |
| `orch-config.json` | `touch-run start` / `bind` | the run's constants: `wf_dir`, port, caps, strategy, `resume_from_run_id`, and `reports` — the three report surfaces (`cycle`, `research`, `final`), each `{enabled, publish}`, published whole from the run spec and `.touch/run.json` and re-read live by the reporter |
| `.watcher-state.json` | `decision_watcher.py` | the watcher's checkpoint — restart-safe, never double-counts |
| `orch-scripts/` | the driver | the byte-for-byte copy of the workflow template this run executed |
| `plan/` | the synthesizer / divider | the plan, its sub-plan partition, and `RESUME.md` |
| `findings/` | agents | gate and critique handoffs, `<plan>-<gate>-attempt-<N>.md` |
| `report/` | `cycle_reporter.py`, and you | per-cycle reports, the final report, cost JSON, any HTML artifact the run produced |

A `plan/` or `report/` directory may legitimately be empty — "plan only, never
run" is a recognized state, not a broken folder, which is why empty ones carry
a `.gitkeep`.

## The join key

Every `orch-config.json` names a `wf_dir` under
`~/.claude/projects/<project-key>/<session-id>/subagents/workflows/<runId>/`.
That path is the join from a task folder to its harness journal — the
`journal.jsonl` the watcher tails and the `agent-*.jsonl` transcripts the cost
reader prices. `wf_dir`'s basename is the `runId`.

A `wf_dir` that no longer exists means **archived — source transcripts
unavailable**. It never means the folder belongs to a different project.

A run is not one directory: a `/clear` mid-run gives the process a new session
id while the `runId` stays the same, so a resumed run's later agent transcripts
land under the *new* session directory. Readers anchor on the `runId` and search
across sessions rather than trusting the recorded parent.

## Two rules that bite

- **Never delete a finished folder or its `events.jsonl`.** Completed runs are
  dashboard history and replay on connect, and the Mongo mirror's `legacy:` key
  space is positional (`legacy:<task>#<line>`), so it *depends* on the rule.
  There is no cleanup step. Wiping is only for a task you are actively
  re-running: stop the daemons, delete `events.jsonl` + `.watcher-state.json`,
  re-seed, restart.
- **Never rewrite one either.** A finished folder — its `orch-scripts/`,
  `orch-config.json`, `RESUME.md`, findings — is dated record. A path migration
  deliberately stops at the payload, the tests and the top-level docs and leaves
  the old absolute paths inside finished runs exactly as they were. The one
  sanctioned exception is a finished run's `plan/`, because an authority ladder
  can live there.

---

## Appendix — this repository's own runs

A development record, not consumer documentation: these are the folders
Touch's own orchestration runs left behind, in the checkout this plugin is
built from. They are gitignored, so a clean clone has none of them. The table
is here because it is the index that keeps the authority ladder in `CLAUDE.md`
honest — every ladder entry names a plan file in one of these folders.

| folder | what it was | state | authoritative artifact |
|---|---|---|---|
| `touch-repo-recon` | first recon of the repo + skills | complete | `findings/` (51 findings) |
| `touch-aggregator` | 6-perspective research → design law | complete | `plan/touch-aggregator-plan.md` (D1–D14) |
| `touch-monitor-spawn` | a v0 slice planned from conversation | **plan only, never run** | `plan/touch-monitor-spawn-plan.md` (historical) |
| `touch-full-recon` | 6-perspective re-recon | complete | `plan/touch-full-recon-plan.md` (**normative**) + `report/probes.md` |
| `touch-mongo-live` | Mongo/live-flow research, then its implementation pass | complete | `plan/touch-mongo-live-plan.md` (**amendment**) |
| `touch-monitor-perf` | monitoring-module performance/robustness pass | complete, follow-ups recorded | `plan/touch-monitor-perf-plan.md` + `plan/POST-RUN.md` |
| `touch-plugin-pack` | packaging Touch as a Claude Code plugin | packaging items complete; 01/06/13 open | `plan/touch-plugin-pack-plan.md` + `plan/RESUME.md` |
| `reflection-plugin` | a superseded 33-item plan; 3 items landed, then halted | **halted** | `plan/reflection-plugin-plan.md` (historical) |
| `touch-plugin-unify` | `plugin/touch/` made canonical + six skills adopted | complete | `plan/touch-plugin-unify-plan.md` (**amendment**, GD-U1…GD-U9) |
| `touch-plugin-compliance` | one dev identity, catalog card fields, release gates, honest install docs | complete | `plan/touch-plugin-compliance-plan.md` (**amendment**, GD-C1…GD-C12) |
| `touch-memory-home` | auto memory mapped into `.touch/memory`; memory CRUD; the tasks-root move | complete | `plan/touch-memory-home-plan.md` (**amendment**, G1…G14) |
| `touch-determinism` | the LLM→deterministic conversion pass: ingest tick, run close, driver envelope, cost reader, context budget | this pass | `plan/touch-determinism-plan.md` (**amendment**, GD-D1…GD-D15, D-01…D-26) + `plan/touch-determinism-modules-plan.md` |
