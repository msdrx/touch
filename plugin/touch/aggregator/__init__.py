"""Touch aggregator — the read/serve side of Touch (R-22 scaffold).

Layout is GD-15's, one file exactly one owner:

    tailer.py       incremental file tailing (R-23)
    store.py        `.touch/` touch-events-v2 append-only store (R-24)
    ws.py           RFC 6455 codec, pure functions (R-29)
    refs.py         ref_key canonicalizer (R-43)
    mongo_store.py  collections/indexes/upsert algebra (R-44)
    mirror.py       write-behind Mongo mirror (R-45)
    sessions.py ingest.py legacy.py agents.py custom_state.py server.py

Runtime dependency policy (GD-21, the D8.1 amendment): **stdlib only on the
ingest and serve critical path.** `pymongo` (pinned `==4.17.0`, with
`dnspython`) is the ONE permitted third-party runtime dependency and may be
imported **only** from `aggregator/mongo_store.py` and `aggregator/mirror.py`,
lazily. Every other module in this package must import with no third-party
package installed at all; `tests/test_stdlib_only.py` is the static guard and
names that single exception explicitly.

This module deliberately imports **nothing** from the package: importing
`aggregator` must never drag in a Mongo-capable module, and the leaf modules
(`tailer`, `store`, `ws`) must stay independently importable.
"""

import sys

__all__ = ["__version__", "SCHEMA_VERSION", "PY_MIN", "check_python"]

__version__ = "0.1.0"

# touch-events-v2 (D4/GD-11). Readers treat a record with no `v` as v1 (legacy).
SCHEMA_VERSION = 2

# D8 stack floor. Kept as a tuple so callers can compare without parsing.
PY_MIN = (3, 11)


def check_python(version=None):
    """Raise RuntimeError with an actionable message below PY_MIN; else return True.

    Called by entry points (server, mirror, CLI), never at import time: a test
    collector on an old interpreter should fail loudly in one place, not with
    a SyntaxError-shaped mystery in whichever module happened to be imported
    first.
    """
    v = tuple((version or sys.version_info)[:2])
    if v < PY_MIN:
        raise RuntimeError(
            "Touch needs Python %d.%d+ (found %d.%d); the aggregator uses "
            "asyncio and typing features added in %d.%d"
            % (PY_MIN + v + PY_MIN)
        )
    return True
