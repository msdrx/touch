#!/usr/bin/env python3
"""The five skill PROSE files tell the post-determinism truth.

sp-09 of the touch-determinism plan: D-19 in full, plus the prose halves of
D-07, D-08, D-09, D-10, D-11, D-12, D-13, D-14, D-15 and D-24. Run as
`python3 test_skills_prose.py`; exits non-zero on failure. No pytest, no
runner — `run_all.sh` picks it up by its `test_*.py` glob.

WHY THIS FILE IS SEPARATE FROM `test_skills_payload.py`
------------------------------------------------------
That file owns the SHIPPING-SHAPE gate: the skills are a moved tree, the
templates are self-contained, no payload file anchors the tasks root on a bare
`$PWD`. This one owns a different question — **does the prose describe the
system that now exists?** — and the two failed for different reasons: a broken
shape breaks an install, while stale prose is obeyed by an agent and produces a
run that half-works. The split is also ownership: sp-03 owns the templates and
their gate, sp-09 owns the five `.md` files an agent reads before it launches
anything.

WHAT MAKES A DOC CLAIM TESTABLE
-------------------------------
Only two kinds of assertion are made here, and neither of them is "the prose
reads well":

  ABSENCE of a retired instruction. A mandate an agent can still find is a
      mandate an agent will still obey — the FIRST/LAST `touch-status` pair
      (D-09), the hand-written spawn-ledger append (D-19), the `grep -vxF …
      > "$f.tmp"` ACTIVE idiom (D-13), "add it by hand" for `agentR` (D-11),
      and the fill-in-the-constants instruction (D-12) are all deleted, so
      each is asserted GONE by the exact words that carried it.

  PRESENCE of the replacement, with its evidence. A deletion with no
      successor sentence is how the next reader "restores" the mandate: the
      `touch-run` verbs (D-13), the derived close and its belt-and-braces
      demotion (D-07), the named deterministic emitters (D-10), the
      `--settle` correction path (D-14), the `--final`/`--narrative` report
      flow (D-15), the hoisted `Method:` paragraph (D-24), the dormant marker
      on the control loop (D-19) and D-09's four measured numbers.

The numbers are pinned deliberately. "The watcher usually gets there first" is
an opinion; 96–99% twin coverage against 79–100% compliance over 1,197 solo
model requests is a measurement, and a reader who wants to reopen the decision
has to argue with it. Pinning them here also means a future edit cannot quietly
soften the claim into something unfalsifiable.

WHAT THIS FILE DELIBERATELY DOES NOT ASSERT
-------------------------------------------
* Template-side facts (no `FIRST run:` in a prompt builder, `agentR` defined,
  the marker at every spawn site, the slice paths) — `test_skills_payload.py`
  owns the templates and already pins all of them.
* `custom_state.BIND_CHANNELS`' vestigial note — that file belongs to the
  hooks sub-plan; only the PROSE side of the same decision is checked here.
* The frontmatter `description:` lines — they are the measured always-on
  budget (`README`/`CHANGELOG` pin the figure), and re-costing them belongs to
  the budget item, not to a prose rewrite. (`monitor`'s was corrected in
  this pass anyway, because it still advertised the three-command world the
  body retired — a stale sentence on the always-on discovery surface is a
  prose defect whoever owns the budget line.)

WHY A HOOK MAY BE NAMED BY FILENAME WHERE A DAEMON MAY NOT
----------------------------------------------------------
`test_skills_payload.DAEMON_FILES` bans a SKILL.md from spelling
`decision_watcher.py` / `cycle_reporter.py`, and this file re-asserts that ban
(D-10's arm) while REQUIRING `agent_lifecycle.py` two files over. Not an
inconsistency: the ban's reason is invocation. A daemon has a wrapper on PATH,
so a payload path in prose is an instruction to reach into a version-stamped
cache that an update sweeps; a hook is never invoked by a reader at all — the
harness runs it from `hooks/hooks.json` — so there is nothing to reach for and
the filename is the only name it has. Generalising the ban to every payload
filename would take this arm with it, which is why the distinction is written
down here rather than left to be re-derived.

THE TWO CONDITIONAL ARMS
------------------------
Both hang on `hooks/agent_lifecycle.py`, another sub-plan's file, which landed
only because the D-17 probe came back green — a prose sentence naming a hook
that does not ship is a promise about behaviour that does not exist, and an
"exactly three emitters" claim in a payload that ships four is the same defect
with the sign flipped. So each arm is two-sided: D-19's mandate is asserted gone
unconditionally (the deterministic coverage argument stands on `find_spawns` +
the marker, hooks or no hooks), while the sentence naming the hook as the
ledger's writer and the sentence disclosing it as a fourth, ADDITIVE emitter are
required only when the hook is actually in the payload.

WHY THE LEDGER SCHEMA IS PINNED HERE TOO
----------------------------------------
`tests/test_slots.py` already reads that JSON block — it is the wire shape
`aggregator/custom_state.py` ingests. It is re-pinned here because the first
attempt at this rewrite deleted the schema along with the mandate and took that
suite red: the two live in one bullet, one is retired and one is not, and only a
test in the file that owns the prose can say which. D-19 retires the
instruction to hand-write a ledger line; the line itself still ships, written by
the hook, and prose that names a writer without stating what it writes leaves
the next hook edit with no spec to be held to.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _roots import PAYLOAD                                  # noqa: E402

SKILLS = PAYLOAD / "skills"
M_SKILL = SKILLS / "monitor" / "SKILL.md"
RECOVERY = SKILLS / "monitor" / "network-recovery.md"
R_SKILL = SKILLS / "research" / "SKILL.md"
I_SKILL = SKILLS / "implement" / "SKILL.md"
O_SKILL = SKILLS / "orchestrate" / "SKILL.md"
LIFECYCLE_HOOK = PAYLOAD / "hooks" / "agent_lifecycle.py"

#: The five files this sub-plan rewrote, by the name used in failure messages.
PROSE = {
    "monitor/SKILL.md": M_SKILL,
    "monitor/network-recovery.md": RECOVERY,
    "research/SKILL.md": R_SKILL,
    "implement/SKILL.md": I_SKILL,
    "orchestrate/SKILL.md": O_SKILL,
}

failures = []
skips = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def skip(msg):
    print(f"  skip: {msg}")
    skips.append(msg)


def read(path):
    """The file's text, or `None` when it is missing (reported by the caller)."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def texts():
    """`{name: text}` for every prose file that exists; missing ones FAIL."""
    out = {}
    for name, path in PROSE.items():
        text = read(path)
        if text is None:
            check(False, f"{name} exists to be checked")
        else:
            out[name] = text
    return out


