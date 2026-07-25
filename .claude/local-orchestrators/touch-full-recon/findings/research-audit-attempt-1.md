# research-audit — attempt 1

Perspective: **audit of the prior research corpus** — the six `touch-aggregator`
findings files, the three `touch-repo-recon` findings files, `driver-context.md`,
and the two plans they produced. What remains unresolved or unverified; which
findings the plans dropped or contradicted without justification; which
corrected-class claims still lurk uncorrected; and the concrete experiment list
the implementation phase must still settle.

Method: read all nine findings files, both plans and the driver digest in full,
then re-ran the cheap empirical checks that the corpus rests on, against the
live substrate as of 2026-07-25 13:5x. All probes were read-only or ran in
`/tmp/claude-1000/audit/`; the only writes into a task folder are the two
mandated `status.sh` calls and this file.

**Corpus size**: 110 findings (aggregator, 6 perspectives) + 51 findings
(repo-recon, 3 perspectives) = 161. Plan coverage: the aggregator plan
disposes of all 110 (Parts B/C/D/E). **The 51 repo-recon findings have zero
disposition anywhere** (AUDIT-1).

**Headline**: four claims the plans encoded as normative are now falsified by
on-disk evidence (AUDIT-2, AUDIT-4, AUDIT-5, AUDIT-6), one binding mechanism is
inoperable in this repo today (AUDIT-8), and the single unverified item the
whole v0 control story rests on has still never been probed (AUDIT-3).

---

## AUDIT-1 — 51 findings from `touch-repo-recon` have no disposition in any plan; both plans predate them

**file:line** `.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md:823`
(Part D, "Merged/discarded findings register" — covers only `SESSIONDATA/AGENTGRAPH/LIVEIO/CONTROL/PRIORART/STACK`)
**severity** blocker

**Scenario.** The chronology is: aggregator research → `touch-aggregator-plan.md`
(D1–D14, T1–T23) → conversation → `touch-monitor-spawn-plan.md` (G1–G9, P1–P12)
→ `touch-orchestrate/SKILL.md` drafted → **repo-recon research** (INTENT-1..16,
SKILLS-1..17, V0TASK-1..18). Nothing was written after the repo-recon run. So
51 findings — including five of blocker severity (INTENT-1, INTENT-2, SKILLS-1,
SKILLS-2, V0TASK-1, V0TASK-2) — exist only as prose in three files that no plan
references and that `CLAUDE.md` does not point at. A synthesizer that starts
from "the normative plan" (`inception.md:10`) inherits the state of the world as
of 03:26Z and silently re-drops every one of them; an implementer handed
`implement-plan` never sees them at all, because `implement-plan` divides a
plan, not a findings directory.

Concretely, these are load-bearing and currently orphaned: the two-profile spawn
decision (SKILLS-1/-15/-16), the blocked-session stop latency (SKILLS-2), the
control-file writer/path/ack contract (SKILLS-3/-4/-5/-10, V0TASK-4/-5/-6), the
missing module-interface contract that makes the v0 plan unimplementable under
`implement-plan`'s isolation (V0TASK-7/-8/-9), the token transport (V0TASK-9),
and every doc correction (INTENT-3/-4/-5/-7/-14).

**Recommendation.** The reconciled plan must open with a **disposition register
covering all 161 findings**, one line each: `→ item Tn` | `→ decision Dn` |
`merged with X` | `rejected, reason`. Make the register a plan item's acceptance
criterion (a static test that every finding id in
`.claude/local-orchestrators/*/findings/*.md` appears exactly once in the plan
text is ~20 lines and enforces it forever). Without it there is no way to tell a
deliberate rejection from an oversight — which is precisely the failure this
recon exists to prevent.

---

## AUDIT-2 — D8's normative rule "journal `result` is an opaque string, never parsed as JSON" is false on every journal on this machine, and contradicts the verdict mapper the same plan says to reuse

