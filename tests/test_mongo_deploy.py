#!/usr/bin/env python3
"""Stdlib-only tests for the Mongo deployment + security baseline (R-42), the
credential half of `aggregator/mirror.py`, and `docs/mongo.md` (R-57's mongo-doc
clauses). Run as `python3 test_mongo_deploy.py`; exits non-zero on failure.

R-42's own test list is the spine:

* the **documented bind is loopback** — asserted by parsing the recipe out of
  `docs/mongo.md` rather than by restating it here, so a doc that loses
  `-p 127.0.0.1:` or `--auth` fails this file;
* **an unauthenticated connect fails against a recipe-provisioned container**,
  which is the arm that actually runs `docker run` (and skips cleanly without
  docker) — and it provisions from the *parsed* recipe, so the container the
  assertions run against is the one the documentation tells an operator to
  start;
* `git check-ignore` passes for `mongo-data/x`;
* a `test_shell.py`-genre guard that **no file under `aggregator/` contains a
  connection string literal**.

GD-27 is the law being enforced, and its refusals are the interesting part: a
plain `docker run -p 27017:27017 mongo:7` is an unauthenticated database on
0.0.0.0 holding the exact unredacted transcripts GD-13 exists to protect
(probed: zero users, anonymous connect succeeds). So this file asserts the
refusals *and* asserts they do not false-positive against a correctly-secured
deployment — a least-privilege user cannot enumerate users, and reading that as
"zero users" would refuse to start against precisely the deployment GD-27 asks
for.

The runtime half of this sub-plan (R-45's queue, breaker, lease, sweep, rebuild
and backfill) lives in `test_mirror.py`; the split follows the two plan items.
"""

import asyncio
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[0]
# The canonical trees are named through `tests/_roots.py`, never by a
# literal under REPO: GD-U1 moves them and this is the single flip point.
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
from _roots import SRC                # noqa: E402  (path juggling first)
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(HERE))

from aggregator import mirror as mr                            # noqa: E402
from aggregator import mongo_store as ms                       # noqa: E402
from aggregator.mirror import (                                # noqa: E402
    CredentialError,
    MemoryBackend,
    Mirror,
    MongoConfig,
    REDACTED,
    STATE_REFUSED,
    database_name,
    is_denied_path,
    load_credentials,
    redact,
    redact_uri,
    resolve_config,
    save_credentials,
    scrub_value,
)

failures = []
skips = []

DOC = SRC / "docs" / "mongo.md"
AGG = SRC / "aggregator"

#: The scheme spelled apart, for the same reason `mirror.py` does it: this file
#: greps for connection-string literals, and a grep that contains one is a grep
#: that finds itself.
SCHEME = "mongodb" + "://"

#: The image the recipe names. Never pulled by the test — 1.2 GB on a CI box is
#: not a test dependency, it is an outage.
IMAGE = "mongo:7"


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def skip(msg):
    print(f"  SKIP: {msg}")
    skips.append(msg)


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception as other:                                  # noqa: BLE001
        print(f"    (raised {type(other).__name__}: {other})")
        return False
    return False


def doc_text():
    return DOC.read_text(encoding="utf-8")


def fenced_blocks(text, language=None):
    """Every fenced code block, optionally filtered by info string."""
    out = []
    pattern = re.compile(r"^```([^\n`]*)\n(.*?)^```", re.MULTILINE | re.DOTALL)
    for match in pattern.finditer(text):
        info = match.group(1).strip()
        if language is None or info == language:
            out.append(match.group(2))
    return out


def join_continuations(block):
    """Collapse shell line continuations so a command is one logical line."""
    return re.sub(r"\\\n\s*", " ", block)


def documented_docker_run():
    """The `docker run` recipe, tokenized, straight out of `docs/mongo.md`.

    Parsed rather than restated: the whole value of this file is that editing
    the documentation is what breaks the test. A copy of the recipe here would
    assert only that this file agrees with itself.
    """
    for block in fenced_blocks(doc_text(), "bash"):
        joined = join_continuations(block)
        for line in joined.splitlines():
            line = line.strip()
            if line.startswith("docker run") and "mongo" in line:
                return shlex.split(line)
    return None


def flag_values(tokens, flag):
    """Every value given to ``flag`` (repeatable flags like -e and -v)."""
    return [tokens[i + 1] for i, token in enumerate(tokens[:-1]) if token == flag]


