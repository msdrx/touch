#!/usr/bin/env python3
"""Agent-lifecycle / run-bind / provenance hook (D-18, gated on the D-17 probe).

One script, four independently gated jobs, all of them **additive**: every fact
this hook records is a fact the deterministic floor already derives more slowly
(`decision_watcher.py` from the run journal, `touch-run bind` from the newest
`wf_*` fallback). GD-D5 is the whole design constraint — *hooks are additive,
never the floor* — so this file appends a line and exits, is inert without an
`ACTIVE` sentinel, never denies anything, and swallows every one of its own
errors. If it does not run, nothing is lost; if it runs, the same facts arrive
earlier and at 100% coverage instead of the 93.4% a mandated `touch-status`
call achieved.

WHAT THE PROBE PROVED, AND WHAT IT DID NOT
------------------------------------------
D-17 (2026-07-31, CLI 2.1.220 —
`.touch/local-orchestrators/touch-determinism/findings/d17-hook-probe-2026-07-31.md`,
summarized in `inception.md` §3) ran a two-agent Workflow and a one-agent
Agent-tool spawn under recording hooks. Facts this module codes against, none of
them assumed:

* `SubagentStart` / `SubagentStop` fire once per `agent()` call in BOTH
  profiles, matcher ``"*"``, with zero LLM cooperation.
* `SubagentStart` carries `agent_id` + `agent_type` and **nothing else useful**
  — no `runId`, no label, no phase, and **no prompt**, so the `[monitor]` marker
  is not in the payload. Anything card-shaped has to come out of the agent
  transcript, and at Start that file may not exist yet (the event fires ~3 ms
  after launch). See `MARKER_WAIT_MS`.
* `SubagentStop` adds `agent_transcript_path` (`…/workflows/<runId>/agent-<id>.jsonl`
  for a Workflow agent), `last_assistant_message` and `background_tasks[]`.
* `PostToolUse` matcher `Workflow` fires **at launch**, 3 ms before the first
  `SubagentStart`, with the launch record verbatim in `tool_response`.
* `agent_type` is the profile discriminator: `"workflow-subagent"` vs the
  Agent-tool agent type name.
* `os.getppid()` inside a hook is the `claude` process, so `<pid>-<procStart>`
  is a *stated* `sessionKey` rather than a path guess.

NOT proved: the `Artifact` matcher (no publish happened in the sandbox). Job (d)
is therefore advisory-only by design as well as by decision — it warns, never
denies, and a wrong guess costs a sentence of context.

THE FOUR JOBS
-------------
(a) `SubagentStart` / `SubagentStop` → ONE `status.sh` line each, addressed by
    the `[monitor]` marker read out of the agent transcript. Start writes
    `running` — the same lane, and the same line, the deleted per-agent mandate
    wrote (D-09), only earlier and without a token. Stop writes **`info`, not
    `done`**: the event observes a *stop*, and the verdict is the journal
    `result` the watcher reads. `last_assistant_message` is right there in the
    payload and is NOT a verdict; reading it as one is R-58's fabricated badge
    with a new author. Two stage names are refused outright — `plan` and
    `complete` are the only stages `monitor.html` lets set a CARD badge or a run
    terminal, and a hook must never be able to settle a run.
(b) `PostToolUse` matcher `Workflow`, `taskType == "local_workflow"` → merge
    `{wf_dir, run_id, task_id, workflow_name}` into that task's
    `orch-config.json`. This is the exact join the driver used to retype from
    its own tool result (SUBSTRATE-5), and it lands before any agent event, so
    it is the replacement for the racy newest-`wf_*` fallback `touch-run bind`
    still carries. `touch-run` is not edited here — its "exact when D-18 lands"
    branch simply reads the merged config.
(c) `SubagentStart` → one spawn-ledger line at `<task>/state/spawn-ledger.jsonl`
    when the prompt carries a `[touch]` marker, replacing the hand-written
    mandate D-19 deletes (zero ledgers were ever written by hand). Workflow
    prompts carry `[monitor]`, which has no `name`/`root`, so a Workflow agent
    correctly produces no ledger line — `custom_state.read_ledger_file` would
    drop a nameless one as `skipped_unaddressable` anyway.
(d) `PostToolUse` matcher `Artifact` → advisory warning when the published path
    is not under an active task's `report/` or `findings/` (SUBSTRATE-12,
    CLAUDE.md's "every generated deliverable is stored in the repo" rule).
    `additionalContext`, never a denial: refusing a publish over a path
    convention is a worse failure than a misfiled report.

WHERE THE SENTINELS ARE LOOKED FOR
----------------------------------
The same three rungs, in the same order, as `orch_scope_guard.py` and
`status.sh` — `$ORCH_TASKS_ROOT` (only when it exists) > `$CLAUDE_PROJECT_DIR`
joined with each of `ORCH_CANDIDATES` > a walk up from the payload `cwd` to the
first `.claude/` marker, then the same candidate pair. The resolver is
*duplicated* here rather than imported: `orch_scope_guard.py` calls `main()` at
import time, so importing it from a second hook would run the guard. Two
sibling copies of forty lines beats one import with a side effect.

`ACTIVE` picks exactly ONE root (first bearing the sentinel), for the reason the
guard spells out at length: unioning two roots would let a stale legacy sentinel
widen a live run's scope. `HALT` is not read here at all — it is a *deny* brake
and this hook denies nothing, so honouring it would only mean skipping the
append, which is what a halted run wants least (the last events before a freeze
are the ones an operator is reading).

Registered in the plugin's own `hooks/hooks.json`, beside the guard — one file,
GD-U5's one-registration rule intact. Off switch: `TOUCH_AGENT_LIFECYCLE=0`
(or the plugin's `agent_lifecycle` userConfig, should one ever be declared).
Stdlib only. Never writes under `~/.claude/`: every write target is a task
folder under the resolved tasks root, and containment is checked with
`realpath` before anything is opened.
"""
import json
import os
import re
import sys

