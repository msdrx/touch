#!/usr/bin/env python3
"""`cycle_reporter.py`'s status.sh resolver: the plugin's own copy, or nothing.

Item 02 (PLUGIN-RUNTIME-3). Run as `python3 test_cycle_reporter.py`; exits
non-zero on failure. No pytest, no runner — `run_all.sh` picks it up by its
`test_*.py` glob.

WHAT THIS FILE IS FOR
---------------------
`Reporter._find_status_sh` used to try `$ORCH_STATE_DIR/../../shared/
monitoring/status.sh` — a monitoring copy inside the reported-on project's own
dot-claude directory — BEFORE the plugin's own copy, and the path it returns is
handed to `bash`
(`emit_close`). So on a consumer's machine the plugin executed a script the
reported-on project supplies, at whatever version that project happens to
carry, with the plugin's authority; and in this repo, where such a file used to
exist, it shadowed the payload copy so drift between the two was invisible in
development. Post-GD-U1 the payload copy IS the canonical monitoring module, so
the project-side rung has no legitimate resolution target anywhere and is
deleted rather than reordered.

The resolver keys off `__file__`, so the only honest way to test it is to build
a throwaway plugin tree, copy the module into it, and import THAT copy — the
fixtures below plant a decoy project-level `status.sh` in every case, and no
assertion is satisfied by a path under the project directory. One arm reads the
shipped source instead (the project candidate must not creep back in via a
different spelling), and one pins the real payload's own resolution.

That source arm judges CODE only — the docstring quotes the deleted candidate
on purpose — so it is deliberately paired with a WHOLE-FILE arm for the text
`tests/test_skills_payload.py` bans in shipped payload. Attempt 1 of this item
put the banned dev-repo path into the new docstring and the code-only arm could
not see it; the whole-file arm exists so this file's own suite catches that.
"""
import ast
import contextlib
import importlib.util
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

# Set before any payload module is imported: the payload tree is what ships,
# and a test run must not leave `.pyc` droppings in it (item 06).
sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _roots  # noqa: E402

REPORTER_SRC = (_roots.PAYLOAD / "skills" / "implement" / "templates"
                / "cycle_reporter.py")

#: Banned in any file under `plugin/touch/skills/` — kept in sync with
#: `tests/test_skills_payload.py`'s BANNED table (that file owns the rule; this
#: is the owning sub-plan's local copy of the one entry this change can break).
#: Assembled from fragments so THIS file never contains the literal either.
#: That is belt-and-braces, NOT a requirement: the ban is scoped to files under
#: `plugin/touch/skills/` and this file is in `tests/`. It costs nothing and
#: survives a future widening of the scan.
BANNED_MONITORING_DIR = ".claude" + "/shared/monitoring"

failures = []
_mod_seq = [0]
_loaded = []          # module names registered in sys.modules by load_module


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        failures.append(msg)
        print(f"  FAIL: {msg}")


def load_module(path, name=None):
    """Import a cycle_reporter.py copy under its own module name."""
    if name is None:
        _mod_seq[0] += 1
        name = f"_cycle_reporter_fixture_{_mod_seq[0]}"
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    _loaded.append(name)          # unregistered in main(); most of these are
    spec.loader.exec_module(mod)  # backed by a TemporaryDirectory that dies
    return mod


def split_docstring(src):
    """Split a function's source into (docstring, body), by AST identity.

    NOT a split on the last triple-quote: "everything after the final triple
    quote" holds only while the function has exactly one triple-quoted string,
    and a second one added later would silently discard every line before it —
    including a re-added project candidate — while the arms below printed "ok".
    NOT `src.replace(fn.__doc__, "")` either: since 3.13 the compiler strips a
    docstring's common leading indentation, so `__doc__` is no longer a
    substring of the source and the replace is a no-op that fails open.
    """
    lines = src.splitlines()
    fn = ast.parse(src).body[0]
    doc = ast.get_docstring(fn) or ""
    if doc:
        head = fn.body[0]
        del lines[head.lineno - 1:head.end_lineno]
    return doc, "\n".join(lines)


def returns_none(node):
    """True for `return` and `return None` — i.e. a non-answer."""
    v = node.value
    return v is None or (isinstance(v, ast.Constant) and v.value is None)


def make_tree(tmp, plugin_copy=True, project_copy=True):
    """A throwaway plugin + project pair.

    Returns (module, task_dir, plugin_status, project_status). The project copy
    is the DECOY: it sits exactly where the deleted candidate looked, so any
    resolver that still consults `self.task` returns it and the arms below go
    red instead of silently passing.
    """
    tmp = Path(tmp)
    templates = tmp / "plugin" / "touch" / "skills" / "implement" / "templates"
    templates.mkdir(parents=True)
    shutil.copy2(REPORTER_SRC, templates / "cycle_reporter.py")

    plugin_status = tmp / "plugin" / "touch" / "shared" / "monitoring" / "status.sh"
    if plugin_copy:
        plugin_status.parent.mkdir(parents=True)
        plugin_status.write_text("#!/usr/bin/env bash\n# plugin copy\n",
                                 encoding="utf-8")
        os.chmod(plugin_status, 0o755)

    project_status = tmp / "proj" / ".claude" / "shared" / "monitoring" / "status.sh"
    if project_copy:
        project_status.parent.mkdir(parents=True)
        project_status.write_text("#!/usr/bin/env bash\n# project copy\n",
                                  encoding="utf-8")
        os.chmod(project_status, 0o755)

    task = tmp / "proj" / ".claude" / "local-orchestrators" / "t1"
    task.mkdir(parents=True)

    mod = load_module(templates / "cycle_reporter.py")
    return mod, task, plugin_status, project_status