# --- the documented recipe ------------------------------------------------
def test_the_documented_recipe_is_loopback_and_authenticated():
    print("test_the_documented_recipe_is_loopback_and_authenticated")
    check(DOC.exists(), f"{DOC.relative_to(REPO)} exists (R-42)")
    tokens = documented_docker_run()
    check(tokens is not None, "…and documents a `docker run` recipe in a bash block")
    if tokens is None:
        return

    publishes = flag_values(tokens, "-p") + flag_values(tokens, "--publish")
    check(publishes, f"the recipe publishes a port: {publishes}")
    for spec in publishes:
        check(spec.startswith("127.0.0.1:"),
              f"…bound to LOOPBACK only: {spec!r} (GD-27: there is no supported Touch "
              f"deployment where the database is reachable from another host)")
        check(not spec.startswith("0.0.0.0"), f"…never 0.0.0.0: {spec!r}")

    check("--auth" in tokens or any(v.startswith("MONGO_INITDB_ROOT_USERNAME=")
                                    for v in flag_values(tokens, "-e")),
          "…and starts an AUTHENTICATED mongod (a zero-user mongod accepts anonymous "
          "connections — MONGOSCHEMA-10 ≡ LIVEFLOW-19 ≡ CUSTOMSTATE-13)")
    check("--auth" in tokens, "…with --auth spelled explicitly, not merely implied")

    volumes = flag_values(tokens, "-v") + flag_values(tokens, "--volume")
    check(volumes, f"…with a volume: {volumes}")
    for volume in volumes:
        source, _, target = volume.partition(":")
        check(target.startswith("/data/db"), f"…mounted at /data/db: {volume!r}")
        check(source and not source.startswith("/") and not source.startswith("."),
              f"…from a NAMED volume, not an anonymous or bind one: {source!r} "
              f"(an anonymous volume defeats the mirror's whole point)")
        check("/" not in source,
              f"…and the volume is a docker name, never a host path: {source!r} — "
              f"no Mongo data directory inside the repo (GD-27)")

    check(IMAGE in tokens, f"…using {IMAGE}, the version this was probed against")

    # The password must come from the environment, never be a literal.
    envs = flag_values(tokens, "-e")
    password = [v for v in envs if v.startswith("MONGO_INITDB_ROOT_PASSWORD=")]
    check(password, f"…and sets a root password: {[p.split('=')[0] for p in password]}")
    for entry in password:
        value = entry.split("=", 1)[1]
        check(value.startswith("$") or not value,
              f"…from a shell variable, never a literal in the docs: {value!r}")


def test_the_docs_never_show_a_reachable_mongo():
    print("test_the_docs_never_show_a_reachable_mongo")
    text = doc_text()

    # `0.0.0.0` and the bare `-p 27017:27017` DO appear — they are what the page
    # tells you not to do. So the rule is not "never mentioned", it is "never
    # recommended": every appearance must sit in a line that marks it as the
    # wrong thing. Anything else would let a real example hide behind prose.
    negative = ("never", "not ", "n't", "unauthenticated", "wrong", "no supported",
                "defeat", "re-creates", "instead")
    # Judged per PARAGRAPH, not per line: prose wraps, and the sentence that
    # calls `-p 27017:27017` a mistake routinely puts the flag on one line and
    # the word "unauthenticated" on the next. A per-line rule would force the
    # documentation to be written around the test.
    for paragraph in re.split(r"\n\s*\n", text):
        risky = "0.0.0.0" in paragraph or re.search(r"(?<!\.)\b-p\s+27017:27017", paragraph)
        if not risky:
            continue
        first = next((l.strip() for l in paragraph.splitlines() if l.strip()), "")
        check(any(marker in paragraph.lower() for marker in negative),
              f"{DOC.name}: a non-loopback mongo appears only as a prohibition "
              f"(paragraph starting {first[:60]!r})")

    check("sbx ports" in text and "27017" in text,
          "the page addresses `sbx ports` and 27017 together (R-57)")
    publish_lines = [l for l in text.splitlines()
                     if "sbx ports" in l and "27017" in l and "publish" in l]
    for line in publish_lines:
        check(any(marker in line.lower() for marker in negative),
              f"…and every `sbx ports … 27017` line is a prohibition: {line.strip()[:80]!r}")
    check(re.search(r"(?i)do not publish 27017|never publish 27017", text) is not None,
          "…stated flatly: never publish 27017 (R-57)")


def test_the_docs_carry_r57s_clauses():
    print("test_the_docs_carry_r57s_clauses")
    text = doc_text()
    lowered = text.lower()

    check(re.search(r"(?i)mongo (is )?down is a non-event", text) is not None,
          "'Mongo down is a non-event' is stated, not implied (R-57)")
    for command in ("--rebuild", "--backfill"):
        check(command in text, f"the {command} command is documented (R-57)")
    check("aggregator.mirror" in text,
          "…as runnable command lines, not prose descriptions")

    for number, what in (("15.7", "corpus size on disk"), ("3 936", "record count"),
                         ("0.53", "mirror-vs-raw ratio"), ("1.3", "growth per session-hour")):
        check(number in text, f"the measured {what} is on the page ({number}) — R-57's "
                              f"growth/retention numbers are measurements, not estimates")

    check("ttl" in lowered and re.search(r"(?i)no ttl", text) is not None,
          "the no-TTL law is stated (GD-26)")
    check(re.search(r"(?i)keep everything|kept forever", text) is not None,
          "…and the v0 retention policy is stated in user-facing words (R-57)")

    # The derived database name, and the credential handling, both belong here.
    check(re.search(r"touch_<?sha1", text) is not None or "sha1" in lowered,
          "the derived database name is documented (GD-27)")
    check("0600" in text and "mongo.json" in text,
          ".touch/mongo.json and its 0600 mode are documented (GD-27)")
    check(re.search(r"(?i)least[- ]privilege|touchIngest", text) is not None,
          "the least-privilege ingest user is documented (GD-26 enforced by the server)")


