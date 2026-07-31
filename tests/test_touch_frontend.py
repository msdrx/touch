#!/usr/bin/env python3
"""Static-source guards for `touch-visual/` — the v0 page (R-22, R-32, R-55).

Run: `python3 test_touch_frontend.py`. Exits non-zero on failure. No pytest, no
runner (D12/STACK-16).

Why a *source* test. The page is HTML/CSS/JS and python3 never executes it, so
this file asserts on its text — the genre `.claude/shared/monitoring/tests/
test_frontend.py` established for `monitor.html`: the required pattern is
present, the forbidden one is absent. Three properties are worth that:

1. **Escape-first, structurally** (GD-20). `app.js` has no markup sink at all —
   no `innerHTML`, no `insertAdjacentHTML`, no `document.write`, no template
   literal interpolated into one — so an agent-authored session name, plan
   detail or artifact path cannot become an element. That is a property of the
   text, not of a code path a test could reach.
2. **No state inference** (R-55's named frontend guard, GD-23). There is one
   reducer and it is server-side: the page may render `derived.state` /
   `derived.label` / `plan.badge` and may not compute them. "The page never
   asks what time it is" is the executable form — `Date.now()` and a bare
   `new Date()` are the ingredients every liveness derivation needs.
3. **Replayed frames paint once** (R-55). The animation class is attached by
   exactly one function, which requires `live === true`, and every keyframe
   animation in the stylesheet is reachable only through it.

Several guards are **cross-file**: the JS class whitelists are compared against
`agents.NODE_STATES`, `legacy.STATES` and `store.PROVENANCE`, the fetched
routes against `server.READ_ROUTES`, and the restated wire contract against the
block in `server.py`'s own docstring (sp-12 restates it verbatim, so drift on
either side is a failure here rather than a mystery at runtime).

The JS comment stripper below is deliberately small: it tracks quotes and
backticks and treats `//` / `/*` outside them as comments. It would mis-read a
regex literal containing `//` or `/*`; `app.js` contains one regex literal
(`/^#/`) and the guard `test_the_stripper_sees_this_file_the_way_it_claims`
asserts the stripper's assumptions on it directly.
"""

import re
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The canonical trees are named through `tests/_roots.py`, never by a
# literal under REPO: GD-U1 moves them and this is the single flip point.
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
from _roots import SRC                # noqa: E402  (path juggling first)
sys.path.insert(0, str(SRC))

from aggregator import agents as agents_mod                        # noqa: E402
from aggregator import legacy as legacy_mod                        # noqa: E402
from aggregator import server as server_mod                        # noqa: E402
from aggregator import store as store_mod                          # noqa: E402

VISUAL = SRC / "touch-visual"
HTML_PATH = VISUAL / "index.html"
JS_PATH = VISUAL / "app.js"
CSS_PATH = VISUAL / "style.css"

failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def read(path):
    return path.read_text(encoding="utf-8")


HTML = read(HTML_PATH) if HTML_PATH.exists() else ""
JS = read(JS_PATH) if JS_PATH.exists() else ""
CSS = read(CSS_PATH) if CSS_PATH.exists() else ""

#: `index.html` with its comments removed.
#:
#: The document explains itself at length, and a guard that scans the raw text
#: for a *markup* property reads those explanations as markup: the live-region
#: count below found two `aria-live="polite"` in a one-region page because a
#: comment quoted the attribute it was describing. Structure is asserted
#: against this; the presence of an explanation is asserted against `HTML`.
MARKUP = re.sub(r"<!--.*?-->", "", HTML, flags=re.DOTALL)


# --- the comment stripper -------------------------------------------------


def strip_js_comments(src: str) -> str:
    """`src` with `//` and `/* */` comments blanked out, strings preserved.

    Comments are replaced by spaces rather than removed so every offset and
    every line number survives — a guard that reports "line 412" should mean
    the line the reader will open.
    """
    out = []
    i, n = 0, len(src)
    quote = None
    while i < n:
        ch = src[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


CODE = strip_js_comments(JS)


def slice_fn(src, start_marker, end_marker):
    """The text from `start_marker` up to the next `end_marker` after it.

    `start_marker` is a name *prefix* and the FIRST match wins, so wherever one
    function's name prefixes another's (`rollup` / `rollupList`, `refKey` /
    `refSummary`) the marker must include the opening paren — otherwise a
    reordering of the file silently slices the wrong body and the guard passes
    for the wrong reason.
    """
    i = src.find(start_marker)
    assert i != -1, f"marker not found: {start_marker!r}"
    j = src.find(end_marker, i + len(start_marker))
    assert j != -1, f"end marker not found after {start_marker!r}: {end_marker!r}"
    return src[i:j]


def js_object_keys(name):
    """The keys of a top-level `const <name> = { … };` literal in app.js."""
    body = slice_fn(CODE, f"const {name} = {{", "};")
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", body, re.MULTILINE))


# --- R-22: the skeleton exists where everything else expects it -----------


def test_the_three_files_are_where_the_server_serves_them_from():
    print("test_the_three_files_are_where_the_server_serves_them_from")
    for path in (HTML_PATH, JS_PATH, CSS_PATH):
        check(path.exists(), f"{path.relative_to(REPO)} exists (R-22 / GD-15 layout)")
    # `Api.asset` resolves names against `--assets` (default `touch-visual/`);
    # the three handlers name exactly these three files, and a 503 naming a
    # missing one is what this replaces.
    for name in ("index.html", "app.js", "style.css"):
        check(f'"{name}"' in read(SRC / "aggregator" / "server.py"),
              f"server.py serves {name} by name")
    check(server_mod.CONTROL_ROUTES == {},
          "the server's control route group is empty — v0 has no verb to render")


def test_the_page_carries_the_serve_time_token_where_it_is_valid():
    print("test_the_page_carries_the_serve_time_token_where_it_is_valid")
    placeholder = "__TOUCH_TOKEN__"
    check(f'<meta name="touch-token" content="{placeholder}">' in HTML,
          "the token placeholder is in a meta tag app.js can read (GD-13)")
    # `/app.js` and `/style.css` are NOT open routes and a browser cannot put a
    # header on a `<script src>` — the query-string arm is the only carrier.
    check(f'href="/style.css?token={placeholder}"' in HTML,
          "the stylesheet URL carries the token (a sub-resource cannot send a header)")
    check(f'src="/app.js?token={placeholder}"' in HTML,
          "the script URL carries the token")
    check(placeholder not in JS and placeholder not in CSS,
          "the raw placeholder appears only in the injected document")
    check("readToken" in CODE and 'meta[name="touch-token"]' in CODE,
          "app.js reads the injected token")
    check("window.TOUCH_TOKEN" in CODE,
          "app.js accepts inject_token's script-tag fallback arm too")
    check("TOKEN_PLACEHOLDER" in CODE and "injected !== TOKEN_PLACEHOLDER" in CODE,
          "an UNinjected placeholder is treated as no token, never sent as one")
    check("URLSearchParams(window.location.search)" in CODE and '.get("token")' in CODE,
          "?token= is accepted so an operator can navigate here the first time")


# --- GD-20: escape-first, structurally ------------------------------------


def test_no_markup_sink_exists_in_the_page():
    print("test_no_markup_sink_exists_in_the_page")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                 "eval(", "new Function", "srcdoc", "createContextualFragment"):
        check(sink not in CODE, f"app.js contains no {sink} (agent-authored text is data)")
    check("createTextNode" in CODE, "the text sink is a text node — the browser's own escape")
    check(".textContent" in CODE, "textContent is used for updates in place")
    check("${" not in CODE and "`" not in CODE,
          "no template literals: there is no string a value can be interpolated into")
    check("javascript:" not in CODE, "no javascript: URL is ever constructed")
    # The FRONTEND-1 shape, in every spelling: a class assembled from server text.
    bad = re.findall(r'"[^"\n]*"\s*\+\s*[A-Za-z_$][A-Za-z0-9_$.]*\.'
                     r'(?:state|badge|kind|type|provenance|label)\b', CODE)
    check(not bad, f"no class/label string is concatenated with raw server text — {bad}")
    check("classOf(" in CODE,
          "server vocabularies reach CSS through a whitelist lookup (classOf)")
    check("encodeURIComponent(" in CODE, "ids are encoded before they enter a URL")
    check("URLSearchParams" in CODE and "url.searchParams.set" in CODE,
          "query strings are built by the URL API, never by concatenation")


def test_the_stripper_sees_this_file_the_way_it_claims():
    print("test_the_stripper_sees_this_file_the_way_it_claims")
    sample = 'const a = "http://x"; // note\nconst b = 1; /* c */ const d = /^#/;\n'
    stripped = strip_js_comments(sample)
    check('"http://x"' in stripped, "a // inside a string is not a comment")
    check("note" not in stripped and " c " not in stripped, "real comments are blanked")
    check("/^#/" in stripped, "a regex literal survives the stripper")
    check(stripped.count("\n") == sample.count("\n"), "line numbers are preserved")
    check(len(re.findall(r"/\^#/", CODE)) == 1,
          "app.js holds exactly one regex literal, and it contains no comment opener")


# --- R-32: no control affordance renders in v0 ----------------------------

#: The verb ladder v0 does not have. `start`/`started` are excluded from the
#: list on purpose — `startedAt` and `startsWith` are ordinary vocabulary, and
#: a guard that cries wolf on them gets deleted.
CONTROL_VERBS = ("pause", "restart", "terminate", "kill", "abort", "interrupt")


def test_no_control_verb_reaches_the_page():
    print("test_no_control_verb_reaches_the_page")
    # `app.js` is checked with its comments blanked: a comment explaining why
    # v0 has no verb is documentation, and a guard that forbids the word
    # everywhere forbids the explanation too. Code and string literals — the
    # only two places an affordance can actually come from — are checked in
    # full, and the HTML/CSS are checked verbatim.
    for verb in CONTROL_VERBS:
        for name, src in (("app.js", CODE), ("index.html", HTML), ("style.css", CSS)):
            hit = re.search(r"\b" + verb + r"\w*", src, re.IGNORECASE)
            check(hit is None, f"{name} contains no {verb!r} affordance"
                               f"{'' if hit is None else f' — {hit.group(0)!r}'}")
    check(not re.search(r"\bstop\b", CODE, re.IGNORECASE), "app.js has no stop verb either")
    for method in ('"POST"', '"PUT"', '"DELETE"', '"PATCH"'):
        check(method not in CODE, f"app.js issues no {method} request — the page only reads")
    check("<form" not in HTML, "the document has no form")
    buttons = re.findall(r"<button[^>]*>([^<]*)<", HTML)
    check(all("load older" in b.strip() for b in buttons),
          f"the only button is R-55's read-only load-older affordance — {buttons}")


# --- R-32: the sidebar, the tree, the rollups -----------------------------


def test_the_sidebar_lists_every_class_of_thing_the_store_knows():
    print("test_the_sidebar_lists_every_class_of_thing_the_store_knows")
    for element in ("sessionList", "runList", "taskList", "detailHead", "detailStatus",
                    "detailBody", "log", "older", "olderBtn", "mirrorChip", "rollup"):
        check(f'id="{element}"' in HTML, f"index.html declares #{element}")
    routes = {route for (method, route) in server_mod.READ_ROUTES}
    for route in ("/api/sessions", "/api/tasks", "/api/run/graph",
                  "/api/session/timeline", "/api/events", "/health"):
        check(route in routes, f"{route} is a real route on the server's table")
        check(f'"{route}"' in CODE, f"app.js reads {route}")
    # GD-6/R-46's tagged union and GD-14's kinds are *rendered*, not filtered
    # away: a historical session and a plan-only folder are real rows.
    check('"historical"' in CODE or 'session.kind === "live"' in CODE,
          "both session classes render (a historical session is not a degraded live one)")
    check("transcriptless" in CODE,
          "the transcriptless session is labelled, not hidden (sources: [])")
    check('task.kind === "run"' in CODE,
          "GD-14's task kinds are rendered — a plan-only folder is a row, not an error")
    check("archive" in CODE and "archive.label" in CODE,
          "the archive label is taken from the server (GD-14: derived, never a constant)")
    check("runIdOf" in CODE and '"run:"' in CODE,
          "the run list is keyed by the `run:<runId>` stream ids the handshake names")


def test_the_agent_tree_is_keyed_by_harness_facts():
    print("test_the_agent_tree_is_keyed_by_harness_facts")
    run_detail = slice_fn(CODE, "function renderRunDetail", "function renderSessionDetail")
    # GD-7: `(runId, key, ordinal)` for Workflow nodes, the full 17-hex agentId
    # for Agent-tool agents. Both are harness facts; the marker only labels.
    check("obs.key" in run_detail and "obs.ordinal" in run_detail,
          "nodes render their (key, ordinal) identity, not a name")
    check("obs.agentId" in run_detail, "the node's full agentId is rendered")
    check("agent.id" in run_detail, "agents are keyed by the agent document's own _id")
    check("renderDerivedBlock(" in run_detail,
          "every card's verdict comes from the reducer's `derived` block")
    check("fragments" in run_detail,
          "R-48's fragment count is reported (the page never stitches them itself)")
    check("spawn.recordUuid" in run_detail,
          "the spawn locator is recordUuid + toolUseId, never a file offset")


