"""Incremental, restart-safe tailing of append-mostly text files (R-23).

Every file Touch reads — harness transcripts, Workflow `journal.jsonl`, legacy
`events.jsonl`, its own `.touch/` streams — is read through this module. Two
rules from GD-20 are copied verbatim from the monitoring module's prior art
(`monitor_server.py:375-400`, `decision_watcher.py` tail loop):

* **torn tails**: cut at the last ``\\n``, never advance the offset past an
  incomplete line, defer the remainder to the next tick;
* **checkpoints are keyed to their source**, not to a name.

Two rules are explicitly *not* inherited (GD-20 do-not-inherit, RUNSTATE-15,
MONITORING-9):

* the monitor keys its checkpoint on the journal **path** only and advances
  only when ``size > offset``; a same-path in-place truncation makes it spin
  forever emitting nothing. Here the checkpoint identity is
  ``(st_dev, st_ino, size, offset)`` per D6 and **both** triggers are explicit:
  an inode/device change (rotation) **and** ``size < offset`` (in-place
  truncation) force a full, idempotent re-ingest from byte 0 (SD-10);
* the monitor re-parses whole transcripts once per second. Here per-tick work
  is **O(bytes appended since the last tick)** (GD-30): the tick stats first
  and only opens the file when `(size, mtime_ns)` moved, then reads from the
  stored offset. `tests/test_tailer.py` asserts the byte counter, not a clock.

The re-ingest this module *signals* (``TailResult.reset``, plus a bumped
``Checkpoint.gen``) is what GD-26's generation mark-and-sweep runs under. The
sweep itself — retracting ``gen < G`` documents — belongs to `mirror.py`
(SD-10); nothing here talks to a database, and nothing here parses JSON.

Line positions are part of the contract, not a convenience: GD-24 keys
positional records by line number (``stream_meta`` ``_id`` =
``<sessionId>#<line:08d>``) and stamps ``lineNo``/``byteOffset`` on every
mirrored record (R-47), so each returned line carries its **1-based** physical
line number and its absolute byte offset. Blank lines are returned too (with
``text == ""``) — skipping them here would silently shift the line numbers of
everything after them.
"""

from __future__ import annotations

import glob
import os
import time
from dataclasses import asdict, dataclass, field, replace

__all__ = [
    "Checkpoint",
    "CompactionInProgress",
    "TailLine",
    "TailResult",
    "Tailer",
    "tail_once",
    "read_complete_lines",
    "split_lines",
    "compaction_in_progress",
    "DEFAULT_READ_CAP",
    "DEFAULT_MAX_LINE_BYTES",
    "COMPACT_BACKOFF_S",
    "REASON_NEW",
    "REASON_APPEND",
    "REASON_ROTATED",
    "REASON_SHRUNK",
    "REASON_RESYNC",
    "REASON_UNCHANGED",
    "REASON_MISSING",
    "REASON_COMPACTING",
    "REASON_OVERSIZE_LINE",
]

NEWLINE = b"\n"

#: Bytes read per file per tick. A first ingest of a large file is split over
#: several ticks (``TailResult.more`` is True) so one huge transcript can never
#: pin its own size in RAM, and the poll loop keeps its 250 ms budget (GD-30).
DEFAULT_READ_CAP = 8 * 1024 * 1024

#: Hard ceiling on **one** line, used by :class:`Tailer`'s bounded escalation.
#: R-44 legislates for payloads >8 MB and ``DEFAULT_READ_CAP`` is exactly 8 MiB,
#: so the very first document R-44 was written for would otherwise be the first
#: line to wedge the live tail. 64 MiB is 8x that threshold and ~75x the 872 KB
#: largest line in the frozen corpus. Cost is bounded, not per tick: the
#: escalated read happens at most **once per observed (size, mtime_ns)** of a
#: stalled file (see :meth:`Tailer.poll`), so a file that never changes again
#: costs one 64 MiB read in total, and a file that grows every tick was going to
#: be read anyway.
DEFAULT_MAX_LINE_BYTES = 64 * 1024 * 1024

#: `.compact.tmp.*` beside a transcript means the CLI is rewriting it
#: (`performCompactTranscript`); back off rather than read a half-written file
#: (D6). A tmp file older than COMPACT_STALE_S is treated as abandoned litter,
#: otherwise one crashed compaction would stall that stream forever.
COMPACT_TMP_PREFIX = ".compact.tmp."
COMPACT_BACKOFF_S = 0.200
COMPACT_STALE_S = 60.0