# --- the static guards ----------------------------------------------------
def test_no_connection_string_literal_under_aggregator():
    print("test_no_connection_string_literal_under_aggregator")
    files = sorted(AGG.glob("*.py"))
    check(files, f"found {len(files)} module(s) under aggregator/")
    for path in files:
        text = path.read_text(encoding="utf-8")
        hits = [lineno for lineno, line in enumerate(text.splitlines(), 1) if SCHEME in line]
        check(not hits,
              f"{path.name} contains no connection string literal (R-42) — lines {hits}")

    # …and the module that must *recognise* one still can, which is the reason
    # the guard is satisfiable at all: `mirror.py` composes the scheme instead of
    # spelling it, so redaction works without the literal being present.
    check(redact_uri(SCHEME + "u:p@h:27017/db").startswith("mongodb"),
          "…while mirror.py still recognises and redacts a URI (it composes the scheme)")

    # No hardcoded host/port either — the URI comes from config, always.
    for path in files:
        text = path.read_text(encoding="utf-8")
        hits = [lineno for lineno, line in enumerate(text.splitlines(), 1)
                if "27017" in line and "#" not in line.split("27017")[0]]
        check(not hits, f"{path.name} hardcodes no mongod port — lines {hits}")


def test_gitignore_covers_the_mongo_artifacts():
    print("test_gitignore_covers_the_mongo_artifacts")
    if not shutil.which("git") or not (REPO / ".git").exists():
        skip("git is unavailable, so check-ignore cannot be asked")
        return

    def ignored(relative):
        proc = subprocess.run(["git", "check-ignore", "-q", "--no-index", relative],
                              cwd=REPO, capture_output=True)
        return proc.returncode == 0

    # R-42's gitignore half landed in sp-01; this asserts it, and never edits it.
    for path in ("mongo-data/x", "mongo-dump/x", "dump.bson", "mongo-data/journal/j.0"):
        check(ignored(path), f"git check-ignore passes for {path} (R-42/GD-27)")
    check(ignored(".touch/mongo.json"),
          "…and the credentials file can never be committed (GD-27)")

    # 2026-07-27 amendment: run state is ignored too — on disk only, never
    # versioned. The monitor and the Mongo legacy keyspace read the disk copy.
    # 2026-07-30 (G10): the tasks root moved to `.touch/local-orchestrators`,
    # where `/.touch/*` ignores it; the legacy `.claude/` rules were KEPT as
    # legacy defence, so both spellings are asserted. Hypothetical task names —
    # the claim is about the RULES, and `--no-index` above means no path here
    # needs to exist.
    orch = ".touch/" + "local-orchestrators"
    legacy = ".claude/" + "local-orchestrators"
    for path in (orch + "/a-task/events.jsonl", orch + "/",
                 legacy + "/a-task/events.jsonl", legacy + "/"):
        check(ignored(path), f"…and {path} is ignored (run state is disk-only)")
    # GD-16 as amended: `.touch/memory/*.md` is the ONE tracked subtree of
    # `.touch/`, and the carve must not have widened far enough to reach the
    # mirror's credentials or any other runtime droppings.
    check(not ignored(".touch/memory/does-not-exist.md"),
          "…while .touch/memory/*.md is the one tracked subtree (GD-16 amended)")
    for path in (".touch/memory/x.token", ".touch/memory/.history/x.md",
                 ".touch/memory-audit.jsonl"):
        check(ignored(path), f"…and the carve stays narrow: {path} is ignored")


# --- GD-27: credentials ---------------------------------------------------
def test_the_credentials_file_must_be_unreadable_to_everyone_else():
    print("test_the_credentials_file_must_be_unreadable_to_everyone_else")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "mongo.json"

        check(load_credentials(target) is None,
              "a missing credentials file is None, not an error — an unconfigured mirror "
              "is a normal, fully supported deployment (GD-21)")

        payload = json.dumps({"uri": SCHEME + "touch:pw@127.0.0.1:27017/touch_x"})
        target.write_text(payload, encoding="utf-8")

        for mode, label in ((0o644, "world-readable"), (0o640, "group-readable"),
                            (0o604, "other-readable"), (0o666, "world-writable")):
            os.chmod(target, mode)
            check(raises(CredentialError, load_credentials, target),
                  f"a {label} ({mode:04o}) credentials file is REFUSED (GD-27)")

        os.chmod(target, 0o600)
        data = load_credentials(target)
        check(data and data["uri"].startswith("mongodb"), "…and 0600 is accepted")
        os.chmod(target, 0o400)
        check(load_credentials(target) is not None,
              "…as is 0400: the mask `mode & 0o177` accepts both spellings of "
              "'only the owner can read this'")
        os.chmod(target, 0o700)
        check(raises(CredentialError, load_credentials, target),
              "…while 0700 is refused: a credentials file has no reason to be executable, "
              "and the mask is cheaper to be right about than a list of safe modes")

        # A symlink passes every mode check there is, and says nothing about its target.
        elsewhere = root / "elsewhere.json"
        elsewhere.write_text(payload, encoding="utf-8")
        os.chmod(elsewhere, 0o644)
        link = root / "link.json"
        os.symlink(elsewhere, link)
        check(raises(CredentialError, load_credentials, link),
              "a symlink is refused outright (a 0600 symlink to a 0644 file passes "
              "every mode check there is)")

        # Shape errors are refusals, not silent degrades: a chmod typo must not
        # quietly turn into "the mirror stopped working and nobody knows why".
        for bad, label in (("[]", "a JSON array"), ("not json", "unparseable text"),
                           ('{"uri": ""}', "an empty uri"), ('{"uri": 7}', "a non-string uri")):
            broken = root / "broken.json"
            broken.write_text(bad, encoding="utf-8")
            os.chmod(broken, 0o600)
            check(raises(CredentialError, load_credentials, broken),
                  f"{label} is a CredentialError, never a silent fallback to 'no mirror'")

        # save_credentials creates 0600 AT OPEN TIME, not by a later chmod.
        fresh = root / "fresh.json"
        save_credentials(SCHEME + "touch:pw@127.0.0.1:27017/touch_x", fresh)
        mode = stat.S_IMODE(os.lstat(fresh).st_mode)
        check(mode == 0o600, f"save_credentials writes mode 0600, got {mode:04o}")
        check(raises(CredentialError, save_credentials, "u", fresh),
              "…and refuses to clobber an existing file unless told to")
        check(load_credentials(fresh) is not None, "…producing a file it can read back")


