# touch-mongo-live — loop cost profile

Snapshot 2026-07-25 ~19:10 UTC (23:10 Tbilisi), mid-run: implement workflow
`wf_b297177a-d11`, sp-03 on attempt 4 (test gate #4 green, critique #4 in
review), 12 of 15 sub-plans not yet started. Source: per-agent accounting in
this task's `events.jsonl` (decision watcher); excludes the main session's own
context and workflow-driver overhead.

## Wall-clock per loop (UTC)

| loop | span | wall time | attempts | status |
|---|---|---|---|---|
| research (5 parallel researchers) | 14:31 → 14:46 | 14 min | 1 | done |
| synthesis (plan writer) | 14:46 → 14:54 | 8 min | 1 | done |
| divide (Fable partition, 15 sub-plans) | 15:12 → 15:19 | 7 min | 1 | done |
| sp-01 repo-bootstrap | 15:19 → 15:48 | 28 min | 2 | green |
| sp-02 fixtures-freeze | 15:48 → 16:23 | 35 min | 1 | green |
| sp-03 watcher-templates-firstwave | 16:23 → open | 2h 43m+ | 4 | critique #4 running |

Pattern: a clean loop closes in ~30 min (≈10 min implement + 5 min gate +
10–15 min critique per attempt). sp-03 is the heavyweight: it owns the whole
first-wave watcher/templates cluster (base R-07…R-13 + amendment R-39/R-40 +
R-58) and took three adversarial rejections before attempt 4.

Note: the raw span computed for `divide` (3h05m) is an artifact — a corrective
`plan done` event appended at 18:17 (for the watcher's fabricated `failed`
badge, the very defect R-58 fixes) stretched the card's span. True divider
runtime: 7 min.

## Token usage (as of snapshot)

29 agents, **97.8M input + 1.0M output ≈ 98.8M total** — of which 94.1M were
cache *reads* and 3.7M cache writes, i.e. ~96% of input volume was cheap
cached context re-read by successive fresh agents.

| card | agents | input | output | of which cached |
|---|---|---|---|---|
| research (5 perspectives) | 5 | 11.2M | 231k | 10.5M |
| synthesis | 1 | 1.3M | 49k | 973k |
| divide | 1 | 499k | 39k | 299k |
| sp-01 repo-bootstrap | 6 | 6.5M | 118k | 6.1M |
| sp-02 fixtures-freeze | 4 | 11.4M | 126k | 11.0M |
| sp-03 watcher-firstwave | 12 | 66.9M | 446k | 65.3M |

## Computation usage in Big-O

Variables: **S** = sub-plans (15), **A** = attempts per loop (≤ 4 =
MAX_ATTEMPTS), **P** = shared corpus each fresh agent reads (both plans +
monitoring module + templates — the dominant constant), **F** = findings
files accrued per failed attempt, **E** = event lines.

| quantity | complexity | grounding |
|---|---|---|
| agents spawned | O(S·A) | 3 per attempt (impl/gate/critique) + 2–4 final gate; worst ≈ 180, observed 23 for 3 loops |
| wall-clock, serial driver | O(S·A·t) | no overlap by design; t ≈ 10–15 min/agent → ~30 min clean loops |
| wall-clock, parallel mode | O(A·t) | would collapse to the slowest loop; forfeits cross-loop file safety — not used |
| raw input tokens | O(S·A·P + S·A²·F) | attempt N re-reads N−1 attempts' findings — the quadratic term is why sp-03 (A=4) holds 66.9M of 97.8M |
| billed *fresh* input | ≈ O(S·A·F) | the prompt cache absorbs P (96% cache reads): re-reading the corpus is marginally ~free; only new findings/diffs cost full price |
| output tokens | O(S·A) | bounded per agent (~40–50k max), no compounding |
| watcher / monitor tick | O(Δbytes) | per-tick ingest is bytes-appended-since-last-tick — literally plan invariant GD-30 |
| events.jsonl analytics | O(E) | linear scans, trivial at this scale |

**Takeaways:** the cost driver is not S (linear, cheap) but **A²·F on
contested loops** — each adversarial rejection makes the next attempt strictly
more expensive to brief. The prompt cache changes the effective regime:
nominal input grows quadratically, billed fresh tokens stay near-linear.
