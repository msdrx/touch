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
    for var in ("ORCH_STATE_DIR", "ORCH_TASKS_ROOT", "ORCH_WF_DIR", "ORCH_PORT",
                "ORCH_BIND", "TOUCH_STATE_DIR", "TOUCH_PROJECT_CWD",
                "CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA",
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


def test_selfcheck(tmp):
    print("test_selfcheck")
    project = tmp / "fresh-project"
    (project / ".claude").mkdir(parents=True)
    res = run([BIN / "touch-selfcheck"], cwd=project, timeout=120)
    if res is None:
        return
    out = res.stdout
    check(res.returncode == 0,
          f"touch-selfcheck passes in a fresh project (rc={res.returncode}, "
          f"{(out + res.stderr).strip()[-300:]})")
    failed = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
    check(not failed, f"no check reports FAIL ({failed})")
    passed = [ln for ln in out.splitlines() if ln.startswith("PASS")]
    # DISTRIBUTION-4's list: python floor, import, assets, project root, task
    # root, port bind, plus the exec bits and the status round trip. EXACTLY
    # eight, not "at least": a lower bound is satisfied by a duplicated line and
    # by a report that grew a ninth check nobody wrote down, so it cannot detect
    # the regression this arm exists for.
    check(len(passed) == 8, f"all eight checks are reported ({len(passed)} PASS "
                            f"lines: {passed})")
    # And the summary the user actually reads agrees with the lines above it —
    # the count is kept by a separate variable, so the two can drift.
    summary = [ln for ln in out.splitlines() if "checks: all passed" in ln]
    check(summary == [f"{len(passed)} checks: all passed"],
          f"the summary counts the same checks it printed ({summary})")
    for want in ("python3", "aggregator", "assets", "project root",
                 "task state", "loopback", "executable", "read back"):
        check(any(want in ln for ln in passed),
              f"selfcheck covers {want!r}")
    # It ends in something the user can paste (DISTRIBUTION-4/7).
    check("touch-serve" in out and "127.0.0.1:8932" in out,
          "the report ends with a copy-pasteable serve command")
    # It must not have written into the project it was pointed at.
    strays = sorted(p.name for p in project.iterdir())
    check(strays == [".claude"],
          f"selfcheck wrote nothing into the project (found: {strays})")

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
    # Checks 1-6 come out of ONE python3 process, so the interesting failure is
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
