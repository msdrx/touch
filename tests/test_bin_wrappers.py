#!/usr/bin/env python3
"""The six `plugin/touch/bin/` wrappers: self-location, posture, dispatch.

Item 08 (CM-5 / probe C2, PLUGIN-SPEC-5, DISTRIBUTION-4, CM-13). Run as
`python3 test_bin_wrappers.py`; exits non-zero on failure. No pytest, no
runner — `run_all.sh` picks it up by its `test_*.py` glob.

WHAT THIS FILE IS FOR
---------------------
`bin/` is the payload's only PATH surface: the harness puts it on `$PATH` and
the skills call the commands by bare name, so a wrapper that resolves the wrong
directory does not fail at build time — it fails on a stranger's machine, at
the first status call, with a message nobody reads. Three measured facts shape
every assertion below.

1. **A `bin/` process gets none of the plugin environment.** Probe C2/P10:
   the plugin root, the plugin data dir and the project directory are all EMPTY
   there, while `$0` is absolute and `$PWD` is the user's project (GD-T4). So
   the wrappers self-locate from `$0` and read no plugin variable at all —
   asserted here with a zero-tolerance grep over the WHOLE file, comments
   included. That is why the wrappers name those variables in prose rather than
   spelling them: a gate with no exception list cannot be argued with, and
   PLUGIN-SPEC-5 asked for exactly this grep.

2. **A wrapper never opens a network port for the user (GD-T8)** — and the
   request has more than one spelling. `aggregator/server.py` resolves `--open`
   and `--host <addr>` in the same statement, and `monitor_server.py` resolves
   its bind as `--open` > `$ORCH_BIND` > loopback, so a guard that knew only
   `--open` would leave the longer spelling (and an *inherited* environment
   variable nobody typed) walking straight past it. All three are refused and
   all three are checked by running them; the four wrappers that front no
   listener are checked by reading the source, which must not mention the flag
   at all.

   The refusal scan is not a blanket search for the word: `touch-serve` mirrors
   the module's own parser and skips value positions (`--allow-origin --open`
   is an origin string to `server.py`, so refusing it would block a command the
   module accepts safely), while `touch-monitor` scans every word because the
   daemon's own `resolve_host` tests for literal membership anywhere in argv.
   Both behaviours are asserted in the value position — the security-relevant
   one — because "matches what the callee does" is the only definition of
   correct here, and a divergence in either direction is a divergence.

3. **The user's project may contain its own `aggregator/`.** For `python3 -m`
   and `python3 -c` the cwd sits FIRST on `sys.path`, ahead of every
   `PYTHONPATH` entry, so `touch-serve` drops the cwd and then verifies where
   the package actually came from. Both arms are exercised: the guard stays
   quiet with a decoy in the cwd, and it fires when a decoy really does win the
   import.

4. **Nothing is resolved twice.** The tasks root has exactly one resolver per
   callee (`status.sh`'s bash rungs, the daemons' `resolve_tasks_root`). A
   wrapper that recomputed it and exported the answer would pin the callee's
   FIRST rung, so a later correction to that resolver could never reach anyone
   who came in through `bin/`. The absence of that export is asserted.

5. **A dispatch assertion must name something only the callee can say.**
   `python3 <path that does not exist>` prints `can't open file '<path>'` — and
   that text quotes the path the wrapper just passed, so it contains the module
   name. "The module name appears in stderr" is therefore satisfied by a payload
   with no `shared/monitoring/` in it at all, which is exactly the failure a
   self-location test exists to catch (`shared/monitoring/` vs
   `.claude/shared/monitoring/` — one directory wrong, no build-time symptom).
   Every dispatch arm below asserts the daemon's OWN sentence and forbids
   `can't open file`, and a payload with the daemons deleted is constructed and
   run to prove the two really do differ.

6. **One wrapper WRITES, and only when asked.** `touch-selfcheck --init` is the
   memory-relocation mechanism (G1/G12: a mode on an existing wrapper, never a
   seventh program). It merges ONE key into `.claude/settings.local.json` and
   nothing else, so the arms below assert the shape of that write from both
   sides: the key is absolute (a relative or `$VAR` value is dropped by the CLI
   in silence, which is the entire reason a program writes it), unrelated keys
   in the file survive, the committed `.claude/settings.json` is never created
   or touched (GD-C1), a corrupt file is refused rather than replaced, `$HOME`
   and the CLI configuration directory are refused as project roots (`~/.claude`
   is a read-only tap, PROTOCOL-7), a symlinked settings file is refused rather
   than replaced, a project inside the plugin directory is refused (it is a
   version-stamped cache that gets swept), a **symlinked `.touch/` or
   `.touch/memory`** is refused rather than followed (it puts the memory tree
   outside the project, `~/.claude` included, and git does not descend one — so
   the tracked `.touch/memory/*.md` carve would match nothing), a **linked
   worktree** is refused before anything is written (the CLI shares the primary
   checkout's tree, Touch serves this one, and no command run from here makes
   them agree), a second `--init` over a populated memory tree changes no byte of
   it (I9 gives the mechanism to a program and the CONTENT to a human), a
   DIFFERENT existing mapping is replaced only with the old value printed (a
   mapping nobody can see is the failure this mode exists for, so silently
   dropping a deliberate one repeats it), and the DEFAULT mode still writes
   nothing at all. Every refusal arm asserts the filesystem as well as the
   sentence: "nothing was written" is a claim, and this is the file that checks
   it.

   And the two modes are asserted to be ONE rule: for every project `--init`
   refuses, `one_rule()` compares its refusal sentence with the default report's
   "`touch-selfcheck --init` would refuse … because …" and requires them to be
   the same string, then requires the bare "maps it to X" hint to be absent.
   Three states used to be advertised as fixable by a command that refuses them
   — a linked worktree, a `$HOME` project, a project inside the plugin cache —
   and for the worktree the report went further and stated the sharing
   capability the implementation had decided not to offer. A gate added to the
   writer alone now fails these arms instead of shipping.

   The memory check in the default report is asserted in both directions too:
   green for an unmapped project, for a mapping that WINS over an inert lower
   layer and for an unreadable layer it outranks; red — naming the file, the
   directory or the variable — for every way a mapping can be present and inert,
   for a layer that outranks the winner and cannot be read (reported as
   unverifiable, never as "not honoured": an unparseable file is not a mapping),
   for a mapping whose directory escapes the project, and for the one where the
   write root and the served root diverge in a linked worktree.
   `primary_checkout()` gets its own arm per git shape (linked worktree /
   submodule / bare repository / unrecognised `.git`), built from two plain files,
   because a payload installed from an archive is exactly the machine with no
   `git` to shell out to.

Platform assumptions the wrappers make, pinned here as source text because no
CI in this sandbox can execute them: `#!/usr/bin/env bash` (bash is not at
`/bin/bash` everywhere) and `${1+"$@"}` in place of a bare `"$@"` — bash before
4.4 treats `"$@"` as an *unset variable* under `set -u` when there are no
positional parameters, and stock macOS ships `/bin/bash` 3.2.57, which
`env bash` finds on a machine without Homebrew. Several of these commands are
normally run with no arguments at all, so that is the common path, not a corner.
`readlink -f` is the third assumption (GNU/BSD; absent on macOS before 12.3) and
is the one this file does NOT relax: GD-T4 mandates the idiom verbatim, so
changing it is a plan amendment rather than a test fix. It also fixes the macOS
floor at 12.3 — a version whose `mktemp` is still BSD, which takes
`[-d] [-q] [-t prefix] [-u] template …` and rejects a bare `mktemp -d` as a
usage error. So the templated form is the fourth pinned assumption.

Exec bits are asserted here on the working tree AND, when this is a git
checkout, in the index — `git archive` is what builds the payload, and it
copies the mode git recorded, not the mode on disk. Both are cheap and the
failure they catch (CM-13: a mode dropped in a zip round trip, every status
call dying with "permission denied" on stderr nobody sees) is silent. Item 10's
staged-tree gate owns the third place a mode can be lost — inside the built
archive — and should assert THAT, not a third copy of either check here.

Every arm runs from a FOREIGN cwd. Running these from the repo root would let a
relative path pass by luck, which is the whole bug class this file exists for.
"""
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "plugin" / "touch" / "bin"

#: The six, in the order the plan names them.
WRAPPERS = ("touch-serve", "touch-status", "touch-monitor", "touch-watcher",
            "touch-cycle-reporter", "touch-selfcheck")

#: GD-T4's measured-empty set. Zero tolerance, comments included — see the
#: module docstring. `CLAUDE_PLUGIN_OPTION_*` is deliberately absent: that one
#: reaches the *hook* process, which is not this file's business.
FORBIDDEN_ENV = ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_DATA")

#: The self-location idiom the plan mandates, verbatim. `$0` is absolute in a
#: bin/ launch and `readlink -f` resolves the symlink a package manager may
#: have put on PATH; anything else here is a guess. (`readlink -f` is GNU/BSD
#: and absent on macOS before 12.3. The idiom is mandated verbatim by GD-T4 and
#: asserted verbatim here, so changing it is a plan amendment, not a test fix —
#: recorded so the next reader knows it was seen, not missed.)
SELF_LOCATE = 'ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"'

#: GD-21 on the payload's shell half: bash and python3, nothing else. `jq` is a
#: status-line-only exception in this repo and is not licensed here.
FORBIDDEN_TOOLS = ("jq", "node", "npx", "perl", "ruby", "python2")

#: The sentence each daemon prints when it loads, resolves nothing and gives up
#: — `monitor_server.py:162`, `decision_watcher.py:121`. Wrapper → sentence,
#: because the sentence is the ONLY evidence that the file the wrapper named was
#: actually opened and executed: python3's own "can't open file '<path>'"
#: contains the module name too (it quotes the path it was handed), so matching
#: the name proves nothing about dispatch. See docstring point 5.
DAEMON_REFUSAL = {
    "touch-monitor": "monitor_server: no task state dir found",
    "touch-watcher": "decision_watcher: no task state dir found",
}

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


def source(name):
    return (BIN / name).read_text(encoding="utf-8")


def code_lines(name):
    """Source with whole-line comments removed — for "is it *done*" checks.

    Only used where a comment legitimately quotes the thing being looked for
    (the `--open` refusal message explains the flag it refuses). The
    environment grep in :func:`test_no_plugin_environment` deliberately does
    NOT use this.
    """
    return [ln for ln in source(name).splitlines()
            if not ln.lstrip().startswith("#")]


def run(args, cwd, env=None, timeout=60, stdin_text=""):
    """CompletedProcess from a foreign cwd, or None if it would not run."""
    base = dict(os.environ)
    # These leak in from the orchestration run that is executing this test and
    # would hand the wrappers a state dir they were supposed to resolve.
    # PYTHONPATH is popped for the same reason, from the other side: an
    # inherited one would decide the import arms below instead of the wrapper.
    #
    # `CLAUDE_PROJECT_DIR` is popped too, and it is the subtle one: no wrapper
    # reads it (that is asserted separately), but every CALLEE does — it is rung
    # 2 of `status.sh`'s and both daemons' resolvers. Running this suite from
    # inside a Claude Code session would otherwise hand them the dev repo as the
    # project, and the "nothing resolves, so refuse" arms below would silently
    # become "resolved to the repo, so write there" arms.
    #
    # The three memory variables are popped for the third reason: they are
    # UNDOCUMENTED overrides that outrank every settings layer, so an inherited
    # one would make `touch-selfcheck`s memory check report red — correctly, and
    # about the environment this suite runs in rather than about the wrapper.
    # The arm that sets one on purpose passes it through `env`.
    for var in ("ORCH_STATE_DIR", "ORCH_TASKS_ROOT", "ORCH_WF_DIR", "ORCH_PORT",
                "ORCH_BIND", "TOUCH_STATE_DIR", "TOUCH_PROJECT_CWD",
                "CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA",
                "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE",
                "CLAUDE_CODE_REMOTE_MEMORY_DIR", "CLAUDE_MEMORY_STORES",
                "PYTHONPATH"):
        base.pop(var, None)
    base.update(env or {})
    try:
        return subprocess.run([str(a) for a in args], cwd=str(cwd), env=base,
                              capture_output=True, text=True, timeout=timeout,
                              input=stdin_text)
    except (OSError, subprocess.SubprocessError) as exc:
        check(False, f"{args[0]} runs ({exc})")
        return None


