#!/usr/bin/env bash
# Cut a Touch release — item 12, GD-T3/GD-T7/GD-T9 (DISTRIBUTION-2/5/12,
# PRIOR-AUDIT-1/2/12, PLUGIN-SPEC-1).
#
# THIS SCRIPT IS THE CHECKLIST. There is deliberately no RELEASE.md: a
# procedure written down twice is a procedure that drifts, and the half nobody
# executes is always the prose half (DISTRIBUTION-12). Everything a release
# needs is either a gate below or a line the preflight makes you confirm.
#
# bash + `git` + `python3` stdlib only. No `jq` — CLAUDE.md's statusline
# exception does not extend to Touch's own scripts (GD-21).
#
# On length: the plan sketched "~50 lines" and this is several times that. The
# executable part is close to the sketch; the rest is the checklist itself,
# which GD-T9 says lives here and nowhere else, plus one comment per gate
# saying which failure it was written for. A gate whose reason is not written
# down is the gate a later reader deletes as redundant.
#
# WHAT SHIPS IS WHAT GIT HAS
# --------------------------
# Step 5 builds the payload with `git archive HEAD:plugin/touch`, i.e. from the
# LAST COMMIT, and steps 3 and 4 read the version and the changelog out of that
# same `HEAD:` tree rather than off disk. An uncommitted fix in your working
# tree does NOT ship, and the release otherwise looks perfectly successful — so
# step 1 refuses a dirty tree outright rather than letting you find out later,
# and it counts UNTRACKED files as dirty, because a new payload file nobody
# `git add`ed is the version of this accident that leaves no other trace. The
# only `cp` in this file
# (step 8) copies out of that git-built stage, never out of the working tree;
# `cp -r` of `plugin/touch/` is exactly the shortcut this file exists to
# prevent (probe E6 shipped a tracked `.touch/leak.txt` and a `__pycache__/`
# that way).
#
# THE DEV REPO IS NEVER AN INSTALL SOURCE
# ---------------------------------------
# `/plugin marketplace add owner/repo` CLONES HISTORY. This repository's
# history is permanently contaminated (a burned token blob, credentialed
# `mongodb://` URIs, hundreds of deleted run-transcript paths) and no checkout
# trick — `--sparse`, `git-subdir` — is a privacy boundary; both limit the
# checkout, not the objects. Releases therefore go to a SEPARATE repo created
# EMPTY (`msdrx/touch-plugin`), never a fork, never a remote of this one. Step
# 7 proves that property about the other repo before anything is pushed into
# it, because no unit test in this repo can (DISTRIBUTION-2).
#
# usage: scripts/release.sh [--check] [--release-clone <path>] [--tag-push] [-h]
#   default          the real thing: every gate is fatal on the spot, and a
#                    green run commits and pushes the release clone.
#   --check          dry run. Stops after step 7, touches no release repo (a
#                    throwaway `git init` stands in when you name none), never
#                    commits, never pushes. Unlike the real run it does NOT
#                    stop at the first red gate — it runs them all and reports
#                    every failure, then exits non-zero if any failed. That is
#                    the same bargain `tests/run_all.sh --keep-going` makes:
#                    you want the whole list before you start fixing.
#   --release-clone  path to a local clone of the release repo. Required for a
#                    real run; optional under --check.
#   --tag-push       also `claude plugin tag --push` after a successful
#                    release. Optional and rarely wanted: the `{name}--v{ver}`
#                    tag matters only for plugin *dependency* constraints, and
#                    Touch has none. The dry-run tag check (step 10) runs
#                    either way, for its version-agreement and dirty checks.
#
# environment:
#   RELEASE_CONFIRM=yes            answer the preflight non-interactively (for
#                                  a run whose stdin is not a terminal).
#   RELEASE_COMMITS_EXPECTED=<n>   enforce step 7's commit count instead of
#                                  printing it as an advisory. <n> is the
#                                  release repo's initial commit plus one per
#                                  release already published — i.e.
#                                  releases-so-far + 1, measured BEFORE this
#                                  run commits. It therefore grows by one with
#                                  every published release: bump it after each
#                                  one, or the next run fails step 7 on a
#                                  perfectly healthy repo. Step 9 prints the
#                                  value to use next time.
#
# exit status: 0 = done / dry run clean, 1 = a gate failed, 2 = bad usage.

