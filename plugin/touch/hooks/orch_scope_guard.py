#!/usr/bin/env python3
"""PreToolUse run-scope guard for orchestration loops.

While a run is active — `.touch/local-orchestrators/ACTIVE` lists the active
task names, one per line — subagent tool calls may touch only those tasks'
folders under `local-orchestrators/`. Every OTHER task keeps its `plan/`
readable (the authority ladder lives in old task folders) and has everything
else denied. A second, unrelated denial rides on the same live-run condition:
subagent `Write`/`Edit`/`NotebookEdit` calls targeting `.touch/memory/**` are
refused (G14 — see "Memory writes", below). Two deliberate non-restrictions:
the main terminal agent (no `agent_id` in the hook payload — the field is
present only for subagent calls) always sees everything, and with no sentinel
file the guard is inert on every tier, so ordinary sessions are unaffected.

The tasks root moved from `.claude/local-orchestrators` to
`.touch/local-orchestrators` (G10). BOTH spellings are consulted, in that
order, under whichever anchor resolves — `ORCH_CANDIDATES`, below. That is
not politeness toward old checkouts; it is what keeps the flip safe. The hook
is a fresh subprocess per tool call, so a one-sided edit would either read a
root the sentinels have not reached yet or a root they have already left, and
in both windows the guard AND the HALT brake go silently inert while a run is
live (PROTOCOL-2, SECURITY-11 — a security control failing open). With the
candidate pair, whichever root actually bears a sentinel decides, so the
order of the code edit and the physical `mv` is irrelevant, and a
still-absent `.touch/` root cannot swallow an `ACTIVE`-bearing legacy one.
`SEG_PATTERN` itself needed no change at all: the leaf name was deliberately
kept, and the match is a bare substring on `local-orchestrators/`, so both
spellings are enforced throughout the transition (PROTOCOL-12, G10). The
legacy candidate is dropped in a separate follow-up once the move is
accepted, not here.

The two sentinels are resolved DIFFERENTLY across that candidate pair, and
the asymmetry is the point. `ACTIVE` picks exactly ONE root (`pick_root`):
unioning two ACTIVE files would let a stale legacy sentinel WIDEN a live
run's scope, the one direction a stale sentinel must never go. `HALT` is a
union over every candidate (`find_halt_any`): a brake can only ever
over-restrict, and it must fire wherever the operator reaches for it —
including the pre-move spelling that every finished run's `RESUME.md`,
`orch-config.json` and `orch-scripts/` still name, because G11 forbids
rewriting historical folders. Honouring the brake at only the root that
happens to bear `ACTIVE` would mean `touch
.claude/local-orchestrators/HALT` creating a file, printing no error, and
freezing nothing (PROTOCOL-2, PROTOCOL-3).

Registered exactly ONCE, by the plugin (GD-U5): `hooks/hooks.json` in **exec
form** (`command: "python3"`, `args: ["${CLAUDE_PLUGIN_ROOT}/hooks/…"]` —
`args[]` is substituted, a shell-form `command` string is not). This repo's
`.claude/settings.json` used to register the same script a second time in
shell form with an identical matcher; both were live at once in the dogfood
loop and the hook fired twice per tool call (measured 2 vs 1), so that block
is gone and `"enabledPlugins": {"touch@inline": true}` is committed there
instead — every `claude --plugin-dir plugin/touch` session auto-enables the
one registration. Accepted consequence: a session started WITHOUT the plugin
has no guard. That is fine — the guard is inert without an ACTIVE file
anyway, and every orchestration run already needs the plugin, whose `bin/` is
the driver toolchain. Stdlib only.

Where the sentinels are looked for — GD-T5's task-state order, which item
04's `resolve_tasks_root()` is to adopt for `status.sh` and both monitoring
daemons (as of 2026-07-28 they do not read `$ORCH_TASKS_ROOT` at all yet, so
today this is the guard's own resolution order and nobody else's):

1. `$ORCH_TASKS_ROOT`, honoured only when it names a directory that
   **exists**, so a stale export or a typo falls through to the project
   anchor instead of disarming the guard. A value that exists but is the
   **wrong** directory still wins, and then the guard and the HALT brake go
   quietly inert. The likely wrong value is `$ORCH_STATE_DIR`'s *task*
   directory — one word apart, set by every `status.sh` call in this repo,
   and holding neither sentinel. Export the tasks root itself: the directory
   that holds `ACTIVE`, `HALT` and the task folders. Tier 2 is not
   existence-conditioned at all, so a wrong `$CLAUDE_PROJECT_DIR` disarms
   the guard the same way; that one is set by the harness rather than by
   hand, which is the only reason it is left unchecked.
2. `$CLAUDE_PROJECT_DIR` joined with each of `ORCH_CANDIDATES` in turn —
   `.touch/local-orchestrators` first, the legacy `.claude/local-orchestrators`
   second; for `ACTIVE` the first one bearing a sentinel wins, and if neither
   does the new spelling is returned as the "nothing here" answer (it is the
   one a deny reason should name);
3. a walk up from the payload `cwd` that stops at the FIRST ancestor holding
   a `.claude/` directory, then the same candidate pair under it. The marker
   dir and the state dir are deliberately DIFFERENT now (`.claude/` marks a
   *Claude Code* project; `.touch/` is created by Touch and is gitignored, so
   it cannot mark one — G10, PROTOCOL-23). The marker is the ceiling: a stray
   `~/.claude/local-orchestrators/ACTIVE` left behind by an old run can never
   restrict an unrelated project that has a `.claude/` of its own.

Tier 1 is not decoration — once item 04 lands, a driver that points the
daemons at an out-of-project tasks root moves ACTIVE and HALT with them, and
a guard that only knew the project anchor would silently stop enforcing
anything (HALT included) for that whole run. It only helps when the variable
is **exported into the `claude` process**, though: a hook inherits `claude`'s
environment, so the per-command form this repo's protocol uses for its
sibling variables (`ORCH_STATE_DIR=… python3 decision_watcher.py`) would be
invisible here. Export it before launching `claude`, or leave the run state
under the project.

This fires on every matched tool call of every session in every project the
plugin is enabled in, so the cost is bounded three ways:

- `CLAUDE_PLUGIN_OPTION_RUN_SCOPE_GUARD=false` (the plugin's
  `run_scope_guard` userConfig, default true) exits before anything else
  happens — including before HALT is consulted, so turning the "scope guard"
  off also removes the HALT emergency brake, which is a feature of this same
  hook;
- fast-inert: with an anchor (either of the first two tiers) and no sentinel
  present under ANY of its candidate roots, the process exits without even
  reading stdin;
- the anchored sentinel lookup is exactly ONE `isfile` per sentinel per
  candidate root — at most four `stat` calls at two known directories, no
  scanning, no climbing — and it is **ceilinged at the project root**: a
  stray ACTIVE file in some ancestor can never restrict an unrelated project,
  and a nested one below the anchor is ignored too, because the fast-inert
  check runs before stdin is read and therefore before any `cwd` is known:
  one rule, two known locations, no path-dependent behaviour. The tier-3
  climb survives only for the case where neither anchor is set, and it stops
  at the first `.claude/` marker, so it is ceilinged as well — never a walk
  to `/`. Tier 3 cannot be asked before stdin is read (the `cwd` is in the
  payload), so `main()` asks the very same "is a run live here?" question a
  second time after parsing, on the tier that could not answer it earlier —
  which is what makes "inert without a sentinel" true on all three tiers
  rather than on two of them.

Measured 2026-07-28 on this machine, four times (after the rewrite, after the
`$ORCH_TASKS_ROOT` tier, after the tier-3 marker ceiling, after this
disclosure-and-deny-reason pass), six 20-run loops each, one subprocess per
call: **~22-24 ms/call on the fast-inert path against a ~22-24 ms
bare-interpreter floor** (`python3 -c pass`) — the guard's own share lands
between 0 and 3 ms across all four measurements, i.e. inside the run-to-run
noise of process start — and ~33-38 ms when a run really is active and the
payload is parsed. The off-switch and `$ORCH_TASKS_ROOT`-inert paths measure
the same ~22-23 ms. The pre-rewrite number was 33 ms against an 18 ms floor
(DISTRIBUTION-8, E7). The candidate pair added afterwards costs at most two
extra `stat` calls on the fast path — microseconds against a ~22 ms
interpreter start — the HALT union at most two more, the post-parse live
check at most two more, and the memory check one compiled regex over one or
two short strings, only on calls that got past the fast path. Those are
arithmetic, not a re-measurement; nothing above was re-timed for them.

Caveat on the fast-inert path, recorded because it is assumed rather than
tested: exiting before reading stdin means the harness's write to this
process's stdin fails with EPIPE once the payload exceeds the pipe buffer.
Measured here against this file: a 32 KB payload is absorbed by the buffer, a
64 KiB+ one (e.g. a large `Write` body) raises `BrokenPipeError` in a raw
`write()`. `subprocess.communicate()` swallows it, which is why the test
suite never sees it. Early-exit PreToolUse hooks are ordinary, so the harness
very likely tolerates this, but "very likely" is not a measurement — the fast
path has only ever run under the tests, never under the real harness. Verify
once on an install (a project with no `.claude/local-orchestrators/`, a
>64 KiB Write) before treating it as proven.

Memory writes (G14, SECURITY-12). `.touch/` now holds the run history AND the
Claude Code memory directory (`.touch/memory/`, mapped there by
`autoMemoryDirectory`). Co-location handed subagents a capability strictly
larger than the one this guard exists to withhold: an agent denied
`…/task-b/findings/x.md` could still `Write` `.touch/memory/MEMORY.md` and
edit the instructions every FUTURE session in this project loads. So while a
run is live at the resolved tasks root, a subagent
`Write`/`Edit`/`NotebookEdit` naming `.touch/memory/` is denied outright —
before the active-task LIST is consulted, because an empty or stale ACTIVE
must not become a way in, but after the same live-run condition the scope
clause itself needs, because a project that has never run Touch must be left
alone. Memory edits go through the monitoring server's default-off write
plane, or through the main terminal agent. This is defense in depth, decided
rather than inherited; it is not a containment claim (see the last bullet
below).

Argument shape (D-20). The scan is no longer "every string in `tool_input`".
`TOOL_KEYS` splits each matched tool's inputs into the keys it RESOLVES as a
filesystem path (`file_path`, `Glob`'s `pattern`, `Grep`'s `path`/`glob`, …)
and the keys that are free text (`Bash`'s `command`), and the deny reason says
which kind matched. Two defects made that necessary, both reproduced:

- `Grep`'s `pattern` is a **regex over file CONTENT**, never a target. A
  researcher grepping for the literal text `local-orchestrators/[^/]+` was
  denied for "naming task `[^`" — a task that does not exist, on a call that
  read nothing (MONITORING-12). That key is no longer scanned at all: it
  cannot select a file, so refusing it protected nothing and the deny sent the
  reader hunting a scope problem that was not there. This is the one place the
  guard was deliberately narrowed; everywhere else over-restriction remains
  the safe direction, and an unknown tool falls back to `UNKNOWN_TOOL_KEYS`
  where `pattern` and `command` are scanned as TEXT — still denied, honestly
  labelled.
- A `Bash` command string is not a path and cannot be made into one here, so
  the substring match stays — but the reason now says "an argument … mentions
  task 'X'" and calls the match textual, never "this call names task 'X'".
  The guard genuinely does not know whether that command would have touched
  the folder: `grep -rn "local-orchestrators/task-b" .` reads nothing, and
  `cd .touch/local-orchestrators && cat task-b/events.jsonl` touches
  everything while matching nothing (ECONOMICS-10, the bullet below). A deny
  message that asserts an access the guard cannot observe is a false
  accusation, and it was costing real retries.

What the guard does NOT promise:

- **Bash coverage is textual, and it is a name-based speed bump.** The hook
  sees only the command string, so it denies commands that MENTION another
  task's folder, and the same access spelled differently walks past it: a
  `cd .touch/local-orchestrators` followed by bare relative paths
  (`cat task-b/events.jsonl`) is not matched and runs (ECONOMICS-10, measured,
  pinned by `test_argument_shape`). Resolving candidate tokens against the
  command's effective cwd is not possible from the payload — the `cwd` field
  is the tool call's, not the state a `cd` earlier in the same command line
  would produce. That is the right strength for the actual threat — loop
  agents drifting into other tasks' state and getting distracted — not an
  adversarial sandbox, and the deny wording now says so rather than implying
  containment.
- **The match is textual for path arguments too, and first-segment only**, so
  traversal through a permitted segment is not detected. Nothing is
  normalized first, so `task-a/../task-b/findings/f.md` matches on the ACTIVE
  `task-a` and passes, and `task-b/plan/../findings/f.md` satisfies the
  `plan/` exception (both relative to the tasks root). Pre-existing,
  unchanged here, pinned by tests, and of a piece with the bullet above: this
  bounds drift, it does not contain a determined caller.
- **A wildcard task segment is passed for the read-only tools**
  (Read/Glob/Grep), so `Grep path=.claude/local-orchestrators/*/findings`
  reads other tasks' content. This is a deliberate read bypass, not a fuzzy
  match: `task-?` and `[t]ask-b` name exactly one task and are passed just
  the same. The guard prevents *drift* into a named task, not *containment*:
  a deliberate glob still reads. The exception is refused for Bash, where
  `.claude/local-orchestrators/*` is as easily the target of an `rm -rf`.
- Consequently the friction that motivated the wildcard clause is only half
  gone: the audit's `git log --name-only -- '.claude/local-orchestrators/*'`
  is a **Bash** call and stays denied (split the string, or use Glob/Grep).
- **The `plan/` exception is granted to Bash too** (`READ_TOOLS` below), so a
  Bash command may write to or delete another task's `plan/` files — only the
  structured editors (Edit/Write) are held out. Again: the guard bounds
  drift, it does not contain writes. Narrowing it to Read/Glob/Grep is a
  behaviour change beyond item 02's clause list, so it is disclosed rather
  than done.
- **The match is not anchored to the resolved tasks root.** `SEG_PATTERN` is
  a bare substring match, so ANY string containing
  `local-orchestrators/<name>` is treated as a task reference — including a
  path in `/tmp`, in another project, or in prose. Measured, not hypothesised
  (LAYOUT-16): a `mkdir -p` naming `$SCRATCH/.touch/local-orchestrators/t1`
  under a scratch dir outside the project was denied. The consequence used to
  be a deny reason quoting a synthesised path that did not exist; that half
  is fixed — the reason now quotes the **matched text** and names the
  resolved root separately (PROTOCOL-13) — but the over-match itself is
  unchanged. Anchoring it (requiring the resolved root, or its
  project-relative tail, immediately before the match) is a behaviour change
  beyond this item, so it is disclosed, not done.
- **The memory deny is textual and names three tools, only two of which the
  matcher actually delivers.** It matches `.touch/memory/` in a path key of
  `Write`/`Edit`/`NotebookEdit`, so it is root-agnostic in the same way as
  the clause above (a `.touch/memory/` under some other project matches too).
  `NotebookEdit` is NOT in `hooks/hooks.json`'s matcher — that closed set is
  pinned by `test_registration` — so the arm for it here is future-proofing,
  not enforcement; the practical risk of the gap is nil, because memory files
  are `.md` and `NotebookEdit` edits `.ipynb`. Widening the matcher is a
  decision recorded in that test, not a change to smuggle in here. `Bash` is
  not covered either — a subagent `Bash` heredoc into `.touch/memory/x.md`
  passes, exactly as `Bash` writes into another task's `plan/` pass. The
  write body is not scanned, only the path. This bounds the accident and the
  drift, not a determined caller; the containment guarantee for memory is the
  write plane's (default-off, loopback, token-gated, containment-checked),
  never this hook's.

A crashed run leaves a stale ACTIVE behind, which only over-restricts and
says so in every deny reason; `rm .touch/local-orchestrators/ACTIVE` clears
it (or the legacy `.claude/local-orchestrators/ACTIVE` — the deny reason
names the root that actually decided, so there is no guessing which one is
armed).
"""
import os
import sys

