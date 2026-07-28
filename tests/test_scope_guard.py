#!/usr/bin/env python3
"""Stdlib-only tests for the run-scope guard
(plugin/touch/hooks/orch_scope_guard.py) and its two registrations — the
plugin's `hooks/hooks.json` (exec form) and this repo's `.claude/settings.json`
(shell form), which must carry an identical matcher.
Run as `python3 test_scope_guard.py`; exits non-zero on failure. No pytest.

The guard is exercised as it runs in production: a subprocess fed the
PreToolUse JSON on stdin, against a throwaway task tree — never against the
repo's own local-orchestrators state, so a test run can never scope-restrict
(or be restricted by) a live session. For the same reason every run starts from
an environment with `CLAUDE_PROJECT_DIR` and the `run_scope_guard` off-switch
stripped; the tests that care about them set them explicitly.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / "plugin" / "touch" / "hooks" / "orch_scope_guard.py"
HOOKS_JSON = REPO / "plugin" / "touch" / "hooks" / "hooks.json"
SETTINGS = REPO / ".claude" / "settings.json"
MATCHER_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write", "Bash")

failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        failures.append(msg)
        print(f"  FAIL: {msg}")


def guard_env(**overrides):
    """A clean environment: the guard's three env inputs are never inherited
    (a live `ORCH_TASKS_ROOT` or `CLAUDE_PROJECT_DIR` from the session running
    the suite would otherwise point every fixture at the repo's own state)."""
    env = dict(os.environ)
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.pop("ORCH_TASKS_ROOT", None)
    env.pop("CLAUDE_PLUGIN_OPTION_RUN_SCOPE_GUARD", None)
    for k, v in overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    return env


def run_guard(payload, **env_overrides):
    """Run the guard exactly as the harness does; returns (rc, stdout)."""
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True, text=True, timeout=30, env=guard_env(**env_overrides),
    )
    return proc.returncode, proc.stdout.strip()


def decision(stdout):
    """'deny' / 'allow' from guard stdout (empty stdout = allow)."""
    if not stdout:
        return "allow"
    return json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]


def hook_call(tool, cwd, agent=True, **tool_input):
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd)}
    if agent:
        payload["agent_id"] = "a-test"
        payload["agent_type"] = "general-purpose"
    return payload


def make_tree(tmp):
    orch = Path(tmp) / ".claude" / "local-orchestrators"
    for task, sub in (("task-a", "findings"), ("task-b", "findings"),
                      ("task-b", "plan")):
        d = orch / task / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / "f.md").write_text("x\n", encoding="utf-8")
    (orch / "ACTIVE").write_text("task-a\n", encoding="utf-8")
    return orch


def names_guard(hook):
    """True for either registration form: the shell-string `command` this repo
    uses, or the exec form (`command: python3`, `args: [...]`) the plugin's
    hooks.json uses. Recognising both matters because the matcher-parity
    assertion is the only thing keeping the two registrations from diverging —
    if it stopped matching, it would stop asserting, silently."""
    if "orch_scope_guard.py" in hook.get("command", ""):
        return True
    return any("orch_scope_guard.py" in str(a) for a in hook.get("args", []))


def settings_entry():
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    entries = settings.get("hooks", {}).get("PreToolUse", [])
    return [e for e in entries if any(names_guard(h) for h in e.get("hooks", []))]


def test_registration():
    print("test_registration")
    ours = settings_entry()
    check(len(ours) == 1, "settings.json registers orch_scope_guard once")
    settings_matcher = ours[0].get("matcher", "") if ours else ""
    for tool in MATCHER_TOOLS:
        check(tool in settings_matcher.split("|"), f"settings matcher covers {tool}")
    check("NotebookEdit" not in settings_matcher,
          "settings matcher no longer carries NotebookEdit")
    # Coverage alone would pass a seventh tool silently (and parity would then
    # propagate it to hooks.json unremarked). The matcher is a closed set:
    # widening what this hook intercepts is a decision, not a diff.
    check(sorted(settings_matcher.split("|")) == sorted(MATCHER_TOOLS),
          "settings matcher is exactly the six matched tools, no more")
    if ours:
        cmd = ours[0]["hooks"][0]["command"]
        check("plugin/touch/hooks/orch_scope_guard.py" in cmd,
              "settings.json points at the relocated guard in the plugin subtree")
        check(".claude/hooks/" not in cmd,
              "settings.json no longer points at the old .claude/hooks/ copy")
    check(GUARD.is_file() and os.access(GUARD, os.X_OK),
          "guard script lives in the plugin subtree and is executable")
    check(not (REPO / ".claude" / "hooks" / "orch_scope_guard.py").exists(),
          "no second copy left behind under .claude/hooks/")


