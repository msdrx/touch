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
# Publishing is therefore an ordinary `git push` of this repo — step 9.
#
# WHAT THAT COSTS, STATED ONCE
# ----------------------------
# `/plugin marketplace add owner/repo` CLONES HISTORY, and this repository's
# history carries a burned token blob and credentialed `mongodb://` URIs. No
# checkout trick — `--sparse`, `git-subdir` — is a privacy boundary; both limit
# the checkout, not the objects. An earlier model published a payload-only
# tree to a separate EMPTY repo for exactly this reason; serving the catalog
# from here trades that away, so the fix is to purge the history rather than to
# route around it. Step 0 therefore GATES on it (GD-C4): a reachable token blob
# or a credentialed `mongodb://` URI is a red gate — in `--check` too — until
# the operator either purges the history or says `RELEASE_HISTORY_ACCEPTED=yes`
# out loud. The purge itself (`git filter-repo` plus a force push) is an
# operator action; this script's job is to make publishing over it deliberate.
# Treat every credential this repo has ever seen as burned (DISTRIBUTION-2).
#
# usage: scripts/release.sh [--check] [--tag-push] [-h]
#   default          the real thing: every gate is fatal on the spot, and a
#                    green run pushes this repository.
#   --check          dry run. Stops after step 8, pushes nothing. Unlike the
#                    real run it does NOT stop at the first red gate — it runs
#                    them all and reports every failure, then exits non-zero if
#                    any failed. That is the same bargain `tests/run_all.sh
#                    --keep-going` makes: you want the whole list before you
#                    start fixing.
#   --tag-push       also `claude plugin tag --push` (step 10) after a
#                    successful release. Optional and rarely wanted: the
#                    `{name}--v{ver}` tag matters only for plugin *dependency*
#                    constraints, and Touch has none. The dry-run tag gate
#                    (step 8) runs either way — it is a PRE-publish gate now,
#                    so its dirty-tree refusal is reachable under `--check`
#                    instead of firing after the point of no return.
#
# environment:
#   RELEASE_CONFIRM=yes            answer the preflight non-interactively (for
#                                  a run whose stdin is not a terminal). It
#                                  answers the four checklist bullets and
#                                  NOTHING else — in particular it does not
#                                  imply RELEASE_HISTORY_ACCEPTED.
#   RELEASE_HISTORY_ACCEPTED=yes   publish even though this repo's history
#                                  still carries a burned token blob or a
#                                  credentialed `mongodb://` URI (step 0's
#                                  gate). Deliberately a separate knob: the
#                                  history is the one property of this
#                                  distribution model that cannot be undone
#                                  after a push, so accepting it is its own
#                                  sentence, not a side effect of confirming
#                                  the checklist. It accepts a KNOWN
#                                  contamination and nothing else: a scan that
#                                  could not complete stays red through it.
#   RELEASE_REMOTE=<name>          the remote to publish to (default `origin`).
#   RELEASE_CONTEXT_CEILING=<n>    the always-on context ceiling, in tokens
#                                  (default 12000). Step 2's (c) half measures
#                                  CLAUDE.md + the memory index + the ten skill
#                                  descriptions and fails when their sum exceeds
#                                  it. Raising this is a deliberate act with a
#                                  recurring bill attached: the prefix is
#                                  re-read on every turn of every agent of every
#                                  future run. Ignored once
#                                  `tests/test_context_budget.py` declares its
#                                  own budgets — those win, and the gate says so.
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

