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
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

# Set before any payload module is imported: the payload tree is what ships,
# and a test run must not leave `.pyc` droppings in it (item 06).
sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _roots  # noqa: E402

REPORTER_SRC = (_roots.PAYLOAD / "skills" / "implement-plan" / "templates"
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
    templates = tmp / "plugin" / "touch" / "skills" / "implement-plan" / "templates"
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
    '# recording stub: plan|stage|state|msg|ORCH_PLANS_TOTAL\n'
    'printf \'%s|%s|%s|%s|%s\\n\' "$1" "$2" "$3" "$4" "${ORCH_PLANS_TOTAL:-}" '
    '>> "$(dirname "$0")/calls.log"\n')


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


def plant(wf, aid, marker, result):
    """One agent transcript carrying `marker` + its journal result record."""
    (wf / f"agent-{aid}.jsonl").write_text(
        json.dumps({"type": "user", "text": marker}) + "\n", encoding="utf-8")
    with open(wf / "journal.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "result", "agentId": aid,
                            "result": result}) + "\n")


def calls(log):
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


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
        check(got == ["divide|plan|done|3 sub-plans|5"],
              f"divide close emitted once, template message + N+2 total (got {got})")
        rep.pass_once()
        check(calls(log) == got, "a second pass emits nothing new")
        rep2 = mod.Reporter(str(task), [str(wf)])   # daemon restart
        rep2.pass_once()
        check(calls(log) == got,
              "a restarted reporter re-ingests but never re-emits (emitted persists)")
        pages = os.listdir(task / "report" / "cycles")
        check(not any(p.startswith("divide") for p in pages),
              "protocol plans render no cycle pages")


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
                  test_scattered_transcripts_still_resolve,
                  test_marker_miss_is_retried_not_cached):
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
