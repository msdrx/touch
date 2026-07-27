#!/usr/bin/env bash
# Deterministic status trace point for orchestrator monitoring.
# usage: status.sh <plan> <stage> <state> [detail...]
#   States: queued | running | done | failed | info | stale
#   Optional: ORCH_TITLE env var sets/updates the plan's display title.
# Appends one JSON event line to $ORCH_STATE_DIR/events.jsonl. ORCH_STATE_DIR
# MUST point at the task's state folder; when it is unset this falls back to the
# shared module directory (and warns on stderr), which keeps the module dir
# stateless only if you always set it — see monitoring.md ("state-dir authority").
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
# Create the state dir up front so a seed/first call before the task folder
# exists does not fail the redirect and silently drop the event (SHELL-6).
if ! mkdir -p "$STATE_DIR" 2>/dev/null; then
    echo "status.sh: cannot create state dir: $STATE_DIR" >&2
    exit 1
fi
# Surface a missing/erroring python3 (or a failed append) instead of failing
# open with zero feedback (SHELL-7).
if ! python3 - "$plan" "$stage" "$state" "$detail" <<'PY' >> "$STATE_DIR/events.jsonl"
import datetime
import json
import os
import sys

event = {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
    "plan": sys.argv[1],
    "stage": sys.argv[2],
    "state": sys.argv[3],
    "detail": sys.argv[4],
}
title = os.environ.get("ORCH_TITLE")
if title:
    event["title"] = title
print(json.dumps(event))
PY
then
    echo "status.sh: failed to append event to $STATE_DIR/events.jsonl (python3 missing or errored?)" >&2
    exit 1
fi
