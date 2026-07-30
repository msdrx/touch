#!/usr/bin/env python3
"""The tracked memory tree: publishable content, and an index the CLI can load.

Item I9 (DOCS-4, DOCS-14, DOCS-19, LAYOUT-10). Run as
`python3 test_memory_hygiene.py`; exits non-zero on failure. No pytest, no
runner — `run_all.sh` picks it up by its `test_*.py` glob.

WHY THIS FILE EXISTS
--------------------
`.touch/memory/*.md` is the ONE tracked subtree of `.touch/` (G9), and it is
tracked content with an author nobody has ever had to review before: the model
writes it, and after I13 a browser form can too. Every other tracked file in
this repository was typed by a human who could see what they were committing.

Auto memory was also *written* on a documented promise this repository is
deliberately breaking — "files are not shared across machines or cloud
environments". The notes reflect that promise: this project's own memory index
records where an un-cleared token scratch file lives, and another note describes
reading credentials to reach an OAuth endpoint (DOCS-19). So the first tracked
commit of that subtree publishes a map of the repository's own secret-handling
state unless somebody reads it first. That reading is a deliberate, human step
(the I9 migration, main-terminal-owned); this file is the machine half that
keeps the result honest afterwards, on every run.

Two content guards, one structural assertion that keeps the first one reachable,
and one namespace check over what may sit in the tree at all:

  (a) the same token classes `test_publish_hygiene` already applies to every
      tracked file — a token-shaped line, a credentialed `mongodb://` URI, a
      token-scratch FILENAME — applied here by name, over the memory tree,
      including files that are on disk but not yet in the index. That is the
      half `test_publish_hygiene` structurally cannot do: it asks git what is
      tracked, so it sees a leak one `git add` too late.
  (b) `MEMORY.md` fits the load budget the CLI actually applies — 200 lines or
      25 KB, whichever comes first, measured the way the CLI measures it
      (frontmatter and block-level HTML comments stripped, v2.1.211+). Over the
      limit, the write still succeeds and everything past the cap is dropped at
      the next load: the file looks complete in the editor and is truncated in
      the model. A cap nobody checks is a cap nobody has.
  (c) `test_publish_hygiene.CONTENT_SCAN_EXCLUDED` does not name the memory
      tree. Excluding it is the tempting fix the first time a memory note trips
      the token scan (LAYOUT-10), and it removes the only guard over the
      least-reviewed content in the repository. Asserted from here, in the file
      whose subject it is, rather than trusted to a comment over there.
  (d) every TRACKED path under the tree is a flat `.md` name — the namespace the
      write path enforces (G7 step 1) and the `.gitignore` carve re-includes
      (G9). A `.history/` entry, a `draft.md.bak` or a `foo.token` in the index
      means the carve was loosened into something that publishes the write
      path's own bookkeeping, which is the LAYOUT-10 / SECURITY-11 leak the
      five-line carve was chosen over a shorter one to prevent.

Every check SKIPS, printing why, when the tree is absent or empty — which it is
until the memory files are migrated. A skip is the honest answer for "there is
nothing tracked to be clean", and it is what lets this file land before the
content does.

The detectors are IMPORTED from `test_publish_hygiene`, never re-spelled: two
copies of an entropy heuristic drift, and the one that guards the
model-written files is the one that would be quietly weaker. Their own
self-tests live over there and run in the same suite.
"""
import re
import string
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True   # no .pyc droppings in tests/ or the payload
# The canonical trees are named through `tests/_roots.py`, never by a literal
# under REPO — GD-U1 moves them and that file is the single flip point.
from _roots import PAYLOAD, REPO         # noqa: E402  (path juggling first)
sys.path.insert(0, str(PAYLOAD))
from aggregator import paths             # noqa: E402  MEMORY_REL, one owner
import test_publish_hygiene as hygiene   # noqa: E402  the detectors, not a copy

#: `.touch/memory`, as the shipped resolver spells it (`paths.MEMORY_REL`).
#: Never a literal here: the tracked subtree, the `.gitignore` carve, the write
#: path and this scan all have to name the SAME directory, and a second spelling
#: is how a guard ends up watching a directory nothing writes to.
MEMORY_REL = Path(paths.MEMORY_REL)
MEMORY_DIR = REPO / MEMORY_REL

