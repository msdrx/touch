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
import subprocess
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


# --------------------------------------------------------------------------
# Command line. The one POSITIONAL argument is still the run dir (`argv[1]`,
# unchanged); flags are split out of it so a `--flag` can never be mistaken for
# a wf_dir. An UNKNOWN flag warns (deferred, R-07) and is ignored rather than
# refused: this is a best-effort observer of someone else's run, and dying on a
# typo would lose the live view the module exists to keep.
# --------------------------------------------------------------------------
KNOWN_FLAGS = ("--reconcile", "--no-tokens")


def parse_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """``(flags, positional)`` — anything starting with ``-`` is a flag."""
    return ([a for a in argv if a.startswith("-")],
            [a for a in argv if not a.startswith("-")])


_FLAGS, _POSITIONAL = parse_argv(sys.argv[1:])
for _flag in _FLAGS:
    if _flag not in KNOWN_FLAGS:
        _CFG_WARNINGS.append(
            f"decision_watcher: unknown flag {_flag!r}; ignored "
            f"(known flags: {', '.join(KNOWN_FLAGS)})")


def _flag_on(flag: str, env: str) -> bool:
    """Is ``flag`` on the command line, or ``env`` set to anything but off?

    Same truthiness ORCH_NO_SELF_EXIT already uses (any non-empty value except
    ``0``/``false``/``no``), so an operator learns one spelling for the module.
    """
    if flag in _FLAGS:
        return True
    return str(os.environ.get(env, "")).strip().lower() not in ("", "0", "false", "no")


# D-16: one-shot post-run reconcile against the run snapshot, then exit. Not a
# daemon mode — it emits the corrections the live tail could not see and stops.
RECONCILE = _flag_on("--reconcile", "ORCH_RECONCILE")
# D-05: suppress this watcher's token accounting entirely, so a deployment that
# has wired the aggregator's ingest tick (D-01) can make `ingest.rollup` the ONE
# reachable implementation of the same pure function. Default OFF: the watcher
# is the live view's token source until 8932 convergence, and the two
# implementations are cross-checked (tests/test_token_crosscheck.py) rather than
# one being quietly preferred.
NO_TOKENS = _flag_on("--no-tokens", "ORCH_NO_TOKENS")


# --------------------------------------------------------------------------
# The tasks-root resolver and the plugin-cache guard are duplicated VERBATIM in
# monitor_server.py. Both daemons must stay independently runnable single
# files (a shared import would make one require the other on PYTHONPATH), so
# the two copies are pinned together by a source-text equality test in
# tests/test_server.py — exactly the FOLD_GEN precedent. Edit both or neither.
# --------------------------------------------------------------------------


def resolve_tasks_root() -> str:
    """The orchestration tasks root: env > project > cwd walk-up (G10).

    Order, and why each rung exists:

    1. ``$ORCH_TASKS_ROOT`` — the operator's explicit override, always wins.
    2. ``$CLAUDE_PROJECT_DIR/.touch/local-orchestrators`` — the hook/skill
       environment's first-class project anchor.
    3. cwd walk-up to the nearest ``.claude/`` marker, then
       ``.touch/local-orchestrators`` under it — a bare shell in a project
       checkout. The MARKER dir and the STATE dir are deliberately DIFFERENT:
       ``.claude/`` is what marks a *Claude Code* project (``.touch/`` is
       created by Touch and is gitignored, so it cannot mark one), and the run
       history lives under ``.touch/``.

    Three rungs, and ``""`` when none of them resolves. The former FOURTH rung —
    a module-relative ``../../local-orchestrators`` sibling lookup — is DELETED:
    after GD-U1 nothing sits two levels above this directory in the payload, so
    it had nothing to resolve to, and in an installed copy it would glob
    whatever sits beside the plugin (LAYOUT-15, PROTOCOL-11). There is
    deliberately no module-directory fallback either: the shared module dir is
    code-only (D6), and in a plugin install it is a version-stamped cache
    directory that is re-copied on update and swept ~14 days later — state
    written there is data loss with extra steps. The caller decides what an
    unresolved root means.
    """
    env = os.environ.get("ORCH_TASKS_ROOT")
    if env:
        return os.path.abspath(env)
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if project:
        return os.path.abspath(os.path.join(project, ".touch", "local-orchestrators"))
    here = os.path.abspath(os.getcwd())
    while True:
        if os.path.isdir(os.path.join(here, ".claude")):
            return os.path.join(here, ".touch", "local-orchestrators")
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return ""


def in_plugin_cache(path: str) -> bool:
    """True when ``path`` sits inside an installed plugin.

    An ancestor holding ``.claude-plugin/plugin.json`` marks a plugin root:
    version-stamped, re-copied on every update and garbage-collected ~14 days
    later. Nothing may write there, unconditionally — not a spool, not a
    checkpoint, not a token file. Walks the string, so it answers for a path
    that does not exist yet.
    """
    here = os.path.abspath(path)
    while True:
        if os.path.isfile(os.path.join(here, ".claude-plugin", "plugin.json")):
            return True
        parent = os.path.dirname(here)
        if parent == here:
            return False
        here = parent


TASKS_ROOT = resolve_tasks_root()


def resolve_state_dir() -> str:
    """State dir: $ORCH_STATE_DIR > newest task folder under TASKS_ROOT.

    The shared module dir is code-only and never an authoritative state dir
    (D6): a stray ``ROOT/events.jsonl`` must not hijack auto-discovery, so we
    fall straight through to the newest task-folder glob when the env is unset
    — and there is no ROOT *fallback* under it. A watcher with nowhere to write
    exits 1 naming the env vars that would fix it; it never mkdirs a root it
    guessed.
    """
    if os.environ.get("ORCH_STATE_DIR"):
        return os.environ["ORCH_STATE_DIR"]
    candidates = (glob.glob(os.path.join(TASKS_ROOT, "*", "events.jsonl"))
                  if TASKS_ROOT else [])
    if candidates:
        return os.path.dirname(max(candidates, key=os.path.getmtime))
    sys.exit("decision_watcher: no task state dir found. Set ORCH_STATE_DIR to the "
             "task folder, or ORCH_TASKS_ROOT / CLAUDE_PROJECT_DIR to the project "
             f"that owns .touch/local-orchestrators (tasks root: {TASKS_ROOT or 'unresolved'})")


STATE_DIR = os.path.abspath(resolve_state_dir())
# Never write into an installed plugin: the cache directory is version-stamped
# and swept, so a checkpoint written there is a checkpoint that vanishes.
if in_plugin_cache(STATE_DIR):
    sys.exit(f"decision_watcher: refusing to write into a plugin cache ({STATE_DIR}); "
             "set ORCH_STATE_DIR to a task folder in your project")
# Create the state dir up front (R-07): a watcher pointed at a not-yet-created
# task folder must still be able to write its very first event. Only ever the
# dir that $ORCH_STATE_DIR named or that the glob already found — never a
# fallback root this process invented.
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
    if _POSITIONAL:
        return _POSITIONAL[0]
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


# --------------------------------------------------------------------------
# GD-LC-6: the DECLARED context window, and nothing else.
#
# Capacity is not recorded on any live path the watcher can read (GD-LC-18):
# a transcript's `message.model` never carries the `[1m]` suffix (44,412 rows;
# 72 agents peaked above 200k, max 522,172, every one of them recorded as the
# bare `claude-opus-5`). So the denominator is DECLARED or it is unknown:
#
#   * `orch-config.json` key `context_window` — an int (all models) or a
#     `{model-string: int}` map — re-read live by refresh_caps() on an mtime
#     change, exactly like `token_tick_secs`;
#   * `ORCH_CONTEXT_WINDOW` PINS it, the way ORCH_TOKEN_TICK_SECS pins the
#     tick: an operator debugging a live run is not overridden by a config the
#     orchestrator script republishes. Set-but-invalid still pins (the config
#     does not quietly take over) and warns.
#   * one grammar, two sources: the env var accepts the same int or JSON
#     `{model: int}` map the config key does, so an operator never has to learn
#     a second spelling to pin what the config already says.
#
# There is deliberately NO built-in model->window table and NO ">200k means
# 1M" promotion (rejection R3: observing 522k proves capacity > 200k, not
# = 1M, and the bare model string cannot tell the variants apart), and NO
# fallback to 200,000 EVER — on this machine that renders a healthy 522k agent
# as "261 % full", which is precisely the R-58 defect this feature exists to
# avoid. An undeclared window is a CORRECT state: absolute tokens, no bar.
# --------------------------------------------------------------------------
CONTEXT_WINDOW_MIN = 1_000
CONTEXT_WINDOW_MAX = 10_000_000
# int | {model: int} | None. None means "undeclared", which is honest, not
# degraded — ctx_field() then omits `cap` and the page says "window unknown".
CONTEXT_WINDOW: int | dict | None = None


def _bounded_window(value: int, model: str | None, source: str) -> int | None:
    """One declared window, bounds-checked (GD-LC-6.2), or None + a warning.

    Out of bounds is refused rather than clamped: a clamp would invent a
    denominator, and every invented denominator here is a percentage nobody
    measured. The warning is DEFERRED like every other config warning (R-07) —
    a typo in someone else's run config must not kill this observer.
    """
    if CONTEXT_WINDOW_MIN <= value <= CONTEXT_WINDOW_MAX:
        return value
    _CFG_WARNINGS.append(
        f"decision_watcher: {source} "
        + (f"[{model}] " if model else "")
        + f"= {value} is outside {CONTEXT_WINDOW_MIN}..{CONTEXT_WINDOW_MAX}; "
          "ignored, context window stays undeclared")
    return None


