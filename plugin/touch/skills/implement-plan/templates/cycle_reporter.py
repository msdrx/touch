#!/usr/bin/env python3
"""cycle_reporter.py — deterministic per-cycle report renderer + loop-close emitter.

Renders ONE visual report after EVERY implement->test->critique cycle of an
implement-plan workflow run, with zero LLM cooperation (the workflow runtime has
no filesystem access, and an LLM scribe would be non-deterministic — this daemon
is the deterministic option). Same derivation technique as decision_watcher.py:

  journal.jsonl result records ── agentId ──> agent-<id>.jsonl transcript
                                              └─ "[monitor] plan=.. stage=.. role=.. attempt=N"

Outputs, all under $ORCH_STATE_DIR (the task folder):
  report/cycles/<plan>-cycle-<N>.html   one page per cycle: UML-style flow
      (implement -> test gate -> critique -> verdict), a "why it failed /
      succeeded" section from the structured verdict summaries, and the full
      gate/critique findings files embedded as evidence.
  report/cycles/index.html              run overview, execution order.
  events.jsonl (via status.sh)          the loop-terminal `plan done|failed`
      event when a loop closes — a REAL verdict at the published cap (never the
      retired phase-advance inference) — PLUS the terminal closes of the
      implement protocol's two single-agent plans: `divide` when the partition
      result lands (with the template's ORCH_PLANS_TOTAL declaration) and
      `finalgate` when the sweep verdict settles. The template emits those two
      via runStatus at fixed control-flow points (R-09), but the current
      workflow runtime has no Node API, so the calls silently no-op and the
      cards sat on "running" until the watcher's run-end settle pass; this
      daemon substitutes for the script there exactly as it does for loop
      closes — same events, same messages, derived from the same journal
      results. Suppress with --no-status.

Attempt caps come from orch-config.json: max_plan_attempts (default 4) plus the
per-plan extra_attempts map ({"sp-x": N} raises only that loop's cap), and
max_finalgate_attempts (default 2) for the sweep close — re-read every poll,
like decision_watcher re-reads its caps.

Usage:
  ORCH_STATE_DIR=<task-dir> python3 cycle_reporter.py <wf_dir> [<wf_dir>...]
      [--once] [--no-status] [--interval SECONDS]

  --once       process the journals once and exit (backfill of a finished or
               foreign run; combine with --no-status to keep history untouched)
  --no-status  never emit status.sh events (render-only)

Checkpointed in $ORCH_STATE_DIR/.cycle-reporter-state.json (restart-safe; a
loop-close event is emitted exactly once). Stdlib only.
"""

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
FINDINGS_EMBED_CAP = 60_000

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


def esc(s):
    return (str("" if s is None else s)
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        self.plan_order = []          # first-seen order == execution order (serial)
        self.closed = {}              # plan -> {"state": .., "cls": .., "attempt": n}
        self.marker_cache = {}        # agentId -> marker tuple or None
        # offsets are IN-MEMORY only: every start re-reads the journals from
        # zero and rebuilds the full picture (rendering is idempotent; journals
        # are small). Only `emitted` persists — a loop-close status event must
        # fire exactly once across restarts.
        self.offsets = {}             # wf_dir -> consumed bytes of journal.jsonl
        self.emitted = set()          # plans whose close event was emitted
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

        `templates/` -> `implement-plan/` -> `skills/` -> the plugin root, and
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
    def marker(self, wf_dir, agent_id):
        if agent_id in self.marker_cache:
            return self.marker_cache[agent_id]
        m = None
        # A resumed run keeps ONE journal but scatters transcripts across the
        # session dirs it lived in — search every provided wf_dir, record's own
        # dir first.
        dirs = [wf_dir] + [d for d in self.wf_dirs if d != wf_dir]
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
                break
        self.marker_cache[agent_id] = m
        return m

    def ingest(self):
        """Consume new complete journal lines; return True if anything changed."""
        changed = False
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
                if rec.get("type") != "result":
                    continue
                mark = self.marker(wf, rec.get("agentId", ""))
                if not mark:
                    continue
                plan, stage, _role, attempt = mark
                res = rec.get("result")
                if plan in PROTOCOL_SLOT:
                    pslot = PROTOCOL_SLOT[plan].get(stage)
                    if pslot is not None and isinstance(res, dict):
                        self.protocol.setdefault(plan, {}) \
                            .setdefault(attempt, {})[pslot] = res
                        changed = True
                    continue
                slot = STAGE_SLOT.get(stage)
                if slot is None or not plan.startswith("sp-"):
                    continue
                if not isinstance(res, dict):
                    continue
                per = self.cycles.setdefault(plan, {})
                if plan not in self.plan_order:
                    self.plan_order.append(plan)
                # journal order: a retry's result simply overwrites its attempt slot
                per.setdefault(attempt, {})[slot] = res
                changed = True
        return changed

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

    def _finalgate_cap(self):
        try:
            with open(os.path.join(self.task, "orch-config.json"), encoding="utf-8") as f:
                return int(json.load(f).get("max_finalgate_attempts", 2))
        except (OSError, ValueError):
            return 2

    def emit_close(self, plan):
        info = self.closed[plan]
        if not self.emit_status or plan in self.emitted or not self.status_sh:
            self.emitted.add(plan)
            return
        if "msg" in info:
            msg = info["msg"]
        elif info["state"] == "done":
            msg = f"green on attempt {info['attempt']}/{info['cap']}"
        else:
            msg = f"attempts exhausted {info['attempt']}/{info['cap']} ({info['cls']})"
        try:
            # self.status_sh is always the payload's own script (see
            # _find_status_sh) — never a path the reported-on project supplies.
            r = subprocess.run(
                ["bash", self.status_sh, plan, "plan", info["state"], msg],
                env={**os.environ, "ORCH_STATE_DIR": self.task,
                     **(info.get("env") or {})},
                capture_output=True, text=True, timeout=30)
            warn = (r.stderr or "").strip()
            if warn:
                print(f"status.sh warned on {plan}: {warn.splitlines()[0]}", file=sys.stderr)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"could not emit close for {plan}: {e}", file=sys.stderr)
        self.emitted.add(plan)

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

    # -- main pass ----------------------------------------------------------
    def pass_once(self):
        changed = self.ingest()
        if not changed and all(p in self.emitted for p in self.closed):
            return False
        cap_of = self.caps()
        self.evaluate_closes(cap_of)
        self.evaluate_protocol_closes()
        os.makedirs(self.report_dir, exist_ok=True)
        for plan in self.plan_order:
            cap = cap_of(plan)
            for attempt in sorted(self.cycles[plan]):
                self.render_page(plan, attempt, cap)
        self.render_index(cap_of)
        for plan in list(self.closed):
            if plan not in self.emitted:
                self.emit_close(plan)
        self._save_state()
        return changed


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    interval = 2.0
    for a in list(flags):
        if a.startswith("--interval"):
            flags.discard(a)
            interval = float(a.split("=", 1)[1]) if "=" in a else interval
    task = os.environ.get("ORCH_STATE_DIR")
    if not task or not os.path.isdir(task):
        print("ORCH_STATE_DIR must point at the task folder", file=sys.stderr)
        return 2
    if not args:
        print("usage: ORCH_STATE_DIR=<task> cycle_reporter.py <wf_dir> [...] "
              "[--once] [--no-status] [--interval=N]", file=sys.stderr)
        return 2
    rep = Reporter(task, [os.path.abspath(a) for a in args],
                   emit_status="--no-status" not in flags)
    stop = {"flag": False}

    def _sig(_n, _f):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

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