set -uo pipefail

# `readlink -f` first: resolving the symlink rather than its target keeps $REPO
# on the checkout when this script is reached through a link on $PATH.
REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
PLUGIN="plugin/touch"
MANIFEST=".claude-plugin/plugin.json"
MARKETPLACE=".claude-plugin/marketplace.json"

# The commit whose TREE still carries the burned `mytok2` blob. Named here as
# an anchor for the preflight's first line, not as a count: every quantitative
# claim about this repo's contamination has drifted at least once, so this file
# cites the COMMANDS that measure it and lets you read today's answer
# (PRIOR-AUDIT-12).
TOKEN_TIP="f3b10a7"

mode=real
REL=""
tag_push=no
while [ $# -gt 0 ]; do
    case "$1" in
        --check|-c)      mode=check ;;
        --release-clone) shift; [ $# -gt 0 ] || { echo "release.sh: --release-clone needs a path" >&2; exit 2; }; REL="$1" ;;
        --tag-push)      tag_push=yes ;;
        -h|--help)       sed -n '2,${/^#/!q;p;}' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "release.sh: unknown argument '$1' (try -h)" >&2; exit 2 ;;
    esac
    shift
done

STAGE=""
TMPREL=""
cleanup() {
    [ -n "$STAGE" ] && rm -rf "$STAGE"
    [ -n "$TMPREL" ] && rm -rf "$TMPREL"
    return 0
}
trap cleanup EXIT

failures=0
step_no=0

# The step number is passed in rather than counted, so the banner a run prints
# and the section comments in this file are the same numbering — a reader
# comparing the two is checking the procedure, not an off-by-one.
step() { step_no="$1"; printf '\n== %s. %s\n' "$1" "$2"; }
ok()   { printf '   ok: %s\n' "$1"; }
note() { printf '   .. %s\n' "$1"; }
skip() { printf '   SKIP: %s\n' "$1"; }

# Reduce a stream of remote URLs to `host/owner/repo`, one per line. Scheme,
# `user@`, an explicit SSH port, the SSH `host:owner` colon, a `.git` suffix, a
# trailing slash and case are all spelling, and step 7's dev-remote gate must
# compare IDENTITY: `https://github.com/msdrx/touch.git`,
# `git@github.com:msdrx/touch` and `ssh://git@github.com:22/msdrx/touch` are one
# repository, and it is precisely the accident of adding the dev repo back under
# another of its forms that the gate exists to catch. The port rule runs BEFORE
# the colon-to-slash rule, or `:22/` would become a path segment and the two
# spellings still would not compare equal. `sed` and `tr` read their whole
# input, so this is safe at the end of a pipeline under `pipefail`.
norm_urls() {
    sed -e 's#^[a-zA-Z][a-zA-Z0-9+.-]*://##' -e 's#^[^@/]*@##' \
        -e 's#^\([^/:]*\):[0-9][0-9]*/#\1/#' -e 's#:#/#' \
        -e 's#\.git$##' -e 's#/*$##' | tr 'A-Z' 'a-z'
}

# The difference between the two modes lives here and nowhere else: a real run
# stops at the first red gate (a half-published release is worse than none),
# a --check run records it and keeps going.
fail() {
    failures=$((failures + 1))
    printf '   FAIL: %s\n' "$1" >&2
    if [ "$mode" = real ]; then
        printf '\nrelease.sh: aborted at step %d — nothing was published.\n' "$step_no" >&2
        exit 1
    fi
}

cd "$REPO" || { echo "release.sh: cannot cd to $REPO" >&2; exit 2; }