# --------------------------------------------------------------- D-09/GD-D14
def test_no_prose_mandates_the_status_pair():
    """The FIRST/LAST pair is deleted from the prose, not merely deprecated.

    `monitoring.md` and both templates lost it in earlier sub-plans; a skill
    that still spells the mandate is the copy an agent actually reads before a
    launch, so the absence has to hold in all five files at once.
    """
    print("test_no_prose_mandates_the_status_pair")
    for name, text in texts().items():
        for banned in ("FIRST run:", "LAST  run:", "LAST run:"):
            check(banned not in text,
                  f"{name} does not spell the retired mandate {banned!r}")
    ms = texts().get("monitor/SKILL.md", "")
    check("Do NOT mandate status calls" in ms,
          "monitor/SKILL.md says the mandate is gone, in so many words")
    # The command survives the mandate (GD-D14): the deletion removed instructed
    # invocations, not the writer. A reader who concludes `touch-status` is
    # retired writes raw JSON into events.jsonl, which is the failure GD-D5
    # exists to prevent.
    check("touch-status" in ms and "ONE write path" in ms,
          "...and that `touch-status` is still the one write path (GD-D5)")
    check("info \"<what is happening now>\"" in ms,
          "...leaving the optional unobservable-middle note as the one legitimate "
          "in-prompt call")


def test_the_deletion_carries_its_measured_evidence():
    """D-09's four numbers, in the file a driver reads.

    Recorded rather than summarised because the decision is re-litigable: the
    pair cost ~$0.14 a run, so the argument for deleting it was never cost, and
    the only thing that settles it either way is the coverage/compliance
    measurement.
    """
    print("test_the_deletion_carries_its_measured_evidence")
    ms = texts().get("monitor/SKILL.md", "")
    for number, what in (("96–99%", "watcher-twin coverage"),
                         ("79–100%", "agent compliance with the mandate"),
                         ("1,197", "solo model requests measured over"),
                         ("0.05%", "the pair's share of a run's bill")):
        check(number in ms, f"monitor/SKILL.md records {number} ({what})")
    check("earlier" in ms,
          "...and that the derived line usually lands EARLIER than the agent's")
    # GD-D3: correctness framing, never token framing. The sentence that forbids
    # a cost argument has to survive, in both directions.
    check("not tokens" in ms or "never tokens" in ms,
          "monitor/SKILL.md frames the deletion as correctness, not tokens")


