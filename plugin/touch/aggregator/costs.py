"""`aggregator/costs.py` — the deterministic cost reader (D-21).

What this module is for
-----------------------
"How much did this run cost, and where did the money go" was, until this file,
a **non-deterministic** flow: an LLM read `agent-*.jsonl` by hand and did the
arithmetic (every figure in `findings/research-economics-attempt-1.md` came
out of ad-hoc Python written inside a research agent). The inputs were always
on disk — `message.usage` is written on every billed assistant turn — so the
flow is a pure function of recorded bytes and belongs in a script. That is the
whole item: a conversion of the *measuring instrument* itself, which is what
makes D-22's context budget maintainable rather than a number that rots
(ECONOMICS-8, SUBSTRATE-7).

**Not a wrapper, and deliberately so.** This is an operator tool on
`mirror.py`'s footing — `PYTHONPATH=plugin/touch python3 -m aggregator.costs`
— not an eighth entry in `bin/`. GD-U4 counts one wrapper per program a
*session* runs; nothing in a session runs this, `scripts/release.sh` and a
human do. The wrapper count stays seven.

What it reads, and what it never does
-------------------------------------
Two file kinds under one run, both `~/.claude`-resident and both **read-only**
(the read-only-tap law is absolute; this module opens nothing for writing and
resolves nothing it did not find on disk):

* `…/<sessionId>/subagents/workflows/<runId>/agent-<id>.jsonl` — the workflow
  agents;
* `…/<sessionId>.jsonl` — the **driver**'s own transcript, the main terminal
  agent that wrote the workflow script, seeded the cards and closed the run.
  It is resolvable from a `wf_dir`: parent-of-parent-of-parent is the session
  *directory*, and the transcript is that directory's sibling, named for it
  (SUBSTRATE-7, verified on this machine).

A run is not one directory
--------------------------
The `wf_dir` a task folder records is an **anchor**, never the corpus. A
`/clear` mid-run gives the process a new sessionId while the runId stays the
same, so the run's later agent transcripts land under the *new* session
directory (R-49) — and a resumed run has one **driver** transcript per session
as well. Reading only the anchor is therefore not a corner case: of the three
runs `findings/research-economics-attempt-1.md` decomposed, two are resumed,
and the anchor alone under-reports one of them by 92 % (5 agents of 70).

So :func:`analyze` expands the anchor with `ingest.find_run_dirs` — the same
plural finder `read_run` uses, under the same `ingest._run_scope` fence, so a
foreign project holding a directory of the same runId cannot contribute — and
folds **every** session's agents into one accumulator and **every** session's
driver transcript into another. The report names `runDirs` and `sessionIds`,
so a resumed run is visible rather than merely correct. `--single-session`
(`expand=False`) is the escape hatch for an operator who means one directory.

The driver row is the point of that second half. The dashboard's token
accounting covers workflow subagents only, so the main session — measured at
**12.2 %** of a run's tokens on 2026-07-30 (2.086 B agent vs 289.4 M main,
last-wins fold) — was invisible. That figure is the corrected prior from the
run-2 register, **not** a constant: this tool reports what it measures, and an
earlier plan sentence saying "7.7 %" was wrong. It is also why both halves of
the ratio must come from the same set of session directories: a numerator from
one session over a denominator sliced to that same session is wrong twice, in
opposite directions, and the printed percentage is wrong by more than either.

`driverShare` is **not** that 12.2 % re-measured, and a reader comparing them
should know why before they conclude the tool is low. The prior is a
whole-corpus figure over every session on the machine; this is one run, sliced
to its own window, and the slice is a floor at both ends (see below) — this
repo's own determinism run prints 2.6 % against a driver row missing its whole
close-out tail. Both numbers are honest answers to different questions.

A session is not dedicated to one run
-------------------------------------
The same trap has a second face, and it is the one that actually fires. Folding
a session's whole `<sessionId>.jsonl` gives a **session** figure; dividing it by
an agent integral sliced to one run gives a **run** figure; printing their ratio
as "% of the run" is the mirror image of the error above. It is not
hypothetical: on this machine **11 session directories hold 2–3 distinct
runIds**, so the same driver dollars would be charged in full to every run the
session launched — up to 3x over-count if a reader summed them, and one
four-agent run rendered "52.9 % driver" from a transcript it shared with
another.

So the driver fold is **bounded to the run's own time window**: the earliest
and latest `timestamp` over the run's agent records (the same records
:func:`analyze` already reads), widened backwards to the `toolUseResult.runId`
launch record for this run when the driver transcript carries one — that record
is the documented join key (`ingest.read_launch`), and it sits just before the
first agent turn. A driver turn outside that window is not counted. The window
is reported as `driverWindow` so the slice is auditable rather than asserted,
`driverCoTenantRuns` counts the other runIds living under the same session
directories, and `driverScope` names which of the two figures you are looking
at.

**Why a turn is outside the window is not one reason, and the rendering must
not pick the wrong one.** When `driverCoTenantRuns` is non-empty the excluded
turns may be another run's, which is correct to drop. When it is EMPTY there is
no other run to belong to, and every excluded turn is this run's own driver
work falling outside the agent span. Saying "they belong to other runs" there
contradicts the field printed beside it, and it is not rare: measured on this
repo's own run, 44 records were excluded with `driverCoTenantRuns: []`.

The slice is a **floor**, deliberately and in the module's usual direction, and
it is short at BOTH ends: the turns where the driver *wrote* the workflow
script before the first agent ever ran fall before the window (the launch
record recovers only the launching turn, not the drafting that preceded it),
and the entire close-out tail after the last agent record — final aggregate
gate, report writing, daemon stop, ACTIVE delete — falls after it. On the run
measured above that is roughly a third of the driver's own turns.
`--driver-whole-session` is the
escape hatch for an operator who wants the un-sliced number; it sets
`driverScope: "whole session"` and the rendering stops saying "of the run",
because a whole-session numerator over a per-run denominator is exactly the
sentence this section exists to prevent anyone from reading.

The locating logic is imported, never re-implemented: `ingest.py` owns which
paths are transcripts, which are agents, which session a file belongs to and
**which directories one run spans**, and `paths.py` owns where the task folders
are (GD-15's ownership rule applies to *readers* too — a second answer to "is
this an agent transcript" is a second answer, and so is a second answer to
"where does this run live").

The fold, stated once
---------------------
`message.usage` repeats: a streaming turn writes the same `message.id` several
times with growing `output_tokens` (2,212 such ids on this machine, 92 % of
agent files). Summing them double-counts. This module folds **per
`message.id`, taking the max of each field** — the same direction
`ingest.map_usage` writes with `$max`, and the reason that direction is
load-bearing. A turn is therefore a distinct billed `message.id`, not a line.

The same fold answers a second question, and the two are not each other's
sanity check. `contextIntegral` sums what every turn re-read; `contextFinal` is
what the last turn re-read. The first is a cost driver, the second is how full
the window was. They are different questions and are never each other's sanity
check — an integral only ever climbs, a level does not, and a compaction moves
the level DOWN while the integral keeps rising. So the level is never summed, never
delta'd and never clamped: `contextPeak`, `contextFinal`, `contextFinalTs` and
`contextByOwner` sit OUTSIDE the money block, whose documented invariant ("every
figure is a floor") is a claim about sums and says nothing about an instant.

The level's turn is the one with the greatest `(timestamp, first-seen
position)` — never the largest, never the last dict entry. On a corpus with no
compaction "largest" and "latest" agree on every run, which is exactly why the
wrong one is tempting: the day something compacts they stop agreeing, silently
and forever. A turn qualifies only if :func:`_level_reading` could read a prompt
off it, its model is billable and its `message.id` is a real `msg_…` — the
harness's own `<synthetic>` records carry all-zero usage and 30 of 649
transcripts on this machine END on one, so a reader that took the last turn
unconditionally would report a busy agent as `0`. Nothing qualifying leaves all
four keys `None`: a level nobody measured is not a zero, and printing one as a
number is the whole defect this reading exists to avoid.

That is also why the level does **not** reuse the money reading. `usage` is
folded twice, from the same bytes, under two rules that disagree on purpose:
:func:`ingest.usage_from_message` reads a `null` component as 0, which is the
right floor for a sum and a fabricated measurement for an instant; and money
sums a multi-iteration `usage` at the top level, which is the right bill and a
prompt that never existed. :func:`_level_reading` refuses a non-`int` outright
and reads `iterations[-1]` — GD-LC-2's rule, in this module's own spelling.

What this reader does NOT do, stated so nobody infers it from the paragraph
above: it takes **usage rows only**. `compact_boundary` records and their
`compactMetadata.postTokens` (GD-LC-3) are invisible here, so a fold whose
newest record is a boundary reports the PRE-compaction level — the last usage
row, which the plan measures overstating 19× across that gap. The live half is
the one that closes it; an offline boundary branch is deliberately not built.

The price table, and how to re-verify it
----------------------------------------
Every row was read on 2026-07-31 out of the **`claude-api` skill's own
instruction body** — its "Current Models (cached: 2026-06-24)" table, the one
whose columns are `Model | Model ID | Context | Input $/1M | Output $/1M`. All
nine rows are priced there; none is from memory. Rates are per **million
tokens**:

===================  =======  ========  =================================
model                 input    output    provenance
===================  =======  ========  =================================
`claude-fable-5`      $10       $50      skill body, Current Models
`claude-mythos-5`     $10       $50      skill body, Current Models
`claude-opus-5`        $5       $25      skill body, Current Models
`claude-opus-4-8`      $5       $25      skill body, Current Models
`claude-opus-4-7`      $5       $25      skill body, Current Models
`claude-opus-4-6`      $5       $25      skill body, Current Models
`claude-sonnet-5`      $2       $10      same row, INTRO to 2026-08-31
`claude-sonnet-4-6`    $3       $15      skill body, Current Models
`claude-haiku-4-5`     $1        $5      skill body, Current Models
===================  =======  ========  =================================

**Re-verify by INVOKING the skill, not by grepping its files.** That
distinction is the whole reason this paragraph is long, and it has already cost
one review cycle: the priced table above is delivered in the skill's
instruction body at invocation time and **is on no file on disk** — a
`grep -rn 'Input \\$/1M'` over the skill directory returns nothing. Worse, the
skill *does* ship a file with a table of the same name: `shared/models.md`'s
"Current Models (recommended)", whose columns are
`Friendly Name | Alias | Full ID | Context | Max Output | Status` — **no price
column at all**. A maintainer who opens that file to check a rate finds every
model listed and no money, and concludes the table below is invented. It is
not; they opened the catalog instead of the price list. Load the skill and read
its Current Models table.

The `claude-sonnet-5` row is the one that is not the sticker. The source states
"$3.00 ($2.00 intro through 2026-08-31)" for input and "$15.00 ($10.00 intro)"
for output; this module bills the **floor** (see below), so it carries the
introductory pair and :data:`SONNET_5_INTRO_RATE_ENDS` records the date it
lapses. Every other row is the sticker.

:data:`PRICE_PROVENANCE` carries the same per-row attribution as data, and
`tests/test_cost.py` asserts the two dicts have identical keys — so a tenth
model cannot be added without saying where its rate came from.

Cache is priced off the input rate: a **read** bills at 0.1x, a **write** at
1.25x for the 5-minute TTL and 2x for the 1-hour one. That split matters and is
read from `usage.cache_creation.{ephemeral_5m,ephemeral_1h}_input_tokens` when
present (A5): without it a cache write is unpriceable and the 5-minute rate is
assumed, which this module *counts and reports* rather than hiding.

A model this table does not know is **never guessed at**. Its tokens are
counted, its dollars are not, and the report names it under `unpricedModels`.
`<synthetic>` — the harness's own non-billable turns — is priced at zero on
purpose and reported separately.

The one normalization, and why it is not a guess: the API returns **dated**
ids for several models and `message.model` records them verbatim (37 turns of
`claude-haiku-4-5-20251001` on this machine at the time of writing). A trailing
`-20YYMMDD` on a model the table already knows is a *lookup miss*, not an
unknown model, so it is stripped once before the lookup. Nothing else is
stripped — no prefix matching, no nearest neighbour — so
`claude-not-a-model-9` still prices at None.

Every dollar figure is a **floor** for the same reason the economics report's
were: cache-write TTL is assumed when the split is missing or does not
reconcile, and a model outside the table contributes nothing.

"Does not reconcile" is load-bearing and not hypothetical. The two cache-write
numbers are folded independently (max per field, per `message.id`), so a turn
whose records carry *different* splits for the same write can end up with
`write5m + write1h` **greater** than `cache_write`, which prices the same
tokens twice and breaks the floor claim in the expensive direction. A turn
whose `cache_creation` dict carries only a zeroed key can end up with the sum
**below** it, which prices a real write at zero and reports no degradation.
Both are settled by one invariant applied after the fold rather than per
record: a split is a *measurement* only when it sums to the write it claims to
describe; otherwise the whole write bills at the 5-minute rate and the turn is
counted in `cacheWritesWithoutTtlSplit`. The 5-minute assumption itself lives
only at pricing time — writing it into the fold is what would let a record that
*lacked* the split outvote the record that had it.

Usage
-----

    PYTHONPATH=plugin/touch python3 -m aggregator.costs               # newest run
    PYTHONPATH=plugin/touch python3 -m aggregator.costs --task <dir>
    PYTHONPATH=plugin/touch python3 -m aggregator.costs --wf-dir <dir> --json
    PYTHONPATH=plugin/touch python3 -m aggregator.costs --wf-dir <dir> --single-session
    PYTHONPATH=plugin/touch python3 -m aggregator.costs --wf-dir <dir> --driver-whole-session
    PYTHONPATH=plugin/touch python3 -m aggregator.costs --baseline --ceiling 12000

To land a run's cost in its own `report/` from a close-out epilogue:

    PYTHONPATH="$REPO/plugin/touch" python3 -P -m aggregator.costs \
        --task "$ORCH_STATE_DIR" --json > "$ORCH_STATE_DIR/report/cost.json"

`--baseline` is D-22's half: the always-on context prefix (this repo's
`CLAUDE.md`, the memory index, and the ten skill descriptions) estimated with a
stdlib chars/4 estimator and compared against a ceiling. It prefers the budgets
`tests/test_context_budget.py` declares once that file exists, and falls back
to the ceiling its caller passes — `scripts/release.sh` is that caller.

No network, ever. No corpus is a **clean skip**, not a failure: a release cut
from a checkout with no run history must not go red for having no history.
"""
from __future__ import annotations

