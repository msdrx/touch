# research-v0task — attempt 1

Perspective: **the touch-monitor-spawn v0 plan, implementability review.**
Every cross-reference the plan makes was checked against the actual file and,
where cheap, against the live substrate (read-only inspection of `~/.claude`
and `/proc`; no writes outside the two mandated `status.sh` calls).

Plan under review:
`/home/laniakea/Projects/touch/.claude/local-orchestrators/touch-monitor-spawn/plan/touch-monitor-spawn-plan.md`
(230 lines; G1–G9 at :16-65, P1–P12 at :69-220).

## Verification ledger (what checked out, so the synthesizer doesn't re-do it)

| Claim | Verdict |
|---|---|
| `decision_watcher.py:86-100` = session-rotation glob union | **true** — `agent_paths()`, globs `<projects>/*/*/subagents/workflows/<WF_NAME>/agent-*.jsonl` |
| `decision_watcher.py:154-197` = message-id token dedup, `input = input + cache_read + cache_write` | **true** (:183, :193) — but see V0TASK-10 for the "monotonic clamps" half |
| `monitor_server.py:199-212` = extension whitelist + realpath containment | **true** — `safe_artifact_path()` |
| `monitor.html:299-321` = the escape-first pattern | **partly false** — see V0TASK-11 |
| P8's "the existing server discards client frames" | **true** — `monitor_server.py:279-310` `parse_client_frames()` skips masked payloads unread |
| P3's liveness rule (`/proc/<pid>/stat` field 22 == `procStart`) | **true, measured** — `~/.claude/sessions/622.json` `procStart:"10028"`; `awk '{print $22}' /proc/622/stat` → `10028` |
| P4's `.compact.tmp.*` backoff sentinel | **true** — `performCompactTranscript` writes `${file}.compact.tmp.${hex}` (SESSIONDATA finding :124) |
| P11's `SubagentStart`/`SubagentStop` hook events | **true** — both in the CLI hook list (LIVEIO finding :222, :377 with measured 70 ms spawn→hook latency) |
| G9's "four monitoring tests run from run_all.sh" | **true, measured** — all four pass invoked by path from repo root; they are cwd-independent |
| P1's `.gitignore` edit is safe | **true** — `test_shell.py:154-161` asserts only the two `.claude/shared/monitoring/*` lines; `__pycache__/` is already at `.gitignore:12`, so P1 only needs to add `.touch/` |
| Skill↔plan agreement on marker field list and ledger path | **true** — `SKILL.md:42` vs plan :135; `SKILL.md:52` vs plan :134 |

Everything below is what did **not** check out.

---

## V0TASK-1 — The stop path is inoperable for the exact loops Touch exists to control

**File:** `.claude/local-orchestrators/touch-monitor-spawn/plan/touch-monitor-spawn-plan.md:144` (P7), `:186` (P10 stop button), `:45` (G6)
**Also:** `.claude/skills/touch-orchestrate/SKILL.md:50-56`, `:78-82`
**Severity: blocker**

**Scenario.** `touch-orchestrate` builds the whole stop mechanism on
*background Agent-tool spawns*: "Spawn via the Agent tool with
`run_in_background` so each agent is a harness-tracked task that `TaskStop`
can kill individually" (`SKILL.md:50-51`), recorded with a `taskId` in the
spawn ledger (`:52-56`), resolved name→`taskId`→`TaskStop` in the control loop
(`:78-80`). But the loops Touch is defined against — `README.md`/`CLAUDE.md`
say the loops are "exactly the ones defined by the `execute-research` and
`implement-plan` skills" — do not spawn that way. Both templates spawn through
Workflow `agent()`:

- `.claude/skills/execute-research/templates/research.workflow.js:140`, `:150`
- `.claude/skills/implement-plan/templates/implement.workflow.js:172`, `:179`, `:189`, `:256`, `:333`, `:343`