def test_hooks_json():
    print("test_hooks_json")
    check(HOOKS_JSON.is_file(), "plugin/touch/hooks/hooks.json exists")
    if not HOOKS_JSON.is_file():
        return
    doc = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    entries = doc.get("hooks", {}).get("PreToolUse", [])
    check(len(entries) == 1, "hooks.json declares exactly one PreToolUse entry")
    if not entries:
        return
    plugin_matcher = entries[0].get("matcher", "")
    hooks = entries[0].get("hooks", [])
    check(len(hooks) == 1, "hooks.json declares exactly one hook command")
    h = hooks[0] if hooks else {}
    check(h.get("type") == "command", "hooks.json hook is type 'command'")
    # GD-T4: args[] IS substituted; a shell-form command string is not.
    check(h.get("command") == "python3",
          "hooks.json uses exec form: command is the bare interpreter")
    check(h.get("args") == ["${CLAUDE_PLUGIN_ROOT}/hooks/orch_scope_guard.py"],
          "hooks.json passes the guard via args[] with ${CLAUDE_PLUGIN_ROOT}")
    # Parity is asserted unconditionally: a settings.json that stopped
    # registering the guard must fail HERE, not silently skip the one check
    # that guarantees the two registrations cannot diverge.
    ours = settings_entry()
    check(len(ours) == 1, "settings.json registers the guard exactly once")
    check(bool(ours) and ours[0].get("matcher", "") == plugin_matcher,
          "matcher parity: settings.json and hooks.json match identically")


def test_scoping():
    print("test_scoping")
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        orch = make_tree(tmp)
        a = orch / "task-a" / "findings" / "f.md"
        b = orch / "task-b" / "findings" / "f.md"
        b_plan = orch / "task-b" / "plan" / "f.md"

        # Anchored at the fixture: a stray /tmp/.claude/… on a shared machine
        # can then never reach these cases. The unbounded climb has its own
        # case below and in test_project_root_ceiling.
        anchored = {"CLAUDE_PROJECT_DIR": tmp}

        rc, out = run_guard(hook_call("Read", tmp, agent=False, file_path=str(b)),
                            **anchored)
        check(rc == 0 and decision(out) == "allow",
              "main agent (no agent_id) reads other task freely")

        rc, out = run_guard(hook_call("Read", tmp, file_path=str(a)), **anchored)
        check(decision(out) == "allow", "subagent reads the ACTIVE task freely")

        rc, out = run_guard(hook_call("Read", tmp, file_path=str(b)), **anchored)
        check(rc == 0 and decision(out) == "deny",
              "subagent Read of another task's findings is denied")
        check("task-a" in out and "task-b" in out,
              "deny reason names both the active and the offending task")

        rc, out = run_guard(hook_call("Read", tmp, file_path=str(b_plan)), **anchored)
        check(decision(out) == "allow",
              "another task's plan/ stays readable (authority ladder)")

        # The plan/ exception does not extend to the structured editors. It
        # DOES extend to Bash (READ_TOOLS includes it) — disclosed in the
        # guard's "does NOT promise" section, and asserted below so the
        # limitation is pinned rather than assumed away.
        rc, out = run_guard(hook_call("Edit", tmp, file_path=str(b_plan)), **anchored)
        check(decision(out) == "deny", "plan/ exception does not extend to Edit")

        rc, out = run_guard(hook_call("Bash", tmp, command=f"rm -f {b_plan}"),
                            **anchored)
        check(decision(out) == "allow",
              "known limitation: Bash inherits the plan/ exception, writes included")

        rc, out = run_guard(hook_call("Write", tmp, file_path=str(b)), **anchored)
        check(decision(out) == "deny", "subagent Write into another task denied")

        # Known limitations, pinned rather than assumed away: the match is
        # textual and first-segment only, and nothing is normalized first, so
        # traversal through a permitted segment is not detected. Both are
        # pre-existing, both are disclosed in the guard's "does NOT promise"
        # section. The paired deny above each is the control that shows the
        # rule really does bite on the direct spelling.
        rc, out = run_guard(hook_call(
            "Read", tmp, file_path=f"{orch}/task-a/../task-b/findings/f.md"),
            **anchored)
        check(decision(out) == "allow",
              "known limitation: traversal via the ACTIVE segment is not detected")
        rc, out = run_guard(hook_call(
            "Read", tmp, file_path=f"{orch}/task-b/plan/../findings/f.md"),
            **anchored)
        check(decision(out) == "allow",
              "known limitation: traversal out of the plan/ exception is not detected")

        # A cwd deeper than the project root changes nothing: with the anchor
        # set the sentinel location is the project's, whatever the cwd.
        deep = Path(tmp) / "sub" / "dir"
        deep.mkdir(parents=True)
        rc, out = run_guard(hook_call("Read", deep, file_path=str(b)), **anchored)
        check(decision(out) == "deny", "deep cwd still resolves the anchored sentinel")

        # ...and with no anchor at all, the same deep cwd finds it by walking
        # up to the fixture's own `.claude/` marker (which is nearer than any
        # stray ancestor's, so no shared-machine leftover can decide this).
        rc, out = run_guard(hook_call("Read", deep, file_path=str(b)))
        check(decision(out) == "deny",
              "tier 3: the climb finds the nearest .claude/ marker's sentinel")


