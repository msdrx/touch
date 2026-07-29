#!/usr/bin/env python3
"""PreToolUse run-scope guard for orchestration loops.

While a run is active — `.claude/local-orchestrators/ACTIVE` lists the active
task names, one per line — subagent tool calls may touch only those tasks'
folders under `local-orchestrators/`. Every OTHER task keeps its `plan/`
readable (the authority ladder lives in old task folders) and has everything
else denied. Two deliberate non-restrictions: the main terminal agent (no
`agent_id` in the hook payload — the field is present only for subagent calls)
always sees everything, and with no ACTIVE file the guard is inert, so ordinary
sessions are unaffected.

Registered exactly ONCE, by the plugin (GD-U5): `hooks/hooks.json` in **exec
form** (`command: "python3"`, `args: ["${CLAUDE_PLUGIN_ROOT}/hooks/…"]` —
`args[]` is substituted, a shell-form `command` string is not). This repo's
`.claude/settings.json` used to register the same script a second time in shell
form with an identical matcher; both were live at once in the dogfood loop and
the hook fired twice per tool call (measured 2 vs 1), so that block is gone and
`"enabledPlugins": {"touch@inline": true}` is committed there instead — every
`claude --plugin-dir plugin/touch` session auto-enables the one registration.
Accepted consequence: a session started WITHOUT the plugin has no guard. That
is fine — the guard is inert without an ACTIVE file anyway, and every
orchestration run already needs the plugin, whose `bin/` is the driver
toolchain. Stdlib only.

Where the sentinels are looked for — GD-T5's task-state order, which item 04's
`resolve_tasks_root()` is to adopt for `status.sh` and both monitoring daemons
(as of 2026-07-28 they do not read `$ORCH_TASKS_ROOT` at all yet, so today this
is the guard's own resolution order and nobody else's):

1. `$ORCH_TASKS_ROOT`, honoured only when it names a directory that **exists**,
   so a stale export or a typo falls through to the project anchor instead of
   disarming the guard. A value that exists but is the **wrong** directory
   still wins, and then the guard and the HALT brake go quietly inert. The
   likely wrong value is `$ORCH_STATE_DIR`'s *task* directory — one word apart,
   set by every `status.sh` call in this repo, and holding neither sentinel.
   Export the tasks root itself: the directory that holds `ACTIVE`, `HALT` and
   the task folders. Tier 2 is not existence-conditioned at all, so a wrong
   `$CLAUDE_PROJECT_DIR` disarms the guard the same way; that one is set by the
   harness rather than by hand, which is the only reason it is left unchecked.
2. `$CLAUDE_PROJECT_DIR/.claude/local-orchestrators`;
3. a walk up from the payload `cwd` that stops at the FIRST ancestor holding a
   `.claude/` directory. The marker is the ceiling: a stray
   `~/.claude/local-orchestrators/ACTIVE` left behind by an old run can never
   restrict an unrelated project that has a `.claude/` of its own.

Tier 1 is not decoration — once item 04 lands, a driver that points the daemons
at an out-of-project tasks root moves ACTIVE and HALT with them, and a guard
that only knew the project anchor would silently stop enforcing anything (HALT
included) for that whole run. It only helps when the variable is **exported
into the `claude` process**, though: a hook inherits `claude`'s environment, so
the per-command form this repo's protocol uses for its sibling variables
(`ORCH_STATE_DIR=… python3 decision_watcher.py`) would be invisible here.
Export it before launching `claude`, or leave the run state under the project.

This fires on every matched tool call of every session in every project the
plugin is enabled in, so the cost is bounded three ways:

- `CLAUDE_PLUGIN_OPTION_RUN_SCOPE_GUARD=false` (the plugin's `run_scope_guard`
  userConfig, default true) exits before anything else happens — including
  before HALT is consulted, so turning the "scope guard" off also removes the
  HALT emergency brake, which is a feature of this same hook;
- fast-inert: with an anchor (either of the first two tiers) and neither
  sentinel present under it, the process exits without even reading stdin;
- the anchored sentinel lookup is exactly ONE `isfile` per sentinel at a known
  directory, **ceilinged at the project root** — a stray ACTIVE file in some
  ancestor can never restrict an unrelated project, and a nested one below the
  anchor is ignored too, because the fast-inert check runs before stdin is read
  and therefore before any `cwd` is known: one rule, one location, no
  path-dependent behaviour. The tier-3 climb survives only for the case where
  neither anchor is set, and it stops at the first `.claude/` marker, so it is
  ceilinged as well — never a walk to `/`.

Measured 2026-07-28 on this machine, four times (after the rewrite, after the
`$ORCH_TASKS_ROOT` tier, after the tier-3 marker ceiling, after this
disclosure-and-deny-reason pass), six 20-run loops each, one subprocess per
call: **~22-24 ms/call on the fast-inert path against a ~22-24 ms
bare-interpreter floor** (`python3 -c pass`) — the guard's own share lands
between 0 and 3 ms across all four measurements, i.e. inside the run-to-run
noise of process start — and ~33-38 ms when a run really is active and the
payload is parsed. The off-switch and `$ORCH_TASKS_ROOT`-inert paths
measure the same ~22-23 ms. The pre-rewrite number was 33 ms against an 18 ms
floor (DISTRIBUTION-8, E7).

Caveat on the fast-inert path, recorded because it is assumed rather than
tested: exiting before reading stdin means the harness's write to this
process's stdin fails with EPIPE once the payload exceeds the pipe buffer.
Measured here against this file: a 32 KB payload is absorbed by the buffer, a
64 KiB+ one (e.g. a large `Write` body) raises `BrokenPipeError` in a raw
`write()`. `subprocess.communicate()` swallows it, which is why the test suite
never sees it. Early-exit PreToolUse hooks are ordinary, so the harness very
likely tolerates this, but "very likely" is not a measurement — the fast path
has only ever run under the tests, never under the real harness. Verify once
on an install (a project with no `.claude/local-orchestrators/`, a >64 KiB
Write) before treating it as proven.

What the guard does NOT promise:

- **Bash coverage is textual.** The hook sees only the command string, so it
  denies commands that NAME another task's folder. That is the right strength
  for the actual threat — loop agents drifting into other tasks' state and
  getting distracted — not an adversarial sandbox.
- **The match is textual for every tool, and first-segment only**, so traversal
  through a permitted segment is not detected. Nothing is normalized first, so
  `task-a/../task-b/findings/f.md` matches on the ACTIVE `task-a` and passes,
  and `task-b/plan/../findings/f.md` satisfies the `plan/` exception (both
  relative to the tasks root). Pre-existing, unchanged here, pinned by tests,
  and of a piece with the bullet above: this bounds drift, it does not contain
  a determined caller.
- **A wildcard task segment is passed for the read-only tools**
  (Read/Glob/Grep), so `Grep path=.claude/local-orchestrators/*/findings` reads
  other tasks' content. This is a deliberate read bypass, not a fuzzy match:
  `task-?` and `[t]ask-b` name exactly one task and are passed just the same.
  The guard prevents *drift* into a named task, not *containment*: a deliberate
  glob still reads. The exception is refused for Bash, where
  `.claude/local-orchestrators/*` is as easily the target of an `rm -rf`.
- Consequently the friction that motivated the wildcard clause is only half
  gone: the audit's `git log --name-only -- '.claude/local-orchestrators/*'`
  is a **Bash** call and stays denied (split the string, or use Glob/Grep).
- **The `plan/` exception is granted to Bash too** (`READ_TOOLS` below), so a
  Bash command may write to or delete another task's `plan/` files — only the
  structured editors (Edit/Write) are held out. Again: the guard bounds drift,
  it does not contain writes. Narrowing it to Read/Glob/Grep is a behaviour
  change beyond item 02's clause list, so it is disclosed rather than done.

A crashed run leaves a stale ACTIVE behind, which only over-restricts and says
so in every deny reason; `rm .claude/local-orchestrators/ACTIVE` clears it.
"""
import os
import sys