# `json` and `re` are imported inside the functions that need them, never at
# module level: together they cost ~14 ms of a ~15 ms per-call overhead, and
# neither the off-switch nor the fast-inert path ever gets far enough to want
# them. Re-importing later is a `sys.modules` lookup.

# A path segment right after local-orchestrators/, then the rest of that path.
# BYTE-IDENTICAL across the tasks-root move, and that is a decision, not luck:
# the leaf name `local-orchestrators` was deliberately kept (G10, PROTOCOL-12)
# precisely so this pattern — which is location-agnostic — keeps enforcing
# both spellings during the transition. A rename to something generic
# (`runs/`) would have made it match `plugin/touch/runs/`,
# `node_modules/x/runs/` and friends in every project the plugin is enabled
# in.
# Stops at whitespace and shell metacharacters so it works on Bash command
# strings as well as plain paths and glob patterns. A segment carrying a glob
# character never equals an active task name, so it is denied by default and
# passed only for the read-only tools below — a deliberate bypass (`task-?`
# names one task as surely as `task-b` does), not a fuzzy match.
# Compiled in main(), for the reason above.
SEG_PATTERN = r"local-orchestrators/+([^/\s\"';|&]+)((?:/[^\s\"';|&]*)?)"
# Which `tool_input` keys are scanned, per tool, and HOW (D-20): the first
# tuple is the keys the tool resolves as a filesystem PATH, the second the keys
# that are free TEXT a path may merely be mentioned in. Both are denied on a
# match — over-restriction stays the safe direction — but they earn different
# deny wording, because only the first is evidence that a path was named.
#
# `Grep`'s `pattern` is in NEITHER: it is a regex over file content, so it can
# never select a target, and matching it produced denies for tasks that do not
# exist on calls that read nothing (MONITORING-12). `Glob`'s `pattern`, by
# contrast, IS the path expression, so it stays path-shaped. `Grep`'s `glob` is
# a filename filter and can carry a path prefix, so it is scanned too.
# `notebook_path` is inert while NotebookEdit is off the matcher; it costs
# nothing and keeps the guard honest if the matcher ever grows again.
TOOL_KEYS = {
    "Read":         (("file_path",), ()),
    "Write":        (("file_path",), ()),
    "Edit":         (("file_path",), ()),
    "NotebookEdit": (("notebook_path",), ()),
    "Glob":         (("pattern", "path"), ()),
    "Grep":         (("path", "glob"), ()),
    "Bash":         ((), ("command",)),
}
# A tool the matcher grows into later, or a payload naming something unknown:
# scan everything that has ever been a path key as a path, and the two
# ambiguous ones as text. The result is the pre-D-20 breadth (nothing new gets
# through) with the honest wording — the narrowing above is a decision about
# `Grep` specifically, not a general licence to stop looking.
UNKNOWN_TOOL_KEYS = (("file_path", "path", "notebook_path"),
                     ("command", "pattern"))