def test_token_rollups_are_sums_of_absolute_records():
    print("test_token_rollups_are_sums_of_absolute_records")
    keys = re.search(r"const TOKEN_KEYS = \[([^\]]*)\]", CODE)
    check(keys is not None, "app.js declares the four token keys")
    if keys:
        found = set(re.findall(r'"([a-z_]+)"', keys.group(1)))
        check(found == set(store_mod.TOKEN_KEYS),
              f"the four keys match store.TOKEN_KEYS — {sorted(found)}")
    rollup = slice_fn(CODE, "function noteTokens", "function rollupList")
    check("held.seq > totals.seq" in rollup,
          "latest-per-ref wins: an older backfilled record never overwrites a newer one")
    check("+=" in rollup, "the rollup sums across refs")
    # GD-25/R-55: deltas are wire-only and this page is not the wire. A single
    # subtraction on a token field is the whole failure mode.
    for name in ("function noteTokens(", "function rollup(", "function rollupList("):
        body = slice_fn(CODE, name, "\n}")
        check(" - " not in body.replace("e - ", ""), f"{name}: no subtraction on a token field")
    check("rollupList(payload.tokens)" in CODE,
          "a legacy folder's folded token records roll up by the same rule (GD-14)")


def test_degraded_and_derived_states_are_labelled():
    print("test_degraded_and_derived_states_are_labelled")
    for marker in ("derivedFromLegacy", "relabel", "conflictingTerminals",
                   "unconventional", "frozen", "retracted", "oversize",
                   "streamsUnobserved"):
        check(marker in CODE, f"{marker} is surfaced rather than dropped")
    # The one string this page must NOT contain: the re-label is the server's
    # conclusion and travels on the row. Spelling it here would let the page
    # print it for a case the reducer did not decide (D13 / R-58).
    check(legacy_mod.CLOSED_NO_VERDICT not in CODE,
          '"closed — no verdict" is rendered from the server label, never hardcoded')
    check("plan.label" in CODE and "plan.badge" in CODE,
          "legacy cards render the reducer's badge and label verbatim")
    check("derived.label" in CODE and "derived.state" in CODE,
          "run/agent cards render the reducer's label and state verbatim")
    check("prov-derived" in CODE and "prov-derived" in CSS,
          "a derived row carries the dashed provenance class")
    check(re.search(r"\.prov-derived\s*\{[^}]*border-style:\s*dashed", CSS),
          "the dashed border is real CSS, not just a class name")
    check(re.search(r"\.card\.derived\s*\{[^}]*border-style:\s*dashed", CSS),
          "a re-labelled card is dashed as a whole")
    check("mirror " in CODE and "MIRROR_CLASS" in CODE,
          "`mirror: absent|down` renders as a label — the page never depends on Mongo")


def test_the_asserted_nodes_are_rendered_and_told_apart():
    print("test_the_asserted_nodes_are_rendered_and_told_apart")
    # D-04's client half. Without a renderer the server's new payload key is a
    # silent no-op: the harness nodes would render (they arrive as `nodes`) and
    # the demoted events.jsonl rows would simply vanish from the UI, which is a
    # DELETION in everything but name — precisely what GD-D12 forbids.
    check("assertedNodes" in CODE,
          "the page reads `assertedNodes` — the demoted events.jsonl rows are "
          "rendered, not dropped (GD-D12: demoted, never deleted)")
    check("nodeList(" in CODE,
          "…through one node-list renderer, so the two lists cannot drift apart")
    check(CODE.count("nodeList(") >= 3,
          "…called for the harness set AND the asserted set (plus its definition)")
    check("node.source" in CODE,
          "a row's source is read from the server's own word, never re-derived")
    check('"prov-asserted"' in CODE and "prov-asserted" in CSS,
          "an asserted row carries its own provenance chip class")
    check('"prov-harness"' in CODE and "prov-harness" in CSS,
          "…and an observed one carries the harness class, so a screenshot still "
          "says which is which")
    check("payload.harness" in CODE and "harness.wfDir" in CODE,
          "the join key is shown: 'why did these rows change source' is answerable "
          "from the page as well as from the response")
    check("lastToolSummary" not in CODE,
          "the page never reaches for `lastToolSummary` itself — the server already "
          "put it on `node.detail`, and a truncated preview is display text that "
          "nothing parses (GD-D4/SUBSTRATE-10)")


def test_the_class_whitelists_match_the_servers_vocabulary():
    print("test_the_class_whitelists_match_the_servers_vocabulary")
    node_states = js_object_keys("NODE_STATE_CLASS")
    check(node_states == set(agents_mod.NODE_STATES),
          f"NODE_STATE_CLASS == agents.NODE_STATES — {sorted(node_states)}")
    check("failed" not in node_states,
          "`failed` is absent from the liveness whitelist (R-58: it is never derived)")
    legacy_states = js_object_keys("LEGACY_STATE_CLASS")
    expected = set(legacy_mod.STATES) | set(legacy_mod.DERIVED_STATES)
    check(legacy_states == expected,
          f"LEGACY_STATE_CLASS == legacy.STATES + DERIVED_STATES — {sorted(legacy_states)}")
    check("failed" in legacy_states,
          "an OBSERVED legacy failure keeps its badge (D13) — only fabricated ones re-label")
    prov = js_object_keys("PROV_CLASS")
    check(set(store_mod.PROVENANCE) <= prov,
          f"PROV_CLASS covers every GD-28 provenance value — {sorted(prov)}")
    for value in sorted(prov):
        check(f".prov-{value} {{" in CSS or f".prov-{value}{{" in CSS,
              f"the stylesheet defines .prov-{value}")
    for value in sorted(node_states | legacy_states | {"other"}):
        cls = "st-" + value
        check(f".{cls} {{" in CSS, f"the stylesheet defines .{cls}")

    # Two correct tables are not enough: what matters is which one each list
    # READS. A harness row's state is the reducer's vocabulary, and looking it
    # up in the legacy table sends `unknown` — every resultless node of a
    # killed run, i.e. exactly what D-04 exists to surface — to `st-other`.
    check("function stateClassFor(" in CODE,
          "the table a row's state is looked up in is chosen by one named function")
    chooser = CODE.split("function stateClassFor(", 1)[1].split("\n}", 1)[0]
    check('node.source === "harness"' in chooser and "NODE_STATE_CLASS" in chooser,
          "…and a harness row is classed from NODE_STATE_CLASS — the vocabulary the "
          "server's `_harness_node_row` actually emits")
    check("LEGACY_STATE_CLASS" in chooser,
          "…while a legacy or asserted row keeps the legacy table")
    check("classOf(stateClassFor(node)" in CODE
          and "classOf(LEGACY_STATE_CLASS, node.state)" not in CODE,
          "…and the node list goes through it, never straight at one table")


# --- R-55: the wire, the boundary, and the once-painted replay ------------


def test_the_wire_contract_is_restated_verbatim():
    print("test_the_wire_contract_is_restated_verbatim")
    server_src = read(SRC / "aggregator" / "server.py")

    def block(src):
        start = src.find('{"type":"hello"')
        end = src.find('{"type":"tick"')
        assert start != -1 and end != -1, "the frame block is missing"
        end = src.find("\n", end)
        raw = src[start:end].splitlines()
        return [line.strip().lstrip("*").strip() for line in raw if line.strip()]

    theirs, ours = block(server_src), block(JS)
    check(theirs == ours,
          "app.js restates server.py's frame block verbatim (sp-12 ⇄ sp-13 contract)")
    if theirs != ours:
        for a, b in zip(theirs, ours):
            if a != b:
                print(f"    server: {a}\n    page:   {b}")


def test_only_live_frames_animate():
    print("test_only_live_frames_animate")
    check('const LIVE_CLASS = "fresh"' in CODE, "the animation class is a named constant")
    adds = re.findall(r"classList\.add\(LIVE_CLASS\)", CODE)
    check(len(adds) == 1, f"LIVE_CLASS is attached in exactly one place — {len(adds)}")
    paint = slice_fn(CODE, "function paint(", "\n}")
    check("live === true" in paint,
          "paint() requires live === true — a missing flag is not a live frame")
    check("classList.add(LIVE_CLASS)" in paint, "the one attachment site is paint()")
    log_row = slice_fn(CODE, "function logRow", "\n}")
    check("paint(li, entry.live)" in log_row,
          "a log row's animation is decided by the frame's own live flag")
    check("paint(li, true)" not in CODE and "paint(li, 1)" not in CODE,
          "no call site hardcodes a live paint")
    older = slice_fn(CODE, "async function loadOlder", "\n}")
    check("live: false" in older,
          "records fetched by 'load older' are history and are painted once")
    backfill = slice_fn(CODE, "function onEvent", "function onMode")
    check("frame.live === true" in backfill,
          "the live flag is read off the frame, never inferred from arrival time")

    # Every animation in the stylesheet must be reachable only through the live
    # class (or the live-tail chip, which renders the socket's own mode).
    for rule in re.findall(r"([^{}/]+)\{([^}]*)\}", CSS):
        selector, body = rule[0].strip(), rule[1]
        if "animation:" not in body or "animation: none" in body:
            continue
        check(".fresh" in selector or ".chip-mode.st-running" in selector,
              f"animated selector is live-only — {selector!r}")
    check(re.search(r"\.fresh\s*\{[^}]*animation:", CSS), ".fresh carries the animation")
    check(re.search(r"\.logrow\s*\{(?:[^}]*)\}", CSS) and
          "animation" not in slice_fn(CSS, ".logrow {", "}"),
          "the base log row has no animation — replayed frames paint once")
    check("prefers-reduced-motion" in CSS, "the animation respects reduced-motion")


def test_the_resume_cursor_is_the_servers_not_ours():
    print("test_the_resume_cursor_is_the_servers_not_ours")
    check("function cursorKey" in CODE and 'padStart(12, "0")' in CODE,
          "the cursor grammar is <stream>#<seq:012d>, byte-identical to store.cursor_key")
    ws_url = slice_fn(CODE, "function wsUrl", "\n}")
    check('append("cursor"' in ws_url and "state.resume" in ws_url,
          "reconnect resumes from the server-published cursors, one ?cursor= per stream")
    check("state.delivered" not in ws_url,
          "the resume position is NEVER 'max seq I received' — a held token record "
          "sits behind it and would be skipped forever")
    adopt = slice_fn(CODE, "function adoptCursors", "\n}")
    check("state.resume[stream]" in adopt, "only mode/subscribed frames move the resume point")
    for handler in ("function onMode", "function onSubscribed"):
        body = slice_fn(CODE, handler, "\n}")
        check("adoptCursors(" in body, f"{handler} adopts the published cursors")
    check("function resync" in CODE and 'type: "subscribe"' in CODE,
          "the page re-asks for the authoritative cursors while the tail is live")
    check("state.seen.has(key)" in CODE and "SEEN_MAX" in CODE,
          "frames are de-duplicated by (stream, seq), in a bounded set")


def test_the_load_older_anchors_come_from_the_frames_that_know_them():
    print("test_the_load_older_anchors_come_from_the_frames_that_know_them")
    hello = slice_fn(CODE, "function onHello", "function onEvent")
    check("truncated" not in hello and "oldest" not in hello,
          "hello carries no anchors — it cannot know them, and an empty one reads as 'nothing cut'")
    mode = slice_fn(CODE, "function onMode", "function onAnchors")
    check("frame.oldest" in mode and "frame.truncated" in mode,
          "the mode frame supplies the first anchors")
    anchors = slice_fn(CODE, "function onAnchors", "function onSubscribed")
    check("frame.oldest" in anchors and "frame.truncated" in anchors,
          "a post-boundary backfill's own anchors frame supplies later ones")
    older = slice_fn(CODE, "function renderOlder", "\n}")
    check("anchor.truncated" in older and "button.hidden" in older,
          "the button appears only when a frame declared something was cut")
    check('"/api/events"' in CODE and "before:" in CODE,
          "walking older uses /api/events?stream=&before= — the documented backwards arm")


# --- GD-23: the page renders the reducer; it never re-derives -------------


def test_the_page_never_infers_state():
    print("test_the_page_never_infers_state")
    check("Date.now(" not in CODE,
          "no Date.now(): the page never asks what time it is (GD-23)")
    check(not re.search(r"new Date\(\s*\)", CODE),
          "no bare new Date(): 'now' is the reducer's ingredient, not the page's")
    check("performance.now(" not in CODE, "no performance.now() either")
    check("new Date(String(value))" in CODE,
          "a timestamp is parsed from a GIVEN instant, for display only")
    check(not re.search(r"idle\w*\s*[<>]", CODE), "no idle threshold comparison")
    # Anchored: a bare substring test fires on any future 1800/180000/x180 and
    # a guard that cries wolf gets deleted.
    check(not re.search(r"\b180\b", CODE),
          "the 180 s liveness limit does not exist on this side")
    check("reducerVersion" in CODE,
          "the reducer's version is displayed, so a rebuild is visible")
    # The reducer's output is *pulled*; the socket only says when to pull.
    check("function refreshModel" in CODE and "queueRefresh(" in CODE,
          "the page refetches the reduction rather than folding records itself")
    reduce_words = re.findall(
        r"\bfunction\s+(reduce\w*|derive\w*|infer\w*|compute\w*|classify\w*)\s*\(", CODE)
    check(not reduce_words, f"app.js defines no reducer of its own — {reduce_words}")