# `json` and `re` are imported inside the functions that need them, never at
# module level: together they cost ~14 ms of a ~15 ms per-call overhead, and
# neither the off-switch nor the fast-inert path ever gets far enough to want
# them. Re-importing later is a `sys.modules` lookup.

# A path segment right after local-orchestrators/, then the rest of that path.
# Stops at whitespace and shell metacharacters so it works on Bash command
# strings as well as plain paths and glob patterns. A segment carrying a glob
# character never equals an active task name, so it is denied by default and
# passed only for the read-only tools below — a deliberate bypass (`task-?`
# names one task as surely as `task-b` does), not a fuzzy match.
# Compiled in main(), for the reason above.
SEG_PATTERN = r"local-orchestrators/+([^/\s\"';|&]+)((?:/[^\s\"';|&]*)?)"
# `notebook_path` is inert while NotebookEdit is off the matcher; it costs
# nothing and keeps the guard honest if the matcher ever grows again.
PATH_KEYS = ("file_path", "path", "pattern", "command", "notebook_path")
READ_TOOLS = {"Read", "Glob", "Grep", "Bash"}
WILDCARD_TOOLS = {"Read", "Glob", "Grep"}
# The sentinels themselves are never "another task": the agents a HALT governs
# must be able to name it, and a run's own ACTIVE line is its handle.
SENTINELS = {"ACTIVE", "HALT"}
GLOB_CHARS = "*?["
OFF_VALUES = {"false", "0", "no", "off"}
ORCH = (".claude", "local-orchestrators")