#: Falsy spellings for the off switches, byte-identical to the guard's set so
#: one habit works on both hooks.
OFF_VALUES = {"0", "false", "no", "off", "disable", "disabled"}

#: The tasks-root spellings, in order — `.touch/` first, the pre-G10 `.claude/`
#: second. Same tuple, same order, same reason as the guard's: during the
#: transition whichever root actually bears the sentinel decides, so the order
#: of a code edit and the physical move is irrelevant.
ORCH_CANDIDATES = ((".touch", "local-orchestrators"),
                   (".claude", "local-orchestrators"))

#: The run sentinel. Unlike the guard, this hook reads ONLY `ACTIVE`: it never
#: denies, so `HALT` has nothing to say to it.
ACTIVE = "ACTIVE"

#: `[monitor] plan=… [stage=…] role=… attempt=…` — the deterministic marker every
#: template prompt carries, FENCED by GD-D1a (never trimmed, renamed or
#: deleted). This hook only READS it; the fence lives in the templates.
MONITOR_TAG = "[monitor]"
#: `[touch] name=… parent=… root=… role=… attempt=…` — the orchestrate skill's
#: identity marker, and job (c)'s only source of a ledger line's address.
TOUCH_TAG = "[touch]"
#: `key=value` on a marker line. Values run to whitespace; the marker itself is
#: written by our own templates, so nothing fancier is needed or wanted.
LABEL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")

#: Stages a hook may never address. `monitor.html` lets exactly these two set a
#: CARD badge (`stage === "plan"`) or settle the whole run
#: (`stage === "complete"`); everything else only paints a stage chip. An
#: additive channel must not be able to settle anything (GD-D5, R-58).
RESERVED_STAGES = ("plan", "complete")