# --- 0. preflight: the part no script can verify ---------------------------
# Five things stand between this repository and a safe publish that neither a
# test nor this script can check from in here: two of them are about the OTHER
# repo, one is about a database, one is about GitHub, and one has to happen on
# a machine where `claude plugin install` may write under ~/.claude (which this
# repo's law forbids doing here — GD-T7). So they are printed, with the command
# that measures each, and confirmed by a human.
step 0 "Preflight — the manual checklist"
cat <<'CHECKLIST'
   Confirm each of these has actually been done. Commands are given so you can
   re-measure rather than trust a number written down some other day.

   (a) The dev repo's tip no longer carries the burned token blob, i.e. the
       local commits are pushed:
           git rev-parse origin/main        # must NOT be the token commit
   (b) The decision about this repo's HISTORY is executed — purge with
       `git filter-repo`, or make the dev repo private and treat the release
       repo as the only public artifact. Either way, the check is:
           git rev-list --all --objects | grep -i mytok
           git grep -aIhE 'mongodb://[^/[:space:]"<]+:[^@[:space:]"<]+@' $(git rev-list --all)
   (c) The MongoDB password that appears in those history blobs is rotated.
       Treat every credential this repo has ever seen as burned.
   (d) The release repo exists, was created EMPTY, and is not a fork of or a
       remote of this one. Step 7 re-proves the last part; the first two are
       yours.
   (e) Install-path verification was done ONCE on your own machine: install
       from the marketplace, confirm the bin/ wrappers kept their exec bits
       through the cache copy, and run `touch-selfcheck`. It cannot happen
       here — every `claude plugin install` writes under ~/.claude.
CHECKLIST
note "local view of (a): origin/main = $(git rev-parse --short origin/main 2>/dev/null || echo '<no origin/main ref here>') (token commit: $TOKEN_TIP; a local ref can be stale — check on the host)"
# NOT `git rev-list … | grep -q`. Under `pipefail` a `-q` consumer exits on the
# first hit, git dies of SIGPIPE, and the pipeline returns 141 — so the shape
# reports "no match" exactly when it MATCHED, and only once the history is big
# enough for git to still be writing. Collect the hits into a variable and test
# that instead; `grep -a` because this sandbox's grep is ugrep and treats a
# NUL-bearing stream as binary.
tokenblobs="$(git rev-list --all --objects 2>/dev/null | grep -ai mytok || true)"
if [ -n "$tokenblobs" ]; then
    note "local view of (b): the token blob IS still reachable in this clone's history"
else
    note "local view of (b): no token-named blob reachable in this clone's history"
fi
if [ "$mode" = check ]; then
    skip "confirmation not requested — --check is a dry run and publishes nothing"
elif [ "${RELEASE_CONFIRM:-}" = yes ]; then
    ok "confirmed by RELEASE_CONFIRM=yes"
else
    if [ ! -t 0 ]; then
        fail "preflight needs a terminal — re-run interactively or set RELEASE_CONFIRM=yes"
    fi
    printf '   Type "yes" to confirm all five: '
    read -r answer
    [ "$answer" = yes ] || fail "preflight not confirmed"
fi

# --- 1. dirty-tree refusal --------------------------------------------------
# `git status --porcelain`, NOT `git diff --quiet`: diff reports modifications
# to tracked files and nothing else, so a brand-new `plugin/touch/bin/…` that
# was never `git add`ed leaves the tree "clean" by that definition while step 5
# — which builds from HEAD — silently drops it. A missing new file is the
# likeliest release accident there is, and it is invisible downstream: step 2
# runs the suite against the WORKING TREE, where the file IS present and every
# payload test passes, so the run scores green while shipping a payload without
# it. Untracked-blind is the one thing this gate cannot afford to be.
step 1 "Dirty-tree refusal"
dirty="$(git status --porcelain 2>/dev/null)"
if [ -z "$dirty" ]; then
    ok "working tree and index are clean (tracked, staged and untracked)"
else
    fail "uncommitted or untracked changes — step 5 builds from HEAD, so they would silently not ship"
    # `tr` then `cut`, never `head`: an early-exiting consumer under pipefail is
    # the same SIGPIPE trap the preflight above documents.
    note "first entries: $(printf '%s' "$dirty" | tr '\n' ';' | cut -c1-160)"
fi

# --- 2. the suite -----------------------------------------------------------
# Includes tests/test_package.py (the payload gate) and the pinned-copy byte
# equality test, so this one line is also "the subtree matches its canonical
# sources" and "the payload carries no secret, cache or fixture".
step 2 "tests/run_all.sh"
if [ ! -x tests/run_all.sh ]; then
    fail "tests/run_all.sh is missing or not executable"
