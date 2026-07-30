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

Every fixture is built at the CURRENT tasks root (`ORCH_REL`,
`.touch/local-orchestrators`), so the whole file is the positive control
SECURITY-11 asked for: not "the guard stays inert after the move" but "an ACTIVE
file at the NEW root really denies a subagent's cross-task read". The legacy
`.claude` spelling keeps its own test (`test_dual_root_candidates`) for as long
as it is a candidate — it is what keeps the guard and the HALT brake live across
the physical move (PROTOCOL-2). When the follow-up item drops that candidate,
`LEGACY_ORCH_REL` and that one test go with it and nothing else moves.
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

#: The tasks root, project-relative — the ONE anchor every fixture below builds
#: from, so the next move is one line here (G10).
ORCH_REL = (".touch", "local-orchestrators")
#: The pre-G10 spelling, still a candidate root in the guard for the duration of
#: the transition. Dropped, here and there, by I17's follow-up item.
LEGACY_ORCH_REL = (".claude", "local-orchestrators")
#: The project marker — deliberately NOT the state dir. `.claude/` marks a
#: *Claude Code* project; `.touch/` is created by Touch and is gitignored, so it
#: cannot mark one (G10, PROTOCOL-23). Every fixture gets one, because a real
#: project keeps `.claude/` after the move (settings, statusline) and tier 3
#: climbs to exactly this.
MARKER = ".claude"
#: `SEG_PATTERN`, byte-for-byte as it must stay (Part D-12). The leaf name
#: `local-orchestrators` was kept through the move precisely so this pattern
#: needs no edit and both spellings stay enforced (G10, PROTOCOL-12); a rename
#: would have made it match `plugin/touch/runs/` and every other `runs/` in
#: every project the plugin is enabled in (LAYOUT-1).
SEG_PATTERN_LINE = (
    'SEG_PATTERN = r"local-orchestrators/+([^/\\s\\"\';|&]+)'
    '((?:/[^\\s\\"\';|&]*)?)"'
)
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


def orch_dir(base, rel=ORCH_REL):
    """The tasks root under `base` — never a literal, so the next move is one
    constant and not forty call sites (I1's reason, applied here)."""
    return Path(base).joinpath(*rel)