def test_the_render_is_coalesced_and_the_log_is_capped():
    print("test_the_render_is_coalesced_and_the_log_is_capped")
    check("requestAnimationFrame" in CODE and "RENDER_DEBOUNCE_MS" in CODE,
          "renders are debounced and land on an animation frame (GD-20 do-not-inherit)")
    on_frame = slice_fn(CODE, "function onFrame", "function onHello")
    check("schedule();" in on_frame and "render();" not in on_frame,
          "a frame schedules a paint; it never paints (a replay burst is ONE render)")
    check("const LOG_MAX" in CODE, "the log cap is a named constant, present from day one")
    flush = slice_fn(CODE, "function flushLog", "\n}")
    check("childElementCount > LOG_MAX" in flush, "the DOM list is trimmed to the cap")
    check("createDocumentFragment" in flush, "a burst is appended in one fragment")
    push = slice_fn(CODE, "function pushLog", "\n}")
    check("LOG_MAX" in push, "the pending queue is capped too, not just the DOM")
    check("state.trimmed" in flush, "what the cap evicted is counted and displayed")
    drop = slice_fn(CODE, "function dropSocket", "\n}")
    for handler in ("onmessage", "onopen", "onclose"):
        check(f"socket.{handler} = null" in drop,
              f"teardown detaches {handler} (a closing socket still delivers)")


def test_the_page_degrades_without_mongo_and_says_why():
    print("test_the_page_degrades_without_mongo_and_says_why")
    # GD-22: the live view is fully functional with `mirror: absent|down`; only
    # history degrades. Nothing on this page may be conditional on the mirror.
    check("state.health" in CODE and "mirror" in CODE,
          "the mirror's state is read from /health and rendered as a label")
    for route in ("/api/query",):
        check(f'"{route}"' not in CODE,
              f"the page does not read {route} — the UI never depends on Mongo")
    check("no per-boot token" in CODE,
          "a page served without a token says so instead of failing silently")


# --- the page states the CURRENT truth, on every surface ------------------


def test_the_notice_surface_states_the_current_cycle():
    print("test_the_notice_surface_states_the_current_cycle")
    # An append-only notice box keeps displaying a failure that has already
    # resolved — the same class of untruth as a fabricated badge (GD-23/D13),
    # and the one this whole plan exists to end. Every slot must therefore be
    # cleared by whoever set it, on the success that contradicts it.
    check(not re.search(r"state\.error\b", CODE),
          "there is no single shared error flag one arm's success can wipe on "
          "another arm's behalf")
    check("function setError" in CODE and "delete state.errors[source]" in CODE,
          "failures live in per-source slots that can be cleared")
    model = slice_fn(CODE, "async function refreshModel", "\n}")
    check('setError("sessions", null)' in model,
          "the sessions arm clears its own slot on success")
    tasks = slice_fn(CODE, "function fetchTasks(", "\n}")
    # The success arm's value is the server's `note` **or** null, so one
    # expression both sets (a 200 that explains an empty list, UI-13) and clears
    # (the ordinary answer, which carries no note) — the rule this guard protects
    # is unchanged: whoever sets a slot clears it, on the success that
    # contradicts it. Counted rather than matched character for character,
    # because the guard is "two calls, one per outcome, in this function", not a
    # spelling; the set-then-clear BEHAVIOUR is driven in the node+vm arm.
    check(tasks.count('setError("tasks"') == 2
          and "body.note" in tasks
          and 'setError("tasks", err.message)' in tasks,
          "the tasks arm sets and clears its own slot, in the one function that "
          "issues the request — the success value is the server's note or null")
    health = slice_fn(CODE, "async function refreshHealth", "\n}")
    check('setError("health", null)' in health and 'setError("health"' in health,
          "a /health failure is named, and un-named again when a poll succeeds")
    older = slice_fn(CODE, "async function loadOlder", "\n}")
    check('setError("load older", null)' in older,
          "load-older owns its own slot — its success clears nothing else")
    check("clearOlderError" in CODE,
          "…and that slot has a second clearer, because its owner is a button "
          "the operator can be locked out of (see the dedicated guard below)")
    conn = slice_fn(CODE, "function connect(", "\n}")
    check("state.wire.notices = []" in conn,
          "wire notices are scoped to one connection: a handshake report "
          "describes THIS handshake, not every handshake since the tab opened")
    boot = slice_fn(CODE, "function boot(", "\n}")
    check("state.bootError" in boot,
          "the one sticky message (served without a token) has its own slot")
    notices = slice_fn(CODE, "function renderNotices", "\n}")
    check("state.bootError" in notices and "state.errors" in notices,
          "the box is rebuilt from state on every paint, never appended to")


def test_the_resync_never_asks_to_be_moved_forward():
    print("test_the_resync_never_asks_to_be_moved_forward")
    server_src = read(SRC / "aggregator" / "server.py")
    # `_advance` clamps the published cursor to `pending_floor - 1` while the
    # coalescer holds a token record, yet the frames after the held one still
    # go out — so "max seq I received" is genuinely AHEAD of the socket on the
    # ordinary path, and `subscribe` refuses it by name.
    check("floor = self.coalescer.pending_floor(stream)" in server_src,
          "the server clamps its published cursor behind a held token record")
    check("ahead of this socket at seq" in server_src,
          "and refuses a cursor above it, with that wording")
    resync = slice_fn(CODE, "function resync", "\n}")
    check("state.resume" in resync,
          "resync hands back the server's OWN published position")
    check("got < published" in resync,
          "a lower value is sent only when a frame was actually missed: a "
          "resume position may go backwards (a rewind), never forwards (a skip)")
    subscribed = slice_fn(CODE, "function onSubscribed", "\n}")
    check('"ahead of this socket"' in subscribed,
          "an ahead-of-socket refusal is classified as the coalescer's ordinary "
          "hold, not surfaced as an alarming notice")
    check("adoptCursors(frame.cursors)" in subscribed,
          "the ack's cursors are adopted regardless — they are the answer")


def test_the_token_rollup_key_is_an_identity_not_a_display_string():
    print("test_the_token_rollup_key_is_an_identity_not_a_display_string")
    server_src = read(SRC / "aggregator" / "server.py")
    key_of = slice_fn(server_src, "    def key_of(", "\n    def ")
    check('"|".join(f"{name}={ref[name]}" for name in sorted(ref))' in key_of,
          "server.TokenCoalescer.key_of joins sorted name=value pairs with '|'")
    ref_key = slice_fn(CODE, "function refKey(", "\n}")
    check('join("|")' in ref_key and ".sort()" in ref_key,
          "app.js refKey uses that same grammar, so both sides bucket a ref alike")
    check("truncate(" not in ref_key,
          "the rollup key is never truncated: two refs that agree in their first "
          "N characters are still two refs, and collapsing them reads LOW")
    note_tokens = slice_fn(CODE, "function noteTokens(", "\n}")
    check("refKey(ref)" in note_tokens and "refSummary(" not in note_tokens,
          "noteTokens buckets by the identity, never by the display formatter")
    check("state.tokensRefless" in note_tokens,
          "a token record with NO ref is counted, not filed under the empty key: "
          "that key collapses every ref-less record in a stream into one slot")
    summary = slice_fn(CODE, "function refSummary(", "\n}")
    check("truncate(" in summary and 'join(" ")' in summary,
          "refSummary stays what its name says — a truncating display formatter")
    check("refSummary(record.ref)" in slice_fn(CODE, "function logRow", "\n}"),
          "and it is what a log row displays")


def js_int(name):
    """The value of a top-level `const <name> = <int>;` in app.js."""
    found = re.search(r"const " + name + r" = (\d+);", CODE)
    assert found is not None, f"no integer constant {name}"
    return int(found.group(1))


def test_load_older_has_its_own_room_and_can_therefore_paint():
    print("test_load_older_has_its_own_room_and_can_therefore_paint")
    # THE effect guard, and the one the shape guards below cannot replace.
    #
    # A stream is only ever declared `truncated` after the server cut a replay
    # at its window (`replay_window`: `truncated = len(window) < len(ordered)`,
    # window ≤ 500), so by the time the button can appear the live log has been
    # pinned at LOG_MAX for a long time and stays there. A history page drawn
    # out of the LIVE list's remaining room is therefore drawn out of zero: the
    # affordance exists in source and can never paint a row. The two budgets
    # must be independent, and one page must fit in the history one.
    log_max, page, older_max = js_int("LOG_MAX"), js_int("OLDER_PAGE"), js_int("OLDER_MAX")
    check(older_max >= page,
          f"the history budget holds at least one page — OLDER_MAX {older_max} "
          f">= OLDER_PAGE {page}")
    check(server_mod.DEFAULT_REPLAY_EVENTS > log_max,
          f"…which matters because the replay window ({server_mod.DEFAULT_REPLAY_EVENTS}) "
          f"is larger than the log cap ({log_max}): a truncation is only ever "
          f"declared to a client whose live log is already full")
    older = slice_fn(CODE, "async function loadOlder", "\n}")
    check("LOG_MAX" not in older,
          "the history page is NOT sized against the live log's cap — the room "
          "left under LOG_MAX is structurally zero whenever the button exists")
    check("const list = dom.older" in older,
          "history is prepended into its own list, #older")
    check("removeChild" not in older and "state.trimmed" not in older,
          "load-older evicts nothing and nothing it does is called 'older dropped'")
    flush = slice_fn(CODE, "function flushLog", "\n}")
    check("dom.older" not in flush and "const list = dom.log" in flush,
          "…and the live cap governs #log alone, so the tail can neither be "
          "paid out of history nor evict it")
    check("older loaded of" in flush,
          "the loaded-history count is displayed with its own words")


def test_load_older_never_fetches_a_page_it_cannot_paint():
    print("test_load_older_never_fetches_a_page_it_cannot_paint")
    older = slice_fn(CODE, "async function loadOlder", "\n}")
    room = slice_fn(CODE, "function olderRoom(", "\n}")
    check("OLDER_MAX" in room and "state.olderShown" in room,
          "the room left is the history budget minus what it already holds")
    # Order matters: the early return must precede the fetch in the source, or
    # the "dead click" it prevents has already cost a whole-stream read_all.
    guard = older.find("if (!room)")
    fetch = older.find('getJson("/api/events"')
    check(guard != -1 and fetch != -1 and guard < fetch,
          "a spent budget returns BEFORE the request: h_events re-reads the "
          "entire stream file per call, in the process that also serves /ws")
    check("Math.min(OLDER_PAGE, room)" in older,
          "and what is requested is clamped to what will be painted, so no "
          "record is ever fetched and then discarded")
    check("loadingOlder" in older and "if (loadingOlder) return" in older,
          "a second click cannot overlap the first — two in-flight pages race "
          "on the same anchor")
    render_older = slice_fn(CODE, "function renderOlder(", "\n}")
    check("button.disabled = !room || loadingOlder" in render_older,
          "the button says so too: disabled while a page is in flight and when "
          "the budget is spent")
    check("history full" in render_older and "rows of " in render_older,
          "…with a label that states the reason AND names the stream whose "
          "budget is spent: 'full' said of some other stream's list is the "
          "false statement the per-stream history exists to prevent")
    # Prepending the page whole is only contiguous because the backwards arm
    # returns the newest `limit` of the older set, ascending.
    check("page = older[-limit:]" in read(SRC / "aggregator" / "server.py"),
          "h_events' before= arm returns the newest `limit` older records, ascending")
    check("anchor.oldest = page.oldest" in older,
          "the anchor advances to what was actually painted, so the next click "
          "resumes there with no gap")


def test_the_loaded_history_belongs_to_one_stream_and_one_connection():
    print("test_the_loaded_history_belongs_to_one_stream_and_one_connection")
    # `OLDER_MAX` is ONE STREAM's budget. Spent globally it is the same dead
    # affordance by a plainer route: two clicks on any run and load-older is
    # disabled for every run for the life of the tab — while `#older` keeps
    # showing the first stream's rows directly above the second stream's live
    # tail, under a button whose "full · 400 rows" is true of the list and false
    # of the stream it names.
    reset = slice_fn(CODE, "function resetOlder(", "\n}")
    check("state.olderStream === stream" in reset and "return" in reset,
          "the history list carries the identity of the stream it holds")
    check("state.olderShown = 0" in reset and "clear(dom.older)" in reset,
          "…and handing it to another stream empties it — rows AND counter")
    check("state.seen" not in reset,
          "the de-dup set is NOT cleared with it: those records were delivered "
          "once, and re-painting them into the tail is the duplicate it exists "
          "to stop")
    render_older = slice_fn(CODE, "function renderOlder(", "\n}")
    check("resetOlder(currentStream())" in render_older or
          ("const stream = currentStream()" in render_older and
           "resetOlder(stream)" in render_older),
          "every paint hands the list to whatever stream the button would walk")
    check("state.olderStream !== stream" in render_older,
          "…and a list belonging to another stream cannot render at all, "
          "independently of that reset")
    check('setAttribute("aria-label"' in render_older,
          "…and the list's own name says whose history it is: 'older records' "
          "immediately above a live tail reads as the older part of THAT tail")
    check('aria-label="older records' in MARKUP,
          "the document ships the generic label the page then specialises")
    older = slice_fn(CODE, "async function loadOlder", "\n}")
    reset_at = older.find("resetOlder(stream)")
    room_at = older.find("const room = olderRoom()")
    check(reset_at != -1 and room_at != -1 and reset_at < room_at,
          "the fetch path re-checks the identity BEFORE reading the budget: a "
          "list still holding another stream's rows reports a full one")
    # The anchors are the other half. They describe what THIS connection's
    # replay cut off, exactly like the wire notices beside them.
    conn = slice_fn(CODE, "function connect(", "\n}")
    check("state.anchors = {}" in conn,
          "a reconnect drops the anchors: kept, they offer load-older for a "
          "stream the new socket never called truncated, from an `oldest` the "
          "resumed replay may already have re-delivered")
    check("resetOlder(null)" in conn,
          "…and the loaded history goes with them — it is history of a window "
          "this socket has not described")
    notices_at = conn.find("state.wire.notices = []")
    anchors_at = conn.find("state.anchors = {}")
    check(notices_at != -1 and anchors_at != -1 and notices_at < anchors_at,
          "both live in the same place in connect(), for the same stated reason")