def test_the_database_name_is_derived_and_fenced():
    print("test_the_database_name_is_derived_and_fenced")
    with tempfile.TemporaryDirectory() as tmp:
        one, two = Path(tmp) / "checkout-a", Path(tmp) / "checkout-b"
        one.mkdir()
        two.mkdir()
        name_one = database_name(one, env={})
        name_two = database_name(two, env={})

    check(re.fullmatch(r"touch_[0-9a-f]{8}", name_one),
          f"the derived name is touch_<sha1(realpath)[:8]>: {name_one}")
    check(name_one != name_two,
          "two checkouts on one machine never share a database (GD-12's wrong-target "
          "invariant, one layer down)")
    check(database_name(one, env={}) == name_one, "…and the derivation is stable")

    check(database_name(None, env={"TOUCH_MONGO_DB": "touch_scratch"}) == "touch_scratch",
          "TOUCH_MONGO_DB overrides it")
    check(raises(CredentialError, database_name, None, env={"TOUCH_MONGO_DB": "admin"}),
          "…but is fenced to the `touch` prefix: Touch never writes to, or drops, a "
          "database it did not construct (GD-27/GD-12)")
    for hostile in ("local", "config", "production", "touchdown_prod", "touchy", "touch"):
        check(raises(CredentialError, database_name, None, env={"TOUCH_MONGO_DB": hostile}),
              f"…including {hostile!r}")
    check(mr.DB_PREFIX == "touch_",
          f"the fence is the prefix WITH the underscore: {mr.DB_PREFIX!r} — a bare "
          f"'touch' admits `touchdown_prod`, a database nobody here constructed")

    # And both names this codebase constructs are inside the tighter fence.
    check(f"touch_test_{os.getpid()}".startswith(mr.DB_PREFIX),
          "the test suite's touch_test_<pid> is inside the same fence (GD-27)")
    check(name_one.startswith(mr.DB_PREFIX), "…as is the derived touch_<sha1> name")


def test_resolve_config_prefers_the_environment_and_never_publishes_the_uri():
    print("test_resolve_config_prefers_the_environment_and_never_publishes_the_uri")
    secret = "pa55word-in-the-uri"
    uri = SCHEME + f"touch:{secret}@127.0.0.1:27017/touch_x?authSource=touch_x"
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "mongo.json"
        save_credentials(SCHEME + "file:filepw@127.0.0.1:27017/touch_file", target)

        from_file = resolve_config(path=target, env={}, repo=tmp)
        check(from_file.source == "file" and from_file.configured,
              "the 0600 file is where credentials live")
        from_env = resolve_config(path=target, env={"TOUCH_MONGO_URI": uri}, repo=tmp)
        check(from_env.source == "env" and from_env.uri == uri,
              "…and TOUCH_MONGO_URI is how they are handed to the aggregator (GD-27)")
        check(resolve_config(path=Path(tmp) / "absent.json", env={}, repo=tmp).configured is False,
              "no file and no env is simply an unconfigured mirror")

    published = json.dumps(from_env.describe())
    check(secret not in published and uri not in published,
          f"describe() publishes no credential: {published}")
    check("127.0.0.1" in published, "…while keeping what an operator needs: the host")
    check(secret in "".join(from_env.secrets),
          "…and the password is on the redaction list, so it is scrubbed from messages")