def make_tree(tmp, rel=ORCH_REL, active="task-a\n"):
    """A throwaway project: the `.claude/` marker, a tasks root under `rel` with
    task-a (active), task-b (not) and task-b's readable `plan/`.

    The marker is created even when the tasks root is `.touch/…` because that is
    what a post-move project looks like — `.claude/` keeps settings and the
    statusline (LAYOUT-6) — and because tier 3 climbs to a `.claude/` and would
    otherwise walk past the fixture into whatever the machine's temp ancestors
    happen to contain.
    """
    (Path(tmp) / MARKER).mkdir(parents=True, exist_ok=True)
    orch = orch_dir(tmp, rel)
    for task, sub in (("task-a", "findings"), ("task-b", "findings"),
                      ("task-b", "plan")):
        d = orch / task / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / "f.md").write_text("x\n", encoding="utf-8")
    if active is not None:
        (orch / "ACTIVE").write_text(active, encoding="utf-8")
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
        # PROTOCOL-13: the reason quotes what the CALLER wrote and labels the
        # root the guard resolved, instead of a `join(root, task) + rest` path
        # that may not exist (the match is not anchored to the root — LAYOUT-16,
        # measured). Both halves are asserted: the matched span verbatim, and
        # the resolved root under its label.
        matched = "local-orchestrators/task-b/findings/f.md"
        check(f"matched '{matched}'" in out,
              "deny reason quotes the matched text verbatim (PROTOCOL-13)")
        check(f"resolved tasks root: {orch}" in out,
              "...and names the tasks root that actually decided, labelled")
        check(f"{orch}/task-b/findings/f.md belongs" not in out,
              "...and no longer presents a synthesised path as the caller's")

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
        deny_cmd = "cat .touch/local-orchestrators/task-b/events.jsonl"
        rc, out = run_guard(hook_call("Bash", tmp, command=deny_cmd))
        check(decision(out) == "deny", "Bash naming another task's state denied")

        # The match is a bare substring on the kept leaf name, so a command
        # written the OLD way is enforced identically — the property that makes
        # the transition window safe for prompts, findings and pasted commands
        # that still spell `.claude/` (PROTOCOL-12, G10).
        rc, out = run_guard(hook_call(
            "Bash", tmp,
            command="cat .claude/local-orchestrators/task-b/events.jsonl"))
        check(decision(out) == "deny",
              "the legacy spelling of another task's path is denied too")

        ok_cmds = (
            "ls .touch/local-orchestrators",
            "cat .touch/local-orchestrators/task-a/findings/f.md",
            "cat .touch/local-orchestrators/task-b/plan/f.md",
            "rm -f .touch/local-orchestrators/ACTIVE",
            "cat .claude/local-orchestrators/task-a/findings/f.md",
        )
        for cmd in ok_cmds:
            rc, out = run_guard(hook_call("Bash", tmp, command=cmd))
            check(decision(out) == "allow", f"Bash allowed: {cmd}")

        # A wildcard task segment is "some task", which is never the active one.
        # Read-only tools may still pass it; Bash may not (there the same glob
        # is just as likely to be the target of an rm).
        rc, out = run_guard(hook_call(
            "Glob", tmp, pattern=".touch/local-orchestrators/*/findings/*.md"))
        check(decision(out) == "allow", "wildcard task segment in Glob allowed")
        rc, out = run_guard(hook_call(
            "Grep", tmp, pattern="verdict",
            path=".touch/local-orchestrators/*/findings"))
        check(decision(out) == "allow", "wildcard task segment in Grep allowed")
        rc, out = run_guard(hook_call(
            "Read", tmp, file_path=".touch/local-orchestrators/task-?/f.md"))
        check(decision(out) == "allow", "single-char wildcard in Read allowed")
        rc, out = run_guard(hook_call(
            "Bash", tmp, command="rm -rf .touch/local-orchestrators/*"))
        check(decision(out) == "deny", "wildcard task segment in Bash still denied")

        rc, out = run_guard(hook_call(
            "Grep", tmp, pattern="verdict",
            path=".touch/local-orchestrators/task-b/findings"))
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
        orch = make_tree(tmp)
        proj = Path(tmp) / "proj"
        (proj / "src").mkdir(parents=True)
        b = orch / "task-b" / "findings" / "f.md"

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
        (proj / MARKER).mkdir()
        rc, out = run_guard(hook_call("Read", proj / "src", file_path=str(b)))
        check(rc == 0 and decision(out) == "allow",
              "tier 3 stops at the first .claude/ marker: ancestor ACTIVE ignored")

        (orch / "HALT").write_text("stop\n", encoding="utf-8")
        rc, out = run_guard(hook_call("Read", proj / "src", file_path=str(b)))
        check(decision(out) == "allow",
              "a HALT above the marker is out of scope for this project too")
        (orch / "HALT").unlink()

        # A HALT above the project root is ignored just the same.
        (orch / "HALT").write_text("stop\n", encoding="utf-8")
        rc, out = run_guard(hook_call("Read", proj / "src", file_path=str(b)),
                            CLAUDE_PROJECT_DIR=proj)
        check(decision(out) == "allow", "HALT above the project root is ignored")

    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        # ...and a sentinel NESTED below the project root is ignored too: the
        # fast-inert check runs before stdin (so before any cwd is known), so
        # the anchored lookup is exactly one location or the guard would
        # behave differently depending on which path it took.
        proj = Path(tmp) / "proj"
        nested = orch_dir(proj / "sub")
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
        proj_orch = orch_dir(proj)
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
        orch_dir(proj).mkdir(parents=True)
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