def _parse_context_window(value, source: str) -> int | dict | None:
    """``context_window`` in either declared form, or None (GD-LC-6.1).

    Accepts an int (every model), a ``{model-string: int}`` map, or — because
    the env var carries the same grammar as the config key — a string holding
    either. A `bool` is refused explicitly: it is an `int` subclass, so a bare
    `isinstance` check would read `true` as a 1-token window.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        raw = value.strip()
        try:
            return _bounded_window(int(raw), None, source)
        except ValueError:
            pass
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = None
        if not isinstance(decoded, (int, dict)) or isinstance(decoded, bool):
            _CFG_WARNINGS.append(
                f"decision_watcher: invalid {source}={raw!r}; expected an int "
                "or a {model: int} map; context window stays undeclared")
            return None
        return _parse_context_window(decoded, source)
    if isinstance(value, bool):
        _CFG_WARNINGS.append(
            f"decision_watcher: invalid {source}={value!r}; expected an int "
            "or a {model: int} map; context window stays undeclared")
        return None
    if isinstance(value, int):
        return _bounded_window(value, None, source)
    if isinstance(value, dict):
        # A bad entry drops ITSELF, not the whole map: one mistyped model must
        # not silently take the window away from every other model in the file.
        windows = {}
        for model, cap in value.items():
            if isinstance(cap, bool) or not isinstance(cap, int):
                _CFG_WARNINGS.append(
                    f"decision_watcher: invalid {source}[{model!r}]={cap!r}; "
                    "expected an int; that model keeps no declared window")
                continue
            bounded = _bounded_window(cap, str(model), source)
            if bounded is not None:
                windows[str(model)] = bounded
        return windows or None
    _CFG_WARNINGS.append(
        f"decision_watcher: invalid {source}={value!r}; expected an int or a "
        "{model: int} map; context window stays undeclared")
    return None


# Presence PINS (see the block comment): a set-but-unparseable env var leaves
# the window undeclared and says so, rather than letting the config win back a
# value the operator was trying to override.
_CONTEXT_WINDOW_PINNED = bool(str(os.environ.get("ORCH_CONTEXT_WINDOW", "")).strip())
_CONTEXT_WINDOW_ENV = (_parse_context_window(os.environ.get("ORCH_CONTEXT_WINDOW"),
                                             "ORCH_CONTEXT_WINDOW")
                       if _CONTEXT_WINDOW_PINNED else None)


def context_cap(model: str | None) -> int | None:
    """The declared window for ``model``, or None (GD-LC-6.4/6.5).

    A map with no entry for this model returns None on purpose: guessing a
    neighbour's window is the model->window table GD-LC-6 forbids, five times
    wrong in one direction or the other.
    """
    window = CONTEXT_WINDOW
    if isinstance(window, int):
        return window
    if isinstance(window, dict) and model:
        return window.get(model)
    return None


def context_window_str() -> str:
    """The declared window, for the ``config reloaded:`` line (LC-03)."""
    window = CONTEXT_WINDOW
    if window is None:
        return "undeclared"
    if isinstance(window, int):
        return str(window)
    return " ".join(f"{model}={cap}" for model, cap in sorted(window.items()))


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
# GD-D: cadence CEILING for the live token tick, in seconds. The watcher still
# POLLS every ~1 s and still emits only when it has a non-zero delta to report —
# this knob can suppress an emit, never manufacture one. That asymmetry is
# load-bearing: the dashboard derives silence from the ABSENCE of events, so a
# heartbeat below the page's 4 min stall threshold would erase every stall
# segment the strip exists to expose (WRITE-SIDE-2, measured: all 17 of them).
#
# 15 s is the measured knee: at 15 s a real 12.3k-event run drops to 4.9k with
# bit-identical timeplan segmentation, while 30 s starts mis-drawing a working
# gap as a stall. Never raise the default past 30 s, and keep it far inside the
# page's TP_IDLE_MS (120 s). ``0`` = emit on every poll tick, i.e. exactly the
# pre-cadence behaviour, kept as the escape hatch.
#
# Precedence is env > orch-config.json > default, the same order resolve_wf_dir
# uses: ORCH_TOKEN_TICK_SECS PINS the value (an operator debugging a live run
# must not be overridden by a config the orchestrator script republishes), and
# without it ``token_tick_secs`` is re-read live by refresh_caps().
#
# Both spellings are documented for operators in monitoring.md by M14/sp-docs
# (this sub-plan owns no doc file): the orch-config row, the ceiling semantics
# and the "values below ~10 s barely help, the flush trigger is p50 5 s" range
# guidance live there, not here.
TOKEN_TICK_DEFAULT = 15
# max(0, ...) on BOTH paths (env here, config in apply_caps): a negative value
# is read as 0 = always due. "Emit less often than never" has no meaning, and
# the reload log line / apply_caps() return must state the value that is
# actually in force, not the typo that produced it.
_TOKEN_TICK_ENV: int | None = (
    max(0, _int_env("ORCH_TOKEN_TICK_SECS", TOKEN_TICK_DEFAULT))
    if os.environ.get("ORCH_TOKEN_TICK_SECS") else None)
TOKEN_TICK_SECS = TOKEN_TICK_DEFAULT if _TOKEN_TICK_ENV is None else _TOKEN_TICK_ENV


def apply_caps(cfg: dict) -> tuple:
    """Set the cap/strategy globals from a config dict; return their new tuple.

    The narration functions (``describe_result``) read these globals at CALL
    time, so re-applying them mid-run changes the next decision line — which is
    the whole point of refresh_caps() below.
    """
    global MAX_PLAN_ATTEMPTS, MAX_GATE_ATTEMPTS, MAX_E2E_ATTEMPTS
    global MAX_FINALGATE_ATTEMPTS, STRATEGY, TOKEN_TICK_SECS, CONTEXT_WINDOW
    MAX_PLAN_ATTEMPTS = _int_cfg(cfg, "max_plan_attempts", CAP_DEFAULTS["max_plan_attempts"])
    MAX_GATE_ATTEMPTS = _int_cfg(cfg, "max_gate_attempts", CAP_DEFAULTS["max_gate_attempts"])
    MAX_E2E_ATTEMPTS = _int_cfg(cfg, "max_e2e_attempts", CAP_DEFAULTS["max_e2e_attempts"])
    MAX_FINALGATE_ATTEMPTS = _int_cfg(cfg, "max_finalgate_attempts",
                                      CAP_DEFAULTS["max_finalgate_attempts"])
    STRATEGY = str(cfg.get("strategy") or "").strip().lower()
    # A negative value is read as 0 (always due) on both paths — see the
    # _TOKEN_TICK_ENV note above; silently freezing every counter would be the
    # worst possible reading of a typo.
    TOKEN_TICK_SECS = (_TOKEN_TICK_ENV if _TOKEN_TICK_ENV is not None
                       else max(0, _int_cfg(cfg, "token_tick_secs", TOKEN_TICK_DEFAULT)))
    # GD-LC-6: env PINS, else whatever the file declares (or nothing at all).
    CONTEXT_WINDOW = (_CONTEXT_WINDOW_ENV if _CONTEXT_WINDOW_PINNED
                      else _parse_context_window(cfg.get("context_window"),
                                                 "context_window"))
    # The returned tuple keeps its historic SHAPE — TOKEN_TICK_SECS stays last,
    # which is what every caller and test reads it by. CONTEXT_WINDOW is a
    # global like the rest and refresh_caps() compares it alongside this tuple.
    return (MAX_PLAN_ATTEMPTS, MAX_GATE_ATTEMPTS, MAX_E2E_ATTEMPTS,
            MAX_FINALGATE_ATTEMPTS, STRATEGY, TOKEN_TICK_SECS)


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
              MAX_FINALGATE_ATTEMPTS, STRATEGY, TOKEN_TICK_SECS, CONTEXT_WINDOW)
    seen = len(_CFG_WARNINGS)
    # apply_caps() keeps its historic return shape, so the declared window is
    # appended here: a config that moves ONLY `context_window` must still be
    # reported as a reload, or a re-declared denominator would land silently.
    after = apply_caps(read_config()) + (CONTEXT_WINDOW,)
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
# Reserved plan id for implement's final aggregate sweep: its decision text
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


def single_line(text) -> str:
    """One line, no double quotes — monitoring.md's `detail` rule, at the writer.

    Applied to every string the watcher did NOT author: an agent's own
    ``summary`` (D-06), a harness error string, a `<failures>` line (D-08).
    The reason is shell and JS-template embedding downstream, not JSON — a
    detail travels through a bash argument and a JS template literal before
    anything parses it — and collapsing whitespace also keeps the 1 KB cap
    spending its budget on words instead of indentation.
    """
    return " ".join(str(text or "").replace('"', "").split())


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


# WRITE-SIDE-10: per-transcript incremental parse cache, keyed by path:
#   {"ident": "<dev>:<ino>", "offset": int, "lines": int,
#    "usage": {message-key: (in, cached, write, out)}}
# ``agent_tokens`` used to re-read and json.loads EVERY line of EVERY transcript
# copy of EVERY running agent on EVERY ~1 s poll tick (4.5-11.0 ms per call on a
# real 1 MB transcript, growing with the transcript, so quadratic in agent
# length). Only the bytes past ``offset`` are parsed now; the message-key union
# that makes a /clear-split transcript safe survives incremental reads unchanged,
# because a re-flushed message overwrites its own key whenever it is re-appended.
# What is retained is four ints per billed message, not the raw usage rows — the
# cost of not re-reading the same bytes every second, kept as small as the
# union semantics allow.
_USAGE_CACHE: dict[str, dict] = {}


def drop_usage_cache(agent_id: str) -> None:
    """Forget one agent's parse caches — called when it is finished for good.

    The cache is per-PATH and lives as long as the daemon, so without this the
    167-agent measured run retains order-10^5 dead entries (four ints plus a
    message key each) for agents that have already resulted or been stale-closed
    and will never be read again. Every call site of flush_agent_tokens() is by
    construction such a terminal point.

    Dropping is always SAFE, never merely cheap: a re-read simply re-parses the
    file from byte 0 and rebuilds the same message-keyed union, because the
    cache is a memo of the file's own bytes and holds no state the file lacks.

    Eviction reads the CACHE's own keys instead of re-running agent_paths():
    globbing the whole projects tree a second time just to throw a dict away
    would double the glob on every result, every stale close and every swept
    agent — in the one pass whose first fix is removing ~93% of that glob. It is
    also strictly more complete: a transcript copy pruned or rotated away
    between the last read and this call is no longer returned by the glob, so a
    glob-driven eviction would leave that entry alive for the daemon's whole
    life with no other eviction path at all.
    """
    suffix = f"agent-{agent_id}.jsonl"
    for path in [p for p in _USAGE_CACHE if os.path.basename(p) == suffix]:
        _USAGE_CACHE.pop(path, None)


def _usage_totals(u: dict) -> tuple[int, int, int, int]:
    """One usage row as ``(input, cache-read, cache-write, output)``.

    ``input`` is the TOTAL input volume (fresh + cache writes + cache reads);
    the cache components are also reported separately because an agent loop
    re-sends its whole conversation prefix every turn.
    """
    cached = u.get("cache_read_input_tokens") or 0
    write = u.get("cache_creation_input_tokens") or 0
    return ((u.get("input_tokens") or 0) + write + cached, cached, write,
            u.get("output_tokens") or 0)


# --------------------------------------------------------------------------
# GD-LC-1/2/3: CONTEXT OCCUPANCY — a LEVEL at an instant, not a total.
#
# `agent.tokens` above is SPEND: the sum over every turn (38.4x the occupancy
# at the median, 197.7x at the extreme). Occupancy is how full the agent's
# context window is RIGHT NOW: `input_tokens + cache_creation_input_tokens +
# cache_read_input_tokens` of the LAST qualifying assistant record — the same
# arithmetic the documented statusline uses, so the card and the user's own
# status line agree digit-for-digit. `output_tokens` is excluded; nothing is
# added for overhead (the row already carries the whole wire prompt).
#
# It is derived as a BY-PRODUCT of the incremental walk below — zero new reads,
# zero new globs, zero new timers — and it is NON-monotonic: a compaction
# legitimately lowers it, so the D7 monotone clamp must never touch it.
#
# This map is deliberately OUTSIDE _USAGE_CACHE: flush_agent_tokens() evicts
# the parse caches BEFORE its emit, so a reading kept in there would be gone by
# the time the terminal line is written (WATCHER-EMIT-3). It is popped at the
# end of flush_agent_tokens() instead — every caller of that is a point where
# the agent stops being ticked.
#   {agent_id: {"used": int, "at": iso, "model": str|None, "peak": int,
#               "src": "compact" (only on the GD-LC-3 branch)}}
_LAST_CONTEXT: dict[str, dict] = {}
# Named counters for the two branches that are COLD on today's corpus, so the
# first wild occurrence is visible instead of silent (GD-LC-2).
_CTX_COUNTS: dict[str, int] = {"agent_id_mismatch": 0, "iterations_multi": 0}
_CTX_PATH_ID = re.compile(r"^agent-(.+)\.jsonl$")


def _ctx_count(name: str, message: str) -> None:
    """Count a cold branch — and SAY SO on the first hit.

    A counter nobody reads is not visibility. Both of these branches measure 0
    over the whole frozen corpus, so the first wild occurrence is precisely the
    thing an operator has to be told about. Once per process, on stderr, never
    an event: zero new event lines is the rule (GD-LC-5).
    """
    _CTX_COUNTS[name] += 1
    if _CTX_COUNTS[name] == 1:
        print(f"decision_watcher: {message}", file=sys.stderr, flush=True)


def _agent_id_from_path(path: str) -> str:
    """The agentId a transcript path ADDRESSES, or "" when it is not one.

    GD-LC-2 verifies the record's own `agentId` against this, so a foreign row
    can never be attributed to the agent whose file it sits in.
    """
    m = _CTX_PATH_ID.match(os.path.basename(path))
    return m.group(1) if m else ""


def _agent_id_ok(d: dict, path_agent: str) -> bool:
    """GD-LC-2's identity verify: a MISMATCH is dropped and counted.

    A record carrying no `agentId` at all is unverifiable, not mismatched, and
    is accepted: it lives in the file the glob resolved FROM an agentId. (0
    records of either kind in the 1,769-record frozen corpus — this is a guard
    against a future shape, not a live filter.)
    """
    if not path_agent:
        return True
    recorded = d.get("agentId")
    if recorded is None:
        return True
    if recorded == path_agent:
        return True
    _ctx_count("agent_id_mismatch",
               f"agent-{path_agent}.jsonl carries a record stamped agentId "
               f"{recorded!r}; dropped from the context reading, never "
               "attributed (GD-LC-2)")
    return False


def _prompt_total(u: dict) -> int | None:
    """GD-LC-1's three-component prompt sum, or None when the row does not say.

    `type(v) is int`, never `isinstance` and never `v or 0`: `bool` is an `int`
    SUBCLASS, so `isinstance` reads `cache_creation_input_tokens: true` as 1,
    and `or 0` silently reads a `null` as zero. Both produce a positive,
    plausible-looking number out of bytes that say nothing — the R-58 defect
    class in miniature. `tests/fixtures/context/ctx-agent-no-usable-turn.jsonl`
    carries a float, a null and a bool for exactly this arm.
    """
    total = 0
    for field in ("input_tokens", "cache_creation_input_tokens",
                  "cache_read_input_tokens"):
        value = u.get(field)
        if type(value) is not int:
            return None
        total += value
    # A zero prompt is not an occupancy: 30 of 649 real transcripts end on an
    # all-zero `<synthetic>` row, and `ctx 0` on a killed agent's card is a
    # fabrication, not a reading (GD-LC-12).
    return total if total > 0 else None


def _note_context(cached: dict, d: dict, path_agent: str, lineno: int) -> None:
    """Retain this record's occupancy candidates in the path's parse memo.

    Two candidates per path, both keyed by ``(timestamp, line number)`` so the
    fold in _fold_context() can order them across fragments:

    * ``ctx_row`` — the NEWEST qualifying assistant row (GD-LC-2: `type ==
      "assistant"`, `message.id` matching `^msg_`, prompt total > 0, model
      != "<synthetic>", agentId verified). `usage.iterations` is read at the
      TOP level when absent or of length 1 (all 7,256 sampled rows behave that
      way); a `len > 1` list reads `iterations[-1]`, which is unambiguously ONE
      API call's prompt whichever way the top level aggregates — the fixture's
      top level is the SUM of three iterations, a prompt that never existed.
    * ``ctx_boundary`` — the newest `compact_boundary` and its `postTokens`.
      `preTokens` is never mixed into GD-LC-1's arithmetic: it is a different
      estimator (a measured 30-token gap on real bytes). Any `trigger` value.

    ``ctx_peak`` is the running max over qualifying rows of this path, which is
    what makes `peak` recomputable by a full re-walk after a restart.
    """
    ts = d.get("timestamp")
    if not isinstance(ts, str) or not ts:
        return
    if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
        post = (d.get("compactMetadata") or {}).get("postTokens")
        if type(post) is int and post > 0 and _agent_id_ok(d, path_agent):
            if (cached["ctx_boundary"] is None
                    or (ts, lineno) > cached["ctx_boundary"][:2]):
                cached["ctx_boundary"] = (ts, lineno, post)
        return
    if d.get("type") != "assistant":
        return
    m = d.get("message") or {}
    mid = m.get("id")
    if not isinstance(mid, str) or not mid.startswith("msg_"):
        return
    model = m.get("model")
    if model == "<synthetic>":
        return
    usage = m.get("usage")
    if not isinstance(usage, dict) or not _agent_id_ok(d, path_agent):
        return
    iterations = usage.get("iterations")
    if isinstance(iterations, list) and len(iterations) > 1:
        _ctx_count("iterations_multi",
                   f"usage.iterations of length {len(iterations)} seen "
                   "(0 in the measured corpus); reading iterations[-1] as the "
                   "prompt, which is unambiguously one API call (GD-LC-2)")
        if not isinstance(iterations[-1], dict):
            return
        usage = iterations[-1]
    used = _prompt_total(usage)
    if used is None:
        return
    if cached["ctx_row"] is None or (ts, lineno) > cached["ctx_row"][:2]:
        cached["ctx_row"] = (ts, lineno, used, model if isinstance(model, str) else None)
    if used > cached["ctx_peak"]:
        cached["ctx_peak"] = used


def _fold_context(agent_id: str, paths: list[str]) -> None:
    """Fold every fragment's candidates into ``_LAST_CONTEXT[agent_id]``.

    The reading is the qualifying row with the greatest ``(record timestamp,
    path order, line number)`` across the union of the agent's transcript
    fragments — NOT `max` over turns. `max` is WRONG here: it coincides with
    `latest` on 100 % of the current corpus and is silently wrong forever after
    the first compaction, because occupancy GOES DOWN there. See
    `tests/fixtures/context/ctx-agent-compaction-boundary.jsonl`, where `max`
    reads 120,000 and the truth is 18,000 — the next reader's instinct will be
    `max`.

    GD-LC-3: when the newest `compact_boundary` outranks the newest qualifying
    row, the reading is `compactMetadata.postTokens` stamped with the
    BOUNDARY's own timestamp and labelled `src: "compact"` — no usage row lands
    until the next API call, so a naive last-row reader overstates 19x for the
    whole gap. The model is carried from the agent's newest qualifying row: the
    boundary record carries none, and dropping it would take `cap` away from a
    perfectly healthy agent for the length of that gap.

    Nothing is cleared when a fragment goes unreadable: GD-LC-12 says the
    previously emitted reading STANDS, visibly aged by its own `at`, and a
    stale value is never re-stamped.
    """
    row = boundary = None
    peak = 0
    for order, path in enumerate(paths):
        cached = _USAGE_CACHE.get(path)
        if not cached:
            continue
        candidate = cached.get("ctx_row")
        if candidate and (row is None or (candidate[0], order, candidate[1]) > row[:3]):
            row = (candidate[0], order, candidate[1], candidate[2], candidate[3])
        edge = cached.get("ctx_boundary")
        if edge and (boundary is None or (edge[0], order, edge[1]) > boundary[:3]):
            boundary = (edge[0], order, edge[1], edge[2])
        peak = max(peak, cached.get("ctx_peak") or 0)
    if row is None and boundary is None:
        return
    previous = _LAST_CONTEXT.get(agent_id) or {}
    if boundary is not None and (row is None or boundary[:3] > row[:3]):
        used, at, src = boundary[3], boundary[0], "compact"
        model = row[4] if row else None
    else:
        used, at, src = row[3], row[0], None
        model = row[4]
    # `peak` is the one sanctioned aggregate and IS monotone (unlike `used`):
    # a compaction must not lower the high-water mark it just dropped from.
    reading = {"used": used, "at": at, "model": model,
               "peak": max(peak, used, previous.get("peak", 0))}
    if src:
        reading["src"] = src
    _LAST_CONTEXT[agent_id] = reading


def _transcript_usage(path: str) -> dict[str, tuple[int, int, int, int]]:
    """Usage rows of ONE transcript copy, parsed incrementally (WRITE-SIDE-10).

    Re-parses from byte 0 when the file shrinks past the stored OFFSET or its
    inode changes — the stored offset is meaningless against different bytes,
    exactly the rule (and the same comparison) the journal tailer applies to its
    own checkpoint. A torn trailing line (the harness is appending while we
    read) is never consumed: the offset advances only past the last ``\\n``, so
    the partial line is re-read when it completes.
    """
    try:
        st = os.stat(path)
    except OSError:
        return {}
    ident = f"{st.st_dev}:{st.st_ino}"
    cached = _USAGE_CACHE.get(path)
    # Compare against the OFFSET, not a separately tracked size: the file can
    # grow between the stat() and the read(), so a stored size would sit BELOW
    # the offset we actually consumed, and a later genuine truncation to a point
    # between the two would slip past the guard, seek beyond EOF and freeze this
    # transcript's totals until it grew back. The offset is the only number that
    # says how many of these bytes we have already believed.
    if cached is None or cached["ident"] != ident or st.st_size < cached["offset"]:
        # ctx_row / ctx_peak / ctx_boundary are the occupancy by-products
        # (GD-LC-2/3): they are rebuilt from byte 0 by exactly the same rule
        # that rebuilds `usage`, which is what makes `peak` recomputable from a
        # full re-walk after a restart.
        cached = {"ident": ident, "offset": 0, "lines": 0, "usage": {},
                  "ctx_row": None, "ctx_peak": 0, "ctx_boundary": None}
        _USAGE_CACHE[path] = cached
    if st.st_size == cached["offset"]:  # nothing new since the last read
        return cached["usage"]
    try:
        with open(path, "rb") as f:
            f.seek(cached["offset"])
            chunk = f.read()
    except OSError:
        return cached["usage"]
    cut = chunk.rfind(b"\n")
    if cut == -1:  # no complete line yet; leave the offset where it is
        return cached["usage"]
    path_agent = _agent_id_from_path(path)
    # split(b"\n") on the BYTES, decoding per line — never str.splitlines(),
    # which also splits on \x0b \x0c \x1c-\x1e \x85 and U+2028/U+2029. Those last
    # two are legal UNESCAPED inside a JSON string (JSON.stringify does not
    # escape them), so an assistant message merely containing a line separator
    # would be torn into two fragments, both failing json.loads, both skipped —
    # and because the offset has already advanced past them the billed row would
    # be dropped from this agent's total FOREVER. Slicing at ``cut + 1`` means
    # the last element is always the empty tail after the final newline.
    for raw in chunk[:cut + 1].split(b"\n"):
        if not raw:  # the tail after the final \n (and any blank line)
            continue
        lineno = cached["lines"]
        cached["lines"] += 1
        try:
            d = json.loads(raw.decode(errors="replace"))
        except json.JSONDecodeError:
            continue
        # Occupancy rides THIS walk (GD-LC-5): same bytes, same pass, no second
        # read. It is noted before the assistant filter because a
        # `compact_boundary` is a `system` record.
        _note_context(cached, d, path_agent, lineno)
        if d.get("type") != "assistant":
            continue
        m = d.get("message") or {}
        u = m.get("usage")
        if u:
            # Fall back to a stable per-row key (path+line) when the entry
            # carries neither message.id nor uuid, so multiple id-less usage
            # rows are summed rather than collapsing to a single "" key
            # (WATCHER-8). The line counter is per-path, monotonic across
            # incremental reads and counts only NON-EMPTY lines, so a row keeps
            # its key as the file grows however the reads happen to be chunked.
            key = m.get("id") or d.get("uuid")
            if not key:
                key = f"\0noid\0{path}\0{lineno}"
            cached["usage"][key] = _usage_totals(u)
    cached["offset"] += cut + 1
    return cached["usage"]


def agent_tokens(agent_id: str) -> tuple[int, int, int, int]:
    """Sum (input, cache-read, cache-write, output) tokens across an agent's API calls, deduped by message id.

    ``input`` is the TOTAL input volume (fresh + cache writes + cache reads).
    Cache reads/writes are broken out separately because an agent loop
    re-sends its whole conversation prefix every turn — cache reads dominate
    the input sum (and cost ~10x less than fresh input), so displays show
    the r:/w: breakdown to keep the big number interpretable.
    """
    # A /clear- or /compact-split transcript yields several copies; the
    # message-id key unions them safely (overlapping messages collapse), and
    # agent_paths() returns them oldest-first so a newer copy's row wins.
    usage_by_msg: dict[str, tuple[int, int, int, int]] = {}
    paths = agent_paths(agent_id)
    for path in paths:
        usage_by_msg.update(_transcript_usage(path))
    # The occupancy fold is the by-product of the same walk (GD-LC-5): this
    # funnel is where every read path meets, so the reading is refreshed
    # wherever a total is, and nowhere else.
    _fold_context(agent_id, paths)
    tin = tcached = twrite = tout = 0
    for u_in, u_cached, u_write, u_out in usage_by_msg.values():
        tin += u_in
        tcached += u_cached
        twrite += u_write
        tout += u_out
    return tin, tcached, twrite, tout


def token_totals(agent_id: str) -> tuple[int, int, int, int] | None:
    """An agent's cumulative usage, or None when ``--no-tokens`` is in force (D-05).

    None is emphatically NOT zero: a suppressed reading must leave the `agent`
    block's `tokens` key ABSENT, because a rendered 0 reads as "this agent
    burned nothing" on every dashboard that folds `agent.tokens` last-wins.
    It also skips the transcript parse, which is the only reason suppressing
    the events is worth anything.
    """
    return None if NO_TOKENS else agent_tokens(agent_id)


def tokens_field(totals: tuple[int, int, int, int] | None) -> dict | None:
    """The `agent.tokens` sub-object for a reading, or None for a suppressed one."""
    if totals is None:
        return None
    tin, tcached, twrite, tout = totals
    return {"in": tin, "out": tout, "cached": tcached, "cache_write": twrite}


# One warning per (model, cap) for a contradicted window — a daemon that
# re-warned every tick would bury the run's real output.
_CTX_CAP_WARNED: set[tuple[str, int]] = set()


def ctx_field(agent_id: str) -> dict | None:
    """The `agent.ctx` sub-object for this agent's reading — or None.

    None means the key is ABSENT on the wire, which is the whole discipline
    (GD-LC-4/12): never `0`, never `null`. Every unknown resolves here —
    spawned but no assistant turn yet, only `<synthetic>` rows, `--no-tokens`,
    a pruned or unreadable transcript, no fragment yielding a qualifying row.
    A fabricated number on a card is worse than showing nothing.

    The wire shape is GD-LC-4 exactly: `used` and `at` required, `model` when
    recorded, `peak` whenever the block is present, `cap` only when DECLARED
    and not contradicted, `src` only on the compaction branch. `at` is the
    SOURCE record's own timestamp, never the emit moment, so a stale reading
    ages visibly instead of being re-stamped. No percentage travels — that is
    client-derivable, and derivables do not ship.
    """
    if NO_TOKENS:
        # One switch, one read, no third state: context is a by-product of
        # exactly the transcript parse D-05 suppresses (GD-LC-12).
        return None
    reading = _LAST_CONTEXT.get(agent_id)
    if not reading:
        return None
    used = reading.get("used")
    if type(used) is not int or used <= 0:
        return None
    block = {"used": used, "at": reading["at"]}
    model = reading.get("model")
    if model:
        block["model"] = model
    block["peak"] = reading.get("peak") or used
    cap = context_cap(model)
    if cap is not None:
        if used > cap:
            # GD-LC-6.3: a contradicted window is OMITTED, never clamped and
            # never rendered as a >100 % bar. The reading is the measurement;
            # the declaration is the guess, so the declaration loses.
            key = (model or "", cap)
            if key not in _CTX_CAP_WARNED:
                _CTX_CAP_WARNED.add(key)
                print(f"decision_watcher: context reading {used} exceeds the "
                      f"declared window {cap} for model {model or 'unknown'}; "
                      "omitting cap (check context_window / "
                      "ORCH_CONTEXT_WINDOW)", file=sys.stderr, flush=True)
        else:
            block["cap"] = cap
    if reading.get("src"):
        block["src"] = reading["src"]
    return block


def ctx_detail(ctx: dict | None) -> str:
    """The ` · ctx 144.0k` / ` · ctx 144.0k/1000.0k` detail suffix, or "".

    GD-11 at the writer: single line, no double quotes, tiny. No percentage,
    and the clause is omitted ENTIRELY when there is no reading — never
    `ctx 0`, never `ctx ?`. The number travels in `agent.ctx`, not here; this
    is display text, the same way `fmt_in` already writes the spend.
    """
    if not ctx:
        return ""
    cap = ctx.get("cap")
    used = fmt_tokens(ctx["used"])
    return f" · ctx {used}/{fmt_tokens(cap)}" if cap else f" · ctx {used}"


def token_deltas(prev: dict, tin: int, tcached: int, twrite: int,
                 tout: int) -> tuple[dict, dict]:
    """``(wire deltas, new baseline)`` under the D7 monotonic rule.

    Deltas are clamped >= 0 and a stored baseline is never lowered, so a
    transiently unreadable or pruned transcript copy can't regress a counter.
    The baseline is what makes the cadence ceiling structurally lossless: a
    skipped emit leaves it where it was, so the NEXT emit — later tick, result
    rollup, stale close or exit sweep — carries the whole accumulated delta.
    No pending-delta accumulator exists, and none may be added (a simulated one
    lost 117k tokens on one plan of the measured corpus).
    """
    deltas = {"in": max(0, tin - prev.get("in", 0)),
              "out": max(0, tout - prev.get("out", 0)),
              "cached": max(0, tcached - prev.get("cached", 0)),
              "cache_write": max(0, twrite - prev.get("cache_write", 0))}
    base = {"in": max(prev.get("in", 0), tin),
            "out": max(prev.get("out", 0), tout),
            "cached": max(prev.get("cached", 0), tcached),
            "cache_write": max(prev.get("cache_write", 0), twrite)}
    return deltas, base


def token_tick_due(agent_id: str, now: float, tok_tick_at: dict,
                   secs: int | None = None) -> bool:
    """Is this agent's LIVE token tick due? (GD-D, the cadence ceiling)

    Consulted only by the poll-tick path — every force-flush path
    (flush_agent_tokens) ignores it by construction. What the window gates is
    the transcript READ, not just the emit: an ungated read is what makes the
    watcher O(transcript bytes x running agents) PER SECOND, and gating it on
    the same window is where WRITE-SIDE-10's ~93% parsing cut comes from.

    Exactly ONE exemption, the one M1 specifies: an absent window key is DUE —
    an agent seen for the first time, or a checkpoint written before this knob
    existed. That is what makes a freshly spawned row light up within a poll
    tick instead of after a whole ceiling, and it is bounded: the read that
    serves it stamps the window, so it can happen once per agent. A broader
    "has never published a counter" exemption was considered and rejected — an
    agent running with no billable activity would then be re-read every second
    until ABANDON_QUIET_SECS (1200 s by default), ~1200 globs where the cadence
    budgets ~80, which is precisely the cost WRITE-SIDE-10 exists to remove.

    A clock that stepped backwards is due rather than frozen for the difference.

    ``secs`` defaults to the live TOKEN_TICK_SECS global (which refresh_caps()
    moves mid-run, so the poll loop must keep reading it at CALL time) and is
    otherwise the ceiling to apply — the whole rule then takes its inputs as
    arguments, which is what lets a caller (or a test arm) ask the question
    without mutating module state other code is reading.
    """
    if secs is None:
        secs = TOKEN_TICK_SECS
    if secs <= 0:
        return True
    last = tok_tick_at.get(agent_id)
    if last is None or now < last:
        return True
    return (now - last) >= secs


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


def _last_ts_in_file(path: str, types: tuple[str, ...] | None = None) -> str | None:
    """Latest parseable ``timestamp`` in one transcript file.

    Grows the tail window until at least one full line is captured, so a final
    transcript line larger than the initial window (a >64KB tool result — the
    real case commit 0586bbbf shows) still yields the true last timestamp
    instead of an older one or ``None`` (WATCHER-7).

    ``types`` narrows the search to records of those ``type``s — D-06 uses
    ``("assistant",)`` to ask for the moment the agent last SPOKE rather than
    the moment its file was last appended to. The window growth is what keeps
    that honest: a filtered search that found nothing in the tail keeps
    doubling to the head of the file rather than reporting the newest record it
    happened to see.
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
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if types is not None and record.get("type") not in types:
                continue
            ts = record.get("timestamp")
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