READ_TOOLS = {"Read", "Glob", "Grep", "Bash"}
WILDCARD_TOOLS = {"Read", "Glob", "Grep"}
# The three structured editors — the tools whose whole job is to put bytes in
# a named file, which is what the memory deny is about (G14). `Bash` is out on
# purpose: see the last "does NOT promise" bullet. `NotebookEdit` is off the
# matcher today, like `notebook_path` above, and is listed for the same
# reason — future-proofing, not enforcement.
WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}
# The memory directory, matched as a segment pair with a REQUIRED separator
# after `memory` — same character class as SEG_PATTERN. The trailing slash is
# what keeps the neighbouring `.touch/memory-audit.jsonl` (and any other
# `memory`-prefixed name) out of the deny: this clause is about the
# instruction files, and a matcher that quietly caught more would be the kind
# of overreach the "does NOT promise" list exists to prevent. Compiled in
# main().
MEMORY_PATTERN = r"\.touch/+memory/+[^\s\"';|&]*"
# The sentinels themselves are never "another task": the agents a HALT governs
# must be able to name it, and a run's own ACTIVE line is its handle. The same
# two names are what `has_sentinel` looks for when it picks a candidate root.
SENTINELS = {"ACTIVE", "HALT"}
GLOB_CHARS = "*?["
OFF_VALUES = {"false", "0", "no", "off"}
#: The tasks-root spellings an anchor may carry, in preference order: the
#: current one first, the pre-G10 one second. Both are consulted everywhere —
#: `candidate_roots` is the ONE place that order is expanded, and the
#: fast-inert check, the ACTIVE resolution and the HALT union all go through
#: it, so they cannot disagree. The legacy entry leaves in a separate
#: follow-up item, once the physical move has been accepted (G11 step 9);
#: until then it is what keeps HALT live across the flip.
ORCH_CANDIDATES = ((".touch", "local-orchestrators"),
                   (".claude", "local-orchestrators"))


