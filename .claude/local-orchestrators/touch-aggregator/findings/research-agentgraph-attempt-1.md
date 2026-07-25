# research — perspective: SUBAGENT + WORKFLOW GRAPH RECONSTRUCTION (attempt 1)

Scope: exactly how a run's node/edge graph can be rebuilt from disk, what the
harness writes unconditionally vs. what only exists because an orchestrator
embedded a convention, and what an n8n-style graph can honestly display at each
moment of a run's lifecycle.

Everything below was verified against primary sources: the live session under
`~/.claude/projects/-home-laniakea-Projects-touch/`, the running workflow
`wf_829e6f58-b2f`, and `grep -a` / `dd` reads of the CLI binary
`/home/agent/.local/share/claude/versions/2.1.220` (byte offsets are cited so
each claim is re-checkable).

---

## Ground truth: what actually exists on disk

Observed layout for the live run (`ls -la` of the session dir):

```
~/.claude/projects/<slug>/<sessionId>.jsonl                       # parent session transcript
~/.claude/projects/<slug>/<sessionId>/subagents/
    agent-<agentId>.jsonl                                         # Agent-tool subagent transcript
    agent-<agentId>.meta.json                                     # sidecar
    workflows/<runId>/journal.jsonl                               # workflow journal
    workflows/<runId>/agent-<agentId>.jsonl                       # workflow subagent transcript
    workflows/<runId>/agent-<agentId>.meta.json                   # sidecar
~/.claude/projects/<slug>/<sessionId>/workflows/<runId>.json      # DOES NOT EXIST until the run ends
```

Observed sidecar contents (verbatim):

```
subagents/agent-a483cae616edffe81.meta.json
{"agentType":"general-purpose","description":"Assess data-layer feasibility","toolUseId":"toolu_011Ug5qnU1bc2nEdXq57eRg7","spawnDepth":1,"model":"opus"}

subagents/workflows/wf_829e6f58-b2f/agent-a2ec106948f58d0c8.meta.json
{"agentType":"workflow-subagent","spawnDepth":1,"model":"opus"}
```

Observed journal (verbatim, complete file at the time of reading):

```
{"type":"started","key":"v2:c13a866b…","agentId":"a2fc883c96ff7b837"}
{"type":"started","key":"v2:37595476…","agentId":"a74f0c93253253ef5"}
{"type":"started","key":"v2:d156e840…","agentId":"a9eabf26b8e3f218c"}
{"type":"started","key":"v2:03b353da…","agentId":"a82d2e2591c84a3d7"}
{"type":"started","key":"v2:8a22f426…","agentId":"a79fa2f48f602defb"}
{"type":"started","key":"v2:c9fddfa8…","agentId":"a2ec106948f58d0c8"}
```

Binary confirms the only two journal shapes (offset 255829400-ish, the
`agent()` implementation):

```
a.append({type:"started", key: ge, agentId: Ze})
a.append({type:"result",  key: ge, agentId: Oe ?? "", result: Ze})
```

---

## AGENTGRAPH-1 — Journal entries have no timestamps, and journal ORDER is not spawn order

- **Proof**: `/home/agent/.claude/projects/-home-laniakea-Projects-touch/dd469822-2546-47d9-aaa3-31db4cb705e8/subagents/workflows/wf_829e6f58-b2f/journal.jsonl:1-6` (no `ts` field anywhere), and binary offset `255829400` (`a.append({type:"started",key:…,agentId:…})` — three fields, no clock).
- **Severity**: major

Journal append order for the 6-way `parallel()` fan-out was
`a2fc, a74f, a9ea, a82d, a79f, a2ec`. The true spawn order, taken from each
transcript's first-line `timestamp`, is
`a2fc(.846), a82d(.849), a74f(.850), a9ea(.851), a2ec(.852), a79f(.852)`.
Two agents even share a millisecond. So journal position is an append race, not
a causal or temporal order.

**Scenario**: Touch lays out an n8n graph left-to-right by journal line number.
On every parallel fan-out the columns are shuffled relative to reality, and any
"who started first" annotation is wrong. Worse, if Touch ever derives edges
("A then B") from adjacency in the journal, it invents causality that does not
exist.

**Recommendation**: treat `journal.jsonl` strictly as an unordered *set* of
facts `(key → agentId)` and `(key → result)`. Every timestamp on a node must
come from the agent transcript: spawn = first parseable `timestamp` in
`agent-<id>.jsonl`; last activity = last parseable `timestamp`. Order sibling
nodes by that spawn timestamp, and when two spawn times are within the same
millisecond, fall back to a stable tiebreak (agentId) rather than pretending to
know. `decision_watcher.py:244-302` (`first_ts` / `last_ts`) already implements
exactly this and is the right model to copy.