def resolve(mod, task, emit_status=True):
    """Construct a Reporter and hand back (status_sh, stderr)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rep = mod.Reporter(str(task), [], emit_status=emit_status)
    return rep.status_sh, err.getvalue()


def test_plugin_copy_wins():
    print("test_plugin_copy_wins")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, plugin_status, _ = make_tree(tmp)
        got, err = resolve(mod, task)
        check(got is not None and os.path.samefile(got, plugin_status),
              "both copies present: the plugin-relative status.sh is chosen")
        check(got is not None and "# plugin copy" in Path(got).read_text(encoding="utf-8"),
              "the chosen file is the plugin's script, not the project's")
        proj = os.path.realpath(str(Path(tmp) / "proj")) + os.sep
        check(got is not None and not os.path.realpath(got).startswith(proj),
              "the resolved path lies outside the reported-on project tree")
        check(err == "", "a successful resolution says nothing on stderr")


def test_project_copy_is_never_a_fallback():
    print("test_project_copy_is_never_a_fallback")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, _, _ = make_tree(tmp, plugin_copy=False)
        got, err = resolve(mod, task)
        check(got is None,
              "plugin copy missing: the resolver returns None, not the project copy")
        # The whole project subtree, not just the decoy file: this catches a
        # future message that echoes `self.task` in any spelling.
        check(str(Path(tmp) / "proj") not in (err or ""),
              "the error names nothing inside the reported-on project tree")
        check("payload incomplete" in err and "status.sh not found" in err,
              "the missing payload is reported clearly on stderr")


def test_render_only_is_quiet():
    print("test_render_only_is_quiet")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, _, _ = make_tree(tmp, plugin_copy=False)
        got, err = resolve(mod, task, emit_status=False)
        check(got is None, "--no-status with no payload copy: still None")
        check(err == "",
              "--no-status never emits events, so the absence is not warned about")


def test_shipped_source_has_no_project_candidate():
    print("test_shipped_source_has_no_project_candidate")
    mod = load_module(REPORTER_SRC, name="_cycle_reporter_shipped")
    src = textwrap.dedent(inspect.getsource(mod.Reporter._find_status_sh))
    doc, body = split_docstring(src)
    # Positive evidence the strip cut the docstring and ONLY the docstring.
    # The probe is the docstring's own first line, read at runtime, so no
    # phrase from the shipped prose is pinned here (rewording the docstring
    # must never be what turns this file red).
    probe = doc.splitlines()[0].strip() if doc else ""
    check(bool(probe) and probe in src and probe not in body,
          "the docstring is removed by the strip")
    check("os.path.isfile" in body, "the code survives the strip")
    # Comments quote the deleted candidate on purpose too; drop them as well.
    body = "\n".join(line for line in body.splitlines()
                     if not line.lstrip().startswith("#"))
    check("self.task" not in body,
          "_find_status_sh derives no candidate from the task folder")
    check(".claude" not in body,
          "_find_status_sh names no project-side .claude path")
    # Behavioural, not spelling-based: counting `os.path.isfile` would go red
    # on a rename to `Path(cand).is_file()` and — worse — would miss a second
    # candidate probed with a different spelling. Exactly one `return` in the
    # whole function may hand back a path.
    fn = ast.parse(src).body[0]
    answers = [n for n in ast.walk(fn)
               if isinstance(n, ast.Return) and not returns_none(n)]
    check(len(answers) == 1,
          "exactly one code path returns a candidate, whatever it is spelled like")
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    check("task" not in attrs,
          "the resolver reads no `task`-named attribute of the reporter")
    check("deleted" in doc and "project" in doc.lower(),
          "the docstring records that the project-side candidate was deleted")


def test_shipped_source_has_no_banned_payload_text():
    """The whole file, prose included — `tests/test_skills_payload.py`'s ban.

    The arm above judges code only, so the docstring is free to explain the
    deletion; this one makes sure the explanation does not SPELL the dev repo's
    monitoring directory, which is banned in every file under
    `plugin/touch/skills/` (it does not exist in any installer's project). That
    gate lives in another sub-plan's file; this keeps the constraint enforced
    where the offending file is owned.
    """
    print("test_shipped_source_has_no_banned_payload_text")
    text = REPORTER_SRC.read_text(encoding="utf-8")
    check(BANNED_MONITORING_DIR not in text,
          "the shipped file never spells the dev repo's monitoring dir "
          "(test_skills_payload BANNED)")
    check("/home/" not in text and "/Users/" not in text,
          "the shipped file carries no absolute home path")


def test_real_payload_resolves_to_its_own_copy():
    print("test_real_payload_resolves_to_its_own_copy")
    mod = load_module(REPORTER_SRC, name="_cycle_reporter_shipped_real")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        got, err = resolve(mod, tmp)
        check(got is not None and os.path.samefile(got, _roots.MON / "status.sh"),
              "the installed payload resolves to plugin/touch/shared/monitoring/status.sh")
        check(err == "", "the real payload is complete: nothing on stderr")


# -- the protocol-close pass ------------------------------------------------
# The template closes `divide` and `finalgate` itself via runStatus (R-09), but
# the workflow runtime has no Node API, so those calls no-op; the reporter's
# evaluate_protocol_closes() is who actually emits them. These arms drive a
# fixture run through pass_once() with a RECORDING status.sh and assert the
# exact events the template would have emitted — including the exactly-once
# guarantee across a daemon restart.

RECORDING_STATUS_SH = (
    '#!/usr/bin/env bash\n'
    '# recording stub: plan|stage|state|msg|ORCH_PLANS_TOTAL in calls.log, and\n'
    '# — separately, so the arms above keep their five-field equality checks —\n'
    '# plan|ORCH_ROSTER in roster.log whenever a roster travels.\n'
    'printf \'%s|%s|%s|%s|%s\\n\' "$1" "$2" "$3" "$4" "${ORCH_PLANS_TOTAL:-}" '
    '>> "$(dirname "$0")/calls.log"\n'
    'if [ -n "${ORCH_ROSTER:-}" ]; then\n'
    '  printf \'%s|%s\\n\' "$1" "$ORCH_ROSTER" >> "$(dirname "$0")/roster.log"\n'
    'fi\n'
    '# ...and it APPENDS THE LINE IT STANDS FOR to events.jsonl. A writer that\n'
    '# records its arguments but leaves no stream is not a writer: the reporter\n'
    '# is stream-authoritative (settle() re-emits whatever events.jsonl does not\n'
    '# carry), so a streamless stub would make it re-emit forever and every arm\n'
    '# built on it would be measuring a fiction. No caps and no flock here —\n'
    '# those are the real writer\'s job, and make_real_run() is what tests them.\n'
    'python3 - "$@" <<\'PY\'\n'
    'import json, os, sys\n'
    'ev = {"ts": "2026-01-01T00:00:00.000+00:00", "plan": sys.argv[1],\n'
    '      "stage": sys.argv[2], "state": sys.argv[3], "detail": sys.argv[4],\n'
    '      "w": "agent"}\n'
    'roster = os.environ.get("ORCH_ROSTER")\n'
    'if roster and os.path.isfile(roster):\n'
    '    with open(roster, encoding="utf-8", errors="replace") as f:\n'
    '        ev["roster"] = [ln for ln in f.read().splitlines() if ln]\n'
    'with open(os.path.join(os.environ["ORCH_STATE_DIR"], "events.jsonl"), "a",\n'
    '          encoding="utf-8") as f:\n'
    '    f.write(json.dumps(ev) + "\\n")\n'
    'PY\n'
    'exit 0\n')


def make_run(tmp):
    """make_tree + a recording status.sh, a wf dir, and pinned caps."""
    mod, task, plugin_status, _ = make_tree(tmp)
    plugin_status.write_text(RECORDING_STATUS_SH, encoding="utf-8")
    os.chmod(plugin_status, 0o755)
    wf = Path(tmp) / "wf_fixture"
    wf.mkdir()
    (task / "orch-config.json").write_text(
        json.dumps({"max_plan_attempts": 4, "max_finalgate_attempts": 2}),
        encoding="utf-8")
    return mod, task, wf, plugin_status.parent / "calls.log"


def make_real_run(tmp):
    """make_run, but with the REAL payload status.sh as the writer.

    The stub above is right for equality-checking a close's four arguments; it
    is wrong for anything that has to observe the STREAM, because it never
    writes one. `--settle`'s whole idempotency claim is against events.jsonl,
    and the roster bounds are enforced inside the real writer, so those arms
    run the shipped script and read the JSON it appends.
    """
    mod, task, plugin_status, _ = make_tree(tmp)
    shutil.copy2(_roots.MON / "status.sh", plugin_status)
    os.chmod(plugin_status, 0o755)
    wf = Path(tmp) / "wf_fixture"
    wf.mkdir()
    (task / "orch-config.json").write_text(
        json.dumps({"max_plan_attempts": 4, "max_finalgate_attempts": 2}),
        encoding="utf-8")
    return mod, task, wf


def plant(wf, aid, marker, result):
    """One agent transcript carrying `marker` + its journal result record."""
    (wf / f"agent-{aid}.jsonl").write_text(
        json.dumps({"type": "user", "text": marker}) + "\n", encoding="utf-8")
    with open(wf / "journal.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "result", "agentId": aid,
                            "result": result}) + "\n")


def spawn(wf, aid, marker):
    """The transcript + a `started` journal record and NO result.

    This is what "zero returns" looks like on disk: agents that went out and
    never came back. Without the `started` records the reporter cannot tell
    that apart from a plan that never ran, which is exactly why it reads them.
    """
    (wf / f"agent-{aid}.jsonl").write_text(
        json.dumps({"type": "user", "text": marker}) + "\n", encoding="utf-8")
    with open(wf / "journal.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "started", "agentId": aid,
                            "key": aid}) + "\n")


def calls(log):
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@contextlib.contextmanager
def exported(**kv):
    """Run the block with `kv` in this process's environment, then restore.

    The reporter builds its child env from `os.environ`, so the only honest way
    to prove it drops the seed variables is to actually export them here.
    """
    saved = {k: os.environ.get(k) for k in kv}
    os.environ.update(kv)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def events(task):
    """events.jsonl as parsed dicts (the real writer's own record)."""
    path = Path(task) / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_divide_closes_done_with_plans_total():
    print("test_divide_closes_done_with_plans_total")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, log = make_run(tmp)
        plant(wf, "a1", "[monitor] plan=divide stage=partition role=synth attempt=1",
              {"subplans": [{"id": "sp-a", "files": ["a.py"]},
                            {"id": "sp-b", "files": ["b.py", "b2.py"]},
                            {"id": "sp-c", "files": ["c.py"]}],
               "subplans_file": "x", "summary": "s"})
        rep = mod.Reporter(str(task), [str(wf)])
        rep.pass_once()
        got = calls(log)
        check(got == ["divide|plan|done|3 sub-plans|5",
                      "orchestrator|divide|info|roster: 3 sub-plans|5"],
              f"divide close emitted once with the template message + N+2 total, "
              f"and the roster follows it on the ORCHESTRATOR card (got {got})")
        rep.pass_once()
        check(calls(log) == got, "a second pass emits nothing new")
        rep2 = mod.Reporter(str(task), [str(wf)])   # daemon restart
        rep2.pass_once()
        check(calls(log) == got,
              "a restarted reporter re-ingests but never re-emits (emitted persists)")
        pages = os.listdir(task / "report" / "cycles")
        check(not any(p.startswith("divide") for p in pages),
              "protocol plans render no cycle pages")
        # GD-D11: the roster travels as a FILE PATH, never env-inlined JSON.
        roster_file = task / "roster.txt"
        check(roster_file.is_file(),
              "the reporter materialises the roster as a file")
        if roster_file.is_file():
            check(roster_file.read_text(encoding="utf-8").splitlines()
                  == ["sp-a", "sp-b", "sp-c"],
                  "one entry per line, in touch-run start's own roster.txt shape")
        rlog = calls(log.parent / "roster.log")
        check(rlog == [f"orchestrator|{roster_file}"],
              f"ORCH_ROSTER carried the PATH, and only on the orchestrator "
              f"event ({rlog})")


def test_divide_closes_failed_like_the_template():
    print("test_divide_closes_failed_like_the_template")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, log = make_run(tmp)
        plant(wf, "a1", "[monitor] plan=divide stage=partition role=synth attempt=1",
              {"subplans": [], "subplans_file": "x", "summary": "s"})
        mod.Reporter(str(task), [str(wf)]).pass_once()
        check(calls(log) == ["divide|plan|failed|divider produced no sub-plans|"],
              "an empty partition closes failed with the template's message")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, log = make_run(tmp)
        plant(wf, "a1", "[monitor] plan=divide stage=partition role=synth attempt=1",
              {"subplans": [{"id": "sp-a", "files": ["a.py"]},
                            {"id": "sp-b", "files": ["a.py"]}],
               "subplans_file": "x", "summary": "s"})
        mod.Reporter(str(task), [str(wf)]).pass_once()
        check(calls(log) == ["divide|plan|failed|partition not isolated: a.py has two owners|"],
              "a duplicate owner closes failed exactly like the isolation guard")


def test_finalgate_closes():
    print("test_finalgate_closes")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, log = make_run(tmp)
        plant(wf, "a1", "[monitor] plan=finalgate stage=sweep role=test attempt=1",
              {"passed": True, "summary": "ok", "findings_file": "f"})
        mod.Reporter(str(task), [str(wf)]).pass_once()
        check(calls(log) == ["finalgate|plan|done|aggregate sweep green|"],
              "a green sweep closes finalgate done")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, log = make_run(tmp)
        plant(wf, "a1", "[monitor] plan=finalgate stage=sweep role=test attempt=1",
              {"passed": False, "summary": "red", "findings_file": "f"})
        rep = mod.Reporter(str(task), [str(wf)])
        rep.pass_once()
        check(calls(log) == [],
              "a red sweep below the cap with no fixer verdict stays open")
        plant(wf, "a2", "[monitor] plan=finalgate stage=sweep role=test attempt=2",
              {"passed": False, "summary": "red", "findings_file": "f"})
        rep.pass_once()
        check(calls(log) == ["finalgate|plan|failed|sweep not green after 2 attempts|"],
              "a red sweep at the cap closes failed with the template's message")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, log = make_run(tmp)
        plant(wf, "a1", "[monitor] plan=finalgate stage=sweep role=test attempt=1",
              {"passed": False, "summary": "red", "findings_file": "f"})
        plant(wf, "a2", "[monitor] plan=finalgate stage=implement role=impl attempt=1",
              {"done": False, "files_changed": [], "summary": "gave up"})
        mod.Reporter(str(task), [str(wf)]).pass_once()
        check(calls(log) == ["finalgate|plan|failed|sweep not green after 2 attempts|"],
              "a fixer that returned done=false ends the retry loop early, as the template does")


def test_sp_loop_close_still_works():
    print("test_sp_loop_close_still_works")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, log = make_run(tmp)
        plant(wf, "a1", "[monitor] plan=sp-x stage=implement role=impl attempt=1",
              {"done": True, "files_changed": ["a.py"], "summary": "s"})
        plant(wf, "a2", "[monitor] plan=sp-x stage=test role=test attempt=1",
              {"passed": True, "summary": "ok", "findings_file": "f"})
        plant(wf, "a3", "[monitor] plan=sp-x stage=critique role=critique attempt=1",
              {"approved": True, "summary": "ok", "findings_file": "f",
               "depth": "in-scope", "critical_defect": False})
        mod.Reporter(str(task), [str(wf)]).pass_once()
        check(calls(log) == ["sp-x|plan|done|green on attempt 1/4|"],
              "the loop-close pass is untouched: a green loop still emits its close")
        check((task / "report" / "cycles" / "sp-x-cycle-1.html").is_file(),
              "the loop still renders its cycle page")


# -- the requirement → implemented → Δ diagram -------------------------------
# What every page now LEADS with, and the one thing on it that no other recorded
# value implies: the divider's `finding_ids` are the REQUIREMENT, the
# implementer's `items` are the IMPLEMENTATION, and the two read-only verdicts'
# `deviations` are the DIFFERENCE. Three recorded inputs, zero inference — so
# these tests are as much about what the renderer may NOT say when an input is
# missing as about what it says when all three are there. A diagram that
# quietly rounds silence up to coverage would be worse than the wall of prose it
# replaced, because it would be short AND wrong.

def coverage_run(tmp, *, items, gate_devs=(), crit_devs=(),
                 finding_ids=("R-01", "R-02", "R-03"), partition=True):
    """One rendered cycle carrying (optionally) the coverage fields."""
    mod, task, wf, _log = make_run(tmp)
    if partition:
        plant(wf, "d1",
              "[monitor] plan=divide stage=partition role=synth attempt=1",
              {"subplans": [{"id": "sp-a", "title": "the diagram",
                             "files": ["a.py"],
                             "finding_ids": list(finding_ids),
                             "slice_file": "/t/plan/slice-a.md"}],
               "subplans_file": "/t/plan/x-subplans.md", "summary": "one"})
    plant(wf, "a1", "[monitor] plan=sp-a stage=implement role=impl attempt=1",
          {"done": True, "files_changed": ["a.py"], "summary": "implemented",
           "items": list(items)})
    plant(wf, "a2", "[monitor] plan=sp-a stage=test role=test attempt=1",
          {"passed": not gate_devs, "summary": "gate said so",
           "findings_file": "", "deviations": list(gate_devs)})
    plant(wf, "a3", "[monitor] plan=sp-a stage=critique role=critique attempt=1",
          {"approved": not crit_devs, "summary": "critique said so",
           "findings_file": "", "depth": "in-scope", "critical_defect": False,
           "deviations": list(crit_devs)})
    rep = mod.Reporter(str(task), [str(wf)], emit_status=False)
    rep.pass_once()
    return mod, task, wf, (task / "report" / "cycles"
                           / "sp-a-cycle-1.html").read_text(encoding="utf-8")


def test_the_diagram_carries_requirement_implementation_and_delta():
    print("test_the_diagram_carries_requirement_implementation_and_delta")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        _mod, _task, _wf, page = coverage_run(
            tmp,
            items=[{"id": "R-01", "status": "done", "note": "built as decided"},
                   {"id": "R-02", "status": "partial", "note": "only half"},
                   {"id": "R-99", "status": "done", "note": "drive-by fix"}],
            gate_devs=[{"id": "R-03", "kind": "missing",
                        "what": "nothing in the tree implements it"}],
            crit_devs=[{"id": "R-02", "kind": "differs",
                        "what": "shape drifts from the decided approach"}])
        for rid in ("R-01", "R-02", "R-03", "R-99"):
            check(rid in page, f"the diagram carries a row for {rid}")
        for chip in ("✓ 1/3 requirements done", "◐ 1 partial", "? 1 unreported",
                     "+ 1 extra", "Δ 2 gate/critique deviations"):
            check(chip in page, f"the counts chip reads {chip!r}")
        for text in ("only half", "nothing in the tree implements it",
                     "shape drifts from the decided approach"):
            check(text in page, f"the Δ column carries {text!r}")
        # Color is never the only channel: every verdict cell pairs a glyph and
        # a word with it, so the diagram survives grayscale and a text scrape.
        for pair in ("✓ done", "◐ partial", "? unreported", "+ extra",
                     "✗ missing", "≠ differs"):
            check(pair in page, f"glyph+word travel together: {pair!r}")
        check("R-99" in page and "not among the sub-plan" not in page.split(
            "R-99")[0], "the extra id is rendered as a row, not dropped")


def test_silence_about_an_item_is_a_gap_never_coverage():
    print("test_silence_about_an_item_is_a_gap_never_coverage")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        _mod, _task, _wf, page = coverage_run(
            tmp, items=[{"id": "R-01", "status": "done", "note": "did it"}])
        check("✓ 1/3 requirements done" in page,
              "an item the implementer never mentioned does not count as done")
        check("? 2 unreported" in page,
              "…it counts as UNREPORTED, and the count says how many")
        check("R-02" in page and "R-03" in page,
              "…and each silent requirement still gets its own row")
        check("never mentioned this requirement" in page,
              "the Δ column names the silence rather than leaving the row blank")


def test_an_unknown_requirement_list_is_stated_never_assumed():
    print("test_an_unknown_requirement_list_is_stated_never_assumed")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        _mod, _task, _wf, page = coverage_run(
            tmp, partition=False,
            items=[{"id": "R-01", "status": "done", "note": "did it"},
                   {"id": "R-77", "status": "done", "note": "did this too"}])
        check("the ids the AGENTS named" in page,
              "with no partition on record the page says whose ids these are")
        check("+ 1 extra" not in page and "+ extra" not in page,
              "…and calls nothing `extra`: membership is unknowable without "
              "the requirement list, and a guess would accuse the implementer")
        check("✓ 2/2 requirements done" in page,
              "…while still reporting what WAS recorded")


def test_a_journal_without_the_coverage_fields_says_so():
    print("test_a_journal_without_the_coverage_fields_says_so")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        _mod, task, _wf, page = coverage_run(tmp, partition=False, items=[])
        check("No requirement coverage recorded" in page,
              "an older or foreign journal renders a STATED ABSENCE")
        check("0/0" not in page and "✓ 0/" not in page,
              "…never a zero-of-zero that reads like a met requirement")
        check("Requirement → implemented → Δ" in page,
              "the section is still there, so the gap is visible where the "
              "diagram would be")
        index = (task / "report" / "cycles" / "index.html").read_text(
            encoding="utf-8")
        check("not reported" in index,
              "the overview says the same thing in its coverage column")


def test_every_surface_reports_the_same_coverage():
    print("test_every_surface_reports_the_same_coverage")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, page = coverage_run(
            tmp,
            items=[{"id": "R-01", "status": "done", "note": "done"},
                   {"id": "R-02", "status": "skipped", "note": "out of scope"}],
            gate_devs=[{"id": "R-03", "kind": "missing", "what": "absent"}])
        rep = mod.Reporter(str(task), [str(wf)], emit_status=False)
        rep.ingest()
        final = Path(rep.render_final()).read_text(encoding="utf-8")
        index = (task / "report" / "cycles" / "index.html").read_text(
            encoding="utf-8")
        # ONE fold, three renderings: the cycle page, the overview and the final
        # report cannot disagree about a sub-plan, because they call the same
        # method on the same records.
        for name, text in (("cycle page", page), ("index", index),
                           ("final report", final)):
            check("1/3" in text, f"the {name} reports 1 of 3 requirements done")
            check("1 skipped" in text, f"the {name} reports the skipped item")
        check("○ skipped" in page, "the cycle page names the skipped status")
        check("the divider's partition" in final,
              "the final report names where its requirement column comes from")


# -- scattered transcripts (the /clear rotation) -----------------------------
# The harness keys the transcript dir to the ACTIVE session id; /clear rotates
# that id mid-run while the journal stays at its launch-time path, so one run's
# transcripts scatter across sibling session dirs. The pre-fix reporter looked
# only in its argv dirs AND cached the miss forever — on the 2026-07-29 run it
# rendered nothing and emitted no loop close for hours after the first /clear
# while the watcher (which globs siblings) narrated the same agents fine.

def make_sessions_run(tmp):
    """make_tree + recording status.sh + a harness-shaped session-dir pair.

    Returns (mod, task, wf_a, wf_b, log). wf_a is the argv/journal dir at
    <root>/<proj-slug>/<session-a>/subagents/workflows/wf_scatter; wf_b is a
    sibling session's dir of the SAME run name, reachable only through the
    glob. `mod.WF_GLOB_ROOT` is pointed at the fixture root — the shipped
    default reads `ORCH_WF_GLOB_ROOT`, the watcher's own knob.
    """
    mod, task, plugin_status, _ = make_tree(tmp)
    plugin_status.write_text(RECORDING_STATUS_SH, encoding="utf-8")
    os.chmod(plugin_status, 0o755)
    (task / "orch-config.json").write_text(
        json.dumps({"max_plan_attempts": 4, "max_finalgate_attempts": 2}),
        encoding="utf-8")
    root = Path(tmp) / "sessions"
    wf_a = root / "proj-slug" / "session-a" / "subagents" / "workflows" / "wf_scatter"
    wf_b = root / "proj-slug" / "session-b" / "subagents" / "workflows" / "wf_scatter"
    wf_a.mkdir(parents=True)
    wf_b.mkdir(parents=True)
    mod.WF_GLOB_ROOT = str(root)
    return mod, task, wf_a, wf_b, plugin_status.parent / "calls.log"


def plant_split(journal_wf, transcript_wf, aid, marker, result):
    """Like plant(), but the transcript and the journal record live apart."""
    (transcript_wf / f"agent-{aid}.jsonl").write_text(
        json.dumps({"type": "user", "text": marker}) + "\n", encoding="utf-8")
    with open(journal_wf / "journal.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "result", "agentId": aid,
                            "result": result}) + "\n")


