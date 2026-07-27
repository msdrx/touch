#!/usr/bin/env python3
"""Stdlib-only tests for the run-scope guard
(.claude/hooks/orch_scope_guard.py) and its settings.json registration.
Run as `python3 test_scope_guard.py`; exits non-zero on failure. No pytest.

The guard is exercised as it runs in production: a subprocess fed the
PreToolUse JSON on stdin, against a throwaway task tree — never against the
repo's own local-orchestrators state, so a test run can never scope-restrict
(or be restricted by) a live session.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / ".claude" / "hooks" / "orch_scope_guard.py"
SETTINGS = REPO / ".claude" / "settings.json"

failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        failures.append(msg)
        print(f"  FAIL: {msg}")


def run_guard(payload):
    """Run the guard exactly as the harness does; returns (rc, stdout)."""
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True, text=True, timeout=30,
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


def test_registration():
    print("test_registration")
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    entries = settings.get("hooks", {}).get("PreToolUse", [])
    ours = [e for e in entries
            if any("orch_scope_guard.py" in h.get("command", "")
                   for h in e.get("hooks", []))]
    check(len(ours) == 1, "settings.json registers orch_scope_guard once")
    if ours:
        matcher = ours[0].get("matcher", "")
        for tool in ("Read", "Glob", "Grep", "Edit", "Write", "Bash"):
            check(tool in matcher.split("|"), f"matcher covers {tool}")
    check(os.access(GUARD, os.X_OK), "guard script is executable")


def test_scoping():
    print("test_scoping")
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        orch = make_tree(tmp)
        a = orch / "task-a" / "findings" / "f.md"
        b = orch / "task-b" / "findings" / "f.md"
        b_plan = orch / "task-b" / "plan" / "f.md"

        rc, out = run_guard(hook_call("Read", tmp, agent=False, file_path=str(b)))
        check(rc == 0 and decision(out) == "allow",
              "main agent (no agent_id) reads other task freely")

        rc, out = run_guard(hook_call("Read", tmp, file_path=str(a)))
        check(decision(out) == "allow", "subagent reads the ACTIVE task freely")

        rc, out = run_guard(hook_call("Read", tmp, file_path=str(b)))
        check(rc == 0 and decision(out) == "deny",
              "subagent Read of another task's findings is denied")
        check("task-a" in out and "task-b" in out,
              "deny reason names both the active and the offending task")

        rc, out = run_guard(hook_call("Read", tmp, file_path=str(b_plan)))
        check(decision(out) == "allow",
              "another task's plan/ stays readable (authority ladder)")

        rc, out = run_guard(hook_call("Edit", tmp, file_path=str(b_plan)))
        check(decision(out) == "deny", "plan/ exception is read-only: Edit denied")

        rc, out = run_guard(hook_call("Write", tmp, file_path=str(b)))
        check(decision(out) == "deny", "subagent Write into another task denied")

        rc, out = run_guard(hook_call(
            "Read", tmp, file_path=str(orch / "ACTIVE")))
        check(decision(out) == "allow", "the ACTIVE sentinel itself is readable")

        # cwd deeper than the repo root still finds the sentinel by walk-up
        deep = Path(tmp) / "sub" / "dir"
        deep.mkdir(parents=True)
        rc, out = run_guard(hook_call("Read", deep, file_path=str(b)))
        check(decision(out) == "deny", "sentinel found by walking up from cwd")


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

        rc, out = run_guard(hook_call(
            "Glob", tmp, pattern=".claude/local-orchestrators/*/findings/*.md"))
        check(decision(out) == "deny", "wildcard task segment in Glob denied")

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


def test_inert_modes():
    print("test_inert_modes")
    with tempfile.TemporaryDirectory(prefix="scope-guard-") as tmp:
        orch = make_tree(tmp)
        b = orch / "task-b" / "findings" / "f.md"
        (orch / "ACTIVE").unlink()
        rc, out = run_guard(hook_call("Read", tmp, file_path=str(b)))
        check(rc == 0 and decision(out) == "allow",
              "no ACTIVE sentinel: guard is inert even for subagents")

    rc, out = run_guard("this is not json")
    check(rc == 0 and out == "", "malformed stdin: exit 0, no output, no block")


def main():
    for t in (test_registration, test_scoping, test_bash_and_glob,
              test_multi_run, test_inert_modes):
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