def test_sentinels_reachable():
    print("test_sentinels_reachable")
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        orch = make_tree(tmp)
        rc, out = run_guard(hook_call("Read", tmp, file_path=str(orch / "ACTIVE")))
        check(decision(out) == "allow", "the ACTIVE sentinel itself is readable")
        rc, out = run_guard(hook_call("Read", tmp, file_path=str(orch / "HALT")))
        check(decision(out) == "allow",
              "the HALT sentinel is readable — the brake must be reachable")
        rc, out = run_guard(hook_call(
            "Bash", tmp, command=f"rm -f {orch}/HALT"))
        check(decision(out) == "allow", "an agent may name the HALT sentinel")
        rc, out = run_guard(hook_call("Write", tmp, file_path=str(orch / "HALT"),
                                      content="stop\n"))
        check(decision(out) == "allow", "an agent may write the HALT sentinel")


def test_halt():
    print("test_halt")
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        orch = make_tree(tmp)
        halt = orch / "HALT"
        halt.write_text("stop\n", encoding="utf-8")
        a = orch / "task-a" / "findings" / "f.md"

        rc, out = run_guard(hook_call("Read", tmp, file_path=str(a)))
        check(rc == 0 and decision(out) == "deny",
              "HALT denies even the active task's own files")
        check("HALTED" in out and str(halt) in out,
              "halt reason says the run is halted and names the sentinel")

        rc, out = run_guard(hook_call("Bash", tmp, command="ls /tmp"))
        check(decision(out) == "deny",
              "HALT denies a tool call that names no task at all")

        rc, out = run_guard(hook_call("Read", tmp, agent=False, file_path=str(a)))
        check(decision(out) == "allow", "HALT never restricts the main agent")

        # ...and with CLAUDE_PROJECT_DIR set, HALT alone (no ACTIVE) still fires
        (orch / "ACTIVE").unlink()
        rc, out = run_guard(hook_call("Read", tmp, file_path=str(a)),
                            CLAUDE_PROJECT_DIR=tmp)
        check(decision(out) == "deny",
              "HALT without ACTIVE is not swallowed by the fast-inert path")