#: The index the CLI loads at the start of every conversation, and its budget.
#: 200 lines OR 25 KB, whichever comes first (documented).
#:
#: **G5 is the one owner of these two numbers** — the plan states them as
#: `limits.indexLines` / `limits.indexBytes`, the memory API reports them to the
#: editor so a person can see the cap on screen, and these constants are that
#: decision's assertion over the repository's own index, not a second decision.
#: `test_the_index_budget_has_one_owner` below cross-checks them against the
#: served numbers as soon as the API exists, so the third spelling cannot drift
#: from this one in silence.
INDEX_NAME = "MEMORY.md"
INDEX_LINES = 200
INDEX_BYTES = 25600

#: Where the memory API will report those limits from (G5, sp-monitor-server).
#: Read as TEXT, never imported: importing that module runs its startup
#: resolution, which exits the process when no task state dir resolves — a test
#: that exits on somebody elses resolver is worse than no test.
#:
#: Two patterns, because the likely shape in this codebase is a NAME
#: (`"indexLines": INDEX_LINES`), not an integer literal: matching only the
#: literal would leave the three-spellings-of-one-cap risk open silently and
#: green, which is the one outcome a cross-check may not have. `MEMORY_API` is
#: the presence test that decides between "nothing to compare yet" (a note) and
#: "the route exists and does not report the cap" (a failure) — the same
#: self-retiring shape `test_bin_wrappers.mid_ladder_migration` uses.
MONITOR_SERVER = PAYLOAD / "shared" / "monitoring" / "monitor_server.py"
SERVED_LIMIT = r"""["']index%s["']\s*:\s*([A-Za-z_0-9]+)"""
MODULE_INT = r"(?m)^[ \t]*%s\s*(?::[^=\n]+)?=\s*(\d+)\s*(?:#.*)?$"
MEMORY_API = "/api/memory/"

#: What may sit directly in the memory tree, as a tracked file: a flat `.md`
#: name. Byte-for-byte the namespace the write path enforces (G7) and the one
#: the `.gitignore` carve re-includes (G9) — `.history/`, `.trash/`, a stray
#: `foo.token` and a `draft.md.bak` are all ignored by git on purpose, so any of
#: them appearing in the INDEX means the carve was edited into something looser.
FLAT_MD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.md$")

#: A memory-only content class, and deliberately ONE literal: this repository
#: names its own token-scratch files `mytok*`, and the memory index has recorded
#: which git ref still carries one (DOCS-19). A note that says where an
#: un-cleared token lives is a leak even though the token itself is not in the
#: file, and no shape-based detector can see it.
#:
#: It is not widened to `token|credential|secret`: Touch's per-boot token posture
#: is one of the things the model most needs to remember, so those words are in
#: honest notes constantly, and a guard that cries wolf on every one of them gets
#: switched off. The judgement call about prose stays human (the I9 migration
#: reads all of it); this catches the one spelling that is never anything else.
SCRATCH_PROSE = re.compile(r"mytok", re.I)

#: Leading YAML frontmatter, and block-level HTML comments: what the CLI strips
#: BEFORE measuring the index (v2.1.211+). Both are stripped here too, so the
#: repository test and the editor budget cannot disagree about the same file.
#: A comment only counts as block-level when it owns its lines — an inline
#: `<!-- x -->` in the middle of a sentence is content the model sees.
#:
#: The comment body is `(?:(?!-->).)*?` and not `.*?`: with `re.S` a lazy `.*?`
#: still crosses `-->` whenever the shorter match fails, so a line that merely
#: STARTS with a comment and continues into prose would be consumed together
#: with every line up to the NEXT block comment. That under-counts, which is the
#: dangerous direction — an over-cap index would pass this gate while the CLI
#: counts the prose and drops the tail at load, silently.
FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)",
                         re.S)
