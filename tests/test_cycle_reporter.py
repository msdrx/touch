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


def main():
    try:
        for t in (test_plugin_copy_wins, test_project_copy_is_never_a_fallback,
                  test_render_only_is_quiet,
                  test_shipped_source_has_no_project_candidate,
                  test_shipped_source_has_no_banned_payload_text,
                  test_real_payload_resolves_to_its_own_copy):
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