def last_assistant_ts(agent_id: str) -> str | None:
    """When the agent last SPOKE: latest ``assistant`` timestamp across copies.

    The moment an agent's final assistant message was written is the closest
    recorded thing to "the agent finished" — every later line in the transcript
    (tool results, hook output, harness scaffolding) is bookkeeping about a turn
    that had already ended. D-06 prefers it over the read moment for exactly
    that reason.
    """
    stamps = []
    for path in agent_paths(agent_id):
        ts = _last_ts_in_file(path, ("assistant",))
        if ts:
            stamps.append(ts)
    return max(stamps) if stamps else None


# How fresh a recorded stamp must be to be believed as the real completion time
# of an agent whose result JUST landed. Unchanged value, named so the two
# candidates below are visibly held to the SAME guard.
RESULT_TS_FRESH_SECS = 30


def result_ts(agent_id: str, live: bool) -> str | None:
    """Completion timestamp for an agent whose journal ``result`` just landed.

    Journal entries carry no timestamps, and a transcript can stop flushing
    mid-run — a long final Bash call leaves the tool result and everything
    after it unwritten, so the transcript's last line may predate the real
    finish by many minutes. When tailing live (the entry appeared since the
    previous ~1s poll) the read moment is the fallback completion time; a
    recorded stamp is trusted only when it is fresh enough to be the real end.
    On backlog catch-up the transcript is the only signal we have.

    D-06 narrows the +29 s p90 tail this used to leave: the agent's own LAST
    ASSISTANT MESSAGE is preferred over the read moment, and only then the
    transcript's last line of any kind. Both candidates pass the SAME staleness
    guard, so nothing is invented — each is a stamp the harness itself wrote,
    and the read moment survives as the answer for the case it was chosen for
    (a transcript that stopped flushing). The residual trade — a result whose
    transcript went quiet still carries the watcher's read moment, not the true
    end — is stated in monitoring.md next to "Timestamps are true occurrence
    times", because a stamp that is honest about being observed beats a stamp
    that is confidently wrong.
    """
    t_tr = last_ts(agent_id)
    if not live:
        return t_tr
    now = datetime.now(timezone.utc)
    for candidate in (last_assistant_ts(agent_id), t_tr):
        if not candidate:
            continue
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - parsed).total_seconds() <= RESULT_TS_FRESH_SECS:
            return candidate
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
        if not fields:
            # Payload-less mention — prose quoting the token (e.g. a sub-plan
            # title naming "[monitor]"), not a marker; it must not clobber a
            # real marker's fields. Same rule touch_marker_misplaced applies
            # below the window.
            continue
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


