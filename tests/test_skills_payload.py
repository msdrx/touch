#!/usr/bin/env python3
"""The shipped skills: moved, renamed, self-contained, and capability-typed.

Items 09 and 10 (PLUGIN-SPEC-7/8/16, CM-11, DISTRIBUTION-9, GD-U3). Run as
`python3 test_skills_payload.py`; exits non-zero on failure. No pytest, no
runner — `run_all.sh` picks it up by its `test_*.py` glob.

WHAT THIS FILE IS FOR
---------------------
`plugin/touch/skills/` is a MOVED tree, not a pinned copy (GD-T2): there is
exactly one copy of each skill and it lives in the payload, because the shipped
text has to differ from the repo-shaped text it grew out of. A second copy would
be a permanent drift trap, and while both existed the CLI offered `/execute-research`
and `/touch:execute-research` side by side with no override between them
(PLUGIN-SPEC-16). So the first thing asserted here is that the old location is
gone. The six engineering-practice skills adopted in GD-U3 arrived the same
way — copied in from `.temp-develop/`, which was then deleted, for the same
reason.

TWO KINDS OF SKILL, ONE TABLE
-----------------------------
`SKILLS` below is the single declaration this file derives everything from: the
directory set, the expected frontmatter names, the per-skill command
requirements and the count the CLI must report. Each entry declares a `kind`.

  orchestration   drives the monitoring stack: it MUST name the `touch-*`
                  commands it calls, because a driver that spells a payload
                  path instead breaks the moment the plugin cache is
                  re-stamped.
  content         pure prose guidance (GD-U3's six): it drives nothing, so it
                  must name NO `touch-*` command at all. Asserting the absence
                  is the point — a `content` skill that grows a daemon call has
                  silently become a driver, and adding the six to a universal
                  `REQUIRED_COMMANDS` with empty tuples would have quietly
                  weakened the rule for the four that do need it.

`BANNED` and `DAEMON_FILES` stay universal: they are hygiene, not role.

The rest is the self-containment gate. A skill body is prose an agent obeys, and
every path in it that will not exist on an installer's machine is an instruction
to fail. Three classes are banned outright:

  repo-absolute paths     `/home/<user>/…`, `/Users/<user>/…`, this project's
                          own slugs — they resolve to nothing anywhere else,
                          and they are also the payload's PII surface.
  `.claude/shared/…`      the dev repo's canonical monitoring directory. In a
                          payload the monitoring files sit under the PLUGIN
                          root, which is a version-stamped cache; the skills
                          reach them through `bin/` command names instead.
  `/ABS/PATH/TO/REPO`     the templates' old single-root placeholder. One root
                          cannot express both "the user's project" (where task
                          state lives, forever) and "the installed plugin"
                          (a cache re-copied on every update) — PLUGIN-SPEC-8.

What replaces them is checked positively, not just negatively: the bare command
names (`touch-status`, `touch-monitor`, `touch-watcher`, `touch-cycle-reporter`),
and the `PROJECT_DIR` / `PLUGIN_ROOT` split in both templates.

One positive check has no negative twin and needs its own paragraph: the tasks
root. The shipped hook resolves it as `$ORCH_TASKS_ROOT` >
`$CLAUDE_PROJECT_DIR/.claude/local-orchestrators` > a marker-ceilinged cwd
walk-up (GD-T5). A driver whose SKILL.md says `$PWD/.claude/local-orchestrators`
agrees with that only on a machine where all three coincide; anywhere else it
writes `ACTIVE` and `HALT` where the guard does not look, and the guard — and
the HALT brake — go inert for the whole run without an error or a warning. So
the manual must name the override, and no payload file may anchor the root on a
bare `$PWD`.

The substitution law (GD-T4) is asserted in BOTH directions, because getting it
backwards is silent: `${CLAUDE_PLUGIN_ROOT}` is expanded in a SKILL.md body and
is NOT expanded in a supporting file the skill tells the agent to read. So the
literal belongs in the SKILL.md — where it is the value the agent pastes — and
must never appear in a template, where it would ship as a path that resolves to
nothing.

Skips: the `claude plugin details` arm needs the CLI, which a clean checkout or
a CI image may not have; it skips with a printed reason (the suite's mongod
convention). Everything else is pure filesystem and always runs.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin" / "touch"
SKILLS_DIR = PLUGIN / "skills"
OLD_SKILLS = REPO / ".claude" / "skills"

#: The payload's declared skill inventory: directory name -> capability record.
#: `kind` is "orchestration" (drives the monitoring stack) or "content" (prose
#: guidance that drives nothing); `commands` are the `touch-*` wrappers the
#: SKILL.md must call by name, and it is empty for every `content` skill —
#: enforced in both directions, see the module docstring.
#:
#: The DIRECTORY name is the invocation surface: a plugin skill invokes as
#: `/<plugin>:<skill>`, so the directory `touch-orchestrate` inside a plugin
#: named `touch` would have read `/touch:touch-orchestrate` (CM-11,
#: DISTRIBUTION-9). That rename is the one rename packaging required. The
#: frontmatter `name:` follows the directory and is documentation only — the
#: CLI ignores a mismatch silently (measured on 2.1.220), so
#: `test_frontmatter_names_match_directories` is the only thing keeping it true.
SKILLS = {
    # Orchestration — the deterministic run drivers.
    "execute-research": {"kind": "orchestration", "commands": ("touch-status",)},
    "implement-plan": {"kind": "orchestration",
                       "commands": ("touch-status", "touch-cycle-reporter")},
    "m-orchestrator": {"kind": "orchestration",
                       "commands": ("touch-status", "touch-monitor", "touch-watcher")},
    "orchestrate": {"kind": "orchestration", "commands": ("touch-status",)},
    # Engineering practice — GD-U3's six, adopted and adapted. No commands.
    "architecture-boundaries": {"kind": "content", "commands": ()},
    "architecture-tradeoffs": {"kind": "content", "commands": ()},
    "code-quality-review": {"kind": "content", "commands": ()},
    "pattern-selection": {"kind": "content", "commands": ()},
    "refactoring-pass": {"kind": "content", "commands": ()},
    "testing-discipline": {"kind": "content", "commands": ()},
}

#: Directory name -> the frontmatter `name:` it must declare. Derived, because
#: the law is that they are equal.
EXPECTED = {name: name for name in SKILLS}

ORCHESTRATION = tuple(n for n, s in SKILLS.items() if s["kind"] == "orchestration")
CONTENT = tuple(n for n, s in SKILLS.items() if s["kind"] == "content")

TEMPLATES = (
    "execute-research/templates/research.workflow.js",
    "implement-plan/templates/implement.workflow.js",
)

#: Banned source text, as (compiled pattern, why). Applied to every file under
#: `plugin/touch/skills/`, line by line, so a failure names path:line.
BANNED = (
    (re.compile(r"/ABS/PATH/TO/REPO"),
     "the retired single-root placeholder (PLUGIN-SPEC-8)"),
    (re.compile(r"\.claude/shared/monitoring"),
     "the dev repo's monitoring directory, absent from any installer's project"),
    (re.compile(r"touch-orchestrate"),
     "the pre-rename skill name (CM-11)"),
    (re.compile(r"/home/[a-z0-9_.-]+/"),
     "a repo-absolute POSIX home path"),
    (re.compile(r"/Users/[A-Za-z0-9_.-]+/"),
     "a repo-absolute macOS home path"),
    (re.compile(r"laniakea|michaelsadradze"),
     "an author/machine slug (payload PII)"),
)

#: Payload FILE names a SKILL.md must never spell. Naming one tells an agent to
#: run (or copy) a path inside the version-stamped plugin cache; the wrappers
#: exist so it can spell a command instead. `[m]onitor_server` (the bracket
#: rule) survives this on purpose — it is a `pkill` pattern, not an invocation,
#: and carries no `.py`. `cycle_reporter.py` and `monitor.html` are here
#: because the rewrite removed mentions of both (`touch-cycle-reporter` replaced
#: the "adapt templates/cycle_reporter.py into orch-scripts/" instruction) and
#: nothing else would notice them coming back.
DAEMON_FILES = ("status.sh", "monitor_server.py", "decision_watcher.py",
                "cycle_reporter.py", "monitor.html")

#: The commands each ORCHESTRATION SKILL.md must reach the monitoring stack by.
#: Derived from the table so the two can never disagree; `content` skills are
#: absent by construction and are checked by the inverse arm instead.
REQUIRED_COMMANDS = {n: SKILLS[n]["commands"] for n in ORCHESTRATION}

#: Any `touch-*` wrapper name. A `content` skill matching this has grown a
#: dependency on the monitoring stack and is no longer content.
TOUCH_COMMAND = re.compile(r"\btouch-[a-z][a-z-]*")

#: Guidance tokens item 11 removed from the adopted six, kept out by name.
#: `should-fix` is the severity word that maps to nothing in the critique
#: schema (which gates on blocker/major/minor/nit), and the three architecture
#: -testing products are JVM/.NET tooling prescribed to a repo whose payload is
#: Python and bash and whose dependency policy (GD-21) forbids adding either.
RETIRED_TOKENS = (
    ("code-quality-review", "should-fix",
     "a severity the critique schema cannot consume"),
    (None, "ArchUnit", "a JVM tool prescribed to a stdlib-only Python repo"),
    (None, "NetArchTest", "a .NET tool prescribed to a stdlib-only Python repo"),
    (None, "JDepend", "a JVM tool prescribed to a stdlib-only Python repo"),
)

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


def payload_files():
    """Every file under `plugin/touch/skills/`, sorted, repo-relative-able."""
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(p for p in SKILLS_DIR.rglob("*") if p.is_file())


def frontmatter_name(text):
    """The `name:` of a YAML frontmatter block, or None.

    Deliberately literal: the loader reads the first `---` block, so a `name:`
    further down the body is not the skill's name and must not satisfy this.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return None