# The two shapes step 0's history gate looks for, kept beside each other so the
# checklist text and the gate cannot drift apart.
#
# The placeholder pattern is not a loophole, it is the difference between a
# credential and a sentence ABOUT credentials: `mongodb://user:password@host`
# and `mongodb://u:p@…` are the documentation forms this repo's own docs use,
# and a redacted `***` password is what the mirror's own logging prints. A gate
# that goes red on its own documentation is a gate that gets bypassed with the
# knob every single release, which is the same as not having it. Anything whose
# password is not one of those named shapes counts as real.
#
# Recorded as a deviation, not smuggled in: GD-C4 says "a credentialed
# `mongodb://` URI" with no exemption, and this is one. Its residual is exact
# and worth knowing — a REAL credential whose user/password pair happens to be
# `user:password`, `u:p`, all-asterisks or `<angle-bracketed>` is exempt by
# name and this gate will not see it. That is the price of a gate that is red
# only when something is wrong.
#
# The scheme is a separate variable and the patterns are ASSEMBLED, for one
# concrete reason: this script lives in the repository it scans, so a pattern
# written out in full here becomes a matching line in the next release's
# history and the gate starts reporting itself. Composition keeps the literal
# `<scheme>` + credential shape from ever appearing on one line of this file.
MONGO_SCHEME='mongodb://'
MONGO_URI_RE="$MONGO_SCHEME"'[^/[:space:]"<]+:[^@[:space:]"<]+@'
MONGO_PLACEHOLDER_RE="$MONGO_SCHEME"'(user:password|u:p)@|'"$MONGO_SCHEME"'[^/[:space:]"<]+:(\*+|<[^@[:space:]"<]*>)@'

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
CLEAN=""
cleanup() {
    [ -n "$STAGE" ] && rm -rf "$STAGE"
    # Step 2's clean checkout. It is removed inline on the happy path; this is
    # for the real-mode `fail`, which exits from inside the gate.
    [ -n "$CLEAN" ] && rm -rf "$CLEAN"
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

# --- 0. preflight: the checklist, and the one part of it that IS a gate -----
# Four things stand between this repository and a safe publish that neither a
# test nor this script can check from in here: two are about history that is
# already public, one is about a database, and one has to happen on a machine
# where `claude plugin install` may write under ~/.claude (which this repo's
# law forbids doing here — GD-T7). So they are printed, with the command that
# measures each, and confirmed by a human.
#
# (b) is the exception, and it is a gate (GD-C4). While releases went to a
# separate empty repo, this repo's history was a private embarrassment; now
# that the catalog is served from here, every `/plugin marketplace add
# msdrx/touch` clones it. The item did not get easier — the blast radius grew,
# and a `note` next to a human typing "yes" was the only thing standing between
# a burned credential and a public marketplace. The scan below therefore FAILS,
# under `--check` as well (a dry run that cannot see the one irreversible
# property of this distribution model is not a dry run of this release), and
# only `RELEASE_HISTORY_ACCEPTED=yes` clears it — never `RELEASE_CONFIRM`,
# because one knob that answers everything answers nothing.
step 0 "Preflight — the manual checklist, and the history gate"
cat <<'CHECKLIST'
   Confirm each of these has actually been done. Commands are given so you can
   re-measure rather than trust a number written down some other day.

   (a) You accept that installing Touch CLONES THIS REPOSITORY, history and
       all. That is what serving the catalog from here means; there is no
       subdirectory or sparse form of a marketplace source that changes it.
   (b) The decision about this repo's HISTORY is executed — purge with
       `git filter-repo` (the fix), or publish knowing what is in there. This
       one is not on your honour: it is the gate printed right below this
       checklist, and RELEASE_HISTORY_ACCEPTED=yes is the only way past it.
       The check is:
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
# Whether the scans below actually RAN, carried all the way to the verdict.
#
# A gate that could not look is not a gate that found nothing, and under
# `--check` — where `fail` records and keeps going — the two read identically
# three lines apart: "the scan did not complete", and then "no credentialed URI
# is reachable in this clone's history". The natural reading of that pair is
# "tooling hiccup, history is clean", which is the one conclusion this gate must
# never suggest: contamination is the single property of this distribution model
# that cannot be undone after a push. So the verdict below withholds itself
# instead, and RELEASE_HISTORY_ACCEPTED cannot clear it — that knob accepts a
# KNOWN contamination; it is not a way to launder an unknown one.
history_scan=ok

# Both halves of (b) start from `git rev-list --all`, so a rev-list that cannot
# run is the single failure that makes BOTH of them silently empty. Ask once and
# keep the status. rc 0 with empty output is a repository with no commits — a
# real, and clean, answer rather than a broken scan.
history_commits=""
if ! history_commits="$(git rev-list --all 2>/dev/null)"; then
    history_scan=failed
    fail "git rev-list --all failed — this clone cannot enumerate its own history, so the two scans below walked nothing; re-run the (b) commands by hand before publishing"
fi
# NOT `git rev-list … | grep -q`. Under `pipefail` a `-q` consumer exits on the
# first hit, git dies of SIGPIPE, and the pipeline returns 141 — so the shape
# reports "no match" exactly when it MATCHED, and only once the history is big
# enough for git to still be writing. Collect the hits into a variable and test
# that instead; `grep -a` because this sandbox's grep is ugrep and treats a
# NUL-bearing stream as binary.
#
# The status is kept rather than `|| true`d away: rc 0 is "matched", rc 1 is the
# legitimate "no match", and anything above it is a scan that did not run. The
# one case `pipefail` cannot tell apart — git dying while grep also finds
# nothing, both reported as 1 — is what the rev-list probe above is for: it
# walks the same history from the same clone, so it fails in the same breath.
tokenblobs="$(git rev-list --all --objects 2>/dev/null | grep -ai mytok)"
token_rc=$?
if [ "$token_rc" -gt 1 ]; then
    history_scan=failed
    fail "the history scan for a token-named blob did not complete (rc=$token_rc) — a scan that errored is not a clean scan; re-run the first (b) command by hand before publishing"
fi
if [ -n "$tokenblobs" ]; then
    note "local view of (b): the token blob IS still reachable in this clone's history — and this clone is now the install source (commit $TOKEN_TIP carries it in its TREE)"
elif [ "$history_scan" = ok ]; then
    note "local view of (b): no token-named blob reachable in this clone's history"
fi
# The second half of (b): a credentialed URI does not need a suggestive blob
# NAME to be reachable, so the contents get scanned too. `git grep` over the
# commit list is the same command the checklist prints, word-split on purpose
# — the commits are arguments. `-I` skips binaries; `-a` because this sandbox's
# grep is ugrep. The whole pipeline ends in `sort -u`, which reads its input to
# the end, so nothing here can invert on SIGPIPE the way a `-q` consumer would.
mongouris=""
if [ -n "$history_commits" ]; then
    # The search runs on its own line, not as the head of the filter pipeline,
    # so its exit status survives: under `pipefail` a downstream `grep` finding
    # nothing also exits 1, and the two "no match"es are indistinguishable from
    # each other and from a git error. This gate stands between a burned
    # credential and a public marketplace, so it fails CLOSED — rc 0 is "matched",
    # rc 1 is the legitimate "no match", and anything above 1 (a git error, an
    # ARG_MAX overflow on the commit list, a grep that is not the grep this was
    # written for) is a gate that did not run, reported as such.
    # shellcheck disable=SC2086
    mongo_raw="$(git grep -aIhE "$MONGO_URI_RE" $history_commits 2>/dev/null)"
    mongo_rc=$?
    if [ "$mongo_rc" -gt 1 ]; then
        history_scan=failed
        fail "the history scan for credentialed mongodb:// URIs did not complete (git grep rc=$mongo_rc) — a scan that errored is not a clean scan; re-run the second (b) command by hand before publishing"
    fi
    # The extraction stops at the first `/` or `?` after the host, so the same
    # URI quoted three different ways (`…admin\`, `…admin&quot;`) counts once
    # instead of three times. Only the COUNT is ever printed — a transcript
    # that quotes the credential back at you is a second copy of the leak.
    mongouris="$(printf '%s\n' "$mongo_raw" \
        | grep -aoE "$MONGO_URI_RE[^/?[:space:]\"<,\`)]*" \
        | grep -avE "$MONGO_PLACEHOLDER_RE" | sort -u || true)"
fi
if [ -n "$mongouris" ]; then
    note "local view of (b): $(printf '%s\n' "$mongouris" | wc -l | tr -d ' ') credentialed mongodb:// URI(s) reachable in this clone's history"
fi
# Found-something is asked FIRST, before scan health: a partial scan that still
# turned up a credential has told you the thing you needed to know, and burying
# that under "verdict withheld" would be the same substitution of process for
# evidence this gate exists to prevent. The withheld arm is therefore only about
# refusing to call an unfinished scan clean — and either way the `fail` raised
# by the incomplete scan above already stands, so a run that could not look is
# red whatever this verdict says.
if [ -n "$tokenblobs" ] || [ -n "$mongouris" ]; then
    if [ "${RELEASE_HISTORY_ACCEPTED:-}" = yes ]; then
        note "(b) contaminated history ACCEPTED by RELEASE_HISTORY_ACCEPTED=yes — publishing this repo hands every clone the objects counted above; re-read them with the two commands in (b)"
    else
        fail "(b) this repo's history still carries a burned credential and this repo IS the install source — purge it with \`git filter-repo\`, or publish deliberately with RELEASE_HISTORY_ACCEPTED=yes (RELEASE_CONFIRM does NOT imply it)"
    fi
elif [ "$history_scan" != ok ]; then
    note "(b) verdict withheld — a scan above did not complete, so 'nothing found' here would mean 'nothing was looked at'; RELEASE_HISTORY_ACCEPTED accepts a known contamination and cannot clear a scan that never ran"
else
    ok "(b) no token-named blob and no credentialed mongodb:// URI is reachable in this clone's history"
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
# likeliest release accident there is, and downstream can only catch the ones a
# test happens to name: step 2 now runs the suite in a clean checkout of HEAD,
# so a missing *tested* file shows up there, but a new wrapper or doc nobody
# asserts about still ships as absence. Untracked-blind is the one thing this
# gate cannot afford to be — it is the only one that sees the file itself.
#
# Two shapes of dirt, told apart since `.touch/memory/*.md` became the ONE
# tracked subtree of `.touch/`: a dirty payload or doc is the classic release
# accident, while a dirty memory tree is a note somebody wrote during the day.
# Both still REFUSE — `git archive HEAD` ships the committed memory files, so an
# uncommitted one means the release carries yesterday's notes — but they get
# different sentences, because "uncommitted changes" sends an operator hunting
# through the payload for something that is actually one edited `.md`. The
# remedy is named in the message too: stage that subtree BY NAME. `git add
# .touch/` would sweep in the per-boot token file, the run history and the
# memory tree's own `.history/`/`.trash/` copies, all of which the ignore carve
# exists to keep out.
step 1 "Dirty-tree refusal"
dirty="$(git status --porcelain 2>/dev/null)"
# Porcelain is `XY <path>`, so drop the two status columns and the separator,
# plus a leading quote on a path git chose to quote. `grep -v` exits 1 on "every
# line matched" (i.e. nothing is outside the memory tree), which is an answer and
# not a failure, hence the `|| true` — under `set -e` it would abort the release.
outside="$(printf '%s\n' "$dirty" | sed -e 's/^...//' -e 's/^"//' \
    | grep -v '^\.touch/memory/' || true)"