def flush_agent_tokens(state: dict, agent_id: str, info: dict | None = None,
                       ts: str | None = None, row_state: str | None = None,
                       totals: tuple[int, int, int, int] | None = None,
                       force: bool = False) -> tuple[int, int, int, int]:
    """UNTHROTTLED token flush for one agent; returns its cumulative totals.

    The one force-flush path (GD-D / WRITE-SIDE-3+4+5), shared by every site
    where an agent is about to stop being ticked: the result rollup, the
    unclassified agent's result (it is ticked too — GD-7 gives it a node), the
    two stale closes, and the exit sweep. It reads the transcript once (or takes a
    reading the caller already made), emits the ``stage:"tokens"`` delta against
    ``tok_emitted``, advances that baseline, and states the agent's CUMULATIVE
    total in the line's ``agent`` block.

    Both halves matter. Without the delta line the agent's last accrual reaches
    no counter at all — deltas are wire-only, so nothing self-heals on replay.
    Without the cumulative the line cannot be folded: a replay that drops quiet
    ticks (every snapshot/prelude design) must reconstruct totals from
    ``agent.tokens`` last-wins, never by summing surviving deltas — on the
    measured corpus the surviving deltas are 1.9% of the truth. 15 of 167 agents
    and 9.14% of that run's input tokens lived only inside quiet ticks before
    this existed.

    ``force`` emits even a zero delta (the result rollup's closing statement,
    which must land whether or not anything accrued since the last tick); every
    other caller stays silent when there is nothing to report. All of it is
    schema-ADDITIVE — ``agent`` is already documented as optional on any event
    and readers ignore keys they don't know.

    ``--no-tokens`` (D-05) turns the whole body into the evictions: no
    transcript is read and no `tokens` event is written, so spawns, results and
    decision lines are untouched while `ingest.rollup` becomes the single
    reachable token implementation. The evictions still happen because every
    caller is still a point where the agent stops being ticked.

    The line also carries the agent's CONTEXT OCCUPANCY when there is one
    (GD-LC-4/5) — absolute, non-monotonic, last-event-wins, and simply ABSENT
    when unknown. It rides this existing line: zero new event kinds and zero
    new event lines, because the positional `legacy:<task>#<line>` key space
    downstream makes a twin-line design a permanent +91 % on the stream.
    """
    if NO_TOKENS:
        drop_usage_cache(agent_id)
        _LAST_CONTEXT.pop(agent_id, None)
        state.setdefault("tok_tick_at", {}).pop(agent_id, None)
        return totals if totals is not None else (0, 0, 0, 0)
    if totals is None:
        totals = agent_tokens(agent_id)
    # Every caller is a point where this agent stops being ticked, so BOTH of
    # its per-agent maps are dead weight from here on: the per-transcript parse
    # caches (the reading above was their last reader) and the cadence window,
    # whose only consumer is token_tick_due() and which is only ever asked about
    # agents in `running`. Keeping the window would double the per-agent
    # footprint of the checkpoint for the life of the run, with nothing ever
    # reading it — tok_emitted persists because it is the D7 baseline; this map
    # has no such reason. Both are dropped BEFORE the zero-delta return, because
    # "nothing accrued" is just as terminal as "something did". If a truncation
    # rebuild ever puts the agent back in `running`, the absent window reads as
    # DUE and costs one read — the safe direction, and the same first-tick rule
    # every new agent gets.
    drop_usage_cache(agent_id)
    state.setdefault("tok_tick_at", {}).pop(agent_id, None)
    tin, tcached, twrite, tout = totals
    prev = state["tok_emitted"].get(agent_id, {})
    deltas, base = token_deltas(prev, tin, tcached, twrite, tout)
    # Read BEFORE either exit pops it: drop_usage_cache() above has already
    # taken the per-path parse memos away, and _LAST_CONTEXT is the map that
    # survives that eviction so this terminal line can still state the level
    # (WATCHER-EMIT-3). The reading is ABSOLUTE — the deltas beside it are not
    # its business, and the D7 clamp must never touch it.
    ctx = ctx_field(agent_id)
    if not force and not any(deltas.values()):
        _LAST_CONTEXT.pop(agent_id, None)
        return totals
    if info is None:
        info = state["agents"].get(agent_id)
    # agent_label() (stage-qualified, R-13) rather than the bare `role #attempt`
    # this line used before the helper existed: six parallel researchers on one
    # plan differ ONLY by stage, and the live tick line next to it has always
    # been labelled this way. `detail` is free text, single-line and inside the
    # 1 KB writer cap, so the change is display-only.
    emit("tokens", "info",
         f"{agent_label(info, agent_id)} used "
         f"{fmt_in(base['in'], base['cached'], base['cache_write'])} · "
         f"out {fmt_tokens(base['out'])} total" + ctx_detail(ctx),
         ts=ts, plan=info["plan"] if info else "orchestrator",
         extra={"tokens": deltas,
                "agent": agent_block(agent_id, info, row_state,
                                     tokens=dict(base), ctx=ctx)})
    # Never clear tok_emitted itself — the truncation branch in main()
    # documents why. (The cadence WINDOW was dropped above: this agent has
    # stopped being ticked, so there is nothing left to throttle.)
    state["tok_emitted"][agent_id] = base
    # Popped LAST, on both exits: the line above is this agent's final word on
    # its occupancy, and a reading left behind would be re-attached to whatever
    # a later pass emits for an agent that has stopped being read (GD-LC-5).
    _LAST_CONTEXT.pop(agent_id, None)
    return totals


def sweep_running_tokens(state: dict) -> None:
    """Final unthrottled flush for every agent still in flight, before stopping.

    The residual hole in a cadence CEILING is an agent that never emits again:
    the watcher stops (drain or either self-exit) while the agent is mid-flight,
    so everything it accrued since its last tick — its ENTIRE usage if it never
    ticked — would live nowhere in the append-only record. Cheap: at most one
    transcript read per running agent, once, on the way out.
    """
    for agent_id in list(state.get("running") or []):
        flush_agent_tokens(state, agent_id, state["agents"].get(agent_id))


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
    SEED lines the monitor recipe writes before launch: seeding after a
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
    folder (one folder hosts research, then implement) from ever reading as
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
    hosts several phases (research, then implement) appending to one
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
    startup baseline.

    Who writes that line, since GD-D6: normally the DETERMINISTIC close (rungs
    1-2 of :func:`poll_run_close`), which appends it by RUNNING ``status.sh``
    precisely so this predicate needs no new case; optionally a driver typing
    it as belt-and-braces (rung 3). Not the templates' ``closeRun`` — it emits
    nothing at all, because the Workflow runtime has no Node API and every such
    call silently no-ops (D-10). The watcher's own inference never stops it
    either; see ABANDON_QUIET_SECS for the killed-session case.
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
    """(stage, state, detail) for a finished agent, carrying its own summary (D-06).

    The DERIVED verdict comes first and is byte-identical to what this module
    has always written — it is what the loop decided, and it is the
    deterministic half. A ``summary`` the agent's structured output actually
    carried is APPENDED to it, single-lined and quote-stripped, inside the
    existing 1 KB writer cap.

    Why it belongs here: the agent's own account of what it did is the ONE
    thing the journal records that the marker cannot derive, and it is exactly
    what the mandated LAST `touch-status` line was carrying. Preserving it on
    the derived line is what makes deleting that mandate information-neutral —
    D-09 depends on this item, not the other way round (GD-D3: the deletion is
    a correctness item, never a token-reduction one).
    """
    stage, state, detail = _result_decision(info, result)
    summary = ""
    if isinstance(result, dict) and isinstance(result.get("summary"), str):
        summary = single_line(result["summary"])
    return stage, state, f"{detail} — {summary}" if summary else detail