`grep -c run_in_background` over both templates: **0**. Workflow agents get no
`taskId`; the only harness task is the *whole workflow run* (prior art:
`research-priorart-attempt-1.md:334` shows one `TaskStop({taskId})` per run,
not per agent). So for a real `implement-plan` run the UI would render 12 agent
nodes each with a **Stop** button that resolves to nothing in the ledger — the
skill's own honesty rule ("never fabricate a result") forces `not_found`, and
the user sees a dead button on every node.

The marker side is empirically worse. Across every agent transcript in
`~/.claude/projects/-home-laniakea-Projects-touch/`:

```
agent-*.jsonl whose prompt head contains "[touch] name=" : 0
agent-*.jsonl whose prompt head contains "[monitor] plan=": 12
```

P6's primary name channel has **zero** real instances today; the channel that
does exist everywhere is the one P6 never mentions.

**Recommendation.** The synthesizer must decide and write down, explicitly:
(a) v0 ingests **both** markers — `[touch]` preferred, `[monitor] plan/stage/role/attempt`
as a first-class fallback that yields a synthetic name
(`<root>_<stage>` / `<plan>_<stage>`), because that is the only marker the
shipped orchestration skills emit; (b) stop granularity is **per workflow run**
for workflow-spawned agents (intent carries `runId`, the orchestrator
`TaskStop`s the run) and **per agent** only for background Agent-tool spawns,
with the UI labelling which it is (G8 honesty); and (c) either
`execute-research`/`implement-plan` templates gain `[touch]` markers + a ledger
(a new plan item, since the templates are normative protocol), or the plan
states in G1 that per-agent stop does not cover them in v0. Today the plan
implies per-agent stop works everywhere, and it does not.

---

## V0TASK-2 — P6's discovery glob misses every workflow-spawned agent (verified on disk)

**File:** plan `:128` ("Watch `<sid>/subagents/` for `agent-*.meta.json`")
**Severity: blocker**

**Scenario.** The real layout under
`~/.claude/projects/-home-laniakea-Projects-touch/<sid>/subagents/` is two
distinct tiers:

```
<sid>/subagents/agent-a483cae616edffe81.meta.json          ← Agent-tool spawn
   {"agentType":"general-purpose","description":"Assess data-layer feasibility",
    "toolUseId":"toolu_011Ug5qnU1bc2nEdXq57eRg7","spawnDepth":1,"model":"opus"}
<sid>/subagents/workflows/wf_455b348c-e17/agent-a8101b66e4aa9ee36.meta.json   ← Workflow spawn
   {"agentType":"workflow-subagent","spawnDepth":1,"model":"opus"}
```

In the live session `e423cd3c-…`, `subagents/` contains **only** the
`workflows/` directory — a non-recursive watch of `<sid>/subagents/` finds
zero agents for the run that is executing right now. Two consequences the plan
does not handle:

1. The workflow `.meta.json` is a 63-byte stub with **no `description` and no
   `toolUseId`**, so P6's "name from `description`" and the toolUseId join both
   return nothing for the majority tier; and G8's "harness joins (toolUseId) as
   fact (solid)" has no input, meaning a real run renders entirely dashed.
2. The genuine harness fact for that tier is **directory containment** —
   `subagents/workflows/<runId>/` *is* the parent edge
   (`research-agentgraph-attempt-1.md:369-373`: "The directory IS the edge").
   P6 never mentions runId or the workflow tier at all.

**Recommendation.** Rewrite P6's discovery as two explicit globs —
`<sid>/subagents/agent-*.meta.json` (Agent tier: name from `description`, edge
from `toolUseId`, solid) and `<sid>/subagents/workflows/*/agent-*.meta.json`
(Workflow tier: name from marker only, edge from the containing `runId`
directory, solid-by-containment) — plus `meta.parentAgentId` for depth ≥ 2
(agentgraph :375). Add `runId` to the v2 `ref` union usage in P6 (D3 already
defines `{runId,key,ordinal}`; the v0 plan's G4 drops that arm of the union at
`:37` — restore it or state why the workflow tier needs no node key).

---

## V0TASK-3 — P10 renders controls on sessions D1 forbids controlling

**File:** plan `:186` (P10 Stop button), `:20` (G1 defers the owned-session spawner)
**Contradicts:** `touch-aggregator-plan.md:73-77` (D1), `:86` (D2)
**Severity: major**

