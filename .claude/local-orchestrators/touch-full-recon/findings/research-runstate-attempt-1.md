# Research findings — perspective: runstate

Scope: `.claude/local-orchestrators/` treated as **data** — `events.jsonl`
streams, `.watcher-state.json` checkpoints, `orch-config.json`, `*.log`, folder
layout — plus the producers that shape them (`status.sh`,
`decision_watcher.py`, `monitor_server.py`) and the repo hygiene around them.

Corpus actually measured (all three streams parsed, 100% valid JSON, zero bad
lines):

| task folder | events lines | ts inversions | token lines | agents in checkpoint | findings files | terminal `complete` |
|---|---|---|---|---|---|---|
| `touch-aggregator` | 590 | 2 | 540 (91%) | 7 | 6 | no |
| `touch-repo-recon` | 103 | 5 | 70 (68%) | 9 | 3 | yes (`stopped by user`) |
| `touch-full-recon` | 128 (live) | 3 | 42 | 6 | 0 (in flight) | no |
| `touch-monitor-spawn` | — (no file) | — | — | — | — | — |

Empirical probes were run in a throwaway dir
(`/tmp/claude-1000/.../scratchpad/rs`), never against the live task folder.

---

## RUNSTATE-1 — The "omnigent" claim in CLAUDE.md / inception.md / the aggregator workflow script is false

**file:line** `CLAUDE.md:127-130`; `inception.md:55`;
`.claude/local-orchestrators/touch-aggregator/orch-scripts/research.workflow.js:31`
**severity** major

**Scenario.** CLAUDE.md:127 states: *"The `orch-config.json` files under
`.claude/local-orchestrators/*/` point at `wf_dir` paths from a **different,
earlier project** (`omnigent`). They are carried-over history — read them as
examples, don't assume they describe this repo."* inception.md:55 repeats it
("mostly carried-over `omnigent` examples") and the aggregator's own workflow
script fed the same false statement to six researchers as subject line 31.

Measured reality — all three `orch-config.json` files:

```
touch-aggregator  wf_dir=/home/agent/.claude/projects/-home-laniakea-Projects-touch/dd469822-…/subagents/workflows/wf_829e6f58-b2f   EXISTS  journal 56741 B
touch-repo-recon  wf_dir=/home/agent/.claude/projects/-home-laniakea-Projects-touch/e423cd3c-…/subagents/workflows/wf_455b348c-e17   EXISTS  journal 15567 B
touch-full-recon  wf_dir=/home/agent/.claude/projects/-home-laniakea-Projects-touch/292fc08c-…/subagents/workflows/wf_930e210a-6da   EXISTS  journal   750 B
```

Every path slug is `-home-laniakea-Projects-touch` (this repo), every directory
exists, and each journal's byte size **exactly equals** the offset recorded in
the sibling `.watcher-state.json` (56741 / 15567 / 750) — i.e. these are this
project's own live runs, fully tailed. No `omnigent` path exists anywhere on
disk. The one legitimate `omnigent` reference is
`touch-aggregator/plan/touch-aggregator-plan.md:764`, which merely names a test
fixture style.

**Why it matters.** This is a doc bug that actively degrades every downstream
agent: it tells researchers and implementers to *discount* the only real
end-to-end run data the project has, which is exactly the corpus D4's legacy
adapter must be built and tested against.

**Recommendation.** Delete the CLAUDE.md:127-130 bullet and replace it with the
truth: *"The `orch-config.json` files point at this repo's own Workflow runs
under `~/.claude/projects/-home-laniakea-Projects-touch/*/subagents/workflows/`;
they are live history and the reference corpus for legacy ingest."* Fix
`inception.md:55` the same way. Do **not** copy line 31 of the aggregator
workflow script into any new `orch-scripts/`.

---

## RUNSTATE-2 — Legacy events carry no run id and no task id; one folder's stream spans multiple script invocations

**file:line** `.claude/local-orchestrators/touch-repo-recon/events.jsonl:1-32,103`
**severity** blocker (for D4 legacy ingest)

**Scenario.** D4 (`touch-aggregator-plan.md:114-137`) requires every v2 record
to carry `ref`, with `{"runId":"wf_…","key":…,"ordinal":…}` for node/run
records, and declares legacy `events.jsonl` a read-only `source:"legacy"`
input. But **no legacy event has a run id, a task name, or any correlation
field**. The full observed key sets across all 821 lines are only:

