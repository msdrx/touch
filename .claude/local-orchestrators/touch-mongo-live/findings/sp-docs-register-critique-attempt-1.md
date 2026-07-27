# sp-docs-register — adversarial review, attempt 1

**Verdict: APPROVED** (0 blocker, 0 major, 4 minor, 4 nit).
**depth:** in-scope. **critical_defect:** false.

Reviewed: the nine owned files, against `touch-mongo-live-subplans.md` §sp-15
(R-04, R-05, R-06, R-38, R-33, R-40:docs, R-57:docs; SD-5, SD-7), the amendment
plan, the base plan (GD-17, R-04, R-05, R-06 verbatim), and the tree the docs
describe.

I set out to reject this on the most likely failure mode for a docs sub-plan —
**fabricated evidence**. It is not fabricated. Details below, then the findings.

---

## What I verified first-hand (the attacks that failed)

### R-04 — the probes were actually run, at the times claimed

This was the primary attack. `probes.md` claims five probes on
2026-07-26T23:48Z–23:57Z. All of it is corroborated by artifacts I read myself:

| claim in probes.md | independent corroboration |
|---|---|
| run 23:48–23:57Z, sandbox TZ | `date -u` = 2026-07-27T00:23Z; sandbox `TZ=UTC`, so the window is ~30 min before this review — not a future/backdated stamp |
| probe drivers | `hook_probe.py` (mtime 23:51:28Z), `pty_hook_probe.py` (23:52:24Z), `mongo_probe.py` (23:49:13Z) present in the scratchpad |
| hook fired from both settings sources, 1 byte each | `/tmp/touch-probe-hooks/fired-settings-arg.txt` = 1 byte @23:51:51Z; `fired-project.txt` present; `probe-settings.json` @23:51:40Z — matches "hooks written mid-session at 23:51:40Z" to the second |
| PTY arm | `fired-pty.txt` = 1 byte @23:55:51Z |
| background Agent-tool spawn | `bg.out` @23:56:31Z, dir mtime 23:56:07Z = the quoted probe-5 timestamp |
| `time claude agents --json` = 0.335 s | `agents.time` + `agents.json` @23:49:35Z |
| `mongo:7` image `sha256:9bdaeb6dac6e…` | `docker images` → `mongo:7  9bdaeb6dac6e` |
| probe container/volume removed after | no `touch-mongo-probe` in `docker ps -a`; only sp05/sp06 containers remain, untouched |
| probe password "appears in no file here" | `grep -rl` for the literal 24-char secret from `probe_pass.txt` across the repo → **zero hits** (GD-27 holds) |
| `sessionId: 06e081e6-…` in probe 4 | equals this run's own session id — the probes ran in this session, not copied from elsewhere |

