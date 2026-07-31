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
be a permanent drift trap, and while both existed the CLI offered `/research`
and `/touch:research` side by side with no override between them
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
`$CLAUDE_PROJECT_DIR/.touch/local-orchestrators` > a walk up from the cwd to a
`.claude/` marker, then that same `.touch/local-orchestrators` beneath it
(GD-T5 as amended by G10 — the marker directory and the state directory
deliberately differ). A driver whose SKILL.md says
`$PWD/.touch/local-orchestrators` (or the legacy `$PWD/.claude/…` spelling)
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
import tempfile
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
    "research": {"kind": "orchestration", "commands": ("touch-status",)},
    "implement": {"kind": "orchestration",
                       "commands": ("touch-status", "touch-cycle-reporter")},
    "monitor": {"kind": "orchestration",
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
    "research/templates/research.workflow.js",
    "implement/templates/implement.workflow.js",
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


#: A prompt-builder declaration. Every prompt in both templates is a
#: module-level `const <name>Prompt = (...) => \`...\`` arrow. Several
#: assertions below are meaningless file-wide and sharp per builder: the full
#: plan path legitimately appears in the divider and final-fixer prompts and
#: must NOT appear in the impl/gate/critique ones (D-23).
PROMPT_DECL = re.compile(r"^const (\w+Prompt) = ", re.M)


def _quoted_end(src, i):
    """Index just past the `'`/`"` string opening at `i`."""
    quote = src[i]
    i += 1
    while i < len(src):
        if src[i] == "\\":
            i += 2
            continue
        if src[i] == quote:
            return i + 1
        i += 1
    return len(src)


def _substitution_end(src, i):
    """Index just past the `}` closing a `${` whose brace body starts at `i`."""
    depth = 1
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "`":
            i = _template_end(src, i)
            continue
        if c in "'\"":
            i = _quoted_end(src, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(src)


def _template_end(src, i):
    """Index just past the backtick closing the template literal opened at `i`.

    A real (small) scanner rather than a line heuristic, because the end
    boundary decides what every per-builder assertion actually covers. The
    naive rule — "up to the next line-initial `const`" — silently swallowed 75
    lines of driver code into `finalFixPrompt`'s body, so a `${PLAN_FILE}`
    anywhere in the serial/parallel driver would have satisfied an assertion
    that claims to be about the prompt. Both hazards this walks over are real
    in these files: escaped backticks inside a prompt (the divider explains
    \\`last: true\\`) and nested literals inside a substitution
    (`${TEST_HINTS ? \\`…\\` : ''}`).
    """
    i += 1
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "`":
            return i + 1
        if c == "$" and src[i + 1:i + 2] == "{":
            i = _substitution_end(src, i + 2)
            continue
        i += 1
    return len(src)


def prompt_bodies(src):
    """{builder name: its source text}, ending at the literal's own backtick."""
    bodies = {}
    for m in PROMPT_DECL.finditer(src):
        open_bt = src.find("`", m.end())
        if open_bt == -1:
            continue
        bodies[m.group(1)] = src[m.start():_template_end(src, open_bt)]
    return bodies


def template_src(rel):
    path = SKILLS_DIR / rel
    return path.read_text(encoding="utf-8") if path.is_file() else None


def code_only(src):
    """`src` with whole-line `//` comments dropped.

    Deletion assertions have to run against CODE, not against the paragraph
    that explains the deletion. These templates document why the dead Node-API
    plumbing was removed — naming `import('node:…')` in the process — and that
    explanation is the whole reason a maintainer does not put it back.
    """
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("//"))


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
    target = SKILLS_DIR / "implement" / "templates" / "cycle_reporter.py"
    check(target.is_file(),
          "skills/implement/templates/cycle_reporter.py ships with the skill")
    wrapper = PLUGIN / "bin" / "touch-cycle-reporter"
    if wrapper.is_file():
        check("skills/implement/templates/cycle_reporter.py"
              in wrapper.read_text(encoding="utf-8"),
              "bin/touch-cycle-reporter points at that same path")
    else:
        skip("plugin/touch/bin/touch-cycle-reporter absent — wrapper arm not run")


#: A tasks-root anchored on a bare `$PWD`. The shipped hook resolves the root as
#: `$ORCH_TASKS_ROOT` > `$CLAUDE_PROJECT_DIR/.touch/local-orchestrators` >
#: a `.claude/`-marker-ceilinged cwd walk-up joined with the same
#: `.touch/local-orchestrators`; a driver that writes `ACTIVE`/`HALT` (or the
#: task folder itself) under `$PWD` instead diverges from that on any machine
#: where the two differ, and the guard then reports itself inert for the whole
#: run — silently, HALT included. `${CLAUDE_PROJECT_DIR:-$PWD}` is the
#: sanctioned tail of the resolution chain and must still pass, so the pattern
#: matches `$PWD` only when it is the WHOLE anchor. BOTH root spellings are
#: matched: `.touch/` is the post-G10 home and `.claude/` is the legacy one the
#: guard still reads transitionally, so a payload file cannot dodge this ban by
#: keeping the old literal.
BARE_PWD_ANCHOR = re.compile(
    r'(?<!:-)\$(?:PWD\b|\{PWD\})[^\n]{0,4}?/\.(?:touch|claude)/local-orchestrators')


def test_tasks_root_is_resolved_not_assumed():
    print("test_tasks_root_is_resolved_not_assumed")
    manual = SKILLS_DIR / "monitor" / "SKILL.md"
    if manual.is_file():
        text = manual.read_text(encoding="utf-8")
        # The operator manual for run scope has to name the override that
        # outranks the project anchor; a driver that never heard of it cannot
        # keep the sentinels where the guard looks.
        check("ORCH_TASKS_ROOT" in text,
              "monitor/SKILL.md names $ORCH_TASKS_ROOT (GD-T5 tier 1)")
        check("CLAUDE_PROJECT_DIR" in text,
              "monitor/SKILL.md names $CLAUDE_PROJECT_DIR (GD-T5 tier 2)")
        # HALT is the other sentinel the guard reads out of that same root, and
        # this file is the only place the payload explains sentinels to a human.
        check("HALT" in text,
              "monitor/SKILL.md documents the HALT sentinel")
    else:
        check(False, "monitor/SKILL.md exists to be checked")
    for path in payload_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits = [n for n, line in enumerate(text.splitlines(), 1)
                if BARE_PWD_ANCHOR.search(line)]
        check(not hits,
              f"{path.relative_to(SKILLS_DIR)}: no bare-$PWD tasks root ({hits[:5]})")
        # G10: the tasks root moved to `.touch/local-orchestrators`. The skills
        # are the copy-paste source for a driver's own ladder one-liner, so a
        # stale `.claude/` spelling here re-creates the old tree on the next run
        # even after the move — the daemons would then write where nothing reads.
        legacy = [n for n, line in enumerate(text.splitlines(), 1)
                  if "/.claude/local-orchestrators" in line]
        check(not legacy,
              f"{path.relative_to(SKILLS_DIR)}: tasks root is "
              f".touch/local-orchestrators, not the legacy .claude/ one ({legacy[:5]})")


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
        # D-10 retired the script-side event writer entirely (see
        # `test_templates_carry_no_dead_node_api`), so there is no `STATUS`
        # constant left to check. What survives is the reason it existed: a
        # payload PATH must never be baked into a copied script, because the
        # plugin root is a version-stamped cache. Assert that directly.
        check("/bin/touch-status" not in src,
              f"{name}: no baked path into the plugin's bin/ survives")
        # Task state hangs off the PROJECT, never off the plugin cache (GD-T5):
        # the plugin root is version-stamped and swept ~14 days after an update.
        check(re.search(r"const TASK = PROJECT_DIR \+ ", src),
              f"{name}: TASK is derived from PROJECT_DIR")
        check("PLUGIN_ROOT + '/.claude" not in src,
              f"{name}: nothing anchors task state under the plugin root")
        check("PLUGIN_ROOT + '/.touch" not in src,
              f"{name}: nothing anchors task state under the plugin root")


def test_substitution_law_is_respected_in_both_directions():
    print("test_substitution_law_is_respected_in_both_directions")
    # A SKILL.md body IS substituted, so the literal there is how the agent
    # learns the real plugin path...
    for directory in ("research", "implement"):
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


def test_templates_guard_every_spawn():
    """The infra guard (network-recovery.md layer 2) is IN the protocol — in BOTH.

    An `agent()` returning null died on the API, not on the work — it must
    never spend a gated attempt or be laundered into a fabricated "gate died"
    red. The 2026-07-29 outage run is why this is pinned: the unguarded loops
    burned two sub-plans' whole attempt caps (~3 minutes per death, zero
    substantive verdicts), and the strictly-last endgame then ran over the red
    board and absorbed the dead loops' work. Every spawn goes through
    `agentR`; raw `await agent(` appears exactly twice — the wrapper's own
    first try and its tagged same-attempt retry.

    D-11a generalized this arm to the research template, which shipped without
    the guard: `network-recovery.md` told the launcher to "add it by hand",
    which is a mandate, not a mechanism — the same class of defect D-09
    deletes. A researcher that dies on an outage now retries in place instead
    of silently shrinking the board the synthesizer plans from.
    """
    print("test_templates_guard_every_spawn")
    for rel in TEMPLATES:
        src = template_src(rel)
        name = rel.rsplit("/", 1)[-1]
        if src is None:
            check(False, f"{name} is in the payload")
            continue
        check("const agentR = async" in src,
              f"{name}: defines the agentR infrastructure guard")
        raw = src.count("await agent(")
        check(raw == 2,
              f"{name}: raw `await agent(` appears exactly twice, both inside "
              f"agentR (found {raw})")
        # `agentR(` matches only call sites — the definition reads
        # `const agentR = async (...)`. The fan-out spawns inside a thunk
        # (`() => agentR(...)`) with no `await` of its own, so counting
        # `await agentR(` would undercount the research template by one.
        guarded = code_only(src).count("agentR(")
        # Every spawn is guarded, and the count is derived rather than
        # hardcoded: one `[monitor]` marker per prompt, one guarded spawn per
        # prompt. A new prompt that forgets the wrapper breaks the equality.
        markers = src.count("[monitor] plan=")
        check(guarded == markers and guarded >= 2,
              f"{name}: every prompt has a guarded spawn "
              f"({guarded} agentR calls vs {markers} markers)")
        check("writePlaceholderFindings" not in src,
              f"{name}: no fabricated 'gate died' verdict path remains")

    impl = template_src(TEMPLATES[1])
    if impl:
        check("last: { type: 'boolean' }" in impl,
              "the divider schema carries the strictly-last marker")
        check("sp.last === true" in impl and "'blocked'" in impl,
              "strictly-last loops are gated on an all-green board and record `blocked` otherwise")

    # D-11b: the research fan-out refuses a partial board. `parallel()` turns a
    # thrown agentR into a silent null, so "a perspective died" and "a
    # perspective returned nothing" look identical at the barrier — the mirror
    # of the implement template's never-vanish rule (`results.length <`).
    research = template_src(TEMPLATES[0])
    if research:
        check("const MIN_REPORTS = ARGS.min_reports ?? PERSPECTIVES.length" in research,
              "research: MIN_REPORTS defaults to the full perspective board")
        guard = re.search(
            r"if \(!reports\.length \|\| reports\.length < MIN_REPORTS\) \{(.*?)\n\}",
            research, re.S)
        check(guard is not None,
              "research: a `reports.length <` guard stands between the barrier "
              "and synthesis, and an empty board never passes it")
        if guard:
            body = guard.group(1)
            check("throw new Error(" in body,
                  "research: the short-board path throws instead of synthesizing")
            # It must NOT announce a badge it cannot cause. cycle_reporter's
            # zero-return rule (D-14) closes a research card `failed` only on an
            # EMPTY board; a PARTIAL board carries results with `findings`,
            # which the same rule reads as `done`. A script that logs "closes
            # failed" while the dashboard goes green is R-58's defect with the
            # sign flipped, and it sends a maintainer to a file that is
            # behaving exactly as specified. Until D-14 learns MIN_REPORTS
            # (carried to the reporter sub-plan), this branch reports only what
            # it did: it refused.
            check("closes failed" not in body and "close failed" not in body,
                  "research: the short-board path claims no verdict it cannot cause")
            check("refusing to synthesize" in body,
                  "research: the short-board path logs what it did, not what "
                  "some other program will emit")
        guard_at = research.find("MIN_REPORTS)")
        synth_at = research.find("phase('Synthesize')")
        check(guard_at != -1 and synth_at != -1 and guard_at < synth_at,
              "research: the refusal happens BEFORE the synthesis phase")
    if impl:
        check("results.length < NORMAL.length" in impl,
              "implement: the never-vanish rule this mirrors is still there")


# --- D-09: the mandated FIRST/LAST touch-status pair is deleted
def test_templates_carry_no_status_mandate():
    """No prompt instructs an agent to trace itself (D-09, correctness).

    This is NOT a token item and must never be defended or re-litigated as one
    (GD-D3): the pair was 0.05% of a run's bill. What was wrong with it is that
    an instruction is not a mechanism — a mandated line can be forgotten,
    mistyped, or written into the wrong task folder with a missing
    ORCH_STATE_DIR, while `decision_watcher.py` derives the same spawn and the
    same result from the journal and the marker, measured 96-99% twin coverage
    against 79-100% compliance, and usually earlier. The one thing the pair
    carried that derivation could not — the agent's own `summary` — moved onto
    the derived result line (D-06), which is what made this deletion
    information-neutral.

    `touch-status` the COMMAND is untouched (GD-D14): it stays the only write
    path into events.jsonl, for a human, a driver, and the deterministic
    emitters. Only the instructed invocations are gone.
    """
    print("test_templates_carry_no_status_mandate")
    for rel in TEMPLATES:
        src = template_src(rel)
        name = rel.rsplit("/", 1)[-1]
        if src is None:
            check(False, f"{name} is in the payload")
            continue
        for token in ("FIRST run:", "LAST run:"):
            check(token not in src, f"{name}: no `{token}` mandate in any prompt")
        check("const statusCmd" not in src,
              f"{name}: the prompt-text status command builder is gone")
        check('ORCH_STATE_DIR="' not in src,
              f"{name}: no prompt hands an agent a status command to run")
        # The bodies, one at a time — a mandate re-added to a single builder
        # would otherwise have to survive only the file-wide greps above.
        for builder, body in prompt_bodies(src).items():
            check("touch-status" not in body,
                  f"{name}: {builder} instructs no touch-status call")
    # The doc half of the same item (sp-02 landed it): monitoring.md documents
    # the deletion instead of the recipe.
    doc = PLUGIN / "shared" / "monitoring" / "monitoring.md"
    if doc.is_file():
        text = doc.read_text(encoding="utf-8")
        check("Do NOT mandate trace calls in agent prompts." in text,
              "monitoring.md no longer mandates the FIRST/LAST pair")
    else:
        skip("monitoring.md absent — the doc half of D-09 not checked")


# --- D-10: the dead Node-API event plumbing is gone
def test_templates_carry_no_dead_node_api():
    """`import('node:…')` throws in this runtime, so the emitters never ran.

    Every `runStatus`/`closeRun`/`publishConfig` call in these templates
    silently no-opped in every real run — 105 dead-import proof lines across 14
    of 28 recorded runs, one of which failed on nothing else. Keeping the
    helpers as "the documented contract" made the templates read as if the
    script emitted its own terminal events, so a maintainer debugging a missing
    badge looked in the wrong file. The deterministic emitters are named
    instead (GD-D5).
    """
    print("test_templates_carry_no_dead_node_api")
    for rel in TEMPLATES:
        src = template_src(rel)
        name = rel.rsplit("/", 1)[-1]
        if src is None:
            check(False, f"{name} is in the payload")
            continue
        # Code only: the header paragraph NAMES `import('node:…')` while
        # explaining why nothing calls it, and that explanation is what keeps
        # it from coming back.
        code = code_only(src)
        check("import('node:" not in code and 'import("node:' not in code,
              f"{name}: no dynamic node: import remains")
        for helper in ("runStatus", "closeRun", "publishConfig"):
            check(f"const {helper} = " not in code,
                  f"{name}: the dead `{helper}` helper is deleted, not kept")
            check(f"{helper}(" not in code,
                  f"{name}: nothing calls `{helper}(` any more")
        # The replacement is a NAME, not a silence: a reader must be able to
        # find who does emit the events.
        for daemon in ("decision_watcher.py", "cycle_reporter.py", "touch-run"):
            check(daemon in src,
                  f"{name}: names {daemon} as a deterministic emitter")
        # The pid-signal half of closeRun went with it — stopping the daemons
        # is `touch-run close`'s, by recorded and /proc-verified pid.
        check("process.kill" not in code and "watcher.pid" not in code,
              f"{name}: the daemon-stop epilogue moved to touch-run close")


# --- GD-D1a: the [monitor] marker is fenced
def test_monitor_marker_is_fenced_at_every_spawn():
    """The one line in these files a token pass may not touch.

    `decision_watcher.py` and `aggregator/agents.py` derive plan/stage/role/
    attempt from this marker with zero LLM cooperation; it is script-authored
    prompt text, so it costs the same whether or not anything reads it — and
    everything reads it. A trimmed or renamed marker drops that agent's whole
    derived history into an unnamed bucket, which is why GD-D1a classifies it
    `no` regardless of how removable it looks.
    """
    print("test_monitor_marker_is_fenced_at_every_spawn")
    field = re.compile(r"^\[monitor\] plan=\S+ stage=\S+ role=\S+ attempt=\S+$")
    for rel in TEMPLATES:
        src = template_src(rel)
        name = rel.rsplit("/", 1)[-1]
        if src is None:
            check(False, f"{name} is in the payload")
            continue
        bodies = prompt_bodies(src)
        check(bool(bodies), f"{name}: prompt builders are discoverable")
        for builder, body in bodies.items():
            lines = [ln for ln in body.splitlines() if ln.startswith("[monitor] ")]
            check(len(lines) == 1,
                  f"{name}: {builder} carries exactly one [monitor] marker "
                  f"(found {len(lines)})")
            for line in lines:
                check(bool(field.match(line)),
                      f"{name}: {builder}'s marker carries all four fields "
                      f"({line[:60]!r})")
                # Line 1 of the prompt: the watcher reads the marker off the
                # transcript's first agent line.
                check(body.splitlines()[1].startswith("[monitor] ")
                      if len(body.splitlines()) > 1 else False,
                      f"{name}: {builder}'s marker is the prompt's first line")


# --- D-12: generic, spec-driven, copied byte-for-byte
def test_templates_are_spec_driven():
    """Per-run values arrive in `args`; the file itself is never edited.

    Measured: 71.8%/63.8% of an adapted copy was verbatim template and the
    re-emission cost 8,313 tokens a run, while the rationale comments survived
    at 12-38% — an LLM re-typing a script it was handed loses exactly the
    footnotes that stop a rule being removed. So the script is `cp`d and the
    driver authors only the spec. The ALL-CAPS strings that remain are
    FALLBACKS a hand-launched copy sees; `touch-run verify` preflights the
    SPEC, never this text, because a byte-for-byte copy carries its defaults
    with it.
    """
    print("test_templates_are_spec_driven")
    for rel in TEMPLATES:
        src = template_src(rel)
        name = rel.rsplit("/", 1)[-1]
        if src is None:
            check(False, f"{name} is in the payload")
            continue
        # `args` may be entirely absent on a runtime that never injected it;
        # a bare identifier reference would be a ReferenceError, not falsy.
        check("typeof args === 'undefined'" in src,
              f"{name}: an absent `args` is handled before it is dereferenced")
        check("typeof args === 'string' ? JSON.parse(args)" in src,
              f"{name}: a JSON-string spec is parsed")
        for key in ("ARGS.project_dir", "ARGS.task", "ARGS.plugin_root",
                    "ARGS.context"):
            check(key in src, f"{name}: reads {key} from the run spec")
        check("TASK_NAME" in src and "'/.touch/local-orchestrators/' + TASK_NAME" in src,
              f"{name}: the task folder is derived from the spec's task name")
        # `net_retries` is a run knob in BOTH templates or in neither: one
        # template honouring a spec key the other silently ignores is a spec a
        # run-spec author cannot reason about.
        check("ARGS.net_retries" in src,
              f"{name}: the infra-retry count comes from the run spec too")
        check("net_retries" in src.split("const ARGS")[0],
              f"{name}: net_retries is listed among the recognized spec keys")
        # `??`, never `||`, on the numeric knobs: a spec value of 0 means 0.
        check("ARGS.net_retries || " not in src,
              f"{name}: a `net_retries: 0` is honoured, not defaulted away")
        # M-1: an incomplete spec REFUSES rather than narrating. Defense in
        # depth under `touch-run verify` — `verify` preflights the spec FILE,
        # this checks what the runtime actually injected — and without it the
        # placeholder `/ABS/PATH/TO/PROJECT` silently becomes a working default
        # that spends a whole opus fan-out on a run that cannot produce
        # anything.
        refusal = re.search(
            r"if \(!ARGS\.project_dir \|\| !ARGS\.task\) \{\s*\n\s*throw new Error\(",
            src)
        check(refusal is not None,
              f"{name}: a spec missing project_dir/task throws, never falls "
              f"back to the placeholder paths")
        first_phase = src.find("\nphase(")
        check(refusal is not None and first_phase != -1
              and refusal.start() < first_phase,
              f"{name}: the refusal happens before the first phase, so nothing "
              f"is spawned against a placeholder project")
    research = template_src(TEMPLATES[0])
    if research:
        for key in ("ARGS.subject", "ARGS.perspectives", "ARGS.min_reports"):
            check(key in research, f"research.workflow.js: reads {key}")
    impl = template_src(TEMPLATES[1])
    if impl:
        for key in ("ARGS.plan_file", "ARGS.parallel", "ARGS.extra_attempts",
                    "ARGS.max_attempts", "ARGS.targeted_test_command",
                    "ARGS.full_suite_command", "ARGS.baseline_notes",
                    "ARGS.review_checklist"):
            check(key in impl, f"implement.workflow.js: reads {key}")
        # SKILLS-7: the per-project half of the spec is a tracked file, and the
        # template says where it is so `touch-run` and `.gitignore` cannot
        # drift from each other silently.
        check(".touch/run.json" in impl,
              "implement.workflow.js: names the tracked per-project constants file")
        for key in ("max_attempts", "finalgate_attempts"):
            check(f"ARGS.{key} || " not in impl,
                  f"implement.workflow.js: a `{key}: 0` is honoured, not "
                  f"defaulted away")


#: An ALL-CAPS `/ABS/PATH/...` literal is legitimate only as the right-hand
#: fallback of a spec read (`ARGS.x || '/ABS/PATH/…'`) — a bare
#: `const PROJECT_DIR = '/ABS/PATH/TO/PROJECT'` is a re-introduced fill-in slot
#: wearing a default's clothes.
UNGUARDED_ABS = re.compile(r"^const \w+ = '/ABS/PATH/", re.M)


def test_templates_need_no_substitution():
    """`orch-scripts/` copies are a pure `cp` — no substitution step exists.

    This is the property that makes D-12's preflight and GD-M8.5's provenance
    digest meaningful: if adapting a template ever needed an edit, a copy's
    sha256 would say nothing about which template it came from, and the
    "5 of 13 copies shipped without agentR" defect would have no detector.

    Asserted as the real property, not as `shutil.copyfile` producing identical
    bytes — that arm was true by construction and tested the stdlib. What can
    actually regress is the file growing a value only an editor could supply:
    a `FILL IN` marker, an angle-bracket slot, or an ALL-CAPS `/ABS/PATH/…`
    literal that is a bare `const` rather than a spec fallback.
    """
    print("test_templates_need_no_substitution")
    for rel in TEMPLATES:
        src = template_src(rel)
        name = rel.rsplit("/", 1)[-1]
        if src is None:
            check(False, f"{name} is in the payload")
            continue
        check("FILL IN" not in src,
              f"{name}: no `FILL IN` instruction to act on before launching")
        code = code_only(src)
        bare = UNGUARDED_ABS.findall(code)
        check(not bare,
              f"{name}: every /ABS/PATH literal is a spec fallback, never a "
              f"bare constant ({bare})")
        # And each one that IS a fallback sits behind an `ARGS.` read on the
        # same line, so the value a run uses always comes from the spec.
        for line in code.splitlines():
            if "'/ABS/PATH/" in line:
                check("ARGS." in line,
                      f"{name}: `{line.strip()[:70]}` reads the spec first")


def wrap_as_workflow(src, epilogue):
    """`src` as the async-function body the workflow runtime evaluates.

    The runtime evaluates a template as the body of an async function (top-level
    `await`, an injected `args`/`log`/`phase`/`agent`/`parallel`), so a bare
    `node --check` on the file rejects the `return` and the top-level await for
    reasons that are not defects. `args` is deliberately left UNDEFINED here —
    the hardest shape a copied template can be launched in, and the one the
    D-12 refusal exists for.
    """
    return (
        "const args = undefined; const log = () => {}; "
        "const phase = () => {}; const agent = async () => null; "
        "const parallel = async () => [];\n"
        "async function __wf() {\n"
        + re.sub(r"^export const meta", "const meta", src, flags=re.M)
        + "\n}\n" + epilogue)


def test_templates_parse_as_javascript():
    """A node syntax check, in the shape the runtime evaluates them."""
    print("test_templates_parse_as_javascript")
    if shutil.which("node") is None:
        skip("`node` not on PATH — template syntax check not run")
        return
    tmp = tempfile.mkdtemp(prefix="touch-syntax-")
    try:
        for rel in TEMPLATES:
            src = template_src(rel)
            name = rel.rsplit("/", 1)[-1]
            if src is None:
                check(False, f"{name} is in the payload")
                continue
            target = Path(tmp) / (name + ".mjs")
            target.write_text(wrap_as_workflow(src, "void __wf\n"),
                              encoding="utf-8")
            res = subprocess.run(["node", "--check", str(target)],
                                 capture_output=True, text=True)
            check(res.returncode == 0,
                  f"{name}: parses as the async body the runtime evaluates "
                  f"({res.stderr.strip()[:200]})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_templates_refuse_an_incomplete_spec():
    """Launched with no spec, a template REFUSES — behaviourally, in node.

    The source assertion in `test_templates_are_spec_driven` pins the shape;
    this one runs it. With `args` undefined every ALL-CAPS default engages at
    once: `PROJECT_DIR` becomes the literal `/ABS/PATH/TO/PROJECT`, the board
    becomes the two placeholder perspectives, and the very next statement used
    to be a full opus fan-out whose required findings writes cannot land. A
    `log()` line scrolling past is not a defence against that; a throw is. The
    stub `agent` returns null, so if the refusal ever regressed to a warning
    this test would see the run continue into `agentR` instead — which is
    exactly the distinction being pinned.
    """
    print("test_templates_refuse_an_incomplete_spec")
    if shutil.which("node") is None:
        skip("`node` not on PATH — the spec refusal not executed")
        return
    epilogue = ("__wf().then(() => console.log('OUTCOME: no-refusal'),\n"
                "            (e) => console.log('OUTCOME: threw: ' + e.message))\n")
    tmp = tempfile.mkdtemp(prefix="touch-spec-")
    try:
        for rel in TEMPLATES:
            src = template_src(rel)
            name = rel.rsplit("/", 1)[-1]
            if src is None:
                check(False, f"{name} is in the payload")
                continue
            target = Path(tmp) / (name + ".run.mjs")
            target.write_text(wrap_as_workflow(src, epilogue), encoding="utf-8")
            res = subprocess.run(["node", str(target)],
                                 capture_output=True, text=True, timeout=60)
            out = res.stdout.strip()
            check("OUTCOME: threw: run-spec incomplete" in out,
                  f"{name}: an absent run spec refuses the launch "
                  f"({out[:140]!r}{res.stderr.strip()[:140]!r})")
            check("no-refusal" not in out,
                  f"{name}: it does not fall through to the placeholder paths")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- D-23: the divider emits one plan slice per sub-plan
def test_divider_emits_plan_slices():
    """The downstream agents read a slice, not the whole plan.

    Measured on one run: 75.8 K tokens of plan read 11 times by 7 agents. The
    slice carries the global-decisions header verbatim plus only that
    sub-plan's items, so the comprehension win doubles as an isolation win — an
    implementer that never read another sub-plan's items cannot drive-by-fix
    another sub-plan's file.
    """
    print("test_divider_emits_plan_slices")
    src = template_src(TEMPLATES[1])
    if src is None:
        check(False, "implement.workflow.js is in the payload")
        return
    check("const sliceFile = " in src,
          "the template derives a slice path per sub-plan")
    check("'slice_file'" in src and "slice_file: { type: 'string' }" in src,
          "the divider schema requires a slice_file per sub-plan")
    bodies = prompt_bodies(src)
    divide = bodies.get("dividePrompt", "")
    check("-subplan-" in divide and "slice_file" in divide,
          "the divider prompt requires one slice file per sub-plan")
    check("VERBATIM" in divide or "verbatim" in divide,
          "the divider prompt requires the global decisions in every slice")
    for builder in ("implPrompt", "gatePrompt", "critPrompt"):
        body = bodies.get(builder)
        if body is None:
            check(False, f"{builder} exists to be checked")
            continue
        check("sliceFile(sp)" in body,
              f"{builder} names the sub-plan's slice")
        check("${PLAN_FILE}" not in body,
              f"{builder} does not hand the agent the whole plan")
        check("${SUBPLANS_FILE}" not in body,
              f"{builder} does not hand the agent the whole partition file")
    # The one deliberate exception: a cross-file integration slip is by
    # definition outside any single slice, so the final-gate fixer reads the
    # plan's global decisions.
    check("${PLAN_FILE}" in bodies.get("finalFixPrompt", ""),
          "the final-gate fixer still reads the plan's global decisions")
    # No slice, no loop: a missing slice would silently degrade every
    # downstream agent to "read whatever you can find".
    check("sliceless" in src,
          "a sub-plan without a slice stops the run instead of degrading it")
    # That guard can only check the FIELD — this runtime has no filesystem, so
    # a divider that returns a path it never wrote passes it. The implementer
    # is the first party that can see the file, so it is told to refuse rather
    # than reconstruct its scope from the plan; the refusal rides to the next
    # attempt through openNotes instead of dying with the agent.
    impl_body = bodies.get("implPrompt", "")
    check("does not exist" in impl_body and "done=false" in impl_body,
          "an implementer handed a nonexistent slice refuses instead of guessing")


# --- D-24: prompt trims, with the marker fenced
def test_prompts_are_trimmed_but_still_complete():
    """The recap goes, the Method paragraph goes, the read line arrives.

    The `Return structured output only: …` recap restated a schema that
    `agent(..., {schema})` already enforces; the `Method:` paragraph never
    varied per perspective and lives in the SKILL.md now (sp-09's half). The
    one ADDITION is a single read-discipline line, and the evidence for it is
    both halves of the measurement — Bash carried 50.6% of tool-result volume
    on 16,786 calls against Read's 3,005 (5.6:1). Citing the share alone reads
    as "Bash is chatty"; citing both says most file reading goes through Bash.
    """
    print("test_prompts_are_trimmed_but_still_complete")
    for rel in TEMPLATES:
        src = template_src(rel)
        name = rel.rsplit("/", 1)[-1]
        if src is None:
            check(False, f"{name} is in the payload")
            continue
        check("Return structured output only" not in src,
              f"{name}: the schema recap line is gone from every prompt")
        check("const READ_DISCIPLINE" in src,
              f"{name}: the read-discipline preamble is declared once")
        check("offset/limit" in src,
              f"{name}: the read line names the windowing it asks for")
        check("16,786" in src and "3,005" in src and "5.6:1" in src,
              f"{name}: the read-discipline evidence cites calls, not only volume")
        check("50.6%" in src,
              f"{name}: ...and the volume share it corrects")
        for builder, body in prompt_bodies(src).items():
            check("READ_DISCIPLINE" in body or "CONTEXT" in body,
                  f"{name}: {builder} carries the shared preamble")
    research = template_src(TEMPLATES[0])
    if research:
        check("Method: study the subject" not in research,
              "research: the invariant Method paragraph is hoisted out of the prompts")
        check("subjectFor" in research and "p.subject" in research,
              "research: a perspective may scope the SUBJECT block to itself")


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
    as stages of one workflow ("the plan `research` produced"), which
    is prose about a hand-off, not an instruction to invoke. Scoped to
    BACKTICKED names deliberately too: three skill names (`research`,
    `implement`, `monitor`) are ordinary English words, so only the code-span
    forms — `<name>` or `<name>/...` — can be read as a reference at all;
    unbackticked prose is left alone.
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
                    if re.search(rf"`{re.escape(other)}(?:`|/)", line)]
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
              test_templates_guard_every_spawn,
              test_templates_carry_no_status_mandate,
              test_templates_carry_no_dead_node_api,
              test_monitor_marker_is_fenced_at_every_spawn,
              test_templates_are_spec_driven,
              test_templates_need_no_substitution,
              test_templates_parse_as_javascript,
              test_templates_refuse_an_incomplete_spec,
              test_divider_emits_plan_slices,
              test_prompts_are_trimmed_but_still_complete,
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