if [ -z "$dirty" ]; then
    ok "working tree and index are clean (tracked, staged and untracked)"
elif [ -z "$outside" ]; then
    fail "the only dirt is under .touch/memory/ — that subtree IS tracked and git archive HEAD ships it, so commit it with 'git add .touch/memory' (never 'git add .touch/') or set it aside; step 5 builds from HEAD and would publish the previous notes"
    note "memory entries: $(printf '%s' "$dirty" | tr '\n' ';' | cut -c1-160)"
else
    fail "uncommitted or untracked changes — step 5 builds from HEAD, so they would silently not ship"
    # `tr` then `cut`, never `head`: an early-exiting consumer under pipefail is
    # the same SIGPIPE trap the preflight above documents.
    note "first entries: $(printf '%s' "$dirty" | tr '\n' ';' | cut -c1-160)"
fi

# --- 2. the suite in a clean checkout of HEAD, then the gates that need git --
# Two questions, and they cannot be asked in the same tree:
#
#   (a) "does the tree that SHIPS hold together" — `git archive HEAD | tar -x`
#       into a temp directory and run the COMMITTED `tests/run_all.sh` there.
#       That is this file's headline (WHAT SHIPS IS WHAT GIT HAS) and the
#       procedure `run_all.sh`'s own header prescribes before any release: the
#       extracted tree has no `.git` ephemera, no `.touch/local-orchestrators/`
#       run history and no untracked anything, which is what a packaged copy
#       looks like, so files that read absent things SKIP there instead of
#       passing on this machine's leftovers. One thing it DOES carry now:
#       `.touch/memory/*.md`, the one tracked subtree of `.touch/` — so "no
#       untracked anything" is still exact, but "no `.touch/` at all" stopped
#       being true, and a test that reads the memory tree runs there for real
#       instead of skipping.
#
#   (b) "does the payload carry a secret" — and that one the clean tree CANNOT
#       answer. `tests/test_package.py` and `tests/test_publish_hygiene.py` both
#       open with `git rev-parse --git-dir` and print `skip: not a git checkout`
#       in an archive tree (measured: both print exactly that and exit 0). They
#       are the ONLY secret scanning a release performs — `sk-ant-`,
#       `gh[pousr]_`, AWS keys, private keys, credentialed `mongodb://`,
#       token-shaped blobs, plus the `__pycache__`/`.touch/` strays probe E6
#       shipped — so leaving them to the clean run would mean a release scans
#       nothing and says green, which is the exact failure mode every other gate
#       in this file is written against. They build their stages from `HEAD`, so
#       running them in $REPO is not a working-tree concession: they read the
#       same committed bytes step 5 stages, and $REPO is the only place they can
#       run at all. Do NOT "fix" this by `git init`-ing the extracted tree —
#       that hands test_package.py a HEAD which is not this repo's HEAD, and the
#       stowaway comparison stops meaning anything (GD-C6).
#
# Neither run relaxes the commit law: a green DEV tree is still what
# CONTRIBUTING §Tests requires before you commit. The gates here ask a different
# question — "is the tree that ships sound" — and a release gets to ask both.
#
# `--keep-going` because a red release gate should hand over the whole list; a
# fail-fast run just makes you re-archive after every fix.
step 2 "tests/run_all.sh in a clean checkout of HEAD, and the git-requiring leak gates in $REPO"
if ! git rev-parse --verify --quiet HEAD >/dev/null; then
    fail "HEAD does not resolve — there is no committed tree to test"