def test_seg_pattern_is_byte_identical():
    """Part D-12: `SEG_PATTERN` survives the tasks-root move unchanged.

    A source-text pin rather than an import, in the style the frontend tests
    already use, because the claim IS about the bytes: keeping the leaf name
    `local-orchestrators` is what let the move be a two-component edit, and it is
    the whole reason both spellings stay enforced during the transition
    (PROTOCOL-12). It also keeps `test_package.py`'s recursive `DENY_EXACT`
    component — the gate that keeps run history out of the payload — pointed at a
    directory that still exists (LAYOUT-1).
    """
    print("test_seg_pattern_is_byte_identical")
    src = GUARD.read_text(encoding="utf-8")
    check(SEG_PATTERN_LINE in src,
          "SEG_PATTERN is byte-identical to the pre-move pattern (Part D-12)")
    # ...and the candidate pair exists, new spelling first. Behaviour pins the
    # preference below; this pins that the constant is the single place the
    # order is written, so the follow-up item has exactly one line to delete.
    check(f'ORCH_CANDIDATES = (("{ORCH_REL[0]}", "{ORCH_REL[1]}"),' in src,
          "ORCH_CANDIDATES lists the current tasks root first")
    check(f'("{LEGACY_ORCH_REL[0]}", "{LEGACY_ORCH_REL[1]}"))' in src,
          "...and the legacy root second, as one deletable line")


