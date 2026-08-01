#!/usr/bin/env python3
"""Stdlib-only live progress monitor: HTTP page + WebSocket event stream on one port.

Serves monitor.html at "/" and a websocket at "/ws" that replays every line of
events.jsonl and then streams new lines as they are appended. No third-party
dependencies (sandbox egress is proxied), so the websocket protocol is
implemented by hand: server->client text frames, ping keepalive, and a reader
task that drains client pongs/close frames.

Two wire protocols share that socket (GD-B):

* **v1 — no ``v`` in the query — is byte-identical to what this file has always
  sent**: one text frame per event line, file order, full replay from byte 0,
  no control frames, 0.5 s tail poll, truncation closes the socket. It is the
  compatibility floor and ``tests/test_ws_e2e.py`` pins it.
* **v2 — ``?v=2``** — is server-declared, never sniffed: a client asks, and only
  a ``{"m":"hello",...}`` FIRST frame licenses it to switch. The sequence is
  ``hello`` -> one ``{"m":"snapshot",...}`` (or, with ``snap=0``, batched array
  frames of raw event lines) -> ONE ``{"m":"tail",...}`` boundary frame ->
  array-framed live tail. ``"m"`` is the reserved control key: events never
  carry it (corpus-verified), an array frame is always events, a bare object is
  a legacy event. After each live tick that delivered events the server also
  sends ``{"m":"cursor","cursor":{"sig","offset"},"n":N}`` so a client can
  reconnect with ``&from=&sig=`` and resume without a gap or a duplicate — the
  boundary frame stays the ONE replay/tail boundary, the cursor frame only
  re-publishes the server's position.

**The v2 control-frame catalogue is exactly four shapes** — ``hello``,
``snapshot``, ``tail`` (the ONE boundary), ``cursor`` — and a client MUST treat
any object carrying ``m`` as control and never hand it to its event path.
``cursor`` is an ADDITION to the plan's canonical list (GD-B named the first
three): there is exactly one boundary frame, so a live tail otherwise has no
way to advance a client's resume point, and a reconnect after an hour of
tailing would replay from the boundary's stale offset. It is inside the
reserved ``m`` key space, so a client that drops it is merely unable to resume
cheaply — but the monitor.html work and the protocol section of monitoring.md
must both know about it.

Two further ADDITIONS to GD-F's snapshot schema, both additive and both
load-bearing for a hydrating client (see ``TimePlan.build`` for the exact
client rule): ``timeplan.open`` is the run-open map at the cursor, without
which a live stall hydrates as benign idle time and every running plan's bar
is lost; and ``timeplan.openAt``/``prevAt``/``atSegs``/``atRuns`` are the
COMMITTED checkpoint at ``tailFrom``, which is the state ``tailTicks`` must be
re-folded onto — re-folding them onto the end state duplicates any run that
opened and closed inside the window.

Reads are shared: one ``Stream`` per ``events.jsonl`` holds an incremental fold
(the same fold ``/tasks`` reports and the v2 snapshot serialises), broadcasts
each freshly read line to every socket subscribed to that file, and — while a
v1 client is attached — keeps an append-only blob of already-framed history
that every v1 replay writes from. One read per stream per poll window, whatever
the number of tabs.
"""
import asyncio
import base64
import bisect
import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import stat
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone

try:  # POSIX only; the audit append degrades to an unlocked write without it.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

ROOT = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------
# The tasks-root resolver and the plugin-cache guard are duplicated VERBATIM in
# decision_watcher.py. Both daemons must stay independently runnable single
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
# Per-task state (events.jsonl, orch-config.json) lives in $ORCH_STATE_DIR;
# the shared module directory holds only code and stays stateless.


def resolve_state_dir() -> str:
    """State dir: $ORCH_STATE_DIR > newest task folder under TASKS_ROOT.

    The shared module directory (ROOT) is code-only and NEVER an authoritative
    state dir (D6): a stray ``events.jsonl`` written there must not hijack
    auto-discovery, so there is no ROOT short-circuit — and no ROOT *fallback*
    either. When neither rung resolves, the daemon exits 1 naming the env vars
    that would fix it rather than serving a directory nobody writes to.
    """
    if os.environ.get("ORCH_STATE_DIR"):
        return os.environ["ORCH_STATE_DIR"]
    import glob
    candidates = (glob.glob(os.path.join(TASKS_ROOT, "*", "events.jsonl"))
                  if TASKS_ROOT else [])
    if candidates:
        return os.path.dirname(max(candidates, key=os.path.getmtime))
    sys.exit("monitor_server: no task state dir found. Set ORCH_STATE_DIR to the "
             "task folder, or ORCH_TASKS_ROOT / CLAUDE_PROJECT_DIR to the project "
             f"that owns .touch/local-orchestrators (tasks root: {TASKS_ROOT or 'unresolved'})")


STATE_DIR = os.path.abspath(resolve_state_dir())
EVENTS = os.path.join(STATE_DIR, "events.jsonl")
HTML = os.path.join(ROOT, "monitor.html")
DEFAULT_TASK = os.path.basename(STATE_DIR.rstrip(os.sep)) or "default"

# --------------------------------------------------------------------------
# Protocol + fold constants (GD-B / GD-F).
#
# FOLD_GEN is the ONE number that binds this file's fold to monitor.html's:
# the same integer literal appears verbatim in both files, a source-text test
# asserts they are equal, and every hello/snapshot carries it. Bump it in BOTH
# files whenever a fold rule changes; a client that sees a foldGen it does not
# recognise discards the snapshot and asks for `?snap=0` (full replay) instead
# of silently rendering a state built by different rules (DATA-MODEL-9).
#
# 3 (GD-LC-9): agent rows carry `ctx`, the per-subagent context-occupancy
# reading. The serialised row shape changed, so an old page against a new
# server — or the reverse — must discard and ask for a full replay rather than
# render half a contract. Bumped in monitor.html as ONE change with this one.
# --------------------------------------------------------------------------
FOLD_GEN = 3

# Timeplan thresholds. Mirrored literals of monitor.html's TP_IDLE_MS and
# TP_STALL_MS: the server derives the same segments the page would derive, so
# they MUST agree; they are part of the fold and therefore covered by FOLD_GEN.
# The twin is named rather than line-numbered on purpose — monitor.html gains
# and loses lines constantly, and a rotted number sends the next reader to the
# wrong function, which is worse than no pointer at all given that
# DATA-MODEL-9's cross-file pin depends on the twin being findable.
TP_IDLE_MS = 120000     # no-run gap worth hatching as idle
TP_STALL_MS = 240000    # run open and the stream silent this long = a stall

# Raw-tick tail window carried in the snapshot so a late ts inversion re-sorts
# against real ticks instead of a frozen segment boundary (DATA-MODEL-2:
# measured max inversion depth 6 events / 33.5 s, an order under both bounds).
TP_TAIL_MS = 120000
TP_TAIL_MIN = 64
# ...and a HARD ceiling on that window. TP_TAIL_MS bounds it in time only, so a
# dense burst (MAX_TICK_EVENTS is 5,000 in a single 0.5 s tick) would put
# thousands of raw ticks into every snapshot. The house rule is that every
# growing collection is capped: once the window holds more than this, the
# oldest pending ticks commit to the segment fold early. They are the ticks a
# late arrival can no longer be re-sorted against — two orders of magnitude
# past the measured inversion depth of 6 (DATA-MODEL-2), so nothing is lost.
TP_TAIL_MAX = 400

# v2 framing caps. Both are independent: a batch closes on whichever binds
# first (WS-PROTOCOL-1). MAX_TICK_EVENTS bounds ONE live tick; the remainder is
# carried into the next tick and the published cursor stops where the cap fell,
# so a capped tick has no gap and no duplicate (WS-PROTOCOL-10).
BATCH_MAX_EVENTS = 500
BATCH_MAX_BYTES = 256 * 1024
MAX_TICK_EVENTS = 5000
WRITE_CHUNK = 256 * 1024        # replay/snapshot slice between drain()s

# Poll cadence. 0.5 s is contract for a live stream; a stream whose mtime has
# not moved for IDLE_AFTER_SECS backs off to IDLE_POLL_SECS (reset on any
# change) so leaving old-run tabs open is free (WS-PROTOCOL-14).
POLL_SECS = 0.5
IDLE_POLL_SECS = 2.0
IDLE_AFTER_SECS = 60.0
REFRESH_MIN_SECS = 0.25         # single-flight window: N tabs = one read
KEEPALIVE_SECS = 20.0
KEEPALIVE_TICKS = 40
BLOB_IDLE_SECS = 60.0           # evict a framed-history blob this long unused
SIG_BYTES = 4096                # bytes of the file that identify the stream

# Log budget (GD-F / DATA-MODEL-7): a GLOBAL budget allocated largest-recent-
# first with a per-plan floor, because a per-plan cap multiplies by plan count
# and the plan count is what grows at 100k events. Per-plan retention matches
# monitor.html's own DOM cap — keeping more than the page can show is waste.
# Truncation is always disclosed (per-plan `logTotal` + `logTruncated`), and
# the way back to the full history is a reconnect with `?snap=0`. There is
# deliberately NO `/events?before=` load-older route: it was considered and
# deferred (WS-PROTOCOL-9) because the reconnect covers the need at a far
# smaller surface — recorded here so it is not re-invented ad hoc.
LOG_BUDGET_LINES = 1500
LOG_BUDGET_BYTES = 400 * 1024
LOG_PLAN_FLOOR = 20
LOG_KEEP_PER_PLAN = 400
LOG_ENTRY_OVERHEAD = 40         # JSON punctuation + key names per log record

# A subscriber that has not drained this many pending events is hopeless; close
# it so the client reconnects and gets a snapshot instead of the server
# buffering without bound. The number is a compromise between two failures: at
# the measured ~458 B/line this is ~9 MB of retained line bytes for one stuck
# reader (the old 200,000 was ~90 MB — an order past the ~12 MB/client
# pathology SERVER-READ-5 measured), and it must still clear the largest
# LEGITIMATE single broadcast, since one refresh hands a subscriber everything
# it read in one go: MAX_TICK_EVENTS is 5,000 per tick and the e2e burst case
# appends 6,000 at once.
MAX_PENDING_EVENTS = 20_000

# Bytes a single scan step reads. A cold fold (or any reset) used to `f.read()`
# the WHOLE file and materialise every record as a list — ~45 MB plus list
# overhead at the 100k headroom target, on the thread pool, all live at once.
# M9's "never materialise all lines" applies here too, so the fold walks a long
# stream in windows: each step reads at most this much, folds it, broadcasts
# it, and the next step continues from the offset it reached.
SCAN_WINDOW = 4 * 1024 * 1024


#: Directory names that are NEVER a task folder, however the tasks root was
#: resolved (SERVER-9). The tasks root is the dedicated
#: `.touch/local-orchestrators/`, so these names cannot appear inside a correctly
#: resolved one — this is the cheap defence against a mis-set `$ORCH_TASKS_ROOT`
#: (or a `CLAUDE_PROJECT_DIR` one level off) pointing the scan at `.touch/`
#: itself, where `memory/` would become a selectable "task": `/artifacts` would
#: then list every memory file as a note and `/file` would serve them through the
#: artifact reader, a second read path for the memory tree with none of the
#: memory rules. Dot-directories go the same way (`.history/`, `.trash/`).
NON_TASK_DIRS = frozenset({"memory", "sessions", "runs", "spool"})


def discover_tasks() -> dict:
    """name -> state dir, rescanned per request so tasks started later appear live."""
    tasks = {}
    try:
        for entry in sorted(os.listdir(TASKS_ROOT)):
            if entry in NON_TASK_DIRS or entry.startswith("."):
                continue
            d = os.path.join(TASKS_ROOT, entry)
            if os.path.isdir(d):
                tasks[entry] = d
    except OSError:
        pass
    # Startup default (e.g. an $ORCH_STATE_DIR outside the tasks root) is
    # always selectable; same-named entry inside the root is the same dir.
    tasks.setdefault(DEFAULT_TASK, STATE_DIR)
    return tasks


# Full-replay results per events file, keyed by (mtime_ns, size) so a task
# that stopped emitting costs one scan total, not one per /tasks poll.
_STATUS_CACHE: dict = {}

# Unparseable lines per events file, surfaced via /health (R-10). A poisoned or
# torn line is skipped silently by the replay — a counter is what turns "the
# dashboard looks wrong" into "line N of this stream is not JSON". Counted once
# per scan (the scan itself is cached by (mtime_ns, size)).
PARSE_FAILURES: dict = {}