def test_secrets_never_survive_redaction():
    print("test_secrets_never_survive_redaction")
    secret = "hunter2-hunter2-hunter2"
    uri = SCHEME + f"touch:{secret}@db.internal:27017/touch_x"

    check(secret not in redact_uri(uri), "redact_uri removes the userinfo")
    check("db.internal:27017" in redact_uri(uri), "…and keeps the host an operator needs")
    check(redact_uri("not-a-uri") == REDACTED,
          "an unrecognised string in a credentials slot is redacted WHOLE — it is not "
          "evidence that no credential is present")
    check(redact_uri(None) == REDACTED and redact_uri("") == REDACTED,
          "…and so are None and empty")

    # The realistic leak: a driver exception embeds the URI verbatim.
    message = f"ServerSelectionTimeoutError: {uri}, Timeout: 0.5s"
    scrubbed = redact(message, (uri, secret))
    check(secret not in scrubbed, f"a password inside a driver exception is removed: {scrubbed}")
    check("db.internal" in scrubbed, "…while the host survives for diagnosis")

    # The reformatted variant, where the structural pass alone would miss it.
    check(secret not in redact(f"auth failed for password {secret}", (uri, secret)),
          "…and a password quoted OUTSIDE a URI is removed by the literal pass")
    check(redact(None) == "", "redact never raises, not even on None")

    # The document backstop, for transcripts that quote an environment dump.
    payload = {"authToken": "abc123", "apiKeySource": "none", "authRequired": True,
               "nested": [{"password": "s3cret"}], "count": 3}
    scrubbed = scrub_value(payload)
    check(scrubbed["authToken"] == REDACTED, "secret-looking string fields are scrubbed")
    check(scrubbed["nested"][0]["password"] == REDACTED, "…at any depth, inside lists")
    check(scrubbed["apiKeySource"] == "none",
          "…while a classification the harness writes constantly is left alone (it is not "
          "a secret, and rewriting it would corrupt the mirrored record)")
    check(scrubbed["authRequired"] is True and scrubbed["count"] == 3,
          "…and non-strings are untouched: {'authRequired': true} is a classification")

    # Bare `key`/`keys` are the two names most likely to hold the real thing, so
    # the exemption is on the VALUE, not on the name. A blanket name exemption
    # let a quoted environment dump carry an API key straight through.
    leaked = scrub_value({"key": "sk-ant-api03-" + "A" * 40, "keys": ["a-token-value"]})
    check(leaked["key"] == REDACTED,
          f"a credential under the key `key` is redacted, not exempted: {leaked['key']!r}")
    kept = scrub_value({"key": "Enter", "keys": "none", "keyType": "ssh-ed25519"})
    check(kept["key"] == "Enter" and kept["keys"] == "none",
          f"…while a keystroke and a classification survive the backstop: {kept}")
    check(kept["keyType"] == "ssh-ed25519",
          "…and the unconditionally-exempt classification names are unchanged")
    check("key" not in mr.SECRET_KEY_EXEMPT and "keys" not in mr.SECRET_KEY_EXEMPT,
          "…because neither name is on the unconditional exempt list any more")

    # A URI password containing `@`, `/` or `:` MUST be percent-encoded to parse,
    # so the URI carries one spelling and any code that already decoded it quotes
    # the other. The literal pass is the belt behind the structural one; a belt
    # that knows only the encoded form misses every message that quotes the real
    # password.
    encoded, decoded = "p%40ss%2Fw0rd", "p@ss/w0rd"
    config = mr.MongoConfig(SCHEME + f"touch:{encoded}@127.0.0.1:27017/touch_x")
    check(decoded in config.secrets and encoded in config.secrets,
          f"both spellings of a percent-encoded password are on the redaction list: "
          f"{[s for s in config.secrets if 'ss' in s]}")
    leaked_message = f"AuthenticationFailed: bad auth for user touch with password {decoded}"
    check(decoded not in redact(leaked_message, config.secrets),
          "…so a driver message quoting the DECODED password is scrubbed too")

    # The literal pass has a floor, and it is a legibility rule rather than a
    # security one: `str.replace` has no word boundary, so searching for a
    # 1–2 character secret rewrites every occurrence of those characters and
    # destroys the host, the error class and the reason anyone opened /health.
    # The structural pass still covers such a password where it can actually
    # appear — inside a URI's userinfo, which is the form a driver embeds.
    check(mr._MIN_LITERAL_SECRET == 3,
          "the literal redaction pass has a documented minimum length")
    message = "ServerSelectionTimeoutError: 127.0.0.1:27017 timed out"
    check(redact(message, ["a"]) == message,
          f"…below which it declines rather than shredding the message: "
          f"{redact(message, ['a'])!r}")
    tiny = mr.MongoConfig(SCHEME + "touch:ab@127.0.0.1:27017/touch_x")
    check(REDACTED in redact(f"connection failed for {tiny.uri}", tiny.secrets),
          "…while the structural pass still removes a two-character password from "
          "the only place a driver exception puts one")