def _result_decision(info: dict, result) -> tuple[str, str, str]:
    """The derived decision line, without the agent's own summary (see above).

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
            # D-06: the implementer's own summary was already preferred here;
            # it is now single-lined and quote-stripped like every other string
            # this module did not author.
            return "done", single_line(r.get("summary")) or f"{len(files)} changed files"
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


# --------------------------------------------------------------------------
# GD-D6 — THE LAYERED RUN CLOSE (D-07), plus the harness's own run stats, the
# real per-agent death causes and the recovery command (D-08), and the post-run
# reconcile against the snapshot (D-16).
#
# Four rungs, FIRST TO FIRE WINS, the rung recorded in the event's detail:
#
#   1. the run SNAPSHOT `<session>/workflows/<runId>.json` — written at the
#      second the run ends; the authoritative status vocabulary
#      (`completed|failed|killed`) plus the harness's own totals;
#   2. the driver session's `<task-notification>` — `<summary>`, `<failures>`,
#      `<recovery>`, `<usage>`;
#   3. a driver-typed `touch-status orchestrator complete` — retained as
#      redundant belt-and-braces, DEMOTED from MUST;
#   4. the existing EXIT_QUIET_SECS / ABANDON_QUIET_SECS timeouts.
#
# Only rungs 1 and 2 are EMITTED here. Rung 3 is recognised but never written —
# a driver's typed close is already the line route 1 of the exit protocol waits
# for, so once it has landed the ladder has fired and this module must not add
# a second terminal event beside it. Rung 4 is the loop's own window: unmoved.
# The emitter writes THROUGH status.sh so the close keeps `w:"agent"` and route
# 1's predicate is untouched (GD-D5) — the point of GD-D6 is that the close
# stops being something a driver has to remember to type, not that the watcher
# starts forging attributions. `killed` NEVER renders `done` (R-58 discipline
# applied to the run close), and the rule that the watcher's own INFERENCE
# cannot authorize an exit is NOT relaxed: a rung is recorded evidence written
# by the harness, which is a different thing from a guess about quiet.
#
# Neither source is guaranteed and that is designed for, not worked around:
# ~7% of runs never get a snapshot (2 of 28 measured), one recorded run has a
# snapshot and no journal at all, and `<failures>` appears in 2 of 28
# notifications. Absence is normal on every path below — never an error, never
# a warning, and never a reason to weaken the timeout rungs.
# --------------------------------------------------------------------------

#: The one write path into events.jsonl for lines that must read as `w:"agent"`.
STATUS_SH = os.path.join(ROOT, "status.sh")
#: Rung names, as they appear in the close event's detail (rung 3 is a driver's
#: own typed close: never emitted here, only RECOGNISED, so that "first rung
#: wins" holds across all three landable rungs and not just the two polled).
CLOSE_RUNGS = {1: "run snapshot", 2: "task notification", 3: "driver close"}
#: How often the two deterministic rungs are polled, in seconds. The journal
#: tail keeps its ~1 s cadence; this is a cheaper question asked slightly less
#: often (a handful of stats plus O(bytes appended) of the driver session).
CLOSE_POLL_SECS = max(1, _int_env("ORCH_CLOSE_POLL_SECS", 2))
#: How far back the FIRST pass over a driver session file reads. A session
#: transcript is unbounded (tens of MB), and for any run this watcher could
#: still close, its launch record and its notification are near the end. A
#: bounded head start is the difference between one cheap scan and re-reading a
#: conversation; every later pass is incremental from the stored offset.
SESSION_SCAN_MAX_BYTES = _int_env("ORCH_SESSION_SCAN_BYTES", 8 * 1024 * 1024)
#: How long the session-dir glob is cached. Rotation (a `/clear` mid-run) adds
#: a dir; nothing removes one, so a minute-stale answer costs nothing.
SESSION_DIRS_TTL = 60.0
#: `record.origin.kind` of the run-close notification. Matched on the KIND,
#: never on the literal `<task-notification>` tag: that tag is the GENERIC
#: background-task block (a polling `sleep` completing carries it too), so a
#: substring test admits foreign tasks, while the kind is 35/35 with zero false
#: positives (JSONL-EXTRACT-5).
NOTIFY_KIND = "task-notification"
#: The `<usage>` counters, in the order they are narrated.
USAGE_TAGS = ("agent_count", "agents_done", "agents_error", "agents_skipped",
              "agents_empty_result", "subagent_tokens", "tool_uses", "duration_ms")
#: The blocks parsed out of a notification body.
NOTIFY_TAGS = ("task-id", "status", "summary", "recovery", "failures", "usage")
#: `plan/RESUME.md`'s recovery section, delimited so the rewrite can never
#: touch a byte a human wrote around it (D-08c).
RESUME_BEGIN = "<!-- touch:recovery -->"
RESUME_END = "<!-- touch:recovery:end -->"
#: How much `<recovery>` text is spliced into RESUME.md. The recorded blocks are
#: one `Workflow({…})` call (hundreds of bytes); a body past this is not a
#: resume command, and an unbounded splice into a file humans read is how a
#: display surface becomes a payload surface.
RECOVERY_MAX_CHARS = _int_env("ORCH_RECOVERY_MAX_CHARS", 4096)
#: Env vars `status.sh` folds into the line it writes. The deterministic close
#: declares none of them, so they are dropped from the child rather than
#: inherited from whatever shell started the watcher (see emit_through_status).
STATUS_ENV_DROP = ("ORCH_TITLE", "ORCH_PLANS_TOTAL", "ORCH_ROSTER")
#: Stages that SET A CARD'S BADGE (monitoring.md §event schema): `plan` is the
#: plan lifecycle and `complete` is its alias on the orchestrator card. A
#: derived annotation — a death cause, a reconcile correction — is never
#: allowed to land on one: the stage of an agent row is `role.split(":")[-1]`,
#: i.e. arbitrary text out of a harness label, and a role that happened to end
#: in `:plan:` would otherwise close a card the loop never closed. That is the
#: fabricated badge R-58 forbids, arriving through a new door.
RESERVED_STAGES = ("plan", "complete")

_SESSION_DIRS: tuple = ()
_SESSION_DIRS_AT = 0.0
#: path -> {"ident", "offset", "partial"} — the incremental driver-session tail.
_SESSION_SCAN: dict[str, dict] = {}
#: The newest launch record naming THIS runId: {"task_id", "ts"}. Last-wins by
#: RECORD timestamp, not by scan order: a resumed run re-uses its runId, so an
#: older launch for the same run sits in the same (or another) session file and
#: must never win over the resume that is actually running now.
_LAUNCH: dict = {}
#: task-id -> parsed notification blocks (bounded; a session holds few).
_NOTIFICATIONS: dict[str, dict] = {}
_NOTIFICATIONS_CAP = 32


def snapshot_glob() -> list[str]:
    """GD-D6's own glob: `<claude-root>/projects/*/*/workflows/<runId>.json`.

    The plan names this glob literally, and it is the WIDEST of the three
    sources below on purpose: 41% of recorded snapshots land in a different
    session dir than their journal (C-E), and a session that rotated in mid-run
    without recording a single further agent transcript leaves no
    ``subagents/workflows/`` trace to find it by. Uncached — one two-level glob
    per close poll — because rung 1 is snapshot APPEARANCE and a minute-stale
    answer would be a minute-late close.
    """
    return sorted(glob.glob(os.path.join(WF_GLOB_ROOT, "*", "*", "workflows",
                                         WF_NAME + ".json")))


def run_session_dirs(now: float | None = None,
                     ttl: float = SESSION_DIRS_TTL) -> list[str]:
    """Every session dir that can hold this run's snapshot or driver transcript.

    Three sources, all needed. The dir three levels above ``WF_DIR`` is the
    session that LAUNCHED the run and is a candidate whether or not any glob
    matches. The ``subagents/workflows/<runId>`` glob adds the sessions a
    mid-run ``/clear`` rotated into — the same rotation ``agent_paths`` already
    searches for transcripts — because the notification lands in whichever
    session was current when the run ended. :func:`snapshot_glob` adds any
    session that recorded a SNAPSHOT for this run without recording an agent
    transcript, which the second source structurally cannot see; its driver
    transcript is exactly where that run's notification will be.
    """
    global _SESSION_DIRS, _SESSION_DIRS_AT
    now = time.time() if now is None else now
    if _SESSION_DIRS and (now - _SESSION_DIRS_AT) < ttl:
        return list(_SESSION_DIRS)
    dirs: list[str] = []

    def add(path: str) -> None:
        if path and path not in dirs and os.path.isdir(path):
            dirs.append(path)

    add(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.normpath(WF_DIR)))))
    for path in glob.glob(os.path.join(WF_GLOB_ROOT, "*", "*", "subagents",
                                       "workflows", WF_NAME)):
        add(os.path.dirname(os.path.dirname(os.path.dirname(path))))
    for path in snapshot_glob():
        add(os.path.dirname(os.path.dirname(path)))
    _SESSION_DIRS = tuple(dirs)
    _SESSION_DIRS_AT = now
    return list(dirs)


def snapshot_paths() -> list[str]:
    """Where rung 1's `<runId>.json` could be — the session dirs, then the glob.

    The glob is asked EVERY time (it is what finds a snapshot in a session this
    watcher has no other trace of), and the derived candidates stay because a
    session dir is knowable before its snapshot exists — which is what lets
    :func:`snapshot_baseline` scope a resume's already-present copy out.
    """
    paths = [os.path.join(d, "workflows", WF_NAME + ".json")
             for d in run_session_dirs()]
    for path in snapshot_glob():
        if path not in paths:
            paths.append(path)
    return paths


def snapshot_baseline() -> dict:
    """``{path: (size, mtime_ns)}`` for every snapshot copy present RIGHT NOW.

    Rung 1 is snapshot APPEARANCE. A resumed run re-uses its runId, so the
    PREVIOUS run's snapshot is already on disk before the resumed watcher
    starts — this repository resumes constantly — and a rung that fired on mere
    existence would close every resume the moment it began. The baseline scopes
    it exactly the way ``events_baseline`` scopes the exit protocol: only a file
    that was not there, or whose bytes moved, after this watcher started counts.
    """
    out = {}
    for path in snapshot_paths():
        try:
            st = os.stat(path)
        except OSError:
            continue
        out[path] = (st.st_size, st.st_mtime_ns)
    return out


def read_run_snapshot(baseline: dict | None = None) -> dict | None:
    """This run's snapshot, newest copy wins — or None when there is none.

    A resumed run writes ONE SNAPSHOT PER OBSERVING SESSION and they disagree
    (the same defect D-02 fixes on the aggregator side), so the later
    ``timestamp`` wins here rather than whichever path the glob returned first.
    ``baseline`` skips copies that have not changed since the watcher started
    (see :func:`snapshot_baseline`); pass None to read whatever is on disk,
    which is what ``--reconcile`` wants.
    """
    best = None
    for path in snapshot_paths():
        if baseline is not None:
            try:
                st = os.stat(path)
            except OSError:
                continue
            if baseline.get(path) == (st.st_size, st.st_mtime_ns):
                continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                snap = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(snap, dict):
            continue
        if snap.get("runId") not in (None, WF_NAME):
            continue
        if best is None or str(snap.get("timestamp") or "") > str(best.get("timestamp") or ""):
            best = snap
    return best


def snapshot_agents(snap: dict | None) -> list[dict]:
    """The ``workflow_agent`` rows of ``workflowProgress[]`` (phases are not agents).

    ``snapshot["agentCount"] != len(rows)`` is NORMAL, not a discrepancy to
    report: the count is the harness's own tally over a run whose agents may
    have been re-spawned, and the rows are what it kept. Nothing here treats a
    mismatch as an error (SUBSTRATE-10).
    """
    rows = (snap or {}).get("workflowProgress")
    if not isinstance(rows, list):
        return []
    return [r for r in rows
            if isinstance(r, dict) and r.get("type") == "workflow_agent"]


def run_close_state(status) -> str:
    """`completed` -> done; `failed`, `killed` and anything unknown -> failed.

    R-58 discipline applied to the run close. Two rules, both one-way: a
    ``killed`` run must never render as a clean ``done``, and an UNKNOWN status
    word is not a verdict either — it settles ``failed`` with the raw word kept
    in the detail, because inventing green from a vocabulary we do not
    recognise is exactly the fabricated badge R-58 exists to kill.
    """
    return "done" if str(status or "").strip().lower() == "completed" else "failed"


def parse_agent_label(label) -> dict | None:
    """``plan:role:attempt`` (the loop's own label grammar) -> classification fields.

    Both harness suffixes are stripped first: ``~rN`` (its internal re-spawn of
    the same attempt) and `` (retry N)``. A label that does not carry all three
    parts, or whose attempt is not a number, returns None — a half-parsed label
    would name a plan card that does not exist.
    """
    text = single_line(label)
    if not text:
        return None
    text = text.split(" (")[0].split("~")[0].strip()
    parts = text.split(":")
    if len(parts) < 3 or not parts[-1].isdigit():
        return None
    role = ":".join(parts[1:-1])
    return {"plan": parts[0], "role": role, "attempt": int(parts[-1]),
            "stage": role.split(":")[-1]}


def annotation_stage(info: dict | None, fallback: str = "watcher") -> str:
    """The stage a DERIVED annotation may be written under (never a reserved one).

    ``parse_agent_label`` derives ``stage`` from the harness's own label text,
    so it is untrusted: a label like ``sp-a:gate:plan:2`` yields the reserved
    stage ``plan``, and a `failed` line there would set the plan card's badge.
    Cause lines and reconcile corrections describe an AGENT; the badge belongs
    to the loop's own close predicate (GD-10) and to the run close (GD-D6).
    A reserved stage is therefore rewritten to ``agent`` — the annotation still
    names its plan card, its role and its attempt in the detail, it just cannot
    be the thing that closes the card.
    """
    stage = (info or {}).get("stage") if info else None
    stage = str(stage or "").strip()
    if not stage:
        return fallback
    return "agent" if stage in RESERVED_STAGES else stage


def record_text(record: dict) -> str:
    """The text of a session record's message, list-content tolerated."""
    content = (record.get("message") or {}).get("content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "")
                          for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


def notification_blocks(text: str) -> dict:
    """A `<task-notification>` body split into its named blocks.

    Only the blocks this module consumes are extracted, and every one of them
    is optional: a notification with no `<failures>` is the NORMAL case (2 of 28
    runs carry one), and a missing block simply means that output is not
    emitted.
    """
    out: dict = {}
    for tag in NOTIFY_TAGS:
        match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
        if match:
            out[tag.replace("-", "_")] = match.group(1).strip()
    usage = out.get("usage")
    if usage is not None:
        counts = {}
        for tag in USAGE_TAGS:
            m = re.search(rf"<{tag}>\s*(-?\d+)\s*</{tag}>", usage)
            if m:
                counts[tag] = int(m.group(1))
        out["usage"] = counts
    failures = out.get("failures")
    if failures is not None:
        out["failures"] = parse_failure_lines(failures)
    return out


def parse_failure_lines(text: str) -> list[dict]:
    """`[<label>] failed: <cause>` lines -> ``[{label, cause}]``.

    Corroborating detail only (see :func:`failure_causes`). A line that does not
    open with a bracketed label is kept with an empty label rather than dropped:
    an unattributable cause is still a cause, and dropping it would be the one
    thing worse than not naming the agent.
    """
    out = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\[([^\]]*)\]\s*(?:failed\s*:)?\s*(.*)$", line)
        if m:
            out.append({"label": m.group(1).strip(), "cause": m.group(2).strip()})
        else:
            out.append({"label": "", "cause": line})
    return out


def driver_session_paths() -> list[str]:
    """`<session>.jsonl` beside every session dir that holds this run."""
    return [d + ".jsonl" for d in run_session_dirs()]


def scan_driver_sessions() -> None:
    """Read this run's driver session file(s) forward, for the launch and the close.

    Records the newest launch `toolUseResult` naming this runId (which supplies
    the ``taskId`` the notification is joined on — GD-M3's discriminator) and
    every `<task-notification>` seen, keyed by its own task id.

    Cost: the first pass over a path starts at most ``SESSION_SCAN_MAX_BYTES``
    from its end and every later pass starts at the stored offset, so a live
    driver conversation costs O(bytes appended) per poll — the same tailer
    discipline the journal reader uses, including the torn-tail rule (nothing
    past the last newline is consumed). A byte-level pre-filter keeps the JSON
    parser off the 99.9% of a conversation that is neither record.

    Read-only, like every other `~/.claude` access in this module.
    """
    for path in driver_session_paths():
        try:
            st = os.stat(path)
        except OSError:
            continue
        ident = f"{st.st_dev}:{st.st_ino}"
        seen = _SESSION_SCAN.get(path)
        if seen is None or seen["ident"] != ident or st.st_size < seen["offset"]:
            start = max(0, st.st_size - SESSION_SCAN_MAX_BYTES)
            seen = {"ident": ident, "offset": start, "partial": start > 0}
            _SESSION_SCAN[path] = seen
        if st.st_size == seen["offset"]:
            continue
        try:
            with open(path, "rb") as f:
                f.seek(seen["offset"])
                chunk = f.read()
        except OSError:
            continue
        cut = chunk.rfind(b"\n")
        if cut == -1:  # no complete line yet; leave the offset where it is
            continue
        body = chunk[:cut + 1]
        seen["offset"] += cut + 1
        if seen.pop("partial", False):
            # We started mid-line to bound the first pass: that fragment is not
            # a record, and json.loads would only fail on it anyway.
            nl = body.find(b"\n")
            body = body[nl + 1:] if nl != -1 else b""
        for raw in body.split(b"\n"):
            if not raw:
                continue
            if b"runId" not in raw and b"task-notification" not in raw:
                continue
            try:
                record = json.loads(raw.decode(errors="replace"))
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(record, dict):
                _absorb_session_record(record)


def _absorb_session_record(record: dict) -> None:
    """One driver-session record -> the launch join and the notification store."""
    ts = str(record.get("timestamp") or "")
    result = record.get("toolUseResult")
    if isinstance(result, dict) and result.get("runId") == WF_NAME:
        task_id = result.get("taskId")
        if task_id and ts >= str(_LAUNCH.get("ts") or ""):
            _LAUNCH["task_id"] = str(task_id)
            _LAUNCH["ts"] = ts
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind") == NOTIFY_KIND:
        blocks = notification_blocks(record_text(record))
        task_id = blocks.get("task_id")
        if not task_id:
            return
        blocks["ts"] = ts
        _NOTIFICATIONS[task_id] = blocks
        while len(_NOTIFICATIONS) > _NOTIFICATIONS_CAP:
            _NOTIFICATIONS.pop(next(iter(_NOTIFICATIONS)))


def run_notification() -> dict | None:
    """This run's notification, joined by task id — or None.

    The join is the launch record's ``taskId``, never the tag and never "the
    newest notification in the file": one driver session runs many background
    tasks, and adopting a foreign one would close a live run on a stranger's
    verdict. A notification OLDER than the launch it would be joined to is
    refused for the same reason a stale snapshot is (a resume re-uses the
    runId, so the previous run's notification is still on disk).
    """
    task_id = _LAUNCH.get("task_id")
    if not task_id:
        return None
    note = _NOTIFICATIONS.get(task_id)
    if note is None:
        return None
    if str(note.get("ts") or "") < str(_LAUNCH.get("ts") or ""):
        return None
    return note


def emit_through_status(plan: str, stage: str, state_word: str,
                        detail: str) -> bool:
    """Append ONE event by RUNNING status.sh, so the line reads `w:"agent"` (GD-D5).

    The run close is the one line this module must not write itself. Route 1 of
    the exit protocol is answered only by a `w:"agent"` close, and forging that
    attribution with a raw append would fork the writer set the attribution
    exists to keep honest — the two ledgerless hand-appended lines on disk are
    the cautionary example (MONITORING-5). So the deterministic emitter shells
    out to the same script an agent uses: one flock, one cap, one attribution,
    and `status.sh` stays the only write path into `events.jsonl`.

    Best-effort like every other write here: a missing script, a missing bash
    or a non-zero exit warns on stderr and returns False. The caller then
    leaves the close to the timeout rungs, which is precisely what they are for.
    """
    if not os.path.isfile(STATUS_SH):
        print(f"decision_watcher: cannot emit the run close: {STATUS_SH} is missing",
              file=sys.stderr, flush=True)
        return False
    # Say exactly what this call means to say and nothing the shell it was
    # started from happened to export. `status.sh` FOLDS ORCH_TITLE,
    # ORCH_PLANS_TOTAL and (GD-D11) ORCH_ROSTER into every line it writes, and
    # a watcher inherits the driver's environment — so an inherited
    # ORCH_PLANS_TOTAL would have the deterministic close silently re-declaring
    # a denominator, or renaming the orchestrator card, as a side effect of the
    # shell it was launched from. The output of a deterministic emitter is a
    # pure function of recorded data; these three are not recorded data.
    env = {k: v for k, v in os.environ.items() if k not in STATUS_ENV_DROP}
    env["ORCH_STATE_DIR"] = STATE_DIR
    try:
        proc = subprocess.run(
            ["bash", STATUS_SH, plan, stage, state_word,
             cap_detail(single_line(detail))],
            env=env, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"decision_watcher: cannot run {STATUS_SH}: {exc}",
              file=sys.stderr, flush=True)
        return False
    if proc.returncode != 0:
        print(f"decision_watcher: {STATUS_SH} exited {proc.returncode}: "
              f"{(proc.stderr or '').strip()}", file=sys.stderr, flush=True)
        return False
    return True


def failure_causes(snap: dict | None, note: dict | None) -> list[dict]:
    """Per-agent death causes: STRUCTURED first, the notification corroborating.

    The primary source is the snapshot's own `workflowProgress[]` — a row with
    ``state:"error"`` and its ``error`` string (×23 on the recorded corpus),
    with ``lastAttemptReason`` (×8) carried alongside. The notification's
    `<failures>` block is CORROBORATION only: it fires on 2 of 28 recorded runs,
    so a reader that made it primary would report "stale" for the other 26
    (JSONL-EXTRACT-4 / CC-SESSIONS-9 — the run-2 correction to D-08 as
    written). When both name the same label the structured cause wins and the
    prose is dropped; when only the prose exists it is used as-is.

    Returns one entry per DEAD agent, each carrying its label, its parsed
    plan/role/attempt when the label is well-formed, the agentId when the
    snapshot knew it, and the cause text. Absence of either source yields an
    empty list — the normal case, and not an error.
    """
    causes: dict[str, dict] = {}
    for row in snapshot_agents(snap):
        error = single_line(row.get("error"))
        if not error and row.get("state") != "error":
            continue
        label = single_line(row.get("label"))
        reason = single_line(row.get("lastAttemptReason"))
        cause = error or reason or "agent ended in state error"
        if error and reason:
            cause = f"{error} (last attempt: {reason})"
        entry = {"label": label, "cause": cause, "source": "snapshot",
                 "agent_id": row.get("agentId") or ""}
        entry.update(parse_agent_label(label) or {})
        causes[label or entry["agent_id"]] = entry
    for item in (note or {}).get("failures") or []:
        label = single_line(item.get("label"))
        if label and label in causes:  # already reported, structurally
            continue
        entry = {"label": label, "cause": single_line(item.get("cause")),
                 "source": "notification", "agent_id": ""}
        entry.update(parse_agent_label(label) or {})
        causes.setdefault(label or f"\0unlabelled\0{len(causes)}", entry)
    return list(causes.values())


def emit_failure_causes(state: dict, causes: list[dict]) -> int:
    """One `<stage> failed` line per dead agent, once (D-08b). Returns the count.

    This is what turns a 529-killed agent from a row that merely went ``stale``
    (a shape derived from silence) into one that reads *failed, with a cause*.
    It writes STAGE events only: plan badges are settled by GD-10's predicate
    and the settle pass, and a cause line must never be the thing that closes a
    card — that would be exactly the fabricated badge R-58 forbids, arriving
    through a new door. ``annotation_stage`` is what ENFORCES that (the stage
    comes out of a harness label and a reserved word in one would otherwise set
    a badge); it is not left to the labels happening to be well-behaved.

    Idempotent by label, checkpointed, so a restart or a second poll after the
    snapshot changed re-emits nothing.
    """
    reported = set(state.setdefault("failure_causes", []))
    emitted = 0
    for cause in causes:
        key = cause.get("label") or cause.get("agent_id") or cause.get("cause")
        if not key or key in reported:
            continue
        reported.add(key)
        info = None
        if cause.get("plan") and cause.get("role"):
            info = {k: cause[k] for k in ("plan", "role", "attempt", "stage")}
        who = (f"{info['role']} #{info['attempt']}" if info
               else (cause.get("label") or "agent"))
        extra = None
        if cause.get("agent_id"):
            extra = {"agent": agent_block(cause["agent_id"], info, "failed")}
        emit(annotation_stage(info), "failed",
             f"{who} died: {cause['cause']} (from the {cause['source']})",
             plan=info["plan"] if info else "orchestrator", extra=extra)
        emitted += 1
    state["failure_causes"] = sorted(reported)
    return emitted


def emit_run_stats(state: dict, note: dict | None) -> bool:
    """The harness's own `<usage>` counts, once, on the orchestrator card (D-08a).

    Carried under a new top-level ``run`` key — additive, and readers that do
    not know it ignore it (GD-D14). It is a CROSS-CHECK, never a substitute:
    the folded live totals stay the in-flight source, because they are computed
    from the transcripts' own `message.usage` rows while these are the harness's
    display figures for a different denominator (`subagent_tokens` excludes the
    driver, GD-11 forbids substituting `harnessTotals` for a computed total).

    Deliberately NOT written under the reserved ``complete`` stage: that stage
    is an alias of ``plan`` for badge purposes, so an `info` there would reopen
    the badge the close just set and cancel the exit protocol's route 1.
    """
    usage = (note or {}).get("usage")
    if not usage or state.get("run_stats_emitted"):
        return False
    parts = [f"{usage[tag]} {tag.replace('_', ' ')}"
             for tag in USAGE_TAGS if tag in usage]
    emit("run", "info", "harness run stats: " + ", ".join(parts),
         extra={"run": dict(usage)})
    state["run_stats_emitted"] = True
    return True


def update_resume_recovery(state: dict, note: dict | None) -> bool:
    """Rewrite `plan/RESUME.md`'s recovery section verbatim from `<recovery>` (D-08c).

    The harness prints the EXACT `Workflow({…})` call that resumes the run, and
    a hand-copied version of it is the thing that goes stale first in a recovery
    procedure. Written between two HTML-comment markers, so:

      * nothing outside the markers is ever touched — the rest of RESUME.md is
        prose a human wrote;
      * a file WITHOUT the markers gets the section appended, once;
      * a file that does not exist is not created. `touch-run bind` (D-13)
        renders RESUME.md; this item owns only the recovery section.

    Idempotent on the recovery TEXT (checkpointed), so a re-poll rewrites
    nothing. Refuses to write inside a plugin cache for the same reason every
    other writer here does.

    The body is harness text going verbatim into a file humans read, so three
    cheap bounds apply before the splice — the same posture the 1 KB detail cap
    takes, for the same reason (the bytes travel through renderers, not only
    through JSON):

      * a body carrying either marker is REFUSED outright, because the next
        rewrite splices on ``find(RESUME_END)`` and an injected copy would cut
        the section in the wrong place, eating whatever a human wrote between;
      * the fence is widened past the longest backtick run in the body, so a
        recovery call containing ``` cannot terminate its own code block;
      * the body is capped at ``RECOVERY_MAX_CHARS`` with the cut marked.
    """
    recovery = (note or {}).get("recovery")
    if not recovery or state.get("resume_recovery") == recovery:
        return False
    path = os.path.join(STATE_DIR, "plan", "RESUME.md")
    if not os.path.isfile(path) or in_plugin_cache(path):
        return False
    body = recovery.strip()
    if RESUME_BEGIN in body or RESUME_END in body:
        print("decision_watcher: refusing to splice a <recovery> block that "
              "carries this section's own markers", file=sys.stderr, flush=True)
        state["resume_recovery"] = recovery   # refused once, refused for good
        return False
    if len(body) > RECOVERY_MAX_CHARS:
        body = body[:RECOVERY_MAX_CHARS] + "\n...[truncated by decision_watcher]"
    runs = [len(m) for m in re.findall(r"`+", body)]
    fence = "`" * max(3, (max(runs) if runs else 0) + 1)
    section = (f"{RESUME_BEGIN}\n"
               f"<!-- written by decision_watcher from the run's own "
               f"<recovery> block; edits here are overwritten -->\n"
               f"{fence}\n{body}\n{fence}\n{RESUME_END}\n")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    start, end = text.find(RESUME_BEGIN), text.find(RESUME_END)
    if start != -1 and end > start:
        new = text[:start] + section + text[end + len(RESUME_END):].lstrip("\n")
    else:
        new = text.rstrip("\n") + "\n\n## Recovery (verbatim, from the harness)\n\n" + section
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"decision_watcher: cannot update {path}: {exc}",
              file=sys.stderr, flush=True)
        return False
    state["resume_recovery"] = recovery
    return True