else
    CLEAN="$(mktemp -d)" || { echo "release.sh: mktemp failed" >&2; exit 1; }
    # `mktemp -d` honours $TMPDIR, and a $TMPDIR that happens to sit inside a git
    # checkout hands the extracted tree an ENCLOSING repository: discovery walks
    # up out of $CLEAN, `git rev-parse --git-dir` succeeds, the two suites in (b)
    # stop self-skipping there, and `tests/test_package.py` builds its stage from
    # a HEAD that is not this repo's. That is the AMBIENT form of the `git init`
    # accident (b) above forbids, and it arrives without anyone deciding
    # anything. GIT_CEILING_DIRECTORIES stops the upward walk at $CLEAN's parent;
    # the probe underneath is not belt-and-braces for its own sake — the ceiling
    # rules are subtle enough (the cwd is never excluded, symlinks are resolved)
    # that the only honest way to claim it worked is to measure it.
    CEILING="$(dirname "$CLEAN")"
    if ! git archive --format=tar HEAD | tar -x -C "$CLEAN"; then
        fail "git archive HEAD failed — could not build the clean checkout the suite runs in"
    elif [ ! -x "$CLEAN/tests/run_all.sh" ]; then
        fail "HEAD carries no executable tests/run_all.sh — commit it (the gate runs the COMMITTED runner)"
    elif GIT_CEILING_DIRECTORIES="$CEILING" git -C "$CLEAN" rev-parse --git-dir >/dev/null 2>&1; then
        fail "the clean checkout at $CLEAN still resolves to a git repository — \$TMPDIR is inside a checkout, so this gate would read the ENCLOSING repo's HEAD instead of a packaged copy; set TMPDIR to somewhere outside any repository and re-run"
    elif (cd "$CLEAN" && export GIT_CEILING_DIRECTORIES="$CEILING" && tests/run_all.sh --keep-going); then
        ok "the full suite is green over tracked bytes only"
    else
        fail "the suite is red in a clean checkout of HEAD — a release is not cut over a red suite (a dev-tree-only green does not count: the clean tree is what ships)"
    fi
    rm -rf "$CLEAN"
    CLEAN=""