def have_git():
    try:
        res = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


# --------------------------------------------------------------------------
# source-text arms
# --------------------------------------------------------------------------
def test_files_and_modes():
    print("test_files_and_modes")
    check(BIN.is_dir(), "plugin/touch/bin/ exists")
    if not BIN.is_dir():
        return
    for name in WRAPPERS:
        path = BIN / name
        check(path.is_file(), f"bin/{name} exists")
        if not path.is_file():
            continue
        check(bool(path.stat().st_mode & stat.S_IXUSR), f"bin/{name} is executable")
        # `or [""]`: a truncated checkout or a failed `cp` leaves a zero-byte
        # file, and indexing an empty list would abort the whole run with a
        # traceback instead of failing the shebang check it is standing in.
        first = (source(name).splitlines() or [""])[0]
        # `#!/usr/bin/env bash`, not `#!/bin/bash`: bash is not at /bin/bash on
        # every platform the CLI runs on, and CM-13(c) wants the wrapper itself
        # to be the thing that runs, so a mode regression fails loudly.
        check(first == "#!/usr/bin/env bash",
              f"bin/{name} starts with the env-bash shebang (got {first!r})")
        res = run(["bash", "-n", path], cwd=REPO)
        if res is not None:
            check(res.returncode == 0,
                  f"bin/{name} parses (bash -n: {res.stderr.strip()[:200]})")
    strays = sorted(p.name for p in BIN.iterdir() if p.name not in WRAPPERS)
    # A non-executable helper in bin/ would break item 10's "everything under
    # bin/ is executable" rule, and a shared prelude file is exactly the thing
    # someone reaches for when six wrappers repeat four lines. They repeat them
    # on purpose: each wrapper must be independently runnable.
    check(not strays, f"bin/ holds only the six wrappers (strays: {strays})")


def test_no_plugin_environment():
    print("test_no_plugin_environment")
    for name in WRAPPERS:
        text = source(name)
        hits = [var for var in FORBIDDEN_ENV if var in text]
        # PLUGIN-SPEC-5's test, stated as it was recommended: grep bin/* and
        # fail if the name appears. The wrappers describe these variables in
        # prose precisely so this can stay absolute.
        check(not hits,
              f"bin/{name} names no plugin environment variable (found: {hits}) — "
              f"they are all EMPTY in bin/ (GD-T4)")
        check(SELF_LOCATE in text,
              f"bin/{name} self-locates with the mandated readlink -f idiom")
        check("PYTHONDONTWRITEBYTECODE=1" in text,
              f"bin/{name} sets PYTHONDONTWRITEBYTECODE (no .pyc in the cache)")
        check("python3 not found on PATH" in text,
              f"bin/{name} reports a missing python3 in one line")


def test_portable_argument_forwarding():
    print("test_portable_argument_forwarding")
    # Every wrapper runs under `set -u`, and bash before 4.4 treats `"$@"` as an
    # unset variable there when `$#` is 0 — stock macOS `/bin/bash` is 3.2.57,
    # and `#!/usr/bin/env bash` (chosen in these files FOR portability) finds it
    # on a machine without Homebrew. `touch-monitor`, `touch-watcher` and
    # `touch-selfcheck` are normally invoked with no arguments at all, so the
    # break would be immediate and total: "unbound variable" instead of a
    # dashboard. This sandbox has only bash 5.2, so the shape is pinned as
    # source text — the one form of evidence available here.
    for name in WRAPPERS:
        text = source(name)
        code = "\n".join(code_lines(name))
        check('set -eu' in text or 'set -u' in text,
              f"bin/{name} runs under set -u (an unset variable is a bug, not a "
              f"default)")
        check('"$@"' not in code.replace('${1+"$@"}', ""),
              f"bin/{name} forwards arguments as ${{1+\"$@\"}}, never a bare "
              f'"$@" (bash < 4.4 under set -u)')


def test_portable_coreutils_usage():
    print("test_portable_coreutils_usage")
    # `mktemp` is the coreutil whose two implementations disagree about the
    # ARGUMENT, not about existence: BSD (stock macOS) is
    # `mktemp [-d] [-q] [-t prefix] [-u] template …` and a template-less call is
    # a usage error there — under `set -eu` that kills the wrapper at that line,
    # before any output it was about to produce. GNU accepts both spellings, so
    # this sandbox cannot execute the difference; it is pinned as source text
    # like the shebang and `${1+"$@"}` above. macOS is in scope by construction:
    # `readlink -f` already puts the floor at 12.3, whose `mktemp` is BSD.
    # `tests/run_all.sh` uses the portable form already.
    for name in WRAPPERS:
        bad = [ln.strip() for ln in code_lines(name)
               if "mktemp" in ln and "XXXXXX" not in ln]
        check(not bad,
              f"bin/{name} passes mktemp a template, never a bare `mktemp -d` "
              f"(BSD/macOS rejects that): {bad}")


def test_no_foreign_tooling():
    print("test_no_foreign_tooling")
    for name in WRAPPERS:
        code = "\n".join(code_lines(name))
        # Word boundaries, not "name followed by a space": `jq)`, `jq|`, "`jq`"
        # and `$(jq …` are all how a shell script actually calls the thing, and
        # every one of them slips past a two-character check.
        hits = [t for t in FORBIDDEN_TOOLS
                if re.search(rf"(^|[^\w-]){re.escape(t)}([^\w-]|$)", code)]
        check(not hits,
              f"bin/{name} shells out to nothing but bash/python3 (found: {hits}) — "
              f"GD-21")


def test_no_duplicated_resolution():
    print("test_no_duplicated_resolution")
    for name in WRAPPERS:
        code = "\n".join(code_lines(name))
        # Exporting a tasks root would override rung 1 of the callee's own
        # resolver — including the `$HOME` skip `aggregator/paths.py` already
        # has and the bash/py monitoring resolvers do not yet. A wrapper is the
        # wrong place to freeze that answer; it is the callee's to fix.
        check("ORCH_TASKS_ROOT=" not in code,
              f"bin/{name} does not resolve or export the tasks root itself — "
              f"one resolver, in the callee")


def test_open_is_never_passed_through():
    print("test_open_is_never_passed_through")
    # The two that front a listener refuse it; the other four have no reason to
    # know the flag exists, so its literal absence is the assertion.
    for name in ("touch-status", "touch-watcher", "touch-cycle-reporter",
                 "touch-selfcheck"):
        check("--open" not in source(name), f"bin/{name} never mentions --open")
    for name in ("touch-serve", "touch-monitor"):
        code = "\n".join(code_lines(name))
        check("refusing to forward --open" in code,
              f"bin/{name} refuses an explicit --open (GD-T8)")
    # The all-interfaces literal appears nowhere, not even in a comment or a
    # printed escape hatch: a wrapper that names it is one edit away from
    # passing it.
    for name in WRAPPERS:
        check("0.0.0.0" not in source(name),
              f"bin/{name} contains no 0.0.0.0 bind literal (GD-T8)")
    # The other two spellings of "bind something reachable", each guarded in the
    # file that fronts the program which honours it.
    serve = "\n".join(code_lines("touch-serve"))
    check("--host" in serve and "refusing --host" in serve,
          "bin/touch-serve guards --host, not just --open (server.py resolves "
          "both into one bind address)")
    monitor = "\n".join(code_lines("touch-monitor"))
    check("ORCH_BIND" in monitor,
          "bin/touch-monitor guards $ORCH_BIND (monitor_server.py resolves "
          "--open > $ORCH_BIND > loopback)")


# --------------------------------------------------------------------------
# behaviour arms — every one of them from a foreign cwd
# --------------------------------------------------------------------------
def test_status_writes_an_event(tmp):
    print("test_status_writes_an_event")
    cwd = tmp / "elsewhere"
    state = tmp / "task"
    cwd.mkdir()
    state.mkdir()
    res = run([BIN / "touch-status", "sp-probe", "implement", "running",
               "wrapper probe"], cwd=cwd, env={"ORCH_STATE_DIR": str(state)})
    if res is None:
        return
    check(res.returncode == 0,
          f"touch-status exits 0 from a foreign cwd (rc={res.returncode}, "
          f"{res.stderr.strip()[:200]})")
    stream = state / "events.jsonl"
    check(stream.is_file(), "touch-status created the task's events.jsonl")
    if not stream.is_file():
        return
    try:
        event = json.loads(stream.read_text(encoding="utf-8").splitlines()[-1])
    except (ValueError, IndexError) as exc:
        check(False, f"the appended line is one JSON event ({exc})")
        return
    check(event.get("plan") == "sp-probe" and event.get("stage") == "implement",
          f"the event carries the plan/stage it was given ({event})")
    # `status.sh` takes `detail` as ${*} after `shift 3`: a wrapper that lost
    # the quoting would store "wrapper" and drop "probe".
    check(event.get("detail") == "wrapper probe",
          f"a multi-word detail survives the wrapper unsplit ({event.get('detail')!r})")


def test_status_without_a_state_dir(tmp):
    print("test_status_without_a_state_dir")
    # The arm above always names the task folder. This one names nothing, from a
    # directory with no `.claude/` anywhere above it — the case where a resolver
    # that guessed would write into the plugin root, which is a version-stamped
    # cache that gets swept (GD-T5). `status.sh` must refuse instead, and the
    # payload-snapshot arm in main() is watching while it does.
    cwd = tmp / "no-project-above"
    cwd.mkdir()
    res = run([BIN / "touch-status", "sp-probe", "implement", "running", "x"],
              cwd=cwd)
    if res is None:
        return
    check(res.returncode != 0,
          f"touch-status refuses when no task folder resolves "
          f"(rc={res.returncode}, {res.stdout.strip()[:160]})")
    lines = [ln for ln in res.stderr.splitlines() if ln.strip()]
    check(len(lines) == 1 and "ORCH_STATE_DIR" in lines[0],
          f"and says so in one line naming the variable to set ({lines})")
    check("Traceback" not in res.stderr,
          "with a message, not a traceback")


def test_serve_runs_and_guards_shadowing(tmp):
    print("test_serve_runs_and_guards_shadowing")
    # A decoy `aggregator/` in the cwd — the normal case on a user's machine,
    # where the session cwd is their own project.
    cwd = tmp / "project-with-decoy"
    (cwd / "aggregator").mkdir(parents=True)
    (cwd / "aggregator" / "__init__.py").write_text(
        'raise SystemExit("DECOY AGGREGATOR IMPORTED")\n', encoding="utf-8")
    res = run([BIN / "touch-serve", "--help"], cwd=cwd)
    if res is None:
        return
    out = res.stdout + res.stderr
    check(res.returncode == 0,
          f"touch-serve --help works with a decoy aggregator/ in the cwd "
          f"(rc={res.returncode}, {out.strip()[:200]})")
    check("DECOY" not in out, "the cwd's aggregator/ was not imported")
    check("--allow-origin" in out, "the real module's usage was printed")

    # And the guard itself: a plugin root with no aggregator/ plus a decoy that
    # really does win the import. Without the assertion this would start a
    # server out of a stranger's package.
    fake = tmp / "fake-plugin"
    (fake / ".claude-plugin").mkdir(parents=True)
    (fake / ".claude-plugin" / "plugin.json").write_text("{}\n", encoding="utf-8")
    (fake / "bin").mkdir()
    shutil.copy2(BIN / "touch-serve", fake / "bin" / "touch-serve")
    decoy = tmp / "decoy-path"
    (decoy / "aggregator").mkdir(parents=True)
    (decoy / "aggregator" / "__init__.py").write_text(
        '__version__ = "decoy"\n', encoding="utf-8")
    res = run([fake / "bin" / "touch-serve", "--help"], cwd=tmp,
              env={"PYTHONPATH": str(decoy)})
    if res is None:
        return
    check(res.returncode != 0,
          f"the shadowing guard refuses a foreign aggregator (rc={res.returncode})")
    check("refusing to run a foreign aggregator" in res.stderr,
          f"the refusal says what it refused ({res.stderr.strip()[:200]})")

    # Same fake root, nothing to import at all: a partial unzip or a build that
    # dropped the package. The one thing the user can act on is which directory
    # is missing, so it is named — and a raw ImportError traceback is exactly
    # what the two daemon arms already forbid.
    res = run([fake / "bin" / "touch-serve", "--help"], cwd=tmp)
    if res is None:
        return
    check(res.returncode != 0 and "no aggregator package at" in res.stderr,
          f"a payload with no aggregator/ says so (rc={res.returncode}, "
          f"{res.stderr.strip()[:200]})")
    check("Traceback" not in res.stderr,
          "and says it with a message, not a traceback")