def have_cli():
    return shutil.which("claude") is not None


def run_cli(args, timeout=240):
    """CompletedProcess, or None when the CLI is missing/hangs/errors out."""
    try:
        return subprocess.run(
            ["claude", *args], cwd=str(REPO),
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


# --- the move itself: one copy, in the payload
def test_moved_not_copied():
    print("test_moved_not_copied")
    check(SKILLS_DIR.is_dir(), "plugin/touch/skills/ exists")
    # The old tree is GONE, not shadowed. While both existed the CLI offered
    # `/x` and `/touch:x` with no override between them, and they diverge the
    # moment the payload copy is rewritten for self-containment.
    check(not OLD_SKILLS.exists(),
          ".claude/skills/ no longer exists (single canonical copy, GD-T2)")
    for name in EXPECTED:
        check((SKILLS_DIR / name / "SKILL.md").is_file(),
              f"plugin/touch/skills/{name}/SKILL.md is in the payload")
    # Nothing extra: EXACT equality, not a subset. An undeclared directory is
    # an undeclared component — it ships, it loads, it costs always-on context,
    # and nothing else in the repo would notice it.
    dirs = sorted(p.name for p in SKILLS_DIR.iterdir() if p.is_dir()) if SKILLS_DIR.is_dir() else []
    check(dirs == sorted(EXPECTED),
          f"exactly the {len(EXPECTED)} declared skill directories ({dirs})")


def test_frontmatter_names_match_directories():
    print("test_frontmatter_names_match_directories")
    for directory, expected in EXPECTED.items():
        path = SKILLS_DIR / directory / "SKILL.md"
        if not path.is_file():
            check(False, f"{directory}/SKILL.md exists to carry a name")
            continue
        got = frontmatter_name(path.read_text(encoding="utf-8"))
        check(got == expected,
              f"{directory}/SKILL.md declares name: {expected} (got {got!r})")


# --- the grep gate
def test_no_banned_text_in_payload():
    print("test_no_banned_text_in_payload")
    files = payload_files()
    check(bool(files), "the skills payload has files to scan")
    for pattern, why in BANNED:
        hits = []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    # path:line only. The point of a leak gate is not to
                    # reprint the leak.
                    hits.append(f"{path.relative_to(REPO)}:{n}")
        check(not hits, f"no {why} — {pattern.pattern} ({hits[:5]})")


def test_no_daemon_paths_in_skill_bodies():
    print("test_no_daemon_paths_in_skill_bodies")
    for directory in EXPECTED:
        path = SKILLS_DIR / directory / "SKILL.md"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for name in DAEMON_FILES:
            check(name not in text,
                  f"{directory}/SKILL.md names no daemon file ({name})")


def test_skill_bodies_use_bare_commands():
    print("test_skill_bodies_use_bare_commands")
    for directory, commands in REQUIRED_COMMANDS.items():
        path = SKILLS_DIR / directory / "SKILL.md"
        if not path.is_file():
            check(False, f"{directory}/SKILL.md exists to be checked")
            continue
        text = path.read_text(encoding="utf-8")
        for command in commands:
            check(command in text, f"{directory}/SKILL.md calls {command} by name")


def test_content_skills_drive_nothing():
    """The inverse of the arm above, and the reason `kind` exists.

    A `content` skill is prose an agent reads while working on someone else's
    codebase; it has no run, no task folder and no daemons. The moment one
    names a `touch-*` wrapper it has become a driver that the orchestration
    rules (tasks root, sentinels, findings handoff) apply to — and none of
    those rules would have been applied to it.
    """
    print("test_content_skills_drive_nothing")
    for directory in CONTENT:
        path = SKILLS_DIR / directory / "SKILL.md"
        if not path.is_file():
            check(False, f"{directory}/SKILL.md exists to be checked")
            continue
        text = path.read_text(encoding="utf-8")
        hits = sorted({m.group(0) for m in TOUCH_COMMAND.finditer(text)})
        check(not hits,
              f"{directory}/SKILL.md names no touch-* command ({hits[:5]})")


def test_cycle_reporter_is_where_its_wrapper_looks():
    print("test_cycle_reporter_is_where_its_wrapper_looks")
    # `bin/touch-cycle-reporter` resolves this exact payload-relative path from
    # its own $0. The wrapper and the skill tree ship together, so the two can
    # only disagree here.
    target = SKILLS_DIR / "implement-plan" / "templates" / "cycle_reporter.py"
    check(target.is_file(),
          "skills/implement-plan/templates/cycle_reporter.py ships with the skill")
    wrapper = PLUGIN / "bin" / "touch-cycle-reporter"
    if wrapper.is_file():
        check("skills/implement-plan/templates/cycle_reporter.py"
              in wrapper.read_text(encoding="utf-8"),
              "bin/touch-cycle-reporter points at that same path")
    else:
        skip("plugin/touch/bin/touch-cycle-reporter absent — wrapper arm not run")


#: A tasks-root anchored on a bare `$PWD`. The shipped hook resolves the root as
#: `$ORCH_TASKS_ROOT` > `$CLAUDE_PROJECT_DIR/.claude/local-orchestrators` >
#: a marker-ceilinged cwd walk-up; a driver that writes `ACTIVE`/`HALT` (or the
#: task folder itself) under `$PWD` instead diverges from that on any machine
#: where the two differ, and the guard then reports itself inert for the whole
#: run — silently, HALT included. `${CLAUDE_PROJECT_DIR:-$PWD}` is the
#: sanctioned tail of the resolution chain and must still pass, so the pattern
#: matches `$PWD` only when it is the WHOLE anchor.
BARE_PWD_ANCHOR = re.compile(r'(?<!:-)\$(?:PWD\b|\{PWD\})[^\n]{0,4}?/\.claude/local-orchestrators')


def test_tasks_root_is_resolved_not_assumed():
    print("test_tasks_root_is_resolved_not_assumed")
    manual = SKILLS_DIR / "m-orchestrator" / "SKILL.md"
    if manual.is_file():
        text = manual.read_text(encoding="utf-8")
        # The operator manual for run scope has to name the override that
        # outranks the project anchor; a driver that never heard of it cannot
        # keep the sentinels where the guard looks.
        check("ORCH_TASKS_ROOT" in text,
              "m-orchestrator/SKILL.md names $ORCH_TASKS_ROOT (GD-T5 tier 1)")
        check("CLAUDE_PROJECT_DIR" in text,
              "m-orchestrator/SKILL.md names $CLAUDE_PROJECT_DIR (GD-T5 tier 2)")
        # HALT is the other sentinel the guard reads out of that same root, and
        # this file is the only place the payload explains sentinels to a human.
        check("HALT" in text,
              "m-orchestrator/SKILL.md documents the HALT sentinel")
    else:
        check(False, "m-orchestrator/SKILL.md exists to be checked")
    for path in payload_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits = [n for n, line in enumerate(text.splitlines(), 1)
                if BARE_PWD_ANCHOR.search(line)]
        check(not hits,
              f"{path.relative_to(SKILLS_DIR)}: no bare-$PWD tasks root ({hits[:5]})")


# --- the two-root split in the templates (PLUGIN-SPEC-8)
def test_templates_split_the_two_roots():
    print("test_templates_split_the_two_roots")
    for rel in TEMPLATES:
        path = SKILLS_DIR / rel
        if not path.is_file():
            check(False, f"{rel} is in the payload")
            continue
        src = path.read_text(encoding="utf-8")
        name = path.name
        check(re.search(r"^const PROJECT_DIR = ", src, re.M),
              f"{name}: declares PROJECT_DIR (task state, project-anchored)")
        check(re.search(r"^const PLUGIN_ROOT = ", src, re.M),
              f"{name}: declares PLUGIN_ROOT (the installed payload)")
        check(not re.search(r"^const REPO = ", src, re.M),
              f"{name}: the single REPO root is gone")
        check(not re.search(r"^const S = ", src, re.M),
              f"{name}: the baked status.sh path constant is gone")
        check("const STATUS = 'touch-status'" in src,
              f"{name}: the event writer is the bare command name")
        # Task state hangs off the PROJECT, never off the plugin cache (GD-T5):
        # the plugin root is version-stamped and swept ~14 days after an update.
        check(re.search(r"const TASK = PROJECT_DIR \+ ", src),
              f"{name}: TASK is derived from PROJECT_DIR")
        check("PLUGIN_ROOT + '/.claude" not in src,
              f"{name}: nothing anchors task state under the plugin root")


def test_substitution_law_is_respected_in_both_directions():
    print("test_substitution_law_is_respected_in_both_directions")
    # A SKILL.md body IS substituted, so the literal there is how the agent
    # learns the real plugin path...
    for directory in ("execute-research", "implement-plan"):
        path = SKILLS_DIR / directory / "SKILL.md"
        if not path.is_file():
            continue
        check("${CLAUDE_PLUGIN_ROOT}" in path.read_text(encoding="utf-8"),
              f"{directory}/SKILL.md carries the literal ${{CLAUDE_PLUGIN_ROOT}}")
    # ...and a supporting file is NOT substituted, so the same literal there
    # would ship as a path that resolves to nothing.
    for rel in TEMPLATES:
        path = SKILLS_DIR / rel
        if not path.is_file():
            continue
        check("${CLAUDE_PLUGIN_ROOT}" not in path.read_text(encoding="utf-8"),
              f"{path.name}: no unsubstitutable ${{CLAUDE_PLUGIN_ROOT}} literal")
    # A content skill has no payload file to point at, so it has no business
    # carrying the substitution literal either — it would read as an
    # instruction to go find something inside the plugin cache.
    for directory in CONTENT:
        path = SKILLS_DIR / directory / "SKILL.md"
        if not path.is_file():
            continue
        check("${CLAUDE_PLUGIN_ROOT}" not in path.read_text(encoding="utf-8"),
              f"{directory}/SKILL.md needs no ${{CLAUDE_PLUGIN_ROOT}} (it reads no payload file)")


# --- item 11: the adopted six say what this repo decided they say
def test_adopted_skills_dropped_the_retired_tokens():
    """Guidance that collides with settled law, kept out by name.

    Both classes here were live defects in the text as supplied, not
    hypotheticals: `should-fix` is a severity the shipped critique schema
    cannot consume (it gates approval on zero blocker/major, so a `should-fix`
    finding approves), and the three architecture-testing products are JVM/.NET
    tooling recommended to a repo that permits exactly one third-party runtime
    dependency (GD-21) and has no CI to run them in.
    """
    print("test_adopted_skills_dropped_the_retired_tokens")
    for scope, token, why in RETIRED_TOKENS:
        directories = (scope,) if scope else tuple(SKILLS)
        for directory in directories:
            path = SKILLS_DIR / directory / "SKILL.md"
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            check(token not in text,
                  f"{directory}/SKILL.md drops {token!r} — {why}")


def test_content_skills_cross_reference_by_invocation():
    """A cross-reference an agent can act on, not a directory listing.

    Inside the plugin a skill invokes as `/touch:<skill>`; a bare backticked
    name reads as a file path and gives the agent nothing to call. Scoped to
    the `content` skills deliberately: the orchestration four name each other
    as stages of one workflow ("the plan `execute-research` produced"), which
    is prose about a hand-off, not an instruction to invoke.
    """
    print("test_content_skills_cross_reference_by_invocation")
    for directory in CONTENT:
        path = SKILLS_DIR / directory / "SKILL.md"
        if not path.is_file():
            check(False, f"{directory}/SKILL.md exists to be checked")
            continue
        text = path.read_text(encoding="utf-8")
        for other in SKILLS:
            if other == directory:
                continue
            bare = [n for n, line in enumerate(text.splitlines(), 1)
                    if re.search(rf"(?<!/touch:){re.escape(other)}", line)]
            check(not bare,
                  f"{directory}/SKILL.md names {other} only as /touch:{other} ({bare[:3]})")


def test_content_skills_defer_to_the_project():
    """The two-line preamble (GD-U3 universal edit).

    These skills auto-load by description, including inside an implementer
    agent that owns three files. Without the preamble the model has a
    first-class instruction to restructure whatever it reads, and the critique
    gate then rejects the whole attempt for out-of-scope edits.
    """
    print("test_content_skills_defer_to_the_project")
    for directory in CONTENT:
        path = SKILLS_DIR / directory / "SKILL.md"
        if not path.is_file():
            check(False, f"{directory}/SKILL.md exists to be checked")
            continue
        text = path.read_text(encoding="utf-8")
        check("a finding, not an edit" in text,
              f"{directory}/SKILL.md keeps owned files in scope")
        check("the project wins" in text,
              f"{directory}/SKILL.md defers to the project's convention")
        check(re.search(r"^Sources: ", text, re.M),
              f"{directory}/SKILL.md keeps a path-free Sources: attribution")


# --- what the CLI actually loads
def test_plugin_details_lists_every_skill():
    print("test_plugin_details_lists_every_skill")
    if not have_cli():
        skip("`claude` CLI not on PATH — plugin details not run")
        return
    res = run_cli(["--plugin-dir", str(PLUGIN), "plugin", "details", "touch"])
    # `have_cli()` covers the one legitimate skip; `run_cli` returns None only
    # on a spawn failure or a timeout. A CLI that RAN and refused the payload is
    # a failure, never a skip.
    if res is None:
        skip("`claude --plugin-dir ...` did not spawn/finish — details not run")
        return
    check(res.returncode == 0,
          f"claude --plugin-dir ... plugin details touch loads the payload "
          f"(rc={res.returncode}, {(res.stdout + res.stderr).strip()[-300:]})")
    out = res.stdout
    check(f"Skills ({len(SKILLS)})" in out,
          f"the payload registers exactly {len(SKILLS)} skills (inventory: "
          f"{[l.strip() for l in out.splitlines() if 'Skills (' in l]})")
    for name in EXPECTED.values():
        check(re.search(rf"\b{re.escape(name)}\b", out),
              f"details names the skill {name}")


def main():
    for t in (test_moved_not_copied,
              test_frontmatter_names_match_directories,
              test_no_banned_text_in_payload,
              test_no_daemon_paths_in_skill_bodies,
              test_skill_bodies_use_bare_commands,
              test_content_skills_drive_nothing,
              test_cycle_reporter_is_where_its_wrapper_looks,
              test_tasks_root_is_resolved_not_assumed,
              test_templates_split_the_two_roots,
              test_substitution_law_is_respected_in_both_directions,
              test_adopted_skills_dropped_the_retired_tokens,
              test_content_skills_cross_reference_by_invocation,
              test_content_skills_defer_to_the_project,
              test_plugin_details_lists_every_skill):
        t()
    print()
    if skips:
        print(f"skipped: {len(skips)} check(s)")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all skills-payload tests passed")


if __name__ == "__main__":
    main()