def test_scattered_transcripts_still_resolve():
    print("test_scattered_transcripts_still_resolve")
    check("ORCH_WF_GLOB_ROOT" in REPORTER_SRC.read_text(encoding="utf-8"),
          "the glob root is env-overridable under the watcher's own knob name")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf_a, wf_b, log = make_sessions_run(tmp)
        plant_split(wf_a, wf_a, "a1", "[monitor] plan=sp-sc stage=implement role=impl attempt=1",
                    {"done": True, "files_changed": ["a.py"], "summary": "s"})
        # the /clear happened here: gate + critique transcripts land in the sibling
        plant_split(wf_a, wf_b, "a2", "[monitor] plan=sp-sc stage=test role=test attempt=1",
                    {"passed": True, "summary": "ok", "findings_file": "f"})
        plant_split(wf_a, wf_b, "a3", "[monitor] plan=sp-sc stage=critique role=critique attempt=1",
                    {"approved": True, "summary": "ok", "findings_file": "f",
                     "depth": "in-scope", "critical_defect": False})
        rep = mod.Reporter(str(task), [str(wf_a)])
        rep.pass_once()
        check(calls(log) == ["sp-sc|plan|done|green on attempt 1/4|"],
              "a loop whose gate/critique transcripts sit in a sibling session dir still closes")
        check((task / "report" / "cycles" / "sp-sc-cycle-1.html").is_file(),
              "its cycle page renders from the argv dir's journal alone")
        check(rep.pending == [], "nothing stays parked once every marker resolves")


def test_marker_miss_is_retried_not_cached():
    print("test_marker_miss_is_retried_not_cached")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf_a, wf_b, log = make_sessions_run(tmp)
        # journal record lands FIRST; the transcript does not exist anywhere yet
        with open(wf_a / "journal.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "result", "agentId": "a9",
                                "result": {"done": True, "files_changed": [],
                                           "summary": "s"}}) + "\n")
        rep = mod.Reporter(str(task), [str(wf_a)])
        rep.pass_once()
        check(len(rep.pending) == 1 and rep.plan_order == [],
              "an unresolvable record parks in pending instead of being dropped")
        # the transcript appears later, in the sibling dir no argv names
        (wf_b / "agent-a9.jsonl").write_text(
            json.dumps({"type": "user",
                        "text": "[monitor] plan=sp-late stage=implement role=impl attempt=1"})
            + "\n", encoding="utf-8")
        rep.pass_once()
        check(rep.pending == [] and "sp-late" in rep.plan_order,
              "the parked record routes on a later pass — a miss is never cached")
        check((task / "report" / "cycles" / "sp-late-cycle-1.html").is_file(),
              "and its cycle page renders then")


# -- the research protocol (D-14) -------------------------------------------
# research's two plans have no impl->test->critique cycle, so the loops
# pass cannot see them, and the template cannot close them itself for the same
# R-09 reason `divide`/`finalgate` cannot. The rule is R-58's: `done` on the
# first result that carries what the plan produces, `failed` ONLY on zero
# returns — and zero returns is a terminal fact, so it is settle's to state.

RESEARCH_KEYS = ("prior-art", "data-model", "economics")


def plant_research_board(wf, returning=3, synth=True):
    """`returning` of three researchers come back; optionally the synthesizer."""
    for i, key in enumerate(RESEARCH_KEYS):
        aid = f"r{i}"
        marker = f"[monitor] plan=research stage={key} role=research attempt=1"
        spawn(wf, aid, marker)
        if i < returning:
            plant(wf, aid, marker,
                  {"findings": [{"id": f"{key}-1"}],
                   "findings_file": f"/t/findings/research-{key}-attempt-1.md",
                   "summary": "s"})
    if synth:
        marker = "[monitor] plan=synthesis stage=synthesize role=synth attempt=1"
        spawn(wf, "s0", marker)
        plant(wf, "s0", marker,
              {"plan_file": "/t/plan/x-plan.md", "item_count": 26, "summary": "s"})


def test_research_plans_close_done_with_two():
    print("test_research_plans_close_done_with_two")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, log = make_run(tmp)
        plant_research_board(wf)
        mod.Reporter(str(task), [str(wf)]).pass_once()
        got = calls(log)
        check(got == ["research|plan|done|3 of 3 research reports in when the card closed|2",
                      "synthesis|plan|done|plan written: 26 items|2"],
              f"both research cards close done, with ORCH_PLANS_TOTAL=2 at the "
              f"barrier (got {got})")
        check(not (task / "report" / "cycles").exists()
              or not any(p.startswith(("research", "synthesis"))
                         for p in os.listdir(task / "report" / "cycles")),
              "neither renders a cycle page — they have no cycle")


def test_research_close_wording_survives_the_late_reports():
    print("test_research_close_wording_survives_the_late_reports")
    # The card closes on the FIRST report (D-14) and its detail is then
    # terminal, but on a real run the six researchers return minutes apart and
    # the 2 s poll fires on the first one. The permanent badge therefore has to
    # be a sentence that is still true after the other five land.
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, log = make_run(tmp)
        markers = [f"[monitor] plan=research stage={key} role=research attempt=1"
                   for key in RESEARCH_KEYS]
        for i, marker in enumerate(markers):
            spawn(wf, f"r{i}", marker)

        def report(i):
            plant(wf, f"r{i}", markers[i],
                  {"findings": [{"id": f"{RESEARCH_KEYS[i]}-1"}],
                   "findings_file": f"/t/findings/research-{RESEARCH_KEYS[i]}.md",
                   "summary": "s"})

        report(0)
        rep = mod.Reporter(str(task), [str(wf)])
        rep.pass_once()                       # the poll that catches the first
        report(1)
        report(2)
        rep.pass_once()                       # the other two, minutes later
        got = calls(log)
        check(got == ["research|plan|done|1 of 3 research reports in when the "
                      "card closed|2"],
              f"one close, worded for the instant it was taken — still true "
              f"once all three are in ({got})")
        check(len(rep.research["research"]["returned"]) == 3,
              "…and the later reports were ingested, not dropped")