# --------------------------------------------------------------------- D-07
def test_the_run_close_is_derived_and_the_typed_close_is_demoted():
    print("test_the_run_close_is_derived_and_the_typed_close_is_demoted")
    ms = texts().get("monitor/SKILL.md", "")
    check("the driver MUST close" not in ms,
          "monitor/SKILL.md no longer MUSTs the driver-typed close")
    check("belt-and-braces" in ms,
          "...it demotes that close to belt-and-braces (GD-D6 rung 3)")
    check("first rung to land wins" in ms,
          "...and states that the first rung to land wins")
    for rung in ("snapshot", "task-notification", "quiet"):
        check(rung in ms, f"...naming the {rung} rung")
    # SUBSTRATE-11 and R-58, the two halves nothing may soften: a missing
    # snapshot is normal, and `killed` is never green.
    check("never an error" in ms,
          "...that a missing recorded source is normal, never an error")
    check("`killed` never renders `done`" in ms,
          "...and that `killed` never renders `done` (R-58)")


# --------------------------------------------------------------------- D-08
def test_recovery_prose_points_at_the_written_resume_block():
    print("test_recovery_prose_points_at_the_written_resume_block")
    rec = texts().get("monitor/network-recovery.md", "")
    check("<!-- touch:recovery -->" in rec,
          "network-recovery.md names the RESUME.md recovery markers the watcher "
          "writes between (D-08c)")
    check("verbatim" in rec,
          "...and says the spliced call is the harness's own, verbatim")
    check("touch-run bind" in rec,
          "...with the resume pointers recorded by `touch-run bind`, not by hand")


# --------------------------------------------------------------------- D-10
def test_the_emitters_are_named_and_the_script_emits_nothing():
    """GD-D5's boundary, stated where a driver will read it.

    The R-09 "script-emitted" claim moved onto the daemons because the runtime
    has no Node API: every `runStatus`/`closeRun`/`publishConfig` silently
    no-opped. A doc that still credits the script sends the next debugger into
    a template looking for an emitter that was never there.
    """
    print("test_the_emitters_are_named_and_the_script_emits_nothing")
    ms = texts().get("monitor/SKILL.md", "")
    # By COMMAND name, not by file: `test_skills_payload.DAEMON_FILES` bans a
    # SKILL.md from spelling `decision_watcher.py`/`cycle_reporter.py` at all,
    # because a payload path in prose is an instruction to reach into a
    # version-stamped cache. The two rules agree — D-10 asks that the emitters
    # be NAMED, and the wrappers are their names.
    for daemon in ("touch-watcher", "touch-cycle-reporter", "touch-run"):
        check(daemon in ms,
              f"monitor/SKILL.md names {daemon} as a deterministic emitter")
    check("decision_watcher.py" not in ms and "cycle_reporter.py" not in ms,
          "...by command name, never by the payload file behind it")
    check("no Node API" in ms,
          "...and states why the workflow script emits nothing (no Node API)")
    for retired in ("runStatus", "closeRun", "publishConfig"):
        check(f"{retired}`" in ms or f"{retired}/" in ms or f"/{retired}" in ms,
              f"...naming the deleted {retired} helper so nobody re-adds it")
    # The list must not read as EXHAUSTIVE while a fourth emitter ships. The
    # lifecycle hook writes lifecycle lines into the same stream (through
    # `touch-status`, so GD-D14's two writers survive), and this file is what an
    # operator consults when they meet an event they cannot account for. Same
    # two-sided shape as the ledger arm: required only when the hook is there.
    if LIFECYCLE_HOOK.is_file():
        check("agent_lifecycle.py" in ms,
              "...and names the lifecycle hook as the fourth, ADDITIVE emitter "
              "(GD-D5: hooks are additive, never the floor)")
        check("additive, never the floor" in ms,
              "...in those words, so no card or verdict is read as depending on it")
        check("exactly three" not in ms,
              "...so the three are the FLOOR, never an exhaustive list")
    else:
        skip("hooks/agent_lifecycle.py is not in the payload — no fourth "
             "emitter to disclose")


