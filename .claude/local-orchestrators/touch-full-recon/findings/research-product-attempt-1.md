# Research — perspective: product intent, docs, repo hygiene (attempt 1)

Scope: `README.md`, `inception.md`, `CLAUDE.md`, `.gitignore`,
`.claude/settings.json` (+ `.claude/statusline.sh`, which `settings.json`
invokes), checked against the actual tree, the git state, and the running
processes. Every claim below was re-verified on disk during this run — none is
inherited on trust from the earlier `touch-repo-recon` findings (where a prior,
user-aborted run reached similar conclusions, I say so and add the fresh
evidence, because the synthesizer needs a *current* verdict, not a citation).

## Verification log (what I actually ran)

- `git status --porcelain` → 5 untracked entries; `git log` → **no commits**
  (`fatal: your current branch 'master' does not have any commits yet`).
- `git add -An .` → **45 paths** would be staged; `git check-ignore -v` on
  `.touch/control.jsonl` → **NOT IGNORED**.
- `git config user.name` / `user.email` → **unset**, locally and in
  `/home/agent/.gitconfig` (which contains only `safe.directory`,
  `core.checkstat`, `core.excludesfile`). Global excludes file contains one
  line: `.sbx`.
- All four monitoring tests run from `.claude/shared/monitoring/tests`:
  `test_server.py` (16 tests), `test_watcher.py`, `test_shell.py`,
  `test_frontend.py` → **all exit 0**. CLAUDE.md's test claim holds.
- `claude --version` → **2.1.220**, matching `inception.md:11`.
- `cat .claude/local-orchestrators/*/orch-config.json` → every `wf_dir` is
  under `/home/agent/.claude/projects/-home-laniakea-Projects-touch/…`.
- `monitor_server.py` port precedence (`:226-241`) and `decision_watcher.py`
  wf_dir precedence (`:54-68`) match CLAUDE.md:108-109 exactly.
- `ps -eo pid,etime,cmd` → two `decision_watcher.py` alive (one **11h00m**, env
  `ORCH_STATE_DIR=…/touch-aggregator`, a run finished at 03:26; one 3 min, this
  run). **No `monitor_server.py` running**; nothing listening on 893x.
- Event-stream forensics on `touch-aggregator/events.jsonl` and
  `touch-repo-recon/events.jsonl` (see PRODUCT-7).
- File mtimes: `CLAUDE.md` 02:34, `.gitignore` 02:34, `README.md` 02:38,
  `inception.md` **13:29**, `touch-repo-recon/findings/*` **13:47–13:49**.
  ⇒ **no doc has been amended in response to the prior recon's findings.**

---

## PRODUCT-1 — CLAUDE.md, the file every fresh session reads first, points at neither `inception.md` nor either plan, and its repo inventory is false

**file:line** `CLAUDE.md:7-10` and `CLAUDE.md:21-24`, `CLAUDE.md:26-31`

**severity** blocker

**scenario** `CLAUDE.md` is auto-loaded into every Claude Code session in this
repo, so it is the de-facto entry point for the next implementer (and for
`implement-plan`'s divider and implementer agents, which get a fresh context and
this file). It states the repo "contains only `README.md` … and `.claude/`"
(`:7-8`) — false: `inception.md` (16 KB, the synthesis of everything) and
`.gitignore` also exist. It tells the reader to read `execute-research` and
`implement-plan` "before designing anything in Touch" (`:22-23`) and never
mentions:
- `inception.md` — verified by `grep -rn "inception.md" *.md` → **zero hits
  outside `inception.md` itself**;
- `.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md`
  (903 lines, D1–D14 + T1–T23, called "**the normative design document**" by
  `inception.md:9-10`);
- `.claude/local-orchestrators/touch-monitor-spawn/plan/touch-monitor-spawn-plan.md`
  (229 lines, the v0 slice actually queued for implementation);
- the skills `m-orchestrator` and `touch-orchestrate`, both of which exist on
  disk (`.claude/skills/`), the latter being *normative* per
  `touch-monitor-spawn-plan.md:11-13`.