elif tests/run_all.sh; then
    ok "the full suite is green"
else
    fail "the suite is red — a release is not cut over a red suite"
fi

# --- 3. the version ---------------------------------------------------------
# `plugin.json` is the ONLY place Touch declares a version (GD-T9): the
# marketplace entry deliberately carries none, because version resolution is
# first-set-wins and setting both is a silent trap (DISTRIBUTION-5).
#
# Read out of `HEAD:`, not off disk. This file's headline is WHAT SHIPS IS WHAT
# GIT HAS, and a version or a changelog entry read from the working tree is a
# statement about a file that step 5 will not stage — the two could disagree
# and the run would still be green. `HEAD:$PLUGIN/…` is byte-for-byte the tree
# step 5 unpacks, so steps 3, 4, 5 and 6 all describe one artifact.
step 3 "Version from HEAD:$PLUGIN/$MANIFEST"
UNREADABLE="0.0.0-unreadable"
manifest_json="$(git show "HEAD:$PLUGIN/$MANIFEST" 2>/dev/null)"
# A here-string, not a pipe: `python3` exits early on malformed JSON, and an
# early-exiting consumer is how the preflight's advisory got inverted.
#
# `.get("version")` behind an `isinstance`, not `["version"]`: a manifest that
# carries `"version": null` makes `print()` emit the four characters `None`,
# which is a NON-EMPTY string, so the `-z` test below passes, step 4 goes
# hunting for a `## None` heading and the run reports the CHANGELOG as the
# fault. One fault, one diagnosis, is this file's rule everywhere else (steps 4
# and 6 both skip rather than cascade), and it has to hold here too — this is
# the step the cascade starts from. Anything that is not a string prints
# nothing and is diagnosed below, where the fault actually is.
version="$(python3 -c 'import json,sys; v=json.load(sys.stdin).get("version"); print(v if isinstance(v,str) else "")' <<<"$manifest_json" 2>/dev/null)"
if [ -z "$version" ]; then
    fail "could not read .version from HEAD:$PLUGIN/$MANIFEST (is the subtree committed?)"
    version="$UNREADABLE"
else
    ok "version $version"
fi

# --- 4. the changelog -------------------------------------------------------
# Anchored at a heading, not a bare substring: a version number that happens to
# appear in prose is not a released entry. And the bump is what actually
# delivers — third-party marketplaces do not auto-update, so users get nothing
# from pushed commits alone (DISTRIBUTION-5).
#
# Skipped outright when step 3 could not read a version. Under --check (which
# keeps going by design) an unguarded cascade prints a second red gate blaming
# the CHANGELOG — a file that is fine — and sends the operator to edit it. One
# fault must produce one diagnosis.
if [ "$version" = "$UNREADABLE" ]; then
    step 4 "CHANGELOG entry (version unknown)"
    skip "the version is unknown (step 3) — the CHANGELOG check has nothing to look for"
else
    step 4 "CHANGELOG entry for $version"
    changelog="$(git show "HEAD:$PLUGIN/CHANGELOG.md" 2>/dev/null)"
    # The version goes into an ERE, so its `.` separators would otherwise match
    # any character and a heading `## 0x1y0` would satisfy a `0.1.0` manifest.
    # The whole point of anchoring the heading was that a loose match is not an
    # entry.
    version_re="$(printf '%s' "$version" | sed -e 's#[][\.^$*+?(){}|]#\\&#g')"
    if grep -qE "^##[[:space:]]+\[?${version_re}\]?([[:space:]]|$)" <<<"$changelog"; then
        ok "HEAD:$PLUGIN/CHANGELOG.md has a '## $version' entry"
    else
        fail "HEAD:$PLUGIN/CHANGELOG.md has no '## $version' heading — bump it (and COMMIT it) before releasing"
    fi
fi

# --- 5. build the stage from git -------------------------------------------
step 5 "Build the stage: git archive HEAD:$PLUGIN"
STAGE="$(mktemp -d)" || { echo "release.sh: mktemp failed" >&2; exit 1; }
staged=no
# Ask git for the tree object first. Without this the failure surfaces as
# `tar: This does not look like a tar archive` on an empty pipe, which sends a
# reader after the archiver instead of after the missing commit.
if ! git rev-parse --verify --quiet "HEAD:$PLUGIN" >/dev/null; then
    fail "HEAD carries no $PLUGIN tree — commit the shipping subtree first (the payload is built from the last COMMIT)"