---

## AGENTGRAPH-2 — `label` and `phase` given to `agent()` are NEVER written to disk; workflow node names are a convention, not harness data

- **Proof**: `.claude/skills/execute-research/templates/research.workflow.js:139-143` passes `label: \`research:${p.key}\`, phase: 'Research'`; the resulting sidecars contain only `{"agentType":"workflow-subagent","spawnDepth":1,"model":"opus"}` (all six files under `subagents/workflows/wf_829e6f58-b2f/*.meta.json`). Binary offset `254695154` shows the full set of fields the sidecar writer can emit — `agentType, isFork, worktreePath, worktreeBranch, cwd, spawnMode, description, name, toolUseId, parentAgentId, stoppedByUser, spawnDepth, taskKind, teamName, color, planModeRequired, customAgentType, model, permissionMode` — `label` and `phase` are not among them, and `description`/`name` are only populated on the Agent-tool path.
- **Severity**: blocker (for the README's "n8n-like UML diagram" with meaningful node names)

**Scenario**: Touch renders the per-terminal graph for a running
`execute-research` run. Six workflow nodes appear, all identical:
`workflow-subagent · opus · spawnDepth 1`. The graph is unreadable — the user
cannot tell the `sessiondata` researcher from the `control` researcher without
opening 400 KB transcripts.

The `label` DOES exist at runtime — binary offset `255835017` shows the
`workflow_agent` progress record carrying
`{index, label, phaseIndex, phaseTitle, agentId, agentType, isolation, model, fallbackModel, state, startedAt, queuedAt, attempt, lastAttemptReason, lastToolName, lastToolSummary, promptPreview, lastProgressAt}` — but that is an in-process progress stream to the TUI, unreachable from another process (see AGENTGRAPH-6).

**Recommendation**: accept that node naming is Touch's own convention and make
it explicit in the product. The only unconditional on-disk name source for a
workflow agent is the prompt text (first line of `agent-<id>.jsonl`). Reuse the
existing marker `[monitor] plan=<id> stage=<stage> role=<role> attempt=<n>`
(`.claude/shared/monitoring/monitoring.md:56-63`,
`decision_watcher.py:121`) as the canonical node-identity header, and specify in
Touch's docs that a workflow without that marker degrades to
`agentId + first 60 chars of prompt` (which is exactly the harness's own default
label rule — binary offset `255832100`: `label ?? promptText.slice(0,60)`).
Do not present convention-derived names as if the harness supplied them.

---

## AGENTGRAPH-3 — One logical node can emit MANY `started` entries with different agentIds (stall / throttle / user-retry), and the existing watcher leaks a phantom "running" node for each

- **Proof**: binary offsets `255836500-255840000`. The inner runner is
  `lr(script, label, attemptNo, reason)`; each invocation allocates a fresh
  agentId (`let _r = xO(); he(_r);`) and `he` is the started-callback that
  appends `{type:"started", key, agentId}`. Retry reasons observed in the code:
  `"throttled"`, `"user-retry"`, `"stalled"`. Constants at offset `255849080`:
  `r6y = 180000` (180 s no-progress stall timeout), `USd = 5` (max stall
  retries). Error strings confirm: `agent stalled on all ${n} attempts (no
  progress for ${ms}ms each)`, `agent abandoned after ${n} attempts`.
- **Severity**: major

**Scenario**: a researcher agent makes a 4-minute `grep` over the 275 MB CLI
binary (exactly what this research task does). The harness's 180 s stall watchdog
aborts it and respawns it as a NEW agentId under the SAME journal `key`. The
journal now has two `started` lines and will eventually have ONE `result`. Touch
draws two nodes; the abandoned one never gets a result and ticks "running"
forever. `decision_watcher.py:610-616` explicitly refuses to close it: the
stale-close guard requires `info["attempt"] > oinfo["attempt"]`, and both
attempts carry the SAME prompt, hence the same `attempt=1` marker. So the bug is
already latent in the prior art and Touch would inherit it.

**Recommendation**: make the journal `key` the node identity and the `agentId`
an *attempt* of that node. Render one graph node per key with an attempt badge
(`attempt 2/5`); when a second `started` arrives for a key, mark the previous
agentId `superseded` (not `running`, not `stale`) and roll its tokens into the
node. Also note the result row carries `agentId: Oe ?? ""` — an empty string
when no agentId was ever allocated; the reader must tolerate that rather than
key a lookup on it.

---

## AGENTGRAPH-4 — The journal `key` is a content hash; identical prompts collide, and a resumed run replays cached agents with NO journal entry at all

- **Proof**: binary offset `255825400`:
  `FSd(e,t,r) = "v2:" + sha256(r ‖ "\0" ‖ e ‖ "\0" ‖ VWy(t))` where `e` is the
  prompt and `VWy(t)` serialises only `{schema, model, effort, isolation,
  agentType}` (offset `255825100`). `label`, `phase` and the call index are
  deliberately excluded. Offset `255825000` (`$Sd`): the loader builds
  `results: Map<key, entry>` and `started: Map<key, entry[]>` — results are a
  MAP, so a duplicate key keeps only the last. Offset `255834300`: on a resume
  cache hit the code emits a `workflow_agent_<i>_cached` progress event, calls
  `m(cached.result)` and **does not append anything to the journal**.
- **Severity**: major

**Scenario A (collision)**: an orchestrator does
`parallel([...Array(3)].map(() => () => agent(SAME_PROMPT, SAME_OPTS)))` — three
live siblings share one key. The journal shows 3 `started` + 1 `result`. Touch
either draws one node (losing two) or three nodes of which two never complete.

**Scenario B (resume)**: the user hits Touch's "restart" control and the run is
resumed via `resumeFromRunId`. Every already-completed agent is replayed from the
journal cache without spawning and without any new journal line. A graph built
purely from a *tail* of the journal after the restart shows an empty run;
a graph built from the whole journal shows the old agentIds as if they were the
new run's. Neither is honest.

**Recommendation**: (a) node id = `key + "#" + ordinal-of-this-agentId-within-that-key`;
(b) explicitly detect resume — if `results.has(key)` at run start, mark those
nodes `replayed from journal (not re-executed)` and grey them, because that is
exactly what the harness's own TUI does (`"from resume journal"`, binary offset
`263874283`). Touch must not claim a re-run of a cached node happened.