Concretely: an implementer that trusts CLAUDE.md starts from "there is no
source, go read two skills" and re-derives a design that has already been
settled by ~1.1M tokens of research. That is the single highest-cost failure
available in this repo right now. (Prior run reached the same conclusion as
INTENT-2/INTENT-3; **nothing has been fixed** — `CLAUDE.md` mtime is still
02:34, i.e. 11 hours before those findings were written.)

**recommendation** Treat "rewrite `CLAUDE.md`'s *Project status* + add a
*Documents and their authority* section" as an ordered plan item, ranked before
any code item, and give it explicit content: (1) a one-line authority ladder —
`touch-aggregator-plan.md` (design law, D1–D14) → `touch-monitor-spawn-plan.md`
(v0 scope) → `inception.md` (summary) → `README.md` (original intent, partly
superseded) → `CLAUDE.md` (session guide); (2) the true file inventory; (3) a
list of all four skills with one line each; (4) a pointer to the findings
corpora under `.claude/local-orchestrators/*/findings/`. Add a static guard
(same genre as `test_shell.py:154-161`) asserting `CLAUDE.md` contains the
strings `inception.md`, `touch-aggregator-plan.md`,
`touch-monitor-spawn-plan.md`, and `touch-orchestrate`, so this drift cannot
silently return.

---

## PRODUCT-2 — `.touch/` is not gitignored in a zero-commit repo, and the store it names will hold unredacted transcript content

**file:line** `.gitignore:1-37` (no `.touch/` entry); requirement stated at
`inception.md:211-214` (D5: "repo-local `.touch/` (gitignored;
`TOUCH_STATE_DIR` override)") and scheduled only inside the first
implementation item (`touch-monitor-spawn-plan.md:71-75`, P1).

**severity** blocker

**scenario** Verified: `git check-ignore -v .touch/control.jsonl` → not
ignored; `git log` → no commits at all. The first commit in this repo will
almost certainly be `git add -A`. Anything that creates `.touch/` before the P1
`.gitignore` edit lands — a manual probe, a P3/P6 unit test with a real store
root, the P12 end-to-end simulation, or simply running the server once — puts
`control.jsonl`, hook spools, per-session record stores and (later) the PTY
spool into the **first commit of the repository's permanent history**.
`inception.md:115-119` states in terms that this material derives from
transcripts that "hold unredacted secrets". Unlike a later accident, a leak in
commit #1 cannot be dropped by a revert; it requires a history rewrite before
any push.

Related, same file: `.claude/settings.local.json` — the file Claude Code itself
writes when a user accepts a permission — is also unignored, so per-user
harness state and local paths would be committed as project config.

**recommendation** Make the `.gitignore` edit the **first action of the first
item**, executed *before* any directory is created, and state in the item that
the edit is strictly additive (verified safe: `test_shell.py:155-161` only does
substring presence checks, so appending cannot break it). Add, at minimum:

```
.touch/
.touch*/
.claude/settings.local.json
*.pid
```

and record a hard gate in the plan: *no `git add`/`git commit` in this repo
until that edit exists*. Also state `TOUCH_STATE_DIR` variants are covered by
the `.touch*/` line so an override cannot escape the ignore.

---

## PRODUCT-3 — There is no defined initial commit, and `git commit` currently cannot run at all