**Scenario.** D1 is unambiguous: an **observed** session (one discovered in
`~/.claude/sessions/*.json`) gets "Read-only semantic transcript view; **no**
terminal pane, **no** control affordances in v1 (not even kill)", and "an
affordance that cannot be honest for a class is not rendered for it". G1
explicitly defers the owned-session spawner. Therefore *every* session in v0 is
observed — and yet P10 puts a Stop button on every running agent in it. The v0
plan claims at `:7-13` that it does not contradict D1–D14; this is a direct
contradiction, and it is the plan's headline feature.

The resolution is real but unstated: control.jsonl stop is **cooperative**, not
injected — it works because the orchestrating session voluntarily polls a file
Touch owns, which is a different trust story from typing into a PTY Touch
holds. That is a legitimate third session class, and D1's rationale ("the
registry file may describe a pid Touch shouldn't touch") does not apply to it.

**Recommendation.** Add an explicit amendment paragraph to the reconciled plan:
a third class **cooperating** — an observed session whose orchestrator declares
itself by writing a spawn ledger / polling `control.jsonl`. Controls render for
`owned` and `cooperating`, never for plain `observed`. State the observable
test for the class (a ledger or an ack line seen for that session within the
last N minutes) so the UI can classify without guessing, and make G8's honesty
labels cover all three.

---

## V0TASK-4 — G6/P7's state machine has no terminal state for two of the skill's three ack results

