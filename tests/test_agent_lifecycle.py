#!/usr/bin/env python3
"""Stdlib-only tests for the agent-lifecycle hook
(`plugin/touch/hooks/agent_lifecycle.py`, D-18) and its registration in the
plugin's ONE `hooks/hooks.json` (GD-U5).
Run as `python3 test_agent_lifecycle.py`; exits non-zero on failure. No pytest.

The hook is exercised exactly as the harness runs it: a subprocess fed one
recorded hook payload on stdin, against a THROWAWAY tasks tree — never against
this repo's own `.touch/local-orchestrators/`, so a test run can neither write
into a live run's stream nor be steered by one. Every subprocess starts from an
environment with `CLAUDE_PROJECT_DIR`, `ORCH_TASKS_ROOT`, `ORCH_STATE_DIR` and
both off-switches stripped.

THE PAYLOADS ARE RECORDED, NOT INVENTED. Every field below comes from the D-17
probe (2026-07-31, CLI 2.1.220 —
`.touch/local-orchestrators/touch-determinism/findings/d17-hook-probe-2026-07-31.md`):
the seven-key `SubagentStart`, the `SubagentStop` that adds
`agent_transcript_path` / `last_assistant_message` / `background_tasks`, and the
`PostToolUse` whose `tool_response` is the launch record verbatim. That matters
for the negative assertions in particular: `last_assistant_message` really is
sitting in the payload, and this suite pins that the hook does NOT turn it into
a verdict (R-58).

What is asserted, and why each is a defect if it regresses:

* **registration** — four events, exec form, `${CLAUDE_PLUGIN_ROOT}` args, and
  the PreToolUse entry still the guard's and only the guard's. A second
  registration of anything is GD-U5's measured double-fire.
* **inert without ACTIVE** — a plugin-enabled session that is not orchestrating
  must pay nothing and write nothing, on every event.
* **exact lines / exact merges** — the `status.sh` line the lifecycle arm
  produces, and the four keys the bind arm merges *without* dropping the
  driver's own config keys.
* **degradation** — a payload missing every optional field, a truncated
  transcript, an unparseable body: rc 0, no output, no file touched.
* **the two refusals** — `stage=plan` / `stage=complete` are never addressed by
  a hook (only those two can settle a card or a run in `monitor.html`), and
  `SubagentStop` never writes `done`/`failed`.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _roots import ORCH_REL, PAYLOAD, SRC       # noqa: E402  (path juggling first)

sys.path.insert(0, str(SRC))
from aggregator import custom_state as cs       # noqa: E402

HOOK = PAYLOAD / "hooks" / "agent_lifecycle.py"
HOOKS_JSON = PAYLOAD / "hooks" / "hooks.json"
GUARD_ARG = "${CLAUDE_PLUGIN_ROOT}/hooks/orch_scope_guard.py"
HOOK_ARG = "${CLAUDE_PLUGIN_ROOT}/hooks/agent_lifecycle.py"
#: The events the lifecycle hook is registered for, and the matcher each carries.
LIFECYCLE_EVENTS = {"SubagentStart": "*", "SubagentStop": "*",
                    "PostToolUse": "Workflow|Artifact"}
#: The project marker every fixture gets — `.claude/` marks a Claude Code
#: project, `.touch/local-orchestrators/` holds the runs (G10).
MARKER = ".claude"
#: A 17-hex agentId, the shape the harness generates.
AGENT = "aba78ac6b9596fd3a"

failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        failures.append(msg)
        print(f"  FAIL: {msg}")


# --------------------------------------------------------------------------
# fixture construction
# --------------------------------------------------------------------------

def make_tree(tmp, tasks=("task-a",), active=("task-a",)):
    """A throwaway project: marker dir, tasks root, ACTIVE, one dir per task.

    Built at the CURRENT tasks root (`ORCH_REL`), so this file is a positive
    control for the resolver and not merely a proof that it stays inert.
    """
    root = Path(tmp)
    (root / MARKER).mkdir(parents=True, exist_ok=True)
    orch = root / ORCH_REL
    orch.mkdir(parents=True, exist_ok=True)
    for name in tasks:
        (orch / name / "orch-scripts").mkdir(parents=True, exist_ok=True)
        (orch / name / "report").mkdir(parents=True, exist_ok=True)
    if active is not None:
        (orch / "ACTIVE").write_text("".join(f"{n}\n" for n in active),
                                     encoding="utf-8")
    return orch


def write_transcript(path, prompt):
    """An agent transcript whose FIRST user record carries `prompt`.

    Two decoys first — a `system` record and a torn/unparseable line — because
    that is what these files look like on this substrate and the hook's reader
    is supposed to skip both rather than give up.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"type": "system", "content": "boot"}),
        '{"type": "user", "message": {"content": "torn',
        json.dumps({"type": "user",
                    "message": {"role": "user",
                                "content": [{"type": "text", "text": prompt}]}}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def hook_env(**overrides):
    env = dict(os.environ)
    for name in ("CLAUDE_PROJECT_DIR", "ORCH_TASKS_ROOT", "ORCH_STATE_DIR",
                 "TOUCH_AGENT_LIFECYCLE", "CLAUDE_PLUGIN_OPTION_AGENT_LIFECYCLE"):
        env.pop(name, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for k, v in overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    return env


def run_hook(payload, **env_overrides):
    """Run the hook as the harness does; returns (rc, stdout)."""
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True, text=True, timeout=60,
        env=hook_env(**env_overrides))
    return proc.returncode, proc.stdout.strip()


def start_payload(cwd, session, **extra):
    """The recorded `SubagentStart` shape — seven keys, no more (D-17)."""
    payload = {"session_id": "35375fe0-ee53-4c6b-814b-4a4f9dfa4944",
               "transcript_path": session,
               "cwd": cwd,
               "prompt_id": "e204f141-c679-4790-bc7d-1b2e62539d45",
               "agent_id": AGENT,
               "agent_type": "workflow-subagent",
               "hook_event_name": "SubagentStart"}
    payload.update(extra)
    return payload


def stop_payload(cwd, session, agent_transcript, **extra):
    """The recorded `SubagentStop` shape, `last_assistant_message` included —
    it is in the real payload, and the point is that the hook ignores it."""
    payload = start_payload(cwd, session)
    payload.update({
        "hook_event_name": "SubagentStop",
        "permission_mode": "bypassPermissions",
        "stop_hook_active": False,
        "agent_transcript_path": agent_transcript,
        "last_assistant_message": "ONE",
        "background_tasks": [{"id": "wchbpg3tb", "type": "workflow",
                              "status": "running",
                              "description": "two trivial agents",
                              "name": "probe-lifecycle"}],
        "session_crons": []})
    payload.update(extra)
    return payload


def launch_payload(cwd, script_path, wf_dir, **response):
    """The recorded `PostToolUse` for a `Workflow` launch — `tool_response`
    verbatim from the probe, `tool_input` carrying the whole script source (it
    does; the hook must not choke on the size)."""
    resp = {"status": "async_launched", "taskId": "wchbpg3tb",
            "taskType": "local_workflow", "workflowName": "probe-lifecycle",
            "runId": "wf_116dd04c-f60", "summary": "two trivial agents",
            "transcriptDir": wf_dir, "scriptPath": script_path}
    resp.update(response)
    return {"session_id": "35375fe0", "transcript_path": "/dev/null",
            "cwd": cwd, "prompt_id": "e83d60cd", "permission_mode": "default",
            "hook_event_name": "PostToolUse", "tool_name": "Workflow",
            "tool_use_id": "toolu_x", "duration_ms": 4,
            "tool_input": {"scriptPath": script_path, "script": "x" * 5000},
            "tool_response": resp}


def events(task_dir):
    """Parsed `events.jsonl` lines for a task, `[]` when the file is absent."""
    path = Path(task_dir) / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

def test_registration():
    print("test_registration")
    check(HOOK.is_file() and os.access(HOOK, os.X_OK),
          "agent_lifecycle.py lives in the plugin subtree and is executable")
    doc = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    hooks = doc.get("hooks", {})
    check(set(hooks) == {"PreToolUse"} | set(LIFECYCLE_EVENTS),
          "hooks.json declares exactly PreToolUse + the three lifecycle events")
    # The guard's entry is untouched: GD-U5's one-registration rule is about
    # ONE FILE, not one event, and this file growing must not disturb it.
    pre = hooks.get("PreToolUse", [])
    check(len(pre) == 1 and pre[0]["hooks"][0]["args"] == [GUARD_ARG],
          "the PreToolUse entry is still the guard's, and only the guard's")
    for event, matcher in LIFECYCLE_EVENTS.items():
        entries = hooks.get(event, [])
        check(len(entries) == 1, f"{event} declares exactly one entry")
        if not entries:
            continue
        entry = entries[0]
        check(entry.get("matcher") == matcher,
              f"{event} matcher is {matcher!r} (the form D-17 proved fires)")
        commands = entry.get("hooks", [])
        check(len(commands) == 1, f"{event} declares exactly one hook command")
        cmd = commands[0] if commands else {}
        check(cmd.get("type") == "command" and cmd.get("command") == "python3",
              f"{event} uses exec form: command is the bare interpreter")
        # GD-T4 again: args[] IS substituted, a shell-form command string is not.
        check(cmd.get("args") == [HOOK_ARG],
              f"{event} passes the hook via args[] with ${{CLAUDE_PLUGIN_ROOT}}")


def test_inert_without_active():
    print("test_inert_without_active")
    with tempfile.TemporaryDirectory(prefix="lifecycle-inert-") as tmp:
        orch = make_tree(tmp, active=None)
        task = orch / "task-a"
        script = task / "orch-scripts" / "implement.workflow.js"
        script.write_text("//\n", encoding="utf-8")
        write_transcript(task / "wf" / f"agent-{AGENT}.jsonl",
                         "[monitor] plan=sp-01 stage=impl role=impl attempt=1")
        for payload in (start_payload(tmp, "/dev/null"),
                        launch_payload(tmp, str(script), str(task / "wf"))):
            rc, out = run_hook(payload, CLAUDE_PROJECT_DIR=tmp)
            check(rc == 0 and out == "",
                  f"{payload['hook_event_name']}/{payload.get('tool_name','-')}"
                  " is silent with no ACTIVE sentinel")
        check(not (task / "orch-config.json").exists(),
              "no orch-config.json is created with no ACTIVE sentinel")
        check(events(task) == [], "no event is written with no ACTIVE sentinel")


def test_workflow_bind():
    print("test_workflow_bind")
    with tempfile.TemporaryDirectory(prefix="lifecycle-bind-") as tmp:
        orch = make_tree(tmp)
        task = orch / "task-a"
        script = task / "orch-scripts" / "implement.workflow.js"
        script.write_text("//\n", encoding="utf-8")
        # The driver's own config is already there: the merge must PRESERVE it.
        (task / "orch-config.json").write_text(
            json.dumps({"port": 8931, "max_plan_attempts": 10}), encoding="utf-8")
        rc, out = run_hook(launch_payload(tmp, str(script), "/wf/dir"),
                           CLAUDE_PROJECT_DIR=tmp)
        check(rc == 0 and out == "", "the bind arm is silent (writes, says nothing)")
        doc = json.loads((task / "orch-config.json").read_text(encoding="utf-8"))
        check(doc.get("wf_dir") == "/wf/dir", "wf_dir merged from transcriptDir")
        check(doc.get("run_id") == "wf_116dd04c-f60", "run_id merged from runId")
        check(doc.get("task_id") == "wchbpg3tb", "task_id merged from taskId")
        check(doc.get("workflow_name") == "probe-lifecycle",
              "workflow_name merged from workflowName")
        check(doc.get("port") == 8931 and doc.get("max_plan_attempts") == 10,
              "the driver's own config keys survive the merge")
        check("summary" not in doc and "scriptPath" not in doc,
              "only the four join keys are published, not the whole launch record")

        # Idempotent: the same launch twice leaves the same document.
        before = (task / "orch-config.json").read_text(encoding="utf-8")
        run_hook(launch_payload(tmp, str(script), "/wf/dir"), CLAUDE_PROJECT_DIR=tmp)
        check((task / "orch-config.json").read_text(encoding="utf-8") == before,
              "re-firing the same launch rewrites nothing")


def test_workflow_bind_refusals():
    print("test_workflow_bind_refusals")
    with tempfile.TemporaryDirectory(prefix="lifecycle-refuse-") as tmp:
        orch = make_tree(tmp)
        task = orch / "task-a"
        script = task / "orch-scripts" / "implement.workflow.js"
        script.write_text("//\n", encoding="utf-8")
        # (1) a taskType that is not a local workflow
        run_hook(launch_payload(tmp, str(script), "/wf/dir", taskType="agent"),
                 CLAUDE_PROJECT_DIR=tmp)
        check(not (task / "orch-config.json").exists(),
              "a non-local_workflow taskType is ignored")
        # (2) a script OUTSIDE the resolved tasks root — another project's run
        with tempfile.TemporaryDirectory(prefix="lifecycle-foreign-") as other:
            foreign = Path(other) / "some-task" / "orch-scripts" / "x.workflow.js"
            foreign.parent.mkdir(parents=True)
            foreign.write_text("//\n", encoding="utf-8")
            run_hook(launch_payload(tmp, str(foreign), "/wf/dir"),
                     CLAUDE_PROJECT_DIR=tmp)
            check(not (Path(other) / "some-task" / "orch-config.json").exists(),
                  "a launch from outside the tasks root writes nothing")
        # (3) a scriptPath that is not under an orch-scripts/ dir
        run_hook(launch_payload(tmp, str(task / "x.workflow.js"), "/wf/dir"),
                 CLAUDE_PROJECT_DIR=tmp)
        check(not (task / "orch-config.json").exists(),
              "a scriptPath not under orch-scripts/ derives no task")


def test_lifecycle_line():
    print("test_lifecycle_line")
    with tempfile.TemporaryDirectory(prefix="lifecycle-line-") as tmp:
        orch = make_tree(tmp)
        task = orch / "task-a"
        session = str(Path(tmp) / "sess.jsonl")
        agent_tx = Path(tmp) / "sess" / "subagents" / f"agent-{AGENT}.jsonl"
        write_transcript(agent_tx,
                         "[monitor] plan=sp-07-hooks stage=implement "
                         "role=impl attempt=2\nDo the thing.")

        rc, out = run_hook(start_payload(tmp, session), CLAUDE_PROJECT_DIR=tmp)
        check(rc == 0 and out == "", "SubagentStart says nothing to the model")
        rows = events(task)
        check(len(rows) == 1, f"SubagentStart wrote exactly one event ({len(rows)})")
        ev = rows[0] if rows else {}
        check(ev.get("plan") == "sp-07-hooks" and ev.get("stage") == "implement",
              "the line is addressed by the [monitor] marker, not invented")
        check(ev.get("state") == "running", "SubagentStart writes state=running")
        check(ev.get("w") == "agent",
              "it went through status.sh (w=agent), the ONE write path (GD-D5)")
        check("impl #2" in (ev.get("detail") or ""),
              "detail names the role and attempt from the marker")
        check('"' not in (ev.get("detail") or "")
              and "\n" not in (ev.get("detail") or ""),
              "detail is single-line and free of double quotes (GD-11)")

        run_hook(stop_payload(tmp, session, str(agent_tx)), CLAUDE_PROJECT_DIR=tmp)
        rows = events(task)
        check(len(rows) == 2, f"SubagentStop wrote exactly one more event ({len(rows)})")
        stop = rows[-1] if len(rows) > 1 else {}
        # THE central refusal: `last_assistant_message` was in the payload.
        check(stop.get("state") == "info",
              "SubagentStop writes info — a stop is not a verdict (R-58)")
        check(stop.get("state") not in ("done", "failed"),
              "SubagentStop never writes done/failed")
        check("ONE" not in json.dumps(stop),
              "last_assistant_message is never repeated into the event stream")


def test_reserved_stages_refused():
    print("test_reserved_stages_refused")
    for stage in ("plan", "complete"):
        with tempfile.TemporaryDirectory(prefix="lifecycle-reserved-") as tmp:
            orch = make_tree(tmp)
            task = orch / "task-a"
            session = str(Path(tmp) / "sess.jsonl")
            write_transcript(Path(tmp) / "sess" / "subagents" / f"agent-{AGENT}.jsonl",
                             f"[monitor] plan=sp-01 stage={stage} role=impl attempt=1")
            run_hook(start_payload(tmp, session), CLAUDE_PROJECT_DIR=tmp)
            check(events(task) == [],
                  f"stage={stage} is refused — only those two settle a card/run")


def test_ledger_line():
    print("test_ledger_line")
    with tempfile.TemporaryDirectory(prefix="lifecycle-ledger-") as tmp:
        orch = make_tree(tmp)
        task = orch / "task-a"
        session = str(Path(tmp) / "sess.jsonl")
        write_transcript(Path(tmp) / "sess" / "subagents" / f"agent-{AGENT}.jsonl",
                         "[touch] name=root_impl1 parent=root root=root "
                         "role=impl attempt=3\nGo.")
        run_hook(start_payload(tmp, session, agent_type="general-purpose"),
                 CLAUDE_PROJECT_DIR=tmp)
        ledger = task / cs.STATE_DIR / cs.LEDGER_FILE
        check(ledger.is_file(), "a [touch] marker produces a spawn-ledger line")
        if not ledger.is_file():
            return
        rec = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        check(rec.get("name") == "root_impl1" and rec.get("root") == "root",
              "name/root come from the marker")
        check(rec.get("attempt") == 3 and isinstance(rec["attempt"], int),
              "attempt is an int — never defaulted, never a string (GD-9)")
        check(rec.get("parent") == "root" and rec.get("role") == "impl",
              "parent/role are carried when the marker states them")
        check(rec.get("taskId") == AGENT,
              "the Agent-tool profile records taskId — the id IS the agentId")
        check(rec.get("w") == "hook", "the line names its own writer")
        # The whole point: `custom_state` must ACCEPT it, with a STATED session.
        counters = cs.new_counters()
        obs = cs.read_ledger_file(str(ledger), counters=counters)
        check(len(obs) == 1 and counters["parsed"] == 1,
              f"custom_state.read_ledger_file accepts the line ({counters})")
        if obs:
            check(obs[0].session_key_source == "ledger",
                  "sessionKey is STATED (read from /proc), not derived from the path")

        # A Workflow agent: no [touch] marker, and no per-agent stop handle.
        write_transcript(Path(tmp) / "sess" / "subagents" / "agent-b.jsonl",
                         "[monitor] plan=sp-01 stage=impl role=impl attempt=1")
        run_hook(start_payload(tmp, session, agent_id="b"), CLAUDE_PROJECT_DIR=tmp)
        lines = ledger.read_text(encoding="utf-8").splitlines()
        check(len(lines) == 1,
              "a [monitor]-only (Workflow) prompt writes no ledger line")


def test_multi_task_join():
    print("test_multi_task_join")
    with tempfile.TemporaryDirectory(prefix="lifecycle-multi-") as tmp:
        orch = make_tree(tmp, tasks=("task-a", "task-b"),
                         active=("task-a", "task-b"))
        wf = Path(tmp) / "sess" / "subagents" / "workflows" / "wf_1"
        (orch / "task-b" / "orch-config.json").write_text(
            json.dumps({"wf_dir": str(wf)}), encoding="utf-8")
        session = str(Path(tmp) / "sess.jsonl")
        agent_tx = wf / f"agent-{AGENT}.jsonl"
        write_transcript(agent_tx,
                         "[monitor] plan=sp-09 stage=impl role=impl attempt=1")

        # Start: two tasks live, no transcript path in the payload, so there is
        # no honest join — and a hook writes nothing rather than guessing.
        run_hook(start_payload(tmp, session), CLAUDE_PROJECT_DIR=tmp)
        check(events(orch / "task-a") == [] and events(orch / "task-b") == [],
              "an ambiguous SubagentStart writes nothing at all")

        # Stop: agent_transcript_path names the run, orch-config names the task.
        run_hook(stop_payload(tmp, session, str(agent_tx)), CLAUDE_PROJECT_DIR=tmp)
        check(events(orch / "task-a") == [],
              "the unrelated active task's stream is untouched")
        rows = events(orch / "task-b")
        check(len(rows) == 1 and rows[0].get("plan") == "sp-09",
              "the event lands on the task whose wf_dir matches the transcript")


def test_artifact_advisory():
    print("test_artifact_advisory")
    with tempfile.TemporaryDirectory(prefix="lifecycle-artifact-") as tmp:
        orch = make_tree(tmp)
        good = orch / "task-a" / "report" / "r.html"
        good.write_text("<p>x</p>", encoding="utf-8")

        def artifact(path):
            return {"cwd": tmp, "hook_event_name": "PostToolUse",
                    "tool_name": "Artifact",
                    "tool_input": {"file_path": path},
                    "tool_response": {"url": "https://claude.ai/x", "path": path,
                                      "title": "t", "version": 1}}

        rc, out = run_hook(artifact(str(good)), CLAUDE_PROJECT_DIR=tmp)
        check(rc == 0 and out == "",
              "a publish from an active task's report/ is silent")
        rc, out = run_hook(artifact(str(Path(tmp) / "stray.html")),
                           CLAUDE_PROJECT_DIR=tmp)
        check(rc == 0, "the advisory never fails the call")
        doc = json.loads(out) if out else {}
        block = doc.get("hookSpecificOutput", {})
        check("additionalContext" in block,
              "a misfiled publish earns an additionalContext warning")
        check("permissionDecision" not in block,
              "the provenance arm NEVER denies (advisory only, SUBSTRATE-12)")
        check("stray.html" in block.get("additionalContext", ""),
              "the warning names the path it is about")


def test_degrades_silently():
    print("test_degrades_silently")
    with tempfile.TemporaryDirectory(prefix="lifecycle-degrade-") as tmp:
        orch = make_tree(tmp)
        task = orch / "task-a"
        cases = {
            "unparseable stdin": "{not json",
            "empty stdin": "",
            "a JSON array instead of an object": "[]",
            "no hook_event_name": {"cwd": tmp},
            "an unknown event": {"cwd": tmp, "hook_event_name": "PreCompact"},
            "SubagentStart with no agent_id": {"cwd": tmp,
                                               "hook_event_name": "SubagentStart"},
            "SubagentStart with no transcript at all":
                start_payload(tmp, "/nope/none.jsonl"),
            "PostToolUse Workflow with no tool_response":
                {"cwd": tmp, "hook_event_name": "PostToolUse",
                 "tool_name": "Workflow"},
            "PostToolUse Artifact with no path":
                {"cwd": tmp, "hook_event_name": "PostToolUse",
                 "tool_name": "Artifact", "tool_response": {}},
            "an untracked tool": {"cwd": tmp, "hook_event_name": "PostToolUse",
                                  "tool_name": "Bash", "tool_input": {}},
        }
        for label, payload in cases.items():
            rc, out = run_hook(payload, CLAUDE_PROJECT_DIR=tmp)
            check(rc == 0 and out == "", f"degrades silently: {label}")
        check(events(task) == [] and not (task / "orch-config.json").exists(),
              "no degraded payload wrote anything")


def test_off_switch():
    print("test_off_switch")
    with tempfile.TemporaryDirectory(prefix="lifecycle-off-") as tmp:
        orch = make_tree(tmp)
        task = orch / "task-a"
        session = str(Path(tmp) / "sess.jsonl")
        write_transcript(Path(tmp) / "sess" / "subagents" / f"agent-{AGENT}.jsonl",
                         "[monitor] plan=sp-01 stage=impl role=impl attempt=1")
        for var in ("TOUCH_AGENT_LIFECYCLE", "CLAUDE_PLUGIN_OPTION_AGENT_LIFECYCLE"):
            rc, out = run_hook(start_payload(tmp, session),
                               CLAUDE_PROJECT_DIR=tmp, **{var: "0"})
            check(rc == 0 and out == "" and events(task) == [],
                  f"{var}=0 turns the hook off entirely")
        # A value that is not an off-spelling must NOT disable it (a truthy
        # export is not a request to go quiet).
        rc, out = run_hook(start_payload(tmp, session),
                           CLAUDE_PROJECT_DIR=tmp, TOUCH_AGENT_LIFECYCLE="1")
        check(len(events(task)) == 1, "a non-off value leaves the hook armed")


def test_tasks_root_tiers():
    print("test_tasks_root_tiers")
    with tempfile.TemporaryDirectory(prefix="lifecycle-tiers-") as tmp:
        orch = make_tree(tmp)
        task = orch / "task-a"
        session = str(Path(tmp) / "sess.jsonl")
        write_transcript(Path(tmp) / "sess" / "subagents" / f"agent-{AGENT}.jsonl",
                         "[monitor] plan=sp-01 stage=impl role=impl attempt=1")
        # Tier 1: $ORCH_TASKS_ROOT names the root outright.
        run_hook(start_payload(tmp, session), ORCH_TASKS_ROOT=str(orch))
        check(len(events(task)) == 1, "tier 1: $ORCH_TASKS_ROOT resolves the root")
        # Tier 1 stale: a non-existent override falls THROUGH to tier 3 (the
        # payload cwd's walk-up), rather than disarming the hook.
        run_hook(start_payload(tmp, session),
                 ORCH_TASKS_ROOT=str(Path(tmp) / "gone"))
        check(len(events(task)) == 2,
              "tier 1: a stale override falls through instead of disarming")
        # Tier 3: no env at all, walk up from the payload cwd to the marker.
        deep = Path(tmp) / "a" / "b"
        deep.mkdir(parents=True)
        run_hook(start_payload(str(deep), session))
        check(len(events(task)) == 3, "tier 3: the cwd walk-up finds the marker")


def main():
    print("=" * 60)
    print("agent lifecycle hook (D-18) — payloads recorded by the D-17 probe")
    print("=" * 60)
    for t in (test_registration, test_inert_without_active, test_workflow_bind,
              test_workflow_bind_refusals, test_lifecycle_line,
              test_reserved_stages_refused, test_ledger_line,
              test_multi_task_join, test_artifact_advisory,
              test_degrades_silently, test_off_switch, test_tasks_root_tiers):
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
