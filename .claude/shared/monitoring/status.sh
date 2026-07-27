#!/usr/bin/env bash
# Deterministic status trace point for orchestrator monitoring.
# usage: status.sh <plan> <stage> <state> [detail...]
#   States: queued | running | done | failed | info | stale
#   Optional: ORCH_TITLE env var sets/updates the plan's display title.
#   Optional: ORCH_PLANS_TOTAL env var declares the run's expected TOTAL number
#             of plan cards (integer), so dashboards can show progress over all
#             plans instead of only the cards already seen in the stream.
# Appends one JSON event line to $ORCH_STATE_DIR/events.jsonl. ORCH_STATE_DIR
# MUST point at the task's state folder; when it is unset this falls back to the
# shared module directory (and warns on stderr), which keeps the module dir
# stateless only if you always set it — see monitoring.md ("state-dir authority").
# Write integrity (R-10): the append is flock'd (events.jsonl has several
# writers — every agent plus decision_watcher.py) and `detail` is capped at 1 KB
# at the writer (GD-11). Every line carries "w":"agent" so a reader never has to
# guess who wrote it (R-39).
# This is a BEST-EFFORT writer: an unknown state warns on stderr but still
# appends, because a monitoring call must never break an agent.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -n "${ORCH_STATE_DIR:-}" ]; then
    STATE_DIR="$ORCH_STATE_DIR"
else
    STATE_DIR="$DIR"
    echo "status.sh: ORCH_STATE_DIR unset; writing to shared module dir ($DIR) — set it to the task's state folder" >&2
fi
plan="${1:?plan}"; stage="${2:?stage}"; state="${3:?state}"; shift 3 || true
detail="${*:-}"
# Out-of-enum state: warn, never fail. The reader maps unknown states to "info"
# rather than dropping the event (RUNSTATE-16).
case "$state" in
    queued|running|done|failed|info|stale) ;;
    *) echo "status.sh: unknown state '$state' (expected queued|running|done|failed|info|stale); appending anyway" >&2 ;;
esac
# Create the state dir up front so a seed/first call before the task folder
# exists does not fail the redirect and silently drop the event (SHELL-6).
if ! mkdir -p "$STATE_DIR" 2>/dev/null; then
    echo "status.sh: cannot create state dir: $STATE_DIR" >&2
    exit 1
fi
# Surface a missing/erroring python3 (or a failed append) instead of failing
# open with zero feedback (SHELL-7). Python does the append itself (no shell
# redirect) so the write can hold an exclusive flock for exactly one line.
if ! python3 - "$plan" "$stage" "$state" "$detail" "$STATE_DIR/events.jsonl" <<'PY'
import datetime
import json
import os
import sys

try:  # POSIX only; append locking degrades to unlocked writes without it.
    # Hard-importing fcntl would make EVERY status call fail on a host that
    # lacks it — the opposite of this file's "a monitoring call must never break
    # an agent" contract, and a stricter rule than decision_watcher.emit's, which
    # writes unlocked in the same situation.
    import fcntl
except ImportError:
    fcntl = None

DETAIL_CAP = 1024  # GD-11: cap detail at 1 KB at the writer


def cap(detail):
    return detail if len(detail) <= DETAIL_CAP else detail[:DETAIL_CAP - 3] + "..."


event = {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
    "plan": sys.argv[1],
    "stage": sys.argv[2],
    "state": sys.argv[3],
    "detail": cap(sys.argv[4]),
    # R-39: writer attribution, additive to the five-key shape. Readers ignore
    # unknown keys, so streams written before this key stay readable.
    "w": "agent",
}
title = os.environ.get("ORCH_TITLE")
if title:
    event["title"] = title
# Declared plan-card total: additive like `title`, readers ignore it when
# absent. Best-effort by contract — a garbage value warns and is omitted,
# never fails the caller.
total = os.environ.get("ORCH_PLANS_TOTAL")
if total:
    try:
        event["plans_total"] = int(total)
    except ValueError:
        print("status.sh: ORCH_PLANS_TOTAL is not an integer: %s -- omitted" % total,
              file=sys.stderr)
line = json.dumps(event) + "\n"
with open(sys.argv[5], "a") as f:
    # One LOCK_EX'd write per event: events.jsonl has many concurrent writers
    # (every agent, plus decision_watcher.py). O_APPEND alone only makes writes
    # that fit in one atomic append safe; the lock is what keeps the guarantee
    # when a line does not (and what serializes writers on filesystems where
    # append atomicity is weaker) — R-10.
    if fcntl is not None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    try:
        f.write(line)
        f.flush()
    finally:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
PY
then
    echo "status.sh: failed to append event to $STATE_DIR/events.jsonl (python3 missing or errored?)" >&2
    exit 1
fi
