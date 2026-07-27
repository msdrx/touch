#!/usr/bin/env python3
"""R-06 / GD-17: the findings-disposition register is complete and honest.

Run as `python3 tests/test_register.py`; exits non-zero on the first failure.
No pytest, no runner.

What this guards, and why each half matters:

* **completeness** — every finding id under
  `.claude/local-orchestrators/*/findings/research-*.md` appears in the
  register **exactly once**. A finding that reaches no disposition is a
  decision nobody made.
* **no phantoms** — every register row names a finding that actually exists.
  Without this half the register could pass by listing invented ids.
* **non-empty dispositions** — a row with a blank disposition is worse than a
  missing row: it looks handled.

Matching is on the **(task, id) pair**, never the bare id, because `SKILLS-n`
exists in two corpora (`touch-repo-recon`, 17 findings; `touch-full-recon`, 16)
as two different finding sets. A bare-id register would silently collapse them.

Findings files are read with `errors="replace"`: at least one report contains
raw bytes that are not valid UTF-8 (it quotes transcript payloads), and a
decoding crash here would be an unrelated failure.
"""
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ORCH = REPO / ".claude/local-orchestrators"
REGISTER = ORCH / "touch-full-recon/plan/findings-register.md"

# A finding is an id-shaped MARKDOWN HEADING inside a research report — not any
# id-shaped string in prose, which would sweep up cross-references to other
# reports' findings and every GD-/R- token.
HEADING_RE = re.compile(r"^#{1,6}\s+\*{0,2}([A-Z][A-Z0-9]{2,}-\d+)\b")
# Register structure: `## <task>` … `| `ID` | finding | disposition |`
TASK_RE = re.compile(r"^##\s+(touch-[a-z-]+)\s*$")
ROW_RE = re.compile(r"^\|\s*`([A-Z][A-Z0-9]{2,}-\d+)`\s*(?<!\\)\|")
# Cells may legitimately contain a pipe (the `slots` _id grammar does); the
# generator escapes those as `\|`, so rows split on UNESCAPED pipes only.
SPLIT_RE = re.compile(r"(?<!\\)\|")

failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def read(path):
    return path.read_text(encoding="utf-8", errors="replace")


def findings_on_disk():
    """-> {(task, id): source-file-name}"""
    found = {}
    for path in sorted(ORCH.glob("*/findings/research-*.md")):
        task = path.parents[1].name
        for line in read(path).splitlines():
            m = HEADING_RE.match(line)
            if m:
                found[(task, m.group(1))] = path.name
    return found


def register_rows():
    """-> [(task, id, finding-cell, disposition-cell)] in file order."""
    rows, task = [], None
    for line in read(REGISTER).splitlines():
        t = TASK_RE.match(line)
        if t:
            task = t.group(1)
            continue
        if ROW_RE.match(line):
            cells = [c.strip() for c in SPLIT_RE.split(line.strip())]
            # split() on "| a | b | c |" yields ['', 'a', 'b', 'c', '']
            if len(cells) != 5:
                raise SystemExit(f"malformed register row ({len(cells) - 2} cells): {line}")
            rows.append((task, cells[1].strip("`"), cells[2], cells[3]))
    return rows


def test_register_exists():
    print("test_register_exists")
    check(REGISTER.is_file(), f"{REGISTER.relative_to(REPO)} exists")


def test_every_finding_registered_exactly_once():
    print("test_every_finding_registered_exactly_once")
    disk = findings_on_disk()
    check(len(disk) > 300, f"found the finding corpus on disk ({len(disk)} findings)")
    rows = register_rows()
    check(bool(rows), f"register parses into rows ({len(rows)} rows)")

    seen = {}
    for task, fid, _, _ in rows:
        seen.setdefault((task, fid), 0)
        seen[(task, fid)] += 1

    missing = sorted(k for k in disk if k not in seen)
    check(not missing, f"every finding is registered (missing: {missing[:6]}"
                       f"{'…' if len(missing) > 6 else ''})")

    dupes = sorted(k for k, n in seen.items() if n > 1)
    check(not dupes, f"no finding is registered twice (dupes: {dupes[:6]})")

    phantom = sorted(k for k in seen if k not in disk)
    check(not phantom, f"no register row invents a finding (phantom: {phantom[:6]})")