#: How long `SubagentStart` may wait for the agent transcript to appear, in
#: milliseconds, spread over `MARKER_POLLS` sleeps. Hooks are STRICTLY BLOCKING
#: — this budget is a delay on every agent start of an active run — so it is
#: small, and running out of it means "emit nothing", never "guess a card".
#: `SubagentStop` needs none of this: the payload hands over
#: `agent_transcript_path` and the file is written by then.
MARKER_WAIT_MS = 150
MARKER_POLLS = 6

#: Bytes of an agent transcript read while hunting the marker. The prompt is the
#: first record; a 256 KB ceiling covers a torn first line with room to spare and
#: bounds the blocking cost at a bad one.
PROMPT_SCAN_BYTES = 256 * 1024

#: The `orch-config.json` keys job (b) publishes, mapped from the launch record
#: (`tool_response`). Deliberately the four the driver used to retype and no
#: more: `summary` is prose that belongs to the run card, and `scriptPath` is
#: already implied by the task folder this is being written into.
LAUNCH_KEYS = (("wf_dir", "transcriptDir"),
               ("run_id", "runId"),
               ("task_id", "taskId"),
               ("workflow_name", "workflowName"))

#: Where a task keeps its spawn ledger (`custom_state.STATE_DIR` /
#: `custom_state.LEDGER_FILE`, restated rather than imported — a hook must not
#: pull the aggregator package onto a blocking path).
LEDGER_REL = ("state", "spawn-ledger.jsonl")

#: Job (d): the subdirectories of a task folder a published deliverable may live
#: in. CLAUDE.md's rule names `report/` for HTML and `findings/` for `.md`
#: notes; the hook does not police WHICH, only that it is one of them.
DELIVERABLE_DIRS = ("report", "findings")

#: `detail` is capped at 1 KB by `status.sh` and travels through a bash argument
#: and a JS template literal before it is ever JSON (GD-11), so this writer keeps
#: its own strings short, single-line and free of double quotes.
DETAIL_MAX = 160


def disabled():
    """True when either off switch says so. Checked first, before stdin."""
    for name in ("TOUCH_AGENT_LIFECYCLE",
                 "CLAUDE_PLUGIN_OPTION_AGENT_LIFECYCLE"):
        value = os.environ.get(name, "").strip().lower()
        if value and value in OFF_VALUES:
            return True
    return False


def plugin_root():
    """The payload root — `$CLAUDE_PLUGIN_ROOT` when the harness set it, else
    this file's grandparent. `__file__` is preferred as the fallback rather than
    the other way round only because the variable is the harness's own answer
    for an installed copy; both resolve to the same directory in the dev loop.
    """
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root and os.path.isdir(root):
        return os.path.abspath(root)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def status_writer():
    """`shared/monitoring/status.sh` — the ONE write path into `events.jsonl`
    (GD-D5). Returns None when the payload is incomplete, and then job (a)
    simply does not run: a hook that started writing raw JSON lines because it
    could not find the writer is MONITORING-5's cautionary example with a new
    author."""
    path = os.path.join(plugin_root(), "shared", "monitoring", "status.sh")
    return path if os.path.isfile(path) else None


# --------------------------------------------------------------------------
# tasks-root resolution — the guard's three rungs, duplicated deliberately
# --------------------------------------------------------------------------

def candidate_roots(anchor):
    """Every tasks-root spelling under `anchor`, in `ORCH_CANDIDATES` order."""
    return [os.path.join(anchor, *rel) for rel in ORCH_CANDIDATES]


def anchor_roots(start):
    """Tiers 1-3 as a list of candidate tasks roots, in preference order.

    Tier 1 (`$ORCH_TASKS_ROOT`) is honoured only when it names an existing
    directory, so a stale export falls through instead of disarming the hook —
    and it admits no candidate pair, because it names the root outright.
    """
    override = os.environ.get("ORCH_TASKS_ROOT")
    if override and os.path.isdir(override):
        return [os.path.abspath(override)]
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if project:
        return candidate_roots(os.path.abspath(project))
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isdir(os.path.join(d, ".claude")):
            return candidate_roots(d)
        parent = os.path.dirname(d)
        if parent == d:
            return []
        d = parent


