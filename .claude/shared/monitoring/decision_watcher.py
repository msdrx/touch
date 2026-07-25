#!/usr/bin/env python3
"""Deterministic orchestrator-decision tracer.

Tails the workflow run's journal.jsonl (every agent() start/result the
orchestrator script executes) and translates it into decision events appended
to events.jsonl, under plan "orchestrator" — spawns, stage verdicts, and the
loop decisions they deterministically imply (retry / advance / complete).
Offset + classification cache persist in .watcher-state.json so restarts
never duplicate events.
"""
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
# Per-task state lives in $ORCH_STATE_DIR; the shared module stays stateless.


def resolve_state_dir() -> str:
    """State dir: $ORCH_STATE_DIR > newest task folder.

    The shared module dir is code-only and never an authoritative state dir
    (D6): a stray ``ROOT/events.jsonl`` must not hijack auto-discovery, so we
    fall straight through to the newest task-folder glob when the env is unset.
    """
    if os.environ.get("ORCH_STATE_DIR"):
        return os.environ["ORCH_STATE_DIR"]
    import glob
    candidates = glob.glob(os.path.join(
        ROOT, "..", "..", "local-orchestrators", "*", "events.jsonl"))
    if candidates:
        return os.path.dirname(max(candidates, key=os.path.getmtime))
    return ROOT


STATE_DIR = os.path.abspath(resolve_state_dir())


def read_config() -> dict:
    for base in (STATE_DIR, ROOT):
        try:
            with open(os.path.join(base, "orch-config.json")) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def resolve_wf_dir() -> str:
    """Workflow transcript dir: argv > $ORCH_WF_DIR > orch-config.json > newest run."""
    if len(sys.argv) > 1:
        return sys.argv[1]
    if os.environ.get("ORCH_WF_DIR"):
        return os.environ["ORCH_WF_DIR"]
    configured = read_config().get("wf_dir")
    if configured and os.path.isdir(configured):
        return configured
    # Auto-discover: most recently active workflow journal under ~/.claude.
    import glob
    candidates = glob.glob(os.path.expanduser(
        "~/.claude/projects/*/*/subagents/workflows/wf_*/journal.jsonl"))
    if not candidates:
        sys.exit("no workflow journal found; pass the run dir as argv[1] "
                 "or set ORCH_WF_DIR / orch-config.json wf_dir")
    return os.path.dirname(max(candidates, key=os.path.getmtime))


WF_DIR = resolve_wf_dir()
JOURNAL = os.path.join(WF_DIR, "journal.jsonl")
WF_NAME = os.path.basename(os.path.normpath(WF_DIR))
# The harness keys the subagent transcript dir to the ACTIVE session id, and
# /clear or /compact rotates that id mid-run while the background workflow
# keeps going — so one run's agent transcripts (even one in-flight agent's
# continued transcript) end up scattered across sibling session dirs, all
# named .../<session-id>/subagents/workflows/<WF_NAME>/. Every per-agent read
# must search all of them, not just the launch-time WF_DIR; the journal alone
# stays at its launch-time absolute path.
WF_GLOB_ROOT = os.environ.get(
    "ORCH_WF_GLOB_ROOT", os.path.expanduser("~/.claude/projects"))


def agent_paths(agent_id: str) -> list[str]:
    """Every transcript copy of an agent across session dirs, oldest first."""
    paths = set(glob.glob(os.path.join(
        WF_GLOB_ROOT, "*", "*", "subagents", "workflows", WF_NAME,
        f"agent-{agent_id}.jsonl")))
    direct = os.path.join(WF_DIR, f"agent-{agent_id}.jsonl")
    if os.path.exists(direct):
        paths.add(direct)

    def mtime(p: str) -> float:
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0
    return sorted(paths, key=mtime)
EVENTS = os.path.join(STATE_DIR, "events.jsonl")
STATE = os.path.join(STATE_DIR, ".watcher-state.json")

# Attempt caps are not baked into the shared watcher (D4): read them from
# orch-config.json (defaults preserve today's behavior when the keys are unset).
_CAPS_CFG = read_config()
MAX_PLAN_ATTEMPTS = int(_CAPS_CFG.get("max_plan_attempts", 4))
MAX_GATE_ATTEMPTS = int(_CAPS_CFG.get("max_gate_attempts", 3))
MAX_E2E_ATTEMPTS = int(_CAPS_CFG.get("max_e2e_attempts", 3))
# Terminal-quiet debounce for watcher-detected run completion (seconds).
# Long enough that the normal spawn-next-agent gap (seconds) never fires it;
# a false fire during an unusual pause self-heals on the next spawn.
QUIET_SECS = int(os.environ.get("ORCH_QUIET_SECS", "60"))