import ast
import json
import os
import re
import statistics
import sys
from dataclasses import dataclass, field

from . import ingest
from . import paths


class CostsError(ValueError):
    """A malformed input this module refuses to guess about."""


# --- the price table -------------------------------------------------------

#: `model` -> (input $/MTok, output $/MTok). Every row read on 2026-07-31 from
#: the `claude-api` skill's INSTRUCTION BODY ("Current Models (cached:
#: 2026-06-24)", the table with `Input $/1M` / `Output $/1M` columns) — none
#: from memory, which is the exact failure this module exists to replace.
#:
#: To edit a rate, INVOKE the skill and read that table. Do NOT grep the skill's
#: files: the priced table is not on disk, and `shared/models.md` ships a
#: same-named table that carries no prices at all (see the module docstring).
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    # The introductory rate, in force until 2026-08-31; the sticker is $3/$15.
    # The lower number is the right one HERE because every figure this module
    # prints is a floor — re-read the skill on that date rather than assuming.
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

#: Where each :data:`PRICES` row came from, per row and in the source's own
#: words. A dict rather than prose because prose rots silently and a test
#: cannot read it: `test_cost.py` asserts these keys equal `PRICES`'s, so a
#: tenth model cannot be priced without an attribution beside it.
#:
#: "skill body" means the `claude-api` skill's instruction body — delivered at
#: invocation, absent from every file the skill ships.
PRICE_PROVENANCE = {
    "claude-opus-5": "skill body, Current Models (cached 2026-06-24), read 2026-07-31",
    "claude-opus-4-8": "skill body, Current Models (cached 2026-06-24), read 2026-07-31",
    "claude-opus-4-7": "skill body, Current Models (cached 2026-06-24), read 2026-07-31",
    "claude-opus-4-6": "skill body, Current Models (cached 2026-06-24), read 2026-07-31",
    "claude-fable-5": "skill body, Current Models (cached 2026-06-24), read 2026-07-31",
    "claude-mythos-5": "skill body, Current Models (cached 2026-06-24), read 2026-07-31",
    "claude-sonnet-5": (
        "skill body, Current Models (cached 2026-06-24), read 2026-07-31 — the "
        "INTRODUCTORY pair ($2/$10) of a row reading '$3.00 ($2.00 intro through "
        "2026-08-31)' / '$15.00 ($10.00 intro)'; billed because every figure here "
        "is a floor"),
    "claude-sonnet-4-6": "skill body, Current Models (cached 2026-06-24), read 2026-07-31",
    "claude-haiku-4-5": "skill body, Current Models (cached 2026-06-24), read 2026-07-31",
}

#: When `claude-sonnet-5`'s introductory rate above lapses. Named rather than
#: buried in a comment so a test can assert the pair is deliberate.
SONNET_5_INTRO_RATE_ENDS = "2026-08-31"

#: Cache multipliers, applied to the model's INPUT rate.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0

#: Models whose turns are recorded but never billed. Priced at zero rather than
#: left unpriced, because "unpriced" is a warning and this is a known state.
NON_BILLABLE_MODELS = frozenset({"<synthetic>"})

#: `usage.cache_creation` -> our two names. Absent on most records; when it is
#: absent the whole write falls back to the 5-minute rate and is counted.
_CACHE_CREATION_SOURCE = {
    "write5m": "ephemeral_5m_input_tokens",
    "write1h": "ephemeral_1h_input_tokens",
}

#: chars/4, the stdlib estimator D-22 specifies. Applied to BYTES, not decoded
#: text: `.touch/memory/MEMORY.md` is written by another process and has been
#: observed mid-write with a truncated multi-byte character, and a budget
#: reader that raises `UnicodeDecodeError` on a torn read is a reader that goes
#: red for a reason that has nothing to do with the budget.
BYTES_PER_TOKEN = 4

#: The always-on prefix this repo OWNS. The `CLAUDE.md` in the PARENT directory
#: of this checkout is deliberately absent: it is out of this repo's write
#: scope, so budgeting it would gate a release on a file no one here may edit
#: (D-22).
BASELINE_SOURCES = (
    ("CLAUDE.md", "CLAUDE.md"),
    ("MEMORY.md", os.path.join(".touch", "memory", "MEMORY.md")),
)

#: Where the ten always-on skill descriptions live, relative to a repo root.
SKILLS_GLOB_ROOT = os.path.join("plugin", "touch", "skills")

#: sp-10's budget test. Its declared budgets outrank the ceiling a caller
#: passes (D-21 -> D-22); `--baseline` names it as its `source` when present.
BUDGET_TEST_REL = os.path.join("tests", "test_context_budget.py")

#: The EXACT module-level names :func:`declared_budgets` reads out of
#: `BUDGET_TEST_REL`, and the contract that file is held to: these three are
#: the parts, they are summed, and **nothing else is consulted**.
#:
#: Named exhaustively rather than matched by suffix on purpose. A suffix rule
#: (`endswith("_BUDGET_TOKENS")`) silently doubles the ceiling the day the
#: budget test declares a total beside its parts — `TOTAL_BUDGET_TOKENS = 8200`
#: next to 6,000 + 800 + 1,400 would make the gate 16,400 and stop it biting,
#: with `ceilingSource` still confidently naming the file. An unknown
#: `*_BUDGET_TOKENS` name is therefore ignored, not added.
BUDGET_KEYS = (
    "CLAUDE_MD_BUDGET_TOKENS",
    "MEMORY_BUDGET_TOKENS",
    "SKILLS_BUDGET_TOKENS",
)

