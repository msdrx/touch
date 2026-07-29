"""The package's ONE `__file__` reference: package root vs project root (CM-2).

Every derived root in the aggregator used to be spelled

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

— "the directory above the package". In this checkout that expression happens
to be the project root, so four wrong roots and one right one were
indistinguishable. Under a plugin install the same expression is
`~/.claude/plugins/cache/<marketplace>/touch/<version>/`: a version-stamped,
docs-declared-ephemeral directory that is swept ~14 days after the next update
(PLUGIN-SPEC-6). The `.touch/` write-ahead log — the one store that is *not*
rebuildable, because the CLI's retention sweep deletes transcripts — would be
written there, the task scan would find nothing, and `mirror.database_name`
would digest the version directory, silently starting a brand-new Mongo
database on every plugin update.

So this module owns the distinction, and it is the only place in the package
that mentions `__file__` (item 03; GD-T5's state law):

* :func:`plugin_root` — where the package's own **files** live. Legitimate for
  exactly one thing today: `touch-visual/` assets, which genuinely ship with
  the code (`server.py`'s `--assets` default). Nothing mutable, ever.
* :func:`project_root` — the checkout Touch is serving: `$CLAUDE_PROJECT_DIR`
  > `$TOUCH_PROJECT_CWD` > a cwd walk-up to a `.claude/` marker >
  `os.getcwd()`. Everything that is written, scanned or keyed hangs off this:
  `.touch/` (`store.state_root`), the task folders (`legacy.orchestrator_root`
  and `server`'s `--tasks-root` default) and the Mongo `database_name` digest.

`$CLAUDE_PROJECT_DIR` first because a hook process is the one component the
harness hands a first-class project anchor (GD-T4/GD-T5), and
`$TOUCH_PROJECT_CWD` second because `sessions.project_cwd` already uses it for
exactly this meaning — "which checkout does this daemon serve" — so a single
export keeps discovery and the derived roots pointing at the same place.

`$CLAUDE_PLUGIN_ROOT` is deliberately **not** consulted by
:func:`plugin_root`. It is redundant when it is right (the package sits one
level below it, which is what the `__file__` expression already computes) and
wrong when it differs (it is exported into a *plugin's* hook/MCP environment,
so a foreign plugin's hook spawning `touch-serve` would aim the assets
resolver at that plugin's tree), and GD-T4 measured it EMPTY in `bin/`, which
is how the wrappers actually start. Self-location is exact; the env var is a
guess.

This module is **pure**: stdlib only (GD-21), no I/O beyond `os.path` stats,
no package imports — so `store.py`, `legacy.py`, `mirror.py` and `server.py`
can all import it at module level without touching GD-15's ownership rules.

One consequence of GD-U1 worth stating where the roots are defined
(PLUGIN-RUNTIME-12): `touch-serve` puts the PLUGIN ROOT on `sys.path`, so every
directory name at that root — `aggregator`, `docs`, `hooks`, `bin`, `shared`,
`skills`, `touch-visual` — becomes a top-level import name AHEAD of
site-packages. Adding a directory here can therefore shadow an installed
package of the same name for the whole process. `plugin_root()` itself needs no
change for any of this: it is `__file__`-derived, and `touch-visual/` is still
the package's sibling wherever the payload is unpacked.
"""

from __future__ import annotations

import os

__all__ = ["PROJECT_MARKER", "plugin_root", "project_root"]

#: The directory whose presence makes an ancestor a project root during the
#: walk-up. `.claude/` and not `.git/`: Touch reads a *Claude Code* project
#: (its transcripts, its `local-orchestrators/` history), and a git checkout
#: without `.claude/` has nothing for Touch to serve.
PROJECT_MARKER = ".claude"


def plugin_root() -> str:
    """The directory the package's own files live in — **assets only** (CM-2).

    Equals the repo root in this checkout and `<cache>/<version>/` under a
    plugin install; both are correct, because what is resolved against it
    (`touch-visual/`) travels with the code. Never join anything writable onto
    this — see the module docstring.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _marker_walk_up(start: str) -> str | None:
    """Nearest ancestor of `start` (inclusive) holding a `.claude/` directory.

    `~/.claude/` is skipped: it is the CLI's own configuration directory, not a
    project marker, so a daemon started from an arbitrary directory under
    `$HOME` must fall through to `os.getcwd()` rather than adopt the whole home
    directory as a project (and write `~/.touch`).
    """
    try:
        home = os.path.realpath(os.path.expanduser("~"))
    except (OSError, RuntimeError, KeyError):        # no HOME, no passwd entry
        home = None
    current = start
    while True:
        if (home is None or os.path.realpath(current) != home) and os.path.isdir(
            os.path.join(current, PROJECT_MARKER)
        ):
            return current
        parent = os.path.dirname(current)
        if parent == current:                         # filesystem root
            return None
        current = parent


def project_root(root=None, *, cwd=None, env=None) -> str:
    """The project Touch serves: arg > `$CLAUDE_PROJECT_DIR` > `$TOUCH_PROJECT_CWD`
    > cwd walk-up to a `.claude/` marker > `os.getcwd()` (GD-T5).

    An explicit argument always wins, so every caller keeps its own override
    (`--tasks-root`, `TOUCH_LEGACY_ROOT`, `TOUCH_STATE_DIR`, an explicit
    `repo=`) without this function knowing about any of them. `env` and `cwd`
    are injectable for the tests, the way `legacy.orchestrator_root` and
    `sessions.project_cwd` already are — a resolution order that can only be
    exercised by mutating the process environment is a resolution order nobody
    tests all four arms of.

    Empty env values are treated as unset: an exported-but-empty
    `CLAUDE_PROJECT_DIR` would otherwise resolve every root to `/`.
    """
    if root:
        return os.path.abspath(os.fspath(root))
    environ = os.environ if env is None else env
    for name in ("CLAUDE_PROJECT_DIR", "TOUCH_PROJECT_CWD"):
        configured = environ.get(name)
        if configured:
            return os.path.abspath(os.fspath(configured))
    start = os.path.abspath(os.fspath(cwd)) if cwd else os.getcwd()
    found = _marker_walk_up(start)
    return found if found is not None else start
