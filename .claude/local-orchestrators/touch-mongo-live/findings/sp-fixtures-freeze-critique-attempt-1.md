# Adversarial review — sp-fixtures-freeze, attempt 1

**Verdict: APPROVED** — 0 blockers, 0 majors, 5 minors, 4 nits.

Scope reviewed: `tests/test_fixtures.py` + all 71 files under `tests/fixtures/`
(new files in an untracked tree ⇒ full content review), against
`touch-mongo-live-subplans.md` §sp-02, R-41/R-58 in `touch-mongo-live-plan.md`,
R-03/GD-18 in `touch-full-recon-plan.md`.

I tried to reject this on three fronts and failed on all three:

1. **The freeze is real, not asserted.** I re-verified it independently of the
   test under review: `sha256sum -c MANIFEST.sha256 --quiet` → clean, 70 entries,
   72 files on disk (the 2 unmanifested are `MANIFEST.sha256` + `PROVENANCE.md`,
   correctly excluded and documented). Nothing under `tests/fixtures/` is
   gitignored (`.gitignore` has `*.bson`, `__pycache__/`, `.env.*`, `*.pid` —
   none match any fixture path), and `tests/` is still untracked, so
   `test_fixtures_are_trackable` is currently a live check, not a vacuous one.
2. **The bytes are verbatim.** I `cmp`-ed every fixture against its live source
   rather than trusting PROVENANCE: all 23 files of `run-wf_829e6f58/` identical
   to `~/.claude/projects/-home-laniakea-Projects-touch/…`; all 4 legacy streams
   identical (`head -320` for the still-live `touch-mongo-live`); `r58-replay/`,
   `wf_455b348c-e17/`, all 9 discovery files, the registry entry, and both
   record specimens identical. `oversize-line.jsonl` really is line 17 of
   `…/wf_455b348c-e17/agent-a2c3883fe5a0bb9c2.jsonl` (877 395 B, and that file's
   next-largest line is 20 908 B, so "largest in the corpus" holds). All 33 rows
   of `file-history-snapshot-dotted.index.json` resolve to byte-identical source
   lines. The only two files that differ from their sources
   (`live-run-shape/…/journal.jsonl` 26 523/32 902 and
   `…/agent-a6a927f2ce55ad975.jsonl` 335 032/538 545) are exact byte prefixes of
   still-appending files — the intended in-flight specimen, and both end on a
   line boundary.
