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
import signal
import sys
import time
from datetime import datetime, timezone

try:  # POSIX only; append locking degrades to unlocked writes without it.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

ROOT = os.path.dirname(os.path.abspath(__file__))
# Per-task state lives in $ORCH_STATE_DIR; the shared module stays stateless.

# Config/env parse warnings queued at import and flushed to stderr right after
# the first heartbeat emit, so a bad value is reported in startup context
# instead of killing the watcher at import (R-07, mirrors SERVER-2).
_CFG_WARNINGS: list[str] = []


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
# Create the state dir up front (R-07): a watcher pointed at a not-yet-created
# task folder must still be able to write its very first event.
try:
    os.makedirs(STATE_DIR, exist_ok=True)
except OSError as _exc:  # pragma: no cover - unwritable parent
    _CFG_WARNINGS.append(f"decision_watcher: cannot create state dir {STATE_DIR}: {_exc}")


def resolve_config() -> tuple[str | None, dict]:
    """THE config resolver: ``(path, values)`` from ONE file, or ``(None, {})``.

    The first orch-config.json that EXISTS wins outright — its values if it
    parses, defaults (plus a deferred warning) if it does not. Path and values
    must come from the same file (m-1): resolving them separately meant a corrupt
    ``STATE_DIR/orch-config.json`` next to a valid ``ROOT`` one made refresh_caps
    watch the mtime of the corrupt file while quoting the other file's numbers,
    so repairing the corrupt file's CONTENT reloaded nothing.

    Not falling through to ROOT on a parse error is deliberate: ROOT is the
    shared module dir (code-only, D6), so a config found there belongs to no task
    in particular, and silently narrating another run's caps is worse than
    narrating the documented defaults. Keeping the corrupt file as the watched
    path is what makes repairing it in place take effect on the next poll.
    """
    for base in (STATE_DIR, ROOT):
        path = os.path.join(base, "orch-config.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                return path, json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            warning = (f"decision_watcher: cannot read {path}: {exc}; "
                       f"using default caps until it is fixed")
            if warning not in _CFG_WARNINGS:
                _CFG_WARNINGS.append(warning)
            return path, {}
    return None, {}


def config_path() -> str | None:
    """The orch-config.json the watcher actually reads, or None if there is none.

    Exposed so the poll loop can watch its mtime and re-read caps WHILE running
    (the orchestrator script publishes them from inside the run, i.e. after the
    daemons started — see refresh_caps).
    """
    return resolve_config()[0]


def read_config() -> dict:
    return resolve_config()[1]


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

def _int_cfg(cfg: dict, key: str, default: int) -> int:
    """Config int with a default and a DEFERRED stderr warning (R-07).

    A non-integer value in ``orch-config.json`` must never kill the watcher at
    import (it is a best-effort observer of someone else's run): the warning is
    queued and flushed after the first heartbeat emit.
    """
    if key not in cfg:
        return default
    try:
        return int(cfg[key])
    except (TypeError, ValueError):
        _CFG_WARNINGS.append(
            f"decision_watcher: invalid {key}={cfg[key]!r} in orch-config.json; "
            f"using default {default}")
        return default


