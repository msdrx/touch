# `tests/cost-corpus/` — the cost reader's frozen mini-corpus (D-21)

Owned by sub-plan **sp-08-cost**, read by `tests/test_cost.py` and by nothing
else.

## Why it is not under `tests/fixtures/`

Two reasons, and both matter:

1. **These bytes are synthesized, not copied.** `tests/fixtures/PROVENANCE.md`
   opens with "every byte here is a **verbatim copy** of a real file"; this
   corpus is not, so filing it there would falsify that sentence for the whole
   tree. Its records are written in the exact recorded shapes — an `assistant`
   record's `message.usage` four base fields plus the
   `usage.cache_creation.{ephemeral_5m,ephemeral_1h}_input_tokens` split, a
   `[monitor]` marker line, `Read` `tool_use` blocks — with round numbers, so
   the expected totals can be computed by hand and asserted exactly.
2. **`tests/fixtures/MANIFEST.sha256` has one owner and it is not this
   sub-plan.** Anything unmanifested under that tree fails
   `tests/test_fixtures.py`, and adding entries would mean editing a file
   another sub-plan is editing in the same pass.

The freeze is kept anyway, just locally: `tests/test_cost.py` pins the sha256
of every `.jsonl` below and fails if one changes by a byte. This file is prose
and is deliberately not hashed.

## What is in it

```
projects/-tmp-cost-fixture/
  c05715f0-…-00000000c057.jsonl                    driver, session 1
  c05715f0-…-00000000c057/subagents/workflows/wf_c0570001-a1b/
    agent-a00000000000000a1.jsonl                  agent 1 (opus, 3 turns)
    agent-a00000000000000a2.jsonl                  agent 2 (fable, 1 turn)
  c05715f0-…-00000000c057/subagents/workflows/wf_c0570002-b2c/
    agent-b00000000000000b1.jsonl                  agent b1 — a DIFFERENT run
  c0572222-…-000000002222.jsonl                    driver, session 2
  c0572222-…-000000002222/subagents/workflows/wf_c0570001-a1b/
    agent-a00000000000000a3.jsonl                  agent 3 (opus, 2 turns)
```

The layout is load-bearing, not decoration.

**A session is not dedicated to one run.** Session 1 holds *two* runIds —
`wf_c0570001-a1b` and `wf_c0570002-b2c` — and exactly **one** driver
transcript between them, because that is how it looks on disk: on the machine
this was written on, 11 session directories hold 2–3 distinct runIds. Folding
that whole transcript into either run charges the same turns to both, so
summing the tool's own driver dollars across a session's runs over-counts by up
to 3x, and a small run can render an absurd driver share. The driver fold is
therefore bounded to each run's own time window, and the two runs' turns are
deliberately **two hours apart** so the windows are disjoint and the arithmetic
is checkable by eye: run 1's driver slice (705 + 910 = 1,615 tok) plus run 2's
(310 tok) is exactly the whole-session fold (1,925 tok) — nothing double-counted
and nothing dropped.

Session 1's transcript also carries a **launch record** per run
(`toolUseResult.runId`, the documented join key), stamped two seconds before
that run's first agent record. That is what pins the window-widening: the turn
that *launched* a run is its first cost and predates every agent record it
created, so a window taken from agent records alone would exclude it.

**One run, two session directories.** A `/clear` mid-run gives the process a
new sessionId while the runId stays the same, so the run's later agents — and
its later driver turns — land under the *second* session (R-49). That is why
the same `wf_c0570001-a1b` appears twice: reading only the directory a task
folder's `orch-config.json` records is a silent under-report, and on this
machine's real corpus it is a 92 % one (5 agents of a run's 70). A corpus with
one session directory cannot catch that, which is the whole reason this one has
two.

**The driver is a sibling, not a child.** Each `<sessionId>.jsonl` sits beside
the directory named for it, which is the resolution
`costs.driver_transcript_for()` performs from a `wf_dir` (SUBSTRATE-7) — and
there is one per session, which is why the driver share has to be a ratio of
two sums taken over the same set of sessions.

Eight shapes are deliberately present:

| shape | where | what it pins |
|---|---|---|
| a streaming duplicate (`msg_c2` twice, `output_tokens` 10 then 200) | agent 1 | the max-fold per `message.id` — summing would report 210 |
| a 1-hour cache write (`ephemeral_1h_input_tokens: 100`) | agent 2 | the 2x multiplier, distinct from 5-minute's 1.25x |
| a `<synthetic>` turn | agent 1 | non-billable: counted as a turn, priced at zero, never "unpriced" |
| `Read` `tool_use` blocks with distinct ids across all three agents | all | the top-re-read census, deduplicated on the block id |
| a second session directory carrying the same runId | session 2 | the run-union: `analyze` over EITHER directory reads the whole run |
| a second driver transcript | session 2 | both halves of `driverShare` folded over the same sessions |
| a second runId under session 1, two hours later | session 1 | the driver slice: one session's transcript is not one run's cost |
| a `toolUseResult.runId` launch record per run | session 1 driver | the window reaches back to the turn that launched the run |

The known totals (hand-computed, asserted in `test_cost.py`): **3 agents,
6 turns, 10,690 tok context-integral, 940 tok baseline/turn, $0.03375 total;
driver 1,615 tok over 2 transcripts and a 13.125 % driver share.** The same
anchor read with `--single-session` reports the session-1 slice only — 2
agents, 4 turns, 8,730 tok, $0.0262, driver 705 tok, 7.472 % — and both
readings are asserted, because "the escape hatch still returns one directory"
and "the default no longer does" are two claims.

The co-tenant run `wf_c0570002-b2c` reads **1 agent, 1 turn, 960 tok,
$0.004375, driver 310 tok**. Note what those numbers are *not*: adding this run
to the corpus left every figure in the paragraph above unchanged, which is the
whole point — before the window slice, run 1's driver row silently absorbed run
2's turn. `--driver-whole-session` over the same anchor reports **1,925 tok
across 3 turns**, correctly, and is labelled a whole-session figure so nothing
renders it as a share "of the run".
