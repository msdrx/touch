#!/usr/bin/env bash
# Re-copy the PINNED trees into the shipping subtree `plugin/touch/` — item 07,
# GD-T2. Bash + coreutils only, no jq, no rsync (GD-21).
#
# GD-T2 splits the payload in two. Some trees are MOVED into `plugin/touch/`
# and are canonical there (`skills/`, `hooks/`) — this script never touches
# those. The rest stay canonical where this repo already lives with them and
# are PINNED: a byte-equal copy under `plugin/touch/`, re-made mechanically by
# this script and asserted by `tests/test_plugin_tree.py`, which fails on any
# drift. The reason is not taste: `aggregator/`, `touch-visual/`, `docs/` and
# `.claude/shared/monitoring/` are this repo's live product and its live-run
# substrate. Moving them would re-anchor ~26 tests and break the monitoring
# daemons of a run that is in flight.
#
# WHY AN EXPLICIT FILE LIST AND NOT `cp -r`
# -----------------------------------------
# The working tree is not the shipping set. `aggregator/` and
# `.claude/shared/monitoring/` both carry untracked `__pycache__/` right now;
# the monitoring directory also carries `tests/` and three dev-only scripts
# that GD-T2 puts on the never-ship list. A recursive copy would ship all of
# it, and `--plugin-url` zips have no git layer to filter it back out
# (PLUGIN-SPEC-12). So every pinned path is named below, one per line, and
# adding a module to a canonical tree is a deliberate edit here — which
# `tests/test_plugin_tree.py` enforces from the other side by failing when a
# canonical `aggregator/*.py` is missing from this list.
#
# Symlinking instead of copying is the trap this file exists to avoid: a
# symlink out of the plugin root survives a marketplace install and is SKIPPED
# under `--plugin-dir`, i.e. it breaks in exactly the loop a developer runs all
# day (PLUGIN-SPEC-12).
#
# usage: scripts/sync_plugin.sh [--check] [--list] [-h]
#   default   delete-then-copy every pinned path (the destination trees are
#             rebuilt from scratch, so a deleted canonical file disappears
#             from the payload instead of lingering)
#   --check   report drift and change nothing; exit 1 if any pinned path
#             differs, is missing, or if an UNLISTED file is sitting in one of
#             the destination trees (a `__pycache__/`, a hand-edit). Both
#             directions, so this is usable on its own as a pre-commit gate and
#             not merely as half of `tests/test_plugin_tree.py`.
#   --list    print `dest<TAB>src` (repo-relative) for every pinned path, one
#             per line. This is the machine-readable form of the pinned set;
#             `tests/test_plugin_tree.py` reads it rather than keeping a second
#             copy of the list that could drift from this one.
#
# exit status: 0 = done / no drift, 1 = drift (--check) or a missing source,
#              2 = bad usage.
set -uo pipefail

# `readlink -f` first: this file is the anchor for the one `rm -rf` below, and
# resolving the SYMLINK rather than its target would put $REPO somewhere other
# than the checkout when the script is reached through a link.
REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
PLUGIN="plugin/touch"

# --- the pinned set --------------------------------------------------------
# One entry per tree: "<dest-dir under plugin/touch>|<src-dir under repo>|<files>".
# Destination directories are REBUILT (rm -rf) on a sync, so nothing may be
# listed here whose destination also holds hand-written files.
TREES=(
    "aggregator|aggregator|__init__.py agents.py custom_state.py ingest.py legacy.py mirror.py mongo_store.py paths.py refs.py server.py sessions.py store.py tailer.py ws.py"
    # The directory name `touch-visual/` is kept verbatim in the payload
    # (GD-T2): a byte-equal copy and an unchanged assets resolver beat a
    # cosmetic rename to `visual/`.
    "touch-visual|touch-visual|app.js index.html style.css"
    "docs|docs|control-semantics.md mongo.md"
    # FIVE CORE FILES ONLY. `tests/` and the three dev-only scripts
    # (gen_stream.py, test_perf_work.py, test_ws_e2e.py) are on GD-T2's
    # never-ship list — four of the payload's five PII-slug carriers live
    # there. This subset is why the equality test does NOT reverse-check this
    # tree the way it does the other three.
    "shared/monitoring|.claude/shared/monitoring|decision_watcher.py monitor.html monitor_server.py monitoring.md status.sh"
)

# Single-file pins that belong to no tree. `LICENSE` is one file in two places
# by necessity — PLUGIN-SPEC-17 wants it at the repo root AND the plugin root,
# and a `"license": "MIT"` manifest field with a drifted or absent file is a
# false claim. Pinning it here means the drift is impossible rather than
# merely unlikely.
SINGLES=(
    "LICENSE|LICENSE"
)

mode=sync
while [ $# -gt 0 ]; do
    case "$1" in
        --check|-c) mode=check ;;
        --list|-l)  mode=list ;;
        -h|--help)  sed -n '2,${/^#/!q;p;}' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "sync_plugin.sh: unknown argument '$1' (try -h)" >&2; exit 2 ;;
    esac
    shift
done