fi
# The (b) half. Named one by one rather than looped over a glob: these two are
# in the list because they SELF-SKIP above, and a future test that does the same
# has to be added here deliberately, not inherited by a pattern match.
#
# An ABSENT file SKIPS rather than fails, and that is a decision with a stated
# residual, not a softening. This script gets copied into and run inside minimal
# trees — the fixture `tests/test_docs.py` builds is one, and it carries a stub
# runner and two manifests by design — and a release script that only runs
# inside its own development checkout is a release script nobody can exercise.
# What the gate asserts is the half that matters: where the file EXISTS it must
# be green, and in THIS repository both exist, so nothing here is weakened.
#
# The residual, measured rather than hoped: nothing in the suite pins these two
# filenames (`tests/run_all.sh:83` discovers by glob, and no test asserts either
# path), so deleting them both would turn this gate into two SKIP lines instead
# of a red one. That is visible in every transcript — SKIP is printed, never
# silent — and it is the price of the portability above.
for gate in tests/test_package.py tests/test_publish_hygiene.py; do
    if [ ! -f "$REPO/$gate" ]; then
        skip "$gate is not present in $REPO — nothing to run here (a checkout that ships it runs it)"
    elif python3 "$REPO/$gate"; then
        ok "$gate is green (run in $REPO, where git exists — in the clean checkout it skips itself)"
    else
        fail "$gate is red — the payload leak/hygiene gate is the one gate a release cannot cut around"
    fi
done
# The (c) half: the deterministic cost reader (D-21) and the always-on context
# ceiling it measures (D-22). Neither is about what ships — both are about what
# every future run PAYS, which is the one release-time quantity nothing else in
# this file looks at.
#
# Why here rather than under a banner of its own: the numbering a run prints is
# the procedure (see `step()`), steps 9 and 10 are the point of no return, and
# inserting a new integer between 8 and 9 would renumber the push — which
# `tests/test_docs.py` reads back out of a real transcript by number. So this
# rides with the other measure-this-tree gates instead of moving the publish.
#
# Both arms run through `PYTHONPATH=$REPO/$PLUGIN python3 -P -m aggregator.costs`
# — the module-direct form `aggregator/mirror.py` already uses for the same
# reason (an operator tool is not a program a session runs, so it gets no
# wrapper and the count stays seven). `-P` because a release may be cut from
# any cwd; no network is reached by either arm, and neither writes anything.
#
# `PYTHONDONTWRITEBYTECODE=1` on both, and it is not decoration: the flag does
# NOT survive fork/exec, so without it every invocation leaves an
# `aggregator/__pycache__` inside the payload — a never-ship path that the
# `test_package.py` gate twelve lines above would flag on the NEXT cut, for a
# reason the operator did not cause. A release script that poisons the tree it
# just certified is a defect in the gate. Same rule `bin/`'s seven wrappers are
# held to by `tests/test_bin_wrappers.py`; the module-direct form is the one
# entry point that check cannot see.
#
# The report arm runs with `$REPO` as its cwd. It resolves the newest run
# itself (that is the point — a release prices the run that just happened), and
# that resolution walks UP from the cwd; this script is invoked from anywhere,
# so without the anchor the number in a release transcript is not necessarily
# this repo's.
#
# An ABSENT reader SKIPS rather than fails, the same bargain the two gates above
# make: this script is copied into minimal trees to be exercised (the fixture
# `tests/test_docs.py` builds is one, and it carries no payload modules at all).
COSTS_REL="$PLUGIN/aggregator/costs.py"
# The recorded ceiling, and the reason it is a REGRESSION threshold rather than
# D-22's target. Measured 2026-07-31 on this tree: CLAUDE.md ~8,400 tok +
# MEMORY.md + the ten skill descriptions ~= 9,500-10,200 tok owned. D-22's
# BUDGET is lower than that on purpose (6,000 + 800 + 1,400), and the file that
# declares it — `tests/test_context_budget.py` — is the thing that will make it
# bite. Until then a gate set at the target would be red on every release for a
# reason the operator cannot fix in this step, which is how a gate gets
# bypassed. So: hold the line where the tree is today plus room to breathe, and
# hand the decision over the moment the budget test exists — the reader prefers
# that file's numbers and says which source it used.
CONTEXT_CEILING="${RELEASE_CONTEXT_CEILING:-12000}"
if [ ! -f "$REPO/$COSTS_REL" ]; then
    skip "$COSTS_REL is not present in $REPO — no cost reader to run (a minimal tree carries none)"