---

## AGENTGRAPH-5 — Queued-but-not-yet-started agents have ZERO on-disk footprint, and the concurrency cap is CPU-derived

- **Proof**: binary offset `255849281`:
  `KWy = zWy(os.cpus().length)`; offset `255827084`:
  `function zWy(e){return Math.min(16, Math.max(2, e-2))}`. `KWy` is the limiter
  wrapping the `agent()` runner (offset `255828431`: `F = AB(KWy, K)`).
  On this box `nproc` = 16 → cap 14, which is why all 6 researchers started
  within 6 ms. The `queued` state is only ever emitted as an in-memory progress
  event (offset `255834800`, `workflow_agent_${i}_queued`) — nothing on disk.
- **Severity**: major

**Scenario**: the same `execute-research` run on a 4-vCPU host → cap = 2. Two
researchers start; four are queued. Touch's graph shows a 2-node run. The user
sees the fan-out "shrink" and has no way to know four nodes are pending — and
when they later appear one at a time the graph appears to be spawning agents
the script never asked for.

**Recommendation**: Touch cannot observe queued nodes; it must *declare* them.
Two honest options, both needed: (1) parse the orchestrator script's
deterministic fan-out (`PERSPECTIVES` in `research.workflow.js:41-45` is a pure
function of a literal array) or, better, (2) have the orchestrator seed the
graph before launching — the monitoring module already supports exactly this
(`status.sh <plan> plan queued`, `monitoring.md:50-53`, and the live
`events.jsonl:1-3` shows three seeded `queued` cards). Render declared-but-
unobserved nodes in a visually distinct "declared" style so the graph never
implies the harness confirmed them.

---

## AGENTGRAPH-6 — The rich node record exists only in memory and is flushed exactly ONCE, at run completion — a live graph cannot use it

- **Proof**: `~/.claude/projects/<slug>/<sessionId>/workflows/` does not exist
  for the currently-running `wf_829e6f58-b2f` (`ls -la` of the session dir shows
  only `subagents/`). Binary offset `255823650` defines the writer
  `OSd(runId, data)` → `<projectDir>/<sessionId>/workflows/<runId>.json`.
  `grep -a -o -b 'OSd(' <binary>` returns exactly two offsets: `255823892` (the
  definition) and `255861108` (the single call site, inside the workflow
  completion path, alongside `tengu_workflow_completed`).
- **Severity**: blocker (for any design that plans to read `<runId>.json` live)