# A disposition is meaningful when it either points at plan law (an item, a
# global decision, a design decision) or says in words that it does not — a
# supersession, a recorded discard, or an explicit "no owning item". Length
# alone would pass "→ ?" and fail the perfectly good "→ T19".
CITES_LAW_RE = re.compile(r"\b(R-\d+|GD-\d+|SD-\d+|[DTPG]\d+(?:\.\d+)?)\b|§\d")
SAYS_SO_RE = re.compile(r"superseded|discard|no owning item|merged|fixed this pass",
                        re.I)


def test_dispositions_are_meaningful():
    print("test_dispositions_are_meaningful")
    vacuous = [(t, f, d) for t, f, _, d in register_rows()
               if not (CITES_LAW_RE.search(d) or SAYS_SO_RE.search(d))]
    check(not vacuous, f"every row cites law or states why it does not "
                       f"(vacuous: {vacuous[:4]})")
    empty = [(t, f) for t, f, _, d in register_rows() if not d]
    check(not empty, f"no row has an empty disposition cell (empty: {empty[:6]})")


def test_d8_is_never_cited_bare():
    print("test_d8_is_never_cited_bare")
    # R-38: "D8" named two different decisions. The register must not re-open it.
    bare = [(t, f) for t, f, _, d in register_rows()
            if re.search(r"\bD8\b(?!\.)", d)]
    check(not bare, f"no disposition cites a bare D8 (found: {bare[:6]})")


def test_skills_namespace_collision_is_kept_apart():
    print("test_skills_namespace_collision_is_kept_apart")
    rows = register_rows()
    per_task = {t: {f for tt, f, _, _ in rows if tt == t}
                for t in ("touch-repo-recon", "touch-full-recon")}
    for task in per_task:
        check("SKILLS-1" in per_task[task], f"{task} has its own SKILLS-1 row")
    # …and they are genuinely different findings, so their dispositions differ.
    disp = {t: d for t, f, _, d in rows if f == "SKILLS-1"}
    check(disp.get("touch-repo-recon") != disp.get("touch-full-recon"),
          "the two SKILLS-1 findings carry different dispositions")
    check("SKILLS-17" in per_task["touch-repo-recon"],
          "touch-repo-recon:SKILLS-17 (which has no full-recon twin) is present")


def test_r58_aliases_are_registered():
    print("test_r58_aliases_are_registered")
    text = read(REGISTER)
    # R-58 names three ids for one defect; the register must say so, or a reader
    # chasing RUNSTATE-4 lands on a dead end.
    for fid in ("SKILLS-1", "RUNSTATE-4", "PRODUCT-7"):
        check(fid in text, f"alias {fid} appears in the register")
    alias_line = [ln for ln in text.splitlines()
                  if "RUNSTATE-4" in ln and "PRODUCT-7" in ln and "SKILLS-1" in ln]
    check(bool(alias_line),
          "the SKILLS-1 = RUNSTATE-4 = PRODUCT-7 alias is stated on one line")
    check(any("R-58" in ln for ln in alias_line),
          "that alias line names R-58 as the item that resolves it")


def test_register_explains_itself():
    print("test_register_explains_itself")
    text = read(REGISTER)
    for token in ("R-06", "GD-17", "test_register.py"):
        check(token in text, f"register cites {token}")
    check("touch-mongo-live" in text, "the five touch-mongo-live reports are covered")


def main():
    for t in (test_register_exists,
              test_every_finding_registered_exactly_once,
              test_dispositions_are_meaningful,
              test_d8_is_never_cited_bare,
              test_skills_namespace_collision_is_kept_apart,
              test_r58_aliases_are_registered,
              test_register_explains_itself):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all findings-register tests passed")


if __name__ == "__main__":
    main()