def guard_disabled():
    """The plugin's `run_scope_guard` userConfig, false → do nothing at all."""
    value = os.environ.get("CLAUDE_PLUGIN_OPTION_RUN_SCOPE_GUARD", "")
    return value.strip().lower() in OFF_VALUES


def project_root():
    """The project anchor, or None. The hook is the ONE component that gets
    `$CLAUDE_PROJECT_DIR` in its environment — with it set, the sentinel
    lookup is one `isfile` at a known path instead of a climb.

    A relative value is resolved against THIS process's cwd, not the payload
    `cwd`: the fast-inert path runs before stdin is read, so the payload's cwd
    is not yet known there, and one rule for both call sites beats two. The
    harness exports an absolute path; `.claude/settings.json` writes
    `${CLAUDE_PROJECT_DIR:-.}` only as a shell fallback.
    """
    root = os.environ.get("CLAUDE_PROJECT_DIR")
    return os.path.abspath(root) if root else None


def tasks_root_override():
    """GD-T5 tier 1: `$ORCH_TASKS_ROOT` names the tasks root outright and
    outranks the project anchor. Item 04 gives `status.sh` and both monitoring
    daemons the same tier; after that, a run which sets it keeps ACTIVE and
    HALT there and nowhere else. Two conditions the module docstring spells
    out: it must be EXPORTED into the `claude` process to reach this hook at
    all, and it must name the tasks root — a *task* directory exists too, so
    it wins the tier and then finds no sentinel."""
    root = os.environ.get("ORCH_TASKS_ROOT")
    return os.path.abspath(root) if root else None