The snapshot's payload is the *only* place the graph-grade data lands:
`{runId, timestamp, taskId, script, scriptPath, args, result, agentCount, logs,
durationMs, error, summary, workflowName, title, status, startTime, phases,
defaultModel, workflowProgress[], totalTokens, totalToolCalls}` — where
`workflowProgress` is the array of `workflow_agent` records with
`index/label/phaseIndex/phaseTitle/state/tokens/toolCalls/durationMs/attempt/
resultPreview/promptPreview`. `status` can be `completed | failed | killed`
(offset `255860700`), so an aborted run *does* get a snapshot — but only if the
CLI process survives to write it. A SIGKILL of the CLI leaves nothing.

**Scenario**: Touch's per-terminal page is designed around `<runId>.json`.
During the entire run (minutes to hours — the live run's turn durations are
90 s–568 s per `system/turn_duration` records) the page shows nothing, then the
whole graph pops into existence at the end. That is the opposite of the product's
intent.

**Recommendation**: build the LIVE graph from `journal.jsonl` + per-agent
transcripts (+ the orchestrator convention markers), and use `<runId>.json`
purely as a post-hoc reconciliation pass that back-fills authoritative
`label`/`phaseTitle`/`index`/`attempt`/`durationMs` once the run ends. Design the
node model so those fields are *optional* and arrive late.

---

## AGENTGRAPH-7 — Transcript and snapshot paths are keyed to the CURRENT session id, so `/clear` or `/compact` mid-run splits one run's data across sibling directories

- **Proof**: binary offset `254287140`:
  `K0(agentId)` builds `<projectDir>/<kt()>/subagents/[<subdir>]/agent-<id>.jsonl`
  where `kt()` is the *current* session id resolved at call time; offset
  `255823700`: `MSd()` (the `<runId>.json` dir) and `gte(runId)` (the workflow
  transcript dir) both do the same. The journal path, by contrast, is captured
  once at workflow launch (`new JPs(runId)` at offset `255845700`, resolved
  through `gte`), so it does **not** move.
- **Severity**: major

**Scenario**: the user runs a long `implement-plan` and `/compact`s the driver
session halfway. Half the sub-plan agents' transcripts are under session A, half
under session B, and `<runId>.json` lands under session B while `journal.jsonl`
stays under session A. Touch, which resolved the run from session A, shows every
post-compaction node as "spawned, zero tokens, no activity", and never finds the
final snapshot.

**Recommendation**: never resolve agent data relative to a single session dir.
Glob `~/.claude/projects/*/*/subagents/workflows/<runId>/agent-<id>.jsonl` and
`~/.claude/projects/*/*/workflows/<runId>.json` and union the results, deduping
token usage by API `message.id`. `decision_watcher.py:76-100` (`agent_paths`)
is the working reference — copy its semantics, including "oldest copy first"
for prompt extraction (`decision_watcher.py:224-241`), because a rotated
continuation transcript can open with resume scaffolding instead of the original
prompt (and therefore without the marker).

---

## AGENTGRAPH-8 — `toolUseResult.totalTokens` is the LAST API call's usage, not a per-agent rollup — using it under-reports by >10x

- **Proof**: parent transcript
  `~/.claude/projects/-home-laniakea-Projects-touch/dd469822-2546-47d9-aaa3-31db4cb705e8.jsonl`,
  the two `Agent` tool results. For `a483cae616edffe81` the harness reports
  `totalTokens: 90971` with `usage.iterations` of length **1**
  (`input_tokens: 2, cache_read: 80697, cache_creation: 2101, output: 8171` —
  sums to exactly 90971). Summing `message.usage` across that agent's own
  transcript (dedup by `message.id`, 70 assistant rows → 21 distinct messages)
  gives `in 1,235,241 / out 24,569`. For `a4e343a0f7d73268c`: reported 147,938
  vs. summed 2,321,856 / 24,763.
- **Severity**: major

**Scenario**: Touch shows "this agent used 91k tokens" on the graph node while
the agent actually consumed ~1.26M. Any cost or budget display built on
`totalTokens` is wrong by an order of magnitude, and wrong *inconsistently*
(the error scales with turn count).

Note the same result object DOES carry trustworthy rollups:
`totalDurationMs` (337105 / 464311 ms — matches transcript first/last delta),
`totalToolUseCount` (38 / 47), `toolStats {readCount, searchCount, bashCount,
editFileCount, linesAdded, linesRemoved, otherToolCount}` and `resolvedModel`
(`"claude-opus-5[1m]"`, i.e. the *resolved* model, unlike the sidecar's coarse
`"opus"`). So the record is mixed-fidelity, not uniformly wrong.

