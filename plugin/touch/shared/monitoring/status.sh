#!/usr/bin/env bash
# Deterministic status trace point for orchestrator monitoring.
# usage: status.sh <plan> <stage> <state> [detail...]
#   States: queued | running | done | failed | info | stale
#   Optional: ORCH_TITLE env var sets/updates the plan's display title.
#   Optional: ORCH_PLANS_TOTAL env var declares the run's expected TOTAL number
#             of plan cards (integer), so dashboards can show progress over all
#             plans instead of only the cards already seen in the stream.
#   Optional: ORCH_ROSTER env var names a FILE holding the run's planned
#             sub-plan list, one `<id> — <title>` entry per line, which is
#             attached to the event as the `roster` array (GD-D11). It is a
#             PATH, never inlined JSON: a roster is unbounded in principle and
#             an argv is not the place to discover that. Bounded HERE, at the
#             writer — ROSTER_MAX entries, ROSTER_ENTRY_CAP chars each, the same
#             bounds monitor.html applies on the way in — so no reader has to be
#             the first line of defence. Readers honor `roster` only on an
#             `orchestrator`-card event; this writer attaches what it is told to
#             attach and never inspects the plan id.
# Appends one JSON event line to $ORCH_STATE_DIR/events.jsonl. ORCH_STATE_DIR
# MUST point at the task's state folder. When it is unset this resolves the
# project's tasks root ($ORCH_TASKS_ROOT > $CLAUDE_PROJECT_DIR > cwd walk-up to
# a .claude/ marker — three rungs, the same order both daemons use) and writes
# to the newest task folder there, warning loudly. When THAT fails too it exits
# 2 rather than spooling into the shared module directory: a spool nobody reads
# is data loss with extra steps, and in a packaged copy the module dir is a
# version-stamped cache that gets swept.
# Write integrity (R-10): the append is flock'd (events.jsonl has several
# writers — every agent plus decision_watcher.py) and `detail` is capped at 1 KB
# at the writer (GD-11). Every line carries "w":"agent" so a reader never has to
# guess who wrote it (R-39).
# This is a BEST-EFFORT writer: an unknown state warns on stderr but still
# appends, because a monitoring call must never break an agent.
set -u

# Both walk-up loops below take the parent with `p="${d%/*}"; [ -z "$p" ] && p=/`
# rather than `p="$(dirname "$d")"`. This is the hottest script in the module —
# every agent calls it several times per stage — and each loop runs to "/", so
# dirname cost a subshell + exec PER ANCESTOR LEVEL on every single status call
# for a string operation bash already does. Both loops start from an absolute
# path ($PWD, and a $1 normalised to absolute), so the empty result can only
# mean "d was /x", whose parent is "/".

# The tasks-root resolver, in bash: the same THREE rungs as the daemons'
# resolve_tasks_root(), which this must stay in step with (G10). Rungs 2 and 3
# join `.touch/local-orchestrators`, Touch's own gitignored state tree. Rung 3's
# MARKER dir and the STATE dir are deliberately DIFFERENT: `.claude/` is what
# marks a Claude Code project (`.touch/` is created by Touch and ignored, so it
# cannot mark one), and the run history then lives under `.touch/`. The former
# FOURTH rung — a module-relative `$DIR/../../local-orchestrators` sibling
# lookup — is DELETED: after GD-U1 it had nothing to resolve to, and in a
# packaged copy it would glob sibling plugins. Prints the root on stdout, or
# returns 1 (the caller then refuses rather than guessing).
resolve_tasks_root() {
    if [ -n "${ORCH_TASKS_ROOT:-}" ]; then
        printf '%s\n' "$ORCH_TASKS_ROOT"; return 0
    fi
    if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
        printf '%s\n' "$CLAUDE_PROJECT_DIR/.touch/local-orchestrators"; return 0
    fi
    local d p
    d="$PWD"
    while :; do
        if [ -d "$d/.claude" ]; then
            printf '%s\n' "$d/.touch/local-orchestrators"; return 0
        fi
        p="${d%/*}"; [ -z "$p" ] && p=/
        [ "$p" = "$d" ] && break
        d="$p"
    done
    return 1
}