def marker_root(start):
    """GD-T5 tier 3: the first ancestor of `start` holding a `.claude/`
    directory — the project marker, and the ceiling of the climb. Returns None
    when there is no marker anywhere above `start` (then nothing is enforced,
    which is the right answer: there is no project here)."""
    d = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(d, ".claude")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def has_sentinel(base):
    """True when `base` bears ACTIVE or HALT — i.e. when it is the tasks root
    a run is actually using. At most two `isfile` calls, no listing."""
    return any(os.path.isfile(os.path.join(base, name)) for name in SENTINELS)


def candidate_roots(anchor):
    """Every tasks-root spelling under `anchor`, in `ORCH_CANDIDATES` order.

    The ONE place the candidate order is expanded. Checks that must choose a
    single root go through `pick_root`; checks that may only ever
    over-restrict — `find_halt_any` — read the whole list.
    """
    return [os.path.join(anchor, *rel) for rel in ORCH_CANDIDATES]


def pick_root(roots):
    """The ONE tasks root out of `roots`: the first bearing a sentinel, else
    the first member. None for an empty list (no tasks root here at all).

    This is the whole of the fail-closed rule for `ACTIVE`: a `.touch/` root
    that does not exist yet — or exists and is empty — cannot swallow an
    `ACTIVE`-bearing legacy `.claude/` root, so the guard stays live no matter
    which side of the physical move the code is on (PROTOCOL-2, SECURITY-11).
    When NEITHER bears a sentinel the answer is the first candidate, the new
    spelling: nothing is being enforced either way, and it is the root a deny
    reason or an error message should name.

    Deliberately NOT a merge of both roots' ACTIVE files. Two armed roots at
    once is a broken state, not a supported one, and unioning them would let a
    stale legacy `ACTIVE` widen a live run's scope — the one direction a stale
    sentinel is not supposed to be able to go (it may over-restrict, never
    over-permit). `HALT` is unioned precisely because it inverts that
    argument; see `find_halt_any`.
    """
    for base in roots:
        if has_sentinel(base):
            return base
    return roots[0] if roots else None


