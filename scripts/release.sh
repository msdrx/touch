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
# `git add`ed is the version of this accident that leaves no other trace. There
# is no `cp` in this file at all: the stage exists to be SCANNED, never to be
# copied anywhere, and `cp -r` of `plugin/touch/` is exactly the shortcut this
# file exists to prevent (probe E6 shipped a tracked `.touch/leak.txt` and a
# `__pycache__/` that way).
#
# THIS REPOSITORY IS THE MARKETPLACE
# ----------------------------------
# `.claude-plugin/marketplace.json` at the ROOT names `msdrx-tools` and lists
# one plugin with `"source": "./plugin/touch"`, so `/plugin marketplace add
# msdrx/touch` clones THIS repo and the install copies that subtree into the
# user's plugin cache. The manifest cannot live beside `plugin.json`: a cloned
# marketplace is read from `<clone>/.claude-plugin/marketplace.json` and
# nowhere else, and `owner/repo/sub/dir` is not a source form (CLI 2.1.220).
# Publishing is therefore an ordinary `git push` of this repo — step 7.
#
# WHAT THAT COSTS, STATED ONCE
# ----------------------------
# `/plugin marketplace add owner/repo` CLONES HISTORY, and this repository's
# history carries a burned token blob and credentialed `mongodb://` URIs. No
# checkout trick — `--sparse`, `git-subdir` — is a privacy boundary; both limit
# the checkout, not the objects. An earlier model published a payload-only
# tree to a separate EMPTY repo for exactly this reason; serving the catalog
# from here trades that away, so the fix is to purge the history rather than to
# route around it, and preflight (b) is where that decision gets re-confirmed
# every single release. Treat every credential this repo has ever seen as
# burned (DISTRIBUTION-2).
#
# usage: scripts/release.sh [--check] [--tag-push] [-h]
#   default          the real thing: every gate is fatal on the spot, and a
#                    green run pushes this repository.
#   --check          dry run. Stops after step 7, pushes nothing. Unlike the
#                    real run it does NOT stop at the first red gate — it runs
#                    them all and reports every failure, then exits non-zero if
#                    any failed. That is the same bargain `tests/run_all.sh
#                    --keep-going` makes: you want the whole list before you
#                    start fixing.
#   --tag-push       also `claude plugin tag --push` after a successful
#                    release. Optional and rarely wanted: the `{name}--v{ver}`
#                    tag matters only for plugin *dependency* constraints, and
#                    Touch has none. The dry-run tag check (step 8) runs
#                    either way, for its version-agreement and dirty checks.
#
# environment:
#   RELEASE_CONFIRM=yes            answer the preflight non-interactively (for
#                                  a run whose stdin is not a terminal).
#   RELEASE_REMOTE=<name>          the remote to publish to (default `origin`).
#
# exit status: 0 = done / dry run clean, 1 = a gate failed, 2 = bad usage.

set -uo pipefail

# `readlink -f` first: resolving the symlink rather than its target keeps $REPO
# on the checkout when this script is reached through a link on $PATH.
REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
PLUGIN="plugin/touch"
#: Payload-relative — the stage's own root looks like this.
MANIFEST=".claude-plugin/plugin.json"
#: REPO-relative, and deliberately not under $PLUGIN: the catalog is not
#: payload. Step 6 validates it where it lives.
MARKETPLACE=".claude-plugin/marketplace.json"
REMOTE="${RELEASE_REMOTE:-origin}"

# The commit whose TREE still carries the burned `mytok2` blob. Named here as
# an anchor for the preflight's first line, not as a count: every quantitative
# claim about this repo's contamination has drifted at least once, so this file
# cites the COMMANDS that measure it and lets you read today's answer
# (PRIOR-AUDIT-12).
TOKEN_TIP="f3b10a7"

mode=real
tag_push=no
while [ $# -gt 0 ]; do
    case "$1" in
        --check|-c)      mode=check ;;
        --tag-push)      tag_push=yes ;;
        -h|--help)       sed -n '2,${/^#/!q;p;}' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "release.sh: unknown argument '$1' (try -h)" >&2; exit 2 ;;
    esac
    shift
done