# Never write into an installed plugin: the plugin root is version-stamped,
# re-copied on update and swept ~14 days later. Walks the string, so it answers
# for a state dir that does not exist yet.
in_plugin_cache() {
    local d p
    d="$1"
    case "$d" in /*) ;; *) d="$PWD/$d" ;; esac
    while :; do
        [ -f "$d/.claude-plugin/plugin.json" ] && return 0
        p="${d%/*}"; [ -z "$p" ] && p=/
        [ "$p" = "$d" ] && return 1
        d="$p"
    done
}

if [ -n "${ORCH_STATE_DIR:-}" ]; then
    STATE_DIR="$ORCH_STATE_DIR"
else
    # No module-dir spool. Try the project's tasks root, newest task folder that
    # already has a stream; failing that, refuse rather than write somewhere
    # nobody will ever read.
    STATE_DIR=""
    if TASKS_ROOT="$(resolve_tasks_root)"; then
        newest="$(ls -t "$TASKS_ROOT"/*/events.jsonl 2>/dev/null | head -n 1)"
        [ -n "$newest" ] && STATE_DIR="$(dirname "$newest")"
    fi
    if [ -z "$STATE_DIR" ]; then
        echo "status.sh: ORCH_STATE_DIR unset and no task folder could be resolved; set ORCH_STATE_DIR to the task's state folder (or ORCH_TASKS_ROOT / CLAUDE_PROJECT_DIR to the project)" >&2
        exit 2
    fi
    echo "status.sh: ORCH_STATE_DIR unset; falling back to newest task folder ($STATE_DIR) — set it to the task's state folder" >&2
fi
if in_plugin_cache "$STATE_DIR"; then
    echo "status.sh: refusing to write into a plugin cache ($STATE_DIR); set ORCH_STATE_DIR to a task folder in your project" >&2
    exit 2
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
#: Roster bounds, GD-D11. They are monitor.html's own numbers (200 entries,
#: 300 chars) deliberately: bounding at the writer as well as at the reader is
#: what keeps a runaway roster out of the append-only file in the first place,
#: and a stream nobody has to sanitize twice is the point of `w` attribution.
ROSTER_MAX = 200
ROSTER_ENTRY_CAP = 300
#: Read ceiling for the roster FILE itself, in BYTES: past this it is not a
#: roster and is not read into memory. It clears the largest roster the two
#: caps above can legitimately produce -- 200 x 300 is 60 K CHARACTERS, and a
#: character is up to 4 bytes in UTF-8 (every entry this repo writes carries an
#: em dash, at 3), so a ceiling under ~240 KB would silently cut a valid
#: maximal roster. Reading a quarter-megabyte once per roster event is free;
#: dropping entries a caller was entitled to send is not.
ROSTER_FILE_CAP = 256 * 1024


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
# The declared sub-plan roster (GD-D11): a FILE path, never inlined JSON, so the
# writers of it (touch-run start from the run spec, cycle_reporter at the divide
# close) hand over a path and this reads it. Best-effort by the same contract as
# every key above -- an unreadable or empty file warns and omits `roster`, and
# never fails the caller, because a monitoring call must never break an agent.
roster_path = os.environ.get("ORCH_ROSTER")
if roster_path:
    try:
        # BINARY, so the cap counts the bytes it claims to count: a text read
        # counts characters, and every entry this repo writes carries an em
        # dash. Decoding after the slice can split a multi-byte character in
        # half, which is what errors="replace" is for.
        with open(roster_path, "rb") as rf:
            raw = rf.read(ROSTER_FILE_CAP + 1)
    except OSError as exc:
        print("status.sh: ORCH_ROSTER unreadable (%s) -- roster omitted" % exc,
              file=sys.stderr)
    else:
        oversize = len(raw) > ROSTER_FILE_CAP
        blob = raw.decode("utf-8", "replace")
        lines = blob.splitlines()
        if oversize:
            print("status.sh: ORCH_ROSTER is larger than %d bytes -- reading the "
                  "head only" % ROSTER_FILE_CAP, file=sys.stderr)
            if lines and not blob.endswith(("\n", "\r")):
                # The read stopped mid-ENTRY, so the last line is a fragment.
                # Drop it rather than ship half a title -- but ONLY then: a file
                # whose last entry ends on the boundary ends with its newline,
                # and dropping there loses a complete entry for nothing.
                lines = lines[:-1]
        entries = [ln.strip()[:ROSTER_ENTRY_CAP] for ln in lines if ln.strip()]
        if len(entries) > ROSTER_MAX:
            print("status.sh: roster of %d entries capped at %d"
                  % (len(entries), ROSTER_MAX), file=sys.stderr)
            entries = entries[:ROSTER_MAX]
        if entries:
            event["roster"] = entries
        else:
            print("status.sh: ORCH_ROSTER named an empty file (%s) -- roster "
                  "omitted" % roster_path, file=sys.stderr)
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