# Generic plug-in protocol: an orchestrator embeds this marker anywhere in an
# agent's prompt and the watcher needs no task-specific patterns:
#   [monitor] plan=<plan-id> [stage=<stage>] role=<role> attempt=<n>
# The marker is script-authored text: the orchestrator script computes it at a
# fixed control-flow point, so every event derived from it is deterministic —
# no LLM cooperation involved.
MARKER = re.compile(r"\[monitor\]\s+plan=(\S+)\s+(?:stage=(\S+)\s+)?role=(\S+)\s+attempt=(\d+)")
# Stage fallback for prompts whose marker omits stage=: the mandated status.sh
# command in the prompt names the stage deterministically too.
STAGE_HINT = re.compile(r"status\.sh\s+\S+\s+(\S+)\s+running")

# Legacy fallback patterns (task-specific prompts without the marker).
ROLE_PATTERNS = [
    (re.compile(r"You are the IMPLEMENTER for sub-plan (sp\d), attempt (\d+)"), "impl"),
    (re.compile(r"You are the TEST RUNNER for sub-plan (sp\d), attempt (\d+)"), "test"),
    (re.compile(r"You are the adversarial CRITIC for sub-plan (sp\d), attempt (\d+)"), "critique"),
    (re.compile(r"You are the GATE runner, attempt (\d+)"), "gate:run"),
    (re.compile(r"You are the regression FIXER, attempt (\d+)"), "gate:fix"),
    (re.compile(r"You are the INSTALL\+E2E runner, attempt (\d+)"), "e2e:run"),
    (re.compile(r"You are the E2E FIXER, attempt (\d+)"), "e2e:fix"),
]


def emit(stage: str, state: str, detail: str, ts: str | None = None,
         plan: str = "orchestrator", extra: dict | None = None) -> None:
    payload = {
        "ts": ts or datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "plan": plan,
        "stage": stage,
        "state": state,
        "detail": detail,
    }
    if extra:
        payload.update(extra)
    line = json.dumps(payload)
    with open(EVENTS, "a") as f:
        f.write(line + "\n")


def agent_tokens(agent_id: str) -> tuple[int, int, int, int]:
    """Sum (input, cache-read, cache-write, output) tokens across an agent's API calls, deduped by message id.

    ``input`` is the TOTAL input volume (fresh + cache writes + cache reads).
    Cache reads/writes are broken out separately because an agent loop
    re-sends its whole conversation prefix every turn — cache reads dominate
    the input sum (and cost ~10x less than fresh input), so displays show
    the r:/w: breakdown to keep the big number interpretable.
    """
    # A /clear- or /compact-split transcript yields several copies; the
    # message-id key unions them safely (overlapping messages collapse).
    usage_by_msg: dict[str, dict] = {}
    for path in agent_paths(agent_id):
        try:
            with open(path) as f:
                for lineno, raw in enumerate(f):
                    try:
                        d = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") != "assistant":
                        continue
                    m = d.get("message") or {}
                    u = m.get("usage")
                    if u:
                        # Fall back to a stable per-row key (path+line) when the
                        # entry carries neither message.id nor uuid, so multiple
                        # id-less usage rows are summed rather than collapsing to
                        # a single "" key (WATCHER-8).
                        key = m.get("id") or d.get("uuid")
                        if not key:
                            key = f"\0noid\0{path}\0{lineno}"
                        usage_by_msg[key] = u
        except OSError:
            continue
    tin = tcached = twrite = tout = 0
    for u in usage_by_msg.values():
        cached = u.get("cache_read_input_tokens") or 0
        write = u.get("cache_creation_input_tokens") or 0
        tin += (u.get("input_tokens") or 0) + write + cached
        tcached += cached
        twrite += write
        tout += u.get("output_tokens") or 0
    return tin, tcached, twrite, tout


def fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def fmt_in(tin: int, tcached: int, twrite: int = 0) -> str:
    """Input-side display, e.g. ``in 3k - r:1k w:2k`` (r = cache read, w = cache write)."""
    parts = ([f"r:{fmt_tokens(tcached)}"] if tcached else []) + \
            ([f"w:{fmt_tokens(twrite)}"] if twrite else [])
    return f"in {fmt_tokens(tin)}" + (" - " + " ".join(parts) if parts else "")


def elapsed_str(t0: str | None, t1: str | None) -> str:
    """Human runtime between two ISO timestamps, e.g. ``"3m41s"``; "" if unknown."""
    if not t0 or not t1:
        return ""
    try:
        a = datetime.fromisoformat(t0.replace("Z", "+00:00"))
        b = datetime.fromisoformat(t1.replace("Z", "+00:00"))
        s = max(0, int((b - a).total_seconds()))
    except ValueError:
        return ""
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def prompt_text(agent_id: str) -> str:
    # Oldest copy first: a rotated continuation may open with harness resume
    # scaffolding rather than the original spawn prompt (and its marker).
    for path in agent_paths(agent_id):
        try:
            with open(path) as f:
                first = f.readline()
            msg = json.loads(first).get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            if content:
                return content
        except (OSError, json.JSONDecodeError):
            continue
    return ""


def first_ts(agent_id: str) -> str | None:
    """True spawn time: earliest first-line timestamp across transcript copies."""
    stamps = []
    for path in agent_paths(agent_id):
        try:
            with open(path) as f:
                ts = json.loads(f.readline()).get("timestamp")
            if ts:
                stamps.append(ts)
        except (OSError, json.JSONDecodeError):
            continue
    return min(stamps) if stamps else None


