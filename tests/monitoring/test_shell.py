#!/usr/bin/env python3
"""Stdlib-only tests for sp-shell fixes (status.sh + implement implement
workflow template + docs). Run as `python3 test_shell.py`; exits non-zero on the first failure.
No pytest, no omnigent imports. Uses ephemeral dirs under /tmp/claude-1000.
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The canonical monitoring module is named through `tests/_roots.py` (GD-U1):
# this file lives in `tests/monitoring/` (GD-U6), the module it asserts about
# does not, and a `parents[N]` hop count is right for exactly one layout.
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _roots import MON, PAYLOAD, REPO                   # noqa: E402

STATUS_SH = MON / "status.sh"
WATCHER_PY = MON / "decision_watcher.py"
# The skills MOVED into the shipping subtree (item 09, GD-T2): one canonical
# copy, in the payload. These constants follow them — the assertions below are
# about the reference protocol's text, wherever that text now lives.
SKILLS = PAYLOAD / "skills"
TEMPLATE = SKILLS / "implement/templates/implement.workflow.js"
RESEARCH_TEMPLATE = SKILLS / "research/templates/research.workflow.js"
MONITORING_MD = MON / "monitoring.md"
M_SKILL = SKILLS / "monitor/SKILL.md"
D_SKILL = SKILLS / "implement/SKILL.md"
GITIGNORE = REPO / ".gitignore"

TMP_ROOT = "/tmp/claude-1000"
os.makedirs(TMP_ROOT, exist_ok=True)


def nearest_claude_marker(start):
    """The nearest ancestor of ``start`` holding a `.claude/`, or None.

    status.sh's third resolver rung walks up looking for exactly this, so any
    arm that means "nothing resolves" has to KNOW its throwaway cwd is isolated.
    Under TMP_ROOT that is an assumption, not a fact — this session's own
    scratchpad already lives at `/tmp/claude-1000/-home-laniakea-Projects-touch/…`,
    one directory away from being a marker — and a silently-flipped arm is worse
    than a skipped one.
    """
    here = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(here, ".claude")):
            return os.path.join(here, ".claude")
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent

failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def run_status(state_dir, args, extra_env=None, unset_state_dir=False, script=None,
               cwd=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("ORCH_STATE_DIR", "ORCH_TITLE", "ORCH_TASKS_ROOT",
                        "CLAUDE_PROJECT_DIR")}
    if not unset_state_dir:
        env["ORCH_STATE_DIR"] = str(state_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script or STATUS_SH), *args],
        env=env, cwd=cwd, capture_output=True, text=True,
    )


# --- status.sh: creates missing state dir + appends one valid JSON line (SHELL-6)
def test_status_creates_state_dir():
    print("test_status_creates_state_dir")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        # A not-yet-created nested dir under the fresh base.
        state_dir = os.path.join(base, "does", "not", "exist", "yet")
        check(not os.path.isdir(state_dir), "state dir does not exist before call")
        proc = run_status(state_dir, ["myplan", "implement", "running", "attempt 1: go"])
        check(proc.returncode == 0, "status.sh exits 0")
        check(os.path.isdir(state_dir), "status.sh created the missing state dir")
        events = os.path.join(state_dir, "events.jsonl")
        check(os.path.isfile(events), "events.jsonl was created")
        lines = Path(events).read_text().splitlines()
        check(len(lines) == 1, f"exactly one event line appended (got {len(lines)})")
        obj = json.loads(lines[0])
        check(obj["plan"] == "myplan" and obj["stage"] == "implement"
              and obj["state"] == "running" and obj["detail"] == "attempt 1: go",
              "event fields round-trip")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- status.sh: hostile detail lands as literal one-line escaped JSON (injection guard)
def test_status_injection_safe():
    print("test_status_injection_safe")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        sentinel = os.path.join(base, "PWNED")
        hostile = f'$(touch {sentinel}) `touch {sentinel}` "quote" and\na newline'
        proc = run_status(state_dir, ["p", "s", "running", hostile])
        check(proc.returncode == 0, "status.sh exits 0 on hostile detail")
        check(not os.path.exists(sentinel), "command substitution did NOT execute (no PWNED file)")
        events = Path(state_dir) / "events.jsonl"
        raw = events.read_text()
        # File must be exactly one physical line (newline escaped inside JSON).
        check(raw.count("\n") == 1, "output is a single physical line (trailing newline only)")
        obj = json.loads(raw.splitlines()[0])
        check(obj["detail"] == hostile, "detail preserved verbatim (incl. newline as literal)")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- status.sh: ORCH_STATE_DIR unset no longer spools into the module dir
#     (item 04 / CM-3). The old behaviour — exit 0, warn, write `events.jsonl`
#     beside the script — was data loss with extra steps, and in a packaged copy
#     it is a write into a version-stamped cache that gets swept. Now: resolve
#     the project's tasks root and use its newest task folder, or exit 2.
#     A COPY of the script in a throwaway dir, and an isolated cwd, so no arm of
#     this test can reach the real module dir or the repo's own task folders.
def test_status_unset_hard_errors():
    print("test_status_unset_hard_errors")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        script_copy = os.path.join(base, "status.sh")
        shutil.copy(STATUS_SH, script_copy)
        isolated = os.path.join(base, "cwd")           # no .claude/ marker above it
        os.makedirs(isolated)
        marker = nearest_claude_marker(isolated)
        if marker:
            print(f"  skip: an ancestor of {TMP_ROOT} holds {marker} — the cwd "
                  "walk-up would resolve and this arm would not mean what it says")
            return
        proc = run_status(None, ["p", "s", "running", "hi"], unset_state_dir=True,
                          script=script_copy, cwd=isolated)
        check(proc.returncode == 2,
              f"status.sh exits 2 when nothing resolves (got {proc.returncode})")
        check("ORCH_STATE_DIR" in proc.stderr,
              "the error names ORCH_STATE_DIR as the fix")
        check(not os.path.isfile(os.path.join(base, "events.jsonl")),
              "NOTHING was written beside the script")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- status.sh: the resolved fallback — $ORCH_TASKS_ROOT's newest task folder,
#     with a loud warning. This is the "project resolution succeeds" arm.
def test_status_unset_resolves_tasks_root():
    print("test_status_unset_resolves_tasks_root")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        script_copy = os.path.join(base, "status.sh")
        shutil.copy(STATUS_SH, script_copy)
        isolated = os.path.join(base, "cwd")
        os.makedirs(isolated)
        tasks_root = os.path.join(base, "tasks")
        older = os.path.join(tasks_root, "old-task")
        newer = os.path.join(tasks_root, "new-task")
        for d in (older, newer):
            os.makedirs(d)
            Path(d, "events.jsonl").write_text("")
        # make the newest unambiguous regardless of filesystem timestamp
        # granularity: `ls -t` orders by mtime.
        os.utime(os.path.join(older, "events.jsonl"), (1000, 1000))
        os.utime(os.path.join(newer, "events.jsonl"), (2000, 2000))
        proc = run_status(None, ["p", "s", "running", "hi"], unset_state_dir=True,
                          script=script_copy, cwd=isolated,
                          extra_env={"ORCH_TASKS_ROOT": tasks_root})
        check(proc.returncode == 0,
              f"status.sh exits 0 once a tasks root resolves (got {proc.returncode})")
        check("ORCH_STATE_DIR unset" in proc.stderr,
              "the fallback still warns loudly on stderr")
        lines = Path(newer, "events.jsonl").read_text().splitlines()
        check(len(lines) == 1, "the event landed in the NEWEST task folder")
        check(Path(older, "events.jsonl").read_text() == "",
              "the older task folder was left alone")
        check(not os.path.isfile(os.path.join(base, "events.jsonl")),
              "nothing was written beside the script")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- status.sh: unconditional refusal to write inside an installed plugin.
#     The plugin root is version-stamped, re-copied on update and swept ~14 days
#     later, so a stream written there is a stream that silently disappears.
def test_status_refuses_a_plugin_cache():
    print("test_status_refuses_a_plugin_cache")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        plugin_root = os.path.join(base, "cache", "msdrx-tools", "touch", "0.1.0")
        os.makedirs(os.path.join(plugin_root, ".claude-plugin"))
        Path(plugin_root, ".claude-plugin", "plugin.json").write_text('{"name":"touch"}')
        state_dir = os.path.join(plugin_root, "shared", "monitoring")
        proc = run_status(state_dir, ["p", "s", "running", "hi"])
        check(proc.returncode == 2,
              f"status.sh exits 2 inside a plugin cache (got {proc.returncode})")
        check("plugin cache" in proc.stderr, "the refusal says why")
        check(not os.path.exists(os.path.join(state_dir, "events.jsonl")),
              "no events.jsonl was created inside the plugin cache")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- status.sh: the two walk-up loops take the parent with parameter expansion,
#     not a `dirname` fork. status.sh is the hottest script in the module (every
#     agent, several times per stage) and both loops run to "/", so a fork per
#     ancestor level was ~7 processes on EVERY call for a string operation bash
#     already does. Behaviour is asserted alongside the source-text rule: a
#     rewrite that broke the walk-up would otherwise pass by deleting dirname.
def test_status_walk_up_is_forkless_and_still_walks():
    print("test_status_walk_up_is_forkless_and_still_walks")
    src = STATUS_SH.read_text()
    # the two resolver FUNCTIONS only. The one `dirname` left in the file sits
    # on the unset-ORCH_STATE_DIR fallback path, runs once, and already follows
    # an `ls -t` fork — that is not the hot loop this rule is about.
    body = src[src.index("resolve_tasks_root()"):src.index('if [ -n "${ORCH_STATE_DIR:-}" ]')]
    # code only — the comment above the loops NAMES the fork it replaced, and a
    # prose mention must not read as the fork itself
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    check("$(dirname" not in code,
          "no dirname subshell survives in either walk-up loop")
    check(code.count('p="${d%/*}"; [ -z "$p" ] && p=/') == 2,
          "both loops take the parent by parameter expansion")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        # ...and it still reaches a marker several levels up (the walk-up rung).
        # G10: the MARKER dir and the STATE dir are deliberately different — the
        # walk-up looks for `.claude/` (that is what marks a Claude Code project;
        # `.touch/` is created by Touch and gitignored, so it cannot mark one) and
        # then joins `.touch/local-orchestrators`. Both dirs exist here, and the
        # assertion is that the event lands under `.touch/`, not `.claude/`.
        proj = os.path.join(base, "proj")
        deep = os.path.join(proj, "a", "b", "c", "d")
        os.makedirs(deep)
        os.makedirs(os.path.join(proj, ".claude"))          # the marker only
        tasks = os.path.join(proj, ".touch", "local-orchestrators", "t")
        os.makedirs(tasks)
        Path(tasks, "events.jsonl").write_text("")
        proc = run_status(None, ["p", "s", "running", "hi"], unset_state_dir=True,
                          cwd=deep)
        check(proc.returncode == 0,
              f"the cwd walk-up still finds a .claude/ 5 levels up ({proc.stderr})")
        check(len(Path(tasks, "events.jsonl").read_text().splitlines()) == 1,
              "the event landed in the resolved task folder under .touch/")
        check(not os.path.exists(
                  os.path.join(proj, ".claude", "local-orchestrators")),
              "nothing was created under the MARKER dir .claude/")
        # ...and the plugin-cache walk-up still refuses several levels down.
        plugin = os.path.join(base, "cache", "touch", "0.1.0")
        os.makedirs(os.path.join(plugin, ".claude-plugin"))
        Path(plugin, ".claude-plugin", "plugin.json").write_text('{"name":"touch"}')
        state = os.path.join(plugin, "a", "b", "c", "state")
        proc = run_status(state, ["p", "s", "running", "hi"])
        check(proc.returncode == 2,
              f"the plugin-cache walk-up still refuses (got {proc.returncode})")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- status.sh: the ladder is THREE rungs and joins `.touch/local-orchestrators`
#     (G10). Rung 1 ($ORCH_TASKS_ROOT) is covered by
#     test_status_unset_resolves_tasks_root above; this covers rung 2
#     ($CLAUDE_PROJECT_DIR) and the DELETION of the former fourth, module-relative
#     rung ($DIR/../../local-orchestrators). That rung was already dead after
#     GD-U1 — nothing sits two levels above the monitoring module in the payload —
#     and in an installed copy it would glob whatever sits beside the plugin, so
#     its removal is asserted behaviourally, not only as absent source text: a
#     sibling tree that WOULD have resolved must now be ignored and the call must
#     refuse (exit 2) rather than write somewhere nobody reads.
def test_status_ladder_is_three_rungs():
    print("test_status_ladder_is_three_rungs")
    src = STATUS_SH.read_text()
    # code only, for the same reason the forkless test above filters comments:
    # the docstring NAMES the deleted rung so a reader knows why it is not there,
    # and a prose mention must not read as the rung itself.
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    check("../../local-orchestrators" not in code,
          "the module-relative fourth rung is gone from the source")
    check(".claude/local-orchestrators" not in code,
          "no rung joins the old .claude/ tasks root any more")
    check(code.count(".touch/local-orchestrators") == 2,
          "rungs 2 and 3 both join .touch/local-orchestrators")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        isolated = os.path.join(base, "cwd")
        os.makedirs(isolated)
        marker = nearest_claude_marker(isolated)
        if marker:
            print(f"  skip: an ancestor of {TMP_ROOT} holds {marker} — the cwd "
                  "walk-up would resolve and these arms would not mean what they say")
            return
        # rung 2: $CLAUDE_PROJECT_DIR, joined with .touch/local-orchestrators.
        # No `.claude/` marker is created at all, so only this rung can answer.
        proj = os.path.join(base, "proj")
        tasks = os.path.join(proj, ".touch", "local-orchestrators", "t")
        os.makedirs(tasks)
        Path(tasks, "events.jsonl").write_text("")
        proc = run_status(None, ["p", "s", "running", "hi"], unset_state_dir=True,
                          cwd=isolated,
                          extra_env={"CLAUDE_PROJECT_DIR": proj})
        check(proc.returncode == 0,
              f"$CLAUDE_PROJECT_DIR resolves the tasks root ({proc.stderr})")
        check(len(Path(tasks, "events.jsonl").read_text().splitlines()) == 1,
              "the event landed under $CLAUDE_PROJECT_DIR/.touch/local-orchestrators")
        # the deleted rung: a copy of the script two levels below a sibling tree
        # that the OLD `$DIR/../../local-orchestrators` rung would have found.
        pkg = os.path.join(base, "pkg")
        mod = os.path.join(pkg, "shared", "monitoring")
        os.makedirs(mod)
        script_copy = os.path.join(mod, "status.sh")
        shutil.copy(STATUS_SH, script_copy)
        sibling = os.path.join(pkg, "local-orchestrators", "t")
        os.makedirs(sibling)
        Path(sibling, "events.jsonl").write_text("")
        proc = run_status(None, ["p", "s", "running", "hi"], unset_state_dir=True,
                          script=script_copy, cwd=isolated)
        check(proc.returncode == 2,
              f"the module-relative rung no longer resolves (got {proc.returncode})")
        check(Path(sibling, "events.jsonl").read_text() == "",
              "nothing was written into the module's sibling tree")
    finally:
        shutil.rmtree(base, ignore_errors=True)


#: Words that mark a mention of the deleted rung as HISTORY rather than as a
#: live fallback. Same bargain `test_status_ladder_is_three_rungs` above makes
#: for `status.sh`'s own docstring: a comment that cannot say "this used to
#: exist and does not any more" is a comment that leaves the next reader to
#: rediscover the deletion.
RUNG_RETIRED = ("delet", "no fourth", "not four", "gone", "no longer",
                "removed", "does not exist")


def test_no_wrapper_documents_a_fourth_resolver_rung():
    """D-25: the first file a debugger opens must not document a dead fallback.

    Three `bin/` wrappers described the tasks-root resolver as "the four rungs
    both daemons use", ending in "a module-relative legacy path kept only if it
    already exists". That rung was DELETED with GD-U1 — `status.sh`'s
    `resolve_tasks_root()` has three — and the arm above proves the deletion in
    the code. This one proves nobody is still promising it in prose, which is
    the half a source-of-truth test cannot see: a wrapper is the first thing
    read when events land nowhere, and it was sending readers to look for a
    sibling-directory fallback that cannot fire.

    Sentence-scoped, not file-scoped. Every wrapper that mentions the deleted
    rung SHOULD say it is deleted — that is the useful comment — so the guard
    fires only on a sentence that names it without retiring it.
    """
    print("test_no_wrapper_documents_a_fourth_resolver_rung")
    wrappers = sorted(p for p in (PAYLOAD / "bin").iterdir() if p.is_file())
    # Pinned at seven, not `>= 6`: D-13 made it seven (the six a session runs
    # plus `touch-selfcheck`), and a floor would stay green if one vanished —
    # which would also silently shrink what the fourth-rung scan below covers.
    check(len(wrappers) == 7,
          f"the bin/ wrappers were found (7 expected, {len(wrappers)} present: "
          f"{', '.join(p.name for p in wrappers)})")
    live_claims, ladders = [], []
    for wrapper in wrappers:
        text = wrapper.read_text(encoding="utf-8", errors="replace")
        # Comments only: this is a claim about documentation, and the code the
        # wrappers run resolves nothing (that is the whole design — one
        # resolver, in the callee).
        comments = " ".join(ln.lstrip().lstrip("#").strip()
                            for ln in text.splitlines()
                            if ln.lstrip().startswith("#"))
        flat = re.sub(r"\s+", " ", comments)
        for sentence in re.split(r"(?<=[.!?])\s+", flat):
            low = sentence.lower()
            names_it = ("../../local-orchestrators" in low
                        or "module-relative" in low
                        or "four rungs" in low or "fourth rung" in low)
            if not names_it:
                continue
            if any(word in low for word in RUNG_RETIRED):
                continue
            live_claims.append(f"{wrapper.name}: {sentence.strip()[:90]}")
        # The positive half, scoped to the wrappers that ENUMERATE the ladder —
        # marked by naming its first rung, `$ORCH_TASKS_ROOT`, in a comment.
        # "tasks root" + "rung" alone is too wide: `touch-run` and
        # `touch-selfcheck` say "rung" about `wf_dir` discovery, close-out rungs
        # and delegation ("never copied — a second copy would pin the callee's
        # first rung"), which is the CORRECT thing for a wrapper to say and must
        # not be forced to recite a count it deliberately does not restate.
        if "$ORCH_TASKS_ROOT" in flat and "rung" in flat.lower():
            ladders.append(wrapper.name)
            # "three rungs", or a comment that enumerates them and then counts
            # what it listed ("any of the three") — `touch-selfcheck` spells it
            # the second way and is not wrong, so the guard reads for the COUNT,
            # not for one phrasing of it.
            low = flat.lower()
            check("three rung" in low or "the three" in low
                  or "all three" in low,
                  f"{wrapper.name} says the tasks-root ladder has THREE rungs")
            check("status.sh" in flat,
                  f"{wrapper.name} names shared/monitoring/status.sh as the "
                  f"ladder's owner, rather than restating it as its own")
    check(not live_claims,
          f"no bin/ wrapper documents the deleted module-relative fourth rung "
          f"as if it still resolved (bad: {live_claims})")
    check(len(ladders) >= 3,
          f"the wrappers that describe the tasks-root ladder were found and "
          f"checked (found {ladders})")


# --- status.sh: ORCH_PLANS_TOTAL declares the run's plan-card total (additive,
#     best-effort like ORCH_TITLE: garbage warns and is omitted, never fails)
def test_status_plans_total():
    print("test_status_plans_total")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        events = Path(state_dir) / "events.jsonl"
        proc = run_status(state_dir, ["divide", "plan", "done", "15 sub-plans"],
                          extra_env={"ORCH_PLANS_TOTAL": "17"})
        check(proc.returncode == 0, "status.sh exits 0 with ORCH_PLANS_TOTAL set")
        obj = json.loads(events.read_text().splitlines()[-1])
        check(obj.get("plans_total") == 17, "plans_total lands as an integer")
        check(obj["plan"] == "divide" and obj["state"] == "done",
              "five-key core event shape preserved alongside plans_total")
        proc = run_status(state_dir, ["p", "s", "info", "hi"],
                          extra_env={"ORCH_PLANS_TOTAL": "not-a-number"})
        check(proc.returncode == 0, "a garbage total does not fail the caller")
        check("ORCH_PLANS_TOTAL" in proc.stderr, "a garbage total warns on stderr")
        obj = json.loads(events.read_text().splitlines()[-1])
        check("plans_total" not in obj, "a garbage total is omitted, event still appended")
        proc = run_status(state_dir, ["p", "s", "info", "hi"])
        obj = json.loads(events.read_text().splitlines()[-1])
        check("plans_total" not in obj, "unset env leaves the key out entirely")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- loop.workflow.js static assertions
def test_template_static():
    print("test_template_static")
    src = TEMPLATE.read_text()
    # SHELL-2 / D2: Test marker is role=test, and no gate:run remains in the reference loop.
    check("stage=test role=test attempt=" in src, "Test marker line reads role=test")
    check("role=gate:run" not in src, "no role=gate:run remains in the reference loop")
    # SHELL-10 used to pin `statusCmd`'s quoting, because the prompt text it
    # built carried agent-authored values (${TASK}, ${plan}) into an agent's
    # shell. D-09 deleted the mandated FIRST/LAST calls those strings existed
    # for, and D-10 deleted the script-side emitter beside them, so there is no
    # status command in either template to quote. The quoting risk is gone
    # because the construct is gone — assert THAT, not a shape that no longer
    # exists. (`tests/test_skills_payload.py` owns the full D-09/D-10 pins.)
    check("const statusCmd" not in src and 'ORCH_STATE_DIR="' not in src,
          "no template-built status command survives (D-09/D-10)")
    # SHELL-8, superseded by the infra guard: a dead gate used to be laundered
    # into a fabricated `{ passed: false, summary: 'gate agent died' }` red via
    # a placeholder findings file — an attempt spent on infrastructure. The
    # 2026-07-29 outage run showed where that road goes (whole caps burned with
    # zero substantive verdicts), so the death paths are GONE: every spawn now
    # rides `agentR` — same-attempt retries, then a clean run stop with
    # attempts preserved. These arms pin the replacement and the absence.
    check("findings_file: ''" not in src and 'findings_file: ""' not in src,
          "no empty-string findings_file fallback remains")
    check("writePlaceholderFindings" not in src,
          "the fabricated died-gate placeholder path is gone")
    check("const agentR = async" in src,
          "the agentR infrastructure guard replaces the death fallbacks")
    check("summary: 'gate agent died'" not in src
          and "summary: 'critique agent died'" not in src,
          "no fabricated 'agent died' verdicts remain in the loop")


# --- docs static assertions
def test_docs_static():
    print("test_docs_static")
    md = MONITORING_MD.read_text()
    ms = M_SKILL.read_text()
    ds = D_SKILL.read_text()

    # cache_write in token schema blocks of both docs.
    check("cache_write" in md, "monitoring.md documents cache_write")
    check("cache_write" in ms, "monitor SKILL.md documents cache_write")
    # stale in the state enum of both docs.
    check("failed|info|stale" in md, "monitoring.md state enum includes stale")
    check("done|failed|info|stale" in ms, "monitor SKILL.md state enum includes stale")
    # files_changed added to the shape-key list in both docs.
    check("fixed_ids`/`files_changed`" in md, "monitoring.md shape list includes files_changed")
    check("fixed_ids`/`files_changed`" in ms, "monitor SKILL.md shape list includes files_changed")
    # agent sub-object documented in both docs.
    check('"agent"' in md and '"runtime"' in md, "monitoring.md documents the agent sub-object")
    check('"agent"' in ms and '"runtime"' in ms, "monitor SKILL.md documents the agent sub-object")
    # config-driven caps noted (D4/#11) in monitoring.md and implement SKILL.md.
    check("max_gate_attempts" in md, "monitoring.md notes config attempt caps")
    check("max_gate_attempts" in ds, "implement SKILL.md notes config attempt caps")
    # M2: the config is re-read while the watcher runs (it starts before the
    # orchestrator script that publishes the caps).
    check("re-reads this file" in md,
          "monitoring.md says the watcher re-reads orch-config.json while running")
    # m4 / R-13: the agent sub-object's real key set, and what `id` means now.
    for key in ('"shortId"', '"identity"', '"flags"', '"unconventional"'):
        check(key in md, f"monitoring.md documents the agent block key {key}")
    check("full 17-hex agentId" in md and "shortId`" in md,
          "monitoring.md pins identity to the full agentId, shortId as display only")
    check("legacy:<task>:<id8>" in md,
          "monitoring.md records the 8-hex legacy id consequence for readers")
    # R-40 lifecycle: what stops a watcher, what does not, and the escape hatch.
    check("stop its watcher" in md, "monitoring.md states the run-close/stop rule")
    check("ORCH_EXIT_QUIET_SECS" in md and "ORCH_ABANDON_QUIET_SECS" in md
          and "ORCH_NO_SELF_EXIT" in md,
          "monitoring.md documents both exit windows and the opt-out")
    check('`"w":"agent"`' in md or '(`"w":"agent"`)' in md,
          "monitoring.md says only a script-written close authorizes the exit")
    check("watcher.pid" in md and "echo $!" in md,
          "monitoring.md documents the watcher.pid launch-side half")
    # R-08/GD-10: the doc must not still promise the retired sequenced close.
    check("sequenced close is **retired" in md and "closed, no verdict" in md,
          "monitoring.md records the retired sequenced close + no-verdict close")
    check("serial advance ->" in md,
          "monitoring.md documents the legacy sequenced close's own detail text")
    # n-2: with both templates publishing `parallel`/`sequential`, the watcher's
    # STRATEGY=="serial" branch has no live producer. Say so, or a future reader
    # "fixes" a template to emit `serial` and resurrects R-58.
    check("no reference template publishes it" in md,
          "monitoring.md marks the legacy `serial` branch as legacy-config-only")
    # M-1: what may and may not cancel the driver's close — the doc described the
    # intended behavior, not the shipped one, for four attempts.
    check("plan card MOVING" in md and "does not" in md,
          "monitoring.md states that a plan card CLOSING is not liveness")
    # M-2: signalling the watcher is safe because it drains first.
    check("ORCH_DRAIN_SECS" in md and "DRAIN" in md,
          "monitoring.md documents the shutdown drain and its window")
    # M1/M14 (WRITE-SIDE-12/-13): the token-tick cadence knob, under BOTH
    # spellings — the env var that PINS it and the orch-config key the watcher
    # re-reads live. An operator who finds only one of them cannot tune a run.
    check("ORCH_TOKEN_TICK_SECS" in md,
          "monitoring.md documents the ORCH_TOKEN_TICK_SECS env knob")
    check("token_tick_secs" in md,
          "monitoring.md documents the token_tick_secs orch-config key")
    # The cadence is a CEILING. A future reader who mistakes it for a heartbeat
    # interval erases every stall segment the timeplan derives from gaps.
    check("ceiling, never a floor" in md,
          "monitoring.md states the cadence is a ceiling, not a heartbeat")
    # M14 (DATA-MODEL-11): the token asymmetry. `tokens` is a delta and
    # `agent.tokens` is absolute; the doc used to say so for only one of them,
    # and a reader who assumes symmetry is wrong in one direction or the other.
    check("**delta** (not absolute)" in md,
          "monitoring.md still says the top-level tokens value is a delta")
    check("ABSOLUTE running total, last-event-wins" in md,
          "monitoring.md says agent.tokens is the absolute running total")
    # M14 (WS-PROTOCOL-11): the reserved control key and the v1/v2 framing. The
    # file schema is normative and unchanged; `m` is the one name spent so that
    # additive EVENT keys stay legal.
    check("Wire framing" in md,
          "monitoring.md has a wire-framing section for the /ws protocol")
    check("Reserved control key `m`" in md and "Events never carry `m`" in md,
          "monitoring.md reserves the control key `m` and bars it from events")
    check("additive event keys remain legal" in md,
          "monitoring.md keeps additive event keys legal beside the reserved key")
    check("server-declared, never sniffed" in md,
          "monitoring.md pins version negotiation to the server's first frame")
    # The refusal is quoted by the DISCRIMINATOR the server really sends, not
    # by an abbreviated frame literal a reader could turn into a wrong assert:
    # the shipped hello also carries v/task/foldGen (`_stream_v2`'s
    # unknown-task branch).
    check('"error":"unknown-task"' in md,
          "monitoring.md documents the v2 unknown-task refusal")
    # Its ENVELOPE is pinned by names in proximity, not by the 62 characters of
    # contiguous pseudo-JSON the doc happens to print today: the literal sits
    # inside a hand-wrapped numbered list, and re-wrapping it — or spelling the
    # placeholder `"task":"<name>"` instead of `"task":…` — must not fail a test
    # whose subject is still true. What matters is that the refusal is shown as
    # a `hello` carrying the fold generation, not as a bare error string.
    check(re.search(r'"m":"hello".{0,60}"error":"unknown-task"', md, re.S)
          and '"foldGen"' in md,
          "monitoring.md prints the unknown-task refusal as a hello with foldGen")
    # Whitespace-normalised: the sentence is prose and may re-wrap; what must
    # not change is that the catalogue is closed at these four names TODAY
    # while unknown `m` values stay ignorable — a reader that treats an
    # unrecognised control frame as an event breaks on the next added shape.
    check(re.search(r"control catalogue is exactly four shapes — `hello`,\s+"
                    r"`snapshot`,\s+`tail`,\s+`cursor`", md),
          "monitoring.md enumerates the four v2 control frames")
    check(re.search(r"\*\*ignore\*\* any other\s+`m` value rather than treat "
                    r"it as an event", md),
          "monitoring.md keeps the `m` space forward-compatible for readers")
    # The resume rule: content sig + BYTE offset, and a wipe-and-rerun is
    # refused rather than silently tailing a foreign stream at a stale offset.
    # Pinned by the two JSON NAMES, not by a pseudo-JSON spelling: the doc
    # prints proper `"key": value` literals, and an assert keyed on the exact
    # punctuation would break on any later reflow of the same true claim.
    check("sig-mismatch" in md and "fromApplied" in md,
          "monitoring.md ties wipe-and-rerun to the sig-based resume refusal")
    check(re.search(r"`offset` is a\s+\*\*byte\*\* offset — never a line number",
                    md),
          "monitoring.md pins the cursor offset to bytes, not line numbers")
    # A digest over less than SIG_BYTES is not an identity yet (`_scan`'s
    # `sig_short` / `sig_is_identity` pair) — a doc that omits this teaches a
    # reader to trust a young stream's sig across an append.
    check("sig_short" in md,
          "monitoring.md notes the short-head sig caveat")
    # WS-PROTOCOL-14: the tail poll is no longer a fixed 0.5 s. The doc must
    # not pin a constant the server contradicts after 60 s of quiet.
    check(re.search(r"0\.5 s while the stream is moving and back off to\s+"
                    r"2 s after ~60 s of quiet", md),
          "monitoring.md documents the idle poll backoff, not a fixed 0.5 s")
    # The v1/v2 switch is `params.get("v") == "2"`, so `v=1`, `v=3` and
    # `v=banana` all take the v1 path. "no `v` in the query" read as if only the
    # absent parameter did, leaving a client that pins `v=1` — the natural
    # reading of "protocol v1" — undocumented.
    check(re.search(r"\*\*v1 — anything but `v=2` in the query", md),
          "monitoring.md scopes v1 to anything but the exact string v=2")
    # There is NO truncation frame: `read_events`' `-1` is server-internal, it
    # trips `_reset`, which sets `sub.closed`; the socket then gets the same
    # bare CLOSE any teardown sends. A client implementer must not go hunting
    # for a sentinel frame that never travels.
    check(re.search(r"no sentinel\s+\*frame\* on the wire", md)
          and "server-internal" in md,
          "monitoring.md does not invent a truncation sentinel frame")
    # `snap` grammar: `1` is legal AND the default, and an unrecognised value is
    # coerced to `1` and named in `ignored` — the hello paragraph promises every
    # unhonoured parameter is named, so the one that can be silently rewritten
    # has to say so.
    check("[&snap=0|1|verify]" in md,
          "monitoring.md's v2 grammar admits the default snap=1")
    check(re.search(r"an unrecognised `snap` value falls back to\s+`1` and is "
                    r"listed in the hello's `ignored`", md),
          "monitoring.md states the snap coercion and its disclosure")
    # An accepted resume is NOT "no frames": the gap between the client's
    # cursor and the server's offset still travels as ordinary array frames
    # (`_stream_v2`'s `if from_applied:` branch sets `replay_from`). The old
    # wording said "neither".
    check(re.search(r"An accepted resume sends no snapshot; if\s+the client's "
                    r"cursor is behind the server's offset, the gap travels "
                    r"first as\s+ordinary array frames", md),
          "monitoring.md states that an accepted resume still ships the gap")
    # The cursor follows every tick that CONSUMED events, poison included
    # (`_tail_loop_client`'s `if sent and v2:` guard) — a reader keyed on
    # "n > 0" resumes stale.
    check(re.search(r"after every tick that \*\*consumed\*\* events", md)
          and "`n: 0`" in md,
          "monitoring.md ties the cursor frame to consumed, not delivered, events")
    # `0` disables the ceiling; it does not turn the watcher into a heartbeat.
    check(re.search(r"a line on every poll\s+tick that has a non-zero delta",
                    md),
          "monitoring.md keeps `0` behind the non-zero-delta guard")
    # A rejected resume gets the prelude ITS MODE calls for — under `snap=0`
    # that is raw history, no snapshot at all (`_stream_v2`'s else-branch sets
    # `replay_from = 0`). "Always a snapshot" is wrong in the one mode an
    # operator reaches for when a resume is misbehaving.
    check(re.search(r"answered with the full\s+prelude the mode calls for", md),
          "monitoring.md does not promise a snapshot for every rejected resume")
    # M14/M1 cross-section consistency: the TIMEPLAN's cadence rationale and
    # the Token-math knob description must quote the SAME ceiling. They sit
    # ~40 lines apart and the old "every few seconds" line survived one edit
    # pass while its sibling was fixed — the two readings cannot both be true,
    # and an operator holding the stale one mis-reads short gaps as outages.
    check(re.search(r"Token ticks land at most once per\s+agent per "
                    r"`token_tick_secs` \(default 15 s\)", md),
          "monitoring.md's timeplan section quotes the real tick ceiling")
    # The Token-math half needs its OWN verbatim pin: a count alone cannot
    # detect the half-applied fix it is supposed to guard (the token occurs in
    # the orch-config row and the quiet bullet too, so a `>= 2` survives
    # deleting BOTH sections this assert names).
    check(re.search(r"Live ticks are throttled per agent\s+"
                    r"\(`token_tick_secs`, default 15 s\)", md),
          "monitoring.md's Token-math section quotes the same tick ceiling")
    check(md.count("`token_tick_secs`") >= 3,
          "monitoring.md names token_tick_secs in the config row, the timeplan "
          "and token math")
    # The THIRD statement of the same ceiling: the Timestamps bullet. It used to
    # promise "live events lag ≤1 s", which the shipped cadence misses by 15× —
    # the ceiling gates the transcript READ (`token_tick_due` sits before
    # `agent_tokens`) and the tick is emitted with no `ts`, so `emit()` stamps
    # the observation moment. 91 % of a measured stream is `stage=="tokens"`,
    # so this is the common case, not an edge one.
    check("live events lag ≤1 s" not in md,
          "monitoring.md no longer promises a ≤1 s stamp for every live event")
    check(re.search(r"a token tick up\s+to `token_tick_secs` \(default 15 s\) "
                    r"after the transcript growth", md),
          "monitoring.md's Timestamps bullet dates a token tick by the ceiling")
    # …and the socket poll is attributed to the SOCKET. `POLL_SECS`/
    # `IDLE_POLL_SECS` are monitor_server.py constants: they change when the
    # page is told, never a timestamp already on disk (the watcher's own
    # journal poll is `poll_sleep(seconds=1.0)`, deliberately left at 1 s).
    check(re.search(r"Delivery to the page adds the socket poll", md)
          and re.search(r"cannot move a stamp already\s+written to disk", md),
          "monitoring.md separates delivery latency from the watcher's stamp")
    # An accepted resume skips the SNAPSHOT, not "the prelude": `hello` and the
    # ONE `{"m":"tail",…}` boundary are written unconditionally (`_stream_v2`).
    # The Behavior-notes summary is what most readers actually read, so it must
    # not restate the error the normative section above it already corrects.
    check(re.search(r"carries a valid cursor skips the \*\*snapshot\*\*", md)
          and "boundary frame, then resumes the tail" in md,
          "monitoring.md's reconnect summary skips the snapshot, not the prelude")
    # The LAST "full replay on connect" claim in the file (the completed-tasks
    # bullet) describes the same act as the connect bullet 60 lines above it,
    # which v2 turned into a hydration. Both or neither — a survivor here is the
    # same half-applied-fix shape as the timeplan/token-math pair above.
    check(re.search(r"hydrates \(or, on a\s+v1 socket or under `\?snap=0`, "
                    r"replays\) the full event history on connect", md),
          "monitoring.md's never-delete bullet describes hydration, not replay")
    check(re.search(r"replays the full\s+event history on connect", md) is None,
          "no unconditional full-replay-on-connect claim survives in monitoring.md")


# --- R-39: every status.sh line is attributed to its writer
def test_status_writer_attribution():
    print("test_status_writer_attribution")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        proc = run_status(state_dir, ["p", "s", "running", "hi"])
        check(proc.returncode == 0, "status.sh exits 0")
        obj = json.loads((Path(state_dir) / "events.jsonl").read_text().splitlines()[0])
        check(obj.get("w") == "agent", 'agent-written line carries "w":"agent"')
        # Additive: the five-key core shape is unchanged.
        check(all(k in obj for k in ("ts", "plan", "stage", "state", "detail")),
              "five-key core event shape preserved alongside w")
        # ORCH_TITLE still rides along (no key was displaced).
        proc = run_status(state_dir, ["p", "plan", "queued", "seeded"],
                          extra_env={"ORCH_TITLE": "Phase 1"})
        titled = json.loads((Path(state_dir) / "events.jsonl").read_text().splitlines()[1])
        check(titled.get("title") == "Phase 1" and titled.get("w") == "agent",
              "title and w coexist")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- R-10 / GD-11: detail capped at 1 KB at the writer
def test_status_detail_cap():
    print("test_status_detail_cap")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        run_status(state_dir, ["p", "s", "info", "z" * 9000])
        obj = json.loads((Path(state_dir) / "events.jsonl").read_text().splitlines()[0])
        check(len(obj["detail"]) == 1024, f"detail capped at 1 KB (got {len(obj['detail'])})")
        check(obj["detail"].endswith("..."), "truncation is visible in the detail")
        run_status(state_dir, ["p", "s", "info", "short"])
        obj2 = json.loads((Path(state_dir) / "events.jsonl").read_text().splitlines()[1])
        check(obj2["detail"] == "short", "a short detail is untouched")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- R-10: out-of-enum state warns but still appends (best-effort writer)
def test_status_bad_state_warns_but_writes():
    print("test_status_bad_state_warns_but_writes")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        proc = run_status(state_dir, ["p", "s", "exploded", "odd"])
        check(proc.returncode == 0, "unknown state does not fail the caller")
        check("unknown state" in proc.stderr, "unknown state warns on stderr")
        obj = json.loads((Path(state_dir) / "events.jsonl").read_text().splitlines()[0])
        check(obj["state"] == "exploded", "the event is still appended verbatim")
        for good in ("queued", "running", "done", "failed", "info", "stale"):
            p = run_status(state_dir, ["p", "s", good, "x"])
            check("unknown state" not in p.stderr, f"'{good}' is in the enum (no warning)")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- R-10: concurrent appends lose/tear no line (smoke test, NOT the lock guard)
def test_status_concurrent_appends_are_atomic():
    print("test_status_concurrent_appends_are_atomic")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        os.makedirs(state_dir)
        env = {k: v for k, v in os.environ.items() if k != "ORCH_TITLE"}
        env["ORCH_STATE_DIR"] = state_dir
        writers = 24
        # NOTE (M-3): this arm does NOT prove the lock. GD-11's writer-side cap
        # truncates every detail to 1 KB BEFORE the write, so 9000 chars in means
        # a ~1.1 KB line out — comfortably inside one atomic append, and this
        # scenario passes verbatim with fcntl.flock deleted from status.sh
        # (measured). R-10's stated ">8 KiB per writer" acceptance test is
        # unsatisfiable once the cap exists. What this arm does prove is that 24
        # concurrent writers lose no line, duplicate none and leave no torn tail;
        # the lock itself is guarded behaviorally by
        # test_status_append_waits_for_the_lock and at the source by
        # test_append_sites_take_lock_ex.
        procs = [subprocess.Popen(
            ["bash", str(STATUS_SH), f"plan{i}", "stage", "running",
             f"{i}-" + "d" * 9000],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for i in range(writers)]
        for p in procs:
            p.wait()
        raw = (Path(state_dir) / "events.jsonl").read_text()
        lines = raw.splitlines()
        check(len(lines) == writers, f"every writer's line survived ({len(lines)}/{writers})")
        check(raw.endswith("\n"), "file ends on a line boundary (no torn tail)")
        plans = set()
        torn = 0
        for ln in lines:
            try:
                ev = json.loads(ln)
            except json.JSONDecodeError:
                torn += 1
                continue
            plans.add(ev["plan"])
            if len(ev["detail"]) > 1024:
                torn += 1
        check(torn == 0, "zero torn/unparseable lines")
        check(len(plans) == writers, f"all {writers} distinct writers present")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- R-10 (M-3): the append really takes LOCK_EX — a contended writer WAITS
def test_status_append_waits_for_the_lock():
    print("test_status_append_waits_for_the_lock")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        os.makedirs(state_dir)
        events = Path(state_dir) / "events.jsonl"
        events.write_text("")
        env = {k: v for k, v in os.environ.items() if k != "ORCH_TITLE"}
        env["ORCH_STATE_DIR"] = state_dir
        # Hold LOCK_EX on the events file from THIS process, then start one
        # status.sh. If the writer takes the lock it must block; if the lock were
        # removed it would append immediately — which is exactly the difference
        # the 24-writer arm above cannot see.
        with open(events, "a") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            proc = subprocess.Popen(
                ["bash", str(STATUS_SH), "locked", "stage", "running", "held"],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            try:
                proc.wait(timeout=1.5)
                blocked = False
            except subprocess.TimeoutExpired:
                blocked = True
            check(blocked, "a status.sh append BLOCKS while the events lock is held")
            check(events.read_text() == "",
                  "nothing was appended behind the held lock")
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        # Released: the writer must now complete on its own and land its line.
        try:
            proc.wait(timeout=10)
            completed = True
        except subprocess.TimeoutExpired:  # pragma: no cover - would hang the suite
            proc.kill()
            completed = False
        check(completed and proc.returncode == 0,
              "the writer completes once the lock is released")
        lines = [ln for ln in events.read_text().splitlines() if ln.strip()]
        check(len(lines) == 1 and json.loads(lines[0])["plan"] == "locked",
              "the queued line lands exactly once, after the release")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- R-10 (M-3): both append sites take LOCK_EX, and both tolerate no fcntl (m-4)
def test_append_sites_take_lock_ex():
    print("test_append_sites_take_lock_ex")
    status_src = STATUS_SH.read_text()
    watcher_src = WATCHER_PY.read_text()
    for name, src in (("status.sh", status_src), ("decision_watcher.py", watcher_src)):
        check("fcntl.flock" in src and "LOCK_EX" in src,
              f"{name}: the append takes an exclusive flock")
        check("LOCK_UN" in src, f"{name}: and releases it")
        # m-4: a best-effort writer must not be killed by a missing fcntl. Both
        # writers degrade to an unlocked append instead of failing every call.
        check(bool(re.search(r"try:[^\n]*\n(?:\s*#[^\n]*\n)*\s*import fcntl\b", src))
              and "except ImportError" in src,
              f"{name}: fcntl is imported defensively, not hard-required")
        check(re.search(r"if fcntl is not None:\s*\n\s*fcntl\.flock", src),
              f"{name}: the lock is skipped (not fatal) when fcntl is absent")


# --- M1: the argv pattern both templates use is injection-proof AND lossless
def test_status_argv_call_is_injection_safe():
    print("test_status_argv_call_is_injection_safe")
    if shutil.which("node") is None:
        print("  skip: node not available")
        return
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        sentinel = os.path.join(base, "PWNED")
        # A sub-plan id shaped like divider-agent output that breaks OUT of the
        # double quotes a shell-string command would have wrapped it in.
        hostile = f'sp-a" ; touch {sentinel} ; echo "'
        script = os.path.join(base, "run.js")
        # Verbatim the templates' runStatus call shape: argv + env, no shell, and
        # the child's stderr captured (status.sh warns there and still exits 0, so
        # a discarded stream is the one way the call fails silently — n1).
        Path(script).write_text(
            "const cp = require('node:child_process')\n"
            "const [S, TASK, plan, state] = process.argv.slice(2)\n"
            "const r = cp.spawnSync('bash', [S, String(plan), 'implement',"
            " String(state), 'attempt 1: go'],"
            " { env: { ...process.env, ORCH_STATE_DIR: TASK }, encoding: 'utf8' })\n"
            "if (r.error) { throw r.error }\n"
            "process.stdout.write('STDERR:' + (r.stderr || '').trim())\n")
        proc = subprocess.run(
            ["node", script, str(STATUS_SH), state_dir, hostile, "running"],
            capture_output=True, text=True)
        check(proc.returncode == 0,
              f"argv status call exits 0 on a hostile plan id ({proc.stderr.strip()[:80]})")
        check(not os.path.exists(sentinel),
              "no command substitution executed (no PWNED file)")
        obj = json.loads((Path(state_dir) / "events.jsonl").read_text().splitlines()[0])
        check(obj["plan"] == hostile, "the hostile plan id lands verbatim, unmangled")
        check(obj["stage"] == "implement" and obj["state"] == "running",
              "the quote never split the arg list (stage/state intact)")
        check(proc.stdout.strip() == "STDERR:",
              "a good call produces no warning to log")
        # n1: the same shape surfaces status.sh's stderr warning instead of
        # swallowing it (an out-of-enum state warns and still appends).
        warned = subprocess.run(
            ["node", script, str(STATUS_SH), state_dir, "sp-ok", "exploded"],
            capture_output=True, text=True)
        check("unknown state" in warned.stdout,
              "the caller can see (and log) status.sh's stderr warning")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- GD-D5 / D-10: neither template emits events; the daemons do
def test_templates_emit_no_events():
    """The script-side emitters are GONE, and that is the correction.

    These templates used to carry `runStatus`/`closeRun`/`publishConfig`, and
    this test used to pin their shape. Every one of them silently no-opped in
    every real run: the workflow runtime has no Node API, so the
    `import('node:child_process')` inside them threw and was swallowed by the
    helper's own try/catch — 105 dead-import proof lines across 14 of 28
    recorded runs, one of which failed on nothing else. Pinning the shape of a
    call that never happens is how a maintainer debugging a missing badge ends
    up in the wrong file, so D-10 deleted the plumbing and this arm inverted.

    Who emits what now (GD-D5, and the templates say so in their own headers):
    `decision_watcher.py` derives spawn/result/verdict/token events from the
    journal and the `[monitor]` marker; `cycle_reporter.py` emits the
    loop-terminal `plan done|failed` events; `touch-run` owns the run envelope
    and stops the daemons by recorded pid. `status.sh` is still the ONE write
    path into events.jsonl (its three legitimate callers are a human, a driver
    and those emitters), which is what the rest of this file tests.

    Ownership note: `tests/monitoring/test_shell.py` belongs to the docs
    sub-plan of the touch-determinism run, which owns one unrelated assertion
    in it. This function was rewritten here rather than left red because a
    sub-plan may not hand a later one a broken suite.
    """
    print("test_templates_emit_no_events")
    for path in (TEMPLATE, RESEARCH_TEMPLATE):
        src = path.read_text()
        name = path.name
        # Code only: both headers NAME the deleted helpers while explaining why
        # nothing calls them, and that explanation is what keeps them deleted.
        code = "\n".join(ln for ln in src.splitlines()
                          if not ln.lstrip().startswith("//"))
        check("import('node:" not in code,
              f"{name}: no dynamic node: import survives")
        for helper in ("runStatus", "closeRun", "publishConfig"):
            check(f"{helper}(" not in code,
                  f"{name}: nothing calls the dead `{helper}(` helper")
        # The deterministic emitters are NAMED, so a reader looking for the
        # missing badge is sent to the right file.
        for daemon in ("decision_watcher.py", "cycle_reporter.py", "touch-run"):
            check(daemon in src, f"{name}: names {daemon} as an emitter")
        # R-40's kill discipline still holds, now by being absent: the daemon
        # epilogue moved to `touch-run close`, which stops watcher and reporter
        # by RECORDED, /proc-verified pid. A template that grew a kill path
        # again would be a per-task epilogue able to take down the shared
        # monitor server that serves every other live run.
        check("pkill" not in code, f"{name}: no pkill (wrong-target kill)")
        check("process.kill" not in code, f"{name}: no signal from the script")
        check("monitor.pid" not in code,
              f"{name}: the shared monitor server is never killed per task")
        # M1's injection lesson, kept as an absence: no status command is built
        # as a shell string anywhere, for the script OR for a prompt.
        check("['-c'" not in code and '["-c"' not in code,
              f"{name}: no `bash -c` execution of anything")
        # GD-D1a: the marker is the one prompt line that must never be trimmed
        # — every event the watcher derives is classified from it.
        check("[monitor] plan=" in src,
              f"{name}: the [monitor] marker is still authored into the prompts")

    impl = TEMPLATE.read_text()
    check("FINALGATE_ATTEMPTS; fga++" in impl,
          "the final-gate loop bound is the published cap, not a literal")
    # The caps are still the loop's own numbers; they reach orch-config.json
    # through `touch-run`, which publishes them from the run spec (D-13).
    check("const MAX_ATTEMPTS = ARGS.max_attempts" in impl,
          "implement template takes its attempt cap from the run spec")
    check("const FINALGATE_ATTEMPTS = ARGS.finalgate_attempts" in impl,
          "implement template takes its final-gate cap from the run spec")

    research = RESEARCH_TEMPLATE.read_text()
    # n-4, unchanged in substance: with a short board there is nothing to
    # synthesize, so the branch throws instead. What changed is what it CLAIMS.
    # The script cannot write an event (GD-D5), and the badge it would like to
    # claim is not the one that lands: cycle_reporter.py's zero-return rule
    # closes a research card `failed` only on an EMPTY board, while a PARTIAL
    # board carries results with `findings` and reads as `done`. A log line
    # announcing "closes failed" beside a green dashboard is R-58's defect with
    # the sign flipped, so the branch reports only what it did.
    start = research.find("|| reports.length < MIN_REPORTS)")
    end = research.find("phase('Synthesize')")
    check(start != -1 and end != -1 and start < end,
          "the short-board branch stands between the barrier and synthesis")
    zero_branch = research[start:end] if (start != -1 and end != -1) else ""
    check("throw new Error" in zero_branch,
          "the short-board branch throws, never spawns synthesis")
    check("closes failed" not in zero_branch,
          "the short-board branch claims no verdict this script cannot cause")


# --- .gitignore entries (R-01 + R-42's Mongo additions, SD-3) + negatives
def test_gitignore():
    print("test_gitignore")
    gi = GITIGNORE.read_text()
    # Item 04 (2026-07-28) INVERTED these two: the module dir no longer receives
    # state at all (status.sh exits 2, the daemons exit 1), so an ignore rule
    # for it would only be a licence for the behaviour to return. Comments may
    # still name the paths — only a live rule counts.
    rules = [ln.strip() for ln in gi.splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    check(not any(".claude/shared/monitoring/events.jsonl" in ln for ln in rules),
          ".gitignore no longer sanctions a module-dir events.jsonl")
    check(not any(".claude/shared/monitoring/.watcher-state.json" in ln for ln in rules),
          ".gitignore no longer sanctions a module-dir .watcher-state.json")
    # SD-3: the verbatim entry list, asserted here and written by the bootstrap.
    # This tuple is the TWIN of tests/test_bootstrap.py's GITIGNORE_ENTRIES and
    # must stay character-for-character the same. The
    # `.claude/local-orchestrators/…` line is legacy defence: since the
    # tasks-root move the live tree is `.touch/local-orchestrators/`, ignored by
    # `/.touch/*`, and that rule stays so a re-created old tree is still
    # ignored. `!/.touch/run.json` is the ONLY carve (D-12): the tracked
    # per-project run constants a workflow template consumes through `args` — a
    # single file, never widened to a directory.
    #
    # 2026-07-31: the three `!/.touch/memory/…` lines that used to sit between
    # `/.touch?*/` and `!/.touch/run.json` are GONE — the auto-memory subtree is
    # no longer published (G9 withdrawn). Their absence is asserted below.
    for entry in ("/.touch/*", "/.touch?*/",
                  "!/.touch/run.json",
                  ".claude/settings.local.json", "*.pid",
                  ".claude/local-orchestrators/*/.watcher-state.json",
                  "mongo-data/", "mongo-dump/", "*.bson"):
        check(entry in gi, f".gitignore contains {entry}")
    # Rules only — the replacement block names the withdrawn lines in a comment
    # explaining why they went, which a whole-text search would trip over.
    gi_rules = [ln.strip() for ln in gi.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
    for gone in ("!/.touch/memory/", "/.touch/memory/*", "!/.touch/memory/*.md"):
        check(gone not in gi_rules, f".gitignore no longer re-includes {gone}")
    # 2026-07-27 amendment: the whole per-task run-state tree is ignored and
    # untracked (kept on disk only). `git check-ignore` exits 0 when ignored.
    # `--no-index` (LAYOUT-18): check-ignore consults the index and answers "not
    # ignored" for a TRACKED path whatever the rules say, so the negative arm
    # below bypasses the index and asserts the RULES rather than the current
    # contents of the index. Every path here is hypothetical.
    def ignored(rel):
        return subprocess.run(["git", "check-ignore", "-q", "--no-index", "--", rel],
                              cwd=REPO, capture_output=True).returncode == 0
    if subprocess.run(["git", "rev-parse", "--git-dir"], cwd=REPO,
                      capture_output=True).returncode != 0:
        print("  skip: not a git repo")
        return
    orch = ".touch/" + "local-orchestrators"
    legacy = ".claude/" + "local-orchestrators"
    check(ignored(orch + "/"), f"{orch}/ itself is ignored")
    check(ignored(orch + "/a-task/events.jsonl"),
          f"events.jsonl under {orch}/ is ignored")
    check(ignored(orch + "/a-task/.watcher-state.json"),
          "watcher checkpoints under the tasks root ARE ignored")
    # legacy defence: the old location stays ignored too, so a re-created old
    # tree is never offered for commit.
    check(ignored(legacy + "/") and ignored(legacy + "/a-task/events.jsonl")
          and ignored(legacy + "/a-task/.watcher-state.json"),
          "the legacy tasks root and its state stay ignored")
    check(ignored(".touch/x") and ignored("mongo-data/x") and ignored("dump.bson"),
          "Touch runtime state and Mongo dumps are ignored")
    # The withdrawn G9 carve, behavioural half: the auto-memory files are
    # ordinary ignored state now. Asserted here as well as in test_bootstrap.py
    # because this file is the monitoring suite's own copy of the SD-3 twin and
    # a half-restored carve must fail on both sides.
    check(ignored(".touch/memory/does-not-exist.md")
          and ignored(".touch/memory/MEMORY.md"),
          ".touch/memory/*.md is ignored like the rest of .touch/ (G9 withdrawn)")
    # D-12's carve is the one tracked FILE, and now the only one — the
    # behavioural half of the entry-list assertion above, so a carve that is
    # present as a line but defeated by ordering still fails here.
    check(not ignored(".touch/run.json"),
          ".touch/run.json is the one tracked FILE of .touch/ (D-12)")
    check(ignored(".touch/run.json.bak") and ignored(".touch/runs.json"),
          "the run.json carve stays a single file, not a prefix")
    for leaked in (".touch/memory/x.pid", ".touch/memory/x.token",
                   ".touch/memory/draft.md.bak", ".touch/memory/.history/x.md",
                   ".touch/memory/.trash/x.md", ".touch/memory-audit.jsonl",
                   ".touch/sessions/x", ".touch/server.json", ".touch/mongo.json"):
        check(ignored(leaked), f"the carve stays narrow: {leaked} is ignored")


def main():
    for t in (test_status_creates_state_dir, test_status_injection_safe,
              test_status_unset_hard_errors, test_status_unset_resolves_tasks_root,
              test_status_refuses_a_plugin_cache, test_status_writer_attribution,
              test_status_detail_cap, test_status_bad_state_warns_but_writes,
              test_status_concurrent_appends_are_atomic,
              test_status_append_waits_for_the_lock,
              test_append_sites_take_lock_ex,
              test_status_argv_call_is_injection_safe, test_status_plans_total,
              test_status_walk_up_is_forkless_and_still_walks,
              test_status_ladder_is_three_rungs,
              test_no_wrapper_documents_a_fourth_resolver_rung,
              test_template_static, test_templates_emit_no_events,
              test_docs_static, test_gitignore):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all sp-shell tests passed")


if __name__ == "__main__":
    main()