#: A skill's always-on `description:`. Two shapes, because YAML has two: a
#: single line, and a folded/literal block (`>-`, `>`, `|`, `|-`) whose text is
#: the indented lines beneath it. Matching only the first would silently
#: measure ~0 bytes for a folded description — under-reporting the very thing
#: D-22's gate exists to cap, in the direction that never goes red.
_DESCRIPTION_RE = re.compile(r"^description:[ \t]*(?P<text>.*?)[ \t]*$", re.MULTILINE)
_DESCRIPTION_BLOCK_RE = re.compile(
    r"^description:[ \t]*(?P<style>[|>][-+]?)[ \t]*$(?P<body>(?:\n(?:[ \t]+.*)?)*)",
    re.MULTILINE)

#: The dated form of a model id: `claude-haiku-4-5-20251001`. Anchored to the
#: END and to a `20YY` century, so it can only ever remove a date — never a
#: version segment, and never enough of a name to make two models collide.
_DATED_MODEL_RE = re.compile(r"-20\d{6}$")


def price_for(model):
    """`(input, output)` $/MTok for ``model``, or None when it is unknown.

    None is a first-class answer and the caller must carry it: a made-up price
    is worse than a missing one, because it is indistinguishable from a
    measurement.

    A **dated** id of a model the table knows is resolved to it — that is a
    lookup miss being fixed, not a rate being guessed at (see the module
    docstring). An id the table does not know in either form stays None.
    """
    if not isinstance(model, str):
        return None
    if model in NON_BILLABLE_MODELS:
        return (0.0, 0.0)
    price = PRICES.get(model)
    if price is None:
        price = PRICES.get(_DATED_MODEL_RE.sub("", model))
    return price


# --- locating a run --------------------------------------------------------


def read_config(task_dir):
    """A task folder's `orch-config.json` as a dict, or `{}`.

    A missing or malformed config is not an error here — it is the "no corpus"
    state, and every caller degrades to a skip rather than a failure.
    """
    path = os.path.join(os.fspath(task_dir), "orch-config.json")
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def newest_task_dir(tasks_root=None, *, cwd=None, env=None):
    """The most recently modified task folder under the tasks root, or None.

    The same "newest wins" rule `status.sh` uses when `$ORCH_STATE_DIR` is
    unset — stated here as a *default*, never as a resolution a caller cannot
    override, because guessing which run you meant is exactly how an event ends
    up in the wrong folder.
    """
    root = paths.tasks_root(tasks_root, cwd=cwd, env=env)
    try:
        entries = [os.path.join(root, name) for name in os.listdir(root)]
    except OSError:
        return None
    folders = [one for one in entries if os.path.isdir(one)]
    if not folders:
        return None
    return max(folders, key=lambda one: os.path.getmtime(one))


def session_dir_for(wf_dir):
    """The session DIRECTORY a `wf_dir` sits under, or None.

    `…/<sessionId>/subagents/workflows/<runId>` -> `…/<sessionId>`: three
    parents up, and only when the two intermediate components are the ones
    SUBSTRATE-7 names AND the result is session-SHAPED. A blind `dirname` x3 on
    a path of another shape returns a directory that exists and means nothing.

    The third check is the one that is easy to skip and costly to omit: without
    it `/srv/backup/subagents/workflows/wf_x` resolves to `/srv/backup`, and
    :func:`driver_transcript_for` would then fold a `/srv/backup.jsonl` that
    happened to exist as "the driver's own transcript". `ingest.session_id_for_path`
    owns what a sessionId looks like, so it is asked rather than re-derived —
    and it is asked about `<session>.jsonl` rather than the bare directory,
    because that function searches a path's PARENTS for a uuid and would skip
    the very component being validated here. The transcript spelling is also
    the one it documents, and the one :func:`driver_transcript_for` opens.
    """
    if not wf_dir:
        return None
    run_dir = os.path.normpath(os.fspath(wf_dir))
    workflows = os.path.dirname(run_dir)
    subagents = os.path.dirname(workflows)
    session = os.path.dirname(subagents)
    if os.path.basename(workflows) != "workflows":
        return None
    if os.path.basename(subagents) != "subagents":
        return None
    if not session:
        return None
    if ingest.session_id_for_path(session + ".jsonl") is None:
        return None
    return session


def claude_root_for(wf_dir):
    """The `~/.claude`-shaped ROOT a `wf_dir` sits under, or None.

    `<root>/projects/<slug>/<sessionId>/subagents/workflows/<runId>` -> `<root>`.
    Derived from the path the caller named rather than from `$TOUCH_CLAUDE_ROOT`
    or `~`, because the only root that can hold this run's sibling session
    directories is the one this run is already in — a fixture corpus and a real
    home have the same shape and neither should be able to see the other.

    None whenever the shape is not the one SUBSTRATE-7 names, which is also the
    signal :func:`run_dirs_for` uses to decline expanding and hand back exactly
    what it was given.
    """
    session = session_dir_for(wf_dir)
    if session is None:
        return None
    slug = os.path.dirname(session)
    projects = os.path.dirname(slug)
    if os.path.basename(projects) != "projects":
        return None
    return os.path.dirname(projects) or None


def run_dirs_for(wf_dir, *, expand=True, cwd=None, env=None) -> tuple:
    """Every session's slice of the run ``wf_dir`` anchors, sorted.

    One run spans several `…/subagents/workflows/<runId>/` directories whenever
    it was resumed (R-49), and the anchor is only the session it *launched*
    from. `ingest.find_run_dirs` is the finder that knows this; `ingest._run_scope`
    is the fence `read_run` passes it — `sessions.scoped_dirs` for the project
    plus the anchor's own slug, so a foreign project holding a directory of the
    same `wf_<12hex>` runId contributes nothing. Both are imported rather than
    re-derived: a second answer to "where does this run live" is a second answer.

    The anchor is always in the result, even when the glob finds nothing (an
    operator naming a directory outside any `projects/` tree still gets that
    directory). ``expand=False`` returns it alone.
    """
    if not wf_dir:
        return ()
    anchor = os.path.normpath(os.fspath(wf_dir))
    root = claude_root_for(anchor) if expand else None
    if root is None:
        return (anchor,)
    # KNOWN COUPLING: `_run_scope` is private to `ingest.py`, which another
    # sub-plan owns. Importing it is still right — copying the fence would be a
    # second answer to "which directories may contribute to this run", and the
    # two would drift. `test_a_foreign_project_with_the_same_run_id_is_not_folded_in`
    # pins the behaviour, so a rename upstream fails loudly here rather than
    # silently widening the scope. Promote to a public alias when `ingest.py` is
    # next opened by whoever owns it.
    scope = ingest._run_scope(anchor, root, cwd, env)
    found = ingest.find_run_dirs(os.path.basename(anchor), root, scope=scope)
    return tuple(sorted({anchor, *(os.path.normpath(one) for one in found)}))


def driver_transcript_for(wf_dir):
    """The launching session's own `<sessionId>.jsonl`, or None if absent.

    The transcript is the session directory's SIBLING, not a file inside it.
    Checked with `isfile`, so an archived session (directory swept, transcript
    gone) reports None and the driver row is reported as unavailable instead of
    silently counting as zero.
    """
    session = session_dir_for(wf_dir)
    if session is None:
        return None
    candidate = session + ".jsonl"
    return candidate if os.path.isfile(candidate) else None


def co_tenant_run_ids(run_dirs) -> tuple:
    """Other runIds living under the same session directories, sorted.

    One `listdir` of each run dir's parent (`…/subagents/workflows/`). A
    non-empty answer is the fact that makes the driver slice necessary rather
    than fastidious: it means the `<sessionId>.jsonl` beside these directories
    was written for more than one run, so a whole-file fold is a figure about
    the session and not about this run.
    """
    mine = {os.path.basename(os.path.normpath(one)) for one in run_dirs}
    others = set()
    for one in run_dirs:
        workflows = os.path.dirname(os.path.normpath(one))
        try:
            names = os.listdir(workflows)
        except OSError:
            continue
        for name in names:
            if name in mine:
                continue
            if os.path.isdir(os.path.join(workflows, name)):
                others.add(name)
    return tuple(sorted(others))


def agent_transcripts(wf_dir):
    """Every `agent-<id>.jsonl` directly under ``wf_dir``, sorted.

    Ownership is `ingest.agent_id_for_path`'s to decide — this function only
    lists a directory. A `.meta.json` sidecar, a `journal.jsonl` and a snapshot
    all live beside the transcripts, and re-deriving "which of these is an
    agent" here would be a second answer to a question `ingest.py` owns.
    """
    if not wf_dir:
        return []
    try:
        names = sorted(os.listdir(wf_dir))
    except OSError:
        return []
    return [os.path.join(wf_dir, name) for name in names
            if ingest.agent_id_for_path(name)]


# --- the fold --------------------------------------------------------------