def _last_ts_in_file(path: str) -> str | None:
    """Latest parseable ``timestamp`` in one transcript file.

    Grows the tail window until at least one full line is captured, so a final
    transcript line larger than the initial window (a >64KB tool result — the
    real case commit 0586bbbf shows) still yields the true last timestamp
    instead of an older one or ``None`` (WATCHER-7).
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    window = 65536
    while True:
        start = max(0, size - window)
        try:
            with open(path, "rb") as f:
                f.seek(start)
                data = f.read()
        except OSError:
            return None
        if start > 0:
            # Drop the leading (probably partial) line we started mid-way into.
            nl = data.find(b"\n")
            data = data[nl + 1:] if nl != -1 else b""
        for line in reversed(data.decode(errors="replace").splitlines()):
            try:
                ts = json.loads(line).get("timestamp")
            except json.JSONDecodeError:
                continue
            if ts:
                return ts
        if start == 0:  # whole file scanned, still nothing parseable
            return None
        window *= 2


def last_ts(agent_id: str) -> str | None:
    """True completion time: latest last-line timestamp across transcript copies."""
    stamps = []
    for path in agent_paths(agent_id):
        ts = _last_ts_in_file(path)
        if ts:
            stamps.append(ts)
    return max(stamps) if stamps else None


def result_ts(agent_id: str, live: bool) -> str | None:
    """Completion timestamp for an agent whose journal ``result`` just landed.

    Journal entries carry no timestamps, and a transcript can stop flushing
    mid-run — a long final Bash call leaves the tool result and everything
    after it unwritten, so the transcript's last line may predate the real
    finish by many minutes. When tailing live (the entry appeared since the
    previous ~1s poll) the read moment IS the completion time; trust the
    transcript timestamp only when it is fresh enough to be the real end.
    On backlog catch-up the transcript is the only signal we have.
    """
    t_tr = last_ts(agent_id)
    if not live:
        return t_tr
    now = datetime.now(timezone.utc)
    if t_tr:
        try:
            parsed = datetime.fromisoformat(t_tr.replace("Z", "+00:00"))
            if (now - parsed).total_seconds() <= 30:
                return t_tr
        except ValueError:
            pass
    return now.isoformat(timespec="milliseconds")


def classify(agent_id: str, retries: int = 3) -> dict | None:
    # Runs inline in the single ~1s poll thread, so the total wait per call is
    # kept small (a few 0.5s retries at most): a transcript that hasn't flushed
    # yet falls through to "pending" (returns None) rather than stalling the
    # loop for seconds while live token ticks and run-completion go unserved
    # (WATCHER-5). The caller re-attempts classification on the later result
    # entry, by which point the transcript is written.
    for _ in range(retries):
        text = prompt_text(agent_id)
        if text:
            # Last occurrence wins: the orchestrator script appends its marker at
            # the end of the prompt, and earlier text may embed quoted findings
            # that leaked a previous agent's marker.
            m = None
            for m in MARKER.finditer(text):
                pass
            if m:
                stage = m.group(2)
                if not stage:
                    sh = None
                    for sh in STAGE_HINT.finditer(text):
                        pass
                    stage = sh.group(1) if sh else None
                role = m.group(3)
                return {"plan": m.group(1), "role": role, "attempt": int(m.group(4)),
                        "stage": stage or role.split(":")[-1]}
            for pattern, role in ROLE_PATTERNS:
                m = pattern.search(text)
                if m:
                    if role in ("impl", "test", "critique"):
                        return {"plan": m.group(1), "role": role, "attempt": int(m.group(2)),
                                "stage": role}
                    plan = role.split(":")[0]
                    return {"plan": plan, "role": role, "attempt": int(m.group(1)),
                            "stage": role.split(":")[-1]}
            return None
        time.sleep(0.5)
    return None


def describe_result(info: dict, result) -> tuple[str, str, str]:
    """Return (stage, state, detail) decision line for a finished agent.

    Shape-driven: keyed on the structured-output fields the orchestrator
    script's schemas force, so the line reflects the actual returned data.
    """
    plan, role, attempt = info["plan"], info["role"], info["attempt"]
    stage = plan
    if result is None:
        return stage, "failed", f"{plan} {role} #{attempt} died or was skipped"
    r = result if isinstance(result, dict) else {}
    if role == "impl" and ("files_changed" in r or "changed_files" in r):
        # Canonical impl key is files_changed (D1); tolerate the legacy alias.
        files = r.get("files_changed", r.get("changed_files")) or []
        return stage, "info", f"{plan} impl #{attempt} returned {len(files)} changed files -> spawn test"
    if role == "test" and "passed" in r:
        if r["passed"]:
            return stage, "done", f"{plan} test #{attempt} PASS -> spawn critique"
        return stage, "failed", f"{plan} test #{attempt} FAIL -> critique will reject; feedback loops"
    if "approved" in r:
        if r["approved"]:
            return stage, "done", f"decision: {plan} approved on attempt {attempt} -> plan complete"
        nxt = (f"retry attempt {attempt + 1}/{MAX_PLAN_ATTEMPTS}"
               if attempt < MAX_PLAN_ATTEMPTS else "attempts exhausted -> plan FAILED")
        return stage, "failed", f"decision: {plan} attempt {attempt} rejected -> {nxt}"
    if "findings" in r:
        return stage, "info", f"{plan} {role} #{attempt}: {len(r['findings'])} findings"
    if "real" in r:
        return stage, "info", f"{plan} verify #{attempt}: real={r['real']}"
    if "fixed_ids" in r:
        return stage, "info", (f"{plan} {role} #{attempt}: fixed {len(r.get('fixed_ids') or [])}, "
                               f"skipped {len(r.get('skipped_ids') or [])}")
    if "passed" in r:
        ok = r["passed"]
        loop = "gate" if ("gate" in role or plan in ("fullsuite", "gate")) else "e2e"
        cap = MAX_GATE_ATTEMPTS if loop == "gate" else MAX_E2E_ATTEMPTS
        # stage stays the plan name: the spawn event opened the chip under
        # `plan`, and a result under any other stage would orphan it as
        # "running" forever; the loop identity lives in the detail text.
        if ok:
            advance = "advance" if loop == "gate" else "workflow COMPLETE"
            return stage, "done", f"decision: {plan} {loop} attempt {attempt} green -> {advance}"
        nxt = (f"spawn fixer, then retry {attempt + 1}/{cap}"
               if attempt < cap else f"{loop} attempts exhausted -> FAILED")
        return stage, "failed", f"decision: {plan} {loop} attempt {attempt} failed -> {nxt}"
    return stage, "info", f"{plan} {role} #{attempt} finished"


def result_stage_state(result) -> tuple[str, str]:
    """(state, detail) for the deterministic per-plan stage chip update."""
    if result is None:
        return "failed", "agent died or skipped"
    r = result if isinstance(result, dict) else {}
    if "findings" in r:
        return "done", f"{len(r['findings'])} findings"
    if "real" in r:
        return "done", f"verdict real={r['real']}"
    if "fixed_ids" in r:
        return "done", f"fixed {len(r.get('fixed_ids') or [])} skipped {len(r.get('skipped_ids') or [])}"
    if "passed" in r:
        if r["passed"]:
            return "done", "green"
        if "failures" in r:
            return "failed", f"{len(r.get('failures') or [])} failures"
        if "checks" in r:
            bad = [c for c in (r.get("checks") or []) if not c.get("ok")]
            return "failed", f"{len(bad)} checks failing"
        return "failed", "failed"
    if "approved" in r:
        return ("done", "approved") if r["approved"] else ("failed", "rejected")
    if "done" in r and ("files_changed" in r or "changed_files" in r):
        # Implementer result (D1): an implementer that returned done:false means
        # the loop retries (loop.workflow.js), so its row must not draw green.
        if r["done"]:
            files = r.get("files_changed", r.get("changed_files")) or []
            return "done", r.get("summary") or f"{len(files)} changed files"
        return "failed", "retrying"
    return "done", "finished"


def run_outcome(state: dict) -> str | None:
    """Terminal run state implied by the journal so far, or None while live.

    "done" when every plan's loop closed green; "failed" when every signal
    left is a rejection (attempts exhausted). The caller still debounces:
    a rejection about to retry, or a pause between loops, looks terminal for
    a moment — and any later spawn reopens the badge, so a premature close
    self-heals (same contract as the per-plan reopen logic below).
    """
    if not state["plans"] or state["running"]:
        return None
    still_open = [p for p, v in state["plans"].items() if v not in ("done", "failed")]
    if still_open and not all(state["decisive"].get(p) is False for p in still_open):
        return None
    ok = (not still_open
          and all(v == "done" for v in state["plans"].values())
          and all(bool(d) for d in state["decisive"].values()))
    return "done" if ok else "failed"


def read_new_lines(path: str, offset: int) -> tuple[list[str], int]:
    """Read whole journal lines appended since ``offset``; defer a torn tail.

    Reads bytes from ``offset`` to EOF, cuts at the last newline, and returns
    only the complete lines plus the advanced offset (exactly past that
    newline). An incomplete trailing line — the reader racing the workflow's
    append (WATCHER-2), possibly truncating a multibyte char (WATCHER-3) — is
    left for the next poll: when ``rfind`` finds no newline, nothing is consumed
    and the offset does not move. Decoding uses ``errors="replace"`` so a torn
    multibyte tail can never crash the process (D5).
    """
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return [], offset
    cut = chunk.rfind(b"\n")
    if cut == -1:
        return [], offset
    text = chunk[:cut + 1].decode(errors="replace")
    return text.splitlines(), offset + cut + 1


def load_state() -> dict:
    """Load the checkpoint, keyed to its journal (D8).

    ``.watcher-state.json`` records the resolved JOURNAL path. If the stored
    journal differs from the current one (auto-discovery or a wf_dir change
    picked a different run), the byte offset is meaningless against the new
    journal — applying it would skip the new run's head or, if the new journal
    is shorter, stall the tailer forever — so reset offset=0 and clear all
    derived run state before tailing.
    """
    fresh = {"offset": 0, "agents": {}, "journal": JOURNAL}
    try:
        with open(STATE) as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return fresh
    if state.get("journal") != JOURNAL:
        return fresh
    return state


def save_state(state: dict) -> None:
    state["journal"] = JOURNAL
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)


def main() -> None:
    state = load_state()
    state.setdefault("agents", {})
    state.setdefault("running", [])
    state.setdefault("tok_emitted", {})
    state.setdefault("plans", {})
    state.setdefault("decisive", {})
    state.setdefault("last_plan", None)
    state.setdefault("run_complete", None)
    for cached in state["agents"].values():  # pre-upgrade cache entries lack "stage"
        cached.setdefault("stage", cached.get("role", "work").split(":")[-1])
    emit("watcher", "info", "decision watcher online (tailing workflow journal)")
    # One-time backfill: token events written before the cache-write split
    # carry no "cache_write", so replayed history under-reports w:. Re-read
    # every already-tracked agent and emit a quiet delta for whatever the
    # emitted totals are missing (normally just the cache-write component).
    for aid, prev in list(state["tok_emitted"].items()):
        tin, tcached, twrite, tout = agent_tokens(aid)
        # Monotonic counters (D7): clamp deltas >= 0; never lower the baseline.
        deltas = {"in": max(0, tin - prev.get("in", 0)),
                  "out": max(0, tout - prev.get("out", 0)),
                  "cached": max(0, tcached - prev.get("cached", 0)),
                  "cache_write": max(0, twrite - prev.get("cache_write", 0))}
        if not any(deltas.values()):
            continue
        info = state["agents"].get(aid)
        plan = info["plan"] if info else "orchestrator"
        label = f"{info['role']} #{info['attempt']}" if info else aid[:8]
        new_base = {"in": max(prev.get("in", 0), tin), "out": max(prev.get("out", 0), tout),
                    "cached": max(prev.get("cached", 0), tcached),
                    "cache_write": max(prev.get("cache_write", 0), twrite)}
        emit("tokens", "info", f"{label} token backfill", plan=plan,
             extra={"tokens": deltas, "quiet": True,
                    # no "state" key: leave the row's queued/running/done dot as-is
                    "agent": {"id": aid[:8], "label": label,
                              "tokens": {"in": new_base["in"], "out": new_base["out"],
                                         "cached": new_base["cached"],
                                         "cache_write": new_base["cache_write"]}}})
        state["tok_emitted"][aid] = new_base
    save_state(state)
    tick = 0
    quiet_since = None  # wall-clock start of the current terminal-quiet stretch
    # False only until the first poll catches up with the journal: a chunk read
    # after that was appended within the last poll interval (fresh), while the
    # startup chunk may be hours-old backlog whose read time means nothing.
    caught_up = False
    while True:
        try:
            size = os.path.getsize(JOURNAL)
        except OSError:
            time.sleep(1)
            continue
        if size > state["offset"]:
            live = caught_up
            lines, new_offset = read_new_lines(JOURNAL, state["offset"])
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                agent_id = entry.get("agentId", "")
                if entry.get("type") == "started":
                    if state.get("run_complete"):
                        # The badge closed (watcher-detected quiet end or the
                        # driver's own event) yet the run spawned again: reopen.
                        state["run_complete"] = None
                        emit("complete", "running", "run resumed: new agent spawned",
                             ts=first_ts(agent_id))
                    info = classify(agent_id)
                    if agent_id not in state["running"]:
                        state["running"].append(agent_id)
                    if info:
                        state["agents"][agent_id] = info
                        ts0 = first_ts(agent_id)
                        # A sequential loop runs one agent per plan+role at a
                        # time, so a same-role spawn at a GREATER attempt while an
                        # earlier one never returned a result means that agent is
                        # gone (driver killed or restarted mid-flight): close its
                        # row so it doesn't tick "running" forever. Best-effort
                        # end time = the dead agent's last transcript activity.
                        # Guard on attempt strictly increasing (DRIVER-1): a
                        # parallel fan-out spawns many agents at the SAME
                        # plan+role+attempt — those are live siblings, not
                        # retries, and must never stale-close each other.
                        for other in list(state["running"]):
                            oinfo = state["agents"].get(other)
                            if (other == agent_id or not oinfo
                                    or oinfo["plan"] != info["plan"]
                                    or oinfo["role"] != info["role"]
                                    or info["attempt"] <= oinfo["attempt"]):
                                continue
                            state["running"].remove(other)
                            emit(oinfo["stage"], "stale",
                                 f"{oinfo['role']} #{oinfo['attempt']} abandoned — no result, "
                                 f"{info['role']} attempt {info['attempt']} respawned",
                                 ts=ts0, plan=oinfo["plan"],
                                 extra={"agent": {"id": other[:8],
                                                  "label": f"{oinfo['role']} #{oinfo['attempt']}",
                                                  "state": "stale",
                                                  "runtime": elapsed_str(first_ts(other), last_ts(other))}})
                        emit(info["plan"], "running",
                             f"spawn {info['plan']} {info['role']} attempt {info['attempt']}",
                             ts=ts0)
                        # Deterministic per-plan card updates, derived from the
                        # script-authored prompt marker — no LLM cooperation.
                        # The "agent" field opens a live per-subagent row on the
                        # plan card (id keys the row; later events update it).
                        emit(info["stage"], "running",
                             f"{info['role']} attempt {info['attempt']} spawned",
                             ts=ts0, plan=info["plan"],
                             extra={"agent": {"id": agent_id[:8],
                                              "label": f"{info['role']} #{info['attempt']}",
                                              "state": "running", "started": ts0}})
                        prev = state.get("last_plan")
                        if prev and prev != info["plan"] and state["plans"].get(prev) == "running":
                            # sequenced loops: a new plan starting closes the prior
                            # one. Green ONLY on a positive decisive result (D3):
                            # a plan that exhausted attempts without ever producing
                            # a gate verdict has decisive unset (None) and must
                            # close "failed", not "done" (WATCHER-9).
                            st = "done" if state["decisive"].get(prev) else "failed"
                            state["plans"][prev] = st
                            emit("plan", st, f"loop exited -> {info['plan']}", ts=ts0, plan=prev)
                        state["last_plan"] = info["plan"]
                        if info["plan"] not in state["plans"]:
                            state["plans"][info["plan"]] = "running"
                            emit("plan", "running", "first agent spawned", ts=ts0, plan=info["plan"])
                        elif state["plans"][info["plan"]] == "done":
                            # A decisive-green result closed this plan, yet the
                            # loop spawned another agent for it — an intermediate
                            # gate (e.g. test green before e2e/critique) closed it
                            # prematurely. The journal has now proven the loop is
                            # still running: reopen the card. The true close is
                            # whichever green survives with no further spawns.
                            state["plans"][info["plan"]] = "running"
                            emit("plan", "running",
                                 f"loop continues: {info['role']} attempt {info['attempt']} spawned",
                                 ts=ts0, plan=info["plan"])
                    else:
                        emit("watcher", "info", f"spawn unclassified agent {agent_id[:8]}",
                             ts=first_ts(agent_id))
                elif entry.get("type") == "result":
                    if agent_id in state["running"]:
                        state["running"].remove(agent_id)
                    info = state["agents"].get(agent_id) or classify(agent_id)
                    if info:
                        state["agents"][agent_id] = info
                        result = entry.get("result")
                        tsN = result_ts(agent_id, live)
                        stage, st, detail = describe_result(info, result)
                        emit(stage, st, detail, ts=tsN)
                        sst, sdetail = result_stage_state(result)
                        a_tin, a_tcached, a_twrite, a_tout = agent_tokens(agent_id)
                        emit(info["stage"], sst,
                             f"{info['role']} #{info['attempt']}: {sdetail}",
                             ts=tsN, plan=info["plan"],
                             extra={"agent": {"id": agent_id[:8],
                                              "label": f"{info['role']} #{info['attempt']}",
                                              "state": sst,
                                              "tokens": {"in": a_tin, "out": a_tout,
                                                         "cached": a_tcached,
                                                         "cache_write": a_twrite},
                                              "runtime": elapsed_str(first_ts(agent_id), tsN)}})
                        if isinstance(result, dict) and ("passed" in result or "approved" in result):
                            ok = bool(result.get("passed") or result.get("approved"))
                            state["decisive"][info["plan"]] = ok
                            if ok:
                                state["plans"][info["plan"]] = "done"
                                emit("plan", "done",
                                     f"{info['role']} attempt {info['attempt']} green",
                                     ts=tsN, plan=info["plan"])
                            elif state["plans"].get(info["plan"]) == "done":
                                # A negative decisive result must reset a stale
                                # green (D3): a same-attempt test-green cannot
                                # survive a later reject on the same plan.
                                state["plans"][info["plan"]] = "running"
                                emit("plan", "running",
                                     f"{info['role']} attempt {info['attempt']} rejected -> reopened",
                                     ts=tsN, plan=info["plan"])
                        tin, tcached, twrite, tout = agent_tokens(agent_id)
                        prev = state["tok_emitted"].get(agent_id,
                                                        {"in": 0, "out": 0, "cached": 0, "cache_write": 0})
                        # Monotonic token counters (D7): clamp emitted deltas >= 0
                        # and never lower the stored baseline, so a transiently
                        # unreadable/pruned transcript copy can't regress the total.
                        emit("tokens", "info",
                             f"{info['role']} #{info['attempt']} used {fmt_in(tin, tcached, twrite)} · out {fmt_tokens(tout)} total",
                             ts=tsN, plan=info["plan"],
                             extra={"tokens": {"in": max(0, tin - prev.get("in", 0)),
                                               "out": max(0, tout - prev.get("out", 0)),
                                               "cached": max(0, tcached - prev.get("cached", 0)),
                                               "cache_write": max(0, twrite - prev.get("cache_write", 0))}})
                        state["tok_emitted"][agent_id] = {
                            "in": max(prev.get("in", 0), tin),
                            "out": max(prev.get("out", 0), tout),
                            "cached": max(prev.get("cached", 0), tcached),
                            "cache_write": max(prev.get("cache_write", 0), twrite)}
                    else:
                        emit("watcher", "info", f"result from unclassified agent {agent_id[:8]}",
                             ts=last_ts(agent_id))
            # Commit the offset only past the fully-consumed lines: a torn tail
            # (new_offset unchanged) is re-read next poll once it completes (D5).
            state["offset"] = new_offset
            save_state(state)
        caught_up = True
        # Watcher-detected run completion: the driver conversation is supposed
        # to close the Orchestrator badge when the workflow returns, but it can
        # lose that duty mid-run (context cleared/compacted, session killed)
        # while the workflow itself keeps running — so the watcher also closes
        # the badge deterministically once the journal reaches a terminal-quiet
        # state. Debounced by QUIET_SECS; a premature close (pause between
        # loops) is reopened by the next spawn, see the "started" branch.
        outcome = run_outcome(state)
        if outcome and state.get("run_complete") != outcome:
            if quiet_since is None:
                quiet_since = time.time()
            elif time.time() - quiet_since >= QUIET_SECS:
                plans = state["plans"]
                # Settle every still-open plan card by its decisive verdict (D3)
                # before the terminal event, so the last plan can't spin
                # "running" forever or keep a stale green: green ONLY on a
                # positive decisive result, else failed.
                for plan, badge in list(plans.items()):
                    if plan == "orchestrator" or badge in ("done", "failed"):
                        continue
                    st = "done" if state["decisive"].get(plan) else "failed"
                    plans[plan] = st
                    emit("plan", st, f"run {outcome}: settling open plan", plan=plan)
                emit("complete", outcome,
                     f"run {outcome}: {len(plans)} plan(s) "
                     + ("all green" if outcome == "done" else "closed with failures")
                     + f"; loops idle {QUIET_SECS}s+ (watcher-detected end)")
                state["run_complete"] = outcome
                quiet_since = None
                save_state(state)
        else:
            quiet_since = None
        tick += 1
        if state["running"]:  # every poll tick (~1s): live token deltas
            # Live token deltas for in-flight agents (quiet: counters only, no log line).
            dirty = False
            for aid in list(state["running"]):
                tin, tcached, twrite, tout = agent_tokens(aid)
                prev = state["tok_emitted"].get(aid, {"in": 0, "out": 0})
                # Monotonic counters (D7): clamp deltas >= 0; never lower baseline.
                din, dout = max(0, tin - prev.get("in", 0)), max(0, tout - prev.get("out", 0))
                dcached = max(0, tcached - prev.get("cached", 0))
                dwrite = max(0, twrite - prev.get("cache_write", 0))
                if din or dout or dcached or dwrite:
                    info = state["agents"].get(aid)
                    plan = info["plan"] if info else "orchestrator"
                    label = f"{info['role']} #{info['attempt']}" if info else aid[:8]
                    base = {"in": max(prev.get("in", 0), tin), "out": max(prev.get("out", 0), tout),
                            "cached": max(prev.get("cached", 0), tcached),
                            "cache_write": max(prev.get("cache_write", 0), twrite)}
                    emit("tokens", "info",
                         f"{label} running: {fmt_in(base['in'], base['cached'], base['cache_write'])} · out {fmt_tokens(base['out'])} so far",
                         plan=plan,
                         extra={"tokens": {"in": din, "out": dout, "cached": dcached,
                                           "cache_write": dwrite},
                                "quiet": True,
                                "agent": {"id": aid[:8], "label": label,
                                          "state": "running",
                                          "tokens": {"in": base["in"], "out": base["out"],
                                                     "cached": base["cached"],
                                                     "cache_write": base["cache_write"]}}})
                    state["tok_emitted"][aid] = base
                    dirty = True
            if dirty:
                save_state(state)
        time.sleep(1)


if __name__ == "__main__":
    main()
