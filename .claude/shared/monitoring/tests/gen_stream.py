#!/usr/bin/env python3
"""Deterministic seeded ``events.jsonl`` generator for the monitoring tests.

Why this exists (GD-G / SERVER-READ-13 / PRIOR-ART-TOUCH-14): the frozen
corpora top out at 590 lines / 232 KB, three orders of magnitude below the
stream that motivates the perf work, so no committed fixture can express
"12k events" — let alone the 100k headroom target. Committing a 5 MB blob
would fix the size at one number and cost the repo forever; a **seeded
generator** costs nothing in git, scales to any N, and is reproducible byte
for byte from ``(n_events, n_plans, n_agents, seed)``.

This module is a HELPER, not a test: it is deliberately named ``gen_stream.py``
so ``tests/run_all.sh``'s ``test_*.py`` glob never executes it. Its own
correctness (determinism, composition, the GD-C token identity, shape fidelity
against the frozen fixture) is asserted from ``test_ws_e2e.py``.

Faithfulness — measured against the two real streams available here, the
12,334-event corpus summarised in the research findings and the live
``touch-monitor-perf`` stream (1,134 lines, same watcher version):

| property                   | corpus | live   | generated |
|----------------------------|--------|--------|-----------|
| ``stage == "tokens"``      | 91 %   | 87.7 % | 91.0 %    |
| ``quiet`` truthy           | 90 %   | 86.2 % | 90.0 %    |
| carries an ``agent`` block | 92.5 % | 91.2 % | 92.7 %    |
| mean bytes per line        | ~458 B | 454 B  | ~470 B    |

The three fractions are hit by construction (they ARE the budget below); the
mean line length is an emergent property of writing the watcher's real
strings, and lands ~3 % high — the per-shape means are the honest check and
they match the live writer closely (token tick 498 B vs 490 B measured,
agent-carrying event 337 B vs 412 B, plain line 165 B vs 171 B).

and, structurally, the four shapes the watcher actually writes:

* a **quiet token tick** — top-level ``tokens`` is the DELTA since that agent's
  previous emit, ``agent.tokens`` is the agent's ABSOLUTE running total (GD-C);
* an **agent-carrying non-token event** — spawn (``started``) and terminal
  (``runtime`` + a cumulative that is HIGHER than the agent's last tick,
  because the rollup arrives separately);
* an **agent-less token line** — the result-rollup delta that covers exactly
  that terminal gap. In the real corpus 144 such lines carry 16,985,189 input
  tokens, precisely the difference between "sum of per-agent deltas" and "sum
  of per-agent last cumulative" (PRIOR-ART-TOUCH-1). Reproducing that is what
  makes the generated stream a valid subject for the GD-C equality test:
  ``sum of every ev.tokens per plan == sum of last cumulative agent.tokens per
  (plan, agent)``, exactly, per plan and in total — see :func:`token_models`;
* an **agent-less non-token line** — plan badges and the driver's own
  ``status.sh`` progress calls (``w: "agent"``).

Two deliberate departures from the 12,334-line corpus, both documented so a
reader does not mistake them for drift: (1) ``w`` is present on EVERY line
(the corpus is a mixed-era stream where 74 % carry it; the current writer
always does, and the live stream measured 100 %); (2) agent ids are the
current 17-hex form with an 8-hex ``shortId``, not the pre-R-39 8-hex ids
still frozen in ``tests/fixtures/legacy/``. Both are additive-only and are
exactly what the fold under test must handle today.

Timestamps mirror the writer's two clocks: watcher-"now" lines use
``+00:00`` and journal-derived lines use ``Z`` and are stamped slightly
EARLIER than their file position — the real, mild ts inversion the timeplan
has to tolerate. File order is therefore the only trustworthy order, which is
what every fold in this repo already assumes.

Usage::

    from gen_stream import make_stream, write_stream, stream_stats
    lines = make_stream(12_000)                 # list[str], no trailing newline
    plan  = write_stream("/tmp/events.jsonl", 12_000)

    python3 gen_stream.py --self-check          # composition + invariants
    python3 gen_stream.py 12000 > events.jsonl  # a stream on stdout
"""
import json
import random
import sys
from datetime import datetime, timezone