else
    if PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO/$PLUGIN" python3 -P -m aggregator.costs \
            --baseline --repo "$REPO" --ceiling "$CONTEXT_CEILING"; then
        ok "the always-on context prefix is within the recorded ceiling"
    else
        fail "the always-on context prefix is OVER the ceiling printed above — every added kilobyte is re-read once per turn by every agent of every future run, so this is not a documentation nit; trim it, or raise RELEASE_CONTEXT_CEILING deliberately"
    fi
    # D-21's own invoke-and-print. A checkout with no run history prints "no
    # corpus" and exits 0 — a release cut from a clean tree must not go red for
    # having no history to price.
    if ( cd "$REPO" && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO/$PLUGIN" python3 -P -m aggregator.costs --top 5 ); then
        ok "the cost reader ran (no network; no corpus is a clean skip)"
    else
        fail "the cost reader exited non-zero — it reads recorded bytes and nothing else, so this is a defect in the reader, not a property of the release"
    fi
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
    # Already-published insurance (GD-C5). `claude plugin tag` cuts
    # `{name}--v{version}` — `touch--v0.2.0` — and a tag that already exists
    # means this version left the building once. Publishing the same number
    # twice is the failure a third-party marketplace cannot recover from:
    # installs are version-keyed, so the second push silently delivers nothing
    # to anyone who already has it.
    #
    # The name is read from the same manifest the version came from rather than
    # spelled out, so a rename cannot leave this line guarding a tag nobody
    # cuts; `tests/test_plugin_tree.py` pins the entry name to `touch`, which is
    # what makes the fallback below safe rather than a guess.
    #
    # The REMOTE is asked first, and that is the whole point of the guard: step
    # 7's `git fetch` happens later, so on a fresh clone — or any clone that has
    # not fetched since another machine cut the release — the local tag list is
    # simply silent about a version that is already published. `git ls-remote`
    # needs no fetch and no working tree. The local list stays as the offline
    # fallback: when ls-remote cannot reach the remote, this becomes a weaker
    # check that says so rather than a green one that lies.
    plugin_name="$(python3 -c 'import json,sys; n=json.load(sys.stdin).get("name"); print(n if isinstance(n,str) else "")' <<<"$manifest_json" 2>/dev/null)"
    [ -n "$plugin_name" ] || plugin_name="touch"
    release_tag="$plugin_name--v$version"
    #
    # GIT_TERMINAL_PROMPT=0 because this is the first network operation in a run
    # that otherwise reaches the network at step 7: against an auth-requiring
    # remote `git ls-remote` will sit on a credential prompt with no timeout, and
    # a `--check` that HANGS is worse than one that says it could not reach the
    # remote. Refusing the prompt turns that into the rc-carrying note below.
    remote_tags="$(GIT_TERMINAL_PROMPT=0 git ls-remote --tags "$REMOTE" "refs/tags/$release_tag" 2>/dev/null)"
    lsremote_rc=$?
    local_tags="$(git tag -l "$release_tag" 2>/dev/null)"
    if [ -n "$remote_tags" ] || [ -n "$local_tags" ]; then
        fail "tag $release_tag already exists ($([ -n "$remote_tags" ] && printf 'on %s' "$REMOTE" || printf 'locally')) — $version was published already; bump .version in $PLUGIN/$MANIFEST (and add its CHANGELOG entry) before releasing"
    elif [ "$lsremote_rc" -ne 0 ]; then
        note "could not read tags from $REMOTE (rc=$lsremote_rc) — the already-published check for $release_tag saw local tags only; re-run with the remote reachable before a real publish"
    else
        ok "no $release_tag tag exists on $REMOTE or locally — $version has not been published"
    fi
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
# GD-T7: validate is a SCHEMA check and NOTHING more. It passes a tree full of
# `sk-ant-` blobs without a murmur — the leak gate is `tests/test_package.py`,
# which step 2 runs in $REPO precisely because it skips itself anywhere else —
# and — measured on CLI 2.1.220 — it also passes a catalog whose
# entry `source` names a directory that does not exist: delete `plugin/` and
# `claude plugin validate .claude-plugin/marketplace.json --strict` still says
# ✔. So schema-valid does not mean installable, and this step says so twice:
# once here, and once in the resolution gate below, which does by hand what the
# CLI declines to do. `tests/test_plugin_tree.py` enforces the same resolution
# from the suite side (step 2) — belt and braces on purpose, because a future
# edit that reorders or skips step 2 would otherwise remove the guarantee
# invisibly. Always by explicit file path: a directory-level run does not check
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
#                        and `./plugin/touch` resolves to nothing. Validating
#                        working-tree bytes is only honest if those bytes are
#                        HEAD's, so the provenance gate below proves it
#                        directly (tracked, and identical to HEAD) rather than
#                        inheriting step 1's dirty-tree refusal — which a
#                        `--check` run records and walks straight past.
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
    else
        # Provenance first: the catalog is validated where it lies, so prove
        # the bytes on disk are the bytes a clone will read. Untracked would
        # mean the file simply is not in the clone; modified would mean this
        # gate validated a draft.
        provenance=ok
        if ! git ls-files --error-unmatch "$MARKETPLACE" >/dev/null 2>&1; then
            provenance=red
            fail "$MARKETPLACE is not tracked — a clone would carry no catalog at all; git add it"
        elif ! git diff HEAD --quiet -- "$MARKETPLACE"; then
            provenance=red
            fail "$MARKETPLACE differs from HEAD — this step validates the file on disk, which is not the file that ships; commit it"
        else
            ok "$MARKETPLACE is tracked and identical to HEAD (validated bytes == published bytes)"
        fi
        # The two statements are about different properties, so a green validate
        # under a red provenance is not a contradiction — but read one under the
        # other in a `--check` transcript it looks like one, so the qualifier is
        # carried into the line rather than left to the reader.
        if claude plugin validate "$REPO/$MARKETPLACE" --strict; then
            if [ "$provenance" = ok ]; then
                ok "$MARKETPLACE validates (repo root)"
            else
                ok "$MARKETPLACE validates (repo root) — of the bytes on disk, which the provenance failure above says are not the bytes that ship"
            fi
        else
            fail "$MARKETPLACE fails --strict validation"
        fi
        # The resolution the CLI does not do. One line per entry, resolved
        # against the marketplace root exactly as an install resolves it; a
        # parse failure prints nothing and is diagnosed as such below.
        #
        # LOCAL-PATH sources only — which is all this catalog has (one entry,
        # `./plugin/touch`). A `source` that is a URL or an `owner/repo`
        # shorthand would be joined onto $REPO like a path and then reported as
        # unresolved, so an entry of that kind needs its own arm here before it
        # is added, or the red it produces will not be a real one.
        entry_sources="$(python3 - "$REPO" "$MARKETPLACE" <<'PY' 2>/dev/null
import json, os, sys

root, rel = sys.argv[1], sys.argv[2]
with open(os.path.join(root, rel), encoding="utf-8") as fh:
    catalog = json.load(fh)
for entry in catalog.get("plugins") or []:
    src = entry.get("source") if isinstance(entry, dict) else None
    if isinstance(src, dict):          # {"source": "git", …} and friends
        src = src.get("path")
    if isinstance(src, str) and src:
        print(os.path.realpath(os.path.join(root, src)))
PY
)"
        payload_real="$(cd "$REPO/$PLUGIN" 2>/dev/null && pwd -P)"
        if [ -z "$entry_sources" ]; then
            fail "no local entry \`source\` could be read from $MARKETPLACE — an entry that resolves to nothing installs nothing"
        elif [ -z "$payload_real" ]; then
            fail "$PLUGIN does not exist in this checkout — the catalog's only local source has no payload behind it"
        else
            unresolved=""
            names_payload=no
            while IFS= read -r src; do
                [ -n "$src" ] || continue
                [ "$src" = "$payload_real" ] && names_payload=yes
                [ -f "$src/$MANIFEST" ] || unresolved="$unresolved${unresolved:+, }${src#"$REPO"/}"
            done <<<"$entry_sources"
            if [ -n "$unresolved" ]; then
                fail "entry source(s) with no $MANIFEST behind them: $unresolved — --strict passes this and the install fails"
            elif [ "$names_payload" != yes ]; then
                fail "no entry \`source\` resolves to $PLUGIN — the catalog and the payload have drifted apart"
            else
                ok "the entry source resolves to $PLUGIN and it carries $MANIFEST"
            fi
        fi
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
        note "$branch tracks nothing — step 9 will push it with -u to set tracking"
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