def test_a_failed_load_older_line_cannot_outlive_its_button():
    print("test_a_failed_load_older_line_cannot_outlive_its_button")
    # Every notice slot is cleared by the arm that owns it. This one's owner is
    # a button the operator can be locked out of — it hides the moment the
    # selected stream is not truncated — so a failed click pinned its line to
    # the notice bar for the life of the tab.
    clearer = slice_fn(CODE, "function clearOlderError(", "\n}")
    check('setError("load older", null)' in clearer, "the slot has a clearer")
    check("renderNotices()" in clearer,
          "…which repaints the box: renderNotices has already run by the time "
          "renderOlder is reached in a paint")
    check('if (!state.errors["load older"]) return' in clearer,
          "…and does nothing at all when there is no line, so it is free")
    render_older = slice_fn(CODE, "function renderOlder(", "\n}")
    unavailable = render_older[render_older.find("if (!available)"):]
    check("clearOlderError()" in unavailable and "button.hidden = true" in unavailable,
          "the not-available arm clears the line as it takes the button away")
    reset = slice_fn(CODE, "function resetOlder(", "\n}")
    check("clearOlderError()" in reset,
          "…and so does a stream change: the line described another stream's "
          "failed page")


def test_the_live_tail_shows_the_live_edge():
    print("test_the_live_tail_shows_the_live_edge")
    # `.log` is a 46 vh scroll box (~17 rows against a 400-row cap), so it
    # overflows inside the first replay burst and never un-overflows. With no
    # scroll management every later frame — every `live:true` row `.fresh`
    # exists to flash — is painted below the fold, permanently, and the page's
    # whole job is the live view.
    check(re.search(r"\.log\s*\{[^}]*overflow:\s*auto", CSS),
          "the live tail is a scroll box, which is what makes this necessary")
    edge = slice_fn(CODE, "function atLiveEdge(", "\n}")
    for part in ("scrollTop", "clientHeight", "scrollHeight", "PIN_SLACK_PX"):
        check(part in edge, f"the at-the-edge test reads {part}")
    check("return true" in edge,
          "a box with no scroll metrics counts as AT the edge — treating "
          "'no numbers' as 'the operator scrolled away' disables the follow "
          "for the whole first screen")
    flush = slice_fn(CODE, "function flushLog", "\n}")
    pinned_at = flush.find("atLiveEdge(list)")
    append_at = flush.find("list.appendChild(fragment)")
    write_at = flush.find("list.scrollTop = list.scrollHeight")
    check(pinned_at != -1 and append_at != -1 and pinned_at < append_at,
          "the check is made BEFORE the append — afterwards every box looks "
          "scrolled away")
    check(write_at != -1 and append_at < write_at,
          "…and the follow is applied after it")
    trim_at = flush.find("removeChild(list.firstChild)")
    check(trim_at != -1 and trim_at < write_at,
          "…after the trim too, so the position is the final one")
    check("heightBeforeTrim" in flush and "shift > 0" in flush,
          "a reader who scrolled away keeps their rows: the trim removes "
          "content ABOVE the viewport and that height is given back")
    older = slice_fn(CODE, "async function loadOlder", "\n}")
    check("list.scrollTop = 0" in older,
          "the history list gets the mirror image: a prepend lands above the "
          "scroll position, so a click that loaded 200 older rows would "
          "otherwise show the rows the operator was already looking at")


def test_a_run_stream_the_run_routes_cannot_answer_for_is_not_a_link():
    print("test_a_run_stream_the_run_routes_cannot_answer_for_is_not_a_link")
    # `store.validate_stream` accepts multi-component ids and `run:legacy:<task>`
    # is a documented one; `ID_PATTERNS["run"]` is `_NAME_RE`, which has no
    # colon. So `runIdOf("run:legacy:x")` names something every run route 400s.
    accepts_run_id = server_mod.ID_PATTERNS["run"][0]
    check("run:legacy:" in read(SRC / "aggregator" / "store.py"),
          "the store names `run:legacy:<task>` as a real stream id shape")
    try:
        store_mod.validate_stream("run:legacy:touch-repo-recon")
        accepted = True
    except Exception as err:                                   # noqa: BLE001
        accepted = f"{type(err).__name__}: {err}"
    check(accepted is True, f"…and validate_stream accepts it — {accepted}")
    check(not accepts_run_id("legacy:touch-repo-recon"),
          "…while the run routes refuse its suffix as a runId (400)")
    check(accepts_run_id("wf_1a3ffcdd"),
          "…and accept an ordinary single-component one")
    guard = slice_fn(CODE, "function isLinkableRunId(", "\n}")
    check('indexOf(":") === -1' in guard,
          "app.js tells the two apart by exactly that property")
    runs = slice_fn(CODE, "function renderRuns(", "\n}")
    check("isLinkableRunId(runId)" in runs and "rowPlain(" in runs,
          "an unroutable run stream renders as a row with no link, never as a "
          "link to a 400")
    check("no graph route" in runs,
          "…and says why, rather than looking like an ordinary row that does "
          "nothing when clicked")
    plain = slice_fn(CODE, "function rowPlain(", "\n}")
    check("setAttribute" not in plain and 'el("a"' not in plain,
          "…because that row builds no anchor and no href at all")
    check("rowstatic" in plain and ".rowstatic" in CSS,
          "…and it does not light up under the pointer: a row that offers a "
          "hover and then does nothing is a worse affordance than none")
    hello = slice_fn(CODE, "function onHello", "function onEvent")
    check("isLinkableRunId(runId)" in hello,
          "and the handshake's auto-select skips one too: booting into a 400 "
          "panel because the newest-written stream happens to be a legacy one "
          "is a worse first screen than no selection")


def test_the_session_timeline_can_reach_every_record_it_admits_to():
    print("test_the_session_timeline_can_reach_every_record_it_admits_to")
    # The panel used to show the first 120 records, say "more beyond this page"
    # and offer nothing that could reach them — a labelled dead end in the one
    # panel R-32 calls the session view.
    check(js_int("TIMELINE_MAX") <= server_mod.MAX_PAGE,
          f"the window the panel will grow to ({js_int('TIMELINE_MAX')}) stays "
          f"within the server's MAX_PAGE ({server_mod.MAX_PAGE}) — positive_int "
          f"CLAMPS an oversized limit rather than refusing it, so a wider "
          f"window would ask for rows that never come and the button would "
          f"never reach its end")
    check(js_int("TIMELINE_MAX") > js_int("TIMELINE_PAGE"),
          "…and it is wider than one page, or the button would be decorative")
    detail = slice_fn(CODE, "async function refreshDetail", "\n}")
    check("limit: state.timelineLimit" in detail,
          "the poll fetches the window the panel means to show, so a widened "
          "one survives the next refresh (this panel is re-fetched on the live "
          "cadence — pages stitched on this side would be thrown away)")
    widen = slice_fn(CODE, "function widenTimeline(", "\n}")
    check("Math.min(TIMELINE_MAX" in widen and "TIMELINE_PAGE" in widen,
          "a click widens it by one page, up to the ceiling")
    check("refreshDetail()" in widen, "…and re-asks for the wider window")
    select = slice_fn(CODE, "function select(", "\n}")
    check("state.timelineLimit = TIMELINE_PAGE" in select,
          "the window belongs to one session and resets with the selection")
    button = slice_fn(CODE, "function timelineButton(", "\n}")
    check("window full" in button and "button.disabled = true" in button,
          "a spent ceiling is said out loud, not left as a control that does "
          "nothing")
    session = slice_fn(CODE, "function renderSessionDetail", "\n}")
    check("showing the first " in session and "payload.count" in session,
          "the line above the list states what the panel is actually showing — "
          "counted off the response, not off the width that was requested")
    check("more beyond this page" not in session,
          "…and no longer implies a continuation that does not exist")
    rendered = slice_fn(CODE, "function renderDetail(", "\n}")
    check("state.timelineLimit" in rendered,
          "the width is part of the panel's signature, so the line cannot go "
          "stale when the records happen to come back identical")


def test_the_expensive_route_is_polled_on_its_own_slow_cadence():
    print("test_the_expensive_route_is_polled_on_its_own_slow_cadence")
    h_tasks = slice_fn(read(SRC / "aggregator" / "server.py"), "def h_tasks", "\ndef ")
    check("legacy_mod.scan(" in h_tasks,
          "h_tasks re-reads and re-reduces every legacy folder from disk on "
          "every call — it is not served from the in-memory reduction")
    check("const TASKS_MS" in CODE, "/api/tasks therefore has its own cadence constant")
    check("const REFRESH_MS" in CODE, "…distinct from the live model cadence")
    model = slice_fn(CODE, "async function refreshModel", "\n}")
    check('"/api/tasks"' not in model,
          "the live-cadence refresh does not touch /api/tasks")
    detail = slice_fn(CODE, "async function refreshDetail", "\n}")
    check("state.tasks.filter(" in detail,
          "a selected task's payload is a row of the list already in hand")
    fetches = CODE.count('getJson("/api/tasks")')
    check(fetches == 1,
          f"…and the whole page issues that request from exactly ONE place — {fetches}")
    shared = slice_fn(CODE, "function fetchTasks(", "\n}")
    check("if (tasksInFlight) return tasksInFlight" in shared,
          "a second caller joins the request in flight rather than starting a "
          "second one: at boot the slow poll and a direct #task/<id> load's "
          "cold start both want this list, unordered")
    check("await fetchTasks()" in detail,
          "…which is how the cold-start arm gets it")
    boot = slice_fn(CODE, "function boot(", "\n}")
    check("window.setInterval(refreshTasks, TASKS_MS)" in boot,
          "the slow poll is wired at boot")


def test_an_empty_task_panel_says_why_it_is_empty():
    print("test_an_empty_task_panel_says_why_it_is_empty")
    # The move of the tasks root is the occasion for this guard (UI-13): the
    # aggregator resolves that root itself, so a half-landed move empties the
    # third sidebar panel and blanks its count. `/api/tasks` already ANSWERS why
    # — a 200 with `note: "no local-orchestrators root configured"` — and the
    # page used to read `body.tasks` only, so a page that had quietly forgotten
    # its history looked exactly like history that ended.
    server_src = read(SRC / "aggregator" / "server.py")
    h_tasks = slice_fn(server_src, "def h_tasks", "\ndef ")
    # Two shapes of empty answer carry a note — no root configured, and a
    # configured root that is not there (the shape the tasks-root move creates).
    # `test_server_core.py` owns the assertions about their content; this end
    # only cares that the page has something to render.
    check(h_tasks.count('"note":') == 2,
          "h_tasks answers 200-with-a-note for BOTH shapes of nothing-to-list")
    shared = slice_fn(CODE, "function fetchTasks(", "\n}")
    check("body.note" in shared and 'setError("tasks"' in shared,
          "…and fetchTasks routes that note into the SAME per-source slot a "
          "fetch failure uses, so it reaches a surface that already renders "
          "(the set-and-clear behaviour itself is driven in the node+vm arm)")
    check("`" not in shared,
          "the arm obeys app.js's no-backtick rule (no template literal — the "
          "slot is a plain string)")
    # The slot has to be painted, or "surfaced" is a claim about a variable.
    check("state.errors[source] = source" in CODE and
          "Object.keys(state.errors)" in CODE,
          "every error slot is composed and then enumerated onto the notice "
          "surface — the note is displayed, not merely stored")


