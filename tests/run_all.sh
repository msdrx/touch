#!/usr/bin/env bash
# Touch test runner (R-22). Stdlib only: no pytest, no plugins, no runner
# library — every test file is a standalone executable that exits non-zero on
# failure, and this script is just the ordered loop over them (D12/STACK-16).
#
# It runs BOTH suites, because Touch inherits the monitoring module's substrate
# (GD-20) and a green Touch suite over a red monitoring suite would be a lie:
#
#   tests/test_*.py                              — Touch's own
#   tests/monitoring/test_*.py                   — the module's (GD-U6: the
#                                                  module's dev-only material
#                                                  lives OUTSIDE the payload)
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
# SKIPS ARE REPORTED, not swallowed. Several suites legitimately skip when
# something they read is absent — no mongod, no node, and above all the
# gitignored run history under `.claude/local-orchestrators/` and the 8 MB
# fixture corpus, neither of which exists in a clean checkout or a packaged
# copy. Green must therefore never quietly mean "the files vanished", so this
# runner counts each file's printed skip lines and prints them as a line-item
# next to the pass/fail totals. The wire convention every test file follows is
# one line per skipped check, `skip`/`SKIP` first on the line after optional
# indent; a trailing `skipped: <reason>` recap block is NOT counted (it would
# double-count the same skip).
#
# THE CLEAN-CHECKOUT GATE. A suite that is green only on the machine that wrote
# it is not a gate at all, so before any wide mechanical change (and before any
# release) run it over tracked bytes ONLY:
#
#   d=$(mktemp -d) && git archive HEAD | tar -x -C "$d" && (cd "$d" && \
#     tests/run_all.sh --keep-going)
#
# That tree has no `.git`, no `.claude/local-orchestrators/` and no untracked
# anything, which is what a packaged copy looks like, and — apart from `.git`,
# which a clone of course has — what a fresh clone looks like. That difference
# is exactly why the git-dependent guards are written as "is THIS tree the git
# checkout" rather than "do these files exist". Files that read the absent
# things SKIP there; nothing crashes.
#
# usage: tests/run_all.sh [--keep-going] [--list] [-h]
#   default        stop at the first failing file (fail fast)
#   --keep-going   run everything, then report every failure
#   --list         print the files that would run, in order
#
# exit status: 0 = all green, 1 = at least one file failed, 2 = bad usage.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MON="$REPO/tests/monitoring"
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
skipped=()
passed=0
skips_total=0
started=$SECONDS

# One capture file, reused: each file's output is streamed through `tee` (so a
# long suite still prints live) and re-read afterwards only to count skips.
# `pipefail` is already set above, so the pipeline's status is still python's.
capture="$(mktemp "${TMPDIR:-/tmp}/run_all.XXXXXX")"
trap 'rm -f "$capture"' EXIT

# One skip = one line whose first word is skip/SKIP. The `skipped:` recap that
# several files print at the end is deliberately NOT matched. `grep -a` because
# this sandbox's grep is ugrep, which stops at a NUL byte otherwise.
count_skips() {
    grep -ac -E '^[[:space:]]*[Ss][Kk][Ii][Pp]([[:space:]:.,-]|$)' "$1" || true
}

for f in "${files[@]}"; do
    rel="${f#"$REPO"/}"
    printf '=== %s\n' "$rel"
    t0=$SECONDS
    if (cd "$(dirname "$f")" && "$PY" "$(basename "$f")" 2>&1) | tee "$capture"; then
        passed=$((passed + 1))
        verdict=PASS
    else
        rc=$?
        failed+=("$rel")
        verdict=FAIL
    fi
    n=$(count_skips "$capture")
    if [ "${n:-0}" -gt 0 ]; then
        skipped+=("$rel:$n")
        skips_total=$((skips_total + n))
    fi
    if [ "$verdict" = PASS ]; then
        printf -- '--- PASS %s (%ss, %s skipped)\n\n' "$rel" "$((SECONDS - t0))" "${n:-0}"
    else
        printf -- '--- FAIL %s (rc=%s, %ss, %s skipped)\n\n' \
            "$rel" "$rc" "$((SECONDS - t0))" "${n:-0}"
        if [ "$keep_going" -eq 0 ]; then
            echo "run_all.sh: stopping at the first failure (--keep-going runs the rest)"
            break
        fi
    fi
done

printf '%s\n' "-----------------------------------------------------------------"
printf 'run_all.sh: %s passed, %s failed, %s skipped check(s), %s file(s) total, %ss\n' \
    "$passed" "${#failed[@]}" "$skips_total" "${#files[@]}" "$((SECONDS - started))"
if [ ${#skipped[@]} -gt 0 ]; then
    for s in "${skipped[@]}"; do echo "  SKIPPED: ${s%:*} (${s##*:} check(s))"; done
fi
if [ ${#failed[@]} -gt 0 ]; then
    for f in "${failed[@]}"; do echo "  FAILED: $f"; done
    exit 1
fi
