#!/usr/bin/env python3
"""Stdlib-only tests for the run-scope guard
(plugin/touch/hooks/orch_scope_guard.py) and its ONE registration — the
plugin's `hooks/hooks.json`, exec form (GD-U5).
Run as `python3 test_scope_guard.py`; exits non-zero on failure. No pytest.

There used to be a second registration, in this repo's `.claude/settings.json`,
shell form, same matcher, and the docstring called the pair deliberate. It was
not an either/or: the dogfood loop is `claude --plugin-dir plugin/touch` with
`touch@inline` enabled, so both fired — measured 2 hook processes per tool call
against 1 with the plugin unloaded (PLUGIN-RUNTIME-4). The settings block is
gone; what lives there now is the `enabledPlugins` opt-in that makes the
plugin's registration the one that runs — for exactly ONE id, `touch@inline`,
and with no `extraKnownMarketplaces` block beside it (GD-C1).
`test_settings_registration` pins every one of those halves, so neither the
double registration nor a second Touch identity can silently return.

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

# Nothing here imports a payload module today, but the guard is one `import`
# away at all times and a `__pycache__/` under `plugin/touch/hooks/` is exactly
# the untracked stray `test_package.py`'s working-tree arm now fails on
# (DUP-MAP-7). Stop it at the source rather than sweeping it up: this flag for
# anything this file imports, `PYTHONDONTWRITEBYTECODE` in `guard_env()` for
# every subprocess it starts.
sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / "plugin" / "touch" / "hooks" / "orch_scope_guard.py"
HOOKS_JSON = REPO / "plugin" / "touch" / "hooks" / "hooks.json"
SETTINGS = REPO / ".claude" / "settings.json"
MATCHER_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write", "Bash")
#: The whole of `.claude/settings.json` — a closed set, so a third top-level
#: key has to be argued for rather than merely committed.
#: `extraKnownMarketplaces` was briefly a member and is now refused by name
#: (GD-C1, `test_settings_registration`): marketplace registration is keyed by
#: catalog `name` and stored per-user GLOBALLY, so a same-name add silently
#: REPLACES an existing registration — trusting this folder would repoint a
#: contributor's real `msdrx-tools` at a working tree, in every project on
#: that machine. It also bought nothing: the `plugin install`/`marketplace`
#: CLI subcommands never read it, and the dev loop is
#: `claude --plugin-dir plugin/touch`, which `touch@inline` already serves.
SETTINGS_KEYS = {"statusLine", "enabledPlugins"}

#: The closed set of plugin ids the committed settings file may enable. One
#: payload, one identity: two ids for one payload is two registrations by
#: intent, and the inline-shadows-installed rule that made the pair benign
#: (measured: 1 hook fire, the `--plugin-dir` copy shadows the same-named
#: installed copy) is UNWRITTEN upstream, so nothing here may depend on it.
ENABLED_PLUGINS = {"touch@inline"}

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
    env["PYTHONDONTWRITEBYTECODE"] = "1"   # never litter the payload tree
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
    """True for either registration form: a shell-string `command`, or the exec
    form (`command: python3`, `args: [...]`) the plugin's hooks.json uses. Both
    shapes are still recognised, because the settings-side assertion is now
    "there is NO registration here" — a detector that only knew one form would
    let the other one back in silently."""
    if "orch_scope_guard.py" in hook.get("command", ""):
        return True
    return any("orch_scope_guard.py" in str(a) for a in hook.get("args", []))


def settings_entries():
    """Every PreToolUse entry in `.claude/settings.json` that names the guard.

    Post-GD-U5 this must be EMPTY. It is computed rather than assumed absent so
    a re-added block is reported as "registered again", not as a KeyError.
    """
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    entries = settings.get("hooks", {}).get("PreToolUse", [])
    return [e for e in entries if any(names_guard(h) for h in e.get("hooks", []))]


def test_settings_registration():
    """GD-U5 + GD-C1: one registration, one identity, and settings.json says so.

    Four halves, and all of them matter. NO `hooks` key — the second
    registration is gone and cannot creep back. The `enabledPlugins` opt-in,
    committed: without it the dev loop's plugin is loaded but disabled, which
    would leave the repo with no guard at all rather than with one. That
    opt-in names exactly ONE id (`touch@inline`). And no
    `extraKnownMarketplaces` block: a per-user, global, same-name replacement
    is not something a project file may do to whoever trusts it.

    The accepted consequence, recorded where the arm lives: a session started
    WITHOUT the plugin now has no guard. That is deliberate, not an oversight —
    the guard is inert without an ACTIVE sentinel anyway (see
    `test_inert_modes`), and every orchestration run already requires the
    plugin, whose `bin/` is the driver toolchain.
    """
    print("test_settings_registration")
    check(SETTINGS.is_file(), ".claude/settings.json exists")
    try:
        settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        check(False, f".claude/settings.json parses as JSON ({exc})")
        return
    check(True, ".claude/settings.json parses as JSON")
    check("hooks" not in settings,
          "settings.json carries NO hooks key — the plugin registers the guard "
          "exactly once (GD-U5); a second registration fired twice per tool call")
    check(settings_entries() == [],
          "...and no PreToolUse entry anywhere in it names orch_scope_guard.py")
    # `.get(...).get(...)` would raise on a hand-edited `enabledPlugins` that is
    # a list or a string; this file reports FAIL and keeps going instead of
    # dying with a traceback before the arms below it run.
    enabled = settings.get("enabledPlugins")
    check(isinstance(enabled, dict) and enabled.get("touch@inline") is True,
          'settings.json commits "enabledPlugins": {"touch@inline": true} so '
          "the plugin's registration is actually live in the dev loop")
    # ...and that id is the ONLY one. Two ids for one payload is two
    # registrations by intent; the inline-shadows-installed rule that made the
    # pair benign (measured: 1 hook fire, `--plugin-dir` shadows the same-named
    # installed copy) is unwritten upstream, so this file must not rely on it.
    # A second id also re-arms the opt-in problem below from the other side:
    # `touch@msdrx-tools` needs a marketplace registration to resolve at all.
    check(isinstance(enabled, dict) and set(enabled) == ENABLED_PLUGINS,
          f"enabledPlugins enables exactly {sorted(ENABLED_PLUGINS)} — one "
          f"payload, one identity (found: "
          f"{sorted(enabled) if isinstance(enabled, dict) else enabled!r})")
    # NEGATIVE arm (GD-C1): there is no `extraKnownMarketplaces` block, and
    # committing one back is a defect, not a convenience. Marketplace
    # registration is keyed by catalog `name` and stored per-user GLOBALLY: a
    # same-name add silently REPLACES the previous registration, so a
    # contributor who installed the published Touch has their real
    # `msdrx-tools` repointed at this working tree the moment they trust this
    # folder — in every project on that machine. It is also inert where it
    # would matter: the `plugin install` / `plugin marketplace` CLI
    # subcommands do not read it (reproduced twice), and the dev loop is
    # `claude --plugin-dir plugin/touch`, which `touch@inline` already serves.
    # Exercise the marketplace install path in a throwaway `CLAUDE_CONFIG_DIR`
    # (`claude plugin marketplace add <checkout>`), never via committed
    # settings.
    check("extraKnownMarketplaces" not in settings,
          "settings.json registers NO marketplace — a same-name add replaces a "
          "contributor's real msdrx-tools registration, per-user and globally")
    # The file's other resident is not collateral: the status line is the one
    # thing that was in here before the hooks block and must survive its removal.
    status_line = settings.get("statusLine")
    check(isinstance(status_line, dict)
          and status_line.get("command", "").endswith("statusline.sh"),
          "the statusline entry is untouched by the hooks-block removal")
    # A closed set, in the same spirit as `test_package.py`'s TOP_ALLOWLIST: the
    # two assertions above are a deny list, and this arm exists precisely
    # because a second registration mechanism crept into this one committed,
    # session-wide file unnoticed once. Fail on ADDITION, not just on `hooks`.
    check(set(settings) == SETTINGS_KEYS,
          f"the committed settings file carries exactly {sorted(SETTINGS_KEYS)} "
          f"(found: {sorted(settings)}) — a new top-level key is how a second "
          f"registration mechanism arrives")


def test_registration():
    print("test_registration")
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
    # The matcher used to be pinned via parity with settings.json. With one
    # registration left there is nothing to compare it against, so the closed
    # set moves here: widening what this hook intercepts is a decision, not a
    # diff, and it is now asserted where the decision is actually recorded.
    for tool in MATCHER_TOOLS:
        check(tool in plugin_matcher.split("|"), f"hooks.json matcher covers {tool}")
    check("NotebookEdit" not in plugin_matcher,
          "hooks.json matcher does not carry NotebookEdit")
    check(sorted(plugin_matcher.split("|")) == sorted(MATCHER_TOOLS),
          "hooks.json matcher is exactly the six matched tools, no more")


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
    for t in (test_settings_registration, test_registration, test_hooks_json,
              test_scoping,
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