def test_every_live_region_is_written_only_when_it_changes():
    print("test_every_live_region_is_written_only_when_it_changes")
    # A live region is `aria-live` OR one of the roles that imply it. Counting
    # only the attribute missed `#notice` entirely — an implicit polite region
    # that was cleared and re-appended on every paint, i.e. re-announced at the
    # paint rate — while the guard's message claimed there was one region.
    regions = re.findall(r'id="([A-Za-z]+)"[^>]*(?:aria-live\s*=|role\s*=\s*'
                         r'"(?:status|alert|log)")', MARKUP)
    check(set(regions) == {"notice", "detailStatus"},
          f"the page's live regions are exactly #notice and #detailStatus — {regions}")
    section = re.search(r'<section id="detail"[^>]*>', MARKUP)
    check(section is not None and "aria-live" not in section.group(0),
          "…and NOT the #detail panel, which renderDetail clears and rebuilds "
          "whenever its signature moves (a screen reader would re-read it all)")
    check("announceDetail(" in CODE and "setText(dom.detailStatus" in CODE,
          "#detailStatus is written through setText, which writes only when the "
          "text actually changed — an unchanged summary announces nothing")
    notices = slice_fn(CODE, "function renderNotices", "\n}")
    check("state.noticeText" in notices and "if (next === state.noticeText) return" in notices,
          "#notice gets the same treatment: the lines are compared as text and "
          "an unchanged box is not touched at all")
    order = notices.find("state.noticeText") < notices.find("clear(box)")
    check(order, "…and the comparison happens BEFORE the box is cleared, which "
                 "is what makes it a change-guard rather than a rebuild")
    check(".detail-status {" in CSS, "the stylesheet defines the status line")
    # m1: `hidden` must actually hide. `[hidden] { display: none }` is a UA
    # rule and loses to any author rule setting `display` on the same element —
    # `.notice { display: flex }` did exactly that, leaving a permanent empty
    # bar under the header.
    check(re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", CSS),
          "the stylesheet resets [hidden] at author level, so `node.hidden = "
          "true` beats every display rule in this file")


def test_the_load_older_button_names_the_stream_it_walks():
    print("test_the_load_older_button_names_the_stream_it_walks")
    render_older = slice_fn(CODE, "function renderOlder", "\n}")
    check('setText(button, "load older' in render_older and "stream" in render_older,
          "the button names the stream it walks: the log interleaves every "
          "stream while a truncation and its anchor are per-stream")
    check("currentStream()" in render_older, "…and that is the current stream")


def test_no_liveness_class_is_attached_outside_a_whitelist():
    print("test_no_liveness_class_is_attached_outside_a_whitelist")
    # `st-*` is the reducer's vocabulary. Painting one onto a value the reducer
    # did not conclude states a verdict the server never reached — which is the
    # GD-23 failure this whole plan exists to end, and the run list's `current`
    # marker was doing it: `_current_run_stream` picks that stream by
    # `os.stat().st_mtime` and says so in its own docstring.
    server_src = read(SRC / "aggregator" / "server.py")
    check("A file mtime is not a record timestamp" in server_src,
          "the server itself insists its `current` pick is not a sequencing or "
          "liveness fact")
    tables = ("NODE_STATE_CLASS", "LEGACY_STATE_CLASS", "MIRROR_CLASS", "MODE_CLASS")
    rest = CODE
    for name in tables:
        rest = rest.replace(slice_fn(CODE, f"const {name} = {{", "};"), "")
    stray = sorted(set(re.findall(r'"(st-[a-z-]+)"', rest)))
    check(stray == ["st-other"],
          f"outside the whitelist tables the only `st-` literal is classOf's "
          f"fallback — {stray}")
    runs = slice_fn(CODE, "function renderRuns", "\n}")
    check('chip("chip-current"' in runs and '"st-' not in runs,
          "the `current` marker is a neutral chip of its own")
    for cls in (".chip-current", ".chip-warn"):
        check(cls + " {" in CSS, f"the stylesheet defines {cls}")
    # The one remaining `st-` at a call site is the mode chip's, and it goes
    # through the table so the animated `.chip-mode.st-running` rule stays
    # reachable exactly as the CSS sweep above expects.
    header = slice_fn(CODE, "function renderHeader", "\n}")
    check("classOf(MODE_CLASS," in header,
          "the mode chip's class is a table lookup too")


def test_a_run_that_starts_after_the_handshake_reaches_the_sidebar():
    print("test_a_run_that_starts_after_the_handshake_reaches_the_sidebar")
    # `hello` is sent once per connection. The server re-evaluates its stream
    # set on EVERY tick and has a dedicated arm for a stream born after the
    # boundary — it publishes an `anchors` frame and then the backlog as
    # `live:false` backfill. A page that consumes those records into the log,
    # the rollup and `state.delivered` but not into `state.streams` prints a
    # run's rows under a sidebar that says the run does not exist.
    server_src = read(SRC / "aggregator" / "server.py")
    check("A stream that came into existence after the mode switch" in server_src,
          "the server has a late-stream arm and describes it in those words")
    check("/api/runs" not in server_src,
          "…and there is no runs route, so the socket is the ONLY channel that "
          "can teach the page about a run")
    note = slice_fn(CODE, "function noteStream", "\n}")
    check("state.streams.push" in note and "state.streams.sort()" in note,
          "noteStream adds a stream the page had not heard of")
    check("state.currentRun" not in note,
          "…and does NOT re-derive `currentRun` from it: which run is current "
          "is the server's word (GD-23), so a run learned here renders without "
          "the marker until a handshake names it")
    for handler in ("function onEvent", "function onAnchors"):
        body = slice_fn(CODE, handler, "\n}")
        check("noteStream(" in body, f"{handler} learns the stream it was handed")


def test_the_agent_tree_is_drawn_as_containment():
    print("test_the_agent_tree_is_drawn_as_containment")
    # R-32 names a tree. `observed.parent` / `observed.root` are projected by
    # `_agent_payload` and are harness facts, so nesting on them is rendering,
    # not deriving — while a flat list beside a `depth N` chip announces that a
    # hierarchy exists without showing it.
    server_src = read(SRC / "aggregator" / "server.py")
    check('"root", "parent"' in server_src,
          "the agent projection carries the spawn edge")
    tree = slice_fn(CODE, "function agentTree", "\nfunction ")
    check("observed || {}).parent" in tree, "the tree nests by that edge")
    check('el("ul", depth ? "cards nested" : "cards")' in tree,
          "a child list is a real nested list, not an indent class on a flat row")
    check("AGENT_TREE_DEPTH" in tree and "drawn.has(id)" in tree,
          "the recursion is depth-capped and visits each agent once — a lost "
          "record can leave two agents naming each other as parent")
    check("not placed in the tree" in tree,
          "…and whatever the walk cannot reach is still rendered, and labelled")
    check(re.search(r"\.cards\.nested\s*\{[^}]*border-left", CSS),
          "the nesting is visible in the stylesheet, not only in the DOM")
    session = slice_fn(CODE, "function renderSessionDetail", "\n}")
    check("agents-by-session route" in session,
          "R-32's PER-SESSION tree needs a route the read API does not have, "
          "and the panel states that gap instead of joining the set itself "
          "(that join would be a client-side derivation — GD-23)")
    routes = {route for (method, route) in server_mod.READ_ROUTES}
    check(not [r for r in routes if "agent" in r and "session" in r],
          f"…and the gap is real: no agents-by-session route exists — {sorted(routes)}")


def test_every_growing_collection_is_capped():
    print("test_every_growing_collection_is_capped")
    # The file's thesis is "capped from day one". The rollup map was the one
    # collection that was not: an entry per (stream, ref) for every stream the
    # socket ever mentions, held for the life of the tab.
    check("const TOKENS_MAX" in CODE, "the rollup map has a named cap")
    note_tokens = slice_fn(CODE, "function noteTokens(", "\n}")
    check("state.tokens.size > TOKENS_MAX" in note_tokens and
          "state.tokens.delete(oldest)" in note_tokens,
          "…and it is enforced, FIFO, where the map is written")
    check("state.tokensEvicted" in note_tokens,
          "an eviction is counted rather than silently lowering the sum")
    # LRU, not first-seen: `Map.set` on an existing key keeps its ORIGINAL
    # position, so a straight overwrite made the cap evict the busiest
    # long-lived ref while a one-shot ref observed later survived.
    delete_at = note_tokens.find("state.tokens.delete(key)")
    set_at = note_tokens.find("state.tokens.set(key, totals)")
    check(delete_at != -1 and set_at != -1 and delete_at < set_at,
          "a ref that is written again is re-inserted, which makes the cap "
          "evict the ref touched longest ago rather than the one seen first")
    title = slice_fn(CODE, "function rollupTitle(", "\n}")
    check("this sum is a floor" in title and "not summed" in title,
          "…and both omissions are stated on the element that shows the sum")
    for cap, what in (("SEEN_MAX", "the de-dup set"), ("LOG_MAX", "the log"),
                      ("OLDER_MAX", "the loaded history"), ("TOKENS_MAX", "the rollup"),
                      ("TIMELINE_MAX", "the session timeline window")):
        check("const " + cap in CODE, f"{what} is bounded by {cap}")


def test_the_log_meta_line_arithmetic_closes():
    print("test_the_log_meta_line_arithmetic_closes")
    # Three numbers on one line only help if they add up. The old ones did not:
    # "shown" was "ever appended" (403 beside a 400-row list) and one counter
    # mixed the pending-queue trim with the DOM eviction.
    flush = slice_fn(CODE, "function flushLog", "\n}")
    check('fmtInt(state.logSeen) + " seen"' in flush,
          "`seen` is every entry the log was offered")
    check('fmtInt(list.childElementCount) + " shown"' in flush,
          "`shown` is read off the DOM, so it cannot claim more rows than exist")
    check("older dropped" in flush and "dropped before paint" in flush,
          "the DOM eviction and the queue trim are two labels, not one number")
    push = slice_fn(CODE, "function pushLog", "\n}")
    check("state.logSeen += 1" in push and "state.queueDropped +=" in push,
          "…and they are counted where each actually happens")
    # The ack's `backfilled` is the server's count of records it re-sent for a
    # rewind — the one number that says a resume re-delivered something rather
    # than being a no-op, and the other half of the duplicate count beside it.
    check("re-sent on resume" in flush, "what a resume re-delivered is displayed")
    subscribed = slice_fn(CODE, "function onSubscribed", "\n}")
    check("frame.backfilled" in subscribed and "state.backfilled +=" in subscribed,
          "…and it is read off the frame that carries it, not inferred")


def test_the_events_toolbar_is_not_the_heading():
    print("test_the_events_toolbar_is_not_the_heading")
    # Inside the `<h2>`, the counters and the button became part of the
    # heading's accessible name: "events 0 seen · 0 shown · 400 older loaded of
    # 400 · window 500 load older · run:wf_a" announced as one heading.
    headings = re.findall(r"<h2[^>]*>(.*?)</h2>", MARKUP, re.DOTALL)
    for heading in headings:
        check("<button" not in heading,
              f"no heading contains a control — {heading.strip()[:60]!r}")
    events = [h for h in headings if "events" in h]
    check(len(events) == 1 and events[0].strip() == "events",
          f"the events heading is the word 'events' — {events}")
    bar = re.search(r'<div class="logbar">(.*?)</div>', MARKUP, re.DOTALL)
    check(bar is not None, "…and there is a toolbar row below it")
    for element in ("logMeta", "olderBtn"):
        check(bar is not None and f'id="{element}"' in bar.group(1),
              f"#{element} sits in that row, not in the heading")
    check(".logbar {" in CSS, "the stylesheet gives that row a look of its own")


def test_a_region_is_rebuilt_only_when_it_changed():
    print("test_a_region_is_rebuilt_only_when_it_changed")
    # `render()` runs on a debounced frame — ~8 Hz during a burst — and every
    # list was cleared and rebuilt each time. A text selection inside the panel
    # dies on each rebuild, and a click whose mousedown and mouseup straddle
    # one produces nothing.
    body = slice_fn(CODE, "function region(", "\n}")
    check("state.sigs[name] === signature" in body,
          "a region paints only when its signature moved")
    for name in ("function renderSessions", "function renderRuns", "function renderTasks",
                 "function renderDetail"):
        fn = slice_fn(CODE, name, "\n}")
        check("region(" in fn and "const signature" in fn,
              f"{name} builds a signature and paints through it")
    detail = slice_fn(CODE, "function renderDetail", "\n}")
    check("state.detailRev" in detail and "state.tokensRev" in detail,
          "the panel's signature covers both the payload and the rollup line "
          "the socket can move without a refetch")
    check("function setDetail" in CODE and "state.detailRev += 1" in CODE,
          "…and the revision is bumped by the one writer of the payload")
    # …but ONLY when the payload moved. A signature keyed on a counter that
    # bumps on every write is not a signature: measured, the detail panel (the
    # largest region on the page, nested agent tree included) was torn down and
    # rebuilt on every 2 s poll returning a byte-identical body, and three
    # times over for three identical token records.
    set_detail = slice_fn(CODE, "function setDetail(", "\n}")
    check("state.detailKey" in set_detail and "if (key === state.detailKey) return" in set_detail,
          "an identical payload does not move the revision — the poll's normal "
          "answer is the same bytes it gave two seconds ago")
    guard_at = set_detail.find("if (key === state.detailKey) return")
    bump_at = set_detail.find("state.detailRev += 1")
    check(guard_at != -1 and bump_at != -1 and guard_at < bump_at,
          "…and the comparison guards the bump rather than following it")
    note_tokens = slice_fn(CODE, "function noteTokens(", "\n}")
    check("const moved" in note_tokens and "if (moved) state.tokensRev += 1" in note_tokens,
          "the token revision moves only when a rendered number did: a record "
          "carrying the same four totals under a new seq changes nothing")
    runs = slice_fn(CODE, "function renderRuns(", "\n}")
    signature = slice_fn(runs, "const signature", "region(")
    check("state.delivered" not in signature,
          "the run list's signature does NOT contain the delivered seq — that "
          "value changes on every frame for the stream, so it rebuilt the "
          "sidebar the operator clicks on every live frame")
    check("runSeqNodes" in runs and "setText(node," in runs,
          "…the seq is written into its own text node in place instead, which "
          "keeps the number current and the row intact")