# --------------------------------------------------------------------- D-11
def test_agentr_is_shipped_not_pasted_in():
    print("test_agentr_is_shipped_not_pasted_in")
    rec = texts().get("monitor/network-recovery.md", "")
    check("add it by hand" not in rec,
          "network-recovery.md no longer tells a launcher to add `agentR` by hand")
    check("BOTH shipped templates" in rec or "Both shipped templates" in rec,
          "...it says both templates carry the wrapper built in (D-11a)")
    check("touch-run verify" in rec,
          "...and that the preflight refuses a copy that lost it")


# --------------------------------------------------------------------- D-12
def test_no_skill_instructs_editing_constants_into_the_script():
    """GD-D9: the driver authors a run spec, never a script.

    The retired instruction was literally "fill them in yourself". While it
    survives anywhere, a driver edits `orch-scripts/*.workflow.js` by hand and
    the byte-identity pin `touch-run verify` applies has nothing true to check.
    """
    print("test_no_skill_instructs_editing_constants_into_the_script")
    for name in ("research/SKILL.md", "implement/SKILL.md"):
        text = texts().get(name, "")
        check("fill them in yourself" not in text,
              f"{name} no longer says to fill constants in by hand")
        check("--spec" in text,
              f"{name} points the driver at the run spec instead")
        check("byte-for-byte" in text,
              f"{name} states the orch-scripts copy is byte-for-byte")
        # SKILLS-14, decided the template's way: PLUGIN_ROOT is never baked.
        check("left as shipped" in text,
              f"{name} leaves PLUGIN_ROOT as shipped rather than filling it in")


# --------------------------------------------------------------------- D-13
def test_the_driver_recipes_collapsed_onto_touch_run():
    print("test_the_driver_recipes_collapsed_onto_touch_run")
    ms = texts().get("monitor/SKILL.md", "")
    for verb in ("touch-run start", "touch-run bind", "touch-run close",
                 "touch-run verify", "touch-run status"):
        check(verb in ms, f"monitor/SKILL.md documents `{verb}`")
    # GD-D8: a driver envelope is not a control verb, and the file that
    # introduces it is where that has to be said.
    check("not** a control verb" in ms or "not a control verb" in ms,
          "...and says touch-run is NOT a control verb (GD-D8)")
    check("CONTROL_ROUTES" in ms,
          "...naming CONTROL_ROUTES so the claim is checkable")
    # Inside a ```bash fence a bare `|` is a pipe, so the wrapper's own usage
    # spelling of the two-form verb is a syntax error when pasted. Two lines.
    check("touch-run verify <task> | --spec" not in ms,
          "...spelling `verify`'s two forms as two pasteable lines, not with a "
          "bare `|` inside a bash fence")
    # The two hand-typed idioms this replaced. Both were WRONG, both were
    # copied, and a reader who finds either one still spelled out will copy it
    # again.
    # The recipe is described, never REPRINTED: a retired command still spelled
    # out in a file agents read is a command that gets copied again. `$f.tmp`
    # and `echo $!` are the two unique tokens of the two recipes that went.
    check('$f.tmp' not in ms and 'grep -qxF' not in ms,
          "monitor/SKILL.md no longer reprints the ACTIVE close-out idiom "
          "with its shared temp path")
    check("retired" in ms,
          "...but does say it is retired, and why (so it is not reinvented)")
    check("echo $! > " not in ms,
          "...nor the hand-rolled daemon pid capture (`touch-run` records pids)")
    # What the shrink must NOT have lost: the resolver rungs and both sentinels.
    for kept in ("ORCH_TASKS_ROOT", "CLAUDE_PROJECT_DIR", "ACTIVE", "HALT"):
        check(kept in ms, f"...while keeping {kept} documented")
    for name in ("research/SKILL.md", "implement/SKILL.md"):
        text = texts().get(name, "")
        for verb in ("touch-run start", "touch-run bind", "touch-run close"):
            check(verb in text, f"{name} drives the run with `{verb}`")


# --------------------------------------------------------------------- D-14
def test_settle_replaces_hand_typed_corrections():
    print("test_settle_replaces_hand_typed_corrections")
    rec = texts().get("monitor/network-recovery.md", "")
    check("--settle" in rec,
          "network-recovery.md points at `--settle` for cards a dead agent left "
          "open (D-14)")
    check("idempotent" in rec,
          "...and says a second run writes nothing")
    check("closed — no verdict" in rec,
          "...and refuses a hand-typed `failed` for a run with no verdict (R-58)")
    imp = texts().get("implement/SKILL.md", "")
    check("--settle" in imp,
          "implement/SKILL.md documents the settle pass too")
    check("touch-run bind" in imp and "cycle reporter" in imp,
          "...and that `touch-run bind` starts the reporter (row 33)")