def anchor_roots(anchor):
    """GD-T5 tiers 1-2 as a list of tasks roots, in preference order; `[]`
    when neither tier supplies one. `anchor` is the project root or None.

    The override is honoured only when it exists, so a stale export or a typo
    falls through to the project root instead of beating a populated one. An
    override that exists but names the WRONG directory — a *task* directory,
    say — still wins this tier and finds no sentinel there, leaving the guard
    and the HALT brake inert. That residue is disclosed in the module
    docstring and pinned by `test_tasks_root_override`, not closed in code: a
    tasks root that legitimately has no sentinel yet must stay resolvable.
    Note that the candidate pair does NOT extend to the override: it names the
    tasks root outright, with no `.touch`/`.claude` component to choose
    between, so there is nothing to fall back to — and nothing to union for
    HALT either.
    """
    override = tasks_root_override()
    if override is not None and os.path.isdir(override):
        return [override]
    if anchor is not None:
        return candidate_roots(anchor)
    return []


def resolved_roots(start, anchor):
    """EVERY tasks root in play for this call, in preference order; `[]` when
    there is none. The ONE place the tier order lives, so `main()`'s
    fast-inert check, the ACTIVE resolution and the HALT union can never
    disagree about where the sentinels are.

    With an anchor — `$ORCH_TASKS_ROOT`, else the project root — that anchor
    is the ceiling: a sentinel in an ancestor cannot restrict this project,
    and a sentinel nested deeper does not either, because the fast-inert check
    in `main()` runs before stdin is read and therefore before any cwd is
    known. One rule, one anchor, no path-dependent behaviour.

    With no anchor at all, tier 3 climbs from `start` to the first `.claude/`
    marker and stops there, then takes the candidate pair under it. The marker
    dir and the state dir differ on purpose (`.claude/` marks the project,
    `.touch/local-orchestrators/` holds the runs — G10). That marker is a
    ceiling too: an unrelated ancestor's tasks root (a forgotten
    `~/.claude/local-orchestrators/ACTIVE`) must never restrict a project that
    has its own `.claude/`.
    """
    roots = anchor_roots(anchor)
    if roots:
        return roots
    marker = marker_root(start)
    if marker is not None:
        return candidate_roots(marker)
    return []