def close_evidence(snap: dict | None, note: dict | None) -> tuple[int, str, str] | None:
    """``(rung, status, summary)`` for the first rung that has landed, or None.

    Rung 1 (the snapshot) wins a tie: its status vocabulary is the harness's own
    and it exists at the second the run ended, where the notification is the
    driver session's account of the same fact.
    """
    if snap is not None:
        return 1, str(snap.get("status") or ""), single_line(snap.get("summary"))
    if note is not None and note.get("status"):
        return 2, str(note.get("status") or ""), single_line(note.get("summary"))
    return None


def emit_run_extras(state: dict, snap: dict | None, note: dict | None) -> int:
    """The three harness-derived outputs of D-08, in reader order. Count written.

    Kept SEPARATE from the close, and polled for as long as the watcher lives,
    because the two facts do not land together. Rung 1 fires within
    ``CLOSE_POLL_SECS`` of the snapshot appearing, while the
    `<task-notification>` is a different record in a different file appended by
    a different writer — nothing orders them. A version of this that stopped
    looking the moment the run closed would drop `<usage>` and `<recovery>`
    every time the notification was one beat late, which is exactly the stale
    hand-copied resume command D-08(c) exists to kill. All three emitters are
    checkpoint-idempotent, so asking again costs a stat and writes nothing.
    """
    written = emit_failure_causes(state, failure_causes(snap, note))
    if emit_run_stats(state, note):
        written += 1
    if update_resume_recovery(state, note):
        emit("watcher", "info",
             "plan/RESUME.md recovery section updated from the run's own <recovery>")
        written += 1
    return written