@dataclass
class Turn:
    """One billed assistant message, max-folded across its repeats."""

    message_id: str
    model: str = ""
    owner: str = ""            #: agentId, or the sessionId for the driver
    ordinal: int = 0           #: first-seen position, for "turn 1" per owner
    split_seen: bool = False   #: some record of this turn carried a TTL split
    #: The greatest `timestamp` any record of this turn carried, or None when
    #: none of them was dated. Max-folded like the tokens, and for the same
    #: reason: a streamed turn is written several times and the last write is
    #: when the turn actually ended. Only the LEVEL keys read it — the money
    #: arithmetic has never needed to know when a turn happened.
    moment: object = None
    #: The strict prompt reading of :func:`_level_reading`, max-folded across
    #: this turn's repeats exactly as the tokens are, or None when no record of
    #: the turn said. NOT :attr:`context` — that one is the money reading and
    #: floors what it cannot parse. Only the LEVEL keys read this.
    level: object = None
    tokens: dict = field(default_factory=lambda: {
        "in": 0, "out": 0, "cached": 0, "cache_write": 0,
        "write5m": 0, "write1h": 0})

    @property
    def context(self) -> int:
        """What the model actually re-read this turn (the context-integral's term).

        The arithmetic is `input + cache_read + cache_creation` — the same
        three components the LEVEL sums, output excluded from both — but this
        is the MONEY reading and :attr:`level` is the level's: this one is
        built from `ingest.usage_from_message`, which floors what it cannot
        parse, and it aggregates a multi-iteration `usage` at the top level
        because every iteration was billed. On the overwhelming majority of
        rows the two are the same integer; where they differ, each is right
        about its own question (see :func:`_level_reading`). Nothing in the
        level block reads this property.
        """
        return self.tokens["in"] + self.tokens["cached"] + self.tokens["cache_write"]

    @property
    def context_qualifies(self) -> bool:
        """This turn is a usable reading of HOW FULL the window was.

        Three clauses, and the first is the one that does the work: a prompt
        :func:`_level_reading` could actually read (see there — it refuses what
        the money reading floors); a **billable** model; and a real `msg_…` id.
        `<synthetic>` records — the harness's own non-billable turns — carry
        all-zero usage, and a transcript can END on one, so a reader that took
        the last turn unconditionally would report a busy agent as `0`. Zero is
        the lie this property exists to refuse: the money block still counts
        such a turn (it is a turn), the level block does not know it happened.

        The rule is GD-LC-2's, and it is stated here and stated AGAIN — never
        imported — in the live half (`decision_watcher.py`'s `_note_context` /
        `_prompt_total`) that reads the same transcripts, because each tree's
        `in` means a different thing (GD-LC-11's deliberate duplication).
        Nothing machine-checks the two spellings equal **for this module**:
        `tests/test_token_crosscheck.py` pins `ingest.py` against the watcher
        and does not mention `costs.py`. So the equality here is a claim
        maintained by hand — if the two ever diverge, this docstring is where a
        reader should have been told, and the divergence must be written down
        rather than left to be discovered.
        """
        return (self.level is not None
                and self.model not in NON_BILLABLE_MODELS
                and isinstance(self.message_id, str)
                and self.message_id.startswith("msg_"))

    @property
    def split_measured(self) -> bool:
        """The 5m/1h pair is a MEASUREMENT of this turn's cache write.

        Seen on some record AND reconciling with the write it claims to
        describe. The second half is the one that matters: the three token
        fields are max-folded independently, so a streamed turn whose records
        carry *different* splits for the same write ends up with the maximum of
        each — an OVER-count, which is the one direction "every dollar figure
        is a floor" cannot survive. The mirror case — a `cache_creation` dict
        carrying only a zeroed key — sums BELOW the write and would price a
        real write at zero while reporting no degradation at all. One
        reconciliation settles both.
        """
        if not self.split_seen:
            return False
        pair = self.tokens["write5m"] + self.tokens["write1h"]
        return pair == self.tokens["cache_write"]

    @property
    def split_assumed(self) -> bool:
        """This turn wrote to cache and no record of it measured the TTL."""
        return bool(self.tokens["cache_write"]) and not self.split_measured

    @property
    def billed_writes(self) -> tuple:
        """`(write5m, write1h)` as BILLED — the split, or the cheaper fallback.

        An unreconciled split is not a measurement, so it is not priced as one:
        the whole write goes to the 5-minute rate and :attr:`split_assumed`
        reports it. Both failures above therefore collapse into the single
        already-reported state "assumed the cheaper TTL on N turns", and the
        floor claim becomes true again.
        """
        if self.split_measured:
            return (self.tokens["write5m"], self.tokens["write1h"])
        return (self.tokens["cache_write"], 0)


@dataclass
class TurnCounter:
    """Distinct TURNS matching one diagnostic, deduplicated on message id.

    One shape for all three of :class:`Fold`'s diagnostics, because they are
    one rule: count the turn, not the record. A record carrying no usable id
    cannot be deduplicated at all, so it is counted anonymously rather than
    dropped — losing it would be the silent zero these counters exist to
    prevent.
    """

    ids: set = field(default_factory=set)
    anonymous: int = 0

    def note(self, message_id):
        if isinstance(message_id, str) and message_id:
            self.ids.add(message_id)
        else:
            self.anonymous += 1

    @property
    def count(self) -> int:
        return len(self.ids) + self.anonymous

    def excluding(self, folded) -> int:
        """:attr:`count`, minus the turns that were folded in after all."""
        return len(self.ids - set(folded)) + self.anonymous


@dataclass
class Fold:
    """The accumulator: turns by message id, plus the read census.

    EVERY diagnostic below is folded per TURN, not per record — all four of
    them, and for exactly the reason the tokens are: a streamed turn is written
    several times, so a per-record count would report one turn's missing TTL
    split four times and the number would grow with how slowly the turn
    streamed. The rule is stated here rather than at each counter because
    stating it once is what made the two window counters, added later, wrong:
    they were `+= 1` per record for a while, and on this repo's own run that
    printed 44 excluded "turns" for 14 real ones, beside a deduplicated count
    of 86.
    """

    turns: dict = field(default_factory=dict)
    reads: dict = field(default_factory=dict)
    read_ids: set = field(default_factory=set)
    files: int = 0
    lines: int = 0
    #: turns whose `usage` ingest refused (a float, a bool, a non-int)
    unusable_turns: TurnCounter = field(default_factory=TurnCounter)
    #: earliest / latest record moment folded IN, as datetimes. The run's own
    #: time window when this fold is the agent half — which is exactly what
    #: bounds the driver half (see the module docstring).
    first_ts: object = None
    last_ts: object = None
    #: TURNS a window excluded, keyed the way the tokens are and for the same
    #: reason (see above): on this repo's own run 44 excluded RECORDS are 14
    #: excluded turns, and a per-record count would print beside a deduplicated
    #: "86 turns" as though the two were comparable — inflated by exactly how
    #: slowly each turn streamed.
    #:
    #: Two reasons, counted apart because they mean different things: `outside`
    #: is a turn the window placed elsewhere, `undated` is a turn this module
    #: could not PLACE at all and therefore refused to claim — a floor, never a
    #: silent zero.
    outside_window_turns: TurnCounter = field(default_factory=TurnCounter)
    undated_turns: TurnCounter = field(default_factory=TurnCounter)

    def note_moment(self, moment):
        """Widen the folded window to include ``moment``."""
        if moment is None:
            return
        if self.first_ts is None or moment < self.first_ts:
            self.first_ts = moment
        if self.last_ts is None or moment > self.last_ts:
            self.last_ts = moment

    @property
    def window(self):
        """`(first, last)` of what was folded, or None when nothing was dated."""
        if self.first_ts is None or self.last_ts is None:
            return None
        return (self.first_ts, self.last_ts)

    @property
    def unusable(self) -> int:
        """Distinct TURNS whose usage was refused, never coerced.

        Not `excluding` the folded turns, unlike the two below: a turn one of
        whose records carried a refused `usage` is a degraded measurement even
        when a later record of it parsed, and saying so is the point.
        """
        return self.unusable_turns.count

    @property
    def outside_window(self) -> int:
        """Distinct TURNS the window placed outside this run.

        A turn no record of which was folded IN. The subtraction is not
        decoration: a turn streaming across the window's own edge writes
        records on both sides, and reporting it as excluded while its tokens
        are in the totals would be the report contradicting itself.
        """
        return self.outside_window_turns.excluding(self.turns)

    @property
    def undated(self) -> int:
        """Distinct TURNS carrying no timestamp this module could place."""
        return self.undated_turns.excluding(self.turns)

    @property
    def split_missing(self) -> int:
        """Distinct cache-writing TURNS with no 5m/1h breakdown on any record."""
        return sum(1 for turn in self.turns.values() if turn.split_assumed)

    def note_turn(self, message_id, model, owner, fields, split, *,
                  split_seen=None, moment=None, level=None):
        turn = self.turns.get(message_id)
        if turn is None:
            turn = Turn(message_id=message_id, model=model or "",
                        owner=owner or "", ordinal=len(self.turns))
            self.turns[message_id] = turn
        elif model and not turn.model:
            turn.model = model
        # The turn's own moment, max-folded exactly as its tokens are: the last
        # record of a streamed turn is when that turn ended. A caller with no
        # moment to give leaves it None, and an undated turn is one the LEVEL
        # keys cannot ORDER — so it is left out of the "latest" answer rather
        # than assumed into it, the same floor direction `note_undated` takes.
        if moment is not None and (turn.moment is None or moment > turn.moment):
            turn.moment = moment
        # The strict LEVEL reading, max-folded for the same reason the tokens
        # are: a streamed turn is written several times and only some of those
        # records carry a complete `usage`. A record that could not be read
        # leaves the fold as it found it — one unreadable write of a turn never
        # unmeasures the writes that WERE readable, and never fabricates a
        # number for the turns that had none.
        if level is not None and (turn.level is None or level > turn.level):
            turn.level = level
        if split_seen is None:                # a caller passing a split means it
            split_seen = bool(split.get("write5m") or split.get("write1h"))
        if split_seen:
            turn.split_seen = True
        merged = dict(fields)
        merged.update(split)
        for name, value in merged.items():
            if value > turn.tokens.get(name, 0):
                turn.tokens[name] = value

    def note_unusable(self, message_id):
        self.unusable_turns.note(message_id)

    def note_outside_window(self, message_id):
        self.outside_window_turns.note(message_id)

    def note_undated(self, message_id):
        self.undated_turns.note(message_id)


def _cache_split(message, fields):
    """`({write5m, write1h}, measured)` for one record's cache write.

    Zeros when the record carried no `cache_creation` block, and **no
    fallback** — the 5-minute assumption belongs at pricing time
    (:attr:`Turn.billed_writes`), never in the fold. That distinction is not
    cosmetic: a turn is written several times, the three fields are max-folded
    independently, and a fallback written into `write5m` on the record that
    lacked the split would survive the max and contradict the split that
    arrived on the record that had it. The fold therefore carries only what was
    measured; what was assumed is derived, once, from that.
    """
    usage = message.get("usage") if isinstance(message, dict) else None
    creation = usage.get("cache_creation") if isinstance(usage, dict) else None
    out = {"write5m": 0, "write1h": 0}
    seen = False
    if isinstance(creation, dict):
        for name, source in _CACHE_CREATION_SOURCE.items():
            value = creation.get(source)
            if isinstance(value, int) and not isinstance(value, bool):
                out[name] = value
                seen = True
    return (out, seen)


#: GD-LC-1's three components, in the transcript's own spelling. `output_tokens`
#: is deliberately absent: the level is the documented statusline arithmetic
#: (input + cache_creation + cache_read), and the row already carries the whole
#: wire prompt without it.
_LEVEL_SOURCE = ("input_tokens", "cache_creation_input_tokens",
                 "cache_read_input_tokens")