def test_the_page_parses_as_javascript():
    print("test_the_page_parses_as_javascript")
    # Every other guard in this file asserts on TEXT, so a syntax error would
    # ship green. `node --check` closes that without adding a dependency; it is
    # skipped, loudly, where node is absent (the source guards still apply).
    node = shutil.which("node")
    if node is None:
        print("  skip: no node on PATH — the static guards above still apply")
        return
    proc = subprocess.run([node, "--check", str(JS_PATH)],
                          capture_output=True, text=True)
    first = (proc.stderr.strip().splitlines() or [""])[0]
    check(proc.returncode == 0, f"node --check app.js parses cleanly — {first}")


# --- the one guard that is not a source guard -----------------------------

#: A fake DOM, a fake socket and a fake `fetch`, driving `app.js` for real.
#:
#: Every other guard in this file asserts on TEXT, and a text guard cannot tell
#: you whether the page *works*. Both of attempt 2's majors passed every static
#: guard here and were still dead in execution: "load older" was capped by the
#: room left under `LOG_MAX`, which is structurally zero at the only moment the
#: button can exist, so a suite that asserted the *ingredient*
#: (`"LOG_MAX - list.childElementCount" in older`) locked the dead behaviour in;
#: and a run that started after the handshake had its rows in the log and its
#: tokens in the header while the sidebar refused to list it. Neither is
#: visible in the source without executing it, so this executes it.
#:
#: It is `node` + `vm` + ~200 lines of DOM, no dependency and no browser, and
#: it skips loudly where node is absent (the static guards still apply). The
#: DOM is deliberately small: elements, text nodes, one fragment, a class list
#: and the four mutators `app.js` uses. Anything it does not implement,
#: `app.js` must not be using.
HARNESS_JS = r""""use strict";
/* A fake DOM + fake socket + fake fetch, driving touch-visual/app.js for real.
 * Prints one `PASS: <label>` / `FAIL: <label> — <detail>` per assertion. */

const fs = require("fs");
const vm = require("vm");

const APP = process.argv[2];

let failed = 0;
function ok(label, cond, detail) {
    if (cond) {
        console.log("PASS: " + label);
    } else {
        failed += 1;
        console.log("FAIL: " + label + " — " + String(detail === undefined ? "" : detail));
    }
}

// --- DOM ------------------------------------------------------------------

class TextNode {
    constructor(data) { this.nodeType = 3; this.data = String(data); this.parentNode = null; }
    get textContent() { return this.data; }
    set textContent(v) { this.data = String(v); }
}

/* Every row is ROW_PX tall and every scroll box shows BOX_PX of them, so a
 * list overflows at 11 rows and `scrollHeight` is a real function of the
 * content. That is enough to drive the pin-to-bottom rule for real: a page
 * whose live edge is off-screen by default is the failure being guarded, and
 * no source guard can see it. */
const ROW_PX = 10;
const BOX_PX = 100;

class Element {
    constructor(tag) {
        this.nodeType = 1;
        this.tagName = String(tag).toUpperCase();
        this.childNodes = [];
        this.attributes = {};
        this.listeners = {};
        this.parentNode = null;
        this.hidden = false;
        this.disabled = false;
        this.id = "";
        this._class = "";
        this.appends = 0;
        this.removes = 0;
        this.scrollTop = 0;
        this.clientHeight = BOX_PX;
        const self = this;
        this.classList = {
            add(name) { const s = self._set(); s.add(name); self._class = [...s].join(" "); },
            remove(name) { const s = self._set(); s.delete(name); self._class = [...s].join(" "); },
            contains(name) { return self._set().has(name); },
        };
    }
    _set() { return new Set(String(this._class).split(/\s+/).filter(Boolean)); }
    get className() { return this._class; }
    set className(v) { this._class = String(v); }
    get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
    get childElementCount() { return this.children.length; }
    get firstChild() { return this.childNodes[0] || null; }
    get scrollHeight() { return this.children.length * ROW_PX; }
    appendChild(node) {
        if (node instanceof Fragment) {
            node.childNodes.slice().forEach((kid) => this.appendChild(kid));
            node.childNodes = [];
            return node;
        }
        if (node.parentNode) node.parentNode.removeChild(node);
        node.parentNode = this;
        this.childNodes.push(node);
        this.appends += 1;
        return node;
    }
    insertBefore(node, ref) {
        if (node instanceof Fragment) {
            const kids = node.childNodes.slice();
            node.childNodes = [];
            kids.forEach((kid) => this.insertBefore(kid, ref));
            return node;
        }
        if (node.parentNode) node.parentNode.removeChild(node);
        const at = ref ? this.childNodes.indexOf(ref) : -1;
        node.parentNode = this;
        if (at === -1) this.childNodes.push(node);
        else this.childNodes.splice(at, 0, node);
        this.appends += 1;
        return node;
    }
    removeChild(node) {
        const at = this.childNodes.indexOf(node);
        if (at !== -1) { this.childNodes.splice(at, 1); node.parentNode = null; this.removes += 1; }
        return node;
    }
    setAttribute(name, value) { this.attributes[String(name)] = String(value); }
    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name)
            ? this.attributes[name] : null;
    }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    fire(type) { (this.listeners[type] || []).forEach((fn) => fn({ type: type })); }
    get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
    set textContent(v) {
        this.childNodes.forEach((n) => { n.parentNode = null; });
        this.childNodes = [];
        if (String(v) !== "") {
            const t = new TextNode(v);
            t.parentNode = this;
            this.childNodes.push(t);
        }
    }
}

class Fragment extends Element {
    constructor() { super("#fragment"); }
}

const ELEMENT_IDS = ["conn", "connText", "modeChip", "mirrorChip", "reducerChip", "rollup",
                     "notice", "sessionList", "runList", "taskList", "sessionCount",
                     "runCount", "taskCount", "detailHead", "detailStatus", "detailBody",
                     "log", "older", "logMeta", "olderBtn"];
const byId = {};
ELEMENT_IDS.forEach((id) => {
    const node = new Element(id === "log" || id === "older" ? "ol" : "div");
    node.id = id;
    byId[id] = node;
});
byId.older.hidden = true;
byId.olderBtn.hidden = true;
byId.notice.hidden = true;

const meta = new Element("meta");
meta.setAttribute("content", "test-token");

const document = {
    readyState: "complete",
    body: new Element("body"),
    createElement: (tag) => new Element(tag),
    createTextNode: (v) => new TextNode(v),
    createDocumentFragment: () => new Fragment(),
    getElementById: (id) => byId[id] || null,
    querySelector: (sel) => (String(sel).indexOf("touch-token") !== -1 ? meta : null),
    addEventListener: () => {},
};

// --- timers, socket, fetch ------------------------------------------------

const timers = [];
function later(fn) { timers.push(fn); return timers.length; }
function drain() {
    let guard = 0;
    while (timers.length && guard < 5000) { timers.shift()(); guard += 1; }
}

/* Intervals are REGISTERED rather than dropped on the floor, and fired only
 * when the driver says so.
 *
 * `setInterval: () => 0` left `resync` — half of R-55's resume mechanism, the
 * half the plan calls "a package" with the absolute tokens — asserted by
 * source text alone. Firing them on a timer would make the run
 * non-deterministic; firing them on demand is what a test wants. */
const intervals = [];
function everyTick(fn) { intervals.push(fn); return intervals.length; }
function tickIntervals() { intervals.slice().forEach((fn) => fn()); }
function stopInterval(id) {
    if (id >= 1 && id <= intervals.length) intervals[id - 1] = () => {};
}

const sockets = [];
function FakeWS(url) {
    this.url = String(url);
    this.readyState = 1;
    this.sent = [];
    sockets.push(this);
}
FakeWS.OPEN = 1;
FakeWS.prototype.close = function () { this.readyState = 3; };
FakeWS.prototype.send = function (data) { this.sent.push(data); };

let runGraph = { runId: "wf_a", observed: {}, nodes: [], agents: [] };
/** The session the timeline arm pretends to hold, in records. */
const SESSION_RECORDS = 300;
/** Flipped on to make the backwards-paging route fail for one click. */
let eventsFail = false;
/** The `note` /api/tasks answers with, or null for the ordinary answer. */
let tasksNote = null;
const fetched = [];
const fetchedUrls = [];
function fakeFetch(url) {
    const u = new URL(String(url));
    fetched.push(u.pathname);
    fetchedUrls.push(u.pathname + "?" + u.searchParams.toString());
    if (u.pathname === "/api/events" && eventsFail) {
        return Promise.resolve({
            ok: false, status: 503,
            json: () => Promise.resolve({ message: "the store is unreadable" }),
        });
    }
    let body = {};
    if (u.pathname === "/api/sessions") body = { sessions: [] };
    else if (u.pathname === "/api/tasks") {
        // A 200 that explains its own empty list (UI-13). `tasksNote` is null
        // for the ordinary answer, which is what makes the CLEAR assertable.
        body = tasksNote === null
            ? { tasks: [], count: 0 }
            : { tasks: [], count: 0, note: tasksNote };
    }
    else if (u.pathname === "/health") body = { mirror: { state: "absent" } };
    else if (u.pathname === "/api/run/graph") body = runGraph;
    else if (u.pathname === "/api/session/timeline") {
        // A real window: `limit` records of a session that holds more, with
        // the server's own `hasMore` and its `(lineNo, _id)` cursor pair.
        const limit = Number(u.searchParams.get("limit")) || 0;
        const count = Math.min(limit, SESSION_RECORDS);
        const records = [];
        for (let n = 1; n <= count; n += 1) {
            records.push({ lineNo: n, _id: "r" + n, type: "user",
                           ts: "2026-07-26T00:00:00.000Z" });
        }
        body = { session: u.searchParams.get("session"),
                 sessionDoc: { id: u.searchParams.get("session"), kind: "historical" },
                 records: records, count: count,
                 nextSince: count, nextSinceId: "r" + count,
                 hasMore: SESSION_RECORDS > count };
    } else if (u.pathname === "/api/events") {
        const before = Number(u.searchParams.get("before"));
        const limit = Number(u.searchParams.get("limit"));
        const start = Math.max(0, before - limit);
        const records = [];
        for (let seq = start; seq < before; seq += 1) {
            records.push({ seq: seq, kind: "event", ts: "2026-07-26T00:00:00.000Z",
                           provenance: "harness", ref: { agent: "old" },
                           data: { note: "history" } });
        }
        body = { stream: u.searchParams.get("stream"), records: records,
                 count: records.length, hasOlder: start > 0,
                 oldest: records.length ? records[0].seq : null };
    }
    return Promise.resolve({
        ok: true, status: 200, json: () => Promise.resolve(body),
    });
}

const window = {
    TOUCH_TOKEN: "",
    location: {
        href: "http://127.0.0.1:8930/", search: "", hash: "", protocol: "http:",
        replace(value) { window.location.hash = String(value); },
    },
    setTimeout: (fn) => later(fn),
    clearTimeout: () => {},
    setInterval: (fn) => everyTick(fn),
    clearInterval: (id) => stopInterval(id),
    requestAnimationFrame: (fn) => later(fn),
    addEventListener: () => {},
};

const sandbox = {
    document: document, window: window, WebSocket: FakeWS, fetch: fakeFetch,
    URL: URL, URLSearchParams: URLSearchParams, console: console,
    Math: Math, JSON: JSON, Date: Date, Number: Number, String: String,
    Object: Object, Array: Array, Set: Set, Map: Map, Promise: Promise, Error: Error,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(APP, "utf8"), sandbox, { filename: APP });

// --- driving --------------------------------------------------------------

async function settle() {
    for (let round = 0; round < 12; round += 1) {
        drain();
        await new Promise((resolve) => setImmediate(resolve));
    }
    drain();
}

function send(sock, frame) {
    if (sock.onmessage) sock.onmessage({ data: JSON.stringify(frame) });
}

function eventFrame(stream, seq, live, record) {
    return { type: "event", live: live, stream: stream, seq: seq,
             cursor: stream + "#" + String(seq).padStart(12, "0"),
             record: record || { kind: "event", ts: "2026-07-26T01:00:00.000Z",
                                 provenance: "harness", ref: { agent: "a" },
                                 data: { note: "n" } } };
}

function rowsOf(node) { return node.children.length; }

function findLists(node, out) {
    node.children.forEach((kid) => {
        if (kid.tagName === "UL") out.push(kid);
        findLists(kid, out);
    });
    return out;
}

function findTags(node, tag, out) {
    node.children.forEach((kid) => {
        if (kid.tagName === tag) out.push(kid);
        findTags(kid, tag, out);
    });
    return out;
}

/** Any anchor with an href anywhere under `node` — i.e. "is this row clickable". */
function hasLink(node) {
    return findTags(node, "A", []).some((a) => a.getAttribute("href") !== null);
}

function rowNamed(list, needle) {
    return list.children.filter((row) => row.textContent.indexOf(needle) !== -1)[0] || null;
}

/** At the bottom of its own scroll box, by the same rule the page uses. */
function atEdge(node) {
    return node.scrollTop + node.clientHeight >= node.scrollHeight - 40;
}

(async () => {
    const sock = sockets[0];
    ok("the page opens a socket at boot", !!sock, "no WebSocket was constructed");
    if (!sock) { process.exit(1); }
    ok("the socket URL carries the token", sock.url.indexOf("token=test-token") !== -1, sock.url);

    sock.onopen();
    send(sock, { type: "hello", live: false, mode: "replay", streams: ["run:wf_a"],
                 currentRun: "run:wf_a", window: 500, reducerVersion: "3", cursors: {} });
    await settle();

    // --- replayed frames paint once, live frames animate -------------------
    send(sock, eventFrame("run:wf_a", 1, false));
    send(sock, eventFrame("run:wf_a", 2, false));
    send(sock, { type: "mode", live: true, mode: "tail", cursors: { "run:wf_a": 2 },
                 oldest: {}, truncated: {} });
    send(sock, eventFrame("run:wf_a", 3, true));
    await settle();
    ok("every frame reaches the log", rowsOf(byId.log) === 3, rowsOf(byId.log));
    const rows = byId.log.children;
    ok("a replayed row does not animate",
       rows[0].classList.contains("fresh") === false, rows[0].className);
    ok("a live row does", rows[2].classList.contains("fresh") === true, rows[2].className);

    // --- M2: a run that appears mid-connection reaches the sidebar ---------
    ok("the sidebar starts with the handshake's one run", rowsOf(byId.runList) === 1,
       byId.runList.textContent);
    send(sock, { type: "anchors", live: true, stream: "run:wf_b", oldest: 5, truncated: false });
    send(sock, eventFrame("run:wf_b", 6, false));
    await settle();
    ok("a stream first named AFTER the handshake is listed", rowsOf(byId.runList) === 2,
       byId.runList.textContent);
    ok("…and the log already had its rows, which is why it must be",
       byId.log.textContent.indexOf("run:wf_b") !== -1, byId.log.textContent.slice(0, 200));
    const currents = byId.runList.textContent.split("current").length - 1;
    ok("only the run the server named is marked current", currents === 1, currents);

    // --- absolute tokens ---------------------------------------------------
    send(sock, eventFrame("run:wf_a", 10, true, {
        kind: "token", ts: "2026-07-26T01:00:01.000Z", provenance: "harness",
        ref: { agent: "z" }, data: { in: 100, out: 5, cached: 0, cache_write: 0 } }));
    await settle();
    send(sock, eventFrame("run:wf_a", 11, true, {
        kind: "token", ts: "2026-07-26T01:00:02.000Z", provenance: "harness",
        ref: { agent: "z" }, data: { in: 300, out: 5, cached: 0, cache_write: 0 } }));
    await settle();
    ok("a later absolute token record replaces the earlier one, never adds to it",
       byId.rollup.textContent.indexOf("in 300") === 0, byId.rollup.textContent);

    // --- M1: load older actually paints, on a FULL log ---------------------
    for (let seq = 100; seq < 520; seq += 1) send(sock, eventFrame("run:wf_a", seq, true));
    await settle();
    ok("the live log is pinned at its cap", rowsOf(byId.log) === 400, rowsOf(byId.log));
    send(sock, { type: "anchors", live: true, stream: "run:wf_a", oldest: 1000,
                 truncated: true });
    await settle();
    ok("a declared truncation reveals the button", byId.olderBtn.hidden === false,
       byId.olderBtn.hidden);
    ok("…and it is clickable", byId.olderBtn.disabled === false, byId.olderBtn.disabled);

    const before = fetched.filter((p) => p === "/api/events").length;
    byId.olderBtn.fire("click");
    await settle();
    ok("a click on a FULL log paints history", rowsOf(byId.older) === 200,
       rowsOf(byId.older));
    ok("…in its own list, leaving the live tail whole", rowsOf(byId.log) === 400,
       rowsOf(byId.log));
    ok("…which is one request", fetched.filter((p) => p === "/api/events").length ===
       before + 1, fetched.filter((p) => p === "/api/events").length);
    ok("history is not animated", byId.older.children.every(
        (row) => !row.classList.contains("fresh")), "a history row carries .fresh");
    ok("the history list is revealed", byId.older.hidden === false, byId.older.hidden);

    byId.olderBtn.fire("click");
    await settle();
    ok("a second click fills the budget", rowsOf(byId.older) === 400, rowsOf(byId.older));

    const spent = fetched.filter((p) => p === "/api/events").length;
    byId.olderBtn.fire("click");
    await settle();
    ok("a click with no room left fetches nothing",
       fetched.filter((p) => p === "/api/events").length === spent,
       fetched.filter((p) => p === "/api/events").length);
    ok("…and the button says so", byId.olderBtn.disabled === true &&
       byId.olderBtn.textContent.indexOf("full") !== -1,
       byId.olderBtn.textContent + " disabled=" + byId.olderBtn.disabled);

    // --- the meta line's numbers close -------------------------------------
    const metaText = byId.logMeta.textContent;
    const seen = Number((/([\d,]+) seen/.exec(metaText) || [0, "0"])[1].replace(/,/g, ""));
    const shown = Number((/([\d,]+) shown/.exec(metaText) || [0, "0"])[1].replace(/,/g, ""));
    const dropped = Number((/([\d,]+) older dropped/.exec(metaText) || [0, "0"])[1]
        .replace(/,/g, ""));
    const queued = Number((/([\d,]+) dropped before paint/.exec(metaText) || [0, "0"])[1]
        .replace(/,/g, ""));
    ok("seen == shown + trimmed + dropped-before-paint",
       seen === shown + dropped + queued,
       metaText + " :: " + seen + " vs " + (shown + dropped + queued));

    // --- the notice box is a live region, written only on change -----------
    send(sock, { type: "wat", live: true });
    await settle();
    ok("an unknown frame type produces a notice", byId.notice.hidden === false,
       byId.notice.textContent);
    const appendsBefore = byId.notice.appends;
    const removesBefore = byId.notice.removes;
    for (let i = 0; i < 3; i += 1) {
        send(sock, { type: "tick", live: true, ts: "2026-07-26T01:02:0" + i + ".000Z" });
        await settle();
    }
    ok("…and three unchanged paints do not touch it",
       byId.notice.appends === appendsBefore && byId.notice.removes === removesBefore,
       "+" + (byId.notice.appends - appendsBefore) + " appends, +" +
       (byId.notice.removes - removesBefore) + " removes");

    // --- the agent tree is nested ------------------------------------------
    runGraph = {
        runId: "wf_a", observed: { provenance: "harness" }, nodes: [],
        agents: [
            { id: "aaa", observed: { provenance: "harness" }, derived: { state: "done", label: "done" } },
            { id: "bbb", observed: { provenance: "harness", parent: "aaa" },
              derived: { state: "done", label: "done" } },
            { id: "ccc", observed: { provenance: "harness", parent: "bbb" },
              derived: { state: "done", label: "done" } },
        ],
    };
    // A seq no history page covered, so this is not a de-duplicated frame.
    send(sock, eventFrame("run:wf_a", 5000, true));
    await settle();
    const lists = findLists(byId.detailBody, []);
    const nested = lists.filter((ul) => ul.className.indexOf("nested") !== -1);
    ok("the agent panel nests children inside their parent's card", nested.length === 2,
       lists.map((l) => l.className).join(" | "));
    ok("…and every agent is drawn exactly once",
       byId.detailBody.textContent.split("bbb").length - 1 >= 1 &&
       byId.detailBody.textContent.split("ccc").length - 1 >= 1,
       byId.detailBody.textContent.slice(0, 300));

    // --- the regions that are rebuilt on every LIVE frame ------------------
    //
    // A `tick` is the one frame type that moves nothing in either signature,
    // so asserting this over three ticks asserted it in the only case where it
    // was never at risk. These are the two live paths that actually ran hot:
    // the run list (its signature carried the per-frame delivered seq) and the
    // detail panel (its revision counter bumped on byte-identical refetches
    // and on unchanged token records).
    const sidebarAppends = byId.runList.appends;
    const sidebarRemoves = byId.runList.removes;
    const detailAppends = byId.detailBody.appends;
    for (let seq = 6000; seq < 6003; seq += 1) {
        send(sock, eventFrame("run:wf_a", seq, true));
        await settle();
    }
    ok("an unchanged run list survives three LIVE frames",
       byId.runList.appends === sidebarAppends && byId.runList.removes === sidebarRemoves,
       "+" + (byId.runList.appends - sidebarAppends) + " appends, +" +
       (byId.runList.removes - sidebarRemoves) + " removes");
    ok("…while the seq it displays is still current",
       byId.runList.textContent.indexOf("seq 6,002") !== -1,
       byId.runList.textContent.slice(0, 240));
    ok("…and the detail panel is not rebuilt by them either",
       byId.detailBody.appends === detailAppends,
       "+" + (byId.detailBody.appends - detailAppends) + " appends");

    const beforeTokens = byId.detailBody.appends;
    for (let seq = 6100; seq < 6103; seq += 1) {
        send(sock, eventFrame("run:wf_a", seq, true, {
            kind: "token", ts: "2026-07-26T01:00:03.000Z", provenance: "harness",
            ref: { agent: "z" }, data: { in: 300, out: 5, cached: 0, cache_write: 0 } }));
        await settle();
    }
    ok("three IDENTICAL token records do not rebuild the detail panel",
       byId.detailBody.appends === beforeTokens,
       "+" + (byId.detailBody.appends - beforeTokens) + " appends");
    send(sock, eventFrame("run:wf_a", 6200, true, {
        kind: "token", ts: "2026-07-26T01:00:04.000Z", provenance: "harness",
        ref: { agent: "z" }, data: { in: 900, out: 5, cached: 0, cache_write: 0 } }));
    await settle();
    ok("…and one that MOVED does", byId.detailBody.appends > beforeTokens &&
       byId.rollup.textContent.indexOf("in 900") === 0,
       byId.rollup.textContent);

    // --- the live tail shows the live edge ---------------------------------
    send(sock, eventFrame("run:wf_a", 6300, true));
    await settle();
    ok("the live tail follows its newest row", atEdge(byId.log),
       byId.log.scrollTop + " + " + byId.log.clientHeight + " vs " + byId.log.scrollHeight);
    byId.log.scrollTop = 50;
    send(sock, eventFrame("run:wf_a", 6301, true));
    await settle();
    ok("…and stops following the moment the operator scrolls away",
       !atEdge(byId.log) && byId.log.scrollTop <= 50, byId.log.scrollTop);
    ok("…keeping their rows under their eyes when the cap trims above them",
       byId.log.scrollTop === 40, byId.log.scrollTop);
    byId.log.scrollTop = byId.log.scrollHeight;
    send(sock, eventFrame("run:wf_a", 6302, true));
    await settle();
    ok("…and follows again when they scroll back to the edge", atEdge(byId.log),
       byId.log.scrollTop + " vs " + byId.log.scrollHeight);

    // --- M1: the history budget belongs to ONE stream ----------------------
    send(sock, { type: "anchors", live: true, stream: "run:wf_b", oldest: 2000,
                 truncated: true });
    await settle();
    ok("the history list is still wf_a's, full, and says so",
       rowsOf(byId.older) === 400 && byId.olderBtn.textContent.indexOf("wf_a") !== -1,
       rowsOf(byId.older) + " rows :: " + byId.olderBtn.textContent);

    window.location.hash = "#run/wf_b";
    sandbox.route();
    await settle();
    ok("switching streams empties the history list", rowsOf(byId.older) === 0,
       rowsOf(byId.older) + " rows :: " + byId.older.textContent.slice(0, 120));
    ok("…and hides it, so one stream's history never sits above another's tail",
       byId.older.hidden === true, byId.older.hidden);
    ok("…and the affordance is alive again for the newly selected stream",
       byId.olderBtn.hidden === false && byId.olderBtn.disabled === false &&
       byId.olderBtn.textContent.indexOf("wf_b") !== -1,
       byId.olderBtn.textContent + " hidden=" + byId.olderBtn.hidden +
       " disabled=" + byId.olderBtn.disabled);

    byId.older.scrollTop = 999;
    byId.olderBtn.fire("click");
    await settle();
    ok("a click paints the NEW stream's history", rowsOf(byId.older) === 200,
       rowsOf(byId.older));
    ok("…all of it that stream's records", byId.older.children.every(
        (row) => row.textContent.indexOf("run:wf_b") !== -1),
       byId.older.textContent.slice(0, 160));
    ok("…and the box scrolls to the oldest row just loaded",
       byId.older.scrollTop === 0, byId.older.scrollTop);

    // --- m2: a failed load-older line cannot outlive its button ------------
    eventsFail = true;
    byId.olderBtn.fire("click");
    await settle();
    eventsFail = false;
    ok("a failed page names itself in the notice box",
       byId.notice.textContent.indexOf("load older") !== -1,
       byId.notice.textContent);
    send(sock, { type: "anchors", live: true, stream: "run:wf_b", oldest: 1800,
                 truncated: false });
    await settle();
    ok("…and the line goes when the button it belongs to goes",
       byId.olderBtn.hidden === true &&
       byId.notice.textContent.indexOf("load older") === -1,
       byId.notice.textContent + " hidden=" + byId.olderBtn.hidden);

    // --- m4: a run stream the run routes cannot answer for -----------------
    send(sock, { type: "anchors", live: true, stream: "run:legacy:touch-repo-recon",
                 oldest: 1, truncated: false });
    await settle();
    const legacyRow = rowNamed(byId.runList, "legacy:touch-repo-recon");
    ok("a run:legacy:<task> stream is listed", legacyRow !== null,
       byId.runList.textContent.slice(0, 240));
    ok("…as a row with NO link, because /api/run/graph 400s on that runId",
       legacyRow !== null && hasLink(legacyRow) === false,
       legacyRow && legacyRow.textContent);
    ok("…and it says why", legacyRow !== null &&
       legacyRow.textContent.indexOf("no graph route") !== -1,
       legacyRow && legacyRow.textContent);
    const ordinaryRow = rowNamed(byId.runList, "wf_a");
    ok("…while an ordinary run row is still a link",
       ordinaryRow !== null && hasLink(ordinaryRow) === true,
       ordinaryRow && ordinaryRow.textContent);

    // --- m5: the session timeline can reach past its first page ------------
    window.location.hash = "#session/6b0a6f2e-0000-4000-8000-000000000000";
    sandbox.route();
    await settle();
    const firstPage = findTags(byId.detailBody, "OL", []).filter(
        (ol) => ol.className.indexOf("timeline") !== -1)[0];
    ok("the timeline paints its first window", firstPage && rowsOf(firstPage) === 120,
       firstPage && rowsOf(firstPage));
    ok("…and says what it is showing rather than 'more beyond this page'",
       byId.detailBody.textContent.indexOf(
           "showing the first 120 records of this session") !== -1,
       byId.detailBody.textContent.slice(0, 300));
    const more = findTags(byId.detailBody, "BUTTON", [])[0];
    ok("…with an affordance that can reach the rest", !!more && !more.disabled,
       more && more.textContent);
    if (more) more.fire("click");
    await settle();
    const widened = findTags(byId.detailBody, "OL", []).filter(
        (ol) => ol.className.indexOf("timeline") !== -1)[0];
    ok("a click widens the window", widened && rowsOf(widened) === 240,
       widened && rowsOf(widened));
    ok("…by asking the server for the wider window, not by stitching pages",
       fetchedUrls.filter((u) => u.indexOf("/api/session/timeline") === 0 &&
                                 u.indexOf("limit=240") !== -1).length === 1,
       fetchedUrls.filter((u) => u.indexOf("/api/session/timeline") === 0).join(" | "));
    // …and the width survives the live cadence, which is why it is a window.
    send(sock, eventFrame("run:wf_a", 7000, true));
    await settle();
    const polled = findTags(byId.detailBody, "OL", []).filter(
        (ol) => ol.className.indexOf("timeline") !== -1)[0];
    ok("…and the next poll does not throw the widened window away",
       polled && rowsOf(polled) === 240, polled && rowsOf(polled));

    // --- m3 / n7: a reconnect is a new connection --------------------------
    const anchoredBefore = byId.runList.textContent.indexOf("window truncated") !== -1;
    ok("a truncation was on screen before the socket dropped", anchoredBefore,
       byId.runList.textContent.slice(0, 240));
    sock.onclose();
    await settle();
    const sock2 = sockets[sockets.length - 1];
    ok("the page reconnects", !!sock2 && sock2 !== sock, sockets.length);
    sock2.onopen();
    send(sock2, { type: "hello", live: false, mode: "replay",
                  streams: ["run:wf_a", "run:wf_b"], currentRun: "run:wf_a",
                  window: 500, reducerVersion: "3", cursors: {}, resumed: true });
    send(sock2, { type: "mode", live: true, mode: "tail",
                  cursors: { "run:wf_a": 700 }, oldest: {}, truncated: {} });
    await settle();
    ok("a clean reconnect drops the previous connection's anchors",
       byId.runList.textContent.indexOf("window truncated") === -1 &&
       byId.olderBtn.hidden === true,
       byId.runList.textContent.slice(0, 240) + " :: hidden=" + byId.olderBtn.hidden);
    ok("…and the history loaded against them", rowsOf(byId.older) === 0,
       rowsOf(byId.older));

    const sentBefore = sock2.sent.length;
    tickIntervals();
    await settle();
    const subscribes = sock2.sent.map((raw) => JSON.parse(raw))
        .filter((frame) => frame.type === "subscribe");
    ok("the resync timer really re-asks for the cursors",
       sock2.sent.length > sentBefore && subscribes.length === 1,
       sock2.sent.join(" | "));
    ok("…with the position the SERVER published, never the newest seq received",
       subscribes.length === 1 && subscribes[0].cursors["run:wf_a"] === 700,
       JSON.stringify(subscribes));

    // --- UI-13: an empty task panel says why it is empty --------------------
    // A source guard can see that the note is passed to `setError`; only
    // execution can see that it reaches the notice box AND leaves again. The
    // second half is the one that rots: a note that never clears is the
    // fabricated-badge failure in another costume.
    tasksNote = "no local-orchestrators root configured";
    await sandbox.refreshTasks();
    await settle();
    ok("a note on an empty /api/tasks 200 is painted on the notice surface",
       byId.notice.textContent.indexOf(
           "no local-orchestrators root configured") !== -1 &&
       byId.notice.hidden === false,
       byId.notice.textContent + " :: hidden=" + byId.notice.hidden);
    ok("…attributed to the panel it explains, not to the page at large",
       byId.notice.textContent.indexOf("tasks: ") !== -1,
       byId.notice.textContent);
    tasksNote = null;
    await sandbox.refreshTasks();
    await settle();
    ok("…and the next note-free answer takes the line away again",
       byId.notice.textContent.indexOf("local-orchestrators") === -1,
       byId.notice.textContent);

    // --- n5: what a resume actually re-delivered ---------------------------
    send(sock2, { type: "subscribed", live: true, cursors: { "run:wf_a": 700 },
                  accepted: { "run:wf_a": 700 }, rejected: [],
                  backfilled: { "run:wf_a": 12 } });
    await settle();
    ok("the ack's backfill count reaches the meta line",
       byId.logMeta.textContent.indexOf("12 re-sent on resume") !== -1,
       byId.logMeta.textContent);

    console.log("harness done, failures=" + failed);
    process.exit(failed ? 1 : 0);
})().catch((err) => {
    console.log("FAIL: the harness ran to completion — " + (err && err.stack));
    process.exit(2);
});
"""