# --------------------------------------------------------------------- D-15
def test_the_report_step_is_render_narrate_publish():
    """SKILLS-9: the report shrank to three steps, one of them LLM-authored."""
    print("test_the_report_step_is_render_narrate_publish")
    for name, page in (("research/SKILL.md", "research-report.html"),
                       ("implement/SKILL.md", "final-report.html")):
        text = texts().get(name, "")
        check("--final" in text,
              f"{name} renders the report with the deterministic `--final` mode")
        check("--narrative" in text,
              f"{name} injects the ONE authored section through --narrative")
        check(page in text, f"{name} names the page it produces ({page})")
        check("Artifact tool" in text,
              f"{name} keeps publishing as the last step, not the storage")
        # The storage rule is satisfied by construction now; saying so is what
        # stops the next reader re-adding a manual `cp` into report/.
        check("by construction" in text,
              f"{name} states the file lands in report/ by construction")
        # The renderer made the PAGE deterministic; the fragment in step 1 is
        # still hand-authored HTML that gets published, and the Artifact tool's
        # own contract asks for the design skill before a page goes out. The
        # shrink dropped that clause from both files at once.
        check("artifact-design" in text,
              f"{name} still loads the `artifact-design` skill before the "
              "authored fragment is written and published")


def test_the_report_surfaces_are_documented_with_their_defaults():
    """Both skills state what a run reports, where it goes, and the default.

    A switch nobody can find is a switch nobody uses, and a DEFAULT nobody
    states is one the next reader re-derives by running something. The three
    values below are the shipped answer (pinned as values in
    `tests/test_cycle_reporter.py`, published by `touch-run start` and
    cross-checked in `tests/test_touch_run.py`), so the prose that quotes them
    is held to the same standard as every other number in this file.

    The second arm is the one that matters operationally: switching a surface
    off must be described as stopping PAGES only. A reader who believes it also
    stops the run's events will "fix" a dashboard by re-enabling reports, or
    worse, will not switch anything off at all.
    """
    print("test_the_report_surfaces_are_documented_with_their_defaults")
    for name, surface, default in (("research/SKILL.md", "research", "local|public"),
                                   ("implement/SKILL.md", "final", "local|public"),
                                   ("implement/SKILL.md", "cycle", "local")):
        text = texts().get(name, "")
        check('"reports"' in text or "`reports`" in text,
              f"{name} names the `reports` key a spec carries")
        # A destination carries a `|`, which a table cell must escape or the
        # row grows a column. Both spellings mean the same value, so the
        # comparison is made on the unescaped text and the markdown is left to
        # be markdown.
        row = [ln.replace("\\|", "|") for ln in text.splitlines()
               if ln.strip().startswith(f"| `{surface}`")]
        check(len(row) == 1 and f"on, `{default}`" in row[0],
              f"{name} states the `{surface}` surface's default (on, {default}) "
              f"in its own row ({row})")
        # The vocabulary is the DESTINATIONS, and a value names the ones it
        # means — so the prose has to carry the joined spelling, not a word
        # like `both` that a reader cannot decompose.
        check("`local|public`" in text.replace("\\|", "|"),
              f"{name} spells a destination as the set it names")
        check("`both`" not in text,
              f"{name} does not still quote a `both` destination")
    for name in ("research/SKILL.md", "implement/SKILL.md"):
        text = texts().get(name, "")
        check("changes nothing else" in text or "nothing else" in text,
              f"{name} says an off surface stops pages and nothing else")
        check(".touch/run.json" in text,
              f"{name} points at the per-project home for the same key")
        # The storage rule is not a knob: `local` chooses whether the Artifact
        # step happens, never whether the durable copy exists.
        check("task-folder copy" in text,
              f"{name} states the task-folder copy is written for every "
              f"destination")