def test_partial_board_is_never_a_fabricated_failure():
    print("test_partial_board_is_never_a_fabricated_failure")
    # research.workflow.js REFUSES to synthesize from a partial board and
    # throws; the honest close for that stop is the watcher's layered run close
    # (D-07). A card whose agents did return must never wear `failed` here.
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, log = make_run(tmp)
        plant_research_board(wf, returning=1, synth=False)
        rep = mod.Reporter(str(task), [str(wf)])
        rep.pass_once()
        check(calls(log) == ["research|plan|done|1 of 3 research reports in when the card closed|2"],
              f"one report of three closes the card DONE, with truthful counts "
              f"({calls(log)})")
        rep.settle()
        check(calls(log) == ["research|plan|done|1 of 3 research reports in when the card closed|2"],
              f"and settle adds nothing: `synthesis` never spawned, so no rule "
              f"implies a close for it ({calls(log)})")


def test_zero_returns_close_failed_only_at_settle():
    print("test_zero_returns_close_failed_only_at_settle")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, log = make_run(tmp)
        plant_research_board(wf, returning=0, synth=False)
        rep = mod.Reporter(str(task), [str(wf)])
        rep.pass_once()
        check(calls(log) == [],
              f"a live pass over three spawned-but-silent researchers says "
              f"NOTHING — 'not yet' and 'never' look the same ({calls(log)})")
        rep.settle()
        check(calls(log) == ["research|plan|failed|no research report returned "
                             "(0 of 3 spawned)|2"],
              f"settle — where the run is declared over — states the zero-return "
              f"failure, with the count it is claiming ({calls(log)})")


# -- --settle: gap-fill, diffed against the STREAM ---------------------------

def test_settle_emits_only_the_missing_closes_and_is_idempotent():
    print("test_settle_emits_only_the_missing_closes_and_is_idempotent")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf = make_real_run(tmp)
        plant(wf, "a1", "[monitor] plan=divide stage=partition role=synth attempt=1",
              {"subplans": [{"id": "sp-a", "title": "the first", "files": ["a.py"]},
                            {"id": "sp-b", "files": ["b.py"]}],
               "subplans_file": "x", "summary": "s"})
        plant(wf, "a2", "[monitor] plan=sp-a stage=implement role=impl attempt=1",
              {"done": True, "files_changed": ["a.py"], "summary": "s"})
        plant(wf, "a3", "[monitor] plan=sp-a stage=test role=test attempt=1",
              {"passed": True, "summary": "ok", "findings_file": "f"})
        plant(wf, "a4", "[monitor] plan=sp-a stage=critique role=critique attempt=1",
              {"approved": True, "summary": "ok", "findings_file": "f",
               "depth": "in-scope", "critical_defect": False})
        # The GAP: `divide` was already closed on the stream by another writer
        # (the watcher's run-end pass, a human, a previous close-out). Settle
        # must leave that one alone and write only what is missing.
        (task / "events.jsonl").write_text(json.dumps(
            {"ts": "2026-01-01T00:00:00.000+00:00", "plan": "divide",
             "stage": "plan", "state": "done", "detail": "3 sub-plans",
             "w": "watcher"}) + "\n", encoding="utf-8")

        wrote = mod.Reporter(str(task), [str(wf)]).settle()
        got = [(e["plan"], e["stage"], e["state"]) for e in events(task)]
        check(got == [("divide", "plan", "done"),
                      ("sp-a", "plan", "done"),
                      ("orchestrator", "divide", "info")],
              f"settle wrote the missing loop close and the missing roster, and "
              f"did NOT re-close divide ({got})")
        check("sp-a" in wrote and "divide" not in wrote,
              f"...and says so ({wrote})")
        written = events(task)[1]
        check(written.get("w") == "agent" and written.get("detail")
              == "green on attempt 1/4",
              f"the close went through status.sh — w:agent, writer-formatted "
              f"detail ({written})")

        # Twice-run emits nothing — and the checkpoint is DELETED first, so the
        # only thing standing between this run and a duplicate close is the
        # stream itself (stream_plan_closes).
        (task / ".cycle-reporter-state.json").unlink()
        again = mod.Reporter(str(task), [str(wf)]).settle()
        check(again == [] and len(events(task)) == 3,
              f"a second settle emits nothing, checkpoint or no checkpoint "
              f"({again}, {len(events(task))} events)")


def test_settle_never_invents_a_verdict_for_an_open_loop():
    print("test_settle_never_invents_a_verdict_for_an_open_loop")
    # A loop that stopped mid-attempt — implementer returned, gate never did —
    # implies no close at all. R-58: the watcher's run-end pass owns that card,
    # and a fabricated FAILED badge was a real defect once.
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf = make_real_run(tmp)
        plant(wf, "a1", "[monitor] plan=sp-open stage=implement role=impl attempt=1",
              {"done": True, "files_changed": ["a.py"], "summary": "s"})
        wrote = mod.Reporter(str(task), [str(wf)]).settle()
        check(wrote == [] and events(task) == [],
              f"nothing implied, nothing written ({wrote}, {events(task)})")


# -- the roster on the wire (GD-D11) ----------------------------------------

def test_roster_event_is_bounded_at_the_writer():
    print("test_roster_event_is_bounded_at_the_writer")
    # monitor.html bounds the roster on the way IN (200 entries, 300 chars).
    # status.sh now bounds it on the way OUT, so an oversized roster never
    # reaches the append-only file at all.
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        task = Path(tmp) / "task"
        task.mkdir()
        roster = Path(tmp) / "roster.txt"
        roster.write_text("".join(f"sp-{i:03d} — {'t' * 500}\n"
                                  for i in range(250)), encoding="utf-8")
        env = {**os.environ, "ORCH_STATE_DIR": str(task),
               "ORCH_ROSTER": str(roster)}
        rc = subprocess.run(["bash", str(_roots.MON / "status.sh"),
                             "orchestrator", "divide", "info", "roster: 250"],
                            env=env, capture_output=True, text=True)
        check(rc.returncode == 0, f"status.sh still exits 0 (rc={rc.returncode})")
        evs = events(task)
        check(len(evs) == 1, f"one line was appended ({len(evs)})")
        if evs:
            got = evs[0].get("roster")
            check(isinstance(got, list) and len(got) == 200,
                  f"250 entries are capped at 200 at the writer "
                  f"({len(got) if isinstance(got, list) else got})")
            check(isinstance(got, list) and got
                  and all(len(e) <= 300 for e in got),
                  "every entry is capped at 300 chars")
            check(evs[0].get("w") == "agent",
                  f"the roster line is attributed like every status.sh line "
                  f"({evs[0].get('w')})")
    # An ORCH_ROSTER naming nothing readable warns and omits the key; a
    # monitoring call must never break an agent.
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        task = Path(tmp) / "task"
        task.mkdir()
        env = {**os.environ, "ORCH_STATE_DIR": str(task),
               "ORCH_ROSTER": str(Path(tmp) / "nope.txt")}
        rc = subprocess.run(["bash", str(_roots.MON / "status.sh"),
                             "sp-x", "plan", "done", "ok"],
                            env=env, capture_output=True, text=True)
        evs = events(task)
        check(rc.returncode == 0 and len(evs) == 1 and "roster" not in evs[0],
              f"an unreadable ORCH_ROSTER omits the key and still writes the "
              f"event (rc={rc.returncode}, {evs})")
        check("ORCH_ROSTER" in rc.stderr,
              f"...and says so on stderr ({rc.stderr.strip()[:120]!r})")


def roster_event(tmp, body):
    """Run the SHIPPED status.sh over a roster file of exactly `body`."""
    task = Path(tmp) / "task"
    task.mkdir()
    roster = Path(tmp) / "roster.txt"
    roster.write_text(body, encoding="utf-8")
    rc = subprocess.run(
        ["bash", str(_roots.MON / "status.sh"), "orchestrator", "divide",
         "info", "roster"],
        env={**os.environ, "ORCH_STATE_DIR": str(task),
             "ORCH_ROSTER": str(roster)},
        capture_output=True, text=True)
    evs = events(task)
    return rc, (evs[0] if evs else {})


def test_roster_file_cap_counts_bytes_and_keeps_whole_entries():
    print("test_roster_file_cap_counts_bytes_and_keeps_whole_entries")
    # The file ceiling is a READ ceiling, so it has to count what it reads:
    # bytes. Every entry this repo writes carries an em dash (3 bytes, one
    # character), so a character count under-reports the read by a third and
    # the ceiling has to clear the largest roster the entry caps allow.
    cap = int(re.search(r"ROSTER_FILE_CAP = (\d+) \* 1024",
                        (_roots.MON / "status.sh").read_text(encoding="utf-8"))
              .group(1)) * 1024
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        # A MAXIMAL legitimate roster: 200 entries of 300 characters, em dashes
        # throughout — 60 K characters, ~180 KB. Not one entry may be lost.
        body = "".join(f"sp-{i:03d} — {'—' * 292}\n" for i in range(200))
        check(len(body.encode("utf-8")) > 128 * 1024,
              f"the fixture really is bigger than a naive ceiling would be "
              f"({len(body.encode('utf-8'))} bytes)")
        rc, ev = roster_event(tmp, body)
        check(rc.returncode == 0 and len(ev.get("roster") or []) == 200,
              f"a maximal 200x300 roster survives the byte ceiling whole "
              f"({len(ev.get('roster') or [])} entries)")
        check("larger than" not in rc.stderr,
              f"…and is not reported as oversized ({rc.stderr.strip()[:120]!r})")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        # The boundary: a file whose LAST entry ends exactly one byte past the
        # ceiling — i.e. its newline is the (cap+1)th byte. Every entry read is
        # complete, so nothing may be dropped for being at the edge.
        line = "x" * 2047 + "\n"
        full = (cap + 1) // len(line) - 1
        rest = (cap + 1) - full * len(line)
        body = line * full + "y" * (rest - 1) + "\n"
        check(len(body.encode("utf-8")) == cap + 1, "the fixture sits on the edge")
        rc, ev = roster_event(tmp, body)
        check(rc.returncode == 0 and len(ev.get("roster") or []) == full + 1,
              f"an entry that ends ON the boundary is kept, not dropped as a "
              f"fragment ({len(ev.get('roster') or [])} of {full + 1})")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        # And a genuinely truncated read still drops its half-entry.
        body = "z" * (cap + 10) + "\n"
        rc, ev = roster_event(tmp, body)
        check(rc.returncode == 0 and "roster" not in ev
              and "larger than" in rc.stderr,
              f"a file cut mid-entry ships nothing rather than half a title "
              f"({ev.get('roster')}, {rc.stderr.strip()[:80]!r})")


def test_reporter_roster_reaches_the_stream_bounded():
    print("test_reporter_roster_reaches_the_stream_bounded")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf = make_real_run(tmp)
        plant(wf, "a1", "[monitor] plan=divide stage=partition role=synth attempt=1",
              {"subplans": [{"id": f"sp-{i:03d}", "title": "t" * 500,
                             "files": [f"{i}.py"]} for i in range(250)],
               "subplans_file": "x", "summary": "s"})
        mod.Reporter(str(task), [str(wf)]).pass_once()
        evs = [e for e in events(task) if e.get("roster")]
        check(len(evs) == 1, f"exactly one roster line reached the stream ({len(evs)})")
        if evs:
            check(evs[0]["plan"] == "orchestrator",
                  f"on the reserved orchestrator card, where readers honor it "
                  f"({evs[0]['plan']})")
            check(len(evs[0]["roster"]) == 200
                  and all(len(e) <= 300 for e in evs[0]["roster"]),
                  f"bounded end to end ({len(evs[0]['roster'])} entries)")
            check(evs[0].get("plans_total") == 252,
                  f"and re-declares the denominator, N+2 (got "
                  f"{evs[0].get('plans_total')})")


