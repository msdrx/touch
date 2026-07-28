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
import json
import os
import signal
import sys
import time
import urllib.parse
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
TASKS_ROOT = os.path.abspath(os.path.join(ROOT, "..", "..", "local-orchestrators"))
# Per-task state (events.jsonl, orch-config.json) lives in $ORCH_STATE_DIR;
# the shared module directory holds only code and stays stateless.


def resolve_state_dir() -> str:
    """State dir: $ORCH_STATE_DIR > newest task folder > script dir (empty fallback).

    The shared module directory (ROOT) is code-only and NEVER an authoritative
    state dir (D6): a stray ``events.jsonl`` written there must not hijack
    auto-discovery, so there is no ROOT short-circuit — we fall through to the
    newest-task-folder glob.
    """
    if os.environ.get("ORCH_STATE_DIR"):
        return os.environ["ORCH_STATE_DIR"]
    import glob
    candidates = glob.glob(os.path.join(TASKS_ROOT, "*", "events.jsonl"))
    if candidates:
        return os.path.dirname(max(candidates, key=os.path.getmtime))
    return ROOT


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
# --------------------------------------------------------------------------
FOLD_GEN = 1

# Timeplan thresholds. Mirrored literals of monitor.html:962/964 — the server
# derives the same segments the page would derive, so they MUST agree; they are
# part of the fold and therefore covered by FOLD_GEN.
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


def discover_tasks() -> dict:
    """name -> state dir, rescanned per request so tasks started later appear live."""
    tasks = {}
    try:
        for entry in sorted(os.listdir(TASKS_ROOT)):
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
        """monitor.html:570-577 — a closing card freezes its still-running rows."""
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
            row = {"label": a.get("label") or aid, "started": a.get("started") or ts,
                   "state": "running", "runtime": None, "finishedMs": None,
                   "tokens": None}
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
        if a.get("runtime"):
            row["runtime"] = a["runtime"]
        # monitor.html:518-519, key for key: the ROLE falls back to the id when
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
                    # this; the page has always done it (monitor.html:632-640).
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
                "logTruncated": self.log_truncated,
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
    # the friendly name and the earlier ones move to their full paths — every
    # stream stays visible instead of one silently overwriting the other.
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
            streams[named[s.task].path] = streams.pop(s.task)
        named[s.task] = s
        streams[s.task] = s.health()
    return {"status": "ok",
            "parse_failures_total": sum(PARSE_FAILURES.values()),
            "parse_failures": dict(PARSE_FAILURES),
            "streams": streams,
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


def resolve_port() -> int:
    """Port: argv > $ORCH_PORT > orch-config.json (state dir, then module dir) > 8931.

    A non-integer argv/env exits cleanly with a one-line message rather than a
    raw ``ValueError`` traceback at import (SERVER-2).
    """
    for source, label in ((sys.argv[1] if len(sys.argv) > 1 else None, "argv"),
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
    ignored = [k for k in params if k not in ("task", "v", "snap", "from", "sig")]
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
    path = request_line[1] if len(request_line) > 1 else "/"
    route, _, query = path.partition("?")
    headers = {}
    for ln in head.split("\r\n")[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    try:
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
                    # report HTML renders in a new tab; CSP sandbox keeps its
                    # scripts in an opaque origin, cut off from this server
                    extra = (b"Content-Type: text/html; charset=utf-8\r\n"
                             b"Content-Security-Policy: sandbox allow-scripts\r\n")
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
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
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
        server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    except OSError as exc:
        sys.exit(f"cannot bind 0.0.0.0:{PORT} ({exc}); is another monitor_server "
                 f"still running? stop it with: pkill -f \"[m]onitor_server\"")
    print(f"monitor listening on 0.0.0.0:{PORT}", flush=True)
    print(f"state dir: {STATE_DIR}", flush=True)
    print(f"events:    {EVENTS}", flush=True)

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