def anchored_orch_dir(anchor):
    """The single tasks directory tiers 1-2 point at, or None if neither
    resolves — what `main()`'s fast-inert check asks before stdin is read."""
    return pick_root(anchor_roots(anchor))


def find_active(base):
    """The set of active task names (one per line, blanks ignored) from the
    ACTIVE sentinel in the resolved tasks root — empty set if no run is
    active (or if no tasks root resolved at all)."""
    if base is None:
        return set()
    p = os.path.join(base, "ACTIVE")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return {ln.strip() for ln in f if ln.strip()}
        except OSError:
            return set()
    return set()


def find_halt(base):
    """The HALT sentinel next to ACTIVE in `base`. While it exists, EVERY
    subagent tool call is denied — the user's emergency brake for an
    orchestration run that has no reachable kill handle. Deleting the file
    lifts the freeze; the main terminal agent is never affected."""
    if base is None:
        return None
    p = os.path.join(base, "HALT")
    return p if os.path.isfile(p) else None


def find_halt_any(roots):
    """The first HALT among `roots`, or None — a UNION over the candidate
    pair, deliberately unlike `pick_root`'s choose-one for ACTIVE.

    A brake can only ever over-restrict, so widening where it is read is safe
    in the one direction that matters; and it MUST be read at both spellings,
    because the move leaves `.claude/local-orchestrators` named all over the
    historical record G11 forbids rewriting (`RESUME.md`, `orch-config.json`,
    every finished run's `orch-scripts/`), and a surviving daemon can even
    re-create that tree (PROTOCOL-3). Reading HALT only at the root that
    happens to bear `ACTIVE` would turn `touch
    .claude/local-orchestrators/HALT` into a file that exists, prints no
    error and freezes nothing, while the operator believes the run is stopped
    — a security control failing open (PROTOCOL-2, SECURITY-11), and the exact
    direction this module promises it cannot fail in.

    With `$ORCH_TASKS_ROOT` there is only ever one root in the list, so the
    union is a no-op there (`anchor_roots`).
    """
    for base in roots:
        p = find_halt(base)
        if p:
            return p
    return None


def scan_keys(tool):
    """The `tool_input` keys to scan for `tool`, as `(key, kind)` pairs with
    `kind` in `{"path", "text"}` — `TOOL_KEYS`, falling back to
    `UNKNOWN_TOOL_KEYS`. The ONE place the classification is read, so the
    memory clause and the scope clause cannot disagree about what a path is.
    """
    path_keys, text_keys = TOOL_KEYS.get(tool, UNKNOWN_TOOL_KEYS)
    return ([(k, "path") for k in path_keys]
            + [(k, "text") for k in text_keys])


def deny(reason):
    import json
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