elif git archive --format=tar "HEAD:$PLUGIN" | tar -x -C "$STAGE"; then
    staged=yes
    ok "staged $(find "$STAGE" -type f | wc -l | tr -d ' ') tracked files from the last COMMIT (not the working tree)"
else
    fail "git archive HEAD:$PLUGIN failed — is the subtree committed?"
fi

# The payload is copies, only ever copies — and the count above is exactly why
# this needs its own gate: `find -type f` does not count symlinks, so a stage
# whose components are all dangling links reports a plausible number and sails
# on to step 8's `cp -a`, which copies the dangle into the release clone.
#
# GD-U2, the CORRECTED law (probed three ways on CLI 2.1.220, 2026-07-28; the
# old "a symlink is SKIPPED under --plugin-dir" rationale is REFUTED and must
# never be re-derived):
#   --plugin-dir <directory>          escaping symlinks are HONOURED
#   --plugin-dir <zip> / --plugin-url SILENTLY DROPPED — the plugin loads,
#                                     reports one fewer component, says nothing
#   git archive of the subtree        preserved verbatim, so the link DANGLES
#                                     (there is no target inside the archive)
# The ban is therefore a PACKAGING rule about archived payloads, which is this
# script's business and not the dev loop's.
if [ "$staged" = yes ]; then
    # Strip the stage prefix with `${link#"$STAGE"/}`, not `sed "s#^$STAGE/##"`:
    # $STAGE comes from `mktemp -d` under $TMPDIR and would be interpolated into
    # a BRE, where a regex metacharacter in TMPDIR mangles the reported paths.
    # Join with ", " rather than `tr '\n' ' '` so a path containing a space
    # cannot read as two separate links.
    links=""
    link_count=0
    while IFS= read -r link; do
        [ -n "$link" ] || continue
        link_count=$((link_count + 1))
        links="$links${links:+, }${link#"$STAGE"/}"
    done <<<"$(find "$STAGE" -type l | sort)"
    if [ "$link_count" -eq 0 ]; then
        ok "the stage contains no symlink (an archived link would ship dangling)"
    else
        fail "the stage contains $link_count symlink(s) — they ship DANGLING (or vanish from a zip install): $links"
    fi
else
    # Every other conditional gate in this file announces itself; a silent one is
    # indistinguishable from a deleted one in a --check transcript.
    skip "the stage was not built (see step 5) — the symlink gate has nothing to walk"
fi

# --- 6. validate both manifests, by explicit file path ---------------------
# GD-T7: validate is a SCHEMA check and nothing more — it passes a tree full of
# `sk-ant-` blobs without a murmur, which is why step 2's payload gate is the
# leak gate. Always by explicit file path: a directory-level run does not check
# remote-source marketplace entries, and the explicit form is immune to that.
step 6 "claude plugin validate --strict (both manifests, staged copies)"
if [ "$staged" != yes ]; then
    # Same one-fault-one-diagnosis rule as step 4: without this, a failed step 5
    # reports again here as "the stage carries no .claude-plugin/plugin.json",
    # which reads like a payload problem rather than the archive that never ran.
    skip "the stage was never built (step 5) — there is nothing to validate"
elif ! command -v claude >/dev/null 2>&1; then
    if [ "$mode" = check ]; then
        skip "\`claude\` CLI not on PATH — manifest validation not run"
    else
        fail "\`claude\` CLI not on PATH — a release is not cut without validating the manifests"
    fi
else
    for m in "$MANIFEST" "$MARKETPLACE"; do
        if [ ! -f "$STAGE/$m" ]; then
            fail "the stage carries no $m"
        elif claude plugin validate "$STAGE/$m" --strict; then
            ok "$m validates"
        else
            fail "$m fails --strict validation"
        fi
    done
fi