#: Every assertion the harness is expected to make, so that a harness which
#: silently stops half way (an exception swallowed, a scenario deleted) fails
#: this gate instead of passing it with fewer checks.
HARNESS_EXPECTED = (
    "the page opens a socket at boot",
    "the socket URL carries the token",
    "every frame reaches the log",
    "a replayed row does not animate",
    "a live row does",
    "the sidebar starts with the handshake's one run",
    "a stream first named AFTER the handshake is listed",
    "…and the log already had its rows, which is why it must be",
    "only the run the server named is marked current",
    "a later absolute token record replaces the earlier one, never adds to it",
    "the live log is pinned at its cap",
    "a declared truncation reveals the button",
    "…and it is clickable",
    "a click on a FULL log paints history",
    "…in its own list, leaving the live tail whole",
    "…which is one request",
    "history is not animated",
    "the history list is revealed",
    "a second click fills the budget",
    "a click with no room left fetches nothing",
    "…and the button says so",
    "seen == shown + trimmed + dropped-before-paint",
    "an unknown frame type produces a notice",
    "…and three unchanged paints do not touch it",
    "the agent panel nests children inside their parent's card",
    "…and every agent is drawn exactly once",
    "an unchanged run list survives three LIVE frames",
    "…while the seq it displays is still current",
    "…and the detail panel is not rebuilt by them either",
    "three IDENTICAL token records do not rebuild the detail panel",
    "…and one that MOVED does",
    "the live tail follows its newest row",
    "…and stops following the moment the operator scrolls away",
    "…keeping their rows under their eyes when the cap trims above them",
    "…and follows again when they scroll back to the edge",
    "the history list is still wf_a's, full, and says so",
    "switching streams empties the history list",
    "…and hides it, so one stream's history never sits above another's tail",
    "…and the affordance is alive again for the newly selected stream",
    "a click paints the NEW stream's history",
    "…all of it that stream's records",
    "…and the box scrolls to the oldest row just loaded",
    "a failed page names itself in the notice box",
    "…and the line goes when the button it belongs to goes",
    "a run:legacy:<task> stream is listed",
    "…as a row with NO link, because /api/run/graph 400s on that runId",
    "…and it says why",
    "…while an ordinary run row is still a link",
    "the timeline paints its first window",
    "…and says what it is showing rather than 'more beyond this page'",
    "…with an affordance that can reach the rest",
    "a click widens the window",
    "…by asking the server for the wider window, not by stitching pages",
    "…and the next poll does not throw the widened window away",
    "a truncation was on screen before the socket dropped",
    "the page reconnects",
    "a clean reconnect drops the previous connection's anchors",
    "…and the history loaded against them",
    "the resync timer really re-asks for the cursors",
    "…with the position the SERVER published, never the newest seq received",
    "the ack's backfill count reaches the meta line",
    "a note on an empty /api/tasks 200 is painted on the notice surface",
    "…attributed to the panel it explains, not to the page at large",
    "…and the next note-free answer takes the line away again",
)