def test_dual_root_candidates():
    """PROTOCOL-2 / SECURITY-11: both tasks-root spellings are consulted, and a
    missing or empty new root cannot swallow an armed legacy one.

    The hook is a fresh subprocess per tool call, so during the physical move
    there is a window where the code and the sentinels disagree about which root
    is live. A one-sided edit makes the guard — and the HALT brake with it — go
    silently inert in that window, which is a security control failing OPEN, not
    a cosmetic ordering bug. The candidate pair makes the order of the code edit
    and the `mv` irrelevant; these arms are what say so.

    The two sentinels resolve differently across the pair and both halves are
    pinned here: `ACTIVE` picks ONE root (arm 4 — a union would let a stale
    sentinel over-PERMIT), `HALT` unions every candidate (arm 5 — a brake may
    only ever over-restrict, and it has to fire at whichever spelling the
    operator reaches for, including the one the un-rewritable historical record
    still names).

    `CLAUDE_PLUGIN_OPTION_RUN_SCOPE_GUARD=false` is not an alternative to any of
    this: it removes HALT too (`test_off_switch`).
    """
    print("test_dual_root_candidates")
    # 1. The positive control at the NEW root: not "still inert", but "denies".
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        orch = make_tree(tmp)
        b = orch / "task-b" / "findings" / "f.md"
        rc, out = run_guard(hook_call("Read", tmp, file_path=str(b)),
                            CLAUDE_PROJECT_DIR=tmp)
        check(rc == 0 and decision(out) == "deny",
              "ACTIVE at the NEW root denies a subagent's cross-task read")
        check(f"resolved tasks root: {orch}" in out,
              "...and the reason names the new root as the one that decided")

    # 2. Fail closed: no `.touch/` root at all, legacy root armed. This is the
    #    pre-move half of the window — the code is new, the sentinels have not
    #    moved yet.
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        legacy = make_tree(tmp, rel=LEGACY_ORCH_REL)
        b = legacy / "task-b" / "findings" / "f.md"
        check(not orch_dir(tmp).exists(), "fixture: the new root does not exist")
        rc, out = run_guard(hook_call("Read", tmp, file_path=str(b)),
                            CLAUDE_PROJECT_DIR=tmp)
        check(rc == 0 and decision(out) == "deny",
              "an absent new root does not disarm an armed LEGACY root")
        check(f"resolved tasks root: {legacy}" in out,
              "...and the reason names the legacy root, so the deny is actionable")

        # ...and the brake, which is the half that actually matters.
        (legacy / "HALT").write_text("stop\n", encoding="utf-8")
        rc, out = run_guard(hook_call("Read", tmp, file_path=str(
            legacy / "task-a" / "findings" / "f.md")), CLAUDE_PROJECT_DIR=tmp)
        check(decision(out) == "deny" and "HALTED" in out,
              "HALT at the legacy root still fires (PROTOCOL-2's whole point)")
        check(str(legacy / "HALT") in out, "...naming the sentinel to delete")
        (legacy / "HALT").unlink()

        # 3. An EXISTING but empty new root must not shadow it either — the
        #    realistic mid-move state (`mkdir -p .touch` before the `mv`).
        orch_dir(tmp).mkdir(parents=True)
        rc, out = run_guard(hook_call("Read", tmp, file_path=str(b)),
                            CLAUDE_PROJECT_DIR=tmp)
        check(rc == 0 and decision(out) == "deny",
              "an EMPTY new root does not shadow the armed legacy root")

    # 4. Both armed: the new root wins, and the legacy ACTIVE does not widen it.
    #    A union would let a stale sentinel over-PERMIT, the one direction a
    #    stale sentinel must never go.
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        orch = make_tree(tmp, active="task-a\n")
        legacy = make_tree(tmp, rel=LEGACY_ORCH_REL, active="task-b\n")
        rc, out = run_guard(hook_call(
            "Read", tmp, file_path=str(orch / "task-a" / "findings" / "f.md")),
            CLAUDE_PROJECT_DIR=tmp)
        check(decision(out) == "allow", "the new root's ACTIVE task is allowed")
        rc, out = run_guard(hook_call(
            "Read", tmp, file_path=str(orch / "task-b" / "findings" / "f.md")),
            CLAUDE_PROJECT_DIR=tmp)
        check(decision(out) == "deny",
              "with both roots armed the new root's ACTIVE decides, no union")
        # The `active` list in full — `"task-b" in out` would pass on the
        # quoted match text alone, so the "only" claim has to be pinned on the
        # prefix the reason builds from `names`. A union would render it
        # `'task-a', 'task-b'` and break this string.
        check("active task(s): 'task-a'." in out,
              "...and the reason lists ONLY the new root's active task")

    # 5. HALT is a UNION over the candidate pair, unlike ACTIVE's choose-one.
    #    The dangerous shape is exactly the one arm 4 covers for ACTIVE: one
    #    root armed, the brake reached for at the OTHER. G11 forbids rewriting
    #    finished folders, so every `RESUME.md` and `orch-config.json` keeps
    #    spelling `.claude/local-orchestrators` forever and a surviving daemon
    #    can re-create that tree (PROTOCOL-3) — an operator following that
    #    muscle memory must not get a file that exists, prints no error and
    #    freezes nothing. Both directions are asserted, because a union has two.
    for armed, braked, label in ((ORCH_REL, LEGACY_ORCH_REL, "legacy"),
                                 (LEGACY_ORCH_REL, ORCH_REL, "new")):
        with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
            active_root = make_tree(tmp, rel=armed)
            halt_root = orch_dir(tmp, braked)
            halt_root.mkdir(parents=True, exist_ok=True)
            halt = halt_root / "HALT"
            halt.write_text("stop\n", encoding="utf-8")
            a = active_root / "task-a" / "findings" / "f.md"
            rc, out = run_guard(hook_call("Read", tmp, file_path=str(a)),
                                CLAUDE_PROJECT_DIR=tmp)
            check(rc == 0 and decision(out) == "deny" and "HALTED" in out,
                  f"HALT at the {label} root fires while ACTIVE is at the "
                  f"other — the brake may only ever over-restrict")
            check(str(halt) in out,
                  f"...and names the {label} root's sentinel, so the operator "
                  f"knows which file to delete")

    # 6. Tier 3, no anchor at all: the climb stops at the `.claude/` marker and
    #    then still checks both candidates under it.
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        legacy = make_tree(tmp, rel=LEGACY_ORCH_REL)
        deep = Path(tmp) / "sub" / "dir"
        deep.mkdir(parents=True)
        rc, out = run_guard(hook_call(
            "Read", deep, file_path=str(legacy / "task-b" / "findings" / "f.md")))
        check(decision(out) == "deny",
              "tier 3 consults the legacy candidate under the marker too")