def test_bash_and_glob():
    print("test_bash_and_glob")
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        make_tree(tmp)
        deny_cmd = "cat .claude/local-orchestrators/task-b/events.jsonl"
        rc, out = run_guard(hook_call("Bash", tmp, command=deny_cmd))
        check(decision(out) == "deny", "Bash naming another task's state denied")

        ok_cmds = (
            "ls .claude/local-orchestrators",
            "cat .claude/local-orchestrators/task-a/findings/f.md",
            "cat .claude/local-orchestrators/task-b/plan/f.md",
            "rm -f .claude/local-orchestrators/ACTIVE",
        )
        for cmd in ok_cmds:
            rc, out = run_guard(hook_call("Bash", tmp, command=cmd))
            check(decision(out) == "allow", f"Bash allowed: {cmd}")

        # A wildcard task segment is "some task", which is never the active one.
        # Read-only tools may still pass it; Bash may not (there the same glob
        # is just as likely to be the target of an rm).
        rc, out = run_guard(hook_call(
            "Glob", tmp, pattern=".claude/local-orchestrators/*/findings/*.md"))
        check(decision(out) == "allow", "wildcard task segment in Glob allowed")
        rc, out = run_guard(hook_call(
            "Grep", tmp, pattern="verdict",
            path=".claude/local-orchestrators/*/findings"))
        check(decision(out) == "allow", "wildcard task segment in Grep allowed")
        rc, out = run_guard(hook_call(
            "Read", tmp, file_path=".claude/local-orchestrators/task-?/f.md"))
        check(decision(out) == "allow", "single-char wildcard in Read allowed")
        rc, out = run_guard(hook_call(
            "Bash", tmp, command="rm -rf .claude/local-orchestrators/*"))
        check(decision(out) == "deny", "wildcard task segment in Bash still denied")

        rc, out = run_guard(hook_call(
            "Grep", tmp, pattern="verdict",
            path=".claude/local-orchestrators/task-b/findings"))
        check(decision(out) == "deny", "Grep path into another task denied")


def test_multi_run():
    print("test_multi_run")
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        orch = make_tree(tmp)
        c = orch / "task-c" / "findings"
        c.mkdir(parents=True)
        (c / "f.md").write_text("x\n", encoding="utf-8")
        # two concurrent runs: one task name per line (blank lines tolerated)
        (orch / "ACTIVE").write_text("task-a\n\ntask-c\n", encoding="utf-8")

        for task in ("task-a", "task-c"):
            rc, out = run_guard(hook_call(
                "Read", tmp, file_path=str(orch / task / "findings" / "f.md")))
            check(decision(out) == "allow", f"listed task {task} accessible")

        rc, out = run_guard(hook_call(
            "Read", tmp, file_path=str(orch / "task-b" / "findings" / "f.md")))
        check(decision(out) == "deny", "unlisted task still denied")
        check("task-a" in out and "task-c" in out,
              "deny reason lists every active task")


def test_project_root_ceiling():
    print("test_project_root_ceiling")
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        # A stray ACTIVE in an ANCESTOR of the project must never restrict it.
        make_tree(tmp)
        proj = Path(tmp) / "proj"
        (proj / "src").mkdir(parents=True)
        b = Path(tmp) / ".claude" / "local-orchestrators" / "task-b" / "findings" / "f.md"

        rc, out = run_guard(hook_call("Read", proj / "src", file_path=str(b)),
                            CLAUDE_PROJECT_DIR=proj)
        check(rc == 0 and decision(out) == "allow",
              "ACTIVE above the project root is ignored (ceiling honored)")

        # Tier 3 with no anchor at all: the climb finds the ancestor's tasks
        # root only because `proj` has no `.claude/` marker of its own yet...
        rc, out = run_guard(hook_call("Read", proj / "src", file_path=str(b)))
        check(decision(out) == "deny",
              "no anchor, no marker below: the climb reaches the ancestor's root")

        # ...and the moment it does, the marker is the ceiling. This is the
        # realistic shape of the failure GD-T5 tier 3 exists to prevent: a
        # forgotten ~/.claude/local-orchestrators/ACTIVE restricting every
        # project under $HOME. The project here has a `.claude/` but no tasks
        # root at all, so the correct answer is "no run here".
        (proj / ".claude").mkdir()
        rc, out = run_guard(hook_call("Read", proj / "src", file_path=str(b)))
        check(rc == 0 and decision(out) == "allow",
              "tier 3 stops at the first .claude/ marker: ancestor ACTIVE ignored")

        (Path(tmp) / ".claude" / "local-orchestrators" / "HALT").write_text(
            "stop\n", encoding="utf-8")
        rc, out = run_guard(hook_call("Read", proj / "src", file_path=str(b)))
        check(decision(out) == "allow",
              "a HALT above the marker is out of scope for this project too")
        (Path(tmp) / ".claude" / "local-orchestrators" / "HALT").unlink()

        # A HALT above the project root is ignored just the same.
        (Path(tmp) / ".claude" / "local-orchestrators" / "HALT").write_text(
            "stop\n", encoding="utf-8")
        rc, out = run_guard(hook_call("Read", proj / "src", file_path=str(b)),
                            CLAUDE_PROJECT_DIR=proj)
        check(decision(out) == "allow", "HALT above the project root is ignored")

    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        # ...and a sentinel NESTED below the project root is ignored too: the
        # fast-inert check runs before stdin (so before any cwd is known), so
        # the anchored lookup is exactly one location or the guard would
        # behave differently depending on which path it took.
        proj = Path(tmp) / "proj"
        nested = proj / "sub" / ".claude" / "local-orchestrators"
        (nested / "task-b" / "findings").mkdir(parents=True)
        (nested / "ACTIVE").write_text("task-a\n", encoding="utf-8")
        rc, out = run_guard(
            hook_call("Read", proj / "sub",
                      file_path=str(nested / "task-b" / "findings" / "f.md")),
            CLAUDE_PROJECT_DIR=proj)
        check(rc == 0 and decision(out) == "allow",
              "with the anchor set, only the project root's sentinel counts")

    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        # The project's OWN sentinel is found even from a foreign cwd.
        orch = make_tree(tmp)
        b = orch / "task-b" / "findings" / "f.md"
        with tempfile.TemporaryDirectory(prefix="foreign-cwd-") as other:
            rc, out = run_guard(hook_call("Read", other, file_path=str(b)),
                                CLAUDE_PROJECT_DIR=tmp)
            check(decision(out) == "deny",
                  "project-root ACTIVE applies even when cwd is outside it")