def replay_plan_states(events_path: str):
    """Replay one stream into ``(plan_states, last, tokens, parse_failures)``.

    Per-plan badge state is **last-event-wins in FILE ORDER** — the SD-4/R-58
    conflict rule: when a stream holds both a fabricated ``plan failed`` and a
    later corrective ``plan done`` for the same plan, the correction wins, and no
    stream is ever rewritten to achieve that. Order is file order, never a ts
    sort (ts values are written by several writers and are not monotonic).

    Continuation reopen (FRONTEND-6, server half): one task folder hosts several
    phases appending to one stream, so events can continue PAST a run-level
    ``orchestrator complete done``. Activity after a terminal orchestrator badge
    — a sub-plan ``plan`` event opening as running/queued (seed lines included),
    or any ``running``-state orchestrator event outside the reserved stages —
    flips the orchestrator badge back to ``running``, exactly as the replaying
    dashboard does. Without this the home-grid tile reads "done" while loops
    are visibly running.
    """
    plan_states: dict = {}
    last = None
    tok_in = tok_out = tok_cached = tok_write = 0
    failures = 0
    with open(events_path, "rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                failures += 1
                continue
            if not isinstance(ev, dict):
                failures += 1
                continue
            stage, state = ev.get("stage"), ev.get("state")
            if stage in ("plan", "complete"):
                plan_states[ev.get("plan")] = state
                if (stage == "plan" and ev.get("plan") != "orchestrator"
                        and state in ("running", "queued")
                        and plan_states.get("orchestrator") in ("done", "failed")):
                    plan_states["orchestrator"] = "running"
            elif (stage != "tokens" and state == "running"
                    and ev.get("plan") == "orchestrator"
                    and plan_states.get("orchestrator") in ("done", "failed")):
                plan_states["orchestrator"] = "running"
            tok = ev.get("tokens")
            if tok:
                tok_in += tok.get("in") or 0
                tok_out += tok.get("out") or 0
                tok_cached += tok.get("cached") or 0
                tok_write += tok.get("cache_write") or 0
            if not ev.get("quiet"):
                last = {k: ev[k] for k in ("ts", "plan", "stage", "state", "detail") if k in ev}
    tokens = {"in": tok_in, "out": tok_out, "cached": tok_cached, "cache_write": tok_write}
    return plan_states, last, tokens, failures


def parse_ts_ms(ts):
    """Event ``ts`` -> epoch milliseconds, or None when it is not a timestamp.

    The page uses ``Date.parse`` (``monitor.html:1041``); this is the Python
    equivalent for the same strings. Both writer clocks are covered: the
    watcher's ``+00:00`` form and the journal-derived ``Z`` form. A timestamp
    with no zone is read as UTC (storage is UTC by policy; only display is
    local) — the writers always stamp one, so this is a guard, not a path.
    """
    if not isinstance(ts, str) or not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def _tp_push(segs: list, kind: str, t0: int, t1: int) -> None:
    """Append/extend one timeplan segment (monitor.html:1108-1113 verbatim)."""
    if t1 <= t0:
        return
    if segs and segs[-1]["kind"] == kind:
        segs[-1]["t1"] = t1          # ticks are contiguous: merge runs of a kind
    else:
        segs.append({"kind": kind, "t0": t0, "t1": t1})


def _tp_step(state: dict, tick: dict) -> None:
    """Fold ONE tick into a timeplan state (monitor.html:1114-1131 verbatim).

    ``state`` is ``{segs, runs, open, prev, t0}``. Gaps classify by whether a
    plan run was open when they began: nothing open and > TP_IDLE_MS is idle,
    open and > TP_STALL_MS is a stall, everything else is working time.
    """
    t = tick["t"]
    if state["prev"] is None:
        state["prev"] = state["t0"] = t
    open_ = state["open"]
    gap = t - state["prev"]
    if gap > 0:
        if not open_ and gap > TP_IDLE_MS:
            kind = "idle"
        elif open_ and gap > TP_STALL_MS:
            kind = "down"
        else:
            kind = "up"
        _tp_push(state["segs"], kind, state["prev"], t)
    plan = tick.get("plan")
    if plan is not None:
        if tick.get("open"):
            open_.setdefault(plan, t)
        elif plan in open_:
            state["runs"].append({"plan": plan, "t0": open_.pop(plan), "t1": t})
    if tick.get("closeAll"):
        for pl, s in list(open_.items()):
            state["runs"].append({"plan": pl, "t0": s, "t1": t})
        open_.clear()
    state["prev"] = t


class TimePlan:
    """The page's timeplan (``tpNote``/``tpRender``) as an INCREMENTAL fold.

    ``tpRender`` is the one rule in the page that is not a left fold: it sorts
    its ticks (``monitor.html:1104``) because the watcher stamps journal-derived
    lines with true transcript times *after* it already wrote live ticks
    (measured: 0.54 % of events, max depth 6, max 33.5 s — DATA-MODEL-2).

    A sorted fold is recovered incrementally with a reorder window: ticks are
    inserted in time order into ``pending`` and only committed to the segment
    fold once they are older than ``max(TP_TAIL_MS, TP_TAIL_MIN ticks)`` — two
    orders of magnitude past the measured inversion depth, so what the segment
    fold sees is the globally sorted order. The same window is what the
    snapshot ships as ``tailTicks``, so a client can re-sort a late arrival
    against real ticks rather than a frozen boundary.
    """

    def __init__(self):
        self.segs: list = []
        self.runs: list = []
        self.open: dict = {}
        self.prev = None
        self.t0 = None
        self.pending: list = []      # (t, seq, tick), kept sorted
        self.seq = 0
        self.max_t = None

    def note(self, t, plan=None, is_open=False, close_all=False) -> None:
        tick = {"t": t}
        if plan is not None:
            tick["plan"] = plan
            tick["open"] = bool(is_open)
        if close_all:
            tick["closeAll"] = True
        bisect.insort(self.pending, (t, self.seq, tick))
        self.seq += 1
        self.max_t = t if self.max_t is None else max(self.max_t, t)
        cut = self.max_t - TP_TAIL_MS
        state = None
        # Two exits from the window, and the count one is not optional: a burst
        # can hold thousands of ticks inside TP_TAIL_MS. Committing always pops
        # the OLDEST pending tick, so the segment fold still sees sorted order.
        while self.pending and (
                len(self.pending) > TP_TAIL_MAX
                or (len(self.pending) > TP_TAIL_MIN and self.pending[0][0] <= cut)):
            if state is None:
                state = {"segs": self.segs, "runs": self.runs, "open": self.open,
                         "prev": self.prev, "t0": self.t0}
            _tp_step(state, self.pending.pop(0)[2])
            self.prev, self.t0 = state["prev"], state["t0"]

    def build(self) -> dict:
        """Derived ``segs``/``runs``/``summary``, the raw tail window, and the
        COMMITTED checkpoint the window must be re-derived from.

        ``segs``/``runs``/``summary`` cover EVERY tick up to the fold's byte
        offset (the tail window included, so nothing is missing from the strip
        on first paint). ``tailTicks`` re-publishes the window's raw ticks — the
        same ticks — so the client can re-fold them once a late inversion or a
        live event lands.

        **The re-derivation is from the checkpoint, not from the end state**,
        and shipping only the end state was a real defect: re-folding
        ``tailTicks`` on top of a fold that already contains their effect
        duplicates every run that opened AND closed inside the window. So the
        payload carries both instants, named for what they are:

        * ``open`` — the run-open map (``{plan: t0}``) AT the offset, for first
          paint. Not derivable by a client: the page classifies a gap as
          ``idle`` or ``down`` from ``open.size`` (monitor.html:1109-1112) and
          draws every still-open run as an in-flight bar (:1136), and
          ``summary.live`` cannot stand in (a bool that goes stale on the first
          live tick).
        * ``openAt`` / ``prevAt`` — the same map and the fold cursor AT
          ``tailFrom``, i.e. BEFORE the window was folded.
        * ``atSegs`` / ``atRuns`` — how many of the shipped ``segs``/``runs``
          are committed. The committed ones are always a PREFIX of what ships
          (the window fold only appends, or extends the last committed
          segment's ``t1``), so counts are exact where a time comparison is not:
          a pending tick sharing a millisecond with the last committed one would
          make "ends at or before ``tailFrom``" ambiguous in both directions.

        **Client rule** (the one sp-client-v2 implements): take
        ``segs.slice(0, atSegs)`` with the last one clipped to ``t1 =
        min(t1, prevAt)``, ``runs.slice(0, atRuns)``, seed ``open`` from
        ``openAt``, ``prev`` from ``prevAt`` and ``t0`` from ``summary.t0``,
        then fold ``tailTicks`` and every live tick with the page's own rules.
        Hydrating that way reproduces the server's ``segs``/``runs`` exactly.
        """
        at_segs, at_runs, at_prev = len(self.segs), len(self.runs), self.prev
        state = {"segs": [dict(s) for s in self.segs], "runs": list(self.runs),
                 "open": dict(self.open), "prev": self.prev, "t0": self.t0}
        open_at = [[pid, t0] for pid, t0 in self.open.items()]
        for _t, _seq, tick in self.pending:
            _tp_step(state, tick)
        segs, runs = state["segs"], state["runs"]
        up = idle = down = stalls = stall_max = 0
        for s in segs:
            dms = s["t1"] - s["t0"]
            if s["kind"] == "up":
                up += dms
            elif s["kind"] == "idle":
                idle += dms
            else:
                stalls += 1
                down += dms
                stall_max = max(stall_max, dms)
        tail = [tick for _t, _seq, tick in self.pending]
        return {"segs": segs, "runs": runs,
                # arrays of pairs (DATA-MODEL-13), like every other ordered map
                "open": [[pid, t0] for pid, t0 in state["open"].items()],
                "openAt": open_at, "prevAt": at_prev,
                "atSegs": at_segs, "atRuns": at_runs,
                "summary": {"t0": state["t0"], "end": state["prev"],
                            "upMs": up, "idleMs": idle, "downMs": down,
                            "stallCount": stalls, "stallMax": stall_max,
                            "live": bool(state["open"])},
                "tailTicks": tail,
                "tailFrom": tail[0]["t"] if tail else state["prev"]}


class Fold:
    """The ONE fold (GD-F): what ``/tasks`` reports and what a v2 snapshot is.

    Two state families live here on purpose:

    * ``plan_states`` / ``last`` / ``tok`` reproduce ``replay_plan_states``
      EXACTLY — badge events only, last-event-wins in file order, the
      continuation reopen — because that trio is what ``/tasks`` folds into a
      verdict and ``test_server.py:127-209`` pins it. The incremental fold is
      tested equal to the reference implementation over a generated stream.
    * ``plans`` is the card fold — everything ``monitor.html`` renders
      (``onEvent`` :601-674, ``upsertAgent`` :496-525, ``freezePlan`` :570-577)
      — and it is what the snapshot serialises. It is deliberately faithful to
      the PAGE, so a hydrated view equals a replayed one: notably a card is
      promoted ``queued``->``running`` on the chip path only (``:657``), which
      is where the page does it, and never by a quiet token tick.

    R-58: every badge here is COPIED from an event. Nothing synthesizes one.
    Tokens follow GD-C: the top-level ``tokens`` is a delta (summed into the
    plan's counters, which is what the page accumulates), ``agent.tokens`` is
    the agent's ABSOLUTE running total (last-wins on ANY event, never summed).
    """

    def __init__(self):
        self.plans: dict = {}
        self.plan_states: dict = {}
        self.last = None
        self.tok = {"in": 0, "out": 0, "cached": 0, "cache_write": 0}
        self.parse_failures = 0
        self.ev_count = 0
        self.quiet_count = 0
        self.plan_total = 0
        self.roster = []             # planned sub-plan roster (latest wins)
        self.log_truncated = False   # set by snapshot(): did the budget cut?
        self.tp = TimePlan()

    # -- card helpers -------------------------------------------------------
    def _plan(self, pid):
        p = self.plans.get(pid)
        if p is None:
            p = {"title": None, "state": "queued", "firstTs": None, "lastTs": None,
                 "lastMs": None,
                 "tok": {"in": 0, "out": 0, "cached": 0, "write": 0},
                 "stages": {}, "agents": {}, "roles": {},
                 "log": collections.deque(maxlen=LOG_KEEP_PER_PLAN), "logTotal": 0}
            self.plans[pid] = p
        return p

    @staticmethod
    def _freeze(p) -> None:
        """monitor.html `function freezePlan` — a closing card freezes its
        still-running rows."""
        for row in p["agents"].values():
            if row["state"] == "running":
                row["state"] = "stale"
        for node in p["roles"].values():
            if node["state"] == "running":
                node["state"] = "stale"

    @staticmethod
    def _upsert_agent(p, a, ts, ts_ms) -> None:
        aid = a.get("id")
        row = p["agents"].get(aid)
        if row is None:
            # `ctx: None`, never `{}`: an empty dict is truthy-shaped downstream
            # and invites a `{"used": 0}` reconstruction, and 0 is the one lie
            # this reading may never tell (GD-LC-4: unknown is the key being
            # ABSENT on the wire; `None` is its fold-internal spelling).
            row = {"label": a.get("label") or aid, "started": a.get("started") or ts,
                   "state": "running", "runtime": None, "finishedMs": None,
                   "tokens": None, "ctx": None}
            p["agents"][aid] = row
        if a.get("started"):
            row["started"] = a["started"]
        state = a.get("state")
        if state:
            row["state"] = state
            if state in ("done", "failed") and not row["finishedMs"] and ts_ms is not None:
                row["finishedMs"] = ts_ms      # first terminal event stamps it
        if a.get("tokens"):
            row["tokens"] = a["tokens"]        # ABSOLUTE, last-wins (GD-C)
        if a.get("ctx"):
            # Last-wins, WHOLE-OBJECT REPLACE, no merge (GD-LC-9). Do not read
            # symmetry into the line above: `tokens` is cumulative SPEND and
            # deliberately monotonic, while `ctx` is a LEVEL at an instant and
            # NON-monotonic — a compaction legitimately lowers it, so nothing
            # here maxes, sums or clamps it (the D7 monotone clamp must never
            # touch it). A partial merge is the other trap: it could keep a
            # stale `cap` alive across a model switch. No staleness logic here
            # either — ship `used`/`at` and let the page decide, so the two
            # folds have exactly one new assignment to agree on.
            row["ctx"] = a["ctx"]
        if a.get("runtime"):
            row["runtime"] = a["runtime"]
        # monitor.html `function upsertAgent`, key for key: the ROLE falls back to the id when
        # there is no label, the ATTEMPT never does (`parseInt((a.label ||
        # "").split(" #")[1]) || 1`). An id carrying a `#` would otherwise be
        # read as an attempt number here and not on the page.
        role = (a.get("label") or aid or "").partition(" #")[0]
        _, _, rest = (a.get("label") or "").partition(" #")
        # ...and `parseInt` itself, not an approximation of it: it skips leading
        # whitespace, accepts one sign, then takes leading ASCII digits only
        # (never `str.isdigit()`, which also accepts non-ASCII digit characters
        # JS would stop at). `parseInt("gate # 3".split(" #")[1])` is 3 on the
        # page, so it is 3 here.
        rest = rest.lstrip()
        sign = -1 if rest[:1] == "-" else 1
        if rest[:1] in ("+", "-"):
            rest = rest[1:]
        digits = ""
        for ch in rest:
            if "0" <= ch <= "9":
                digits += ch
            else:
                break
        attempt = sign * int(digits) if digits else 1
        node = p["roles"].get(role)
        if node is None:
            node = {"state": "running", "attempt": 1}
            p["roles"][role] = node
        if state:
            node["state"] = state
        node["attempt"] = max(node["attempt"], attempt)

    # -- the fold -----------------------------------------------------------
    def apply(self, raw: bytes) -> bool:
        """Fold one raw line; return whether it parsed (R-10).

        The fold is the ONE place that parses a line, so it is also the one
        place that knows a line is poison. The flag rides out with the line to
        every subscriber: a v1 socket still sends it (one bad frame costs one
        event, exactly as it always has), and a v2 socket leaves it out of the
        array frame it would otherwise invalidate — without re-parsing.
        """
        try:
            ev = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.parse_failures += 1
            return False
        if not isinstance(ev, dict):
            self.parse_failures += 1
            return False
        pid = ev.get("plan")
        stage, state = ev.get("stage"), ev.get("state")
        tok = ev.get("tokens")

        # --- /tasks parity block: replay_plan_states, line for line ---------
        if stage in ("plan", "complete"):
            self.plan_states[pid] = state
            if (stage == "plan" and pid != "orchestrator"
                    and state in ("running", "queued")
                    and self.plan_states.get("orchestrator") in ("done", "failed")):
                self.plan_states["orchestrator"] = "running"
        elif (stage != "tokens" and state == "running" and pid == "orchestrator"
                and self.plan_states.get("orchestrator") in ("done", "failed")):
            self.plan_states["orchestrator"] = "running"
        if tok:
            self.tok["in"] += tok.get("in") or 0
            self.tok["out"] += tok.get("out") or 0
            self.tok["cached"] += tok.get("cached") or 0
            self.tok["cache_write"] += tok.get("cache_write") or 0
        quiet = bool(ev.get("quiet"))
        if not quiet:
            self.last = {k: ev[k] for k in ("ts", "plan", "stage", "state", "detail")
                         if k in ev}

        # --- card fold (the page's onEvent) ---------------------------------
        self.ev_count += 1
        if quiet:
            self.quiet_count += 1
        ts = ev.get("ts")
        ts_ms = parse_ts_ms(ts)
        # the timeplan sees EVERY event, quiet token ticks included
        # (PRIOR-ART-TOUCH-13: folding them away is data loss, not a saving)
        if ts_ms is not None:
            if stage == "plan" and pid and pid != "orchestrator":
                if state in ("running", "queued"):
                    self.tp.note(ts_ms, plan=pid, is_open=True)
                elif state in ("done", "failed"):
                    self.tp.note(ts_ms, plan=pid, is_open=False)
                else:
                    self.tp.note(ts_ms)
            elif stage == "complete":
                self.tp.note(ts_ms, close_all=True)
            else:
                self.tp.note(ts_ms)
        p = self._plan(pid)
        if ts_ms is not None:
            if p["firstTs"] is None:
                p["firstTs"] = ts
            if p["lastMs"] is None or ts_ms > p["lastMs"]:
                p["lastMs"], p["lastTs"] = ts_ms, ts
        if ev.get("title"):
            p["title"] = ev["title"]
        if ev.get("plans_total") is not None:
            try:
                n = int(float(ev["plans_total"]))
            except (TypeError, ValueError):
                n = None
            if n is not None and n > self.plan_total and n <= 9999:
                self.plan_total = n        # monotonic max, bounded (untrusted)
        if pid == "orchestrator" and isinstance(ev.get("roster"), list):
            # Planned sub-plan roster (driver-emitted, latest wins). Part of
            # the fold — a snapshot must carry it or hydration drops it
            # (FOLD_GEN 2). Untrusted: strings only, bounded count + length.
            self.roster = [str(x)[:300] for x in ev["roster"]
                           if isinstance(x, str) and x][:200]
        agent = ev.get("agent")
        if isinstance(agent, dict) and agent.get("id"):
            self._upsert_agent(p, agent, ts, ts_ms)
        logged = True
        if stage in ("plan", "complete"):
            p["state"] = state
            if state in ("done", "failed"):
                self._freeze(p)
                if stage == "complete":
                    # FRONTEND-3 / GD-F run-complete sweep: a run-level close
                    # settles every still-open sub-plan card. The server lacked
                    # this; the page has always done it (monitor.html, the
                    # FRONTEND-3 arm guarded by `if (ev.stage === "complete")`).
                    for other_id, other in self.plans.items():
                        if other_id == "orchestrator" or other is p:
                            continue
                        if other["state"] in ("queued", "running"):
                            other["state"] = state
                            self._freeze(other)
        elif stage == "tokens" and tok:
            p["tok"]["in"] += tok.get("in") or 0
            p["tok"]["out"] += tok.get("out") or 0
            p["tok"]["cached"] += tok.get("cached") or 0
            p["tok"]["write"] += tok.get("cache_write") or 0
            logged = not quiet          # a quiet delta is counters only, no log
        else:
            reopen = (pid == "orchestrator" and state == "running"
                      and p["state"] in ("done", "failed"))
            if p["state"] == "queued" or reopen:
                p["state"] = "running"
            p["stages"][stage] = state
        if logged:
            p["log"].appendleft({"ts": ts, "state": state, "stage": stage,
                                 "detail": ev.get("detail")})
            p["logTotal"] += 1
        return True

    # -- serialisation ------------------------------------------------------
    @staticmethod
    def _log_bytes(entry) -> int:
        return LOG_ENTRY_OVERHEAD + sum(
            len(entry[k]) for k in ("ts", "state", "stage", "detail")
            if isinstance(entry[k], str))

    def _log_alloc(self) -> dict:
        """Global log budget, floor first, then largest-recent-first (GD-F).

        Two passes over the plans: the floor pass walks them SMALLEST-first, the
        remainder pass largest-first. Newest lines are taken first within a plan,
        and ``logTotal`` discloses what was cut so the page can offer the
        ``?snap=0`` full replay.

        The floor is ``min(LOG_PLAN_FLOOR, budget // plans)`` rather than a flat
        20, because a flat floor is not a floor at the scale that motivated the
        global budget: at M9(g)'s stated 145-plan case, 145 x 20 = 2,900 lines
        against a 1,500-line budget, and a floor pass walking largest-first
        hands the whole budget to the 75 plans that need it least — the other 70
        cards render blank. Dividing keeps every card non-empty and leaves the
        remainder pass to spend what is left where the activity is.
        """
        alloc = {pid: 0 for pid in self.plans}
        lines_left, bytes_left = LOG_BUDGET_LINES, LOG_BUDGET_BYTES
        floor = min(LOG_PLAN_FLOOR,
                    max(1, LOG_BUDGET_LINES // max(1, len(self.plans))))
        order = sorted(self.plans.items(), key=lambda kv: -len(kv[1]["log"]))
        for want_floor in (True, False):
            for pid, p in (reversed(order) if want_floor else order):
                log = p["log"]
                have = alloc[pid]
                room = (min(floor, len(log)) - have if want_floor
                        else len(log) - have)
                for i in range(have, have + max(0, room)):
                    if lines_left <= 0:
                        break
                    b = self._log_bytes(log[i])
                    if bytes_left - b < 0:
                        break
                    lines_left -= 1
                    bytes_left -= b
                    alloc[pid] += 1
        return alloc

    def snapshot(self, sig: str, offset: int) -> dict:
        """The GD-F snapshot: every ordered map an ARRAY OF PAIRS.

        Arrays, never objects: card order, chip order, agent-row order and the
        flow-strip topology are all insertion order, and a JS engine hoists
        integer-like object keys to the front — a plan literally named "42"
        would silently reorder a hydrated page but not a replayed one
        (DATA-MODEL-13). Nothing client-derivable ships (no stats tiles, no
        totals line), and no line numbers ever: the cursor is (sig, byte
        offset) and this payload is not a source for Mongo's positional
        ``legacy:<task>#<line>`` keyspace (DATA-MODEL-6).
        """
        alloc = self._log_alloc()
        self.log_truncated = False
        plans = []
        for pid, p in self.plans.items():
            n = alloc.get(pid, 0)
            log = p["log"]
            if n < p["logTotal"]:
                self.log_truncated = True
            plans.append([pid, {
                "title": p["title"], "state": p["state"],
                "firstTs": p["firstTs"], "lastTs": p["lastTs"],
                "tok": p["tok"],
                "stages": [[s, st] for s, st in p["stages"].items()],
                "agents": [[aid, row] for aid, row in p["agents"].items()],
                "roles": [[r, node] for r, node in p["roles"].items()],
                # newest-first, already in that order in the ring; one list()
                # walk rather than n deque index lookups (which are O(n) each)
                "log": list(log)[:n],
                "logTotal": p["logTotal"],
            }])
        return {"m": "snapshot", "kind": "monitor-snapshot", "foldGen": FOLD_GEN,
                "sig": sig, "offset": offset,
                "evCount": self.ev_count, "quietCount": self.quiet_count,
                "planTotal": self.plan_total, "parseFailures": self.parse_failures,
                "logTruncated": self.log_truncated, "roster": self.roster,
                "plans": plans, "timeplan": self.tp.build()}

    def status(self) -> dict:
        """``task_status``'s verdict, from the incremental fold (same rules)."""
        orch = self.plan_states.get("orchestrator")
        plans = [s for p, s in self.plan_states.items() if p != "orchestrator"]
        if orch in ("done", "failed"):
            status = orch
        elif "running" in plans:
            status = "running"
        elif "failed" in plans:
            status = "failed"
        elif plans and all(s == "done" for s in plans):
            status = "done"
        elif self.last:
            status = "running"
        else:
            status = "empty"
        return {"status": status, "last": self.last, "tokens": dict(self.tok)}


def path_digest(path: str) -> str:
    """A stable, non-reversible handle for a filesystem path (AUDIT-15 parity).

    `/health` is the ONE unauthenticated route, and an events path spells out
    `<home>/<user>/<project>/.touch/local-orchestrators/<task>/events.jsonl` —
    the machine's username, the directory the work lives in, and the run roster,
    to anybody who can reach the port. The aggregator hashes every path on its
    own `/health` for exactly this reason (`Api.target_hash`), and item 05 is
    written as parity with that posture, so the monitor hashes too.

    Twelve hex characters is plenty to correlate two probes of the same server
    and nowhere near enough to walk back to a path. Reimplemented here rather
    than imported: this module must run with nothing else on PYTHONPATH.
    """
    return hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()[:12]


def health_payload() -> dict:
    """`/health`: liveness plus the per-stream parse-failure counters (R-10).

    The counters are a by-product of the `/tasks` stream scan (that is what makes
    them free), so a probe taken before the first scan honestly reports zero — it
    means "nothing scanned yet", not "no bad lines". A dashboard polls `/tasks`
    continuously, so every counter here is as current as that stream's last scan,
    and a stream that stops being scannable (deleted, rotated) drops out of the
    map instead of contributing forever.
    """
    # Additive (SERVER-READ-11): the read side's own accounting, so the perf
    # work is externally observable and testable — bytes actually read, events
    # actually folded, blob/snapshot sizes, clients. These are the counters the
    # work-based tests assert on; they cost nothing, because the registry
    # already holds them. Keyed by task NAME, which is a folder basename and
    # therefore not unique: an $ORCH_STATE_DIR outside TASKS_ROOT can share one
    # with a folder inside it. On such a collision the newest registration keeps
    # the friendly name and the earlier ones move to `path_digest(their path)` —
    # every stream stays visible instead of one silently overwriting the other,
    # and the tie-breaker is a digest rather than the absolute path it used to
    # be, because this route answers without a token (see path_digest). The task
    # NAME is deliberately kept in the clear: it is a label the operator chose,
    # it carries no username and no directory, and without it /health cannot say
    # WHICH stream a counter belongs to — which is the entire reason a
    # supervisor probes an untokened route in the first place.
    #
    # ``events_sent`` counts EVENTS PUT ON A WIRE (M8): v1 frames, v2 array
    # frames and a ``snap=0`` replay all add their event count. A v2 SNAPSHOT
    # adds nothing to it — it is a fold, not a sequence of events — so a
    # snapshot-heavy deployment would otherwise look idle. ``snapshots_sent``
    # is that missing half; the two together are what the perf tests assert on.
    streams: dict = {}
    named: dict = {}
    for s in Stream._REGISTRY.values():
        if s.task in named:
            streams[path_digest(named[s.task].path)] = streams.pop(s.task)
        named[s.task] = s
        streams[s.task] = s.health()
    # PARSE_FAILURES stays keyed by absolute path IN MEMORY — that is what
    # task_status() can pop when a stream disappears — and is digested only on
    # the way out, where the untokened reader is.
    # The file plane's own two fields, and they are the whole disclosure: counts
    # and booleans, no path, no filename (SERVER-10, SECURITY-1). `memoryWrite`
    # is the string "on"/"off" rather than a bool because it answers an
    # operational question — "is the port in front of me a viewer or an editor?"
    # — and a supervisor reads it in a log line.
    return {"status": "ok",
            "parse_failures_total": sum(PARSE_FAILURES.values()),
            "parse_failures": {path_digest(p): n for p, n in PARSE_FAILURES.items()},
            "streams": streams,
            "memory": memory_health(),
            "memoryWrite": "on" if MEMORY_WRITE else "off",
            "stats": {"ws_clients": STATS["ws_clients"],
                      "ws_active": STATS["ws_active"],
                      "events_sent": STATS["events_sent"],
                      "snapshots_sent": STATS["snapshots_sent"],
                      "page_hits": STATS["page_hits"],
                      "uptime_s": int(time.monotonic() - STATS["started"]),
                      "fold_gen": FOLD_GEN}}


def task_status(events_path: str) -> dict:
    """Overall run status + last meaningful event, for the home-grid tile.

    Replays the whole stream the same way the dashboard does — badge events
    (stage ``plan``/``complete``) set per-plan state, last-event-wins in file
    order, continuation activity reopens a stale orchestrator close — then folds
    those into one verdict. The reserved ``orchestrator`` card is authoritative:
    the run-level ``complete done|failed`` that nothing reopened marks the run
    finished. Until it lands, LIVE ACTIVITY WINS: any running plan means the
    flow is running (same rule as the stats page's flow tile — a plan that
    already exhausted its attempts must not flag the whole run failed while
    later loops are still working); with nothing running, a failed plan wins,
    else all-done folds to done.
    """
    try:
        st = os.stat(events_path)
    except OSError:
        # A stream that no longer exists (deleted or rotated after a poisoned
        # scan) must not keep contributing to `/health`'s parse_failures_total
        # for the life of the server — there is nothing left to fix (m-2).
        PARSE_FAILURES.pop(events_path, None)
        return {"status": "empty", "last": None, "tokens": {"in": 0, "out": 0}}
    key = (st.st_mtime_ns, st.st_size)
    cached = _STATUS_CACHE.get(events_path)
    if cached and cached[0] == key:
        return cached[1]
    try:
        plan_states, last, tokens, failures = replay_plan_states(events_path)
    except OSError:
        PARSE_FAILURES.pop(events_path, None)  # unreadable now: same rule (m-2)
        return {"status": "empty", "last": None, "tokens": {"in": 0, "out": 0}}
    if failures:
        PARSE_FAILURES[events_path] = failures
    else:
        PARSE_FAILURES.pop(events_path, None)
    tok_in, tok_out = tokens["in"], tokens["out"]
    tok_cached, tok_write = tokens["cached"], tokens["cache_write"]
    orch = plan_states.get("orchestrator")
    plans = [s for p, s in plan_states.items() if p != "orchestrator"]
    if orch in ("done", "failed"):
        status = orch
    elif "running" in plans:
        status = "running"
    elif "failed" in plans:
        status = "failed"
    elif plans and all(s == "done" for s in plans):
        # every plan card closed but the driver never closed the
        # orchestrator card — effectively finished, show it as such
        status = "done"
    elif last:
        status = "running"
    else:
        status = "empty"
    payload = {"status": status, "last": last,
               "tokens": {"in": tok_in, "out": tok_out, "cached": tok_cached,
                          "cache_write": tok_write}}
    _STATUS_CACHE[events_path] = (key, payload)
    return payload


async def tasks_payload_live() -> dict:
    """`/tasks` from the Stream registry: one stat per quiet task, per poll.

    The home grid polls this every 5 s for EVERY task folder. The old path
    re-read and re-parsed each whole stream whenever ``(mtime_ns, size)``
    changed — i.e. on every append, 48 ms per poll for a 5.6 MB stream, growing
    linearly forever (SERVER-READ-1 / DATA-MODEL-12). Here each task's stream
    advances by the appended bytes only, and a task that stopped emitting costs
    a single ``stat`` because ``size == offset`` is a no-op.
    """
    entries = []
    for name, d in discover_tasks().items():
        stream = Stream.get(os.path.join(d, "events.jsonl"))
        await stream.refresh(REFRESH_MIN_SECS)
        if stream.missing:
            entries.append({"name": name, "events": False, "mtime": 0,
                            "status": "empty", "last": None,
                            "tokens": {"in": 0, "out": 0}})
        else:
            entries.append({"name": name, "events": True, "mtime": stream.mtime,
                            **stream.status()})
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return {"default": DEFAULT_TASK, "tasks": entries}


def tasks_payload() -> dict:
    """The pre-registry full-scan payload: the fold's reference implementation.

    Kept callable (and equal to ``tasks_payload_live``) on purpose — the same
    reason ``replay_plan_states`` is kept: the incremental fold is tested for
    equality against it, and a divergence in either direction is a bug in the
    new path, not a licence to edit the old one.
    """
    entries = []
    for name, d in discover_tasks().items():
        events = os.path.join(d, "events.jsonl")
        try:
            st = os.stat(events)
            entries.append({"name": name, "events": True, "mtime": st.st_mtime,
                            **task_status(events)})
        except OSError:
            entries.append({"name": name, "events": False, "mtime": 0,
                            "status": "empty", "last": None, "tokens": {"in": 0, "out": 0}})
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return {"default": DEFAULT_TASK, "tasks": entries}


def resolve_task(query: str):
    """``(name, state_dir, known)`` for ``?task=``, without the silent fallback.

    An UNKNOWN task name is reported as such (``known=False``) instead of being
    answered with the default task's data. Serving another task's stream and
    artifacts under the name the caller asked for is a wrong answer, not a
    convenience — it is how a live server hands out an 18 KB artifact listing
    for a task that does not exist (SERVER-READ-10). Only an ABSENT ``?task=``
    falls back to ``STATE_DIR``. Names only ever match discovered directories,
    so there is no traversal here either way.
    """
    task = urllib.parse.parse_qs(query).get("task", [None])[0]
    if task is None:
        return DEFAULT_TASK, STATE_DIR, True
    tasks = discover_tasks()
    if task in tasks:
        return task, tasks[task], True
    return task, STATE_DIR, False


def resolve_task_dir(query: str) -> str:
    """State dir for ?task=<name>; names only match discovered dirs (no traversal).

    Keeps the historical fallback (unknown name -> default stream) because the
    v1 websocket contract depends on it (``test_ws_e2e.py`` pins it as the
    compatibility floor). The HTTP routes and v2 use ``resolve_task`` instead.
    """
    task = urllib.parse.parse_qs(query).get("task", [None])[0]
    return discover_tasks().get(task, STATE_DIR)


def resolve_events_path(query: str) -> str:
    return os.path.join(resolve_task_dir(query), "events.jsonl")


# Task-page artifacts: the final HTML report plus agent-written .md handoff
# notes (findings/, reviews/, plan/, ...). Extension-whitelisted both when
# listing and when serving.
ARTIFACT_EXTS = {".md", ".html", ".htm"}


def task_artifacts(state_dir: str) -> list:
    """List report HTMLs + .md notes under a task folder, report(s) first.

    Hidden entries and __pycache__ are skipped and paths are task-relative
    with forward slashes. The walk is bounded (depth 4, 300 files) so a
    runaway folder cannot stall the endpoint.
    """
    out = []
    base = os.path.realpath(state_dir)
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(dirpath, base)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth >= 4:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d != "__pycache__")
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if fn.startswith(".") or ext not in ARTIFACT_EXTS:
                continue
            try:
                st = os.stat(os.path.join(dirpath, fn))
            except OSError:
                continue
            rel = fn if rel_dir == "." else os.path.join(rel_dir, fn)
            out.append({"path": rel.replace(os.sep, "/"),
                        "kind": "note" if ext == ".md" else "report",
                        "size": st.st_size, "mtime": st.st_mtime})
            if len(out) >= 300:
                return sorted(out, key=lambda a: (a["kind"] != "report", a["path"]))
    out.sort(key=lambda a: (a["kind"] != "report", a["path"]))
    return out


def safe_artifact_path(state_dir: str, rel: str):
    """Absolute path for a task-relative artifact, or None if not servable.

    Extension whitelist + realpath containment in the task dir, so a hostile
    ``?path=`` (.. traversal, absolute path, or a symlink pointing outside)
    can never read beyond the task folder.
    """
    if not rel or os.path.splitext(rel)[1].lower() not in ARTIFACT_EXTS:
        return None
    base = os.path.realpath(state_dir)
    full = os.path.realpath(os.path.join(base, rel))
    if not full.startswith(base + os.sep):
        return None
    return full if os.path.isfile(full) else None


def read_config() -> dict:
    for base in (STATE_DIR, ROOT):
        try:
            with open(os.path.join(base, "orch-config.json")) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
    return {}


#: Flags that consume the NEXT argv element as their value.
VALUE_FLAGS = ("--allow-origin", "--allow-host")


def positional_args() -> list:
    """argv minus flags, and minus the values those flags consume.

    ``monitor_server.py 9001 --open --allow-origin http://127.0.0.1:9001`` must
    read 9001 as the port and nothing else: neither a flag nor an option's
    value is a positional, and reading either as a port is the ``invalid port
    from argv`` exit at startup.
    """
    out, skip = [], False
    for arg in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if arg in VALUE_FLAGS:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        out.append(arg)
    return out


def resolve_port() -> int:
    """Port: argv > $ORCH_PORT > orch-config.json (state dir, then module dir) > 8931.

    A non-integer argv/env exits cleanly with a one-line message rather than a
    raw ``ValueError`` traceback at import (SERVER-2). Flag arguments (``--open``
    and friends) are not positional and are skipped here, not mis-parsed as a
    port.
    """
    positional = positional_args()
    for source, label in ((positional[0] if positional else None, "argv"),
                          (os.environ.get("ORCH_PORT"), "ORCH_PORT")):
        if source:
            try:
                return int(source)
            except ValueError:
                sys.exit(f"invalid port from {label}: {source!r}")
    try:
        return int(read_config().get("port") or 8931)
    except (TypeError, ValueError):
        return 8931


PORT = resolve_port()
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# --------------------------------------------------------------------------
# Security posture (GD-T8) — parity with the aggregator, reimplemented HERE.
#
# The aggregator (`aggregator/server.py`) already ships this exact posture, but
# the two are NOT allowed to share code: this module is a standalone pair of
# single-file daemons that must run with nothing else on PYTHONPATH. So the
# rules are re-stated in-module and kept honest by tests, not by an import.
#
# 1. bind loopback by default; `--open` / $ORCH_BIND is the explicit opt-in
#    (no skill and no wrapper may pass it on a user's behalf);
# 2. one per-boot token, printed at startup and written 0600 into the resolved
#    state dir; required on /tasks, /artifacts, /file and the WS upgrade;
# 3. an Origin/Host allowlist on the upgrade, extensible from the command line
#    (`--allow-host`, `--allow-origin`) so the 403 has a documented way out;
# 4. /health and the page itself stay open — /health so a supervisor can probe
#    a server it has no token for, the page because it is the static HTML that
#    *carries* the token in its query string. Because /health is open, every
#    filesystem path it would otherwise publish is hashed (`path_digest`),
#    matching what the aggregator does on its own open route.
# --------------------------------------------------------------------------
DEFAULT_HOST = "127.0.0.1"
OPEN_HOST = "0.0.0.0"
#: 256 bits of entropy, per boot. `token_urlsafe` takes *bytes*.
TOKEN_BYTES = 32
#: Routes served without a token.
OPEN_ROUTES = frozenset({"/health"})
AUTH_HEADER = "x-orch-token"
AUTH_QUERY = "token"


def resolve_host() -> str:
    """Bind address: ``--open`` > $ORCH_BIND > loopback (GD-T8).

    Loopback is not a default anyone may drift off by accident: reaching
    0.0.0.0 takes an explicit flag or an explicit env var, and both are named
    in the startup line so an operator can see which one is in force.
    """
    if "--open" in sys.argv[1:]:
        return OPEN_HOST
    env = (os.environ.get("ORCH_BIND") or "").strip()
    return env or DEFAULT_HOST


HOST = resolve_host()
#: The per-boot token. Worthless the moment this process exits, which is what
#: makes it acceptable to carry in a URL query string — a browser cannot set a
#: header on a `new WebSocket(...)` handshake or a top-level navigation.
TOKEN = secrets.token_urlsafe(TOKEN_BYTES)
AUTH_REJECTIONS = {"token": 0, "origin": 0}


def host_name(value: str) -> str:
    """The Host header's NAME, port stripped (``[::1]:8931`` -> ``[::1]``).

    The allowlist is a list of names, not of ports: a rebinding attack is a page
    on `evil.example` resolving to 127.0.0.1, and it is the name that gives it
    away. Pinning the port too would only mean the check breaks whenever the
    daemon is reached on a port the module-level PORT does not know about — an
    ephemeral test bind, an SSH tunnel — for no extra safety.
    """
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value[:value.index("]") + 1] if "]" in value else value
    return value.rsplit(":", 1)[0] if ":" in value else value


def flag_values(flag: str, env_var: str) -> list:
    """Values of a repeatable ``--flag V`` / ``--flag=V``, plus a CSV env var.

    Both spellings are honoured on purpose. ``positional_args()`` already drops
    ``--flag=V`` from the port scan (it starts with ``-``), so reading only the
    space-separated form would leave the equals form *accepted and inert* — the
    worst of the three outcomes, because nothing on screen says the allowlist
    the operator just typed was ignored.
    """
    values, argv = [], sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == flag and i + 1 < len(argv):
            values.append(argv[i + 1])
        elif arg.startswith(flag + "="):
            values.append(arg[len(flag) + 1:])
    values += (os.environ.get(env_var) or "").split(",")
    return [v.strip() for v in values if v.strip()]


def allowed_hosts() -> frozenset:
    """Host NAMES this bind will answer on (the DNS-rebinding half).

    Empty for an open bind — including when ``--allow-host`` was passed: the
    operator reaches it through whatever address the sandbox published, so a
    derived allowlist would be a list of guesses and a partial one would refuse
    the very addresses `--open` exists to serve. The same-origin rule below
    still applies — an open bind is not an open Origin policy.

    For every other bind the derived set is the bind address plus the loopback
    names, EXTENDED by ``--allow-host`` / $ORCH_ALLOW_HOST. That extension is
    not decoration: an operator who binds a reachable address and then browses
    the box by NAME presents a Host header nothing derived can predict, and this
    gate runs before the Origin rules — without an escape hatch here, the Origin
    allowlist below could never be reached (a flag that parses and cannot
    possibly take effect).
    """
    if HOST in (OPEN_HOST, "::", ""):
        return frozenset()
    extra = flag_values("--allow-host", "ORCH_ALLOW_HOST")
    return frozenset(n.lower() for n in
                     (HOST, "localhost", "127.0.0.1", "[::1]", *extra))


def allowed_origins() -> frozenset:
    """Extra allowed Origins, from repeatable ``--allow-origin`` / $ORCH_ALLOW_ORIGIN."""
    values = flag_values("--allow-origin", "ORCH_ALLOW_ORIGIN")
    derived = {v.rstrip("/").lower() for v in values}
    for name in allowed_hosts():
        derived.add(f"http://{name}:{PORT}")
    return frozenset(derived)


HOSTS = allowed_hosts()
ORIGINS = allowed_origins()


def presented_token(headers: dict, query: str, *, header_only: bool = False):
    """The token a request carries, from any of the three carriers.

    ``header_only`` drops the query-string carrier, and it is set for exactly one
    family: the memory WRITE verbs (W4). The page's own URL carries the token in
    its query string — so it is in the address bar, in history, in a screenshot,
    in a sandboxed document's `location.search` and in any `Referer` that
    escapes — and a mutation that accepts that carrier is a mutation a bookmark,
    a prefetch or an `<img src>` can perform. A header cannot be set by any of
    those.
    """
    auth = (headers or {}).get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    header = (headers or {}).get(AUTH_HEADER)
    if header:
        return header.strip()
    if header_only:
        return ""
    values = urllib.parse.parse_qs(query or "").get(AUTH_QUERY) or []
    return values[0] if values else ""


def token_ok(route: str, headers: dict, query: str, *,
             header_only: bool = False) -> bool:
    """True when the request may proceed. Constant-time, always.

    The comparison runs even when nothing was presented — against the empty
    string rather than short-circuiting — so a missing token and a wrong one
    take the same path and the same time.
    """
    if route in OPEN_ROUTES:
        return True
    presented = presented_token(headers, query, header_only=header_only) or ""
    ok = hmac.compare_digest(presented.encode("utf-8", "replace"),
                             TOKEN.encode("utf-8"))
    if not ok:
        AUTH_REJECTIONS["token"] += 1
    return ok


def origin_refusal(headers: dict, *, allow_missing_origin: bool = True):
    """None when the request may proceed, else a one-line reason (403).

    Runs on the WS upgrade and — since the file plane exists — on every memory
    route, reads included: `Api`-style enforcement only at the upgrade left every
    plain-HTTP route DNS-rebindable, and under rebinding a request is same-origin,
    so no preflight happens and a required custom header is settable. The Host
    NAME check below is the only rule that survives that, which is why it now
    runs on more than one route (SECURITY-3).

    ``allow_missing_origin=False`` is passed by the memory WRITE verbs and by
    nothing else (W3): rule 3 exists for non-browser READERS, and a mutation that
    accepts an absent Origin accepts a cross-site form post.

    Four rules, in order, and the last is what makes a default install safe
    with zero configuration:

    1. an EXPLICITLY allow-listed Origin passes, and passes first. It is checked
       ahead of the Host gate because the two travel together — a browser that
       sends ``Origin: http://mybox:8931`` sends ``Host: mybox:8931`` — so a
       Host gate that returned before consulting ORIGINS would make
       ``--allow-origin`` unreachable, which is precisely the escape hatch it
       exists to be. Nothing is weakened: an Origin header cannot be forged by
       the page, so being on this list is the operator's own decision.
    2. the Host header must be on the allowlist (empty = open bind, skipped);
    3. a **missing** Origin passes: browsers always send one on a WS handshake,
       so an absent Origin means a non-browser client — which still had to
       present the token, and refusing it would break curl and this suite
       without closing anything a browser could open;
    4. otherwise the Origin's authority must equal the Host header — the page
       this server served, talking back to it.
    """
    headers = headers or {}
    host = (headers.get("host") or "").strip().lower()
    origin = (headers.get("origin") or "").strip()
    if origin and origin.rstrip("/").lower() in ORIGINS:
        return None
    if HOSTS and host_name(host) not in HOSTS:
        AUTH_REJECTIONS["origin"] += 1
        return (f"Host {host or '(absent)'} is not on the allowlist "
                f"(--allow-host / $ORCH_ALLOW_HOST extends it)")
    if not origin or origin.lower() == "null":
        if allow_missing_origin:
            return None
        AUTH_REJECTIONS["origin"] += 1
        return ("a write must carry an Origin header that matches the Host it was "
                "sent to (a missing Origin is accepted on reads only)")
    parsed = urllib.parse.urlsplit(origin)
    authority = parsed.netloc.lower() if parsed.scheme in ("http", "https") else ""
    if authority and authority == host:
        return None
    AUTH_REJECTIONS["origin"] += 1
    return f"Origin {origin} is not allowed"


def write_token_file(state_dir: str):
    """``<state dir>/monitor.json``, mode 0600 — the token's only resting place.

    Created 0600 from the start (never written then chmod'ed, which leaves a
    window where the token is world-readable). Refuses outright inside a plugin
    cache; returns the path, or None when the write was refused or failed.
    """
    if in_plugin_cache(state_dir):
        return None
    path = os.path.join(state_dir, "monitor.json")
    payload = {"token": TOKEN, "host": HOST, "port": PORT, "pid": os.getpid(),
               "url": f"http://{'127.0.0.1' if HOST in (OPEN_HOST, '::', '') else HOST}"
                      f":{PORT}/?token={TOKEN}"}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(path, 0o600)
    except OSError:
        return None
    return path

STATS = {"started": time.monotonic(), "ws_clients": 0, "ws_active": 0,
         "events_sent": 0, "snapshots_sent": 0, "page_hits": 0}


def stats_line() -> str:
    """The shutdown line, read from the registry — zero I/O (SERVER-READ-14).

    This used to re-open and line-count the default task's file at exit (5.6 MB
    on the live host) and report only that one task. Every stream this server
    actually served has already been folded; asking the fold is free and it
    covers all of them.

    ``events streamed`` is raw events on the wire; a v2 snapshot is counted as
    ONE snapshot rather than as the events it folds (see ``health_payload``).
    """
    up = int(time.monotonic() - STATS["started"])
    uptime = (f"{up // 3600}h{up % 3600 // 60:02d}m" if up >= 3600
              else f"{up // 60}m{up % 60:02d}s" if up >= 60 else f"{up}s")
    folded = [s for s in Stream._REGISTRY.values() if s.fold.ev_count]
    folded.sort(key=lambda s: -s.fold.ev_count)
    if folded:
        shown = ", ".join(f"{s.task} {s.fold.ev_count:,}" for s in folded[:3])
        tail = f" (+{len(folded) - 3} more)" if len(folded) > 3 else ""
        streams = f"{len(folded)} streams folded: {shown}{tail}"
    else:
        streams = f"no stream folded (default task {EVENTS})"
    return (f"stopped after {uptime} · {STATS['ws_clients']} ws clients "
            f"({STATS['ws_active']} still connected) · {STATS['events_sent']:,} events streamed · "
            f"{STATS['snapshots_sent']:,} snapshots · "
            f"{STATS['page_hits']} page loads · {streams}")


def ws_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += n.to_bytes(2, "big")
    else:
        header.append(127)
        header += n.to_bytes(8, "big")
    return bytes(header) + payload


def parse_client_frames(buf: bytearray) -> bool:
    """Consume whole client->server frames from ``buf`` in place.

    Returns True if a CLOSE frame (opcode 0x8) was seen. Client frames are
    always masked (RFC 6455); we only need enough parsing to detect CLOSE and
    to skip over pong/other frames so a following CLOSE in the same read is not
    missed. Incomplete trailing bytes are left in ``buf`` for the next read.
    """
    saw_close = False
    while len(buf) >= 2:
        opcode = buf[0] & 0x0F
        masked = buf[1] & 0x80
        length = buf[1] & 0x7F
        idx = 2
        if length == 126:
            if len(buf) < idx + 2:
                break
            length = int.from_bytes(buf[idx:idx + 2], "big")
            idx += 2
        elif length == 127:
            if len(buf) < idx + 8:
                break
            length = int.from_bytes(buf[idx:idx + 8], "big")
            idx += 8
        if masked:
            idx += 4  # 4-byte masking key
        if len(buf) < idx + length:
            break  # frame body not fully arrived yet
        del buf[:idx + length]
        if opcode == 0x8:
            saw_close = True
    return saw_close


async def drain_client(reader: asyncio.StreamReader, closed: asyncio.Event) -> None:
    """Discard incoming frames (pongs); flag close on CLOSE frame/EOF so the writer stops."""
    buf = bytearray()
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            buf += data
            if parse_client_frames(buf):
                break
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        closed.set()


def read_frames(events_path: str, offset: int):
    """Read complete newline-terminated records appended since ``offset``.

    Returns ``(frames, new_offset)`` where ``frames`` is the list of stripped
    non-empty lines up to the last ``\\n`` and ``new_offset`` advances by exactly
    those bytes, leaving any incomplete trailing line for the next tick (D5).
    A negative sentinel offset of ``-1`` signals truncation (``size < offset``);
    the caller closes the stream so the client reconnects cleanly (D10).
    """
    try:
        size = os.path.getsize(events_path)
    except OSError:
        return [], offset
    if size < offset:  # truncated/rotated under a live socket
        return [], -1
    if size <= offset:
        return [], offset
    with open(events_path, "rb") as f:
        f.seek(offset)
        data = f.read()
    nl = data.rfind(b"\n")
    if nl == -1:
        return [], offset  # no complete line yet; defer the partial tail
    complete = data[:nl + 1]
    return split_lines(complete), offset + len(complete)


def split_lines(complete: bytes) -> list:
    """Split a complete-line byte prefix the way every other reader here does.

    ``bytes.splitlines()`` breaks on \\v, \\f, \\x1c-\\x1e and U+2028/9 as well as
    \\n, so ONE physical line carrying a stray control byte became TWO frames,
    both invalid JSON (SERVER-READ-9). Records are newline-delimited by
    definition — split on ``b"\\n"`` only, exactly like ``legacy.py`` and
    ``tailer.py``, and drop the empty trailing element plus any blank lines.
    """
    return [line for line in (ln.strip() for ln in complete.split(b"\n")) if line]


def split_records(complete: bytes, base: int) -> list:
    """``split_lines`` plus each line's END byte offset in the file.

    Returns ``[(line, end_offset)]``. The offsets are what makes a capped tick
    resumable: the cursor published after a partial tick is the end offset of
    the LAST line actually sent, so the continuation has no gap and no
    duplicate. They are computed from the raw split (blank lines included in
    the arithmetic, excluded from the output) — never by re-adding stripped
    line lengths, which blank lines and trailing ``\\r`` would silently skew.
    """
    out = []
    pos = base
    for piece in complete.split(b"\n"):
        pos += len(piece) + 1
        line = piece.strip()
        if line:
            out.append((line, pos))
    return out


def read_records(events_path: str, offset: int):
    """``read_frames`` with per-line end offsets: ``(records, new_offset)``.

    Same torn-tail rule (D5, DATA-MODEL-4): everything after the last ``\\n`` is
    deferred and the offset never advances into it, so a snapshot built here
    can never land INSIDE a line the writer is still finishing. ``-1`` is the
    truncation sentinel.
    """
    try:
        size = os.path.getsize(events_path)
    except OSError:
        return [], offset
    if size < offset:
        return [], -1
    if size <= offset:
        return [], offset
    with open(events_path, "rb") as f:
        f.seek(offset)
        data = f.read()
    nl = data.rfind(b"\n")
    if nl == -1:
        return [], offset
    complete = data[:nl + 1]
    return split_records(complete, offset), offset + len(complete)


REPLAY_WINDOW = 1024 * 1024     # bytes per disk read of a v2 batched replay


def read_window(path: str, start: int, stop: int, max_bytes: int = REPLAY_WINDOW):
    """Records in ``[start, stop)``, reading at most ``max_bytes`` at a time.

    ``read_records`` reads to EOF, which is fine for a tail step and wrong for
    a full replay: ``?snap=0`` is what an operator reaches for when something
    is ALREADY wrong, and it must not be the one path that materialises a 19 MB
    stream as a Python list (SERVER-READ-5, M9's "never materialise all lines").
    This walks the prefix in windows instead and never reads past ``stop`` — the
    fold's offset — so what goes out stays a byte PREFIX of what the fold
    covers while the file keeps growing underneath it.

    Returns ``(records, next_start)``. A record longer than the window grows the
    read rather than being skipped; a window with no newline left before
    ``stop`` ends the walk.
    """
    if start >= stop:
        return [], start
    span = min(max_bytes, stop - start)
    try:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(span)
            while data.rfind(b"\n") == -1 and start + len(data) < stop:
                span = min(span * 2, stop - start)
                f.seek(start)
                data = f.read(span)
    except OSError:
        return [], stop
    nl = data.rfind(b"\n")
    if nl == -1:
        return [], stop
    complete = data[:nl + 1]
    return split_records(complete, start), start + len(complete)


def stream_sig(events_path: str) -> str:
    """Content identity: first 16 hex of sha256 over the first 4 KB.

    Server-local ``(dev, ino)`` catches rotation between polls but is not
    presentable to a client; the sig is, and it survives a server restart. It
    is what turns the documented wipe-and-rerun (delete, re-seed, restart —
    monitoring.md) from "resume into a foreign stream at the same byte offset"
    into "sig mismatch, rebuild" (DATA-MODEL-5).
    """
    try:
        with open(events_path, "rb") as f:
            head = f.read(SIG_BYTES)
    except OSError:
        return ""
    return hashlib.sha256(head).hexdigest()[:16]


TAIL_CHECK_BYTES = 64


def _scan(path: str, offset: int, known: dict, max_bytes: int = SCAN_WINDOW):
    """One blocking read step for a Stream: stat, identity, new records.

    Runs in a worker thread and reads at most ``max_bytes`` (plus the
    continuity window) per call: ``more`` in the result says whether there are
    complete bytes left, and the caller loops. A cold fold of a 45 MB stream is
    therefore a dozen bounded steps rather than one 45 MB list of records.

    Three identity checks, cheapest first, because an incremental fold that
    mistakes a DIFFERENT stream for a longer version of the same one keeps the
    old run's badges and token totals forever:

    1. ``size == offset`` with an unchanged inode AND an unchanged ``mtime_ns``
       is the hot path and returns before touching the file at all — that no-op
       is what turns a live ``/tasks`` poll from a 48 ms full re-scan into a
       stat (SERVER-READ-1). An in-place rewrite always moves ``mtime_ns``, so
       it can never be mistaken for "nothing happened".
    2. ``size < offset``, a changed ``(dev, ino)`` or a changed content ``sig``
       each mean a new stream — truncation, rotation, or the documented
       wipe-and-rerun (DATA-MODEL-5). The sig is only an IDENTITY once it has
       been taken over a full ``SIG_BYTES``: below that it hashes the whole
       file, so it changes on every ordinary append, and comparing it there
       would fire the protocol's one destructive signal (full re-fold, every
       socket closed) roughly 20-40 times at the start of every run — the exact
       window in which someone opens the dashboard to watch a run begin. While
       either side of the comparison is short the sig is refreshed and NOT
       compared; tier 3 carries identity until the head settles.
    3. **Continuity at the cursor**: the last bytes we already folded must
       still be there, unchanged, immediately before ``offset``. A wipe-and-
       rerun whose first 4 KB happens to be identical (same header, same plan
       names) is otherwise read as an append — the old fold plus a mid-line
       slice of a foreign stream. The window rides along in the read we were
       going to do anyway, so it costs 64 bytes and no extra syscall.

    Any failure ⇒ ``reset``: a full re-fold from byte 0, never a stale answer.
    """
    try:
        st = os.stat(path)
    except OSError:
        return {"missing": True}
    dev_ino = (st.st_dev, st.st_ino)
    out = {"missing": False, "size": st.st_size, "mtime": st.st_mtime,
           "mtime_ns": st.st_mtime_ns, "dev_ino": dev_ino, "reset": False,
           "records": [], "new_offset": offset, "sig": known.get("sig", ""),
           "sig_short": known.get("sig_short", False), "more": False,
           "tail": known.get("tail", b"")}
    if (st.st_size == offset and dev_ino == known.get("dev_ino")
            and st.st_mtime_ns == known.get("mtime_ns")):
        return out

    def _slice(start: int, span: int) -> bytes:
        """``span`` bytes from ``start``, grown until a newline is in reach."""
        with open(path, "rb") as f:
            f.seek(start)
            buf = f.read(span)
            # a single record longer than the window would otherwise stall the
            # walk forever: grow the read instead of never finding a newline
            while buf.rfind(b"\n") == -1 and start + len(buf) < st.st_size:
                span *= 2
                f.seek(start)
                buf = f.read(span)
        return buf

    sig = stream_sig(path)
    out["sig"] = sig
    out["sig_short"] = st.st_size < SIG_BYTES
    known_sig = known.get("sig") or ""
    # comparable only when BOTH digests cover a full SIG_BYTES head
    sig_is_identity = (bool(known_sig) and not known.get("sig_short")
                       and not out["sig_short"])
    reset = (st.st_size < offset
             or (known.get("dev_ino") is not None and dev_ino != known["dev_ino"])
             or (sig_is_identity and sig != known_sig))
    tail = known.get("tail") or b""
    back = len(tail) if (offset and not reset) else 0
    try:
        data = _slice(0 if reset else max(0, offset - back), back + max_bytes)
    except OSError:
        return out
    if back:
        if data[:back] != tail:       # the bytes under our cursor moved
            reset = True
            try:
                data = _slice(0, max_bytes)
            except OSError:
                return out
        else:
            data = data[back:]
            if data.rfind(b"\n") == -1 and offset + len(data) < st.st_size:
                # ``_slice``'s growth loop was satisfied by the newline inside
                # the continuity prefix, so it cannot see that the NEW bytes
                # hold no complete line: a record longer than the window would
                # defer forever instead of growing the read. Retry without the
                # prefix (never on the normal path — this costs a read only
                # when one record exceeds ``max_bytes``).
                data = _slice(offset, max_bytes * 2)
    base = 0 if reset else offset
    nl = data.rfind(b"\n")
    out["reset"] = reset
    if nl == -1:                      # no complete line yet: defer the partial
        out["new_offset"] = base
        if reset:
            out["tail"] = b""
        return out
    complete = data[:nl + 1]
    out["records"] = split_records(complete, base)
    out["new_offset"] = base + len(complete)
    window = complete if reset else (tail + complete)
    out["tail"] = window[-TAIL_CHECK_BYTES:]
    out["more"] = out["new_offset"] < st.st_size
    return out


class Subscriber:
    """One live socket's view of a stream: a queue of (line, end_offset)."""

    __slots__ = ("queue", "closed", "pending_events")

    def __init__(self):
        self.queue = collections.deque()
        self.closed = False          # set on truncation/rotation: rebuild
        self.pending_events = 0


class Stream:
    """One shared, incremental fold per ``events.jsonl`` (M6).

    Everything that reads a stream reads it HERE: ``/tasks``, ``/health``, and
    every websocket. The registry holds ``(sig, dev_ino, byte_offset, fold,
    framed history)`` and advances by the appended bytes only — 0.33 ms for 124
    events against 48 ms for the full re-scan it replaces.

    Two laws hold this together:

    * **single-flight** — every refresh runs under this stream's
      ``asyncio.Lock`` behind a ``REFRESH_MIN_SECS`` stamp, so N dashboard tabs
      on one task cost ONE read, not N (SERVER-READ-3);
    * **no double count** — the fold, the framed-history blob and the tail
      offset all come from the same locked step, and a socket subscribes INSIDE
      that step. A line therefore lands in exactly one of {prelude, tail}
      (SERVER-READ-8, WS-PROTOCOL-6).
    """

    _REGISTRY: dict = {}

    def __init__(self, path: str):
        self.path = path
        self.task = os.path.basename(os.path.dirname(path)) or DEFAULT_TASK
        self.offset = 0
        self.size = 0
        self.mtime = 0.0
        self.mtime_ns = 0
        self.sig = ""
        self.sig_short = False       # sig taken over < SIG_BYTES: not an identity
        self.tail_bytes = b""        # bytes just before offset: cursor continuity
        self.dev_ino = None
        self.missing = True
        self.fold = Fold()
        self.generation = 0
        self.lock = asyncio.Lock()
        self.loop = None
        self.subscribers: set = set()
        self.tailer = None
        # framed-v1 history: an append-only blob of ws frames covering a
        # COMPLETE byte prefix of the file (blob_offset). Every v1 replay
        # writes it and then tails from exactly blob_offset (SERVER-READ-4/8).
        self.blob = bytearray()
        self.blob_offset = 0
        self.blob_lines = 0
        self.blob_framed = 0         # cumulative lines ever framed (work counter)
        self.blob_builds = 0
        self.blob_subs = 0
        self.blob_idle_at = None
        self.snap_cache = None       # (key, bytes, log_truncated)
        self.snap_truncated = False  # disclosure that goes with those bytes
        self.snap_builds = 0
        self.bytes_read = 0
        self.events_folded = 0
        self.refreshes = 0
        self.resets = 0
        self.last_refresh = 0.0      # monotonic
        self.last_refresh_wall = 0.0
        self.last_change = time.monotonic()

    # -- registry -----------------------------------------------------------
    @classmethod
    def get(cls, path: str) -> "Stream":
        path = os.path.abspath(path)
        stream = cls._REGISTRY.get(path)
        if stream is None:
            stream = cls(path)
            cls._REGISTRY[path] = stream
        return stream

    def sync_lock(self) -> asyncio.Lock:
        """This stream's lock, rebound if the running event loop changed.

        The registry outlives an event loop in exactly one place — a test file
        that calls ``asyncio.run()`` once per case against the same module —
        and an ``asyncio.Lock`` remembers the loop it was first awaited on. Any
        state that belongs to the dead loop (its lock, its subscribers, its
        tailer task) is dropped here rather than raising "bound to a different
        event loop" halfway through a connect. The FOLD is loop-independent and
        deliberately survives.
        """
        loop = asyncio.get_running_loop()
        if self.loop is not loop:
            self.loop = loop
            self.lock = asyncio.Lock()
            self.subscribers = set()
            self.tailer = None
        return self.lock

    # -- refresh ------------------------------------------------------------
    async def refresh(self, min_interval: float = 0.0) -> None:
        async with self.sync_lock():
            await self.refresh_locked(min_interval)

    async def refresh_locked(self, min_interval: float = 0.0) -> None:
        """Advance the fold to the end of the file, one bounded step at a time.

        One CALL is one refresh (the counter, the min-refresh stamp and the
        single-flight window all work per call); one STEP is at most
        ``SCAN_WINDOW`` bytes. A live append is one step; a cold fold of a long
        stream is several, and each one folds and broadcasts what it read
        before the next is issued, so peak memory is a window rather than a
        whole stream (M9's "never materialise all lines").
        """
        now = time.monotonic()
        if min_interval and (now - self.last_refresh) < min_interval:
            return                    # a peer already read this window
        self.refreshes += 1
        while True:
            prev_offset = self.offset
            res = await asyncio.to_thread(
                _scan, self.path, self.offset,
                {"sig": self.sig, "sig_short": self.sig_short,
                 "dev_ino": self.dev_ino, "mtime_ns": self.mtime_ns,
                 "tail": self.tail_bytes},
                SCAN_WINDOW)      # read per call, not bound as a default
            self.last_refresh = time.monotonic()
            self.last_refresh_wall = time.time()
            if res["missing"]:
                # A stream that no longer exists must not keep contributing to
                # /health's parse_failures_total for the life of the server (m-2).
                PARSE_FAILURES.pop(self.path, None)
                if not self.missing:
                    self._reset()
                self.missing = True
                return
            self.missing = False
            self.size, self.mtime = res["size"], res["mtime"]
            self.mtime_ns = res["mtime_ns"]
            self.dev_ino, self.sig = res["dev_ino"], res["sig"]
            self.sig_short = res["sig_short"]
            if res["reset"]:
                self._reset()
            self.tail_bytes = res["tail"]
            records = res["records"]
            new_offset = res["new_offset"]
            base = 0 if res["reset"] else prev_offset
            if new_offset > self.offset:
                # The blob is extended ONLY while it is exactly in step with the
                # fold, so it always covers a complete byte PREFIX of the file —
                # the property that lets every v1 client tail from its end without
                # a gap or a duplicate (SERVER-READ-8). Out of step (a v1 client
                # attached mid-stream), it stays put and ensure_blob catches up.
                in_step = (self.blob_offset == self.offset
                           and (self.blob_subs > 0 or self.blob_offset > 0))
                self.bytes_read += new_offset - self.offset
                # (line, end_offset, parsed) — the fold already knows whether each
                # line is JSON, so the flag travels with it instead of every v2
                # tick re-parsing the batch it is about to concatenate (R-10).
                flagged = [(line, end, self.fold.apply(line)) for line, end in records]
                self.events_folded += len(records)
                if in_step:
                    for line, _end, _ok in flagged:
                        self.blob += ws_frame(line)
                    self.blob_lines += len(records)
                    self.blob_framed += len(records)
                    self.blob_offset = new_offset
                self.offset = new_offset
                if flagged:
                    self.last_change = time.monotonic()
                    self._broadcast(flagged)
            # `more` is this step's own stat: keep walking while complete bytes
            # remain, and stop unconditionally if a step made no progress (an
            # unreadable file, or a torn tail with no newline yet).
            if not res["more"] or new_offset <= base:
                break
        if self.fold.parse_failures:
            PARSE_FAILURES[self.path] = self.fold.parse_failures
        else:
            PARSE_FAILURES.pop(self.path, None)
        self._evict_blob()

    def _reset(self) -> None:
        """Truncation / rotation / sig change: drop everything, close clients.

        The v1 truncation sentinel's semantics are preserved exactly — every
        subscribed socket is closed so the client rebuilds cleanly (D10) —
        and the fold, the blob and the snapshot cache all start over.
        """
        self.generation += 1
        self.resets += 1
        self.offset = 0
        self.tail_bytes = b""
        self.fold = Fold()
        self.blob = bytearray()
        self.blob_offset = 0
        self.blob_lines = 0
        self.snap_cache = None
        self.snap_truncated = False
        self.last_change = time.monotonic()
        for sub in list(self.subscribers):
            sub.closed = True
        self.subscribers.clear()

    def _broadcast(self, records: list) -> None:
        for sub in list(self.subscribers):
            sub.queue.append(records)
            sub.pending_events += len(records)
            if sub.pending_events > MAX_PENDING_EVENTS:
                sub.closed = True     # hopeless reader: let it reconnect
                self.subscribers.discard(sub)

    # -- subscribers --------------------------------------------------------
    def subscribe(self) -> Subscriber:
        """Attach at the CURRENT offset. Call inside the locked step."""
        sub = Subscriber()
        self.subscribers.add(sub)
        if self.tailer is None or self.tailer.done():
            self.tailer = asyncio.create_task(self._tail_loop())
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self.subscribers.discard(sub)

    def poll_interval(self) -> float:
        """0.5 s while the stream moves; 2 s once it has been quiet a minute."""
        quiet = time.monotonic() - self.last_change
        return POLL_SECS if quiet < IDLE_AFTER_SECS else IDLE_POLL_SECS

    async def _tail_loop(self) -> None:
        """The stream's own clock: ONE reader for every socket on this file.

        Client loops poke ``refresh`` on their own tick too, so a socket never
        waits on this coroutine's phase; the ``REFRESH_MIN_SECS`` stamp makes
        the extra pokes free (they return without touching the disk).
        """
        try:
            while self.subscribers:
                await asyncio.sleep(self.poll_interval())
                if not self.subscribers:
                    break
                async with self.sync_lock():
                    await self.refresh_locked(REFRESH_MIN_SECS)
        except asyncio.CancelledError:
            pass
        except (ConnectionError, OSError):
            pass

    # -- framed history + snapshot ------------------------------------------
    async def ensure_blob(self) -> None:
        """Bring the framed-v1 blob up to the fold's offset (M7).

        Built at most once per byte: a second connect finds ``blob_offset ==
        offset`` and builds nothing, which is exactly what the work-based test
        asserts. Live growth is appended by ``refresh_locked`` from lines
        already in memory, so a long-lived tab never re-reads the file.
        """
        self.blob_idle_at = None
        if self.blob_offset == self.offset:
            return
        if self.blob_offset > self.offset:
            self.blob = bytearray()
            self.blob_offset = self.blob_lines = 0
        start = self.blob_offset
        records, new_offset = await asyncio.to_thread(read_records, self.path, start)
        if new_offset == -1:
            self.blob = bytearray()
            self.blob_offset = self.blob_lines = 0
            return
        # The blob covers a COMPLETE byte prefix or it is worthless, so the new
        # blob_offset is where the framing ACTUALLY reached — never `self.offset`
        # on faith. An unreadable file (read_records swallows OSError and
        # returns no records) or a record past the fold's offset would otherwise
        # leave a silent gap in the next v1 replay (SERVER-READ-8).
        framed_end, stopped = start, False
        for line, end in records:
            if end > self.offset:
                stopped = True         # never frame past the fold's offset
                break
            self.blob += ws_frame(line)
            self.blob_lines += 1
            self.blob_framed += 1
            framed_end = end
        self.blob_offset = framed_end if stopped else min(new_offset, self.offset)
        self.blob_builds += 1

    def _evict_blob(self) -> None:
        if self.blob_subs or not self.blob:
            return
        now = time.monotonic()
        if self.blob_idle_at is None:
            self.blob_idle_at = now
        elif now - self.blob_idle_at > BLOB_IDLE_SECS:
            self.blob = bytearray()
            self.blob_offset = 0
            # ...and the line count with it. `blob_lines` is what a v1 replay
            # reports into STATS["events_sent"], so leaving it behind made the
            # rebuilt blob claim the evicted one's lines too. `_reset()` has
            # always cleared all three; these two paths must not disagree.
            self.blob_lines = 0
            self.blob_idle_at = None

    def snapshot_bytes(self) -> bytes:
        """The v2 prelude, cached per ``(sig, offset)`` — never per connect.

        The truncation disclosure is cached WITH the bytes: it is a property of
        this payload, not of the fold, and a caller that reads it off the fold
        after a cache hit is reading whatever the last build happened to leave
        there.
        """
        key = (self.sig, self.offset)
        if self.snap_cache and self.snap_cache[0] == key:
            self.snap_truncated = self.snap_cache[2]
            return self.snap_cache[1]
        snap = self.fold.snapshot(self.sig, self.offset)
        blob = json.dumps(snap).encode()
        self.snap_truncated = bool(snap["logTruncated"])
        self.snap_cache = (key, blob, self.snap_truncated)
        self.snap_builds += 1
        return blob

    async def validate_cursor(self, from_offset, sig):
        """``&from=&sig=`` per GD-B: identity, range, and a newline boundary.

        Returns ``(ok, reason)``. ``from`` is client-supplied, i.e. untrusted:
        a mid-line offset would frame a garbage first record, and an offset
        from a different run would tail a foreign stream.
        """
        if self.missing:
            return False, "no-stream"
        if not sig:
            return False, "no-sig"
        if sig != self.sig:
            return False, "sig-mismatch"
        if from_offset is None or from_offset < 0:
            return False, "bad-offset"
        if from_offset > self.offset:
            return False, "offset-ahead"
        if from_offset == 0:
            return True, None
        ok = await asyncio.to_thread(_ends_with_newline, self.path, from_offset)
        return (True, None) if ok else (False, "mid-line")

    def status(self) -> dict:
        if self.missing:
            return {"status": "empty", "last": None, "tokens": {"in": 0, "out": 0}}
        return self.fold.status()

    def health(self) -> dict:
        return {"offset": self.offset, "events": self.fold.ev_count,
                "bytes_read": self.bytes_read, "events_folded": self.events_folded,
                "refreshes": self.refreshes, "resets": self.resets,
                # "frames" is the plan's name for the cumulative framed-line
                # counter (M12); the attribute keeps the blob_ prefix of its
                # siblings.
                "blob_bytes": len(self.blob), "frames": self.blob_framed,
                "snapshot_bytes": len(self.snap_cache[1]) if self.snap_cache else 0,
                "clients": len(self.subscribers),
                "last_refresh_ms": int(self.last_refresh_wall * 1000)}


def _ends_with_newline(path: str, offset: int) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(offset - 1)
            return f.read(1) == b"\n"
    except OSError:
        return False


def keep_parseable(lines: list) -> list:
    """Drop lines that are not a JSON object — the v2 batch filter.

    One poisoned line inside ``b"[" + b",".join(lines) + b"]"`` invalidates the
    WHOLE frame, so up to ``BATCH_MAX_EVENTS`` good events would vanish in the
    page's ``catch (e) {}``; under v1 the same line costs exactly itself. The
    live tail gets this for free (the fold already told it which lines parsed);
    a ``snap=0`` replay reads raw bytes off disk, so it filters here — and
    because filtering happens BEFORE framing, the caller's event count is the
    number of events that actually went out, not the number it read.
    """
    out = []
    for line in lines:
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(ev, dict):
            out.append(line)
    return out


def batch_frames(lines: list) -> list:
    """Pack raw event lines into v2 array frames by BYTE CONCATENATION.

    ``b"[" + b",".join(lines) + b"]"`` — never a re-parse and re-encode: the
    bytes on the wire are the bytes on disk, which is what keeps a batched
    replay byte-comparable with a v1 one. Both caps are independent and a
    batch closes on whichever binds first; a single line larger than the byte
    cap gets a frame of its own (there is no smaller legal unit).

    Every line handed here is framed: filtering poison is ``keep_parseable``'s
    job, one step earlier, so exactly one function decides what goes out and
    the count is knowable without re-parsing a frame.
    """
    out, cur, cur_bytes = [], [], 2
    for line in lines:
        if cur and (len(cur) >= BATCH_MAX_EVENTS
                    or cur_bytes + 1 + len(line) > BATCH_MAX_BYTES):
            out.append(b"[" + b",".join(cur) + b"]")
            cur, cur_bytes = [], 2
        cur.append(line)
        cur_bytes += len(line) + 1
    if cur:
        out.append(b"[" + b",".join(cur) + b"]")
    return out


async def write_chunked(writer, payload: bytes, closed: asyncio.Event) -> bool:
    """Write one big frame in ~256 KB slices with ``drain()`` between them.

    Backpressure, not buffering: a stalled browser stops the producer instead
    of the server holding the whole stream per client (SERVER-READ-5, measured
    ~12 MB of RSS per slow client). Returns False if the client went away
    mid-write, so the caller can stop early.
    """
    for i in range(0, len(payload), WRITE_CHUNK):
        if closed.is_set():
            return False
        writer.write(payload[i:i + WRITE_CHUNK])
        await writer.drain()
    return True


def _ctl(obj: dict) -> bytes:
    return ws_frame(json.dumps(obj).encode())


async def stream_events(reader, writer, events_path: str, query: str = "",
                        task_name: str = "", task_known: bool = True) -> None:
    """Serve one websocket: v1 byte-identical, or the negotiated v2 (GD-B)."""
    params = urllib.parse.parse_qs(query)
    v2 = params.get("v", [None])[0] == "2"
    closed = asyncio.Event()
    drainer = asyncio.create_task(drain_client(reader, closed))
    stream = Stream.get(events_path)
    try:
        if not v2:
            await _stream_v1(writer, stream, closed)
        else:
            await _stream_v2(writer, stream, closed, params, task_name, task_known)
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        drainer.cancel()
        try:  # best-effort CLOSE frame so a conforming client tears down cleanly
            writer.write(ws_frame(b"", 0x8))
            await writer.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass


async def _stream_v1(writer, stream: Stream, closed: asyncio.Event) -> None:
    """The compatibility floor: one text frame per line, then the live tail.

    Byte-identical to what this file has always sent — the only change is that
    the frames come from the stream's shared, already-framed history instead
    of being re-read and re-framed per client, and that they leave in ~256 KB
    slices with a ``drain()`` between them so a stalled reader throttles the
    producer instead of inflating the server's memory (SERVER-READ-5).
    """
    stream.blob_subs += 1
    sub = None
    try:
        # The locked step (SERVER-READ-8): fold, framed history and the tail
        # offset all come from ONE refresh, and the subscription is registered
        # before the lock is released — so a line appended right now lands in
        # exactly one of {replay, tail}, never both and never neither.
        async with stream.sync_lock():
            await stream.refresh_locked()
            await stream.ensure_blob()
            if stream.blob_offset != stream.offset:
                # The replay covers [0, blob_offset) and the tail starts at the
                # fold's offset (SERVER-READ-8's "tail from blob_prefix_offset"
                # — they are the same byte on every healthy path). If the blob
                # could not be brought up to the fold (an unreadable file
                # mid-build), those two are not the same byte and the range
                # between them would be silently never delivered. Refuse the
                # connection instead: the client reconnects and gets a whole
                # history, which is the one thing a v1 client can always do.
                return
            sub = stream.subscribe()
            generation = stream.generation
            blob_end = len(stream.blob)
            blob_lines = stream.blob_lines
        complete = True
        for i in range(0, blob_end, WRITE_CHUNK):
            if closed.is_set() or stream.generation != generation:
                complete = False
                break
            # The slice END is clamped to the length captured under the lock,
            # not just the loop bound: `await drain()` yields, and while it is
            # suspended the stream's tailer appends freshly framed lines to this
            # very blob. An unclamped final slice would run PAST blob_end into
            # lines that are already queued for the tail — the same event twice
            # on a v1 socket, which is double-counted tokens and duplicated log
            # rows on the live dashboard.
            writer.write(bytes(stream.blob[i:min(i + WRITE_CHUNK, blob_end)]))
            await writer.drain()
        if complete:
            STATS["events_sent"] += blob_lines
        await _tail_loop_client(writer, stream, sub, closed, v2=False)
    finally:
        if sub is not None:
            stream.unsubscribe(sub)
        stream.blob_subs = max(0, stream.blob_subs - 1)
        stream.blob_idle_at = time.monotonic()


async def _write_replay(writer, stream: Stream, start: int, stop: int,
                        generation: int, closed: asyncio.Event,
                        verify: bool) -> int:
    """Batch-frame the byte range ``[start, stop)`` onto a v2 socket.

    Serves both raw-history paths — the ``snap=0`` full replay and the (usually
    empty) gap between an accepted cursor and the server's — with ONE discipline:
    read a window, frame it under both batch caps, ``write_chunked`` it, repeat.
    Peak memory is one window, not one stream, and nothing is held while the
    socket drains. Returns the number of events written, or ``-1`` if the client
    went away or the stream reset underneath the replay.

    ``written`` counts what was FRAMED, never what was read: on a stream with a
    parse failure the poison is filtered out first, and counting the raw lines
    would over-report ``events_sent`` by exactly the poison count.
    """
    pos, written = start, 0
    while pos < stop:
        if closed.is_set() or stream.generation != generation:
            return -1
        records, nxt = await asyncio.to_thread(read_window, stream.path, pos, stop)
        if nxt <= pos:
            break                      # unreadable, or no complete line left
        lines = [line for line, _end in records]
        if verify:
            lines = keep_parseable(lines)
        for frame in batch_frames(lines):
            if closed.is_set() or stream.generation != generation:
                return -1
            if not await write_chunked(writer, ws_frame(frame), closed):
                return -1
        written += len(lines)
        pos = nxt
    STATS["events_sent"] += written
    return written


async def _stream_v2(writer, stream: Stream, closed: asyncio.Event, params: dict,
                     task_name: str, task_known: bool) -> None:
    """hello -> snapshot (or batched replay) -> ONE boundary frame -> tail."""
    if not task_known:
        # After the 101 there is no status code left to refuse with, so the
        # refusal is the hello itself: v2 NEVER answers an unknown ?task= with
        # the default task's stream the way v1 falls back (SERVER-READ-10).
        writer.write(_ctl({"m": "hello", "v": 2, "error": "unknown-task",
                           "task": task_name, "foldGen": FOLD_GEN}))
        await writer.drain()
        return
    snap_mode = params.get("snap", ["1"])[0]
    from_raw = params.get("from", [None])[0]
    sig_raw = params.get("sig", [None])[0]
    # `token` is honoured — by the auth gate in handle(), before this function
    # is ever reached — so it must not be reported back as an ignored parameter:
    # the page always sends it, and every connection would carry a false note.
    ignored = [k for k in params
               if k not in ("task", "v", "snap", "from", "sig", "token")]
    reason = None
    try:
        from_offset = int(from_raw) if from_raw is not None else None
    except (TypeError, ValueError):
        from_offset, reason = None, "bad-offset"
    if snap_mode not in ("0", "1", "verify"):
        ignored.append("snap")
        snap_mode = "1"

    sub = None
    try:
        # One locked step for the whole prelude — same law as v1: the fold, the
        # snapshot, the cursor, the replay RANGE and the subscription all come
        # from a single refresh (WS-PROTOCOL-6, the atomicity law). The replay
        # BYTES are read afterwards, outside the lock: the range is finished,
        # append-only history and therefore immutable, and the one thing that
        # could move it (truncation/rotation) bumps the generation, which the
        # writer re-checks every window.
        async with stream.sync_lock():
            await stream.refresh_locked()
            from_applied = False
            if from_raw is not None:
                if from_offset is None:
                    reason = reason or "bad-offset"
                else:
                    from_applied, why = await stream.validate_cursor(from_offset,
                                                                     sig_raw)
                    if not from_applied:
                        reason = why
            snapshot = None
            replay_from = None
            if from_applied:
                # Accepted resume: no snapshot — but the bytes between the
                # client's cursor and ours still have to travel or the resume
                # would be a silent gap. Usually that is nothing, which is the
                # whole point of a cursor: a 500 ms resync costs one empty tick.
                if from_offset < stream.offset:
                    replay_from = from_offset
            else:
                if snap_mode in ("1", "verify"):
                    snapshot = stream.snapshot_bytes()
                if snap_mode in ("0", "verify"):
                    replay_from = 0
            sub = stream.subscribe()
            offset, sig = stream.offset, stream.sig
            generation = stream.generation
            n_events = stream.fold.ev_count
            verify_json = stream.fold.parse_failures > 0
            log_truncated = bool(snapshot) and stream.snap_truncated

        writer.write(_ctl({"m": "hello", "v": 2, "task": task_name, "sig": sig,
                           "foldGen": FOLD_GEN, "fromApplied": bool(from_applied),
                           "reason": reason, "snap": snap_mode,
                           "ignored": ignored}))
        await writer.drain()
        # ``n`` on the boundary frame is ONE quantity: how many events the
        # prelude covers. With a snapshot that is the fold's event count as of
        # the cursor; without one it is the number of events the replay actually
        # framed. Never the larger of the two — under ``snap=verify`` both exist
        # and they differ by exactly the lines that failed to parse.
        covered = n_events if snapshot is not None else 0
        if snapshot is not None:
            if not await write_chunked(writer, ws_frame(snapshot), closed):
                return
            # A snapshot is a FOLD, not a sequence of events, so it deliberately
            # adds nothing to `events_sent`; it is counted here instead, or a
            # v2-only deployment reads as idle in /health and the shutdown line.
            STATS["snapshots_sent"] += 1
        if replay_from is not None:
            written = await _write_replay(writer, stream, replay_from, offset,
                                          generation, closed, verify_json)
            if written < 0:
                return
            if snapshot is None:
                covered = written
        # ONE boundary frame: the explicit replay/tail edge that replaces the
        # client's 600 ms guess (WS-PROTOCOL-2, PRIOR-ART-TOUCH-9).
        writer.write(_ctl({"m": "tail", "cursor": {"sig": sig, "offset": offset},
                           "n": covered, "truncated": log_truncated}))
        await writer.drain()
        await _tail_loop_client(writer, stream, sub, closed, v2=True)
    finally:
        if sub is not None:
            stream.unsubscribe(sub)


async def _tail_loop_client(writer, stream: Stream, sub: Subscriber,
                            closed: asyncio.Event, v2: bool) -> None:
    """Per-client tail: write frames from my offset, nothing else.

    The reading is the stream's; this loop only drains its queue, honours
    ``MAX_TICK_EVENTS`` with carry-over, and keeps the socket alive. The
    keepalive fires on a monotonic deadline (so the idle poll backoff cannot
    silently stretch it past an intermediary's timeout) with the historical
    tick count as a second arm, which is what keeps it observable when the poll
    cadence is shortened.
    """
    pending: collections.deque = collections.deque()
    ticks = 0
    next_ping = time.monotonic() + KEEPALIVE_SECS
    try:
        while not closed.is_set():
            await asyncio.sleep(stream.poll_interval())
            await stream.refresh(REFRESH_MIN_SECS)
            if sub.closed:
                break                 # truncation/rotation: rebuild cleanly (D10)
            while sub.queue:
                batch = sub.queue.popleft()
                sub.pending_events -= len(batch)
                pending.extend(batch)
            sent = 0
            cursor = None
            lines = []
            while pending and sent < MAX_TICK_EVENTS:
                line, end, parsed = pending.popleft()
                # A line the fold could not parse is dropped from a v2 array
                # frame and kept on a v1 one. v1's cost for a poisoned line has
                # always been exactly that line (the client's own JSON.parse
                # throws and the page swallows it); concatenating it into a v2
                # batch would instead invalidate the WHOLE frame and silently
                # lose up to BATCH_MAX_EVENTS good events with it. The cursor
                # still advances past it, so nothing is re-sent on a resume.
                if parsed or not v2:
                    lines.append(line)
                cursor = end
                sent += 1
            if lines:
                # v1: one text frame per line, exactly as always. v2: array
                # frames under both batch caps. Either way the tick is drained
                # every ~256 KB, so a capped burst cannot buffer megabytes for
                # a slow reader before the first await (SERVER-READ-5).
                payloads = batch_frames(lines) if v2 else lines
                buffered = 0
                for payload in payloads:
                    writer.write(ws_frame(payload))
                    buffered += len(payload)
                    if buffered >= WRITE_CHUNK:
                        await writer.drain()
                        buffered = 0
                STATS["events_sent"] += len(lines)
            if sent and v2:
                # Re-publish the server's position after every tick that
                # consumed events — capped ticks and dropped-poison ticks
                # included, which is what lets a reconnect resume with no gap
                # and no duplicate. ``n`` counts events actually delivered.
                writer.write(_ctl({"m": "cursor",
                                   "cursor": {"sig": stream.sig, "offset": cursor},
                                   "n": len(lines)}))
            if sent:
                await writer.drain()
            ticks += 1
            now = time.monotonic()
            if now >= next_ping or ticks % KEEPALIVE_TICKS == 0:
                next_ping = now + KEEPALIVE_SECS
                writer.write(ws_frame(b"", 0x9))
                await writer.drain()
    finally:
        stream.unsubscribe(sub)


# --------------------------------------------------------------------------
# THE FILE PLANE (GD-13 as amended: read / control / file) — Claude Code's
# auto-memory directory, listed and read over HTTP and, behind an explicit
# flag, WRITTEN.
#
# Everything below is new code placed BELOW the byte-pinned resolver region at
# the top of this file, so the source-text equality test that pins
# `resolve_tasks_root`/`in_plugin_cache` to decision_watcher.py is untouched.
#
# Why this server and not the aggregator (G3): the requirement is an editor
# reachable *from the monitoring page*, and the monitor page holds only THIS
# server's per-boot token — the aggregator's lives in `.touch/server.json` and
# is never served here, and neither server emits CORS, so a cross-origin fetch
# or a tokenless link could not authenticate. The cost is paid here, honestly:
# this file had to learn HTTP methods, request bodies, 404-under-prefix, 405 and
# an Origin gate on plain HTTP routes (I10).
#
# Why the rules below are as heavy as they are: these bytes become MODEL
# INSTRUCTIONS. Anything that reaches this directory is loaded into future
# sessions in this project — the index at every conversation start, a topic note
# on demand, and a file carrying `pinned:` frontmatter into every session,
# unasked (DOCS-6, undocumented in the CLI's own docs). A write plane over that
# directory is a persistent-instruction-injection primitive, so:
#
#   * it is DEFAULT-OFF (G6): `--allow-memory-write` / $TOUCH_ALLOW_MEMORY_WRITE;
#   * writes take the token from a HEADER only, never `?token=` — the page's own
#     URL carries the token in its query string, so a query-carried write is a
#     bookmarkable, prefetchable, `<img src>`-able mutation (W4);
#   * every memory route runs the Origin/Host gate, reads included, and a write
#     additionally requires an `X-Touch-Write: 1` header and a PRESENT
#     same-origin Origin (W2/W3);
#   * no `Access-Control-Allow-*` header is ever emitted, on any route;
#   * the write path handles the hazards in G7's order, 1 to 10, and this file
#     keeps that order because the order is the security property.
#
# What it deliberately does NOT do (Part D-6, PROTOCOL-20): emit a `touch-status`
# event or append to any `events.jsonl`. A memory edit is not a plan card and
# must never fabricate a badge; its record is the audit log at
# `.touch/memory-audit.jsonl`, which carries its own `w` attribution.
# --------------------------------------------------------------------------

#: The page (a second document, G4) and the JSON API under it. `/memory` is a
#: top-level navigation from `monitor.html`'s one header link, so it carries the
#: token the way `monitor.html` itself does — in the query string.
MEMORY_ROUTE = "/memory"
MEMORY_API_PREFIX = "/api/memory/"
MEMORY_HTML = os.path.join(ROOT, "memory.html")

#: The auto-memory index, and the CLI's OWN documented load budget for it: the
#: first 200 lines or 25 KB, whichever comes first, are injected at the start of
#: every conversation and everything past that is dropped at the next load,
#: silently (DOCS-14). Since v2.1.211 the measurement strips YAML frontmatter and
#: block-level HTML comments first, which is why `memory_index_budget` does too.
#: These two numbers have ONE owner (G5): they are reported to the editor in
#: `limits` and cross-checked against `tests/test_memory_hygiene.py`'s copy, so
#: the page cannot disclose a cap the repository gate does not enforce.
MEM_INDEX_NAME = "MEMORY.md"
MEM_INDEX_LINES = 200
MEM_INDEX_BYTES = 25600

#: Caps, in the house style of the budget block above — every growing collection
#: is capped, and a cap that is not enforced is a lie the editor would repeat
#: (W9, SERVER-13). `MAX_MEMORY_BYTES` is per FILE and comfortably above the
#: index budget; the directory caps are what stop a disk-fill through a
#: single-threaded event loop; `MAX_MEMORY_BODY_BYTES` is checked against
#: `Content-Length` BEFORE a byte is read.
MAX_MEMORY_BYTES = 64 * 1024
MAX_MEMORY_FILES = 100
MAX_MEMORY_DIR_BYTES = 1024 * 1024
MAX_MEMORY_BODY_BYTES = 1024 * 1024
MEMORY_BODY_TIMEOUT = 10.0
#: Backups kept per file in `.history/<name>/`, and the audit log's ceiling.
MEMORY_HISTORY_KEEP = 20
MEMORY_AUDIT_BYTES = 256 * 1024
#: Per-name write locks are a growing collection too; swept past this many.
MEMORY_LOCK_CAP = 512
#: Rows one list answer will build, matching `memory.html`'s own display cap. A
#: bigger directory is DISCLOSED (`listTruncated`) rather than silently clipped.
MEMORY_LIST_CAP = 200

#: How long `/health` may reuse an alignment answer. `/health` is the ONE
#: untokened route and a supervisor may poll it every second, so the one piece of
#: work in its memory block that is not a `stat` — reading up to three settings
#: files and parsing them as JSON to answer `aligned` — is memoised for this many
#: seconds on that route only. The TOKENED list route always re-reads them: an
#: operator who has just fixed `settings.local.json` is entitled to see it in the
#: next refresh, and that route is the one the page reads `aligned` from anyway.
MEMORY_HEALTH_TTL = 2.0

#: G7 step 1 — the whole namespace, as a flat name. No `/`, no `\`, no `..`, no
#: leading dot, `.md` only. This deletes the traversal class before any
#: filesystem call, and it also refuses the shapes that would be dangerous even
#: INSIDE the directory: a `settings.json`, a `hooks.json`, a `*.py`/`*.sh` on an
#: import path or a `PATH`, a `foo.token` that the `.gitignore` carve would then
#: have to reason about (W7, LAYOUT-10/19). Byte-for-byte the regex `memory.html`
#: validates a new name against and the one `tests/test_memory_hygiene.py` calls
#: `FLAT_MD` — one namespace, three spellings that must not drift.
MEMORY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.md$")

#: G7 step 5's carrier: an `ifMatch` is the sha256 hex digest of the bytes the
#: client last read, and its SHAPE is checked before it is compared. Two reasons,
#: and the first one is a live defect this pattern closes: `hmac.compare_digest`
#: raises `TypeError` when either `str` argument is not ASCII, `ifMatch` is fully
#: client-controlled (`?ifMatch=%C3%A9` is enough), and a `TypeError` is neither
#: `MemoryRefusal` nor `OSError` — so the coroutine would die with the socket
#: still open and the caller would get ZERO bytes plus a traceback in a
#: world-readable daemon log. An empty reply is exactly the "this build has no
#: memory API" misreport G5 spends its whole budget deleting. The second reason is
#: honesty: a value that is not a digest was never going to match, and a named 412
#: is a better answer than a 409 that publishes the whole file back.
MEMORY_SHA_RE = re.compile(r"[0-9a-f]{64}")

#: G7 step 7 — content hygiene. `MEMORY_FRONTMATTER` and `MEMORY_BLOCK_COMMENT`
#: are `tests/test_memory_hygiene.py`'s two regexes, ported character for
#: character, because the editor's budget, this server's measurement and the
#: repository gate must agree about the same file. The comment body is
#: `(?:(?!-->).)*` and not `.*?` for the reason that file records: with `re.S` a
#: lazy `.*?` still crosses `-->` when the shorter match fails, which
#: UNDER-counts, and under-counting is the dangerous direction.
MEMORY_FRONTMATTER = re.compile(
    r"\A---[ \t]*\r?\n.*?\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.S)
MEMORY_BLOCK_COMMENT = re.compile(
    r"^[ \t]*<!--(?:(?!-->).)*-->[ \t]*(?:\r?\n|\Z)", re.S | re.M)
#: A `pinned:` key in LEADING frontmatter — undocumented, and stronger than the
#: index: such a file is injected into EVERY session, newest-modified first,
#: unasked (DOCS-6). Refused unless the request says `allowPinned`, which is the
#: flag the page attaches to a sentence the operator answered in words.
MEMORY_PINNED_RE = re.compile(r"(?m)^[ \t]*pinned[ \t]*:")
#: An `@`-import: the CLI expands it transitively at load, so one accepted line
#: turns this directory into an arbitrary-file read into model context
#: (SECURITY-6, W10). Scanned over PROSE only — a documented `@path` inside a
#: code span or fence is not an import.
MEMORY_IMPORT_RE = re.compile(r"(?:^|[\s(\[])@[A-Za-z0-9._~/-]")
#: A lone CR (an old-Mac line ending, or a torn CRLF) — rejected rather than
#: normalised, because rewriting somebody's bytes is its own surprise (W10).
MEMORY_LONE_CR_RE = re.compile(r"\r(?!\n)")
#: `tests/test_publish_hygiene.py`'s two secret detectors, reimplemented here
#: (this module imports nothing but the stdlib): a line that is exactly a
#: high-entropy URL-safe blob of token length, and a credentialed Mongo URI
#: whose password is not visibly a documentation stand-in (PROTOCOL-16). The
#: refusal names the CATEGORY and the line number and NEVER the match — echoing
#: a token back into a JSON body, a browser and a screenshot is the leak the
#: check exists to prevent.
MEMORY_TOKEN_SHAPE_RE = re.compile(r"^[A-Za-z0-9_-]{40,50}$")
MEMORY_TOKEN_DISTINCT = 12
MEMORY_MONGO_URI_RE = re.compile(r"mongodb(?:\+srv)?://([^/\s:]+):([^@\s]+)@")
MEMORY_PLACEHOLDER_WORDS = frozenset({
    "password", "pass", "passwd", "pwd", "secret", "redacted", "changeme",
    "yourpassword", "p",
})

#: The three UNDOCUMENTED environment overrides that outrank every settings
#: layer (DOCS-13). This server never reads them as a mechanism — it only
#: reports that one is set, because with one in force the alignment question
#: cannot be answered from the documented layers at all, and a confident
#: `aligned: true` over that is exactly the diagnosis trap they are.
MEMORY_ENV_OVERRIDES = ("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE",
                        "CLAUDE_CODE_REMOTE_MEMORY_DIR",
                        "CLAUDE_MEMORY_STORES")

#: `sandbox`, byte-identical with `aggregator/server.py`'s `FILE_CSP` — GD-20's
#: verbatim twin, machine-checked across the two servers. `allow-scripts` is
#: deliberately absent: a report opened from the dashboard is served at a URL
#: that CONTAINS the per-boot token, an opaque origin does not stop a script in
#: it from reading its own `location.search`, and that token now also authorizes
#: memory writes — one unaudited agent-authored report would be token-exfil →
#: memory write → persistent injection (SECURITY-4/5).
FILE_CSP = "sandbox"
#: The second header of that pair, named once so a use site cannot set the CSP
#: and forget this one. Also on the two HTML pages: their URLs carry the token
#: in the query string, and a `Referer` would hand it to whatever they link to.
NO_REFERRER = "no-referrer"


class MemoryRefusal(Exception):
    """A named refusal from the file plane: a status, a category, a sentence.

    The category is machine-readable and stable; the reason is what the operator
    reads. Neither ever carries the offending TEXT — a token-shaped line is
    refused BY CATEGORY (PROTOCOL-16). `extra` is for the one refusal that must
    publish state: a 409 precondition failure carries the current
    `{sha256, mtime_ns, size, content}` so the page can offer reload /
    show-both / overwrite instead of a bare retry (G5, UI-3).
    """

    def __init__(self, status: int, category: str, reason: str, extra: dict = None):
        super().__init__(reason)
        self.status = int(status)
        self.category = category
        self.reason = reason
        self.extra = dict(extra or {})

    def body(self) -> dict:
        payload = {"error": self.category, "category": self.category,
                   "reason": self.reason}
        payload.update(self.extra)
        return payload


# --------------------------------------------------------------------------
# Where the files are, and whether this daemon may touch them.
# --------------------------------------------------------------------------

def resolve_project_root() -> str:
    """The project this daemon serves: env > env > cwd walk-up, else ``""``.

    `$CLAUDE_PROJECT_DIR` > `$TOUCH_PROJECT_CWD` > the nearest ancestor of the
    cwd holding a `.claude/` marker. Reimplemented here rather than imported
    from `aggregator/paths.py` for the same reason `resolve_tasks_root` is: both
    daemons must stay independently runnable single files with nothing else on
    PYTHONPATH.

    Two deliberate differences from `paths.project_root`, both about what the
    answer is USED for:

    * `~` is skipped as a marker (the CLI's own configuration directory is not a
      project), and
    * an unresolved project is ``""``, not `os.getcwd()`. The aggregator has a
      write-ahead log it must place somewhere; this caller would otherwise
      invent a memory root in whatever directory the daemon happened to be
      started from, and then serve an editor over it. Refusing loudly is the
      house rule for a state root nobody asked for.
    """
    for name in ("CLAUDE_PROJECT_DIR", "TOUCH_PROJECT_CWD"):
        configured = (os.environ.get(name) or "").strip()
        if configured:
            return os.path.abspath(configured)
    try:
        home = os.path.realpath(os.path.expanduser("~"))
    except (OSError, RuntimeError, KeyError):
        home = None
    here = os.path.abspath(os.getcwd())
    while True:
        if (home is None or os.path.realpath(here) != home) and \
                os.path.isdir(os.path.join(here, ".claude")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return ""
        here = parent


def resolve_memory_root() -> str:
    """``<project>/.touch/memory`` — project-anchored, or ``""`` (G1/G10).

    The marker dir and the state dir are deliberately different (`.claude/`
    marks a Claude Code project; `.touch/` is created by Touch and gitignored),
    exactly as in the tasks-root ladder above.

    There is deliberately NO environment rung, and the asymmetry with
    `$ORCH_TASKS_ROOT` is the decision, not an omission: `aggregator/paths.py`
    declines one for this root because a *new* way to move it is a hole in two
    controls that are spelled as paths rather than derived — the scope guard's
    subagent write-deny on `.touch/memory/**` (G14) and the `.gitignore`
    re-inclusion of `.touch/memory/*.md` (G9). Relocation belongs to
    `autoMemoryDirectory`, the one key the CLI actually reads, and this server
    REPORTS the comparison (`aligned`) rather than assuming it.
    """
    project = resolve_project_root()
    return os.path.join(project, ".touch", "memory") if project else ""


MEMORY_ROOT = resolve_memory_root()


def memory_write_enabled() -> bool:
    """The write plane is DEFAULT-OFF: an explicit flag or env var turns it on.

    A user who installed a read-only, loopback, token-gated dashboard cannot be
    talked into a write surface by a leaked token alone (G6, W14, SECURITY-1).
    The flag starts with `-`, so `positional_args()` already keeps it out of the
    port scan.
    """
    if "--allow-memory-write" in sys.argv[1:]:
        return True
    return (os.environ.get("TOUCH_ALLOW_MEMORY_WRITE") or "").strip().lower() in (
        "1", "true", "yes", "on")


MEMORY_WRITE = memory_write_enabled()


def memory_unavailable() -> str:
    """``""`` when the memory family may answer at all, else the reason (503).

    Two ways it may not: nothing resolved a project (so there is no directory
    this server has any business serving), and a root inside an installed plugin
    cache — a version-stamped directory that is re-copied on update and swept
    ~14 days later, so a memory file written there is data loss with extra steps
    and an instruction file that vanishes (SERVER-16, W8, Part D-8).
    """
    root = MEMORY_ROOT
    if not root:
        return ("no project root resolved, so this server has no memory directory: "
                "start touch-monitor from the project checkout, or set "
                "CLAUDE_PROJECT_DIR to it")
    if os.path.islink(root):
        # G7 step 2's rule, applied to the ROOT rather than to a target: a link
        # planted at `.touch/memory` would redirect every save, every backup and
        # every trash move out of the project, and `realpath` containment cannot
        # see it because a resolved root IS its own base. Checked here, once,
        # rather than in `safe_memory_path` (which is per file and documents the
        # root as trusted for exactly this reason).
        return (f"the resolved memory root is a symlink ({root}); this server "
                f"refuses to follow one, because a link at that path redirects "
                f"every save and every backup out of the project")
    if in_plugin_cache(root):
        return (f"the resolved memory root is inside an installed plugin cache "
                f"({root}); that directory is version-stamped and swept, so the "
                f"memory family is disabled rather than writing where the files "
                f"would silently vanish")
    return ""


def safe_memory_path(root: str, name: str) -> str:
    """Absolute path of one memory file, or raise. G7 steps 1-3 and G8.

    A SEPARATE, separately-named resolver, and not a widening of
    `safe_artifact_path`: that one is task-scoped, read-only, and whitelists
    `.html` as well. Here `.md` is the ONLY extension — a memory file has no
    reason to render as a document, and refusing `.html` deletes the stored-XSS
    class from a directory a browser can write.

    Order matters and is the security property:

    1. the flat-name regex, before any `os.path.join` — traversal dies here;
    2. `lstat` (via `os.path.islink`) the target and refuse a symlink outright,
       without resolving it: a symlink planted in this directory by any local
       process turns a memory save into an arbitrary-file overwrite (SERVER-5);
    3. containment with `realpath` on BOTH sides (mixing `abspath` with
       `realpath` is the classic bypass), plus two explicit ancestor refusals:
       `~/.claude` — a read-only tap, always, and the refusal is greppable
       (PROTOCOL-7, Part D-9) — and an installed plugin cache (PROTOCOL-6).

    The ROOT itself is TRUSTED here, deliberately: `base = realpath(root)` follows
    a symlinked root by design, because a per-file check cannot distinguish an
    operator who deliberately parked `.touch/` on another volume from an attacker
    who planted a link — and planting one needs local write access to the project,
    which is strictly more than this plane grants anyone. The root is checked ONCE
    instead, where it is resolved: `memory_unavailable()` refuses a symlinked root
    for the whole family, and `memory_side_dir` refuses a symlinked
    `.history`/`.trash`. G7 step 2 governs the TARGET, and that check is here.
    """
    if not name or not MEMORY_NAME_RE.match(name):
        raise MemoryRefusal(
            400, "bad-name",
            "a memory file name is letters, digits, dot, dash or underscore, "
            "starts with a letter or digit, ends in .md and is at most 64 "
            "characters — no directories, no leading dot")
    base = os.path.realpath(root)
    target = os.path.join(base, name)
    if os.path.islink(target):
        raise MemoryRefusal(
            409, "symlink",
            "the target is a symlink; this server refuses to follow one into or "
            "out of the memory root, and nothing was read or written")
    full = os.path.realpath(target)
    if not (full == base or full.startswith(base + os.sep)):
        raise MemoryRefusal(403, "outside-root",
                            "the resolved path is outside the memory root")
    home_claude = os.path.realpath(os.path.expanduser(os.path.join("~", ".claude")))
    if full == home_claude or full.startswith(home_claude + os.sep):
        raise MemoryRefusal(
            403, "home-claude",
            "~/.claude is a read-only tap: this server never reads or writes "
            "inside it, whatever the memory root resolves to")
    if in_plugin_cache(full):
        raise MemoryRefusal(
            403, "plugin-cache",
            "the resolved path is inside an installed plugin cache, which is "
            "swept on update — nothing may be written there")
    return full


# --------------------------------------------------------------------------
# Measurement and hygiene (G7 step 7, G8).
# --------------------------------------------------------------------------

def memory_index_budget(text: str):
    """``(lines, bytes)`` the CLI would count for the auto-memory index.

    Frontmatter and block-level HTML comments come off FIRST, because the CLI
    strips them before measuring (v2.1.211+). The text is not newline-normalised
    first: the count is of the bytes it was given, which is what
    `tests/test_memory_hygiene.py` and `memory.html`'s live budget also count.
    """
    stripped = MEMORY_FRONTMATTER.sub("", text)
    stripped = MEMORY_BLOCK_COMMENT.sub("", stripped)
    return len(stripped.splitlines()), len(stripped.encode("utf-8"))


def memory_prose(text: str) -> str:
    """`text` with fenced code blocks and inline code spans removed.

    The `@`-import scan runs over this, so documentation that SHOWS an import
    inside backticks or a fence is not mistaken for one — the difference between
    a hygiene rule and a rule people route around.

    An UNTERMINATED fence does not hide its tail. CommonMark says an unclosed
    fence runs to the end of the document, so a loader and this validator would
    probably agree — but SECURITY-6's requirement is that they DO agree, and this
    is the one direction where disagreeing is exploitable (a lenient validator in
    front of a strict loader). So the tail after an unclosed ``` is re-read as
    prose rather than skipped: the cost of being wrong is a refusal the author can
    fix by closing the fence, which is the safe way round.
    """
    out, tail, in_fence = [], [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            if in_fence:
                tail = []            # candidate unterminated tail starts here
            continue
        if in_fence:
            tail.append(line)
            continue
        out.append(re.sub(r"`[^`]*`", "", line))
    if in_fence:
        out.extend(re.sub(r"`[^`]*`", "", line) for line in tail)
    return "\n".join(out)


def memory_placeholder(password: str) -> bool:
    """True when a URI's password field is visibly a documentation stand-in."""
    p = (password or "").strip()
    if p.startswith("<") and p.endswith(">"):
        return True
    if p.startswith("$") or p.startswith("{") or p.startswith("%"):
        return True
    if p and set(p) <= set("*.x"):
        return True
    return p.lower() in MEMORY_PLACEHOLDER_WORDS


def memory_decode(raw: bytes) -> str:
    """Strict UTF-8, or a named 400 — never ``errors="replace"``.

    A replacement character written into a file the model reads as instructions
    is a silent corruption in the one file class where silent corruption is
    least acceptable (SERVER-14).
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise MemoryRefusal(
            400, "not-utf8",
            "the content is not valid UTF-8; nothing was written (this server "
            "never substitutes replacement characters into a file the model "
            "reads as instructions)")


def memory_hygiene(text: str, *, allow_pinned: bool) -> None:
    """Raise `MemoryRefusal` for content that must not become an instruction.

    Every rule here is about the bytes' SECOND life — they are read back by a
    model, not by a browser — which is why the refusals are content-shaped
    rather than encoding-shaped, and why each one names a category the page can
    print without quoting the offending text (G7 step 7, W10, PROTOCOL-16).
    """
    if "\x00" in text:
        raise MemoryRefusal(400, "nul-byte",
                            "the content contains a NUL byte")
    if MEMORY_LONE_CR_RE.search(text):
        raise MemoryRefusal(
            400, "lone-cr",
            "the content contains a bare carriage return; send LF or CRLF line "
            "endings (this server preserves your bytes rather than rewriting "
            "them, so it refuses instead of normalising)")
    if MEMORY_IMPORT_RE.search(memory_prose(text)):
        raise MemoryRefusal(
            400, "import-directive",
            "the content carries an @-import outside a code span or fence; the "
            "CLI expands those transitively at load, which would turn this "
            "directory into an arbitrary-file read into model context")
    if MEMORY_BLOCK_COMMENT.search(text):
        raise MemoryRefusal(
            400, "html-comment",
            "the content carries a block-level HTML comment; the CLI strips "
            "those before the model sees the file, so a comment is a place to "
            "hide text from the model that a human editing it still reads")
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if (MEMORY_TOKEN_SHAPE_RE.match(stripped)
                and len(set(stripped)) >= MEMORY_TOKEN_DISTINCT):
            raise MemoryRefusal(
                400, "token-shape",
                f"line {number} is a high-entropy URL-safe blob of token "
                f"length; a secret in a file that loads into every session is "
                f"a secret in every transcript (the line itself is not echoed "
                f"back)")
        found = MEMORY_MONGO_URI_RE.search(line)
        if found and not memory_placeholder(found.group(2)):
            raise MemoryRefusal(
                400, "credentialed-uri",
                f"line {number} carries a database URI with a real password; "
                f"write the password as <password> if the shape is what you "
                f"meant to record (the line itself is not echoed back)")
    front = MEMORY_FRONTMATTER.match(text)
    if front and MEMORY_PINNED_RE.search(front.group(0)) and not allow_pinned:
        raise MemoryRefusal(
            422, "pinned",
            "the content carries a `pinned:` key in its frontmatter, which "
            "loads this file into EVERY session unasked; re-send with "
            "allowPinned:true to confirm that in words")


def memory_normalize(text: str) -> str:
    """Exactly one trailing newline, and nothing else touched (UI-3).

    Only newlines are stripped, so trailing spaces inside a line survive: this
    is the one normalisation the server performs, and it performs it server-side
    so a `<textarea>` round trip cannot quietly delete the final newline off
    every file it touches.
    """
    return text.rstrip("\n") + "\n"


def memory_stamp(text: str) -> str:
    """Refresh `modified:` inside EXISTING leading frontmatter. Never invent it.

    The CLI stamps `modified` (UTC ISO-8601) whenever Claude writes a file that
    already HAS frontmatter, and reads it back to judge how current a fact is —
    so a browser save that left the field alone would make the timestamp lie
    (SERVER-14, DOCS-16). Adding frontmatter to a file that has none is the
    opposite error: it would opt the file into `modified` stamping AND into the
    `pinned` scan, which is a behaviour change nobody asked for.
    """
    match = MEMORY_FRONTMATTER.match(text)
    if not match:
        return text
    block = match.group(0)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"modified: {stamp}"
    if re.search(r"(?m)^[ \t]*modified[ \t]*:.*$", block):
        block = re.sub(r"(?m)^[ \t]*modified[ \t]*:.*$", line, block, count=1)
    else:
        lines = block.split("\n")
        for index in range(len(lines) - 1, -1, -1):
            if lines[index].strip() in ("---", "..."):
                lines.insert(index, line)
                break
        block = "\n".join(lines)
    return block + text[match.end():]


# --------------------------------------------------------------------------
# Alignment: is this the directory the CLI itself reads? (SERVER-4)
# --------------------------------------------------------------------------

def memory_settings_value():
    """`autoMemoryDirectory` from the DOCUMENTED settings layers, nearest first.

    Returns `(value, layer_path)` or `(None, None)`. Only the three documented
    layers are consulted — project-local, project, user — in the CLI's own
    precedence order. The undocumented environment overrides are deliberately
    not read as a mechanism (see `MEMORY_ENV_OVERRIDES`), and a `--settings`
    file or a managed policy file cannot be discovered from here at all, which
    is why `aligned` is reported as UNKNOWN rather than guessed whenever an
    override is in force.
    """
    project = resolve_project_root()
    candidates = []
    if project:
        candidates.append(os.path.join(project, ".claude", "settings.local.json"))
        candidates.append(os.path.join(project, ".claude", "settings.json"))
    candidates.append(os.path.expanduser(os.path.join("~", ".claude", "settings.json")))
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            value = data.get("autoMemoryDirectory")
            if isinstance(value, str) and value:
                return value, path
    return None, None


def memory_effective_dir(value: str):
    """The directory the CLI would USE for `value`, or None if it rejects it.

    The CLI's validator, reproduced: an absolute path (or a `~/` one it expands
    first), at least three characters, no NUL, not a UNC/`//` form. A value that
    fails it — `".touch/memory"`, the obvious thing to write — returns
    `undefined` there and the CLI falls back to its default with **no error and
    no warning** (DOCS-1, verified against 2.1.220). That silence is the whole
    reason this function exists: the page can then say "Claude Code reads
    somewhere else" instead of the operator hunting a memory that is being
    ignored.
    """
    if not isinstance(value, str) or not value:
        return None
    raw = os.path.expanduser(value) if value == "~" or value.startswith("~/") else value
    if "\x00" in raw or len(raw) < 3 or raw.startswith("//") or raw.startswith("\\\\"):
        return None
    if not os.path.isabs(raw):
        return None
    return os.path.normpath(raw)


def memory_alignment(root: str):
    """`(aligned, effective)` — a tri-state and a sentence, never a guess.

    `aligned` is True/False when the documented layers answer, and None when
    they cannot (an undocumented env override outranks them). `effective` is
    always a string, because the page prints it into a sentence.
    """
    live = [name for name in MEMORY_ENV_OVERRIDES
            if (os.environ.get(name) or "").strip()]
    if live:
        return None, (f"unknown — {live[0]} is set in this daemon's environment "
                      f"and it outranks every settings layer, so where a session "
                      f"reads memory cannot be answered from settings alone")
    value, _layer = memory_settings_value()
    if value is None:
        return False, ("Claude Code's default (~/.claude/projects/<project "
                       "key>/memory) — no autoMemoryDirectory is set in any "
                       "documented settings layer")
    effective = memory_effective_dir(value)
    if effective is None:
        return False, ("Claude Code's default: the configured autoMemoryDirectory "
                       "is not an absolute path, so the CLI rejects it silently "
                       "and falls back")
    if not root:
        return False, effective
    return os.path.realpath(effective) == os.path.realpath(root), effective


#: `(monotonic, answer)` per root, for `/health` ONLY (see `MEMORY_HEALTH_TTL`).
_MEMORY_ALIGN_CACHE: dict = {}


def memory_alignment_cached(root: str):
    """`memory_alignment`, memoised for `MEMORY_HEALTH_TTL` seconds, for `/health`.

    `/health` is the one route with no token in front of it, and answering
    `aligned` costs up to three `open()` + `json.load()` calls on the event loop
    (`health_payload` is called inline, in front of the live `/ws` stream). An
    unauthenticated poller must not be able to buy that work per request, and a
    supervisor polling every second cannot tell a two-second-old alignment answer
    from a fresh one: the value changes only when somebody edits a settings file.

    Deliberately NOT used by the tokened list route — that is where the page reads
    `aligned`, and an operator who has just corrected `settings.local.json`
    is entitled to see it in the very next refresh. Keyed by root and swept
    wholesale (a handful of keys at most; a growing dict is a growing collection
    like any other).
    """
    now = time.monotonic()
    cached = _MEMORY_ALIGN_CACHE.get(root)
    if cached is not None and (now - cached[0]) < MEMORY_HEALTH_TTL:
        return cached[1]
    answer = memory_alignment(root)
    if len(_MEMORY_ALIGN_CACHE) > 32:
        _MEMORY_ALIGN_CACHE.clear()
    _MEMORY_ALIGN_CACHE[root] = (now, answer)
    return answer


# --------------------------------------------------------------------------
# Reading: the list and one file.
# --------------------------------------------------------------------------

def memory_root_writable(root: str) -> bool:
    """True when this process could write the root — creating it if need be.

    An ABSENT root with a writable parent counts as writable, and that is not a
    convenience: a fresh checkout has no `.touch/memory/` yet, and a `writable:
    false` there would disable the create affordance that would bring the
    directory into existence — a control the server would then refuse to honour
    for a reason that is not true (D13's honest-affordance rule, read the other
    way round). `/health` reports the directory's EXISTENCE separately
    (`present`), so the two questions stay two questions.
    """
    if not root:
        return False
    here = os.path.abspath(root)
    while not os.path.isdir(here):
        # The write path creates the root with `memory_makedirs`, which creates
        # `.touch/` too, so the question is whether the nearest EXISTING ancestor
        # is writable — not whether the leaf happens to be there yet.
        parent = os.path.dirname(here)
        if parent == here:
            return False
        here = parent
    return os.access(here, os.W_OK)


def memory_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def memory_read_file(full: str):
    """`(bytes, stat)` for an existing memory file, or raise a named 404."""
    try:
        with open(full, "rb") as handle:
            data = handle.read(MAX_MEMORY_BYTES + 1)
        st = os.stat(full)
    except FileNotFoundError:
        raise MemoryRefusal(404, "missing", "no such file in the memory root")
    except IsADirectoryError:
        raise MemoryRefusal(404, "missing", "that name is a directory")
    if len(data) > MAX_MEMORY_BYTES:
        raise MemoryRefusal(
            413, "too-large",
            f"the file on disk is larger than this editor's {MAX_MEMORY_BYTES}-byte "
            f"per-file cap, so it is not served for editing")
    return data, st


def memory_entry(root: str, name: str, writable_root: bool) -> dict:
    """One `files[]` row: what the editor needs to be honest about the file.

    `lines` is Python's `splitlines()` count — the unit the page and the
    repository gate both count in — and `overLoadLimit` is only ever about the
    INDEX, because it is the only file with a documented load budget.
    """
    full = os.path.join(root, name)
    row = {"name": name, "size": 0, "mtime_ns": 0, "lines": None,
           "isIndex": name == MEM_INDEX_NAME, "overLoadLimit": False,
           "hasFrontmatter": False, "writable": writable_root, "reason": ""}
    if not MEMORY_NAME_RE.match(name):
        # Listed, and honestly unwritable: a name this API cannot address (a
        # space, a leading dot, 70 characters) is still a file the model loads,
        # so hiding it would understate what is in the directory.
        row["writable"] = False
        row["reason"] = ("this name is outside the flat namespace this editor "
                         "can address, so it can be seen here but not saved")
        try:
            st = os.stat(full)
            row["size"] = st.st_size
            row["mtime_ns"] = st.st_mtime_ns
        except OSError:
            pass
        return row
    try:
        st = os.lstat(full)
    except OSError:
        row["writable"] = False
        row["reason"] = "this entry could not be stat'ed"
        return row
    if stat.S_ISLNK(st.st_mode):
        # `lstat`, and the row stops HERE: reporting the size and mtime of a
        # symlink's target would publish a fact about a file outside the memory
        # root through a row the operator reads as being about a memory file.
        row["writable"] = False
        row["reason"] = ("this entry is a symlink and the write path refuses to "
                         "follow one")
        return row
    row["size"] = st.st_size
    row["mtime_ns"] = st.st_mtime_ns
    if st.st_size > MAX_MEMORY_BYTES:
        row["writable"] = False
        row["reason"] = (f"this file is over the {MAX_MEMORY_BYTES}-byte per-file "
                         f"cap, so this editor will not load or save it")
        return row
    try:
        with open(full, "rb") as handle:
            raw = handle.read(MAX_MEMORY_BYTES + 1)
    except OSError:
        row["writable"] = False
        row["reason"] = "this file could not be read"
        return row
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        row["writable"] = False
        row["reason"] = ("this file is not valid UTF-8, so this editor will not "
                         "rewrite it")
        return row
    row["lines"] = len(text.splitlines())
    row["hasFrontmatter"] = MEMORY_FRONTMATTER.match(text) is not None
    if row["isIndex"]:
        lines, size = memory_index_budget(text)
        row["overLoadLimit"] = lines > MEM_INDEX_LINES or size > MEM_INDEX_BYTES
    return row


def memory_scan(root: str):
    """The flat `.md` listing: `(rows, name_count, bytes, truncated)`.

    A flat non-recursive `os.scandir`, never `os.walk`: this is one directory
    with a handful of small files, and the artifact walker's depth-4/300-file
    crawl is the wrong tool (SERVER-13). `.history/` and `.trash/` are
    directories and drop out for free.
    """
    writable_root = memory_root_writable(root) and MEMORY_WRITE
    names = []
    total = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.name.lower().endswith(".md"):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    continue
                names.append(entry.name)
    except OSError:
        return [], 0, 0, False
    rows, truncated = [], False
    for name in sorted(names, key=lambda n: (n != MEM_INDEX_NAME, n.lower())):
        if len(rows) >= MEMORY_LIST_CAP:
            truncated = True
            break
        row = memory_entry(root, name, writable_root)
        total += int(row.get("size") or 0)
        rows.append(row)
    return rows, len(names), total, truncated


def memory_list_payload() -> dict:
    """`GET /api/memory/list` — G5's canonical shape, no field invented.

    `root` and the per-file names are published HERE and not on `/health`: this
    route needs the token, and a memory filename is a topic name — a disclosure
    in its own right (SERVER-10). The page reads `aligned` from this route for
    exactly that reason, never from `/health`.
    """
    root = MEMORY_ROOT
    rows, count, _total, truncated = memory_scan(root)
    aligned, effective = memory_alignment(root)
    return {
        "root": root,
        "aligned": aligned,
        "effective": effective,
        # Two SEPARATE booleans, never folded into one: the page words its
        # disabled affordance from whichever is false ("the write plane is off"
        # vs "the memory root is not writable"), and a conflated field would
        # print the wrong reason for a true refusal (G6/UI-6).
        "writable": memory_root_writable(root),
        "memoryWrite": bool(MEMORY_WRITE),
        "limits": {"maxBytes": MAX_MEMORY_BYTES, "maxFiles": MAX_MEMORY_FILES,
                   "indexLines": MEM_INDEX_LINES, "indexBytes": MEM_INDEX_BYTES},
        "files": rows,
        # Additive, in the house style of the snapshot's `logTruncated`: a cap
        # that is not disclosed is a cap that lies about the directory.
        "count": count,
        "listTruncated": truncated,
    }


def memory_read_payload(name: str) -> dict:
    """`GET /api/memory/file?name=` — G5's shape, JSON and not `text/plain`.

    G8 describes serving a memory file as a DOCUMENT as `text/plain`; this route
    is the API, and G5's table is canonical for it: the `sha256` a client must
    echo back as `ifMatch` cannot travel in a bare text body at all, and
    `memory.html` refuses any answer that is not `application/json` rather than
    mis-parsing one. There is deliberately no document route for these files —
    `.md`-only plus no HTML serving is what keeps the stored-XSS class absent
    from a directory a browser can write (G8).
    """
    full = safe_memory_path(MEMORY_ROOT, name)
    data, st = memory_read_file(full)
    text = memory_decode(data)
    return {"name": name, "content": text, "size": st.st_size,
            "sha256": memory_sha(data), "mtime_ns": st.st_mtime_ns,
            "hasFrontmatter": MEMORY_FRONTMATTER.match(text) is not None}


# --------------------------------------------------------------------------
# Writing: G7's hazard order, 1 to 10.
# --------------------------------------------------------------------------

_MEMORY_LOCKS: dict = {}
_MEMORY_LOCKS_GUARD = threading.Lock()


def memory_lock(name: str) -> threading.Lock:
    """The per-name write lock (G7 step 5).

    The realistic racer is not two tabs: it is Claude writing the same file from
    the session this dashboard is watching. `ifMatch` is what protects against
    THAT; this lock is what keeps two of this server's own requests from
    interleaving a read-modify-write on one name. Swept at a cap, because a dict
    keyed by attacker-supplied names is a growing collection like any other.
    """
    with _MEMORY_LOCKS_GUARD:
        lock = _MEMORY_LOCKS.get(name)
        if lock is None:
            if len(_MEMORY_LOCKS) >= MEMORY_LOCK_CAP:
                for key, value in list(_MEMORY_LOCKS.items()):
                    if not value.locked():
                        _MEMORY_LOCKS.pop(key, None)
            lock = _MEMORY_LOCKS[name] = threading.Lock()
        return lock


def memory_atomic_write(full: str, data: bytes) -> None:
    """G7 step 4: `O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW` 0600 temp, fsync, replace.

    Never `open(target, "w")`: that truncates before it writes, so a process
    that dies mid-request leaves a ZERO-BYTE instruction file that the next
    session loads. The temp file is created in the same directory (so
    `os.replace` is a rename, not a copy), `O_EXCL` kills the
    check-then-open TOCTOU, `O_NOFOLLOW` refuses a planted symlink at the temp
    name, and the directory fd is fsync'ed so the rename itself survives a
    crash.
    """
    directory = os.path.dirname(full)
    tmp = os.path.join(directory,
                       f"{os.path.basename(full)}.tmp-{secrets.token_hex(6)}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, full)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def memory_makedirs(path: str) -> None:
    """`os.makedirs`, except EVERY level is created 0700 (SECURITY-15).

    `os.makedirs(path, mode=0o700)` applies the mode to the FINAL component only;
    every intermediate directory it creates gets the default `0777 & ~umask`,
    which is 0755 on a normal box. That is how a `.touch/` or a `.trash/` ends up
    world-readable underneath a 0700 leaf — one directory below files that hold
    model instructions and, one level up from the memory root, the per-boot token
    and the Mongo credentials.
    """
    parts = os.path.abspath(path).split(os.sep)
    here = os.sep
    for part in parts[1:]:
        here = os.path.join(here, part)
        try:
            os.mkdir(here, 0o700)
        except FileExistsError:
            continue


def memory_side_dir(root: str, kind: str, name: str) -> str:
    """`<root>/.history/<name>/` or `<root>/.trash/<name>/`, created 0700.

    Both live INSIDE the memory root and both are invisible to git: the `.md`
    allowlist carve re-includes only top-level `*.md`, so a dot-directory under
    it stays ignored (G9, verified). They are also invisible to the list route,
    which lists files and not directories — the trash is surfaced by the page
    from the path each DELETE returns, not by a second listing.

    Neither `<root>/<kind>` nor the per-name folder under it may be a symlink:
    `memory_makedirs` swallows `FileExistsError` at every level (it has to — the
    normal case is that the tree already exists), so without this check a planted
    `.trash -> /elsewhere` would silently redirect every backup and every deleted
    file out of the project. Same refusal category as a symlinked target, because
    it is the same hazard one directory up (G7 step 2).
    """
    parent = os.path.join(root, kind)
    path = os.path.join(parent, name)
    for candidate in (parent, path):
        if os.path.islink(candidate):
            raise MemoryRefusal(
                409, "symlink",
                "the memory root's history/trash directory is a symlink; this "
                "server refuses to follow one, and nothing was written")
    memory_makedirs(path)
    return path


def memory_keep_newest(directory: str, keep: int) -> None:
    """Cap one history/trash folder: newest `keep` survive (W11's "capped")."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return
    for stale in names[:-keep] if keep < len(names) else []:
        try:
            os.unlink(os.path.join(directory, stale))
        except OSError:
            pass


def memory_snapshot(root: str, kind: str, name: str, data: bytes) -> str:
    """Copy `data` into the history/trash folder 0600; return the relative path."""
    directory = memory_side_dir(root, kind, name)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    leaf = f"{stamp}-{memory_sha(data)[:12]}.md"
    memory_atomic_write(os.path.join(directory, leaf), data)
    memory_keep_newest(directory, MEMORY_HISTORY_KEEP)
    return os.path.join(kind, name, leaf)


def memory_audit(root: str, op: str, name: str, data: bytes) -> None:
    """G7 step 10: one JSON line per mutation, beside the memory dir.

    `.touch/memory-audit.jsonl` — OUTSIDE `.touch/memory/`, because a `.jsonl`
    inside it would be listed by the list route and reasoned about by the git
    carve; out here `/.touch/*` ignores it (G9). `status.sh`'s discipline: one
    `LOCK_EX`'d append per line, a `w` attribution on every line (Part D-5), and
    a cap on the file.

    This is NOT the plan-card stream: no `touch-status` runs, no `events.jsonl`
    line is appended, and no badge — fabricated or otherwise — can come out of a
    memory edit (PROTOCOL-20, R-58, Part D-6). An audit failure never fails the
    write that already landed; it warns on stderr, because a monitoring record
    must not break the thing it records.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(root)), "memory-audit.jsonl")
    line = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "op": op, "name": name, "bytes": len(data),
        "sha256": memory_sha(data), "w": "monitor",
    }) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(line)
                handle.flush()
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        if os.path.getsize(path) > MEMORY_AUDIT_BYTES:
            with open(path, "rb") as handle:
                tail = handle.read()[-(MEMORY_AUDIT_BYTES // 2):]
            tail = tail.partition(b"\n")[2]      # never keep half a line
            memory_atomic_write(path, tail)
    except OSError as exc:
        print(f"monitor_server: memory audit line not written ({exc})",
              file=sys.stderr, flush=True)


def memory_current(full: str):
    """`(bytes, stat)` of what is on disk now, for a precondition check.

    Bounded like every other read on this plane: the file on disk was not
    necessarily written here (Claude writes these files too), so a 200 MB
    `MEMORY.md` must be a named 413 rather than 200 MB in the event loop's
    memory — and, since a 409 body carries the current content, in a JSON
    response as well.
    """
    with open(full, "rb") as handle:
        data = handle.read(MAX_MEMORY_BYTES + 1)
    if len(data) > MAX_MEMORY_BYTES:
        raise MemoryRefusal(
            413, "too-large",
            f"the file on disk is larger than the {MAX_MEMORY_BYTES}-byte "
            f"per-file cap, so this editor will not rewrite it")
    return data, os.stat(full)


def memory_precondition(full: str, if_match: str, op: str):
    """G7 step 5: `ifMatch` is REQUIRED, `"*"` is refused, a mismatch is a 409.

    Two different statuses on purpose, because they are two different operator
    situations: a request with NO precondition is a client that has not read the
    file (412 — there is no legitimate blind overwrite of a file that is
    injected into future sessions, which is also why `"*"` is not a carrier this
    API accepts), and a request whose precondition FAILED is a real concurrent
    write (409, carrying the current state so the page can offer reload /
    show-both / overwrite instead of a bare retry).

    The shape check (`MEMORY_SHA_RE`) runs FIRST, before the file is even read:
    it is what keeps a client-supplied non-ASCII string out of
    `hmac.compare_digest`, and it costs nothing on the path that succeeds.

    One deliberate divergence from G5's canonical error table, recorded rather
    than left to be discovered: the table lists `412` on the `PUT` row only, and
    this function raises the same `412 no-precondition` for a DELETE with no (or
    a malformed) `ifMatch`. A delete of an instruction file is not a lesser
    operation than a save, so it gets the same precondition and the same honest
    status; the DELETE row's error set is therefore `400 401 403 404 405 409 412`
    and I16's route table must say so.
    """
    if not isinstance(if_match, str) or not MEMORY_SHA_RE.fullmatch(if_match):
        raise MemoryRefusal(
            412, "no-precondition",
            f"this {op} needs the ifMatch sha256 of the bytes you last read — 64 "
            f"lowercase hex characters; \"*\" is not accepted for a file that "
            f"loads into future sessions")
    data, st = memory_current(full)
    current = memory_sha(data)
    # Both sides are now known to be 64 ASCII hex characters, which is what makes
    # `compare_digest` (whose `str` form refuses non-ASCII) safe to call here.
    if not hmac.compare_digest(if_match, current):
        state = {"sha256": current, "mtime_ns": st.st_mtime_ns,
                 "size": st.st_size}
        try:
            # Strict, like every other read on this plane: a `replace` here would
            # publish U+FFFD as "what is on disk now", and the page's own
            # reload-then-save exit would then write those replacement characters
            # back over bytes it never actually read.
            state["content"] = data.decode("utf-8")
        except UnicodeDecodeError:
            raise MemoryRefusal(
                409, "precondition",
                "the file changed on disk since you read it, and what is there "
                "now is not valid UTF-8 — its bytes are not published here "
                "because this editor will not rewrite them", state)
        raise MemoryRefusal(
            409, "precondition", "the file changed on disk since you read it",
            state)
    return data, st


def memory_file_cap(size: int) -> None:
    """G7 step 6's per-FILE half.

    Called twice per write: once on the content as it was sent, and again after
    `memory_stamp`, because refreshing a `modified:` line changes the length and a
    cap enforced only before the last transformation is a cap with a gap.
    """
    if size > MAX_MEMORY_BYTES:
        raise MemoryRefusal(
            413, "too-large",
            f"the content is {size} bytes; the per-file cap is "
            f"{MAX_MEMORY_BYTES}")


def memory_dir_caps(root: str, rows: list, incoming: int) -> None:
    """G7 step 6's directory half, enforced on CREATE only."""
    if len(rows) >= MAX_MEMORY_FILES:
        raise MemoryRefusal(
            413, "too-many-files",
            f"the memory root already holds {len(rows)} files, the cap is "
            f"{MAX_MEMORY_FILES}")
    total = sum(int(row.get("size") or 0) for row in rows)
    if total + incoming > MAX_MEMORY_DIR_BYTES:
        raise MemoryRefusal(
            413, "dir-too-large",
            f"the memory root would exceed its {MAX_MEMORY_DIR_BYTES}-byte "
            f"total cap")


def memory_mutate(op: str, name: str, payload: dict, if_match: str):
    """One mutation. G7's ten hazards, grouped VALIDATE -> DECIDE -> COMMIT.

    Returns `(status, body)`. Every step below keeps its G7 number in a comment,
    and this paragraph is here because those numbers do NOT execute 1...10 and
    cannot: step 4 IS the atomic write, so 5 (the precondition), 6 (the caps) and
    7 (hygiene) necessarily precede it, and 9 (the backup of the bytes being
    replaced) has to happen in the same breath as it. So the phases are explicit
    and the numbering is monotone WITHIN each phase:

    * **VALIDATE** — no filesystem effect at all: G7 1 (the flat name), 2 (the
      `lstat` symlink refusal), 3 (containment plus the `~/.claude` and
      plugin-cache ancestor refusals). The memory root is created only AFTER this
      passes, so a refused name leaves nothing behind.
    * **DECIDE** — inside the per-name lock, so existence and the precondition are
      one atomic story: existence (409 `exists` / 404 `missing`), G7 5, the strict
      decode, G7 6 (both caps), G7 7 (content hygiene).
    * **COMMIT** — G7 9 (the backup), 4 (the atomic write), 8 (for a delete, the
      move to trash instead of an `unlink`), 10 (the audit line).

    Hygiene runs after existence and the precondition deliberately: a PUT of
    unhygienic content to a file that is not there must answer `404 missing`, and
    a POST into a full directory must answer `413`, rather than reporting a body
    problem for a request that could not have succeeded either way. The one check
    that still runs before the lock is "is this a write request at all" — the
    `content` type check — because that is a malformed request rather than a
    decision about a file, and it belongs with the JSON parse it follows.

    Runs on a worker thread (it is all blocking file I/O), which is also why the
    per-name lock is a `threading.Lock` and not an asyncio one.
    """
    root = MEMORY_ROOT
    raw, allow_pinned = None, False
    if op != "delete":
        raw = payload.get("content")
        if not isinstance(raw, str):
            raise MemoryRefusal(400, "no-content",
                                "the request body needs a string `content`")
        allow_pinned = payload.get("allowPinned") is True
    full = safe_memory_path(root, name)                      # G7 steps 1-3
    if op != "delete" and not os.path.isdir(root):
        # First write into a fresh checkout, and created only AFTER the name has
        # been validated: G7 step 1 is explicitly "flat name validation, not path
        # handling ... before any filesystem call", so a refused name must not
        # leave a new directory behind as its only visible effect. 0700 from the
        # start, like the rest of `.touch/` — never created loose and chmod'ed
        # after (SECURITY-15).
        try:
            memory_makedirs(root)
        except OSError as exc:
            raise MemoryRefusal(500, "no-root",
                                f"the memory root could not be created: {exc}")
    with memory_lock(name):
        exists = os.path.isfile(full)
        if op == "create" and exists:
            raise MemoryRefusal(
                409, "exists",
                "a file with that name is already in the memory root, and a "
                "create never overwrites one")
        if op in ("update", "delete") and not exists:
            raise MemoryRefusal(404, "missing", "no such file in the memory root")
        if op == "delete":
            previous, _st = memory_precondition(full, if_match, "delete")  # step 5
            trash = memory_snapshot(root, ".trash", name, previous)   # G7 step 8
            os.unlink(full)
            memory_audit(root, "delete", name, previous)              # G7 step 10
            return 200, {"name": name, "deleted": True, "trash": trash}
        if op == "update":
            previous, _st = memory_precondition(full, if_match, "save")   # step 5
        else:
            previous = None
        # JSON hands us a `str`, and a `\udXXX` escape in it is a lone surrogate
        # that no valid UTF-8 file can hold. The round trip through
        # `surrogatepass` is what turns that into the same named 400 a bad byte
        # sequence gets, instead of a UnicodeEncodeError at write time.
        content = memory_normalize(memory_decode(
            raw.encode("utf-8", "surrogatepass")))
        incoming = len(content.encode("utf-8"))
        memory_file_cap(incoming)                                     # G7 step 6
        if op == "create":                                            # G7 step 6
            rows, _count, _total, _trunc = memory_scan(root)
            memory_dir_caps(root, rows, incoming)
        memory_hygiene(content, allow_pinned=allow_pinned)            # G7 step 7
        # `memory_stamp` is a no-op unless the content ALREADY carries leading
        # frontmatter, so it is called unconditionally: the rule is "never invent
        # frontmatter", not "never stamp".
        final = memory_stamp(content)
        data = final.encode("utf-8")
        memory_file_cap(len(data))                                    # G7 step 6
        if previous is not None and previous != data:
            memory_snapshot(root, ".history", name, previous)          # G7 step 9
        memory_atomic_write(full, data)                               # G7 step 4
        st = os.stat(full)
        memory_audit(root, op, name, data)                            # G7 step 10
        return (201 if op == "create" else 200), {
            "name": name, "size": st.st_size, "sha256": memory_sha(data),
            "mtime_ns": st.st_mtime_ns,
        }


def memory_health() -> dict:
    """The `/health` block: counts and booleans ONLY (SERVER-10).

    `/health` is the one untokened route, which is why every path it would
    otherwise publish is hashed. A memory filename is a topic name, so there are
    no names here and no root either — not even a digest, because the digest
    would answer nothing a supervisor needs. Whether the plane is on, whether it
    is aligned, how much is there: that is the whole disclosure.
    """
    root = MEMORY_ROOT
    if memory_unavailable():
        return {"present": False, "writable": False, "aligned": None,
                "files": 0, "bytes": 0, "indexOverLimit": False}
    # A LIGHTER scan than the list route's, on purpose: this route answers
    # without a token and a supervisor may poll it every second. The work per
    # request is, exactly: one `scandir` with N `stat`s (no file contents), at
    # most ONE bounded file read — the index, the only file with a documented
    # load budget to be over — and an `aligned` answer that comes from
    # `memory_alignment_cached`, so the up-to-three settings-file parses behind it
    # happen at most once every `MEMORY_HEALTH_TTL` seconds however hard this
    # route is polled. `memory_scan` reads every file to count lines, which is
    # right for the tokened editor and wrong here.
    files, total, over = 0, 0, False
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.name.lower().endswith(".md"):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                files += 1
                total += st.st_size
    except OSError:
        pass
    index = os.path.join(root, MEM_INDEX_NAME)
    try:
        if os.path.isfile(index) and os.path.getsize(index) <= MAX_MEMORY_BYTES:
            with open(index, "rb") as handle:
                # `replace` is correct HERE and nowhere else on this plane: this
                # is a measurement, not a write, and an index that is not valid
                # UTF-8 still has a line count the operator should hear about.
                text = handle.read().decode("utf-8", "replace")
            lines, size = memory_index_budget(text)
            over = lines > MEM_INDEX_LINES or size > MEM_INDEX_BYTES
    except OSError:
        pass
    aligned, _effective = memory_alignment_cached(root)
    return {"present": os.path.isdir(root), "writable": memory_root_writable(root),
            "aligned": aligned, "files": files, "bytes": total,
            "indexOverLimit": over}


# --------------------------------------------------------------------------
# The HTTP half (I10): a (method, route) table, a response builder, a bounded
# body reader, and the positive write-auth predicate.
# --------------------------------------------------------------------------

#: Every memory route, keyed the way the aggregator keys its own table: by
#: (METHOD, route). A known route reached by the wrong method is a 405 with an
#: `Allow:` header, and an unknown route under the prefix is a JSON 404 — never
#: the HTML page, which is what turns a client typo into an invisible failure
#: (SERVER-1, SERVER-7, UI-1). The read routes of the REST of this server stay
#: method-blind on purpose (SERVER-1b): tightening them is a real behaviour
#: change with its own test pass, and mixing it in here would risk an unrelated
#: regression in the same commit.
MEMORY_ROUTES = {
    ("GET", MEMORY_ROUTE): "page",
    ("GET", MEMORY_API_PREFIX + "list"): "list",
    ("GET", MEMORY_API_PREFIX + "file"): "read",
    ("POST", MEMORY_API_PREFIX + "file"): "create",
    ("PUT", MEMORY_API_PREFIX + "file"): "update",
    ("DELETE", MEMORY_API_PREFIX + "file"): "delete",
}
MEMORY_KNOWN_ROUTES = frozenset(route for _method, route in MEMORY_ROUTES)
MEMORY_WRITE_OPS = frozenset({"create", "update", "delete"})

STATUS_TEXT = {
    200: "OK", 201: "Created", 204: "No Content",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
    411: "Length Required", 412: "Precondition Failed",
    413: "Payload Too Large", 415: "Unsupported Media Type",
    422: "Unprocessable Content", 500: "Internal Server Error",
    503: "Service Unavailable",
}
_STATUS_CLASS = {1: "Informational", 2: "Success", 3: "Redirection",
                 4: "Client Error", 5: "Server Error"}


def status_text(status: int) -> str:
    """The reason phrase for `status`, and NEVER `"OK"` for one we do not name.

    The aggregator carried this bug until SERVER-8 named it (fixed there in the
    same pass, and its table is the one this mirrors): a
    `STATUS_TEXT.get(status, "OK")` fallback sends `HTTP/1.1 409 OK`, a conflict
    wearing a success phrase. A reason phrase is advisory to every parser and
    read by exactly one audience — the human looking at a capture — so an
    unnamed status falls back to its CLASS, which is derived from the code and
    therefore cannot contradict it (SERVER-8).
    """
    status = int(status)
    named = STATUS_TEXT.get(status)
    if named:
        return named
    return _STATUS_CLASS.get(status // 100, "Unknown Status")


def header_value(value) -> str:
    """A header value with CR/LF and other control bytes removed.

    Every value this file emits is server-generated today, so this is belt and
    braces — but a header is a line in a protocol, and the one place a caller's
    string could ever reach one is worth making structurally safe rather than
    remembering to check.
    """
    return re.sub(r"[^\t\x20-\x7e]", "", str(value))


def http_response(status: int, body: bytes, content_type: str,
                  headers: dict = None) -> bytes:
    """One response, built once. `Cache-Control: no-store` on every one of them.

    The memory group answers over the same connection style the rest of this
    server uses (`Connection: close`), and never caches: a stale editor list or
    a cached file body is an operator editing bytes that are no longer there.
    """
    lines = [f"HTTP/1.1 {status} {status_text(status)}",
             f"Content-Type: {content_type}",
             "X-Content-Type-Options: nosniff",
             "Cache-Control: no-store",
             f"Content-Length: {len(body)}",
             "Connection: close"]
    for key, value in (headers or {}).items():
        lines.append(f"{header_value(key)}: {header_value(value)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin1") + body


def json_response(status: int, payload: dict, headers: dict = None) -> bytes:
    """A JSON answer — the shape every route under `/api/memory/` speaks.

    Including the failures: `memory.html` checks the content type BEFORE it
    parses, so an HTML fallback or a text/plain error would be NAMED on screen
    instead of silently mis-parsed (UI-1/UI-4).
    """
    body = json.dumps(payload).encode("utf-8")
    return http_response(status, body, "application/json", headers)


def requires_write_auth(method: str, route: str) -> bool:
    """POSITIVE: does this (method, route) mutate the file plane? (W4/SECURITY-16)

    Derived from the route table itself, not from a hand-maintained open list.
    The negative shape — `route not in OPEN_ROUTES` — is how a route added by
    analogy ends up unauthenticated with no second gate behind it; here a new
    write entry in the table is covered the moment it exists. The method is part
    of the question because one route (`/api/memory/file`) is read by `GET` and
    written by three other verbs.
    """
    return MEMORY_ROUTES.get(((method or "").upper(), route)) in MEMORY_WRITE_OPS


def is_memory_route(route: str) -> bool:
    """True for the page and for everything under the API prefix.

    Prefix membership and not table membership: an unknown route under
    `/api/memory/` must be answered by THIS group (with a JSON 404), never by
    the HTML fallback at the bottom of `handle()`.
    """
    return (route == MEMORY_ROUTE or route == MEMORY_API_PREFIX.rstrip("/")
            or route.startswith(MEMORY_API_PREFIX))


def memory_allowed_methods(route: str) -> list:
    return sorted(method for method, known in MEMORY_ROUTES if known == route)


async def read_memory_body(reader, headers: dict):
    """A bounded body read, for this group ONLY. `(raw, None)` or `(None, refusal)`.

    Nothing else on this server has ever read a body, so every rule is written
    here rather than assumed (SERVER-2, SECURITY-14, W9):

    * `Transfer-Encoding: chunked` is refused outright — this server does not
      implement the framing, and accepting the header while ignoring it is a
      request-smuggling shape;
    * `Content-Length` is REQUIRED (411): a write with no length would otherwise
      become an empty save that reports success;
    * the length is checked against the cap BEFORE a byte is read (413);
    * `readexactly` under a timeout, and a short read is a 400 — never a
      silently truncated instruction file.
    """
    if "chunked" in (headers.get("transfer-encoding") or "").lower():
        return None, MemoryRefusal(
            400, "chunked", "chunked request bodies are not accepted; send a "
                            "Content-Length")
    raw_length = (headers.get("content-length") or "").strip()
    try:
        length = int(raw_length)
    except ValueError:
        return None, MemoryRefusal(
            411, "no-length",
            "a write needs an explicit Content-Length; nothing was written")
    if length < 0:
        return None, MemoryRefusal(411, "no-length",
                                   "Content-Length must not be negative")
    if length > MAX_MEMORY_BODY_BYTES:
        return None, MemoryRefusal(
            413, "body-too-large",
            f"the body is {length} bytes; the cap is {MAX_MEMORY_BODY_BYTES} "
            f"(checked before reading)")
    if length == 0:
        return b"", None
    try:
        data = await asyncio.wait_for(reader.readexactly(length),
                                      MEMORY_BODY_TIMEOUT)
    except asyncio.IncompleteReadError:
        return None, MemoryRefusal(
            400, "short-body",
            "the connection ended before Content-Length bytes arrived; nothing "
            "was written (a short read is never treated as a truncation)")
    except (asyncio.TimeoutError, OSError):
        return None, MemoryRefusal(
            400, "body-timeout",
            "the body did not arrive within the read timeout; nothing was written")
    return data, None


async def memory_http(method: str, route: str, query: str, headers: dict,
                      reader) -> bytes:
    """The whole `/memory` + `/api/memory/` group, in G5's order.

    Auth already ran in `handle()` (token first, before any route does work, and
    header-only on the write verbs). What is left, in order: the route table,
    the Origin/Host gate on EVERY route including the reads, the write marker,
    the availability of the family, the write plane's own flag, the content
    type, the bounded body, and only then the handler.
    """
    op = MEMORY_ROUTES.get((method, route))
    if op is None:
        if route not in MEMORY_KNOWN_ROUTES:
            return json_response(404, {
                "error": "unknown-route", "category": "unknown-route",
                "reason": f"no memory route answers {route}"})
        allow = memory_allowed_methods(route)
        return json_response(
            405, {"error": "method-not-allowed", "category": "method-not-allowed",
                  "reason": f"{method} is not allowed on {route}",
                  "allow": allow},
            {"Allow": ", ".join(allow)})
    write = op in MEMORY_WRITE_OPS
    # The Origin/Host gate on a PLAIN HTTP route, not only on the /ws upgrade
    # (SECURITY-3, W3). `allow_missing_origin` is right for a read — a
    # non-browser client that already presented the token — and wrong for a
    # write: under DNS rebinding the request is same-origin, no preflight
    # happens, and the Host NAME check is the only wall left standing.
    refusal = origin_refusal(headers, allow_missing_origin=not write)
    if refusal:
        return json_response(403, {"error": "origin", "category": "origin",
                                   "reason": refusal})
    if write and (headers.get("x-touch-write") or "").strip() != "1":
        # A simple cross-origin request cannot set this header, and the preflight
        # it forces has nothing to succeed against because this server emits no
        # Access-Control-Allow-* header, ever (W2).
        return json_response(403, {
            "error": "write-marker", "category": "write-marker",
            "reason": "a write must carry the X-Touch-Write: 1 header"})
    if op == "page":
        try:
            with open(MEMORY_HTML, "rb") as handle:
                body = handle.read()
        except OSError:
            return json_response(503, {
                "error": "page-missing", "category": "page-missing",
                "reason": "memory.html is missing from this installation"})
        STATS["page_hits"] += 1
        return http_response(200, body, "text/html; charset=utf-8",
                             {"Referrer-Policy": NO_REFERRER})
    unavailable = memory_unavailable()
    if unavailable:
        # 503 and not 404: the directory is not missing, this SERVER is not in a
        # position to serve one. The page prints the reason as its banner.
        return json_response(503, {"error": "memory-unavailable",
                                   "category": "memory-unavailable",
                                   "reason": unavailable})
    if write and not MEMORY_WRITE:
        return json_response(403, {
            "error": "write-plane-off", "category": "write-plane-off",
            "reason": "the write plane is off (the default): restart "
                      "touch-monitor with --allow-memory-write, or "
                      "TOUCH_ALLOW_MEMORY_WRITE=1, to enable saves"})
    params = urllib.parse.parse_qs(query or "")
    name = (params.get("name") or [""])[0]
    if_match = (params.get("ifMatch") or [""])[0]
    payload = {}
    if op in ("create", "update"):
        ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return json_response(415, {
                "error": "content-type", "category": "content-type",
                "reason": "a write body must be application/json"})
        raw, body_refusal = await read_memory_body(reader, headers)
        if body_refusal is not None:
            return json_response(body_refusal.status, body_refusal.body())
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return json_response(400, {
                "error": "bad-json", "category": "bad-json",
                "reason": "the request body is not readable JSON"})
        if not isinstance(payload, dict):
            return json_response(400, {
                "error": "bad-json", "category": "bad-json",
                "reason": "the request body must be a JSON object"})
        if op == "update":
            supplied = payload.get("ifMatch")
            if isinstance(supplied, str):
                if_match = supplied
    try:
        if op == "list":
            return json_response(200, await asyncio.to_thread(memory_list_payload))
        if op == "read":
            return json_response(200, await asyncio.to_thread(
                memory_read_payload, name))
        status, body = await asyncio.to_thread(memory_mutate, op, name, payload,
                                              if_match)
        return json_response(status, body)
    except MemoryRefusal as exc:
        return json_response(exc.status, exc.body())
    except OSError as exc:
        # The filesystem said no (permissions, a full disk, a race with another
        # writer). Named, not swallowed: the page renders the sentence.
        return json_response(500, {
            "error": "io", "category": "io",
            "reason": f"the memory root could not be served: {exc.strerror or exc}"})


CONNECTIONS: set = set()  # live handler tasks, cancelled on shutdown


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    task = asyncio.current_task()
    CONNECTIONS.add(task)
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    except Exception:
        CONNECTIONS.discard(task)
        writer.close()
        return
    head = raw.decode("latin1")
    request_line = head.split("\r\n", 1)[0].split(" ")
    # The METHOD, at last (SERVER-1/SECURITY-2). It used to be parsed and thrown
    # away, so `POST /tasks` and `DELETE /` both answered as GETs — harmless while
    # nothing on this server accepted input, and unacceptable the moment one route
    # group mutates files. Only the memory group dispatches on it: the read routes
    # stay method-blind deliberately (SERVER-1b), because tightening them is an
    # unrelated behaviour change with its own test pass.
    method = (request_line[0] if request_line else "GET").upper()
    path = request_line[1] if len(request_line) > 1 else "/"
    route, _, query = path.partition("?")
    headers = {}
    for ln in head.split("\r\n")[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    try:
        # Auth FIRST, before any route does work and before the upgrade shapes
        # below get their protocol answer: an unauthenticated peer must learn
        # nothing about this server, not even that its Sec-WebSocket-Version is
        # wrong. The page itself ("/") is open — it is the static HTML that
        # carries the token in its own query string — and so is /health, so a
        # supervisor can probe a server it has no token for.
        if route not in ("/", "") and not token_ok(
                route, headers, query,
                header_only=requires_write_auth(method, route)):
            if route.startswith(MEMORY_API_PREFIX):
                # The JSON API answers JSON even when it refuses: `memory.html`
                # checks the content type BEFORE it parses, so a text/plain 401
                # would be reported as "this build has no memory API" instead of
                # raising the auth banner the operator can act on (UI-1/UI-4).
                writer.write(json_response(
                    401, {"error": "unauthorized", "category": "unauthorized",
                          "reason": "a per-boot token is required on this route, "
                                    "and a write must carry it in the "
                                    "X-Orch-Token header"},
                    {"WWW-Authenticate": 'Bearer realm="monitor"'}))
                await writer.drain()
                return
            body = b"a per-boot token is required on this route"
            writer.write(
                b"HTTP/1.1 401 Unauthorized\r\n"
                b'WWW-Authenticate: Bearer realm="monitor"\r\n'
                b"Content-Type: text/plain\r\nContent-Length: " +
                str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
            )
            await writer.drain()
            return
        if is_memory_route(route):
            # The file plane owns its own dispatch, its own 404 and its own 405:
            # an unknown route under the prefix must never fall through to the
            # HTML page below, which is how a client typo becomes an invisible
            # failure (SERVER-7, UI-1).
            writer.write(await memory_http(method, route, query, headers, reader))
            await writer.drain()
            return
        if route == "/ws":
            refusal = origin_refusal(headers)
            if refusal:
                body = refusal.encode()
                writer.write(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n"
                    b"Content-Length: " + str(len(body)).encode() +
                    b"\r\nConnection: close\r\n\r\n" + body
                )
                await writer.drain()
                return
        if route == "/ws" and (
            "sec-websocket-key" not in headers
            or headers.get("sec-websocket-version") not in (None, "13")
        ):
            # Malformed/unsupported upgrade: never serve the HTML body on /ws.
            # Missing key -> 400; wrong version -> 426 advertising 13 (SERVER-3).
            if headers.get("sec-websocket-version") not in (None, "13"):
                writer.write(
                    b"HTTP/1.1 426 Upgrade Required\r\n"
                    b"Sec-WebSocket-Version: 13\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
            else:
                writer.write(
                    b"HTTP/1.1 400 Bad Request\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
            await writer.drain()
        elif route == "/ws" and "sec-websocket-key" in headers:
            accept = base64.b64encode(
                hashlib.sha1((headers["sec-websocket-key"] + GUID).encode()).digest()
            ).decode()
            writer.write(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode()
            )
            await writer.drain()
            STATS["ws_clients"] += 1
            STATS["ws_active"] += 1
            try:
                # The path resolves the historical way in BOTH versions (an
                # unknown ?task= falls back to the default stream); what v2 adds
                # is that it refuses to serve that fallback, on the hello (GD-B).
                name, _dir, known = resolve_task(query)
                await stream_events(reader, writer, resolve_events_path(query),
                                    query, name, known)
            finally:
                STATS["ws_active"] -= 1
        elif route in ("/health", "/tasks", "/artifacts"):
            # ``to_thread`` buys I/O isolation, never CPU isolation — the GIL
            # means a CPU-bound helper stalls the event loop from a worker
            # thread just as thoroughly as it would inline (SERVER-READ-2; the
            # comment that used to sit here claimed otherwise). So the rule is
            # the other one: keep per-request Python work down to a few ms and
            # use a thread only for the blocking file access. ``task_artifacts``
            # walks a directory (I/O) and gets one; ``/tasks`` no longer needs a
            # full re-scan at all — the registry advances by appended bytes and
            # does its reading in a thread of its own.
            status = b"200 OK"
            if route == "/health":
                payload = health_payload()
            elif route == "/tasks":
                payload = await tasks_payload_live()
            else:
                name, state_dir, known = resolve_task(query)
                if not known:
                    payload, status = {"error": "unknown-task", "task": name}, b"404 Not Found"
                else:
                    arts = await asyncio.to_thread(task_artifacts, state_dir)
                    payload = {"artifacts": arts}
            body = json.dumps(payload).encode()
            writer.write(
                b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
            )
            await writer.drain()
        elif route == "/file":
            qs = urllib.parse.parse_qs(query)
            _name, state_dir, known = resolve_task(query)
            full = (safe_artifact_path(state_dir, qs.get("path", [""])[0])
                    if known else None)
            body = None
            if full:
                try:
                    body = await asyncio.to_thread(lambda: open(full, "rb").read())
                except OSError:
                    body = None
            if body is None:
                writer.write(
                    b"HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\n"
                    b"Content-Length: 9\r\nConnection: close\r\n\r\nnot found"
                )
            else:
                if full.lower().endswith(".md"):
                    # served as plain text: the dashboard fetches and renders
                    # the preview itself with its escape-first mini renderer
                    extra = b"Content-Type: text/plain; charset=utf-8\r\n"
                else:
                    # Report HTML renders in a new tab at
                    # `/file?...&token=<the per-boot token>`. A bare `sandbox`
                    # (NO allow-scripts) is what keeps that safe: an opaque
                    # origin stops the tab reading same-origin responses, but it
                    # does NOT stop a script in the report reading its own
                    # `location.search` and POSTing the token somewhere — and
                    # agent-generated reports are exactly the documents nobody
                    # audited. The reports this repo produces are static HTML;
                    # scripts are the thing being given up, and the token is the
                    # thing being kept. `Referrer-Policy` closes the same leak
                    # through the other door (an outbound subresource carrying
                    # the tokened URL as its Referer).
                    extra = (b"Content-Type: text/html; charset=utf-8\r\n" +
                             f"Content-Security-Policy: {FILE_CSP}\r\n"
                             f"Referrer-Policy: {NO_REFERRER}\r\n".encode("latin1"))
                writer.write(
                    b"HTTP/1.1 200 OK\r\n" + extra +
                    b"X-Content-Type-Options: nosniff\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
                )
            await writer.drain()
        else:
            STATS["page_hits"] += 1
            try:
                with open(HTML, "rb") as f:
                    body = f.read()
            except OSError:
                body = b"monitor.html missing"
            # The page's own URL carries the per-boot token in its query string,
            # and that token now also authorizes memory writes: a `Referer`
            # header leaving this document would hand it to whatever the page
            # links to (SECURITY-5). The page ALSO carries a `<meta
            # name="referrer">`, because the two cover different fetches — the
            # meta reaches a subresource a browser starts before it has parsed a
            # header, and this reaches a client that ignores meta tags entirely.
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n" +
                f"Referrer-Policy: {NO_REFERRER}\r\n".encode("latin1") +
                b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
            )
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        CONNECTIONS.discard(task)
        try:
            writer.close()
        except Exception:
            pass


async def main() -> None:
    try:
        server = await asyncio.start_server(handle, HOST, PORT)
    except OSError as exc:
        sys.exit(f"cannot bind {HOST}:{PORT} ({exc}); is another monitor_server "
                 f"still running? stop it with: pkill -f \"[m]onitor_server\"")
    reachable = "127.0.0.1" if HOST in (OPEN_HOST, "::", "") else HOST
    print(f"monitor listening on {HOST}:{PORT}"
          f"{'  (OPEN BIND — every route but /health still needs the token)' if HOST == OPEN_HOST else ''}",
          flush=True)
    token_path = write_token_file(STATE_DIR)
    # The token file is created 0600 with no world-readable window. Printing the
    # secret unconditionally would undo that: every driver in this repo
    # redirects this stdout into `<task-dir>/daemon.log`, which is 0644. So the
    # full URL goes to a TTY (a human watching a terminal) or to a log ONLY when
    # there is no file to read it from — otherwise the line carries a
    # non-reversible fingerprint, which is all a log needs to tell two boots
    # apart.
    if sys.stdout.isatty() or not token_path:
        print(f"open:      http://{reachable}:{PORT}/?token={TOKEN}", flush=True)
    else:
        print(f"open:      http://{reachable}:{PORT}/   "
              f"(append ?token= from {token_path}; token fp "
              f"{hashlib.sha256(TOKEN.encode()).hexdigest()[:8]})", flush=True)
    print(f"state dir: {STATE_DIR}", flush=True)
    print(f"events:    {EVENTS}", flush=True)
    # The file plane says what it is, out loud, at every boot: which directory
    # (or why none), and whether this process can write it. An operator must not
    # have to probe /health to learn that the port in front of them is an editor
    # (SECURITY-16's "refuse loudly", W14's disclosure).
    unavailable = memory_unavailable()
    if unavailable:
        print(f"memory:    unavailable — {unavailable}", flush=True)
    else:
        aligned, effective = memory_alignment(MEMORY_ROOT)
        print(f"memory:    {MEMORY_ROOT}", flush=True)
        if aligned is not True:
            print(f"           NOT aligned — Claude Code reads {effective}",
                  flush=True)
    print("memory writes: "
          + ("ON (--allow-memory-write / $TOUCH_ALLOW_MEMORY_WRITE)"
             if MEMORY_WRITE else
             "off (the default; --allow-memory-write enables the write plane)"),
          flush=True)
    if token_path:
        print(f"token written to {token_path} (0600)", flush=True)
    else:
        print("token not written to disk (state dir unwritable or inside a "
              "plugin cache); the URL above is the only copy", flush=True)

    stop = asyncio.Event()

    def confirm_stop() -> None:
        # Ctrl-C: confirm on a TTY; stop immediately when non-interactive.
        # input() briefly blocks the event loop — fine for a local dev tool.
        if stop.is_set():
            return
        if not sys.stdin.isatty():
            stop.set()
            return
        try:
            answer = input("\nStop monitor_server? [y/N] ").strip().lower()
        except (EOFError, RuntimeError):
            answer = "y"
        if answer in ("y", "yes"):
            stop.set()
        else:
            print("continuing", flush=True)

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, confirm_stop)
        loop.add_signal_handler(signal.SIGTERM, stop.set)  # no prompt on kill
    except NotImplementedError:
        pass  # non-Unix: fall back to default KeyboardInterrupt behavior
    await stop.wait()
    # server.close() alone isn't enough: wait_closed() (and Server.__aexit__)
    # blocks until every open connection finishes, and websocket streams never
    # end on their own — cancel them explicitly so shutdown can't hang.
    server.close()
    for task in list(CONNECTIONS):
        task.cancel()
    await asyncio.gather(*CONNECTIONS, return_exceptions=True)
    # ...and the per-stream tailers, which are not connection tasks: they end
    # on their own once the last subscriber goes, but a sleeping one would
    # otherwise be torn down mid-tick at loop close.
    tailers = [s.tailer for s in Stream._REGISTRY.values() if s.tailer]
    for task in tailers:
        task.cancel()
    await asyncio.gather(*tailers, return_exceptions=True)
    await server.wait_closed()
    print(stats_line(), flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Only reachable where signal handlers are unavailable — still exit clean.
        print(stats_line(), flush=True)
