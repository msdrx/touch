#!/usr/bin/env python3
"""PreToolUse run-scope guard for orchestration loops.

While a run is active — `.claude/local-orchestrators/ACTIVE` lists the active
task names, one per line — subagent tool calls may touch only those tasks'
folders under `local-orchestrators/`. Every OTHER task keeps its `plan/`
readable (the
authority ladder lives in old task folders) and has everything else denied.
Two deliberate non-restrictions: the main terminal agent (no `agent_id` in the
hook payload — the field is present only for subagent calls) always sees
everything, and with no ACTIVE file the guard is inert, so ordinary sessions
are unaffected. Registered in `.claude/settings.json`; stdlib only.

Bash coverage is textual — the hook sees only the command string, so it denies
commands that NAME another task's folder. That is the right strength for the
actual threat (loop agents drifting into other tasks' state and getting
distracted), not an adversarial sandbox.

A crashed run leaves a stale ACTIVE behind, which only over-restricts and says
so in every deny reason; `rm .claude/local-orchestrators/ACTIVE` clears it.
"""
import json
import os
import re
import sys

# A path segment right after local-orchestrators/, then the rest of that path.
# Stops at whitespace and shell metacharacters so it works on Bash command
# strings as well as plain paths and glob patterns (a wildcard segment like
# `*` is "some task", which is never the active one — denied).
SEG = re.compile(r"local-orchestrators/+([^/\s\"';|&]+)((?:/[^\s\"';|&]*)?)")
PATH_KEYS = ("file_path", "path", "pattern", "command", "notebook_path")
READ_TOOLS = {"Read", "Glob", "Grep", "Bash"}


def find_active(start):
    """Walk up from `start` to the ACTIVE sentinel; the set of active task
    names (one per line, blanks ignored) — empty set if no run is active."""
    d = start
    while True:
        p = os.path.join(d, ".claude", "local-orchestrators", "ACTIVE")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return {ln.strip() for ln in f if ln.strip()}
            except OSError:
                return set()
        parent = os.path.dirname(d)
        if parent == d:
            return set()
        d = parent


def main():
    try:
        hook = json.load(sys.stdin)
    except Exception:
        return  # a broken payload must never block work
    if not hook.get("agent_id"):
        return  # main terminal agent: unrestricted
    active = find_active(hook.get("cwd") or os.getcwd())
    if not active:
        return
    tool = hook.get("tool_name", "")
    tool_input = hook.get("tool_input") or {}
    for key in PATH_KEYS:
        value = tool_input.get(key)
        if not isinstance(value, str):
            continue
        for m in SEG.finditer(value):
            task, rest = m.group(1), m.group(2) or ""
            if task in active or task == "ACTIVE":
                continue
            if tool in READ_TOOLS and (rest == "/plan" or rest.startswith("/plan/")):
                continue  # authority-ladder plans stay readable
            names = ", ".join(f"'{t}'" for t in sorted(active))
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Run scope: the active task(s): {names}. "
                    f".claude/local-orchestrators/{task}{rest} belongs to "
                    "another task; during this run only active tasks' "
                    "folders and other tasks' plan/ files are accessible."),
            }}))
            return


main()