**Recommendation**: compute per-node tokens by summing `message.usage` over the
transcript, deduped by `message.id` with a `path+lineno` fallback for id-less
rows — `decision_watcher.py:154-197` already does this correctly, including the
`in = input + cache_creation + cache_read` convention and the separate `r:`/`w:`
breakdown that keeps the number interpretable. Use `totalDurationMs`,
`totalToolUseCount`, `toolStats` and `resolvedModel` from `toolUseResult`;
ignore its `totalTokens`. Document the token convention on the UI (a per-turn
re-send means "input" is cumulative prefix volume, not distinct content) or
users will report the 2.9M numbers as a bug.

---

## AGENTGRAPH-9 — There is no completion marker inside a subagent transcript; liveness is a three-state problem, not a boolean

- **Proof**: all eight subagent transcripts end on an ordinary `assistant` or
  `user` row with no terminal record (checked with a first/last-line dump of
  every `agent-*.jsonl` in the session). Measured flush lag for the six *live*
  workflow agents at one instant: `0.9s, 5.0s, 6.4s, 13.8s, 20.1s, 30.4s` since
  their last transcript timestamp — all six were running. Meanwhile the harness's
  own stall threshold is 180 s (binary `r6y=180000`), so any "idle > N ⇒
  finished" rule with N < 180 s produces false positives, and with N ≥ 180 s the
  graph is three minutes behind reality.
- **Severity**: major