# ------------------------------------------------------------ D-12 / GD-D11
def test_the_run_specs_seed_the_cards_touch_run_can_know_up_front():
    """A documented spec must be able to make the claim beside it true.

    `touch-run start` seeds plan cards from the spec's `roster` and nothing
    else — there is no `divide` default in the wrapper (`grep -c divide
    bin/touch-run` is 0). A skill that promises a seeded card while its own
    example omits the key is the exact failure mode this sub-plan exists to
    remove: prose describing a system that does not exist. Reproduced before
    this assertion was written — the documented implement spec without a roster
    seeded one card, its own, and left `plans_total` unset until the divide
    close.
    """
    print("test_the_run_specs_seed_the_cards_touch_run_can_know_up_front")
    for name, ids in (("research/SKILL.md", ("research", "synthesis")),
                      ("implement/SKILL.md", ("divide", "finalgate"))):
        text = texts().get(name, "")
        check('"roster"' in text,
              f"{name}'s run-spec example carries the `roster` key")
        for plan_id in ids:
            check(f'"id": "{plan_id}"' in text,
                  f"...naming the `{plan_id}` card it can seed up front")
    # The half a roster CANNOT cover, said out loud, so nobody "fixes" the
    # missing sub-plan cards by inventing ids the template never emits.
    imp = texts().get("implement/SKILL.md", "")
    check("N+2" in imp or "N sub-plans + 2" in imp,
          "implement/SKILL.md says the reporter re-declares plans_total "
          "as N+2 once the partition exists (GD-D11)")
    # Caps: name the two this protocol actually reads, and do not send a driver
    # to the run spec for the two that have no spec key at all.
    check("max_finalgate_attempts" in imp,
          "...and names `max_finalgate_attempts`, the cap the reporter reads at "
          "the sweep close")
    check("no spec key" in imp,
          "...while stating that max_gate_attempts / max_e2e_attempts have no "
          "spec key, so nobody edits a run spec expecting them to land")


# --------------------------------------------------------------------- D-24
def test_the_method_paragraph_lives_in_the_skill():
    print("test_the_method_paragraph_lives_in_the_skill")
    rs = texts().get("research/SKILL.md", "")
    # The HEADING form, not the bare word: "Method" matches any prose sentence,
    # and what D-24 moved is a titled section that stands in for N prompt copies.
    check("**Method — " in rs,
          "research/SKILL.md carries the hoisted Method paragraph (D-24)")
    for phrase in ("adversarial", "throwaway", "blocker | major | minor | nit"):
        check(phrase in rs, f"...including its {phrase!r} clause")
    # ECONOMICS-6's one line, with BOTH measurements the run-2 register requires
    # (volume AND call counts — the bare percentage was the corrected claim).
    check("50.6%" in rs and "16,786" in rs and "3,005" in rs and "5.6:1" in rs,
          "...and the read-discipline line cites volume AND call counts")
    # ...attributed to the denominator that was actually measured. 50.6% is
    # `cat`/`sed`/`head`'s share of BASH's own result volume; promoting it to a
    # share of ALL tool-result volume is a bigger, unmeasured claim, and this is
    # the one line where the number IS the argument for the instruction.
    check("of all *Bash* result volume" in rs or "of all Bash result volume" in rs,
          "...against Bash's own result volume, not all tool-result volume")


# --------------------------------------------------------------------- D-19
def test_the_spawn_ledger_mandate_is_gone():
    """Zero ledger lines were ever written by hand; the mandate is deleted.

    Unconditional: the deterministic bind is `agents.find_spawns` plus the
    `[touch]` marker, which needs no hook at all. The hook is the durable
    channel when it is present, which the next arm checks separately.
    """
    print("test_the_spawn_ledger_mandate_is_gone")
    os_text = texts().get("orchestrate/SKILL.md", "")
    for banned in ("append one line to the spawn", "Immediately after each spawn"):
        check(banned not in os_text,
              f"orchestrate/SKILL.md no longer instructs {banned!r}")
    check("do not hand-write" in os_text.lower(),
          "...it says the ledger is written for you")
    check("zero ledger lines were ever written" in os_text.lower()
          or "zero ledger lines" in os_text.lower(),
          "...and records the measurement that settled it")
    # The in-head per-parent counter, retired with it.
    check("per-parent counter" not in os_text,
          "orchestrate/SKILL.md no longer asks for an in-head per-parent counter")
    check("find_spawns" in os_text,
          "...and names the deterministic bind that covers it instead")