def guard_disabled():
    """The plugin's `run_scope_guard` userConfig, false → do nothing at all."""
    value = os.environ.get("CLAUDE_PLUGIN_OPTION_RUN_SCOPE_GUARD", "")
    return value.strip().lower() in OFF_VALUES


def project_root():
    """The project anchor, or None. The hook is the ONE component that gets
    `$CLAUDE_PROJECT_DIR` in its environment — with it set, the sentinel
    lookup is one `isfile` at a known path instead of a climb.

    A relative value is resolved against THIS process's cwd, not the payload
    `cwd`: the fast-inert path runs before stdin is read, so the payload's cwd
    is not yet known there, and one rule for both call sites beats two. The
    harness exports an absolute path; `.claude/settings.json` writes
    `${CLAUDE_PROJECT_DIR:-.}` only as a shell fallback.
    """
    root = os.environ.get("CLAUDE_PROJECT_DIR")
    return os.path.abspath(root) if root else None


def tasks_root_override():
    """GD-T5 tier 1: `$ORCH_TASKS_ROOT` names the tasks root outright and
    outranks the project anchor. Item 04 gives `status.sh` and both monitoring
    daemons the same tier; after that, a run which sets it keeps ACTIVE and
    HALT there and nowhere else. Two conditions the module docstring spells
    out: it must be EXPORTED into the `claude` process to reach this hook at
    all, and it must name the tasks root — a *task* directory exists too, so it
    wins the tier and then finds no sentinel."""
    root = os.environ.get("ORCH_TASKS_ROOT")
    return os.path.abspath(root) if root else None


def marker_root(start):
    """GD-T5 tier 3: the first ancestor of `start` holding a `.claude/`
    directory — the project marker, and the ceiling of the climb. Returns None
    when there is no marker anywhere above `start` (then nothing is enforced,
    which is the right answer: there is no project here)."""
    d = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(d, ".claude")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def anchored_orch_dir(anchor):
    """The single tasks directory the anchors point at, or None if neither
    resolves. `anchor` is the project root (tier 2) or None. This is the ONE
    place tiers 1 and 2 are ordered — `main()`'s fast-inert check and
    `tasks_dir()` must never disagree about where the sentinels are.

    The override is honoured only when it exists, so a stale export or a typo
    falls through to the project root instead of beating a populated one. An
    override that exists but names the WRONG directory — a *task* directory,
    say — still wins this tier and finds no sentinel there, leaving the guard
    and the HALT brake inert. That residue is disclosed in the module docstring
    and pinned by `test_tasks_root_override`, not closed in code: a tasks root
    that legitimately has no sentinel yet must stay resolvable.
    """
    override = tasks_root_override()
    if override is not None and os.path.isdir(override):
        return override
    if anchor is not None:
        return os.path.join(anchor, *ORCH)
    return None


