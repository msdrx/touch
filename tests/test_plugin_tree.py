#!/usr/bin/env python3
"""The shipping subtree `plugin/touch/`: manifests, the monitoring subset, LICENSE.

Item 07 (CM-1, PLUGIN-SPEC-9/12/14/17/18, DISTRIBUTION-1), rewritten by GD-U1.
Run as `python3 test_plugin_tree.py`; exits non-zero on failure. No pytest, no
runner — `run_all.sh` picks it up by its `test_*.py` glob.

WHAT THIS FILE IS FOR
---------------------
GD-T2 ships the plugin as a tracked subtree, `plugin/touch/`, because the
manifest schema has no `files`/`exclude`/`ignore` field: the directory boundary
is the only exclusion primitive there is (PLUGIN-SPEC-9, DISTRIBUTION-1).

GD-U1 finished the job: `plugin/touch/` is now the SINGLE canonical home for
`aggregator/`, `touch-visual/`, `docs/` and the monitoring core. There is no
second copy anywhere, so the arms that used to police one — the sync script,
byte-equality per pinned path, strays-in-a-pinned-tree, canonical-fully-pinned
— have no subject and are gone with `scripts/sync_plugin.sh`. Their properties
did not weaken; they dissolved. A new `aggregator/` module now ships because it
IS the payload, not because a copier was re-run.

Two properties survive the move, and one is new:

  the monitoring subset   the payload's `shared/monitoring/` holds EXACTLY the
                          six shipping files. Its dev-only material (tests,
                          fixtures, `gen_stream.py`) lives at `tests/monitoring/`
                          (GD-U6) — 1.1 MB of PII-slug carriers that must never
                          creep back across the directory boundary.
  the manifests           name, version, description, disclosure clauses,
                          component keys, the single marketplace entry.

                          The two manifests live in DIFFERENT trees and this
                          file is where that is asserted. `plugin.json` is
                          payload — it ships, and the cache copy is what a
                          consumer loads. `marketplace.json` is a CATALOG about
                          the payload, and a git-cloned catalog is only ever
                          read from `<repo>/.claude-plugin/marketplace.json`:
                          there is no subdirectory form (CLI 2.1.220 refuses
                          `owner/repo/sub/dir` outright and looks nowhere else
                          after cloning). So it sits at the repo root, naming
                          the payload with `"source": "./plugin/touch"`, and
                          the payload must NOT carry a second copy of it —
                          which is also why the payload's `.claude-plugin/` is
                          held to an EXACT set (`plugin.json`, nothing else)
                          and why the root catalog must be TRACKED, not merely
                          on disk: an untracked catalog publishes a repository
                          with no marketplace at all.
  LICENSE (new, GD-U7)    PLUGIN-SPEC-17 wants a LICENSE at the repo root AND
                          at the plugin root, so it is the ONE deliberate
                          duplicate in the tree. The byte-equality check the
                          sync script used to make is re-homed here.

Skips: `claude plugin validate` and `claude plugin details` need the CLI, which
a clean checkout or a CI image may not have; those arms skip with a printed
reason (the suite's mongod convention). Everything else is pure filesystem and
always runs.
"""
import json
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
from _roots import CATALOG, PAYLOAD, REPO               # noqa: E402

PLUGIN = PAYLOAD
MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"
#: The catalog, at the REPO root — not beside `plugin.json`. `/plugin
#: marketplace add msdrx/touch` clones this repository and reads exactly
#: `<clone>/.claude-plugin/marketplace.json`; a manifest anywhere else is
#: invisible to it. Named through `_roots.CATALOG` rather than spelled out
#: again here: the longhand literal in three files is how the last move
#: half-landed (RELEASE-TESTS-15).
MARKETPLACE = CATALOG

#: What that entry's `source` must say, and the tree it must resolve to.
MARKETPLACE_SOURCE = "./plugin/touch"