REASON_NEW = "new"                  # first sight of this file
REASON_APPEND = "append"            # ordinary incremental growth
REASON_ROTATED = "rotated"          # st_dev/st_ino changed => re-ingest from 0
REASON_SHRUNK = "shrunk"            # size < offset (in-place truncate)
REASON_RESYNC = "resync"            # same size, mtime moved (opt-in re-ingest)
REASON_UNCHANGED = "unchanged"      # stat-first short circuit; file not opened
REASON_MISSING = "missing"          # path absent/unstatable this tick
REASON_COMPACTING = "compacting"    # rewrite in progress; deferred one tick
REASON_OVERSIZE_LINE = "oversize_line"   # one line > read_cap: cannot progress

RESET_REASONS = frozenset({REASON_NEW, REASON_ROTATED, REASON_SHRUNK, REASON_RESYNC})


class CompactionInProgress(Exception):
    """A one-shot read was deferred because the directory is being rewritten.

    Raised only by :func:`read_complete_lines` and only when the caller asked
    for the deferral (``skip_while_compacting=True``). A helper whose contract
    is "every complete line" may not answer a deferral with an empty list: the
    caller cannot tell that apart from an empty file, and
    :func:`compaction_in_progress` is directory-scoped, so one
    `.compact.tmp.*` would silently blank **every** transcript in that project
    directory for up to ``COMPACT_STALE_S``.
    """

    def __init__(self, path):
        super().__init__(
            f"{path}: a .compact.tmp.* rewrite is in progress in this directory; "
            f"retry, or pass skip_while_compacting=False to read anyway"
        )
        self.path = path


@dataclass(frozen=True)
class Checkpoint:
    """Where a tail left off, keyed to the source file's identity (D6).

    ``size``/``mtime_ns`` are the *observed* stat values at the last read; they
    make the stat-first short circuit possible. ``offset`` is the byte position
    after the last **complete** line consumed, ``line_no`` the 1-based number
    of that line, and ``gen`` the re-ingest generation (starts at 1 on first
    sight, +1 on every reset — GD-26's per-file ``gen``).

    Frozen: a checkpoint is a value. Advancing a tail produces a new one, so a
    caller that persists checkpoints can never accidentally write a
    half-updated one (`decision_watcher.py`'s mutable dict is the anti-pattern).
    """

    st_dev: int = 0
    st_ino: int = 0
    size: int = 0
    offset: int = 0
    line_no: int = 0
    gen: int = 0
    mtime_ns: int = 0

    @property
    def fresh(self) -> bool:
        """True when this checkpoint does not name a position in a file yet.

        Deliberately *not* keyed on ``gen``: :meth:`Tailer.rewind` keeps the
        generation counter (it is monotonic per file, GD-26) while clearing the
        position, and that must still read as "start from 0".
        """
        return self.st_ino == 0 and self.offset == 0

    def identity(self) -> tuple:
        """The four-tuple D6 names, in D6's order."""
        return (self.st_dev, self.st_ino, self.size, self.offset)

    def to_dict(self) -> dict:
        """JSON-serializable form (keys are the field names, stable order)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data) -> "Checkpoint":
        """Rebuild from :meth:`to_dict`; unknown keys are ignored, missing ones
        default. Tolerant on purpose: a checkpoint file written by an older
        Touch must never crash a restart, it may only lose precision.

        Tolerance is per key, not per file: a `null`, a string or any other
        non-integer value drops **that one field** to its default instead of
        raising. A half-written state file is exactly the shape a restart meets,
        and the restart path is the one place tolerance was the point.

        Booleans are excluded explicitly: ``int(True) == 1``, so a `true` in a
        state file would otherwise become byte offset 1 — a plausible-looking
        position rather than the default. Every other value-validator in this
        sub-plan (`validate_ref`, `normalize_tokens`) excludes bools the same
        way.
        """
        if not data:
            return cls()
        fields = {}
        for name in cls.__dataclass_fields__:
            if name not in data:
                continue
            value = data[name]
            if isinstance(value, bool):
                continue                     # not a position; take the default
            try:
                fields[name] = int(value)
            except (TypeError, ValueError):
                continue                     # lose precision, never crash
        return cls(**fields)


@dataclass(frozen=True)
class TailLine:
    """One complete physical line, with the position identity GD-24 keys on."""

    line_no: int        # 1-based physical line number in the current generation
    byte_offset: int    # absolute offset of the line's first byte
    text: str           # decoded, without the trailing "\n"
    nbytes: int         # bytes consumed including the trailing "\n"

    # Deliberately NO `__len__`: it would make `len(line)` mean `len(line.text)`
    # and therefore make a *blank* line falsy, so the most natural ingest loop
    # anyone writes — `if line:` / `[l for l in lines if l]` — would silently
    # drop blank lines and shift the physical line numbers of everything after
    # them. That shift is the exact failure this module's line accounting exists
    # to prevent (GD-24 keys `stream_meta` by `<sessionId>#<line:08d>`), so a
    # TailLine is always truthy: it *is* a line, whatever it contains. Ask for
    # `len(line.text)` explicitly when you mean the text's length.


@dataclass
class TailResult:
    """What one tick saw. ``lines`` may be empty for many honest reasons."""

    lines: list = field(default_factory=list)
    checkpoint: Checkpoint = field(default_factory=Checkpoint)
    reason: str = REASON_UNCHANGED
    reset: bool = False        # full re-ingest happened: drop derived state for this file
    bytes_read: int = 0        # bytes actually read from disk this tick (GD-30 budget)
    deferred: int = 0          # bytes of incomplete trailing line left for next tick
    more: bool = False         # read cap hit; poll again immediately
    missing: bool = False
    compacting: bool = False
    oversize_line: bool = False  # one line > read_cap: no progress with this cap

    def __bool__(self) -> bool:
        return bool(self.lines)


def _stat(path):
    try:
        return os.stat(path)
    except OSError:
        return None


def compaction_in_progress(path, *, now=None) -> bool:
    """True when a fresh ``.compact.tmp.*`` sits in ``path``'s directory (D6).

    The CLI writes the temp file beside the transcript it is rewriting. We do
    not try to match it to a specific transcript (the suffix is opaque): a
    rewrite in that directory is reason enough to defer one tick. Stale temp
    files (>COMPACT_STALE_S old) are ignored so a crashed compaction cannot
    wedge the stream permanently.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    now = time.time() if now is None else now
    for tmp in glob.glob(os.path.join(glob.escape(directory), COMPACT_TMP_PREFIX + "*")):
        st = _stat(tmp)
        if st is None:
            continue
        if now - st.st_mtime <= COMPACT_STALE_S:
            return True
    return False