def test_the_page_behaves_when_it_is_actually_driven():
    print("test_the_page_behaves_when_it_is_actually_driven")
    node = shutil.which("node")
    if node is None:
        print("  skip: no node on PATH — the static guards above still apply, but "
              "NOTHING here has been executed")
        return
    with tempfile.TemporaryDirectory() as tmp:
        driver = Path(tmp) / "drive.js"
        driver.write_text(HARNESS_JS, encoding="utf-8")
        proc = subprocess.run([node, str(driver), str(JS_PATH)],
                              capture_output=True, text=True, timeout=180)
    passed = set()
    for line in proc.stdout.splitlines():
        if line.startswith("PASS: "):
            passed.add(line[6:])
        elif line.startswith("FAIL: "):
            check(False, "driven: " + line[6:])
    for label in HARNESS_EXPECTED:
        check(label in passed, "driven: " + label)
    check(proc.returncode == 0,
          f"the harness itself ran to completion — rc {proc.returncode} "
          f"{proc.stderr.strip().splitlines()[:1]}")


def main():
    for t in (test_the_three_files_are_where_the_server_serves_them_from,
              test_the_page_carries_the_serve_time_token_where_it_is_valid,
              test_no_markup_sink_exists_in_the_page,
              test_the_stripper_sees_this_file_the_way_it_claims,
              test_no_control_verb_reaches_the_page,
              test_the_sidebar_lists_every_class_of_thing_the_store_knows,
              test_the_agent_tree_is_keyed_by_harness_facts,
              test_token_rollups_are_sums_of_absolute_records,
              test_degraded_and_derived_states_are_labelled,
              test_the_asserted_nodes_are_rendered_and_told_apart,
              test_the_class_whitelists_match_the_servers_vocabulary,
              test_the_wire_contract_is_restated_verbatim,
              test_only_live_frames_animate,
              test_the_resume_cursor_is_the_servers_not_ours,
              test_the_load_older_anchors_come_from_the_frames_that_know_them,
              test_the_page_never_infers_state,
              test_the_render_is_coalesced_and_the_log_is_capped,
              test_the_page_degrades_without_mongo_and_says_why,
              test_the_notice_surface_states_the_current_cycle,
              test_the_resync_never_asks_to_be_moved_forward,
              test_the_token_rollup_key_is_an_identity_not_a_display_string,
              test_load_older_has_its_own_room_and_can_therefore_paint,
              test_load_older_never_fetches_a_page_it_cannot_paint,
              test_the_loaded_history_belongs_to_one_stream_and_one_connection,
              test_a_failed_load_older_line_cannot_outlive_its_button,
              test_the_live_tail_shows_the_live_edge,
              test_a_run_stream_the_run_routes_cannot_answer_for_is_not_a_link,
              test_the_session_timeline_can_reach_every_record_it_admits_to,
              test_the_expensive_route_is_polled_on_its_own_slow_cadence,
              test_an_empty_task_panel_says_why_it_is_empty,
              test_every_live_region_is_written_only_when_it_changes,
              test_the_events_toolbar_is_not_the_heading,
              test_the_load_older_button_names_the_stream_it_walks,
              test_no_liveness_class_is_attached_outside_a_whitelist,
              test_a_run_that_starts_after_the_handshake_reaches_the_sidebar,
              test_the_agent_tree_is_drawn_as_containment,
              test_every_growing_collection_is_capped,
              test_the_log_meta_line_arithmetic_closes,
              test_a_region_is_rebuilt_only_when_it_changed,
              test_the_page_parses_as_javascript,
              test_the_page_behaves_when_it_is_actually_driven):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all touch-visual source guards passed")


if __name__ == "__main__":
    main()