#: The payload's `.claude-plugin/` holds exactly this, and CLAUDE.md and
#: CONTRIBUTING both say so in prose. GD-U5 is the reason: the hook manifest
#: lives at `hooks/hooks.json`, beside the script it registers, and a
#: `.claude-plugin/hooks.json` added "helpfully" tomorrow is read by nobody —
#: `claude plugin validate` does not look there either, so a denylist of one
#: name (`marketplace.json`) leaves the whole rest of the directory silent.
PLUGIN_MANIFEST_DIR_CONTENTS = {"plugin.json"}

#: `category` is a free-form string in the entry schema — the CLI's own
#: declaration (2.1.220) describes it as *"Category for organizing plugins
#: (e.g. \"productivity\", \"development\")"* and enumerates nothing. Those two
#: examples are therefore the entire documented vocabulary, so the choice is
#: pinned to them: Touch is developer tooling (a dashboard over a coding
#: session's agent tree, plus engineering-practice skills), which makes
#: `development` the closer of the two. Widen this set only against a
#: documented value, never to legitimise a coinage.
DOCUMENTED_CATEGORIES = {"productivity", "development"}

#: The payload's monitoring tree holds exactly these six files and nothing
#: else. Stated positively (an exact set, not a denylist) because that is the
#: form a silent SHRINK also fails: an empty directory passes every "none of
#: the bad names arrived" check ever written.
#:
#: `memory.html` is the sixth, and it is a SECOND page rather than a section of
#: `monitor.html` on purpose (G4): the dashboard is 2,700 lines with
#: enumerated view-gating CSS and insertion-fragile text-marker test slices,
#: while the memory editor has to be small enough to drive under a real
#: `node` + `vm` harness. It lives here, inside an existing owned tree, so the
#: feature adds no top-level payload directory (G12) and `TOP_ALLOWLIST` is
#: untouched.
MONITORING_CORE = {
    "status.sh", "monitor_server.py", "decision_watcher.py",
    "monitor.html", "memory.html", "monitoring.md",
}

#: GD-T2's never-ship list, kept as an explicit arm on top of the exact-set one
#: so the failure message names the thing that came back. These are the paths
#: GD-U6 moved OUT to `tests/monitoring/`; `tests/` alone is ~1.1 MB and its
#: fixtures and dev scripts carry the author's home-directory slug.
MONITORING_NEVER_SHIP = (
    "tests", "fixtures", "gen_stream.py", "test_perf_work.py", "test_ws_e2e.py",
    "test_server.py", "test_shell.py", "test_watcher.py", "test_frontend.py",
)

#: GD-T6 keeps the component table closed: these are wired up by nobody, in
#: either manifest, until a plan says otherwise.
COMPONENT_KEYS = (
    "commands", "agents", "skills", "hooks", "mcpServers", "lspServers",
    "workflows", "monitors", "outputStyles", "themes", "channels",
)

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

#: Verbs the shipped description may not use while no SESSION verb ships. The
#: `/plugin` UI shows the description and never the README (DISTRIBUTION-7), so
#: this string is the entire pre-install surface — the one place a claimed
#: capability cannot be contradicted by the UI before the user has believed it.
#: `README.md:23` and `docs/control-semantics.md:7` both say the control plane
#: is unshipped; D13 ("a control is rendered only where it can be honest") and
#: the R-58 precedent (a fabricated FAILED badge) are the law being kept here.
#: When a control verb really ships, DELETE the matching alternative from this
#: pattern deliberately — that edit is the point of the assertion.
UNSHIPPED_VERBS = re.compile(r"\b(steer|control|stop|restart|terminate|kill)\b",
                             re.IGNORECASE)

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


