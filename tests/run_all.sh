#!/usr/bin/env bash
# Touch test runner (R-22). Stdlib only: no pytest, no plugins, no runner
# library — every test file is a standalone executable that exits non-zero on
# failure, and this script is just the ordered loop over them (D12/STACK-16).
#
# It runs BOTH suites, because Touch inherits the monitoring module's substrate
# (GD-20) and a green Touch suite over a red monitoring suite would be a lie:
#
#   tests/test_*.py                              — Touch's own
#   .claude/shared/monitoring/tests/test_*.py    — the module's
#
# Registration is by GLOB, not by a hand-maintained list: dropping a
# `test_<thing>.py` into either directory registers it, and nothing else in
# this script has to change. The corollary is the naming rule — a test HELPER
# must not be named `test_*.py` or it will be executed as a suite. That is why
# the monitoring module's stream generator is `tests/gen_stream.py`
# (imported by `test_ws_e2e.py`, never run on its own; it has a `--self-check`
# mode for humans).
#
# Each file runs with its own directory as cwd (the monitoring tests resolve
# fixtures relative to themselves) and with PYTHONDONTWRITEBYTECODE set, so a
# test run never litters __pycache__ into the tree it is asserting about.
#
# usage: tests/run_all.sh [--keep-going] [--list] [-h]
#   default        stop at the first failing file (fail fast)
#   --keep-going   run everything, then report every failure
#   --list         print the files that would run, in order
#
# exit status: 0 = all green, 1 = at least one file failed, 2 = bad usage.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MON="$REPO/.claude/shared/monitoring/tests"
export PYTHONDONTWRITEBYTECODE=1
PY="${PYTHON:-python3}"

keep_going=0
list_only=0
while [ $# -gt 0 ]; do
    case "$1" in
        --keep-going|-k) keep_going=1 ;;
        --list|-l) list_only=1 ;;
        # print the whole header block, however long it grows: stop at the
        # first non-comment line rather than at a hard-coded line number that
        # silently truncates the usage text the next time this file is edited.
        -h|--help) sed -n '2,${/^#/!q;p;}' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "run_all.sh: unknown argument '$1' (try -h)" >&2; exit 2 ;;
    esac
    shift
done

# Collect: Touch's tests first (they are the fast unit layer), then the
# monitoring module's. `nullglob` so an empty suite is empty, not a literal
# glob — R-22 requires an empty suite to run green.
shopt -s nullglob
files=("$REPO"/tests/test_*.py "$MON"/test_*.py)
shopt -u nullglob

if [ "$list_only" -eq 1 ]; then
    for f in "${files[@]}"; do echo "${f#"$REPO"/}"; done
    exit 0
fi

if [ ${#files[@]} -eq 0 ]; then
    echo "run_all.sh: no test files found (empty suite) — green by definition"
    exit 0
fi

if ! command -v "$PY" >/dev/null 2>&1; then
    echo "run_all.sh: $PY not found; set PYTHON=... to point at an interpreter" >&2
    exit 2
fi

failed=()
passed=0
started=$SECONDS
for f in "${files[@]}"; do
    rel="${f#"$REPO"/}"
    printf '=== %s\n' "$rel"
    t0=$SECONDS
    if (cd "$(dirname "$f")" && "$PY" "$(basename "$f")"); then
        passed=$((passed + 1))
        printf -- '--- PASS %s (%ss)\n\n' "$rel" "$((SECONDS - t0))"
    else
        rc=$?
        failed+=("$rel")
        printf -- '--- FAIL %s (rc=%s, %ss)\n\n' "$rel" "$rc" "$((SECONDS - t0))"
        if [ "$keep_going" -eq 0 ]; then
            echo "run_all.sh: stopping at the first failure (--keep-going runs the rest)"
            break
        fi
    fi
done

printf '%s\n' "-----------------------------------------------------------------"
printf 'run_all.sh: %s passed, %s failed, %s file(s) total, %ss\n' \
    "$passed" "${#failed[@]}" "${#files[@]}" "$((SECONDS - started))"
if [ ${#failed[@]} -gt 0 ]; then
    for f in "${failed[@]}"; do echo "  FAILED: $f"; done
    exit 1
fi