def _int_env(name: str, default: int) -> int:
    """Env int with a default and the same deferred-warning contract (R-07)."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        _CFG_WARNINGS.append(
            f"decision_watcher: invalid {name}={raw!r}; using default {default}")
        return default


# Attempt caps are not baked into the shared watcher (D4): read them from
# orch-config.json (defaults preserve today's behavior when the keys are unset).
CAP_DEFAULTS = {"max_plan_attempts": 4, "max_gate_attempts": 3,
                "max_e2e_attempts": 3, "max_finalgate_attempts": 2}
MAX_PLAN_ATTEMPTS = CAP_DEFAULTS["max_plan_attempts"]
MAX_GATE_ATTEMPTS = CAP_DEFAULTS["max_gate_attempts"]
MAX_E2E_ATTEMPTS = CAP_DEFAULTS["max_e2e_attempts"]
MAX_FINALGATE_ATTEMPTS = CAP_DEFAULTS["max_finalgate_attempts"]
# GD-10: the sequenced "a new plan starting closes the previous one" heuristic is
# RETIRED for new runs — it is the source of the fabricated `plan failed
# "loop exited -> ..."` badge on every research fan-out (R-58). It survives only
# for legacy runs that declare themselves serial in orch-config.json. New runs
# close plans with the templates' terminal `plan done` events plus the settle
# pass below, both of which use close_state_for(). NOTE: the config key is NOT
# what fixes R-58 — close_state_for() is; `strategy` only decides whether the
# retired heuristic runs at all.
STRATEGY = ""


def apply_caps(cfg: dict) -> tuple:
    """Set the cap/strategy globals from a config dict; return their new tuple.

    The narration functions (``describe_result``) read these globals at CALL
    time, so re-applying them mid-run changes the next decision line — which is
    the whole point of refresh_caps() below.
    """
    global MAX_PLAN_ATTEMPTS, MAX_GATE_ATTEMPTS, MAX_E2E_ATTEMPTS
    global MAX_FINALGATE_ATTEMPTS, STRATEGY
    MAX_PLAN_ATTEMPTS = _int_cfg(cfg, "max_plan_attempts", CAP_DEFAULTS["max_plan_attempts"])
    MAX_GATE_ATTEMPTS = _int_cfg(cfg, "max_gate_attempts", CAP_DEFAULTS["max_gate_attempts"])
    MAX_E2E_ATTEMPTS = _int_cfg(cfg, "max_e2e_attempts", CAP_DEFAULTS["max_e2e_attempts"])
    MAX_FINALGATE_ATTEMPTS = _int_cfg(cfg, "max_finalgate_attempts",
                                      CAP_DEFAULTS["max_finalgate_attempts"])
    STRATEGY = str(cfg.get("strategy") or "").strip().lower()
    return (MAX_PLAN_ATTEMPTS, MAX_GATE_ATTEMPTS, MAX_E2E_ATTEMPTS,
            MAX_FINALGATE_ATTEMPTS, STRATEGY)


def _config_mtime() -> int | None:
    path = config_path()
    if path is None:
        return None
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


_CAPS_CFG = read_config()
apply_caps(_CAPS_CFG)
# Import-time baseline: a config written LATER (the orchestrator script publishes
# it from inside the run) moves this and is picked up by refresh_caps().
_CFG_MTIME: int | None = _config_mtime()


def refresh_caps() -> tuple | None:
    """Re-read orch-config.json when it CHANGES; return the new tuple if values moved.

    Import-time resolution alone is not enough: the documented launch order
    starts the daemons BEFORE the orchestrator script runs, and the script
    publishes `max_plan_attempts` / `max_finalgate_attempts` / `strategy` from
    inside the run (R-09). A watcher that froze its values at import would quote
    its own defaults forever — exactly the "caps baked into the shared watcher"
    defect D4 forbids. Cost is one ``os.stat`` per poll tick; the file is only
    parsed when its mtime moves.
    """
    global _CFG_MTIME
    mtime = _config_mtime()
    if mtime is None or mtime == _CFG_MTIME:
        return None
    _CFG_MTIME = mtime
    before = (MAX_PLAN_ATTEMPTS, MAX_GATE_ATTEMPTS, MAX_E2E_ATTEMPTS,
              MAX_FINALGATE_ATTEMPTS, STRATEGY)
    seen = len(_CFG_WARNINGS)
    after = apply_caps(read_config())
    # A bad value in a RELOAD cannot use the import-time deferred queue (nothing
    # flushes it again), so report it immediately and keep going (R-07).
    for warning in _CFG_WARNINGS[seen:]:
        print(warning, file=sys.stderr, flush=True)
    # Already reported: drop them instead of growing the queue for the life of a
    # long-running daemon whose config keeps being rewritten.
    del _CFG_WARNINGS[seen:]
    return None if after == before else after


# Terminal-quiet debounce for watcher-detected run completion (seconds).
# Long enough that the normal spawn-next-agent gap (seconds) never fires it;
# a false fire during an unusual pause self-heals on the next spawn.
QUIET_SECS = _int_env("ORCH_QUIET_SECS", 60)
# R-40 run-close protocol: how long the journal must stay quiet AFTER a terminal
# `complete` event before the watcher exits on its own. Longer than QUIET_SECS so
# a run that resumes right after a premature close keeps its watcher.
EXIT_QUIET_SECS = _int_env("ORCH_EXIT_QUIET_SECS", 120)
# ABANDONED-run window: how long the journal must stay quiet before the watcher
# stops on its OWN inference, with no driver-written close in the stream. An
# order of magnitude above EXIT_QUIET_SECS on purpose — the inferred close is a
# guess (a harness stall or an approval prompt between agents looks identical to
# a finished run), and exiting on a wrong guess loses the live view irreversibly,
# where a wrong BADGE self-heals on the next spawn. It exists only so a killed
# session (agents die with no journal `result`) cannot orphan its watcher
# forever, which is the case CONVO-14 recorded.
ABANDON_QUIET_SECS = _int_env("ORCH_ABANDON_QUIET_SECS", 10 * EXIT_QUIET_SECS)
# Escape hatch: a watcher started to babysit a long run can be told never to
# stop itself (any non-empty value but "0"/"false"/"no").
NO_SELF_EXIT = str(os.environ.get("ORCH_NO_SELF_EXIT", "")).strip().lower() \
    not in ("", "0", "false", "no")
# R-40 shutdown DRAIN (M-2). Both templates' `closeRun` appends the terminal
# `orchestrator complete` event and then SIGTERMs the recorded watcher pid ~0.1
# -0.3 s later, while this loop sleeps up to a full poll interval. Dying inside
# that sleep permanently loses the LAST agent's journal `result` — its stage
# chip stays `running` forever on replay, its decision line is never written, and
# because token deltas are wire-only its ENTIRE usage (the synthesizer / final
# gate: usually the run's largest consumer) never enters the totals.
# events.jsonl is the durable record that replays on connect, so none of that
# self-heals. A stop signal therefore does not exit: it ARMS a drain — at least
# one more tail+emit pass, then polling until DRAIN_SECS have passed, then a
# checkpoint and a clean return. A SECOND signal exits at once, so an operator is
# never held by the drain.
DRAIN_SECS = _int_env("ORCH_DRAIN_SECS", 3)
_STOP_SIGNALS: list[int] = []


def _record_stop(signum, _frame) -> None:
    """Signal handler: appends only — no I/O, nothing to re-enter."""
    _STOP_SIGNALS.append(signum)


def stop_requested() -> bool:
    return bool(_STOP_SIGNALS)


def install_stop_handlers(handler=_record_stop) -> None:
    """Arm the drain on SIGTERM/SIGINT (best-effort, like every other write here).

    A platform without the signal, or a caller running this off the main thread,
    simply keeps the default disposition — the watcher then dies on the signal
    exactly as it did before, which is no worse than not installing anything.
    """
    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:  # pragma: no cover - non-POSIX
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # pragma: no cover - not the main thread
            pass


def poll_sleep(seconds: float = 1.0, step: float = 0.1) -> None:
    """Sleep in slices so a stop signal is acted on within ``step``.

    ``time.sleep`` is RESTARTED after a handler returns (PEP 475), so a plain
    one-second sleep would swallow most of the drain's head start.
    """
    end = time.time() + seconds
    while not stop_requested():
        remaining = end - time.time()
        if remaining <= 0:
            return
        time.sleep(min(step, remaining))


# GD-11: writer-side detail cap. The reason is shell/JS-template embedding of
# these strings downstream, not JSON — a 1 KB cut keeps every consumer safe.
DETAIL_CAP = 1024
# Reserved plan id for implement-plan's final aggregate sweep: its decision text
# is keyed on (plan, role) because no critique follows the sweep (R-08).
FINALGATE_PLAN = "finalgate"

# Generic plug-in protocol: an orchestrator embeds this marker in an agent's
# prompt and the watcher needs no task-specific patterns:
#   [monitor] plan=<plan-id> [stage=<stage>] role=<role> attempt=<n>
# The marker is script-authored text: the orchestrator script computes it at a
# fixed control-flow point, so every event derived from it is deterministic —
# no LLM cooperation involved.
#
# GD-9 marker grammar (one grammar, stated once):
#   * matched PER PHYSICAL LINE, only inside the first MARKER_WINDOW_LINES lines
#     of the prompt (a leading blank line is tolerated — real prompts open with
#     "\n"). A marker outside that window is quoted prose (12 false-positive
#     files exist on disk today) and is NEVER used.
#   * fields are order-independent `key=value` pairs; unknown keys are ignored,
#     so `model=`, `phase=`, `ledger=` can be added compatibly.
#   * `[monitor]`: last occurrence within the window wins. `[touch]`: must be
#     inside the window too, else the node is flagged `marker-misplaced` — but
#     only a REAL marker (token + `key=value` payload) below the window counts;
#     prose that merely quotes the token is not a misplaced marker.
#   * two markers on ONE line both parse (`[touch] … [monitor] …`); each one's
#     fields stop at its own line end.
MARKER_WINDOW_LINES = 4
# Split on the marker TOKEN instead of matching it to end-of-line, so two
# markers on ONE line (`[touch] name=a [monitor] plan=…`) both parse; each
# payload is still cut at its own line end, so prose under a marker can never
# leak stray `key=value` pairs into its fields.
MARKER_SPLIT = re.compile(r"\[(monitor|touch)\]")
MARKER_KV = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)=(\S+)")
TOUCH_FIELDS = ("name", "parent", "root", "ledger")
# Stage fallback for prompts whose marker omits stage=: the mandated status.sh
# command in the prompt names the stage deterministically too. Quoting-tolerant
# (R-13): the templates interpolate `bash "${S}" "${plan}" <stage> running`, so
# both the script path and the plan may arrive quoted.
STAGE_HINT = re.compile(r"status\.sh\"?\s+\"?\S+?\"?\s+\"?([\w:-]+)\"?\s+running")

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


def cap_detail(detail: str) -> str:
    """Truncate a detail string to DETAIL_CAP at the writer (GD-11)."""
    if not detail:
        return detail or ""
    if len(detail) <= DETAIL_CAP:
        return detail
    return detail[:DETAIL_CAP - 3] + "..."


def emit(stage: str, state: str, detail: str, ts: str | None = None,
         plan: str = "orchestrator", extra: dict | None = None) -> None:
    """Append one event line. Best-effort by contract: a failed write warns on
    stderr and never kills the watcher (R-07).

    The five-key shape is preserved and ``w`` is purely additive: it records the
    WRITER of the line (R-39) so a reader never has to guess attribution the way
    the historic streams force (GD-28). Appends are flock'd because
    ``events.jsonl`` is a multi-writer file — status.sh appends to it too
    (R-10).
    """
    payload = {
        "ts": ts or datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "plan": plan,
        "stage": stage,
        "state": state,
        "detail": cap_detail(detail),
        "w": "watcher",
    }
    if extra:
        payload.update(extra)
    line = json.dumps(payload)
    try:
        with open(EVENTS, "a") as f:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line + "\n")
                f.flush()
            finally:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        print(f"decision_watcher: cannot append event to {EVENTS}: {exc}",
              file=sys.stderr, flush=True)


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


def _split_window(text: str, lines: int = MARKER_WINDOW_LINES) -> tuple[list[str], list[str]]:
    """(window lines, lines below it), leading blank lines skipped."""
    parts = text.split("\n")
    i = 0
    while i < len(parts) and not parts[i].strip():
        i += 1
    return parts[i:i + lines], parts[i + lines:]


def marker_window(text: str, lines: int = MARKER_WINDOW_LINES) -> str:
    """The first ``lines`` physical lines of a prompt, leading blanks tolerated.

    GD-9's window rule: only markers in here are real. Everything below is
    prompt body, where a quoted finding may carry another agent's marker.
    """
    return "\n".join(_split_window(text, lines)[0])


def marker_records(text: str) -> list[tuple[str, str]]:
    """``(kind, payload)`` for every marker in ``text``, in order (GD-9)."""
    parts = MARKER_SPLIT.split(text)
    return [(parts[i], parts[i + 1].split("\n", 1)[0])
            for i in range(1, len(parts) - 1, 2)]


def parse_markers(text: str) -> tuple[dict | None, dict | None]:
    """``(monitor_fields, touch_fields)`` from the marker window (GD-9).

    Fields are order-independent key=value pairs; unknown keys are kept so a
    caller can pass through (or ignore) additions like ``model=``/``phase=``.
    Last occurrence within the window wins.
    """
    monitor = touch = None
    for kind, rest in marker_records(marker_window(text)):
        fields = dict(MARKER_KV.findall(rest))
        if kind == "monitor":
            monitor = fields
        else:
            touch = fields
    return monitor, touch


def touch_marker_misplaced(text: str) -> bool:
    """Is there a REAL ``[touch]`` marker BELOW the window? (GD-9)

    A prompt that merely mentions the token — a findings file quoted into a
    critique prompt, a discussion of the skill — is prose, not a misplaced
    marker: only a marker carrying a ``key=value`` payload counts, so the
    ``marker-misplaced`` flag stays a real signal.
    """
    for line in _split_window(text)[1]:
        for kind, rest in marker_records(line):
            if kind == "touch" and MARKER_KV.search(rest):
                return True
    return False


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
            monitor, touch = parse_markers(text)
            if monitor and monitor.get("plan") and monitor.get("role"):
                stage = monitor.get("stage")
                if not stage:
                    sh = None
                    for sh in STAGE_HINT.finditer(text):
                        pass
                    stage = sh.group(1) if sh else None
                role = monitor["role"]
                try:
                    attempt = int(monitor.get("attempt", 1))
                except (TypeError, ValueError):
                    attempt = 1
                info = {"plan": monitor["plan"], "role": role, "attempt": attempt,
                        "stage": stage or role.split(":")[-1]}
                # Additive marker keys travel through untouched (GD-9).
                for key in ("model", "phase"):
                    if monitor.get(key):
                        info[key] = monitor[key]
                # Optional Touch identity layer: labels only — a missing or
                # misplaced [touch] marker degrades the label, never the node
                # (GD-7/GD-9).
                if touch:
                    ident = {k: touch[k] for k in TOUCH_FIELDS if k in touch}
                    if ident:
                        info["identity"] = ident
                elif touch_marker_misplaced(text):
                    info["marker_misplaced"] = True
                return info
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


def agent_label(info: dict | None, agent_id: str = "") -> str:
    """Display label for one agent row: ``<stage>:<role> #<attempt>`` (R-13).

    Stage-qualified so PARALLEL siblings on one plan (six researchers all
    `plan=research role=research attempt=1`, distinguished only by stage) get six
    distinct labels instead of collapsing into one row. Unclassified agents fall
    back to the short id — the node still exists (GD-7).
    """
    if not info:
        return agent_id[:8]
    stage = info.get("stage") or info["role"]
    return f"{stage}:{info['role']} #{info['attempt']}"


def agent_block(agent_id: str, info: dict | None, state: str | None = None,
                **fields) -> dict:
    """Per-subagent row payload for an event's ``agent`` sub-object.

    Identity is the FULL 17-hex agentId (GD-7/R-13); the 8-char form travels
    only as ``shortId`` for display, so two agents sharing an 8-hex prefix can
    never collapse into one row.
    """
    block = {"id": agent_id, "shortId": agent_id[:8],
             "label": agent_label(info, agent_id)}
    if state:
        block["state"] = state
    block.update({k: v for k, v in fields.items() if v})
    if info:
        if info.get("identity"):
            block["identity"] = info["identity"]
        if info.get("marker_misplaced"):
            block["flags"] = ["marker-misplaced"]
    else:
        block["unconventional"] = True
    return block


def close_state_for(plan: str, decisive: dict, last_result_ok: dict) -> str:
    """GD-10 plan-close predicate — the ONE rule, used by every close site.

    A decisive verdict (a gate's ``passed`` / a critic's ``approved``) decides.
    Absent one, the plan settles on whether its LAST result was a failure: a
    plan whose agents all resulted without a decisive verdict — every research
    fan-out — closes **done** ("closed, no verdict"), NEVER failed. That
    fabricated `failed` is the defect R-58 exists to kill.
    """
    ok = decisive.get(plan) if plan in decisive else last_result_ok.get(plan, False)
    return "done" if ok else "failed"


def close_detail(plan: str, decisive: dict, base: str) -> str:
    """Close-event detail; a verdict-less close says so verbatim (D13 honesty)."""
    return base if plan in decisive else f"{base} (closed, no verdict)"


# Cache for the events-stream scan below, keyed by (path, size, mtime, offset):
# the check runs on every ~1s poll tick while the loop is idle-but-not-terminal,
# and events.jsonl grows without bound (hundreds of KB on a real task), so an
# unconditional full read per tick would be O(stream)/second (m1).
_TERMINAL_CACHE: dict[tuple, bool] = {}


def stream_terminal_close(events_path: str | None = None,
                          since_offset: int = 0,
                          writer: str | None = None) -> bool:
    """Does the event stream END on a terminal run close? (R-40)

    LAST-EVENT-WINS in file order, restricted to the reserved ``orchestrator``
    plan (monitoring.md's reserved ids): only a final ``complete done|failed``
    counts. What RESETS it is evidence the run is LIVE AGAIN — a later
    ``complete running`` (the reopen event this module emits when a closed run
    spawns again) or a later plan card MOVING, i.e. a `plan` event whose state is
    not itself a close. (A moving card deliberately includes the `plan queued`
    SEED lines the m-orchestrator recipe writes before launch: seeding after a
    close would mean a new run is starting.)

    A terminal `plan done|failed` is NOT a reset (M-1): closing a card is the
    opposite of the run resuming, and the watcher's own settle pass emits exactly
    those — plus its own `complete done` — AFTER the driver's close, because
    QUIET_SECS < EXIT_QUIET_SECS. Treating them as liveness invalidated the
    driver's close in the normal flow, so the authorized self-exit never fired
    and the run fell through to the ABANDONED window (20 min by default) with a
    dishonest "no driver close" detail. A close written by a foreign writer is
    likewise NEUTRAL when ``writer`` is set — it neither authorizes nor cancels.

    ``since_offset`` scopes the scan to bytes appended after the watcher started,
    which is what keeps a STALE close from an EARLIER phase in the same task
    folder (one folder hosts research, then implement-plan) from ever reading as
    this session's ending. ``writer`` additionally requires the closing line's
    ``w`` attribution to match (R-39) — ``"agent"`` means "written by a script or
    an agent through status.sh, not inferred by this watcher".
    """
    path = events_path or EVENTS
    try:
        st = os.stat(path)
    except OSError:
        return False
    key = (path, st.st_size, st.st_mtime_ns, since_offset, writer)
    if key in _TERMINAL_CACHE:
        return _TERMINAL_CACHE[key]
    terminal = False
    try:
        # errors="replace": this is a best-effort observer — a stray byte in a
        # multi-writer stream must never raise out of the liveness loop (D5).
        with open(path, encoding="utf-8", errors="replace") as f:
            if since_offset:
                f.seek(since_offset)
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                stage = ev.get("stage")
                if stage == "complete" and ev.get("plan", "orchestrator") == "orchestrator":
                    if ev.get("state") in ("done", "failed"):
                        if writer is None or ev.get("w") == writer:
                            terminal = True   # a matching close; a foreign one is neutral
                    else:
                        terminal = False      # `complete running` = the run reopened
                elif stage == "plan" and ev.get("state") not in ("done", "failed"):
                    # A plan card MOVING (queued/running) = the run is live again.
                    # A plan card CLOSING is not liveness — see the docstring.
                    terminal = False
    except (OSError, ValueError):
        return False
    _TERMINAL_CACHE.clear()   # single-entry cache: only the newest key is useful
    _TERMINAL_CACHE[key] = terminal
    return terminal


def stream_badge_closed(events_path: str | None = None) -> bool:
    """Does the stream's Orchestrator BADGE currently read done/failed?

    LAST-EVENT-WINS over the reserved ``orchestrator`` plan's badge events —
    stage ``plan`` or ``complete``, exactly the events that set the card badge
    in monitor.html. Unlike :func:`stream_terminal_close`, sub-plan ``plan``
    events do NOT reset the verdict: they move their own cards, never the
    Orchestrator badge.

    Consulted once at startup to arm the continuation heal: one task folder
    hosts several phases (research, then implement-plan) appending to one
    ``events.jsonl``, so the stream can END on an EARLIER phase's
    ``complete done`` — which a replaying dashboard shows as a closed run while
    THIS phase's loops are visibly running. Arming ``run_complete`` makes the
    existing "started" branch heal the stream with ``complete running`` on the
    first spawn (the stream-side half of FRONTEND-6).
    """
    path = events_path or EVENTS
    closed = False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if ev.get("plan", "orchestrator") != "orchestrator":
                    continue
                if ev.get("stage") in ("plan", "complete"):
                    closed = ev.get("state") in ("done", "failed")
    except OSError:
        return False
    return closed


def stream_plan_closes(events_path: str | None = None,
                       since_offset: int = 0) -> dict:
    """Plans the STREAM already closed: ``{plan: "done"|"failed"}`` (m-3).

    Same last-event-wins-in-FILE-order fold the dashboard and
    ``monitor_server.replay_plan_states`` use (SD-4), narrowed to terminal
    ``stage="plan"`` events. The settle pass consults it so it never writes a
    SECOND close for a card the orchestrator script already closed with its
    terminal `plan done` (R-09): the duplicate was contradictory on its face —
    the script-VERIFIED close was followed by one labelled "(closed, no
    verdict)" — and events.jsonl is the durable record, so it misreads forever.

    ``since_offset`` scopes the fold to this session exactly like
    :func:`stream_terminal_close`: one task folder hosts several phases, and
    adopting an EARLIER phase's close for a plan id that is open again now would
    be a fabricated badge of its own. The cost of that scope is one harmless
    duplicate close if a watcher is restarted mid-phase; the alternative cost is
    a wrong verdict, so the trade is not symmetric.
    """
    path = events_path or EVENTS
    closes: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            if since_offset:
                f.seek(since_offset)
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if ev.get("stage") != "plan" or ev.get("state") not in ("done", "failed"):
                    continue
                plan = ev.get("plan")
                if plan and plan != "orchestrator":
                    closes[plan] = ev["state"]
    except OSError:
        return {}
    return closes


def exit_authorized(events_path: str | None = None,
                    since_offset: int = 0) -> bool:
    """May the watcher STOP? Only an externally written run close says yes (R-40).

    The badge-level question ("is this run closed?") is answered by
    ``state["run_complete"]`` — the settle pass's own inference, which is
    debounced and self-healing: a premature close is reopened by the next spawn.
    The EXIT question is strictly harder, because exiting self-heals nothing (no
    one restarts the watcher), so it is answered only by a ``w:"agent"``
    ``orchestrator complete done|failed`` line appended after this watcher's
    startup baseline — i.e. by the driver/template that actually knows the
    workflow returned (both templates emit it in ``closeRun``). The watcher's own
    guess never stops it; see ABANDON_QUIET_SECS for the killed-session case.
    """
    return stream_terminal_close(events_path, since_offset, writer="agent")


def journal_quiescent(state: dict) -> bool:
    """Nothing in flight and nothing left that could still resolve (R-40).

    This is what makes the self-exit safe against a premature close: an in-flight
    agent — a 20-minute implementer appends nothing to the journal the whole
    time — must keep its watcher alive, and so must a plan that could still
    resolve. An EMPTY ``plans`` set is *unknown*, not quiescent: a watcher
    started before the driver's first spawn (the documented start order) has no
    plans yet, and unknown is never a verdict (GD-10) — least of all a reason to
    stop monitoring a run that has not begun.
    """
    if state.get("running") or not state.get("plans"):
        return False
    return run_outcome(state) is not None


def should_exit(quiet_secs: float, terminal: bool,
                window: int | None = None) -> bool:
    """R-40: exit only when the run is terminally complete AND the journal has
    been quiet for the whole window. Never on quiet alone (a long agent turn
    appends nothing for minutes), never on a complete event alone (the badge is
    reopened by a later spawn). ``terminal`` must come from exit_authorized() —
    the watcher's own inferred close is not a licence to stop. The caller adds
    journal_quiescent()."""
    return bool(terminal) and quiet_secs >= (EXIT_QUIET_SECS if window is None else window)


def abandoned_exit(state: dict, quiet_secs: float,
                   window: int | None = None) -> bool:
    """R-40 fallback: stop an ABANDONED run's watcher after a much longer window.

    No driver close ever arrived, the run is settled (``run_complete`` set by the
    settle pass) and the journal has been silent for ABANDON_QUIET_SECS — ten
    times the authorized window by default. This is the killed-session case
    (CONVO-14's orphans): the driver died before it could close the run, so
    nothing will ever authorize the exit, and a watcher pinned to a dead run is
    exactly what the amended GD-1 commit gate trips over.

    Deliberately NOT gated on "the last decision line promised no next stage":
    in the abandoned case it usually DID promise one (impl -> spawn test) and the
    promise is precisely what will never be kept, so that condition would make
    this branch unreachable for the only case it exists to handle. The long
    window is the guard; ORCH_NO_SELF_EXIT is the opt-out.
    """
    if not state.get("run_complete"):
        return False
    return quiet_secs >= (ABANDON_QUIET_SECS if window is None else window)


def exit_precheck(state: dict, quiet_secs: float) -> bool:
    """Cheap gate in front of the events-stream scan (R-40 / m1).

    Both exit routes need ``events.jsonl`` read (route 1) or the settle state
    (route 2), and this runs on every ~1 s poll tick, so the O(1) conditions come
    first: the opt-out, "nothing is in flight and nothing is left that could
    still resolve", and the shortest window either route could possibly fire in.

    ``min`` of the two windows, not ``EXIT_QUIET_SECS``: the windows are
    configured independently, so a gate pinned to the authorized one would
    silently clamp an operator who lowered only ``ORCH_ABANDON_QUIET_SECS`` —
    the abandoned branch would be unreachable inside its own window.
    """
    return bool(not NO_SELF_EXIT and journal_quiescent(state)
                and quiet_secs >= min(EXIT_QUIET_SECS, ABANDON_QUIET_SECS))


def transcript_idle_for(agent_id: str, now: float | None = None) -> float | None:
    """Seconds since the agent's newest transcript copy was written; None if none exist.

    Only consulted on the abandoned path (a full glob per agent), so it stays off
    the hot loop.
    """
    stamps = []
    for path in agent_paths(agent_id):
        try:
            stamps.append(os.path.getmtime(path))
        except OSError:
            continue
    if not stamps:
        return None
    return max(0.0, (now if now is not None else time.time()) - max(stamps))


def abandoned_agents(running: list, quiet_secs: float, idle_for=transcript_idle_for,
                     window: int | None = None) -> list:
    """Which in-flight agents are provably gone, not merely slow? (R-40 / GD-10)

    An agent leaves ``running`` only on a journal ``result``. When the session is
    killed mid-agent the result never comes, so ``running`` never empties, the
    settle pass never fires and the run card ticks "running" forever. GD-10 (as
    amended) already says a long-idle agent is *unknown*, never running: after
    ABANDON_QUIET_SECS of journal silence, an agent whose transcript has also not
    been touched in that window is closed `stale`. A transcript still being
    written (a 30-minute implementer) keeps its agent — that is the one case this
    must never misjudge.
    """
    w = ABANDON_QUIET_SECS if window is None else window
    if quiet_secs < w:
        return []
    gone = []
    for aid in list(running):
        idle = idle_for(aid)
        if idle is None or idle >= w:
            gone.append(aid)
    return gone


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
        if plan == FINALGATE_PLAN:
            # The final-gate FIXER is an impl role with no test stage after it
            # (R-08): the loop re-runs the sweep itself, so the generic impl line
            # below would name a stage that never runs.
            nxt = (f"re-gate {attempt + 1}/{MAX_FINALGATE_ATTEMPTS}"
                   if attempt < MAX_FINALGATE_ATTEMPTS
                   else f"no re-gate left {attempt}/{MAX_FINALGATE_ATTEMPTS}")
            return stage, "info", (f"{plan} fixer #{attempt} returned {len(files)} "
                                   f"changed files -> {nxt}")
        return stage, "info", f"{plan} impl #{attempt} returned {len(files)} changed files -> spawn test"
    if plan == FINALGATE_PLAN and "passed" in r:
        # Final-gate text is keyed on (plan, role), not role alone (R-08): the
        # aggregate sweep is a test role with NO critique after it, so the
        # generic test line below would name a stage that never runs.
        if r["passed"]:
            return stage, "done", f"decision: {plan} sweep #{attempt} PASS -> run complete"
        nxt = (f"spawn fixer, re-gate {attempt + 1}/{MAX_FINALGATE_ATTEMPTS}"
               if attempt < MAX_FINALGATE_ATTEMPTS
               else f"sweep attempts exhausted {attempt}/{MAX_FINALGATE_ATTEMPTS} -> run FAILED")
        return stage, "failed", f"decision: {plan} sweep #{attempt} FAIL -> {nxt}"
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

    Every plan is folded to its EFFECTIVE close state: already-terminal badges
    as they stand, still-open ones through GD-10's close predicate. So a run
    whose plans produced results but no gate verdict (a research run: findings
    only) closes **done**, where the old rule required a decisive verdict on
    every plan and therefore never closed such a run at all (R-08).

    None while any agent is still running: "no complete event + journal quiet"
    is *unknown*, never a verdict. The caller still debounces — a rejection
    about to retry looks terminal for a moment, and any later spawn reopens the
    badge, so a premature close self-heals.
    """
    if not state["plans"] or state["running"]:
        return None
    decisive = state.get("decisive", {})
    last_result_ok = state.get("last_result_ok", {})
    effective = [v if v in ("done", "failed") else close_state_for(p, decisive, last_result_ok)
                 for p, v in state["plans"].items() if p != "orchestrator"]
    if not effective:
        return None
    return "done" if all(v == "done" for v in effective) else "failed"


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


def journal_identity(path: str | None = None) -> str | None:
    """``"<st_dev>:<st_ino>"`` for the journal, or None if it cannot be stat'ed.

    SD-10 pins checkpoint identity as ``(st_dev, st_ino, size, offset)``: a
    journal REPLACED in place by a LARGER file keeps ``size >= offset``, so the
    size-only shrink check misses it and the stale offset would point into
    unrelated bytes. The inode pair catches that rotation.
    """
    try:
        st = os.stat(path or JOURNAL)
    except OSError:
        return None
    return f"{st.st_dev}:{st.st_ino}"


def load_state() -> dict:
    """Load the checkpoint, keyed to its journal's path AND inode (D8, SD-10).

    ``.watcher-state.json`` records the resolved JOURNAL path plus its
    ``(st_dev, st_ino)`` identity. If either differs from the current journal
    (auto-discovery or a wf_dir change picked a different run; the file was
    replaced/rotated in place), the byte offset is meaningless against the new
    journal — applying it would skip the new run's head, stall the tailer
    forever, or read unrelated bytes — so reset offset=0 and clear all derived
    run state before tailing.
    """
    fresh = {"offset": 0, "agents": {}, "journal": JOURNAL,
             "journal_id": journal_identity()}
    try:
        with open(STATE) as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return fresh
    if state.get("journal") != JOURNAL:
        return fresh
    now_id = journal_identity()
    if state.get("journal_id") and now_id and state["journal_id"] != now_id:
        return fresh
    return state


def save_state(state: dict) -> None:
    state["journal"] = JOURNAL
    jid = journal_identity()
    if jid:
        state["journal_id"] = jid
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
    # GD-10: "was the plan's LAST result a failure" — the close predicate's
    # fallback when no gate/critic ever returned a decisive verdict.
    state.setdefault("last_result_ok", {})
    state.setdefault("last_plan", None)
    state.setdefault("run_complete", None)
    for cached in state["agents"].values():  # pre-upgrade cache entries lack "stage"
        cached.setdefault("stage", cached.get("role", "work").split(":")[-1])
    # Session scope for the R-40 self-exit: every `complete` event already in the
    # stream belongs to an EARLIER phase of this task folder (one folder hosts
    # research, then implement-plan), so only bytes appended past this baseline
    # can end THIS watcher's run. Recorded before the first emit so the
    # heartbeat itself stays outside the window.
    try:
        events_baseline = os.path.getsize(EVENTS)
    except OSError:
        events_baseline = 0
    # Continuation heal: a wf_dir change resets this checkpoint fresh
    # (run_complete=None), so a stale ``complete done`` left by an EARLIER
    # phase in the same task folder would never be healed — the badge (and
    # every replaying dashboard) would read "done" while this phase's loops
    # run. If the stream's badge already reads closed, arm the same reopen
    # the settle pass uses: the next spawn emits ``complete running``.
    if state.get("run_complete") is None and stream_badge_closed():
        state["run_complete"] = "stale-stream-close"
    emit("watcher", "info", "decision watcher online (tailing workflow journal)")
    # Deferred config/env warnings land after the heartbeat, in startup context.
    for warning in _CFG_WARNINGS:
        print(warning, file=sys.stderr, flush=True)
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
        label = agent_label(info, aid)
        new_base = {"in": max(prev.get("in", 0), tin), "out": max(prev.get("out", 0), tout),
                    "cached": max(prev.get("cached", 0), tcached),
                    "cache_write": max(prev.get("cache_write", 0), twrite)}
        emit("tokens", "info", f"{label} token backfill", plan=plan,
             extra={"tokens": deltas, "quiet": True,
                    # no "state" key: leave the row's queued/running/done dot as-is
                    "agent": agent_block(aid, info,
                                         tokens={"in": new_base["in"], "out": new_base["out"],
                                                 "cached": new_base["cached"],
                                                 "cache_write": new_base["cache_write"]})})
        state["tok_emitted"][aid] = new_base
    save_state(state)
    # M-2: from here on a SIGTERM (the templates' closeRun epilogue) arms a drain
    # instead of killing the process mid-poll. Installed after the startup
    # backfill so a signal that lands during it cannot leave a half-written
    # checkpoint.
    install_stop_handlers()
    tick = 0
    drain_until = None  # set when a stop signal arms the shutdown drain (M-2)
    quiet_since = None  # wall-clock start of the current terminal-quiet stretch

    def tick_sleep() -> None:
        """One poll interval: fast while draining, interruptible otherwise (M-2).

        Every ``continue`` path in the loop goes through this, so a stop signal is
        picked up within ~0.1 s no matter which branch the watcher is in — and a
        watcher parked on a missing journal still stops when it is signalled,
        which is what it did before a handler existed at all.
        """
        if drain_until is not None:
            time.sleep(0.1)
        else:
            poll_sleep()

    last_growth = time.time()  # last time the journal actually grew (R-40)
    # False only until the first poll catches up with the journal: a chunk read
    # after that was appended within the last poll interval (fresh), while the
    # startup chunk may be hours-old backlog whose read time means nothing.
    caught_up = False
    while True:
        # M-2 shutdown drain, checked BEFORE the tail so every branch below
        # (including the `continue` paths) can reach it. The first stop signal
        # only arms the window: the passes that follow are what actually rescue
        # the last agent's result, decision line and token totals from
        # `closeRun`'s immediate SIGTERM. A second signal exits now.
        if stop_requested():
            if len(_STOP_SIGNALS) > 1:
                emit("watcher", "info",
                     "watcher exiting: second stop signal, drain cut short")
                save_state(state)
                return
            if drain_until is None:
                drain_until = time.time() + DRAIN_SECS
            elif time.time() >= drain_until:
                emit("watcher", "info",
                     f"watcher exiting: stop signal, journal drained {DRAIN_SECS}s")
                save_state(state)
                return
        try:
            jstat = os.stat(JOURNAL)
        except OSError:
            tick_sleep()
            continue
        size = jstat.st_size
        jid = f"{jstat.st_dev}:{jstat.st_ino}"
        rotated = bool(state.get("journal_id")) and state["journal_id"] != jid
        if size < state["offset"] or rotated:
            # Truncated (size < offset) or replaced in place (a different inode at
            # the same path — SD-10's identity, which catches a REPLACEMENT that
            # is LARGER than the old offset): the stored byte offset is
            # meaningless against these bytes, so tailing from it would stall
            # forever or read unrelated content. Rewind and rebuild every
            # journal-derived fact. `tok_emitted` is deliberately KEPT: token
            # baselines are keyed to transcripts, not to journal bytes, and
            # clearing them would re-emit every delta and double the dashboard's
            # counters.
            state["offset"] = 0
            state["agents"] = {}
            state["running"] = []
            state["plans"] = {}
            state["decisive"] = {}
            state["last_result_ok"] = {}
            state["last_plan"] = None
            state["run_complete"] = None
            save_state(state)
            emit("watcher", "info", "journal truncated — rebuilding")
            caught_up = False
            quiet_since = None
            last_growth = time.time()
            tick_sleep()
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
                        # The badge closed (watcher-detected quiet end, the
                        # driver's own event, or an earlier phase's stale close
                        # armed at startup) yet the run spawned again: reopen.
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
                                 extra={"agent": agent_block(
                                     other, oinfo, "stale",
                                     runtime=elapsed_str(first_ts(other), last_ts(other)))})
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
                             extra={"agent": agent_block(agent_id, info, "running",
                                                         started=ts0)})
                        prev = state.get("last_plan")
                        if (STRATEGY == "serial" and prev and prev != info["plan"]
                                and state["plans"].get(prev) == "running"):
                            # LEGACY-ONLY sequenced close, gated on
                            # orch-config.json "strategy":"serial" (GD-10): in a
                            # serial loop a new plan starting does imply the prior
                            # one exited. For every other run this heuristic is
                            # retired — applied to a parallel fan-out it fabricated
                            # `plan failed "loop exited -> synthesis"` the moment
                            # synthesis spawned, while all researchers had
                            # succeeded (R-58). The close STATE now comes from the
                            # GD-10 predicate either way: verdict-less and
                            # non-failing closes done, not failed.
                            #
                            # The detail says "serial advance ->", NOT the historic
                            # "loop exited ->": that exact phrase is the signature
                            # SD-4's read-time re-labeler keys on to re-read the
                            # ALREADY-WRITTEN fabricated badges as "closed — no
                            # verdict". A new legacy-mode run must not emit a
                            # genuine close that collides with it (SD-4/R-51).
                            st = close_state_for(prev, state["decisive"],
                                                 state["last_result_ok"])
                            state["plans"][prev] = st
                            emit("plan", st,
                                 close_detail(prev, state["decisive"],
                                              f"serial advance -> {info['plan']}"),
                                 ts=ts0, plan=prev)
                        state["last_plan"] = info["plan"]
                        if info["plan"] not in state["plans"]:
                            state["plans"][info["plan"]] = "running"
                            emit("plan", "running", "first agent spawned", ts=ts0, plan=info["plan"])
                        elif state["plans"][info["plan"]] in ("done", "failed"):
                            # A terminal badge closed this plan, yet the loop
                            # spawned another agent for it — an intermediate gate
                            # (e.g. test green before e2e/critique) closed it
                            # prematurely, or a retry followed a rejection. The
                            # journal has now proven the loop is still running:
                            # reopen the card. Reopening from `failed` too (R-08)
                            # is what stops a mid-run failure badge from sticking
                            # to a plan that went on to pass.
                            state["plans"][info["plan"]] = "running"
                            emit("plan", "running",
                                 f"loop continues: {info['role']} attempt {info['attempt']} spawned",
                                 ts=ts0, plan=info["plan"])
                    else:
                        # GD-7: harness facts create nodes, markers only LABEL
                        # them — an unclassifiable agent still gets its row (full
                        # agentId identity, `unconventional` flag), it just has no
                        # plan/stage label to hang on a plan card.
                        ts0 = first_ts(agent_id)
                        emit("watcher", "info", f"spawn unclassified agent {agent_id}",
                             ts=ts0,
                             extra={"agent": agent_block(agent_id, None, "running",
                                                         started=ts0)})
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
                        # GD-10's close fallback: remember whether this plan's
                        # LAST result was a failure, so a verdict-less plan can
                        # close "done — no verdict" instead of a fabricated failed.
                        state["last_result_ok"][info["plan"]] = sst != "failed"
                        a_tin, a_tcached, a_twrite, a_tout = agent_tokens(agent_id)
                        emit(info["stage"], sst,
                             f"{info['role']} #{info['attempt']}: {sdetail}",
                             ts=tsN, plan=info["plan"],
                             extra={"agent": agent_block(
                                 agent_id, info, sst,
                                 tokens={"in": a_tin, "out": a_tout,
                                         "cached": a_tcached, "cache_write": a_twrite},
                                 runtime=elapsed_str(first_ts(agent_id), tsN))})
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
                        tsN = last_ts(agent_id)
                        emit("watcher", "info",
                             f"result from unclassified agent {agent_id}", ts=tsN,
                             extra={"agent": agent_block(
                                 agent_id, None, "done",
                                 runtime=elapsed_str(first_ts(agent_id), tsN))})
            # Commit the offset only past the fully-consumed lines: a torn tail
            # (new_offset unchanged) is re-read next poll once it completes (D5).
            if new_offset != state["offset"]:
                last_growth = time.time()
            state["offset"] = new_offset
            save_state(state)
        caught_up = True
        quiet_for = time.time() - last_growth
        # Caps/strategy are re-read while running (D4/R-09): the orchestrator
        # script publishes them from INSIDE the run, i.e. after the daemons
        # started, so a watcher that only read them at import would narrate its
        # own defaults for the whole run.
        moved = refresh_caps()
        if moved:
            emit("watcher", "info",
                 f"config reloaded: plan cap {MAX_PLAN_ATTEMPTS}, gate cap "
                 f"{MAX_GATE_ATTEMPTS}, finalgate cap {MAX_FINALGATE_ATTEMPTS}, "
                 f"strategy {STRATEGY or 'unset'}")
        # ABANDONED agents: a session killed mid-agent leaves journal `started`
        # entries with no `result`, so `running` never empties and the run card
        # ticks forever. After the long window, close them `stale` (GD-10: a
        # long-idle agent is unknown, never running) and let the settle pass and
        # the abandoned-exit below do their job.
        gone = abandoned_agents(state["running"], quiet_for)
        for aid in gone:
            ainfo = state["agents"].get(aid)
            state["running"].remove(aid)
            emit(ainfo["stage"] if ainfo else "watcher", "stale",
                 (f"{ainfo['role']} #{ainfo['attempt']}" if ainfo else f"agent {aid}")
                 + " abandoned — no result, no transcript activity for "
                   f"{ABANDON_QUIET_SECS}s",
                 plan=ainfo["plan"] if ainfo else "orchestrator",
                 extra={"agent": agent_block(
                     aid, ainfo, "stale",
                     runtime=elapsed_str(first_ts(aid), last_ts(aid)))})
        if gone:
            save_state(state)
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
                # m-3: adopt the closes the STREAM already carries before writing
                # any of our own. The orchestrator script emits terminal
                # `plan done` events itself (R-09) and the watcher never folds
                # them into state["plans"], so without this the settle pass wrote
                # a second, "(closed, no verdict)"-labelled close for a card the
                # script had just closed with a verified one.
                for plan, closed_state in stream_plan_closes(
                        EVENTS, events_baseline).items():
                    if plan in plans and plans[plan] not in ("done", "failed"):
                        plans[plan] = closed_state
                # The adoption can move a plan's effective state (a script close
                # is authoritative over the predicate's inference), so the run
                # verdict is re-derived from the adopted badges.
                outcome = run_outcome(state) or outcome
                # Settle every still-open plan card before the terminal event so
                # the last plan can't spin "running" forever or keep a stale
                # green. The state is GD-10's ONE predicate: a decisive verdict
                # decides; absent one, the plan closes on whether its LAST result
                # failed — so a verdict-less fan-out closes "done (closed, no
                # verdict)", NEVER a fabricated `failed` (R-58). Do not "restore"
                # a decisive-only rule here.
                for plan, badge in list(plans.items()):
                    if plan == "orchestrator" or badge in ("done", "failed"):
                        continue
                    st = close_state_for(plan, state["decisive"], state["last_result_ok"])
                    plans[plan] = st
                    emit("plan", st,
                         close_detail(plan, state["decisive"],
                                      f"run {outcome}: settling open plan"), plan=plan)
                # Only when the VERDICT actually moved: adopting the stream's
                # closes can re-derive the same outcome the badge already carries,
                # and re-announcing it would be one more duplicate close of
                # exactly the kind m-3 exists to remove.
                if state.get("run_complete") != outcome:
                    emit("complete", outcome,
                         f"run {outcome}: {len(plans)} plan(s) "
                         + ("all green" if outcome == "done" else "closed with failures")
                         + f"; loops idle {QUIET_SECS}s+ (watcher-detected end)")
                    state["run_complete"] = outcome
                quiet_since = None
                save_state(state)
        else:
            quiet_since = None
        # R-40 run-close protocol. Two routes, deliberately asymmetric, because
        # exiting is irreversible (nothing restarts a watcher) while a wrong badge
        # self-heals on the next spawn:
        #   1. AUTHORIZED — the driver/template appended `orchestrator complete
        #      done|failed` (w="agent") after this watcher's startup baseline, the
        #      journal has been quiet for EXIT_QUIET_SECS and nothing is left that
        #      could still resolve. This is the normal end of a run.
        #   2. ABANDONED — no such line ever came, but the run settled and the
        #      journal has been silent for ABANDON_QUIET_SECS (10x). This covers
        #      the killed session, whose driver can no longer close anything.
        # The watcher's OWN inferred close (state["run_complete"]) never satisfies
        # route 1: a harness stall between agents looks exactly like a finished
        # run, and stopping there silently ends monitoring of a live run — the one
        # thing this module exists to prevent. Route 2 accepts that inference only
        # after a window no ordinary pause survives.
        #
        # That makes "is this loop still running" answerable from process state —
        # the amended GD-1 commit gate and the Touch UI both depend on it, and
        # three orphaned watchers from finished runs are what forced the rule. A
        # run that resumes gets a fresh watcher; state is checkpointed, so
        # restarting never double-counts.
        #
        # exit_precheck() is what keeps the stream scan off the hot path: the
        # terminal check reads events.jsonl, so it must not run on every poll tick
        # of a merely-idle run (m1). It also carries the ORCH_NO_SELF_EXIT opt-out.
        if exit_precheck(state, quiet_for):
            if should_exit(quiet_for, exit_authorized(EVENTS, events_baseline)):
                emit("watcher", "info",
                     f"watcher exiting: run closed by the driver, journal quiet "
                     f"{EXIT_QUIET_SECS}s+")
                save_state(state)
                return
            if abandoned_exit(state, quiet_for):
                emit("watcher", "info",
                     f"watcher exiting: run abandoned — no driver close, journal "
                     f"quiet {ABANDON_QUIET_SECS}s+")
                save_state(state)
                return
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
                    label = agent_label(info, aid)
                    base = {"in": max(prev.get("in", 0), tin), "out": max(prev.get("out", 0), tout),
                            "cached": max(prev.get("cached", 0), tcached),
                            "cache_write": max(prev.get("cache_write", 0), twrite)}
                    emit("tokens", "info",
                         f"{label} running: {fmt_in(base['in'], base['cached'], base['cache_write'])} · out {fmt_tokens(base['out'])} so far",
                         plan=plan,
                         extra={"tokens": {"in": din, "out": dout, "cached": dcached,
                                           "cache_write": dwrite},
                                "quiet": True,
                                "agent": agent_block(
                                    aid, info, "running",
                                    tokens={"in": base["in"], "out": base["out"],
                                            "cached": base["cached"],
                                            "cache_write": base["cache_write"]})})
                    state["tok_emitted"][aid] = base
                    dirty = True
            if dirty:
                save_state(state)
        tick_sleep()


if __name__ == "__main__":
    main()