STAGE=""
cleanup() {
    [ -n "$STAGE" ] && rm -rf "$STAGE"
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
# Four things stand between this repository and a safe publish that neither a
# test nor this script can check from in here: two are about history that is
# already public, one is about a database, and one has to happen on a machine
# where `claude plugin install` may write under ~/.claude (which this repo's
# law forbids doing here — GD-T7). So they are printed, with the command that
# measures each, and confirmed by a human.
#
# (b) carries more weight than it used to. While releases went to a separate
# empty repo, this repo's history was a private embarrassment; now that the
# catalog is served from here, every `/plugin marketplace add msdrx/touch`
# clones it. The item did not get easier — the blast radius grew.
step 0 "Preflight — the manual checklist"
cat <<'CHECKLIST'
   Confirm each of these has actually been done. Commands are given so you can
   re-measure rather than trust a number written down some other day.

   (a) You accept that installing Touch CLONES THIS REPOSITORY, history and
       all. That is what serving the catalog from here means; there is no
       subdirectory or sparse form of a marketplace source that changes it.
   (b) The decision about this repo's HISTORY is executed — purge with
       `git filter-repo` (the fix), or publish knowing what is in there. The
       check is:
           git rev-list --all --objects | grep -i mytok
           git grep -aIhE 'mongodb://[^/[:space:]"<]+:[^@[:space:]"<]+@' $(git rev-list --all)
   (c) The MongoDB password that appears in those history blobs is rotated.
       Treat every credential this repo has ever seen as burned.
   (d) Install-path verification was done ONCE on your own machine: install
       from the marketplace, confirm the bin/ wrappers kept their exec bits
       through the cache copy, and run `touch-selfcheck`. It cannot happen
       here — every `claude plugin install` writes under ~/.claude.
CHECKLIST
note "local view of (a): a clone of this repo is what an install downloads — $(git rev-list --all --count 2>/dev/null || echo '?') commit(s) of history come with it"
# NOT `git rev-list … | grep -q`. Under `pipefail` a `-q` consumer exits on the
# first hit, git dies of SIGPIPE, and the pipeline returns 141 — so the shape
# reports "no match" exactly when it MATCHED, and only once the history is big
# enough for git to still be writing. Collect the hits into a variable and test
# that instead; `grep -a` because this sandbox's grep is ugrep and treats a
# NUL-bearing stream as binary.
tokenblobs="$(git rev-list --all --objects 2>/dev/null | grep -ai mytok || true)"
if [ -n "$tokenblobs" ]; then
    note "local view of (b): the token blob IS still reachable in this clone's history — and this clone is now the install source (commit $TOKEN_TIP carries it in its TREE)"
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
    printf '   Type "yes" to confirm all four: '
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
# whose components are all dangling links reports a plausible number and every
# later gate reads a payload that is mostly not there. Installing copies
# `plugin/touch/` out of the marketplace clone into `~/.claude/plugins/cache/`,
# so a link that escapes the payload has nothing to point at once it lands.
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
#
# The two manifests are read from two different places, and that asymmetry is
# the layout, not an oversight:
#   $STAGE/$MANIFEST     the STAGED plugin.json — bytes from HEAD, the copy a
#                        consumer's cache receives.
#   $REPO/$MARKETPLACE   the catalog IN PLACE. It is not payload, so it is not
#                        in the stage; and it must be validated where it sits,
#                        because a relative `source` resolves against the
#                        marketplace root — copy the file to a temp directory
#                        and `./plugin/touch` resolves to nothing. Step 1
#                        already refused a dirty tree, so on-disk == HEAD here.
step 6 "claude plugin validate --strict (plugin.json staged, marketplace.json in place)"
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
    if [ ! -f "$STAGE/$MANIFEST" ]; then
        fail "the stage carries no $MANIFEST"
    elif claude plugin validate "$STAGE/$MANIFEST" --strict; then
        ok "$MANIFEST validates (staged bytes)"
    else
        fail "$MANIFEST fails --strict validation"
    fi
    # A catalog that ships inside the payload is a second copy of itself under
    # the same marketplace name. Cheap to check, and it is the exact regression
    # the root-catalog layout replaced.
    if [ -e "$STAGE/$MARKETPLACE" ]; then
        fail "the stage carries $MARKETPLACE — the catalog is NOT payload; it belongs at the repo root only"
    else
        ok "the stage carries no marketplace.json (the catalog is not payload)"
    fi
    if [ ! -f "$REPO/$MARKETPLACE" ]; then
        fail "no $MARKETPLACE at the repo root — a cloned marketplace is read from there and nowhere else"
    elif claude plugin validate "$REPO/$MARKETPLACE" --strict; then
        ok "$MARKETPLACE validates (repo root)"
    else
        fail "$MARKETPLACE fails --strict validation"
    fi
fi

# --- 7. the publish target --------------------------------------------------
# Publishing is a `git push` of THIS repository: the catalog sits at its root,
# so whatever the remote's default branch holds is what the next `/plugin
# marketplace add msdrx/touch` clones and what `/plugin marketplace update`
# pulls. The gates here are therefore about this repo's remote — the old
# "prove things about the OTHER repo" step has no subject any more, and the
# properties it checked (fresh history, no dev remote, no deletions) were
# statements about a publishing model that no longer exists. Do not reconstruct
# them here: a deletion in THIS history is ordinary, and a dev remote is the
# publish target.
step 7 "The publish target ($REMOTE)"
publishable=no
remote_url="$(git remote get-url "$REMOTE" 2>/dev/null)"
if [ -z "$remote_url" ]; then
    fail "no remote named '$REMOTE' — add it, or name another with RELEASE_REMOTE=<name>; there is nowhere to publish"
else
    ok "$REMOTE = $remote_url"

    # The `repository` field on the plugin page and the repo an install
    # actually clones must be ONE repository. They are two independently-edited
    # strings, and the day they drift the page links somewhere the plugin does
    # not come from. Compared as identity, not spelling: `https://…/touch.git`
    # and `git@github.com:msdrx/touch` are the same place.
    remote_id="$(printf '%s\n' "$remote_url" | norm_urls)"
    repo_field=""
    if [ -n "${manifest_json:-}" ]; then
        repo_field="$(python3 -c '
import json,sys
try:
    r = json.load(sys.stdin).get("repository")
except Exception:
    r = None
if isinstance(r, dict):
    r = r.get("url")
print(r if isinstance(r, str) else "")' <<<"$manifest_json" 2>/dev/null)"
    fi
    if [ -z "$repo_field" ]; then
        note "HEAD:$PLUGIN/$MANIFEST declares no .repository — nothing to compare the remote against"
    else
        repo_id="$(printf '%s\n' "$repo_field" | norm_urls)"
        if [ "$repo_id" = "$remote_id" ]; then
            ok "plugin.json .repository names the repo being published ($repo_id)"
        else
            fail "plugin.json .repository is $repo_field but $REMOTE is $remote_url — the plugin page would link away from the repo the install clones"
        fi
    fi

    # What the remote serves right now, so the transcript says what this push
    # would change rather than leaving the operator to infer it. A fetch
    # failure is a note, not a gate: the push below reports the truth anyway,
    # and an offline dry run is still worth running.
    branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
    if [ "$branch" = HEAD ]; then
        fail "HEAD is detached — publish from a branch that tracks $REMOTE"
    elif ! git fetch --quiet "$REMOTE" 2>/dev/null; then
        note "could not fetch $REMOTE — the ahead/behind reading below may be stale"
    fi
    upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)"
    if [ -z "$upstream" ]; then
        # No backticks in a double-quoted note, ever: they are command
        # substitution, and a message ABOUT a push would run one.
        note "$branch tracks nothing — step 8 will push it with -u to set tracking"
        publishable=yes
    else
        # `rev-list --left-right --count` in one call: two separate counts can
        # disagree with each other if anything moves between them.
        counts="$(git rev-list --left-right --count "$upstream...HEAD" 2>/dev/null)"
        behind="$(printf '%s' "$counts" | cut -f1)"
        ahead="$(printf '%s' "$counts" | cut -f2)"
        if [ "${behind:-0}" -gt 0 ] 2>/dev/null; then
            fail "$branch is $behind commit(s) BEHIND $upstream — integrate them first; a release does not force-push"
        elif [ "${ahead:-0}" -gt 0 ] 2>/dev/null; then
            ok "$ahead commit(s) to publish onto $upstream"
            publishable=yes
        else
            note "$branch is level with $upstream — the payload is already published; only the version bump delivers an update"
        fi
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

