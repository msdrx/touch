#!/usr/bin/env python3
"""cycle_reporter.py — deterministic report renderer + loop-close emitter.

Renders ONE visual report after EVERY implement->test->critique cycle of an
implement workflow run — and, at the end, the whole run's final report —
with zero LLM cooperation (the workflow runtime has no filesystem access, and an
LLM scribe would be non-deterministic — this daemon is the deterministic
option). It serves BOTH reference protocols: implement's gated loops and
research's research/synthesis pair (D-14). Same derivation technique as
decision_watcher.py:

  journal.jsonl result records ── agentId ──> agent-<id>.jsonl transcript
                                              └─ "[monitor] plan=.. stage=.. role=.. attempt=N"

Outputs, all under $ORCH_STATE_DIR (the task folder):
  report/cycles/<plan>-cycle-<N>.html   one page per cycle: UML-style flow
      (implement -> test gate -> critique -> verdict), a "why it failed /
      succeeded" section from the structured verdict summaries, and the full
      gate/critique findings files embedded as evidence.
  report/cycles/index.html              run overview, execution order.
  report/final-report.html              the run's FINAL report (--final), or
      report/research-report.html for a research run: a deterministic skeleton
      (run header, timeline, per-plan cards with verdicts/attempts/tokens/
      durations, links to the plan and every findings file) with exactly ONE
      slot an LLM fills — the narrative section, injected from a file with
      --narrative. Every number on it has ONE named source (journal, stream, or
      run snapshot), the page carries no render timestamp, and rendering the
      same inputs twice is byte-identical (D-15).
  events.jsonl (via status.sh)          the loop-terminal `plan done|failed`
      event when a loop closes — a REAL verdict at the published cap (never the
      retired phase-advance inference) — PLUS the terminal closes of the
      single-agent plans of both protocols: `divide` when the partition result
      lands (with ORCH_PLANS_TOTAL and, on the orchestrator card, the roster),
      `finalgate` when the sweep verdict settles, and `research`/`synthesis`
      for a research run (with ORCH_PLANS_TOTAL=2 at the barrier). The
      templates emit those via runStatus at fixed control-flow points (R-09),
      but the current workflow runtime has no Node API, so the calls silently
      no-op and the cards sat on "running" until the watcher's run-end settle
      pass; this daemon substitutes for the script there exactly as it does for
      loop closes — same events, same messages, derived from the same journal
      results. Suppress with --no-status.

Attempt caps come from orch-config.json: max_plan_attempts (default 4) plus the
per-plan extra_attempts map ({"sp-x": N} raises only that loop's cap), and
max_finalgate_attempts (default 2) for the sweep close — re-read every poll,
like decision_watcher re-reads its caps.

Usage:
  ORCH_STATE_DIR=<task-dir> python3 cycle_reporter.py <wf_dir> [<wf_dir>...]
      [--once] [--settle] [--final [--narrative FILE]] [--no-status]
      [--interval SECONDS]

  Both value flags take `--flag=VALUE` and `--flag VALUE`; a value flag with no
  value is a hard error, never a silent default (a `--narrative` the parser
  dropped rendered the EMPTY slot and exited 0, which is a lie about what the
  operator did).

  --once       process the journals once and exit (backfill of a finished or
               foreign run; combine with --no-status to keep history untouched)
  --settle     one-shot close-out: re-read the journals, then emit ONLY the
               closes that are implied by the recorded results and ABSENT from
               events.jsonl, and stop. The STREAM is the authority in BOTH
               directions (stream_plan_closes()): a close already on it is
               never written twice — a second run, a run after the checkpoint
               was lost, a card some other writer already closed — and a close
               the local checkpoint CLAIMS but the stream does not carry is
               written anyway, because a write that was recorded and then
               failed is exactly the loss this mode exists to repair. `touch-run
               close` calls this; it is also what network-recovery points at
               instead of hand-typed corrective events. It never invents a
               verdict: a loop that ended mid-attempt with no decisive result
               is left open for the watcher's own run-end pass (R-58).
  --final      one-shot: render the run's final report and exit. Implies
               render-only — it emits no event.
  --narrative=FILE  HTML fragment to inject into --final's narrative slot.
  --no-status  never emit status.sh events (render-only)

Checkpointed in $ORCH_STATE_DIR/.cycle-reporter-state.json (restart-safe; a
loop-close event is emitted exactly once). The checkpoint records WRITES that
landed, never intentions: a status.sh that exits non-zero leaves its close due,
the next poll retries it after asking the stream whether the failed attempt
landed anyway, and `--settle` can repair it even across a restart. Stdlib only.
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

MARKER_RE = re.compile(
    r"\[monitor\] plan=([\w.-]+) stage=([\w-]+) role=([\w:-]+) attempt=(\d+)")
STAGE_SLOT = {"implement": "impl", "test": "gate", "critique": "crit"}
#: The implement protocol's two single-agent plans (never `sp-*` loops). Their
#: markers are fixed by the template's dividePrompt/finalGatePrompt/finalFixPrompt;
#: `plan -> {stage -> slot}` mirrors STAGE_SLOT for the loops pass.
PROTOCOL_SLOT = {"divide": {"partition": "partition"},
                 "finalgate": {"sweep": "sweep", "implement": "fix"}}
#: The RESEARCH protocol's two plans (research/templates/
#: research.workflow.js), mapped to the result key that proves an agent
#: actually returned something. `research` fans out one agent per perspective
#: and its stage is the perspective key, so it cannot be a fixed stage table
#: like PROTOCOL_SLOT above — the plan id is the whole classification.
#: `synthesis` is the single fable agent that writes the plan file.
RESEARCH_PROOF = {"research": ("findings", "findings_file"),
                  "synthesis": ("plan_file",)}
#: A research run has exactly two plan cards, declared at the barrier (GD-D11;
#: monitoring.md's plans_total bullet says the same for the reference template).
RESEARCH_PLANS_TOTAL = "2"
FINDINGS_EMBED_CAP = 60_000
#: The narrative fragment `--final` injects: the slot is ONE section, not a
#: second page. Unlike FINDINGS_EMBED_CAP above — which truncates text inside a
#: `<pre>`, where a cut can only lose bytes — an over-cap fragment is refused
#: WHOLE, because it is markup and a cut inside a tag eats the rest of the page.
NARRATIVE_CAP = 64_000
#: Marks the one LLM-authored region of the final report (D-15). Rendering
#: without a fragment leaves the markers and a note between them, so the slot
#: is always findable by name in the output.
NARRATIVE_SLOT = "touch:narrative"
#: Active-content shapes refused in an injected narrative — the tags that can
#: execute, navigate or restyle the whole page, any `on*=` handler (after a
#: SPACE **or a slash**: `<img/onerror=…>` is the commonest spelling and a
#: `\s`-only class misses it by one character), and `javascript:` URIs. This is
#: a seatbelt over the shapes named HERE, not a sanitizer and not a trust
#: boundary: the fragment is authored by the run's own narrator, and anything
#: outside this list is injected verbatim. What it buys is that a report a human
#: opens off disk — or publishes, per the storage rule — cannot carry script by
#: accident.
NARRATIVE_ACTIVE_RE = re.compile(
    r"<\s*/?\s*(script|iframe|object|embed|style|form|base|link|meta)\b"
    r"|[\s/]on\w+\s*=|javascript:", re.I)
#: The roster's entry in the emitted-once checkpoint. `@` is outside MARKER_RE's
#: plan class ([\w.-]+), so it can never collide with a real plan id.
ROSTER_SENTINEL = "@roster"
#: The writer's own roster bounds (`status.sh`: ROSTER_MAX / ROSTER_ENTRY_CAP),
#: mirrored so the file this daemon writes IS the array that lands on the
#: stream — which is what lets settle() compare the two instead of guessing
#: from a boolean. status.sh stays the ENFORCER: a roster reaching it from
#: anywhere else is still bounded there.
ROSTER_MAX = 200
ROSTER_ENTRY_CAP = 300
#: The seed variables `status.sh` folds into every line it writes. This daemon
#: declares them per call or not at all, and NEVER inherits them from the shell
#: that started it — decision_watcher.STATUS_ENV_DROP and touch-run's emit()
#: drop the same three, for the reason touch-run's comment names: an inherited
#: ORCH_ROSTER attaches a foreign roster to every later line, an inherited
#: ORCH_TITLE renames every card this daemon closes, and an inherited
#: ORCH_PLANS_TOTAL is folded MONOTONIC-MAX by readers — irreversible on that
#: stream and on every replay of it. The output of a deterministic emitter is a
#: pure function of recorded data; these three are not recorded data.
STATUS_ENV_DROP = ("ORCH_TITLE", "ORCH_PLANS_TOTAL", "ORCH_ROSTER")
#: Where session dirs live: <root>/<project-slug>/<session-id>/subagents/…
#: The same override knob decision_watcher.py uses (tests point it at a
#: fixture tree; a foreign layout can too).
WF_GLOB_ROOT = os.environ.get(
    "ORCH_WF_GLOB_ROOT", os.path.expanduser("~/.claude/projects"))

CYCLE_CSS = """
  :root { --ink:#1a1a19; --muted:#6b6b68; --surface:#fff; --card:#f6f6f4; --line:#d8d8d4;
          --good:#0ca30c; --bad:#d03b3b; --warn:#b26a00; }
  @media (prefers-color-scheme: dark) {
    :root { --ink:#ececea; --muted:#9c9c98; --surface:#1a1a19; --card:#242422; --line:#3a3a37;
            --warn:#fab219; } }
  body { font: 14px/1.5 system-ui, sans-serif; color: var(--ink); background: var(--surface);
         margin: 0 auto; max-width: 60rem; padding: 1.5rem; }
  h1 { font-size: 1.15rem; } h2 { font-size: 1rem; margin-top: 1.6rem; }
  .flow { display: flex; align-items: stretch; gap: .4rem; flex-wrap: wrap; margin: 1rem 0; }
  .node { flex: 1 1 11rem; border: 2px solid var(--line); border-radius: .5rem;
          padding: .6rem .7rem; background: var(--card); min-width: 11rem; }
  .node .role { font-size: .75rem; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
  .node .verdict { font-weight: 700; margin: .15rem 0; }
  .node .sum { color: var(--muted); font-size: .85rem; overflow-wrap: anywhere; }
  .arrow { align-self: center; color: var(--muted); font-size: 1.2rem; }
  .why { border-left: 4px solid var(--line); padding: .4rem .9rem; background: var(--card);
         border-radius: 0 .5rem .5rem 0; }
  .why.pass { border-left-color: var(--good); } .why.fail { border-left-color: var(--bad); }
  .chip { display: inline-block; border-radius: 1rem; padding: 0 .6rem; font-size: .8rem;
          font-weight: 700; border: 1px solid var(--line); text-decoration: none; }
  details { margin: .6rem 0; } summary { cursor: pointer; }
  pre { background: var(--card); padding: .8rem; border-radius: .5rem; overflow-x: auto;
        white-space: pre-wrap; overflow-wrap: anywhere; font-size: .8rem; }
  table { border-collapse: collapse; width: 100%; }
  td, th { border: 1px solid var(--line); padding: .35rem .6rem; text-align: left; }
  a { color: inherit; }
  .foot { color: var(--muted); font-size: .8rem; margin-top: 1.5rem; }
"""


FINAL_CSS = """
  .tiles { display: flex; flex-wrap: wrap; gap: .5rem; margin: 1rem 0; }
  .tile { flex: 1 1 8rem; background: var(--card); border: 1px solid var(--line);
          border-radius: .5rem; padding: .6rem .7rem; }
  .tile .k { font-size: .72rem; text-transform: uppercase; letter-spacing: .05em;
             color: var(--muted); }
  .tile .v { font-size: 1.35rem; font-weight: 700; overflow-wrap: anywhere; }
  .card { border: 1px solid var(--line); border-radius: .5rem; padding: .2rem .9rem .7rem;
          margin: .8rem 0; }
  .card h2 { margin-top: .8rem; } .card h3 { font-size: .85rem; margin: .8rem 0 .2rem; }
"""
#: The four token fields, disjoint by law (GD-M2 / GD-11): `in` is fresh input
#: only, and `cached` / `cache_write` are NOT parts of it. Summing all four is
#: therefore a real total, not a double count.
TOKEN_KEYS = ("in", "out", "cached", "cache_write")


def esc(s):
    return (str("" if s is None else s)
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def as_int(v):
    """`v` as a non-negative int, or 0 — event data is untrusted like any input."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def parse_ts(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def span(first, last):
    """`"12m 30s"` between two ISO stamps, or `"—"` when either is missing."""
    a, b = parse_ts(first), parse_ts(last)
    if a is None or b is None:
        return "—"
    secs = int(max(0.0, (b - a).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"


def fmt_tokens(tok):
    return (f"{tok['in']:,} in / {tok['out']:,} out"
            f" <span class=\"sum\">(r:{tok['cached']:,} w:{tok['cache_write']:,})</span>")


def tile(key, value):
    return (f'<div class="tile"><div class="k">{esc(key)}</div>'
            f'<div class="v">{esc(value)}</div></div>')


def badge_color(state):
    return {"done": "var(--good)", "failed": "var(--bad)",
            "stale": "var(--warn)"}.get(state, "var(--muted)")


def rel_link(from_dir, path):
    """A link from the report directory to `path`, relative where it can be.

    `os.path.relpath` answers for any two paths on the same root, including a
    walk up out of the task folder — which is where the plan and findings files
    of a real run actually live. On a different drive it raises; the absolute
    path is the honest fallback, not an error.
    """
    try:
        return os.path.relpath(path, from_dir)
    except ValueError:
        return path


def verdict_of(res):
    """`(word, summary)` for any result shape either protocol returns.

    One table, ordered by how specific the shape is; every reference template's
    schema is covered and anything else reads as a plain return rather than an
    invented verdict.
    """
    summary = res.get("summary") or ""
    if "passed" in res:
        return ("PASS" if res.get("passed") else "FAIL"), summary
    if "approved" in res:
        return ("APPROVED" if res.get("approved") else "REJECTED"), summary
    if "subplans" in res:
        subs = res.get("subplans")
        n = len(subs) if isinstance(subs, list) else 0
        return f"{n} SUB-PLANS", summary
    if "plan_file" in res:
        items = res.get("item_count")
        return ("PLAN WRITTEN" if not isinstance(items, int)
                else f"PLAN WRITTEN ({items} items)"), summary
    if "findings" in res:
        f = res.get("findings")
        return f"{len(f) if isinstance(f, list) else 0} FINDINGS", summary
    if "done" in res:
        return ("DONE" if res.get("done") is not False else "INCOMPLETE"), summary
    return "RETURNED", summary


class Reporter:
    def __init__(self, task_dir, wf_dirs, emit_status=True):
        self.task = task_dir
        self.wf_dirs = wf_dirs
        self.emit_status = emit_status
        self.report_dir = os.path.join(task_dir, "report", "cycles")
        self.state_path = os.path.join(task_dir, ".cycle-reporter-state.json")
        self.status_sh = self._find_status_sh()
        # cycles[plan][attempt] = {"impl": r, "gate": r, "crit": r}
        self.cycles = {}
        # protocol[plan][attempt] = {"partition"|"sweep"|"fix": r} — the two
        # single-agent plans of PROTOCOL_SLOT; closed by their own pass, never
        # rendered as cycle pages (they have no impl->test->critique flow).
        self.protocol = {}
        # research[plan] = {"started": {agentId}, "returned": [result]} — the
        # research protocol's two plans; same treatment, different close rule.
        self.research = {}
        # Every correlated journal record, in journal order: the ONE
        # journal -> (plan, stage, role, attempt, result) fold in this file
        # (GD-D10). The routers below and the final report all read THIS, so
        # adding a consumer never adds a second correlation.
        self.records = []
        # [(id, title)] from the divider's partition — the roster, emitted once
        # on the orchestrator card at the divide close (GD-D11).
        self.roster = []
        self.plan_order = []          # first-seen order == execution order (serial)
        self.closed = {}              # plan -> {"state": .., "cls": .., "attempt": n}
        self.marker_cache = {}        # agentId -> marker tuple; HITS only
        self.pending = []             # result records whose marker is unresolved YET
        # offsets are IN-MEMORY only: every start re-reads the journals from
        # zero and rebuilds the full picture (rendering is idempotent; journals
        # are small). Only `emitted` persists — a loop-close status event must
        # fire exactly once across restarts.
        self.offsets = {}             # wf_dir -> consumed bytes of journal.jsonl
        # Plans (and ROSTER_SENTINEL) whose event REACHED the stream. Writes,
        # never intentions: a status.sh that exited non-zero leaves its plan
        # due, so the next poll retries it and settle() can still repair it.
        self.emitted = set()
        # ...and the plans no writer exists for: --no-status by policy, or a
        # payload with no status.sh. In memory only, deliberately NOT `emitted`,
        # because a checkpoint that claims an unwritten close is the one lie
        # settle() cannot see past. It exists solely to keep the poll loop from
        # re-rendering for a card nothing will ever emit.
        self.unwritable = set()
        # ...and the ones whose write was ATTEMPTED and failed. Those stay due,
        # and the RETRY reads the stream before it writes: a status.sh that
        # appended and then blew its 30 s timeout is indistinguishable from one
        # that wrote nothing, so this is what keeps self-healing from turning
        # into doubling. In memory only; a restart re-derives it from the stream.
        self.write_failed = set()
        self._load_state()

    def _find_status_sh(self):
        """The plugin's OWN status.sh — one candidate, deliberately.

        There used to be a first candidate derived from `self.task`
        (`$ORCH_STATE_DIR/../../shared/monitoring/status.sh` — a monitoring
        copy inside the reported-on project's own dot-claude directory). It is
        deleted, not merely reordered: the reporter feeds this path to `bash`
        below, so that rung executed a PROJECT-controlled script with the
        plugin's authority — and Touch's docs used to tell people to keep
        exactly such a file. Where one existed it also shadowed the payload
        copy, so drift between the two was invisible during development and
        would surface only on a stranger's machine. The payload copy IS the
        canonical monitoring module; a project-side copy has no legitimate
        resolution target anywhere.

        `templates/` -> `implement/` -> `skills/` -> the plugin root, and
        `bin/touch-cycle-reporter` guarantees `here` is that templates
        directory, so this resolves under every install.

        The "payload incomplete" warning is deliberately gated on
        `self.emit_status`: under `--no-status` the reporter never spawns
        status.sh at all, so its absence is not an error for that mode. Do not
        ungate it — the repo's own test suite pins the quiet path.
        """
        # realpath, not abspath: it resolves symlinks, so this agrees with
        # `bin/touch-cycle-reporter`'s `readlink -f "$0"`. With no fallback
        # rung left, a link on the way in must not cost the reporter its
        # status.sh.
        here = os.path.dirname(os.path.realpath(__file__))
        cand = os.path.normpath(
            os.path.join(here, "..", "..", "..", "shared", "monitoring", "status.sh"))
        if os.path.isfile(cand):
            return cand
        if self.emit_status:
            print(f"status.sh not found at {cand}; payload incomplete — "
                  "no loop-close events will be emitted", file=sys.stderr)
        return None

    # -- checkpoint ---------------------------------------------------------
    def _load_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                st = json.load(f)
            self.emitted = set(st.get("emitted", []))
        except (OSError, ValueError):
            pass

    def _save_state(self):
        # Render-only modes keep history untouched (the module docstring says so
        # of `--once --no-status`, and `--final` is render-only by construction):
        # the checkpoint IS history — it is the record of which closes have
        # already fired — so a backfill that emits nothing must not be able to
        # mark a close, or the roster, emitted and cost a later live daemon its
        # event. Belt and braces since `unwritable`: nothing that could not be
        # written enters `emitted` in the first place, and this guard keeps the
        # file untouched even so.
        if not self.emit_status:
            return
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"emitted": sorted(self.emitted)}, f)
        os.replace(tmp, self.state_path)

    # -- config -------------------------------------------------------------
    def caps(self):
        base, extra = 4, {}
        try:
            with open(os.path.join(self.task, "orch-config.json"), encoding="utf-8") as f:
                cfg = json.load(f)
            base = int(cfg.get("max_plan_attempts", 4))
            extra = cfg.get("extra_attempts", {}) or {}
        except (OSError, ValueError):
            pass
        return lambda plan: base + int(extra.get(plan, 0) or 0)

    # -- journal ingestion --------------------------------------------------
    def _run_dirs(self):
        """Every session dir carrying this run's transcripts, argv dirs first.

        The harness keys the transcript dir to the ACTIVE session id, and
        /clear or /compact rotates that id mid-run while a background workflow
        keeps going — so one run's transcripts scatter across sibling session
        dirs named ``…/<session-id>/subagents/workflows/<run-name>/`` while
        ``journal.jsonl`` stays at its launch-time path. decision_watcher.py
        rides the same glob (``agent_paths``); the reporter that searched only
        its argv dirs sat wedged for hours on a real run — every post-/clear
        result was markerless, so no page rendered and no loop close fired
        after the rotation. Re-globbed every pass: a /clear can add a dir at
        any moment.
        """
        dirs = list(self.wf_dirs)
        for wf in self.wf_dirs:
            run = os.path.basename(os.path.normpath(wf))
            pat = os.path.join(WF_GLOB_ROOT, "*", "*",
                               "subagents", "workflows", run)
            for d in sorted(glob.glob(pat)):
                if d not in dirs:
                    dirs.append(d)
        return dirs

    def marker(self, dirs, agent_id):
        m = self.marker_cache.get(agent_id)
        if m is not None:
            return m
        for d in dirs:
            path = os.path.join(d, f"agent-{agent_id}.jsonl")
            try:
                with open(path, "rb") as f:
                    head = f.read(262_144).decode("utf-8", "replace")
            except OSError:
                continue
            hit = MARKER_RE.search(head)
            if hit:
                m = (hit.group(1), hit.group(2), hit.group(3), int(hit.group(4)))
                # Cache HITS only. A miss may just be a transcript that has
                # not landed yet — or one sitting in a session dir a later
                # pass will see; caching the None is what made the wedge above
                # permanent instead of transient.
                self.marker_cache[agent_id] = m
                return m
        return None

    def correlate(self, rec, dirs):
        """One journal record -> (plan, stage, role, attempt, kind, result).

        THE journal->marker correlation of this file, and deliberately the only
        one (GD-D10): the loops router, the two single-agent protocol routers,
        the research router and the final report are all consumers of the
        ``self.records`` list this fills, so a new derived value never buys a
        fourth fold. ``kind`` is the journal record type (``started`` /
        ``result``); ``result`` is None for anything but a result.

        Returns None when the agent's [monitor] marker has not landed yet —
        that is a "not yet", never a "not ours".
        """
        mark = self.marker(dirs, rec.get("agentId", ""))
        if not mark:
            return None
        plan, stage, role, attempt = mark
        return (plan, stage, role, attempt, rec.get("type"), rec.get("result"))

    def _apply(self, rec, dirs):
        """Route one journal record into cycles/protocol/research.

        Returns False when the record's [monitor] marker cannot be resolved
        YET — the caller parks it in ``pending`` and retries next pass.
        Everything else (routed, or resolvable-but-not-ours) is consumed:
        True. State changes flag ``self._dirty`` directly.
        """
        corr = self.correlate(rec, dirs)
        if corr is None:
            return False
        plan, stage, _role, attempt, kind, res = corr
        self.records.append(
            {"plan": plan, "stage": stage, "role": _role, "attempt": attempt,
             "kind": kind, "result": res, "agentId": rec.get("agentId", "")})
        if plan in RESEARCH_PROOF:
            info = self.research.setdefault(
                plan, {"started": set(), "returned": []})
            if kind == "started":
                info["started"].add(rec.get("agentId", ""))
            elif isinstance(res, dict) and any(
                    res.get(k) for k in RESEARCH_PROOF[plan]):
                info["returned"].append(res)
            self._dirty = True
            return True
        if plan in PROTOCOL_SLOT:
            pslot = PROTOCOL_SLOT[plan].get(stage)
            if pslot is not None and isinstance(res, dict):
                self.protocol.setdefault(plan, {}) \
                    .setdefault(attempt, {})[pslot] = res
                self._dirty = True
            return True
        slot = STAGE_SLOT.get(stage)
        if slot is None or not plan.startswith("sp-"):
            return True
        if not isinstance(res, dict):
            return True
        per = self.cycles.setdefault(plan, {})
        if plan not in self.plan_order:
            self.plan_order.append(plan)
        # journal order: a retry's result simply overwrites its attempt slot
        per.setdefault(attempt, {})[slot] = res
        self._dirty = True
        return True

    def ingest(self):
        """Consume new journal lines + retry pending; True if anything changed."""
        self._dirty = False
        dirs = self._run_dirs()
        # Parked records first (oldest first): their transcripts may have
        # appeared since, possibly in a session dir the last pass had no way
        # to see.
        if self.pending:
            self.pending = [r for r in self.pending if not self._apply(r, dirs)]
        for wf in self.wf_dirs:
            jp = os.path.join(wf, "journal.jsonl")
            try:
                size = os.path.getsize(jp)
            except OSError:
                continue
            off = int(self.offsets.get(wf, 0))
            if size <= off:
                continue
            with open(jp, "rb") as f:
                f.seek(off)
                chunk = f.read(size - off)
            # only complete lines; leave a partial tail for the next pass
            end = chunk.rfind(b"\n")
            if end < 0:
                continue
            self.offsets[wf] = off + end + 1
            for raw in chunk[:end + 1].splitlines():
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                # `started` matters as well as `result` now: "zero returns" is
                # only a fact about a plan that HAS spawned agents, and the
                # research close is the one rule in this file that has to know
                # the difference between "nobody came back" and "nobody went".
                if rec.get("type") not in ("result", "started"):
                    continue
                if not self._apply(rec, dirs):
                    self.pending.append(rec)
        return self._dirty

    # -- loop close ---------------------------------------------------------
    def classify(self, crit):
        if crit and crit.get("critical_defect"):
            return "critical-stop"
        if crit and crit.get("depth") == "needs-own-flow":
            return "needs-own-flow"
        return "retryable"

    def evaluate_closes(self, cap_of):
        for plan in self.plan_order:
            if plan in self.closed:
                continue
            per = self.cycles.get(plan, {})
            cap = cap_of(plan)
            for attempt in sorted(per):
                cy = per[attempt]
                crit, gate, impl = cy.get("crit"), cy.get("gate"), cy.get("impl")
                if crit and crit.get("approved") and gate and gate.get("passed"):
                    self.closed[plan] = {"state": "done", "cls": "green", "attempt": attempt, "cap": cap}
                    break
                # a cycle is COMPLETE on a critique verdict, or on an implementer
                # that returned done=false (the loop skips its gates)
                complete = crit is not None or (impl is not None and impl.get("done") is False)
                if complete and attempt >= cap:
                    cls = self.classify(crit)
                    self.closed[plan] = {"state": "failed", "cls": cls, "attempt": attempt, "cap": cap}
                    break

    def evaluate_protocol_closes(self):
        """Close the two single-agent protocol plans the loops pass cannot see.

        The template closes `divide` and `finalgate` itself via runStatus at
        fixed control-flow points (R-09), but the current workflow runtime has
        no Node API, so those calls no-op and the cards sat on "running" until
        the watcher's run-end settle pass. This pass substitutes for the
        script: same states, same messages, same ORCH_PLANS_TOTAL declaration,
        derived from the same structured results the script branched on.
        """
        if "divide" not in self.closed:
            for attempt in sorted(self.protocol.get("divide", {})):
                res = self.protocol["divide"][attempt].get("partition")
                if res is None:
                    continue
                subs = res.get("subplans")
                if not isinstance(subs, list) or not subs:
                    self.closed["divide"] = {
                        "state": "failed", "msg": "divider produced no sub-plans"}
                    break
                # the template's deterministic isolation guard, mirrored
                owner, dup = {}, None
                for sp in subs:
                    for f in (sp.get("files") or []):
                        if f in owner:
                            dup = f
                            break
                        owner[f] = sp.get("id")
                    if dup:
                        break
                if dup:
                    self.closed["divide"] = {
                        "state": "failed",
                        "msg": f"partition not isolated: {dup} has two owners"}
                else:
                    # divide + N sub-plans + finalgate: the template's own
                    # plans_total declaration, folded monotonically by readers
                    self.closed["divide"] = {
                        "state": "done", "msg": f"{len(subs)} sub-plans",
                        "env": {"ORCH_PLANS_TOTAL": str(len(subs) + 2)}}
                    # …and the roster the dashboard has been able to read since
                    # long before anything wrote one (GD-D11). The DIVIDER's
                    # partition is the authoritative list — `touch-run start`
                    # seeded whatever the run spec guessed, and this is what the
                    # run actually has.
                    self.roster = [(sp.get("id") or "", sp.get("title") or "")
                                   for sp in subs if sp.get("id")]
                break
        if "finalgate" not in self.closed:
            cap = self._finalgate_cap()
            for attempt in sorted(self.protocol.get("finalgate", {})):
                cy = self.protocol["finalgate"][attempt]
                sweep = cy.get("sweep")
                if sweep is None:
                    continue
                if sweep.get("passed"):
                    self.closed["finalgate"] = {
                        "state": "done", "msg": "aggregate sweep green"}
                    break
                fix = cy.get("fix")
                # past the cap the template stops unconditionally; below it,
                # only a fixer that returned done=false ends the retry loop (a
                # dead fixer is indistinguishable from a running one here, and
                # that rare close is the driver's run-end backstop anyway)
                if attempt >= cap or (fix is not None and fix.get("done") is False):
                    self.closed["finalgate"] = {
                        "state": "failed",
                        "msg": f"sweep not green after {cap} attempts"}
                    break

    def evaluate_research_closes(self, final=False):
        """Close the research protocol's two plans (D-14).

        `research` fans out one read-only agent per perspective and stops at a
        barrier; `synthesis` is the one fable agent that writes the plan file.
        Neither has an impl->test->critique cycle, so the loops pass cannot see
        them at all, and the template cannot close them itself for the same
        R-09 reason `divide`/`finalgate` cannot.

        The rule, and it is R-58's rule: a plan closes **done** on the first
        result that actually carries what that plan produces (`findings` /
        `findings_file` for research, `plan_file` for synthesis), and **failed
        only on ZERO returns**. Zero returns is a terminal fact, so the failed
        arm fires only under ``final=True`` — i.e. from ``--settle``, after the
        run is over. A live pass that has seen agents start and nothing come
        back yet says nothing, because "not yet" and "never" are the same
        picture until someone declares the run finished.

        A PARTIAL board is deliberately NOT a failure here: research.workflow.js
        refuses to synthesize from one and throws, and the honest close for that
        stop is the watcher's layered run close (D-07), not a `failed` badge on
        a card whose agents did return. That reading is stated in the template's
        own comment; MIN_REPORTS is carried, unimplemented, on purpose.
        """
        for plan in RESEARCH_PROOF:
            if plan in self.closed:
                continue
            info = self.research.get(plan)
            if not info:
                continue
            returned, spawned = info["returned"], len(info["started"])
            env = {"ORCH_PLANS_TOTAL": RESEARCH_PLANS_TOTAL}
            if returned:
                if plan == "research":
                    # Worded for what it asserts AT THIS INSTANT, because the
                    # card closes on the FIRST report and the detail is then
                    # terminal: researchers return minutes apart, so a bare
                    # "1 of 6 returned" would be the permanent badge of a run
                    # where all six came back. D-14 asks for truthful wording,
                    # and a count pinned to the moment it was taken stays true.
                    msg = (f"{len(returned)} of {max(spawned, len(returned))} "
                           "research reports in when the card closed")
                else:
                    items = returned[0].get("item_count")
                    msg = ("plan written" if not isinstance(items, int)
                           else f"plan written: {items} items")
                self.closed[plan] = {"state": "done", "msg": msg, "env": env}
            elif final and spawned:
                what = ("research report" if plan == "research"
                        else "synthesized plan")
                self.closed[plan] = {
                    "state": "failed", "env": env,
                    "msg": f"no {what} returned (0 of {spawned} spawned)"}

    def _finalgate_cap(self):
        try:
            with open(os.path.join(self.task, "orch-config.json"), encoding="utf-8") as f:
                return int(json.load(f).get("max_finalgate_attempts", 2))
        except (OSError, ValueError):
            return 2

    def emit_event(self, plan, stage, state, msg, env=None):
        """One event, THROUGH status.sh — the only write path (GD-D5).

        `status.sh` owns the flock, the 1 KB detail cap, the `w:"agent"`
        attribution and the roster bounds; nothing here writes JSON into
        events.jsonl, because the only two ledgerless roster lines that ever
        reached disk were hand-appended raw JSON and that is exactly the
        failure `w` exists to expose (MONITORING-5).

        Every seed variable is declared per call or DROPPED (STATUS_ENV_DROP);
        the caller's `env` is the only source of one. Returns True when the
        writer actually appended a line, so a caller can report what reached
        the stream rather than what it merely implied.
        """
        base = {k: v for k, v in os.environ.items() if k not in STATUS_ENV_DROP}
        try:
            # self.status_sh is always the payload's own script (see
            # _find_status_sh) — never a path the reported-on project supplies.
            r = subprocess.run(
                ["bash", self.status_sh, plan, stage, state, msg],
                env={**base, "ORCH_STATE_DIR": self.task, **(env or {})},
                capture_output=True, text=True, timeout=30)
            warn = (r.stderr or "").strip()
            if warn:
                print(f"status.sh warned on {plan}: {warn.splitlines()[0]}", file=sys.stderr)
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError) as e:
            print(f"could not emit {plan}/{stage}/{state}: {e}", file=sys.stderr)
            return False

    def _due(self, key):
        """True while `key`'s event is still owed — nothing wrote it, and
        nothing in this process CAN write it yet."""
        return key not in self.emitted and key not in self.unwritable

    def emit_close(self, plan):
        """The plan's terminal event; True only when one reached the stream.

        A failed write leaves the plan due on purpose. `emitted` used to be set
        regardless, so one bad poll — a 30 s flock timeout, a status.sh whose
        own mkdir failed — dropped the close for the rest of the run AND wrote
        that claim into the checkpoint, where `--settle` then believed it. That
        is the MONITORING-7 symptom D-14 exists to remove, so it is recorded
        only on success; the two cases where no writer exists at all go to
        `unwritable`, which quiets the loop without claiming anything.
        """
        info = self.closed[plan]
        if plan in self.emitted:
            return False
        if not self.emit_status or not self.status_sh:
            self.unwritable.add(plan)
            return False
        if plan in self.write_failed and plan in self.stream_plan_closes():
            # A retry whose FIRST attempt landed after all — the writer
            # appended and then failed or timed out. Only the retry path pays
            # for this read; the ordinary close never touches events.jsonl.
            self.emitted.add(plan)
            return False
        if "msg" in info:
            msg = info["msg"]
        elif info["state"] == "done":
            msg = f"green on attempt {info['attempt']}/{info['cap']}"
        else:
            msg = f"attempts exhausted {info['attempt']}/{info['cap']} ({info['cls']})"
        wrote = self.emit_event(plan, "plan", info["state"], msg, info.get("env"))
        (self.emitted if wrote else self.write_failed).add(plan)
        return wrote

    def roster_entries(self):
        """The exact `roster` array an event of this reporter's would carry.

        One derivation for the file that is written and for the comparison
        settle() makes against the stream — bounded to the writer's own numbers
        so those two are the same list, not two lists that usually agree.
        """
        out = []
        for plan_id, title in self.roster:
            entry = f"{plan_id} — {title}" if title else plan_id
            out.append(" ".join(entry.split())[:ROSTER_ENTRY_CAP])
        return out[:ROSTER_MAX]

    def emit_roster(self):
        """The declared sub-plan roster, once, on the orchestrator card (GD-D11).

        Two things are deliberate. It rides its own event rather than the
        `divide` close, because readers honor `roster` only on the reserved
        `orchestrator` card and a roster on a `divide`-card line would be
        silently dropped. And it travels as a FILE PATH in `ORCH_ROSTER`, never
        as env-inlined JSON: the file is written here, the writer bounds it.
        `roster.txt` is the same file `touch-run start` seeds from the run
        spec — one roster per task, latest wins, and the divider's partition is
        the later and truer of the two.

        True only when a roster line actually reached the stream — and the
        sentinel is checkpointed only then, for the reason emit_close names.
        """
        if not self.roster or ROSTER_SENTINEL in self.emitted:
            return False
        if not self.emit_status or not self.status_sh:
            self.unwritable.add(ROSTER_SENTINEL)
            return False
        entries = self.roster_entries()
        if (ROSTER_SENTINEL in self.write_failed
                and self.scan_stream()[1] == entries):
            # The same retry guard emit_close uses, in the roster's own terms:
            # the stream already ends with THIS list, so the failed write
            # landed and repeating it would only add a duplicate.
            self.emitted.add(ROSTER_SENTINEL)
            return False
        path = os.path.join(self.task, "roster.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(entry + "\n")
        except OSError as e:
            print(f"could not write the roster file: {e}", file=sys.stderr)
            return False
        env = {"ORCH_ROSTER": path}
        total = (self.closed.get("divide") or {}).get("env") or {}
        env.update(total)
        wrote = self.emit_event("orchestrator", "divide", "info",
                                f"roster: {len(self.roster)} sub-plans", env)
        (self.emitted if wrote else self.write_failed).add(ROSTER_SENTINEL)
        return wrote

    # -- the stream itself --------------------------------------------------
    def scan_stream(self):
        """`(plans already closed on the stream, the LAST roster on it)`.

        Read straight off events.jsonl, so it answers for EVERY writer — this
        daemon, the watcher, `touch-run`, a human — not just for what this
        process remembers. That is what makes `--settle` idempotent in the way
        that matters: the local checkpoint can be lost, or belong to a previous
        run of the reporter, and a close that is already on the stream must
        still never be written twice.

        The roster is returned as the ARRAY, not as "some roster exists":
        `touch-run start` seeds one from the run SPEC, and a boolean let that
        guess suppress the divider's authoritative partition on the one path —
        recovery close-out — where `--settle` is the only writer left. Latest
        wins on the wire, so the last one read is the one to compare against.
        """
        closes, roster = set(), []
        try:
            with open(os.path.join(self.task, "events.jsonl"),
                      encoding="utf-8", errors="replace") as f:
                for raw in f:
                    try:
                        ev = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    plan = ev.get("plan")
                    if not isinstance(plan, str) or not plan:
                        continue
                    # `plan` sets a card's badge; `complete` is its alias on the
                    # orchestrator card (monitoring.md, reserved stages).
                    if (ev.get("stage") in ("plan", "complete")
                            and ev.get("state") in ("done", "failed")):
                        closes.add(plan)
                    if isinstance(ev.get("roster"), list) and ev["roster"]:
                        roster = [str(e) for e in ev["roster"]]
        except OSError:
            pass
        return closes, roster

    def stream_plan_closes(self):
        """The plan ids events.jsonl already carries a terminal close for."""
        return self.scan_stream()[0]

    # -- rendering ----------------------------------------------------------
    def _node(self, role, ok, word, summary):
        color = "var(--muted)" if ok is None else ("var(--good)" if ok else "var(--bad)")
        glyph = "○" if ok is None else ("✓" if ok else "✗")
        return (f'<div class="node" style="border-color:{color}"><div class="role">{esc(role)}</div>'
                f'<div class="verdict" style="color:{color}">{glyph} {esc(word)}</div>'
                f'<div class="sum">{esc(summary or "")}</div></div>')

    def _findings(self, label, path):
        if not path:
            return ""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                body = f.read(FINDINGS_EMBED_CAP + 1)
        except OSError:
            return f'<p class="sum">{esc(label)}: findings file not readable ({esc(path)})</p>'
        if len(body) > FINDINGS_EMBED_CAP:
            body = body[:FINDINGS_EMBED_CAP] + "\n… (truncated)"
        name = os.path.basename(path)
        return (f"<details><summary>{esc(label)} — {esc(name)}</summary>"
                f"<pre>{esc(body)}</pre></details>")

    def render_page(self, plan, attempt, cap):
        per = self.cycles[plan]
        cy = per[attempt]
        impl, gate, crit = cy.get("impl"), cy.get("gate"), cy.get("crit")
        closed = self.closed.get(plan)
        latest_open = (closed is None and attempt == max(per))
        success = bool(gate and gate.get("passed") and crit and crit.get("approved"))

        def pending(word):
            return "PENDING" if latest_open else word

        why = []
        if impl is not None:
            inc = " (incomplete)" if impl.get("done") is False else ""
            why.append(f"Implementer{inc}: {impl.get('summary', '')}")
        else:
            why.append("Implementer result missing (agent died / cut off, or still running).")
        if gate is not None:
            why.append(f"Test gate {'PASSED' if gate.get('passed') else 'FAILED'}: {gate.get('summary', '')}")
        elif impl is not None:
            why.append("Test gate has not reported." if latest_open else "Test gate did not run.")
        if crit is not None:
            why.append(f"Critique {'APPROVED' if crit.get('approved') else 'REJECTED'}: {crit.get('summary', '')}")
        elif gate is not None:
            why.append("Critique has not reported." if latest_open else "Critique did not run.")

        chips = []
        if crit and crit.get("depth") == "needs-own-flow":
            chips.append('<span class="chip" style="color:var(--warn);border-color:var(--warn)">'
                         "⚠ needs its own research→implement flow</span>")
        if crit and crit.get("critical_defect"):
            chips.append('<span class="chip" style="color:var(--bad);border-color:var(--bad)">'
                         "✗ critical defect — user decision needed</span>")

        verdict_ok = True if success else (None if latest_open and crit is None else False)
        verdict_word = ("GREEN" if success else
                        pending("FAILED") if latest_open and crit is None else "FAILED")
        verdict_sum = ("loop closes green" if success
                       else "cycle in progress" if latest_open and crit is None
                       else (f"next: attempt {attempt + 1}/{cap}" if attempt < cap else "attempts exhausted"))

        next_steps = ""
        if crit and crit.get("critical_defect") and crit.get("next_steps"):
            next_steps = (f"<p><strong>Next steps (user decision):</strong> "
                          f"{esc(crit.get('next_steps'))}</p>")

        html = f"""<title>{esc(plan)} — cycle {attempt}</title><style>{CYCLE_CSS}</style>
<h1>{esc(plan)} — implement→test→critique, attempt {attempt}/{cap}</h1>
<div class="flow">
{self._node('implement',
            (impl.get('done') is not False) if impl is not None else (None if latest_open else False),
            ('DONE' if impl.get('done') is not False else 'INCOMPLETE') if impl is not None
            else pending('DIED'), impl.get('summary') if impl else None)}
<div class="arrow">→</div>
{self._node('test gate', bool(gate.get('passed')) if gate is not None else None,
            ('PASS' if gate.get('passed') else 'FAIL') if gate is not None else pending('NOT RUN'),
            gate.get('summary') if gate else None)}
<div class="arrow">→</div>
{self._node('critique', bool(crit.get('approved')) if crit is not None else None,
            ('APPROVED' if crit.get('approved') else 'REJECTED') if crit is not None else pending('NOT RUN'),
            crit.get('summary') if crit else None)}
<div class="arrow">→</div>
{self._node('cycle verdict', verdict_ok, verdict_word, verdict_sum)}
</div>
{('<p>' + ' '.join(chips) + '</p>') if chips else ''}
<h2>Why this cycle {'succeeded' if success else 'failed' if not latest_open or crit is not None else 'stands'}</h2>
<div class="why {'pass' if success else 'fail'}"><ul>{''.join(f'<li>{esc(w)}</li>' for w in why)}</ul>
{next_steps}</div>
<h2>Full findings (the evidence)</h2>
{self._findings('Test gate findings', gate.get('findings_file') if gate else None)}
{self._findings('Critique findings', crit.get('findings_file') if crit else None)}
<p class="sum"><a href="index.html">← run overview</a></p>
<p class="foot">rendered deterministically by cycle_reporter.py at {utcnow()}</p>
"""
        path = os.path.join(self.report_dir, f"{plan}-cycle-{attempt}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)

    def render_index(self, cap_of):
        word = {"green": "✓ GREEN", "retryable": "✗ FAILED (attempts exhausted)",
                "needs-own-flow": "⚠ FAILED — needs its own research→implement flow",
                "critical-stop": "✗ CRITICAL — run stopped for user decision",
                "open": "… running"}
        color = {"green": "var(--good)", "retryable": "var(--bad)",
                 "needs-own-flow": "var(--warn)", "critical-stop": "var(--bad)",
                 "open": "var(--muted)"}
        rows = []
        for plan in self.plan_order:
            per = self.cycles[plan]
            closed = self.closed.get(plan)
            st = closed["cls"] if closed else "open"
            chips = " ".join(
                f'<a class="chip" style="color:{"var(--good)" if (c.get("gate", {}) or {}).get("passed") and (c.get("crit", {}) or {}).get("approved") else "var(--bad)"}" '
                f'href="{esc(plan)}-cycle-{a}.html">'
                f'{"✓" if (c.get("gate", {}) or {}).get("passed") and (c.get("crit", {}) or {}).get("approved") else "✗"} a{a}</a>'
                for a, c in sorted(per.items()))
            rows.append(f"<tr><td>{esc(plan)}</td><td>{chips}</td>"
                        f'<td style="color:{color[st]};font-weight:700">{word[st]}</td></tr>')
        html = f"""<title>implement run — cycle overview</title><style>{CYCLE_CSS}</style>
<h1>Gated loops — every implement→test→critique cycle</h1>
<p class="sum">One page per cycle; chips link to them. Execution order, top to bottom.</p>
<table><tr><th>sub-plan</th><th>cycles</th><th>loop status</th></tr>
{os.linesep.join(rows)}</table>
<p class="foot">rendered deterministically by cycle_reporter.py at {utcnow()}</p>
"""
        with open(os.path.join(self.report_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)

    # -- the final report (D-15) --------------------------------------------
    # Three sources, named on the page itself, and each number comes from
    # exactly ONE of them:
    #   journal.jsonl + the [monitor] marker -> plans, attempts, verdicts
    #   events.jsonl                         -> timeline, badges, tokens
    #   <runId>.json (the run snapshot)      -> the harness's own cross-check
    # The page carries no render timestamp, so the same inputs render the same
    # bytes; the ONE region an LLM writes is the narrative slot.

    def stream_events(self):
        """events.jsonl as a list of dicts (the stream's own record)."""
        out = []
        try:
            with open(os.path.join(self.task, "events.jsonl"),
                      encoding="utf-8", errors="replace") as f:
                for raw in f:
                    try:
                        ev = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(ev, dict) and isinstance(ev.get("plan"), str):
                        out.append(ev)
        except OSError:
            pass
        return out

    def fold_stream(self, events):
        """The stream's own numbers: per-plan title/badge/span/tokens + totals.

        Tokens come from `agent.tokens` — an agent's ABSOLUTE running total,
        LAST-WINS per (plan, agent id) — and never from summing the top-level
        `tokens` deltas. That is not a preference: deltas are wire-only, and
        summing them over a stream with any gap in it is measurably wrong
        (monitoring.md, the `agent` bullet). The same fold the dashboard's
        stats view uses, for the same reason.

        Yes, that makes this the monitoring plane's fourth implementation of
        the badge/token/span fold (monitor.html, monitor_server.py, the
        aggregator reducer). It is GD-D10's stated exemption — the monitoring
        plane may re-derive "where its own protocol requires" — and D-15
        requires a renderer that reads the stream IN-PROCESS with no server
        running. The safeguard is that the test cross-checks this against an
        independent re-implementation, not against itself.

        Recorded as debt, with the direction GD-D10 names: when the aggregator's
        reducer grows a library entry point for the stream fold, this should
        call it and stop being the fourth.
        """
        plans, order, first, last = {}, [], None, None
        for ev in events:
            pid = ev.get("plan")
            if not pid:
                continue
            p = plans.get(pid)
            if p is None:
                p = plans[pid] = {"id": pid, "title": "", "badge": "",
                                  "first": None, "last": None, "agents": {},
                                  "events": 0}
                order.append(pid)
            p["events"] += 1
            ts = ev.get("ts")
            if isinstance(ts, str) and ts:
                p["first"] = ts if p["first"] is None else min(p["first"], ts)
                p["last"] = ts if p["last"] is None else max(p["last"], ts)
                first = ts if first is None else min(first, ts)
                last = ts if last is None else max(last, ts)
            title = ev.get("title")
            if isinstance(title, str) and title:
                p["title"] = title
            if (ev.get("stage") in ("plan", "complete")
                    and isinstance(ev.get("state"), str)):
                p["badge"] = ev["state"]
            agent = ev.get("agent")
            if isinstance(agent, dict) and agent.get("id"):
                tok = agent.get("tokens")
                if isinstance(tok, dict):
                    p["agents"][agent["id"]] = {k: as_int(tok.get(k))
                                                for k in TOKEN_KEYS}
        totals = {k: 0 for k in TOKEN_KEYS}
        for p in plans.values():
            p["tokens"] = {k: sum(a[k] for a in p["agents"].values())
                           for k in TOKEN_KEYS}
            for k in TOKEN_KEYS:
                totals[k] += p["tokens"][k]
        return {"order": order, "plans": plans, "totals": totals,
                "agents": sum(len(p["agents"]) for p in plans.values()),
                "first": first, "last": last}

    def find_snapshot(self):
        """The run snapshot `<session>/workflows/<runId>.json`, newest wins.

        Every session dir the transcripts scattered across is a candidate —
        a /clear mid-run leaves the same run named in more than one — so all
        matches are folded and the newest by mtime is taken rather than
        whichever the glob happened to list first. A snapshot is NOT
        guaranteed to exist (SUBSTRATE-11): its absence is a normal state of
        the world, never an error, and the page simply omits the cross-check.
        """
        best = None
        for wf in self._run_dirs():
            wf = os.path.normpath(wf)
            session = os.path.dirname(os.path.dirname(os.path.dirname(wf)))
            cand = os.path.join(session, "workflows",
                                os.path.basename(wf) + ".json")
            try:
                mtime = os.path.getmtime(cand)
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, cand)
        if best is None:
            return None, None
        try:
            with open(best[1], encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, ValueError):
            return None, None
        return (best[1], snap) if isinstance(snap, dict) else (None, None)

    def rollup(self):
        """plan -> {attempt: [(stage, role, verdict word, summary)]}, journal-derived.

        Reads `self.records` — the one correlation fold (GD-D10) — so it covers
        both protocols' plans without knowing which one ran.
        """
        out = {}
        for rec in self.records:
            if rec["kind"] != "result" or not isinstance(rec["result"], dict):
                continue
            word, summary = verdict_of(rec["result"])
            out.setdefault(rec["plan"], {}).setdefault(rec["attempt"], []) \
               .append((rec["stage"], rec["role"], word, summary))
        return out

    def artifacts(self):
        """[(label, path)] — the plan and every findings file, journal-derived.

        First-seen order, deduped: these are the paths the agents themselves
        reported returning, so nothing here is a guess about what is on disk.
        """
        found, out = set(), []

        def add(label, path):
            if isinstance(path, str) and path and path not in found:
                found.add(path)
                out.append((label, path))

        for rec in self.records:
            res = rec["result"]
            if not isinstance(res, dict):
                continue
            add("plan", res.get("plan_file"))
            add("sub-plans", res.get("subplans_file"))
            add(f"{rec['plan']} · {rec['stage']}", res.get("findings_file"))
            for sub in (res.get("subplans") or []):
                if isinstance(sub, dict):
                    add(f"slice · {sub.get('id') or '?'}", sub.get("slice_file"))
        return out

    def report_kind(self):
        """`"research"` or `"implement"`, from the plan ids that actually ran."""
        ids = {r["plan"] for r in self.records}
        implement = bool(ids & set(PROTOCOL_SLOT)) or any(
            i.startswith("sp-") for i in ids)
        if ids & set(RESEARCH_PROOF) and not implement:
            return "research"
        return "implement"

    def narrative_block(self, path):
        """The ONE LLM-authored region of the page (D-15), or the empty slot.

        Injected verbatim between the named markers, so re-rendering with the
        same fragment file is byte-identical. Refused whole — the empty slot
        renders instead, loudly — when it is over NARRATIVE_CAP or carries one
        of the shapes NARRATIVE_ACTIVE_RE names (executing/navigating/restyling
        tags, an `on*=` handler, a `javascript:` URI). One policy for both:
        this renderer never publishes a fragment it had to alter. That list is
        a seatbelt over the common spellings, NOT a sanitizer: the narrator is
        the run's own agent and everything else it writes is injected as-is.
        The narrator's job is prose.
        """
        empty = ('<section id="narrative"><h2>What was decided, and why</h2>'
                 '<p class="sum">No narrative was supplied. Re-render with '
                 '<code>--narrative=&lt;file.html&gt;</code>; every other '
                 'section of this page is derived, never authored.</p>'
                 '</section>')
        if not path:
            return empty
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                body = f.read(NARRATIVE_CAP + 1)
        except OSError as e:
            print(f"narrative not readable ({e}); rendering the empty slot",
                  file=sys.stderr)
            return empty
        if len(body) > NARRATIVE_CAP:
            # Refused WHOLE, like active content, and for a reason of the same
            # kind: a cut at an arbitrary offset lands inside a `<details>`,
            # `<div>` or `<pre>` as easily as between two paragraphs, and an
            # unbalanced tag there swallows every section BELOW it on a page
            # whose whole job is to be read off disk and published. An empty
            # slot is a visible, fixable absence; a half-eaten report is not.
            print(f"narrative over the {NARRATIVE_CAP}-character cap; "
                  "rendering the empty slot instead", file=sys.stderr)
            return empty
        if NARRATIVE_ACTIVE_RE.search(body):
            print("narrative carries active content (script/handler/uri); "
                  "rendering the empty slot instead", file=sys.stderr)
            return empty
        return body.strip() or empty

    def render_final(self, narrative_path=None):
        """Render the run's final report; returns the path written."""
        events = self.stream_events()
        fold = self.fold_stream(events)
        snap_path, snap = self.find_snapshot()
        rolls = self.rollup()
        kind = self.report_kind()
        out_dir = os.path.join(self.task, "report")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(
            out_dir, "research-report.html" if kind == "research"
            else "final-report.html")

        order = list(fold["order"])
        for rec in self.records:                 # journal-only plans, appended
            if rec["plan"] not in order:
                order.append(rec["plan"])

        rows, cards = [], []
        for pid in order:
            p = fold["plans"].get(pid) or {"title": "", "badge": "",
                                           "first": None, "last": None,
                                           "tokens": {k: 0 for k in TOKEN_KEYS},
                                           "agents": {}, "events": 0}
            per = rolls.get(pid, {})
            badge = p["badge"] or "open"
            rows.append(
                f"<tr><td>{esc(pid)}</td><td>{esc(p['title'])}</td>"
                f'<td style="color:{badge_color(badge)};font-weight:700">{esc(badge)}</td>'
                f"<td>{len(per)}</td><td>{esc(span(p['first'], p['last']))}</td>"
                f"<td>{fmt_tokens(p['tokens'])}</td></tr>")
            steps = []
            for attempt in sorted(per):
                items = "".join(
                    f"<li>{esc(stage)} · {esc(role)} — <strong>{esc(word)}</strong>"
                    f'<span class="sum"> {esc(summary)}</span></li>'
                    for stage, role, word, summary in per[attempt])
                steps.append(f"<h3>attempt {attempt}</h3><ul>{items}</ul>")
            if not steps:
                steps.append('<p class="sum">No journal result carried this '
                             "card — it exists on the stream only.</p>")
            cycle_link = ""
            if os.path.isfile(os.path.join(self.report_dir,
                                           f"{pid}-cycle-1.html")):
                cycle_link = ('<p class="sum"><a href="cycles/index.html">'
                              "per-cycle pages</a></p>")
            cards.append(
                f'<div class="card"><h2 id="{esc(pid)}">{esc(pid)}'
                f"{(' — ' + esc(p['title'])) if p['title'] else ''}</h2>"
                f'<p class="sum">badge <strong style="color:{badge_color(badge)}">'
                f"{esc(badge)}</strong> · {len(p['agents'])} agent(s) · "
                f"{esc(span(p['first'], p['last']))} · {fmt_tokens(p['tokens'])}</p>"
                f"{''.join(steps)}{cycle_link}</div>")

        links = "".join(
            f'<li>{esc(label)} — <a href="{esc(rel_link(out_dir, path))}">'
            f"{esc(os.path.basename(path))}</a>"
            f"{'' if os.path.exists(path) else ' <em>(not on disk)</em>'}</li>"
            for label, path in self.artifacts()) or \
            '<li class="sum">no plan or findings file was reported</li>'

        cross = '<p class="sum">No run snapshot on disk — normal while a run ' \
                'is live, and never an error (SUBSTRATE-11).</p>'
        if snap:
            cross = (
                "<table><tr><th>snapshot field</th><th>value</th></tr>"
                f"<tr><td>status</td><td>{esc(snap.get('status'))}</td></tr>"
                f"<tr><td>durationMs</td><td>{esc(snap.get('durationMs'))}</td></tr>"
                f"<tr><td>totalTokens</td><td>{esc(snap.get('totalTokens'))}</td></tr>"
                f"<tr><td>totalToolCalls</td><td>{esc(snap.get('totalToolCalls'))}</td></tr>"
                f"<tr><td>agentCount</td><td>{esc(snap.get('agentCount'))}</td></tr>"
                f"</table><p class=\"sum\">{esc(os.path.basename(snap_path))} — a "
                "CROSS-CHECK, never a substitute: it counts a different "
                "denominator (the driver is outside it) and an "
                "agentCount that differs from the per-agent rows is normal.</p>")

        html = f"""<title>{esc(os.path.basename(self.task))} — {esc(kind)} run report</title>
<style>{CYCLE_CSS}{FINAL_CSS}</style>
<h1>{esc(os.path.basename(self.task))} — {esc(kind)} run report</h1>
<p class="sum">Deterministic render from three recorded sources; the narrative
section is the only authored text on this page.</p>
<div class="tiles">
{tile('plans', len(order))}
{tile('agents', fold['agents'])}
{tile('tokens in', f"{fold['totals']['in']:,}")}
{tile('tokens out', f"{fold['totals']['out']:,}")}
{tile('span', span(fold['first'], fold['last']))}
</div>
<!-- {NARRATIVE_SLOT}:start -->
{self.narrative_block(narrative_path)}
<!-- {NARRATIVE_SLOT}:end -->
<h2>Timeline — every card, in first-seen order</h2>
<table><tr><th>plan</th><th>title</th><th>badge</th><th>attempts</th>
<th>span</th><th>tokens</th></tr>
{os.linesep.join(rows) if rows else '<tr><td colspan="6">no cards</td></tr>'}</table>
<h2>Per-plan detail — verdicts, attempt by attempt</h2>
{os.linesep.join(cards) if cards else '<p class="sum">no plan produced a result</p>'}
<h2>The plan and every findings file</h2>
<ul>{links}</ul>
<h2>Harness cross-check</h2>
{cross}
<h2>Where each number comes from</h2>
<table><tr><th>on this page</th><th>source</th></tr>
<tr><td>plans, attempts, verdicts, artifact paths</td><td>journal.jsonl results,
classified by the [monitor] marker in each agent transcript</td></tr>
<tr><td>badges, spans, token counters</td><td>events.jsonl — agent.tokens
last-wins per (plan, agent), never a sum of the wire deltas</td></tr>
<tr><td>the cross-check table</td><td>the run snapshot, read-only</td></tr>
<tr><td>the narrative section</td><td>authored — the one slot on this page a
renderer does not fill</td></tr></table>
<p class="foot">rendered deterministically by cycle_reporter.py --final. No
render timestamp: the same inputs render the same bytes.</p>
"""
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        return out_path

    # -- main pass ----------------------------------------------------------
    def pass_once(self):
        changed = self.ingest()
        # A close that failed to reach the stream stays due, so an unchanged
        # journal still costs a retry — one subprocess and an idempotent
        # re-render, against a card that would otherwise sit "running" for the
        # rest of the run.
        if (not changed and not any(self._due(p) for p in self.closed)
                and not (self.roster and self._due(ROSTER_SENTINEL))):
            return False
        cap_of = self.caps()
        self.evaluate_closes(cap_of)
        self.evaluate_protocol_closes()
        self.evaluate_research_closes()
        os.makedirs(self.report_dir, exist_ok=True)
        for plan in self.plan_order:
            cap = cap_of(plan)
            for attempt in sorted(self.cycles[plan]):
                self.render_page(plan, attempt, cap)
        self.render_index(cap_of)
        for plan in list(self.closed):
            if self._due(plan):
                self.emit_close(plan)
        if self.roster and self._due(ROSTER_SENTINEL):
            self.emit_roster()
        self._save_state()
        return changed

    def settle(self):
        """One-shot close-out: emit the closes that are implied but ABSENT.

        `touch-run close` calls this, and so does the recovery procedure that
        used to tell a human to hand-type corrective events. It differs from a
        normal pass in exactly two ways:

        * the terminal rules are switched on (``final=True``) — "zero returns"
          is only knowable once someone declares the run over;
        * every emission is diffed against the STREAM and against nothing else.
          Running it twice — or after some other writer already closed a card —
          emits nothing; and a checkpoint that claims a close events.jsonl does
          not carry is OVERRULED rather than believed, because a recorded write
          that never landed is the failure this mode repairs.

        What it does NOT do is invent a verdict for a loop that stopped
        mid-attempt: no rule implies a close there, so none is written and the
        watcher's own run-end pass keeps that job (R-58).

        Returns the plan ids it actually wrote.
        """
        self.ingest()
        cap_of = self.caps()
        self.evaluate_closes(cap_of)
        self.evaluate_protocol_closes()
        self.evaluate_research_closes(final=True)
        on_stream, on_roster = self.scan_stream()
        wrote = []
        for plan in list(self.closed):
            if plan in on_stream:
                # Someone already said it. Record that, so a later live pass in
                # the same process does not write a second, contradictory line.
                self.emitted.add(plan)
                continue
            # The stream does NOT carry this close, so whatever the checkpoint
            # remembers about it is wrong — drop the stale claim rather than
            # short-circuit on it, or the one mechanism that can repair a lost
            # write is the one thing stopping the repair.
            self.emitted.discard(plan)
            # emit_close answers for the WRITER: under --no-status, or with no
            # status.sh in the payload, it writes nothing and says so. `wrote`
            # is what reached the stream, never what was merely implied — the
            # caller (`touch-run close`) prints this list to a human.
            if self.emit_close(plan):
                wrote.append(plan)
        mine = self.roster_entries()
        if mine:
            if on_roster == mine:
                # Already ours, byte for byte. Anything ELSE on the stream is a
                # different roster — `touch-run start`'s seed from the run spec,
                # most likely — and the divider's partition is the later and
                # truer of the two, so it still goes out. Same rule as the
                # closes above: the stream decides, not the checkpoint.
                self.emitted.add(ROSTER_SENTINEL)
            else:
                self.emitted.discard(ROSTER_SENTINEL)
                if self.emit_roster():
                    wrote.append(ROSTER_SENTINEL)
        self._save_state()
        return wrote


#: Flags that TAKE a value, in both spellings. The `=`-only parser they replace
#: dropped `--narrative FILE` on the floor: the value became a wf_dir, the page
#: rendered its EMPTY slot, and the process exited 0 — a silent lie about what
#: the operator did, on the flag a skill's own prose tells an LLM to pass.
VALUE_FLAGS = ("--narrative", "--interval")


def parse_args(argv):
    """`(args, flags, interval, narrative)`, or None when the argv is refused.

    A value flag missing its value is an ERROR, never a fall-back to the
    default: this program's whole claim is that its output is a function of
    recorded inputs, and quietly dropping one of them is the opposite.
    """
    args, flags = [], set()
    interval, narrative = 2.0, None
    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        i += 1
        if not a.startswith("--"):
            args.append(a)
            continue
        name, eq, value = a.partition("=")
        if name not in VALUE_FLAGS:
            flags.add(a)
            continue
        if not eq:
            if i >= n or argv[i].startswith("--"):
                print(f"{name} needs a value ({name}=X or {name} X)",
                      file=sys.stderr)
                return None
            value, i = argv[i], i + 1
        if name == "--narrative":
            narrative = value
        else:
            try:
                interval = float(value)
            except ValueError:
                print(f"--interval must be a number of seconds, not {value!r}",
                      file=sys.stderr)
                return None
            # The bounded comparison also rejects `nan` and `inf`, which parse
            # fine as floats and then wedge the poll loop instead of failing.
            if not 0 < interval <= 3600:
                print("--interval must be a positive number of seconds, at "
                      f"most 3600, not {value!r}", file=sys.stderr)
                return None
    return args, flags, interval, narrative


def main(argv):
    parsed = parse_args(list(argv[1:]))
    if parsed is None:
        return 2
    args, flags, interval, narrative = parsed
    task = os.environ.get("ORCH_STATE_DIR")
    if not task or not os.path.isdir(task):
        print("ORCH_STATE_DIR must point at the task folder", file=sys.stderr)
        return 2
    if not args:
        print("usage: ORCH_STATE_DIR=<task> cycle_reporter.py <wf_dir> [...] "
              "[--once] [--settle] [--final [--narrative FILE]] [--no-status] "
              "[--interval N]   (value flags also take --flag=VALUE)",
              file=sys.stderr)
        return 2
    final = "--final" in flags or "--run-report" in flags
    rep = Reporter(task, [os.path.abspath(a) for a in args],
                   # --final renders; it never writes an event, so it needs no
                   # --no-status beside it and cannot be told to emit one.
                   emit_status="--no-status" not in flags and not final)
    stop = {"flag": False}

    def _sig(_n, _f):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    if "--settle" in flags:
        wrote = rep.settle()
        # This line is what a human reads to decide whether a run closed
        # cleanly, so it may only report the stream when the stream was
        # actually consulted AND writable. An empty list means "already
        # closed" ONLY when a write was possible; otherwise it means "this
        # invocation was never allowed to write", which is a different fact.
        failed = sorted(rep.write_failed)
        if wrote:
            print("settle: " + ", ".join(wrote))
        if failed:
            # An attempted write that the writer refused. Exit stays 0 — a
            # monitoring call never breaks a close-out, and `touch-run close`
            # treats this step as the optional one — but the line says so, so
            # nobody reads silence as success and re-runs nothing.
            print("settle: FAILED to write " + ", ".join(failed)
                  + " — status.sh refused; re-run --settle once it is fixed")
        elif not wrote:
            if not rep.emit_status:
                print("settle: nothing written — emission is off (--no-status "
                      "or --final); the stream was not touched")
            elif not rep.status_sh:
                print("settle: nothing written — no status.sh in the payload")
            else:
                print("settle: nothing to close — every implied close is "
                      "already on the stream")
        return 0
    if final:
        # Ingest and render, and nothing else: every badge on the final report
        # is read off the STREAM, so evaluating closes here would compute a
        # verdict the page does not use and cannot write.
        rep.ingest()
        print(rep.render_final(narrative))
        return 0

    rep.pass_once()
    if "--once" in flags:
        return 0
    while not stop["flag"]:
        time.sleep(interval)
        rep.pass_once()
    rep._save_state()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