def test_open_is_refused(tmp):
    print("test_open_is_refused")
    for name in ("touch-serve", "touch-monitor"):
        res = run([BIN / name, "--open"], cwd=tmp)
        if res is None:
            continue
        check(res.returncode != 0,
              f"{name} --open exits non-zero (rc={res.returncode})")
        check("refusing to forward --open" in res.stderr,
              f"{name} says why it refused ({res.stderr.strip()[:160]})")
        # The point of refusing is that nothing bound. A wrapper that printed a
        # warning and served anyway would pass the two checks above.
        check("listening" not in (res.stdout + res.stderr).lower(),
              f"{name} started no listener")


def free_port():
    """A port nothing is listening on right now — good enough for a negative."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_bind_requests_are_refused(tmp):
    print("test_bind_requests_are_refused")
    # `--host <public>` is the same request as `--open` (server.py:
    # `if arg == "--open": host = OPEN_HOST elif arg == "--host" and argv:
    # host = argv.pop(0)`), so it must die the same way. The strongest part of
    # this arm is that it TERMINATES: a wrapper that forwarded the flag would
    # start a server, block, and be killed by the timeout — which `run` reports
    # as a failure.
    port = free_port()
    res = run([BIN / "touch-serve", "--host", "0.0.0.0", "--port", port],
              cwd=tmp, timeout=45)
    if res is not None:
        check(res.returncode != 0,
              f"touch-serve --host 0.0.0.0 exits non-zero (rc={res.returncode})")
        check("refusing --host" in res.stderr,
              f"it says which flag it refused ({res.stderr.strip()[:160]})")
        with socket.socket() as probe:
            probe.settimeout(2)
            bound = probe.connect_ex(("127.0.0.1", port)) == 0
        check(not bound, f"nothing is listening on 127.0.0.1:{port} afterwards")

    # The joined spelling the current parser does not accept — guarded anyway,
    # because a guard a parser change can silently open is not a guard.
    res = run([BIN / "touch-serve", "--host=0.0.0.0"], cwd=tmp, timeout=45)
    if res is not None:
        check(res.returncode != 0 and "refusing --host" in res.stderr,
              f"touch-serve --host=0.0.0.0 is refused too (rc={res.returncode}, "
              f"{res.stderr.strip()[:120]})")

    # ...and the commands that must still work, or the guard has simply become
    # "refuse everything with a flag in it". All four loopback spellings, plus a
    # one-character value that must NOT pass: in a `case` pattern `[::1]` is a
    # bracket expression unless it is quoted, and an unquoted one would match a
    # single `:` or `1` instead of the address it was meant to name.
    #
    # The hostname family is the other way a `case` pattern is looser than it
    # reads: the allowlist arm is a GLOB, so any `127.…*` spelling admits a
    # NAME beginning that way, which getaddrinfo resolves to whatever DNS says —
    # a public bind let through by a loopback-only list. The class is probed at
    # three widths on purpose, because narrowing the glob kills witnesses one at
    # a time while leaving the class open: `127.evil.example` dies to
    # `127.[0-9]*`, `127.9foo.example` needs the digit-and-dot shape test, and
    # `127.0.0.1.evil.example` — the canonical attack, since it READS as
    # loopback and wildcard DNS hands out `<anything>.<domain>` for free —
    # survives every prefix test there is. `127.1.2.3` must still pass, or the
    # fix has simply narrowed the allowlist to one address.
    for host, want_ok in (("127.0.0.1", True), ("127.1.2.3", True),
                          ("localhost", True), ("::1", True), ("[::1]", True),
                          ("1", False), ("10.0.0.5", False),
                          ("127.evil.example", False),
                          ("127.9foo.example", False),
                          ("127.0.0.1x", False),
                          ("127.0.0.1.evil.example", False)):
        res = run([BIN / "touch-serve", "--host", host, "--help"], cwd=tmp)
        if res is None:
            continue
        ok = res.returncode == 0 and "--allow-origin" in res.stdout + res.stderr
        check(ok is want_ok,
              f"--host {host} is {'accepted' if want_ok else 'refused'} "
              f"(rc={res.returncode})")
    # Value position: `--open` here is an origin string to server.py's parser,
    # so the wrapper must not read it as a bind request.
    res = run([BIN / "touch-serve", "--allow-origin", "--open", "--help"], cwd=tmp)
    if res is not None:
        check(res.returncode == 0,
              f"--open in a value position is not refused (rc={res.returncode}, "
              f"{res.stderr.strip()[:160]})")
    # The same spelling against the OTHER wrapper, where the correct answer is
    # the opposite one. `monitor_server.resolve_host` tests `"--open" in
    # sys.argv[1:]` — literal membership anywhere — so this command WOULD bind
    # the daemon off loopback, and `touch-monitor` must refuse it even though
    # `touch-serve` accepts the identical words. This is the security-relevant
    # half of the docstring's "both behaviours are asserted", and the arm that
    # catches a future edit narrowing the monitor scan to "first flag position"
    # for symmetry with touch-serve.
    res = run([BIN / "touch-monitor", "--allow-origin", "--open"], cwd=tmp,
              timeout=45)
    if res is not None:
        check(res.returncode != 0 and "refusing to forward --open" in res.stderr,
              f"touch-monitor refuses --open in a value position too — its "
              f"callee reads argv by membership (rc={res.returncode}, "
              f"{res.stderr.strip()[:160]})")

    # monitor_server.resolve_host: `--open` > $ORCH_BIND > loopback. The env var
    # is the dangerous one — nobody types it at the prompt; it is inherited from
    # a previous shell or an earlier run. The padded spelling is checked because
    # the daemon reads `(os.environ.get("ORCH_BIND") or "").strip()`: a wrapper
    # that did not strip would over-refuse a value the daemon binds to loopback,
    # and "the wrapper agrees with the thing it fronts" is the only standard
    # that keeps a guard from becoming folklore.
    #
    # The loopback-shaped hostnames are checked HERE too, not only against
    # `touch-serve`: the two guards are separate `case` statements in separate
    # files, so a shape test landing in one of them says nothing about the
    # other — and this is the arm where nobody typed the value, which makes an
    # unnoticed public bind the likelier outcome of the two.
    for value in ("0.0.0.0", " 0.0.0.0 ", "127.0.0.1.evil.example",
                  "127.9x.example"):
        res = run([BIN / "touch-monitor", free_port()], cwd=tmp,
                  env={"ORCH_BIND": value}, timeout=45)
        if res is None:
            continue
        check(res.returncode != 0,
              f"touch-monitor refuses ORCH_BIND={value!r} (rc={res.returncode})")
        check("ORCH_BIND" in res.stderr,
              f"it names the variable to unset ({res.stderr.strip()[:160]})")
    # ...and the ones the daemon itself reads as loopback (or as unset), which
    # must therefore reach it.
    for value in ("127.0.0.1", " 127.0.0.1", "   "):
        res = run([BIN / "touch-monitor", free_port()], cwd=tmp,
                  env={"ORCH_BIND": value}, timeout=45)
        if res is None:
            continue
        check("refusing" not in res.stderr,
              f"ORCH_BIND={value!r} is left alone ({res.stderr.strip()[:160]})")
        # The daemon's own sentence, not its file name: see DAEMON_REFUSAL.
        check(DAEMON_REFUSAL["touch-monitor"] in res.stderr,
              f"and the daemon was reached ({res.stderr.strip()[:160]})")


def test_daemons_are_dispatched(tmp):
    print("test_daemons_are_dispatched")
    # Both daemons resolve their state dir AT IMPORT and exit with ONE line when
    # nothing resolves. From a foreign cwd with no `.claude/` above it that exit
    # is deterministic, and the sentence is one only the loaded module can
    # produce — which is the whole point of asserting it rather than the module
    # NAME. `python3 <missing path>` prints
    #
    #     python3: can't open file '<ROOT>/shared/monitoring/monitor_server.py'
    #
    # and the module name is in there, inside the path the wrapper just passed.
    # An assertion on the name alone is tautological with respect to the `exec`
    # line: it passes for a payload that ships no daemon at all, and therefore
    # for the one self-location error this arm still has to catch — a wrong
    # relative path inside an otherwise correct payload.
    for name, marker in DAEMON_REFUSAL.items():
        args = [BIN / name] + (["notaport"] if name == "touch-monitor" else [])
        res = run(args, cwd=tmp, timeout=90)
        if res is None:
            continue
        check(res.returncode != 0,
              f"{name} exits non-zero when no task state dir resolves "
              f"(rc={res.returncode})")
        check(marker in res.stderr,
              f"{name} reached its daemon, which said so itself "
              f"({res.stderr.strip()[:160]})")
        check("can't open file" not in res.stderr,
              f"{name} passed python3 a path that exists "
              f"({res.stderr.strip()[:160]})")
        check("Traceback" not in res.stderr,
              f"{name} fails with a message, not a traceback")

    # ...and the proof that the assertion above can fail: the same wrappers in a
    # payload whose `shared/monitoring/` was never assembled. Without this, "the
    # daemon was reached" is a claim nobody has ever seen come out false.
    hollow = tmp / "hollow-payload"
    (hollow / ".claude-plugin").mkdir(parents=True)
    (hollow / ".claude-plugin" / "plugin.json").write_text("{}\n", encoding="utf-8")
    (hollow / "bin").mkdir()
    for name in DAEMON_REFUSAL:
        shutil.copy2(BIN / name, hollow / "bin" / name)
    for name, marker in DAEMON_REFUSAL.items():
        res = run([hollow / "bin" / name], cwd=tmp, timeout=90)
        if res is None:
            continue
        check(res.returncode != 0,
              f"{name} in a payload with no shared/monitoring/ exits non-zero "
              f"(rc={res.returncode})")
        check(marker not in res.stderr,
              f"and does NOT produce the daemon's own message — the arm above "
              f"is discriminating ({res.stderr.strip()[:160]})")
        # The wrapper checks its target rather than letting python3 report it,
        # so the user is told which file is missing instead of reading an
        # interpreter error about a path they never typed.
        check("is missing" in res.stderr and "Traceback" not in res.stderr,
              f"{name} names the file that is missing ({res.stderr.strip()[:160]})")


def test_cycle_reporter_target(tmp):
    print("test_cycle_reporter_target")
    target = REPO / "plugin/touch/skills/implement-plan/templates/cycle_reporter.py"
    res = run([BIN / "touch-cycle-reporter"], cwd=tmp)
    if res is None:
        return
    check(res.returncode != 0, "touch-cycle-reporter with no wf_dir exits non-zero")
    if target.is_file():
        # The skills tree has landed (item 09): the wrapper must reach the real
        # reporter, whose own usage/ORCH_STATE_DIR message is the proof.
        check("ORCH_STATE_DIR" in res.stderr or "usage:" in res.stderr,
              f"touch-cycle-reporter reached cycle_reporter.py "
              f"({res.stderr.strip()[:160]})")
    else:
        # Item 09 moves the skills into the subtree; until it does, the wrapper
        # must say exactly what is missing rather than dying in `python3`.
        check("is missing" in res.stderr and "cycle_reporter.py" in res.stderr,
              f"touch-cycle-reporter names the file it could not find "
              f"({res.stderr.strip()[:160]})")
        skip("plugin/touch/skills/ not present yet (item 09) — dispatch arm "
             "checked against the missing-target message instead")


#: The ONE report line this suite tolerates while the tasks-root move is
#: half-landed: `legacy.orchestrator_root()` already joins
#: `.touch/local-orchestrators` and `monitor_server.resolve_tasks_root()` still
#: joins the `.claude` spelling, so check 5 correctly reports that the two
#: shipping halves disagree. Neither file belongs to this test.
#:
#: The tolerance is deliberately shaped so it cannot outlive the window or cover
#: anything else: it matches only a disagreement whose two answers differ by
#: exactly that FIRST component under one project, and the moment the ladders
#: agree the strict path below runs again with nothing to switch off. Any other
#: disagreement — a plugin-internal path, a different leaf, two projects — fails
#: as it always did.
LADDER_MIGRATION = re.compile(
    r"^FAIL\s+task state resolves into the project, not the plugin — but the two "
    r"halves disagree: monitoring (?P<mon>\S+) vs aggregator (?P<agg>\S+)$")


def mid_ladder_migration(out):
    """True when the report's ONLY failure is the half-landed tasks-root move.

    The gate is the two shipped resolvers OWN answers, quoted in the report line
    — never a grep for the old join over `monitor_server.py`. A source-text gate
    reads the same in a comment as in code, and this repository explains its
    moves in prose, so one sentence spelling `".claude", "local-orchestrators"`
    would have kept the tolerance switched on after it should have expired: the
    exact failure mode a self-retiring tolerance exists to avoid.
    Behaviour cannot be faked that way. The moment the monitoring ladder joins
    `.touch/`, the two halves agree, check 5 stops failing, this returns False on
    its `len(failed) != 1` arm, and the strict path below runs with nothing left
    to switch off — with nobody having to remember to delete anything.
    """
    failed = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
    if len(failed) != 1:
        return False
    m = LADDER_MIGRATION.match(failed[0])
    if not m:
        return False
    legacy_root = Path(m.group("mon"))
    new_root = Path(m.group("agg"))
    return (legacy_root.parts[-2:] == (".claude", "local-orchestrators")
            and new_root.parts[-2:] == (".touch", "local-orchestrators")
            and legacy_root.parents[1] == new_root.parents[1])


def test_selfcheck(tmp):
    print("test_selfcheck")
    project = tmp / "fresh-project"
    (project / ".claude").mkdir(parents=True)
    res = run([BIN / "touch-selfcheck"], cwd=project, timeout=120)
    if res is None:
        return
    out = res.stdout
    # See LADDER_MIGRATION: one specific line is another sub-plans to fix, and
    # it is subtracted from every count below rather than ignored, so the arms
    # keep asserting the same things about the rest of the report.
    transitional = mid_ladder_migration(out)
    if transitional:
        skip("the two shipped tasks-root ladders are mid-migration (monitoring "
             "still answers .claude/local-orchestrators, the aggregator answers "
             ".touch/) — check 5 is expected red until the monitoring ladder is "
             "flipped; every other arm below is still asserted. The tolerance "
             "retires itself: it is gated on the two resolvers OWN answers in "
             "this report, so it cannot outlive the edit that makes them agree")
    check(res.returncode == 0 or transitional,
          f"touch-selfcheck passes in a fresh project (rc={res.returncode}, "
          f"{(out + res.stderr).strip()[-300:]})")
    failed = [ln for ln in out.splitlines()
              if ln.startswith("FAIL") and not LADDER_MIGRATION.match(ln)]
    check(not failed, f"no other check reports FAIL ({failed})")
    passed = [ln for ln in out.splitlines() if ln.startswith("PASS")]
    reported = passed + [ln for ln in out.splitlines() if ln.startswith("FAIL")]
    # DISTRIBUTION-4's list: python floor, import, assets, project root, task
    # root, the memory mapping, port bind, plus the exec bits and the status
    # round trip. EXACTLY nine, not "at least": a lower bound is satisfied by a
    # duplicated line and by a report that grew a tenth check nobody wrote down,
    # so it cannot detect the regression this arm exists for.
    check(len(reported) == 9, f"all nine checks are reported ({len(reported)} "
                              f"report lines: {reported})")
    # And the summary the user actually reads agrees with the lines above it —
    # the count is kept by a separate variable, so the two can drift.
    if transitional:
        summary = [ln for ln in out.splitlines() if "checks: " in ln]
        check(summary == [f"{len(reported)} checks: {len(passed)} passed, "
                          f"{len(reported) - len(passed)} failed"],
              f"the summary counts the same checks it printed ({summary})")
    else:
        summary = [ln for ln in out.splitlines() if "checks: all passed" in ln]
        check(summary == [f"{len(passed)} checks: all passed"],
              f"the summary counts the same checks it printed ({summary})")
    for want in ("python3", "aggregator", "assets", "project root",
                 "task state", "auto memory", "loopback", "executable",
                 "read back"):
        check(any(want in ln for ln in reported),
              f"selfcheck covers {want!r}")
    # It ends in something the user can paste (DISTRIBUTION-4/7) — printed only
    # by a green run, which is what the summary above just established.
    check(transitional or ("touch-serve" in out and "127.0.0.1:8932" in out),
          "the report ends with a copy-pasteable serve command")
    # It must not have written into the project it was pointed at. This is the
    # arm that pins "the default mode reads, `--init` writes": the memory check
    # resolves a directory and a settings key WITHOUT creating either.
    strays = sorted(p.name for p in project.iterdir())
    check(strays == [".claude"],
          f"selfcheck wrote nothing into the project (found: {strays})")
    check(not (project / ".claude" / "settings.local.json").exists(),
          "and wrote no settings file (only --init may)")

    # The canary: outside a project the report goes RED. A verifier that cannot
    # fail verifies nothing.
    bare = tmp / "not-a-project"
    bare.mkdir()
    res = run([BIN / "touch-selfcheck"], cwd=bare, timeout=120)
    if res is None:
        return
    check(res.returncode != 0,
          f"touch-selfcheck fails outside a project (rc={res.returncode})")
    check(any(ln.startswith("FAIL") for ln in res.stdout.splitlines()),
          "the failing run prints a FAIL line")

    # ...and the red run a first-run user ACTUALLY gets, which the canary above
    # cannot produce: a non-project directory under $HOME. There, both state
    # resolvers answer and they answer differently — `paths._marker_walk_up`
    # skips `$HOME` deliberately and `monitor_server.resolve_tasks_root` does
    # not — so check 5 reported an internal disagreement one line beneath check
    # 4, which had already named the real fault (a wrong `cd`). The resolver gap
    # is real and is another sub-plan's to close; printing it as this run's
    # diagnosis is what sends a new user to debug Touch's internals. A
    # `TemporaryDirectory` is never under $HOME, which is why this arm needs its
    # own directory rather than one more `tmp / …`.
    try:
        under_home = Path(tempfile.mkdtemp(prefix=".touch-selfcheck-probe.",
                                           dir=os.path.expanduser("~")))
    except OSError as exc:
        skip(f"$HOME not writable ({exc}) — the under-$HOME selfcheck arm not run")
        return
    try:
        res = run([BIN / "touch-selfcheck"], cwd=under_home, timeout=120)
        if res is None:
            return
        lines = res.stdout.splitlines()
        check(res.returncode != 0,
              f"touch-selfcheck under $HOME but outside a project fails "
              f"(rc={res.returncode})")
        check(any(ln.startswith("FAIL") and "holds .claude/" in ln for ln in lines),
              f"check 4 names the real fault — a cwd that is not a project "
              f"({[ln for ln in lines if ln.startswith('FAIL')]})")
        check(not any("two halves disagree" in ln for ln in lines),
              f"and check 5 does not re-report it as a Touch-internal "
              f"disagreement ({[ln for ln in lines if ln.startswith('FAIL')]})")
        check(any(ln.startswith("FAIL") and "run this from a project" in ln
                  for ln in lines),
              f"it points back at check 4 instead "
              f"({[ln for ln in lines if ln.startswith('FAIL')]})")
        strays = sorted(p.name for p in under_home.iterdir())
        check(not strays,
              f"and it wrote nothing into that directory (found: {strays})")
    finally:
        shutil.rmtree(under_home, ignore_errors=True)


def report_of(res):
    """The PASS/FAIL lines of a selfcheck run, in order."""
    return [ln for ln in res.stdout.splitlines()
            if ln.startswith("PASS") or ln.startswith("FAIL")]


def memory_lines(res):
    return [ln for ln in report_of(res) if "auto memory" in ln]


#: `--init`s refusal line, and the default report's version of the same thing.
#: Both are anchored, so a report that merely mentions the words somewhere does
#: not satisfy either arm.
INIT_REFUSAL = re.compile(
    r"^FAIL\s+auto memory was not mapped: (?P<why>.*) \(nothing was written\)$")
CHECK_REFUSAL = re.compile(
    r"`touch-selfcheck --init` would refuse to .*? because (?P<why>.*)$")


def init_reason(res):
    """Why `--init` wrote nothing, out of its own FAIL line, or None."""
    for ln in report_of(res):
        m = INIT_REFUSAL.match(ln)
        if m:
            return m.group("why")
    return None


def refusal_reason(res):
    """Why the DEFAULT report says `--init` would refuse here, or None."""
    for ln in memory_lines(res):
        m = CHECK_REFUSAL.search(ln)
        if m:
            return m.group("why")
    return None


def one_rule(what, init_res, check_res):
    """The two modes give the SAME reason, and the bare hint is gone.

    This is the arm that makes "the verifier never advertises a write the writer
    refuses" a MEASURED property rather than a comment. Three of the five gates
    used to be invisible to the report: a linked worktree, a `$HOME` project and
    a project inside the plugin cache were each told to run `touch-selfcheck
    --init`, which refuses all three — and for the worktree the line went further
    and stated the sharing capability the implementation had decided not to offer.
    Comparing the two sentences catches any future gate added to one side only,
    because a gate the report cannot see produces a `None` here.
    """
    wrote = init_reason(init_res) if init_res is not None else None
    told = refusal_reason(check_res) if check_res is not None else None
    check(wrote is not None,
          f"--init in {what} refuses with a reason "
          f"({report_of(init_res) if init_res is not None else None})")
    check(told is not None,
          f"and the DEFAULT report says --init would refuse {what} "
          f"({memory_lines(check_res) if check_res is not None else None})")
    check(wrote is not None and wrote == told,
          f"...for the SAME reason, word for word — the report may not advertise "
          f"a write the writer refuses ({wrote!r} vs {told!r})")
    if check_res is not None:
        bare = [ln for ln in memory_lines(check_res)
                if "--init` maps it to" in ln or "--init` aligns them" in ln]
        check(not bare,
              f"and never offers the bare hint in {what} ({bare})")


def test_selfcheck_arguments(tmp):
    print("test_selfcheck_arguments")
    # `--init` is the only mode that writes, so the argument surface is part of
    # the safety story: a typo must not fall through to the writing branch, and
    # `--help` must work on a machine where nothing else does.
    res = run([BIN / "touch-selfcheck", "--help"], cwd=tmp)
    if res is not None:
        check(res.returncode == 0 and "usage:" in res.stdout,
              f"--help prints usage and exits 0 (rc={res.returncode})")
        check("--init" in res.stdout,
              f"and names the writing mode ({res.stdout.strip()[:160]!r})")
    res = run([BIN / "touch-selfcheck", "--innit"], cwd=tmp)
    if res is not None:
        check(res.returncode == 2 and "unknown argument" in res.stderr,
              f"a misspelled flag is refused, not ignored (rc={res.returncode}, "
              f"{res.stderr.strip()[:160]})")
    project = tmp / "argument-project"
    (project / ".claude").mkdir(parents=True)
    res = run([BIN / "touch-selfcheck", "--init", "extra"], cwd=project)
    if res is not None:
        check(res.returncode == 2,
              f"--init with a stray extra argument is refused (rc={res.returncode})")
        strays = sorted(p.name for p in project.iterdir())
        check(strays == [".claude"],
              f"and wrote nothing on the way out (found: {strays})")


def test_selfcheck_init_maps_memory(tmp):
    print("test_selfcheck_init_maps_memory")
    project = tmp / "init-project"
    (project / ".claude").mkdir(parents=True)
    settings = project / ".claude" / "settings.local.json"
    # A pre-existing local settings file holding a key that has nothing to do
    # with memory. `--init` MERGES one key: the unforgivable failure here is a
    # verifier that ate somebodys permission rules on the way past.
    keep = {"permissions": {"allow": ["Bash(ls)"]}}
    settings.write_text(json.dumps(keep) + "\n", encoding="utf-8")
    memory = project / ".touch" / "memory"

    res = run([BIN / "touch-selfcheck", "--init"], cwd=project, timeout=120)
    if res is None:
        return
    lines = report_of(res)
    check(res.returncode == 0,
          f"--init exits 0 in a project (rc={res.returncode}, "
          f"{(res.stdout + res.stderr).strip()[-300:]})")
    check(len(lines) == 4 and all(ln.startswith("PASS") for ln in lines),
          f"all four init steps are reported and pass ({lines})")
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except ValueError as exc:
        check(False, f"settings.local.json is still valid JSON ({exc})")
        return
    check(data.get("permissions") == keep["permissions"],
          f"the keys --init did not come for survived ({data})")
    value = data.get("autoMemoryDirectory")
    check(isinstance(value, str) and os.path.isabs(value),
          f"autoMemoryDirectory is an ABSOLUTE path ({value!r}) — a relative or "
          f"$VAR value is silently ignored by the CLI, which is the whole reason "
          f"a program writes this key")
    check(bool(value) and os.path.realpath(value) == os.path.realpath(memory),
          f"and it points at <project>/.touch/memory ({value!r} vs {memory})")
    check(memory.is_dir(), f"the memory directory was created ({memory})")
    if memory.is_dir():
        check(stat.S_IMODE(memory.stat().st_mode) == 0o700,
              f"...with mode 0700 "
              f"({oct(stat.S_IMODE(memory.stat().st_mode))})")
        # And its PARENT, which `makedirs(mode=…)` does NOT cover — the mode
        # applies to the leaf only. `.touch/` is not a public directory either:
        # `server.json` lives there and carries the per-boot token, so a 0755
        # parent hands every local account the listing of a 0700 leaf.
        check(stat.S_IMODE(memory.parent.stat().st_mode) == 0o700,
              f"...and so is .touch/ itself, which holds the per-boot token "
              f"({oct(stat.S_IMODE(memory.parent.stat().st_mode))})")
    # GD-C1: the committed sibling is never the target and is never created.
    check(not (project / ".claude" / "settings.json").exists(),
          "the committed .claude/settings.json was not created or touched")
    check(sorted(p.name for p in project.iterdir()) == [".claude", ".touch"],
          f"nothing else was written into the project "
          f"({sorted(p.name for p in project.iterdir())})")

    # Twice is a normal thing to do (a second project, a re-run after an
    # upgrade), so the second run must be a no-op that says so rather than a
    # rewrite — and the ordinary report must now SEE the mapping, which is the
    # only end-to-end evidence that what was written is what the resolver reads.
    #
    # The planted index is the arm that pins this sub-plan's own boundary: I9
    # gives the MECHANISM to a program and the memory CONTENT to a human, so a
    # re-run over a populated tree must not touch a byte of it. Nothing else
    # would notice if `--init` grew a "seed the index" step, because the only
    # thing asserting that boundary today is the absence of code that breaks it.
    index = memory / "MEMORY.md"
    planted = "# Memory index\n\n- not written by --init\n"
    if memory.is_dir():
        index.write_text(planted, encoding="utf-8")
    res = run([BIN / "touch-selfcheck", "--init"], cwd=project, timeout=120)
    if res is not None:
        check(res.returncode == 0 and any("already set" in ln
                                          for ln in report_of(res)),
              f"a second --init is a no-op ({report_of(res)})")
    if memory.is_dir():
        check(sorted(p.name for p in memory.iterdir()) == ["MEMORY.md"],
              f"a second --init added no file to the memory tree "
              f"({sorted(p.name for p in memory.iterdir())})")
        check(index.read_text(encoding="utf-8") == planted,
              "and left the index byte-for-byte alone — --init maps memory, it "
              "never writes memory content (I9)")
    res = run([BIN / "touch-selfcheck"], cwd=project, timeout=120)
    if res is not None:
        got = memory_lines(res)
        check(len(got) == 1 and got[0].startswith("PASS")
              and "maps to" in got[0] and "local settings" in got[0],
              f"the ordinary report reads the mapping back green ({got})")

    # A DIFFERENT absolute value already in the file. `--init` may replace it —
    # aligning the two roots is what the mode is for — but it may not do it
    # quietly: a memory mapping is invisible unless a program says so out loud,
    # which is this mode's entire justification, and "(written)" over a
    # deliberate setting somebody else made is the same silence one level up.
    # The old value has to be in the line, because the whole remedy is pasting
    # it back.
    elsewhere = tmp / "previously-mapped-memory"
    settings.write_text(json.dumps({"autoMemoryDirectory": str(elsewhere),
                                    "permissions": keep["permissions"]}) + "\n",
                        encoding="utf-8")
    res = run([BIN / "touch-selfcheck", "--init"], cwd=project, timeout=120)
    if res is not None:
        lines = report_of(res)
        check(res.returncode == 0,
              f"--init over a different existing mapping exits 0 "
              f"(rc={res.returncode}, {lines})")
        check(any("replaced" in ln and str(elsewhere) in ln for ln in lines),
              f"and names the value it replaced ({lines})")
        check(written_value(settings) is not None
              and os.path.realpath(written_value(settings))
              == os.path.realpath(memory),
              f"which is now the intended one ({written_value(settings)!r})")
        check(json.loads(settings.read_text(encoding="utf-8")).get("permissions")
              == keep["permissions"],
              "and the unrelated keys survived that write too")
    if memory.is_dir():
        check(index.read_text(encoding="utf-8") == planted,
              "and the planted index is still byte-for-byte untouched")


def test_selfcheck_init_refuses_outside_a_project(tmp):
    print("test_selfcheck_init_refuses_outside_a_project")
    # No `.claude/` anywhere above: the resolver falls back to the cwd, and a
    # mode that wrote a settings file THERE would scatter mappings into whatever
    # directory a shell happened to be sitting in.
    bare = tmp / "init-not-a-project"
    bare.mkdir()
    res = run([BIN / "touch-selfcheck", "--init"], cwd=bare, timeout=120)
    if res is None:
        return
    lines = report_of(res)
    check(res.returncode != 0,
          f"--init outside a project fails (rc={res.returncode})")
    check(any(ln.startswith("FAIL") and "no .claude/" in ln for ln in lines),
          f"and names the reason ({lines})")
    check(any("nothing was written" in ln for ln in lines),
          f"and says it wrote nothing ({lines})")
    strays = sorted(p.name for p in bare.iterdir())
    check(not strays, f"which is true (found: {strays})")


def test_selfcheck_init_refuses_home_and_the_config_dir(tmp):
    print("test_selfcheck_init_refuses_home_and_the_config_dir")
    # `$HOME` holds a `.claude/`, so the "is this a project" gate passes for it —
    # and the project directory is something the environment can hand in. Writing
    # `~/.claude/settings.local.json` would break the hard rule that `~/.claude`
    # is a read-only tap (PROTOCOL-7). Both spellings are driven against a FAKE
    # home and a fake config dir: an arm that probed the real ones would, if the
    # guard were missing, do the damage it is testing for.
    fake_home = tmp / "fake-home"
    (fake_home / ".claude").mkdir(parents=True)
    config = tmp / "fake-config"
    (config / ".claude").mkdir(parents=True)
    cases = (
        ("the home directory", {"HOME": str(fake_home),
                                "TOUCH_PROJECT_CWD": str(fake_home)}),
        ("the configuration directory", {"CLAUDE_CONFIG_DIR": str(config),
                                         "TOUCH_PROJECT_CWD": str(config)}),
    )
    for what, env in cases:
        res = run([BIN / "touch-selfcheck", "--init"], cwd=tmp, timeout=120,
                  env=env)
        if res is None:
            continue
        lines = report_of(res)
        check(res.returncode != 0,
              f"--init refuses {what} as a project (rc={res.returncode})")
        check(any(ln.startswith("FAIL") and "read-only" in ln for ln in lines),
              f"and says why ({lines})")
        target = Path(env["TOUCH_PROJECT_CWD"]) / ".claude" / "settings.local.json"
        check(not target.exists(), f"and wrote nothing ({target} does not exist)")
        # ...and the DEFAULT report, over the same directory, must say the same
        # thing. This one held a green "…`touch-selfcheck --init` maps it to
        # <home>/.touch/memory" while `--init` refused it outright: `$HOME` holds
        # a `.claude/`, so the report's own gate passed and it never asked the
        # writer. Both sentences now come from `init_refusal`.
        one_rule(what, res,
                 run([BIN / "touch-selfcheck"], cwd=tmp, timeout=120, env=env))


def test_selfcheck_init_refuses_a_corrupt_settings_file(tmp):
    print("test_selfcheck_init_refuses_a_corrupt_settings_file")
    project = tmp / "init-corrupt"
    (project / ".claude").mkdir(parents=True)
    settings = project / ".claude" / "settings.local.json"
    original = '{"permissions": {oops\n'
    settings.write_text(original, encoding="utf-8")
    res = run([BIN / "touch-selfcheck", "--init"], cwd=project, timeout=120)
    if res is None:
        return
    lines = report_of(res)
    check(res.returncode != 0,
          f"--init over an unparseable settings file fails (rc={res.returncode})")
    check(any(ln.startswith("FAIL") and "autoMemoryDirectory" in ln
              for ln in lines),
          f"and says which key it could not set ({lines})")
    check(settings.read_text(encoding="utf-8") == original,
          "and leaves the file byte-for-byte alone — a settings file somebody is "
          "mid-edit is not this commands to replace")
    check("Traceback" not in res.stderr, "with a message, not a traceback")


def test_selfcheck_init_refuses_a_symlinked_settings_file(tmp):
    print("test_selfcheck_init_refuses_a_symlinked_settings_file")
    # A local settings file that is a SYMLINK to somewhere else — a dotfile
    # layout people really keep. `os.replace` replaces the LINK, so following it
    # would copy the keys out of the shared file into a new project-local one and
    # silently delete the link. Refused instead, and the arm asserts the two
    # things that make it a refusal rather than an accident: the link survives
    # and its target is untouched.
    project = tmp / "init-symlink"
    (project / ".claude").mkdir(parents=True)
    shared = tmp / "shared-settings.json"
    original = '{"permissions": {"allow": ["Bash(ls)"]}}\n'
    shared.write_text(original, encoding="utf-8")
    settings = project / ".claude" / "settings.local.json"
    try:
        settings.symlink_to(shared)
    except (OSError, NotImplementedError) as exc:
        skip(f"symlinks not available ({exc}) — the symlinked-settings arm not run")
        return
    res = run([BIN / "touch-selfcheck", "--init"], cwd=project, timeout=120)
    if res is None:
        return
    lines = report_of(res)
    check(res.returncode != 0,
          f"--init over a symlinked settings file fails (rc={res.returncode})")
    check(any(ln.startswith("FAIL") and "symlink" in ln for ln in lines),
          f"and names the reason ({lines})")
    check(settings.is_symlink(), "the symlink is still a symlink")
    check(shared.read_text(encoding="utf-8") == original,
          "and the file it points at was not rewritten")


#: A `paths` module that answers the three questions `--init` asks, so an arm can
#: point a FAKE plugin root at a project of its choosing. `CRASHING_PATHS` cannot
#: serve here: this shape has to get far enough to reach the write gates.
WORKING_PATHS = (
    'import os\n'
    '\n'
    'MEMORY_REL = os.path.join(".touch", "memory")\n'
    '\n'
    '\n'
    'def plugin_root():\n'
    '    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n'
    '\n'
    '\n'
    'def project_root():\n'
    '    return os.environ.get("TOUCH_PROJECT_CWD") or os.getcwd()\n'
    '\n'
    '\n'
    'def memory_root():\n'
    '    return os.path.join(project_root(), MEMORY_REL)\n'
)


def test_selfcheck_init_refuses_the_plugin_directory(tmp):
    print("test_selfcheck_init_refuses_the_plugin_directory")
    # The plugin root is a version-stamped cache: an update re-copies it and a
    # sweep removes the old one, so a memory tree created inside it is data loss
    # with extra steps — the reasoning check 5 already applies to task state, one
    # plane over (Part D-8). It is reachable because the project directory can be
    # handed in through the environment, and a cache directory that happens to
    # hold a `.claude/` passes the "is this a project" gate.
    #
    # Driven against a FAKE plugin root, never the real payload: an arm that
    # pointed --init at `plugin/touch/` would, if the gate were missing, write
    # into the tree `test_payload_is_read_only` exists to protect.
    cache = fake_plugin_root(tmp / "fake-cache", "", paths_text=WORKING_PATHS)
    (cache / ".claude").mkdir()
    res = run([cache / "bin" / "touch-selfcheck", "--init"], cwd=tmp, timeout=120,
              env={"TOUCH_PROJECT_CWD": str(cache)})
    if res is None:
        return
    lines = report_of(res)
    check(res.returncode != 0,
          f"--init refuses a project inside the plugin directory "
          f"(rc={res.returncode})")
    check(any(ln.startswith("FAIL") and "version-stamped" in ln for ln in lines),
          f"and says why ({lines})")
    check(not (cache / ".claude" / "settings.local.json").exists(),
          "and wrote no settings file into the cache")
    check(not (cache / ".touch").exists(),
          "and created no memory tree in it")
    # The third state the default report used to advertise as fixable by a
    # command that refuses it. Only the memory line is asserted here: this
    # fixture has no `touch-visual/` and no `shared/monitoring/monitor_server.py`,
    # so the run is red for reasons that are the fixture, not the subject.
    one_rule("a project inside the plugin tree", res,
             run([cache / "bin" / "touch-selfcheck"], cwd=tmp, timeout=120,
                 env={"TOUCH_PROJECT_CWD": str(cache)}))


def test_selfcheck_init_refuses_a_symlinked_memory_tree(tmp):
    print("test_selfcheck_init_refuses_a_symlinked_memory_tree")
    # `intended` is built with `os.path.join` and is the ONE thing --init creates,
    # so a symlinked `.touch/` used to walk past every gate: the settings file and
    # the project were checked, the directory the mapping names was not, and the
    # run reported 4/4 PASS having created a memory tree outside the project —
    # `~/.claude` included, the one tree this repository forbids writing to
    # (PROTOCOL-7). It is not an exotic layout either: putting a state directory
    # on another filesystem through a symlink is ordinary, and the person who does
    # it gets a green install report over a memory tree git tracks nothing under
    # (git does not descend a symlinked directory, so the `.touch/memory/*.md`
    # carve — G9, the whole premise of the tracked-memory design — silently
    # matches nothing and the content guards skip for ever).
    #
    # Every arm drives a FAKE home and a FAKE plugin cache: an arm that pointed at
    # the real ones would, with the guard missing, do the damage it tests for.
    def symlinked(name, target, extra_env=None, wrapper=None, leaf=False):
        """A project whose `.touch` (or, with `leaf`, `.touch/memory`) is a link."""
        project = tmp / name
        (project / ".claude").mkdir(parents=True)
        link = project / ".touch"
        if leaf:                            # the memory directory itself is the link
            link.mkdir()
            link = link / "memory"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            skip(f"symlinks not available ({exc}) — {name} arm not run")
            return None, None
        res = run([wrapper or (BIN / "touch-selfcheck"), "--init"], cwd=project,
                  timeout=120, env=extra_env)
        return project, res

    outside = tmp / "symlink-outside"
    outside.mkdir()
    project, res = symlinked("init-symlink-touch", outside)
    if res is not None:
        lines = report_of(res)
        check(res.returncode != 0,
              f"--init over a symlinked .touch/ fails (rc={res.returncode})")
        check(any(ln.startswith("FAIL") and "symlink" in ln
                  and str(project / ".touch") in ln for ln in lines),
              f"and names the symlink ({lines})")
        check(any("outside the project" in ln for ln in lines),
              f"and says the target is outside the project, which it is ({lines})")
        check(any("nothing was written" in ln for ln in lines),
              f"and says it wrote nothing ({lines})")
        check(not sorted(outside.iterdir()),
              f"which is true — the link target is still empty "
              f"({sorted(p.name for p in outside.iterdir())})")
        check(not (project / ".claude" / "settings.local.json").exists(),
              "and no mapping was written either")

    # A link pointing INSIDE the project — `.touch -> ./state`, an ordinary
    # layout. Still refused, for the reason that holds whatever the target is:
    # git does not descend a symlinked directory, so the `.touch/memory/*.md`
    # carve (G9) matches nothing and the content guards skip for ever. But the
    # sentence may not claim the tree is outside the project, because here it
    # plainly is not — a refusal that states something false about the layout in
    # front of the reader is how a file like this loses the benefit of the doubt
    # on the lines that ARE load-bearing.
    (tmp / "init-symlink-inside" / "state").mkdir(parents=True)
    project, res = symlinked("init-symlink-inside", "./state")
    if res is not None:
        lines = report_of(res)
        check(res.returncode != 0 and any("symlink" in ln for ln in lines),
              f"a .touch/ symlinked INSIDE the project is refused too "
              f"(rc={res.returncode}, {lines})")
        check(all("outside the project" not in ln for ln in lines),
              f"and the reason does not claim the target is outside the project "
              f"— it is not ({lines})")
        check(any("does not descend a symlinked directory" in ln for ln in lines),
              f"it gives the reason that holds either way: git tracks nothing "
              f"under a symlinked directory ({lines})")
        check(not sorted((project / "state").iterdir()),
              f"and the link target is still empty "
              f"({sorted(p.name for p in (project / 'state').iterdir())})")

    # The leaf spelling: `.touch/` is a real directory and `.touch/memory` is the
    # link. `islink` has to be asked about both, or half the class walks through.
    outside_leaf = tmp / "symlink-outside-leaf"
    outside_leaf.mkdir()
    project, res = symlinked("init-symlink-leaf", outside_leaf, leaf=True)
    if res is not None:
        lines = report_of(res)
        check(res.returncode != 0 and any("symlink" in ln for ln in lines),
              f"a symlinked .touch/memory itself is refused too "
              f"(rc={res.returncode}, {lines})")
        check(not sorted(outside_leaf.iterdir()),
              f"and its target is still empty "
              f"({sorted(p.name for p in outside_leaf.iterdir())})")

    # ...and the spelling that matters most, because the report was green for it:
    # a link into the CLI configuration directory. Driven against a fake $HOME.
    fake_home = tmp / "symlink-fake-home"
    (fake_home / ".claude").mkdir(parents=True)
    project, res = symlinked("init-symlink-home", fake_home / ".claude",
                             extra_env={"HOME": str(fake_home)})
    if res is not None:
        lines = report_of(res)
        check(res.returncode != 0 and any(ln.startswith("FAIL") for ln in lines),
              f"--init over a .touch/ symlinked into ~/.claude fails "
              f"(rc={res.returncode}, {lines})")
        check(not sorted((fake_home / ".claude").iterdir()),
              f"and wrote nothing under the home configuration directory — a "
              f"read-only tap by hard rule "
              f"({sorted(p.name for p in (fake_home / '.claude').iterdir())})")

    # The gate that already worked must keep working: the plugin-cache refusal
    # caught the symlinked route only because `contained` realpaths both sides,
    # and it is the proof the technique the new gate uses is the right one. A
    # FAKE cache, for the reason test_selfcheck_init_refuses_the_plugin_directory
    # gives.
    cache = fake_plugin_root(tmp / "symlink-fake-cache", "",
                             paths_text=WORKING_PATHS)
    inside = cache / "swept"
    inside.mkdir()
    project, res = symlinked("init-symlink-cache", inside,
                             wrapper=cache / "bin" / "touch-selfcheck")
    if res is not None:
        lines = report_of(res)
        check(res.returncode != 0
              and any(ln.startswith("FAIL") and "version-stamped" in ln
                      for ln in lines),
              f"a .touch/ symlinked into the plugin cache is still refused as a "
              f"cache write, not as a symlink (rc={res.returncode}, {lines})")
        check(not sorted(inside.iterdir()),
              f"and nothing was created in the cache "
              f"({sorted(p.name for p in inside.iterdir())})")

    # Finally the read-only side of the same rule. --init refuses to CREATE such a
    # tree; check 6 must refuse to CALL one green, or a tree that was symlinked
    # after --init ran stays certified for ever — and `same_dir` is lexical by
    # design (it compares a configured string to a resolver answer), so nothing
    # else in the report can notice.
    project = tmp / "check-symlink"
    (project / ".claude").mkdir(parents=True)
    elsewhere = tmp / "check-symlink-elsewhere"
    (elsewhere / "memory").mkdir(parents=True)
    try:
        (project / ".touch").symlink_to(elsewhere)
    except (OSError, NotImplementedError) as exc:
        skip(f"symlinks not available ({exc}) — the check-mode symlink arm not run")
        return
    (project / ".claude" / "settings.local.json").write_text(
        json.dumps({"autoMemoryDirectory": str(project / ".touch" / "memory")})
        + "\n", encoding="utf-8")
    res = run([BIN / "touch-selfcheck"], cwd=project, timeout=120)
    if res is None:
        return
    got = memory_lines(res)
    check(len(got) == 1 and got[0].startswith("FAIL") and "symlink" in got[0],
          f"the ordinary report refuses a mapping whose .touch/ is a symlink — "
          f"the verifier may not certify what --init refuses to write ({got})")


def fake_worktree(project, gitdir, commondir=None):
    """Give `project` a `.git` FILE pointing at `gitdir`, git binary not needed.

    The shapes `primary_checkout()` has to tell apart are all expressible as two
    plain files, so they are built by hand: an arm that shelled out to `git
    worktree add` would skip itself on a machine without git — and a payload
    installed from an archive is exactly the machine that has no git, which is
    why the function under test reads `.git` instead of running `rev-parse`.
    """
    (project / ".claude").mkdir(parents=True, exist_ok=True)
    (project / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    Path(gitdir).mkdir(parents=True, exist_ok=True)
    if commondir is not None:
        (Path(gitdir) / "commondir").write_text(commondir + "\n", encoding="utf-8")


def written_value(settings):
    """`autoMemoryDirectory` out of a settings file, or None."""
    try:
        return json.loads(settings.read_text(encoding="utf-8")).get(
            "autoMemoryDirectory")
    except (OSError, ValueError):
        return None


def test_selfcheck_maps_the_primary_checkout(tmp):
    print("test_selfcheck_maps_the_primary_checkout")
    # `primary_checkout()` decides where the model's memory lives, from a
    # hand-rolled read of git internals, in the one situation the CLI documents
    # as sharing memory across worktrees (DOCS-10). Every OTHER --init arm runs in
    # a non-git directory, where the function returns on its first line — so
    # without these four shapes the property it exists for is asserted nowhere,
    # and its fallbacks are asserted nowhere either.

    # (a) A linked worktree: `.git` is a file, `<gitdir>/commondir` points back at
    # the primary `.git`, so the mapping the CLI would honour names the PRIMARY
    # checkout (DOCS-10) — while `paths.memory_root()`, which is what the memory
    # page serves, is anchored on the project Touch was started in. The two
    # legitimately diverge here and nothing run from this worktree can make them
    # agree, so `--init` REFUSES and writes nothing: a mode that wrote the key and
    # then reported it red would leave rc 1, a mapping pointing out of the
    # checkout, and a new `.touch/` in somebody else's checkout that nobody asked
    # for. The report and the filesystem have to agree about that.
    primary = tmp / "wt-primary"
    (primary / ".claude").mkdir(parents=True)
    linked = tmp / "wt-linked"
    fake_worktree(linked, primary / ".git" / "worktrees" / "linked",
                  commondir="../..")
    res = run([BIN / "touch-selfcheck", "--init"], cwd=linked, timeout=120)
    if res is not None:
        settings = linked / ".claude" / "settings.local.json"
        got = memory_lines(res)
        check(len(got) == 1 and got[0].startswith("FAIL")
              and str(primary / ".touch" / "memory") in got[0]
              and str(linked / ".touch" / "memory") in got[0],
              f"a linked worktree is refused naming BOTH roots — the primary "
              f"checkout the CLI reads and the worktree Touch serves ({got})")
        check(any("nothing was written" in ln for ln in got),
              f"and says it wrote nothing ({got})")
        check(res.returncode != 0,
              f"and the run is red for it (rc={res.returncode}) — the memory page "
              f"would edit a directory no session in this worktree reads")
        # ...which is the half a report cannot be trusted on: assert the claim.
        check(written_value(settings) is None,
              f"which is true — no mapping was written into the worktree "
              f"({written_value(settings)!r})")
        check(not (primary / ".touch").exists(),
              f"and no memory tree was created in the primary checkout "
              f"({sorted(p.name for p in primary.iterdir())})")
        check(not (linked / ".touch").exists(),
              f"nor in this one "
              f"({sorted(p.name for p in linked.iterdir())})")
        # ...and the DEFAULT report over the same worktree. This is the line the
        # implementation got worst: a green "`touch-selfcheck --init` maps it to
        # <primary>/.touch/memory [a linked worktree, so the mapping names the
        # primary checkout, which every worktree shares]" — a stated capability,
        # not just a wrong command name, over a mode that refuses the layout
        # outright. Nothing is mapped at this point (the arm above asserted the
        # settings file is absent), so the unmapped branch is what answers.
        one_rule("a linked worktree", res,
                 run([BIN / "touch-selfcheck"], cwd=linked, timeout=120))

    # (b) A submodule: `gitdir:` points into `modules/<name>`, which has no
    # `commondir` and is NOT a worktree. Its parent is `.git/modules`, so the
    # worktree derivation would answer with a directory inside the superproject's
    # git dir. The project itself is the answer.
    sub = tmp / "wt-submodule"
    fake_worktree(sub, tmp / "super" / ".git" / "modules" / "sub")
    # (c) A worktree of a BARE repository: `commondir` resolves to `srv.git`,
    # whose parent is merely the directory the bare repo sits in — not a
    # checkout, not a project, and outside every worktree the `.gitignore` carve
    # covers. Verified to have mapped memory THERE before this was guarded.
    bare_holder = tmp / "bare-holder"
    bare = bare_holder / "wtA"
    fake_worktree(bare, bare_holder / "srv.git" / "worktrees" / "wtA",
                  commondir="../..")
    # (d) A `.git` file with something else in it: a future git format, a merge
    # artefact, a hand-edited file. Unrecognised must mean "this directory", not
    # an exception and not a guess.
    garbage = tmp / "wt-garbage"
    (garbage / ".claude").mkdir(parents=True)
    (garbage / ".git").write_text("this is not a gitdir pointer\n",
                                  encoding="utf-8")

    for what, project in (("a submodule", sub),
                          ("a worktree of a bare repository", bare),
                          ("an unrecognised .git file", garbage)):
        res = run([BIN / "touch-selfcheck", "--init"], cwd=project, timeout=120)
        if res is None:
            continue
        value = written_value(project / ".claude" / "settings.local.json")
        check(value is not None
              and os.path.realpath(value)
              == os.path.realpath(project / ".touch" / "memory"),
              f"{what} maps memory into the project itself ({value!r})")
        check(res.returncode == 0,
              f"...and the run is green ({what}, rc={res.returncode}, "
              f"{report_of(res)})")
    # The bare shape is the one that used to write OUTSIDE every checkout, so the
    # absence is asserted by name rather than left to the value comparison.
    check(not (bare_holder / ".touch").exists(),
          f"nothing was created beside the bare repository "
          f"({sorted(p.name for p in bare_holder.iterdir())})")


def test_selfcheck_diagnoses_a_silently_ignored_mapping(tmp):
    print("test_selfcheck_diagnoses_a_silently_ignored_mapping")
    project = tmp / "memory-diagnosis"
    (project / ".claude").mkdir(parents=True)
    settings = project / ".claude" / "settings.local.json"
    memory = project / ".touch" / "memory"

    def with_value(value):
        settings.write_text(json.dumps({"autoMemoryDirectory": value}) + "\n",
                            encoding="utf-8")
        return run([BIN / "touch-selfcheck"], cwd=project, timeout=120)

    # The two spellings that LOOK right and do nothing. The CLI validator drops
    # both and falls back to its default location with no error, no warning and
    # no log line, so this check is the only place either can be noticed.
    for value in (".touch/memory", "$HOME/.touch/memory"):
        res = with_value(value)
        if res is None:
            continue
        got = memory_lines(res)
        check(len(got) == 1 and got[0].startswith("FAIL")
              and "silently ignored" in got[0],
              f"{value!r} is reported as ignored, not as a mapping ({got})")
        check(res.returncode != 0,
              f"and the run is red for it (rc={res.returncode})")

    # An absolute path that is simply somewhere else: honoured by the CLI, and a
    # trap for Touch, whose memory page serves <project>/.touch/memory.
    res = with_value(str(tmp / "elsewhere-memory"))
    if res is not None:
        got = memory_lines(res)
        check(len(got) == 1 and got[0].startswith("FAIL")
              and "elsewhere-memory" in got[0] and str(memory) in got[0],
              f"a mapping pointing elsewhere is reported with BOTH paths ({got})")

    # An inert value in a layer that CANNOT win. `local settings` outranks
    # `user settings`, so a correct project-local mapping is honoured whatever
    # nonsense sits below it — and the first thing a person tries by hand is the
    # relative spelling in `~/.claude/settings.json`, which means this is the
    # state a working install is most likely to be in. The verdict has to come
    # from the WINNER: a report that calls this "not honoured" is both false and
    # red, and `--init` would exit non-zero straight after doing its job.
    config = tmp / "diagnosis-config"
    config.mkdir()
    (config / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": ".touch/memory"}) + "\n",
        encoding="utf-8")
    settings.write_text(json.dumps({"autoMemoryDirectory": str(memory)}) + "\n",
                        encoding="utf-8")
    res = run([BIN / "touch-selfcheck"], cwd=project, timeout=120,
              env={"CLAUDE_CONFIG_DIR": str(config)})
    if res is not None:
        got = memory_lines(res)
        check(len(got) == 1 and got[0].startswith("PASS") and str(memory) in got[0],
              f"a mapping that WINS is green even with an inert value in a lower "
              f"layer ({got})")
        check(all("ignored elsewhere" in ln and "user settings" in ln
                  for ln in got),
              f"and the inert layer is still named, as a footnote ({got})")
        check(res.returncode == 0 or mid_ladder_migration(res.stdout),
              f"and the run is not red for it (rc={res.returncode}; the "
              f"mid-migration check-5 tolerance applies here too — the `--init` "
              f"arm below asserts rc 0 strictly, and --init never runs check 5)")
    res = run([BIN / "touch-selfcheck", "--init"], cwd=project, timeout=120,
              env={"CLAUDE_CONFIG_DIR": str(config)})
    if res is not None:
        check(res.returncode == 0,
              f"--init exits 0 in that state too (rc={res.returncode}, "
              f"{report_of(res)})")

    # An UNPARSEABLE layer is a third thing, and calling it a broken mapping is
    # false in both directions. A trailing comma in `~/.claude/settings.json` is
    # an ordinary state (and one that silently voids that whole file for the CLI
    # too), so the report has to say what it actually knows: nothing configures
    # memory here, and one file could not be read, so this cannot certify the
    # location. Red — a verifier that cannot see a layer must not print a verdict
    # about it — but never "the mapping is not honoured", which names a mapping
    # that does not exist.
    blind = tmp / "diagnosis-blind"
    blind.mkdir()
    (blind / "settings.json").write_text('{"permissions": {"allow": [],}}\n',
                                         encoding="utf-8")
    unmapped = tmp / "memory-unreadable"
    (unmapped / ".claude").mkdir(parents=True)
    res = run([BIN / "touch-selfcheck"], cwd=unmapped, timeout=120,
              env={"CLAUDE_CONFIG_DIR": str(blind)})
    if res is not None:
        got = memory_lines(res)
        check(len(got) == 1 and got[0].startswith("FAIL")
              and "could not be read" in got[0]
              and str(blind / "settings.json") in got[0],
              f"an unreadable settings layer is reported as unverifiable, naming "
              f"the file ({got})")
        check(all("not honoured" not in ln for ln in got),
              f"and NOT as a mapping that is not honoured — there is no mapping "
              f"({got})")

    # ...and the same fault BELOW a mapping that wins cannot change the answer, so
    # it is a footnote on a green line rather than a verdict. The two arms
    # together are what pin the precedence-awareness: the kind of a complaint
    # depends on where the layer sits, not on what the fault is.
    settings.write_text(json.dumps({"autoMemoryDirectory": str(memory)}) + "\n",
                        encoding="utf-8")
    res = run([BIN / "touch-selfcheck"], cwd=project, timeout=120,
              env={"CLAUDE_CONFIG_DIR": str(blind)})
    if res is not None:
        got = memory_lines(res)
        check(len(got) == 1 and got[0].startswith("PASS") and str(memory) in got[0]
              and "outranked" in got[0],
              f"an unreadable layer the mapping outranks is a footnote on a green "
              f"line ({got})")

    # And the diagnosis trap the settings file cannot show you: an undocumented
    # environment override that outranks every layer. Set on purpose here — `run`
    # pops all three otherwise.
    settings.write_text(json.dumps({"autoMemoryDirectory": str(memory)}) + "\n",
                        encoding="utf-8")
    for var in ("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE",
                "CLAUDE_CODE_REMOTE_MEMORY_DIR", "CLAUDE_MEMORY_STORES"):
        res = run([BIN / "touch-selfcheck"], cwd=project, timeout=120,
                  env={var: str(tmp / "override-memory")})
        if res is None:
            continue
        got = memory_lines(res)
        check(len(got) == 1 and got[0].startswith("FAIL") and var in got[0],
              f"${var} is named as the thing that outranks the settings ({got})")


#: A `paths` module whose `plugin_root()` explodes — the shape of a half-built
#: or version-skewed payload, and the one thing a probe cannot guard against by
#: guarding its own statements one at a time.
CRASHING_PATHS = (
    'def plugin_root():\n'
    '    raise RuntimeError("selfcheck probe crash")\n'
    '\n'
    '\n'
    'def project_root():\n'
    '    return "/nonexistent"\n'
)


#: The same shape with a message that spans two lines — the report is read back
#: line by line, so a newline inside a message is a diagnosis cut in half.
MULTILINE_CRASH_PATHS = (
    'def plugin_root():\n'
    '    raise RuntimeError("first line\\nsecond line of the same fault")\n'
    '\n'
    '\n'
    'def project_root():\n'
    '    return "/nonexistent"\n'
)


def fake_plugin_root(base, init_text, paths_text=CRASHING_PATHS):
    """A minimal tree `touch-selfcheck` will locate itself in and probe.

    Complete enough that the two checks the SHELL runs (the exec bits, and the
    event round trip through `touch-status`) both pass on their own merits, so
    an arm below can assert the exact number of failures a shape produces. A
    fixture that fails checks for reasons the arm is not about turns "exactly
    one FAIL" into "at least one FAIL", which is the assertion that lets a
    spurious extra failure through.
    """
    (base / ".claude-plugin").mkdir(parents=True)
    (base / ".claude-plugin" / "plugin.json").write_text("{}\n", encoding="utf-8")
    (base / "bin").mkdir()
    shutil.copy2(BIN / "touch-selfcheck", base / "bin" / "touch-selfcheck")
    shutil.copy2(BIN / "touch-status", base / "bin" / "touch-status")
    (base / "shared" / "monitoring").mkdir(parents=True)
    shutil.copy2(REPO / "plugin/touch/shared/monitoring/status.sh",
                 base / "shared" / "monitoring" / "status.sh")
    (base / "aggregator").mkdir()
    (base / "aggregator" / "__init__.py").write_text(init_text, encoding="utf-8")
    (base / "aggregator" / "paths.py").write_text(paths_text, encoding="utf-8")
    (base / "aggregator" / "legacy.py").write_text(
        "def orchestrator_root():\n    return ''\n", encoding="utf-8")
    return base


def test_selfcheck_cannot_summarize_a_partial_run(tmp):
    print("test_selfcheck_cannot_summarize_a_partial_run")
    # Checks 1-7 come out of ONE python3 process, so the interesting failure is
    # not a check reporting FAIL — it is the process ending before the report
    # does. A verifier that prints "3 checks: all passed" over a probe that died
    # at check 3 is worse than no verifier, because the README sends users here
    # as the install proof. Both shapes are constructed and run.
    project = tmp / "selfcheck-project"
    (project / ".claude").mkdir(parents=True)

    # (a) A check raises where nothing catches it. `__version__` is absent as
    # well, which used to be a second way to trigger exactly this — it must now
    # be survivable, and the report says "unknown" instead of dying.
    crash = fake_plugin_root(tmp / "fake-crash", "")
    res = run([crash / "bin" / "touch-selfcheck"], cwd=project, timeout=120)
    if res is not None:
        out = res.stdout
        check(res.returncode != 0,
              f"a probe that crashes mid-report fails the run (rc={res.returncode})")
        check("checks: all passed" not in out,
              f"and never prints an all-passed summary ({out.strip()[-200:]})")
        check(any(ln.startswith("FAIL") and "crashed after" in ln
                  for ln in out.splitlines()),
              f"the crash is named ({[ln for ln in out.splitlines() if ln.startswith('FAIL')]})")
        check(any(ln.startswith("FAIL") and "reported every check" in ln
                  for ln in out.splitlines()),
              "and the missing lines are reported as their own failure")
        check(any(ln.startswith("PASS") and "aggregator unknown" in ln
                  for ln in out.splitlines()),
              f"a package with no __version__ is reported, not fatal ({out.strip()[:400]})")

    # (b) The probe dies with NO output and exit status 0 — a hard exit inside
    # an imported module, a signal, a full disk. This is the shape that used to
    # be invisible: nothing on stdout, nothing on stderr, and a green rc.
    silent = fake_plugin_root(tmp / "fake-silent", "import os\nos._exit(0)\n")
    res = run([silent / "bin" / "touch-selfcheck"], cwd=project, timeout=120)
    if res is not None:
        out = res.stdout
        check(res.returncode != 0,
              f"a probe that dies silently fails the run (rc={res.returncode})")
        check(any(ln.startswith("FAIL") and "never ended" in ln
                  for ln in out.splitlines()),
              f"and says the report never ended ({out.strip()[:300]})")

    # (c) A run that STOPS ON PURPOSE is a complete report of a short list, not
    # a truncated report of a long one, and the completeness detector must tell
    # them apart. Without the package there is nothing left to measure, so the
    # probe reports the import failure and stops — and a second line blaming
    # "the python probe" underneath a line that already named the real fault
    # sends the user to debug the verifier. This is the consumer-facing install
    # proof (DISTRIBUTION-4); its first impression is part of the artifact.
    broken = fake_plugin_root(tmp / "fake-import",
                              'raise ImportError("broken payload")\n')
    res = run([broken / "bin" / "touch-selfcheck"], cwd=project, timeout=120)
    if res is not None:
        out = res.stdout
        failed = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
        check(res.returncode != 0,
              f"a payload whose package will not import fails the run "
              f"(rc={res.returncode})")
        check(len(failed) == 1 and "aggregator imports from this tree" in failed[0],
              f"with exactly one FAIL, the true one ({failed})")
        check(not any("reported every check" in ln for ln in out.splitlines()),
              f"and no second failure blaming the probe for stopping on purpose "
              f"({out.strip()[:300]})")
        check("checks: all passed" not in out,
              f"and still no all-passed summary ({out.strip()[-160:]})")

    # (d) The exec-bit check reads bin/, it does not carry a list of six: a mode
    # dropped on a SEVENTH wrapper added later is the identical CM-13 failure,
    # and a hardcoded list would report PASS without ever looking at it.
    seventh = fake_plugin_root(tmp / "fake-seventh", "")
    (seventh / "bin" / "touch-seventh").write_text("#!/usr/bin/env bash\n",
                                                   encoding="utf-8")
    (seventh / "bin" / "touch-seventh").chmod(0o644)
    res = run([seventh / "bin" / "touch-selfcheck"], cwd=project, timeout=120)
    if res is not None:
        line = [ln for ln in res.stdout.splitlines() if "executable" in ln]
        check(any(ln.startswith("FAIL") and "touch-seventh" in ln for ln in line),
              f"an unlisted, non-executable bin/ entry is caught by name ({line})")

    # (e) A fault whose MESSAGE spans two lines. The shell loop reads the report
    # one line at a time and recognises a line only by its PASS/FAIL prefix, so an
    # embedded newline splits a diagnosis in half and the loop silently discards
    # the half that carries the detail — the report stays the right length and
    # says less than it knows, which is the failure mode hardest to notice. Every
    # message from the outside world is whitespace-collapsed before it is
    # reported; this is the arm that observes it.
    noisy = fake_plugin_root(tmp / "fake-multiline", "",
                             paths_text=MULTILINE_CRASH_PATHS)
    res = run([noisy / "bin" / "touch-selfcheck"], cwd=project, timeout=120)
    if res is not None:
        lines = res.stdout.splitlines()
        crashed = [ln for ln in lines if "crashed after" in ln]
        check(res.returncode != 0,
              f"a fault with a multi-line message still fails the run "
              f"(rc={res.returncode})")
        check(len(crashed) == 1
              and "first line second line of the same fault" in crashed[0],
              f"and the whole message arrives on ONE report line ({crashed})")
        check(not any(ln.startswith("second line") for ln in lines),
              f"with no orphaned fragment for the loop to discard "
              f"({[ln for ln in lines if ln.startswith('second line')]})")


def test_missing_python3(tmp):
    print("test_missing_python3")
    # A PATH with the two coreutils the wrappers use before the python3 check,
    # and nothing else. Invoked through `bash <path>` because `#!/usr/bin/env
    # bash` needs `env` to find bash on PATH, which this PATH deliberately
    # cannot do.
    shim = tmp / "shim-bin"
    shim.mkdir()
    for tool in ("readlink", "dirname"):
        real = shutil.which(tool)
        if real is None:
            skip(f"{tool} not found — missing-python3 arm not run")
            return
        os.symlink(real, shim / tool)
    bash = shutil.which("bash")
    if bash is None:
        skip("bash not found — missing-python3 arm not run")
        return
    for name in WRAPPERS:
        res = run([bash, BIN / name], cwd=tmp, env={"PATH": str(shim)})
        if res is None:
            continue
        check(res.returncode != 0,
              f"{name} exits non-zero without python3 (rc={res.returncode})")
        lines = [ln for ln in res.stderr.splitlines() if ln.strip()]
        check(len(lines) == 1 and "python3 not found" in lines[0],
              f"{name} says it in one line ({lines})")


def test_index_modes():
    print("test_index_modes")
    if not have_git():
        skip("not a git checkout — the payload is built with `git archive`, "
             "whose modes come from the index, which does not exist here")
        return
    res = subprocess.run(
        ["git", "ls-files", "-s", "--", "plugin/touch/bin"], cwd=str(REPO),
        capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        check(False, f"git ls-files runs (rc={res.returncode})")
        return
    modes = {}
    for line in res.stdout.splitlines():
        # `<mode> <sha> <stage>\tpath`: the separator before the path is a TAB,
        # and only a tab. Splitting on whitespace and taking field 4 truncates a
        # path containing a space, which lands the mode under a name no wrapper
        # has — reported as "untracked", i.e. a skip, i.e. silence, in the one
        # test whose subject is a mode nobody can see.
        head, sep, path = line.partition("\t")
        if not sep:
            continue
        modes[os.path.basename(path)] = head.split()[0]
    untracked = [n for n in WRAPPERS if n not in modes]
    if untracked:
        # Brand-new files are not in the index until they are added; that is a
        # driver step, not a defect, and the on-disk arm above already covers
        # the mode. Say so instead of failing a working tree mid-change.
        skip(f"not yet tracked, index mode unchecked: {untracked}")
        if len(untracked) == len(WRAPPERS):
            return
    bad = sorted(n for n, m in modes.items() if m != "100755")
    check(not bad,
          f"every tracked wrapper is mode 100755 in the index (wrong: {bad}) — "
          f"`git archive` ships the index mode, not the working-tree mode (CM-13)")


def payload_snapshot():
    """Every path under the plugin root, relative and sorted.

    GD-T5's "nothing writes the plugin root" is a claim about behaviour, and the
    plugin root is a version-stamped cache that is re-copied on update and swept
    ~14 days later — a spool, a checkpoint or a stray `__pycache__` written there
    is data loss with extra steps. Source-grepping for
    `PYTHONDONTWRITEBYTECODE=1` only proves someone typed it.
    """
    root = BIN.parent
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def main():
    if not BIN.is_dir():
        print("test_files_and_modes")
        check(False, "plugin/touch/bin/ exists")
    else:
        before = payload_snapshot()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            test_files_and_modes()
            test_no_plugin_environment()
            test_portable_argument_forwarding()
            test_portable_coreutils_usage()
            test_no_foreign_tooling()
            test_no_duplicated_resolution()
            test_open_is_never_passed_through()
            test_status_writes_an_event(tmp)
            test_status_without_a_state_dir(tmp)
            test_serve_runs_and_guards_shadowing(tmp)
            test_open_is_refused(tmp)
            test_bind_requests_are_refused(tmp)
            test_daemons_are_dispatched(tmp)
            test_cycle_reporter_target(tmp)
            test_selfcheck(tmp)
            test_selfcheck_arguments(tmp)
            test_selfcheck_init_maps_memory(tmp)
            test_selfcheck_init_refuses_outside_a_project(tmp)
            test_selfcheck_init_refuses_home_and_the_config_dir(tmp)
            test_selfcheck_init_refuses_a_corrupt_settings_file(tmp)
            test_selfcheck_init_refuses_a_symlinked_settings_file(tmp)
            test_selfcheck_init_refuses_the_plugin_directory(tmp)
            test_selfcheck_init_refuses_a_symlinked_memory_tree(tmp)
            test_selfcheck_maps_the_primary_checkout(tmp)
            test_selfcheck_diagnoses_a_silently_ignored_mapping(tmp)
            test_selfcheck_cannot_summarize_a_partial_run(tmp)
            test_missing_python3(tmp)
        print("test_payload_is_read_only")
        after = payload_snapshot()
        appeared = sorted(set(after) - set(before))
        vanished = sorted(set(before) - set(after))
        check(not appeared and not vanished,
              f"the behaviour arms wrote nothing into the plugin root "
              f"(appeared: {appeared[:5]}, vanished: {vanished[:5]}) — GD-T5")
        test_index_modes()
    print()
    if skips:
        print(f"skipped: {len(skips)} check(s)")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all bin-wrapper tests passed")


if __name__ == "__main__":
    main()