def test_the_deny_list_is_never_read():
    print("test_the_deny_list_is_never_read")
    for name in ("server.json", ".credentials.json", ".claude.json", "mongo.json"):
        check(is_denied_path(f"/home/someone/.claude/{name}"),
              f"{name} is never mirrored (GD-27) — not redacted downstream, never read")
    check(is_denied_path("/any/copy/of/server.json"),
          "…matched by basename, so a test tree and a second state root are covered too")
    check(not is_denied_path("/home/someone/.claude/projects/p/session.jsonl"),
          "…while ordinary transcripts are mirrored")
    check(is_denied_path(""), "an empty path is denied (fail closed)")

    # The backfill walk applies the deny-list at the SOURCE.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        projects = root / "projects" / "slug"
        projects.mkdir(parents=True)
        (projects / "session.jsonl").write_text("{}\n", encoding="utf-8")
        (projects / "notes.txt").write_text("x", encoding="utf-8")
        (root / "projects" / ".credentials.json").write_text("{}", encoding="utf-8")
        found = mr.iter_backfill_sources(root)

        # …and the claim is asserted rather than co-signed by the extension
        # filter. Every basename in DENY_BASENAMES ends `.json`, so a deny check
        # placed after the `.jsonl` filter can never fire: this assertion passed
        # identically with the deny-list deleted. Two ways to make the parameter
        # load-bearing in the test exactly as it is in the code:
        consulted = []

        def recording_deny(path):
            consulted.append(Path(path).name)
            return mr.is_denied_path(path)

        recorded = mr.iter_backfill_sources(root, deny=recording_deny)
        refused = mr.iter_backfill_sources(
            root, deny=lambda path: path.endswith("session.jsonl"))
    check([Path(p).name for p in found] == ["session.jsonl"],
          f"the backfill walk yields transcripts only, and no credential file: {found}")
    check("session.jsonl" in consulted and ".credentials.json" in consulted,
          f"…and the deny-list is genuinely consulted, for the transcript as well as "
          f"for the credential file: {sorted(set(consulted))}")
    check(recorded == found, "…without changing the answer when it answers the same way")
    check(refused == [],
          f"…and a deny rule that DOES name a .jsonl file is honoured — the refusal is "
          f"asked first, so it does not depend on the extension filter agreeing: {refused}")


def test_zero_configured_users_is_a_refusal_that_does_not_false_positive():
    print("test_zero_configured_users_is_a_refusal_that_does_not_false_positive")
    backend = MemoryBackend()
    backend.users = 0
    mirror = Mirror(MongoConfig("u", "touch_test"), backend=backend)
    state = asyncio.run(mirror.start())
    check(state == STATE_REFUSED,
          f"Touch refuses to mirror into a mongod reporting zero users, got {state!r}")
    check("zero configured users" in (mirror.last_error or ""),
          "…and /health says why (GD-27)")
    check("mongo.md" in (mirror.last_error or ""),
          "…pointing at the recipe that fixes it")

    # …and the refusal is booked as the POLICY refusal it is. `refused` means
    # three different things and the module keeps them apart everywhere else;
    # booking this one under `refused_no_lease` sent an operator hunting for a
    # second writer that a zero-user deployment does not have.
    from aggregator import refs
    refused_op = mr.MirrorOp(
        "records", refs.record_key("00000000-0000-4000-8000-000000000001"),
        ms.op_set({"sessionId": "292fc08c-923d-4ab4-8ff2-a9572417dbc8",
                   "type": "assistant", "provenance": "harness"}))
    check(mirror.enqueue([refused_op]) == 0, "a refusing mirror accepts nothing")
    check(mirror.stats["refused_policy"] == 1 and mirror.stats["refused_no_lease"] == 0,
          f"…counted under `refused_policy`, not under the lease counter: "
          f"policy={mirror.stats['refused_policy']} "
          f"lease={mirror.stats['refused_no_lease']} (GD-27 vs GD-29)")

    # The false positive that would matter: a least-privilege user cannot run
    # `usersInfo`, and reading "cannot enumerate" as "zero" would refuse to start
    # against exactly the correctly-secured deployment GD-27 asks for.
    backend2 = MemoryBackend()
    backend2.users = None
    healthy = Mirror(MongoConfig("u", "touch_test"), backend=backend2)
    check(asyncio.run(healthy.start()) == mr.STATE_LIVE,
          "…while 'we could not enumerate users' is the HEALTHY answer, not zero")


# --- the live docker arm (skips cleanly) ----------------------------------
def docker_unavailable():
    """A reason string, or None when the recipe can actually be provisioned."""
    if not shutil.which("docker"):
        return "docker is not installed"
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"docker is not usable: {exc}"
    if proc.returncode != 0:
        return "the docker daemon is not reachable"
    image = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
    if image.returncode != 0:
        return (f"the {IMAGE} image is not present locally (this test never pulls it — "
                f"1.2 GB is not a test dependency)")
    if not ms.pymongo_available():
        return "pymongo is not installed (GD-21: absence is legal)"
    return None


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def provisioned_argv(name, port, volume, password):
    """The documented recipe, re-scoped to this test run.

    Only the identifiers change — name, published port, volume, password. Every
    security-relevant flag (`--auth`, the loopback bind, the named volume, the
    image) comes from the page, so a documentation edit that weakens the recipe
    weakens the container these assertions run against, and they fail.
    """
    tokens = documented_docker_run()
    if tokens is None:
        return None
    out = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--name":
            out += ["--name", name]
            index += 2
            continue
        if token in ("-p", "--publish"):
            spec = tokens[index + 1]
            container_port = spec.rsplit(":", 1)[1]
            out += [token, f"127.0.0.1:{port}:{container_port}"]
            index += 2
            continue
        if token in ("-v", "--volume"):
            target = tokens[index + 1].partition(":")[2]
            out += [token, f"{volume}:{target}"]
            index += 2
            continue
        if token == "-e":
            entry = tokens[index + 1]
            key = entry.split("=", 1)[0]
            out += ["-e", f"{key}={password}" if key.endswith("PASSWORD") else entry]
            index += 2
            continue
        out.append(token)
        index += 1
    return out