def test_tasks_root_override():
    """GD-T5 tier 1: `$ORCH_TASKS_ROOT` outranks the project anchor.

    `status.sh` and both monitoring daemons resolve run state through that
    variable, so a driver that sets it moves ACTIVE and HALT out of the
    project — a guard that only knew `$CLAUDE_PROJECT_DIR` would stop
    enforcing anything for the whole run, and the HALT brake with it, silently.
    Each case is built so the two tiers give DIFFERENT answers; the same call
    without the variable is run as the control.

    The tier is conditioned on existence, and that has its own cases below: a
    stale or mistyped value falls through to the project root rather than
    disarming the guard, while a value that exists but names the WRONG
    directory — a task directory, one word away from `$ORCH_STATE_DIR` — still
    wins the tier and leaves the guard, and the HALT brake, inert. That residue
    is real, so it is asserted here in the direction the code actually behaves
    rather than the direction the existence check was hoped to cover; the
    module docstring discloses it in the same words. (The variable must also be
    EXPORTED into the `claude` process to reach the hook at all — a per-command
    assignment on a daemon's command line is invisible here. That is an
    environment fact, not something a subprocess test can assert; it too is
    disclosed in the module docstring.)
    """
    print("test_tasks_root_override")
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        proj = Path(tmp) / "proj"
        proj_orch = proj / ".claude" / "local-orchestrators"
        proj_orch.mkdir(parents=True)
        # The project's own ACTIVE lists task-b — the opposite of the override's.
        (proj_orch / "ACTIVE").write_text("task-b\n", encoding="utf-8")

        outside = make_tree(Path(tmp) / "outside")  # ACTIVE lists task-a
        a = outside / "task-a" / "findings" / "f.md"
        b = outside / "task-b" / "findings" / "f.md"

        rc, out = run_guard(hook_call("Read", proj, file_path=str(b)),
                            CLAUDE_PROJECT_DIR=proj, ORCH_TASKS_ROOT=outside)
        check(rc == 0 and decision(out) == "deny",
              "ORCH_TASKS_ROOT's ACTIVE decides: unlisted task denied")
        check("'task-a'" in out and "task-b" in out,
              "the deny reason comes from the override root's ACTIVE, not the project's")
        check(str(outside) in out,
              "the deny reason names the tasks root that decided, not a fixed path")

        rc, out = run_guard(hook_call("Read", proj, file_path=str(b)),
                            CLAUDE_PROJECT_DIR=proj)
        check(decision(out) == "allow",
              "control: without ORCH_TASKS_ROOT the project's ACTIVE allows task-b")

        rc, out = run_guard(hook_call("Read", proj, file_path=str(a)),
                            CLAUDE_PROJECT_DIR=proj, ORCH_TASKS_ROOT=outside)
        check(decision(out) == "allow",
              "the override root's own active task stays accessible")

        # A stale export or a typo must not beat a populated project root —
        # otherwise the guard (and the HALT brake) go silently off. Same
        # fixture, same call as the deny case above; only the override's
        # existence differs. A value that EXISTS but is the wrong directory is
        # a different outcome, asserted right after.
        proj_b = proj_orch / "task-b" / "findings"
        proj_b.mkdir(parents=True)
        (proj_b / "f.md").write_text("x\n", encoding="utf-8")
        proj_c = proj_orch / "task-c" / "findings"
        proj_c.mkdir(parents=True)
        (proj_c / "f.md").write_text("x\n", encoding="utf-8")

        missing = Path(tmp) / "nope" / "nothing" / "here"
        rc, out = run_guard(hook_call("Read", proj, file_path=str(proj_c / "f.md")),
                            CLAUDE_PROJECT_DIR=proj, ORCH_TASKS_ROOT=missing)
        check(rc == 0 and decision(out) == "deny",
              "a non-existent ORCH_TASKS_ROOT falls through: the project's ACTIVE decides")
        check("'task-b'" in out and "task-c" in out,
              "...and the deny reason comes from the project root, not the ghost override")

        rc, out = run_guard(hook_call("Read", proj, file_path=str(proj_b / "f.md")),
                            CLAUDE_PROJECT_DIR=proj, ORCH_TASKS_ROOT=missing)
        check(decision(out) == "allow",
              "control: the project's own active task is still allowed in that state")

        # The residue the existence check does NOT cover, pinned as it behaves:
        # a task directory exists, so it wins the tier, holds no sentinel, and
        # the guard fast-inerts. Its control is the case two above — the same
        # call, denied, when the override falls through instead.
        wrong = outside / "task-a"          # the shape $ORCH_STATE_DIR carries
        rc, out = run_guard(hook_call("Read", proj, file_path=str(proj_c / "f.md")),
                            CLAUDE_PROJECT_DIR=proj, ORCH_TASKS_ROOT=wrong)
        check(rc == 0 and decision(out) == "allow",
              "known residue: an ORCH_TASKS_ROOT that exists but is a TASK dir "
              "disarms the guard")

        # ...and it takes the emergency brake with it, which is why the module
        # docstring says "export the tasks root itself" instead of claiming the
        # existence check makes a wrong value harmless.
        (proj_orch / "HALT").write_text("stop\n", encoding="utf-8")
        rc, out = run_guard(hook_call("Read", proj, file_path=str(proj_b / "f.md")),
                            CLAUDE_PROJECT_DIR=proj, ORCH_TASKS_ROOT=wrong)
        check(decision(out) == "allow",
              "known residue: that same wrong override hides the project's HALT")
        rc, out = run_guard(hook_call("Read", proj, file_path=str(proj_b / "f.md")),
                            CLAUDE_PROJECT_DIR=proj)
        check(decision(out) == "deny" and "HALTED" in out,
              "control: with no override at all that HALT does halt the run")
        (proj_orch / "HALT").unlink()

    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        # The emergency brake must be visible through the same tier: an empty
        # project tasks dir would otherwise fast-inert past a HALT that the
        # daemons and status.sh are writing next to.
        proj = Path(tmp) / "proj"
        (proj / ".claude" / "local-orchestrators").mkdir(parents=True)
        outside = make_tree(Path(tmp) / "outside")
        halt = outside / "HALT"
        halt.write_text("stop\n", encoding="utf-8")
        a = outside / "task-a" / "findings" / "f.md"

        rc, out = run_guard(hook_call("Read", proj, file_path=str(a)),
                            CLAUDE_PROJECT_DIR=proj, ORCH_TASKS_ROOT=outside)
        check(decision(out) == "deny" and "HALTED" in out,
              "HALT in the override root halts the run")
        check(str(halt) in out, "the halt reason names the override root's sentinel")

        rc, out = run_guard(hook_call("Read", proj, file_path=str(a)),
                            CLAUDE_PROJECT_DIR=proj)
        check(decision(out) == "allow",
              "control: without ORCH_TASKS_ROOT that HALT is invisible (fast-inert)")

        # The override is an anchor in its own right: it works with no
        # CLAUDE_PROJECT_DIR at all, and it is still exactly one location
        # (no climb from it, no climb from cwd).
        rc, out = run_guard(hook_call("Read", tmp, file_path=str(a)),
                            ORCH_TASKS_ROOT=outside)
        check(decision(out) == "deny" and "HALTED" in out,
              "ORCH_TASKS_ROOT alone anchors the guard, with no project dir set")