**file:line** repo root (`git log`: no commits); `CLAUDE.md:9-10` ("was just
`git init`-ed on `master` and has no commits yet")

**severity** major

**scenario** Three concrete obstacles, all verified:

1. **No git identity.** `git config user.name` / `user.email` return nothing
   locally *and* in `/home/agent/.gitconfig`. The first `git commit` will abort
   with `Please tell me who you are`. Any plan item that says "commit" will fail
   on its first attempt and burn an implement→gate cycle.
2. **No commit-content decision.** `git add -An .` stages **45 paths** in one
   lump: product docs, four skills, the monitoring module + tests, and ~306 KB
   of machine-generated event streams plus `.watcher-state.json` checkpoints for
   three runs. Nothing in any doc says whether run history belongs in commit #1,
   a separate commit, or at all.
3. **Live run state would be captured mid-flight.** At the time of writing,
   `touch-full-recon/events.jsonl` and `.watcher-state.json` are being rewritten
   every few seconds by the watcher for *this* research run (mtimes tick with
   `date`). A commit taken now freezes a half-written stream and a checkpoint
   that is meaningless out of context.

Additionally the branch is `master` while this environment's stated PR default
is `main`; renaming after the first push is more expensive than deciding now.

**recommendation** One ordered item, "repository bootstrap", with an explicit
recipe: (a) set `user.name`/`user.email` (repo-local is enough) and decide
`master` vs `main` before commit #1; (b) apply the PRODUCT-2 `.gitignore` edit;
(c) commit in two commits — **C1 "tooling and docs"** = `README.md`,
`CLAUDE.md`, `inception.md`, `.gitignore`, `.claude/settings.json`,
`.claude/statusline.sh`, `.claude/skills/**`, `.claude/shared/monitoring/**`;
**C2 "orchestration history"** = `.claude/local-orchestrators/**`, taken only
when no watcher is writing (check `ps -eo cmd | grep "[d]ecision_watcher"`
first). Do not commit while a run is in flight.

---

## PRODUCT-4 — README promises "pause", `inception.md` proves it cannot exist, and CLAUDE.md repeats the promise

**file:line** `README.md:5-6` ("we must have control in which we can pause,
restart, start and terminate agents loops") and `CLAUDE.md:18-19` (verbatim
restatement) vs `inception.md:140-148` ("**\"Pause\" does not exist** in any CLI
channel — the harness's own pause is kill with a different status label") and
`inception.md:164` (pause = v1.5 hook gate, owned sessions only).

**severity** major

**scenario** The two documents an implementer reads first (`README.md` as
"product intent", `CLAUDE.md` as session guide) assert a capability that the
research proved absent; the honest verb table lives only in `inception.md:150-174`
and the plan. The v0 slice explicitly defers the pause gate
(`touch-monitor-spawn-plan.md:21-23`, G1) and its "Discarded" section repeats
"pause (no honest mechanism without the hook gate)". So an implementer who
takes README at face value either builds a fake pause button (a D13 honesty
violation — `inception.md:229-231`) or stalls on an impossible item. The same
applies, more subtly, to **restart**: README says "restart"; `inception.md:159-163`
defines it as a *model-mediated* `Workflow({resumeFromRunId})` where replayed
agents do not re-execute, while `touch-orchestrate/SKILL.md:83` defines restart
as "a fresh spawn with `attempt` + 1" — two incompatible meanings of the same
user-facing word, and the v0 plan implements **neither** (G6: "stop only, v0").

**recommendation** Amend `README.md` (or add a short "Control verbs — what is
real" block to it) with the four verbs and their honest status, copied from the
plan's D7 table: start = deterministic; terminate = deterministic ladder;
stop = model-mediated; restart = model-mediated *and* ambiguous — pick one
meaning; pause = hook gate, owned sessions, v1.5. Have the synthesizer *decide*
the single meaning of "restart" (resume-run vs fresh-attempt) and write it into
both `README.md` and `touch-orchestrate/SKILL.md` so the skill and the UI use
one vocabulary. Mirror the same table into `CLAUDE.md:18-19`.

---

## PRODUCT-5 — CLAUDE.md and inception.md both claim the task folders are foreign `omnigent` history; all four are this repo's own runs

**file:line** `CLAUDE.md:127-130` ("The `orch-config.json` files … point at
`wf_dir` paths from a **different, earlier project** (`omnigent`) … don't assume
they describe this repo") and `inception.md:54-56` ("per-task run history
(mostly carried-over `omnigent` examples)").

**severity** major

**scenario** Verified by reading all three configs: every `wf_dir` is
`/home/agent/.claude/projects/**-home-laniakea-Projects-touch**/<sessionId>/subagents/workflows/wf_…`
— this repo, this machine, this project. There is no `omnigent` path anywhere in
the tree (`grep -ri omnigent` finds only these two prose claims). The four
folders are `touch-aggregator` (the research that produced the normative plan),
`touch-repo-recon` (a user-stopped recon), `touch-monitor-spawn` (a plan-only
folder), and `touch-full-recon` (this run). The instruction "read them as
examples, don't assume they describe this repo" therefore actively tells the
next implementer to *discount the repo's only real data* — including the
`events.jsonl` streams that Touch is being built to render and that P12's
simulation should be validated against.

**recommendation** Delete both claims and replace with a short, true inventory
of the four folders: for each, one line of what it is, whether the run completed,
and what artefact is authoritative in it. Put it in `CLAUDE.md` (near the state
layout section) and mirror one sentence into `inception.md:54-56`. Include the
sessionId-bearing `wf_dir` note as a *feature*: those paths are the join key
back to the harness's own workflow journals, which the aggregator will need.

---

## PRODUCT-6 — A user-directed, normative model policy exists only inside an aborted run's workflow script and is dropped from this run

**file:line** `.claude/local-orchestrators/touch-repo-recon/orch-scripts/research.workflow.js:49`
(the `models` perspective: "THE MANDATE (user-directed, normative) …") vs
`.claude/local-orchestrators/touch-full-recon/orch-scripts/research.workflow.js:67-72`
(six perspectives: product, monitoring, skills, plans, audit, runstate — **no
`models` perspective**).

**severity** major

**scenario** The earlier recon carried an explicitly user-directed mandate:
research/implementation roles pinned to Opus 4.8 at xhigh must become **Opus 5
at xhigh**; **Fable** is reserved for exactly three roles (plan synthesizer,
main user-terminal agent, final review agent); the `implement-plan` **divider**
pin is flagged as an *open decision the synthesizer must make explicitly, never
change silently*; effort caps stay ≤ xhigh. That run was stopped before
synthesis (see PRODUCT-7), so the mandate was never written into any durable
document. `grep -n "opus\|fable"` across `README.md`, `inception.md`,
`CLAUDE.md` returns only descriptive prose (`inception.md:33-35`,
`CLAUDE.md:68-71`) — no policy, no model-id table, and no mention of Opus 5.
This run's perspective list drops the topic entirely, so the synthesizer will
produce a plan with **no role→model table**, and `implement-plan` will silently
inherit whatever the templates happen to pin.

**recommendation** The synthesizer must carry this forward from the source above
even though no researcher was assigned to it: emit a **canonical role→model
table** as a global decision in the new plan (researcher / implementer /
test-gate / critic = Opus 5 @ xhigh; synthesizer, main terminal, final review =
Fable; **divider = flagged open decision**), plus an item that applies it to
`.claude/skills/*/templates/*.workflow.js` and records it in `CLAUDE.md` so it
survives the next context reset. If the synthesizer declines, it must say so in
the discarded register with a reason — losing a user-directed mandate silently
is the failure mode to avoid.

---

## PRODUCT-7 — The run that produced the normative plan is recorded in its own event stream as never-completed, and a partial user-stopped run is indistinguishable from a complete one

**file:line** `inception.md:235-246` ("The `execute-research` run for Touch is
**complete** (7 agents, ~1.09M tokens …)"); data at
`.claude/local-orchestrators/touch-aggregator/events.jsonl`;
`.claude/local-orchestrators/touch-repo-recon/` (whole folder).

**severity** major

**scenario** Verified directly from the streams (not inherited):

- `touch-aggregator/events.jsonl` contains exactly **one** `stage:"complete"`
  line, and it is `state:"running"` at 02:59:14 ("touch-aggregator research
  starting"). There is **no terminal `orchestrator/complete/done`** — and
  `grep` across every workflow script in the repo (both skill templates and all
  three task copies) finds **no code that emits one**. `monitoring.md:103-108`
  says the driver *should* emit it, so every run in this repo is permanently
  open by construction.
- The same file contains
  `{"plan":"research","stage":"plan","state":"failed","detail":"loop exited -> synthesis"}`
  at 03:16:40, immediately followed by `synthesis plan running`. A barrier-only
  research plan never produces a gate verdict, so the watcher closes it as
  *failed*; nine minutes later `synthesis plan done "plan written"` — the plan
  that `inception.md` calls normative. The repo's flagship success is on record
  as a failure.
- `touch-repo-recon/events.jsonl` ends with a **hand-typed**
  `orchestrator complete done "run wf_455b348c-e17 stopped by user - 6
  researchers aborted, no plan written"` — proving the terminal event only ever
  arrives by hand. That folder has `plan/` and `report/` **empty** and only
  **3 of 6** findings files (`intent`, `skills`, `v0task`; `monitoring`,
  `aggtask`, `models` missing). Nothing inside the folder, and nothing in any
  doc, marks it partial — a reader (or Touch's own importer) will treat those
  three findings as a complete six-perspective recon.
- Minor, same sentence: "~1.09M tokens" is off — the *synthesizer alone* ended
  at `in 1144.7k … out 44.3k` (`events.jsonl`, last line), so the 7-agent total
  is far larger.

Touch's stated job is to render exactly this history honestly (D13,
`inception.md:229-231`). If it ingests these streams naively, its first
screenshot shows a red "failed" research run and a partial run displayed as
complete.

**recommendation** Three separable items for the plan. (a) **Docs**: amend
`inception.md:235` to say the run completed *and* that its monitor record reads
failed/open for the reasons above, so nobody "fixes" it by re-running; correct
the token figure or drop it. (b) **Protocol**: make the research/implement
templates emit `status.sh <plan> orchestrator complete done "<summary>"` on the
success path, or have the watcher render a barrier-closed plan with all agents
resulted as *closed, no verdict* rather than `failed`. (c) **Touch ingestion**:
specify that Touch must not inherit the derivation — a plan that ended with every
agent resulted and no verdict renders "closed, no verdict"; and add a
`RUN-STATUS.md` (or a `status` key in `orch-config.json`) to every task folder
recording complete / partial / aborted, starting with `touch-repo-recon`.

---

## PRODUCT-8 — CLAUDE.md's run/serve instructions contradict the security decisions in inception §5

**file:line** `CLAUDE.md:104-114` vs `inception.md:176-198`

**severity** minor

**scenario** CLAUDE.md's Commands section ends with "Dashboard at
`http://<host>:8931/` … bind any Touch dev server to `0.0.0.0`, not
`127.0.0.1`" — no mention of port **8932** (the port every plan assigns Touch,
`touch-monitor-spawn-plan.md:39-40` G5), no mention of the **per-boot 256-bit
token on every route**, and no mention of the **Origin/Host allowlist at WS
upgrade** that `inception.md:188-194` calls "a non-negotiable fix in Touch"
(the existing monitor accepted a cross-origin handshake). An implementer
following CLAUDE.md verbatim ships an unauthenticated service on 0.0.0.0 in a
sandbox whose data includes unredacted transcripts. Separately, the premise
"8931 is the live monitor" (`inception.md:189`) is situational, not permanent:
right now **no `monitor_server.py` is running and nothing is listening on
893x** — only two `decision_watcher.py` processes are alive. The port split is
still the right call, but the doc should say *reserved*, not *occupied*.

**recommendation** Rewrite `CLAUDE.md:104-114` into two labelled blocks —
"legacy monitor (8931)" and "Touch (8932: token on every route except
`/health`, `hmac.compare_digest`, Origin/Host allowlist at upgrade, bind
`0.0.0.0` only with the token)" — and state the ports are reserved by
convention. Keep the `sbx ports … --publish` line for both.

---

## PRODUCT-9 — `.gitignore` gaps and an undecided policy on mutating per-task checkpoints

**file:line** `.gitignore:1-37`; state files at
`.claude/local-orchestrators/*/.watcher-state.json`

**severity** minor

**scenario** Verified with `git status --ignored=matching`: the ignore file
correctly catches `__pycache__/` (6 dirs of `.pyc` present) and
`*/decision_watcher.log`, `*/monitor_server.log` — good. Gaps that will bite:
- `.claude/settings.local.json` (written by the harness itself) — unignored, see
  PRODUCT-2.
- Log files nested one level deeper (`<task>/logs/x.log`) escape the
  `local-orchestrators/*/*.log` glob; the plans put Touch's own logs under
  `.touch/`, so this is only a trap for orchestrator scripts.
- `.watcher-state.json` is **tracked** per task by current policy
  (`CLAUDE.md:58-61`; `monitoring.md:200` only gitignores the *module-dir*
  copies). It is a live checkpoint rewritten on every poll: during any run the
  tree is permanently dirty, and each commit carries a meaningless byte-diff.
  Meanwhile `events.jsonl` genuinely *is* history worth tracking (236 KB for the
  aggregator run alone — expect the repo to gain ~0.2–0.3 MB of machine log per
  research run, forever, given the never-delete rule at `CLAUDE.md:118-121`).

**recommendation** Decide and record, in one place: track `events.jsonl`
(history, replayed by the monitor, input for Touch's importer), **ignore**
`.claude/local-orchestrators/*/.watcher-state.json` (derivable, mutating; the
never-delete rule protects the event stream, not the checkpoint). Add the two
missing patterns from PRODUCT-2. Keep the edit additive and re-run
`test_shell.py`. If the growth of tracked event logs is a concern, state the
policy explicitly (e.g. "runs older than N are gzipped in place") rather than
leaving it to the first person who notices the repo size.

---

## PRODUCT-10 — README.md is nominated as the product source of truth but is a 7-line stub, and the plan schedules a *second* README

**file:line** `README.md:1-7`; `CLAUDE.md:12-13` ("Per `README.md`, Touch is…");
`touch-monitor-spawn-plan.md:210-211` (P12 writes `README-touch.md`)

**severity** minor

**scenario** `README.md` is seven lines of lowercase, partly ungrammatical prose
("Touch have 2 main components"), with no setup, no run instructions, no
architecture, and — per PRODUCT-4 — a capability list that is partly
unimplementable. Yet CLAUDE.md and both plans treat it as the statement of
product intent. P12 then adds `README-touch.md` for the how-to-run docs, which
leaves the repo with two READMEs and no stated relationship between them: a
newcomer opening `README.md` (the file GitHub renders) still sees the stub.

**recommendation** Decide the doc architecture in the plan, explicitly:
`README.md` becomes the human entry point (what Touch is, honest verb table,
how to run, where the design docs live) and P12's content merges into it —
or `README.md` is frozen verbatim as "original intent" with a one-line banner
pointing to `README-touch.md`. Either is fine; leaving it undecided produces
the worst outcome by default.

---

## PRODUCT-11 — `.claude/settings.json` + `statusline.sh` are undocumented committed harness config, an external `jq` dependency, and the future host of the hook pack

**file:line** `.claude/settings.json:1-6`; `.claude/statusline.sh:19-26`

**severity** minor

**scenario** `settings.json` contains only a `statusLine` command pointing at
the *relative* path `.claude/statusline.sh`, which shells out to **`jq`** six
times (present here: `/usr/bin/jq`; not a stdlib-only dependency, and the repo's
stated rule is stdlib-only — `inception.md:179-181`). Neither file is mentioned
in `README.md`, `CLAUDE.md`, or `inception.md`'s repo inventory
(`inception.md:29-31`), so both arrive in commit #1 unexplained, and they apply
to every contributor's session. More consequentially, `settings.json` is where
the plan's hook pack must register (`touch-monitor-spawn-plan.md:194-205` P11
documents "the settings.json wiring"; `touch-aggregator-plan.md:546-561` T10),
so a later item will edit this committed file — and the same plan lists
`settings.json` among paths Touch must never serve
(`touch-aggregator-plan.md:261`).

**recommendation** Add one line to `CLAUDE.md`'s inventory naming both files and
the `jq` requirement, and state in the P11/T10 item that the settings edit is
**additive** to a committed, session-wide file (with the hook's `"timeout": 5`
and the opt-in default off, as P11 already says). If stdlib-purity matters for
the statusline too, note it as an accepted exception rather than an oversight.

---

## PRODUCT-12 — Operational leftovers nothing in the docs tells anyone to clean up

**file:line** `CLAUDE.md:116-126` ("Rules that bite" — no shutdown rule);
observed processes and empty dirs

**severity** nit

**scenario** A `decision_watcher.py` for `touch-aggregator` has been running
**11 hours** (`ORCH_STATE_DIR` confirmed via `/proc/4929/environ`) for a run
that ended at 03:26. The docs say emphatically never to delete a finished task
folder and how to `pkill` safely, but never say *when a run is over, stop its
watcher* — so daemons accumulate across sessions, each holding a checkpoint file
open. Related tidiness: `report/` is empty in all three completed/aborted task
folders, and `touch-repo-recon/plan/` is empty, though `CLAUDE.md:58-61`
describes both as part of the fixed per-task layout — a reader cannot tell
"never produced" from "produced and lost".

**recommendation** Add one bullet to "Rules that bite": when a run reaches its
terminal event, stop its watcher (`pkill -f "[d]ecision_watcher"` scoped by
`ORCH_STATE_DIR`, or have the driver do it) and leave the state files in place.
Say in the layout section that `plan/` and `report/` may legitimately be empty
and what that means.

---

## PRODUCT-13 — Machine-specific absolute paths are baked into everything about to be committed

**file:line** e.g. `.claude/local-orchestrators/*/orch-config.json` (`wf_dir`
under `/home/agent/.claude/projects/-home-laniakea-Projects-touch/…`);
`touch-full-recon/orch-scripts/research.workflow.js:22-26`
(`const REPO = '/home/laniakea/Projects/touch'`)

**severity** nit

**scenario** Commit #1 will fix two different absolute roots (`/home/agent`
for `~/.claude`, `/home/laniakea/Projects/touch` for the repo) into permanent
history. On any other machine, or if the sandbox user changes, the orch-scripts
and configs are inert and the paths are misleading. This is tolerable for
run *history* (it is a record of what happened) but not for the skill
**templates**, which are meant to be copied and adapted.

**recommendation** Keep the history as-is (it is a record), but verify the skill
templates under `.claude/skills/*/templates/` derive `REPO` from `process.cwd()`
or an env var rather than a hard-coded path, and note in `CLAUDE.md` that the
absolute paths inside `local-orchestrators/` are historical artefacts of the
machine that ran them.

---

## Summary — authoritative vs stale, from this run's evidence

**Authoritative (re-verified true right now)**
- All four monitoring test suites pass; `CLAUDE.md:86-102`'s test claims,
  including the `.gitignore` guard, are accurate.
- `CLAUDE.md:104-110`'s port/wf_dir precedence lines match the code exactly
  (`monitor_server.py:226-241`, `decision_watcher.py:54-68`).
- `inception.md:11`'s CLI pin (2.1.220) matches the installed binary.
- The never-delete-a-task-folder and always-set-`ORCH_STATE_DIR` rules.

**Stale or false (must be corrected by the new plan)**
- `CLAUDE.md:7-10` inventory; `CLAUDE.md:21-31` doc pointers (PRODUCT-1).
- `CLAUDE.md:127-130` + `inception.md:54-56` omnigent provenance (PRODUCT-5).
- `CLAUDE.md:112-114` port/bind guidance (PRODUCT-8).
- `inception.md:235-246` run status and token figure (PRODUCT-7).
- `README.md:5-6` / `CLAUDE.md:18-19` control verbs (PRODUCT-4).

**Decisions the synthesizer must make, not inherit**
- The single meaning of "restart" (PRODUCT-4).
- The role→model table, including the divider (PRODUCT-6).
- Track vs ignore `.watcher-state.json`, and the growth policy for tracked
  event logs (PRODUCT-9).
- One README or two (PRODUCT-10).
- Branch name and the two-commit boundary (PRODUCT-3).

**Hard preconditions before any implementation item runs**
`.gitignore` gains `.touch/` (+ `.touch*/`, `.claude/settings.local.json`)
*before* anything creates the store; git identity is set; no commit is taken
while a watcher is writing.