def _level_reading(message):
    """This record's context LEVEL, or None when the bytes do not say.

    A SECOND reading of the same `usage` block that
    :func:`ingest.usage_from_message` folds for money, and the two disagree on
    purpose — twice:

    * **`type(v) is int`, never `isinstance` and never `or 0`.** `bool` is an
      `int` subclass, so `isinstance` reads `cache_creation_input_tokens: true`
      as 1; and the money reader coerces a `null` to 0 (`ingest.py`), which is
      the right FLOOR for a sum and a fabricated measurement for an instant. A
      component that is a float, a bool, a `null` or missing refuses the whole
      row: a level nobody measured is not a zero, and a plausible-looking
      number made out of bytes that say nothing is the R-58 defect class in
      miniature. `tests/fixtures/context/ctx-agent-no-usable-turn.jsonl` is the
      frozen specimen for exactly this arm.
    * **`usage.iterations`.** Read the TOP level when the key is absent or the
      list holds one entry (every sampled row behaves that way); a `len > 1`
      list reads `iterations[-1]`, which is unambiguously ONE API call's prompt
      whichever way the top level aggregates. Money keeps the top-level sum —
      correct, because every iteration was billed — so the two readings
      legitimately differ on such a row, the level being the smaller.
      (`tests/fixtures/context/ctx-agent-iterations-multi.jsonl`: top level
      65,690 against `iterations[-1]`'s 22,131, a 2.97× overstatement for a
      reader that took the aggregate.)

    A zero total is not an occupancy either — 30 of 649 real transcripts end on
    an all-zero `<synthetic>` row — so it comes back None as well.

    This is GD-LC-2's rule in this module's spelling; the live half states it
    again in its own (see :attr:`Turn.context_qualifies` on why it is duplicated
    rather than shared, and on what does *not* machine-check the two equal).
    """
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    iterations = usage.get("iterations")
    if isinstance(iterations, list) and len(iterations) > 1:
        if not isinstance(iterations[-1], dict):
            return None
        usage = iterations[-1]
    total = 0
    for name in _LEVEL_SOURCE:
        value = usage.get(name)
        if type(value) is not int:
            return None
        total += value
    return total if total > 0 else None


def record_moment(record):
    """A transcript record's `timestamp` as a UTC datetime, or None.

    KNOWN COUPLING, the same one `run_dirs_for` carries: `ingest._record_ts` is
    private to a module another sub-plan owns. It is asked anyway, because it
    already knows every spelling the CLI has emitted (`…Z`, `+00:00`, seconds
    or milliseconds) and a second ISO-8601 parser here would be a second answer
    to "when did this happen". It reports malformed values rather than raising,
    so an unparseable stamp arrives as None and the caller treats the record as
    undated instead of crashing a cost report over one bad line.
    """
    moment, _raw, _error = ingest._record_ts(record)
    return moment


def launch_moment_for(path, run_id):
    """When ``path``'s session launched ``run_id``, or None.

    `ingest.read_launch` owns the join: a driver record whose `toolUseResult`
    carries `runId` is the Workflow launch for that run (R-49/CONVO-12). The
    moment matters because the launching turn's own tokens are this run's setup
    cost and fall a second or two BEFORE the first agent record — so the window
    the agents describe would otherwise exclude the turn that started them.

    Cheap on purpose: a run id is a distinctive `wf_<12hex>` token, so a
    substring test rejects ~every line before `json.loads` ever sees it. That
    keeps this second pass over the driver transcript from doubling the cost of
    the fold that follows it.
    """
    if not run_id:
        return None
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    earliest = None
    with handle:
        for line in handle:
            if run_id not in line:
                continue
            try:
                record = json.loads(line.strip())
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            launch = ingest.read_launch(record)
            if launch is None or launch.run_id != run_id:
                continue
            moment = record_moment(record)
            if moment is not None and (earliest is None or moment < earliest):
                earliest = moment
    return earliest


def fold_transcript(path, fold=None, *, owner=None, window=None):
    """Fold one `.jsonl` transcript into ``fold`` (created when omitted).

    Reads line by line and skips what it cannot parse: a transcript being
    appended to while this runs can end in half a line, and refusing the whole
    file for that would make the tool unusable on a LIVE run — which is the
    only time anyone wants a cost number in a hurry.

    ``window`` is an inclusive `(start, end)` pair of datetimes. When given,
    only records stamped inside it are folded — the driver half's run slice
    (see the module docstring). Without it every record is folded and the
    window the records themselves describe is recorded on the fold.
    """
    if fold is None:
        fold = Fold()
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return fold
    fold.files += 1
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            fold.lines += 1
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            _fold_record(record, fold, owner=owner, window=window)
    return fold


def _fold_record(record, fold, *, owner=None, window=None):
    if record.get("type") == "assistant":
        message = record.get("message")
        if not isinstance(message, dict):
            return
        # The window gate stands BEFORE the read census as well as the token
        # fold: a turn belonging to another run did not read files for this
        # one, and letting its `Read` calls into the census would put another
        # run's hot files at the top of this run's "top re-read" list.
        moment = record_moment(record)
        if window is not None:
            # Both exclusions are noted by message id, so a streamed turn
            # written four times outside the window is one excluded turn — the
            # same rule the tokens are folded under, and the reason the
            # rendered count is comparable to the "N turns" printed beside it.
            if moment is None:
                fold.note_undated(message.get("id"))
                return
            if moment < window[0] or moment > window[1]:
                fold.note_outside_window(message.get("id"))
                return
        fold.note_moment(moment)
        # Counted FIRST, and deliberately not gated on the usage block: the
        # re-read census is a fact about tool calls, and a turn whose `usage`
        # ingest refused still read the files it read. Gating it would drop
        # exactly the turns the census exists to explain.
        _note_reads(message, fold)
        fields = ingest.usage_from_message(message)
        if fields is None:
            if message.get("usage") is not None:
                fold.note_unusable(message.get("id"))
            return
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            return
        who = record.get("agentId") or owner or record.get("sessionId") or ""
        split, measured = _cache_split(message, fields)
        # Two readings of the same `usage`, taken here so each stays whole:
        # `fields` is the money fold (floored), `_level_reading` is the level
        # (refused rather than floored). Neither is derived from the other.
        fold.note_turn(message_id, message.get("model"), who, fields, split,
                       split_seen=measured, moment=moment,
                       level=_level_reading(message))


def _note_reads(message, fold):
    """Count `Read` tool calls by `file_path` (A12/A19).

    The tool_use INPUT is the source, not `toolUseResult.file`: the structured
    result shape appears on main-session records and almost never on an agent's
    own transcript, so a reader keyed on it would report zero re-reads for the
    agents — which are the ones paying for them.

    Deduplicated on the `tool_use` block's own id, for the same reason the
    token fold is keyed on `message.id`: a streaming turn writes its content
    several times, so counting per RECORD would report a file as re-read once
    per stream chunk and the "top re-read" list would rank by how slowly a turn
    streamed.
    """
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") != "Read":
            continue
        block_id = block.get("id")
        if isinstance(block_id, str) and block_id:
            if block_id in fold.read_ids:
                continue
            fold.read_ids.add(block_id)
        payload = block.get("input")
        target = payload.get("file_path") if isinstance(payload, dict) else None
        if isinstance(target, str) and target:
            fold.reads[target] = fold.reads.get(target, 0) + 1


# --- the report ------------------------------------------------------------


#: Sentinel for "this model has not been looked up yet", so a memo of the
#: price table can cache the None that means "unknown" without re-asking.
_MISSING = object()


def _dollars(turn, prices):
    """`(input, cacheRead, cacheWrite, output)` dollars for one turn, or None."""
    price = prices.get(turn.model, _MISSING)
    if price is _MISSING:
        price = price_for(turn.model)
        prices[turn.model] = price
    if price is None:
        return None
    rate_in, rate_out = price
    tokens = turn.tokens
    write5m, write1h = turn.billed_writes
    return (
        tokens["in"] / 1e6 * rate_in,
        tokens["cached"] / 1e6 * rate_in * CACHE_READ_MULTIPLIER,
        (write5m / 1e6 * rate_in * CACHE_WRITE_5M_MULTIPLIER
         + write1h / 1e6 * rate_in * CACHE_WRITE_1H_MULTIPLIER),
        tokens["out"] / 1e6 * rate_out,
    )


@dataclass
class _Level:
    """The context LEVEL over one scope: its peak, and its latest reading.

    Folded unlike everything else in this module, and the difference is the
    point. A level is an instant, not a total: `peak` is a MAX over the
    qualifying turns and `final` is the reading of the greatest-timestamp one.
    Neither is ever summed, and `final` is emphatically not clamped up to
    `peak` — a compaction legitimately lowers the level while the peak stands,
    so a monotone `final` would be a fabrication in the one case the field
    exists for.

    Both stay None until a qualifying turn arrives. "Nobody measured this" and
    "this measured zero" are different sentences and only the first is ever
    true here, so the absence is spelled as an absence.
    """

    peak: object = None       #: int once a qualifying turn has been seen
    final: object = None      #: int once a qualifying DATED turn has been seen
    moment: object = None     #: that turn's own timestamp, never an emit clock
    ordinal: int = -1         #: and its fold position, for the timestamp tie

    def note(self, turn):
        """Fold one turn in. The CALLER owns qualification (`context_qualifies`).

        The winner is the greatest `(timestamp, first-seen position)`. The
        second component is the tie-break, and it is not decoration: the fold's
        ordinal is (path order, line number) — transcripts are read in sorted
        path order and appended in line order — so two turns stamped to the
        same millisecond resolve the way the bytes on disk are ordered instead
        of the way a dict happens to iterate.

        The value folded is :attr:`Turn.level` — the strict reading — never
        :attr:`Turn.context`, which is the money one.
        """
        if self.peak is None or turn.level > self.peak:
            self.peak = turn.level
        if turn.moment is None:
            return
        if (self.moment is None
                or (turn.moment, turn.ordinal) > (self.moment, self.ordinal)):
            self.final = turn.level
            self.moment = turn.moment
            self.ordinal = turn.ordinal

    @property
    def ts(self):
        """`final`'s own moment as ISO-8601, or None. Never a wall clock.

        The staleness stamp is the SOURCE record's timestamp — the same
        spelling `driverWindow` prints — so a reading that stopped moving an
        hour ago says so instead of looking fresh because something re-read it.
        """
        return self.moment.isoformat() if self.moment is not None else None

    def as_dict(self) -> dict:
        return {"peak": self.peak, "final": self.final, "ts": self.ts}