BLOCK_COMMENT = re.compile(r"^[ \t]*<!--(?:(?!-->).)*-->[ \t]*(?:\r?\n|\Z)",
                           re.S | re.M)

failures = []
skips = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def skip(msg):
    print(f"  skip: {msg}")
    skips.append(msg)


def index_budget(text):
    """(lines, bytes) the CLI would count for a memory index.

    Frontmatter and block-level HTML comments come off first, because the CLI
    strips them before it measures — a file that fits only when its comments are
    counted would be reported over the cap here and loaded fine there, and the
    reverse mistake is the dangerous one: content silently dropped from the
    model while the editor shows it whole.
    """
    stripped = FRONTMATTER.sub("", text)
    stripped = BLOCK_COMMENT.sub("", stripped)
    return len(stripped.splitlines()), len(stripped.encode("utf-8"))


def tracked_memory():
    """Repo-relative tracked paths under the memory tree, or None without git."""
    if not hygiene.have_git():
        return None
    prefix = MEMORY_REL.as_posix() + "/"
    return [p for p in hygiene.tracked_paths() if p.startswith(prefix)]


def on_disk_memory():
    """Files sitting directly in the memory tree — tracked or not, flat only.

    Non-recursive on purpose: `.history/` and `.trash/` are the write paths own
    bookkeeping (backups and soft deletes), they are ignored by git by design,
    and scanning them would report the same leak once per revision of it.
    """
    if not MEMORY_DIR.is_dir():
        return []
    return sorted(p for p in MEMORY_DIR.iterdir() if p.is_file())