**file:line** `.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md:243-244`
("journal `result` strings are opaque display text, never parsed as JSON
(SESSIONDATA-10)"), restated at `inception.md:106`; origin
`findings/research-sessiondata-attempt-1.md:337-364` (SESSIONDATA-10)
**severity** blocker

**Scenario.** SESSIONDATA-10 observed, mid-run, `"result":"{'findings': [{'id':
'AGENTGRAPH-2', …"` — a Python-repr string — and concluded `result` is never
JSON. Measured today across **all three** journals on disk (`wf_829e6f58-b2f`,
`wf_455b348c-e17`, `wf_930e210a-6da`), every `result` payload is a **JSON
object**:

```
dd469822…/subagents/workflows/wf_829e6f58-b2f/journal.jsonl
  6 × result → dict keys ['findings','findings_file','summary']
  1 × result → dict keys ['item_count','plan_file','summary']
e423cd3c…/wf_455b348c-e17/journal.jsonl → 2 × dict ['findings','findings_file','summary']
```

zero string payloads. And `decision_watcher.py:370-447` — the exact range T8
instructs the implementer to copy for verdict mapping — is **built on the dict
shape**: `describe_result()` branches on `"passed" in r`, `"approved" in r`,
`"findings" in r`, `"files_changed" in r`. So the plan simultaneously says
"never parse `result`" and "reuse the shape-driven mapping at :370-447".

An implementer following D8 literally renders `{'findings': [...4000 chars...]}`
as node display text, drops every verdict (`passed`/`approved`/`files_changed`),
and T8's own test ("journal `result` strings … opaque") passes while the graph
shows no gate outcomes at all — the single most important thing an
`implement-plan` graph must show.

**Recommendation.** Replace the rule with the observed truth: **`result` is
polymorphic — a schema-validated object when the agent returned structured
output, a free string otherwise.** Parse defensively (`r = result if
isinstance(result, dict) else {}`, exactly as the prior art does), drive verdicts
from the dict keys per `decision_watcher.py:370-447`, and render the string case
as opaque text. Add the fixture: copy the three journals above into
`tests/fixtures/journals/` (they are deleted by the retention sweep — AUDIT-7)
and assert both arms.

---

## AUDIT-3 — the one unverified item the entire v0 control story rests on (hook hot-reload) has still never been probed

**file:line** `touch-aggregator-plan.md:866-869` (Part E item 2, "Hook
hot-reload for already-running sessions (CONTROL-10) … v1 assumes NO");
depends on it: `touch-monitor-spawn-plan.md:20` (G1 defers the owned-session
spawner), `findings/research-skills-attempt-1.md:96-100` (SKILLS-2: "make the
deterministic backstop a v0 item")
**severity** blocker

**Scenario.** Chain the three documents: (a) v0 has **no owned sessions** — G1
explicitly defers the spawner, so every session in v0 is *observed*; (b) hooks
are session-scoped configuration and the plan's own v1 assumption is that they
apply **only at spawn** (CONTROL-10, Part E item 2, still unprobed); (c) SKILLS-2
shows the cooperative `control.jsonl` stop cannot work during a skill run at all,
because the orchestrating session is blocked inside one `Workflow` tool call for
the whole run and never polls — so G6's 120 s expiry fires on every stop of the
loops Touch exists to control, and SKILLS-2's fix is "promote the hook gate to
v0". If hooks cannot be installed into an already-running session, that fix is
unavailable and **v0 ships with no working stop for any real run**: cooperative
stop can't be polled, hook stop can't be installed, and there is no PTY to type
into.

The experiment is 10 minutes: start a probe session under `/tmp`, write a
`PreToolUse` hook into that project's `.claude/settings.json` *after* it is
running, ask it to run one Bash command, observe whether the hook fires.

**Recommendation.** Run it **before** the reconciled plan fixes v0's scope, and
branch the plan on the outcome: hot-reload works ⇒ hook gate is available for
observed sessions and becomes v0's deterministic stop; hot-reload does not work
⇒ v0 must either pull the owned-session spawner forward (so hooks can be passed
via `--settings` at spawn) or state in G1 that stop is honest **only** for
sessions started after hook installation, and label every other Stop button
disabled with that reason. Do not ship a plan whose headline verb is gated on an
unrun 10-minute probe.

---

## AUDIT-4 — the `tool-results` spill is settled, and its schema is NOT the one T7 was told to implement

**file:line** `touch-aggregator-plan.md:466-469` (T7: "Handle
`toolUseResult.persistedOutputPath`/`persistedOutputSize` + `<persisted-output>`
placeholder"), `:861-865` (Part E item 1, "never observed on disk");
origin `driver-context.md:152`, conflict resolution `:34-41`
**severity** major

**Scenario.** Measured today, three ways:

```
find ~/.claude -type d -name tool-results
  → ~/.claude/projects/-home-laniakea-Projects-touch/e423cd3c-…/tool-results   (mode 755)
     b0ky2licz.txt 40570 B, b3ixg5aki.txt 39900 B, bc6wwd73z.txt 32624 B
records whose toolUseResult carries persistedOutputPath/persistedOutputSize : 0
```

The spill is real and live — but the pointer is **not** a `toolUseResult` field.
It is plain text inside the `tool_result` **content block**:

```
{'tool_use_id':'toolu_01UsAW9KgPV5QAYBnqvJWZgH','type':'tool_result',
 'content':'<persisted-output>\nOutput too large (39.6KB). Full output saved to:
            /home/agent/.claude/projects/<slug>/<sessionId>/tool-results/<id>.txt …'}
```

(verbatim from `…/wf_455b348c-e17/agent-abc69d2e545b15f8c.jsonl:43`.) An
implementer who codes T7 as written checks two fields that never exist, finds
nothing, ships, and every large tool result renders as the literal string
`<persisted-output> Output too large…` with no way to fetch the content — a
silent feature-never-fires bug that only surfaces on long outputs. Note also the
observed trigger is ~39.6 KB, not the driver's "threshold ~50000", and the spill
files are mode 644 while transcripts are 600.

**Recommendation.** Delete Part E item 1 (settled). Rewrite T7: detect the
placeholder by parsing the `tool_result` content for
`^<persisted-output>` + `Full output saved to: (?P<path>\S+)`; store
`{tool_use_id, path, declared_size}`; serve it through
`/api/toolresult/<tool_use_id>` with realpath containment under
`~/.claude/projects/*/*/tool-results/` **only** (the path in the record is
absolute and attacker-adjacent — it is agent-authored text, so it must be
validated, never trusted). Add the three files above as a fixture; the exact
regex is cheap to test and impossible to guess later.

---

## AUDIT-5 — `<runId>.json` lands under the session current at run END, not the launching session; T8's path yields ENOENT

**file:line** `touch-aggregator-plan.md:510-512` ("When
`<sessionId>/workflows/<runId>.json` appears, copy to
`.touch/runs/<runId>/snapshot.json`"); AGENTGRAPH-7 predicted it at
`findings/research-agentgraph-attempt-1.md:248-273`
**severity** major

**Scenario.** Observed on disk for the run the plan itself was produced by:

```
journal   : dd469822…/subagents/workflows/wf_829e6f58-b2f/journal.jsonl   (14 lines)
snapshot  : e423cd3c…/workflows/wf_829e6f58-b2f.json                      (status completed)
```

The run was launched in session `dd469822`, the session rotated, and the
snapshot was written under `e423cd3c`. T8 resolves the run from the session that
launched it (that is where the `Workflow` `toolUseResult` was observed — T8's
own attach rule at `:488`), so `<sessionId>/workflows/<runId>.json` is
permanently ENOENT and the reconciliation pass silently never runs. AGENTGRAPH-7
recommended globbing for exactly this; the plan adopted the glob for *agent
transcripts* (`:496-498`) and not for the snapshot.

**Recommendation.** One line in T8: resolve the snapshot as
`glob(~/.claude/projects/*/*/workflows/<runId>.json)` (union, newest wins), never
relative to the attaching session; same for `tool-results` (AUDIT-4) and for the
`subagents/workflows/<runId>/` directory itself. Add a two-session fixture — the
one above is on disk today and is the only real specimen this project has.

---

## AUDIT-6 — `workflowProgress` mixes `workflow_phase` rows whose every field is null; back-filling from it null-wipes good labels

**file:line** `touch-aggregator-plan.md:511-512` ("back-fill authoritative
label/phase/index/attempt/durations/status as optional late fields");
source claim `findings/research-agentgraph-attempt-1.md:225-231`
**severity** major

**Scenario.** AGENTGRAPH-6 described `workflowProgress` as "the array of
`workflow_agent` records" — because the file did not exist yet and the shape was
read out of the binary. The real file
(`e423cd3c…/workflows/wf_829e6f58-b2f.json`) contains **9 entries for
`agentCount: 7`**, and the first two are a different record type:

```
{'type':'workflow_phase','index':1,'label':None,'phaseTitle':None,'agentId':None,'state':None,'attempt':None}
{'type':'workflow_phase','index':2,'label':None, … all None}
{'type':'workflow_agent','index':1,'label':'research:sessiondata','phaseIndex':1,'phaseTitle':'Research','agentId':'a2fc883c96ff7b837','state':'done','attempt':1}
… ×7
```

A back-fill that iterates `workflowProgress` and indexes by `index` merges the
two `workflow_phase` rows onto agents 1 and 2 and blanks their labels and states
— i.e. the reconciliation pass *degrades* the graph, and only for the first two
nodes, which reads as a random bug. Separately, `phases` (`[{title, detail,
model}, …]`) is the only persisted source of phase titles/models and is not in
the plan's back-fill list at all.

**Recommendation.** State in T8: filter `workflowProgress` on
`type == "workflow_agent"`; key by `agentId`, never by `index`; treat any null
field as absent (never overwrite an observed value with null); ingest `phases[]`
as the phase roster. Add the fixture and a test asserting the two
`workflow_phase` rows are ignored and all seven labels survive.

---

## AUDIT-7 — the plan's acceptance criterion depends on `~/.claude` data that the CLI is scheduled to delete, and that is already split across two sessions

**file:line** `touch-aggregator-plan.md:895-903` (Part F: "the touch-aggregator
research run of 2026-07-25 renders as a graph with six *distinctly labelled*
researcher nodes, correct token rollups…"); the sweep is SESSIONDATA-13
(`findings/research-sessiondata-attempt-1.md:456-478`)
**severity** major

**Scenario.** Part F's acceptance test is "render this specific historical run
correctly". That run's data lives entirely under `~/.claude/projects/`, which
SESSIONDATA-13 proves the CLI's retention sweep unlinks — transcript **and**
`rm -rf` of the whole `<sessionId>/` tree. Nothing in the plan captures it
first, and T20 (archived runs) deliberately renders history from
`events.jsonl` **only**, which cannot produce token rollups or three-state
liveness. So the acceptance criterion is unachievable the day the sweep runs,
and there is no test that will notice — it is prose, not a test.

Its current on-disk state also makes it the single most valuable fixture this
project has, and it is already exercising four hazards at once:

```
dd469822…/subagents/workflows/wf_829e6f58-b2f/  journal.jsonl + 6 agent transcripts
e423cd3c…/subagents/workflows/wf_829e6f58-b2f/  agent-a2ed16d57db0e9887.jsonl (synthesizer)
                                                agent-a2fc883c96ff7b837.jsonl  ← SAME agentId, 12 598 B,
                                                                                  a rotated continuation
e423cd3c…/workflows/wf_829e6f58-b2f.json        the completion snapshot
```

i.e. cross-session split (AGENTGRAPH-7), the same `agentId` present in two dirs
with different content (the `message.id` dedup rule is load-bearing, not
theoretical), snapshot under the wrong session (AUDIT-5), and a real 7-node
research graph.

**Recommendation.** Add an early plan item: **freeze the fixture** — copy this
run's journal, the nine agent transcripts (both copies of `a2fc883c…`), the
seven `.meta.json` stubs, the snapshot, the three `tool-results/*.txt`, and the
task's `events.jsonl` into `tests/fixtures/run-wf_829e6f58/` (sanitised if
necessary; they contain repo source, not credentials), and rewrite Part F's
criterion against the **fixture**, not against `~/.claude`. Keep the live-render
demo as a manual smoke check.

---

## AUDIT-8 — D7/T14's `git stash create` checkpoint cannot run in this repo: it fails on a zero-commit repository

**file:line** `touch-aggregator-plan.md:208` (D7 restart row: "Touch records
`git stash create` + `git status --porcelain` checkpoint first"), `:649-651`
(T14), `:657` (test: "checkpoint recorded before restart"); origin CONTROL-7
(`findings/research-control-attempt-1.md:242-263`)
**severity** major

**Scenario.** Reproduced in `/tmp/claude-1000/audit/stashprobe`:

```
git init -q . && echo hi > a.txt && git add a.txt
git stash create   →  "You do not have the initial commit yet"   exit=1
```

The Touch repo has **zero commits** (`git log` → "your current branch 'master'
does not have any commits yet", verified now), so the first stop/restart a user
issues hits a non-zero exit from the checkpoint step. Depending on how T14 is
written that either aborts the control verb (a stop that refuses to run) or is
swallowed and the UI shows a checkpoint that does not exist — the worse of the
two, because CONTROL-7's whole point is that the tree is unguarded. A second,
permanent case: on a clean tree `git stash create` prints nothing and exits 0, so
"empty output" is ambiguous between "no changes" and "failed".

**Recommendation.** Specify the checkpoint as a three-state result:
`{sha|none|unavailable, reason}` — `unavailable` when `git rev-parse HEAD`
fails (no commits), when the cwd is not a work tree, or when `stash create`
exits non-zero; render it in the UI as "no checkpoint — <reason>" and never
block the control verb on it. Always capture `git status --porcelain` (which
works with zero commits — verified: `A  a.txt`) as the fallback record. Test all
three arms against throwaway repos, including the zero-commit one.

---

## AUDIT-9 — marker parsing is specified three incompatible ways, and quoted markers in findings text make every "first occurrence" rule wrong (reproduced)

**file:line** `touch-aggregator-plan.md:499-501` (T8: parse
`^\[monitor\] plan=… stage=… role=… attempt=…`), vs
`.claude/shared/monitoring/decision_watcher.py:340-345` (unanchored
`finditer`, **last** wins), vs `.claude/skills/touch-orchestrate/SKILL.md:39-48`
(`[touch]` must be the **first** line); analysed in
`findings/research-skills-attempt-1.md:188-222` (SKILLS-6)
**severity** major

**Scenario.** Two mechanical facts, both reproduced today.

1. `grep -rl '\[touch\] name=' ~/.claude/projects --include=agent-*.jsonl` →
   **12 files**. Every one is a false positive: the matches are marker *text
   quoted inside prose* — `[touch] name=<name> parent=<parent_name> …` from
   `SKILL.md`, `` `[touch] name=` marker `` from a findings file, and even
   `[touch] name=: 12` (a previous agent's own grep output echoed into its
   transcript). Genuine first-line `[touch]` markers on disk: still **zero**, as
   V0TASK-1 measured. Any detector that searches the transcript body — or takes
   the *first* occurrence, as the skill mandates — will bind names from quoted
   text. This is exactly why the shipped watcher takes the **last** match.
2. T8's `^\[monitor\]` is a Python `^` without `re.MULTILINE`, i.e. it matches
   only at string start. The moment SKILLS-7's `[touch]` line is prepended (the
   very change the skill exists to cause), T8's parse stops matching and every
   workflow node silently falls back to `agentType` + 60 chars — with no test to
   catch it, because no test exists.

**Recommendation.** One normative "marker grammar" paragraph in the plan,
mirrored into `SKILL.md` and `monitoring.md`: markers are matched **per physical
line** (`re.MULTILINE` with `^`), only within the **first N lines** (N=3) of the
agent's **oldest** transcript's first `user` record; `[monitor]` keeps last-wins
within that window; `[touch]` must be on line 1 or the node is flagged
`marker-misplaced`; a marker found outside the window is ignored, never used.
Ship the adversarial fixture: line 1 `[touch]`, line 2 `[monitor]`, plus a body
containing three quoted markers of both kinds copied verbatim from the 12 files
above.

---

## AUDIT-10 — legacy ingest will faithfully reproduce the watcher's fabricated verdicts, and the plan forbids fixing them

**file:line** `touch-aggregator-plan.md:750-766` (T20, legacy `events.jsonl` as
a read-only source), `:768-789` (T21, "Do not touch tailing, checkpoints, or
the journal reader"), `:843-847` (Part D discards the watcher fixes); the
defects are INTENT-5 (`findings/research-intent-attempt-1.md:150-186`) and
AGENTGRAPH-3 (`findings/research-agentgraph-attempt-1.md:119-148`)
**severity** major

**Scenario.** Two watcher defects are *baked into the event history* Touch is
required to render:

- `decision_watcher.py:646` — `st = "done" if state["decisive"].get(prev) else
  "failed"`. A research plan never produces a gate verdict, so every
  `execute-research` run in this repo is written to `events.jsonl` as
  `research plan failed "loop exited -> synthesis"`. Verified in
  `touch-aggregator/events.jsonl`; the run in question is the one that produced
  the plan and it **succeeded**.
- `decision_watcher.py:610-616` — the stale-close guard requires
  `info["attempt"] > oinfo["attempt"]`, but a stall-respawn (AGENTGRAPH-3) emits
  a second `started` for the same key with the *same* `attempt=1` marker, so the
  abandoned agentId is never closed and ticks "running" forever in the stream.

T20 renders archived tasks "from `events.jsonl` only". D13 says a run must never
show "a fabricated verdict". Both cannot hold: the archive *is* fabricated
verdicts. And T21 forbids touching the code that produces them, while Part D
records the watcher fixes as discarded.

**Recommendation.** Add an explicit legacy-ingest rule to the reconciled plan
(one paragraph in D4 + a clause in T20): when ingesting `source:"legacy"`,
(a) a `plan failed` event whose detail matches the barrier-transition pattern
and whose stage agents all resulted is re-labelled **"closed — no verdict"**,
never `failed`; (b) an agent left `running` with no result and a later sibling
`started` on the same stage is rendered **superseded**, not running; (c) the
mapping is recorded on the event as `derived_from_legacy: true` so the UI can
say where the state came from. Then either fix `decision_watcher.py:646` in T21
(it is one condition) or record explicitly that legacy history stays wrong on
disk and is corrected only at read time.

---

## AUDIT-11 — two opposite directions for node identity were adopted a week apart and never reconciled

**file:line** `touch-aggregator-plan.md:503-505` (T8: "Ordinary session
subagents (no journal, no marker) are first-class nodes … visibility is not
marker-gated (PRIORART-4)") vs
`findings/research-intent-attempt-1.md:385-408` (INTENT-13: make `[touch] name=`
"**mandatory rather than advisory**"; "Touch's graph must key nodes on `name`
… never on `role#attempt`") and `touch-monitor-spawn-plan.md:130-136` (P6 builds
the whole tree from the name marker)
**severity** major

**Scenario.** PRIORART-4's finding was that the prior art is *marker-gated* and
therefore blind to half the agents; the aggregator plan fixed that by making
harness facts primary and the marker a decorator. The v0 plan and INTENT-13 then
went the other way: names come from the marker, hierarchy comes from `parent=`,
and P6's tree has no harness-derived arm at all — so an agent spawned without
the marker (i.e. **every agent on this machine today**, AUDIT-9) has no node in
the v0 UI. Two implementers reading the two plans build two different graphs,
and the legacy `events.jsonl` cards (grouped by `plan`/`stage`) join to neither
(SKILLS-8).

**Recommendation.** Rule once, in a global decision: **harness facts create
nodes; markers name and group them.** Node identity = `(runId, key, ordinal)`
for workflow agents and `agentId` for Agent-tool agents (both harness-derived,
both always present); `name`/`plan`/`stage`/`attempt` are *labels* carried in a
separate, dashed layer, with a documented join `(plan, stage, attempt) →
node` for legacy cards. A missing marker degrades the label, never the node.
State explicitly that P6's name-only tree is superseded.

---

## AUDIT-12 — the settling-experiment list omits the probes the control and delivery items actually rest on

**file:line** `touch-aggregator-plan.md:859-889` (Part E, 10 items)
**severity** major

**Scenario.** Part E is well-formed but scoped to the *data* questions. Four
verifications that gate shipped items are absent, and each one, if it fails,
invalidates a whole item rather than a field:

1. **Hook registration for an interactive PTY session via `--settings`.**
   T10 passes the hook pack with `--settings` at spawn (`:559-561`). Every
   measurement in the corpus (CONTROL-8's 20 s hold, LIVEIO-6/-7's timing) was
   taken in `-p`/print mode; LIVEIO-17 explicitly reports that hook events
   observable in SDK mode produced **nothing** when the same settings file drove
   a real interactive REPL. Whether hooks even *run* under the PTY path is
   assumed, not measured.
2. **`command` vs `http` hook types.** T10 ships a `command` hook
   (`touch-hook.sh`); T15's pause gate is an `http` hook (`POST /hook/gate`).
   Only the `http` type was ever verified to hold a response (CONTROL-8), and
   only the `command` type was verified to be cheap. The pause gate's core
   behaviour is therefore untested for the type T10 installs, and mixing both
   types in one settings file is unverified.
3. **Reproducibility of the vendoring step (T2).** STACK-8 verified `npm
   install` works *today, through this proxy*. T2 has no recorded fallback if
   the policy changes; the acceptance is "sha256 in VERSIONS.txt matches", which
   cannot be met the first time if the fetch fails.
4. **`claude agents --json` on this machine now.** The 0.61–0.71 s measurement
   (STACK-12) came from a session that no longer exists; D6 makes it the
   reconciliation path with a 10 s TTL.

**Recommendation.** Add all four to Part E with their cheapest form (1: spawn
`claude` under a PTY with a `--settings` hook that touches a file, one prompt,
check the file; 2: one settings file with both hook types, assert both fire and
that the http one holds; 3: run the vendor step now and commit the artefacts —
it is the fixture, not an experiment; 4: one `time claude agents --json`). Mark
Part E item 1 **settled** (AUDIT-4) and item 9 (`~/.claude/todos/`) **settled**
— re-checked today, still only `lost+found`.

---

## AUDIT-13 — `inception.md`'s token figure is the exact value of the one field D8 bans, and the docs still carry it

**file:line** `inception.md:235-236` ("7 agents, **~1.09M tokens**"); the ban is
`touch-aggregator-plan.md:240-241` ("`toolUseResult.totalTokens` is ignored
(last-call-only — AGENTGRAPH-8)"); INTENT-14 flagged the number at
`findings/research-intent-attempt-1.md:412-431`
**severity** major

**Scenario.** Measured today:
`e423cd3c…/workflows/wf_829e6f58-b2f.json → totalTokens: 1089990`. That is
`~1.09M` to three digits — so `inception.md` did not merely quote a wrong
number, it quoted **the field the plan forbids reading**, at run level. The true
rollup for the same run (monitor, per-`message.id` dedup) is ≈29.5M in /
316k out — a 27× under-report, consistent with AGENTGRAPH-8's 14× per-agent
measurement. This is the same failure mode INTENT-14 warns about, committed in
the summary document a new session is most likely to read.

It also exposes a real gap: D3/T7 specify token dedup per **agent**; nothing
specifies the **run-level** rollup, and `<runId>.json.totalTokens` is sitting
right there in the snapshot T8 is told to back-fill from (AUDIT-6) — the next
implementer will use it for exactly the same reason inception did.

**Recommendation.** (a) Correct `inception.md:235` to the deduped rollup and
name its source; (b) add to D8/T8 an explicit rule: **run-level tokens = Σ over
the run's nodes of the per-node deduped total; `<runId>.json.totalTokens` and
`totalToolCalls` are display-only "harness reported" values, rendered beside the
computed one and never substituted for it**; (c) T8's test already asserts
"`totalTokens` never read" — extend it to the snapshot back-fill path, which is
where it will actually be read.

---

## AUDIT-14 — the `.gitignore`/first-commit window INTENT-6 opened is still open

**file:line** `/home/laniakea/Projects/touch/.gitignore:1-37` (no `.touch/`
entry — verified now); scheduled only inside
`touch-aggregator-plan.md:339-347` (T1) and `touch-monitor-spawn-plan.md:69-76`
(P1); the risk is INTENT-6 (`findings/research-intent-attempt-1.md:189-213`)
**severity** major

**Scenario.** Verified today: `.gitignore` still has no `.touch/` line, the repo
still has **zero commits**, and `.touch/` does not exist yet. Both plans put the
ignore edit *inside* the same item that creates `aggregator/`, `touch-visual/`
and the first modules — i.e. an item whose implementer will be running code and
tests in the same working tree. `.touch/` holds hook spools, the control audit,
per-session stores and (later) the PTY spool, all derived from transcripts that
`inception.md:117` records as unredacted. One `git add -A` before the ignore
lands commits them irreversibly into the repo's first commit.

**Recommendation.** Make it the literal first action of the reconciled plan,
before any directory is created: a one-line item "append `.touch/` (and
`.touch*/`, for `TOUCH_STATE_DIR` variants) to `.gitignore`", strictly additive,
with the existing guard (`test_shell.py:155-161` checks only substring presence
— verified safe). Add a plan-level gate sentence: *no `git add`/commit in this
repo until that item is green.*

---

## AUDIT-15 — daemon lifecycle: the monitor the docs point at is dead, two watchers keep running, and nothing owns shutdown

**file:line** `CLAUDE.md:116-125` ("Rules that bite" — never-delete and safe
`pkill`, nothing about stopping a daemon); INTENT-15
(`findings/research-intent-attempt-1.md:434-454`)
**severity** minor

**Scenario.** Measured now, ~11 h after the run they belong to:

```
ps: python3 .claude/shared/monitoring/decision_watcher.py   pid 4929  11:01:52 elapsed
    python3 .claude/shared/monitoring/decision_watcher.py   pid 16627 (this run)
monitor_server.py (pid 4614): present in one ps sample, absent two minutes later
127.0.0.1:8931  → connection refused (python socket + curl)
```

So: the dashboard `CLAUDE.md:112` tells every reader to open is **down**, while
a watcher for a run that finished at 03:26 has been tailing a dead journal for
eleven hours. `INTENT-1`'s verification of the same endpoints (`/health` → ok,
`/tasks` → 3 tasks) was true hours ago and is false now — a reminder that "the
process is in `ps`" is not "the service is up", which is the same inference
error D6 forbids for *sessions* and which Touch's own health surface must avoid
for its own daemons.

**Recommendation.** (a) CLAUDE.md gains a line: when a run ends, stop its
watcher (`pkill -f "[d]ecision_watcher"` scoped by `ORCH_STATE_DIR`), never
delete its state. (b) The reconciled plan states that any Touch tailer whose
target is gone (`/proc/<pid>` absent or `procStart` mismatch) exits rather than
polls forever, and that `/health` reports per-tailer liveness — so the product
does not reproduce the failure its own repo is demonstrating. (c) T21's
precondition ("must not run while a live orchestration is mid-run") needs an
observable test, since stale watchers make "is anything running?" ambiguous.

---

## AUDIT-16 — verification ledger: what I settled or re-confirmed today, so nobody re-runs it

**file:line** `touch-aggregator-plan.md:859-889` (Part E)
**severity** minor

Re-measured against the live substrate (CLI 2.1.220, session `292fc08c…`):

| claim | source | status today |
|---|---|---|
| `tool-results` spill never observed | Part E-1 | **SETTLED — observed**, different schema (AUDIT-4) |
| `~/.claude/todos/` empty | Part E-9, AGENTGRAPH-15 | re-confirmed: only `lost+found` |
| `progress` records: zero on disk | SESSIONDATA-16 | re-confirmed: **0** across all projects |
| journal `result` is a repr string | SESSIONDATA-10 | **FALSIFIED** — all dicts (AUDIT-2) |
| `<runId>.json` written once at run end | AGENTGRAPH-6 | confirmed: 2 snapshots exist, `status: completed` |
| `[touch]` marker instances on disk | V0TASK-1 | still **0 genuine**; 12 quoted false positives (AUDIT-9) |
| `procStart` = `/proc/<pid>/stat` field 22 | SESSIONDATA-7 | unchanged (registry now lists 1 session) |
| `~/.claude/sessions/` holds a non-JSON entry | INTENT-11 | re-confirmed: `lost+found` present |
| session registry vs transcripts | INTENT-10 | 1 registry entry vs **6** transcripts in this project |
| `isSidechain` true rows in the parent transcript | LIVEIO-13 | confirmed 0 in parents; 2346 rows across 25 agent files |
| `git stash create` on this repo | CONTROL-7 / D7 | **fails** (AUDIT-8) |
| monitor on 8931 healthy | INTENT-1 | **no longer true** (AUDIT-15) |

**Recommendation.** Paste this ledger into the reconciled plan's unverified
section, replacing the settled rows, and adopt the convention INTENT-14 asked
for: every quoted metric carries the command or endpoint that produced it and
the date. Half the drift in this corpus is measurements without provenance
outliving their substrate.

---

## AUDIT-17 — D3's session key has no arm for historical sessions, which is most of them

**file:line** `touch-aggregator-plan.md:99-112` (D3 identity table: session =
`(pid, procStart)`); INTENT-10
(`findings/research-intent-attempt-1.md:312-335`)
**severity** minor

**Scenario.** Verified now: `~/.claude/sessions/` contains exactly **one**
`*.json` (plus `lost+found`), while
`~/.claude/projects/-home-laniakea-Projects-touch/` holds **six** transcripts
(`292fc08c`, `ad7b421c`, `c2f92a2c`, `dd469822`, `e144bb01`, `e423cd3c`). Ended
sessions are reaped from the registry, so five of six sessions have **no**
`(pid, procStart)` and cannot be keyed at all under D3 — including
`dd469822`, the session that produced the plan and that Part F's acceptance
requires rendering. T6's sidebar is built entirely on the registry scan, so the
"list of terminal sessions" the README asks for shows one row.

**Recommendation.** Extend D3 with a second arm and say which is which:
live sessions keyed `(pid, procStart)`; historical sessions keyed `sessionId`
with `liveness: historical`, discovered by scanning `projects/*/*.jsonl`;
reconciliation when a registry entry names a `sessionId`. State that a
historical row may be a *fragment* (a `/clear` splits one logical run across
sibling ids) and that it carries no controls. Add `lost+found` and a
zero-byte registry file to T6's fixture — both are on this machine.

---

## What the synthesizer must decide (not merely record)

1. **How the 51 orphaned repo-recon findings enter the plan** (AUDIT-1) — with a
   register and a test, or they will be lost a third time.
2. **`result` parsing** (AUDIT-2) — the normative rule contradicts the code the
   plan tells implementers to copy.
3. **Whether v0 has any honest stop at all** (AUDIT-3) — gated on a 10-minute
   probe that has never been run.
4. **Node identity: harness-first or marker-first** (AUDIT-11) — the two plans
   answer differently and both are cited as binding.
5. **Fixture freeze before the retention sweep** (AUDIT-7) — the project's only
   real specimen of a completed multi-session run is on a deletion clock.
</content>
</invoke>