def summarize(fold, *, top=10) -> dict:
    """Every number the item names, as a plain dict (JSON-ready, no clock)."""
    turns = sorted(fold.turns.values(), key=lambda one: one.ordinal)
    owners = {}
    totals = {"in": 0, "out": 0, "cached": 0, "cache_write": 0,
              "write5m": 0, "write1h": 0}
    money = {"input": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0, "output": 0.0}
    unpriced = {}
    prices = {}
    context_integral = 0
    level = _Level()
    by_owner = {}
    for turn in turns:
        for name, value in turn.tokens.items():
            totals[name] += value
        # The reported 5m/1h pair is what was BILLED, not the raw fold, so
        # `write5m + write1h == cache_write` is an invariant of every report
        # and the two numbers can never contradict the total beside them.
        billed5m, billed1h = turn.billed_writes
        totals["write5m"] += billed5m - turn.tokens["write5m"]
        totals["write1h"] += billed1h - turn.tokens["write1h"]
        context_integral += turn.context
        # The LEVEL, folded beside the integral and never into it. Only
        # qualifying turns are offered: an owner whose every turn is a
        # zero-context `<synthetic>` record is therefore ABSENT from
        # `contextByOwner` rather than present with a zero — it is still an
        # agent, `agents` still counts it, and what is unknown about it is
        # spelled as an absence.
        if turn.context_qualifies:
            level.note(turn)
            if turn.owner not in by_owner:
                by_owner[turn.owner] = _Level()
            by_owner[turn.owner].note(turn)
        # An owner is seeded by its FIRST turn — but a zero-context turn is not
        # a baseline, it is a `<synthetic>` harness record with no usage at all,
        # and letting one land first would contribute a 0 to the median for an
        # agent that read a full prefix like every other. The owner is still
        # registered (it is an agent; `agents` counts it), only its baseline
        # waits for a turn that actually read something.
        if turn.owner not in owners:
            owners[turn.owner] = None
        if owners[turn.owner] is None and turn.context:
            owners[turn.owner] = turn.context
        spend = _dollars(turn, prices)
        if spend is None:
            named = turn.model or "(no model recorded)"
            unpriced[named] = unpriced.get(named, 0) + 1
            continue
        for name, value in zip(("input", "cacheRead", "cacheWrite", "output"), spend):
            money[name] += value
    baselines = sorted(one for one in owners.values() if one)

    baseline = statistics.median(baselines) if baselines else 0.0
    agents = len(owners)
    turn_count = len(turns)
    re_read = sorted(((count, name) for name, count in fold.reads.items()),
                     key=lambda pair: (-pair[0], pair[1]))
    return {
        "agents": agents,
        "turns": turn_count,
        "contextIntegral": context_integral,
        # The four LEVEL keys, deliberately NOT inside `dollars`: every figure
        # in that block is a floor, which is a claim about a sum and says
        # nothing about an instant. `None` here means no turn qualified — it is
        # never a zero, and a consumer that renders it must render nothing.
        "contextPeak": level.peak,
        "contextFinal": level.final,
        "contextFinalTs": level.ts,
        "contextByOwner": {name: stats.as_dict()
                           for name, stats in sorted(by_owner.items())},
        "baselinePerTurn": baseline,
        "baselineShare": (baseline * turn_count / context_integral
                          if context_integral else 0.0),
        "promptTokensPerAgent": (context_integral / agents) if agents else 0.0,
        "tokens": dict(totals),
        "dollars": dict(money, total=sum(money.values())),
        "topReReadFiles": [{"path": name, "reads": count}
                           for count, name in re_read[:max(top, 0)]],
        "unpricedModels": dict(sorted(unpriced.items())),
        "files": fold.files,
        "unusableUsage": fold.unusable,
        "cacheWritesWithoutTtlSplit": fold.split_missing,
    }


def analyze(*, task_dir=None, wf_dir=None, top=10, expand=True,
            whole_session=False, env=None, cwd=None) -> dict:
    """The whole reading: agents, driver, and the driver's share.

    The `wf_dir` is an ANCHOR, not the corpus — see :func:`run_dirs_for` and
    the module docstring. Both sides of the fold are taken over the same set of
    session directories, which is the only way `driverShare` can mean anything
    on a resumed run.

    The driver half is additionally bounded to the run's own time window,
    because a session directory can hold several runs and an unbounded fold
    would charge one session's driver turns to every run it launched. The bound
    is reported (`driverScope`, `driverWindow`, `driverCoTenantRuns`) rather
    than assumed. ``whole_session=True`` declines the slice and labels the
    result accordingly — it never renders as "of the run".

    Either half may be absent — an archived session has no driver transcript, a
    plan-only task folder has no `wf_dir` — and each absence is reported as
    such rather than as a zero.
    """
    resolved_task = os.path.abspath(os.fspath(task_dir)) if task_dir else None
    if wf_dir is None:
        if resolved_task is None:
            resolved_task = newest_task_dir(cwd=cwd, env=env)
        if resolved_task:
            configured = read_config(resolved_task).get("wf_dir")
            if isinstance(configured, str) and configured:
                wf_dir = configured
    run_dirs = run_dirs_for(wf_dir, expand=expand, cwd=cwd, env=env)
    report = {
        "taskDir": resolved_task,
        "wfDir": wf_dir,
        "runId": os.path.basename(os.path.normpath(wf_dir)) if wf_dir else None,
        "runDirs": list(run_dirs),
        "sessionIds": sorted({one for one in
                              (ingest.session_id_for_path(d) for d in run_dirs)
                              if one}),
        "corpus": "absent",
    }
    transcripts = [one for directory in run_dirs
                   for one in agent_transcripts(directory)]
    # The ANCHOR's own transcript goes first, before the sorted expansion, so
    # `driver["transcript"]` is the launching session's and not whichever
    # sessionId happens to sort lowest — `run_dirs_for` sorts by path string,
    # and a resumed run whose second sessionId sorts first would otherwise
    # silently rename "the launching session" in the report.
    driver_paths = []
    anchor_driver = driver_transcript_for(wf_dir) if wf_dir else None
    if anchor_driver:
        driver_paths.append(anchor_driver)
    for directory in run_dirs:
        found = driver_transcript_for(directory)
        if found and found not in driver_paths:
            driver_paths.append(found)
    if not transcripts and not driver_paths:
        report["note"] = ("no agent transcript and no driver transcript under "
                          "this run — nothing to measure")
        return report
    report["corpus"] = "present"
    # A run directory whose session transcript has been swept is reported, not
    # silently dropped: "3 of 4 driver transcripts survive" is a materially
    # different sentence from "the driver cost this much".
    report["driverSessionsMissing"] = len(run_dirs) - len(driver_paths)
    # The sessions this run shares with OTHER runs. Reported whether or not it
    # changes a number, because "this session also launched 2 other runs" is
    # what tells a reader the driver row is a slice rather than a whole.
    co_tenants = co_tenant_run_ids(run_dirs)
    report["driverCoTenantRuns"] = list(co_tenants)
    agent_fold = Fold()
    for one in transcripts:
        fold_transcript(one, agent_fold)
    report["agentsSummary"] = summarize(agent_fold, top=top)
    if not driver_paths:
        report["driver"] = None
        report["driverNote"] = ("no session transcript for this run is on disk "
                                "(archived) — the driver row is unavailable, "
                                "not zero")
        report["driverShare"] = None
        report["driverScope"] = None
        report["driverWindow"] = None
        return report
    run_id = report["runId"]
    window = None if whole_session else agent_fold.window
    # Present-or-null like every other driver field: None means "no window to
    # widen, so no launch record was looked for", which is a different fact
    # from "looked and found none". An absent KEY is neither, and `--json` is
    # read by a close-out epilogue that cannot ask.
    report["driverLaunchSeen"] = None
    if window is not None:
        # Widen backwards to the launch, when the driver's own transcript
        # records one: the turn that emitted `toolUseResult.runId` is this
        # run's first cost and predates every agent record it created.
        launches = [one for one in
                    (launch_moment_for(path, run_id) for path in driver_paths)
                    if one is not None]
        if launches:
            earliest = min(launches)
            if earliest < window[0]:
                window = (earliest, window[1])
        report["driverLaunchSeen"] = bool(launches)
    driver_fold = Fold()
    # Folded in SORTED path order, not in the anchor-first order of the list
    # above. That list is anchor-first because `driver["transcript"]` has to
    # name the LAUNCHING session — but fold position is the level's timestamp
    # tie-break, and a tie-break that depended on which session directory the
    # caller happened to name would make two readings of one run disagree about
    # a number. Naming either directory reads the same run, down to every
    # figure on the driver row, and that has to survive a tie: this corpus's
    # two driver turns share a millisecond, and unsorted they answered 910 from
    # one anchor and 705 from the other. Money is order-free, so nothing else
    # here can notice.
    for one in sorted(driver_paths):
        fold_transcript(one, driver_fold, window=window,
                        owner=ingest.session_id_for_path(one) or "driver")
    driver = summarize(driver_fold, top=top)
    # `summarize` counts OWNERS and calls them agents, which is true of the
    # agent half and false here: the driver fold's owners are sessions. Both
    # agent-named keys are renamed rather than left to be misread beside
    # `sessions` (transcripts READ, which is the larger number whenever a
    # session contributed no in-window turn).
    driver["owners"] = driver.pop("agents")
    driver["promptTokensPerOwner"] = driver.pop("promptTokensPerAgent")
    driver["transcript"] = driver_paths[0]
    driver["transcripts"] = list(driver_paths)
    driver["sessions"] = len(driver_paths)
    driver["turnsOutsideWindow"] = driver_fold.outside_window
    driver["turnsUndated"] = driver_fold.undated
    report["driver"] = driver
    if whole_session:
        report["driverScope"] = "whole session"
    elif window is None:
        # No agent record carried a usable timestamp, so there is no window to
        # slice with. Fall back to the whole file and SAY so — an unlabelled
        # whole-session number is the defect this scope field exists to end.
        report["driverScope"] = "whole session (no dated agent record to bound it)"
    else:
        report["driverScope"] = "run window"
    report["driverWindow"] = ([window[0].isoformat(), window[1].isoformat()]
                              if window is not None else None)
    # The driver's LEVEL keys inherit the scope that produced them, because on
    # their own they would answer a question nobody asked. `summarize` reports
    # the greatest-timestamp turn OF THIS FOLD, and this fold is a slice: the
    # close-out tail after the last agent record is outside the window, so the
    # driver's last in-window turn is not "the driver's context now" — and
    # under `--driver-whole-session` the last turn may belong to another run
    # entirely. Labelled rather than dropped, and never rendered beside the
    # run's own level (see `render`). ONE key scopes the WHOLE level block —
    # `contextPeak`, `contextFinal`, `contextFinalTs` and `contextByOwner` are
    # every one of them computed over this same sliced fold — which is exactly
    # why it is not named after any single one of them.
    driver["contextScope"] = report["driverScope"]
    notes = []
    if report["driverSessionsMissing"]:
        notes.append(
            f"{report['driverSessionsMissing']} of {len(run_dirs)} session "
            f"transcripts are not on disk (archived) — the driver row is a "
            f"floor over the {len(driver_paths)} that survive")
    if co_tenants and report["driverScope"] != "run window":
        notes.append(
            f"WHOLE-SESSION figure: these session transcripts also cover "
            f"{len(co_tenants)} other run(s) ({', '.join(co_tenants)}) — it is "
            f"not this run's driver cost, and summing it across those runs "
            f"counts the same turns more than once")
    elif co_tenants:
        notes.append(
            f"sliced to this run's window; the same session transcripts also "
            f"cover {len(co_tenants)} other run(s) ({', '.join(co_tenants)})")
    # WHY the window dropped what it dropped, decided from what was measured
    # rather than assumed. With no co-tenant there is no other run for an
    # excluded turn to belong to, and every one of them is this run's own
    # driver work outside the agent span: the drafting before the launch record
    # (the launch widening recovers the launching turn, not the drafting), and
    # the whole close-out tail after the last agent record. Said in the NOTE
    # rather than in the rendering alone, so `--json` carries it too.
    if report["driverScope"] == "run window" and driver["turnsOutsideWindow"]:
        notes.append(
            f"{driver['turnsOutsideWindow']} driver turn(s) fall outside this "
            f"run's window: "
            + ("some belong to the other run(s) named above, and this run's own "
               "pre-launch drafting and close-out tail are outside it too"
               if co_tenants else
               "no other run shares these transcripts, so they are this run's "
               "OWN work outside the agent span — pre-launch drafting and the "
               "close-out tail")
            + ", so the driver row is a floor")
    if driver_fold.undated:
        notes.append(
            f"{driver_fold.undated} driver turn(s) carry no usable timestamp "
            f"and could not be placed in the window — excluded, so the row is "
            f"low by that much")
    if notes:
        report["driverNote"] = "; ".join(notes)
    both = report["agentsSummary"]["contextIntegral"] + driver["contextIntegral"]
    report["driverShare"] = (driver["contextIntegral"] / both) if both else None
    return report