def read_text(path):
    """Decoded text, or None for binary/unreadable — the same blind spot (b) has."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def scan(targets):
    """(named, blobs, uris, scratch, unreadable) over {relative path: Path}.

    Separated from the test that calls it so the synthetic self-test below can
    drive the same code over a tree it built: a leak detector nobody has watched
    fire is a detector nobody has tested. Hits are LOCATIONS — printing the line
    to prove it leaked would leak it into the test log.
    """
    named, blobs, uris, scratch, unreadable = [], [], [], [], 0
    for rel in sorted(targets):
        if hygiene.TOKEN_SCRATCH.search(rel):
            named.append(rel)
        text = read_text(Path(targets[rel]))
        if text is None:
            unreadable += 1
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if hygiene.token_shaped(line):
                blobs.append(f"{rel}:{n}")
            m = hygiene.MONGO_URI.search(line)
            if m and not hygiene.is_placeholder(m.group(2)):
                uris.append(f"{rel}:{n}")
            if SCRATCH_PROSE.search(line):
                scratch.append(f"{rel}:{n}")
    return named, blobs, uris, scratch, unreadable


# --- (c) the structural one FIRST: it is what keeps (a) reachable at all.
def test_memory_is_not_excluded_from_the_content_scans():
    print("test_memory_is_not_excluded_from_the_content_scans")
    excluded = tuple(hygiene.CONTENT_SCAN_EXCLUDED)
    named = [x for x in excluded
             if "memory" in x or ".touch" in x or MEMORY_REL.as_posix() in x]
    check(not named,
          f"test_publish_hygiene does not exclude the memory tree from its "
          f"content scans (found: {named}) — model-written prose is the LEAST "
          f"reviewed tracked content in this repo, so it is the last thing that "
          f"may be exempted (LAYOUT-10)")
    print(f"  note: content-scan exclusions are {excluded}")


# --- (a) the token classes, over tracked AND not-yet-tracked memory files
def test_memory_content_is_publishable():
    print("test_memory_content_is_publishable")
    tracked = tracked_memory()
    if tracked is None:
        skip("not a git checkout — the tracked half of this scan asks the index, "
             "which does not exist in an archive/tarball checkout")
        tracked = []
    disk = on_disk_memory()
    if not tracked and not disk:
        skip(f"{MEMORY_REL.as_posix()}/ holds no file (nothing has been migrated "
             f"into it yet) — nothing to scan")
        return

    # One list, deduplicated by repo-relative path: a file that is both tracked
    # and on disk must not be reported twice, and a file that is only on disk is
    # exactly the one worth reporting BEFORE it is committed.
    targets = {}
    for rel in tracked:
        targets[rel] = REPO / rel
    for path in disk:
        targets[path.relative_to(REPO).as_posix()] = path

    named, blobs, uris, scratch, binary = scan(targets)
    check(not named,
          f"no memory file is named like a token scratch file (found: {named}) — "
          f"such a name is refused by the write path and would fail the release "
          f"gate after it was committed")
    check(not blobs,
          f"no memory file carries a token-shaped line (at: {blobs})")
    check(not uris,
          f"no memory file carries a credentialed Mongo URI (at: {uris})")
    check(not scratch,
          f"no memory file records where an un-cleared token scratch lives "
          f"(at: {scratch}) — redact the note; the memory tree is published "
          f"(DOCS-19)")
    print(f"  note: scanned {len(targets) - binary} memory file(s) "
          f"({len(tracked)} tracked), skipped {binary} unreadable")


def test_memory_tree_holds_only_flat_md_files():
    print("test_memory_tree_holds_only_flat_md_files")
    tracked = tracked_memory()
    if tracked is None:
        skip("not a git checkout — the tracked namespace is an index question")
        return
    if not tracked:
        skip(f"nothing tracked under {MEMORY_REL.as_posix()}/ yet")
        return
    prefix = MEMORY_REL.as_posix() + "/"
    bad = [p for p in tracked
           if "/" in p[len(prefix):] or not FLAT_MD.match(p[len(prefix):])]
    check(not bad,
          f"every tracked memory path is a flat .md name (wrong: {bad}) — the "
          f"`.gitignore` carve re-includes `*.md` and nothing else, so anything "
          f"else here means the carve was loosened (G9, LAYOUT-10)")


# --- (b) the index budget, measured the way the CLI measures it
def test_index_fits_the_load_budget():
    print("test_index_fits_the_load_budget")
    index = MEMORY_DIR / INDEX_NAME
    if not index.is_file():
        skip(f"{(MEMORY_REL / INDEX_NAME).as_posix()} does not exist yet")
        return
    text = read_text(index)
    if text is None:
        check(False, f"{INDEX_NAME} decodes as UTF-8")
        return
    lines, size = index_budget(text)
    check(lines <= INDEX_LINES,
          f"{INDEX_NAME} is within the {INDEX_LINES}-line load limit "
          f"({lines} lines) — everything past it is dropped at the next load, "
          f"silently")
    check(size <= INDEX_BYTES,
          f"{INDEX_NAME} is within the {INDEX_BYTES}-byte load limit "
          f"({size} bytes)")
    print(f"  note: {INDEX_NAME} measures {lines}/{INDEX_LINES} lines, "
          f"{size}/{INDEX_BYTES} bytes with frontmatter and block comments "
          f"stripped")


# --- the measurement itself, before it is trusted over the index
def test_measurement():
    print("test_measurement")
    plain = "one\ntwo\nthree\n"
    check(index_budget(plain) == (3, len(plain.encode("utf-8"))),
          "a plain file measures its own lines and bytes")
    fm = "---\ntitle: x\nmodified: 2026-07-30T00:00:00Z\n---\none\ntwo\n"
    check(index_budget(fm) == (2, len("one\ntwo\n")),
          f"leading frontmatter is stripped before counting "
          f"(got {index_budget(fm)})")
    dots = "---\ntitle: x\n...\none\n"
    check(index_budget(dots) == (1, len("one\n")),
          f"a `...` frontmatter terminator counts too (got {index_budget(dots)})")
    block = "one\n<!-- a note\nspanning lines -->\ntwo\n"
    check(index_budget(block) == (2, len("one\ntwo\n")),
          f"a block-level HTML comment is stripped (got {index_budget(block)})")
    inline = "one <!-- x --> two\n"
    check(index_budget(inline) == (1, len(inline.encode("utf-8"))),
          f"an INLINE comment is content and is counted (got {index_budget(inline)})")
    # The dangerous direction, and the one a lazy `.*?` under `re.S` gets wrong:
    # a line that STARTS with a comment and continues into prose is content, and
    # a body that may cross `-->` swallows it plus everything up to the next
    # block comment. Under-counting means an over-cap index passes this gate and
    # is truncated at load — exactly what the docstring above rules out. Two
    # comments, because one cannot expose a crossing match.
    mixed = "<!-- a --> real content\nmore\n<!-- b -->\ntail\n"
    check(index_budget(mixed)
          == (3, len("<!-- a --> real content\nmore\ntail\n")),
          f"an inline comment before prose does not swallow the lines after it "
          f"(got {index_budget(mixed)})")
    # Not frontmatter: a horizontal rule mid-file, and a file that merely
    # STARTS with a rule. Stripping either would under-count a real index.
    rule = "text\n---\nmore\n"
    check(index_budget(rule) == (3, len(rule.encode("utf-8"))),
          f"a mid-file `---` rule is not frontmatter (got {index_budget(rule)})")
    over = "x\n" * (INDEX_LINES + 1)
    check(index_budget(over)[0] > INDEX_LINES,
          "the line detector can fail (an over-long file measures over)")


def resolve_int(src, token):
    """`token` as an int — a literal, or the value of an assignment to that name.

    `"indexLines": 200` and `"indexLines": INDEX_LINES` are the same decision
    written two ways, and the second is the likelier shape in this codebase — a
    cross-check that gave up on it would leave the risk it exists for open,
    silently and green (which is worse than absent, because the green line reads
    as evidence). Returns (value, why-not) so the caller can say which it was.

    Whitespace is allowed before the name so a cap defined inside the route
    function counts too; if two assignments to one name disagree, that is
    reported rather than resolved — picking one would be a guess about which the
    route uses, and this file has no business guessing that.
    """
    if token.isdigit():
        return int(token), None
    found = {int(m) for m in re.findall(MODULE_INT % re.escape(token), src)}
    if not found:
        return None, f"{token} is a name this file cannot resolve to an integer"
    if len(found) > 1:
        return None, (f"{token} is assigned {sorted(found)} in more than one "
                      f"place, so which one the route reports is not knowable "
                      f"from the source")
    return found.pop(), None


def test_limit_resolution():
    print("test_limit_resolution")
    # `resolve_int` decides whether the cross-check below can run at all, so a
    # bug in it degrades that test to a green note — the failure mode the
    # cross-check exists to prevent, one level up. Driven over synthetic source.
    cases = (("200", "a bare literal", 200),
             ("INDEX_LINES", "INDEX_LINES = 200\n", 200),
             ("INDEX_LINES", "INDEX_LINES: int = 200\n", 200),
             ("INDEX_LINES", "    INDEX_LINES = 200  # in a function\n", 200),
             ("INDEX_LINES", "INDEX_LINES = 200\nINDEX_LINES = 200\n", 200))
    for token, src, want in cases:
        value, why = resolve_int(src, token)
        check(value == want and why is None,
              f"{token!r} in {src!r} resolves to {want} (got {value!r}, {why!r})")
    for token, src, what in (("NOPE", "OTHER = 200\n", "an unresolvable name"),
                             ("INDEX_LINES", "INDEX_LINES = 200\nINDEX_LINES = 25\n",
                              "two disagreeing assignments"),
                             ("INDEX_LINES", "SOME_INDEX_LINES = 7\n",
                              "a name that is only a SUFFIX of another")):
        value, why = resolve_int(src, token)
        check(value is None and why,
              f"{what} is reported rather than guessed (got {value!r}, {why!r})")


def test_the_index_budget_has_one_owner():
    print("test_the_index_budget_has_one_owner")
    # The two numbers above are spelled here, in the plan (G5) and — once
    # sp-monitor-server lands — in the route that reports `limits` to the editor.
    # Three spellings of one cap is how an editor comes to disclose a limit the
    # gate does not enforce, so the moment the third one exists it is compared
    # with this one. Until then this is a NOTE, not a skip: there is nothing
    # missing to report, only nothing yet to compare against.
    #
    # The note is only allowed while the route ITSELF is absent. Once
    # `/api/memory/` is in that file, a missing `indexLines` is a real gap — G5
    # requires the editor to disclose the cap on screen — and a cross-check that
    # kept printing a reassuring note over it would be the third spelling drifting
    # in exactly the silence it was written to break.
    try:
        src = MONITOR_SERVER.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"  note: {MONITOR_SERVER} not readable ({exc}) — the served "
              f"limits could not be cross-checked")
        return
    api = MEMORY_API in src
    for label, want in (("Lines", INDEX_LINES), ("Bytes", INDEX_BYTES)):
        found = re.search(SERVED_LIMIT % label, src)
        if found is None:
            if api:
                check(False,
                      f"the memory API exists in {MONITOR_SERVER.name} but "
                      f"reports no index{label} — G5 puts it in "
                      f"`limits` on /api/memory/list so the editor can disclose "
                      f"the cap this file enforces ({want})")
            else:
                print(f"  note: the memory API does not exist yet — G5 owns "
                      f"index{label}, this file asserts {want} over the "
                      f"repository index")
            continue
        served = found.group(1)
        value, why = resolve_int(src, served)
        if value is None:
            check(False,
                  f"the memory API reports index{label} as {served!r} and this "
                  f"file can compare it with its own {want}: {why} — the two "
                  f"numbers have one owner (G5), so they must be comparable")
            continue
        shown = served if served == str(value) else f"{served} = {value}"
        check(value == want,
              f"the memory API reports index{label} as {shown}, the same cap "
              f"this file measures the index against ({want}) — the editor must "
              f"not disclose a limit the release gate does not enforce (G5)")


def test_scan_can_fail():
    print("test_scan_can_fail")
    # The memory tree is empty today, so every arm of the real scan SKIPS — and
    # a scan that has only ever been observed skipping is a scan nobody has
    # tested. Same code, a tree built here, one file per class. Both samples are
    # ASSEMBLED at runtime for the reason `test_publish_hygiene` assembles its
    # own: a literal token or a literal credentialed URI in this file would be
    # found by that suites scan of every tracked file, which includes this one.
    blob = (string.ascii_letters + string.digits)[:43]
    uri = "mongodb://" + "touch" + ":" + "hunter2x" + "@" + "127.0.0.1:27017/db"
    note = "origin/main tip still holds " + "myto" + "k2 — clear before publish"
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "blob.md").write_text(blob + "\n", encoding="utf-8")
        (tmp / "uri.md").write_text(uri + "\n", encoding="utf-8")
        (tmp / "note.md").write_text(note + "\n", encoding="utf-8")
        (tmp / "clean.md").write_text("# notes\n\nnothing here.\n", encoding="utf-8")
        (tmp / "foo.token").write_text("x\n", encoding="utf-8")
        targets = {f"{MEMORY_REL.as_posix()}/{p.name}": p
                   for p in sorted(tmp.iterdir())}
        named, blobs, uris, scratch, unreadable = scan(targets)
    check([p.rsplit("/", 1)[-1] for p in named] == ["foo.token"],
          f"the filename class fires on a token-scratch name only (got {named})")
    check(len(blobs) == 1 and blobs[0].endswith("blob.md:1"),
          f"the token-shape class fires on a 43-char blob only (got {blobs})")
    check(len(uris) == 1 and uris[0].endswith("uri.md:1"),
          f"the Mongo class fires on a credentialed URI only (got {uris})")
    check(len(scratch) == 1 and scratch[0].endswith("note.md:1"),
          f"the scratch-prose class fires on the un-cleared-token note only "
          f"(got {scratch})")
    check(unreadable == 0, f"every sample was readable (got {unreadable})")


def main():
    for t in (test_memory_is_not_excluded_from_the_content_scans,
              test_measurement,
              test_limit_resolution,
              test_the_index_budget_has_one_owner,
              test_scan_can_fail,
              test_memory_content_is_publishable,
              test_memory_tree_holds_only_flat_md_files,
              test_index_fits_the_load_budget):
        t()
    print()
    if skips:
        print(f"skipped: {len(skips)} check(s)")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all memory-hygiene tests passed")


if __name__ == "__main__":
    main()