def test_seed_env_never_leaks_into_the_stream():
    print("test_seed_env_never_leaks_into_the_stream")
    # `status.sh` folds ORCH_TITLE / ORCH_PLANS_TOTAL / ORCH_ROSTER into every
    # line it writes, and this daemon inherits the shell that started it. The
    # other two writers of those keys drop them by name
    # (decision_watcher.STATUS_ENV_DROP, touch-run's emit); this one must too.
    # plans_total is the sharp end: readers fold it MONOTONIC-MAX, so a leaked
    # 99 is irreversible on that stream and on every replay of it.
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf = make_real_run(tmp)
        check(mod.STATUS_ENV_DROP
              == ("ORCH_TITLE", "ORCH_PLANS_TOTAL", "ORCH_ROSTER"),
              f"the three seed variables are named as a constant, like the "
              f"watcher's ({mod.STATUS_ENV_DROP})")
        leak = Path(tmp) / "leak-roster.txt"
        leak.write_text("zz-leak — not this run\n", encoding="utf-8")
        plant(wf, "a1", "[monitor] plan=divide stage=partition role=synth attempt=1",
              {"subplans": [{"id": "sp-a", "title": "the only one",
                             "files": ["a.py"]}],
               "subplans_file": "x", "summary": "s"})
        plant(wf, "a2", "[monitor] plan=sp-a stage=implement role=impl attempt=1",
              {"done": True, "files_changed": ["a.py"], "summary": "s"})
        plant(wf, "a3", "[monitor] plan=sp-a stage=test role=test attempt=1",
              {"passed": True, "summary": "ok", "findings_file": "f"})
        plant(wf, "a4", "[monitor] plan=sp-a stage=critique role=critique attempt=1",
              {"approved": True, "summary": "ok", "findings_file": "f",
               "depth": "in-scope", "critical_defect": False})
        with exported(ORCH_TITLE="LEAKED TITLE", ORCH_PLANS_TOTAL="99",
                      ORCH_ROSTER=str(leak)):
            mod.Reporter(str(task), [str(wf)]).pass_once()
        evs = events(task)
        by = {(e.get("plan"), e.get("stage")): e for e in evs}
        check(evs and not any("title" in e for e in evs),
              f"no line wears the inherited title ({[e.get('title') for e in evs]})")
        loop = by.get(("sp-a", "plan"))
        check(loop is not None and "plans_total" not in loop
              and "roster" not in loop,
              f"an ordinary loop close declares neither key ({loop})")
        divide = by.get(("divide", "plan"))
        check(divide is not None and divide.get("plans_total") == 3,
              f"the divide close carries ITS OWN N+2, not the leaked 99 "
              f"({divide.get('plans_total') if divide else None})")
        roster = by.get(("orchestrator", "divide"))
        check(roster is not None and roster.get("roster") == ["sp-a — the only one"],
              f"and the roster is the divider's partition, not the exported "
              f"file ({roster.get('roster') if roster else None})")


def test_settle_replaces_a_seeded_roster_with_the_dividers():
    print("test_settle_replaces_a_seeded_roster_with_the_dividers")
    # `touch-run start` seeds a roster from the run SPEC. The divider's
    # partition is the later and truer of the two, so "some roster exists" must
    # not suppress it — which is what settle's boolean did, on exactly the
    # recovery path settle exists for.
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf = make_real_run(tmp)
        plant(wf, "a1", "[monitor] plan=divide stage=partition role=synth attempt=1",
              {"subplans": [{"id": "sp-real", "title": "the real one",
                             "files": ["a.py"]}],
               "subplans_file": "x", "summary": "s"})
        (task / "events.jsonl").write_text(json.dumps(
            {"ts": "2026-01-01T00:00:00.000+00:00", "plan": "orchestrator",
             "stage": "launch", "state": "running",
             "detail": "launched by touch-run start", "w": "agent",
             "roster": ["guess-1 — from the spec"]}) + "\n", encoding="utf-8")

        wrote = mod.Reporter(str(task), [str(wf)]).settle()
        rosters = [e["roster"] for e in events(task) if e.get("roster")]
        check(rosters[-1:] == [["sp-real — the real one"]],
              f"settle emits the divider's roster over the seeded guess ({rosters})")
        check(mod.ROSTER_SENTINEL in wrote, f"...and says it wrote one ({wrote})")

        # Now the last roster on the stream IS this reporter's: a second
        # close-out — checkpoint deleted, so only the stream can stop it —
        # writes nothing.
        (task / ".cycle-reporter-state.json").unlink()
        again = mod.Reporter(str(task), [str(wf)]).settle()
        rosters = [e["roster"] for e in events(task) if e.get("roster")]
        check(again == [] and len(rosters) == 2,
              f"a settle whose roster is already the last one on the stream "
              f"emits nothing ({again}, {len(rosters)} roster lines)")


def test_settle_repairs_a_checkpoint_that_claims_an_unwritten_close():
    print("test_settle_repairs_a_checkpoint_that_claims_an_unwritten_close")
    # An emission can be RECORDED and still fail: status.sh can exit non-zero
    # (its own mkdir failed, the append failed) or time out under flock
    # contention, after which the checkpoint claims a line the stream does not
    # carry. `--settle` is the one mechanism that repairs that loss, so the
    # stream has to be what it believes — in BOTH directions. A checkpoint that
    # merely agrees with the stream cannot tell the two apart, which is why the
    # fixture below makes them DISAGREE.
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf = make_real_run(tmp)
        plant(wf, "a1", "[monitor] plan=divide stage=partition role=synth attempt=1",
              {"subplans": [{"id": "sp-a", "title": "the first", "files": ["a.py"]}],
               "subplans_file": "x", "summary": "s"})
        plant(wf, "a2", "[monitor] plan=sp-a stage=implement role=impl attempt=1",
              {"done": True, "files_changed": ["a.py"], "summary": "s"})
        plant(wf, "a3", "[monitor] plan=sp-a stage=test role=test attempt=1",
              {"passed": True, "summary": "ok", "findings_file": "f"})
        plant(wf, "a4", "[monitor] plan=sp-a stage=critique role=critique attempt=1",
              {"approved": True, "summary": "ok", "findings_file": "f",
               "depth": "in-scope", "critical_defect": False})
        # events.jsonl does not exist; the checkpoint claims all three went out.
        (task / ".cycle-reporter-state.json").write_text(
            json.dumps({"emitted": ["sp-a", "divide", mod.ROSTER_SENTINEL]}),
            encoding="utf-8")

        wrote = mod.Reporter(str(task), [str(wf)]).settle()
        got = [(e["plan"], e["stage"], e["state"]) for e in events(task)]
        check(sorted(got) == [("divide", "plan", "done"),
                              ("orchestrator", "divide", "info"),
                              ("sp-a", "plan", "done")],
              f"both closes AND the roster are re-written: the checkpoint's "
              f"claim loses to an events.jsonl that does not carry them ({got})")
        check(sorted(wrote) == sorted(["divide", "sp-a", mod.ROSTER_SENTINEL]),
              f"...and settle names every one of them ({wrote})")
        check(any(e.get("roster") == ["sp-a — the first"] for e in events(task)),
              f"the re-written roster is the divider's own ({events(task)})")

        # The repair does not become a duplicate machine: now that the stream
        # carries them, a second close-out writes nothing.
        again = mod.Reporter(str(task), [str(wf)]).settle()
        check(again == [] and len(events(task)) == 3,
              f"a settle after the repair emits nothing ({again}, "
              f"{len(events(task))} events)")


def test_a_failed_write_stays_due_and_the_next_poll_retries_it():
    print("test_a_failed_write_stays_due_and_the_next_poll_retries_it")
    # The live daemon used to mark a plan emitted on the failing poll and never
    # revisit it, so ONE bad write left the card "running" for the rest of the
    # run — the MONITORING-7 symptom D-14 exists to remove — and persisted that
    # claim into the checkpoint. `emitted` records writes, not intentions.
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, log = make_run(tmp)
        status = log.parent / "status.sh"
        status.write_text("#!/usr/bin/env bash\nexit 9\n", encoding="utf-8")
        os.chmod(status, 0o755)
        plant(wf, "a1", "[monitor] plan=divide stage=partition role=synth attempt=1",
              {"subplans": [{"id": "sp-a", "files": ["a.py"]}],
               "subplans_file": "x", "summary": "s"})
        rep = mod.Reporter(str(task), [str(wf)])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rep.pass_once()
        check(calls(log) == [] and events(task) == [],
              f"a writer that exits non-zero appends nothing ({calls(log)})")
        state = json.loads((task / ".cycle-reporter-state.json")
                           .read_text(encoding="utf-8"))
        check(state.get("emitted") == [],
              f"...and the checkpoint does not claim otherwise ({state})")

        # Same process, same (unchanged) journal: the next poll retries.
        status.write_text(RECORDING_STATUS_SH, encoding="utf-8")
        os.chmod(status, 0o755)
        with contextlib.redirect_stderr(err):
            rep.pass_once()
        check(calls(log) == ["divide|plan|done|1 sub-plans|3",
                             "orchestrator|divide|info|roster: 1 sub-plans|3"],
              f"the close and the roster land on the poll after the writer "
              f"comes back ({calls(log)})")
        with contextlib.redirect_stderr(err):
            rep.pass_once()
        check(len(calls(log)) == 2,
              f"...exactly once: a landed write is checkpointed ({calls(log)})")


def test_a_retry_asks_the_stream_before_it_doubles_a_line():
    print("test_a_retry_asks_the_stream_before_it_doubles_a_line")
    # The other half of the retry: a writer that APPENDED and then failed —
    # what a status.sh killed by its 30 s timeout under flock contention looks
    # like from here. Indistinguishable from a write that never happened, so
    # the retry path reads events.jsonl before it writes, and self-healing
    # never turns into a doubled close.
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, log = make_run(tmp)
        status = log.parent / "status.sh"
        status.write_text(RECORDING_STATUS_SH.replace("exit 0\n", "exit 9\n"),
                          encoding="utf-8")
        os.chmod(status, 0o755)
        plant(wf, "a1", "[monitor] plan=divide stage=partition role=synth attempt=1",
              {"subplans": [{"id": "sp-a", "files": ["a.py"]}],
               "subplans_file": "x", "summary": "s"})
        rep = mod.Reporter(str(task), [str(wf)])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rep.pass_once()
        got = [(e["plan"], e["stage"]) for e in events(task)]
        check(got == [("divide", "plan"), ("orchestrator", "divide")],
              f"both lines DID reach the stream before the writer failed ({got})")
        state = json.loads((task / ".cycle-reporter-state.json")
                           .read_text(encoding="utf-8"))
        check(state.get("emitted") == [],
              f"...and the reporter, told they failed, checkpointed neither "
              f"({state})")

        status.write_text(RECORDING_STATUS_SH, encoding="utf-8")
        os.chmod(status, 0o755)
        with contextlib.redirect_stderr(err):
            rep.pass_once()
        check(len(events(task)) == 2 and len(calls(log)) == 2,
              f"the retry found its own close and its own roster already on "
              f"the stream and wrote neither again ({calls(log)})")