**File:** plan `:45-51` (G6), `:144-150` (P7), `:59` (G8 four-state chip)
**Skill:** `SKILL.md:80-82`
**Contradicts:** `touch-aggregator-plan.md:196-198` (D7's five states)
**Severity: major**

**Scenario.** The skill's ack line is
`{"ack":"stop","name":"…","taskId":"…","result":"stopped|not_found|already_done","ts":"…"}`.
P7 reduces *any* ack to `sent`, then waits to observe the target agent
"finished/aborted after ack" to reach `confirmed`; the only other exit is
`expired`, defined as "120 s **without ack**". So:

- `result:"not_found"` — the orchestrator has no such name in its ledger. An
  ack exists, so expiry can never fire; no agent will ever be observed
  finishing, so `confirmed` can never fire. The intent is **stuck at `sent`
  forever**, which is precisely the "silently dropped" outcome G6 promises to
  avoid.
- `result:"already_done"` — same trap unless the agent happens to be observed
  finished, and the honest answer ("it was already over") is not representable.

D7 (`touch-aggregator-plan.md:196-198`) has five states — `requested → sent →
confirmed | failed | expired` — and the v0 plan silently dropped `failed`
(G8 `:59` lists four). `failed` is exactly where `not_found` belongs.

**Recommendation.** Restore `failed`. Map `result` deterministically:
`stopped → sent` (then `confirmed` on observation, or `expired` after a
**second, post-ack** timeout — the plan currently has no such timer),
`already_done → confirmed` immediately (with a "was already finished" note),
`not_found → failed`. Write the mapping table into the plan and into
`SKILL.md` §4 so both sides encode the same contract, and add a P7 test case
per result value.

---

## V0TASK-5 — Control intents carry no scope and no identity

**File:** plan `:46-47` (`{action:"stop", name, ts}`), `:173` (`POST /api/control {action:"stop", name}`)
**Skill:** `SKILL.md:74-83`
**Severity: major**

**Scenario.** `.touch/control.jsonl` is a single repo-global file (D5), but the
intent is keyed on `name` alone. Three concrete failures:

1. **Two orchestrations, one file.** Two sessions in the same repo both run
   `touch-orchestrate` and both poll `.touch/control.jsonl` (`SKILL.md:74`).
   A stop for `auth_refactor_research1` is read by both; the unrelated one
   appends `not_found` and, per V0TASK-4, poisons the intent. Worse, if two
   orchestrations chose the same `ROOT_NAME` (the skill derives it "from the
   user's words or the task name" — collisions are likely, not exotic), the
   wrong agent is killed.
2. **Attempts.** G3 `:31` and `SKILL.md:30-32` are explicit that a name is a
   logical slot and `(name, attempt)` is the physical spawn. A stop intent has
   no `attempt`, so a stop requested against attempt 1 can land on the freshly
   spawned attempt 2 (which `SKILL.md:83` explicitly allows to exist).
3. **Duplicate acks.** P7's test says "duplicate acks harmless", but with no
   intent id there is no way to tell a duplicate ack from an ack for the *next*
   stop of the same name.

**Recommendation.** Make the intent line
`{"id":"<uuid4>","action":"stop","root":"<ROOT_NAME>","name":"…","attempt":N,"pid":…,"procStart":"…","ts":"…"}`
and require the ack to echo `id` verbatim. Add to `SKILL.md` §4: *ignore any
intent whose `root` is not this orchestration's `ROOT_NAME`*. P7 reduces by
`id`, never by `name`. This is cheap now and unfixable later without a format
break.

---

## V0TASK-6 — `TOUCH_STATE_DIR` silently splits the aggregator and the orchestrator

**File:** plan `:84` (`TOUCH_STATE_DIR` override), `:46` (aggregator writes `.touch/control.jsonl`), `:198-199` (hook writes `.touch/hooks/<session_id>.jsonl`)
**Skill:** `SKILL.md:74-75`
**Severity: major**

**Scenario.** D5 and P2 give the store a `TOUCH_STATE_DIR` override. The skill
(`SKILL.md:74`) hard-codes `.touch/control.jsonl` with a fallback to
`<task-dir>/control.jsonl` "if no `.touch/` exists" — it has no notion of the
override. Set `TOUCH_STATE_DIR=/var/touch` and: the UI writes
`/var/touch/control.jsonl`; the orchestrating session polls `<repo>/.touch/`,
finds nothing (or, if a stale `.touch/` exists, polls a dead file); every stop
silently never fires and only `expired` ever shows. The same split hits P11's
hook script, which resolves `.touch/` from the hook's cwd.

Note also the fallback in `SKILL.md:74-75` is dead in practice: the aggregator
always creates `.touch/`, so `<task-dir>/control.jsonl` is never reached — but
it *is* reached in the window before the server first boots, which is exactly
when an orchestration is starting.

**Recommendation.** One of: (a) v0 forbids the override (drop it from P2, keep
D5's mention as future work); or (b) the resolved absolute control-file path is
published in a fixed, non-overridable location (`<repo>/.touch/server.json`
always, containing `{"stateDir": "...", "controlFile": "..."}`), and
`SKILL.md` §4 is rewritten to read that file first and fall back to
`$TOUCH_STATE_DIR` then `<repo>/.touch/`. Whichever is chosen, the hook script
must use the identical resolution, and P12's e2e sim must exercise it with the
override set — otherwise the bug ships green.

---

## V0TASK-7 — Hook-spool ingestion has no owning source file

**File:** plan `:194-206` (P11), `:153-164` (P8)
**Severity: major**

**Scenario.** P11's file list is `aggregator/hooks/touch-hook.sh`,
`aggregator/hooks/README.md`, `tests/test_hooks.py`, and its body says
"Aggregator ingests the spool as `source:"hook"` (**P8 wires it**)". But P8's
file list is `aggregator/server.py` + its test, and P8's own description of the
loop is "sessions → tailer → ingest → agents → store" — no hook stage. P8 is
implemented **before** P11 by a fresh implementer (implement-plan spawns a
brand-new agent per sub-plan, with no shared memory and no sight of later
items), so nobody writes the spool reader: P8's implementer isn't told to, and
P11's implementer cannot touch `server.py` because P8 owns it (one file, one
owner). P11's own test ("spool ingestion produces agent events") is therefore
unpassable by P11's implementer.

**Recommendation.** Give P11 its own module — `aggregator/hooks/spool.py` with
a stated signature (e.g. `poll_spools(state_dir, checkpoints) -> list[Event]`)
— and move the exact call site into P8's text as a one-line requirement
("the poll loop calls `hooks.spool.poll_spools(...)` after `agents`"), so P8's
implementer writes one line against a signature fixed by the plan rather than
by a module that does not exist yet. This is the general shape of V0TASK-8; it
is called out separately because P11 currently ships a test it cannot pass.

---

## V0TASK-8 — No module interface contract across P2–P9, and no owner for the in-memory model

**File:** plan `:78-176` (P2–P9), `:26-28` (G2), `:36-38` (G4)
**Severity: major**

**Scenario.** The plan partitions cleanly by file (verified: no two of P1–P12
name the same path — that part is good), but implement-plan's isolation is
exactly what makes the missing contract fatal. Six modules (`store`,
`sessions`, `tailer`, `ingest`, `agents`, `control`) are each written by a
different fresh agent, and then P8 must wire all six and P9 must project them
into JSON. The plan specifies *behaviour* for each but not one function
signature, not one data type. Concretely:

- G2 `:26-28` and G4 `:37-38` say state is a **reduction over the log** held
  "in memory" — but no item owns a `model.py`. Is the model a dict passed
  around? An object owned by `store`? Six implementers will answer six ways.
- P5 "Upsert by `uuid` into the in-memory model" and P6 "Per-agent rollups"
  mutate the same structure from two separately-authored modules.
- P4 is "a pure library — no policy" whose consumer (P5? P8?) is unnamed.
- P3 "Emit v2 `kind:"session"` events" — emit to what? P2's writer, presumably,
  but P3 is implemented before... P2 (fine) — while P8, which owns the loop, is
  implemented after both and must guess both APIs.

The critique gate will catch this only as "P8 doesn't compile", after three
attempts have burned.

**Recommendation.** Add an **Interfaces** section to the reconciled plan fixing
the exact signatures before division, e.g. `Store.append(rec: dict) -> int`,
`Store.replay() -> Iterator[dict]`, `Tailer.poll() -> tuple[list[str], bool]`
(bool = reset), `ingest.apply(model, lines, source) -> list[dict]`,
`agents.scan(model, session) -> list[dict]`, `control.reduce(model, lines) -> list[dict]`;
and give the model its own file (`aggregator/model.py`, owned by P2) with the
dataclasses named. Every downstream item then codes against text, not against
a sibling's imagination.

---

## V0TASK-9 — The auth token has no specified transport, across a sub-plan boundary

**File:** plan `:41-43` (G5 token on every route), `:166-176` (P9 endpoints), `:178-192` (P10 frontend)
**Severity: major**

**Scenario.** G5 mandates a per-boot 256-bit token on every route except
`/health`, checked with `hmac.compare_digest`. P9 lists five endpoints and a
POST without saying where the token travels. P10 — a different implementer, a
different sub-plan — describes the whole frontend and **never mentions the
token at all**: not on `fetch`, not on the WS client, not in the page load. The
browser cannot set headers on a `WebSocket` constructor, so the token must be
in the URL query (or a cookie set at page load). P9 and P10 will pick
differently; P10's static-guard tests can't catch it (they assert on source
text) and P8's socket tests only prove the server rejects the tokenless case —
so the integration failure surfaces as a 401 page in manual use.

**Recommendation.** Decide in the plan: token as `?t=<token>` on the initial
page load, server sets an httpOnly, SameSite=Strict cookie scoped to the
port, subsequent `/api/*` and `/ws` authenticate on the cookie (this also keeps
the token out of `document.referrer` and out of the WS URL that ends up in
logs). Write the exact rule in G5, restate it in P9's endpoint list and in
P10's WS-client bullet, and add a P10 static guard ("no token literal in
app.js") plus a P8 test for the cookie path.

---

## V0TASK-10 — P5 cites the wrong lines for "monotonic clamps"; following the citation yields no clamping

**File:** plan `:118-119`
**Cited:** `.claude/shared/monitoring/decision_watcher.py:154-197`
**Severity: minor**

**Scenario.** P5 says token accounting follows "the semantics of
`decision_watcher.py:154-197`", naming three behaviours: per-`message.id`
dedup, `input = input + cache_read + cache_write`, and **monotonic clamps**.
The first two are genuinely there (`:183` key selection, `:193` the sum). The
clamps are **not** — `agent_tokens()` is a pure union-and-sum with no
comparison to a prior value. The clamps live elsewhere:

```
decision_watcher.py:541   # Monotonic counters (D7): clamp deltas >= 0; never lower the baseline.
decision_watcher.py:551-553   new_base = {"in": max(prev.get("in",0), tin), ...}
decision_watcher.py:708-722, :770-780   (the same clamp at the two other emit sites)
```

An implementer who reads 154-197 (as instructed) implements no clamp; then P5's
own "re-ingest idempotence" path — a transcript compaction triggers G7's full
re-ingest — replays a *shorter* prefix and the UI's token counter visibly runs
backwards, which G8's honesty rules would then have to explain away.

**Recommendation.** Change the citation to
`decision_watcher.py:154-197` (dedup + input sum) **and** `:541-553` (the
clamp), and state the invariant in words: emitted per-agent totals are
`max(previous, computed)` per field, deltas clamped at ≥ 0. Add the explicit
test: ingest N lines, truncate to N/2, re-ingest, assert totals did not
decrease.

---

## V0TASK-11 — P10's "escape-first" citation points at a no-escaping-at-all pattern

**File:** plan `:188-189`, `:192` (test: "escape function present and used")
**Cited:** `.claude/shared/monitoring/monitor.html:299-321`
**Severity: minor**

**Scenario.** `monitor.html:299-321` is `NODE_STATES` + `renderFlow()`. Its
discipline is the opposite of escaping — it *refuses* string interpolation and
builds DOM with `createElement` + `textContent` + a whitelisted `className`
(the comment at `:295-298` says so: "never interpolate n.state into an
innerHTML string"). There is no escape function in that range; the `esc()`
helper appears much later, in the markdown preview path (`:398+`, guarded by
`test_frontend.py:111-112`). So P10 cites the DOM-construction pattern while
its test demands the escaping pattern, and the two prescribe different code.
An implementer satisfying the literal test ("escape function present and used")
can legitimately write `innerHTML = \`<span class="${esc(state)}">\`` — which
`test_frontend.py:37-45` exists specifically to forbid in the prior art.

**Recommendation.** Split the requirement: **(a)** all node/label/state
rendering uses `createElement` + `textContent` + a whitelisted class list
(guard: `innerHTML` absent from the tree-rendering function, `NODE_STATES`-style
whitelist present) — pattern of `monitor.html:295-321`; **(b)** any
rich-text/markdown preview escapes first, inline transforms run on already-escaped
text — pattern of `monitor.html:398+` and `test_frontend.py:107-112`. State both
citations, and mirror the two guards separately in
`tests/test_touch_frontend.py`.

---

## V0TASK-12 — `seq` is per-session in P2, global in G4, and ambiguous in P9's API

**File:** plan `:82-83` (P2 "single-writer monotonic `seq` per session"), `:37` (G4 "Single-writer `seq` in the aggregator process"), `:171-172` (P9 `/api/events?after=<seq>` vs `/ws?session=<key>`)
**Severity: minor**

**Scenario.** P2 scopes `seq` to a session's own `events.jsonl`; G4 (and D4 at
`touch-aggregator-plan.md:130-132`) reads as one aggregator-wide counter. P9
then exposes `/api/events?after=<seq>` with **no session parameter** — which is
only meaningful under a global counter — alongside `/ws?session=<key>` replay
"from seq 0 or `?after=`" — which is only meaningful under a per-session one.
A client that pages `/api/events?after=` and then opens a WS with the last seq
it saw will silently skip or duplicate events across sessions.

**Recommendation.** Choose per-session (it falls out of the per-session file
layout and survives adding a session without rewriting anything): make
`session` a **required** parameter on `/api/events`, document that `seq` is only
comparable within one session, and add a P9 test that `/api/events` without
`session` is a 400 (not a 404 fallback, per D9's routing rule).

---

## V0TASK-13 — P1 omits `aggregator/__init__.py` and any import convention

**File:** plan `:69-76` (P1 files: `tests/run_all.sh`, `.gitignore` only)
**Severity: minor**

**Scenario.** The big plan's T1 (`touch-aggregator-plan.md:341`) lists
`aggregator/__init__.py`, `aggregator/util.py`, `touch-visual/.gitkeep`; the v0
P1 lists neither the package marker nor any `sys.path` convention. Tests live in
`tests/` and modules in `aggregator/`, so `import aggregator.store` from
`tests/test_store.py` fails unless every test file inserts the repo root on
`sys.path`. P2's implementer will invent one convention, P3's another; P12's
e2e imports six modules and inherits whichever mess exists. (Note the empty
`aggregator/` and `touch-visual/` directories P1 "creates" also cannot be
committed to git without a placeholder file.)

**Recommendation.** Add to P1: `aggregator/__init__.py`, `touch-visual/.gitkeep`,
`tests/_path.py` (a two-line `sys.path` bootstrap), and one sentence — "every
`tests/test_*.py` starts with `import _path`" — so the convention is fixed by
the plan rather than by whoever goes first. Also note P1's `run_all.sh` glob:
at P1 time `tests/test_*.py` matches nothing, and an unguarded
`for f in tests/test_*.py` expands to the literal pattern; use `find`/`nullglob`
so P1's own acceptance test ("green on a fresh checkout") is honest.

---

## V0TASK-14 — `.touch/server.json` has no defined shape and a circular writer

**File:** plan `:40` (G5 reads port from it), `:81` (P2 "Owns: `.touch/server.json`"), `:156-157` (P8 mints the token)
**Severity: minor**

**Scenario.** G5 resolves the port `argv > $TOUCH_PORT > .touch/server.json > 8932`
— so the file is read at startup — while D5 (`touch-aggregator-plan.md:148`)
says it holds "port, token fingerprint, pid", all of which are only known
*after* bind. P2 declares ownership of the file but specifies no schema; P8 is
the only component that has a token and a pid. Two implementers, one file, no
schema, and a read-then-write ordering nobody wrote down.

**Recommendation.** Specify in P2: `{"v":1,"port":8932,"pid":1234,"tokenFp":"<sha256[:16]>","stateDir":"<abs>","controlFile":"<abs>","boundAt":"<iso>"}`,
written by `store.write_server_json()` (P2's function, called by P8 immediately
after bind, atomic tmp+`os.replace` per `decision_watcher.py:514-519`), and read
by the port resolver with "missing or unparseable ⇒ default, never crash".
Never store the raw token — the fingerprint is enough to tell a stale page from
a fresh boot. This also gives V0TASK-6 its publication point.

---

## V0TASK-15 — The description→name parse rule is unspecified and the delimiter is an em dash

**File:** plan `:130-133` (P6 "name from `description`")
**Skill:** `SKILL.md:45-47` (`description: "<name> — <short task>"`)
**Severity: minor**

**Scenario.** The skill's delimiter is U+2014 surrounded by spaces. P6 says only
"name from `description`". Real descriptions on disk look like
`"Assess data-layer feasibility"` (no name, no delimiter — verified in
`agent-a483cae616edffe81.meta.json`), and a cooperating orchestrator may write
`"auth_refactor_research1 - short task"` (hyphen), or a name containing a
hyphen, or no delimiter at all. Without a stated rule the parser either takes
the whole description as a name (polluting the tree with prose) or drops valid
names.

**Recommendation.** State the rule in P6: split on the first occurrence of
`" — "` (U+2014) **or** `" - "` (ASCII); accept the left side only if it matches
`^[a-z][a-z0-9_]*$` (the skill's own slug rule, `SKILL.md:18-19`); otherwise
treat the agent as unnamed. **The marker always wins over the description** when
both are present and disagree (the marker is first-line, script-authored;
the description is free text) — and a disagreement is itself worth surfacing in
the UI per G8. Add both cases to P6's test list.

---

## V0TASK-16 — G3's `fileHint` and G2's document shapes are decisions no item implements

**File:** plan `:32-34` (G3 `{recordUuid, toolUseId}` + `fileHint{path,line,ino,size}`), `:26-28` (G2 document shapes)
**Severity: minor**

**Scenario.** G3 mandates that spawn location is stored as `{recordUuid,
toolUseId}` plus a perishable `fileHint` "validated before use". Searching
P1–P12 for `fileHint`, `recordUuid`, or any validation of a stored line/ino:
**zero occurrences**. Same for G2's named document shapes — they are described
as "the in-memory model" but no item creates it (see V0TASK-8). A global
decision with no implementing item is either dead weight that later readers
will mistake for shipped behaviour, or a silently dropped requirement.

**Recommendation.** For each: assign or delete. `fileHint` belongs in P5 (it is
produced when the `tool_use` record is ingested) with a stated validation rule
("re-read the hinted line; accept only if the record's `uuid` matches, else
re-scan by uuid") and a test that a stale hint after compaction is rejected
rather than trusted. G2's shapes belong in the `model.py` of V0TASK-8.

---

## V0TASK-17 — P3's transcript-path resolution cites a session-dir union but describes slug derivation

**File:** plan `:96-97`
**Cited:** `decision_watcher.py:86-100`
**Severity: minor**

**Scenario.** P3 says "resolve transcript paths via the project-slug glob union
(pattern of `decision_watcher.py:86-100`)". The cited `agent_paths()` does not
derive any slug — it globs `<projects>/*/*/subagents/workflows/<WF_NAME>/…`,
i.e. it *unions over unknown slugs and session ids* precisely to avoid deriving
them. Deriving a slug from the registry's `cwd` is genuinely lossy: observed
slugs include
`-tmp-claude-1000--home-laniakea-Projects-touch-dd469822-…-scratchpad-castprobe`
— separators are flattened to `-` with no escaping, so distinct cwds can
collide and the inverse mapping is not a function. Building session→transcript
on a derived slug will intermittently attach one session's transcript to
another.

**Recommendation.** Resolve by the globally-unique `sessionId` instead:
`~/.claude/projects/*/<sessionId>.jsonl` (single-element result in practice,
union if not), exactly the spirit of the cited code. Keep the slug for display
only, never as a key. Note this composes with G7's "re-resolve sessionId every
tick", which P3 already does correctly.

---

## V0TASK-18 — P12 adds a second root README; and the CLAUDE.md edit needs a stated boundary

**File:** plan `:210-211` (P12 files: `README-touch.md`, `CLAUDE.md` additive edit)
**Severity: nit**

**Scenario.** The repo root already has `README.md` (product intent). Adding
`README-touch.md` beside it leaves two root READMEs with no stated division;
GitHub renders the first, so the run instructions land in the file nobody sees.
Separately, `CLAUDE.md` is a human-authored instruction file whose current text
documents the monitoring stack in detail — "additive edit" is the right
instruction, but without a named anchor the implementer may restructure it.

**Recommendation.** Put the run/publish/token documentation in `docs/touch.md`
(or extend the existing `README.md` with a "Running Touch" section — it is 655
bytes and welcomes it) and, for `CLAUDE.md`, name the exact insertion points:
append a "Touch app" subsection under `## Commands` and add the new test files
to the existing test list, changing no existing line. That also keeps
`test_shell.py`'s docs guards unaffected.

---

## Summary of what the synthesizer must decide (not just record)

1. **Marker reality** (V0TASK-1): `[touch]` has zero instances on disk;
   `[monitor]` has 12. v0 must ingest both, or ship blind.
2. **Stop granularity** (V0TASK-1/-2): per-agent stop exists only for
   background Agent-tool spawns; workflow agents can only be stopped a whole
   run at a time. Say so in the UI.
3. **Session class** (V0TASK-3): name the cooperating class or drop the Stop
   button — D1 forbids the current combination.
4. **Control protocol** (V0TASK-4/-5/-6): five states with a result→state map,
   scoped+identified intents, one non-overridable path publication. All three
   are format decisions that are cheap now and breaking later.
5. **Interfaces** (V0TASK-7/-8/-9): implement-plan's isolation makes an
   unwritten API an unimplementable plan; signatures, a `model.py`, a
   `hooks/spool.py`, and a token transport must be in the plan text.
