"""One anchor for every canonical path the Touch suite reads (item 03/05).

Not a test — the deliberate leading underscore keeps it out of `run_all.sh`'s
`test_*.py` glob (the same naming rule that makes the monitoring module's
stream generator `gen_stream.py`). It is imported for its constants only.

Why it exists. Before GD-U1 the repo carried TWO copies of `aggregator/`,
`touch-visual/`, `docs/` and the monitoring core: canonical ones at the repo
root and pinned ones under `plugin/touch/`. GD-U1 makes `plugin/touch/` the
single canonical home, and a migration that has to edit one `REPO / "..."`
literal in each of ~27 places is a migration that silently half-lands. So every
Touch test names the tree it means through the four constants below, and the
move is a two-line edit HERE.

    REPO      the repository root (this file's grandparent). Test material —
              `tests/fixtures/`, `tests/run_all.sh`, `.git` — hangs off it and
              never moves.
    PAYLOAD   the shipping subtree, `plugin/touch/`. Tests that mean "what a
              consumer installs" (`test_plugin_tree.py`, `test_package.py`,
              `test_skills_payload.py`, `test_bin_wrappers.py`) use this and
              say so at their own anchor — they are deliberately NOT rewritten
              in terms of SRC.
    SRC       the directory that CONTAINS the canonical `aggregator/`,
              `touch-visual/` and `docs/`. This is the flip point.
    MON       the canonical monitoring module directory (the five core files:
              `status.sh`, `monitor_server.py`, `decision_watcher.py`,
              `monitor.html`, `monitoring.md`). The second flip point.
    ORCH_REL  the tasks root RELATIVE to a project root — the two components
              every shipped resolver joins onto whatever project it found
              (`paths.tasks_root()`, `legacy.TASK_ROOT`,
              `server.default_tasks_root()`, `status.sh`, both daemons).
              Flip point 3.
    ORCH      this repo's own run history, `REPO / ORCH_REL` — the tree the
              task folders live in.
    CATALOG   the marketplace catalog, `.claude-plugin/marketplace.json` at the
              REPO root — the one canonical path that is deliberately NOT under
              PAYLOAD. It is not payload: it is a catalog *about* the payload,
              and `/plugin marketplace add msdrx/touch` clones this repository
              and reads `<clone>/.claude-plugin/marketplace.json` and nowhere
              else. It gets an anchor for the same reason the trees do — it was
              spelled out longhand in three places, which is how a move
              half-lands (RELEASE-TESTS-15).

Since GD-U1's move SRC == PAYLOAD and MON == PAYLOAD/"shared"/"monitoring".
They stay separate names because they answer different questions — "where does
the source live" and "what does a consumer install" — and a reader of
`SRC / "aggregator"` should not have to know that those happen to coincide
today. The next layout change is a two-line edit here, again.

The asserts below are the loud half: a wrong flip fails at IMPORT, in every
file at once, with the path it looked for — instead of ~200 individually
confusing "file not found" checks.

`ORCH_REL`/`ORCH` are the deliberate exception: they get NO import-time assert.
The tasks root is gitignored, so it is absent from a clean checkout of HEAD —
which is exactly the tree `scripts/release.sh` step 2 runs the whole suite in.
An assert there would turn "this repo has no runs yet" into every test file
failing to import, during a release. The tests that read real run folders skip
themselves instead (`test_docs.py`, `test_register.py`, `test_custom_state.py`).
"""
from pathlib import Path

#: The repository root. `tests/_roots.py` -> `tests/` -> repo.
REPO = Path(__file__).resolve().parents[1]

#: The shipping subtree — what `git archive HEAD:plugin/touch` packages.
PAYLOAD = REPO / "plugin" / "touch"

#: Flip point 1: the parent of the canonical `aggregator/`, `touch-visual/`,
#: `docs/`.
SRC = PAYLOAD

#: Flip point 2: the canonical monitoring module.
MON = PAYLOAD / "shared" / "monitoring"

#: Flip point 3: the tasks root as a path RELATIVE to a project root. Every
#: shipped resolver joins these two components onto the project it resolved, so
#: the leaf name `local-orchestrators` is load-bearing — the scope guard's
#: `SEG_PATTERN` is a bare literal on it (G10), which is why the move is a
#: `.claude` -> `.touch` edit and not a rename.
ORCH_REL = Path(".touch") / "local-orchestrators"

#: This repo's own run history. NO assert: see the module docstring — the tree
#: is gitignored and absent in the clean checkout `release.sh` step 2 tests.
ORCH = REPO / ORCH_REL

#: The marketplace catalog, at the REPO root and never inside PAYLOAD — a
#: git-cloned catalog is only ever read from `<repo>/.claude-plugin/
#: marketplace.json`, so this path IS the distribution model.
CATALOG = REPO / ".claude-plugin" / "marketplace.json"

assert (SRC / "aggregator").is_dir(), (
    f"_roots.SRC is wrong: no aggregator/ under {SRC}")
assert (MON / "status.sh").is_file(), (
    f"_roots.MON is wrong: no status.sh under {MON}")
assert CATALOG.is_file(), (
    f"_roots.CATALOG is wrong: no marketplace.json at {CATALOG}")