def test_settle_reports_only_what_it_actually_wrote():
    print("test_settle_reports_only_what_it_actually_wrote")
    # `touch-run close` prints settle's return value to a human. A run whose
    # payload has no status.sh writes NOTHING, and must not claim otherwise.
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf = make_real_run(tmp)
        plant(wf, "a1", "[monitor] plan=sp-a stage=implement role=impl attempt=1",
              {"done": True, "files_changed": ["a.py"], "summary": "s"})
        plant(wf, "a2", "[monitor] plan=sp-a stage=test role=test attempt=1",
              {"passed": True, "summary": "ok", "findings_file": "f"})
        plant(wf, "a3", "[monitor] plan=sp-a stage=critique role=critique attempt=1",
              {"approved": True, "summary": "ok", "findings_file": "f",
               "depth": "in-scope", "critical_defect": False})
        (Path(tmp) / "plugin" / "touch" / "shared" / "monitoring"
         / "status.sh").unlink()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            wrote = mod.Reporter(str(task), [str(wf)]).settle()
        check(wrote == [] and events(task) == [],
              f"a close nothing could write is not reported as written "
              f"({wrote}, {events(task)})")
        # ...and the operator line names which kind of nothing it was. "every
        # implied close is already on the stream" is a claim ABOUT the stream,
        # and this invocation never had a writer to consult one with.
        r = subprocess.run([sys.executable, "-B", str(Path(mod.__file__)),
                            str(wf), "--settle"],
                           env={**os.environ, "ORCH_STATE_DIR": str(task)},
                           capture_output=True, text=True)
        check("no status.sh" in r.stdout and "already on the stream" not in r.stdout,
              f"the operator line names the reason nothing was written "
              f"({r.stdout.strip()!r})")


def test_settle_only_claims_the_stream_when_it_could_write_one():
    print("test_settle_only_claims_the_stream_when_it_could_write_one")
    # `touch-run close` prints this line and a human reads it to decide whether
    # a run closed cleanly. Under --no-status (and --final, which forces the
    # same) settle returns [] because it was FORBIDDEN to write, which is a
    # different fact from "everything was already closed".
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf = make_real_run(tmp)
        plant(wf, "a1", "[monitor] plan=sp-a stage=implement role=impl attempt=1",
              {"done": True, "files_changed": ["a.py"], "summary": "s"})
        plant(wf, "a2", "[monitor] plan=sp-a stage=test role=test attempt=1",
              {"passed": True, "summary": "ok", "findings_file": "f"})
        plant(wf, "a3", "[monitor] plan=sp-a stage=critique role=critique attempt=1",
              {"approved": True, "summary": "ok", "findings_file": "f",
               "depth": "in-scope", "critical_defect": False})
        env = {**os.environ, "ORCH_STATE_DIR": str(task)}

        def run(*argv):
            return subprocess.run([sys.executable, "-B", str(Path(mod.__file__)),
                                   str(wf), *argv], env=env,
                                  capture_output=True, text=True)

        r = run("--settle", "--no-status")
        check("nothing written" in r.stdout
              and "already on the stream" not in r.stdout,
              f"a settle that was forbidden to write says so, and claims "
              f"nothing about the stream ({r.stdout.strip()!r})")
        check(events(task) == [], f"...truthfully ({events(task)})")

        r = run("--settle")
        check("sp-a" in r.stdout and len(events(task)) == 1,
              f"a settle that CAN write names what it wrote ({r.stdout.strip()!r})")
        r = run("--settle")
        check("already on the stream" in r.stdout and len(events(task)) == 1,
              f"...and only then does the stream claim get made "
              f"({r.stdout.strip()!r})")

        # A writer that is PRESENT and refuses is a third kind of nothing, and
        # the one most worth naming: the close is implied, was attempted, and
        # did not land.
        plant(wf, "b1", "[monitor] plan=sp-b stage=implement role=impl attempt=1",
              {"done": True, "files_changed": ["b.py"], "summary": "s"})
        plant(wf, "b2", "[monitor] plan=sp-b stage=test role=test attempt=1",
              {"passed": True, "summary": "ok", "findings_file": "f"})
        plant(wf, "b3", "[monitor] plan=sp-b stage=critique role=critique attempt=1",
              {"approved": True, "summary": "ok", "findings_file": "f",
               "depth": "in-scope", "critical_defect": False})
        status = (Path(tmp) / "plugin" / "touch" / "shared" / "monitoring"
                  / "status.sh")
        status.write_text("#!/usr/bin/env bash\nexit 9\n", encoding="utf-8")
        os.chmod(status, 0o755)
        r = run("--settle")
        check("FAILED to write" in r.stdout and "sp-b" in r.stdout
              and "already on the stream" not in r.stdout,
              f"a refused write is reported as a failure, not as silence "
              f"({r.stdout.strip()!r})")
        check(r.returncode == 0 and len(events(task)) == 1,
              f"...and it still exits 0: monitoring never breaks a close-out "
              f"(rc={r.returncode}, {len(events(task))} events)")


def test_no_status_leaves_the_checkpoint_untouched():
    print("test_no_status_leaves_the_checkpoint_untouched")
    # `--once --no-status` is documented as keeping history untouched. The
    # checkpoint IS history — it records which closes have already fired — so a
    # render-only backfill must not be able to cost a later live daemon its
    # events.
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, log = make_run(tmp)
        plant(wf, "a1", "[monitor] plan=divide stage=partition role=synth attempt=1",
              {"subplans": [{"id": "sp-a", "files": ["a.py"]},
                            {"id": "sp-b", "files": ["b.py"]}],
               "subplans_file": "x", "summary": "s"})
        mod.Reporter(str(task), [str(wf)], emit_status=False).pass_once()
        check(calls(log) == [], "a --no-status pass emits nothing")
        check(not (task / ".cycle-reporter-state.json").exists(),
              "...and writes no checkpoint at all")
        mod.Reporter(str(task), [str(wf)]).pass_once()
        check(calls(log) == ["divide|plan|done|2 sub-plans|4",
                             "orchestrator|divide|info|roster: 2 sub-plans|4"],
              f"so a later live daemon still emits both lines ({calls(log)})")


# -- the final report (D-15) -------------------------------------------------
# A deterministic skeleton over three named sources with ONE authored slot.

FINAL_STREAM = [
    {"ts": "2026-01-01T00:00:00.000+00:00", "plan": "orchestrator",
     "stage": "launch", "state": "running", "detail": "launched",
     "title": "a run", "w": "agent"},
    {"ts": "2026-01-01T00:01:00.000+00:00", "plan": "sp-a", "stage": "tokens",
     "state": "info", "detail": "", "w": "watcher", "quiet": True,
     "tokens": {"in": 10, "out": 1, "cached": 0, "cache_write": 0},
     "agent": {"id": "a" * 17, "shortId": "aaaaaaaa", "state": "running",
               "tokens": {"in": 10, "out": 1, "cached": 5, "cache_write": 2}}},
    # The SAME agent, later and higher: agent.tokens is an absolute running
    # total, so the fold must last-win it, never add it to the line above.
    {"ts": "2026-01-01T00:03:00.000+00:00", "plan": "sp-a", "stage": "tokens",
     "state": "info", "detail": "", "w": "watcher", "quiet": True,
     "tokens": {"in": 90, "out": 9, "cached": 0, "cache_write": 0},
     "agent": {"id": "a" * 17, "shortId": "aaaaaaaa", "state": "done",
               "tokens": {"in": 100, "out": 10, "cached": 50, "cache_write": 20}}},
    {"ts": "2026-01-01T00:04:00.000+00:00", "plan": "sp-a", "stage": "tokens",
     "state": "info", "detail": "", "w": "watcher", "quiet": True,
     "agent": {"id": "b" * 17, "shortId": "bbbbbbbb", "state": "done",
               "tokens": {"in": 7, "out": 3, "cached": 1, "cache_write": 0}}},
    {"ts": "2026-01-01T00:05:00.000+00:00", "plan": "sp-a", "stage": "plan",
     "state": "done", "detail": "green on attempt 1/4", "w": "agent"},
]


def fold_expected(stream):
    """The stats fold, computed independently: agent.tokens, LAST-WINS.

    Deliberately a second implementation rather than a call into the module —
    a page that agrees with the code that rendered it proves nothing. Summing
    the top-level `tokens` deltas here would give 100/10 by coincidence on this
    fixture and the WRONG answer on any stream with a gap, which is exactly the
    trap monitoring.md documents.
    """
    per = {}
    for ev in stream:
        ag = ev.get("agent") or {}
        if ag.get("id") and isinstance(ag.get("tokens"), dict):
            per[(ev["plan"], ag["id"])] = ag["tokens"]
    out = {"in": 0, "out": 0, "cached": 0, "cache_write": 0}
    for tok in per.values():
        for k in out:
            out[k] += int(tok.get(k) or 0)
    return out


def make_final_run(tmp):
    """A finished implement run: journal results + a hand-written stream."""
    mod, task, wf, log = make_run(tmp)
    plant(wf, "a1", "[monitor] plan=divide stage=partition role=synth attempt=1",
          {"subplans": [{"id": "sp-a", "title": "the only one", "files": ["a.py"],
                         "slice_file": "/t/plan/slice-a.md"}],
           "subplans_file": "/t/plan/x-subplans.md", "summary": "one sub-plan"})
    plant(wf, "a2", "[monitor] plan=sp-a stage=implement role=impl attempt=1",
          {"done": True, "files_changed": ["a.py"], "summary": "did it"})
    plant(wf, "a3", "[monitor] plan=sp-a stage=test role=test attempt=1",
          {"passed": True, "summary": "suite green",
           "findings_file": "/t/findings/sp-a-test-attempt-1.md"})
    plant(wf, "a4", "[monitor] plan=sp-a stage=critique role=critique attempt=1",
          {"approved": True, "summary": "no defects",
           "findings_file": "/t/findings/sp-a-critique-attempt-1.md",
           "depth": "in-scope", "critical_defect": False})
    (task / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in FINAL_STREAM), encoding="utf-8")
    return mod, task, wf, log


def render_final(mod, task, wf, narrative=None):
    """Exactly what `--final` does: ingest, render, nothing else."""
    rep = mod.Reporter(str(task), [str(wf)], emit_status=False)
    rep.ingest()
    return Path(rep.render_final(narrative))


def test_final_report_is_deterministic_and_sourced():
    print("test_final_report_is_deterministic_and_sourced")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, _ = make_final_run(tmp)
        out = render_final(mod, task, wf)
        check(out == task / "report" / "final-report.html",
              f"the report lands in report/ BY CONSTRUCTION ({out})")
        first = out.read_bytes()
        again = render_final(mod, task, wf)
        check(again.read_bytes() == first,
              "re-rendering the same inputs is byte-identical (no render stamp)")
        text = first.decode("utf-8")

        want = fold_expected(FINAL_STREAM)
        check(f"{want['in']:,} in" in text and f"{want['out']:,} out" in text,
              f"the page carries the STREAM's own totals ({want})")
        check("117" not in text,
              "…and never the sum of the wire deltas, which double-counts an "
              "agent's absolute total")
        check("touch:narrative:start" in text and "touch:narrative:end" in text,
              "the named narrative slot is present in the rendered page")
        placeholders = ("TASK_SPECIFIC", "/ABS/PATH/TO", "TODO", "FIXME",
                        "lorem ipsum", "<fill")
        left = [p for p in placeholders if p in text]
        check(not left, f"no LLM-era placeholder survives into the report ({left})")

        # Every number's source is named, and the artifacts are linked.
        for probe in ("journal.jsonl", "events.jsonl", "the run snapshot"):
            check(probe in text, f"the sources table names {probe}")
        for probe in ("x-subplans.md", "sp-a-test-attempt-1.md",
                      "sp-a-critique-attempt-1.md", "slice-a.md"):
            check(probe in text, f"the report links {probe}")
        check("PASS" in text and "APPROVED" in text and "1 SUB-PLANS" in text,
              "per-plan detail carries each attempt's verdict word")
        check("No run snapshot on disk" in text,
              "a missing snapshot is stated as normal, never as an error")


def test_final_report_names_a_research_run_for_what_it_is():
    print("test_final_report_names_a_research_run_for_what_it_is")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, _ = make_run(tmp)
        plant_research_board(wf)
        out = render_final(mod, task, wf)
        check(out == task / "report" / "research-report.html",
              f"a research run renders research-report.html ({out.name})")
        text = out.read_text(encoding="utf-8")
        check("PLAN WRITTEN (26 items)" in text and "1 FINDINGS" in text,
              "research verdict words are rendered from the results' own shapes")