# --- 8. the tag gate, BEFORE the push --------------------------------------
# `claude plugin tag` is given the PLUGIN root, not the repo root: it looks for
# `<path>/.claude-plugin/plugin.json` and the repo root holds only the catalog
# (measured: `claude plugin tag . --dry-run` → rc=1, "No plugin manifest
# found"; `claude plugin tag plugin/touch --dry-run` → rc=0).
#
# It runs here, before step 9, and that placement is the whole point: as a
# post-push step it was a gate on the far side of the point of no return —
# guaranteed red on the first real release, and reporting it only after the
# repo had already been published. Here it is also reachable under `--check`.
#
# What it buys, now that it is aimed correctly: a dirty-tree refusal, a
# tag-already-exists refusal (`claude plugin tag --help`: `-f` is documented as
# skipping "the dirty-working-tree and tag-already-exists checks", so both are
# checks the dry run performs), and the `{name}--v{version}` tag itself.
#
# On the version-agreement claim, measured rather than assumed: the command
# validates "that plugin.json and any enclosing marketplace entry agree", and
# pointed at `$PLUGIN` it DOES walk up and find the root catalog — reproduced,
# it prints `Marketplace entry: plugins[0] in <repo>/.claude-plugin/
# marketplace.json`. So this is in fact the one place at release time where the
# manifest and the catalog entry are checked against each other. What it cannot
# report is a version disagreement, because the entry deliberately carries no
# `version` (GD-T9) — there is nothing there to disagree. That is a property of
# our catalog, not a blindness of the command; add a `version` to the entry and
# this gate starts having an opinion about it.
step 8 "claude plugin tag $PLUGIN --dry-run"
if ! command -v claude >/dev/null 2>&1; then
    skip "\`claude\` CLI not on PATH"