# Composition measured on the 12,334-event corpus (research findings' table).
TOKENS_FRAC = 0.91      # stage == "tokens"
QUIET_FRAC = 0.90       # quiet truthy  (every quiet line is a token tick)
AGENT_FRAC = 0.925      # carries an agent block
ROLLUP_FRAC = 0.01      # agent-less token deltas (the result rollups)
TARGET_LINE_BYTES = 458

# Corpus density: 12,334 events / 167 agents / 19 plans. Used when the caller
# does not pin n_agents / n_plans, so composition stays faithful at any N.
EVENTS_PER_AGENT = 74
EVENTS_PER_PLAN = 650

TOKEN_KEYS = ("in", "out", "cached", "cache_write")

# Vocabulary. Roles double as `stage` values, exactly like the real streams
# (the watcher writes the role name into `stage`, "tokens" being the one
# reserved stage and "plan"/"complete" the badge stages).
RESEARCH_ROLES = ("convo", "sessionjsonl", "liveflow", "mongoschema",
                  "customstate", "frontend-render", "server-read",
                  "write-side", "data-model", "ws-protocol",
                  "prior-art-touch")
LOOP_ROLES = ("implement", "test", "critique")
_LOOP_KIND = {"implement": "impl", "test": "test", "critique": "critique"}
SUBPLAN_WORDS = ("write-cadence", "ws-harness", "server-stream", "client-v2",
                 "docs", "touch-legacy", "registry", "snapshot", "cursor",
                 "budget", "timeplan", "observability")


def _plan_names(n_plans: int) -> list:
    """Plan card names, in the order a real run queues them."""
    names = ["research", "synthesis"][:max(0, min(2, n_plans))]
    i = 0
    while len(names) < n_plans:
        word = SUBPLAN_WORDS[i % len(SUBPLAN_WORDS)]
        suffix = "" if i < len(SUBPLAN_WORDS) else f"-{i // len(SUBPLAN_WORDS) + 1}"
        names.append(f"sp-{word}{suffix}")
        i += 1
    return names


def _roles_for(plan_name: str) -> tuple:
    return RESEARCH_ROLES if plan_name in ("research", "synthesis") else LOOP_ROLES


def _iso(t: float, journal: bool = False) -> str:
    """Watcher-"now" (``+00:00``) or journal-derived (``Z``) timestamp."""
    s = datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")
    return s.replace("+00:00", "Z") if journal else s


def _k(n: int) -> str:
    """The watcher's own compact token rendering ("117.2k", "736")."""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _runtime(secs: float) -> str:
    secs = int(secs)
    return f"{secs // 60}m{secs % 60:02d}s" if secs >= 60 else f"{secs}s"


def _hex_id(rng: random.Random) -> str:
    """A 17-hex agent id in the current writer's shape (leading 'a')."""
    return "a" + "".join(rng.choice("0123456789abcdef") for _ in range(16))