# --- 7. freshness gates on the release clone -------------------------------
# The one property no test in this repo can express, because it belongs to the
# other repo (DISTRIBUTION-2). Under --check with no clone named, a throwaway
# `git init` stands in: the gates then prove only that they run, which is still
# the thing a dry run is for.
step 7 "Freshness gates on the release clone"
if [ -z "$REL" ]; then
    if [ "$mode" = check ]; then
        TMPREL="$(mktemp -d)"
        REL="$TMPREL/release"
        git init -q "$REL" && note "no --release-clone given; using a throwaway empty repo"
    else
        fail "--release-clone <path> is required for a real release"
    fi
fi

# Resolve the release repository POSITIVELY. The obvious spelling — `[ -d
# "$REL/.git" ]` — is false in two entirely ordinary situations, and in both of
# them every gate below was skipped WITHOUT PRINTING A LINE while steps 8, 9 and
# 10 went on working, because `git -C "$REL" …` walks up to the enclosing
# repository and succeeds there:
#
#   * `$REL` is a path INSIDE a clone — the operator made the directory and the
#     `git clone` failed or was never run. An empty directory is invisible to
#     step 1, so nothing else catches it either.
#   * `$REL` is a linked worktree, whose `.git` is a FILE, not a directory.
#     Step 8's `git -C "$REL" rm -rq .` then deletes every tracked file of that
#     worktree's branch, and step 9 commits and pushes the result.
#
# So: ask git what the toplevel is, require `$REL` to BE that toplevel, and
# refuse anything that resolves to this development repository or shares its
# object store. An unusable `$REL` is fatal in both modes — under --check a
# silently empty step 7 printed "every gate through step 7 is green" about
# gates that never ran, which is worse than no dry run at all.
relok=no
if [ -n "$REL" ]; then
    reltop="$(git -C "$REL" rev-parse --show-toplevel 2>/dev/null)"
    repophys="$(cd "$REPO" && pwd -P)"
    relphys="$(cd "$REL" 2>/dev/null && pwd -P)"
    topphys="$(cd "$reltop" 2>/dev/null && pwd -P)"
    # Where the objects actually live: a linked worktree reports ITSELF as the
    # toplevel, so only the common dir tells you whose history you are about to
    # rewrite.
    relcommon="$(cd "$REL" 2>/dev/null && d="$(git rev-parse --git-common-dir 2>/dev/null)" && cd "$d" 2>/dev/null && pwd -P)"
    repocommon="$(cd "$REPO" && d="$(git rev-parse --git-common-dir 2>/dev/null)" && cd "$d" 2>/dev/null && pwd -P)"
    # A BARE repo has no work tree, so `rev-parse --show-toplevel` fails there
    # exactly as it does on a directory that is no repo at all. Both must be
    # refused — steps 8-9 need a work tree — but they need different sentences:
    # telling an operator to "clone the release repo there first" about a
    # directory that IS a clone of it sends them to fix a thing that is not
    # broken.
    isbare="$(git -C "$REL" rev-parse --is-bare-repository 2>/dev/null)"
    if [ ! -d "$REL" ]; then
        fail "$REL does not exist — clone the release repo there first"
    elif [ "$isbare" = true ]; then
        fail "$REL is a BARE repository — steps 8-9 replace and commit a work tree; clone it non-bare"
    elif [ -z "$topphys" ]; then
        fail "$REL is not a git repository — clone the release repo there first"
    elif [ "$relphys" != "$topphys" ]; then
        fail "$REL is INSIDE the repository at $topphys, not its root — steps 8-9 would rewrite and push THAT repo"
    elif [ "$topphys" = "$repophys" ]; then
        fail "$REL resolves to this development repository — releases never go here (GD-T3)"
    elif [ -n "$relcommon" ] && [ "$relcommon" = "$repocommon" ]; then
        fail "$REL shares this repository's object store (a linked worktree?) — the release repo is a separate repository with its own history"
    else
        relok=yes
        ok "release clone resolves to its own repository root at $topphys"
    fi
fi