def split_lines(data: bytes, start_offset: int, start_line_no: int):
    """Split ``data`` into complete lines; return ``(lines, consumed, line_no)``.

    Torn-tail rule, verbatim from the prior art: everything after the last
    ``\\n`` is **not** consumed and stays for the next tick, so ``consumed``
    only ever covers whole lines. Because a ``\\n`` byte cannot occur inside a
    multi-byte UTF-8 sequence, cutting here never splits a character; decoding
    uses ``errors="replace"`` anyway — a corrupt byte in one line must not
    abort the ingest of the whole file (D6: unknown/undecodable content is
    retained, never crashed on).
    """
    cut = data.rfind(NEWLINE)
    if cut == -1:
        return [], 0, start_line_no
    complete = data[: cut + 1]
    lines = []
    line_no = start_line_no
    pos = 0
    end = len(complete)
    while pos < end:
        nl = complete.index(NEWLINE, pos)
        line_no += 1
        lines.append(
            TailLine(
                line_no=line_no,
                byte_offset=start_offset + pos,
                text=complete[pos:nl].decode("utf-8", "replace"),
                nbytes=nl + 1 - pos,
            )
        )
        pos = nl + 1
    return lines, end, line_no


def _read_at(path, start: int, nbytes: int):
    """``nbytes`` from ``start``, or ``None`` if the file went away mid-tick."""
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            return fh.read(nbytes)
    except OSError:
        return None