def live_root(start):
    """The ONE tasks root bearing `ACTIVE`, or None — the whole inert check.

    Choose-one, never a union: a stale sentinel at the legacy spelling must be
    able to over-restrict, never to widen (the guard's `pick_root` argument,
    which applies unchanged to *where a hook writes*).
    """
    for base in anchor_roots(start):
        if os.path.isfile(os.path.join(base, ACTIVE)):
            return base
    return None


def active_tasks(root):
    """The active task names, one per line, blanks dropped. An `ACTIVE` file
    that exists but is EMPTY still means a run is live (a half-written sentinel
    is a live run) — it just names no task, which the callers handle."""
    try:
        with open(os.path.join(root, ACTIVE), encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]
    except OSError:
        return []


def contained(root, path):
    """`path` resolved, if it is inside `root`; else None. Every write target
    goes through this, which is what makes the "never writes under `~/.claude/`"
    promise structural rather than a rule to remember — the only roots that
    resolve are the ones `live_root` found."""
    try:
        real_root = os.path.realpath(root)
        real = os.path.realpath(path)
    except OSError:
        return None
    if real == real_root or real.startswith(real_root + os.sep):
        return real
    return None


# --------------------------------------------------------------------------
# markers
# --------------------------------------------------------------------------

def parse_labels(text, tag):
    """`{key: value}` from the first line carrying `tag`, or `{}`.

    Both markers may share a line (`[touch] name=… [monitor] plan=…`), which the
    watcher's own parser tolerates; this one is deliberately looser still — it
    reads every `key=value` on the line and lets the caller take what it knows.
    """
    if not text:
        return {}
    for line in text.splitlines():
        if tag in line:
            return {m.group(1): m.group(2) for m in LABEL_RE.finditer(line)}
    return {}


def attempt_of(labels):
    """`attempt` as an int, or None. Never defaulted: an invented attempt
    addresses a real, stale slot rather than failing loudly (GD-9/R-53)."""
    raw = labels.get("attempt")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def read_prompt(path):
    """The spawn prompt out of an agent transcript, or ''.

    The prompt is the first `user` record. Torn tails are the norm on this
    substrate, so a line that will not parse is skipped rather than fatal, and
    the scan is bounded by `PROMPT_SCAN_BYTES`.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            blob = fh.read(PROMPT_SCAN_BYTES)
    except OSError:
        return ""
    for line in blob.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        message = rec.get("message")
        if rec.get("type") != "user" or not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            if any(parts):
                return "\n".join(parts)
    return ""


def transcript_candidates(hook, state_dir):
    """Where this agent's transcript could be, best guess first.

    `SubagentStop` hands over `agent_transcript_path` outright. `SubagentStart`
    hands over nothing, so the two recorded layouts are rebuilt from
    `transcript_path` (the session file, whose stem is the session DIRECTORY)
    and from the `wf_dir` job (b) published milliseconds earlier — which is the
    whole reason (b) runs before (a) needs it.
    """
    out = []
    given = hook.get("agent_transcript_path")
    if isinstance(given, str) and given:
        out.append(given)
    agent_id = hook.get("agent_id") or ""
    session = hook.get("transcript_path") or ""
    if agent_id and isinstance(session, str) and session.endswith(".jsonl"):
        session_dir = session[: -len(".jsonl")]
        out.append(os.path.join(session_dir, "subagents",
                                f"agent-{agent_id}.jsonl"))
    wf_dir = read_config(state_dir).get("wf_dir") if state_dir else None
    if agent_id and isinstance(wf_dir, str) and wf_dir:
        out.append(os.path.join(wf_dir, f"agent-{agent_id}.jsonl"))
    return out


def await_prompt(hook, state_dir, wait_ms):
    """The spawn prompt, polling the candidates for at most `wait_ms`.

    The budget exists because `SubagentStart` fires ~3 ms after launch and the
    transcript may not be on disk yet (D-17). It is small on purpose: a blocking
    hook is a tax on every agent start, and the honest outcome of running out is
    an empty string — the watcher derives the same card from the journal a beat
    later, which is what "additive, never the floor" means in practice.
    """
    import time
    deadline_polls = max(1, MARKER_POLLS if wait_ms else 1)
    nap = (wait_ms / 1000.0) / deadline_polls if wait_ms else 0.0
    for attempt in range(deadline_polls):
        for path in transcript_candidates(hook, state_dir):
            text = read_prompt(path)
            if text:
                return text
        if nap and attempt + 1 < deadline_polls:
            time.sleep(nap)
    return ""


# --------------------------------------------------------------------------
# writers
# --------------------------------------------------------------------------

def clip(text):
    """A `detail` string this pipeline survives: one line, no double quotes,
    bounded well under `status.sh`'s 1 KB cap (GD-11)."""
    flat = " ".join(str(text).replace('"', "'").split())
    return flat[:DETAIL_MAX]