# --- 8. publish -------------------------------------------------------------
# The point of no return, and it is one command. There is no staging, no copy
# and no second repository: the marketplace IS this repo, so pushing it is the
# release. Everything above ran so that this line is boring.
step 8 "Push to $REMOTE"
if [ "$publishable" != yes ]; then
    note "nothing to push — $REMOTE already has this commit"
elif [ -z "$upstream" ]; then
    git push -u "$REMOTE" "$branch" && ok "pushed $branch to $REMOTE (tracking set)" \
        || fail "push failed"
else
    git push "$REMOTE" "$branch" && ok "pushed $branch to $REMOTE" || fail "push failed"
fi

# --- 9. the tag gate --------------------------------------------------------
# Free, and it buys two checks: plugin.json/marketplace.json version agreement,
# and a dirty-tree refusal. `--push` is optional because the
# `{name}--v{version}` tag only matters for plugin dependency constraints,
# which Touch has none of (DISTRIBUTION-5).
step 9 "claude plugin tag --dry-run"
if ! command -v claude >/dev/null 2>&1; then
    skip "\`claude\` CLI not on PATH"
elif claude plugin tag "$REPO" --dry-run; then
    ok "tag dry run agrees on $version"
    if [ "$tag_push" = yes ]; then
        claude plugin tag "$REPO" --push && ok "tag pushed" || fail "tag push failed"
    fi
else
    fail "claude plugin tag --dry-run refused"
fi

printf '\nrelease.sh: touch %s published — users get it with `/plugin marketplace update msdrx-tools` then `/plugin update touch@msdrx-tools`\n' "$version"
exit 0