def test_memory_writes():
    """G14 / SECURITY-12: a subagent may not write Claude Code memory.

    After the move `.touch/` holds the run history AND the memory dir, so a
    subagent denied another task's `findings/` was free to `Write`
    `.touch/memory/MEMORY.md` — the instructions every future session in this
    project loads. That is a strictly larger capability than the guard exists to
    withhold, acquired as a side effect of a directory move, so it is closed
    deliberately and pinned here.

    The limits are pinned in the same test, because a security clause whose
    edges are unasserted is a clause nobody can reason about: three structured
    editors only, path keys only, a required trailing separator (so the
    neighbouring audit log is untouched), and the main terminal agent never
    restricted.
    """
    print("test_memory_writes")
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        make_tree(tmp)
        mem = Path(tmp) / ".touch" / "memory" / "MEMORY.md"
        mem.parent.mkdir(parents=True, exist_ok=True)
        mem.write_text("# memory\n", encoding="utf-8")
        anchored = {"CLAUDE_PROJECT_DIR": tmp}

        rc, out = run_guard(hook_call("Write", tmp, file_path=str(mem),
                                      content="owned\n"), **anchored)
        check(rc == 0 and decision(out) == "deny",
              "subagent Write into .touch/memory/ is denied")
        check("matched '.touch/memory/MEMORY.md'" in out,
              "...and the reason quotes the matched text, not a built path")
        # Both sanctioned routes by name. `"memory" in out` would be satisfied
        # by the quoted path alone, so the write plane is asserted on its own
        # words — the affordance an agent is meant to go use instead.
        check("write plane" in out and "main terminal agent" in out,
              "...and points at the two sanctioned routes instead")

        rc, out = run_guard(hook_call("Edit", tmp, file_path=str(mem)), **anchored)
        check(decision(out) == "deny", "subagent Edit into .touch/memory/ denied")
        # NotebookEdit is off the matcher today, so this can only ever be
        # belt-and-braces — but `notebook_path` is a path key and the tool is in
        # WRITE_TOOLS, so the guard stays honest if the matcher grows again.
        rc, out = run_guard(hook_call(
            "NotebookEdit", tmp,
            notebook_path=str(mem.with_name("notes.ipynb"))), **anchored)
        check(decision(out) == "deny",
              "NotebookEdit is covered too, for when the matcher grows")

        rc, out = run_guard(hook_call("Write", tmp, agent=False,
                                      file_path=str(mem), content="fine\n"),
                            **anchored)
        check(decision(out) == "allow",
              "the main terminal agent writes memory freely (never restricted)")

        rc, out = run_guard(hook_call("Read", tmp, file_path=str(mem)), **anchored)
        check(decision(out) == "allow",
              "reading memory is not the threat: a subagent may still read it")

        # Disclosed limitations, asserted so they stay disclosed rather than
        # assumed away: Bash is not covered (like the plan/ exception), and the
        # write BODY is not scanned — only path keys.
        rc, out = run_guard(hook_call(
            "Bash", tmp, command=f"echo hi >> {mem}"), **anchored)
        check(decision(out) == "allow",
              "known limitation: Bash writes into memory are not caught")
        rc, out = run_guard(hook_call(
            "Write", tmp, file_path=str(Path(tmp) / "notes.md"),
            content="see .touch/memory/MEMORY.md for details\n"), **anchored)
        check(decision(out) == "allow",
              "the content is not scanned — naming the path in prose is not a write")

        # The boundary the required separator buys: the audit log lives NEXT to
        # the memory dir and is not part of this clause.
        rc, out = run_guard(hook_call(
            "Write", tmp, file_path=str(Path(tmp) / ".touch" / "memory-audit.jsonl"),
            content="{}\n"), **anchored)
        check(decision(out) == "allow",
              ".touch/memory-audit.jsonl is not .touch/memory/ (separator required)")

    # The clause is evaluated BEFORE the active-task list, so an ACTIVE that is
    # present but empty — a stale or half-written sentinel — is not a way in.
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        orch = make_tree(tmp, active="\n\n")
        mem = Path(tmp) / ".touch" / "memory" / "MEMORY.md"
        mem.parent.mkdir(parents=True, exist_ok=True)
        rc, out = run_guard(hook_call("Write", tmp, file_path=str(mem),
                                      content="owned\n"), CLAUDE_PROJECT_DIR=tmp)
        check(decision(out) == "deny",
              "an empty ACTIVE list is not a way into memory")
        rc, out = run_guard(hook_call(
            "Read", tmp, file_path=str(orch / "task-b" / "findings" / "f.md")),
            CLAUDE_PROJECT_DIR=tmp)
        check(decision(out) == "allow",
              "control: with no active task the scope clause itself is inert")

    # ...and with no run at all the guard never runs, so memory is the write
    # plane's business, not the hook's. Same for the user's own off-switch.
    #
    # This is asserted on BOTH resolution tiers, and that is not belt-and-braces:
    # the fast-inert early return only fires when an anchor supplies a root, so
    # a tier-3 call (no `$CLAUDE_PROJECT_DIR`, no `$ORCH_TASKS_ROOT`) reads
    # stdin, climbs to the `.claude/` marker and arrives at the memory clause
    # with nothing established. Gating the clause on the resolved root actually
    # bearing a sentinel is what makes the two tiers agree — without it, any
    # subagent `Write` naming `.touch/memory/` was denied in every project that
    # has a `.claude/` marker and has never run Touch at all, which the module
    # docstring twice promises cannot happen.
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        make_tree(tmp, active=None)
        mem = Path(tmp) / ".touch" / "memory" / "MEMORY.md"
        mem.parent.mkdir(parents=True, exist_ok=True)
        call = hook_call("Write", tmp, file_path=str(mem), content="x\n")
        rc, out = run_guard(call, CLAUDE_PROJECT_DIR=tmp)
        check(rc == 0 and decision(out) == "allow",
              "no sentinel (tier 2, anchored): the guard is inert, memory "
              "clause included")
        rc, out = run_guard(call)
        check(rc == 0 and decision(out) == "allow",
              "no sentinel (tier 3, the climb): same answer — the memory "
              "clause is not path-dependent")
        # ...and the control on that same tier: arm the run and the clause
        # bites, so the arm above is an inertness assertion and not a dead one.
        (orch_dir(tmp) / "ACTIVE").write_text("task-a\n", encoding="utf-8")
        rc, out = run_guard(call)
        check(decision(out) == "deny",
              "control: tier 3 with an ACTIVE sentinel does deny the memory "
              "write")
        rc, out = run_guard(call, CLAUDE_PROJECT_DIR=tmp,
                            CLAUDE_PLUGIN_OPTION_RUN_SCOPE_GUARD="false")
        check(decision(out) == "allow",
              "the off-switch lifts the memory clause too — it is the user's switch")


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
        orch_dir(proj).mkdir(parents=True)

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

        (orch_dir(proj) / "ACTIVE").write_text("task-a\n", encoding="utf-8")
        check(run_with_open_stdin(proj, 2) is None,
              "control: with an ACTIVE sentinel the guard does read stdin")