def plant_research_coverage(wf, coverage, findings=None):
    """A board whose findings carry ids + a synthesizer that accounts for them.

    `findings` is {perspective: [(id, severity, title)]}; every researcher is
    spawned, and one is deliberately left silent by the caller when the test is
    about an incomplete board.
    """
    for i, key in enumerate(RESEARCH_KEYS):
        aid = f"c{i}"
        marker = f"[monitor] plan=research stage={key} role=research attempt=1"
        spawn(wf, aid, marker)
        rows = (findings or {}).get(key)
        if rows is None:
            continue
        plant(wf, aid, marker,
              {"findings": [{"id": fid, "file": "x.py", "severity": sev,
                             "title": title} for fid, sev, title in rows],
               "findings_file": f"/t/findings/research-{key}-attempt-1.md",
               "summary": "s"})
    marker = "[monitor] plan=synthesis stage=synthesize role=synth attempt=1"
    spawn(wf, "sc", marker)
    result = {"plan_file": "/t/plan/x-plan.md", "item_count": 2, "summary": "s"}
    if coverage is not None:
        result["coverage"] = coverage
    plant(wf, "sc", marker, result)


def test_the_research_report_draws_the_board_and_the_plan():
    print("test_the_research_report_draws_the_board_and_the_plan")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, _ = make_run(tmp)
        plant_research_coverage(
            wf,
            findings={"prior-art": [("PA-1", "blocker", "the real defect"),
                                    ("PA-2", "minor", "a nit")],
                      "data-model": [("DM-1", "major", "shape drift")]},
            # 'economics' is spawned and never returns: an incomplete board.
            coverage=[{"id": "PA-1", "status": "accepted", "note": "item R-01"},
                      {"id": "PA-2", "status": "dropped",
                       "note": "cosmetic, no behavior change"},
                      {"id": "DM-1", "status": "merged", "note": "into R-01"}])
        text = render_final(mod, task, wf).read_text(encoding="utf-8")
        # Diagram 1 — was every angle actually covered?
        check("✓ 2/3 perspectives reported" in text,
              "the board diagram counts the perspectives that reported")
        check("? 1 never returned" in text and "never returned" in text,
              "…and names the one that was spawned and never came back")
        check("absent from the plan" in text,
              "…as a Δ, because a silent angle is a hole in the plan")
        # Diagram 2 — did every finding reach the plan, or get a reason not to?
        for probe in ("✓ 1/3 findings accepted", "⊕ 1 merged", "○ 1 dropped"):
            check(probe in text, f"the board→plan diagram reads {probe!r}")
        check("cosmetic, no behavior change" in text,
              "a dropped finding carries its justification as the Δ")
        check("blocker" in text and "PA-1" in text,
              "each finding row carries its id and severity")
        check("the researchers' own" in text,
              "the sources table says the research board needs no partition")


def test_a_synthesis_that_says_nothing_is_never_full_coverage():
    print("test_a_synthesis_that_says_nothing_is_never_full_coverage")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, _ = make_run(tmp)
        plant_research_coverage(
            wf, coverage=None,
            findings={"prior-art": [("PA-1", "blocker", "t")],
                      "data-model": [("DM-1", "major", "t")],
                      "economics": [("EC-1", "minor", "t")]})
        text = render_final(mod, task, wf).read_text(encoding="utf-8")
        check("✓ 0/3 findings accepted" in text,
              "a synthesizer that returned no coverage accepted nothing")
        check("? 3 unaccounted" in text,
              "…the three findings read as UNACCOUNTED, the one real gap")
        check("never said what became of this finding" in text,
              "…and the Δ column says whose silence it is")
        check("✓ 3/3 perspectives reported" in text,
              "the board itself is still reported as complete — the two "
              "diagrams answer different questions")


def test_the_run_shape_diagram_reads_the_stream_not_a_computed_close():
    print("test_the_run_shape_diagram_reads_the_stream_not_a_computed_close")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, _ = make_final_run(tmp)
        text = render_final(mod, task, wf).read_text(encoding="utf-8")
        check("The run, end to end" in text,
              "the final report opens with the run-shape diagram")
        check("1 SUB-PLANS" in text and "GREEN" in text,
              "…drawn in the same node vocabulary as the cycle pages")
        # `--final` never evaluates a close, so a shape read from self.closed
        # would print every loop open on a finished run. FINAL_STREAM carries
        # sp-a's `done` badge; the diagram has to use it.
        check("0 RED" in text,
              "the loop node counts red loops from the STREAM's badges")
        check("<details><summary>Every agent's verdict" in text,
              "the attempt-by-attempt long form is demoted behind a fold, so "
              "the page leads with diagrams")

    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, _ = make_run(tmp)
        plant_research_board(wf)
        text = render_final(mod, task, wf).read_text(encoding="utf-8")
        check("3/3 REPORTED" in text and "PLAN" in text,
              "a research run gets the research shape, not the loops' one")
        check("SUB-PLANS" not in text,
              "…and never a divide node it never had")