def test_the_ledger_schema_survives_the_mandates_deletion():
    """D-19 retires the INSTRUCTION to write the ledger, not the ledger.

    The first attempt at this rewrite deleted the schema along with the mandate
    and took `tests/test_slots.py` red with it: that block is the only place the
    wire shape `custom_state` ingests is written down, so a hook edit would have
    had no spec to be held to. It is documentation now, not an instruction —
    which is why the banned mandate strings above still have to be absent.
    """
    print("test_the_ledger_schema_survives_the_mandates_deletion")
    os_text = texts().get("orchestrate/SKILL.md", "")
    check("```json" in os_text,
          "orchestrate/SKILL.md still shows the ledger record as JSON")
    for field in ("name", "parent", "root", "role", "attempt", "taskId",
                  "sessionKey", "ts"):
        check(f'"{field}"' in os_text,
              f"...carrying `{field}` (R-53's amendment)")
    check("<pid>-<procStart>" in os_text,
          "...with sessionKey spelled as the composite the session grammar emits")
    check("sessionKeySource" in os_text and '"path"' in os_text,
          "...and the pre-amendment path fallback stated, not left to guess "
          "(CUSTOMSTATE-10)")
    check("spawn-ledger.jsonl" in os_text,
          "...at the path the ingest actually reads")


def test_the_ledger_sentence_matches_what_actually_ships():
    print("test_the_ledger_sentence_matches_what_actually_ships")
    os_text = texts().get("orchestrate/SKILL.md", "")
    if LIFECYCLE_HOOK.is_file():
        check("agent_lifecycle.py" in os_text,
              "orchestrate/SKILL.md names the hook that writes the ledger now "
              "(D-18(c), the probe came back green)")
        check("additive" in os_text,
              "...and that the hook is additive, never the floor (GD-D5)")
        # The hook is inert with no ACTIVE sentinel, and this is the one skill
        # that advertises ad-hoc spawns — "written for you" without the caveat
        # promises a ledger line in the case where nothing writes one.
        check("inert" in os_text and "ACTIVE" in os_text,
              "...and that it is inert without an `ACTIVE` sentinel, so an "
              "ad-hoc spawn outside a run gets no ledger line")
    else:
        skip("hooks/agent_lifecycle.py is not in the payload — the prose must "
             "not promise hook behaviour, and it does not")
        check("agent_lifecycle.py" not in os_text,
              "orchestrate/SKILL.md promises no hook that does not ship")


def test_the_control_loop_is_marked_dormant():
    print("test_the_control_loop_is_marked_dormant")
    os_text = texts().get("orchestrate/SKILL.md", "")
    check("DORMANT" in os_text,
          "orchestrate/SKILL.md marks the control loop DORMANT (D-19)")
    check("Do not poll" in os_text,
          "...with the instruction turned off explicitly")
    # The actual phrasing, not "CONTROL_ROUTES" plus a stray `{}` — the file
    # shows a JSON record now, so the loose form would pass on the ledger block
    # alone and stop pinning anything.
    check("`CONTROL_ROUTES` is `{}`" in os_text,
          "...naming CONTROL_ROUTES as `{}` for the reason (GD-4)")
    # The label channel is NOT dormant — deleting the marker with the loop is
    # the mistake this line exists to prevent.
    check("[touch]" in os_text,
          "...while the [touch] marker stays as the live label channel")


def main():
    print("test_skills_prose.py")
    for t in (test_no_prose_mandates_the_status_pair,
              test_the_deletion_carries_its_measured_evidence,
              test_the_run_close_is_derived_and_the_typed_close_is_demoted,
              test_recovery_prose_points_at_the_written_resume_block,
              test_the_emitters_are_named_and_the_script_emits_nothing,
              test_agentr_is_shipped_not_pasted_in,
              test_no_skill_instructs_editing_constants_into_the_script,
              test_the_driver_recipes_collapsed_onto_touch_run,
              test_settle_replaces_hand_typed_corrections,
              test_the_report_step_is_render_narrate_publish,
              test_the_report_surfaces_are_documented_with_their_defaults,
              test_the_run_specs_seed_the_cards_touch_run_can_know_up_front,
              test_the_method_paragraph_lives_in_the_skill,
              test_the_spawn_ledger_mandate_is_gone,
              test_the_ledger_schema_survives_the_mandates_deletion,
              test_the_ledger_sentence_matches_what_actually_ships,
              test_the_control_loop_is_marked_dormant):
        t()
    print()
    if skips:
        print(f"skipped: {len(skips)} check(s)")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all skills-prose tests passed")


if __name__ == "__main__":
    main()