elif [ "$version" = "$UNREADABLE" ]; then
    # Same one-fault-one-diagnosis rule as steps 4 and 6: step 3 already
    # reported the unreadable manifest, and a tag gate that reads a version off
    # disk here would either contradict it or blame the tag machinery for it.
    skip "the version is unknown (step 3) — the tag gate has no release to dry-run"
elif claude plugin tag "$REPO/$PLUGIN" --dry-run; then
    ok "tag dry run is clean for $version (nothing was tagged)"
else
    fail "claude plugin tag --dry-run refused — fix it BEFORE publishing, which is the point of running it here"
fi

if [ "$mode" = check ]; then
    printf '\n'
    if [ "$failures" -gt 0 ]; then
        echo "release.sh --check: $failures gate(s) red — fix them before a real run." >&2
        exit 1
    fi
    echo "release.sh --check: every gate through step 8 is green; nothing was published."
    exit 0
fi

# --- 9. publish -------------------------------------------------------------
# The point of no return, and it is one command. There is no staging, no copy
# and no second repository: the marketplace IS this repo, so pushing it is the
# release. Everything above ran so that this line is boring.
step 9 "Push to $REMOTE"
if [ "$publishable" != yes ]; then
    note "nothing to push — $REMOTE already has this commit"
elif [ -z "$upstream" ]; then
    git push -u "$REMOTE" "$branch" && ok "pushed $branch to $REMOTE (tracking set)" \
        || fail "push failed"
else
    git push "$REMOTE" "$branch" && ok "pushed $branch to $REMOTE" || fail "push failed"
fi

# --- 10. push the tag (optional) --------------------------------------------
# The only half of the tag work that has to happen after the push: a tag names
# a commit, so it goes to the same remote the commit just went to — hence
# `--remote "$REMOTE"`, without which `RELEASE_REMOTE=fork` puts the commits on
# `fork` and the tag on `origin`, pointing at a commit that remote does not
# have. Optional because the `{name}--v{version}` tag only matters for plugin
# dependency constraints, which Touch has none of (DISTRIBUTION-5).
step 10 "claude plugin tag $PLUGIN --push"
if [ "$tag_push" != yes ]; then
    skip "--tag-push not requested — the tag matters only for plugin dependency constraints"
elif ! command -v claude >/dev/null 2>&1; then
    skip "\`claude\` CLI not on PATH"
else
    claude plugin tag "$REPO/$PLUGIN" --push --remote "$REMOTE" \
        && ok "tag pushed to $REMOTE" || fail "tag push failed"
fi

printf '\nrelease.sh: touch %s published — users get it with `/plugin marketplace update msdrx-tools` then `/plugin update touch@msdrx-tools`\n' "$version"
exit 0