def emit(state_dir, plan, stage, state, detail):
    """One event, through `status.sh` and nothing else (GD-D5).

    Returns True when the writer was invoked. Every failure mode — no writer,
    no bash, a non-zero exit, a timeout — is swallowed: a monitoring call must
    never break an agent, and this one is not even the floor.
    """
    writer = status_writer()
    if writer is None:
        return False
    import subprocess
    env = dict(os.environ)
    env["ORCH_STATE_DIR"] = state_dir
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        subprocess.run(["bash", writer, plan, stage, state, clip(detail)],
                       env=env, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return False
    return True


def read_config(state_dir):
    """`orch-config.json` as a dict, `{}` when absent or unreadable."""
    if not state_dir:
        return {}
    try:
        with open(os.path.join(state_dir, "orch-config.json"),
                  encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def merge_config(state_dir, updates):
    """Merge `updates` into `<state_dir>/orch-config.json`, atomically.

    MERGE, never replace: the file also carries the driver's own `port`,
    `max_plan_attempts`, `strategy` and friends, and a run whose caps vanished
    because a hook rewrote the file would be a spectacular way to prove the
    additive rule was violated. Written tmp + `os.replace` so a reader never
    sees a half file.
    """
    doc = read_config(state_dir)
    changed = {k: v for k, v in updates.items() if doc.get(k) != v}
    if not changed:
        return False
    doc.update(changed)
    target = os.path.join(state_dir, "orch-config.json")
    tmp = target + f".hook-{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def session_key():
    """`<pid>-<procStart>` for the `claude` process that spawned this hook, or
    None off Linux / on any surprise.

    D-17 measured `/proc/<getppid()>/comm == "claude"` in both profiles, which
    is what lets a hook-written ledger line carry a STATED `sessionKey`
    (`custom_state.SESSION_KEY_SOURCE_RANK`: `ledger` outranks the `path`
    derivation, and rightly — this is read out of the process table, not guessed
    from a directory name). `procStart` is `/proc/<pid>/stat` field 22 as a
    string, and the field is taken after the LAST `)` because a process name can
    contain spaces and parentheses.
    """
    pid = os.getppid()
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            stat = fh.read()
        fields = stat[stat.rindex(")") + 1:].split()
        start = fields[19]
    except (OSError, ValueError, IndexError):
        return None
    if not pid or not start.isdigit():
        return None
    return f"{pid}-{start}"


def append_ledger(state_dir, record):
    """Append one JSON line to `<state_dir>/state/spawn-ledger.jsonl`, flock'd.

    The ledger is append-only and its `seq` is POSITIONAL (`ledger:<scope>` line
    numbers, `custom_state`), so the lock is not politeness: two agents starting
    in the same millisecond writing interleaved partial lines would corrupt an
    addressing scheme, not just a file.
    """
    directory = os.path.join(state_dir, LEDGER_REL[0])
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return False
    path = os.path.join(directory, LEDGER_REL[1])
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        import fcntl
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except (OSError, ImportError):
        return False
    return True


def advise(event, message):
    """The one thing this hook ever says to the model: a warning, on stdout, in
    the documented `additionalContext` envelope. Never a `permissionDecision`,
    never a non-zero exit — job (d) is advisory by decision (SUBSTRATE-12) and
    by evidence (D-17 never exercised the `Artifact` matcher)."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event,
        "additionalContext": message,
    }}))


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

def task_dir_for(root, names, name):
    """`<root>/<name>`, contained and existing, when `name` is one of `names`."""
    if name not in names:
        return None
    path = contained(root, os.path.join(root, name))
    return path if path and os.path.isdir(path) else None


def task_from_script(root, script_path):
    """The task folder a launch record's `scriptPath` names, or None.

    `…/<task>/orch-scripts/<file>.workflow.js` — the layout every driver copy
    uses (SUBSTRATE-5 derives it two ways; this is the exact one). The result is
    accepted only when it is CONTAINED in the resolved tasks root, which is what
    stops a launch from some other project's checkout writing here.

    Membership in `ACTIVE` is deliberately NOT required: the merge writes into
    the task's own config and cannot leak across tasks, and a driver may
    legitimately launch in the window before it appends its ACTIVE line — the
    very window the racy newest-`wf_*` fallback exists to cover. Containment
    plus "the directory exists" is the whole safety argument.
    """
    if not isinstance(script_path, str) or not script_path:
        return None
    scripts = os.path.dirname(os.path.abspath(script_path))
    if os.path.basename(scripts) != "orch-scripts":
        return None
    real = contained(root, os.path.dirname(scripts))
    if real is None or not os.path.isdir(real):
        return None
    return real


def state_dir_for_agent(root, names, hook):
    """The task folder an agent lifecycle event belongs to, or None.

    One active task is the common case and needs nothing. With several live at
    once the only honest join is the run: `agent_transcript_path` (Stop only)
    names `…/workflows/<runId>/agent-<id>.jsonl`, and each candidate task's
    `orch-config.json` carries the `wf_dir` job (b) published. No match, or no
    transcript path at all (Start), means **no event** — an event on the wrong
    task's stream is worse than a missing one the watcher supplies anyway.
    """
    dirs = [d for d in (task_dir_for(root, names, n) for n in names) if d]
    if len(dirs) == 1:
        return dirs[0]
    if not dirs:
        return None
    given = hook.get("agent_transcript_path")
    if not isinstance(given, str) or not given:
        return None
    wf_dir = os.path.dirname(os.path.abspath(given))
    for directory in dirs:
        recorded = read_config(directory).get("wf_dir")
        if isinstance(recorded, str) and recorded and \
                os.path.abspath(recorded) == wf_dir:
            return directory
    return None


def on_subagent(hook, root, names, event):
    """(a) the lifecycle line, and (c) the spawn-ledger line."""
    agent_id = hook.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        return
    state_dir = state_dir_for_agent(root, names, hook)
    if state_dir is None:
        return
    start = event == "SubagentStart"
    prompt = await_prompt(hook, state_dir, MARKER_WAIT_MS if start else 0)
    if not prompt:
        return

    monitor = parse_labels(prompt, MONITOR_TAG)
    plan = monitor.get("plan")
    role = monitor.get("role") or ""
    stage = monitor.get("stage") or role
    if plan and stage and stage not in RESERVED_STAGES:
        attempt = attempt_of(monitor)
        who = f"{role} #{attempt}" if role and attempt is not None else agent_id[:8]
        if start:
            emit(state_dir, plan, stage, "running",
                 f"{who} started (hook {agent_id[:8]})")
        else:
            # `info`, never `done`/`failed`: this observes a STOP. The verdict is
            # the journal `result` the watcher reads, and `last_assistant_message`
            # sitting in the payload is a message, not a verdict (R-58).
            emit(state_dir, plan, stage, "info",
                 f"{who} returned (hook {agent_id[:8]}) - verdict from journal")

    if not start:
        return
    labels = parse_labels(prompt, TOUCH_TAG)
    attempt = attempt_of(labels)
    key = session_key()
    if not (labels.get("name") and labels.get("root") and
            attempt is not None and key):
        return  # unaddressable: skipped, never guessed (CUSTOMSTATE-10)
    record = {"w": "hook", "agentId": agent_id,
              "name": labels["name"], "root": labels["root"],
              "attempt": attempt, "sessionKey": key}
    for field in ("parent", "role"):
        if labels.get(field):
            record[field] = labels[field]
    # The run-level handle is NOT the agent's: a Workflow agent has no per-agent
    # stop (inception §4's two granularities), so `taskId` is recorded only for
    # the Agent-tool profile, where the task id IS the 17-hex agentId.
    if hook.get("agent_type") != "workflow-subagent":
        record["taskId"] = agent_id
    import datetime
    record["ts"] = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    append_ledger(state_dir, record)


def on_workflow(hook, root):
    """(b) publish the run→task join into `orch-config.json`."""
    response = hook.get("tool_response")
    if not isinstance(response, dict):
        return
    if response.get("taskType") != "local_workflow":
        return
    state_dir = task_from_script(root, response.get("scriptPath"))
    if state_dir is None:
        return
    updates = {}
    for key, source in LAUNCH_KEYS:
        value = response.get(source)
        if isinstance(value, str) and value:
            updates[key] = value
    if updates:
        merge_config(state_dir, updates)


def on_artifact(hook, root, names):
    """(d) advisory provenance warning — warn, never deny."""
    response = hook.get("tool_response")
    path = None
    if isinstance(response, dict):
        path = response.get("path")
    if not isinstance(path, str) or not path:
        tool_input = hook.get("tool_input")
        if isinstance(tool_input, dict):
            path = tool_input.get("file_path")
    if not isinstance(path, str) or not path:
        return
    real = contained(root, path)
    if real is not None:
        rest = os.path.relpath(real, os.path.realpath(root)).split(os.sep)
        if len(rest) > 2 and rest[0] in names and rest[1] in DELIVERABLE_DIRS:
            return
    where = " or ".join(f"<task>/{d}/" for d in DELIVERABLE_DIRS)
    advise("PostToolUse",
           f"Touch provenance: the published path {path} is not under an "
           f"active task's {where}. The claude.ai artifact store is a share "
           f"mirror, never the storage: copy the file under one of the active "
           f"tasks ({', '.join(names) or 'none listed'}) so it survives. "
           f"Advisory only - nothing was blocked.")


# --------------------------------------------------------------------------

def main():
    if disabled():
        return
    try:
        hook = json.load(sys.stdin)
    except Exception:
        return  # a broken payload must never delay, or break, an agent
    if not isinstance(hook, dict):
        return
    event = hook.get("hook_event_name") or ""
    tool = hook.get("tool_name") or ""
    if event not in ("SubagentStart", "SubagentStop", "PostToolUse"):
        return
    if event == "PostToolUse" and tool not in ("Workflow", "Artifact"):
        return
    root = live_root(hook.get("cwd"))
    if root is None:
        return  # inert: no run is active here
    names = active_tasks(root)
    if event == "PostToolUse":
        if tool == "Workflow":
            on_workflow(hook, root)
        else:
            on_artifact(hook, root, names)
        return
    on_subagent(hook, root, names, event)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Last line of defence. This hook is additive by design (GD-D5); an
        # unhandled traceback here would put a stack trace where the harness
        # expects a decision, on a path that is blocking every agent start of
        # every active run. Nothing it could report is worth that.
        pass