**Scenario**: Touch colours a node green when its transcript stops growing for
30 s. An agent running a 4-minute `grep` (or a long test suite — precisely what
`implement-plan`'s test gate does) is drawn as finished, then "un-finishes" when
it writes again. The graph flickers and the user distrusts it.

**Recommendation**: model node liveness as three states with distinct visuals:
`running` (started, no result observed, last activity < 180 s),
`finished` (a journal `result` for its key, or a parent `tool_result` for its
`toolUseId` — the ONLY definitive completion evidence), and
`unknown / possibly stalled` (started, no result, idle ≥ 180 s — show the idle
duration rather than guessing). Note `decision_watcher.py:305-327` already
recognises the converse hazard: a transcript can stop flushing *before* the real
end (a long final Bash call), so when a `result` arrives live, the read moment is
a better completion timestamp than the transcript's last line unless that line is
fresh (≤30 s). Copy that rule.

---

## AGENTGRAPH-10 — `spawnDepth` does not encode workflow nesting; the parent edge for a workflow agent exists only as a directory name

- **Proof**: `subagents/agent-a483cae616edffe81.meta.json` (a direct Agent-tool
  child of the session) has `"spawnDepth":1`; every
  `subagents/workflows/wf_829e6f58-b2f/agent-*.meta.json` (a child of a workflow
  that is itself a child of the session) ALSO has `"spawnDepth":1`. Binary offset
  `254695154` shows `parentAgentId` is written only when the spawning context had
  an `agentId` (`...r.agentId && {parentAgentId: r.agentId}`), and offset
  `255833700` shows a workflow agent's context is built with
  `parentAgentId: fW(Ae) ? undefined : Ae?.agentId` — undefined when the workflow
  was launched from the main session, which is our case. No sidecar in this
  session has `parentAgentId`. No sidecar has `startTime` either.
- **Severity**: major

**Scenario**: Touch builds edges by `spawnDepth` and draws a flat two-level graph
with the workflow's six researchers as siblings of the two ad-hoc feasibility
subagents — losing the fact that the six belong to a workflow run and the two do
not. The "loop" the README wants to control is exactly that grouping.

**Recommendation**: build edges from three distinct, unconditional sources, in
this priority:
1. **session → Agent-tool subagent**: match `meta.toolUseId` against the
   `tool_use.id` of the `Agent` block in the parent transcript (observed pair:
   `toolu_011Ug5qnU1bc2nEdXq57eRg7` ↔ `agent-a483cae616edffe81.meta.json`).
   That tool_use also gives you `subagent_type`, `description`,
   `run_in_background` and `model`.
2. **session → workflow → workflow subagent**: the `Workflow` tool_result (see
   AGENTGRAPH-11) gives `runId` + `transcriptDir`; every `agent-*.jsonl` inside
   `subagents/workflows/<runId>/` is a child of that workflow node. The directory
   IS the edge.
3. **agent → nested agent** (depth ≥ 2): `meta.parentAgentId`, which the writer
   does populate when the spawner is itself an agent.
Treat `spawnDepth` as a hint only, never as the tree.

---

## AGENTGRAPH-11 — The session→workflow edge exists in exactly one place: a single `toolUseResult` in the parent transcript

- **Proof**: parent transcript, `2026-07-25T02:59:29.624Z`:
  ```json
  {"status":"async_launched","taskId":"wpbwj76b3","taskType":"local_workflow",
   "workflowName":"touch-aggregator-research","runId":"wf_829e6f58-b2f",
   "summary":"Research how to build Touch …",
   "transcriptDir":"/home/agent/.claude/projects/…/subagents/workflows/wf_829e6f58-b2f",
   "scriptPath":"/home/laniakea/Projects/touch/.claude/local-orchestrators/touch-aggregator/orch-scripts/research.workflow.js"}
  ```
  The only other trace of the running workflow in the whole parent transcript is
  `pendingWorkflowCount: 1` on one `system/turn_duration` record
  (`2026-07-25T03:00:07.993Z`). No workflow progress is mirrored into the parent
  transcript.
- **Severity**: minor (easy to handle, fatal if missed)

**Scenario**: Touch's aggregator watches only `subagents/workflows/*/` and finds
a run it cannot attribute to a session, a script, or a human request. The sidebar
("list of terminal sessions") then cannot show "this session is running
touch-aggregator-research".

**Recommendation**: index every `Workflow` tool_result at ingest and key a
`WorkflowRun` node off `runId`, carrying `taskId`, `workflowName`, `summary`,
`scriptPath`, `transcriptDir` and the launching `tool_use.id`/timestamp. Also
persist `scriptPath` — it is the only pointer back to the orchestrator source,
which is what Touch's "start / restart a loop" control would have to re-invoke.

---

## AGENTGRAPH-12 — The prior art's per-agent labels collide across parallel siblings

- **Proof**: `.claude/local-orchestrators/touch-aggregator/events.jsonl:11-20` —
  all six researchers emit
  `"agent": {"id": "<8 hex>", "label": "research #1", "state": "running", …}`.
  The label is built as `f"{info['role']} #{info['attempt']}"`
  (`decision_watcher.py:637, 683`), and role/attempt are identical for every
  sibling in a fan-out; only `stage` differs (`sessiondata`, `liveio`, `control`,
  `agentgraph`, `stack`, `priorart`).
- **Severity**: minor (but directly visible in the product Touch is copying)

**Scenario**: Touch's graph inherits the label and renders six nodes all reading
`research #1`. The user cannot tell them apart — the exact failure AGENTGRAPH-2
describes, reintroduced by the tooling rather than the harness.

**Recommendation**: node label = `stage` (the perspective/sub-plan key) with
`role #attempt` as secondary text, i.e. `agentgraph · research #1`. `stage` is
already unique per sibling in both skills (`p.key` in `research.workflow.js:67`,
`sp.id` in `implement.workflow.js:89`).

---

## AGENTGRAPH-13 — Agent ids are truncated to 8 chars in the event stream, breaking the join back to the transcript

- **Proof**: `decision_watcher.py:636` (`"id": agent_id[:8]`) vs. the real 17-hex
  agentId (`a82d2e2591c84a3d7` → `a82d2e25`). `.watcher-state.json` keeps the full
  id; `events.jsonl` does not.
- **Severity**: minor

**Scenario**: Touch's graph node is clicked; the UI wants to open that agent's
transcript. From `events.jsonl` alone it has only 8 chars and must scan every
`agent-*.jsonl` filename to disambiguate — and cannot detect a genuine prefix
collision if one ever occurs.

**Recommendation**: if Touch reuses this event stream, carry the full agentId
(add `agent.agent_id` alongside the short `agent.id` so existing consumers keyed
on `id` don't break) plus the resolved transcript path. If Touch reads the
harness files directly, this is moot — prefer that.

---

## AGENTGRAPH-14 — Every carried-over run's `wf_dir` points at deleted directories, and the watcher's auto-discovery will silently latch onto the wrong run

- **Proof**: `.claude/local-orchestrators/{orch-monitoring-bugfix,sbx-cli-defaults,sbx-webui-host-access,trust-env-split-brain}/orch-config.json` all point at
  `/home/agent/.claude/projects/-home-laniakea-Projects-omnigent/…`;
  `ls -d /home/agent/.claude/projects/*/` returns only
  `-home-laniakea-Projects-touch/` and `lost+found/`.
  `find ~/.claude/projects -name journal.jsonl` returns exactly one file (the
  live run). `decision_watcher.py:62-69` falls back to
  `glob("~/.claude/projects/*/*/subagents/workflows/wf_*/journal.jsonl")` and
  picks `max(..., key=os.path.getmtime)` when the configured dir is missing.
- **Severity**: minor (for the graph), but a correctness trap

**Scenario**: Touch opens the `sbx-cli-defaults` task page and tries to rebuild
its graph. There is no journal and there are no transcripts — only
`events.jsonl`. If Touch instead reuses the watcher's fallback logic, it silently
attaches the *currently running* `touch-aggregator` journal to the historical
task and shows live nodes under a dead run.

**Recommendation**: for historical tasks, render the graph from the archived
`events.jsonl` only, and label it explicitly `archived — source transcripts
unavailable`. Never auto-discover a journal for a task whose configured `wf_dir`
does not exist; treat a missing `wf_dir` as "no live source", not as "find one".

---

## AGENTGRAPH-15 — Two data sources named in the brief are empty on this machine; the graph must degrade, not crash

- **Proof**: `ls -la ~/.claude/todos/` → only `lost+found`.
  `~/.claude/projects/<slug>/<sessionId>/workflows/` → does not exist (see
  AGENTGRAPH-6). `~/.claude/sessions/` contains exactly one file, `622.json`:
  `{"pid":622,"sessionId":"dd469822-…","cwd":"/home/laniakea/Projects/touch","startedAt":1784946693282,"procStart":"10028","version":"2.1.220","kind":"interactive","entrypoint":"cli","name":"touch-2b","nameSource":"derived","status":"busy","updatedAt":…,"statusUpdatedAt":…}` — note it is keyed by **pid**, not sessionId, and carries a `status` field (`busy`) that is the cleanest liveness signal for the *session* (not for agents).
- **Severity**: minor

**Scenario**: Touch's graph builder assumes per-agent progress records exist in
`todos/` (as `TodoWrite` state) and a `workflows/<runId>.json` per run. On a
fresh machine both are absent and the page renders empty or throws.

**Recommendation**: every source except `<sessionId>.jsonl`, `subagents/*.jsonl`
+ `.meta.json`, and `subagents/workflows/<runId>/journal.jsonl` must be treated
as optional enrichment. Also: `~/.claude/sessions/<pid>.json` is the right source
for the sidebar's session list and its `status` field, but it is pid-keyed, so a
stale file survives a crashed CLI — validate the pid (and `procStart`) before
showing a session as live.

---

## AGENTGRAPH-16 — The only on-disk evidence that a user terminated an agent is `stoppedByUser` in the sidecar — and workflow agents never get it

- **Proof**: binary offset `254695154` includes `...r.stoppedByUser && {stoppedByUser:!0}` in the sidecar writer, and offset `256121003` shows
  `TIe(e,t)` setting `stoppedByUser:!0` on the task registry entry. Offset
  `256489670` shows resume refusing a stopped agent
  (`Agent ${e} was stopped by the user and won't be resumed. Treat its work as
  cancelled`). But the workflow-agent path's user controls are different:
  offsets `255837000-255838000` show abort reasons `"user-retry"` and
  `"user-skip"` producing in-memory progress states (`error: "skipped by user"`,
  `error: "retry requested by user"`) — with **no** sidecar update and **no**
  journal entry. Our six workflow sidecars contain three keys and nothing else.
- **Severity**: major (this is precisely the README's pause/terminate feature)

**Scenario**: the user terminates a loop from Touch. If Touch implements that by
whatever mechanism, the graph afterwards must show *why* a node stopped. For an
Agent-tool subagent it can (`stoppedByUser: true` in the sidecar). For a
workflow subagent — the only kind `execute-research` and `implement-plan`
produce — there is **no on-disk record at all**: the node simply has a `started`
with no `result`, indistinguishable from a crash, a stall, or the CLI being
killed.

**Recommendation**: Touch must write its own control-audit record (who asked for
what, when, against which `runId`/`key`/`agentId`) and render terminated nodes
from *that*, not from harness state. Present harness-derived node state and
Touch-derived control state as two visually distinct layers, so the graph never
implies the harness confirmed a termination it did not record. Also surface the
distinction "no result because the run is still going" vs. "no result and the
run's journal has been quiet for N minutes" rather than collapsing both to
"failed".

---

## AGENTGRAPH-17 — What an n8n-style graph can HONESTLY show at each moment (the summary this perspective owes)

- **Proof**: the composite of AGENTGRAPH-1..16; the reconstruction recipe below
  was validated against the live run.
- **Severity**: major (this is the design decision the plan must make explicitly)

**Reconstruction recipe (unconditional harness data only)**

| Graph element | Source | Unconditional? |
|---|---|---|
| Session node | `~/.claude/projects/<slug>/<sessionId>.jsonl` + `~/.claude/sessions/<pid>.json` | yes |
| Session → Agent-tool subagent edge | parent `tool_use` (`name:"Agent"`, `id`) ↔ `meta.toolUseId` | yes |
| Agent-tool node label | `tool_use.input.description` / `meta.description` | yes |
| Session → Workflow node edge | parent `toolUseResult` `{runId, transcriptDir, scriptPath, workflowName}` | yes |
| Workflow → agent edge | file lives in `subagents/workflows/<runId>/` | yes |
| Nested agent edge (depth ≥ 2) | `meta.parentAgentId` | yes |
| Node spawn time | first `timestamp` in `agent-<id>.jsonl` | yes |
| Node last-activity | last parseable `timestamp` in `agent-<id>.jsonl` | yes |
| Node completion | journal `result` for its `key` / parent `tool_result` | yes |
| Node tokens | Σ `message.usage` over transcript, dedup by `message.id` | yes |
| Node tool count / duration | `toolUseResult.totalToolUseCount`, `.totalDurationMs` (Agent tool only) | yes |
| Node **role / plan / attempt / phase** | `[monitor]` marker in prompt | **NO — convention** |
| Node **label / phase title / index** | `<runId>.json` `workflowProgress[]`, at run END only | no (late) |
| **Queued** nodes | nothing | **NO — must be declared** |
| **Loop / retry grouping** | journal `key` (harness) + marker `attempt` (convention) | mixed |

**Honest lifecycle rendering**

- *Before launch*: only declared nodes (from the seeded `status.sh … queued`
  events or a parsed script). Style them as declarations.
- *T+0 to first transcript flush (~200 ms observed: workflow launched
  02:59:29.624, first agent transcript line 02:59:29.846)*: journal `started`
  lines may exist before the transcript is readable —
  `decision_watcher.py:330-367` retries classification 3× at 0.5 s and falls
  through to "pending" rather than blocking. Touch needs the same: a node can be
  known-to-exist but unnamed for a second.
- *During the run*: nodes have identity (agentId), parent, spawn time, live
  token/last-activity, and — only if the orchestrator embedded the marker — role,
  plan and attempt. State is `running` or `unknown/idle Ns`. Edges between
  sibling agents (the "loop" arrows impl→test→critique) are **not** in any
  harness data; they are implied by the orchestrator script's control flow and
  must be drawn from the marker's `plan`+`stage`, i.e. convention.
- *At each result*: the node gets a verdict shape. The verdict semantics
  (`passed`/`approved`/`files_changed`/`findings`) come from the workflow's
  schemas (`implement.workflow.js:45-61`), not the harness —
  `decision_watcher.py:370-447` already encodes exactly this shape-driven
  mapping and is the right thing to reuse.
- *At run end*: reconcile against `<runId>.json` to fill in authoritative
  `index`, `label`, `phaseTitle`, `attempt`, `durationMs`, `status`
  (`completed|failed|killed`), `totalTokens`, `totalToolCalls`. Only after this
  can Touch claim the graph is complete — and only if the snapshot exists.

**Recommendation**: write this table into Touch's data-model design as a
first-class contract, and make the UI encode the harness/convention split
visually (e.g. solid nodes+edges for harness-derived facts, dashed for
convention-derived or declared structure). The README asks for a graph that also
*controls* loops; a control surface built on convention-derived structure that
looks identical to harness-derived structure is how a user ends up terminating
the wrong node.

---

## Cross-cutting note on the two skills' shapes

`research.workflow.js` produces a graph of exactly
`len(PERSPECTIVES)` parallel nodes → barrier → 1 synthesis node
(`research.workflow.js:136-153`). `implement.workflow.js` produces
`1 divide node → per sub-plan { impl → test → critique }×attempts → finalgate
{ gate → fix }×2` (`implement.workflow.js:163-210, 255-349`), serial by default.
Both are pure functions of one structured output (`PERSPECTIVES`, `divide.subplans`),
so the *shape* of the graph is knowable ahead of execution — but only by reading
the script or the divider's result. Nothing in the harness's on-disk data
describes it. That is the single strongest argument for AGENTGRAPH-5's
"declare, then observe" model.