def wait_for_mongod(port, timeout=90.0):
    """Poll until the server answers `ping` (which auth permits anonymously)."""
    import pymongo
    from pymongo.errors import OperationFailure, PyMongoError

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client = pymongo.MongoClient(f"127.0.0.1:{port}", serverSelectionTimeoutMS=500,
                                     connectTimeoutMS=500)
        try:
            client.admin.command("ping")
            client.close()
            return True
        except OperationFailure:
            client.close()
            return True                     # answering at all is what we waited for
        except PyMongoError:
            client.close()
            time.sleep(0.4)
    return False


def test_the_recipe_provisions_a_database_nobody_else_can_reach():
    print("test_the_recipe_provisions_a_database_nobody_else_can_reach")
    reason = docker_unavailable()
    if reason:
        skip(f"live docker arm: {reason}")
        return

    name = f"touch-mongo-test-{os.getpid()}"
    volume = f"{name}-data"
    port = free_port()
    password = "test-root-" + os.urandom(6).hex()
    argv = provisioned_argv(name, port, volume, password)
    check(argv is not None, "the recipe parsed out of the docs is runnable")
    if argv is None:
        return
    check("--auth" in argv and any(a.startswith("127.0.0.1:") for a in argv),
          "…and the provisioned container keeps the doc's --auth and loopback bind")

    # Past this point docker has already been established as usable by
    # `docker_unavailable()` (daemon reachable, image present). So a failure here
    # is a failure of the DOCUMENTED RECIPE — which is precisely what R-42 asks
    # this arm to catch — and converting it into a skip would hide the one thing
    # the arm exists for. Only the pre-flight reasons above skip.
    subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)
    started = subprocess.run(argv, capture_output=True, text=True)
    check(started.returncode == 0,
          f"the documented `docker run` recipe starts, verbatim: "
          f"{started.stderr.strip()[:200]}")
    if started.returncode != 0:
        return
    try:
        ready = wait_for_mongod(port)
        check(ready, "…and the container it describes becomes ready inside 90 s")
        if not ready:
            return
        _live_deployment_checks(name, port, password)
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)
        subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True)


def _live_deployment_checks(name, port, password):
    import pymongo
    from pymongo.errors import OperationFailure

    # 1. The bind, as the kernel sees it — not as the docs claim it.
    ports = subprocess.run(["docker", "port", name], capture_output=True, text=True).stdout
    check("127.0.0.1:" in ports,
          f"the running container publishes on loopback only: {ports.strip()!r}")
    check("0.0.0.0" not in ports and ":::" not in ports,
          f"…and on no other address: {ports.strip()!r}")

    # 2. An unauthenticated connect cannot read or write. THIS is R-42's arm:
    #    the probe that motivated GD-27 found a plain `mongo:7` with zero users
    #    accepting anonymous connections.
    anon = pymongo.MongoClient(f"127.0.0.1:{port}", serverSelectionTimeoutMS=1000)
    for label, action in (
            ("list databases", lambda: anon.admin.command("listDatabases")),
            ("enumerate users", lambda: anon.admin.command({"usersInfo": {"forAllDBs": True}})),
            ("read a collection", lambda: anon["touch_test_probe"]["x"].find_one({})),
            ("write a document", lambda: anon["touch_test_probe"]["x"].insert_one({"_id": "a"})),
    ):
        check(raises(OperationFailure, action),
              f"an unauthenticated client cannot {label} (GD-27)")
    anon.close()

    # 3. The root user works, and a mongod WITH users does not trip the refusal.
    root_uri = (SCHEME + f"touchadmin:{password}@127.0.0.1:{port}/?authSource=admin")
    root = pymongo.MongoClient(root_uri, serverSelectionTimeoutMS=2000)
    info = root.admin.command({"usersInfo": {"forAllDBs": True}})
    check(len(info.get("users", [])) >= 1,
          f"the recipe leaves a mongod with configured users: "
          f"{[u.get('user') for u in info.get('users', [])]}")

    # 4. The documented least-privilege bootstrap (§2) actually runs, and the
    #    role it creates is what enforces GD-26 at the SERVER, not in review.
    db_name = f"touch_test_{os.getpid()}"
    app_password = "test-app-" + os.urandom(6).hex()
    script = documented_user_bootstrap(db_name, app_password)
    check(script is not None, "docs/mongo.md documents a user-bootstrap script (R-42)")
    if script is None:
        root.close()
        return
    result = subprocess.run(
        ["docker", "exec", "-i", name, "mongosh", "-u", "touchadmin", "-p", password,
         "--authenticationDatabase", "admin", "--quiet"],
        input=script, capture_output=True, text=True, timeout=120)
    check(result.returncode == 0,
          f"the documented role/user bootstrap runs as written: {result.stderr.strip()[:200]}")
    if result.returncode != 0:
        root.close()
        return

    app_uri = (SCHEME + f"touch:{app_password}@127.0.0.1:{port}/{db_name}"
               f"?authSource={db_name}")
    asyncio.run(_live_mirror_checks(app_uri, db_name))

    root.drop_database(db_name)
    root.close()