def poll_run_close(state: dict, baseline: dict,
                   events_baseline: int = 0) -> tuple[bool, int]:
    """One pass of the GD-D6 close plane. ``(closed_now, extras_written)``.

    Reads both recorded sources once, emits the death causes / run stats /
    recovery section that either of them supports, and THEN — if the run is not
    closed yet — the terminal `orchestrator complete done|failed`, which is the
    run's final word and the line route 1 of the exit protocol waits for. That
    order is deliberate: the causes happened before the end, and the close is
    the last thing a reader should see.

    Rung 3 is consulted here too, so "first rung wins" is true of all three
    landable rungs and not just of the two this module polls: a driver that
    still types its belt-and-braces close has fired the ladder, and emitting a
    second terminal event beside it would be the duplicate close this module
    exists to remove — worse, a disagreement between the two would flip the
    badge on last-event-wins.

    With no rung landed this function writes NOTHING and the run falls through
    to the timeout rungs exactly as it always has.
    """
    snap = read_run_snapshot(baseline)
    scan_driver_sessions()
    note = run_notification()
    extras = emit_run_extras(state, snap, note)
    if state.get("run_closed_rung"):
        return False, extras
    evidence = close_evidence(snap, note)
    if evidence is None:
        return False, extras
    if stream_terminal_close(EVENTS, events_baseline, writer="agent"):
        # Rung 3 landed first: record it as the rung that fired and do not
        # compete. Asked HERE, not at the top, because it reads events.jsonl
        # and this module keeps stream scans off the poll path (the same rule
        # exit_precheck follows) — a run with no rung 1/2 evidence was never
        # going to emit anything, so the question would cost a re-read of a
        # growing multi-writer file every two seconds to change nothing.
        state["run_closed_rung"] = CLOSE_RUNGS[3]
        return False, extras
    rung, status, summary = evidence
    close_state = run_close_state(status)
    detail = (f"{WF_NAME} {single_line(status) or 'ended'} "
              f"(close rung {rung}: {CLOSE_RUNGS[rung]})")
    if summary:
        detail += f" — {summary}"
    if not emit_through_status("orchestrator", "complete", close_state, detail):
        return False, extras
    state["run_closed_rung"] = CLOSE_RUNGS[rung]
    state["run_complete"] = close_state
    return True, extras


def reconcile(state: dict, snap: dict) -> int:
    """D-16: the post-run snapshot as an oracle. Returns the corrections emitted.

    Two holes the live tail cannot close, both recorded on disk:

      * an agent whose transcript had not flushed when its journal entry was
        read classifies as nothing and renders as ``agentId[:8]`` forever;
      * a run whose watcher never ran at all (5 of 12 recorded task folders)
        has no rows and no token totals.

    Both are corrected from the snapshot's `workflowProgress[]` — which agents
    existed, what state each ended in, what its label was. Two caveats are
    encoded rather than commented: ``promptPreview``/``resultPreview`` are
    TRUNCATED and are never parsed for markers (the marker is re-read from the
    agent's real transcript, or the LABEL is parsed, and nothing else), and
    ``agentCount != len(workflowProgress)`` is normal.

    Token totals are re-derived from the transcripts by the ordinary flush, not
    copied from the snapshot's own ``tokens`` figure: GD-11 forbids substituting
    the harness's display number for a computed one, and the snapshot is used
    here only to learn WHICH agents to look at.

    Idempotent: every corrected agent is checkpointed, so a second run of
    ``--reconcile`` emits nothing.
    """
    reconciled = set(state.setdefault("reconciled", []))
    emitted = 0
    for row in snapshot_agents(snap):
        agent_id = row.get("agentId")
        if not agent_id or agent_id in reconciled:
            continue
        known = state["agents"].get(agent_id)
        lost_tokens = not NO_TOKENS and agent_id not in state.get("tok_emitted", {})
        if known and not lost_tokens:
            continue
        # The real transcript's marker first (the authoritative source), then
        # the snapshot's own label. NEVER promptPreview: it is truncated, and a
        # marker cut in half classifies an agent onto a plan card that does not
        # exist (SUBSTRATE-10).
        info = known or classify(agent_id, retries=1) or parse_agent_label(row.get("label"))
        row_state = {"done": "done", "error": "failed"}.get(row.get("state"), "stale")
        totals = token_totals(agent_id)
        # annotation_stage, and here it matters MORE than in emit_failure_causes:
        # these rows carry `done`/`failed`, so a label ending in a reserved
        # stage would close a plan card — or the run — from a post-run pass.
        emit(annotation_stage(info), row_state,
             (f"{info['role']} #{info['attempt']}" if info
              else f"agent {agent_id}")
             + f": reconciled from the run snapshot ({single_line(row.get('state')) or 'unknown'})",
             plan=info["plan"] if info else "orchestrator",
             extra={"agent": agent_block(agent_id, info, row_state,
                                         tokens=tokens_field(totals),
                                         ctx=ctx_field(agent_id))})
        # --reconcile re-derives from the TRANSCRIPTS (never from the
        # snapshot's display numbers), so the occupancy here is the same
        # measured reading the live tail would have emitted. Completed runs'
        # history is not rewritten: a reader ignoring a missing key IS the
        # backfill story.
        flush_agent_tokens(state, agent_id, info, row_state=row_state,
                           totals=totals)
        if info:
            state["agents"][agent_id] = info
        reconciled.add(agent_id)
        emitted += 1
    state["reconciled"] = sorted(reconciled)
    return emitted