R-04's spec lists exactly five probes ((1) hook hot-reload, (2) PTY `--settings`,
(3) mixed `command`+`http`, (4) `time claude agents --json`, (5) background
spawn transcript + usage). All five are present, each with command + date per
AUDIT-16. The "not settled" clause is honoured in probes 1 and 5 (`TaskStop`
against a running background agent explicitly recorded as unproven, R-35 named
as its owner) — a probe file that claimed everything settled would have been a
finding. Appendix A (R-38's Mongo evidence) carries all four required subjects:
pymongo 4.17.0 via proxy, `mongo:7` running, sub-document `_id` field-order
sensitivity (A3), BSON type strictness (A4).

### R-05 / R-57 — the docs describe the tree that exists, and the commands run

Every load-bearing claim I could execute, I executed:

- `python3 -m aggregator.server --help` → exits 0, and the usage block contains
  `--open`, `--allow-origin`, `--allow-host` exactly as README/CLAUDE.md print
  them. `DEFAULT_PORT = 8932` and the GD-13 loopback default are in
  `aggregator/server.py:235-241`.
- CLAUDE.md's one-liner
  `python3 -c "import aggregator.mirror as m; raise SystemExit(m.main(['--check']))"`
  → exits 0, prints `{"config": {...}, "pymongo": true}` with `"uri": null`
  (no credential — GD-27). `--health/--rebuild/--backfill` all exist
  (`mirror.py:2673-2689`).
- `tests/run_all.sh --list` → 28 files, both suites, including the two new ones;
  `--keep-going` and `--list` are real flags (`run_all.sh:33-34`).
- README's `mirror: absent | down | degraded` matches `mirror.py:189-199`
  (`STATE_ABSENT/"absent"`, `STATE_DOWN/"down"`, `STATE_DEGRADED/"degraded"`).
- README's "every route except `/health` requires the token" matches
  `OPEN_ROUTES = frozenset({"/health"})` (`server.py:568-572`) and the 401 text
  at `server.py:630`. `.touch/server.json` at 0600 matches
  `server.py:2807-2829`.
- README/CLAUDE.md's "no control affordance renders in v0": `touch-visual/`
  contains exactly one `<button>` (`olderBtn`, load-older); every
  `stop|kill|pause` hit in `app.js` is prose in a comment. The claim is true.
- CLAUDE.md's `w` writer stamp: `status.sh:66` `"w": "agent"`,
  `decision_watcher.py:350` `"w": "watcher"`. `.claude/settings.json` and
  `.claude/statusline.sh` exist as described.

**The omnigent retraction is correct, not merely a deletion.** All four
`orch-config.json` files on disk name a `wf_dir` under
`~/.claude/projects/-home-laniakea-Projects-touch/…/subagents/workflows/`; the
fifth folder (`touch-monitor-spawn`) has no `orch-config.json` and holds
`plan/` and nothing else — exactly what inception.md §7 and CLAUDE.md's table
say. The R-05-mandated token figure (≈29.5 M in / 316 k out, AUDIT-13) is in
`inception.md:321`, with the `1089990 == totalTokens` explanation of *why* the
old number was wrong — that is the good version of a truth pass, not a
find-and-replace. The R-57 storage numbers (15.7 MB / 3 936 records / ≈4 KB per
record / ≈1.3 MB h⁻¹ / 0.53×) match the amendment plan verbatim
(`touch-mongo-live-plan.md:441,967`) and trace to
`research-mongoschema-attempt-1.md:532`.

The R-57 per-session-collection disposition is in the user's own words
("Separate collections for separate session datas" — asked, and declined) with
the measured reason, per §0.3.

### R-38 — all four anchor repairs landed

D8 split into **D8.1** (stack, amended by GD-21 — explicitly *not* superseded)
and **D8.2** (journal `result`, superseded), with an anchor note and the "cite
the qualified id, never a bare D8" rule, propagated into `inception.md` §6 and
CLAUDE.md. `inception.md`'s "usage copied onto every split record" is gone,
replaced by the running-counter + `$max` rule with the 571-of-901 measurement
and both error directions (2.09× over / 2.8× under). R-03/GD-18's "both copies"
→ "**disjoint continuations**". Probes.md Appendix A appended.

### R-40:docs / SD-5 — the scoped commit gate

Both halves are present and identical in substance: plan GD-1 now carries the
scoped rule ("no commit while a watcher whose `ORCH_STATE_DIR` is inside the
paths being committed is writing") plus the explicit statement that watcher
self-exit is a convenience, never a precondition — which is exactly SD-5.
CLAUDE.md carries the same rule and the "when a run ends, stop its watcher"
lifecycle line. `test_docs.py:100-104` asserts both sides, so the halves cannot
drift apart silently.

### R-06 — the register is real work, not a template

344 rows, and I re-derived them independently: 51 `touch-repo-recon` +
110 `touch-aggregator` + 93 `touch-full-recon` + 90 `touch-mongo-live` = 344,
matching the counts CLAUDE.md and inception.md quote for the same corpora.
**278 distinct disposition strings across 344 rows**, zero empty finding cells,
zero cells under 15 chars — that is not a generated filler table. Spot-checked
citations resolve: `MONGOSCHEMA-6 → R-43, GD-24 — merged (alias kept)` matches
`touch-mongo-live-plan.md:464,655`; `AGENTGRAPH-2 → T19` matches
`touch-aggregator-plan.md:729-732`. The `SKILLS-n` namespace collision is
handled correctly — two corpora, separate tables, `(task, id)` matching, and
`test_register.py` asserts the two `SKILLS-1` rows carry *different*
dispositions, which is the assertion that would actually catch a collapse.

### Scope, ownership, and the attack checklist

- **No edits outside the ownership list.** Every file modified inside sp-15's
  window (23:48Z → 00:17Z) is one of the nine owned files.
  `touch-visual/app.js` (22:46Z), `style.css` (22:44Z),
  `test_touch_frontend.py` (22:46Z) and `test_e2e_sim.py` (23:30Z) all predate
  it — sp-13/sp-14's property, untouched here. `test_shell.py` untouched.
- **No commit made** (SD-6): `git status` still shows the four owned docs as
  modified and the five new files as untracked.
- **GD-21/22/24/25/26/28/29/30** are not reachable by this change-set: it is
  Markdown plus two test files importing only `re`, `sys`, `os`, `pathlib`.
  Neither test imports pymongo, opens a socket, or needs a mongod, so they skip
  cleanly by construction. Where the docs *restate* those rules they restate
  them correctly (no TTL "by rule"; `$max` never `$inc`/`$set`; loopback-only
  mongod; never publish 27017; agents append files, never hold a client).
- **Tests assert real behaviour.** `test_docs.py`'s load-bearing halves are the
  *negative* ones (`"omnigent" not in text`, `"no application source yet" not in
  text`, `"copied onto every split record" not in text`, the unqualified-pause
  sweep) — each pinned to a claim that was demonstrably wrong in this repo
  before. `test_register.py`'s completeness/phantom/dupe triple is a real
  cross-check against a 344-item disk corpus, not a restatement of the file.
  Both are green; I re-ran them.

---

## Findings

### 1. minor — 107 of 344 register rows dispose findings into the *superseded* plan's ids, and 23 into its deferred tier, without saying so
`.claude/local-orchestrators/touch-full-recon/plan/findings-register.md:18-24`
(and the `## touch-aggregator` table, e.g. `:147`)

I re-derived this: 110 rows cite no `R-`/`GD-`/`SD-` id at all; 107 of those are
pure `→ Tn` / `→ Dn` citations into `touch-aggregator-plan.md`, which CLAUDE.md
and inception.md both now describe as design law but "**not** an implementable
plan any more". Twenty-three of them cite the tier the sub-plans file itself
records as deferred (`T2, T9, T13, T15, T17, T19, T22` — scope exclusion #6):
`AGENTGRAPH-2 → T19`, `CONTROL-9 → T15`, `LIVEIO-1 → T9`, etc.

Nothing here is false — `T19` really does cite `AGENTGRAPH-2`. But "How to read
a disposition" presents `→ Tn` / `→ Dn` on equal footing with `→ R-nn` /
`→ GD-n` as "the plan sections that cite this finding", so a reader chasing
`AGENTGRAPH-2` reasonably concludes it is live work, when the item is deferred
and the normative plan's own old→new mapping (`T7/T8→R-26`, `T14→R-35`, …)
routes it elsewhere. GD-17's stated disposition vocabulary is
`→ item R-nn | → GD-n | merged | rejected, reason`; a bare `→ T19` is outside it.

**Fix:** two sentences in "How to read a disposition" — (a) `Tn`/`Dn` are
`touch-aggregator-plan.md` ids, superseded as *items* though live as design law,
and the old→new mapping is in `touch-full-recon-plan.md` §1; (b) tag the seven
deferred T-ids, e.g. `→ T19 (deferred tier)`. Cheapest mechanical version: have
the generator append the mapped `R-` id when the normative plan's mapping table
names one.

### 2. minor — the register overstates its own guarantee; the id scan is not exhaustive
`.claude/local-orchestrators/touch-full-recon/plan/findings-register.md:20-22`

> "These are **derived**: the register is built by scanning the three plans for
> each id, so a disposition **cannot drift** from the plan text that justifies
> it."

The scan misses inline prose citations. `AGENTGRAPH-2` is cited twice in
`touch-aggregator-plan.md` — once in T19's `Resolves:` line (`:732`) and once in
prose at `:829` (`SESSIONDATA-11 ≡ AGENTGRAPH-2 ≡ LIVEIO-13 (→ T8)`) — and the
register row (`:147`) shows only `→ T19`, dropping the `T8` link that maps
forward to R-26. And no test guards citation *completeness*: `test_register.py`
checks presence, uniqueness, non-phantom and non-vacuity, none of which would
notice a dropped citation. So "cannot drift" is a claim the suite does not back.

**Fix:** soften to "citations found by an id scan of the three plans; the scan
catches `Resolves:` lines and inline `→` references it recognises, and is not
guaranteed exhaustive for prose", or extend the generator's pattern to the
`(→ Tn)` prose form and re-generate.

### 3. minor — `test_register.py`'s corpus glob is narrower than GD-17/R-06 specify, and the narrowing is asserted nowhere
`tests/test_register.py:64` (`ORCH.glob("*/findings/research-*.md")`), docstring
`:9-11`; register `:9-13`

GD-17 and R-06 both say "every finding id under
`.claude/local-orchestrators/*/findings/*.md`". The test scans
`research-*.md` only. Today that is sound — I checked every non-`research-`
findings file (all the `sp-*-test-attempt-*.md` / `sp-*-critique-attempt-*.md`
gate reports) and **none** contains an id-shaped heading, exactly as the
register claims. But that claim is stated as a fact in two documents and
verified by no test, so a future research report named anything other than
`research-*` silently escapes the completeness guard — the precise failure R-06
exists to prevent.

**Fix:** glob `*/findings/*.md`, and in the same pass assert that files outside
the `research-` prefix yield zero id-shaped headings (turning the register's
"checked: they contain zero id-shaped headings" into an enforced invariant
rather than a snapshot). One added assertion; no behaviour change today.

### 4. minor — README points readers at a suite it knows is red, without naming which files
`README.md:31-32` ("the suite is the authority, not this table: run
`tests/run_all.sh` and believe it"), and the `implemented` rows at `:22,25`

The table calls the Mongo mirror and the read API "implemented", defines the
word honestly ("the module and its tests exist in this tree"), and defers to the
suite — good. But the suite currently has two known-red files
(`tests/test_mirror.py`, `tests/test_sessions.py`, per this sub-plan's own test
gate and seven prior gates), owned by sp-06/sp-07. A reader who follows the
instruction sees red and has no way to distinguish "known open, owned elsewhere"
from "you broke it" — which is the same class of honesty gap D13/GD-4 exist to
close for the UI.

**Fix:** one sentence under the table, e.g. "Known red as of 2026-07-27:
`test_mirror.py`, `test_sessions.py` — open findings under
`.claude/local-orchestrators/touch-mongo-live/findings/`; everything else is
green." (This edit is inside sp-15's ownership: README.md only.)

### 5. nit — `test_no_published_mongo_port`'s negation vocabulary is permissive
`tests/test_docs.py:215`

`negations = ("never", "not ", "n't", "do not", "refus", …)` — a hypothetical
paragraph "publishing 27017 is not hard, `sbx ports … 27017`" would satisfy it.
The guard's *intent* (every `sbx ports … 27017` mention is a prohibition) is
right; the implementation would accept an endorsement containing "not ".
**Fix:** require a prohibition token adjacent to the `sbx ports` occurrence
(e.g. `never publish|must not|do not publish` within the same sentence).

### 6. nit — `test_readme_pause_is_always_qualified` is weakened by the paragraph granularity
`tests/test_docs.py:174-188`

Blocks are blank-line separated, so the entire verb table is one block. Any
future pause claim added anywhere inside that table inherits the existing row's
"does not exist" qualifier and passes. The comment explains why per-block was
chosen (wrapped sentences), which is correct — this is just noting the residual
hole. **Fix:** for table blocks, evaluate per `|`-row rather than per block.

### 7. nit — one assertion is proved by a section heading, not by coverage
`tests/test_register.py:180` — `check("touch-mongo-live" in text, "the five
touch-mongo-live reports are covered")`

The string is present because the table's own `## touch-mongo-live` heading is
present; the assertion cannot fail while the heading exists. The real coverage
guarantee comes from `test_every_finding_registered_exactly_once`, which does
scan those five reports — so nothing is actually unguarded, but this line reads
as evidence it does not supply. **Fix:** assert the row count for that task
(`>= 85`, actual 90), or drop the line as redundant.

### 8. nit — small over-generalisation about `.gitkeep`
`CLAUDE.md` ("…it is why empty ones carry a `.gitkeep`")

Three `.gitkeep` files exist (`touch-aggregator/report/`,
`touch-repo-recon/report/`, `touch-repo-recon/plan/`), but `touch-monitor-spawn`
— the folder the sentence is introduced to explain — has no `report/` directory
at all, so it carries none. inception.md gets this exactly right ("the folder
holds `plan/` and nothing else"). **Fix:** "…which is why the empty ones that
exist carry a `.gitkeep`; a folder may also simply lack the directory."

---

## Verdict rationale

Zero blockers, zero majors. Every item in the sub-plan's mandate is present and
substantively done, every static guard the sub-plan enumerated is implemented,
the probe evidence is genuine and independently corroborated, the register is
complete against a 344-finding corpus with 278 distinct dispositions, no file
outside the ownership list was touched, and nothing was committed. The four
minors are documentation-clarity and test-strength improvements on files this
sub-plan owns; none of them makes a document say something untrue, and none
blocks the remaining sub-plans.

**depth: in-scope** — every finding is a small edit to these same nine files.
**critical_defect: false** — nothing here would waste or corrupt the remaining
work.