def tasks_dir(start, anchor):
    """The ONE tasks directory to consult, or None.

    With an anchor — `$ORCH_TASKS_ROOT`, else the project root — that anchor
    is the ceiling: a sentinel in an ancestor cannot restrict this project, and
    a sentinel nested deeper does not either, because the fast-inert check in
    `main()` runs before stdin is read and therefore before any cwd is known.
    One rule, one location, no path-dependent behaviour.

    With no anchor at all, tier 3 climbs from `start` to the first `.claude/`
    marker and stops there. That marker is a ceiling too: an unrelated
    ancestor's tasks root (a forgotten `~/.claude/local-orchestrators/ACTIVE`)
    must never restrict a project that has its own `.claude/`.
    """
    anchored = anchored_orch_dir(anchor)
    if anchored is not None:
        return anchored
    marker = marker_root(start)
    if marker is not None:
        return os.path.join(marker, *ORCH)
    return None


def find_active(base):
    """The set of active task names (one per line, blanks ignored) from the
    ACTIVE sentinel in the resolved tasks root — empty set if no run is
    active (or if no tasks root resolved at all)."""
    if base is None:
        return set()
    p = os.path.join(base, "ACTIVE")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return {ln.strip() for ln in f if ln.strip()}
        except OSError:
            return set()
    return set()


def find_halt(base):
    """The HALT sentinel next to ACTIVE in the resolved tasks root. While it
    exists, EVERY subagent tool call is denied — the user's emergency brake for
    an orchestration run that has no reachable kill handle. Deleting the file
    lifts the freeze; the main terminal agent is never affected."""
    if base is None:
        return None
    p = os.path.join(base, "HALT")
    return p if os.path.isfile(p) else None


def deny(reason):
    import json
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


def main():
    if guard_disabled():
        return
    root = project_root()
    # The two `isfile` calls below are repeated by find_halt/find_active a few
    # lines on. That duplication is DELIBERATE: this check must happen before
    # stdin is read (item 02(b)) while those two run after, and both must ask
    # `anchored_orch_dir` — one resolver, two callers. Collapsing them is how
    # the fast path and the enforcing path drift apart.
    base = anchored_orch_dir(root)
    if base is not None:
        if not (os.path.isfile(os.path.join(base, "ACTIVE"))
                or os.path.isfile(os.path.join(base, "HALT"))):
            return  # fast-inert: no run here — stdin is never even read
    import json
    try:
        hook = json.load(sys.stdin)
    except Exception:
        return  # a broken payload must never block work
    if not hook.get("agent_id"):
        return  # main terminal agent: unrestricted
    start = hook.get("cwd") or os.getcwd()
    # Resolved once, here: the deny reason must name the tasks root that
    # actually decided, not a hardcoded `.claude/local-orchestrators/` the
    # agent cannot act on when tier 1 or tier 3 chose somewhere else.
    tasks = tasks_dir(start, root)
    halt = find_halt(tasks)
    if halt:
        deny("RUN HALTED by the user: the orchestration run this subagent "
             "belongs to was ordered stopped. Do not retry any tool call. "
             "Return immediately with a short note that the run is halted. "
             f"(sentinel: {halt})")
        return
    active = find_active(tasks)
    if not active:
        return
    tool = hook.get("tool_name", "")
    tool_input = hook.get("tool_input") or {}
    import re
    seg = re.compile(SEG_PATTERN)
    for key in PATH_KEYS:
        value = tool_input.get(key)
        if not isinstance(value, str):
            continue
        for m in seg.finditer(value):
            task, rest = m.group(1), m.group(2) or ""
            if task in active or task in SENTINELS:
                continue
            if tool in READ_TOOLS and (rest == "/plan" or rest.startswith("/plan/")):
                continue  # authority-ladder plans stay readable
            if tool in WILDCARD_TOOLS and any(c in task for c in GLOB_CHARS):
                continue  # a read-only glob over the runs is not a scope breach
            names = ", ".join(f"'{t}'" for t in sorted(active))
            deny(f"Run scope: the active task(s): {names}. "
                 f"{os.path.join(tasks, task)}{rest} belongs to "
                 "another task; during this run only active tasks' "
                 "folders and other tasks' plan/ files are accessible.")
            return


main()