# Emit `dest<TAB>src`, repo-relative, for every pinned path. Every mode below
# consumes this one function, so `--list` cannot describe a different set than
# a sync copies.
pinned_pairs() {
    local entry dest src files f
    for entry in "${TREES[@]}"; do
        dest="${entry%%|*}"
        src="${entry#*|}"; files="${src#*|}"; src="${src%%|*}"
        for f in $files; do
            printf '%s/%s/%s\t%s/%s\n' "$PLUGIN" "$dest" "$f" "$src" "$f"
        done
    done
    for entry in "${SINGLES[@]}"; do
        printf '%s/%s\t%s\n' "$PLUGIN" "${entry%%|*}" "${entry#*|}"
    done
}

if [ "$mode" = list ]; then
    pinned_pairs
    exit 0
fi

rc=0

if [ "$mode" = check ]; then
    drift=0
    listed=""
    while IFS=$'\t' read -r dest src; do
        listed="$listed$dest"$'\n'
        if [ ! -f "$REPO/$src" ]; then
            echo "MISSING SOURCE: $src" >&2; drift=$((drift + 1)); continue
        fi
        if [ ! -f "$REPO/$dest" ]; then
            echo "MISSING: $dest"; drift=$((drift + 1)); continue
        fi
        if ! cmp -s "$REPO/$src" "$REPO/$dest"; then
            echo "DRIFTED: $dest (differs from $src)"; drift=$((drift + 1))
        fi
    done < <(pinned_pairs)
    if [ -z "$listed" ]; then
        echo "sync_plugin.sh: the pinned set came out empty — refusing to report clean" >&2
        exit 1
    fi
    # The other direction. Equality alone would report "clean" with a
    # `plugin/touch/aggregator/__pycache__/` sitting in the payload — the exact
    # case the explicit file list above exists to prevent — so --check walks
    # each destination tree too. Only the TREES destinations are walked:
    # SINGLES land beside files owned by other sub-plans (README, bin/), which
    # are none of this script's business.
    for entry in "${TREES[@]}"; do
        target="$REPO/$PLUGIN/${entry%%|*}"
        [ -d "$target" ] || continue
        while IFS= read -r found; do
            rel="${found#"$REPO/"}"
            case $'\n'"$listed" in
                *$'\n'"$rel"$'\n'*) ;;
                *) echo "STRAY: $rel (in the payload, named by nobody)"
                   drift=$((drift + 1)) ;;
            esac
        done < <(find "$target" -type f)
    done
    if [ "$drift" -gt 0 ]; then
        echo "sync_plugin.sh: $drift pinned path(s) out of date — run scripts/sync_plugin.sh" >&2
        exit 1
    fi
    echo "sync_plugin.sh: every pinned path is byte-equal to its canonical source, and no unlisted file is in $PLUGIN/"
    exit 0
fi

# --- sync: delete then copy ------------------------------------------------
# The destination trees are removed first so that a file deleted from a
# canonical tree also leaves the payload. `rm -rf` on a computed path gets a
# containment assertion rather than trust.
for entry in "${TREES[@]}"; do
    dest="${entry%%|*}"
    target="$REPO/$PLUGIN/$dest"
    # A literal prefix test, not a glob: `case` would interpolate $REPO into a
    # pattern, and a checkout path containing `[`, `*` or `?` is exactly the
    # case where the guard on the file's one `rm -rf` must not get creative.
    if [ "${target#"$REPO/$PLUGIN/"}" = "$target" ] || [ "$target" = "$REPO/$PLUGIN/" ]; then
        echo "sync_plugin.sh: refusing to remove '$target' (outside $PLUGIN)" >&2
        exit 1
    fi
    rm -rf "$target" || { echo "sync_plugin.sh: could not remove '$target'" >&2; exit 1; }
    # Unchecked, a failed mkdir turns into N failed copies and a final
    # "synced 0 pinned path(s)" line that reads like success.
    mkdir -p "$target" || { echo "sync_plugin.sh: could not create '$target'" >&2; exit 1; }
done

copied=0
while IFS=$'\t' read -r dest src; do
    if [ ! -f "$REPO/$src" ]; then
        echo "sync_plugin.sh: missing canonical source '$src'" >&2
        rc=1; continue
    fi
    mkdir -p "$(dirname "$REPO/$dest")"
    # -p keeps the mode: `status.sh` is an executable the payload's bin/
    # wrappers exec, and a copy that lost its exec bit fails only at run time
    # on a consumer's machine.
    if ! cp -p "$REPO/$src" "$REPO/$dest"; then
        echo "sync_plugin.sh: failed to copy '$src'" >&2
        rc=1; continue
    fi
    copied=$((copied + 1))
done < <(pinned_pairs)

# A `pinned_pairs` that fails inside the process substitution yields an empty
# loop and no complaint, which would print "synced 0" over freshly emptied
# destination trees. Zero copies is never a success here.
if [ "$copied" -eq 0 ]; then
    echo "sync_plugin.sh: copied nothing — the pinned set is empty or unreadable" >&2
    exit 1
fi
echo "sync_plugin.sh: synced $copied pinned path(s) into $PLUGIN/"
exit "$rc"