def main():
    if guard_disabled():
        return
    root = project_root()
    # The sentinel checks below are repeated after the payload is parsed. That
    # duplication is DELIBERATE: this check must happen before stdin is read
    # (item 02(b)) while the enforcing path runs after, and both must go
    # through `resolved_roots`/`pick_root` — one resolver, two callers.
    # Collapsing them is how the fast path and the enforcing path drift apart.
    # `has_sentinel` on the root `pick_root` chose is enough for BOTH candidate
    # spellings: it already returned a sentinel-bearing one if either had one,
    # so "the chosen root has no sentinel" means no candidate did.
    base = anchored_orch_dir(root)
    if base is not None:
        if not has_sentinel(base):
            return  # fast-inert: no run here — stdin is never even read
    import json
    try:
        hook = json.load(sys.stdin)
    except Exception:
        return  # a broken payload must never block work
    if not hook.get("agent_id"):
        return  # main terminal agent: unrestricted
    start = hook.get("cwd") or os.getcwd()
    # Resolved once, here: the deny reason must name the tasks root that
    # actually decided, not a hardcoded one the agent cannot act on when tier
    # 1 or tier 3 chose somewhere else — or when the candidate pair chose the
    # legacy spelling because that is where the sentinels still are.
    roots = resolved_roots(start, root)
    tasks = pick_root(roots)
    # HALT first, and over EVERY candidate root: the brake outranks
    # everything, and reading it at only the ACTIVE-bearing root would make it
    # fail open at the other spelling (`find_halt_any`).
    halt = find_halt_any(roots)
    if halt:
        deny("RUN HALTED by the user: the orchestration run this subagent "
             "belongs to was ordered stopped. Do not retry any tool call. "
             "Return immediately with a short note that the run is halted. "
             f"(sentinel: {halt})")
        return
    # The same question the fast-inert check asked, asked once more — for tier
    # 3, which could not be resolved before stdin was read because the `cwd`
    # arrives in the payload. Without it the memory clause below would fire in
    # projects that have never run Touch (a tier-3 call reaches it with no
    # sentinel anywhere), which is both the path-dependent behaviour this
    # module says it does not have and a straight contradiction of "with no
    # sentinel file the guard is inert". HALT has already returned above, so
    # what is left to find here is an ACTIVE file — present but EMPTY still
    # counts, deliberately: a half-written sentinel is a live run.
    if tasks is None or not has_sentinel(tasks):
        return
    tool = hook.get("tool_name", "")
    tool_input = hook.get("tool_input") or {}
    import re
    # G14, before the ACTIVE LIST is consulted (but after the live-run check
    # above): memory is off-limits to subagents for as long as a run is live
    # at the resolved root, so an empty or stale ACTIVE cannot become the way
    # in. The main terminal agent already returned above; HALT already denied
    # everything above that.
    if tool in WRITE_TOOLS:
        mem = re.compile(MEMORY_PATTERN)
        for key, _kind in scan_keys(tool):
            value = tool_input.get(key)
            if not isinstance(value, str):
                continue
            m = mem.search(value)
            if m:
                deny("Run scope: subagents may not write Claude Code memory "
                     f"— matched '{m.group(0)}'. Files under .touch/memory/ "
                     "are loaded into every future session in this project, "
                     "which is a wider capability than this run grants. Use "
                     "the monitoring page's memory editor (write plane, "
                     "default off), or ask the main terminal agent.")
                return
    active = find_active(tasks)
    if not active:
        return
    seg = re.compile(SEG_PATTERN)
    for key, kind in scan_keys(tool):
        value = tool_input.get(key)
        if not isinstance(value, str):
            continue
        for m in seg.finditer(value):
            task, rest = m.group(1), m.group(2) or ""
            if task in active or task in SENTINELS:
                continue
            if tool in READ_TOOLS and (rest == "/plan" or rest.startswith("/plan/")):
                continue  # authority-ladder plans stay readable
            if tool in WILDCARD_TOOLS and any(c in task for c in GLOB_CHARS):
                continue  # a read-only glob over the runs is not a scope breach
            names = ", ".join(f"'{t}'" for t in sorted(active))
            # The claim is scoped to what the guard can actually observe
            # (D-20). A path argument really does name the task. A free-text
            # argument only MENTIONS it: the guard cannot tell `cat
            # …/task-b/x` from `grep "…/task-b" .`, denies both, and must say
            # so — asserting an access it never saw sent readers hunting scope
            # problems that did not exist (MONITORING-12, ECONOMICS-10).
            if kind == "path":
                claim = (f"The path argument '{key}' of this call names task "
                         f"'{task}', which is not one of them")
            else:
                claim = (f"An argument of this call ('{key}') mentions task "
                         f"'{task}', which is not one of them — the match is "
                         "TEXTUAL, so the guard cannot tell a path from a "
                         "mention and denies either way")
            # The matched TEXT, not a path built from it: `os.path.join(tasks,
            # task) + rest` used to present a synthesised path the caller
            # never typed (and which often did not exist, since the match is
            # not anchored to the resolved root — LAYOUT-16). The resolved
            # root is still reported, separately and labelled, because it is
            # what `pick_root` actually decided and the only root an agent can
            # act on — and after the tasks-root move there are two plausible
            # ones, so naming it is the difference between a debuggable deny
            # and a guess (PROTOCOL-13).
            deny(f"Run scope: the active task(s): {names}. {claim}; during "
                 "this run only active tasks' folders and other tasks' plan/ "
                 f"files are accessible. (matched '{m.group(0)}'; resolved "
                 f"tasks root: {tasks})")
            return


main()