def tail_once(
    path,
    checkpoint: Checkpoint = None,
    *,
    read_cap: int = DEFAULT_READ_CAP,
    resync_on_mtime_only: bool = False,
    skip_while_compacting: bool = True,
    escalate_oversize_line: bool = False,
    max_escalated_bytes: int = 0,
    now=None,
) -> TailResult:
    """Read what was appended to ``path`` since ``checkpoint``.

    Decision order (each branch is a named ``reason`` so `/health` and the
    tests can assert *why* a tick did what it did):

    1. **unstatable** -> ``missing``, checkpoint untouched. A transcript the
       retention sweep deleted must not reset anyone's offset: the file may be
       a rename away from coming back, and a reset would re-emit its history.
    2. **fresh checkpoint** -> read from 0, ``reason="new"``, ``gen=1``.
    3. **inode/device changed** -> rotation; read from 0, ``reason="rotated"``.
    4. **size < offset** -> in-place truncation; read from 0,
       ``reason="shrunk"``. This is the branch RUNSTATE-15 found missing in
       `decision_watcher.py`; inode identity alone does not catch it.
    5. **size == offset** -> nothing appended. Returns ``unchanged`` **without
       opening the file** (stat-first, D6). If ``mtime_ns`` moved as well the
       file was rewritten in place to exactly the same length — undetectable
       without hashing, and not reachable through the two rewrite paths the CLI
       actually has (`performRemoveByUuid` truncates, so size shrinks;
       `performCompactTranscript` writes a temp file and renames, so the inode
       changes). ``resync_on_mtime_only=True`` opts a paranoid caller into a
       full re-ingest there anyway, at O(file) cost.
    6. otherwise **append** -> read ``[offset, size)``, capped at ``read_cap``.

    ``reset=True`` on branches 2-4 (and 5 under the opt-in) tells the caller to
    re-derive everything it holds for this file; the new ``checkpoint.gen`` is
    the generation GD-26's sweep retracts older documents against (SD-10).

    One line longer than ``read_cap`` is its own named outcome, never a silent
    "caught up": ``reason=REASON_OVERSIZE_LINE`` with ``oversize_line=True``, so
    a poll loop and `/health` can see that this stream cannot progress at the
    current cap (``more`` stays False — polling again would re-read the same
    prefix, the exact opposite of GD-30's O(bytes appended)).
    ``escalate_oversize_line=True`` instead promotes that one read to the whole
    remaining tail (``st.st_size - start``) so the line comes back whole; the
    one-shot readers (:func:`read_complete_lines`, `store.Store`) opt in,
    because "return every complete line" is their contract.
    ``max_escalated_bytes`` bounds that promotion (0 = unbounded): a live poll
    loop wants the line, but not at the price of an unbounded allocation
    dictated by whatever a foreign writer put on disk, so :class:`Tailer`
    escalates up to ``DEFAULT_MAX_LINE_BYTES`` and stays honestly stalled past
    it.
    """
    ck = checkpoint or Checkpoint()
    st = _stat(path)
    if st is None:
        return TailResult(checkpoint=ck, reason=REASON_MISSING, missing=True)

    if ck.fresh:
        reason, start = REASON_NEW, 0
    elif (st.st_dev, st.st_ino) != (ck.st_dev, ck.st_ino):
        reason, start = REASON_ROTATED, 0
    elif st.st_size < ck.offset:
        reason, start = REASON_SHRUNK, 0
    elif st.st_size == ck.offset:
        if resync_on_mtime_only and st.st_mtime_ns != ck.mtime_ns:
            reason, start = REASON_RESYNC, 0
        else:
            # Stat-first short circuit: no read, no decode, no allocation.
            return TailResult(
                checkpoint=replace(ck, size=st.st_size, mtime_ns=st.st_mtime_ns),
                reason=REASON_UNCHANGED,
            )
    else:
        reason, start = REASON_APPEND, ck.offset

    reset = reason in RESET_REASONS

    if skip_while_compacting and compaction_in_progress(path, now=now):
        # Deferring keeps line numbering honest: reading a file mid-rewrite
        # yields lines whose positions are about to change anyway.
        return TailResult(
            checkpoint=ck, reason=REASON_COMPACTING, compacting=True,
        )

    want = st.st_size - start
    capped = min(want, read_cap) if read_cap and read_cap > 0 else want
    data = _read_at(path, start, capped)
    if data is None:
        # Vanished/permission-changed between stat and open: same contract as
        # branch 1 — say nothing happened rather than lose the checkpoint.
        return TailResult(checkpoint=ck, reason=REASON_MISSING, missing=True)

    base_line_no = 0 if reset else ck.line_no
    lines, consumed, line_no = split_lines(data, start, base_line_no)

    # A single line longer than ``read_cap`` yields ``consumed == 0`` while the
    # file still has unread bytes past the capped read — the torn-tail rule has
    # no exception, so the offset cannot advance. That is *not* "caught up", and
    # it must not read like it: either escalate this one read to the whole
    # remaining tail, or name the condition so the caller can see the stall
    # instead of re-reading ``read_cap`` bytes every tick forever.
    # (``consumed == 0`` with ``capped == want`` is the ordinary deferred torn
    # tail: we already hold every byte there is.)
    oversize_line = consumed == 0 and capped < want
    if oversize_line and escalate_oversize_line:
        escalated = min(want, max_escalated_bytes) if max_escalated_bytes else want
        if escalated > capped:
            data = _read_at(path, start, escalated)
            if data is None:
                return TailResult(checkpoint=ck, reason=REASON_MISSING, missing=True)
            lines, consumed, line_no = split_lines(data, start, base_line_no)
        # Still no newline in `escalated` bytes: only a *bounded* escalation can
        # end up here, and it is still a stall — say so rather than report the
        # bytes as an ordinary deferred tail. Reading the whole remaining tail
        # (``escalated == want``) and finding no newline is the ordinary torn
        # tail instead: we already hold every byte there is.
        oversize_line = consumed == 0 and escalated < want
    if oversize_line:
        reason = REASON_OVERSIZE_LINE

    new_offset = start + consumed
    gen = (ck.gen + 1) if reset else max(ck.gen, 1)
    return TailResult(
        lines=lines,
        checkpoint=Checkpoint(
            st_dev=st.st_dev,
            st_ino=st.st_ino,
            size=st.st_size,
            offset=new_offset,
            line_no=line_no,
            gen=gen,
            mtime_ns=st.st_mtime_ns,
        ),
        reason=reason,
        reset=reset,
        bytes_read=len(data),
        deferred=len(data) - consumed,
        more=new_offset < st.st_size and consumed > 0,
        oversize_line=oversize_line,
    )


