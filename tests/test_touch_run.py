#!/usr/bin/env python3
"""`plugin/touch/bin/touch-run` — the deterministic driver envelope (D-13).

Run as `python3 test_touch_run.py`; exits non-zero on failure. No pytest, no
runner — `run_all.sh` picks it up by its `test_*.py` glob.

WHAT THIS FILE IS FOR
---------------------
`touch-run` replaces ~230 lines of `monitor/SKILL.md` prose that a driver
re-typed per run. Every step it owns has exactly one correct spelling and no
judgment in it, and the two that were reliably got wrong are the two this file
spends most of its assertions on.

1. **The `ACTIVE` close-out.** The documented idiom is
   `grep -vxF "<task>" "$f" > "$f.tmp"` guarded by
   `if [ -s "$f.tmp" ]; then mv …; else rm -f "$f" "$f.tmp"; fi`, and it is
   wrong twice over: `$f.tmp` is a FIXED, SHARED path, so two close-outs
   running at once interleave into one temp file and the loser publishes a
   sentinel it did not compute; and the `else` branch deletes the WHOLE
   sentinel whenever grep failed for any reason other than "nothing matched",
   disarming the run scope for every other run listed in it. `close` must
   remove EXACTLY its own line and leave every other line alone, and that is
   asserted against a multi-line sentinel, a single-line one, an absent one and
   one carrying blank lines and indentation the command did not write.

2. **"Nothing half-created".** A refused launch must leave no task folder, no
   card, no sentinel line. Asserted on the filesystem, from every refusal shape
   — an unparseable spec, a spec whose preflight fails, a malformed envelope
   (a roster entry with no id, a non-integer `plans_total`) and a plain usage
   error (`--port abc`) — because "it refused" and "it refused before it
   started writing" are different claims and only the second is worth anything
   to somebody re-running the command.

3. **`status.sh` is the only writer (GD-D5).** Every event `touch-run` emits is
   asserted to carry `"w": "agent"` and to have gone through the writer that
   owns the flock and the cap. A seeder that wrote its own JSON would produce
   lines that look identical until the day two writers interleave — the two
   ledgerless roster lines already on disk are the cautionary example.

4. **R-58, at the run close.** `close` never infers `failed`. With no
   `--state`, a run settles `done` with the honest "closed — no verdict"
   wording; `failed` appears only when the caller typed it. And when an earlier
   GD-D6 rung has already written the terminal event, `close` writes no second,
   contradictory one.

5. **The preflight refuses the launches that used to burn a whole run** (D-12):
   a placeholder that survived into the spec, a `project_dir` that does not
   exist, a plan file that is not there, a script that lost `agentR` or grew a
   third raw `await agent(` call, and an `orch-scripts/` copy that is not
   byte-identical to the template it was copied from.

6. **Stopping by pid is stopping the RIGHT process.** Pids are reused. A
   recorded pid whose command line does not carry the daemon's marker is
   refused rather than signalled, and that arm points the recorded pid at THIS
   test process — the one process the suite can be certain is alive and is
   certainly not a watcher. The SUCCESSFUL stop is exercised too, against a
   real sleeping process whose command line carries the watcher's marker: an
   orphaned watcher is the failure the repo's commit gate exists for, so
   "close stopped it" cannot be a claim only the refusal path tests.

7. **The roster has a writer now (GD-D11).** `ORCH_ROSTER` is a FILE PATH env
   var, never env-inlined JSON. `status.sh` does not read it yet (it lands with
   the reporter item), so the assertion is made where it is already true: the
   file `start` materialises, and the environment it hands the writer — under a
   plugin root whose `bin/` writers are recording shims.

8. **The watcher is started when — and only when — its journal is known.**
   `decision_watcher` resolves that journal once at import and never again, and
   its last rung is the newest `wf_*` anywhere under the configuration
   directory, machine-wide. Started from `start`, before the Workflow exists,
   it therefore attaches to somebody else's run and replays that run's cards
   and verdicts into this task's stream. So `start` defers it and `bind` starts
   it with the bound directory in ARGV, and both halves are asserted at the
   DAEMON's own command line rather than from the source text.

No arm starts a listener. The ones that need a real `launch()` point it at a
shim that records its argv and environment and exits, and the stop arm points
it at a sleeper — a suite that starts servers is a suite that fails differently
on a busy machine, and a suite that never runs `launch()` at all ships an
untested daemon lifecycle.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "plugin" / "touch" / "bin"
RUN = BIN / "touch-run"
TEMPLATE = (REPO / "plugin" / "touch" / "skills" / "implement"
            / "templates" / "implement.workflow.js")

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


def run(args, cwd, env=None, timeout=120):
    """CompletedProcess from a throwaway project, or None if it would not run.

    The orchestration run that executes this suite exports `ORCH_STATE_DIR` and
    friends; inherited, they would hand `touch-run` the answers it is supposed
    to resolve. `CLAUDE_PROJECT_DIR` is the subtle one — `touch-run` never reads
    it, but `aggregator.paths.project_root()` does, so leaving it set would
    point every arm below at the development repo instead of the temporary
    project.
    """
    base = dict(os.environ)
    for var in ("ORCH_STATE_DIR", "ORCH_TASKS_ROOT", "ORCH_WF_DIR", "ORCH_PORT",
                "ORCH_BIND", "TOUCH_STATE_DIR", "TOUCH_PROJECT_CWD",
                "TOUCH_LEGACY_ROOT", "CLAUDE_PROJECT_DIR", "TOUCH_CLAUDE_ROOT",
                "PYTHONPATH"):
        base.pop(var, None)
    base.update(env or {})
    try:
        return subprocess.run([str(a) for a in args], cwd=str(cwd), env=base,
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        check(False, f"{args[0]} runs ({exc})")
        return None


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def make_project(tmp, name, task="sp05run", active=None, plan=True):
    """A temporary Claude Code project with a tasks root and (maybe) a plan.

    `.claude/` is the project MARKER every resolver walks up to; the tasks root
    is `.touch/local-orchestrators` beneath it. The two names differ on purpose
    and the fixture spells both out rather than deriving one from the other.
    """
    project = tmp / name
    (project / ".claude").mkdir(parents=True)
    tasks = project / ".touch" / "local-orchestrators"
    tasks.mkdir(parents=True)
    if active is not None:
        (tasks / "ACTIVE").write_text(active, encoding="utf-8")
    plan_file = tasks / task / "plan" / f"{task}-plan.md"
    if plan:
        plan_file.parent.mkdir(parents=True)
        plan_file.write_text("# plan\n\n- item\n", encoding="utf-8")
    return project, tasks, plan_file


def make_spec(tmp, name, project, task="sp05run", plan_file=None, **extra):
    spec = {"kind": "implement", "task": task, "project_dir": str(project),
            "title": "a run", "roster": ["sp-01-a", {"id": "sp-02-b",
                                                     "title": "the second"}]}
    if plan_file is not None:
        spec["plan_file"] = str(plan_file)
    spec.update(extra)
    path = tmp / name
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return path


def events_of(tasks, task="sp05run"):
    path = tasks / task / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


#: A recording shim: it appends what it was called with — argv plus every
#: `ORCH_*` variable it inherited — and then either forwards to the real
#: program or exits. Written as a program rather than asserted from the source
#: text because "the source says it sets ORCH_ROSTER" and "the writer received
#: ORCH_ROSTER" are different claims, and only the second one is a behaviour.
SHIM = '''#!/usr/bin/env python3
import json, os, sys

with open({record!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{
        "prog": {name!r},
        "argv": sys.argv[1:],
        "env": {{k: v for k, v in os.environ.items() if k.startswith("ORCH_")}},
    }}) + "\\n")
real = {real!r}
if real:
    os.execv(real, [real] + sys.argv[1:])
'''


def fake_root(tmp, name):
    """A plugin root whose `bin/` writers are recording shims. -> (root, record)

    `touch-run` resolves its root from `readlink -f "$0"`, so the wrapper is
    COPIED here — a symlink would resolve straight back to the real payload and
    the shims would never be reached. Everything it reads or imports
    (`aggregator/`, `skills/`, `shared/`) is symlinked to the real tree, so the
    program under test stays byte-identical to the shipped one and only the
    programs it SPAWNS are substituted.

    `touch-status` forwards to the real writer, because the stream this
    produces still has to be a real `status.sh` stream; the daemons do not,
    because this suite starts no daemons.
    """
    root = tmp / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "touch", "version": "0.0.0-test"}\n', encoding="utf-8")
    (root / "bin").mkdir()
    for sub in ("aggregator", "skills", "shared"):
        os.symlink(REPO / "plugin" / "touch" / sub, root / sub)
    copy = root / "bin" / "touch-run"
    shutil.copyfile(RUN, copy)
    copy.chmod(0o755)
    record = tmp / (name + "-calls.jsonl")
    for prog, real in (("touch-status", str(BIN / "touch-status")),
                       ("touch-monitor", None), ("touch-watcher", None),
                       ("touch-cycle-reporter", None)):
        path = root / "bin" / prog
        path.write_text(SHIM.format(record=str(record), name=prog, real=real),
                        encoding="utf-8")
        path.chmod(0o755)
    return root, record


def calls(record, prog=None):
    """Every recorded shim invocation, oldest first."""
    if not record.is_file():
        return []
    out = [json.loads(ln) for ln in record.read_text(encoding="utf-8").splitlines()
           if ln.strip()]
    return [c for c in out if prog is None or c["prog"] == prog]


def wait_for_calls(record, prog, count, timeout=15.0):
    """Recorded invocations of `prog`, once there are `count` of them.

    A daemon is DETACHED on purpose — `touch-run` returns as soon as it has the
    pid, so the shim's own write races the command's exit. Polling is the
    honest way to wait for it; a bare sleep would either be flaky on a loaded
    machine or slow on an idle one.
    """
    deadline = time.time() + timeout
    while True:
        got = calls(record, prog)
        if len(got) >= count or time.time() >= deadline:
            return got
        time.sleep(0.05)


def started(tmp, name, task="sp05run", active=None, spec_extra=None,
            start_args=()):
    """A project with one started task — the common setup for the later arms."""
    project, tasks, plan_file = make_project(tmp, name, task=task, active=active)
    spec = make_spec(tmp, name + ".json", project, task=task, plan_file=plan_file,
                     **(spec_extra or {}))
    res = run([RUN, "start", task, "--spec", spec, "--no-daemons"] + list(start_args),
              cwd=project)
    return project, tasks, spec, res


# --------------------------------------------------------------------------
# the wrapper itself
# --------------------------------------------------------------------------
def test_wrapper_shape(tmp):
    print("test_wrapper_shape")
    check(RUN.is_file(), "plugin/touch/bin/touch-run exists")
    if not RUN.is_file():
        return
    check(bool(RUN.stat().st_mode & stat.S_IXUSR), "it is executable")
    text = RUN.read_text(encoding="utf-8")
    check(text.splitlines()[0] == "#!/usr/bin/env bash",
          "it starts with the env-bash shebang")
    res = run(["bash", "-n", RUN], cwd=tmp)
    if res is not None:
        check(res.returncode == 0, f"it parses (bash -n: {res.stderr[:200]})")
    # A bare invocation is a USAGE error, not a default action: every verb here
    # either writes or resolves a project, and neither is a sensible thing to do
    # because somebody pressed enter.
    res = run([RUN], cwd=tmp)
    if res is not None:
        check(res.returncode == 2 and "usage:" in res.stdout,
              f"a bare `touch-run` prints usage and exits 2 (rc={res.returncode})")
    res = run([RUN, "--help"], cwd=tmp)
    if res is not None:
        check(res.returncode == 0 and "start" in res.stdout and "close" in res.stdout,
              f"--help exits 0 and names the verbs (rc={res.returncode})")
        # Every verb parses --tasks-root, and the parser is strict on purpose:
        # a mistyped flag is a usage error rather than a silent positional.
        # That argument cuts both ways — a flag documented for one verb and
        # accepted by five is one a driver reaches by guessing.
        synopsis = [ln.strip() for ln in res.stdout.splitlines()
                    if ln.strip().startswith("touch-run ")]
        check(len(synopsis) == 5,
              f"--help shows one synopsis line per verb ({len(synopsis)})")
        missing = [ln.split()[1] for ln in synopsis if "--tasks-root" not in ln]
        check(not missing,
              f"and every verb that accepts --tasks-root documents it "
              f"(undocumented on: {missing})")
    res = run([RUN, "launch"], cwd=tmp)
    if res is not None:
        check(res.returncode == 2 and "unknown verb" in res.stderr,
              f"an unknown verb is refused, not guessed (rc={res.returncode}, "
              f"{res.stderr[:120]})")


# --------------------------------------------------------------------------
# verify — the preflight, standalone (D-12)
# --------------------------------------------------------------------------
def test_verify_accepts_a_good_spec(tmp):
    print("test_verify_accepts_a_good_spec")
    project, tasks, plan_file = make_project(tmp, "verify-good")
    spec = make_spec(tmp, "verify-good.json", project, plan_file=plan_file)
    res = run([RUN, "verify", "--spec", spec, "--task", "sp05run"], cwd=project)
    if res is None:
        return
    check(res.returncode == 0,
          f"a complete spec preflights clean (rc={res.returncode}, "
          f"{res.stdout.strip()[-300:]})")
    check(not [ln for ln in res.stdout.splitlines() if ln.startswith("FAIL")],
          f"no check fails ({[ln for ln in res.stdout.splitlines() if ln.startswith('FAIL')]})")
    # The shape arms are about the shipped template, so they are evidence that
    # the preflight read the real script and not a stub.
    check("agentR" in res.stdout and "await agent(" in res.stdout,
          "and it reports on the script shape (agentR, the raw spawn count)")


def test_verify_catches_a_planted_placeholder(tmp):
    print("test_verify_catches_a_planted_placeholder")
    # The failure this refuses used to spawn a whole opus fan-out against
    # `/ABS/PATH/TO/PROJECT`, whose findings writes cannot land: a full run of
    # tokens for a result that could not exist.
    project, tasks, plan_file = make_project(tmp, "verify-placeholder")
    for key, value in (("project_dir", "/ABS/PATH/TO/PROJECT"),
                       ("context", "TASK_SPECIFIC_CONTEXT (goal, constraints)"),
                       ("test_hints", "TODO fill this in")):
        spec = make_spec(tmp, f"ph-{key}.json", project, plan_file=plan_file,
                         **{key: value})
        res = run([RUN, "verify", "--spec", spec, "--task", "sp05run"], cwd=project)
        if res is None:
            continue
        check(res.returncode != 0,
              f"a spec whose {key} is still a placeholder is REFUSED "
              f"(rc={res.returncode})")
        check(any(ln.startswith("FAIL") and key in ln
                  for ln in res.stdout.splitlines()),
              f"...and the failing line names the key ({key}): "
              f"{[ln for ln in res.stdout.splitlines() if ln.startswith('FAIL')]}")


def test_verify_catches_a_missing_plan_file(tmp):
    print("test_verify_catches_a_missing_plan_file")
    project, tasks, plan_file = make_project(tmp, "verify-noplan", plan=False)
    spec = make_spec(tmp, "verify-noplan.json", project, plan_file=plan_file)
    res = run([RUN, "verify", "--spec", spec, "--task", "sp05run"], cwd=project)
    if res is None:
        return
    check(res.returncode != 0,
          f"an implement run whose plan file is absent is REFUSED "
          f"(rc={res.returncode})")
    check(any(ln.startswith("FAIL") and "plan file" in ln
              for ln in res.stdout.splitlines()),
          f"...naming the plan file "
          f"{[ln for ln in res.stdout.splitlines() if ln.startswith('FAIL')]}")
    # A missing project_dir is the same class and the same refusal.
    spec = make_spec(tmp, "verify-nodir.json", project, plan_file=plan_file,
                     project_dir=str(project / "gone"))
    res = run([RUN, "verify", "--spec", spec, "--task", "sp05run"], cwd=project)
    if res is not None:
        check(res.returncode != 0,
              f"a project_dir that does not exist is REFUSED (rc={res.returncode})")


def test_verify_catches_a_damaged_script(tmp):
    print("test_verify_catches_a_damaged_script")
    if not TEMPLATE.is_file():
        skip(f"{TEMPLATE} is not present — the script-shape arms need it")
        return
    project, tasks, plan_file = make_project(tmp, "verify-script")
    spec = make_spec(tmp, "verify-script.json", project, plan_file=plan_file)
    source = TEMPLATE.read_text(encoding="utf-8")

    # GD-D1a is fenced elsewhere; this is the other half of the same rule — a
    # copy that lost the infrastructure wrapper turns an agent that died on
    # infrastructure into a narrated null, mid-run, with no symptom until the
    # aggregate gate.
    damaged = tmp / "no-agentr.workflow.js"
    damaged.write_text(source.replace("agentR", "agentPlain"), encoding="utf-8")
    res = run([RUN, "verify", "--spec", spec, "--task", "sp05run",
               "--script", damaged], cwd=project)
    if res is not None:
        check(res.returncode != 0 and any("agentR" in ln for ln in
                                          res.stdout.splitlines()
                                          if ln.startswith("FAIL")),
              f"a script with no agentR is REFUSED, naming it (rc={res.returncode})")

    # A THIRD raw spawn is the shape of "somebody added a call and skipped the
    # wrapper" — the count is the cheap way to see it.
    extra = tmp / "extra-spawn.workflow.js"
    extra.write_text(source + "\nconst stray = await agent({})\n", encoding="utf-8")
    res = run([RUN, "verify", "--spec", spec, "--task", "sp05run",
               "--script", extra], cwd=project)
    if res is not None:
        check(res.returncode != 0 and any("await agent(" in ln for ln in
                                          res.stdout.splitlines()
                                          if ln.startswith("FAIL")),
              f"a third raw `await agent(` is REFUSED (rc={res.returncode})")

    # ...and the discriminating half: the UNDAMAGED script through the same
    # code path passes, so the two arms above are not passing on the flag.
    res = run([RUN, "verify", "--spec", spec, "--task", "sp05run",
               "--script", TEMPLATE], cwd=project)
    if res is not None:
        check(res.returncode == 0,
              f"the shipped template through the same path is clean "
              f"(rc={res.returncode}, {res.stdout.strip()[-200:]})")


def test_verify_pins_the_copy_to_the_template(tmp):
    print("test_verify_pins_the_copy_to_the_template")
    # GD-D9: `orch-scripts/` copies are byte-for-byte `cp`. A drifted copy is a
    # run whose archived script is not the one that ran — the reason the plans
    # in this repo can be read years later.
    project, tasks, spec, res = started(tmp, "verify-copy")
    if res is None or res.returncode != 0:
        check(False, f"the fixture run started ({res.stdout if res else None})")
        return
    res = run([RUN, "verify", "sp05run"], cwd=project)
    if res is not None:
        check(res.returncode == 0 and "byte-identical" in res.stdout,
              f"a fresh copy verifies byte-identical (rc={res.returncode})")
    copy = tasks / "sp05run" / "orch-scripts" / "implement.workflow.js"
    copy.write_text(copy.read_text(encoding="utf-8") + "// drifted\n",
                    encoding="utf-8")
    res = run([RUN, "verify", "sp05run"], cwd=project)
    if res is not None:
        check(res.returncode != 0 and any("byte-identical" in ln for ln in
                                          res.stdout.splitlines()
                                          if ln.startswith("FAIL")),
              f"an edited copy is REFUSED (rc={res.returncode})")


# --------------------------------------------------------------------------
# start
# --------------------------------------------------------------------------
def test_start_seeds_everything_exactly(tmp):
    print("test_start_seeds_everything_exactly")
    project, tasks, spec, res = started(tmp, "start-seeds", active="other-run\n")
    if res is None:
        return
    check(res.returncode == 0,
          f"start exits 0 (rc={res.returncode}, {(res.stdout + res.stderr)[-400:]})")
    task_dir = tasks / "sp05run"

    # The tasks root is resolved ONCE, by the shipped ladder, and PRINTED —
    # anchoring on a bare $PWD is the mistake that fails silently, so the answer
    # has to be visible in the output a driver reads.
    check(f"tasks root: {tasks}" in res.stdout,
          f"the resolved tasks root is printed ({res.stdout.splitlines()[:2]})")

    for sub in ("plan", "findings", "report", "orch-scripts"):
        check((task_dir / sub).is_dir(), f"the {sub}/ subdirectory exists")
    copy = task_dir / "orch-scripts" / "implement.workflow.js"
    check(copy.is_file(), "the template was copied into orch-scripts/")
    if copy.is_file() and TEMPLATE.is_file():
        check(copy.read_bytes() == TEMPLATE.read_bytes(),
              "...byte for byte (GD-D9)")

    events = events_of(tasks)
    check(len(events) == 3,
          f"exactly three cards are seeded — the run card plus one per roster "
          f"entry ({[e.get('plan') for e in events]})")
    if len(events) != 3:
        return
    run_card, first, second = events
    check(run_card.get("plan") == "orchestrator"
          and run_card.get("state") == "running",
          f"the run card opens `running` ({run_card})")
    check(run_card.get("title") == "a run",
          f"...carrying the title from the spec ({run_card.get('title')!r})")
    check([e.get("plan") for e in (first, second)] == ["sp-01-a", "sp-02-b"],
          f"the roster seeds one card each, in order "
          f"({[e.get('plan') for e in (first, second)]})")
    check(second.get("title") == "the second",
          f"a roster entry may carry its own title ({second.get('title')!r})")
    check(all(e.get("state") == "queued" for e in (first, second)),
          f"plan cards start `queued` ({[e.get('state') for e in (first, second)]})")
    # GD-D11: the denominator is declared at the seed, and it counts PLAN cards
    # — the run card is the run, not a plan.
    check(all(e.get("plans_total") == 2 for e in events),
          f"every seeded line declares plans_total=2 "
          f"({[e.get('plans_total') for e in events]})")
    # GD-D5: through `status.sh`, never raw JSON. The attribution is the tell.
    check(all(e.get("w") == "agent" for e in events),
          f"every line went through status.sh (w=agent): "
          f"{[e.get('w') for e in events]}")
    check(all(set(e) >= {"ts", "plan", "stage", "state", "detail"} for e in events),
          "and carries the five-key event shape")

    active = (tasks / "ACTIVE").read_text(encoding="utf-8")
    check(active == "other-run\nsp05run\n",
          f"ACTIVE gained this task and kept the other run's line ({active!r})")

    config = json.loads((task_dir / "orch-config.json").read_text(encoding="utf-8"))
    check(config.get("port") == 8931 and config.get("strategy") == "sequential",
          f"orch-config.json carries the caps/strategy touch-run published "
          f"(INVENTORY-2): {config}")
    saved = json.loads((task_dir / "run-spec.json").read_text(encoding="utf-8"))
    check(saved.get("task") == "sp05run" and saved.get("project_dir") == str(project),
          f"the effective spec is recorded beside the run ({sorted(saved)})")

    # --no-daemons means no daemons: no pid file, and nothing listening.
    check(not (task_dir / "watcher.pid").exists()
          and not (tasks / "monitor.pid").exists(),
          "--no-daemons records no pid (this suite starts no servers)")

    # The launch line is the deliverable: it must name the COPY, not the
    # template, and carry the args the script will actually read.
    check(f'scriptPath: "{copy}"' in res.stdout,
          f"the printed Workflow line names the copy ({res.stdout[-500:]})")
    match = re.search(r"args: (\{.*\}) \}\)", res.stdout)
    check(bool(match), f"...and an args object ({res.stdout[-300:]})")
    if match:
        args = json.loads(match.group(1))
        check(args.get("project_dir") == str(project) and args.get("task") == "sp05run",
              f"...with the two keys the script throws without ({sorted(args)})")
        check("roster" not in args and "title" not in args and "kind" not in args,
              f"...and without the envelope-only keys ({sorted(args)})")


def test_start_folds_the_denominator(tmp):
    print("test_start_folds_the_denominator")
    # GD-D11: `max(existing cards, declared)`. A re-seed over a stream that
    # already has more cards than the spec declares must not SHRINK the
    # denominator — the fold downstream is monotonic-max and a seed that went
    # backwards would be the one input it cannot reconcile.
    project, tasks, spec, res = started(tmp, "start-fold")
    if res is None or res.returncode != 0:
        check(False, f"the fixture run started ({res.stdout if res else None})")
        return
    env = {"ORCH_STATE_DIR": str(tasks / "sp05run")}
    for plan in ("sp-03-c", "sp-04-d", "sp-05-e"):
        run([BIN / "touch-status", plan, "plan", "queued", "later card"],
            cwd=project, env=env)
    res = run([RUN, "start", "sp05run", "--spec", spec, "--no-daemons"], cwd=project)
    if res is None:
        return
    check(res.returncode == 0, f"a re-seed succeeds (rc={res.returncode})")
    totals = [e.get("plans_total") for e in events_of(tasks)
              if e.get("plans_total") is not None]
    check(totals[-1] == 5,
          f"the re-seed declares max(existing cards, declared) = 5, not 2 "
          f"({totals})")
    active = (tasks / "ACTIVE").read_text(encoding="utf-8")
    check(active == "sp05run\n",
          f"and ACTIVE is appended IDEMPOTENTLY — one line, not two ({active!r})")


def test_start_refuses_and_creates_nothing(tmp):
    print("test_start_refuses_and_creates_nothing")
    # Both refusal shapes, and the assertion is the FILESYSTEM: "it refused" and
    # "it refused before it wrote" are different claims.
    project, tasks, plan_file = make_project(tmp, "start-refuses",
                                             task="nevermade", plan=False)
    bad = tmp / "bad-spec.json"
    bad.write_text("{ not json at all\n", encoding="utf-8")
    res = run([RUN, "start", "nevermade", "--spec", bad], cwd=project)
    if res is not None:
        check(res.returncode != 0,
              f"an unparseable spec exits non-zero (rc={res.returncode})")
        check("could not be read" in res.stderr,
              f"...saying which file and why ({res.stderr.strip()[:160]})")
    check(not (tasks / "nevermade").exists(),
          f"no task folder was created "
          f"({sorted(p.name for p in tasks.iterdir())})")
    check(not (tasks / "ACTIVE").exists(), "and no sentinel was created")

    # The second shape: parseable, but the preflight refuses it.
    spec = make_spec(tmp, "start-refuses.json", project, task="nevermade",
                     plan_file=plan_file)
    res = run([RUN, "start", "nevermade", "--spec", spec], cwd=project)
    if res is not None:
        check(res.returncode != 0,
              f"a spec that fails preflight exits non-zero (rc={res.returncode})")
        check("refusing to launch" in (res.stdout + res.stderr),
              f"...and says it is refusing to launch "
              f"({(res.stdout + res.stderr).strip()[-200:]})")
    check(not (tasks / "nevermade").exists(),
          f"still no task folder ({sorted(p.name for p in tasks.iterdir())})")
    check(not (tasks / "ACTIVE").exists(), "still no sentinel")

    # A missing --spec is a usage error (2), not a failed step (1): nothing was
    # attempted, so nothing can have half happened.
    res = run([RUN, "start", "nevermade"], cwd=project)
    if res is not None:
        check(res.returncode == 2,
              f"start without --spec is a usage error (rc={res.returncode})")


def test_start_refuses_a_malformed_envelope(tmp):
    print("test_start_refuses_a_malformed_envelope")
    # Every one of these used to be judged AFTER `os.makedirs`, so the command
    # said it refused while leaving a task folder, a copied script, a config and
    # — for the roster shape — an OPEN run card on the dashboard for a run that
    # was never launched. A refusal that leaves a live-looking card is the same
    # family of state lie R-58 exists to prevent.
    cases = {
        "roster-entry": ({"roster": ["sp-01", {"title": "no id here"}]}, ()),
        "roster-shape": ({"roster": "sp-01,sp-02"}, ()),
        "plans-total": ({"plans_total": "many"}, ()),
        "spec-script": ({"script": "skills/x/y.workflow.js"}, ()),
        "bad-port": ({}, ("--port", "abc")),
    }
    for name, (extra, args) in cases.items():
        project, tasks, plan_file = make_project(tmp, "envelope-" + name,
                                                 task="nevermade", plan=True)
        spec = make_spec(tmp, f"envelope-{name}.json", project, task="nevermade",
                         plan_file=plan_file, **extra)
        res = run([RUN, "start", "nevermade", "--spec", spec, "--no-daemons"]
                  + list(args), cwd=project)
        if res is None:
            continue
        check(res.returncode != 0,
              f"[{name}] the launch is refused (rc={res.returncode})")
        check("Traceback" not in res.stderr,
              f"[{name}] with a sentence, not a traceback "
              f"({res.stderr.strip()[-200:]})")
        # The plan/ directory is the fixture's own (it holds the plan file);
        # everything `start` would have created is what must be absent.
        made = [p for p in ("events.jsonl", "orch-config.json", "run-spec.json",
                            "orch-scripts", "roster.txt", "findings", "report")
                if (tasks / "nevermade" / p).exists()]
        check(not made, f"[{name}] nothing was created: {made}")
        check(not (tasks / "ACTIVE").exists(),
              f"[{name}] and no sentinel was armed")
    # `--port abc` is a USAGE error (2) and not a failed step (1) — nothing was
    # attempted, so nothing can have half happened.
    project, tasks, plan_file = make_project(tmp, "envelope-port-rc",
                                             task="nevermade")
    spec = make_spec(tmp, "envelope-port-rc.json", project, task="nevermade",
                     plan_file=plan_file)
    res = run([RUN, "start", "nevermade", "--spec", spec, "--port", "abc"],
              cwd=project)
    if res is not None:
        check(res.returncode == 2,
              f"a non-integer --port is a usage error (rc={res.returncode})")


def test_start_seeds_the_roster_file(tmp):
    print("test_start_seeds_the_roster_file")
    # GD-D11: the roster finally has a WRITER, and `ORCH_ROSTER` is a FILE PATH
    # env var — never env-inlined JSON, so a roster cannot be as long as an argv
    # allows. `status.sh` learns to read it in the reporter item; what is
    # asserted here is the half that is already true, and it is asserted at the
    # writer's own environment rather than from the source text.
    root, record = fake_root(tmp, "roster-root")
    project, tasks, plan_file = make_project(tmp, "roster-run")
    spec = make_spec(tmp, "roster-run.json", project, plan_file=plan_file)
    res = run([root / "bin" / "touch-run", "start", "sp05run", "--spec", spec,
               "--no-daemons"], cwd=project, env={"ORCH_ROSTER": "/inherited/x"})
    if res is None:
        return
    check(res.returncode == 0,
          f"start exits 0 under the shim root (rc={res.returncode}, "
          f"{(res.stdout + res.stderr)[-300:]})")
    roster = tasks / "sp05run" / "roster.txt"
    check(roster.is_file(), "start materialises the roster as a FILE")
    if roster.is_file():
        lines = roster.read_text(encoding="utf-8").splitlines()
        check(lines == ["sp-01-a", "sp-02-b — the second"],
              f"...one entry per line, in the `<id> — <title>` shape "
              f"monitoring.md documents ({lines})")
    seen = calls(record, "touch-status")
    check(len(seen) == 3,
          f"three events went through the writer ({[c['argv'][:1] for c in seen]})")
    if len(seen) != 3:
        return
    run_card, first, second = seen
    check(run_card["env"].get("ORCH_ROSTER") == str(roster),
          f"the run card's write carries ORCH_ROSTER as a PATH "
          f"({run_card['env'].get('ORCH_ROSTER')!r})")
    check("ORCH_ROSTER" not in first["env"] and "ORCH_ROSTER" not in second["env"],
          f"...and only that one: a roster belongs to the orchestrator card "
          f"({[c['env'].get('ORCH_ROSTER') for c in (first, second)]})")
    check(run_card["env"].get("ORCH_TITLE") == "a run"
          and run_card["env"].get("ORCH_PLANS_TOTAL") == "2",
          f"the title and the denominator travel the same way ({run_card['env']})")
    # And the INHERITED value never leaks: the run above exported
    # ORCH_ROSTER=/inherited/x, which nothing may pass through.
    check(all(c["env"].get("ORCH_ROSTER") != "/inherited/x" for c in seen),
          f"an inherited ORCH_ROSTER is dropped, never forwarded "
          f"({[c['env'].get('ORCH_ROSTER') for c in seen]})")

    # A spec with no roster names no file and sets no variable at all.
    root2, record2 = fake_root(tmp, "roster-none-root")
    project2, tasks2, plan2 = make_project(tmp, "roster-none")
    spec2 = make_spec(tmp, "roster-none.json", project2, plan_file=plan2, roster=[])
    res = run([root2 / "bin" / "touch-run", "start", "sp05run", "--spec", spec2,
               "--no-daemons"], cwd=project2, env={"ORCH_ROSTER": "/inherited/x"})
    if res is not None and res.returncode == 0:
        check(not (tasks2 / "sp05run" / "roster.txt").exists(),
              "a run with no roster writes no roster file")
        check(all("ORCH_ROSTER" not in c["env"] for c in calls(record2)),
              f"...and sets no ORCH_ROSTER "
              f"({[c['env'].get('ORCH_ROSTER') for c in calls(record2)]})")


def test_start_never_walks_a_card_backwards(tmp):
    print("test_start_never_walks_a_card_backwards")
    # `plan` is a reserved stage that SETS the badge and the fold is last-wins,
    # so re-seeding `queued` over a sub-plan that is already running would flip
    # live loops back to queued on the dashboard. The denominator is folded
    # monotonically for exactly this reason; card state gets the same care.
    project, tasks, spec, res = started(tmp, "reseed-run")
    if res is None or res.returncode != 0:
        check(False, f"the fixture run started ({res.stdout if res else None})")
        return
    env = {"ORCH_STATE_DIR": str(tasks / "sp05run")}
    run([BIN / "touch-status", "sp-01-a", "plan", "running", "attempt 1"],
        cwd=project, env=env)
    before = len(events_of(tasks))
    res = run([RUN, "start", "sp05run", "--spec", spec, "--no-daemons"], cwd=project)
    if res is None:
        return
    check(res.returncode == 0, f"a re-seed still succeeds (rc={res.returncode})")
    events = events_of(tasks)
    check(len(events) == before + 1,
          f"only the run card is re-emitted ({[e.get('plan') for e in events[before:]]})")
    check(all(not (e.get("plan") == "sp-01-a" and e.get("state") == "queued")
              for e in events[before:]),
          "no `queued` is written over a card that is already running")
    check("already on the stream" in res.stdout,
          f"...and the command says so ({res.stdout[-300:]})")


def test_start_refuses_a_sentinel_name(tmp):
    print("test_start_refuses_a_sentinel_name")
    # `HALT` in `ACTIVE` disarms the emergency brake, and `ACTIVE` as a task
    # name would have the command edit its own sentinel. Neither is a task.
    project, tasks, plan_file = make_project(tmp, "start-sentinel")
    spec = make_spec(tmp, "start-sentinel.json", project, plan_file=plan_file)
    for name in ("HALT", "ACTIVE", "../escape", "a/b"):
        res = run([RUN, "start", name, "--spec", spec], cwd=project)
        if res is None:
            continue
        check(res.returncode == 2,
              f"{name!r} is refused as a task name (rc={res.returncode})")
    check(not (tasks / "ACTIVE").exists(),
          "and none of them created a sentinel")


# --------------------------------------------------------------------------
# close
# --------------------------------------------------------------------------
def test_close_removes_only_its_own_active_line(tmp):
    print("test_close_removes_only_its_own_active_line")
    # The whole point of D-13's "the grep -vxF hazard dies here". Four sentinel
    # shapes, and the multi-line one carries a blank line and an indented line
    # this command never wrote: everything it does not own comes back verbatim.
    cases = {
        "multi": ("before-run\nsp05run\nafter-run\n", "before-run\nafter-run\n"),
        "single": ("sp05run\n", ""),
        "padded": ("keep-me\n\n  spaced-line  \nsp05run\n",
                   "keep-me\n\n  spaced-line  \n"),
        "unlisted": ("someone-else\n", "someone-else\n"),
    }
    for name, (before, after) in cases.items():
        project, tasks, spec, res = started(tmp, "close-" + name, active=before)
        if res is None or res.returncode != 0:
            check(False, f"[{name}] the fixture run started "
                         f"({res.stdout if res else None})")
            continue
        # `start` appends this task, so the sentinel now ends with its line —
        # except in the case that already had it.
        res = run([RUN, "close", "sp05run"], cwd=project)
        if res is None:
            continue
        check(res.returncode == 0,
              f"[{name}] close exits 0 (rc={res.returncode}, "
              f"{(res.stdout + res.stderr)[-200:]})")
        got = (tasks / "ACTIVE").read_text(encoding="utf-8")
        check(got == after,
              f"[{name}] only this task's line is gone ({got!r}, wanted {after!r})")

    # ...and an ABSENT sentinel is not created by a close-out. An empty ACTIVE
    # is an inert guard; a close is the last moment a command should arm one.
    project, tasks, spec, res = started(tmp, "close-absent")
    if res is not None and res.returncode == 0:
        (tasks / "ACTIVE").unlink()
        res = run([RUN, "close", "sp05run"], cwd=project)
        if res is not None:
            check(res.returncode == 0, f"close over no sentinel still exits 0 "
                                       f"(rc={res.returncode})")
        check(not (tasks / "ACTIVE").exists(),
              "and did not create one on the way out")


def test_close_never_fabricates_a_verdict(tmp):
    print("test_close_never_fabricates_a_verdict")
    # R-58. A plan whose agents all returned without a decisive verdict settles
    # `done` ("closed — no verdict"), NEVER `failed`; a fabricated FAILED badge
    # was a real defect and the rule that killed it is asserted here.
    project, tasks, spec, res = started(tmp, "close-verdict")
    if res is None or res.returncode != 0:
        check(False, f"the fixture run started ({res.stdout if res else None})")
        return
    res = run([RUN, "close", "sp05run"], cwd=project)
    if res is None:
        return
    last = events_of(tasks)[-1]
    check(last.get("plan") == "orchestrator" and last.get("stage") == "complete",
          f"close writes the terminal run event ({last})")
    check(last.get("state") == "done",
          f"...`done` by default, never an inferred `failed` ({last.get('state')})")
    check("no verdict" in (last.get("detail") or ""),
          f"...with the honest wording ({last.get('detail')!r})")
    check(last.get("w") == "agent",
          f"...and it went through status.sh ({last.get('w')!r})")

    # `failed` exists, and it is only ever the caller's explicit claim.
    project, tasks, spec, res = started(tmp, "close-failed")
    if res is not None and res.returncode == 0:
        run([RUN, "close", "sp05run", "--state", "failed",
             "--summary", 'stopped at sp-01: "must-green" loop red'], cwd=project)
        last = events_of(tasks)[-1]
        check(last.get("state") == "failed",
              f"--state failed is honoured when typed ({last.get('state')})")
        check('"' not in (last.get("detail") or ""),
              f"...and the detail is sanitised of double quotes, which do not "
              f"survive the bash/JS hops ({last.get('detail')!r})")
    res = run([RUN, "close", "sp05run", "--state", "cancelled"], cwd=project)
    if res is not None:
        check(res.returncode == 2,
              f"an invented state is a usage error, not a new badge "
              f"(rc={res.returncode})")


def test_close_defers_to_an_earlier_rung(tmp):
    print("test_close_defers_to_an_earlier_rung")
    # GD-D6 layers four sources, first to fire wins, and `close` is the
    # belt-and-braces rung. Two terminal events on one run is a card that flips
    # back and forth on replay, and a `killed` run that a later `done` overwrote
    # is exactly the R-58 failure one layer up.
    project, tasks, spec, res = started(tmp, "close-deferred")
    if res is None or res.returncode != 0:
        check(False, f"the fixture run started ({res.stdout if res else None})")
        return
    env = {"ORCH_STATE_DIR": str(tasks / "sp05run")}
    run([BIN / "touch-status", "orchestrator", "complete", "failed",
         "rung 1: snapshot says killed"], cwd=project, env=env)
    before = len(events_of(tasks))
    res = run([RUN, "close", "sp05run"], cwd=project)
    if res is None:
        return
    check(res.returncode == 0, f"close still exits 0 (rc={res.returncode})")
    check(len(events_of(tasks)) == before,
          f"no second terminal event was written "
          f"({[e.get('state') for e in events_of(tasks)[-2:]]})")
    check("already closed" in res.stdout,
          f"...and it says which rung got there first ({res.stdout[-300:]})")
    check(events_of(tasks)[-1].get("state") == "failed",
          "the earlier verdict stands — a close never overwrites one")


def test_close_stops_only_a_verified_pid(tmp):
    print("test_close_stops_only_a_verified_pid")
    project, tasks, spec, res = started(tmp, "close-pids")
    if res is None or res.returncode != 0:
        check(False, f"the fixture run started ({res.stdout if res else None})")
        return
    task_dir = tasks / "sp05run"

    # A pid that is ALIVE but is not a watcher: this test process. Pids are
    # reused, and a close-out that signalled a recorded number without checking
    # what now answers to it is how a run kills an editor.
    (task_dir / "watcher.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    # A pid that is definitely gone: a child that has already been reaped.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (task_dir / "cycle-reporter.pid").write_text(f"{dead.pid}\n", encoding="utf-8")

    res = run([RUN, "close", "sp05run"], cwd=project)
    if res is None:
        return
    check(res.returncode == 0, f"close exits 0 (rc={res.returncode})")
    check("REFUSING to signal a reused pid" in res.stdout,
          f"a live pid whose command line is not a watcher is NOT signalled "
          f"({res.stdout[-400:]})")
    check((task_dir / "watcher.pid").exists(),
          "...and its pid file is left for a human to look at")
    check("not running" in res.stdout,
          f"a stale pid is reported as stale ({res.stdout[-400:]})")
    check(not (task_dir / "cycle-reporter.pid").exists(),
          "...and its pid file is cleaned up")
    # The shared monitor is never stopped by a close: another run may still be
    # watching, and restarting it would mint a new token and break every open
    # dashboard.
    check("monitor: left running" in res.stdout,
          f"the shared monitor is left alone ({res.stdout[-200:]})")


# --------------------------------------------------------------------------
# bind / status
# --------------------------------------------------------------------------
def test_close_stops_a_real_daemon(tmp):
    print("test_close_stops_a_real_daemon")
    # The refusal path above proves close will not signal the WRONG process;
    # this proves it signals the right one. An orphaned watcher is the failure
    # the repo's commit gate exists for, so a close-out that silently failed to
    # stop one must not be able to ship green. No port is bound: the marker is
    # matched on the /proc command line, so a sleeper whose script is NAMED
    # `decision_watcher.py` is indistinguishable from a watcher to `stop()`.
    project, tasks, spec, res = started(tmp, "close-live")
    if res is None or res.returncode != 0:
        check(False, f"the fixture run started ({res.stdout if res else None})")
        return
    task_dir = tasks / "sp05run"
    sleeper = tmp / "close-live-daemon" / "decision_watcher.py"
    sleeper.parent.mkdir(parents=True, exist_ok=True)
    sleeper.write_text("import time\ntime.sleep(600)\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(sleeper)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        (task_dir / "watcher.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
        res = run([RUN, "close", "sp05run"], cwd=project)
        if res is None:
            return
        check(res.returncode == 0, f"close exits 0 (rc={res.returncode})")
        check(f"watcher: pid {proc.pid} stopped" in res.stdout,
              f"the watcher is reported stopped ({res.stdout[-400:]})")
        try:
            gone = proc.wait(timeout=10) is not None
        except subprocess.TimeoutExpired:
            gone = False
        check(gone, "...and the process is actually gone")
        check(not (task_dir / "watcher.pid").exists(),
              "...and its pid file was cleaned up")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def another_users_pid():
    """A live pid this user may not signal, or None if the box has none.

    `kill(pid, 0)` on it raises EPERM, which is the only way to reach the
    branch below without being root or spawning a setuid helper. It is
    DISCOVERED rather than assumed: pid 1 belongs to this same user inside a
    container, so hard-coding it would silently test nothing.
    """
    me = os.getuid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for name in entries:
        if not name.isdigit():
            continue
        try:
            if os.stat("/proc/" + name).st_uid == me:
                continue
            os.kill(int(name), 0)
        except PermissionError:
            return int(name)
        except OSError:
            continue
    return None


def test_close_reports_an_unsignalable_pid_honestly(tmp):
    print("test_close_reports_an_unsignalable_pid_honestly")
    # An EPERM pid and a reused pid are different findings, and only one of
    # them names a fact the code established. When `kill(pid, 0)` fails with
    # EPERM the command line was never read, so saying "its command line does
    # not carry 'decision_watcher' — a reused pid" would send a driver hunting
    # for pid reuse that nothing observed.
    pid = another_users_pid()
    if pid is None:
        skip("no other user's process on this machine — the EPERM branch of "
             "daemon_state cannot be reached without one")
        return
    project, tasks, spec, res = started(tmp, "close-eperm")
    if res is None or res.returncode != 0:
        check(False, f"the fixture run started ({res.stdout if res else None})")
        return
    task_dir = tasks / "sp05run"
    (task_dir / "watcher.pid").write_text(f"{pid}\n", encoding="utf-8")
    res = run([RUN, "close", "sp05run"], cwd=project)
    if res is None:
        return
    check("not signalable by this user" in res.stdout,
          f"an EPERM pid is reported as exactly that ({res.stdout[-400:]})")
    check("REFUSING to signal a reused pid" not in res.stdout,
          f"...and never as a reused pid, a fact nothing checked "
          f"({res.stdout[-400:]})")
    check((task_dir / "watcher.pid").exists(),
          "and its pid file is kept for a human to look at")


def test_close_escalates_and_only_then_says_stopped(tmp):
    print("test_close_escalates_and_only_then_says_stopped")
    # "stopped" is the one sentence in a close-out a driver acts on, so it is
    # only printed after the process is CONFIRMED gone — and the confirmation
    # must not misfire on the normal escalation. This daemon ignores SIGTERM, so
    # close has to fall through the full wait and SIGKILL it, and must then
    # still report the truth: stopped, pid file cleaned up, process gone.
    #
    # The trap being guarded is a reaped-pending corpse: `kill(pid, 0)` keeps
    # succeeding for a zombie whose parent (this test) has not called wait()
    # yet, so a liveness-only re-check would report a dead daemon as a
    # surviving orphan on EVERY close-out.
    project, tasks, spec, res = started(tmp, "close-stubborn")
    if res is None or res.returncode != 0:
        check(False, f"the fixture run started ({res.stdout if res else None})")
        return
    task_dir = tasks / "sp05run"
    sleeper = tmp / "close-stubborn-daemon" / "decision_watcher.py"
    sleeper.parent.mkdir(parents=True, exist_ok=True)
    sleeper.write_text("import signal, time\n"
                       "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                       "time.sleep(600)\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(sleeper)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.5)                 # let it install the handler before TERM
        (task_dir / "watcher.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
        res = run([RUN, "close", "sp05run"], cwd=project)
        if res is None:
            return
        check(res.returncode == 0, f"close exits 0 (rc={res.returncode})")
        check(f"watcher: pid {proc.pid} stopped" in res.stdout,
              f"a daemon that ignored SIGTERM is escalated to SIGKILL and "
              f"reported stopped — not as a surviving orphan "
              f"({res.stdout[-400:]})")
        check("did NOT die" not in res.stdout,
              f"...the orphan report is NOT printed for a process that died "
              f"({res.stdout[-400:]})")
        try:
            gone = proc.wait(timeout=10) is not None
        except subprocess.TimeoutExpired:
            gone = False
        check(gone, "...and the process is actually gone")
        check(not (task_dir / "watcher.pid").exists(),
              "...and its pid file was cleaned up")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_bind_launches_both_daemons_on_the_bound_journal(tmp):
    print("test_bind_launches_both_daemons_on_the_bound_journal")
    # Three claims in one arm, all about `launch()` — the code path every other
    # arm skips with --no-daemons. (1) it records a pid and a log beside the
    # task; (2) an explicit --tasks-root reaches the DAEMON, not only the
    # command: a monitor that re-resolved its own root would list a different
    # set of tasks than the one this command just wrote into; (3) BOTH per-run
    # daemons are started here, and the watcher gets the bound directory in
    # ARGV — its first rung.
    #
    # (3) is the one that matters most. `decision_watcher` resolves its journal
    # ONCE at import (argv > $ORCH_WF_DIR > orch-config.json > the newest
    # `wf_*` anywhere under the configuration directory) and never re-resolves,
    # so a watcher started with none of the first three attaches to whatever
    # run was newest ON THE MACHINE and replays that run's plan cards, verdicts
    # and token totals into this task's stream — measured at 192 foreign events
    # in 25 seconds. Recording `wf_dir` in the config afterwards cannot repair a
    # process that already read it. Passing it in argv is what makes the answer
    # unambiguous, so it is asserted at the DAEMON's own command line.
    root, record = fake_root(tmp, "bind-launch-root")
    project, tasks, plan_file = make_project(tmp, "bind-launch")
    elsewhere = tmp / "bind-launch-root-dir"
    elsewhere.mkdir()
    spec = make_spec(tmp, "bind-launch.json", project, plan_file=plan_file)
    res = run([root / "bin" / "touch-run", "start", "sp05run", "--spec", spec,
               "--tasks-root", elsewhere, "--no-daemons"], cwd=project)
    if res is None or res.returncode != 0:
        check(False, f"the fixture run started ({res.stdout if res else None})")
        return
    wf = tmp / "wf_0badc0de5678"
    wf.mkdir()
    (wf / "journal.jsonl").write_text("{}\n", encoding="utf-8")
    res = run([root / "bin" / "touch-run", "bind", "sp05run", "--wf-dir", wf,
               "--tasks-root", elsewhere], cwd=project)
    if res is None:
        return
    check(res.returncode == 0,
          f"bind exits 0 (rc={res.returncode}, {(res.stdout + res.stderr)[-300:]})")
    task_dir = elsewhere / "sp05run"
    pid_file = task_dir / "cycle-reporter.pid"
    check(pid_file.is_file() and pid_file.read_text(encoding="utf-8").strip().isdigit(),
          f"the reporter's pid is recorded ({pid_file})")
    check((task_dir / "cycle-reporter.log").is_file(),
          "...and its output goes to a log beside it")
    started_calls = wait_for_calls(record, "touch-cycle-reporter", 1)
    check(len(started_calls) == 1,
          f"the reporter was started exactly once ({len(started_calls)})")
    if not started_calls:
        return
    launched = started_calls[0]
    check(launched["argv"] == [str(wf)],
          f"...with the bound wf_dir as its argument ({launched['argv']})")
    check(launched["env"].get("ORCH_STATE_DIR") == str(task_dir),
          f"...ORCH_STATE_DIR pointing at the task folder "
          f"({launched['env'].get('ORCH_STATE_DIR')!r})")
    check(launched["env"].get("ORCH_TASKS_ROOT") == str(elsewhere),
          f"...and the explicit --tasks-root handed down, so the daemon reads "
          f"the tree the command wrote into "
          f"({launched['env'].get('ORCH_TASKS_ROOT')!r})")

    watcher_calls = wait_for_calls(record, "touch-watcher", 1)
    check(len(watcher_calls) == 1,
          f"the watcher was started exactly once, by bind ({len(watcher_calls)})")
    if watcher_calls:
        w = watcher_calls[0]
        check(w["argv"] == [str(wf)],
              f"...with the bound wf_dir in ARGV — the watcher's FIRST rung, so "
              f"it can never fall through to the newest run on the machine "
              f"({w['argv']})")
        check(w["env"].get("ORCH_STATE_DIR") == str(task_dir),
              f"...writing into this task's stream and no other "
              f"({w['env'].get('ORCH_STATE_DIR')!r})")
    watcher_pid = task_dir / "watcher.pid"
    check(watcher_pid.is_file()
          and watcher_pid.read_text(encoding="utf-8").strip().isdigit(),
          f"and its pid is recorded so close can stop it ({watcher_pid})")


def test_start_launches_a_watcher_only_when_there_is_one_to_launch(tmp):
    print("test_start_launches_a_watcher_only_when_there_is_one_to_launch")
    # The negative half of the arm above, and the only one that runs `start`'s
    # daemon block. `start` runs BEFORE the Workflow it prints has been
    # launched, so at that moment this run has no journal in existence; a
    # watcher started there would bind — once, at import — to the newest run on
    # the machine. So it must NOT be started, and the command must say why
    # rather than quietly doing nothing.
    #
    # Then the resume half: re-run over a task whose wf_dir is already recorded
    # and the watcher IS started, with that directory in argv. "Deferred" is a
    # decision about what is knowable, not a blanket refusal.
    root, record = fake_root(tmp, "start-watcher-root")
    project, tasks, plan_file = make_project(tmp, "start-watcher")
    spec = make_spec(tmp, "start-watcher.json", project, plan_file=plan_file)
    # A port nothing on this machine is serving: the monitor branch is not what
    # this arm is about, and the shipped 8931 may well be answered by a real
    # monitor belonging to whoever is running the suite.
    res = run([root / "bin" / "touch-run", "start", "sp05run", "--spec", spec,
               "--port", "18937"], cwd=project, timeout=180)
    if res is None:
        return
    check(res.returncode == 0,
          f"start exits 0 with the daemons enabled "
          f"(rc={res.returncode}, {(res.stdout + res.stderr)[-400:]})")
    task_dir = tasks / "sp05run"
    check("watcher: deferred to `touch-run bind`" in res.stdout,
          f"it says the watcher is deferred, and why ({res.stdout[-600:]})")
    # The monitor shim records its call and exits, which is exactly the shape
    # of a monitor that cannot start (a taken port, a broken payload). Silence
    # there would mean five seconds of nothing followed by a dashboard URL for
    # a server that is already dead, with the reason unread in the log.
    check("exited before it answered /health" in res.stdout,
          f"a monitor that dies immediately is REPORTED, not waited out and "
          f"then called started ({res.stdout[-600:]})")
    check(not (task_dir / "watcher.pid").exists(),
          "no watcher pid is recorded — `launch()` writes it before the command "
          "can return, so its absence is conclusive")
    check(calls(record, "touch-watcher") == [],
          f"and no watcher was started at all "
          f"({calls(record, 'touch-watcher')})")

    # The resume case: bind records the journal, a later start finds it.
    wf = tmp / "wf_0badc0de9999"
    wf.mkdir()
    (wf / "journal.jsonl").write_text("{}\n", encoding="utf-8")
    res = run([root / "bin" / "touch-run", "bind", "sp05run", "--wf-dir", wf,
               "--no-daemons"], cwd=project)
    if res is None or res.returncode != 0:
        check(False, f"bind exits 0 ({(res.stdout + res.stderr)[-300:] if res else None})")
        return
    res = run([root / "bin" / "touch-run", "start", "sp05run", "--spec", spec,
               "--port", "18937"], cwd=project, timeout=180)
    if res is None:
        return
    check(res.returncode == 0,
          f"a re-start over a bound task exits 0 (rc={res.returncode})")
    started_watchers = wait_for_calls(record, "touch-watcher", 1)
    check(len(started_watchers) == 1,
          f"NOW the watcher is started ({len(started_watchers)})")
    if started_watchers:
        check(started_watchers[0]["argv"] == [str(wf)],
              f"...on the recorded journal, passed in argv "
              f"({started_watchers[0]['argv']})")
    check(f"tailing {wf}" in res.stdout,
          f"...and it says which journal it attached to ({res.stdout[-500:]})")

    # And a re-run never leaves two watchers on one task: they would race the
    # same `.watcher-state.json` checkpoint, which is the file that makes a
    # relaunch safe. The predecessor is dealt with — stopped, or reported
    # stale — before the replacement is started.
    res = run([root / "bin" / "touch-run", "start", "sp05run", "--spec", spec,
               "--port", "18937"], cwd=project, timeout=180)
    if res is not None:
        check("stop watcher:" in res.stdout,
              f"a re-start deals with the recorded watcher before starting "
              f"another ({res.stdout[-500:]})")
        check(len(wait_for_calls(record, "touch-watcher", 2)) == 2,
              f"...and there is exactly one launch per start, not a growing "
              f"pile ({len(calls(record, 'touch-watcher'))})")


def test_start_tells_a_foreign_monitor_from_this_projects(tmp):
    print("test_start_tells_a_foreign_monitor_from_this_projects")
    # A `/health` answer on 127.0.0.1:<port> proves a monitor is there; it does
    # NOT prove it is OURS. One monitor serves every task on a PROJECT, not on
    # a machine, and 8931 is a convention every checkout shares — so another
    # project's server answers the probe, `start` reports "already serving",
    # prints a dashboard URL, and the run never appears on it. Silent, and the
    # same shape as the tasks-root mistake this command exists to end.
    #
    # This is the ONE arm that binds a socket, and the doctrine it bends is
    # worth restating: no listener, because a suite that occupies a FIXED port
    # fails differently on a busy machine. Port 0 has no such failure — the
    # kernel hands out an unused one — and the branch under test cannot be
    # reached without something answering. Nothing here starts a Touch daemon:
    # the stand-in serves one route, and the watcher is deferred anyway.
    import http.server
    import threading

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                    # noqa: N802
            body = b'{"status": "ok", "streams": {}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sleeper_proc = None
    try:
        project, tasks, plan_file = make_project(tmp, "foreign-monitor")
        spec = make_spec(tmp, "foreign-monitor.json", project, plan_file=plan_file)
        res = run([RUN, "start", "sp05run", "--spec", spec, "--port", str(port)],
                  cwd=project, timeout=180)
        if res is None:
            return
        check(res.returncode == 0,
              f"start exits 0 (rc={res.returncode}, "
              f"{(res.stdout + res.stderr)[-400:]})")
        check("records no live monitor of its own" in res.stdout,
              f"a server that answers but is not this project's is REPORTED as "
              f"that, not as a shared one ({res.stdout[-600:]})")
        check("already serving" not in res.stdout,
              f"...and is never called 'already serving' ({res.stdout[-400:]})")
        check(not (tasks / "monitor.pid").exists(),
              "and no second monitor is started onto the occupied port")

        # The positive branch: a live process this project recorded, whose
        # command line carries the monitor's own marker. Same technique as the
        # close arm — the marker is matched on /proc, so a sleeper NAMED
        # monitor_server.py is indistinguishable from a monitor.
        sleeper = tmp / "foreign-monitor-daemon" / "monitor_server.py"
        sleeper.parent.mkdir(parents=True, exist_ok=True)
        sleeper.write_text("import time\ntime.sleep(600)\n", encoding="utf-8")
        sleeper_proc = subprocess.Popen([sys.executable, str(sleeper)],
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        (tasks / "monitor.pid").write_text(f"{sleeper_proc.pid}\n",
                                           encoding="utf-8")
        res = run([RUN, "start", "sp05run", "--spec", spec, "--port", str(port)],
                  cwd=project, timeout=180)
        if res is None:
            return
        check("already serving on %d — left alone" % port in res.stdout,
              f"this project's own live monitor IS left alone — restarting it "
              f"would mint a new token and break every open dashboard "
              f"({res.stdout[-500:]})")
    finally:
        server.shutdown()
        server.server_close()
        if sleeper_proc is not None and sleeper_proc.poll() is None:
            sleeper_proc.kill()
            sleeper_proc.wait(timeout=10)


def test_bind_records_and_renders(tmp):
    print("test_bind_records_and_renders")
    project, tasks, spec, res = started(tmp, "bind-run")
    if res is None or res.returncode != 0:
        check(False, f"the fixture run started ({res.stdout if res else None})")
        return
    wf = tmp / "wf_0badc0de1234"
    wf.mkdir()
    (wf / "journal.jsonl").write_text("{}\n", encoding="utf-8")
    res = run([RUN, "bind", "sp05run", "--wf-dir", wf, "--no-daemons"], cwd=project)
    if res is None:
        return
    check(res.returncode == 0,
          f"bind exits 0 (rc={res.returncode}, {(res.stdout + res.stderr)[-300:]})")
    config = json.loads((tasks / "sp05run" / "orch-config.json").read_text(
        encoding="utf-8"))
    check(config.get("wf_dir") == str(wf),
          f"wf_dir is recorded for the NEXT reader — a hand-started watcher, "
          f"the reporter, a later `touch-run status` ({config})")
    check(config.get("resume_from_run_id") == "wf_0badc0de1234",
          f"the run id is the wf_dir basename ({config.get('resume_from_run_id')})")
    resume = tasks / "sp05run" / "plan" / "RESUME.md"
    check(resume.is_file(), "plan/RESUME.md was rendered")
    if not resume.is_file():
        return
    text = resume.read_text(encoding="utf-8")
    check("wf_0badc0de1234" in text and str(wf) in text,
          "the values are substituted, not left as placeholders")
    check("resumeFromRunId" in text,
          "it carries the exact resume incantation")
    # "Procedures linked, not restated" (D-13): a RESUME that re-states the
    # daemon protocol is a fourth copy of it to keep in step.
    for pointer in ("monitor/SKILL.md", "network-recovery.md",
                    "monitoring.md"):
        check(pointer in text, f"...and links {pointer} rather than restating it")
    check("touch-run close sp05run" in text,
          "and names its own close-out command")


def test_status_reports_and_writes_nothing(tmp):
    print("test_status_reports_and_writes_nothing")
    project, tasks, spec, res = started(tmp, "status-run")
    if res is None or res.returncode != 0:
        check(False, f"the fixture run started ({res.stdout if res else None})")
        return
    before = sorted(str(p.relative_to(tasks)) for p in tasks.rglob("*"))
    events_before = (tasks / "sp05run" / "events.jsonl").read_bytes()
    res = run([RUN, "status", "sp05run"], cwd=project)
    if res is None:
        return
    check(res.returncode == 0, f"status exits 0 (rc={res.returncode})")
    for want in ("tasks root:", "wf_dir:", "run scope:", "watcher:", "stream:",
                 "run close:"):
        check(want in res.stdout, f"it reports {want!r}")
    check("ACTIVE lists this task" in res.stdout,
          f"...and says the run scope is armed ({res.stdout[:400]})")
    after = sorted(str(p.relative_to(tasks)) for p in tasks.rglob("*"))
    check(before == after,
          f"status created nothing (appeared: {sorted(set(after) - set(before))})")
    check((tasks / "sp05run" / "events.jsonl").read_bytes() == events_before,
          "and wrote no event")

    # An unknown task is a failed report, not a created folder.
    res = run([RUN, "status", "never-started"], cwd=project)
    if res is not None:
        check(res.returncode != 0,
              f"status on an unknown task exits non-zero (rc={res.returncode})")
    check(not (tasks / "never-started").exists(),
          "and did not create it by asking")


def test_tasks_root_override(tmp):
    print("test_tasks_root_override")
    # The ladder is the shipped one (`aggregator.paths.tasks_root`): an explicit
    # argument beats `$ORCH_TASKS_ROOT`, which beats the project walk-up. Asked
    # of the resolver rather than reimplemented here, so a later correction to
    # it reaches anyone who came in through bin/.
    project, tasks, plan_file = make_project(tmp, "root-override")
    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir()
    res = run([RUN, "status", "sp05run", "--tasks-root", elsewhere], cwd=project)
    if res is not None:
        check(f"tasks root: {elsewhere}" in res.stdout,
              f"--tasks-root wins ({res.stdout.splitlines()[:1]})")
    res = run([RUN, "status", "sp05run"], cwd=project,
              env={"ORCH_TASKS_ROOT": str(elsewhere)})
    if res is not None:
        check(f"tasks root: {elsewhere}" in res.stdout,
              f"$ORCH_TASKS_ROOT wins over the walk-up "
              f"({res.stdout.splitlines()[:1]})")
    res = run([RUN, "status", "sp05run"], cwd=project)
    if res is not None:
        check(f"tasks root: {tasks}" in res.stdout,
              f"and the walk-up finds the project's own ({res.stdout.splitlines()[:1]})")


def main():
    if not RUN.is_file():
        print("test_wrapper_shape")
        check(False, "plugin/touch/bin/touch-run exists")
    else:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            test_wrapper_shape(tmp)
            test_verify_accepts_a_good_spec(tmp)
            test_verify_catches_a_planted_placeholder(tmp)
            test_verify_catches_a_missing_plan_file(tmp)
            test_verify_catches_a_damaged_script(tmp)
            test_verify_pins_the_copy_to_the_template(tmp)
            test_start_seeds_everything_exactly(tmp)
            test_start_folds_the_denominator(tmp)
            test_start_refuses_and_creates_nothing(tmp)
            test_start_refuses_a_malformed_envelope(tmp)
            test_start_seeds_the_roster_file(tmp)
            test_start_never_walks_a_card_backwards(tmp)
            test_start_refuses_a_sentinel_name(tmp)
            test_close_removes_only_its_own_active_line(tmp)
            test_close_never_fabricates_a_verdict(tmp)
            test_close_defers_to_an_earlier_rung(tmp)
            test_close_stops_only_a_verified_pid(tmp)
            test_close_stops_a_real_daemon(tmp)
            test_close_reports_an_unsignalable_pid_honestly(tmp)
            test_close_escalates_and_only_then_says_stopped(tmp)
            test_bind_launches_both_daemons_on_the_bound_journal(tmp)
            test_start_launches_a_watcher_only_when_there_is_one_to_launch(tmp)
            test_start_tells_a_foreign_monitor_from_this_projects(tmp)
            test_bind_records_and_renders(tmp)
            test_status_reports_and_writes_nothing(tmp)
            test_tasks_root_override(tmp)
    print()
    if skips:
        print(f"skipped: {len(skips)} check(s)")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all touch-run tests passed")


if __name__ == "__main__":
    main()