def load_json(path):
    """Parsed JSON, or None (with a recorded failure) if it will not parse."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        check(False, f"{path.relative_to(REPO)} parses as JSON ({exc})")
        return None


def have_cli():
    return shutil.which("claude") is not None


def have_git():
    """True when REPO is a git checkout (an unpacked `git archive` is not)."""
    try:
        res = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def run_cli(args, timeout=180):
    """CompletedProcess, or None when the CLI is missing/hangs/errors out."""
    try:
        return subprocess.run(
            ["claude", *args], cwd=str(REPO),
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


# --- the payload's monitoring subset: exactly the five shipping files
def test_monitoring_subset_ships_nothing_extra():
    print("test_monitoring_subset_ships_nothing_extra")
    root = PLUGIN / "shared" / "monitoring"
    check(root.is_dir(), "plugin/touch/shared/monitoring/ exists")
    if not root.is_dir():
        return
    present = {p.name for p in root.iterdir()}
    leaked = sorted(present & set(MONITORING_NEVER_SHIP))
    check(not leaked,
          f"none of GD-T2's never-ship monitoring paths is in the payload "
          f"(found: {leaked})")
    # EXACT equality, not a subset in either direction. GD-U1 makes this tree
    # canonical, so "extra" now means "someone put a dev-only file in the
    # module" rather than "the copier over-copied" — and the shrink half still
    # matters: an empty directory passes every denylist ever written.
    check(present == MONITORING_CORE,
          f"the payload's shared/monitoring/ holds exactly the six shipping "
          f"files (missing: {sorted(MONITORING_CORE - present)}, "
          f"extra: {sorted(present - MONITORING_CORE)})")


# --- the manifests
def test_plugin_manifest():
    print("test_plugin_manifest")
    check(MANIFEST.is_file(), "plugin/touch/.claude-plugin/plugin.json exists")
    # EXACT equality, the same idiom (and the same reason) as the monitoring
    # subset above: `.claude-plugin/` is "exactly one file" in the prose, so it
    # is exactly one file here. A stowaway `hooks.json` or a second catalog now
    # FAILS instead of being silently ignored by every reader in the stack
    # (PAYLOAD-5, GD-U5). Runs before the early return: an empty directory has
    # to fail loudly too.
    mdir = MANIFEST.parent
    present = {p.name for p in mdir.iterdir()} if mdir.is_dir() else set()
    check(present == PLUGIN_MANIFEST_DIR_CONTENTS,
          f"the payload's .claude-plugin/ holds exactly "
          f"{sorted(PLUGIN_MANIFEST_DIR_CONTENTS)} (missing: "
          f"{sorted(PLUGIN_MANIFEST_DIR_CONTENTS - present)}, extra: "
          f"{sorted(present - PLUGIN_MANIFEST_DIR_CONTENTS)})")
    if not MANIFEST.is_file():
        return
    m = load_json(MANIFEST)
    if m is None:
        return
    check(m.get("$schema") ==
          "https://json.schemastore.org/claude-code-plugin-manifest.json",
          "$schema points at the plugin-manifest schema (editor validation, P2)")
    # GD-T1: no rename. `name` is the namespace (`/touch:orchestrate`);
    # `displayName` is the sanctioned escape hatch for any later rebrand.
    check(m.get("name") == "touch", "name is 'touch' (GD-T1)")
    check(m.get("displayName") == "Touch", "displayName is 'Touch' (GD-T1)")
    check(SEMVER.match(str(m.get("version", ""))),
          f"version is semver (got {m.get('version')!r})")
    desc = m.get("description", "")
    # DISTRIBUTION-7: the /plugin UI shows the description and never the
    # README, so this string carries the whole pitch AND the disclosure.
    check(isinstance(desc, str) and len(desc) >= 80,
          f"description is a real sentence, not a label ({len(desc)} chars)")
    check("hook" in desc.lower(),
          "description discloses the hook (DISTRIBUTION-7/GD-T8)")
    claimed = sorted({m_.group(0).lower() for m_ in UNSHIPPED_VERBS.finditer(desc)})
    check(not claimed,
          f"description claims no session-control verb — none ships "
          f"(README.md:23, D13; found: {claimed})")
    # The memory write plane (G6/SECURITY-13). This string is the WHOLE
    # pre-install disclosure — the `/plugin` UI shows it and never the README —
    # so the two halves are asserted separately: the stale claim must be gone,
    # and the replacement must be specific enough to be a decision.
    #
    # "read-only" as a bare adjective for the product is now false: the dashboard
    # writes `<project>/.touch/memory/*.md`. It may still describe a PART (the
    # skills' read-only researchers, a read-only tap), so the arm forbids the
    # word only where it qualifies Touch or its dashboard — the shape the old
    # string used ("a read-only, loopback-only, token-gated dashboard").
    stale = re.search(r"read-only[^.]{0,60}\bdashboard\b", desc, re.IGNORECASE)
    check(stale is None,
          f"the description no longer sells a read-only dashboard — one write "
          f"plane ships (found: {stale.group(0) if stale else None!r})")
    check(".touch/memory" in desc,
          "the description names the ONE directory Touch writes on the user's "
          "behalf (SECURITY-13)")
    check("--allow-memory-write" in desc,
          "the description names the flag that turns the write plane on, so "
          "'off by default' is checkable before installing (G6)")
    # GD-T8's other two disclosure clauses, asserted rather than assumed: the
    # transcript read and the project-local write. Both were written into the
    # string once and could be edited back out by anyone trimming for length.
    check("~/.claude/projects" in desc,
          "description names the transcript directory it reads (GD-T8)")
    check(m.get("license") == "MIT", "license is MIT")
    # This repository IS the marketplace: its root carries the catalog and the
    # catalog names `./plugin/touch`, so an install clones THIS repo and both
    # links have to point at it. They named `msdrx/touch-plugin` — a separate,
    # empty-history release repo — while that was the publish target; anyone
    # reviving that model changes these two lines and the catalog together, or
    # the plugin page links somewhere the plugin no longer comes from.
    check(m.get("homepage", "").endswith("msdrx/touch"),
          "homepage points at the marketplace repo")
    check(str(m.get("repository", "")).endswith("msdrx/touch"),
          "repository points at the repo the catalog is served from")
    check(isinstance(m.get("keywords"), list) and m["keywords"],
          "keywords is a non-empty list")
    # PLUGIN-SPEC-14: a plugin whose hook fires on every tool call installs
    # DISABLED. The user opts in after reading the description.
    check(m.get("defaultEnabled") is False, "defaultEnabled is false (GD-T6)")

    author = m.get("author") or {}
    check(author.get("name") and author.get("url"),
          "author carries a name and a url")
    check("email" not in author,
          "author carries no email (it would ship in every install)")

    uc = (m.get("userConfig") or {}).get("run_scope_guard")
    check(isinstance(uc, dict), "userConfig declares run_scope_guard (GD-T6)")
    if isinstance(uc, dict):
        check(uc.get("type") == "boolean", "run_scope_guard is a boolean")
        check(uc.get("default") is True,
              "run_scope_guard defaults to true (the guard is on by default)")
    declared = [k for k in COMPONENT_KEYS if k in m]
    check(not declared,
          f"plugin.json declares no component paths — the conventional "
          f"directories are discovered (GD-T6; declared: {declared})")


def test_marketplace_manifest():
    print("test_marketplace_manifest")
    check(MARKETPLACE.is_file(),
          ".claude-plugin/marketplace.json exists at the REPO root")
    # The payload must not carry a second catalog. Two files declaring the
    # marketplace name `msdrx-tools` is one catalog too many — a user adding
    # both gets whichever came last (same name replaces), and nothing keeps the
    # two equal. This is the arm that fails if the old plugin-local copy comes
    # back rather than moves.
    stowaway = PLUGIN / ".claude-plugin" / "marketplace.json"
    check(not stowaway.exists(),
          "the payload carries NO marketplace.json — the catalog is not payload")
    if not MARKETPLACE.is_file():
        return
    m = load_json(MARKETPLACE)
    if m is None:
        return
    #: `plugin.json` is the other half of every cross-manifest arm below. It is
    #: loaded here rather than passed in so this function stays runnable on its
    #: own; `test_plugin_manifest` owns the arms about its CONTENTS.
    pm = load_json(MANIFEST) if MANIFEST.is_file() else None
    if not isinstance(pm, dict):
        pm = {}
    check(m.get("name") == "msdrx-tools", "marketplace name is 'msdrx-tools' (GD-T3)")
    # MANIFESTS-10: the plugin manifest's `$schema` is pinned twenty lines up
    # and this one was not, so a typo here was silent — the field is ignored at
    # load time by design and `claude plugin validate` passes regardless. Pinned
    # symmetrically. (That the URL SERVES a schema is an operator check from an
    # unblocked host, GD-C10; schemastore is network-blocked from this sandbox,
    # so the test pins the string the official catalog declares, not a 200.)
    check(m.get("$schema") ==
          "https://json.schemastore.org/claude-code-marketplace.json",
          "$schema points at the marketplace schema (editor validation)")
    # --strict turns a missing marketplace description into an error (E2).
    check(bool(m.get("description")), "marketplace carries a description")
    owner = m.get("owner") or {}
    check(bool(owner.get("name")), "owner carries a name")
    check("email" not in owner, "owner carries no email")
    # MANIFESTS-8 / PAYLOAD-10 / DOCS-12 / RELEASE-TESTS-12: `owner.url` is
    # "Website, GitHub profile, or organization URL" — the MAINTAINER, not the
    # catalog's repository, which `plugin.json` already states twice
    # (`homepage`, `repository`). It was quietly repointed at the repo during
    # the move, leaving two adjacent manifests disagreeing about one person's
    # URL. Pinned to agreement rather than to a literal: whichever profile the
    # maintainer moves to, the two files move together or this fails.
    check(owner.get("url") and owner.get("url") == (pm.get("author") or {}).get("url"),
          f"owner.url equals plugin.json's author.url "
          f"({owner.get('url')!r} vs {(pm.get('author') or {}).get('url')!r})")

    plugins = m.get("plugins") or []
    check(len(plugins) == 1, f"exactly one plugin entry ({len(plugins)})")
    if len(plugins) != 1:
        return
    e = plugins[0]
    # PLUGIN-SPEC-14: keep the entry name and plugin.json's name identical, so
    # the "which name namespaces components" ambiguity cannot bite.
    check(e.get("name") == "touch", "entry name matches plugin.json's name")
    # The marketplace root is the REPO root, and relative sources resolve
    # against it (never against `.claude-plugin/`), so the entry names the
    # payload subtree. `../` is rejected by the validator; `./` would offer the
    # whole development repo as the plugin.
    check(e.get("source") == MARKETPLACE_SOURCE,
          f"entry source is '{MARKETPLACE_SOURCE}' (got {e.get('source')!r})")
    # And it resolves to a real plugin: a catalog pointing at a directory with
    # no manifest installs nothing, and no schema check catches that.
    resolved = (MARKETPLACE.parent.parent / str(e.get("source", ""))).resolve()
    check(resolved == PLUGIN.resolve(),
          f"the source resolves to the payload tree ({resolved})")
    check((resolved / ".claude-plugin" / "plugin.json").is_file(),
          "the resolved source carries .claude-plugin/plugin.json")
    # --- the card fields (SPEC-5, MANIFESTS-11; GD-C9)
    # The entry IS the pre-install card in `/plugin`'s Discover tab, and for a
    # local/custom marketplace that card is thin by design (no context cost, no
    # last-updated). It is also where a user decides whether to install a
    # plugin that ships a PreToolUse hook, so the fields that carry no trust
    # claim are worth filling in. `displayName` is the one that must not drift:
    # a surface reading the catalog WITHOUT the payload would otherwise show
    # `touch` where the plugin calls itself `Touch`.
    check(e.get("displayName") and e.get("displayName") == pm.get("displayName"),
          f"entry displayName equals plugin.json's "
          f"({e.get('displayName')!r} vs {pm.get('displayName')!r})")
    check(e.get("category") in DOCUMENTED_CATEGORIES,
          f"entry declares a documented category "
          f"(got {e.get('category')!r}, documented: "
          f"{sorted(DOCUMENTED_CATEGORIES)})")
    tags = e.get("tags")
    keywords = set(pm.get("keywords") or [])
    check(isinstance(tags, list) and tags, f"entry carries tags ({tags!r})")
    # Tags are marketplace-only (nothing in `plugin.json` shadows them), so
    # GD-T9 does not forbid them — but they are the same vocabulary as
    # `keywords`, and a tag that is not a keyword is a second description of
    # what Touch is, drifting on its own. Subset, not equality: the card may
    # carry fewer terms than the manifest's search keywords.
    check(isinstance(tags, list) and set(tags) <= keywords,
          f"entry tags are drawn from plugin.json's keywords "
          f"(stray: {sorted(set(tags or []) - keywords)})")
    # DISTRIBUTION-5: version resolution is plugin.json > entry > commit SHA,
    # first set wins. Two versions is a way to ship a stale one forever.
    check("version" not in e,
          "entry declares NO version — semver lives in plugin.json only (GD-T9)")
    # Same reasoning one field over. `plugin.json` owns the description, and it
    # is the copy carrying GD-T8's disclosure clauses; an entry description is
    # a second copy of a trust-bearing string with nothing keeping the two
    # equal, and the precedence between them is not established by any finding
    # in this run's research. One owner, as with `version`.
    #
    # Where the line falls, since the entry now DOES carry card fields
    # (GD-C9): `displayName` is duplicated but pinned equal above and carries
    # no claim about behaviour; `category` and `tags` have no `plugin.json`
    # counterpart to contradict. `version` and `description` are the two the
    # entry stays out of — a stale version ships forever, and a short entry
    # description would be a trust-bearing string competing with the one that
    # carries GD-T8's disclosures.
    check("description" not in e,
          "entry carries no description — plugin.json owns it (GD-T9 pattern)")
    declared = [k for k in COMPONENT_KEYS if k in e]
    # PLUGIN-SPEC-18: with strict: true (the default) the entry SUPPLEMENTS
    # plugin.json; declaring components in both is a hard conflict error.
    check(not declared,
          f"entry declares no components (strict-mode conflict; found: {declared})")


def test_catalog_is_tracked():
    """The catalog must be in the INDEX, not merely on disk (RELEASE-TESTS-7).

    The whole distribution model is "the pushed tree carries
    `.claude-plugin/marketplace.json`". `MARKETPLACE.is_file()` above is
    satisfied by an untracked file, so a catalog that was never `git add`ed
    passes every other arm in this file while the published repository has no
    marketplace at all — and `release.sh` step 6 gates on the same fact from
    its side.
    """
    print("test_catalog_is_tracked")
    if not have_git():
        skip("not a git checkout (no `git rev-parse --git-dir`) — nothing can "
             "be tracked here; an unpacked archive only carries what was")
        return
    rel = MARKETPLACE.relative_to(REPO).as_posix()
    try:
        res = subprocess.run(["git", "ls-files", "--error-unmatch", "--", rel],
                             cwd=str(REPO), capture_output=True, text=True,
                             timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        check(False, f"git ls-files runs ({exc})")
        return
    check(res.returncode == 0,
          f"{rel} is tracked by git (rc={res.returncode}; an untracked catalog "
          f"publishes a repository with no marketplace)")


def test_license():
    print("test_license")
    root_license = REPO / "LICENSE"
    check(root_license.is_file(), "LICENSE exists at the repo root")
    check((PLUGIN / "LICENSE").is_file(), "LICENSE exists at the plugin root")
    if not root_license.is_file():
        return
    text = root_license.read_text(encoding="utf-8")
    # A `"license": "MIT"` manifest field with no LICENSE file is a false claim
    # (PLUGIN-SPEC-17). This is the "and it really is MIT" half.
    check("MIT License" in text, "LICENSE is the MIT licence")
    check("Michael Sadradze" in text, "LICENSE names the copyright holder")
    # GD-U7: after GD-U1 moved every other pinned tree INTO the payload, this
    # pair is the ONE deliberate duplicate left in the repo — PLUGIN-SPEC-17
    # wants a LICENSE at the repo root and at the plugin root, and no layout
    # dissolves that. `scripts/sync_plugin.sh` used to keep the two equal; the
    # script is gone, so the check is re-homed here, two lines, byte for byte.
    # No `if` around it: a deleted plugin LICENSE must fail HERE too, not
    # degrade this arm to silence while only the existence arm above speaks.
    plugin_license = PLUGIN / "LICENSE"
    check(plugin_license.is_file()
          and root_license.read_bytes() == plugin_license.read_bytes(),
          "the plugin's LICENSE is byte-equal to the root's (GD-U7: the one "
          "deliberate duplicate)")


# --- the CLI arms (skip when `claude` is absent)
def test_manifests_validate():
    print("test_manifests_validate")
    if not have_cli():
        skip("`claude` CLI not on PATH — manifest validation not run")
        return
    # GD-T7: ALWAYS by explicit file path. A directory-level run does walk into
    # locally-resolvable entries, but remote-source entries go unchecked, and
    # the explicit form is immune to that trap.
    for path in (MANIFEST, MARKETPLACE):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO)
        res = run_cli(["plugin", "validate", "--strict", str(path)])
        if res is None:
            skip(f"`claude plugin validate` did not complete for {rel}")
            continue
        check(res.returncode == 0,
              f"claude plugin validate --strict {rel} passes "
              f"(rc={res.returncode}, {(res.stdout + res.stderr).strip()[-300:]})")


def test_plugin_details():
    print("test_plugin_details")
    if not have_cli():
        skip("`claude` CLI not on PATH — plugin details not run")
        return
    res = run_cli(["--plugin-dir", str(PLUGIN), "plugin", "details", "touch"])
    # `have_cli()` above already covers the one legitimate skip (prerequisite
    # absent) and `run_cli` returns None only for a spawn failure or a timeout.
    # A CLI that RAN and refused the payload is a failure, never a skip — this
    # is the only arm covering DISTRIBUTION-14's hook inventory, so a green
    # skip here would hide exactly the thing it exists to catch.
    if res is None:
        skip("`claude --plugin-dir ...` did not spawn/finish — details not run")
        return
    check(res.returncode == 0,
          f"claude --plugin-dir ... plugin details touch loads the payload "
          f"(rc={res.returncode}, {(res.stdout + res.stderr).strip()[-300:]})")
    out = res.stdout
    # FOUR hook EVENTS, still exactly TWO scripts and exactly ONE registration
    # file (`hooks/hooks.json`) — GD-U5's rule is about registration sites, not
    # event count, and `test_scope_guard.py` / `test_agent_lifecycle.py` own the
    # per-event shape. The count moved 1 -> 4 when D-18's lifecycle/bind/
    # provenance pack landed on the back of the D-17 probe: `PreToolUse` (the
    # scope guard) plus `SubagentStart`, `SubagentStop` and `PostToolUse` (the
    # agent-lifecycle hook). It is asserted as an exact number on purpose — a
    # fifth event appearing is a decision, not a diff.
    check("Hooks (4)" in out,
          f"the payload registers exactly four hook events (inventory: "
          f"{[l.strip() for l in out.splitlines() if 'Hooks (' in l]})")
    # Skills are NOT asserted here: they arrive with item 09. When they do,
    # this file keeps working and the skills test owns that count.
    check("(touch)" in out or "Touch" in out,
          "details resolves the plugin by its manifest name")


def main():
    for t in (test_monitoring_subset_ships_nothing_extra,
              test_plugin_manifest,
              test_marketplace_manifest,
              test_catalog_is_tracked,
              test_license,
              test_manifests_validate,
              test_plugin_details):
        t()
    print()
    if skips:
        print(f"skipped: {len(skips)} check(s)")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all plugin-tree tests passed")


if __name__ == "__main__":
    main()