def test_narrative_is_the_only_authored_region():
    print("test_narrative_is_the_only_authored_region")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, _ = make_final_run(tmp)
        frag = Path(tmp) / "narrative.html"
        frag.write_text("<section id=\"narrative\"><h2>Why</h2>"
                        "<p>Because the plan said so.</p></section>",
                        encoding="utf-8")
        out = render_final(mod, task, wf, str(frag))
        text = out.read_text(encoding="utf-8")
        body = text.split("touch:narrative:start -->")[1] \
                   .split("<!-- touch:narrative:end")[0]
        check("Because the plan said so." in body,
              "the fragment is injected into the slot, verbatim")
        check(render_final(mod, task, wf, str(frag)).read_bytes()
              == out.read_bytes(),
              "…and the injected render is byte-identical too")

        # Active content is refused; the page falls back to the empty slot.
        # Not only `<script>`: a handler after a SLASH is the commonest XSS
        # spelling there is, and a guard that a report cites as protection has
        # no business missing it by one character.
        shapes = (("script tag", "<script>alert(1)</script>"),
                  ("slash-separated handler", "<img/onerror=alert(1) src=x>"),
                  ("space-separated handler", '<div onmouseover="x()">hi</div>'),
                  ("iframe srcdoc", '<iframe srcdoc="&lt;b&gt;x"></iframe>'),
                  ("style block", "<style>body{display:none}</style>"),
                  ("form", '<form action="https://example.invalid/x"></form>'),
                  ("javascript: uri", '<a href="javascript:alert(1)">x</a>'))
        for label, fragment in shapes:
            bad = Path(tmp) / "bad.html"
            bad.write_text(f"<section><p>prose</p>{fragment}</section>",
                           encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                text2 = render_final(mod, task, wf,
                                     str(bad)).read_text(encoding="utf-8")
            check(fragment not in text2 and "No narrative was supplied" in text2,
                  f"a narrative carrying a {label} is refused, not rendered")
            check("active content" in err.getvalue(),
                  f"…loudly, for the {label} "
                  f"({err.getvalue().strip()[:80]!r})")

        # An OVER-CAP fragment is refused whole for the same reason, not sliced
        # at the cap and injected: a cut lands inside a `<details>` as easily as
        # between two paragraphs, and one unbalanced tag swallows every section
        # below it on a page whose whole job is to be read off disk.
        big = Path(tmp) / "big.html"
        big.write_text("<section><details><summary>s</summary>"
                       + "z" * (mod.NARRATIVE_CAP + 10)
                       + "</details></section>", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            text3 = render_final(mod, task, wf,
                                 str(big)).read_text(encoding="utf-8")
        check("No narrative was supplied" in text3 and "zzzzzzzzzz" not in text3,
              "an over-cap narrative is refused whole, never truncated into "
              "the page")
        check("cap" in err.getvalue(),
              f"…loudly, like every other refusal here "
              f"({err.getvalue().strip()[:80]!r})")


# -- the report surfaces ------------------------------------------------------
# Three switches, one per page family (`cycle`, `research`, `final`), read live
# out of orch-config.json where `touch-run start` publishes them. Two rules are
# worth a test each, because both are the kind that decay into a lie rather
# than into an error:
#
#   the SPLIT — a surface that is off stops PAGES and nothing else. The loop
#       closes, protocol closes and roster are the monitoring protocol, so a
#       run whose reports are switched off must still settle every card. The
#       failure this guards is R-58 with a config file in front of it: a
#       dashboard stuck on "running" because somebody turned off some HTML.
#   the DESTINATION is reported, never acted on. This renderer cannot publish
#       and always writes the local copy — the enum is an instruction to the
#       driver, so it must reach the driver (stderr) without disturbing the
#       one thing the driver captures (stdout: the path).

def set_reports(task, reports):
    """Merge a `reports` map into the fixture's orch-config.json."""
    path = Path(task) / "orch-config.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["reports"] = reports
    path.write_text(json.dumps(cfg), encoding="utf-8")


def render_final_maybe(mod, task, wf, **kw):
    """`render_final`, allowed to answer None; returns (path|None, stderr)."""
    rep = mod.Reporter(str(task), [str(wf)], emit_status=False)
    rep.ingest()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = rep.render_final(**kw)
    return (Path(out) if out else None), err.getvalue()


def test_the_shipped_report_defaults_are_the_documented_ones():
    print("test_the_shipped_report_defaults_are_the_documented_ones")
    # Pinned as VALUES, not as prose: these six are what a run reports when
    # nobody configured anything, they are quoted in both skills, and
    # `bin/touch-run` carries the same table (cross-checked in
    # tests/test_touch_run.py against what `start` actually publishes).
    mod = load_module(REPORTER_SRC, "cycle_reporter_defaults")
    check(mod.REPORT_DEFAULTS == {
        "cycle": {"enabled": True, "publish": "local"},
        "research": {"enabled": True, "publish": "local|public"},
        "final": {"enabled": True, "publish": "local|public"}},
        f"cycle renders and stays local; research and final render and "
        f"publish ({mod.REPORT_DEFAULTS})")
    # A destination NAMES its targets rather than counting them: the value that
    # used to be the opaque word `both` is spelled `local|public`, so the
    # vocabulary is readable off any one value and a new destination is an
    # entry in REPORT_DESTINATIONS instead of a new word to look up.
    check(mod.REPORT_DESTINATIONS == ("local", "public"),
          f"the destinations are the vocabulary ({mod.REPORT_DESTINATIONS})")
    check(mod.REPORT_PUBLISH == ("local", "public", "local|public"),
          f"…and the accepted values are every selection of them, canonically "
          f"ordered ({mod.REPORT_PUBLISH})")
    # Derived, not typed out — so the pair cannot drift into a value with no
    # directive, which would be a KeyError at the one moment a run has a page
    # to hand over.
    check(sorted(mod.PUBLISH_DIRECTIVE) == sorted(mod.REPORT_PUBLISH),
          f"every value has the words the driver acts on "
          f"({sorted(mod.PUBLISH_DIRECTIVE)})")
    # Parsed, so order and repetition are the writer's business and what gets
    # stored is the one canonical spelling.
    for spelling in ("public|local", "PUBLIC | Local", "local|local|public"):
        check(mod.normalize_publish(spelling) == "local|public",
              f"{spelling!r} canonicalizes ({mod.normalize_publish(spelling)!r})")
    for bad in ("", "both", "local|publik", "local|", None, ["local"]):
        check(mod.normalize_publish(bad) is None,
              f"{bad!r} names no destination ({mod.normalize_publish(bad)!r})")
    # An off surface has no second spelling to drift from: `"off"` normalizes
    # to the same `enabled: False` the object form produces.
    for spelling in ("off", {"enabled": False}):
        got, problems = mod.normalize_reports({"cycle": spelling})
        check(not problems and got["cycle"]["enabled"] is False,
              f"{spelling!r} switches the cycle surface off ({got['cycle']})")
    got, problems = mod.normalize_reports({"cycle": "public"})
    check(not problems and got["cycle"] == {"enabled": True, "publish": "public"},
          f"the bare-string shorthand is a whole surface ({got['cycle']})")
    got, problems = mod.normalize_reports({"cycle": {"publish": "public|local"}})
    check(not problems and got["cycle"]["publish"] == "local|public",
          f"…and a destination is stored canonically, however it was written "
          f"({got['cycle']})")


def test_switching_the_cycle_surface_off_stops_pages_not_events():
    print("test_switching_the_cycle_surface_off_stops_pages_not_events")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, log = make_run(tmp)
        set_reports(task, {"cycle": {"enabled": False}})
        plant(wf, "a1", "[monitor] plan=sp-x stage=implement role=impl attempt=1",
              {"done": True, "files_changed": ["a.py"], "summary": "s",
               "items": [{"id": "R-1", "status": "done", "note": "built"}]})
        plant(wf, "a2", "[monitor] plan=sp-x stage=test role=test attempt=1",
              {"passed": True, "summary": "ok", "findings_file": "f",
               "deviations": []})
        plant(wf, "a3", "[monitor] plan=sp-x stage=critique role=critique attempt=1",
              {"approved": True, "summary": "ok", "findings_file": "f",
               "depth": "in-scope", "critical_defect": False, "deviations": []})
        mod.Reporter(str(task), [str(wf)]).pass_once()
        check(calls(log) == ["sp-x|plan|done|green on attempt 1/4|"],
              f"the loop still closes — the switch owns pages, not the "
              f"protocol ({calls(log)})")
        check(not (task / "report" / "cycles").exists(),
              "and no page, and no empty report/cycles/ to be read as a run "
              "that rendered nothing")
        # Flipped back mid-run: the daemon re-reads its config every pass, so
        # the pages appear without a restart and the close is NOT re-emitted.
        set_reports(task, {"cycle": {"enabled": True}})
        rep = mod.Reporter(str(task), [str(wf)])
        rep.pass_once()
        check((task / "report" / "cycles" / "sp-x-cycle-1.html").is_file(),
              "switching it back on renders the page the run already has")
        check(calls(log) == ["sp-x|plan|done|green on attempt 1/4|"],
              f"…and emits nothing a second time ({calls(log)})")


def test_a_disabled_final_report_renders_nothing_and_force_overrides():
    print("test_a_disabled_final_report_renders_nothing_and_force_overrides")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, _ = make_final_run(tmp)
        set_reports(task, {"final": "off"})
        out, err = render_final_maybe(mod, task, wf)
        page = task / "report" / "final-report.html"
        check(out is None and not page.exists(),
              f"an off surface renders NO page and writes nothing ({out})")
        check("reports.final is off" in err,
              f"…and says which switch did it ({err.strip()[:90]!r})")
        # The empty stdout IS the protocol: a driver's `path=$(… --final)` gets
        # the empty string and publishes nothing, without parsing a reason.
        script = Path(mod.__file__)
        res = subprocess.run(
            [sys.executable, "-B", str(script), str(wf), "--final"],
            env={**os.environ, "ORCH_STATE_DIR": str(task)},
            capture_output=True, text=True)
        check(res.returncode == 0 and res.stdout.strip() == "",
              f"exit 0 with an empty stdout — a configured 'no report' is an "
              f"answer, not a failure (rc={res.returncode}, {res.stdout!r})")
        check(not page.exists(), "still nothing on disk after the CLI run")
        # …and the human override, for the one who switched it off themselves.
        out, _ = render_final_maybe(mod, task, wf, force=True)
        check(out is not None and page.is_file(),
              f"--force renders it anyway ({out})")


def test_each_run_kind_reads_its_own_surface():
    print("test_each_run_kind_reads_its_own_surface")
    # Which switch applies is derived from the plan ids that actually ran — the
    # same fold that picks the filename — so `final` can never gate a research
    # run's page, or the two switches would be one switch with two names.
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, _ = make_run(tmp)
        plant_research_board(wf)
        set_reports(task, {"final": "off"})
        out, _ = render_final_maybe(mod, task, wf)
        check(out is not None and out.name == "research-report.html",
              f"a research run ignores `final` ({out})")
        set_reports(task, {"research": "off", "final": {"enabled": True}})
        out, err = render_final_maybe(mod, task, wf)
        check(out is None and "reports.research is off" in err,
              f"…and obeys `research` ({out}, {err.strip()[:70]!r})")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, _ = make_final_run(tmp)
        set_reports(task, {"research": "off"})
        out, _ = render_final_maybe(mod, task, wf)
        check(out is not None and out.name == "final-report.html",
              f"an implement run ignores `research` ({out})")


def test_the_publish_destination_is_printed_never_acted_on():
    print("test_the_publish_destination_is_printed_never_acted_on")
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, _ = make_final_run(tmp)
        script = Path(mod.__file__)
        page = task / "report" / "final-report.html"
        for value, needle in (("local", "publish nothing"),
                              ("public", "hand back the URL"),
                              ("local|public", "beside the URL")):
            set_reports(task, {"final": value})
            res = subprocess.run(
                [sys.executable, "-B", str(script), str(wf), "--final"],
                env={**os.environ, "ORCH_STATE_DIR": str(task)},
                capture_output=True, text=True)
            check(res.stdout.strip() == str(page),
                  f"stdout stays exactly the path for publish={value} "
                  f"({res.stdout.strip()[-40:]!r})")
            check(f"publish: {value}" in res.stderr and needle in res.stderr,
                  f"…and stderr carries the instruction the driver acts on "
                  f"({res.stderr.strip()[:90]!r})")
            # The local copy is written for EVERY value: the storage rule is
            # satisfied by construction, so `public` is not a way to skip it.
            check(page.is_file(),
                  f"the task-folder copy exists for publish={value}")
            page.unlink()


def test_a_malformed_report_config_falls_back_loudly_and_once():
    print("test_a_malformed_report_config_falls_back_loudly_and_once")
    # `touch-run` refuses a malformed spec before a run exists; by the time
    # THIS reader sees one it is a live daemon tailing a journal, so a bad
    # value falls back to the shipped default — reported, so nobody reads an
    # ignored `"publlish"` as an honoured one, and once, so a 2-second poll
    # loop does not print it for the length of the run.
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, _ = make_run(tmp)
        set_reports(task, {"cycle": {"publlish": "public"}, "finl": "off"})
        rep = mod.Reporter(str(task), [str(wf)], emit_status=False)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            first = rep.reports()
            rep.reports()
            rep.reports()
        check(first == mod.REPORT_DEFAULTS,
              f"every unusable value falls back to the shipped default ({first})")
        lines = [ln for ln in err.getvalue().splitlines() if ln.strip()]
        check(len(lines) == 2 and all("orch-config.json" in ln for ln in lines),
              f"each distinct problem is reported exactly once ({lines})")
        check(any("publlish" in ln for ln in lines)
              and any("finl" in ln for ln in lines),
              f"…naming the key that was not understood ({lines})")


def test_value_flags_take_both_spellings():
    print("test_value_flags_take_both_spellings")
    # `--narrative FILE` used to leave narrative=None AND leave the path in
    # argv as a wf_dir: the page rendered "No narrative was supplied" and the
    # process exited 0. sp-09's prose is about to tell an LLM to run this
    # renderer and pass a narrative, so the space form has to work — or say so.
    with tempfile.TemporaryDirectory(prefix="cycle-reporter-") as tmp:
        mod, task, wf, _ = make_final_run(tmp)
        script = Path(mod.__file__)
        frag = Path(tmp) / "n.html"
        frag.write_text('<section id="narrative"><p>Because the plan said so.'
                        "</p></section>", encoding="utf-8")
        env = {**os.environ, "ORCH_STATE_DIR": str(task)}

        def run(*argv):
            # -B: a by-hand invocation must not drop a .pyc in the payload tree.
            return subprocess.run([sys.executable, "-B", str(script), str(wf),
                                   *argv], env=env, capture_output=True,
                                  text=True)

        r = run("--final", "--narrative", str(frag))
        page = task / "report" / "final-report.html"
        check(r.returncode == 0 and page.is_file(),
              f"the space form renders (rc={r.returncode}, {r.stderr[:120]!r})")
        check(page.is_file() and "Because the plan said so."
              in page.read_text(encoding="utf-8"),
              "…and the narrative it names is actually injected")
        page.unlink()

        r = run("--final", "--narrative=" + str(frag))
        check(r.returncode == 0 and page.is_file()
              and "Because the plan said so." in page.read_text(encoding="utf-8"),
              "the `=` form is unchanged")

        r = run("--final", "--narrative")
        check(r.returncode == 2 and "needs a value" in r.stderr,
              f"a value-less --narrative is refused, loudly, instead of "
              f"rendering the empty slot (rc={r.returncode}, {r.stderr[:120]!r})")

        r = run("--once", "--no-status", "--interval", "0.5")
        check(r.returncode == 0,
              f"--interval takes a space too (rc={r.returncode}, "
              f"{r.stderr[:120]!r})")
        # `inf` and `nan` parse as floats and then wedge the poll loop, so they
        # are refused with the out-of-range numbers rather than accepted.
        for bad in ("nope", "0", "-1", "inf", "nan"):
            r = run("--once", "--interval", bad)
            check(r.returncode == 2 and "--interval" in r.stderr,
                  f"--interval {bad} is refused, not silently defaulted "
                  f"(rc={r.returncode})")


def main():
    try:
        for t in (test_plugin_copy_wins, test_project_copy_is_never_a_fallback,
                  test_render_only_is_quiet,
                  test_shipped_source_has_no_project_candidate,
                  test_shipped_source_has_no_banned_payload_text,
                  test_real_payload_resolves_to_its_own_copy,
                  test_divide_closes_done_with_plans_total,
                  test_divide_closes_failed_like_the_template,
                  test_finalgate_closes,
                  test_sp_loop_close_still_works,
                  test_the_diagram_carries_requirement_implementation_and_delta,
                  test_silence_about_an_item_is_a_gap_never_coverage,
                  test_an_unknown_requirement_list_is_stated_never_assumed,
                  test_a_journal_without_the_coverage_fields_says_so,
                  test_every_surface_reports_the_same_coverage,
                  test_scattered_transcripts_still_resolve,
                  test_marker_miss_is_retried_not_cached,
                  test_research_plans_close_done_with_two,
                  test_research_close_wording_survives_the_late_reports,
                  test_partial_board_is_never_a_fabricated_failure,
                  test_zero_returns_close_failed_only_at_settle,
                  test_settle_emits_only_the_missing_closes_and_is_idempotent,
                  test_settle_never_invents_a_verdict_for_an_open_loop,
                  test_roster_event_is_bounded_at_the_writer,
                  test_roster_file_cap_counts_bytes_and_keeps_whole_entries,
                  test_reporter_roster_reaches_the_stream_bounded,
                  test_seed_env_never_leaks_into_the_stream,
                  test_settle_replaces_a_seeded_roster_with_the_dividers,
                  test_settle_repairs_a_checkpoint_that_claims_an_unwritten_close,
                  test_a_failed_write_stays_due_and_the_next_poll_retries_it,
                  test_a_retry_asks_the_stream_before_it_doubles_a_line,
                  test_settle_reports_only_what_it_actually_wrote,
                  test_settle_only_claims_the_stream_when_it_could_write_one,
                  test_no_status_leaves_the_checkpoint_untouched,
                  test_final_report_is_deterministic_and_sourced,
                  test_final_report_names_a_research_run_for_what_it_is,
                  test_the_research_report_draws_the_board_and_the_plan,
                  test_a_synthesis_that_says_nothing_is_never_full_coverage,
                  test_the_run_shape_diagram_reads_the_stream_not_a_computed_close,
                  test_narrative_is_the_only_authored_region,
                  test_the_shipped_report_defaults_are_the_documented_ones,
                  test_switching_the_cycle_surface_off_stops_pages_not_events,
                  test_a_disabled_final_report_renders_nothing_and_force_overrides,
                  test_each_run_kind_reads_its_own_surface,
                  test_the_publish_destination_is_printed_never_acted_on,
                  test_a_malformed_report_config_falls_back_loudly_and_once,
                  test_value_flags_take_both_spellings):
            t()
    finally:
        # Most fixture modules were loaded out of a TemporaryDirectory that no
        # longer exists; leave nothing dangling in sys.modules for a harness
        # that imports this file instead of running it.
        for name in _loaded:
            sys.modules.pop(name, None)
    print("-" * 60)
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        for f in failures:
            print(f"  FAILED: {f}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