def _shape(n_events: int, n_plans, n_agents):
    """Resolve (plans, agents, ticks, rollups, filler) so the counts sum to N.

    Budget, top down — every number here is an exact event count, so a stream
    of exactly ``n_events`` lines comes out the other end:

      2            orchestrator card open + run-level ``complete done``
      3 * plans    per plan: queued, running, done
      4 * agents   per agent: orchestrator spawn line, spawn, terminal,
                   orchestrator verdict line
      rollups      agent-less result-rollup token deltas (<= agents)
      ticks        quiet token ticks  (the QUIET_FRAC target)
      filler       agent-written progress lines, whatever is left over

    ``n_agents``/``n_plans`` default to the corpus density; both are shrunk
    (never grown) when the requested N cannot structurally hold them, so a
    100-event smoke stream still generates rather than raising.
    """
    if n_events < 12:
        raise ValueError("n_events must be >= 12 (one plan, one agent, one tick)")
    if n_agents is None:
        n_agents = max(1, round(n_events / EVENTS_PER_AGENT))
    if n_plans is None:
        n_plans = max(1, round(n_events / EVENTS_PER_PLAN))
    agents = max(1, int(n_agents))
    plans = max(1, int(n_plans))
    while True:
        plans = max(1, min(plans, agents))
        rollups = min(agents, round(ROLLUP_FRAC * n_events))
        base = 2 + 3 * plans + 4 * agents + rollups
        if base + agents <= n_events:  # every agent needs at least one tick
            break
        if agents == 1 and plans == 1:
            raise ValueError(f"n_events={n_events} too small for one agent")
        agents = max(1, agents // 2)
        plans = max(1, plans // 2)
    ticks = min(n_events - base, max(agents, round(QUIET_FRAC * n_events)))
    filler = n_events - base - ticks
    return plans, agents, ticks, rollups, filler


def _tick_quota(total_ticks: int, n_agents: int, rng: random.Random) -> list:
    """Heavy-tailed per-agent tick counts: >= 1 each, summing EXACTLY."""
    weights = [rng.lognormvariate(0.0, 0.7) for _ in range(n_agents)]
    total_w = sum(weights)
    spare = total_ticks - n_agents
    raw = [spare * w / total_w for w in weights]
    base = [int(x) for x in raw]
    short = spare - sum(base)
    order = sorted(range(n_agents), key=lambda i: (-(raw[i] - base[i]), i))
    for i in order[:short]:
        base[i] += 1
    return [b + 1 for b in base]


def make_events(n_events: int, n_plans=None, n_agents=None, seed: int = 20260727,
                start: float = 1785225600.0) -> list:
    """Return exactly ``n_events`` event dicts in FILE order.

    Deterministic in ``(n_events, n_plans, n_agents, seed)`` — same arguments,
    byte-identical stream. ``start`` is the epoch seconds of the run's first
    event (default 2026-07-28T08:00:00Z) and only moves the timestamps; it is
    a parameter so a test can place two generated streams on disjoint clocks.
    """
    rng = random.Random(seed)
    plans, agents, ticks, rollups, filler = _shape(n_events, n_plans, n_agents)
    names = _plan_names(plans)
    quota = _tick_quota(ticks, agents, rng)

    # Agents are dealt round-robin over the plan cards, so every plan card has
    # at least one agent and the per-plan token folds are all non-trivial.
    plan_of = [names[i % plans] for i in range(agents)]
    per_plan_index: dict = {}
    specs = []
    for i in range(agents):
        plan = plan_of[i]
        roles = _roles_for(plan)
        n_in_plan = per_plan_index.get(plan, 0)
        per_plan_index[plan] = n_in_plan + 1
        role = roles[n_in_plan % len(roles)]
        attempt = 1 + n_in_plan // len(roles)
        # Label shape is the watcher's: "<stage>:<kind> #<attempt>", e.g.
        # "frontend-render:research #1", "implement:impl #2".
        kind = "research" if roles is RESEARCH_ROLES else _LOOP_KIND[role]
        specs.append({
            "plan": plan, "role": role, "attempt": attempt,
            "id": _hex_id(rng), "ticks": quota[i],
            "label": f"{role}:{kind} #{attempt}",
            "rollup": False, "cum": {k: 0 for k in TOKEN_KEYS},
        })
    # Which agents get an agent-less rollup delta (spread, deterministic).
    if rollups:
        step = agents / rollups
        for j in range(rollups):
            specs[min(agents - 1, int(j * step))]["rollup"] = True

    out = []          # (scheduled time, emission seq, event dict)
    seq = 0

    def emit(t: float, ev: dict, journal: bool = False, shift: float = 0.0):
        nonlocal seq
        ev["ts"] = _iso(t - shift, journal)
        out.append((t, seq, ev))
        seq += 1

    # --- run opening: every plan card queued up front, orchestrator running ---
    t = start
    for idx, name in enumerate(names):
        ev = {"ts": None, "plan": name, "stage": "plan", "state": "queued",
              "detail": f"{name} queued by the driver", "w": "agent",
              "title": f"{name.replace('sp-', '').replace('-', ' ').title()}"}
        if idx == 0:
            ev["plans_total"] = plans
        emit(t, ev)
        t += 0.035
    emit(t, {"ts": None, "plan": "orchestrator", "stage": "plan",
             "state": "running", "detail": "run launching", "w": "agent",
             "title": "Orchestrator"})
    t += 0.4

    # --- plans run one after another; agents inside a plan run in waves ------
    filler_slots = []      # agent indices, in the order they settle
    for name in names:
        emit(t, {"ts": None, "plan": name, "stage": "plan", "state": "running",
                 "detail": f"{name} loop opening", "w": "agent"})
        wave_size = 6 if name == "research" else 3
        mine = [i for i in range(agents) if plan_of[i] == name]
        cursor = t + 1.0
        for w0 in range(0, len(mine), wave_size):
            wave = mine[w0:w0 + wave_size]
            wave_start = cursor
            wave_end = wave_start
            for i in wave:
                spec = specs[i]
                a_start = wave_start + rng.uniform(0.0, 3.0)
                spec["start"] = a_start
                # orchestrator's own spawn line (agent-less, journal clock)
                emit(a_start, {"ts": None, "plan": "orchestrator",
                               "stage": name, "state": "running",
                               "detail": (f"spawn {name} {spec['role']} attempt "
                                          f"{spec['attempt']}"),
                               "w": "watcher"},
                     journal=True, shift=rng.uniform(0.0, 2.5))
                emit(a_start + 0.02, {
                    "ts": None, "plan": name, "stage": spec["role"],
                    "state": "running",
                    "detail": f"{spec['role']} attempt {spec['attempt']} spawned",
                    "w": "watcher",
                    "agent": {"id": spec["id"], "shortId": spec["id"][:8],
                              "label": spec["label"], "state": "running",
                              "started": _iso(a_start, journal=True)}},
                     journal=True, shift=rng.uniform(0.0, 2.5))
                # quiet token ticks
                tcur = a_start
                for _ in range(spec["ticks"]):
                    tcur += rng.uniform(4.0, 18.0)
                    # Delta magnitudes are the measured ones: a per-tick `in`
                    # around 78k, so an agent's cumulative lands in the low
                    # millions after ~60 ticks — the same digit counts the real
                    # lines carry, which is half of what sets the mean line
                    # length (the other half is the label appearing twice).
                    d_in = rng.randrange(8_000, 150_000)
                    d_cached = int(d_in * rng.uniform(0.72, 0.97))
                    d_fresh = rng.randrange(0, 64)
                    d_write = max(0, d_in - d_cached - d_fresh)
                    d_out = rng.randrange(24, 1400)
                    delta = {"in": d_in, "out": d_out, "cached": d_cached,
                             "cache_write": d_write}
                    for k in TOKEN_KEYS:
                        spec["cum"][k] += delta[k]
                    cum = dict(spec["cum"])
                    emit(tcur, {
                        "ts": None, "plan": name, "stage": "tokens",
                        "state": "info",
                        "detail": (f"{spec['label']} running: in {_k(cum['in'])} - "
                                   f"r:{_k(cum['cached'])} w:{_k(cum['cache_write'])} "
                                   f"· out {_k(cum['out'])} so far"),
                        "w": "watcher", "tokens": delta, "quiet": True,
                        "agent": {"id": spec["id"], "shortId": spec["id"][:8],
                                  "label": spec["label"], "state": "running",
                                  "tokens": cum}})
                spec["end"] = tcur + rng.uniform(1.0, 6.0)
                wave_end = max(wave_end, spec["end"] + 1.2)
            cursor = wave_end
        t = cursor

        # --- terminals for this plan's agents, in start order ----------------
        for i in mine:
            spec = specs[i]
            # the rollup delta the terminal event's cumulative already includes
            residual = {k: 0 for k in TOKEN_KEYS}
            if spec["rollup"]:
                r_in = rng.randrange(2_000, 190_000)
                r_cached = int(r_in * rng.uniform(0.7, 0.98))
                r_write = max(0, r_in - r_cached - rng.randrange(0, 48))
                residual = {"in": r_in, "out": rng.randrange(40, 3_600),
                            "cached": r_cached, "cache_write": r_write}
            final = {k: spec["cum"][k] + residual[k] for k in TOKEN_KEYS}
            # 1 in 12 agents ends stale: no tokens block on the terminal event,
            # so the fold's "last cumulative on ANY event" must fall back to the
            # agent's last tick (and such an agent never gets a rollup).
            stale = spec["rollup"] is False and (i % 12 == 11)
            state = "stale" if stale else ("failed" if i % 9 == 8 else "done")
            agent = {"id": spec["id"], "shortId": spec["id"][:8],
                     "label": spec["label"], "state": state}
            if not stale:
                agent["tokens"] = final
            agent["runtime"] = _runtime(spec["end"] - spec["start"])
            detail = (f"{spec['label']} abandoned — no transcript activity"
                      if stale else
                      f"{spec['label']}: {'rejected' if state == 'failed' else 'complete'}")
            emit(spec["end"], {
                "ts": None, "plan": name, "stage": spec["role"], "state": state,
                "detail": detail, "w": "watcher", "agent": agent},
                journal=True, shift=rng.uniform(0.0, 2.5))
            if spec["rollup"]:
                emit(spec["end"] + 0.01, {
                    "ts": None, "plan": name, "stage": "tokens", "state": "info",
                    "detail": (f"{spec['label']} used in {_k(final['in'])} - "
                               f"r:{_k(final['cached'])} w:{_k(final['cache_write'])} "
                               f"· out {_k(final['out'])} total"),
                    "w": "watcher", "tokens": residual},
                     journal=True, shift=rng.uniform(0.0, 2.5))
            emit(spec["end"] + 0.02, {
                "ts": None, "plan": "orchestrator", "stage": name,
                "state": "running",
                "detail": (f"verdict {name} {spec['role']} attempt "
                           f"{spec['attempt']}: {state}"),
                "w": "watcher"},
                 journal=True, shift=rng.uniform(0.0, 2.5))
            filler_slots.append(i)
        t = max(t, max(specs[i]["end"] for i in mine) + 0.5)
        emit(t, {"ts": None, "plan": name, "stage": "plan", "state": "done",
                 "detail": f"{name} closed", "w": "agent"})
        t += rng.uniform(2.0, 20.0)

    # --- agent-written progress lines (the `w:"agent"` status.sh calls) ------
    # Attached to agents in passes so they land inside each agent's own window;
    # these are the agent-LESS non-token lines the composition budget expects.
    passes = ("attempt {a}: implementing", "attempt {a}: {r} pass",
              "attempt {a}: writing findings", "attempt {a}: verifying")
    for j in range(filler):
        i = filler_slots[j % len(filler_slots)] if filler_slots else 0
        spec = specs[i]
        p = j // max(1, len(filler_slots))
        span = max(0.5, spec["end"] - spec["start"])
        at = spec["start"] + span * min(0.95, 0.1 + 0.2 * p) + 0.05 * (j % 7)
        emit(at, {"ts": None, "plan": spec["plan"], "stage": spec["role"],
                  "state": "running",
                  "detail": passes[p % len(passes)].format(a=spec["attempt"],
                                                           r=spec["role"]),
                  "w": "agent"})

    # --- run close ----------------------------------------------------------
    emit(t + 1.0, {"ts": None, "plan": "orchestrator", "stage": "complete",
                   "state": "done", "detail": "run complete", "w": "agent"})

    out.sort(key=lambda row: (row[0], row[1]))
    events = [ev for _, _, ev in out]
    assert len(events) == n_events, (len(events), n_events)  # budget is exact
    return events


def make_stream(n_events: int, n_plans=None, n_agents=None, seed: int = 20260727,
                start: float = 1785225600.0) -> list:
    """The generated stream as JSON lines (no trailing newlines).

    ``ensure_ascii`` is left at its default so the middle dot and em dash in
    the watcher's details are escaped exactly as the real writer escapes them —
    the line bytes, and therefore the mean line length, are the real thing.
    """
    return [json.dumps(ev) for ev in
            make_events(n_events, n_plans, n_agents, seed, start)]


def write_stream(path: str, n_events: int, n_plans=None, n_agents=None,
                 seed: int = 20260727, start: float = 1785225600.0) -> dict:
    """Write a generated stream to ``path``; return its :func:`stream_stats`."""
    lines = make_stream(n_events, n_plans, n_agents, seed, start)
    blob = ("\n".join(lines) + "\n").encode()
    with open(path, "wb") as f:
        f.write(blob)
    stats = stream_stats(lines)
    stats["bytes"] = len(blob)
    return stats


def _records(stream) -> list:
    """Accept lines (str/bytes) or already-parsed dicts."""
    return [item if isinstance(item, dict) else json.loads(item)
            for item in stream]


def token_models(stream):
    """The two GD-C token models, per plan.

    * model A (what ``monitor.html`` accumulates today, and what
      ``monitor_server.replay_plan_states`` sums): EVERY ``ev.tokens`` delta.
    * model B (the fold / snapshot): per ``(plan, agent.id)`` the LAST
      ``agent.tokens`` seen on ANY event, agent-less deltas ignored.

    Returns ``(model_a, model_b)`` as ``{plan: {in,out,cached,cache_write}}``.
    Equality of the two is the property the snapshot design rests on; this
    generator is built so it holds exactly.
    """
    recs = _records(stream)
    a: dict = {}
    last: dict = {}
    for ev in recs:
        plan = ev.get("plan")
        tok = ev.get("tokens")
        if tok:
            row = a.setdefault(plan, {k: 0 for k in TOKEN_KEYS})
            for k in TOKEN_KEYS:
                row[k] += tok.get(k) or 0
        agent = ev.get("agent")
        if isinstance(agent, dict) and agent.get("tokens"):
            last[(plan, agent.get("id"))] = agent["tokens"]
    b: dict = {}
    for (plan, _aid), tok in last.items():
        row = b.setdefault(plan, {k: 0 for k in TOKEN_KEYS})
        for k in TOKEN_KEYS:
            row[k] += tok.get(k) or 0
    return a, b


def stream_stats(stream) -> dict:
    """Composition + invariant summary of a stream (lines or dicts).

    ``stream`` is materialised first so a one-shot iterator works: it is read
    twice here (records and raw line bytes) and a half-consumed generator would
    report a silently truncated composition.
    """
    stream = list(stream)
    recs = _records(stream)
    n = len(recs) or 1
    lines = [item if isinstance(item, (str, bytes)) else json.dumps(item)
             for item in stream]
    nbytes = sum(len(ln if isinstance(ln, bytes) else ln.encode()) + 1
                 for ln in lines)
    agents = {(r.get("plan"), r["agent"].get("id"))
              for r in recs if isinstance(r.get("agent"), dict)}
    plans = {r.get("plan") for r in recs}
    model_a, model_b = token_models(recs)
    total_a = {k: sum(v[k] for v in model_a.values()) for k in TOKEN_KEYS}
    total_b = {k: sum(v[k] for v in model_b.values()) for k in TOKEN_KEYS}
    return {
        "events": len(recs),
        "tokens_frac": sum(1 for r in recs if r.get("stage") == "tokens") / n,
        "quiet_frac": sum(1 for r in recs if r.get("quiet")) / n,
        "agent_frac": sum(1 for r in recs if "agent" in r) / n,
        "rollups": sum(1 for r in recs
                       if r.get("stage") == "tokens" and "agent" not in r),
        "mean_bytes": nbytes / n,
        "bytes": nbytes,
        "agents": len(agents),
        "plans": len(plans),
        "model_a": model_a, "model_b": model_b,
        "total_a": total_a, "total_b": total_b,
        "tokens_exact": total_a == total_b and model_a == model_b,
    }


def _self_check() -> int:
    ok = True
    for n in (200, 2_000, 12_000):
        st = stream_stats(make_stream(n))
        exact = st["tokens_exact"]
        ok = ok and st["events"] == n and exact
        print(f"n={n:>6}  tokens={st['tokens_frac']:.3f} quiet={st['quiet_frac']:.3f} "
              f"agent={st['agent_frac']:.3f} mean={st['mean_bytes']:.0f}B "
              f"agents={st['agents']} plans={st['plans']} rollups={st['rollups']} "
              f"GD-C exact={exact}")
    print("ok" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] in ("--self-check", "-c"):
        raise SystemExit(_self_check())
    if not args or args[0] in ("-h", "--help"):
        raise SystemExit("usage: gen_stream.py <n_events> [seed] | --self-check")
    n_events = int(args[0])
    seed = int(args[1]) if len(args) > 1 else 20260727
    for line in make_stream(n_events, seed=seed):
        print(line)