def _degradations(summary):
    """Every measured degradation in ``summary``, as rendered lines.

    Written once and used by BOTH halves. `render` is what lands in a release
    transcript while the JSON is read by nobody, so a degradation the JSON
    carries and the text drops is hidden — and a rule enforced on the agent
    summary alone is a rule the driver row is exempt from, which is how the
    driver's own `unpricedModels` stayed invisible.
    """
    lines = []
    unpriced = summary.get("unpricedModels")
    if unpriced:
        named = ", ".join(f"{name} x{count}" for name, count in unpriced.items())
        lines.append(f"unpriced models {named}")
    if summary.get("cacheWritesWithoutTtlSplit"):
        lines.append(f"cache writes with no TTL split: "
                     f"{summary['cacheWritesWithoutTtlSplit']} "
                     f"(billed at the 5m rate)")
    if summary.get("unusableUsage"):
        lines.append(f"turns with unusable usage: {summary['unusableUsage']} "
                     f"(refused, not coerced — the totals are low by that much)")
    return lines


def _excluded_clause(driver) -> str:
    """How many turns the window dropped — the COUNT, and no attribution.

    The clause used to read "outside it belong to other runs" unconditionally,
    which is false whenever `driverCoTenantRuns` is empty: there is no other
    run to belong to. Why they are outside is measured in :func:`analyze` and
    said in `driverNote`, which `render` prints a few lines further down this
    same block — so the reason reaches the JSON as well as the transcript, and
    what belongs here is the number.
    """
    excluded = driver.get("turnsOutsideWindow")
    return f", {excluded} turn(s) outside it" if excluded else ""


def render(report) -> str:
    """The human half: one fixed-width block, no colour, no clock."""
    lines = []
    if report.get("corpus") != "present":
        lines.append("touch-cost: no corpus")
        if report.get("taskDir"):
            lines.append(f"  task: {report['taskDir']}")
        if report.get("note"):
            lines.append(f"  {report['note']}")
        return "\n".join(lines)
    summary = report["agentsSummary"]
    lines.append(f"touch-cost: run {report.get('runId') or '?'}")
    if report.get("taskDir"):
        lines.append(f"  task            {report['taskDir']}")
    # A resumed run is stated out loud. Silence here is how a reader concludes
    # the anchor directory was the whole run, which for two of this machine's
    # three decomposed runs it is not.
    session_ids = report.get("sessionIds") or []
    if len(session_ids) > 1:
        lines.append(f"  sessions        {len(session_ids)} "
                     f"(resumed run — folded across "
                     f"{len(report.get('runDirs') or session_ids)} run dirs)")
    lines.append(f"  agents          {summary['agents']}")
    lines.append(f"  turns           {summary['turns']}")
    lines.append(f"  context-integral{summary['contextIntegral']:>14,} tok")
    # The LEVEL, on its own line and deliberately far from the dollar column: a
    # reader scanning a money block reads every number in it as money, and this
    # one is how full a window was. Printed only when a turn qualified —
    # a fixed-width row has no way to spell "unknown" that a reader will not
    # quote back as a number, so the honest rendering of an absent level is an
    # absent line.
    if summary.get("contextFinal") is not None:
        # The stamp is unconditional on purpose: `_Level` only ever sets
        # `final` inside the branch that also sets `moment`, so a level with no
        # instant attached is unreachable — and printing a bare number here
        # would be printing a level with no "as of", which is the one thing a
        # staleness stamp exists to prevent.
        #
        # And it is named with its OWNER, because it belongs to one agent. Every
        # other figure in this block is a run-wide fact; this one is "whichever
        # agent spoke last", every cross-agent aggregate of a level being a
        # fabrication (separate windows). `contextByOwner` already knows which
        # — the run's winning turn is that owner's winning turn too — and when
        # a tie leaves that ambiguous the parenthetical still refuses to let the
        # number read as the run's.
        owners = [name for name, one in
                  sorted((summary.get("contextByOwner") or {}).items())
                  if one.get("final") == summary["contextFinal"]
                  and one.get("ts") == summary["contextFinalTs"]]
        who = owners[0] if len(owners) == 1 else "last agent"
        lines.append(f"  context final   {summary['contextFinal']:>14,} tok"
                     f" @ {summary['contextFinalTs']} ({who})")
    lines.append(f"  baseline/turn   {summary['baselinePerTurn']:>14,.0f} tok")
    lines.append(f"  baseline share  {summary['baselineShare'] * 100:>13.1f} %")
    lines.append(f"  prompt tok/agent{summary['promptTokensPerAgent']:>14,.0f} tok")
    money = summary["dollars"]
    lines.append(f"  $ cache-read    {money['cacheRead']:>14.2f}")
    lines.append(f"  $ cache-write   {money['cacheWrite']:>14.2f}")
    lines.append(f"  $ input         {money['input']:>14.2f}")
    lines.append(f"  $ output        {money['output']:>14.2f}")
    lines.append(f"  $ total (floor) {money['total']:>14.2f}")
    # A degraded measurement is reported, never hidden — and the human half has
    # to say it too, because this rendering is what lands in a release
    # transcript while the JSON is read by nobody.
    lines.extend(f"  {one}" for one in _degradations(summary))
    driver = report.get("driver")
    if driver is None:
        lines.append(f"  driver          {report.get('driverNote', 'unavailable')}")
    else:
        share = report.get("driverShare")
        sessions = driver.get("sessions") or 1
        sliced = report.get("driverScope") == "run window"
        # The share's NAME is the scope, and this is the whole point of the
        # field: "% of the run" over a whole-session numerator is a sentence a
        # reader will quote, and it would be false. An un-sliced figure gets
        # said out loud as one instead.
        if share is None:
            share_text = ""
        elif sliced:
            share_text = f", {share * 100:.1f} % of the run"
        else:
            share_text = (f", {share * 100:.1f} % of session+run tokens "
                          f"(WHOLE-SESSION figure, not this run's)")
        lines.append(f"  driver          {driver['turns']} turns, "
                     f"{driver['contextIntegral']:,} tok, "
                     f"${driver['dollars']['total']:.2f}"
                     + share_text
                     + (f" (over {sessions} session transcripts)"
                        if sessions > 1 else ""))
        if sliced and report.get("driverWindow"):
            start, end = report["driverWindow"]
            lines.append(f"                  window {start} .. {end}"
                         + _excluded_clause(driver))
        elif report.get("driverScope"):
            lines.append(f"                  scope: {report['driverScope']}")
        # The driver half measures the same three degradations the agent half
        # does, and `summarize` puts all three in its JSON. A degradation the
        # JSON carries and the text drops is hidden — the rule stated for the
        # agent summary above applies to both folds or it is not a rule.
        lines.extend(f"                  driver {one}"
                     for one in _degradations(driver))
        if report.get("driverNote"):
            lines.append(f"                  {report['driverNote']}")
    if summary["topReReadFiles"]:
        lines.append("  top re-read files")
        for entry in summary["topReReadFiles"]:
            lines.append(f"    {entry['reads']:>4} x {entry['path']}")
    return "\n".join(lines)