def test_off_switch():
    print("test_off_switch")
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        orch = make_tree(tmp)
        b = orch / "task-b" / "findings" / "f.md"
        call = hook_call("Read", tmp, file_path=str(b))

        rc, out = run_guard(call, CLAUDE_PLUGIN_OPTION_RUN_SCOPE_GUARD="false")
        check(rc == 0 and decision(out) == "allow",
              "run_scope_guard=false disables the guard entirely")
        rc, out = run_guard(call, CLAUDE_PLUGIN_OPTION_RUN_SCOPE_GUARD="true")
        check(decision(out) == "deny", "run_scope_guard=true keeps it enforcing")
        rc, out = run_guard(call, CLAUDE_PLUGIN_OPTION_RUN_SCOPE_GUARD="")
        check(decision(out) == "deny", "an empty off-switch value is not 'off'")

        (orch / "HALT").write_text("stop\n", encoding="utf-8")
        rc, out = run_guard(call, CLAUDE_PLUGIN_OPTION_RUN_SCOPE_GUARD="false")
        check(decision(out) == "allow",
              "the off-switch also lifts HALT — it is the user's own switch")


def test_fast_inert_never_reads_stdin():
    print("test_fast_inert_never_reads_stdin")
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        proj = Path(tmp) / "proj"
        (proj / ".claude" / "local-orchestrators").mkdir(parents=True)

        def run_with_open_stdin(project_dir, timeout):
            """Hold stdin open and never write: a guard that reads it hangs.

            `with Popen(...)` so every pipe is closed and the child reaped even
            on the timeout branch — the interesting case is the one where the
            process is still alive.

            The two timeouts are deliberately asymmetric. Interpreter start is
            ~25 ms, so the exit-expected case gets a 10 s budget it will never
            approach: a loaded machine must make the test slow, never red. The
            blocked case can only ever spend its full budget, so it stays at
            2 s to keep the suite quick.
            """
            with subprocess.Popen(
                    [sys.executable, str(GUARD)], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env=guard_env(CLAUDE_PROJECT_DIR=project_dir)) as proc:
                try:
                    proc.wait(timeout=timeout)
                    return proc.returncode
                except subprocess.TimeoutExpired:
                    return None  # still blocked on stdin: it is reading it
                finally:
                    if proc.poll() is None:
                        proc.kill()

        check(run_with_open_stdin(proj, 10) == 0,
              "no sentinel in CLAUDE_PROJECT_DIR: exits 0 without reading stdin")

        (proj / ".claude" / "local-orchestrators" / "ACTIVE").write_text(
            "task-a\n", encoding="utf-8")
        check(run_with_open_stdin(proj, 2) is None,
              "control: with an ACTIVE sentinel the guard does read stdin")


def test_inert_modes():
    print("test_inert_modes")
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        orch = make_tree(tmp)
        b = orch / "task-b" / "findings" / "f.md"
        (orch / "ACTIVE").unlink()
        rc, out = run_guard(hook_call("Read", tmp, file_path=str(b)),
                            CLAUDE_PROJECT_DIR=tmp)
        check(rc == 0 and decision(out) == "allow",
              "no ACTIVE sentinel: guard is inert even for subagents")

    rc, out = run_guard("this is not json")
    check(rc == 0 and out == "", "malformed stdin: exit 0, no output, no block")


def main():
    for t in (test_registration, test_hooks_json, test_scoping,
              test_sentinels_reachable, test_halt, test_bash_and_glob,
              test_multi_run, test_project_root_ceiling,
              test_tasks_root_override, test_off_switch,
              test_fast_inert_never_reads_stdin, test_inert_modes):
        t()
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