def read_complete_lines(path, **kwargs):
    """Every complete line of ``path`` right now, torn tail deferred.

    Convenience for one-shot readers (fixtures, boot-time scans). Loops over
    :func:`tail_once` so a file larger than ``read_cap`` still comes back whole
    — the cap bounds one *read*, not one call.

    Returning a short list silently is the one outcome a "give me every line"
    helper may not have, and there are two doors to it. Both are closed here:

    * a line longer than ``read_cap`` -> ``escalate_oversize_line=True`` by
      default, so the long line is read whole;
    * a ``.compact.tmp.*`` in the directory -> ``skip_while_compacting=False``
      by default. The deferral is a *poll loop's* policy (wait a tick, the
      rewrite finishes), not a one-shot reader's: `compaction_in_progress` is
      directory-scoped, so honouring it here would blank every transcript in
      that project directory for up to ``COMPACT_STALE_S`` and report it as an
      empty file. ``read_at``/``split_lines`` tolerate a half-written file, and
      the caller asked for "now".

    A caller that *does* want the deferral passes ``skip_while_compacting=True``
    and gets it as a raised :class:`CompactionInProgress` — visible, never an
    empty list.
    """
    kwargs.setdefault("escalate_oversize_line", True)
    kwargs.setdefault("skip_while_compacting", False)
    ck = Checkpoint()
    out = []
    while True:
        res = tail_once(path, ck, **kwargs)
        if res.compacting:
            raise CompactionInProgress(path)
        out.extend(res.lines)
        if res.missing or not res.more:
            return out
        ck = res.checkpoint


