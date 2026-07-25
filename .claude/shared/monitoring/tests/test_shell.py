#!/usr/bin/env python3
"""Stdlib-only tests for sp-shell fixes (status.sh + implement-plan implement
workflow template + docs). Run as `python3 test_shell.py`; exits non-zero on the first failure.
No pytest, no omnigent imports. Uses ephemeral dirs under /tmp/claude-1000.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
STATUS_SH = REPO / ".claude/shared/monitoring/status.sh"
TEMPLATE = REPO / ".claude/skills/implement-plan/templates/implement.workflow.js"
MONITORING_MD = REPO / ".claude/shared/monitoring/monitoring.md"
M_SKILL = REPO / ".claude/skills/m-orchestrator/SKILL.md"
D_SKILL = REPO / ".claude/skills/implement-plan/SKILL.md"
GITIGNORE = REPO / ".gitignore"

TMP_ROOT = "/tmp/claude-1000"
os.makedirs(TMP_ROOT, exist_ok=True)

failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def run_status(state_dir, args, extra_env=None, unset_state_dir=False, script=None):
    env = {k: v for k, v in os.environ.items() if k not in ("ORCH_STATE_DIR", "ORCH_TITLE")}
    if not unset_state_dir:
        env["ORCH_STATE_DIR"] = str(state_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script or STATUS_SH), *args],
        env=env, capture_output=True, text=True,
    )


# --- status.sh: creates missing state dir + appends one valid JSON line (SHELL-6)
def test_status_creates_state_dir():
    print("test_status_creates_state_dir")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        # A not-yet-created nested dir under the fresh base.
        state_dir = os.path.join(base, "does", "not", "exist", "yet")
        check(not os.path.isdir(state_dir), "state dir does not exist before call")
        proc = run_status(state_dir, ["myplan", "implement", "running", "attempt 1: go"])
        check(proc.returncode == 0, "status.sh exits 0")
        check(os.path.isdir(state_dir), "status.sh created the missing state dir")
        events = os.path.join(state_dir, "events.jsonl")
        check(os.path.isfile(events), "events.jsonl was created")
        lines = Path(events).read_text().splitlines()
        check(len(lines) == 1, f"exactly one event line appended (got {len(lines)})")
        obj = json.loads(lines[0])
        check(obj["plan"] == "myplan" and obj["stage"] == "implement"
              and obj["state"] == "running" and obj["detail"] == "attempt 1: go",
              "event fields round-trip")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- status.sh: hostile detail lands as literal one-line escaped JSON (injection guard)
def test_status_injection_safe():
    print("test_status_injection_safe")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        sentinel = os.path.join(base, "PWNED")
        hostile = f'$(touch {sentinel}) `touch {sentinel}` "quote" and\na newline'
        proc = run_status(state_dir, ["p", "s", "running", hostile])
        check(proc.returncode == 0, "status.sh exits 0 on hostile detail")
        check(not os.path.exists(sentinel), "command substitution did NOT execute (no PWNED file)")
        events = Path(state_dir) / "events.jsonl"
        raw = events.read_text()
        # File must be exactly one physical line (newline escaped inside JSON).
        check(raw.count("\n") == 1, "output is a single physical line (trailing newline only)")
        obj = json.loads(raw.splitlines()[0])
        check(obj["detail"] == hostile, "detail preserved verbatim (incl. newline as literal)")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- status.sh: ORCH_STATE_DIR unset warns on stderr (SHELL-5). Use a COPY of the
#     script in a throwaway dir so the fallback write never touches the real module dir.
def test_status_unset_warns():
    print("test_status_unset_warns")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        script_copy = os.path.join(base, "status.sh")
        shutil.copy(STATUS_SH, script_copy)
        proc = run_status(None, ["p", "s", "running", "hi"], unset_state_dir=True, script=script_copy)
        check(proc.returncode == 0, "status.sh still exits 0 when ORCH_STATE_DIR unset")
        check("ORCH_STATE_DIR unset" in proc.stderr, "warning emitted to stderr when unset")
        # Fallback write lands next to the copied script, NOT the real module dir.
        check(os.path.isfile(os.path.join(base, "events.jsonl")),
              "fallback wrote events.jsonl next to the (copied) script")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- loop.workflow.js static assertions
def test_template_static():
    print("test_template_static")
    src = TEMPLATE.read_text()
    # SHELL-2 / D2: Test marker is role=test, and no gate:run remains in the reference loop.
    check("stage=test role=test attempt=" in src, "Test marker line reads role=test")
    check("role=gate:run" not in src, "no role=gate:run remains in the reference loop")
    # SHELL-10: statusCmd quotes the path interpolations.
    check('ORCH_STATE_DIR="${TASK}" bash "${S}" "${plan}"' in src,
          "statusCmd quotes ${TASK}/${S}/${plan}")
    # SHELL-8: died-gate fallback findings_file is NOT the empty string.
    check("findings_file: ''" not in src and 'findings_file: ""' not in src,
          "no empty-string findings_file fallback remains")
    check("writePlaceholderFindings" in src,
          "died-gate fallback writes/points at a placeholder findings file")
    # Both death paths (gate + critique) route through the placeholder helper.
    check(src.count("await writePlaceholderFindings(") >= 2,
          "both gate and critique death fallbacks use the placeholder")


# --- docs static assertions
def test_docs_static():
    print("test_docs_static")
    md = MONITORING_MD.read_text()
    ms = M_SKILL.read_text()
    ds = D_SKILL.read_text()

    # cache_write in token schema blocks of both docs.
    check("cache_write" in md, "monitoring.md documents cache_write")
    check("cache_write" in ms, "m-orchestrator SKILL.md documents cache_write")
    # stale in the state enum of both docs.
    check("failed|info|stale" in md, "monitoring.md state enum includes stale")
    check("done|failed|info|stale" in ms, "m-orchestrator SKILL.md state enum includes stale")
    # files_changed added to the shape-key list in both docs.
    check("fixed_ids`/`files_changed`" in md, "monitoring.md shape list includes files_changed")
    check("fixed_ids`/`files_changed`" in ms, "m-orchestrator SKILL.md shape list includes files_changed")
    # agent sub-object documented in both docs.
    check('"agent"' in md and '"runtime"' in md, "monitoring.md documents the agent sub-object")
    check('"agent"' in ms and '"runtime"' in ms, "m-orchestrator SKILL.md documents the agent sub-object")
    # config-driven caps noted (D4/#11) in monitoring.md and implement-plan SKILL.md.
    check("max_gate_attempts" in md, "monitoring.md notes config attempt caps")
    check("max_gate_attempts" in ds, "implement-plan SKILL.md notes config attempt caps")


# --- .gitignore contains the module-dir events.jsonl ignore entry
def test_gitignore():
    print("test_gitignore")
    gi = GITIGNORE.read_text()
    check(".claude/shared/monitoring/events.jsonl" in gi,
          ".gitignore ignores the module-dir events.jsonl")
    check(".claude/shared/monitoring/.watcher-state.json" in gi,
          ".gitignore ignores the module-dir .watcher-state.json")


def main():
    for t in (test_status_creates_state_dir, test_status_injection_safe,
              test_status_unset_warns, test_template_static, test_docs_static,
              test_gitignore):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all sp-shell tests passed")


if __name__ == "__main__":
    main()