```
(ts, plan, stage, state, detail)                                  67
(ts, plan, stage, state, detail, title)                            8
(ts, plan, stage, state, detail, agent)                           31
(ts, plan, stage, state, detail, tokens)                           9
(ts, plan, stage, state, detail, agent, tokens, quiet)           706
```

Worse, a single task folder's stream demonstrably spans **two script
invocations**. `touch-repo-recon/events.jsonl`:

- lines 1-2 (`13:50:48`) — seed `plan|queued` events from a *re-run* of
  `research.workflow.js`;
- lines 8-18 (`ts 13:41:06Z`) — five agents (`intent`, `monitoring`, `skills`,
  `v0task`, `aggtask`) replayed by the watcher from the **earlier** wave in the
  same journal, appended *after* the 13:50 seed lines;
- lines 25-32 (`13:50:52Z`) — four fresh agents (`models`, `v0task`, `aggtask`,
  `monitoring`) from the second wave.

The only place the run identifier appears at all is free text inside a detail
string: line 103 `"run wf_455b348c-e17 stopped by user - 6 researchers aborted,
no plan written"`. Regex-scraping a run id out of a human-readable detail is
not a contract.

**Recommendation.** The legacy adapter must **synthesize** the missing refs, and
the plan must say how: `runId` = `basename(orch-config.json["wf_dir"])` when
that file exists, else `legacy:<task-folder-name>`; `taskId` = folder name;
`ordinal` = a per-`(plan,stage)` monotonic counter incremented on every
`state:"running"` spawn event so re-spawn waves become distinct nodes rather
than one flickering node. Add an explicit plan item for this (nothing in
`touch-monitor-spawn-plan.md` P1-P12 covers legacy ingest at all — see
RUNSTATE-10's sibling gap) and build `tests/test_legacy.py` fixtures from
`touch-repo-recon/events.jsonl` verbatim, because it is the only two-wave
sample in existence.

---

## RUNSTATE-3 — Agent ids are truncated to 8 chars in events but stored full-length in the checkpoint

**file:line** `.claude/shared/monitoring/decision_watcher.py:557` (and `:787`,
`:550`, `:777`)
**severity** major

**Scenario.** The watcher emits `"agent": {"id": aid[:8], "label": …}` while
`.watcher-state.json` keys its `agents` and `tok_emitted` maps on the **full**
17-char id. Concretely, `touch-aggregator`:

```
events.jsonl:12    "agent": {"id": "a2fc883c", "label": "research #1", "state": "running", "started": "2026-07-25T02:59:29.846Z"}
.watcher-state.json  "a2fc883c96ff7b837": {"plan": "research", "role": "research", "attempt": 1, "stage": "sessiondata"}
```

An ingester that joins the two sources on `agent.id` gets zero matches. Prefix
matching works today but is unsound in principle (8 chars = `a` + 7 hex ≈ 2^28
of entropy) and silently wrong if two ids ever share a prefix.

**Recommendation.** (a) In the legacy adapter, join by prefix, treat the 8-char
value as a *display* id only, and namespace it as `legacy:<task>:<id8>` so it
can never collide with a v2 `agentId`. (b) In the new v2 stream, carry the full
`agentId` in `ref` and put the 8-char form only in `data.shortId` if the UI
wants it. (c) Do not read `.watcher-state.json` at all (see RUNSTATE-5) — which
makes the join moot.

---

## RUNSTATE-4 — A fully successful run is recorded as `plan|failed`

**file:line** `.claude/local-orchestrators/touch-aggregator/events.jsonl:571`;
`.claude/local-orchestrators/touch-aggregator/.watcher-state.json` (`plans`)
**severity** major

**Scenario.** The `touch-aggregator` research run succeeded completely: all six
researchers emitted `done` (lines 491-567, "found 17/18/18/17/20/20 findings"),
six findings files exist on disk, the synthesizer ran, and line 586 says
`synthesis | plan | done | "plan written"` with a 52 KB plan file to prove it.
Yet line 571 says:

```
{"plan":"research","stage":"plan","state":"failed","detail":"loop exited -> synthesis", …}
```

and the checkpoint agrees: `"plans": {"research": "failed", "synthesis":
"running"}`, `"run_complete": null`. The watcher classifies "the research plan's
loop exited and control moved on" as a plan failure.

**Why it matters.** Any Touch view that derives task health from the `plan`
reserved stage (which `monitoring.md:42` says *is* the card badge) will paint
the single most valuable run in this repo red, and a "retry failed plans"
control would offer to re-run a plan that succeeded.

**Recommendation.** Legacy adapter: derive plan health from
(i) the terminal `orchestrator|complete` event if present, else (ii) the
per-stage terminal states — and treat `plan|failed` whose detail matches
`loop exited ->` as an *advance*, not a failure. Separately file the watcher fix
(emit `plan|done` when the loop exits because every stage reached a terminal
`done`). Add this exact line as a regression fixture.

---

## RUNSTATE-5 — `.watcher-state.json` contradicts `events.jsonl` and is never closed on kill

**file:line** `.claude/local-orchestrators/touch-repo-recon/.watcher-state.json`
vs `.claude/local-orchestrators/touch-repo-recon/events.jsonl:101-103`
**severity** major

**Scenario.** `touch-repo-recon` is a **stopped** run. Its stream terminates
cleanly:

```
101  research     | plan     | failed | stopped by user before completion
102  synthesis    | plan     | failed | run stopped before synthesis started
103  orchestrator | complete | done   | run wf_455b348c-e17 stopped by user - 6 researchers aborted, no plan written
```

Its checkpoint, written by the watcher that was killed at the same moment, still
says `"plans": {"research": "running"}`, `"run_complete": null`, and
`"running": ["abc69d2e…","a36d6a70…","a4fa0e2d…","a2c3883f…","abf33f08…","a446960c…","a434fe01…"]`
— seven agents permanently "running". `touch-full-recon` (this run) has the same
shape by construction. There is no code path that writes a terminal checkpoint
when the daemon dies, because `save_state` is only called inside the loop.

**Why it matters.** If Touch's task list or graph reads `.watcher-state.json`
for "is this live / who is running", every historical folder shows phantom live
agents forever, and a "stop" control would target dead agent ids.

**Recommendation.** State in the plan as a hard rule: **`.watcher-state.json` is
watcher-private and Touch never reads it.** Derive everything from
`events.jsonl` (order + terminal events) and liveness from process existence
(`/proc/<pid>` + `procStart`) per D6. If a Touch task page wants a token total,
recompute it from the stream, not from `tok_emitted`.

---

## RUNSTATE-6 — Streams are append-ordered but not timestamp-ordered, and mix two ISO formats

**file:line** `.claude/shared/monitoring/status.sh:35` vs
`.claude/shared/monitoring/decision_watcher.py:141`;
`.claude/local-orchestrators/touch-aggregator/events.jsonl:10-11`
**severity** major

**Scenario.** Two writers stamp differently:

- `status.sh:35` — `datetime.now(timezone.utc).isoformat(timespec="milliseconds")`
  → `"2026-07-25T02:59:14.537+00:00"` (offset suffix).
- `decision_watcher.py:141` — same default, but nearly every watcher event
  passes `ts=` sourced from the journal/transcript `timestamp`
  → `"2026-07-25T02:59:29.846Z"` (Z suffix, and **backdated** to when the
  journal entry was written, not when the line was appended).

Result, `touch-aggregator/events.jsonl`:

```
10  2026-07-25T02:59:40.881+00:00  orchestrator | watcher | info
11  2026-07-25T02:59:29.846Z       orchestrator | research | running   ← 11 s earlier, one line later
```

Measured inversions: 2 (`touch-aggregator`), 5 (`touch-repo-recon`), 3
(`touch-full-recon`). A naive `new Date(ev.ts)` handles both, but
`datetime.fromisoformat` on Python < 3.11 rejects `Z`, and any consumer that
*sorts* by ts reorders the spawn/observe pairs.

**Recommendation.** Legacy adapter: parse with an explicit
`ts.replace("Z","+00:00")` normalisation (the same trick already used at
`decision_watcher.py:216-217,322`), assign `seq` from **file line order**, never
from ts, and keep the original string in `data.sourceTs`. The v2 writer must
emit exactly one format — `…Z`, per the D4 example — and a test should assert
it.

---

## RUNSTATE-7 — Every stage completion is written twice, by two independent writers, with different details

**file:line** `.claude/local-orchestrators/touch-aggregator/events.jsonl:491` and `:503`
**severity** major

**Scenario.** The same logical fact arrives twice, 38 s apart:

```
491  2026-07-25T03:09:24.609+00:00  research | agentgraph | done | "found 17 findings"          (agent: none)   ← status.sh, from inside the agent
503  2026-07-25T03:10:02.764+00:00  research | agentgraph | done | "research #1: 17 findings"   (agent: a82d2e25 done)  ← watcher, from the journal result
```

This holds for all six stages in `touch-aggregator` (491/503, 527/539,
532/548, 552/555, 558/564, 561/567). The two rows differ in `detail`, in the
presence of `agent`, and in ts format. `monitoring.md` documents the two writers
but nothing states the duplication contract.

**Recommendation.** Legacy adapter dedupes on
`(task, plan, stage, terminal-state)` with **watcher-wins** (it is the
deterministic source; `status.sh` is best-effort per CLAUDE.md) but keeps the
`status.sh` detail as `data.agentDetail` — the agent's own "found 17 findings"
is better UI copy than the watcher's. Emit exactly one v2 `kind:"node"`
completion record. Without this, any "N stages done" counter double-counts.

---

## RUNSTATE-8 — Agent labels are not unique, `(plan,stage)` is not unique, and orchestrator rows carry no agent at all

**file:line** `.claude/local-orchestrators/touch-aggregator/events.jsonl:12,15,17,19,21,23,502`;
`.claude/local-orchestrators/touch-repo-recon/events.jsonl:16,28`
**severity** major

**Scenario.** Three overlapping identity failures in the real data:

1. **Labels collide.** All six parallel researchers in `touch-aggregator` carry
   `"label": "research #1"` (label = `role #attempt`,
   `decision_watcher.py:550`). Six distinct nodes, one string.
2. **`(plan,stage)` collides over time.** `touch-repo-recon` has two different
   agents on stage `v0task` — `a36d6a70` (line 16, `13:41:06Z`) and `abf33f08`
   (line 28, `13:50:52Z`) — and likewise two each on `aggtask` and `monitoring`.
   Nine agent entries, six stage names.
3. **Orchestrator rows have no agent.** Line 502
   `orchestrator | research | info | "research research #1: 17 findings"` has no
   `agent` key, so it is unattributable to any of the six identical labels; only
   the paired plan-row event (503) carries the id.

Additionally the `plan` namespace and the `stage` namespace overlap: `research`
is both a plan id and a stage id (under plan `orchestrator`). A reducer keyed on
bare stage names conflates them.

**Recommendation.** Touch's graph must key nodes on
`(runId, agentId, spawnTs)` — never on label, never on `(plan,stage)` — which is
exactly what D3's `(runId, key, ordinal)` gives if `ordinal` is incremented per
re-spawn (RUNSTATE-2). Labels are display-only and must be disambiguated in the
UI (append the short id). Orchestrator-row events should be ingested as
`kind:"log"` attached to the run, not to an agent.

---

## RUNSTATE-9 — Abandoned agents are never closed; the stale-close guard cannot fire on same-attempt re-spawns

**file:line** `.claude/shared/monitoring/decision_watcher.py:606` (DRIVER-1
guard); `.claude/local-orchestrators/touch-repo-recon/events.jsonl:9-18`
**severity** major

**Scenario.** `touch-repo-recon`'s checkpoint holds 9 agents; only 3 findings
files were ever written (`intent`, `skills`, `v0task`). Agents `a36d6a70`
(v0task), `a4fa0e2d` (aggtask), `abc69d2e` (monitoring) from the 13:41 wave
never returned a result and never got a terminal event — their rows say
`running` at EOF. The watcher's stale-close logic exists but is deliberately
gated on *attempt strictly increasing* (`decision_watcher.py:606`, comment
"DRIVER-1: a parallel fan-out spawns many agents at the SAME
plan+role+attempt — those are live siblings, not retries"). The 13:50 re-spawn
reused `attempt: 1`, so the guard correctly declined to fire — and the
abandoned agents stay running forever. This is not a bug in the guard; it is a
gap the stream cannot close on its own.

**Recommendation.** Touch must own staleness rather than trusting the stream to
close rows: mark an agent node `stale` when (a) the run has a terminal
`orchestrator|complete` event and the node has no terminal state (this alone
fixes `touch-repo-recon`), or (b) no event referencing that agent for
> N minutes and its process is gone. `monitor.html:335` already synthesises
client-side staleness — copy that semantic into the aggregator so it is in the
data, not only in one frontend.

---

## RUNSTATE-10 — `.gitignore` has no `.touch/` entry, and `.touch/` will contain raw PTY capture

**file:line** `/home/laniakea/Projects/touch/.gitignore` (whole file — entry absent);
`.claude/local-orchestrators/touch-monitor-spawn/plan/touch-monitor-spawn-plan.md:72`;
`.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md:140-160`
**severity** major

**Scenario.** D5 specifies `.touch/` at repo root "(gitignored; override
`TOUCH_STATE_DIR`)" and P1 says "Add `.touch/`". The current `.gitignore`
contains monitoring, Python, Node, env and editor sections — and **no `.touch/`
rule**. Per D5 that tree will hold
`.touch/sessions/<pid>-<procStart>/pty.log` (raw terminal capture) and
`.touch/hooks/<session_id>.jsonl` (hook spool with prompt content). The repo is
also entirely uncommitted (RUNSTATE-11), so the first `git add -A` after the
first aggregator run commits verbatim terminal output — API keys, tokens, and
anything else that ever crossed a monitored PTY — into history.

**Recommendation.** Add `.touch/` to `.gitignore` **before** any code that
creates it, in the same change as P1. Extend
`.claude/shared/monitoring/tests/test_shell.py:155` (`test_gitignore`, which
today only asserts the two module-dir entries) with a `.touch/` assertion, and
add a negative assertion that no rule ignores `.claude/local-orchestrators/`
itself — the existing `.claude/local-orchestrators/*/*.log` rule is one careless
edit away from swallowing the history CLAUDE.md:120 forbids deleting, and no
test would catch it.

---

## RUNSTATE-11 — Nothing is committed; the "never delete this history" rule protects files that exist only in the working tree

**file:line** repo root (`git status --porcelain`);
`CLAUDE.md:120-123`; `.claude/shared/monitoring/monitoring.md:187`
**severity** major

**Scenario.** `master` has **zero commits**. `git status --porcelain` returns
only:

```
?? .claude/
?? .gitignore
?? CLAUDE.md
?? README.md
?? inception.md
```

CLAUDE.md:120 says "**Never delete a finished task folder or its
`events.jsonl`** — completed runs are monitor history and replay on connect.
There is no cleanup step." `monitoring.md:187` likewise depends on old runs'
events surviving. But 824 KB of irreplaceable run history (three event streams,
nine findings files, two plans, the 20 KB driver context) is untracked: one
`git clean -fdx`, one sandbox teardown, or one careless `rm -rf .claude/` and it
is gone. Note also that `touch-full-recon/events.jsonl` is being **appended
right now** (128 lines and growing while this was written), so a commit taken
mid-run captures a partial stream.

**Recommendation.** Make "initial commit" item #1 of the implementation plan,
executed before any application source lands, and take it when no run is in
flight. Commit `.gitignore` first, then everything else. If the churn of
append-only streams in git is undesirable, decide it explicitly now
(RUNSTATE-12 halves the volume) rather than leaving the history unprotected by
default.

---

## RUNSTATE-12 — 91% of the legacy stream is per-delta token noise

**file:line** `.claude/local-orchestrators/touch-aggregator/events.jsonl`
(540 of 590 lines have `"stage":"tokens"`, `"quiet":true`)
**severity** minor

**Scenario.** Measured composition of `touch-aggregator`: 540/590 lines
(91.5%) are token-delta events, 236 KB for a 27-minute run with seven agents;
`touch-repo-recon` is 70/103 (68%). Every one carries the full
`{"in","out","cached","cache_write"}` delta plus a duplicate cumulative copy
inside `agent.tokens`. `monitoring.md` mandates full replay on connect, and D4
routes the same stream through legacy ingest, so both pay this cost twice.
A one-hour implement-plan run with retries would produce a multi-MB stream that
Touch replays into the browser on every reconnect.

**Recommendation.** The legacy adapter should fold `quiet:true` token events
into a per-agent running total and emit at most one `kind:"token"` v2 record per
agent per throttle window (the cumulative `agent.tokens` object is already
carried on each event, so folding is lossless — take the last, don't sum the
deltas). The v2 writer should persist cumulative totals with a throttle, never
one row per delta. Add a size/line-count assertion to the legacy test so this
does not regress.

---

## RUNSTATE-13 — Task-folder layout is not uniform: a plan-only folder is listed as a task

**file:line** `.claude/local-orchestrators/touch-monitor-spawn/` (contains only
`plan/`); `.claude/shared/monitoring/monitor_server.py:133-145`
**severity** minor

**Scenario.** `discover_tasks()` (`monitor_server.py:49-62`) lists **every
directory** under `local-orchestrators/`, so `touch-monitor-spawn` — which has
no `events.jsonl`, no `orch-config.json`, no `findings/`, no `orch-scripts/`,
only `plan/touch-monitor-spawn-plan.md` — appears in `/tasks` as a real task
with `events:false, status:"empty", mtime:0`. Layout also varies the other way:
`touch-aggregator` uniquely has `context/driver-context.md`, and only it has
`monitor_server.log`. So none of `events.jsonl`, `orch-config.json`,
`orch-scripts/`, `context/` can be assumed present.

**Recommendation.** Touch's task list must (a) tolerate a missing
`orch-config.json` (no `wf_dir` ⇒ synthesize `runId` per RUNSTATE-2), (b) render
plan-only folders as a distinct "plan only / never run" kind rather than an
empty task, and (c) never offer join/pause/stop controls on a folder with no
stream. Assert this against `touch-monitor-spawn` as a fixture — it is a free
real-world instance of the edge case.

---

## RUNSTATE-14 — `/tasks` returns two different token shapes

**file:line** `.claude/shared/monitoring/monitor_server.py:82`, `:111`, `:143`
vs `:126-128`
**severity** minor

**Scenario.** The populated path returns four keys
(`{"in","out","cached","cache_write"}`, line 126-128); the three empty/short-
circuit paths return only two (`{"in": 0, "out": 0}`). `monitor.html:655` masks
it with `tok.cached || 0, tok.cache_write || 0`, so it is invisible today — but
a typed Touch client (or a `sum()` over the four keys) throws or silently
under-reports on the `touch-monitor-spawn` entry.

**Recommendation.** Return the full four-key object from all four paths in
`monitor_server.py`. In Touch, define the token record shape once with all four
fields required and default-zero, and never rely on `|| 0` at the render site.

---

## RUNSTATE-15 — The watcher stalls silently if the journal is truncated in place

**file:line** `.claude/shared/monitoring/decision_watcher.py:493-512`
(`load_state`) and `:575` (`if size > state["offset"]`)
**severity** minor

**Scenario.** `load_state` resets the checkpoint only when the journal **path**
changes (documented as D8). The tail loop then advances only when
`size > state["offset"]`; there is no `size < offset ⇒ re-ingest from 0` branch.
If the same journal path is ever truncated or rewritten shorter, the watcher
spins forever emitting nothing, with no error. This is not hypothetical
housekeeping: all three checkpoints currently sit at offsets **exactly equal**
to their journal's size (56741 / 15567 / 750), so the condition is one
truncation away, and the aggregator plan's own D6 mandates precisely the
missing rule for its tailer ("inode change or `size < offset` ⇒ full idempotent
re-ingest from 0").

**Recommendation.** Touch's tailer must key checkpoints on
`(st_dev, st_ino, size, offset)` per D6 and must not inherit this gap — call it
out explicitly in the plan item that says "copy `decision_watcher.py`'s tailing
semantics", so the copy is understood as *torn-tail handling only*
(`decision_watcher.py:470-491`), not the checkpoint identity. Optionally add the
`size < offset` branch to the watcher too; it is a three-line fix.

---

## RUNSTATE-16 — `status.sh` validates nothing; the "no double quotes" rule is documented for the wrong reason

**file:line** `.claude/shared/monitoring/status.sh:18,28-44`; `CLAUDE.md:126`
**severity** minor

**Scenario.** Verified in a throwaway dir. `status.sh` accepts arbitrary
`plan`/`stage`/`state` strings with no enum check:

```
$ status.sh 'plan|weird' 'st age' 'BOGUSSTATE' 'detail'
{"ts":"…","plan":"plan|weird","stage":"st age","state":"BOGUSSTATE","detail":"detail","title":"T\"x"}
```

`monitor.html` styles only `queued|running|done|failed|info|stale`
(`:11,:46-95,:299`), so a typo'd state produces a silently unstyled row. An
empty detail is also accepted (`"detail": ""`).

Separately, CLAUDE.md:126 says "Keep event `detail` strings short, single-line,
and free of double quotes." The JSON-validity half of that is false — `status.sh`
builds the line with `json.dumps` (`:44`), and a detail containing `"` and a
newline round-trips fine:
`{"detail": "has \"quotes\" and\nnewline"}` parses cleanly. The rule is real but
its reason is **shell and JS-template-literal embedding** in the generated
`orch-scripts/*.workflow.js`, not JSON.

**Recommendation.** (a) Have the v2 writer validate `state` against the enum and
reject unknown values; the legacy adapter maps unknown states to `info` rather
than dropping the event. (b) Reword CLAUDE.md:126 to state the real reason
("the detail is embedded in a shell command inside a JS template literal") so
the constraint survives contact with someone who tests it. (c) If touching
`status.sh`, warn on stderr for an out-of-enum state rather than failing — it is
best-effort colour and must never break an agent.

---

## RUNSTATE-17 — `stale` is documented and styled but never appears in 821 real event lines

**file:line** `.claude/shared/monitoring/status.sh:4`;
`.claude/shared/monitoring/monitor.html:77,88,299,335`
**severity** nit

**Scenario.** `status.sh:4` documents six states; `monitor.html:299` declares
`NODE_STATES = ["running","done","failed","stale"]` and styles `.stale`. Across
all 821 lines of real history the observed state set is only
`{queued, running, info, done, failed}` — `stale` occurs zero times, because the
frontend *synthesises* it client-side (`monitor.html:335`). Any legacy fixture
written from the documented schema would therefore exercise a shape that never
occurs in production, while missing the shapes that do (RUNSTATE-4's
`plan|failed` on success, RUNSTATE-7's duplicate terminals, RUNSTATE-2's two
waves).

**Recommendation.** Build `tests/test_legacy.py` (aggregator plan line 764)
fixtures by **copying real lines** out of `touch-aggregator/events.jsonl` and
`touch-repo-recon/events.jsonl`, not from the schema doc. Copy them into the
test fixtures directory now, while the folders still exist and before any
commit/cleanup decision — that also insulates the tests from RUNSTATE-11.

---

## RUNSTATE-18 — Empty state directories will not survive a clone

**file:line** `.claude/local-orchestrators/touch-full-recon/plan/`,
`.../touch-full-recon/report/`, `.../touch-aggregator/report/`
**severity** nit

**Scenario.** Three of the per-task subdirectories are empty. Git does not track
empty directories, so once RUNSTATE-11's initial commit is taken, a fresh clone
will not have `report/` or an empty `plan/`. The generated workflow scripts
redirect agent output into these paths; any script that writes
`<task>/report/x.html` without a preceding `mkdir -p` fails on a clone.
(`status.sh:22` already does `mkdir -p` for its own state dir — the same care is
not guaranteed elsewhere.)

**Recommendation.** Either add `.gitkeep` files to the per-task
`plan/`/`report/`/`findings/` directories, or make the workflow-script template
`mkdir -p` every output directory before writing. The second is the durable fix
and belongs in the template, not in each copy.

---

## Summary for the synthesizer

The three event streams in this repo are a **better spec than the spec**. They
contain, as real data: two-wave re-spawns into one folder (RUNSTATE-2),
a success recorded as a failure (RUNSTATE-4), a checkpoint that contradicts its
own stream (RUNSTATE-5), out-of-order and dual-format timestamps (RUNSTATE-6),
duplicated terminal events (RUNSTATE-7), colliding labels and stage keys
(RUNSTATE-8), and never-closed agents (RUNSTATE-9). Every one of these is a case
D4's legacy adapter must handle, and none is described anywhere in the plans.
The two structural asks are therefore: (1) add an explicit **legacy-ingest plan
item** — absent from `touch-monitor-spawn-plan.md` P1-P12 entirely — with
synthesized `runId`/`ordinal` rules and fixtures copied from these files; and
(2) **commit the repo and add `.touch/` to `.gitignore` before writing any
code** (RUNSTATE-10, RUNSTATE-11). Also correct the false `omnigent` provenance
claim (RUNSTATE-1) that has been telling every agent to ignore this corpus.