class Tailer:
    """Stateful convenience wrapper: one file, one advancing checkpoint.

    The pure function is the contract (and what the tests hammer); this class
    only remembers the checkpoint so a poll loop reads
    ``for line in tailer.poll().lines``. It holds no file handle between ticks
    — a held handle is how you keep a deleted transcript's inode alive and miss
    the rotation you exist to detect.
    """

    def __init__(self, path, checkpoint: Checkpoint = None, *,
                 escalate_after: int = 2,
                 max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
                 **options):
        self.path = os.fspath(path)
        self.checkpoint = checkpoint or Checkpoint()
        self.options = dict(options)
        #: Consecutive oversize ticks on an unchanged file before ONE bounded
        #: escalation is attempted (``0`` disables escalation entirely and keeps
        #: the pre-existing "stall and name it" behaviour).
        self.escalate_after = escalate_after
        #: Ceiling for that escalation. Past it the stream stays stalled with a
        #: named reason rather than allocating whatever a foreign writer chose.
        self.max_line_bytes = max_line_bytes
        self.resets = 0
        self.bytes_read = 0
        self.lines_read = 0
        self.last_reason = ""
        #: The full result of the last :meth:`poll` — what :meth:`drain` points
        #: at when it returns an empty list (an empty list alone cannot say
        #: whether the file is idle, missing, deferred or stalled).
        self.last_result = TailResult()
        #: How many ticks hit a line longer than ``read_cap`` (a `/health`
        #: number: it means this stream is stalled, not idle).
        self.oversize_lines = 0
        #: How many bounded escalations were attempted (also `/health`: a
        #: non-zero value means this stream carries lines over ``read_cap``).
        self.escalations = 0
        # (size, mtime_ns) observed when an oversize line stalled us, and
        # whether the one escalation allowed for that observation was spent.
        # While the file is unchanged there is provably nothing to gain from
        # reading it again, and re-reading read_cap bytes per tick would burn
        # ~32 MB/s on a 250 ms poll — the opposite of GD-30. Both are cleared as
        # soon as the file moves.
        self._stalled_at = None
        self._stall_ticks = 0
        self._escalated = False

    @property
    def stalled(self) -> bool:
        """True while an oversize line is blocking this file's progress."""
        return self._stalled_at is not None

    def poll(self) -> TailResult:
        """One tick. Bounded-escalation policy for a line over ``read_cap``:

        1. the tick that meets it returns ``reason=REASON_OVERSIZE_LINE`` and
           marks the file stalled at its observed ``(size, mtime_ns)``;
        2. after ``escalate_after`` consecutive stalled ticks on that same
           observation, **one** read of up to ``max_line_bytes`` is attempted —
           this is what lets an R-44-class >8 MB payload through the live path
           at all, since ``DEFAULT_READ_CAP`` is exactly 8 MiB;
        3. if even that does not reach a newline, the stream stays stalled with
           the named reason and is **not** read again until the file changes.

        Worst case per file is therefore one ``max_line_bytes`` read per
        distinct ``(size, mtime_ns)``, never per tick (GD-30).
        """
        if self._stalled_at is not None:
            st = _stat(self.path)
            if st is not None and (st.st_size, st.st_mtime_ns) == self._stalled_at:
                self._stall_ticks += 1
                may_escalate = (
                    self.escalate_after
                    and not self._escalated
                    and self._stall_ticks >= self.escalate_after
                    and not self.options.get("escalate_oversize_line")
                )
                if not may_escalate:
                    self.last_reason = REASON_OVERSIZE_LINE
                    self.last_result = TailResult(
                        checkpoint=self.checkpoint,
                        reason=REASON_OVERSIZE_LINE,
                        oversize_line=True,
                    )
                    return self.last_result
                self._escalated = True
                self.escalations += 1
                return self._tick(escalate_oversize_line=True,
                                  max_escalated_bytes=self.max_line_bytes)
            self._clear_stall()
        return self._tick()

    def _clear_stall(self):
        self._stalled_at = None
        self._stall_ticks = 0
        self._escalated = False

    def _tick(self, **overrides) -> TailResult:
        options = dict(self.options)
        options.update(overrides)
        res = tail_once(self.path, self.checkpoint, **options)
        self.checkpoint = res.checkpoint
        self.last_reason = res.reason
        self.last_result = res
        self.bytes_read += res.bytes_read
        self.lines_read += len(res.lines)
        if res.reset:
            self.resets += 1
        if res.oversize_line:
            self.oversize_lines += 1
            observation = (res.checkpoint.size, res.checkpoint.mtime_ns)
            if observation != self._stalled_at:
                # A different observation than the one we already spent an
                # escalation on: this stall gets its own budget.
                self._stalled_at = observation
                self._stall_ticks = 1
                self._escalated = False
        else:
            self._clear_stall()
        return res

    def drain(self, max_ticks: int = 1000):
        """Poll until the file is caught up (``more`` clear); return all lines.

        ``max_ticks`` is a guard, not a policy: a file growing faster than we
        read must yield to the poll loop instead of starving every other
        stream.

        An empty return is not self-explanatory — idle, missing, deferred by a
        compaction and stalled on an oversize line all look alike from the list
        alone. :attr:`last_reason` (and :attr:`last_result`) say which, and
        :attr:`stalled` is the one that will not clear on its own.
        """
        out = []
        for _ in range(max_ticks):
            res = self.poll()
            out.extend(res.lines)
            if res.missing or not res.more:
                break
        return out

    def rewind(self):
        """Forget the position, keep the counters: the next poll re-ingests."""
        self.checkpoint = Checkpoint(gen=self.checkpoint.gen)
        self._clear_stall()
        return self.checkpoint