def state_stamp() -> tuple | None:
    """``(size, mtime_ns)`` of the checkpoint, or None if there is not one yet."""
    try:
        st = os.stat(STATE)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def pid_alive(pid) -> bool:
    """Is ``pid`` a live process? Stdlib, no /proc dependency.

    ``os.kill(pid, 0)`` sends nothing and only asks the kernel whether the
    process exists; ``EPERM`` means it exists and is someone else's. A recycled
    pid reads as alive — deliberately the safe direction here, since the only
    consequence is that a checkpoint write is withheld.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def reconcile_main() -> None:
    """`decision_watcher.py --reconcile` — one pass, then stop (D-16).

    A **post-run** pass, and the checkpoint is what makes that structural
    rather than merely documented: `.watcher-state.json` has exactly one writer
    at a time. Two questions are asked, in order, and either one withholds the
    save:

      * does the checkpoint name a LIVE pid other than this process? The daemon
        records its own on every save, so this is the deterministic answer —
        a recycled pid reads as live, which is the safe direction;
      * did the file move under the pass anyway? The mtime stamp catches a
        writer this build did not record a pid for (an older checkpoint).

    The corrections already emitted stand — `events.jsonl` is append-only and
    flock'd — and only the checkpoint write is withheld, which costs at most a
    re-emit on a later reconcile.
    """
    state = load_state()
    state.setdefault("agents", {})
    state.setdefault("running", [])
    state.setdefault("tok_emitted", {})
    state.setdefault("tok_tick_at", {})
    stamp = state_stamp()
    for warning in _CFG_WARNINGS:
        print(warning, file=sys.stderr, flush=True)
    snap = read_run_snapshot()
    if snap is None:
        print(f"decision_watcher --reconcile: no run snapshot for {WF_NAME} "
              f"(a snapshot is not guaranteed — nothing to reconcile)")
        return
    rows = snapshot_agents(snap)
    emitted = reconcile(state, snap)
    owner = state.get("pid")
    live = pid_alive(owner) and int(owner) != os.getpid()
    if not live and state_stamp() == stamp:
        save_state(state)
    else:
        print(f"decision_watcher --reconcile: a live watcher owns "
              f"{os.path.basename(STATE)}"
              + (f" (pid {owner})" if live else " (it changed during this pass)")
              + "; checkpoint NOT written", file=sys.stderr, flush=True)
    print(f"decision_watcher --reconcile: {WF_NAME} {snap.get('status')} — "
          f"{len(rows)} snapshot agent row(s), {emitted} correction(s) emitted")


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
    if RECONCILE:
        # D-16: a one-shot corrective pass, not a daemon. It shares every
        # reader above and writes through the same emit(), so nothing about the
        # live path has a second implementation.
        reconcile_main()
        return
    state = load_state()
    state.setdefault("agents", {})
    state.setdefault("running", [])
    state.setdefault("tok_emitted", {})
    # GD-D: per-agent wall-clock of the last token emit, checkpointed beside the
    # baseline so a restart does not re-open every agent's cadence window.
    # setdefault, so a pre-cadence .watcher-state.json loads unchanged.
    state.setdefault("tok_tick_at", {})
    state.setdefault("plans", {})
    state.setdefault("decisive", {})
    # GD-10: "was the plan's LAST result a failure" — the close predicate's
    # fallback when no gate/critic ever returned a decisive verdict.
    state.setdefault("last_result_ok", {})
    state.setdefault("last_plan", None)
    state.setdefault("run_complete", None)
    # Who owns this checkpoint. Read by `--reconcile`, which is a POST-run pass
    # and must not rewind a live daemon's offsets; nothing else consumes it, and
    # a reader that does not know the key ignores it like every other.
    state["pid"] = os.getpid()
    for cached in state["agents"].values():  # pre-upgrade cache entries lack "stage"
        cached.setdefault("stage", cached.get("role", "work").split(":")[-1])
    # Session scope for the R-40 self-exit: every `complete` event already in the
    # stream belongs to an EARLIER phase of this task folder (one folder hosts
    # research, then implement), so only bytes appended past this baseline
    # can end THIS watcher's run. Recorded before the first emit so the
    # heartbeat itself stays outside the window.
    try:
        events_baseline = os.path.getsize(EVENTS)
    except OSError:
        events_baseline = 0
    # GD-D6 rung 1 is snapshot APPEARANCE, so the copies already on disk are
    # this run's PREVIOUS attempt (a resume re-uses the runId) and must not
    # close it. Same scoping, same reason, as events_baseline above.
    close_baseline = snapshot_baseline()
    close_poll_at = 0.0
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
    backfill_at = time.time()
    for aid, prev in ([] if NO_TOKENS else list(state["tok_emitted"].items())):
        tin, tcached, twrite, tout = agent_tokens(aid)
        # For an agent still in flight this backfill IS a read, so it opens the
        # cadence window like any other (GD-D: the window restarts on the READ).
        # Without this every in-flight agent would be re-parsed a second time on
        # the very next poll tick, immediately after the pass that just parsed
        # it. Agents that are NOT in `running` are never ticked, so stamping a
        # window for them would only be state nobody reads — see the sweep below.
        if aid in state["running"]:
            state["tok_tick_at"][aid] = backfill_at
        # Monotonic counters (D7): clamp deltas >= 0; never lower the baseline.
        deltas, new_base = token_deltas(prev, tin, tcached, twrite, tout)
        if not any(deltas.values()):
            continue
        info = state["agents"].get(aid)
        plan = info["plan"] if info else "orchestrator"
        label = agent_label(info, aid)
        emit("tokens", "info", f"{label} token backfill", plan=plan,
             extra={"tokens": deltas, "quiet": True,
                    # no "state" key: leave the row's queued/running/done dot as-is
                    "agent": agent_block(aid, info,
                                         tokens={"in": new_base["in"], "out": new_base["out"],
                                                 "cached": new_base["cached"],
                                                 "cache_write": new_base["cache_write"]},
                                         ctx=ctx_field(aid))})
        state["tok_emitted"][aid] = new_base
    # The backfill is a one-shot TERMINAL read for every agent that is NOT in
    # flight, and on a RESTART (the documented resume workflow) that is nearly
    # all of them: `tok_emitted` holds every agent the run has ever tracked —
    # 167 on the measured run — while `running` holds the handful still alive.
    # Their parse memos are exactly the order-1e5 dead entries drop_usage_cache()
    # exists to prevent, and their cadence windows (inherited from the
    # checkpoint or stamped by an earlier session) have no consumer either,
    # since token_tick_due() is only ever asked about agents in `running`.
    # Sweeping both here is what keeps a resumed watcher's footprint
    # proportional to CONCURRENCY instead of to the length of the run.
    live_transcripts = {f"agent-{aid}.jsonl" for aid in state["running"]}
    for path in [p for p in _USAGE_CACHE
                 if os.path.basename(p) not in live_transcripts]:
        _USAGE_CACHE.pop(path, None)
    # The occupancy map is swept on the same rule and for the same reason: the
    # backfill above reads every agent the run has ever tracked, and only the
    # handful still in flight will ever be emitted for again.
    for aid in [a for a in _LAST_CONTEXT if a not in state["running"]]:
        _LAST_CONTEXT.pop(aid, None)
    for aid in [a for a in state["tok_tick_at"] if a not in state["running"]]:
        del state["tok_tick_at"][aid]
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
                # No token sweep here on purpose: a second signal means the
                # operator wants out NOW, and the drain below is where the
                # rescue work belongs (M-2's own contract).
                emit("watcher", "info",
                     "watcher exiting: second stop signal, drain cut short")
                save_state(state)
                return
            if drain_until is None:
                drain_until = time.time() + DRAIN_SECS
            elif time.time() >= drain_until:
                # GD-D force-flush: state every in-flight agent's total before
                # the process that alone can report it goes away.
                sweep_running_tokens(state)
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
                            # WRITE-SIDE-4: an agent that leaves `running`
                            # without a result is never ticked again, so this is
                            # its last chance to state a total. One transcript
                            # read serves both the row's cumulative and the
                            # flushing delta line below.
                            o_totals = token_totals(other)
                            # D7: state the CLAMPED baseline, never the raw
                            # reading. agent_paths() unions transcript COPIES,
                            # so a pruned or rotated copy can shrink the union —
                            # and if it has, the flush below is silent (zero
                            # delta), leaving this row as the last word on the
                            # agent's cumulative. A raw reading there would be
                            # the one place a counter goes backwards and GD-C's
                            # "delta sum == last cumulative" equality breaks.
                            o_base = (None if o_totals is None else token_deltas(
                                state["tok_emitted"].get(other, {}), *o_totals)[1])
                            emit(oinfo["stage"], "stale",
                                 f"{oinfo['role']} #{oinfo['attempt']} abandoned — no result, "
                                 f"{info['role']} attempt {info['attempt']} respawned",
                                 ts=ts0, plan=oinfo["plan"],
                                 extra={"agent": agent_block(
                                     other, oinfo, "stale",
                                     tokens=None if o_base is None else dict(o_base),
                                     ctx=ctx_field(other),
                                     runtime=elapsed_str(first_ts(other), last_ts(other)))})
                            flush_agent_tokens(state, other, oinfo, ts=ts0,
                                               totals=o_totals)
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
                        a_totals = token_totals(agent_id)
                        emit(info["stage"], sst,
                             f"{info['role']} #{info['attempt']}: {sdetail}",
                             ts=tsN, plan=info["plan"],
                             extra={"agent": agent_block(
                                 agent_id, info, sst,
                                 tokens=tokens_field(a_totals),
                                 ctx=ctx_field(agent_id),
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
                        # The per-agent rollup: unconditional (force), so the
                        # agent's closing statement lands even when nothing
                        # accrued since its last tick, and now carrying the
                        # `agent` block those 144 lines never had — without it
                        # no folded replay can attribute the run's largest
                        # single token line to an agent (WRITE-SIDE-5). The
                        # reading taken for the stage event above is reused, so
                        # a result costs ONE transcript parse, not two.
                        flush_agent_tokens(state, agent_id, info, ts=tsN,
                                           row_state=sst, totals=a_totals,
                                           force=True)
                    else:
                        tsN = last_ts(agent_id)
                        # GD-D force-flush, the fourth site: the `started`
                        # branch above puts EVERY agent in `running` before it
                        # knows whether the prompt carries a marker (GD-7:
                        # harness facts create nodes), so an unclassified agent
                        # is ticked like any other — under plan="orchestrator" —
                        # and its result is just as terminal as the classified
                        # rollup below. Without a flush here the cadence is lossy
                        # exactly here, and can lose an agent's WHOLE usage: its
                        # first tick a second after the spawn legitimately reads
                        # a transcript with no usage rows yet, which spends the
                        # first-tick exemption and stamps the window, so an agent
                        # that finishes inside one ceiling reports nothing at all
                        # — where the pre-cadence per-second tick reported
                        # essentially everything. The GD-C equality cannot catch
                        # it either: both sides under-report by the same amount.
                        flush_agent_tokens(state, agent_id, None, ts=tsN,
                                           row_state="done")
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
                 f"strategy {STRATEGY or 'unset'}, token tick {TOKEN_TICK_SECS}s"
                 f", context window {context_window_str()}")
        # ABANDONED agents: a session killed mid-agent leaves journal `started`
        # entries with no `result`, so `running` never empties and the run card
        # ticks forever. After the long window, close them `stale` (GD-10: a
        # long-idle agent is unknown, never running) and let the settle pass and
        # the abandoned-exit below do their job.
        gone = abandoned_agents(state["running"], quiet_for)
        for aid in gone:
            ainfo = state["agents"].get(aid)
            state["running"].remove(aid)
            # WRITE-SIDE-4: the stale close used to state neither a cumulative
            # nor a flush, so a stale-closed agent's usage survived only inside
            # quiet ticks — 15 of 167 agents and 9.14% of the measured run's
            # input tokens, invisible to any replay that folds those away.
            a_totals = token_totals(aid)
            # D7 again (see the respawn stale close above): the clamped
            # baseline, so this row can never be the event that lowers an
            # agent's cumulative.
            a_base = (None if a_totals is None else token_deltas(
                state["tok_emitted"].get(aid, {}), *a_totals)[1])
            emit(ainfo["stage"] if ainfo else "watcher", "stale",
                 (f"{ainfo['role']} #{ainfo['attempt']}" if ainfo else f"agent {aid}")
                 + " abandoned — no result, no transcript activity for "
                   f"{ABANDON_QUIET_SECS}s",
                 plan=ainfo["plan"] if ainfo else "orchestrator",
                 extra={"agent": agent_block(
                     aid, ainfo, "stale",
                     tokens=None if a_base is None else dict(a_base),
                     ctx=ctx_field(aid),
                     runtime=elapsed_str(first_ts(aid), last_ts(aid)))})
            flush_agent_tokens(state, aid, ainfo, totals=a_totals)
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
                # The run-level announcement, under TWO guards.
                #
                # (1) GD-D6, first rung wins: once a DETERMINISTIC rung has
                # closed the run with the harness's own verdict, this pass may
                # still settle open plan cards but must never re-announce the
                # run. Its verdict is an INFERENCE over the journal, and a
                # `killed` run whose plans all resulted would be folded to
                # `done` by it — exactly the fabricated badge R-58 exists to
                # kill, arriving through a new door.
                #
                # (2) Only when the VERDICT actually moved: adopting the
                # stream's closes can re-derive the same outcome the badge
                # already carries, and re-announcing it would be one more
                # duplicate close of exactly the kind m-3 exists to remove.
                if not state.get("run_closed_rung") and state.get("run_complete") != outcome:
                    emit("complete", outcome,
                         f"run {outcome}: {len(plans)} plan(s) "
                         + ("all green" if outcome == "done" else "closed with failures")
                         + f"; loops idle {QUIET_SECS}s+ (watcher-detected end)")
                    state["run_complete"] = outcome
                quiet_since = None
                save_state(state)
        else:
            quiet_since = None
        # GD-D6 rungs 1 and 2: the DETERMINISTIC run close. Polled here, after
        # the settle pass and before the exit protocol, because what it writes
        # is the very line route 1 below waits for — a `w:"agent"`
        # `orchestrator complete`, appended through status.sh, that no driver
        # had to remember to type. It writes nothing at all until the harness
        # has actually recorded the run's end, so a run with neither rung falls
        # through to the timeout rungs exactly as it always did.
        #
        # It keeps being polled AFTER the close, too, and that is not an
        # oversight: the close is one-shot but the notification-derived outputs
        # (D-08's run stats and RESUME.md recovery section) are a different
        # record in a different file, which routinely lands a beat later than
        # the snapshot that already closed the run. They are idempotent, so
        # asking again until the watcher exits costs a stat and writes nothing.
        if time.time() >= close_poll_at:
            close_poll_at = time.time() + CLOSE_POLL_SECS
            closed, extras = poll_run_close(state, close_baseline, events_baseline)
            if closed:
                emit("watcher", "info",
                     f"run close detected deterministically "
                     f"(rung: {state['run_closed_rung']})")
            if closed or extras:
                save_state(state)
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
                sweep_running_tokens(state)  # GD-D force-flush before stopping
                emit("watcher", "info",
                     f"watcher exiting: run closed by the driver, journal quiet "
                     f"{EXIT_QUIET_SECS}s+")
                save_state(state)
                return
            if abandoned_exit(state, quiet_for):
                sweep_running_tokens(state)  # GD-D force-flush before stopping
                emit("watcher", "info",
                     f"watcher exiting: run abandoned — no driver close, journal "
                     f"quiet {ABANDON_QUIET_SECS}s+")
                save_state(state)
                return
        tick += 1
        # D-05: with --no-tokens the live tick is not throttled, it is absent —
        # no transcript read, no delta line. Spawns, results and decisions are
        # untouched, so the event plane still works; only the second
        # implementation of `ingest.rollup` goes quiet.
        if state["running"] and not NO_TOKENS:  # every poll tick (~1s): live token deltas
            # Live token deltas for in-flight agents (quiet: counters only, no log line).
            dirty = False
            now = time.time()
            for aid in list(state["running"]):
                # GD-D: the cadence ceiling gates the transcript READ as well as
                # the emit — at the 15 s default that removes ~93% of the
                # per-second parsing (agent_paths' glob included), which is the
                # whole of WRITE-SIDE-10's first fix. The journal tail above
                # keeps polling at 1 s: spawn/result latency is user-visible
                # contract and is not what this knob tunes.
                if not token_tick_due(aid, now, state["tok_tick_at"]):
                    continue
                tin, tcached, twrite, tout = agent_tokens(aid)
                # The window restarts on the READ: one transcript parse per
                # agent per TOKEN_TICK_SECS is the point, and an agent that
                # read as unchanged must not fall back to per-second polling.
                # (The token BASELINE is a different matter — see below.)
                state["tok_tick_at"][aid] = now
                prev = state["tok_emitted"].get(aid, {"in": 0, "out": 0})
                # Monotonic counters (D7): clamp deltas >= 0; never lower baseline.
                deltas, base = token_deltas(prev, tin, tcached, twrite, tout)
                din, dout = deltas["in"], deltas["out"]
                dcached, dwrite = deltas["cached"], deltas["cache_write"]
                # The non-zero-delta guard is load-bearing and the throttle lives
                # INSIDE it, never around it: this watcher emits only when it has
                # something to report, so the cadence can suppress a line but can
                # never manufacture one (WRITE-SIDE-2 — a heartbeat here would
                # erase every stall segment the timeplan draws).
                if din or dout or dcached or dwrite:
                    info = state["agents"].get(aid)
                    plan = info["plan"] if info else "orchestrator"
                    label = agent_label(info, aid)
                    # GD-LC-5: the occupancy rides THIS line and no other. A
                    # context change requires a billed turn and a billed turn
                    # always moves these counters, so every change coincides
                    # with a tick that was going to be written anyway — no
                    # heartbeat, no second cadence knob, no new event line.
                    ctx = ctx_field(aid)
                    emit("tokens", "info",
                         f"{label} running: {fmt_in(base['in'], base['cached'], base['cache_write'])} · out {fmt_tokens(base['out'])} so far" + ctx_detail(ctx),
                         plan=plan,
                         extra={"tokens": {"in": din, "out": dout, "cached": dcached,
                                           "cache_write": dwrite},
                                "quiet": True,
                                "agent": agent_block(
                                    aid, info, "running",
                                    tokens={"in": base["in"], "out": base["out"],
                                            "cached": base["cached"],
                                            "cache_write": base["cache_write"]},
                                    ctx=ctx)})
                    # The baseline advances ONLY on an actual emit (GD-D): a
                    # suppressed or empty tick leaves it exactly where it was,
                    # so the next emit — later tick, rollup, stale close or exit
                    # sweep — carries the whole accumulated delta. That is what
                    # makes coalescing lossless by construction, with no
                    # pending-delta accumulator anywhere.
                    state["tok_emitted"][aid] = base
                    dirty = True
            # Only an EMIT checkpoints. A read that found nothing leaves its
            # fresh tok_tick_at stamp in memory only, so a restart re-opens that
            # agent's window and re-reads it once — the safe direction: the
            # cadence can over-emit after a restart, never under-emit. Paying a
            # checkpoint write per silent poll tick to avoid one re-read would
            # be the wrong trade in exactly the loop this pass is making cheaper.
            if dirty:
                save_state(state)
        tick_sleep()


if __name__ == "__main__":
    main()