def documented_user_bootstrap(db_name, password):
    """§2's mongosh script, re-scoped: the doc's own text, minimally substituted.

    `passwordPrompt()` needs a TTY, and the database name is a placeholder — so
    exactly those two are replaced, and the substitution is asserted to have
    happened. A silent no-op substitution would leave this testing nothing.
    """
    for block in fenced_blocks(doc_text(), "bash"):
        if "createRole" not in block:
            continue
        start = block.find("const ")
        end = block.rfind("EOF")
        script = block[start:end if end > start else len(block)]
        script = re.sub(r'const db_name = "[^"]*";', f'const db_name = "{db_name}";', script)
        script = script.replace("passwordPrompt()", json.dumps(password))
        if db_name not in script or "passwordPrompt" in script:
            return None
        return script
    return None


async def _live_mirror_checks(uri, db_name):
    from aggregator import refs
    from aggregator.mirror import AsyncBackend, MirrorOp

    backend = await AsyncBackend.connect(uri, db_name)
    mirror = Mirror(MongoConfig(uri, db_name), backend=backend)
    state = await mirror.start()
    health = mirror.health()
    check(state == mr.STATE_LIVE,
          f"Touch reaches 'live' against the documented deployment, as the "
          f"least-privilege user, got {state!r} "
          f"({health['lastError']} / {health['notes']})")
    check(await backend.user_count() is None,
          "…and the least-privilege user cannot enumerate users, which Touch reads as "
          "healthy rather than as zero (the GD-27 false positive that would matter)")
    check(health["lastError"] is None,
          f"…with NO lastError on /health: the deployment the docs ask for must not "
          f"look like an unresolved fault to whoever is on call: {health['lastError']!r}")

    session = "292fc08c-923d-4ab4-8ff2-a9572417dbc8"
    key = refs.record_key("00000000-0000-4000-8000-000000000001")
    mirror.enqueue([MirrorOp("records", key,
                             ms.op_set({"sessionId": session, "type": "assistant",
                                        "provenance": "harness"}))])
    await mirror.flush()
    check((await backend.counts()).get("records") == 1,
          "…and a mirrored document lands through the whole real path")

    # GD-26 enforced by the database itself: the ingest role has no `remove` on
    # `records`, so a bug that tried to delete history fails at the server.
    from pymongo.errors import OperationFailure
    refused = False
    try:
        await backend.db["records"].delete_many({"sessionId": session})
    except OperationFailure:
        refused = True
    check(refused,
          "the SERVER refuses a delete on `records` — with the documented role, the only "
          "thing standing between the CLI's destruction of history and the mirror "
          "re-importing it is no longer a code review (GD-26)")

    allowed = True
    try:
        await backend.db["stream_meta"].delete_many({"sessionId": "no-such-session"})
    except OperationFailure:
        allowed = False
    check(allowed,
          "…while the ONE legal delete (renumbered positional stream_meta) is permitted")

    # §2 grants the role `dropCollection` on `derived` alone, for GD-23's
    # drop-and-rebuild. Nothing exercised that grant until here, so the docs
    # could have described a privilege the mirror never actually needs — or,
    # worse, omitted one it does.
    await backend.db["derived"].insert_one(
        {"_id": "d1", "reducerVersion": "1", "derivedFromSeq": 5, "provenance": "derived"})
    check((await backend.counts()).get("derived") == 1, "a derived document exists to drop")
    result = await mirror.rebuild([])
    check(result["droppedDerived"] is True and (await backend.counts()).get("derived", 0) == 0,
          "--rebuild drops the reducer-owned collection AS THE DOCUMENTED ROLE — the "
          "`dropCollection` grant in docs/mongo.md §2 is real and is the right one")

    refused_drop = False
    try:
        await backend.db["records"].drop()
    except OperationFailure:
        refused_drop = True
    check(refused_drop,
          "…and the grant is scoped: the same role cannot drop `records` (GD-26)")

    await backend.close()


def main():
    for test in (
        test_the_documented_recipe_is_loopback_and_authenticated,
        test_the_docs_never_show_a_reachable_mongo,
        test_the_docs_carry_r57s_clauses,
        test_no_connection_string_literal_under_aggregator,
        test_gitignore_covers_the_mongo_artifacts,
        test_the_credentials_file_must_be_unreadable_to_everyone_else,
        test_the_database_name_is_derived_and_fenced,
        test_resolve_config_prefers_the_environment_and_never_publishes_the_uri,
        test_secrets_never_survive_redaction,
        test_the_deny_list_is_never_read,
        test_zero_configured_users_is_a_refusal_that_does_not_false_positive,
        test_the_recipe_provisions_a_database_nobody_else_can_reach,
    ):
        test()
    print()
    for message in skips:
        print(f"skipped: {message}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("all mongo deployment (R-42 / R-57 mongo-doc) tests passed")


if __name__ == "__main__":
    main()
