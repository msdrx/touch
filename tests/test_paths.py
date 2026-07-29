#!/usr/bin/env python3
"""Relocation tests for `aggregator/paths.py` — item 03 (CM-2, PLUGIN-SPEC-6).

Run as `python3 test_paths.py`; exits non-zero on failure. No pytest, no runner.

The defect this file exists to catch is invisible in a checkout: every derived
root used to be `dirname(dirname(abspath(__file__)))`, which in this repo *is*
the project root, so four wrong roots (`.touch/`, the legacy task folders, the
server's task folders, the Mongo `database_name` digest) and one right one
(`touch-visual/` assets) evaluated to the same directory. Under a plugin
install they diverge: the package sits in a version-stamped cache directory
that is swept ~14 days after the next update.

So this test **relocates the package** — copies `aggregator/` into a fake
`<cache>/<marketplace>/touch/<version>/`, runs a probe from a foreign cwd with
a fake project — and asserts each root's provenance **independently**:

    state  -> project      legacy -> project      tasks -> project
    db digest -> project-derived AND update-invariant
    assets -> package

A single "it works in the repo" assertion cannot distinguish those, which is
exactly how the defect survived to HEAD. Two cache versions are staged so
update-invariance is a real observation and not a re-statement of the code.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The canonical trees are named through `tests/_roots.py`, never by a
# literal under REPO: GD-U1 moves them and this is the single flip point.
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
from _roots import SRC                # noqa: E402  (path juggling first)
sys.path.insert(0, str(SRC))

from aggregator import paths                                   # noqa: E402
from aggregator.mirror import database_name                    # noqa: E402

failures = []

#: What the probe prints, from inside the relocated copy. Every root the
#: package derives, plus where the package itself thinks it is.
PROBE = r"""
import json, os
from aggregator import legacy, mirror, paths, server, store
print(json.dumps({
    "package":   os.path.dirname(os.path.abspath(paths.__file__)),
    "plugin":    paths.plugin_root(),
    "project":   paths.project_root(),
    "state":     store.state_root(),
    "legacy":    legacy.orchestrator_root(),
    "tasks":     server.default_tasks_root(),
    "assets":    server.default_assets(),
    "db":        mirror.database_name(),
}))
"""


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def stage(tmp, version):
    """Copy the package into `<tmp>/cache/msdrx-tools/touch/<version>/`."""
    root = Path(tmp) / "cache" / "msdrx-tools" / "touch" / version
    shutil.copytree(SRC / "aggregator", root / "aggregator",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # The assets tree ships with the code, so the relocated copy has one too;
    # its presence is what makes the assets assertion meaningful.
    (root / "touch-visual").mkdir()
    (root / "touch-visual" / "index.html").write_text("<!-- staged -->\n",
                                                      encoding="utf-8")
    return root


def probe(package_root, cwd, **env):
    """Run PROBE from `cwd` with only the env this test sets (never the suite's).

    The session running the suite exports `CLAUDE_PROJECT_DIR` and may export
    `TOUCH_*`; inheriting either would make every arm below pass for the wrong
    reason.
    """
    clean = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(package_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": env.pop("HOME", str(Path(cwd).parent / "fake-home")),
    }
    clean.update({k: v for k, v in env.items() if v is not None})
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=str(cwd), env=clean,
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(f"probe failed ({out.returncode}): {out.stderr.strip()}")
    return json.loads(out.stdout)


def make_world(tmp):
    """A fake project, a foreign cwd and a fake home — none of them the repo."""
    project = Path(tmp) / "project"
    (project / ".claude" / "local-orchestrators").mkdir(parents=True)
    foreign = Path(tmp) / "elsewhere" / "deep"
    foreign.mkdir(parents=True)
    (Path(tmp) / "fake-home").mkdir()
    return project, foreign


# --- the resolution order, arm by arm -------------------------------------
def test_project_root_resolution_order():
    print("test_project_root_resolution_order")
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "project"
        (project / ".claude").mkdir(parents=True)
        other = Path(tmp) / "other"
        other.mkdir()
        env = {"CLAUDE_PROJECT_DIR": str(project), "TOUCH_PROJECT_CWD": str(other)}

        check(paths.project_root(str(other), env=env) == str(other),
              "an explicit argument wins over every environment variable")
        check(paths.project_root(env=env) == str(project),
              "$CLAUDE_PROJECT_DIR wins over $TOUCH_PROJECT_CWD (the hook's anchor)")
        check(paths.project_root(env={"TOUCH_PROJECT_CWD": str(other)}) == str(other),
              "$TOUCH_PROJECT_CWD is honoured when the harness anchor is absent")
        check(paths.project_root(env={"CLAUDE_PROJECT_DIR": "",
                                      "TOUCH_PROJECT_CWD": ""},
                                 cwd=str(other)) == str(other),
              "an exported-but-empty variable is not a project root (it would be /)")


def test_project_root_walk_up_and_fallback():
    print("test_project_root_walk_up_and_fallback")
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "project"
        (project / ".claude").mkdir(parents=True)
        deep = project / "a" / "b"
        deep.mkdir(parents=True)
        check(paths.project_root(env={}, cwd=str(deep)) == str(project),
              "the cwd walk-up stops at the nearest `.claude/` marker")

        bare = Path(tmp) / "bare" / "sub"
        bare.mkdir(parents=True)
        check(paths.project_root(env={}, cwd=str(bare)) == str(bare),
              "with no marker anywhere above, the cwd itself is the root")


def test_home_is_not_a_project():
    print("test_home_is_not_a_project")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        (home / ".claude").mkdir(parents=True)          # the CLI's own config dir
        here = home / "scratch"
        here.mkdir()
        saved = os.environ.get("HOME")
        try:
            os.environ["HOME"] = str(home)
            check(paths.project_root(env={}, cwd=str(here)) == str(here),
                  "~/.claude is the CLI's config, not a marker: no adopting $HOME "
                  "as the project (and writing ~/.touch)")
        finally:
            if saved is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved


def test_only_paths_mentions_dunder_file():
    print("test_only_paths_mentions_dunder_file")
    offenders = []
    for module in sorted((SRC / "aggregator").glob("*.py")):
        if module.name == "paths.py":
            continue
        for lineno, line in enumerate(module.read_text(encoding="utf-8").splitlines(), 1):
            if "__file__" in line:
                offenders.append(f"{module.name}:{lineno}")
    check(not offenders,
          "`paths.py` is the package's only `__file__` reference "
          f"(offenders: {offenders or 'none'})")


# --- the relocation: provenance, one root at a time -----------------------
def test_relocated_roots_are_project_anchored():
    print("test_relocated_roots_are_project_anchored")
    with tempfile.TemporaryDirectory() as tmp:
        cache = stage(tmp, "0.1.0")
        project, foreign = make_world(tmp)
        got = probe(cache, foreign, CLAUDE_PROJECT_DIR=str(project))

        check(got["package"] == str(cache / "aggregator"),
              "the probe really ran the relocated copy, not this checkout")
        check(got["project"] == str(project),
              "project_root() is the fake project, not the cwd and not the cache")
        check(got["state"] == str(project / ".touch"),
              "state -> project: the WAL never lands in the version-stamped cache")
        check(got["legacy"] == str(project / ".claude" / "local-orchestrators"),
              "legacy tasks root -> project")
        check(got["tasks"] == str(project / ".claude" / "local-orchestrators"),
              "server tasks root -> project")
        check(got["assets"] == str(cache / "touch-visual"),
              "assets -> package: the one root that genuinely ships with the code")
        check(got["plugin"] == str(cache),
              "plugin_root() is the package's parent, wherever the package is")

        inside_cache = [k for k in ("state", "legacy", "tasks")
                        if got[k].startswith(str(cache))]
        check(not inside_cache,
              f"nothing mutable is written under the plugin cache: {inside_cache or 'none'}")
        check(not got["state"].startswith(str(foreign)),
              "…and nothing mutable follows the foreign cwd either")


def test_relocated_roots_follow_touch_project_cwd_and_the_walk_up():
    print("test_relocated_roots_follow_touch_project_cwd_and_the_walk_up")
    with tempfile.TemporaryDirectory() as tmp:
        cache = stage(tmp, "0.1.0")
        project, foreign = make_world(tmp)

        got = probe(cache, foreign, TOUCH_PROJECT_CWD=str(project))
        check(got["state"] == str(project / ".touch"),
              "$TOUCH_PROJECT_CWD alone re-anchors every derived root")

        inner = project / "nested" / "dir"
        inner.mkdir(parents=True)
        walked = probe(cache, inner)
        check(walked["state"] == str(project / ".touch")
              and walked["legacy"] == str(project / ".claude" / "local-orchestrators"),
              "with no variables at all, the cwd walk-up finds the same project")
        check(walked["assets"] == str(cache / "touch-visual"),
              "…and assets still resolve against the package, not the walk-up")


def test_database_name_is_project_derived_and_update_invariant():
    print("test_database_name_is_project_derived_and_update_invariant")
    with tempfile.TemporaryDirectory() as tmp:
        old = stage(tmp, "0.1.0")
        new = stage(tmp, "0.2.0")                       # what `/plugin update` makes
        project, foreign = make_world(tmp)
        second = Path(tmp) / "second-project"
        (second / ".claude").mkdir(parents=True)

        before = probe(old, foreign, CLAUDE_PROJECT_DIR=str(project))["db"]
        after = probe(new, foreign, CLAUDE_PROJECT_DIR=str(project))["db"]
        elsewhere = probe(new, foreign, CLAUDE_PROJECT_DIR=str(second))["db"]

        check(before == after,
              f"the database name survives a plugin update ({before} == {after}): "
              "digesting the version dir would orphan the mirror under GD-27's "
              "drop fence on every update")
        check(before == database_name(str(project), env={}),
              "…because it digests the project root (same name as the direct call)")
        check(before != elsewhere,
              "…and it is still per-checkout: a second project is a second database")
        check(before != database_name(str(old), env={}),
              "the cache directory is not what is digested")


def main():
    for test in (
        test_project_root_resolution_order,
        test_project_root_walk_up_and_fallback,
        test_home_is_not_a_project,
        test_only_paths_mentions_dunder_file,
        test_relocated_roots_are_project_anchored,
        test_relocated_roots_follow_touch_project_cwd_and_the_walk_up,
        test_database_name_is_project_derived_and_update_invariant,
    ):
        test()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all aggregator/paths.py relocation tests passed")


if __name__ == "__main__":
    main()