# --- D-22's half: the always-on context baseline ---------------------------


def estimate_tokens(data) -> int:
    """chars/4 over BYTES. Calibrated once, pinned here, never re-derived.

    Takes the bytes themselves or a byte COUNT, so a caller that has already
    summed some sizes (`skill_descriptions`) still goes through this function
    instead of writing `// BYTES_PER_TOKEN` again — two spellings of one
    estimator is two estimators the day one of them changes.
    """
    size = data if isinstance(data, int) else len(data)
    return size // BYTES_PER_TOKEN


def _read_bytes(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def description_text(source):
    """A `SKILL.md`'s always-on `description:` value, or None.

    Handles both YAML spellings — the single line, and the folded/literal block
    whose text lives in the indented lines under `description: >-`. The block
    form is checked FIRST: its header line matches the single-line pattern too,
    with an empty capture, so testing them the other way round would report a
    folded description as zero bytes.
    """
    block = _DESCRIPTION_BLOCK_RE.search(source)
    if block:
        lines = [one.strip() for one in block.group("body").splitlines()]
        joined = " ".join(one for one in lines if one)
        return joined or None
    match = _DESCRIPTION_RE.search(source)
    if match:
        return match.group("text") or None
    return None


def skill_descriptions(repo) -> tuple:
    """`(bytes, count)` of the always-on `description:` frontmatter values.

    Only the description is always-on — the body of a `SKILL.md` is read on
    demand — so budgeting the whole file would measure something no session
    pays for on every turn.
    """
    root = os.path.join(os.fspath(repo), SKILLS_GLOB_ROOT)
    total = 0
    count = 0
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return (0, 0)
    for name in names:
        raw = _read_bytes(os.path.join(root, name, "SKILL.md"))
        if raw is None:
            continue
        text = description_text(raw.decode("utf-8", "replace"))
        if text is not None:
            total += len(text.encode("utf-8"))
            count += 1
    return (total, count)


def declared_budgets(repo):
    """The budgets `tests/test_context_budget.py` declares, or None.

    Only the names in :data:`BUDGET_KEYS` are read — see that constant for why
    the set is closed rather than a suffix match. What is returned is what was
    FOUND, which may be a subset; :func:`baseline` is what decides a partial
    declaration is not a ceiling, because "some of the budget" is not a number
    anything may be gated on.

    Both assignment forms are read. `CLAUDE_MD_BUDGET_TOKENS = 6000` is an
    `ast.Assign`; the annotated `CLAUDE_MD_BUDGET_TOKENS: int = 6000` is an
    `ast.AnnAssign` and matches no `Assign` filter — a reader that saw only the
    first would report a fully-declared file as partial, purely because sp-10
    typed its constants.

    Parsed with `ast`, never imported: this runs inside a release gate, and
    importing a test module to read three integers executes whatever else that
    module does at import time. Absent today — sp-10 lands the file — so the
    caller's ceiling stands until it does.
    """
    path = os.path.join(os.fspath(repo), BUDGET_TEST_REL)
    raw = _read_bytes(path)
    if raw is None:
        return None
    try:
        tree = ast.parse(raw.decode("utf-8", "replace"), filename=path)
    except SyntaxError:
        return None
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]       # `value` is None on a bare annotation
        else:
            continue
        if not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, int) or isinstance(node.value.value, bool):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in BUDGET_KEYS:
                found[target.id] = node.value.value
    return found or None


def baseline(repo, *, ceiling=None) -> dict:
    """The always-on prefix this repo owns, measured and compared."""
    root = os.path.abspath(os.fspath(repo))
    entries = []
    total = 0
    for label, relative in BASELINE_SOURCES:
        raw = _read_bytes(os.path.join(root, relative))
        if raw is None:
            entries.append({"name": label, "path": relative,
                            "tokens": 0, "bytes": 0, "present": False})
            continue
        tokens = estimate_tokens(raw)
        total += tokens
        entries.append({"name": label, "path": relative, "tokens": tokens,
                        "bytes": len(raw), "present": True})
    described, count = skill_descriptions(root)
    skill_tokens = estimate_tokens(described)
    total += skill_tokens
    entries.append({"name": f"skill descriptions ({count})",
                    "path": SKILLS_GLOB_ROOT, "tokens": skill_tokens,
                    "bytes": described, "present": bool(count)})
    # A PARTIAL declaration is not a ceiling. sp-10 can reach one two ways —
    # declaring its three constants incrementally, or landing two of them in a
    # commit — and summing what happens to be there would gate the release at
    # 600 or 6,000 while `ceilingSource` confidently named the file. That is
    # the same "confidently naming the file" failure `BUDGET_KEYS`'s comment
    # exists to prevent, in the other direction, so the missing names are said
    # out loud and the caller's number stands until the file is complete.
    declared = declared_budgets(root)
    missing = sorted(set(BUDGET_KEYS) - set(declared or ()))
    if declared and not missing:
        limit = sum(declared[name] for name in BUDGET_KEYS)
        source = BUDGET_TEST_REL
    elif declared:
        limit = ceiling
        source = (f"{BUDGET_TEST_REL} is incomplete (missing "
                  f"{', '.join(missing)}) — using the caller's recorded ceiling")
    else:
        limit = ceiling
        source = "the caller's recorded ceiling"
    return {
        "repo": root,
        "entries": entries,
        "totalTokens": total,
        "ceiling": limit,
        "ceilingSource": source,
        "declaredBudgets": declared,
        "over": bool(limit is not None and total > limit),
    }


def render_baseline(measured) -> str:
    lines = ["touch-cost --baseline: the always-on context this repo owns"]
    for entry in measured["entries"]:
        mark = "" if entry["present"] else "  (absent)"
        lines.append(f"  {entry['name']:<26}{entry['tokens']:>8,} tok "
                     f"{entry['bytes']:>9,} B{mark}")
    lines.append(f"  {'TOTAL':<26}{measured['totalTokens']:>8,} tok")
    if measured["ceiling"] is None:
        lines.append("  ceiling                     none given — printed, not gated")
    else:
        verdict = "OVER" if measured["over"] else "under"
        lines.append(f"  {'ceiling':<26}{measured['ceiling']:>8,} tok "
                     f"({verdict}; source: {measured['ceilingSource']})")
    return "\n".join(lines)


# --- CLI -------------------------------------------------------------------


def _usage():
    return (
        "usage: python3 -m aggregator.costs [--task DIR | --wf-dir DIR]\n"
        "                                   [--single-session] [--driver-whole-session]\n"
        "                                   [--top N] [--json]\n"
        "       python3 -m aggregator.costs --baseline [--repo DIR]\n"
        "                                   [--ceiling N] [--json]\n"
        "  (default)    read one run's agent + driver transcripts and print\n"
        "               agents, turns, context-integral, baseline/turn,\n"
        "               baseline share, $ by cache class, top re-read files,\n"
        "               prompt tok/agent and a driver row. A resumed run spans\n"
        "               several session directories and ALL of them are read;\n"
        "               the driver row is bounded to the run's time window,\n"
        "               because one session can launch several runs.\n"
        "               No corpus -> a clean skip (exit 0).\n"
        "  --single-session\n"
        "               read only the directory named, not the run's other\n"
        "               sessions. An escape hatch, never the default.\n"
        "  --driver-whole-session\n"
        "               do not bound the driver row to this run's window; fold\n"
        "               each session transcript whole. The result is a SESSION\n"
        "               figure and is labelled as one. An escape hatch.\n"
        "  --baseline   measure the always-on context prefix this repo owns\n"
        "               and compare it against --ceiling (or, once it exists,\n"
        "               the budgets tests/test_context_budget.py declares).\n"
        "               Exits 1 when over.\n"
        "  --json       print the same numbers as JSON.\n"
        "Reads only. Never writes, never reaches the network.\n")


def _parse(argv):
    options = {"mode": "run", "task": None, "wf_dir": None, "top": 10,
               "json": False, "repo": None, "ceiling": None, "expand": True,
               "whole_session": False}
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if flag in ("-h", "--help"):
            return None
        if flag == "--json":
            options["json"] = True
            continue
        if flag == "--single-session":
            options["expand"] = False
            continue
        if flag == "--driver-whole-session":
            options["whole_session"] = True
            continue
        if flag == "--baseline":
            options["mode"] = "baseline"
            continue
        if flag in ("--task", "--wf-dir", "--top", "--repo", "--ceiling"):
            if not rest:
                raise CostsError(f"{flag} needs a value")
            value = rest.pop(0)
            if flag == "--top":
                options["top"] = int(value)
            elif flag == "--ceiling":
                options["ceiling"] = int(value)
            elif flag == "--wf-dir":
                options["wf_dir"] = value
            elif flag == "--task":
                options["task"] = value
            else:
                options["repo"] = value
            continue
        raise CostsError(f"unknown argument {flag!r}")
    # `--wf-dir` wins over `--task` inside `analyze`, so accepting both would
    # print a report naming a task folder it did not use to find the run. This
    # module refuses rather than guesses, everywhere else; here too.
    if options["task"] and options["wf_dir"]:
        raise CostsError("--task and --wf-dir name the run two different ways;"
                         " pass one")
    return options


def main(argv=None):
    """A small operator CLI. Returns a process exit code; writes nothing."""
    try:
        options = _parse(list(argv if argv is not None else []))
    except (CostsError, ValueError) as exc:
        print(f"costs: {exc}\n", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2
    if options is None:
        print(_usage())
        return 0
    if options["mode"] == "baseline":
        repo = options["repo"] or paths.project_root()
        measured = baseline(repo, ceiling=options["ceiling"])
        print(json.dumps(measured, indent=2, sort_keys=True) if options["json"]
              else render_baseline(measured))
        return 1 if measured["over"] else 0
    report = analyze(task_dir=options["task"], wf_dir=options["wf_dir"],
                     top=options["top"], expand=options["expand"],
                     whole_session=options["whole_session"])
    print(json.dumps(report, indent=2, sort_keys=True) if options["json"]
          else render(report))
    return 0


if __name__ == "__main__":                                       # pragma: no cover
    sys.exit(main(sys.argv[1:]))