if [ "$relok" = yes ]; then
    # `git rm -rq .` removes TRACKED files only, so anything untracked in the
    # release clone survives step 8, gets swept up by step 9's `git add -A` and
    # is pushed to a public repo. `.touch/server.json` — Touch's own per-boot
    # token file, written into whatever directory the server was started from —
    # is the realistic instance. Step 1 applies exactly this discipline to the
    # dev repo; the clone that actually gets published deserves it more, and it
    # has to run BEFORE anything is written.
    relstate="$(git -C "$REL" status --porcelain 2>/dev/null)"
    if [ -z "$relstate" ]; then
        ok "the release clone is clean (tracked, staged and untracked)"
    else
        fail "the release clone has uncommitted or untracked files — step 9's \`git add -A\` would publish them"
        note "first entries: $(printf '%s' "$relstate" | tr '\n' ';' | cut -c1-160)"
    fi

    # A dev remote in the release clone is how contaminated history gets one
    # `git push` away from being public. Compare against this repo's own remote
    # URLs rather than a hardcoded name, so a rename cannot fool the gate.
    devurls="$(git remote 2>/dev/null | while read -r r; do git remote get-url "$r" 2>/dev/null; done | norm_urls)"
    relurls="$(git -C "$REL" remote 2>/dev/null | while read -r r; do git -C "$REL" remote get-url "$r" 2>/dev/null; done | norm_urls)"
    # `read` over a here-string, never `for u in $devurls`: an unquoted
    # expansion is both word-split AND pathname-expanded, so a remote URL
    # holding a `*` or a `?` would be globbed against this repo's root and the
    # gate would then compare directory names to URLs. A here-string keeps each
    # line whole and the loop out of a subshell, so `shared` survives it.
    shared=""
    while IFS= read -r u; do
        [ -n "$u" ] || continue
        while IFS= read -r v; do
            [ -n "$v" ] || continue
            [ "$u" = "$v" ] && shared="$u"
        done <<<"$relurls"
    done <<<"$devurls"
    if [ -n "$shared" ]; then
        fail "the release clone has a remote in common with this repo ($shared)"
    elif [ -z "$relurls" ]; then
        # No remote at all is not the same reassurance as "a different remote",
        # and printing one `ok` for both is how a dry run against a throwaway
        # `git init` came to look like a passing gate.
        if [ "$mode" = check ]; then
            note "the release clone has no remote at all — this gate proved only that it runs"
        else
            fail "the release clone has no remote — a real release has nowhere to push"
        fi
    else
        ok "no dev remote in the release clone"
    fi

    # A release repo only ever gains files. A deletion in its history means a
    # tree was published and then taken back — which git does not do.
    if [ -z "$(git -C "$REL" log --all --diff-filter=D --name-only --pretty=format: 2>/dev/null)" ]; then
        ok "no deletions in the release history"
    else
        fail "the release history contains deletions — it is not a fresh-history repo"
    fi

    # The number to expect, stated once so there is no second reading: the
    # release repo's initial commit (the one that made it non-empty) plus one
    # commit per release already published — releases-so-far + 1, counted
    # BEFORE this run's own commit in step 9. A repo with more commits than
    # that has a history nobody planned.
    count="$(git -C "$REL" rev-list --all --count 2>/dev/null || echo 0)"
    if [ -n "${RELEASE_COMMITS_EXPECTED:-}" ]; then
        # Numeric comparison, not `=`: an operator who exports " 3" or 03 means
        # three, and a STRING compare would quietly report a mismatch and send
        # them hunting through the other repo's history for a commit that is
        # not there. Whitespace is stripped, the rest must be digits, and
        # anything else fails loudly rather than dying inside `-eq`.
        expected="$(printf '%s' "$RELEASE_COMMITS_EXPECTED" | tr -d '[:space:]')"
        case "$expected" in
            ''|*[!0-9]*)
                fail "RELEASE_COMMITS_EXPECTED='$RELEASE_COMMITS_EXPECTED' is not a non-negative integer" ;;
            *)
                if [ "$count" -eq "$expected" ]; then
                    ok "release history holds $count commit(s), as expected"
                else
                    fail "release history holds $count commit(s), expected $expected"
                fi ;;
        esac
    else
        # The number to hand the operator is the one that will be RIGHT NEXT
        # TIME, not the one measured now: $count is taken before this run's own
        # commit, so pinning it and then releasing makes the next run fail step
        # 7 on a healthy repo. It is a monotonically increasing quantity — "from
        # here on" is never true of it.
        note "release history holds $count commit(s) — the initial commit plus one per release published so far; if this run publishes, set RELEASE_COMMITS_EXPECTED=$((count + 1)) before the NEXT one, and bump it by one after every release"
    fi

    # The release repo's commits carry a git identity that becomes public.
    #
    # `config --local`, and the flag is the whole gate. A bare `git config
    # user.email` reads the ENTIRE cascade — local, then global, then system —
    # so on any machine with a global identity (i.e. every machine where a
    # release is actually cut) it answers "yes" about the operator's usual
    # address and reports a per-repo identity that is not there. The advisory
    # would then fire only in the environment that does not need it, and stay
    # silent in the one that does, while step 9 commits the operator's personal
    # address into a public repo. `--local` prints nothing and exits non-zero
    # when the key is unset locally, whatever the global holds.
    if [ -z "$(git -C "$REL" config --local user.email 2>/dev/null)" ]; then
        note "no per-repo user.email in the release clone — set one BEFORE the first commit if your address should not be published"
    else
        ok "release clone has a per-repo user.email"
    fi