def test_inert_modes():
    """"No sentinel → inert" on every tier, not just the anchored ones.

    The anchored arm exercises the fast-inert early return; the unanchored one
    exercises the path that reads stdin, climbs to the marker and then has to
    reach the same conclusion the long way. They are different code paths, and
    the module docstring's claim is about behaviour, so both are pinned.
    """
    print("test_inert_modes")
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        orch = make_tree(tmp)
        b = orch / "task-b" / "findings" / "f.md"
        (orch / "ACTIVE").unlink()
        rc, out = run_guard(hook_call("Read", tmp, file_path=str(b)),
                            CLAUDE_PROJECT_DIR=tmp)
        check(rc == 0 and decision(out) == "allow",
              "no ACTIVE sentinel: guard is inert even for subagents")
        for tool, kwargs in (("Read", {"file_path": str(b)}),
                             ("Write", {"file_path": str(b),
                                        "content": "x\n"}),
                             ("Bash", {"command": f"cat {b}"})):
            rc, out = run_guard(hook_call(tool, tmp, **kwargs))
            check(rc == 0 and decision(out) == "allow",
                  f"...and with no anchor either (tier 3), {tool} is inert too")

    rc, out = run_guard("this is not json")
    check(rc == 0 and out == "", "malformed stdin: exit 0, no output, no block")


def main():
    for t in (test_settings_registration, test_registration, test_hooks_json,
              test_scoping,
              test_sentinels_reachable, test_halt, test_bash_and_glob,
              test_multi_run, test_project_root_ceiling,
              test_tasks_root_override, test_seg_pattern_is_byte_identical,
              test_dual_root_candidates, test_memory_writes, test_off_switch,
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