3. **GD-27 is clean.** I ran a wider credential scan than the test does
   (`mongodb://` with userinfo, `Bearer <tok>`, `MONGO_INITDB_ROOT_PASSWORD=`,
   `-----BEGIN … PRIVATE KEY-----`, `(password|secret|api_key|token)=<value>`).
   Every hit is documentation or placeholder text inside a research agent's
   transcript: `apiKey: 'your-api-key'`, `passwd: '/etc/passwd'`,
   `MONGO_INITDB_ROOT_PASSWORD=…` (the plan's own elided recipe), and the
   "PRIVATE KEY" hits are the *regex source* `-----BEGIN [A-Z ]*PRIVATE
   KEY-----'` a researcher wrote. No real secret is frozen.

Checklist items GD-21/22/24/25/26/29/30 are not exercised by this sub-plan (no
Python module, no Mongo code, no poll loop, no client): `test_fixtures.py`
imports `hashlib, json, re, subprocess, sys, datetime, pathlib` only — stdlib,
so the no-mongod arm is vacuous here and correctly absent rather than faked.
GD-15 ownership is clean: only `tests/test_fixtures.py` + `tests/fixtures/**`
were created; the `.gitignore` worktree modification is sp-repo-bootstrap's
`*.bson` comment (unrelated hunk), no commit was made, nothing reverted.

---

## Minor

### M1 — Two frozen files are undocumented and unasserted; one is an orphan
`tests/fixtures/PROVENANCE.md:131-154` (live-run-shape + r58-replay sections)

`MANIFEST.sha256:36` and `:39` freeze
`mirror/live-run-shape/a8d43bb1…/tool-results/bkw8r4iwk.txt` and
`mirror/r58-replay/292fc08c…/tool-results/bgvnv500l.txt`, but neither appears in
any PROVENANCE group table and no shape assertion mentions them. PROVENANCE
describes live-run-shape as "`journal.jsonl` … plus all 9 agent transcripts and
their metas" (20 files are actually frozen there, not 19) and r58-replay as
"two journals … with both terminal snapshots" (5 files). That is exactly the
"provenance by guessing" GD-28 forbids, in the one file whose job is provenance.

Worse for the second one: `bkw8r4iwk.txt` *is* pointed at by frozen records
(`agent-a7e63f522c03f3e47.jsonl` ×1, `agent-a6a927f2ce55ad975.jsonl` ×3), so it
has an obvious reason to exist — but **no frozen record anywhere references
`bgvnv500l.txt`** (r58-replay freezes only journals, which carry no
persisted-output pointers). It is a dangling 138+-line spill body whose pointer
record lives in the unfrozen `292fc08c….jsonl`.

Fix: add a row for each to the matching PROVENANCE table — source path, and why
it is frozen (`bkw8r4iwk.txt`: "so the 4 pointer records in the live-run
transcripts resolve"; `bgvnv500l.txt`: state its real purpose, e.g. the
SESSIONJSONL-14 unlinked/`linkedToolUseId:null` directory-scan specimen, and the
source line that points at it) — or drop `bgvnv500l.txt` and regenerate the
manifest. Both are cheap and neither touches frozen bytes.

### M2 — The two Task-tool agent transcripts are left on the retention clock
`tests/fixtures/PROVENANCE.md:59` ("deliberately not copied; only the shape is
needed")

R-03's file list says "9 agent transcripts"; the implementation freezes 8 and
resolves the gap as prose drift. That resolution is defensible for the *count*
(the run genuinely has 8 files / 7 ids / 9 `workflowProgress` rows — I confirmed
the source dirs hold nothing more), but the most natural reading of "9" is
7 workflow ids + the 2 Task-tool spawns whose **metas were copied**:
`dd469822…/subagents/agent-a483cae616edffe81.jsonl` (421 109 B, 70 `usage` rows)
and `agent-a4e343a0f7d73268c.jsonl` (937 652 B, 78 `usage` rows). Those are the
project-local specimens of the GD-8 **Agent-tool profile**, whose transcript
location and token recoverability R-04 still has to confirm empirically
(`…/touch-full-recon/report/probes.md` does not exist yet) — and they sit on the
same retention-sweep clock this whole item exists to beat. 1.3 MB now versus
unrecoverable later.

This is only a minor because the shape is not in fact lost: I found that
`mirror/discovery/…/-tmp-claude-1000-liveio/08ffb13f…/subagents/agent-a342353f7b157760b.jsonl`
(8 records, 4 `usage` rows) with its `{agentType:general-purpose, description,
toolUseId, spawnDepth}` meta is already frozen — but nothing says so, so a
downstream sub-plan hunting for a Task-tool transcript will not find it.

Fix: either copy the two transcripts (preferred — do it while they exist), or
record their sizes + sha256 in PROVENANCE as a deliberate non-freeze **and** name
`discovery/…/agent-a342353f7b157760b.jsonl` as the standing Task-tool-transcript
specimen so R-04/GD-8 work has a pointer.

### M3 — R-03's "`touch-monitor-spawn/` noted as the plan-only-folder fixture" is not noted
`tests/fixtures/PROVENANCE.md` (no mention anywhere)

R-03's Files bullet ends with that clause, and the folder is real and matches
(`.claude/local-orchestrators/touch-monitor-spawn/` contains exactly one file,
`plan/touch-monitor-spawn-plan.md`, and **no** `events.jsonl`). It is the only
specimen of a task folder with a plan and no stream — the shape that breaks any
"every task dir has events.jsonl" assumption in discovery/sidebar code. It is
not frozen (fine — it is a tracked repo file) but it is also not *noted*, so the
item is not fully discharged.

Fix: one line in PROVENANCE — "plan-only folder fixture (R-03):
`.claude/local-orchestrators/touch-monitor-spawn/` — plan file, no
`events.jsonl`; not copied because it is tracked repo source."

### M4 — "Mixed ts formats", one of R-03's four named legacy shapes, is asserted nowhere
`tests/test_fixtures.py:269-272`

The file's own contract is "each fixture group also gets a shape assertion naming
the finding it exists to serve" (`:6-10`), and R-03 names four shapes for
`legacy/`: two-wave respawn ✅, `plan|failed "loop exited -> synthesis"` ✅,
duplicate terminals ✅, **mixed ts formats ❌**. The anchors encode it only in
free-text `what` strings (`anchors.json:12,20`
`ts-format-offset-suffix-written-by-status.sh` / `…-Z-suffix-written-by-watcher`)
which the test never reads — it asserts only `plan/stage/state/detail`. The mix
is genuinely there (aggregator 555 `+00:00` vs 35 `Z`; repo-recon 78/25;
full-recon 238/38; mongo-live 277/43) and it is the reason R-39/`w` and the
watcher-vs-`status.sh` writer split matter.

Fix: in `test_legacy_anchors`, add per stream
`check(any(r["ts"].endswith("Z") for r in recs) and any(r["ts"].endswith("+00:00") for r in recs), …)`,
and for the aggregator anchor pair assert the formats explicitly
(`recs[9]["ts"].endswith("+00:00")`, `recs[10]["ts"].endswith("Z")`).

### M5 — `index.json` provenance has an undocumented root
`tests/fixtures/mirror/records/file-history-snapshot-dotted.index.json` (all 33
rows) + `PROVENANCE.md:161`

Rows are `{"source": "292fc08c-923d-4ab4-8ff2-a9572417dbc8.jsonl", "line": 180,
"bytes": 782}` — bare filenames with no root, and the files they name are **not**
in the corpus. I had to guess the base
(`~/.claude/projects/-home-laniakea-Projects-touch/`) to verify them; all 33
resolve there byte-identically, so the data is right, but a future reader cannot
tell whether `source` is a fixture path or a harness path. `test_record_specimens`
only asserts the keys exist, never that they resolve.

Fix: state the root in PROVENANCE's `mirror/records/` row (the index is frozen,
so do not add a key to it).

---

## Nits

### N1 — ts-inversion check compares strings while `_ms()` sits three lines above
`tests/test_fixtures.py:269-272`

`recs[i]["ts"].replace("Z","+00:00")` then `cur < prev` is lexicographic. It is
correct here **only** because every ts in all four streams has offset `+00:00`
and the same length after substitution (I checked; there are no non-UTC offsets
and no missing-millisecond timestamps). It is also inconsistent with `:234-237`
and `:412-416`, which use `_ms()`. Fix: `check(_ms(recs[i-1]["ts"]) < _ms(recs[i-2]["ts"]), …)`.

### N2 — `test_fixtures_are_trackable` goes vacuous the moment the corpus is committed
`tests/test_fixtures.py:116-117`

`git check-ignore` consults the index by default, so tracked paths are reported
as not-ignored regardless of the rules — and R-02's later commit will track
these. The intent ("survives a clone") still holds, but the ignore-rule
regression guard silently stops testing anything. Fix: add `--no-index` to the
argv.

### N3 — Three messages that do not match their assertions
- `:237` prints "the seam across the /clear is under a minute" while asserting
  `0 < seam < 5` (measured 0.4 min). Say `< 5 min` or tighten to `< 1`.
- `:175` "over 7 distinct agentIds — one id appears twice" reads as a typo for
  "only 7 distinct agentIds".
- `:359` asserts `len(repeats) == 3` but the message claims each pair "occurs
  twice"; add `and set(repeats) == {2}`.

### N4 — Stray `tests/__pycache__/test_fixtures.cpython-313.pyc`
Gitignored, so harmless, but it means the module was imported rather than run —
worth deleting so the tree matches the "each file is a standalone executable"
convention.

---

## Non-defect observations (no action required from this sub-plan)

- The corpus commits ~8.0 MiB of verbatim agent prompts, tool output and full
  file bodies into the repo — the same class of content `.gitignore`'s `.touch/`
  rule exists to keep out. This is explicitly mandated (R-03/R-41 "verbatim
  bytes, sanitize only if credentials found") and no credential is present, so it
  is not a finding; but PROVENANCE could usefully say it out loud in one line, so
  nobody treats this repo as publishable-by-default.
- `run-wf_829e6f58/` has 4 `tool-results/*.txt` where R-03 says 3: superset of
  the requirement, and all 4 exist in the source. Correctly documented.
- `mirror/live-run-shape/` freezes transcripts from the session that is running
  this very orchestration, including the reviewer's own sibling agents. Declared
  in PROVENANCE:141-143 and intentional.
- The anchors/shape tests are self-consistency checks over frozen bytes (both
  sides are manifested), so they can only fail if someone edits one side. That is
  inherent to a freeze, not a tautology defect: the assertions state semantic
  claims (which line is fabricated, which is genuine, which is corrective) that
  the bytes independently satisfy, and I re-derived them from the sources.