fi

if [ "$mode" = check ]; then
    printf '\n'
    if [ "$failures" -gt 0 ]; then
        echo "release.sh --check: $failures gate(s) red — fix them before a real run." >&2
        exit 1
    fi
    echo "release.sh --check: every gate through step 7 is green; nothing was published."
    exit 0
fi

# --- 8. replace the release tree from the STAGE ----------------------------
# THE ONE LEGITIMATE `cp` IN THIS FILE, and it copies out of $STAGE — the tree
# step 5 built from git — never out of the working tree. `rm -rq .` first so a
# file deleted from the payload actually leaves the release repo instead of
# lingering there forever. Both manifests are already in the stage (the release
# repo is flat: repo root == plugin root == marketplace root), so nothing is
# hand-written into $REL here.
step 8 "Replace the release tree from the stage"
if [ -n "$(git -C "$REL" ls-files 2>/dev/null)" ]; then
    git -C "$REL" rm -rq . || fail "could not clear the release clone"
fi
cp -a "$STAGE"/. "$REL"/ || fail "could not copy the stage into the release clone"
ok "release clone now holds the staged payload"

# --- 9. commit and push -----------------------------------------------------
step 9 "Commit and push the release"
git -C "$REL" add -A || fail "git add failed in the release clone"
committed=no
if git -C "$REL" diff --cached --quiet; then
    note "nothing changed since the last release — no commit made"
else
    git -C "$REL" commit -q -m "touch $version" || fail "commit failed in the release clone"
    committed=yes
    ok "committed 'touch $version'"
    # Printed here rather than left to arithmetic in step 7's advisory: this is
    # the count that exists now, so it is the one the next run must expect.
    note "the release repo now holds $(git -C "$REL" rev-list --all --count 2>/dev/null) commit(s) — that is the RELEASE_COMMITS_EXPECTED for the next run"
fi
# Only push what this run committed. Pushing anyway and printing `ok: pushed`
# after "no commit made" reads like a release went out when nothing did — and
# that reading is what an operator carries away from the transcript.
if [ "$committed" = yes ]; then
    git -C "$REL" push || fail "push failed"
    ok "pushed"
else
    note "nothing to push (if an earlier run left a commit unpushed, push the release clone by hand)"
fi

# --- 10. the tag gate -------------------------------------------------------
# Free, and it buys two checks: plugin.json/marketplace.json version agreement,
# and a dirty-tree refusal in the release clone. `--push` is optional because
# the `{name}--v{version}` tag only matters for plugin dependency constraints,
# which Touch has none of (DISTRIBUTION-5).
step 10 "claude plugin tag --dry-run"
if ! command -v claude >/dev/null 2>&1; then
    skip "\`claude\` CLI not on PATH"
elif claude plugin tag "$REL" --dry-run; then
    ok "tag dry run agrees on $version"
    if [ "$tag_push" = yes ]; then
        claude plugin tag "$REL" --push && ok "tag pushed" || fail "tag push failed"
    fi
else
    fail "claude plugin tag --dry-run refused"
fi

printf '\nrelease.sh: touch %s released from the release clone at %s\n' "$version" "$REL"
exit 0
