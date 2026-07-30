#!/usr/bin/env python3
"""Static-source regression guards for monitor.html (sp-frontend fixes).

monitor.html is HTML/JS and is not executed by python3, so these tests assert on
the source text: that the fixed pattern is present and the vulnerable/buggy
pattern is absent. Run: `python3 test_frontend.py` (exits non-zero on failure).

Covers FRONTEND-1..5 and PROTOCOL-6 (frontend half), plus the touch-monitor-perf
pass (M13 frontend half): the dirty-flag render coalescer, the banned forced
reflow, cached formatters, the capped+disclosed log, the v2 frame dispatch,
snapshot hydration and cursor resume, and the cross-file literals (FOLD_GEN,
TP_IDLE_MS, TP_STALL_MS) that bind this page's fold to monitor_server.py's.

Every assertion here is ADDITIVE: the pre-existing ones above are untouched.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# The module under test is named through `tests/_roots.py` (GD-U1/GD-U6): this
# file lives in `tests/monitoring/`, the module it asserts about does not.
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _roots import MON                                  # noqa: E402

HTML = os.path.join(str(MON), "monitor.html")
PY = os.path.join(str(MON), "monitor_server.py")
#: The memory editor is a SECOND document (G4), which is why its guards can be
#: a whole new section here instead of insertions between existing slices.
MEMORY_HTML = os.path.join(str(MON), "memory.html")
#: The repository's own index-budget rule (I9's gate). Read as TEXT and never
#: imported — importing it pulls in `test_publish_hygiene` and `aggregator.paths`
#: and runs their module bodies, and this file has no business doing that to
#: assert about two regexes. The page must measure the index the SAME way that
#: file does, so if its rule is re-spelled, the tuple expectations in the driven
#: harness below have to be re-derived — that is what the text pin catches.
HYGIENE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "test_memory_hygiene.py")


def _slice(src, start_marker, end_marker=None):
    """Return the substring of `src` starting at the line containing
    `start_marker` up to (but not including) the next line containing
    `end_marker` (or end-of-file). Used to scope assertions to one function."""
    i = src.find(start_marker)
    assert i != -1, f"marker not found: {start_marker!r}"
    if end_marker is None:
        return src[i:]
    j = src.find(end_marker, i + len(start_marker))
    assert j != -1, f"end marker not found after {start_marker!r}: {end_marker!r}"
    return src[i:j]


def main():
    with open(HTML, encoding="utf-8") as fh:
        src = fh.read()

    # ---- FRONTEND-1: renderFlow must not interpolate n.state into innerHTML ----
    flow = _slice(src, "function renderFlow", "setInterval")
    # No innerHTML assignment anywhere in renderFlow.
    assert "innerHTML" not in flow, \
        "FRONTEND-1: renderFlow must not assign innerHTML (use createElement/textContent)"
    # The vulnerable interpolation shape must be gone.
    assert "class=\"node ${" not in src and "class='node ${" not in src, \
        "FRONTEND-1: `class=\"node ${...}\"` interpolation must be removed"
    assert "${n.state}" not in src, "FRONTEND-1: n.state must not be interpolated into a string"
    # The safe construction must be present.
    assert "createElement(\"span\")" in flow, "FRONTEND-1: renderFlow should build nodes via createElement"
    assert "node.className" in flow and ".textContent" in flow, \
        "FRONTEND-1: renderFlow should set className + textContent"
    # A whitelist of node state classes that includes 'stale'.
    assert re.search(r"NODE_STATES\s*=\s*\[[^\]]*\"stale\"", src), \
        "FRONTEND-1/D9: NODE_STATES whitelist must exist and include 'stale'"
    for st in ("running", "done", "failed", "stale"):
        assert f'"{st}"' in _slice(src, "NODE_STATES", "function renderFlow"), \
            f"FRONTEND-1: NODE_STATES whitelist should include {st!r}"

    # ---- PROTOCOL-6 / D9: .chip.stale CSS rule present ----
    assert re.search(r"\.chip\.stale\s*\{", src), "PROTOCOL-6: .chip.stale CSS rule must be present"

    # ---- FRONTEND-2: plan-close updates the flow strip (roles -> stale + renderFlow) ----
    freeze = _slice(src, "function freezePlan", "function onEvent")
    assert "pl.roles.values()" in freeze, "FRONTEND-2: freezePlan must iterate the flow-strip role nodes"
    assert "renderFlow(pl)" in freeze, "FRONTEND-2: freezePlan must re-render the flow strip"
    assert 'node.state = "stale"' in freeze, "FRONTEND-2: freezePlan must stale still-running role nodes"

    # ---- FRONTEND-3: settle open plan cards on run-complete ----
    onev = _slice(src, "function onEvent", "// ---- routing ----")
    assert 'ev.stage === "complete"' in onev, "FRONTEND-3: complete stage must be handled in onEvent"
    # The sweep iterates all plans, skips orchestrator, and settles queued/running ones.
    sweep = _slice(onev, 'if (ev.stage === "complete")', "} else if (ev.stage === \"tokens\"")
    assert "for (const [id, pl] of plans)" in sweep, \
        "FRONTEND-3: complete handler must sweep every plan"
    assert 'id === "orchestrator"' in sweep, "FRONTEND-3: sweep must skip the orchestrator plan"
    assert '"queued"' in sweep and '"running"' in sweep, \
        "FRONTEND-3: sweep must settle still-open (queued/running) plan cards"
    assert "freezePlan(pl)" in sweep, "FRONTEND-3: swept plans must have their rows/flow frozen"

    # ---- FRONTEND-6: continuation activity reopens a settled orchestrator badge ----
    # One task folder hosts several phases appending to one events.jsonl, so a
    # replay can pass an earlier phase's run-level "complete done" and keep
    # going; fresh running activity on the orchestrator card must reopen it.
    f6 = _slice(onev, "FRONTEND-6", "let chip")
    assert 'ev.plan === "orchestrator"' in f6, \
        "FRONTEND-6: the reopen must be restricted to the reserved orchestrator card"
    assert 'ev.state === "running"' in f6, \
        "FRONTEND-6: only running-state activity reopens a settled badge"
    assert 'p.state === "done"' in f6 and 'p.state === "failed"' in f6, \
        "FRONTEND-6: the reopen must cover both terminal badge states"
    assert '"badge running"' in f6, "FRONTEND-6: the reopened badge must render as running"

    # ---- FRONTEND-4: detach onmessage/onopen on teardown (both paths) ----
    stopws = _slice(src, "function stopWs", "// ---- home grid ----")
    assert "ws.onmessage = null" in stopws, "FRONTEND-4: stopWs must null ws.onmessage"
    assert "ws.onopen = null" in stopws, "FRONTEND-4: stopWs must null ws.onopen"
    resync = _slice(src, "function forceResync", "function applyRate")
    assert "ws.onmessage = null" in resync, "FRONTEND-4: forceResync must null ws.onmessage"
    assert "ws.onopen = null" in resync, "FRONTEND-4: forceResync must null ws.onopen"

    # ---- FRONTEND-5: unknown-task dropdown gets an explicit state, not a blank ----
    assert "function selectTask" in src, "FRONTEND-5: a selectTask helper must exist"
    seltask = _slice(src, "function selectTask", "window.addEventListener(\"popstate\"")
    assert "unknown task" in seltask, "FRONTEND-5: selectTask must surface an explicit 'unknown task' option"
    assert "data-unknown" in seltask or "dataset.unknown" in seltask, \
        "FRONTEND-5: selectTask must mark the synthetic option so it can be reconciled"
    assert ".includes(name)" in seltask, "FRONTEND-5: selectTask must test membership before assigning value"
    # Both routing call sites must use the helper, not a raw sel.value assignment.
    assert "selectTask(name)" in src, "FRONTEND-5: route() must call selectTask(name)"
    assert "selectTask(currentTask)" in src, "FRONTEND-5: refreshTasks must call selectTask(currentTask)"
    assert "if (sel.dataset.names) sel.value = name;" not in src, \
        "FRONTEND-5: the blank-inducing `sel.value = name` in route() must be replaced"

    # ---- ARTIFACTS-1: strip fetches /artifacts and links files via /file ----
    assert "/artifacts?task=" in src, "ARTIFACTS-1: dashboard must fetch /artifacts for the current task"
    assert "/file?task=" in src, "ARTIFACTS-1: artifact links must go through the /file endpoint"
    assert "encodeURIComponent(rel)" in src, "ARTIFACTS-1: artifact paths must be URI-encoded in links"
    arts = _slice(src, "function renderArtifacts", "// ---- minimal escape-first markdown preview ----")
    assert "innerHTML" not in arts, \
        "ARTIFACTS-1: renderArtifacts must not assign innerHTML (names are agent-written)"
    assert ".textContent" in arts and "createElement" in arts, \
        "ARTIFACTS-1: artifact chips must be built via createElement/textContent"
    assert 'rel = "noopener"' in arts, "ARTIFACTS-1: report links must carry rel=noopener"

    # ---- ARTIFACTS-2: markdown preview escapes BEFORE inline transforms ----
    inline = _slice(src, "function mdInline", "function renderMd")
    assert "https?:" in inline, "ARTIFACTS-2: link hrefs must be protocol-whitelisted"
    rmd = _slice(src, "function renderMd", "const mdOverlay")
    assert "esc(" in rmd, "ARTIFACTS-2: renderMd must escape source text"
    assert "mdInline(esc(" in rmd, "ARTIFACTS-2: inline transforms must run on already-escaped text"
    assert "innerHTML" not in rmd, "ARTIFACTS-2: renderMd itself returns a string, no direct DOM writes"

    # ---- ARTIFACTS-4: row placed after the orchestrator card, before the loop cards ----
    place = _slice(src, "function placeArtifacts", "function renderArtifacts")
    assert "insertBefore(artifactsEl, orch.nextSibling)" in place, \
        "ARTIFACTS-4: placeArtifacts must insert the row right after the orchestrator card"
    renderfn = _slice(src, "function render()", "function fmtTok")
    assert "placeArtifacts()" in renderfn, \
        "ARTIFACTS-4: render() must re-place the artifacts row after re-appending cards"
    assert "placeArtifacts()" in _slice(src, "function renderArtifacts",
                                        "// ---- minimal escape-first markdown preview ----"), \
        "ARTIFACTS-4: renderArtifacts must re-attach the row (cards grid gets cleared on reconnect)"

    # ---- ARTIFACTS-3: navigation resets the strip and closes the preview ----
    routefn = _slice(src, "function route()", "function selectTask")
    assert "clearArtifacts()" in routefn, "ARTIFACTS-3: route() must clear the artifacts strip"
    assert "closeMd()" in routefn, "ARTIFACTS-3: route() must close a stale markdown preview"
    refr = _slice(src, "async function refreshArtifacts", "function renderArtifacts")
    assert "forTask !== currentTask" in refr, \
        "ARTIFACTS-3: a stale /artifacts response must not render after task switch"

    # ---- ARTIFACTS-5: per-loop "files" pill opens the popup filtered to that loop ----
    assert 'base.startsWith(id + "-") || base.startsWith(id + ".")' in src, \
        "ARTIFACTS-5: loop files must be matched by basename prefix (findings naming convention)"
    assert "sort((a, b) => b.length - a.length)" in src, \
        "ARTIFACTS-5: the longest plan id must win attribution (sp-agents vs sp-agents-reducer)"
    btns = _slice(src, "function updateFileBtns", "function clearArtifacts")
    assert "innerHTML" not in btns and "createElement" in btns, \
        "ARTIFACTS-5: the files pill must be built via createElement/textContent"
    assert "openArt(id)" in btns, \
        "ARTIFACTS-5: the pill must open the shared popup filtered to its loop"
    assert "artFilter ? (planArts.get(artFilter) || []) : lastArts" in src, \
        "ARTIFACTS-5: renderArtPopup must honour the plan filter"
    assert "artFilter = null" in _slice(src, "function closeArt", "document.getElementById"), \
        "ARTIFACTS-5: closing the popup must drop the plan filter"
    assert "updateFileBtns()" in _slice(src, "function render()", "function renderTotals"), \
        "ARTIFACTS-5: render() must refresh pills for cards created after the /artifacts fetch"

    # ---- SUBPLANS-1: orchestrator accordion lists every sub-plan loop ----
    subfn = _slice(src, "function renderSubplans", "function fmtTok")
    assert "innerHTML" not in subfn and ".textContent" in subfn, \
        "SUBPLANS-1: the sub-plans list must be built via createElement/textContent"
    assert "scrollIntoView" in subfn, \
        "SUBPLANS-1: clicking a sub-plan bullet must jump to that loop's card"
    assert 'if (id === "orchestrator") continue;' in subfn, \
        "SUBPLANS-1: the orchestrator itself is not a sub-plan"
    assert ".card.logfold .subplans { display:none; }" in src, \
        "SUBPLANS-1: the list must fold with the log behind the accordion arrow"
    assert "renderSubplans()" in _slice(src, "function renderTotals", "function renderSubplans"), \
        "SUBPLANS-1: the list refreshes on the renderTotals cadence, never per event"

    # ---- TIMEPLAN-1: session timeplan strip — whitelisted classes, no event text ----
    assert 'id="timeplan"' in src, "TIMEPLAN-1: the session timeplan container must exist"
    tpjs = _slice(src, "// ---- session timeplan ----", "// ---- routing ----")
    assert "innerHTML" not in tpjs, \
        "TIMEPLAN-1: timeplan code must not assign innerHTML (events are untrusted input)"
    assert re.search(r"TP_KINDS\s*=\s*\{", tpjs), "TIMEPLAN-1: TP_KINDS whitelist must exist"
    for kind in ("up", "idle", "down"):
        assert kind + ":" in tpjs, f"TIMEPLAN-1: TP_KINDS whitelist must include {kind!r}"
    assert "i.className = s.kind" in tpjs, \
        "TIMEPLAN-1: segment class must come from the whitelisted kind, never event text"
    # Every event (quiet token ticks included) feeds the timeplan: the tpNote
    # call sits before onEvent's quiet-tick early return.
    onev_head = _slice(src, "function onEvent", "// ---- artifacts")
    assert "tpNote(ev)" in onev_head, "TIMEPLAN-1: onEvent must feed every event to the timeplan"
    assert onev_head.find("tpNote(ev)") < onev_head.find("ev.quiet"), \
        "TIMEPLAN-1: tpNote must run before the quiet-tick early return"
    # Both teardown paths reset the accumulated ticks (task switch / reconnect
    # replay), mirroring the plans.clear() double-count protection.
    assert "tpReset()" in _slice(src, "function stopWs", "// ---- home grid ----"), \
        "TIMEPLAN-1: stopWs must reset the timeplan"
    assert "tpReset()" in _slice(src, "ws.onopen = () =>", "ws.onmessage"), \
        "TIMEPLAN-1: the connect-replay rebuild must reset the timeplan"

    # ---- TIMEPLAN-2: time dials — static SVG, attribute updates, literal states ----
    for did in ("dialElapsed", "dialSilence", "dialWork"):
        assert f'id="{did}"' in src, f"TIMEPLAN-2: {did} dial must exist"
    assert 'pathLength="100"' in src, \
        "TIMEPLAN-2: gauge arcs must normalize pathLength so dasharray is a 0..100 fraction"
    assert "tpDialsRender" in _slice(tpjs, "function tpRender", "tpEl.classList.add"), \
        "TIMEPLAN-2: tpRender must drive the dials"
    dials = _slice(tpjs, "function tpDialsRender", "function tpReset")
    assert "innerHTML" not in dials, "TIMEPLAN-2: dial updates must be attribute/textContent only"
    assert re.search(r'silenceMs > TP_STALL_MS \? "stall" : silenceMs > TP_SLOW_MS \? "slow" : "ok"',
                     dials), "TIMEPLAN-2: silence state must derive from the fixed thresholds"
    assert '"dial"' in dials and "cls += " in dials, \
        "TIMEPLAN-2: silence classes must be assembled from fixed literals, never event text"
    assert dials.count("aria-label") >= 3, "TIMEPLAN-2: every dial must refresh its aria-label"
    # state is never color-alone: the silence value text carries the state word
    assert '" · " + state' in dials, \
        "TIMEPLAN-2: the silence value must spell the state word next to the color"
    assert "tpDialEls" in _slice(tpjs, "function tpReset", "function tpNote"), \
        "TIMEPLAN-2: tpReset must also rest the dials"

    # ---- STATS-1: statistics view — routed by link, literal tiles, no innerHTML ----
    assert 'id="statsView"' in src, "STATS-1: the statistics main element must exist"
    assert 'id="statsLink"' in src, "STATS-1: the statistics link must exist"
    assert 'id="statsLink"' in _slice(src, 'id="timeplan"', 'id="cards"'), \
        "STATS-1: the statistics link lives in the timeplan card, not the header"
    assert re.search(r"body\.stats main#statsView\s*\{\s*display:grid", src), \
        "STATS-1: the stats grid must only show in the stats body state"
    stats_js = _slice(src, "// ---- statistics view ----", "// ---- routing ----")
    assert "innerHTML" not in stats_js, \
        "STATS-1: stat tiles must be built via createElement/textContent only"
    assert "STAT_TILES" in stats_js and "createElement" in stats_js and ".textContent" in stats_js, \
        "STATS-1: tiles must come from the STAT_TILES literals"
    routefn2 = _slice(src, "function route()", "function selectTask")
    assert "statsMode" in routefn2 and 'view === "stats"' in src, \
        "STATS-1: route() must derive stats mode from the view query param"
    assert "statsLink" in routefn2, "STATS-1: route() must flip the stats link direction"
    assert '"&view=stats"' in src, "STATS-1: navigate must carry the view param in the URL"
    assert "statsReset()" in _slice(src, "function stopWs", "// ---- home grid ----"), \
        "STATS-1: stopWs must reset the stats accumulators"
    assert "statsReset()" in _slice(src, "ws.onopen = () =>", "ws.onmessage"), \
        "STATS-1: the connect-replay rebuild must reset the stats accumulators"

    # ---- STATS-2: entire-flow status tile — literal states, activity wins ----
    assert '{ id: "flow"' in stats_js, "STATS-2: STAT_TILES must include the flow tile"
    assert re.search(r"\.stat \.sl \.dot\.running\s*\{", src), \
        "STATS-2: a .stat dot.running CSS rule must exist for the live flow dot"
    assert re.search(r"\.stat \.sl \.dot\.queued\s*\{", src), \
        "STATS-2: a .stat dot.queued CSS rule must exist for the reset flow dot"
    srender = _slice(stats_js, "function statsRender")
    assert "runningPlans > 0 || runningAgents > 0" in srender, \
        "STATS-2: live activity (running plans/agents) must win over a stale badge"
    assert 'orch.state === "done"' in srender and 'orch.state === "failed"' in srender, \
        "STATS-2: flow state must derive from === comparisons against literals"
    # idle verdicts without a run-level close: a failed plan with nothing
    # running IS the flow verdict (a resumed flow re-enters via `active`
    # first, so a pre-resume failure never overrides running loops), and
    # all-green folds to done.
    assert 'else if (failed > 0) flow = "failed"' in srender, \
        "STATS-2: idle + a failed plan (awaiting user after last try) must read failed"
    assert "done === plansAll" in srender, \
        "STATS-2: idle + all DECLARED plans green must fold to done (an idle run " \
        "whose declared plans never started is not finished)"
    # STATS-3: the progress denominator covers the run's declared plan-card
    # total (plans_total events), floored by cards actually seen; the running
    # sub-line numerator counts SETTLED plans (green + red), not green alone.
    assert "Math.max(plansSeen, planTotal)" in srender, \
        "STATS-3: the denominator must be max(cards seen, declared plans_total)"
    assert "(done + failed) + \"/\" + plansAll" in srender, \
        "STATS-3: the running sub-line must count settled (done+failed) over all plans"
    assert "ev.plans_total" in src, \
        "STATS-3: onEvent must fold the declared plans_total"
    fold = _slice(src, "ev.plans_total", "upsertAgent")
    assert "Math.floor(Number(" in fold and "Number.isFinite(" in fold, \
        "STATS-3: plans_total is untrusted event data — coerce to a bounded integer"
    assert "n > planTotal" in fold, \
        "STATS-3: plans_total folds monotonically — a smaller declaration never shrinks it"
    assert "planTotal = 0" in _slice(src, "function statsReset"), \
        "STATS-3: statsReset must clear the declared total"
    assert srender.find("active) flow = \"running\"") < srender.find("failed > 0"), \
        "STATS-2: the running-activity branch must be checked before the failed verdict"
    assert '"badge " + ev.state' not in srender and "orch.state" not in \
        _slice(srender, 'setStat("flow"', "setStat(\"tokens\""), \
        "STATS-2: raw orchestrator state (untrusted event text) must never reach the tile"
    assert re.search(r'setStat\("flow"', srender), "STATS-2: statsRender must update the flow tile"
    # the reset path restores every dot to its declared literal
    assert 'setStat(t.id, "—", "", t.dot)' in stats_js, \
        "STATS-2: statsReset must restore tile dots to their STAT_TILES literals"

    # ---- ZOOM-1: task-view zoom — CSS zoom (reflow), fixed options, 100% default ----
    header = _slice(src, "<header>", "</header>")
    assert 'id="zoomSel"' in header, "ZOOM-1: the zoom selector must live in the header"
    assert re.search(r"body\.task,\s*body\.stats\s*\{\s*zoom:\s*var\(--zoom", src), \
        "ZOOM-1: zoom must apply via CSS zoom + --zoom var to task/stats bodies (home never zooms)"
    assert "transform" not in _slice(src, "body.task, body.stats { zoom:", "\n"), \
        "ZOOM-1: scaling must use CSS zoom (layout reflow), not transform:scale"
    zoomsel = _slice(src, 'id="zoomSel"', "</label>")
    assert '<option value="1">100%</option>' in zoomsel, \
        "ZOOM-1: the default option must be value=1 rendered as 100%"
    zoomjs = _slice(src, "const zoomSel", "refreshTasks();")
    assert 'localStorage.getItem("orchZoom") || "1"' in zoomjs, \
        "ZOOM-1: the stored zoom must load with 100% as the default"
    assert "zoomSel.selectedIndex < 0" in zoomjs, \
        "ZOOM-1: a stored value with no matching option must fall back to the default"
    assert 'localStorage.setItem("orchZoom"' in zoomjs, "ZOOM-1: the zoom choice must persist"
    assert re.search(r"body\.home [^{]*#zoom\b", src), \
        "ZOOM-1: the home view must hide the zoom control"

    perf_guards(src)
    roster_guards(src)
    editico_guards(src)
    memory_guards(src)
    memory_driven()

    print("test_frontend.py: all assertions passed")
    return 0


def perf_guards(src):
    """M13 (frontend half) — the touch-monitor-perf source-text guards.

    The page's cost model is the thing under test: work that used to happen once
    per event now happens once per BURST, no path forces a synchronous layout,
    every growing collection is capped and every cap is disclosed, and the v2
    protocol is dispatched by frame shape. None of it is observable to python3,
    so each rule is pinned as "the fixed pattern is present, the pathological one
    is absent" — the same discipline the assertions above use.
    """
    # ---- RENDER-1: render() is gated by dirty flags, and the card loop is
    # behind the flag (not merely near it) ----
    renderfn = _slice(src, "function render()", "function fmtTok")
    assert "if (cardsDirty) {" in renderfn, \
        "RENDER-1: render() must gate the card re-append on cardsDirty"
    assert renderfn.find("if (cardsDirty)") < renderfn.find("for (const id of plans.keys())"), \
        "RENDER-1: the card-ordering loop must sit INSIDE the cardsDirty branch"
    assert "cardsDirty = false;" in renderfn, "RENDER-1: render() must clear the flag it consumed"
    assert "summaryDirty = true" in renderfn, \
        "RENDER-1: the totals line must be deferred (summaryDirty) during a burst"
    assert "cardsDirty = true;" in _slice(src, "function planEl", "function render()"), \
        "RENDER-1/-8: planEl must MARK the card set dirty, never render itself"
    # A burst is one paint: a v2 batch and a replay are both bounded by
    # `batching`/`replaying`, so onEvent's render() can never paint per event.
    assert "let batching = false;" in src, \
        "RENDER-1: a v2 array frame (up to MAX_TICK_EVENTS events) needs its own burst flag"
    assert "replaying || batching" in renderfn, \
        "RENDER-1: totals must be deferred inside a batch as well as a replay"

    # ---- RENDER-1: the coalescer itself — one pass over the dirty flags ----
    flush = _slice(src, "function flushDirty", "// The timeplan strip is deliberately NOT")
    for flag in ("p.flowDirty", "p.spanDirty", "p.logBuf.length"):
        assert flag in flush, f"RENDER-1: flushDirty must consume {flag}"
    assert "renderFlow(p)" in flush and "updateSpan(p)" in flush and "flushLog(p)" in flush, \
        "RENDER-1: flushDirty is where the deferred DOM work actually happens"
    # The three flush points: batch/control-frame end, replay end, the 1 s tick.
    assert "flushDirty();" in _slice(src, "function endReplay", "function setTok"), \
        "RENDER-1: replay end must flush once"
    assert "if (!replaying) flushDirty();" in src, \
        "RENDER-1: the standing 1 s interval must flush when no burst is in flight"

    # ---- RENDER-2/RENDER-7: the timeplan strip is coalesced on its OWN cadence,
    # never inside the per-frame flush ----
    # tpRender writes tpTrack.style.width and then READS clientWidth to re-fit the
    # view: a forced synchronous layout. flushDirty runs at the end of EVERY array
    # frame, and the server splits one MAX_TICK_EVENTS tick into up to ten of them
    # (batch caps), so calling tpRender from there means ten forced layouts inside
    # one 0.5 s tick — the very shape this pass removes elsewhere.
    assert "tpRender()" not in flush, \
        "RENDER-2: flushDirty runs per array frame — the strip's forced-layout rebuild must not"
    tpflush = _slice(src, "function tpFlush", "let replaying")
    assert "tpDirty || tpLive" in tpflush and "tpRender()" in tpflush, \
        "RENDER-7: tpFlush is the strip's single coalesced render, gated on dirty/live"
    assert "tpFlush();" in _slice(src, "function endReplay", "function setTok"), \
        "RENDER-7: a burst end paints the strip exactly once"
    assert "if (!replaying) tpFlush();" in src, \
        "RENDER-7: the strip's standing cadence is the 1 s interval, not the frame boundary"

    # ---- RENDER-2 (blocker): no forced synchronous layout, anywhere ----
    # The pulse was `classList.remove` / read offsetWidth for its side effect /
    # `classList.add` — 137 s of layout on one replay. The idiom is banned
    # file-wide, comments included, so this guard is a plain absence check.
    # The guard is on the PATTERN, not on the token `void `: a bare substring
    # test also matches the English word "avoid " and would fail a future
    # comment with a message about an idiom that is not there.
    assert not re.search(r"void\s+[\w.$\[\]]*offsetWidth", src), \
        "RENDER-2: the `void <el>.offsetWidth` forced-reflow idiom must not appear in monitor.html"
    pulse = _slice(src, "function pulseTok", "function noteReplay")
    assert "el.animate(" in pulse, "RENDER-2: the counter pulse must use WAAPI el.animate()"
    assert "offsetWidth" not in pulse, "RENDER-2: the pulse must not read layout"
    assert "REDUCED_MOTION" in pulse, \
        "RENDER-2: WAAPI bypasses the CSS media query — reduced motion is a JS decision"

    # ---- RENDER-3: exactly two module-level Intl.DateTimeFormat instances,
    # and no formatter construction on a per-event path ----
    assert src.count("new Intl.DateTimeFormat(") == 2, \
        "RENDER-3: the page needs exactly two cached formatters (clock, clock+seconds)"
    assert "\nconst FMT_CLOCK = new Intl.DateTimeFormat(" in src, \
        "RENDER-3: FMT_CLOCK must be a module-level constant, not a per-call construction"
    assert "\nconst FMT_CLOCK_S = new Intl.DateTimeFormat(" in src, \
        "RENDER-3: FMT_CLOCK_S must be a module-level constant"
    onev_full = _slice(src, "function onEvent", "// ---- per-plan log")
    span = _slice(src, "function updateSpan", "setInterval")
    for name, chunk in (("onEvent", onev_full), ("updateSpan", span)):
        assert "toLocaleTimeString(" not in chunk, \
            f"RENDER-3: {name} must format through the cached Intl instances"
        assert "new Intl.DateTimeFormat(" not in chunk, \
            f"RENDER-3: {name} must not construct a formatter"
    assert "ev.tsMs = Date.parse(" in onev_full, \
        "RENDER-3: ev.ts must be parsed ONCE per event and the number passed down"

    # ---- RENDER-5: the log is buffered, flushed as one fragment, capped, and
    # the cap is disclosed with a route back to the full history ----
    assert "const LOG_CAP = 400;" in src, "RENDER-5: the per-plan log cap must be a named constant"
    push = _slice(src, "function pushLog", "function flushLog")
    assert "p.logBuf.push(rec)" in push, "RENDER-5: a log line is buffered, not inserted per event"
    assert "LOG_CAP" in push, "RENDER-5: the BUFFER is capped too, not only the DOM list"
    assert "if (statsMode) return;" in push, \
        "RENDER-5: the stats view hides #cards — building invisible rows is waste"
    flushlog = _slice(src, "function flushLog", "function trimLog")
    assert "createDocumentFragment()" in flushlog, "RENDER-5: a burst is built into ONE fragment"
    assert flushlog.count("insertBefore") == 1, \
        "RENDER-5: a burst costs exactly one insertBefore per plan"
    trim = _slice(src, "function trimLog", "// ---- artifacts")
    # The disclosure row's class must not live in the same namespace as the
    # stream-derived ones (logLi does `li.className = rec.state`), or an event
    # whose state read "trimmed" would render as a disclosure row.
    assert 'li.className = "logtrim"' in trim, \
        "RENDER-5: the trimmed-lines row needs a PREFIXED class, disjoint from the event states"
    assert re.search(r"\.log li\.logtrim \{", src), \
        "RENDER-5: the disclosure row's CSS must match the prefixed class"
    assert "childElementCount > LOG_CAP" in trim, "RENDER-5: the DOM list is trimmed to the cap"
    assert "p.logTrimmed" in trim, "RENDER-5: what the cap dropped is counted, never silently lost"
    assert "requestFullReplay(" in trim, \
        "RENDER-5: the disclosure row's action must reconnect for the full history (?snap=0)"
    logli = _slice(src, "function logLi", "function pushLog")
    assert "LOG_LI.cloneNode(true)" in logli, \
        "RENDER-5: a log row is cloned from a module-level template, not parsed per line"
    # the assignment form is what matters — logLi's own comment names the banned one
    assert "innerHTML =" not in flushlog + trim + logli, \
        "RENDER-5/FRONTEND-1: log rows are cloned template + textContent, never innerHTML"
    assert re.search(r"\.log li \{[^}]*content-visibility:auto", src), \
        "RENDER-5: .log li needs content-visibility:auto so off-screen rows skip layout"
    assert re.search(r"\.log li \{[^}]*contain-intrinsic-size:", src), \
        "RENDER-5: content-visibility needs an intrinsic size or the scrollbar jumps"

    # ---- RENDER-7: the timeplan folds incrementally through a BOUNDED
    # reorder window (a growing collection, therefore capped) ----
    tpins = _slice(src, "function tpInsert", "// Seed the fold")
    assert "TP_TAIL_MAX" in tpins and "TP_TAIL_MS" in tpins, \
        "RENDER-7: the reorder window must be bounded by both count and time"
    assert "tpStep(tpFold," in tpins, \
        "RENDER-7: aged-out ticks commit to the incremental fold, never a re-scan"

    # ---- RENDER-9: the duration ticker only ever runs on a LIVE row ----
    tick = _slice(src, "const LIVE_TICK_MS", "function freezePlan")
    assert "if (replaying) return;" in tick, "RENDER-9: the ticker must not run during a replay"
    assert "LIVE_TICK_MS" in tick, \
        "RENDER-9: a plan silent longer than the live window must not get wall-clock-now durations"

    # ---- WS-2/M8: the socket dispatches by frame SHAPE; control frames never
    # reach onEvent ----
    onmsg = _slice(src, "ws.onmessage = (m) =>", "ws.onclose")
    assert "dispatchFrame(" in onmsg, "M8: onmessage must hand every frame to the shape dispatcher"
    disp = _slice(src, "function dispatchFrame", "function noteFirstEventFrame")
    assert "Array.isArray(v)" in disp, "M8: an ARRAY frame is a batch of events"
    assert "v.m !== undefined" in disp, \
        "M8: the reserved `m` key marks a control frame — it is what keeps one out of onEvent"
    assert 'v.m === "hello"' in disp and "v2Mode = true" in disp, \
        "M8: v2 is SERVER-DECLARED — only a hello as the FIRST frame switches the client"
    assert "onEvent(" not in disp, \
        "M8: dispatchFrame must never call onEvent directly (control frames would mint junk cards)"
    assert "batching = true;" in disp, "M8: a batch is one burst, flushed at the frame's end"
    ctl = _slice(src, "function onControl", "function onBoundary")
    for m in ('"snapshot"', '"tail"', '"cursor"'):
        assert m in ctl, f"M8: the control dispatcher must handle {m}"

    # ---- M4/WS-2: the boundary frame is the authoritative replay end; the
    # timer survives only as a v1 backstop ----
    note = _slice(src, "function noteReplay", "function endReplay")
    assert "if (v2Mode) return;" in note, \
        "M4: a v2 socket must arm no settle timer — the boundary frame is authoritative"
    assert "setTimeout(endReplay, 0)" in note, "M4: the v1 settle is a self-tuning task-queue drain"
    # Note what "backstop" can still mean here: once ANY frame has landed the
    # 0 ms drain always beats the 600 ms timer, so the fixed timer can only ever
    # fire BEFORE the first frame (an empty stream that never sends one).
    assert "setTimeout(endReplay, 600)" in note, \
        "M4: the fixed 600 ms timer stays only as the pre-first-frame backstop"
    assert "if (!firstFrame) replaySettle" in note, \
        "M4: the 0 ms drain must not be armed before the first frame — it would beat the " \
        "socket's first message and end the replay before it began"
    assert "noteReplay();" in _slice(src, "function dispatchFrame", "function noteFirstEventFrame"), \
        "M4: a confirmed hello must disarm the v1 heuristics on the spot"
    bound = _slice(src, "function onBoundary", "// Operator fallback")
    assert "endReplay()" in bound, "M4: the boundary frame ends the replay"
    assert "noteCursor(b.cursor)" in bound, "M11: the boundary publishes the server's cursor"
    assert "b.truncated" in bound, \
        "GD-F: a server-side log-budget trim must be disclosed, never silent"

    # ---- M10: snapshot hydration — validated, seeded before any card, and
    # built with createElement/textContent only ----
    snapfn = _slice(src, "function applySnapshot", "function hydratePlan")
    assert "innerHTML" not in snapfn, \
        "M10: applySnapshot must not assign innerHTML (snapshot text is agent-written)"
    assert 'snap.kind !== "monitor-snapshot"' in snapfn, "M10: the snapshot kind must be validated"
    assert "snap.foldGen !== FOLD_GEN" in snapfn and "requestFullReplay(" in snapfn, \
        "M10: a foldGen mismatch must DISCARD the payload and ask for ?snap=0 (fail safe)"
    # The ONE named re-anchor (M10): the tail-path assert that tpNote runs
    # before the quiet return survives verbatim above; this is its companion on
    # the snapshot path — the strip is fold state and is seeded BEFORE any card.
    assert snapfn.find("tpSeed(") != -1 and snapfn.find("tpSeed(") < snapfn.find("planEl("), \
        "M10: applySnapshot must call tpSeed(...) before the first plan card is built"
    hyd = _slice(src, "function hydratePlan", "// ---- ?snap=verify")
    assert "innerHTML" not in hyd, "M10: hydratePlan must build rows via createElement/textContent"
    assert "createDocumentFragment()" in hyd, "M10: a hydrated log lands in one fragment"
    # GD-C: snapshot totals are ABSOLUTE as of the cursor; the tail adds deltas.
    assert "p.tokIn = snapNum(tk.in)" in hyd, \
        "GD-C: hydration ASSIGNS the snapshot's absolute totals"
    assert "p.tokIn += ev.tokens.in" in onev_full, "GD-C: tail events ADD their delta"
    # ...and it must read them WITHOUT ToInt32 truncation. `x | 0` wraps at 2^31,
    # which this repo's own streams (~94k tokens/event) cross inside a single
    # plan well before the 100k-event target — the default v2 path would then
    # disagree with a ?snap=0 replay of the same bytes.
    for field in ("tk.in", "tk.out", "tk.cached", "tk.write"):
        assert not re.search(re.escape(field) + r"\s*\|\s*0", hyd), \
            f"GD-C/MAJOR-1: {field} must not be truncated through ToInt32 (`| 0`) — " \
            "token totals exceed 2^31 at this plan's scale"
    snapnum = _slice(src, "function snapNum", "function hydratePlan")
    assert 'typeof v === "number" && isFinite(v)' in snapnum, \
        "GD-C: the snapshot numeric coercion must accept any finite number and reject the rest"
    assert "Math.floor(v)" in snapnum, \
        "GD-C: the server only emits non-negative integers — a negative or fractional total is a " \
        "corrupt payload and must read 0, not render as if it were real"
    assert "p.logTrimmed = Math.max(0, p.logTotal - log.length)" in hyd, \
        "GD-F: the server's log budget cut must be disclosed on the hydrated card"
    # A task switch between a snapshot landing and its application must never
    # hydrate the previous task's fold onto the new page. The LOAD-BEARING
    # guarantee is the ordering below — stopWs detaches onmessage BEFORE close(),
    # so the old socket's frames are never parsed at all. Assert the ordering,
    # not just the presence: `ws.close()` first would leave a window in which a
    # queued frame still hydrates the previous task's fold.
    stop = _slice(src, "function stopWs", "// ---- home grid ----")
    assert stop.find("ws.onmessage = null") != -1 and \
        stop.find("ws.onmessage = null") < stop.find("ws.close()"), \
        "M10/FRONTEND-4: stopWs must detach ws.onmessage BEFORE closing the socket"
    assert "cancelSnapshot()" in stop, \
        "M10: stopWs must also drop any snapshot tagged for the task being left"
    pend = _slice(src, "function applyPendingSnapshot", "// Hydration, not replay")
    assert "pend.task !== currentTask" in pend, \
        "M10: a pending snapshot must be re-checked against the CURRENT task before it applies"
    # stopWs clears per-socket/per-stream state but deliberately keeps the
    # server-scoped ?snap=0 pin (one server serves all tasks).
    assert "wsCursor = null" in stop, "M10: stopWs must clear the stream cursor"
    assert not re.search(r"\bfoldMismatch\s*=", stop), \
        "M10: the foldMismatch pin is a SERVER property (one server, all tasks) — " \
        "a task switch must not clear it"

    # ---- GD-F: the orchestrator continuation reopen, in parity with the
    # server's fold; sub-plan CARDS keep their stage-\"plan\" restriction ----
    assert "reopenOrch()" in onev_full, \
        "GD-F: a sub-plan going running/queued must reopen a settled orchestrator badge"
    ro = _slice(src, "function reopenOrch", "function onEvent")
    assert 'o.state !== "done" && o.state !== "failed"' in ro, \
        "GD-F/R-58: the reopen only ever touches an ALREADY-SETTLED badge — nothing is synthesized"

    # ---- M11: cursor resume, and the old-server double-count defence ----
    conn = _slice(src, "function connect(resume)", "function dispatchFrame")
    assert '"&v=2"' in conn, "M11: the page always ASKS for v2 (the server decides)"
    assert '"&from=" +' in conn and '"&sig=" +' in conn, \
        "M11: a resume is (sig, byte offset) — never a line number"
    # MAJOR-3: a rebuild invalidates the stored position. Every connect that is
    # NOT a resume re-hydrates from the server's current offset, so a surviving
    # cursor would let the resync interval resume from a stale offset — double
    # counting the overlap, or dropping everything below it onto an empty view.
    assert "if (!resumeFrom) wsCursor = null;" in conn, \
        "M11: connect() must clear the stored cursor on any non-resume rebuild"
    assert conn.find("if (!resumeFrom) wsCursor = null;") < conn.find("ws = new WebSocket("), \
        "M11: the cursor must be invalidated BEFORE the socket is opened"
    # MINOR-4 companion: the ws.onopen rebuild (tpReset/statsReset, guarded
    # verbatim above by TIMEPLAN-1) is CONDITIONAL — it runs only when this is
    # not a cursor resume, which is the other half of the same invariant.
    onopen = _slice(conn, "ws.onopen = () =>", "ws.onmessage")
    assert "if (!resumeFrom) {" in onopen, \
        "M11: the connect-replay rebuild (plans.clear/tpReset/statsReset) must be skipped ONLY on a resume"
    assert onopen.find("if (!resumeFrom) {") < onopen.find("tpReset()"), \
        "M11/TIMEPLAN-1: tpReset()/statsReset() must sit inside the !resumeFrom rebuild branch"
    # MINOR-1: a foldGen mismatch pins ?snap=0 and must beat ?snap=verify —
    # verify asks for a snapshot, so re-asking for the one just discarded is a
    # tight reconnect loop against a version-skewed server.
    assert "const snap = pinFull ? \"0\" : (wantVerify ? \"verify\" : null);" in conn, \
        "M11: the ?snap=0 pin must win over ?snap=verify (otherwise: infinite reconnect loop)"
    # ...and ?snap=verify must never RESUME: an accepted cursor is answered with a
    # boundary and no snapshot, so applySnapshot never runs, the shadow fold is
    # never armed and the pill keeps a verdict several resyncs old. The one
    # escape hatch that exists to make drift observable must not go silently inert.
    assert "resumeFrom = !wantVerify && resume" in conn, \
        "DATA-MODEL-9: under ?snap=verify every connect is a rebuild — a resume has nothing to verify"

    # ---- ONE socket, ONE pending reconnect (attempt-2 MAJOR) ----
    # `ws` is a single global, so a second deferred scheduler could overwrite it
    # at `ws = new WebSocket(...)` while the first socket stayed OPEN with a live
    # onmessage feeding the same plans map: every token total roughly doubled,
    # every log line twice, and the stranded socket's own onclose scheduling yet
    # another connect. Two clicks on the always-visible "load full history" row
    # reached it. Both halves are pinned: one timer handle, and a connect that
    # never leaves a live socket behind.
    assert len(re.findall(r"^\s*(?:const \w+ = )?ws = new WebSocket\(", src, re.M)) == 1, \
        "M8/M11: exactly one site may open a socket — connect()"
    # An error on a SUPERSEDED socket must not close the current one: the
    # handlers close the socket they were installed on, never the global.
    assert "const sock = ws = new WebSocket(" in conn and "sock.close()" in conn, \
        "M11: ws.onerror must close ITS OWN socket, not whatever `ws` points at by then"
    assert "ws.onerror = null" in _slice(src, "function stopWs", "// ---- home grid ----"), \
        "FRONTEND-4: a teardown must detach onerror too, or a dying old socket closes the new one"
    assert not re.search(r"setTimeout\(\(\) => \{ if \(currentTask\) connect\(\); \}", src), \
        "M11: no ad-hoc reconnect timer — every deferred connect goes through armReconnect()"
    arm = _slice(src, "function armReconnect", "function connect(resume)")
    assert "clearTimeout(reconnectTimer);" in arm, \
        "M11: arming a reconnect must cancel the pending one (ONE handle, never two live sockets)"
    assert "reconnectTimer = null;" in arm and "if (currentTask) connect();" in arm, \
        "M11: the single reconnect handle clears itself and only reconnects a routed task"
    assert conn.find("cancelReconnect();") != -1 and \
        conn.find("cancelReconnect();") < conn.find("ws = new WebSocket("), \
        "M11: a connect IS the pending reconnect — it must cancel the timer before opening"
    assert conn.find("ws.close()") < conn.find("ws = new WebSocket("), \
        "M11: connect() must detach+close the socket it replaces BEFORE overwriting `ws`, " \
        "or the old one keeps feeding this page's fold"
    assert conn.find("ws.onmessage = null") < conn.find("ws.close()"), \
        "FRONTEND-4: connect()'s drop must detach onmessage before close(), like stopWs's"
    assert "cancelReconnect();" in _slice(src, "function stopWs", "// ---- home grid ----"), \
        "M11: a teardown must cancel a pending reconnect, or it builds a socket for the task just left"
    full = _slice(src, "function requestFullReplay", "// ---- v2 snapshot hydration")
    assert "armReconnect(retry)" in full, \
        "M11: requestFullReplay must reconnect through the single handle and the same backoff"
    assert not re.search(r"^\s*connect\(\);", full, re.M), \
        "M11: requestFullReplay must not call connect() synchronously — against a server whose " \
        "foldGen the page rejects that is an unthrottled reconnect loop"
    assert "retry = Math.min(retry * 2" in full, \
        "M11: the full-replay reconnect must grow its backoff, or a rejecting server is hammered"
    assert "wsCursor = null" in full, \
        "M11: a full-replay request must drop the cursor, or a resync racing the backoff resumes past it"
    assert 'setConn("full replay…", false)' in full, \
        "M11: the pill must stop claiming 'live' for the whole backoff after the socket was closed"
    # A refused handshake is final: an identical reconnect gets an identical
    # refusal, so retrying only overwrites the reason with 'reconnecting…' and
    # opens a socket every 1-10 s for as long as the tab is open.
    onclose = _slice(conn, "ws.onclose = () =>", "ws.onerror")
    assert "if (refused) return;" in onclose, \
        "M11: a v2 handshake refusal must not be retried into a permanent reconnect loop"
    assert "refused = true" in _slice(src, "function onHello", "function onControl"), \
        "M11: the refusal must set the flag ws.onclose reads"
    resync2 = _slice(src, "function forceResync", "function applyRate")
    assert "connect(replaying ? null : wsCursor)" in resync2, \
        "M11/WS-3: a forced resync is a CURSOR resume, not a full re-replay — but never mid-burst, " \
        "when the screen does not yet hold the state the cursor describes"
    # DATA-MODEL-9/-13: ?snap=verify compares the hydrated fold against a shadow
    # replay of the same bytes. The digest must stay ORDER-SENSITIVE — sorting
    # the rows would make it blind to card-order divergence, which is precisely
    # what the array-of-pairs snapshot shape exists to preserve.
    vd = _slice(src, "function verifyDigest", "function verifyReport")
    assert ".sort()" not in vd, \
        "DATA-MODEL-13: verifyDigest must not sort — card ORDER is part of what the snapshot reproduces"
    hello = _slice(src, "function onHello", "function onControl")
    assert '"unknown-task"' in hello, "M11: v2 refuses an unknown ?task= at the handshake"
    assert "h.fromApplied" in hello and "resetView()" in hello, \
        "M11: a REFUSED resume must rebuild before the snapshot hydrates on top of kept state"
    assert "h.reason" in hello, "M11: the refusal reason is surfaced, never silent"
    # ...and it must SURVIVE. hello, snapshot and boundary land within
    # milliseconds of each other on the refused-resume path, so a boundary that
    # assigns connTxt.title outright erases the reason almost as soon as it is
    # set — the guard above would then be true of the source and false of the
    # page. The tooltip is composed from keyed notes; only setConnNote and
    # clearConnNotes may write it, and each condition owns one key.
    title_writes = [ln for ln in src.splitlines()
                    if "connTxt.title =" in ln and not ln.strip().startswith("//")]
    assert len(title_writes) == 2, \
        "MINOR-2: connTxt.title may only be written by setConnNote/clearConnNotes — " \
        f"a direct assignment erases whatever another condition just published; found {title_writes}"
    notes = _slice(src, "const connNotes = new Map();", "let reconnectTimer")
    assert "connNotes.delete(key)" in notes and "connNotes.values()" in notes, \
        "MINOR-2: a note is added or removed by key, and the title is the join of the live ones"
    assert 'setConnNote("resume"' in hello and 'setConnNote("fold"' in hello, \
        "MINOR-2: the refusal reason and the foldGen mismatch each own a note key"
    assert 'setConnNote("trim"' in bound and "connTxt.title" not in bound, \
        "MINOR-2: the boundary frame owns the log-budget note and nothing else"
    nff = _slice(src, "function noteFirstEventFrame", "function feedEvent")
    assert "resumeFrom = null" in nff and "resetView()" in nff, \
        "M11/DATA-MODEL-10: events before a hello mean an old server — drop the resume assumption"
    assert "wsCursor = null" in nff, \
        "M11: ...and drop the stored position with it — an old server ignores &from=/&sig= every time"
    # The stored cursor is the SERVER's: it only ever comes off a published frame.
    cur = _slice(src, "function noteCursor", "function connect(resume)")
    assert "wsCursor = { sig: c.sig, offset: c.offset }" in cur, \
        "M11: the page stores only a server-published (sig, offset)"

    # ---- PRIOR-ART-TOUCH-12: the guard shapes ported from Touch's own suite
    # (tests/test_touch_frontend.py:505/:903/:1063) — a coalescer exists, the
    # expensive HTTP routes poll on their OWN slower cadence than the paint
    # flush, and every growing collection is capped ----
    tick = _slice(src, "// The standing flush point (GD-E)", "// ---- statistics view ----")
    assert "if (!replaying) flushDirty();" in tick and "if (!replaying) tpFlush();" in tick, \
        "PRIOR-ART-TOUCH-12: the paint coalescer AND the strip run on the 1 s tick"
    assert "}, 1000);" in tick, "PRIOR-ART-TOUCH-12: that tick is the 1 s interval"
    assert re.search(r"setInterval\(\(\) => \{ refreshTasks\(\); refreshArtifacts\(\); \}, 5000\);", src), \
        "PRIOR-ART-TOUCH-12: /tasks + /artifacts are re-read on their own slower cadence"
    for cap in ("LOG_CAP", "TP_TAIL_MAX", "LIVE_TICK_MS"):
        assert cap in src, f"PRIOR-ART-TOUCH-12: {cap} — every growing collection carries a named cap"

    # ---- DATA-MODEL-9: the cross-file literals must be equal, verbatim ----
    with open(PY, encoding="utf-8") as fh:
        py = fh.read()
    for name in ("FOLD_GEN", "TP_IDLE_MS", "TP_STALL_MS", "TP_TAIL_MS",
                 "TP_TAIL_MIN", "TP_TAIL_MAX"):
        # `const NAME = n` or a continuation of one (`const A = 1, NAME = 2`)
        h = re.search(r"(?:const |, )%s = (\d+)" % name, src)
        p = re.search(r"^%s = (\d+)" % name, py, re.M)
        assert h, f"DATA-MODEL-9: monitor.html must declare {name} as a module-level integer literal"
        assert p, f"DATA-MODEL-9: monitor_server.py must declare {name} as a module-level integer literal"
        assert h.group(1) == p.group(1), (
            f"DATA-MODEL-9: {name} differs — monitor.html={h.group(1)} "
            f"monitor_server.py={p.group(1)}; the two folds must agree "
            "(bump FOLD_GEN in BOTH files when a fold rule changes)")
    # TP_SLOW_MS is deliberately NOT in that list: the server derives no "slow"
    # segment, so it is presentation-only and FOLD_GEN says nothing about it.
    # It sits between two mirrored literals under a comment about FOLD_GEN and
    # would otherwise read as covered, so the page must say so out loud.
    assert "TP_SLOW_MS is PRESENTATION-ONLY" in src, \
        "DATA-MODEL-9: TP_SLOW_MS must be documented as page-only, not a mirrored fold literal"
    assert not re.search(r"^TP_SLOW_MS = ", py, re.M), \
        "DATA-MODEL-9: TP_SLOW_MS is page-only — a server-side copy would need mirroring too"


def roster_guards(src):
    """ROSTER: the orchestrator accordion also lists driver-declared PLANNED
    sub-plans (optional event key `roster`, latest wins) before their loops
    exist — display-only bullets, never materialized as cards."""
    ev_slice = _slice(src, "function onEvent(", "function logLi")
    assert "Array.isArray(ev.roster)" in ev_slice, \
        "ROSTER: onEvent must capture the roster array from orchestrator-card events"
    assert 'ev.plan === "orchestrator" && Array.isArray(ev.roster)' in ev_slice, \
        "ROSTER: the roster is honored only on the reserved orchestrator card"
    sub_slice = _slice(src, "function renderSubplans()", "function fmtTok")
    assert 'className = "planned"' in sub_slice, \
        "ROSTER: renderSubplans must render card-less roster entries as planned bullets"
    assert 'st.textContent = "planned"' in sub_slice, \
        "ROSTER: planned bullets carry the state word (color is never the only signal)"
    assert "plans.has(id)" in sub_slice, \
        "ROSTER: an entry whose loop already has a card must not render twice"
    assert ".subplans li.planned .dot" in src, \
        "ROSTER: planned bullets need their hollow-dot style"
    snap_slice = _slice(src, "function applySnapshot(", "function snapNum")
    assert "snap.roster" in snap_slice, \
        "ROSTER: v2 snapshot hydration must apply the fold's roster (FOLD_GEN 2) — " \
        "without it a hydrated view drops what a full replay shows"


def editico_guards(src):
    """EDITICO: the source-impact icon in the loop-card header. A pencil for
    loops whose observed roles include an implementer (they edit source), the
    same pencil struck through for role sets that only ever write .md files.
    Derived from the role set — never from the plan id — and recomputed only
    when a NEW role joins, so it stays off the per-event path."""
    plan_el = _slice(src, "function planEl(", "function render()")
    assert 'class="editico" hidden' in plan_el, \
        "EDITICO: the icon is part of planEl's static template, hidden until a role is known"
    assert 'class="strike"' in plan_el, \
        "EDITICO: the template carries the strike line the readonly variant reveals"
    assert 'editIco: id === "orchestrator" ? null' in plan_el, \
        "EDITICO: the orchestrator card never wears the icon (editIco is null by design)"
    upd = _slice(src, "function updateEditIcon(", "function upsertAgent(")
    assert 'role.split(":").pop() === "impl"' in upd, \
        "EDITICO: classification keys on the impl role, bare or stage-qualified"
    assert 'classList.toggle("readonly"' in upd, \
        "EDITICO: the variant flips via classList, never innerHTML"
    assert "aria-label" in upd and ".title = " in upd, \
        "EDITICO: color/shape is not the only signal — tooltip + aria-label travel with it"
    ups = _slice(src, "function upsertAgent(", "const NODE_STATES")
    assert re.search(r"if \(!node\) \{[^\n]*updateEditIcon\(p\);", ups), \
        "EDITICO: the live path recomputes only when a NEW role joins the set"
    hyd = _slice(src, "function hydratePlan(", "// ---- ?snap=verify")
    assert "updateEditIcon(p);" in hyd, \
        "EDITICO: snapshot hydration classifies too, or a hydrated view drops the icon"
    assert ".card h2 .editico .strike { visibility:hidden; }" in src, \
        "EDITICO: the strike is hidden by default (editor loops show the plain pencil)"
    assert ".card h2 .editico.readonly .strike" in src, \
        "EDITICO: the readonly variant reveals the strike"


# ---- memory manager ----------------------------------------------------------
# I13 + the monitor.html half of I14. Everything below is ADDITIVE and lives
# after the last existing section: no marker moves, no existing slice changes
# shape (UI-11). The subject is mostly a SECOND document, `memory.html`, which is
# why it can be guarded whole instead of by text-marker slices — and why it can
# be EXECUTED (see `memory_driven`, the only guard in this file that runs code).


def memory_guards(src):
    """The memory editor page, and the two edits monitor.html was allowed.

    `src` is monitor.html. `memory.html` is read here rather than in `main()`
    because it is this section's subject and nothing above it may depend on it.
    """
    # ---- I14 (SECURITY-5): the streaming page pins its referrer policy ----
    assert '<meta name="referrer" content="no-referrer">' in src, \
        "SECURITY-5: monitor.html must pin <meta name=\"referrer\" content=\"no-referrer\"> — " \
        "its URL carries the per-boot token and that token now authorizes memory writes"
    inline = _slice(src, "function mdInline", "function renderMd")
    assert 'rel="noopener noreferrer"' in inline, \
        "SECURITY-5: mdInline's agent-authored links must carry rel=\"noopener noreferrer\" " \
        "(rel=noopener alone blocks window.opener and NOT the Referer header)"
    assert 'rel="noopener">' not in inline, \
        "SECURITY-5: the bare rel=\"noopener\" form must be gone from mdInline"

    # ---- G4: exactly ONE link, in the header, tokened ----
    header = _slice(src, "<header>", "</header>")
    assert 'id="memoryLink"' in header, \
        "G4: the memory link lives in monitor.html's header"
    assert src.count('id="memoryLink"') == 1, \
        "G4: exactly one link to the memory page (the page's whole footprint on " \
        "the streaming document is that anchor plus its href)"
    assert 'withToken("/memory")' in src, \
        "G4/UI-2: the href must be tokened like #statsLink's — a bare /memory is a " \
        "top-level navigation with no carrier and lands on memory.html's auth banner"
    # ...and the write surface itself did NOT leak onto the streaming page: the
    # editor is a separate document precisely so this stays true (UI-7, G4).
    for banned in ("textarea", "<form", '"PUT"', '"DELETE"', '"POST"'):
        assert banned not in src, \
            f"G4: monitor.html must stay read-only — {banned!r} belongs to memory.html"

    with open(MEMORY_HTML, encoding="utf-8") as fh:
        mem = fh.read()

    # ---- the page's own posture: no markup sink, no socket, no task ----
    assert '<meta name="referrer" content="no-referrer">' in mem, \
        "SECURITY-5: memory.html must pin the referrer policy too"
    assert "// ---- memory manager ----" in mem, \
        "UI-11: all memory JS lives in one designated marker section"
    js = _slice(mem, "// ---- memory manager ----", "</script>")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                 "eval(", "new Function("):
        assert sink not in js, \
            f"UI-11/FRONTEND-1: memory.html must contain no {sink} — memory content is " \
            "agent-written text the operator pastes model output into"
    assert "createElement" in js and "textContent" in js, \
        "UI-11: rows and preview nodes are built via createElement/textContent"
    assert "WebSocket" not in mem, \
        "G4: the memory page opens no socket — that is half of why it is a second document"
    assert "?task=" not in mem and "currentTask" not in mem, \
        "G4/UI-2: memory is project-scoped, not task-scoped — no task state on this page"

    # ---- UI-15: reuse, not re-invention ----
    for reused in ("const TOKEN = new URLSearchParams(location.search).get(\"token\")",
                   "const tokenParam = (sep) =>", "const withToken = (url) =>",
                   "function showAuthBanner", 'id="authBanner"', "function fmtSize",
                   "function fmtAgo", '"arow"', "orchMemoryWrap"):
        assert reused in mem, \
            f"UI-15: memory.html must reuse monitor.html's {reused!r} rather than " \
            "growing a second divergent helper"

    # ---- G5/UI-5: reads may carry the token in the query, writes may NOT ----
    wi = _slice(js, "function memWriteInit", "const fileUrl")
    assert '"x-orch-token"' in wi, "G5/UI-5: a write carries the token in x-orch-token"
    assert '"x-touch-write": "1"' in wi, "G5/W2: a write must carry X-Touch-Write: 1"
    assert '"content-type": "application/json"' in wi, \
        "G5/UI-5: a write body must be declared application/json (a non-simple request " \
        "property, so a cross-origin attempt needs a preflight the server never satisfies)"
    assert "withToken" not in wi, "G5: the write path must not reach for the query carrier"
    for site in ("memReq(fileUrl(name), memWriteInit(\"PUT\"",
                 "memWriteInit(\"DELETE\", null)",
                 "memWriteInit(\"POST\", { content:"):
        assert site in js, f"G5: the write call sites must go through memWriteInit — {site!r}"
    assert not re.search(r"memReq\(withToken\([^)]*\), memWriteInit", js), \
        "G5/UI-5: no write may be sent to a ?token= URL"
    # every read that needs the token asks for it explicitly
    assert 'memReq(withToken("/api/memory/list")' in js, \
        "G5: the list is a tokened GET on /api/memory/list"
    assert "memReq(withToken(fileUrl(name))" in js, \
        "G5: the file read is a tokened GET on /api/memory/file?name="

    # ---- UI-4: JSON is verified before it is parsed ----
    req = _slice(js, "async function memReq", "/** Write init")
    assert 'res.headers.get("content-type")' in req and \
        'ct.indexOf("application/json") === -1' in req, \
        "UI-4: every memory answer is checked for JSON BEFORE .json() — an older " \
        "monitor_server answers GET /memory with monitor.html, 200, 151 KB"
    assert "restart touch-monitor" in req, \
        "UI-4: the version-skew state must be NAMED, never an empty list"
    assert "catch (e) {}" not in js, \
        "UI-4: `catch (e) {}` is the idiom that made this failure class invisible"
    assert "showAuthBanner(" in req, "UI-4: a 401 raises the banner that says the word 'token'"

    # ---- UI-3: optimistic concurrency, and the sha the server returned ----
    assert "ifMatch: mem.base.sha256" in js, \
        "UI-3/G5: a save carries the sha of the bytes the operator loaded"
    assert 'ifMatch: "*"' not in js and '"*"' not in _slice(js, "async function memSave",
                                                            "/** The response's sha"), \
        "G5: `\"*\"` is not an accepted ifMatch — there is no blind overwrite of a file " \
        "that is injected into future sessions"
    adopt = _slice(js, "function adoptSaved", "/** A 409 writes NOTHING")
    assert "mem.base = {" in adopt and 'sha256: String(b.sha256 || "")' in adopt, \
        "UI-3: the PUT response's sha must be ADOPTED, or save-then-save 409s against " \
        "the operator's own previous write"
    conflict = _slice(js, "function enterConflict", "function conflictReload")
    assert "edText" not in conflict, \
        "UI-3: a 409 must not touch the textarea — the buffer is the only copy of the edit"
    for exit_name in ("function conflictReload", "function conflictShowBoth",
                      "function conflictOverwrite"):
        assert exit_name in js, f"UI-3: the 409 must offer {exit_name} as a NAMED exit"
    assert "retry" not in conflict.lower() and "retry" not in _slice(js, "function memSave",
                                                                    "function adoptSaved").lower(), \
        "UI-3: a conflict offers reload / show both / overwrite — never a bare retry"
    over = _slice(js, "function conflictOverwrite", "/** DELETE is a move")
    assert "mem.base.sha256 = mem.conflict.sha256" in over, \
        "UI-3: a deliberate overwrite is still an ifMatch save, against the sha the " \
        "server just published"

    # ---- UI-3/UI-18: the buffer is LOCKED while a write is out ----
    # The textarea stayed live during a PUT, so keystrokes that landed mid-flight
    # were bytes the server never saw — and the response then rendered "saved"
    # over them and cleared the dirty gate. Three defences, all pinned here.
    chrome = _slice(js, "function renderEditorChrome", "/** The CLI's own measurement")
    assert "edText.disabled = !mem.base || inFlight;" in chrome and \
        'const inFlight = mem.state === "saving";' in chrome, \
        "UI-18: the buffer must be disabled while a write is in flight — a live " \
        "textarea under a PUT is bytes the server never receives"
    assert "renderEditorChrome();" in _slice(js, "async function memDelete",
                                             "/** Restore is a CREATE"), \
        "UI-18: the DELETE path must lock the buffer too (it is a write like any other)"
    assert "mem.dirty = oneTrailingNewline(edText.value) !== oneTrailingNewline(content);" \
        in adopt, \
        "UI-18/D13: the response must recompute dirtiness against the LIVE buffer — " \
        "a disabled textarea does not cover a paste that lands in the same tick"
    handler = _slice(js, 'edText.addEventListener("input"', "/** PUT. `ifMatch`")
    assert 'mem.state !== "saving"' in handler, \
        "UI-18: typing must not drive the state machine out of `saving` — the " \
        "in-flight label is the only evidence of what has left the browser"

    # ---- UI-3: the 409 panel performs the operation that was REFUSED ----
    assert 'enterConflict(r.body, "save")' in js and 'enterConflict(r.body, "delete")' in js, \
        "UI-3: a conflict records WHICH mutation was refused — a save and a delete " \
        "are not interchangeable"
    assert 'cfOverwrite.textContent = isDel ? "delete it anyway"' in js, \
        "UI-3: ...so the third exit is NAMED for the operation (a refused delete may " \
        "not offer 'overwrite with mine')"
    assert 'if (op === "delete") { memDelete(); return; }' in over, \
        "UI-3: ...and taking that exit deletes, rather than saving the buffer over a " \
        "file the operator asked to remove"

    # ---- m3: the list has its own captured id ----
    lister = _slice(js, "async function memRefreshList", "/** The open file moved")
    assert "const seq = mem.listSeq + 1;" in lister and \
        "if (seq !== mem.listSeq) return false;" in lister, \
        "UI-9: the list is sequenced like the editor — a slow answer landing last " \
        "must not replace the rows it was overtaken by"

    # ---- m4: one reason line, urgent writer wins ----
    assert "function renderReason" in js, \
        "UI-18: one function owns the reason line — four writers wanted it and the " \
        "poll used to overwrite the urgent sentence with the least urgent one"
    drifted = _slice(js, "function noteDiskDrift", "function renderHead")
    assert "mem.drift" in drifted and "setState" in drifted and \
        'mem.state !== "saved" && mem.state !== "error"' in drifted, \
        "UI-18: a poll may add the drift line but may not erase a `saved · <mtime>` " \
        "badge or a live error — those are the server's own last answer"

    # ---- m5: an unreported cap is SAID to be unreported ----
    assert "did not report the " in js and \
        "const MEM_LIMIT_FALLBACK = { maxBytes: null, maxFiles: null" in js, \
        "UI-14: a missing `limits` entry must not print as a number — 'at most 0 " \
        "files' is a false server guarantee, not a default"

    # ---- the deliberate exit adopts the WHOLE disk copy, not just its sha ----
    # Adopting sha+mtime alone let "delete it anyway" remove the other writer's
    # newer revision and then offer a one-click restore of the OLDER one, because
    # `memDelete` keeps `mem.base.content` as the trash entry's restorable bytes.
    assert "mem.base.content = mem.conflict.content;" in over and \
        "mem.base.size = mem.conflict.size;" in over, \
        "UI-3/UI-18: the conflict exit must adopt the disk copy's BYTES too — the " \
        "trash entry is built from `mem.base.content`, so a half-adopted base " \
        "restores a revision that was already replaced"
    trash = _slice(js, "function pushTrash", "async function memRestore")
    assert "content: gone" in trash and "byteLen(gone)" in trash, \
        "UI-18: the trash entry's restorable bytes are the DELETED bytes — the " \
        "DELETE carried ifMatch, so the server removed the file only because the " \
        "disk still matched them"
    assert "buffer: held !== gone" in trash, \
        "UI-18: ...and a discarded unsaved edit is KEPT beside them rather than " \
        "silently becoming what `restore` means"
    tr = _slice(js, "function renderTrash", "// ---- the editor ----")
    assert "restore writes the " in tr and "you had unsaved in the editor" in tr and \
        "fmtSize(t.deletedBytes)" in tr and "fmtSize(t.bufferBytes)" in tr, \
        "UI-18: the row NAMES both byte-sets and their sizes — the operator can " \
        "see neither, and only one of them is what a click writes"
    assert 'memRestore(t.name, "buffer")' in tr, \
        "UI-18: ...and the second byte-set is reachable, as its own named button"
    # n1: the other growing collection is capped, and the cap is on screen
    assert re.search(r"const MEM_TRASH_CAP = \d+;", js) and "MEM_TRASH_CAP" in tr, \
        "UI-14: every growing collection is capped AND discloses its cap — the " \
        "trash group is the second one on this page"

    # ---- m1: the in-flight lock has no second door ----
    assert 'mem.state !== "conflict" && mem.state !== "saving"' in drifted, \
        "UI-18/m1: the poll's missing-file branch must not fire while a write is " \
        "out either — it would re-enable the textarea mid-flight, which is the " \
        "clobber the lock exists to prevent, opened by a timer the page schedules"

    # ---- m2: a list the page could not read disables the write affordances ----
    assert "function rootWritable" in js and "mem.listOk === true" in \
        _slice(js, "function rootWritable", "function entryReason"), \
        "G6/UI-6/m2: writability must require the list answer we still have — " \
        "after a 401 the rows are a memory, not an observation"
    assert "the file list could not be read" in js, \
        "G6/D13/m2: ...and that is the row's REASON, said in words"
    assert "renderEditorChrome();" in lister, \
        "m2: the list's failure path must re-render the EDITOR too, or save stays " \
        "live for the next 12 s over a directory the page cannot read"
    assert "if (mem.listOk !== true) return;" in drifted, \
        "m2: ...and nothing may claim a file is 'gone from the memory root' from " \
        "an answer that never arrived"

    # ---- m3: the outcome of an abandoned write is surfaced, not dropped ----
    assert "function noteOutcome" in js and "mem.outcomes" in js, \
        "UI-18/m3: a write whose editor moved on must still report its outcome — " \
        "the operator launched it and nothing else on the page can tell them"
    saver = _slice(js, "async function memSave", "function adoptSaved")
    assert "noteOutcome(name," in saver, \
        "m3: ...from the save path, on all three exits (failed, refused, landed)"
    assert "noteOutcome(name," in _slice(js, "async function memDelete",
                                        "/** One trash entry"), \
        "m3: ...and from the delete path, which also still records the trash entry"
    assert "mem.outcomes[f.name]" in _slice(js, "function renderRows", "function tag("), \
        "m3: ...and it renders on the row of the file it is ABOUT"

    # ---- m4: one decision, one dialog ----
    assert "mem.leaving = true;" in js and "if (mem.leaving || !isDirty()) return;" in js, \
        "UI-8/m4: the link's confirm and the browser's unload prompt are the same " \
        "question — answering one must not raise the other, or the operator is " \
        "trained to click through unload dialogs"
    assert "mem.dirty = false" not in \
        _slice(js, 'byId("backLink").addEventListener', "window.addEventListener"), \
        "UI-8/m4: ...and the flag is what records the answer, never a cleared " \
        "dirty bit — something else could still cancel the navigation"

    # ---- n2/n4: the pinned confirmation, on every path that can write it ----
    restore = _slice(js, "async function memRestore", "/** Create. The name")
    assert "memHasPinned" in restore and "allowPinned" in restore, \
        "DOCS-6/n2: a restore of bytes carrying `pinned:` needs the same explicit " \
        "confirmation a save does, or the file can never be restored at all"
    assert "memReason(r.body" in restore, \
        "n2: ...and a refused restore is worded from the SERVER's reason, not " \
        "blamed on a name collision that may not be the cause"
    closer = _slice(js, 'byId("edClose").addEventListener', 'byId("cfReload")')
    assert "mem.pinAsked = false;" in closer and "edPin.checked = false;" in closer, \
        "DOCS-6/n4: the pinned confirmation is per FILE — closing the editor must " \
        "clear it, or the comment that says so becomes false"

    # ---- n2/n5: the small honesty fixes ----
    assert '<div id="authBanner" hidden role="alert"></div>' in mem, \
        "UI-4: the auth banner is the one thing a tokenless visitor must hear — " \
        "role=\"alert\", like monitor.html's twin"
    assert 'href="/memory"' not in src and 'href="/">' not in mem, \
        "G4/UI-2: neither link ships an UNTOKENED href in the markup — that href is " \
        "a live navigation to the other page's auth banner whenever the script " \
        "has not run; the tokened URL is written by JS"

    # ---- UI-9: the poll's two races ----
    assert re.search(r"const MEM_POLL_MS = 1[0-5]\d{3};", js), \
        "UI-9: the list polls on its OWN 10-15 s cadence, not the dashboard's 5 s live one"
    assert "setInterval(memRefreshList, MEM_POLL_MS)" in js, \
        "UI-9: ...and that is the only standing timer on the page"
    refresh = _slice(js, "async function memRefreshList", "/** The open file moved")
    assert "edText" not in refresh, \
        "UI-9: the poll replaces the LIST and never writes the editor"
    opener = _slice(js, "async function memOpen", "function hideExtras")
    assert "if (seq !== mem.seq || mem.name !== name) return false;" in opener, \
        "UI-9: a captured-id guard must precede the write — click A, click B, A lands second"
    assert opener.find("if (seq !== mem.seq || mem.name !== name) return false;") < \
        opener.find("edText.value = mem.base.content"), \
        "UI-9: ...and it must sit BEFORE the textarea is filled, not after"
    drift = _slice(js, "function noteDiskDrift", "function renderHead")
    assert "changed on disk" in drift, \
        "UI-3: the poll raises 'changed on disk' before the operator presses save"

    # ---- UI-8: one dirty predicate over every exit ----
    assert "function isDirty()" in js and "function guardDirty" in js, \
        "UI-8: one isDirty() gate, consulted by every exit"
    assert 'window.addEventListener("beforeunload"' in js and "isDirty()" in \
        _slice(js, 'window.addEventListener("beforeunload"', "function memBoot"), \
        "UI-8: beforeunload covers tab close/reload"
    assert 'byId("backLink").addEventListener("click"' in js and \
        'guardDirty("leave this page")' in js, \
        "UI-8: the one link off the page is gated too"
    assert "popstate" not in js, \
        "UI-8: this page has exactly ONE route — a popstate handler here would be a " \
        "cancel that does nothing"

    # ---- UI-18: `saved` is derived from the server's mtime ----
    assert 'setState("saved", fmtStampNs(b.mtime_ns))' in adopt, \
        "UI-18: 'saved' spells the SERVER's mtime; it is never flipped on the click"
    for st in ("idle", "loading", "clean", "dirty", "saving", "saved", "conflict", "error"):
        assert st + ":" in _slice(js, "const MEM_STATES", "function setState"), \
            f"I13: the per-file state machine must name {st!r}"

    # ---- UI-16/UI-17: the page says whose memory it is and what it does ----
    assert "These files are loaded into every future Claude Code session in this " \
        "project." in mem, "UI-17: the fixed posture sentence must render always"
    assert "loaded as: the auto-memory INDEX" in js and \
        "loaded as: an auto-memory topic note" in js, \
        "UI-17: per-tier labels, one line each"
    head = _slice(js, "function renderHead", "function limitNum")
    assert "mem.root" in head and "/health" not in head, \
        "UI-16/UI-4: the root and the aligned banner come from the TOKENED list route — " \
        "/health digests every path it publishes because it answers without a token"
    assert "NOT aligned" in head and "autoMemoryDirectory" in head, \
        "UI-16: a mis-aligned root must say so, and name the key that fixes it"

    # ---- DOCS-14/DOCS-16: what an edit actually does, and frontmatter ----
    assert "takes effect in the NEXT session" in js and "read on demand" in js, \
        "DOCS-14: index edits and topic-file edits reach a session differently"
    # The budget rule itself is checked BEHAVIORALLY (`memory_driven` asserts the
    # exact {lines, bytes} this function returns for `test_memory_hygiene.py`'s own
    # cases, including the two it wrote comments about). What is pinned here is the
    # shape a substring check cannot see, because the previous guard —
    # `"<!--" in budget and '"---\\n"' in budget` — passed for an implementation
    # that disagreed with the repo rule in BOTH directions.
    budget = _slice(js, "const MEM_FRONTMATTER_RE", "function memHasPinned")
    assert r"const MEM_FRONTMATTER_RE = /^---[ \t]*\r?\n[\s\S]*?\r?\n(?:---|\.\.\.)" \
        r"[ \t]*(?:\r?\n|$)/;" in budget, \
        "DOCS-14: frontmatter must be recognized the way the repo gate recognizes " \
        "it — trailing spaces after the fence, and BOTH terminators (`---` and " \
        "YAML's `...`), or a one-line index is reported as four"
    assert r"const MEM_BLOCK_COMMENT_RE = /^[ \t]*<!--(?:(?!-->)[\s\S])*-->[ \t]*" \
        r"(?:\r?\n|$(?![\s\S]))/gm;" in budget, \
        "DOCS-14: the comment body must be `(?:(?!-->)[\\s\\S])*` and the match " \
        "must END at a line end — a lazy body or an optional newline swallows a " \
        "line that merely STARTS with a comment and UNDER-counts, which is the " \
        "direction that silently truncates the index in the model"
    assert "[\\s\\S]*?-->" not in js, \
        "DOCS-14: the lazy comment body the repo rule explicitly rejects must not " \
        "come back (it crosses `-->` whenever the shorter match fails)"
    assert "function countLines" in budget and "splitlines" in budget, \
        "DOCS-14: lines are counted in Python's `splitlines` unit, which is what " \
        "the gate counts in"
    with open(HYGIENE, encoding="utf-8") as fh:
        hyg = fh.read()
    for frag, why in ((r"(?:(?!-->).)*", "the non-crossing comment body"),
                      (r"(?:---|\.\.\.)", "both frontmatter terminators"),
                      ("def index_budget", "the gate's own measurement")):
        assert frag in hyg, \
            f"DOCS-14: {HYGIENE} no longer spells {why} ({frag!r}) — the page's " \
            f"regexes are a port of THAT rule and the driven tuples below were " \
            f"derived from it, so both have to be re-checked together"
    assert "never adds" in js and "never writes that field" in js, \
        "DOCS-16: the editor neither invents frontmatter nor forges `modified`"

    # ---- DOCS-6 / G7 step 7: `pinned` is surfaced in words ----
    assert "allowPinned" in js, \
        "DOCS-6/G7: the pinned confirmation travels as an explicit request flag"
    assert "loaded into EVERY session" in mem, \
        "DOCS-6: ...and the UI spells what that means, in words, beside the checkbox"

    # ---- G6/UI-6: honest disabled affordances ----
    assert "function entryWritable" in js and "function entryReason" in js, \
        "G6/D13: writability and its REASON are one pair, read from the list contract"
    assert "the write plane is off" in js and "--allow-memory-write" in js, \
        "G6: the default-off write plane is named on screen, with the flag that opens it"
    assert "read-only: " in js, \
        "UI-6/D13: a non-writable row renders read-only WITH its reason, never a live " \
        "textarea over a path the server will refuse"

    # ---- UI-14: caps, disclosed ----
    assert re.search(r"const MEM_LIST_CAP = \d+;", js), "UI-14: the list carries a named cap"
    assert "holds more than this view lists" in js, \
        "UI-14: a truncated list discloses the truncation, never silently"
    caps = _slice(js, "capsEl.textContent", "function limitNum")
    assert "maxFiles" in caps and "maxBytes" in caps, \
        "UI-14: the server's own caps are disclosed on screen"

    # ---- UI-7: delete is recoverable, and restore is a create ----
    assert "async function memDelete" in js and "function askDelete" in js, \
        "UI-7: delete is its own confirmed step"
    ask = _slice(js, "function askDelete", "async function memDelete")
    assert "Type its name" in ask and "confirm(" in ask, \
        "UI-7: a non-empty file needs its name typed; an empty one takes a single confirm"
    assert "ifMatch=" in js, "UI-7: a delete carries the same ifMatch guard as a save"
    assert "async function memRestore" in js and 'memWriteInit("POST"' in js, \
        "UI-7: restore is a CREATE, which is why a name that came back cannot be " \
        "restored over"

    # ---- G8: the preview is a node builder over SAVED bytes only ----
    prev = _slice(js, "function memPreviewToggle", "// ---- wiring ----")
    assert "mem.base.content" in prev and "edText.value" not in prev, \
        "G8/UI-15: the preview renders the SAVED file — renderMd is a lossy one-way " \
        "transform and must never be round-tripped through the editor"
    assert "preview — rendered from the SAVED file" in prev, \
        "UI-15: the preview is LABELED as a preview"
    assert "createTextNode" in _slice(js, "function memInline", "function memRenderMd"), \
        "G8: every literal character reaches the DOM as a text node — there is no " \
        "escaping step to get wrong because there is no markup sink"
    assert 'a.setAttribute("rel", "noopener noreferrer")' in js, \
        "SECURITY-5: links in previewed memory text carry rel=\"noopener noreferrer\""
    assert "/^(https?:\\/\\/|#|\\.{0,2}\\/)/" in js, \
        "ARTIFACTS-2/G8: preview link hrefs stay protocol-whitelisted"

    # ---- G7 step 1: the server's flat-name rule, mirrored verbatim ----
    assert r"/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.md$/" in js, \
        "G7: the page mirrors the server's flat-name regex character-for-character"

    # ---- UI-3: the trailing-newline decision is honored on the client too ----
    assert "function oneTrailingNewline" in js and \
        "const content = oneTrailingNewline(edText.value)" in js, \
        "UI-3: a <textarea> yields no trailing newline; the sha we adopt must describe " \
        "the bytes we hold, so the same normalization runs here"


#: A fake DOM + fake `fetch`, driving memory.html's script for real.
#:
#: Every other guard in this file asserts on TEXT, and a text guard cannot see a
#: clobber: an implementation can contain every asserted substring and still fill
#: the wrong editor, or overwrite a file that moved under it. Touch's own suite
#: states the lesson in `tests/test_touch_frontend.py`: *"Both of attempt 2's
#: majors passed every static guard here and were still dead in execution."* So
#: this executes it — node + `vm`, no dependency and no browser, and it skips
#: loudly where node is absent (UI-10).
#:
#: `getElementById` answers ONLY for ids memory.html actually declares (they are
#: passed in from the HTML), so a typo'd id is a TypeError here rather than a
#: silently dead handler in a browser.
MEM_HARNESS_JS = r""""use strict";
/* Drives plugin/touch/shared/monitoring/memory.html's script block.
 * Prints one `PASS: <label>` / `FAIL: <label> — <detail>` per assertion. */

const fs = require("fs");
const vm = require("vm");

const HTML_PATH = process.argv[2];
const IDS = new Set(JSON.parse(process.argv[3]));

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
        this.checked = false;
        this.value = "";
        this.title = "";
        this.href = "";
        this.type = "";
        this.id = "";
        this._class = "";
    }
    get className() { return this._class; }
    set className(v) { this._class = String(v); }
    get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
    get childElementCount() { return this.children.length; }
    appendChild(node) {
        if (node.parentNode) node.parentNode.removeChild(node);
        node.parentNode = this;
        this.childNodes.push(node);
        return node;
    }
    removeChild(node) {
        const at = this.childNodes.indexOf(node);
        if (at !== -1) { this.childNodes.splice(at, 1); node.parentNode = null; }
        return node;
    }
    setAttribute(name, value) { this.attributes[String(name)] = String(value); }
    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name)
            ? this.attributes[name] : null;
    }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    fire(type) {
        const ev = { type: type, preventDefault() { ev.defaulted = true; }, returnValue: "" };
        (this.listeners[type] || []).forEach((fn) => fn(ev));
        return ev;
    }
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

const byId = {};
IDS.forEach((id) => { const n = new Element("div"); n.id = id; byId[id] = n; });

const document = {
    body: new Element("body"),
    createElement: (tag) => new Element(tag),
    createTextNode: (v) => new TextNode(v),
    getElementById: (id) => (Object.prototype.hasOwnProperty.call(byId, id) ? byId[id] : null),
    addEventListener: () => {},
};

// --- timers, storage, fetch -----------------------------------------------

/* Intervals are REGISTERED and fired on demand: a real timer would make the
 * run non-deterministic, and dropping them would leave the poll — half of
 * UI-9 — asserted by source text alone. */
const intervals = [];
const store = {};

const ROOT = "/home/dev/proj/.touch/memory";
const LIMITS = { maxBytes: 262144, maxFiles: 64, indexLines: 200, indexBytes: 25600 };
let writePlane = true;
let rootWritable = true;
const disk = {
    "MEMORY.md": { content: "# Memory index\n\n- [a](a.md) — thing\n",
                   sha256: "sha-index-1", mtime_ns: 1700000000000000000,
                   hasFrontmatter: false },
    "note.md": { content: "note one\n", sha256: "sha-note-1",
                 mtime_ns: 1700000001000000000, hasFrontmatter: false },
    "other.md": { content: "other one\n", sha256: "sha-other-1",
                  mtime_ns: 1700000002000000000, hasFrontmatter: false },
};
function sizeOf(name) { return Buffer.byteLength(disk[name].content, "utf8"); }
function listBody() {
    return {
        root: ROOT, effective: ROOT, aligned: true, writable: rootWritable,
        memoryWrite: writePlane, limits: LIMITS,
        files: Object.keys(disk).map((name) => ({
            name: name, size: sizeOf(name), mtime_ns: disk[name].mtime_ns,
            lines: disk[name].content.split("\n").length - 1,
            isIndex: name === "MEMORY.md", overLoadLimit: false,
            hasFrontmatter: disk[name].hasFrontmatter === true,
            writable: true, reason: "",
        })),
    };
}
function fileBody(name) {
    const f = disk[name];
    return { name: name, content: f.content, size: sizeOf(name), sha256: f.sha256,
             mtime_ns: f.mtime_ns, hasFrontmatter: f.hasFrontmatter === true };
}
function jsonRes(status, body) {
    return {
        ok: status >= 200 && status < 300, status: status,
        headers: { get: (n) => (String(n).toLowerCase() === "content-type"
            ? "application/json; charset=utf-8" : null) },
        json: () => Promise.resolve(body),
    };
}

const reqs = [];
/** name -> array of thunks that resolve a held GET (the out-of-order race). */
const held = new Map();
/** The same race for the LIST route: a poll, a save's trailing refresh and a
 *  delete's refresh all call it, so one of them can land last. */
let holdList = false;
const heldLists = [];
/** The list route can also FAIL — a restarted monitor answers 401 to the token
 *  in this tab's URL. Everything the page believes about the directory is then a
 *  memory, and the write affordances over it are a claim it can no longer make. */
let listStatus = 200;
/** Queued answers for the next writes, by method. */
const queued = { PUT: [], POST: [], DELETE: [] };
/* Writes can be held IN FLIGHT, which is the only way to see the label the
 * operator actually stares at while the request is out (UI-18). Without this,
 * an implementation that flips "saved" on the click and then corrects itself
 * from the response is indistinguishable from an honest one. */
let holdWrites = false;
const heldWrites = [];
let confirmAnswer = true;

function fakeFetch(url, init) {
    const u = new URL(String(url), "http://127.0.0.1:8931/");
    const method = String((init && init.method) || "GET").toUpperCase();
    const rec = { path: u.pathname, search: u.search, method: method,
                  headers: (init && init.headers) || {},
                  body: init && init.body ? JSON.parse(init.body) : null,
                  name: u.searchParams.get("name"),
                  ifMatch: u.searchParams.get("ifMatch") };
    reqs.push(rec);
    if (u.pathname === "/api/memory/list") {
        if (listStatus !== 200) {
            return Promise.resolve(jsonRes(listStatus, { error: "not authorized" }));
        }
        // Snapshotted at REQUEST time, so a held answer is genuinely stale.
        const snap = jsonRes(200, listBody());
        if (holdList) return new Promise((resolve) => { heldLists.push(() => resolve(snap)); });
        return Promise.resolve(snap);
    }
    if (u.pathname !== "/api/memory/file") return Promise.resolve(jsonRes(404, { error: "no route" }));
    const name = rec.name;
    if (method === "GET") {
        if (!Object.prototype.hasOwnProperty.call(disk, name))
            return Promise.resolve(jsonRes(404, { error: "no such file" }));
        const answer = () => jsonRes(200, fileBody(name));
        if (held.has(name)) {
            return new Promise((resolve) => { held.get(name).push(() => resolve(answer())); });
        }
        return Promise.resolve(answer());
    }
    const pending = queued[method] || [];
    const respond = () => {
        if (pending.length) return pending.shift();
        if (method === "PUT" || method === "POST") {
            // The default answer is a real write: the disk moves, and the
            // response carries the NEW sha the page is required to adopt.
            const content = rec.body && typeof rec.body.content === "string"
                ? rec.body.content : "";
            const gen = (disk[name] ? String(disk[name].sha256).split("-").pop() : "0");
            disk[name] = { content: content,
                           sha256: "sha-" + name + "-w" + (Number(gen) + 1 || 9),
                           mtime_ns: 1700000100000000000, hasFrontmatter: false };
            return jsonRes(method === "POST" ? 201 : 200,
                { name: name, size: sizeOf(name), sha256: disk[name].sha256,
                  mtime_ns: disk[name].mtime_ns });
        }
        delete disk[name];
        return jsonRes(200, { name: name, deleted: true,
            trash: ROOT + "/.trash/" + name + ".20260730T000000Z" });
    };
    if (holdWrites) {
        return new Promise((resolve) => { heldWrites.push(() => resolve(respond())); });
    }
    return Promise.resolve(respond());
}

const window = { addEventListener: (t, fn) => { window["on" + t] = fn; } };
const sandbox = {
    document: document, window: window, fetch: fakeFetch,
    location: { search: "?token=t0k", href: "http://127.0.0.1:8931/memory?token=t0k" },
    localStorage: {
        getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
        setItem: (k, v) => { store[k] = String(v); },
    },
    /* Answerable both ways: a `confirm` stubbed to `true` forever never
     * exercises guardDirty's REFUSAL path, which is the half that protects the
     * buffer (UI-8). */
    confirm: () => confirmAnswer,
    setInterval: (fn) => { intervals.push(fn); return intervals.length; },
    clearInterval: () => {},
    URL: URL, URLSearchParams: URLSearchParams, console: console, Intl: Intl,
    Math: Math, JSON: JSON, Date: Date, Number: Number, String: String, Boolean: Boolean,
    Object: Object, Array: Array, Set: Set, Map: Map, Promise: Promise, Error: Error,
    RegExp: RegExp, isFinite: isFinite, encodeURIComponent: encodeURIComponent,
};
sandbox.globalThis = sandbox;

const html = fs.readFileSync(HTML_PATH, "utf8");
const js = html.split("<script>")[1].split("</script>")[0];
vm.createContext(sandbox);
vm.runInContext(js, sandbox, { filename: HTML_PATH });

async function settle() {
    for (let round = 0; round < 12; round += 1) {
        await new Promise((resolve) => setImmediate(resolve));
    }
}
function poll() { intervals.slice().forEach((fn) => fn()); }
function lastWrite(method) {
    return reqs.filter((r) => r.method === method).slice(-1)[0] || null;
}

(async () => {
    await settle();

    // --- boot: the list, and the header that names its target ---------------
    const first = reqs[0] || {};
    ok("the list is read from the tokened list route",
       first.path === "/api/memory/list" && first.search.indexOf("token=t0k") !== -1,
       JSON.stringify(first));
    ok("the page names the absolute memory root it edits",
       byId.memRoot.textContent === ROOT, byId.memRoot.textContent);
    ok("the aligned banner comes from the list route, not /health",
       byId.memAligned.textContent.indexOf("aligned — this is the directory") === 0,
       byId.memAligned.textContent);
    ok("every file in the root gets a row",
       byId.memRows.children.length === 3, byId.memRows.children.length);

    // --- the list is sequenced like the editor ------------------------------
    // Four callers share this route. A stale answer landing last would replace
    // the rows with a directory that no longer exists — and noteDiskDrift reads
    // those rows, so it can then claim an existing file is gone.
    holdList = true;
    const staleList = sandbox.memRefreshList();        // snapshot: 3 files
    await settle();
    holdList = false;
    disk["extra.md"] = { content: "extra\n", sha256: "sha-extra-1",
                         mtime_ns: 1700000003000000000, hasFrontmatter: false };
    await sandbox.memRefreshList();                    // answers now: 4 files
    heldLists.splice(0).forEach((release) => release());
    await settle();
    await staleList;
    ok("a stale list answer landing last does not replace the newer rows",
       byId.memRows.children.length === 4, byId.memRows.children.length);
    delete disk["extra.md"];
    await sandbox.memRefreshList();
    await settle();

    // --- a load fills the editor -------------------------------------------
    await sandbox.memOpen("note.md");
    await settle();
    ok("a load fills the editor from the file route",
       byId.edText.value === "note one\n" && byId.edName.textContent === "note.md",
       JSON.stringify(byId.edText.value) + " / " + byId.edName.textContent);

    // --- UI-9(b): the out-of-order load ------------------------------------
    held.set("other.md", []);
    const slow = sandbox.memOpen("other.md");     // held open, will land LAST
    await settle();
    const fast = sandbox.memOpen("note.md");      // answers immediately
    await settle();
    await fast;
    held.get("other.md").forEach((release) => release());
    held.delete("other.md");
    await settle();
    await slow;
    ok("an out-of-order load does not fill the wrong editor",
       byId.edName.textContent === "note.md" && byId.edText.value === "note one\n",
       byId.edName.textContent + " / " + JSON.stringify(byId.edText.value));

    // --- UI-9(a): the poll and a dirty buffer ------------------------------
    byId.edText.value = "note one\nmy paragraph";
    byId.edText.fire("input");
    ok("an edit reads as unsaved changes",
       byId.edState.textContent === "unsaved changes", byId.edState.textContent);
    disk["note.md"].mtime_ns = 1700000050000000000;   // another writer moved it
    disk["note.md"].sha256 = "sha-note-2";
    disk["note.md"].content = "note one\nwritten by the agent\n";
    poll();
    await settle();
    ok("the poll does not clobber a dirty buffer",
       byId.edText.value === "note one\nmy paragraph", JSON.stringify(byId.edText.value));
    ok("the poll says the file changed on disk",
       byId.edReason.textContent.indexOf("changed on disk") === 0, byId.edReason.textContent);

    // --- UI-8: the dirty gate's REFUSAL path -------------------------------
    confirmAnswer = false;
    const refused = await sandbox.memOpen("MEMORY.md");
    await settle();
    ok("a refused discard leaves the dirty buffer exactly where it was",
       refused === false && byId.edName.textContent === "note.md" &&
       byId.edText.value === "note one\nmy paragraph",
       byId.edName.textContent + " / " + JSON.stringify(byId.edText.value));
    confirmAnswer = true;

    // --- UI-3: a 409 does not overwrite ------------------------------------
    queued.PUT.push(jsonRes(409, { error: "precondition failed", sha256: "sha-note-2",
        mtime_ns: 1700000050000000000, size: 30,
        content: "note one\nwritten by the agent\n" }));
    await sandbox.memSave();
    await settle();
    ok("a 409 does not overwrite the operator's buffer",
       byId.edText.value === "note one\nmy paragraph", JSON.stringify(byId.edText.value));
    ok("...and the file on disk is untouched by the refusal",
       disk["note.md"].content === "note one\nwritten by the agent\n",
       JSON.stringify(disk["note.md"].content));
    ok("a 409 renders as a conflict, not as saved",
       byId.edState.className.indexOf("conflict") !== -1 && byId.edConflict.hidden === false,
       byId.edState.className + " hidden=" + byId.edConflict.hidden);
    const cf = byId.cfText.textContent + " " + byId.edReason.textContent;
    ok("a 409 offers reload, show both and overwrite — never a bare retry",
       cf.indexOf("Reload") !== -1 && cf.indexOf("show both") !== -1 &&
       cf.indexOf("overwrite") !== -1 && cf.toLowerCase().indexOf("retry") === -1, cf);

    // "show both" prints the disk copy WITHOUT touching the buffer
    byId.cfBoth.fire("click");
    ok("show both prints the disk copy without touching the buffer",
       byId.edOtherText.textContent === "note one\nwritten by the agent\n" &&
       byId.edText.value === "note one\nmy paragraph",
       JSON.stringify(byId.edOtherText.textContent));

    // --- the deliberate overwrite carries the SERVER's sha -----------------
    byId.cfOverwrite.fire("click");
    await settle();
    const put = lastWrite("PUT");
    ok("overwrite re-sends with the server's sha, never a blind star",
       put && put.body.ifMatch === "sha-note-2", put && JSON.stringify(put.body));
    ok("a write carries the token in a header and never in the query",
       put && put.headers["x-orch-token"] === "t0k" &&
       put.headers["x-touch-write"] === "1" &&
       put.headers["content-type"] === "application/json" &&
       put.search.indexOf("token=") === -1,
       put && JSON.stringify(put.headers) + " " + put.search);
    ok("the buffer is normalized to exactly one trailing newline on the wire",
       put && put.body.content === "note one\nmy paragraph\n",
       put && JSON.stringify(put.body.content));
    ok("saved renders the server's mtime, not the click",
       byId.edState.textContent.indexOf("saved · ") === 0 &&
       byId.edState.textContent.indexOf("unknown") === -1, byId.edState.textContent);

    // --- UI-3: the adopted sha means save-then-save does not 409 -----------
    const adopted = disk["note.md"].sha256;
    byId.edText.value = "note one\nmy paragraph\nand another";
    byId.edText.fire("input");
    await sandbox.memSave();
    await settle();
    const put2 = lastWrite("PUT");
    ok("a save adopts the sha the server returned",
       put2 && put2.body.ifMatch === adopted,
       put2 && put2.body.ifMatch + " vs " + adopted);
    ok("...and the second save lands", byId.edState.textContent.indexOf("saved · ") === 0,
       byId.edState.textContent);

    // --- a poll may ADD the drift line; it may not erase the confirmation ---
    // `saved · <mtime>` and a live error are the server's own last answer to the
    // operator. A 12 s poll that overwrote them with `clean`/`dirty` would erase
    // the one thing on screen that says the write landed.
    disk["note.md"].mtime_ns = 1700000070000000000;
    poll();
    await settle();
    ok("a poll that sees drift keeps the saved confirmation on the badge",
       byId.edState.textContent.indexOf("saved · ") === 0, byId.edState.textContent);
    ok("...and puts the drift line beside it, not over it",
       byId.edReason.textContent.indexOf("changed on disk") === 0, byId.edReason.textContent);

    // --- UI-18: the label WHILE the request is in flight -------------------
    // A save that says "saved" on the click and corrects itself from the
    // response looks identical once it settles. Hold the write and look.
    holdWrites = true;
    byId.edText.value = "note one\nmy paragraph\nand another\nand a third";
    byId.edText.fire("input");
    const inflight = sandbox.memSave();
    await settle();
    ok("a save in flight reads saving…, never saved",
       byId.edState.textContent === "saving…", byId.edState.textContent);
    ok("...and the save button is disabled while it is out",
       byId.edSave.disabled === true, byId.edSave.disabled);
    holdWrites = false;
    heldWrites.splice(0).forEach((release) => release());
    await settle();
    await inflight;
    ok("...and only the server's answer turns it into saved",
       byId.edState.textContent.indexOf("saved · ") === 0, byId.edState.textContent);

    // --- UI-7: delete moves to the trash group, restore is a create --------
    const before = Object.keys(disk).length;
    await sandbox.memDelete();
    await settle();
    ok("a delete moves the file into the trash group",
       byId.trashCard.hidden === false && Object.keys(disk).length === before - 1 &&
       byId.trashRows.textContent.indexOf("note.md") !== -1,
       byId.trashRows.textContent);
    await sandbox.memRestore("note.md");
    await settle();
    const post = lastWrite("POST");
    ok("restore re-creates the file as a POST with the bytes the page kept",
       post && post.name === "note.md" &&
       post.body.content === "note one\nmy paragraph\nand another\nand a third\n",
       post && JSON.stringify(post.body));

    // A restore whose name came back is refused, and the refusal renders on
    // that row rather than in a banner the next poll erases (UI-7, UI-18).
    await sandbox.memOpen("note.md");
    await settle();
    await sandbox.memDelete();
    await settle();
    queued.POST.push(jsonRes(409, { error: "a file of that name exists" }));
    await sandbox.memRestore("note.md");
    await settle();
    poll();
    await settle();
    ok("a refused restore renders on its own row and survives the poll",
       byId.trashRows.textContent.indexOf("restore refused") !== -1 &&
       byId.trashRows.textContent.indexOf("note.md") !== -1,
       byId.trashRows.textContent);

    // --- the buffer while a write is OUT (the mid-flight clobber) -----------
    // A locked textarea covers the keyboard; a paste landing in the same tick is
    // what this arm is. The response may not render "saved" over bytes the
    // server never received, and it may not clear the dirty gate with them —
    // that confirmation is the optimistic one D13 rule 1 forbids, and it makes
    // the operator's only copy of the edit silently discardable.
    await sandbox.memOpen("other.md");
    await settle();
    holdWrites = true;
    byId.edText.value = "other one\nsent bytes";
    byId.edText.fire("input");
    const racing = sandbox.memSave();
    await settle();
    ok("the buffer is locked while a write is out",
       byId.edText.disabled === true, byId.edText.disabled);
    byId.edText.value = "other one\nsent bytes\nTYPED WHILE SAVING";
    byId.edText.fire("input");
    ok("typing mid-flight does not knock the badge off saving…",
       byId.edState.textContent === "saving…", byId.edState.textContent);
    holdWrites = false;
    heldWrites.splice(0).forEach((release) => release());
    await settle();
    await racing;
    ok("bytes typed while the write was out are NOT declared saved",
       byId.edState.textContent === "unsaved changes" &&
       byId.edText.value.indexOf("TYPED WHILE SAVING") !== -1 &&
       disk["other.md"].content === "other one\nsent bytes\n",
       byId.edState.textContent + " / " + JSON.stringify(disk["other.md"].content));
    ok("...and the page says which bytes DID reach the disk",
       byId.edReason.textContent.indexOf("in flight are on disk") !== -1,
       byId.edReason.textContent);
    const bu = { returnValue: "", defaulted: false,
                 preventDefault() { this.defaulted = true; } };
    window.onbeforeunload(bu);
    ok("...and beforeunload still warns about them", bu.defaulted === true, bu.defaulted);

    // --- a refused DELETE stays a delete ------------------------------------
    // The 409 panel is shared with the save path. If it describes a refused save
    // and its third button saves, the operator asked to delete a file and got a
    // write of their buffer instead — a different mutation than the one clicked.
    const putsBefore = reqs.filter((r) => r.method === "PUT").length;
    const delsBefore = reqs.filter((r) => r.method === "DELETE").length;
    queued.DELETE.push(jsonRes(409, { error: "precondition failed",
        sha256: "sha-other-9", mtime_ns: 1700000060000000000, size: 31,
        content: "other, rewritten by the agent\n" }));
    await sandbox.memDelete();
    await settle();
    ok("a refused delete is described as a refused delete, not as a refused save",
       byId.edReason.textContent.indexOf("DELETE was refused") !== -1 &&
       byId.edReason.textContent.indexOf("NOT saved") === -1,
       byId.edReason.textContent);
    ok("...and the third exit is labelled as a delete, not as an overwrite",
       byId.cfOverwrite.textContent === "delete it anyway", byId.cfOverwrite.textContent);
    byId.cfOverwrite.fire("click");
    await settle();
    const dels = reqs.filter((r) => r.method === "DELETE");
    ok("...and taking it DELETES with the server's sha and saves nothing",
       reqs.filter((r) => r.method === "PUT").length === putsBefore &&
       dels.length === delsBefore + 2 &&
       dels[dels.length - 1].ifMatch === "sha-other-9" &&
       Object.prototype.hasOwnProperty.call(disk, "other.md") === false,
       "PUTs +" + (reqs.filter((r) => r.method === "PUT").length - putsBefore) +
       " DELETEs +" + (dels.length - delsBefore) + " ifMatch=" +
       dels[dels.length - 1].ifMatch + " onDisk=" +
       Object.prototype.hasOwnProperty.call(disk, "other.md"));

    // --- the deliberate delete exit acts on the bytes that are THERE ---------
    // "delete it anyway" removes the copy on disk NOW. If the page's idea of that
    // copy is still the stale load, it trashes the wrong byte-set and then offers
    // a one-click restore of the revision the other writer had already replaced —
    // unlabelled, and older. The 409 published the real bytes; they are what a
    // restore must write.
    disk["m1.md"] = { content: "the original bytes\n", sha256: "sha-m1-1",
                      mtime_ns: 1700000200000000000, hasFrontmatter: false };
    await sandbox.memRefreshList();
    await settle();
    await sandbox.memOpen("m1.md");
    await settle();
    const rewritten = "REWRITTEN by the agent\n";
    disk["m1.md"] = { content: rewritten, sha256: "sha-m1-9",
                      mtime_ns: 1700000210000000000, hasFrontmatter: false };
    queued.DELETE.push(jsonRes(409, { error: "precondition failed",
        sha256: "sha-m1-9", mtime_ns: 1700000210000000000,
        size: Buffer.byteLength(rewritten, "utf8"), content: rewritten }));
    await sandbox.memDelete();
    await settle();
    byId.cfOverwrite.fire("click");
    await settle();
    ok("delete it anyway removes the copy that is on disk NOW",
       Object.prototype.hasOwnProperty.call(disk, "m1.md") === false &&
       (lastWrite("DELETE") || {}).ifMatch === "sha-m1-9",
       "onDisk=" + Object.prototype.hasOwnProperty.call(disk, "m1.md") +
       " ifMatch=" + (lastWrite("DELETE") || {}).ifMatch);
    ok("...and the row names both byte-sets before either is written",
       byId.trashRows.textContent.indexOf("restore writes the") !== -1 &&
       byId.trashRows.textContent.indexOf("you had unsaved in the editor") !== -1,
       byId.trashRows.textContent);
    await sandbox.memRestore("m1.md");
    await settle();
    const back = lastWrite("POST");
    ok("...and restore writes the bytes that were DELETED, not the stale load",
       back && back.name === "m1.md" && back.body.content === rewritten,
       back && JSON.stringify(back.body.content));
    // ...and the edit that was discarded with the file is not gone either: it is
    // the second, separately named choice, never a silent substitution.
    await sandbox.memOpen("m1.md");
    await settle();
    byId.edText.value = "my unsaved edit\n";
    byId.edText.fire("input");
    await sandbox.memDelete();
    await settle();
    await sandbox.memRestore("m1.md", "buffer");
    await settle();
    const alt = lastWrite("POST");
    ok("the discarded edit is restorable as its own named choice",
       alt && alt.body.content === "my unsaved edit\n", alt && JSON.stringify(alt.body));
    delete disk["m1.md"];
    await sandbox.memRefreshList();
    await settle();

    // --- DOCS-14: the index budget, tuple for tuple with the repo gate --------
    // The old guard was `"<!--" in budget and '"---\n"' in budget`, which passed
    // for an implementation that disagreed with `tests/test_memory_hygiene.py` in
    // BOTH directions: it over-counted a `...`-terminated file (shouting about a
    // budget the file fits) and under-counted a line that merely STARTS with a
    // comment (silence about a file the CLI truncates in the model). Each case
    // below names the STRIPPED text, exactly as that test states its own cases,
    // and both numbers are derived from it.
    const budgetCases = [
        ["---\ntitle: x\n...\nbody line\n", "body line\n",
         "a `...` terminator closes frontmatter"],
        ["--- \ntitle: x\n---\nbody line\n", "body line\n",
         "trailing spaces after the fence still open frontmatter"],
        ["<!-- a --> real content\nmore\ntail\n", "<!-- a --> real content\nmore\ntail\n",
         "an inline comment before prose is CONTENT"],
        ["<!-- a --> real content\nmore\n<!-- b -->\ntail\n",
         "<!-- a --> real content\nmore\ntail\n",
         "...and it does not swallow the lines up to the next comment"],
        ["one\ntwo\nthree\n", "one\ntwo\nthree\n", "a plain file measures itself"],
        ["---\ntitle: x\nmodified: 2026-07-30T00:00:00Z\n---\none\ntwo\n", "one\ntwo\n",
         "leading frontmatter comes off"],
        ["one\n<!-- a note\nspanning lines -->\ntwo\n", "one\ntwo\n",
         "a block comment that owns its lines comes off"],
        ["one <!-- x --> two\n", "one <!-- x --> two\n", "an inline comment is counted"],
        ["text\n---\nmore\n", "text\n---\nmore\n", "a mid-file rule is not frontmatter"],
    ];
    const budgetBad = [];
    budgetCases.forEach((c) => {
        const got = sandbox.indexBudget(c[0]);
        const wantLines = c[1] === "" ? 0 : c[1].replace(/\n$/, "").split("\n").length;
        const wantBytes = Buffer.byteLength(c[1], "utf8");
        if (got.lines !== wantLines || got.bytes !== wantBytes) {
            budgetBad.push(c[2] + ": got " + JSON.stringify(got) + ", want {lines:"
                + wantLines + ",bytes:" + wantBytes + "}");
        }
    });
    ok("the index budget measures exactly what the repo gate measures",
       budgetBad.length === 0, budgetBad.join(" || "));
    const overLong = sandbox.indexBudget("x\n".repeat(201));
    ok("...and an over-long index measures over the 200-line limit",
       overLong.lines === 201, JSON.stringify(overLong));

    // --- m1: the in-flight lock has no door the page opens for itself ---------
    disk["lock.md"] = { content: "lock me\n", sha256: "sha-lock-1",
                        mtime_ns: 1700000300000000000, hasFrontmatter: false };
    await sandbox.memRefreshList();
    await settle();
    await sandbox.memOpen("lock.md");
    await settle();
    holdWrites = true;
    byId.edText.value = "lock me\nand a line";
    byId.edText.fire("input");
    const locked = sandbox.memSave();
    await settle();
    const kept = disk["lock.md"];
    delete disk["lock.md"];              // another writer removes it mid-flight
    poll();
    await settle();
    ok("a poll that cannot see the file does not release the in-flight lock",
       byId.edState.textContent === "saving…" && byId.edText.disabled === true,
       byId.edState.textContent + " disabled=" + byId.edText.disabled);
    disk["lock.md"] = kept;
    holdWrites = false;
    heldWrites.splice(0).forEach((release) => release());
    await settle();
    await locked;
    ok("...and the server's answer, not the poll, ends the write",
       byId.edState.textContent.indexOf("saved · ") === 0, byId.edState.textContent);

    // --- m2: rows the page could not read are not an observation --------------
    listStatus = 401;
    poll();
    await settle();
    ok("a 401 on the list raises the auth banner",
       byId.authBanner.hidden === false &&
       byId.authBanner.textContent.indexOf("token") !== -1,
       byId.authBanner.hidden + " / " + byId.authBanner.textContent);
    ok("...and does not leave save live over a directory the page cannot read",
       byId.edSave.disabled === true && byId.edDelete.disabled === true &&
       byId.edSave.title.indexOf("file list could not be read") !== -1,
       byId.edSave.disabled + " / " + byId.edSave.title);
    ok("...and nothing claims the open file vanished from the memory root",
       byId.edState.textContent.indexOf("gone from the memory root") === -1,
       byId.edState.textContent);
    listStatus = 200;
    await sandbox.memRefreshList();
    await settle();
    ok("...and a successful list makes the write affordances honest again",
       byId.edSave.disabled === false, byId.edSave.disabled);

    // --- m3: an abandoned write still reports what it did ---------------------
    // The rows are not disabled while a write is out, and `memOpen` bumps the
    // captured id — so the answer to the PUT the operator launched arrives for a
    // file that is no longer on screen. Dropping it leaves them with no way to
    // learn whether those bytes are on disk.
    holdWrites = true;
    byId.edText.value = "lock me\nabandoned bytes";
    byId.edText.fire("input");
    const abandoned = sandbox.memSave();
    await settle();
    await sandbox.memOpen("MEMORY.md");   // the operator moves on, mid-flight
    await settle();
    holdWrites = false;
    heldWrites.splice(0).forEach((release) => release());
    await settle();
    await abandoned;
    ok("the outcome of a write the operator navigated away from is not dropped",
       byId.memRows.textContent.indexOf("the save you launched from here landed") !== -1,
       byId.memRows.textContent);
    await sandbox.memOpen("lock.md");
    await settle();
    ok("...and reopening that file states it beside the badge, once",
       byId.edReason.textContent.indexOf("the save you launched from here landed") !== -1 &&
       byId.memRows.textContent.indexOf("the save you launched from here") === -1,
       byId.edReason.textContent + " || rows: " + byId.memRows.textContent);

    // --- m4: one decision, one dialog ----------------------------------------
    byId.edText.value = "lock me\nunsaved when leaving";
    byId.edText.fire("input");
    confirmAnswer = false;
    const stay = byId.backLink.fire("click");
    ok("a refused discard cancels the navigation off the page",
       stay.defaulted === true, stay.defaulted);
    const staying = { returnValue: "", defaulted: false,
                      preventDefault() { this.defaulted = true; } };
    window.onbeforeunload(staying);
    ok("...and beforeunload still warns while the operator stays",
       staying.defaulted === true, staying.defaulted);
    confirmAnswer = true;
    const leave = byId.backLink.fire("click");
    ok("a confirmed discard lets the link navigate",
       leave.defaulted !== true, leave.defaulted);
    const twice = { returnValue: "", defaulted: false,
                    preventDefault() { this.defaulted = true; } };
    window.onbeforeunload(twice);
    ok("...and the browser's own unload prompt does not ask it again",
       twice.defaulted === false, twice.defaulted);
    // The flag says "answered for THAT navigation". If the navigation never
    // happened, a fresh edit is a fresh unguarded buffer.
    byId.edText.value = "lock me\nunsaved when leaving\nand more";
    byId.edText.fire("input");
    const again = { returnValue: "", defaulted: false,
                    preventDefault() { this.defaulted = true; } };
    window.onbeforeunload(again);
    ok("...but a NEW edit after a navigation that never happened re-arms it",
       again.defaulted === true, again.defaulted);

    // --- G6: the default-off write plane, honestly ------------------------
    await sandbox.memOpen("MEMORY.md");
    await settle();
    ok("the index carries its load budget on screen",
       byId.edBudget.hidden === false && byId.edBudget.textContent.indexOf("/200 lines") !== -1,
       byId.edBudget.textContent);
    writePlane = false;
    poll();
    await settle();
    ok("the write plane being off disables save with its reason",
       byId.edSave.disabled === true &&
       byId.edSave.title.indexOf("write plane is off") !== -1 &&
       byId.edReason.textContent.indexOf("read-only") === 0,
       byId.edSave.disabled + " / " + byId.edSave.title + " / " + byId.edReason.textContent);
    ok("...and no create affordance renders while it is off",
       byId.createRow.hidden === true, byId.createRow.hidden);
    const spent = reqs.length;
    await sandbox.memSave();
    await settle();
    ok("...and a save attempt sends nothing at all", reqs.length === spent,
       reqs.length - spent);

    process.exit(failed === 0 ? 0 : 1);
})().catch((e) => { console.log("FAIL: the harness itself ran to completion — " + e.stack); process.exit(1); });
"""

#: Every label the harness must print as PASS. Listed here so a harness that
#: dies early, or an assertion that silently stops running, is a FAILURE rather
#: than a suite that "passed" because it asserted nothing.
MEM_HARNESS_EXPECTED = (
    "the list is read from the tokened list route",
    "the page names the absolute memory root it edits",
    "the aligned banner comes from the list route, not /health",
    "every file in the root gets a row",
    "a stale list answer landing last does not replace the newer rows",
    "a load fills the editor from the file route",
    "an out-of-order load does not fill the wrong editor",
    "an edit reads as unsaved changes",
    "the poll does not clobber a dirty buffer",
    "the poll says the file changed on disk",
    "a refused discard leaves the dirty buffer exactly where it was",
    "a 409 does not overwrite the operator's buffer",
    "...and the file on disk is untouched by the refusal",
    "a 409 renders as a conflict, not as saved",
    "a 409 offers reload, show both and overwrite — never a bare retry",
    "show both prints the disk copy without touching the buffer",
    "overwrite re-sends with the server's sha, never a blind star",
    "a write carries the token in a header and never in the query",
    "the buffer is normalized to exactly one trailing newline on the wire",
    "saved renders the server's mtime, not the click",
    "a save adopts the sha the server returned",
    "...and the second save lands",
    "a poll that sees drift keeps the saved confirmation on the badge",
    "...and puts the drift line beside it, not over it",
    "a save in flight reads saving…, never saved",
    "...and the save button is disabled while it is out",
    "...and only the server's answer turns it into saved",
    "the buffer is locked while a write is out",
    "typing mid-flight does not knock the badge off saving…",
    "bytes typed while the write was out are NOT declared saved",
    "...and the page says which bytes DID reach the disk",
    "...and beforeunload still warns about them",
    "a refused delete is described as a refused delete, not as a refused save",
    "...and the third exit is labelled as a delete, not as an overwrite",
    "...and taking it DELETES with the server's sha and saves nothing",
    "a delete moves the file into the trash group",
    "restore re-creates the file as a POST with the bytes the page kept",
    "a refused restore renders on its own row and survives the poll",
    "delete it anyway removes the copy that is on disk NOW",
    "...and the row names both byte-sets before either is written",
    "...and restore writes the bytes that were DELETED, not the stale load",
    "the discarded edit is restorable as its own named choice",
    "the index budget measures exactly what the repo gate measures",
    "...and an over-long index measures over the 200-line limit",
    "a poll that cannot see the file does not release the in-flight lock",
    "...and the server's answer, not the poll, ends the write",
    "a 401 on the list raises the auth banner",
    "...and does not leave save live over a directory the page cannot read",
    "...and nothing claims the open file vanished from the memory root",
    "...and a successful list makes the write affordances honest again",
    "the outcome of a write the operator navigated away from is not dropped",
    "...and reopening that file states it beside the badge, once",
    "a refused discard cancels the navigation off the page",
    "...and beforeunload still warns while the operator stays",
    "a confirmed discard lets the link navigate",
    "...and the browser's own unload prompt does not ask it again",
    "...but a NEW edit after a navigation that never happened re-arms it",
    "the index carries its load budget on screen",
    "the write plane being off disables save with its reason",
    "...and no create affordance renders while it is off",
    "...and a save attempt sends nothing at all",
)


def memory_driven():
    """UI-10: the one guard in this file that EXECUTES the page."""
    print("  memory.html: driving the page under node + vm")
    node = shutil.which("node") or ("/usr/bin/node"
                                    if os.path.exists("/usr/bin/node") else None)
    if node is None:
        print("  skip: no node on PATH — the source guards above still apply, but "
              "NOTHING in memory.html has been executed")
        return
    with open(MEMORY_HTML, encoding="utf-8") as fh:
        html = fh.read()
    # getElementById answers only for ids the document declares.
    ids = sorted(set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', html)))
    assert "edText" in ids and "memRows" in ids, \
        "UI-10: the harness needs the page's own id set — this one looks wrong"
    with tempfile.TemporaryDirectory() as tmp:
        driver = os.path.join(tmp, "drive-memory.js")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(MEM_HARNESS_JS)
        proc = subprocess.run([node, driver, MEMORY_HTML, json.dumps(ids)],
                              capture_output=True, text=True, timeout=180)
    passed = set()
    fails = []
    for line in proc.stdout.splitlines():
        if line.startswith("PASS: "):
            passed.add(line[6:])
        elif line.startswith("FAIL: "):
            fails.append(line[6:])
    assert not fails, "UI-10 (driven): " + " || ".join(fails)
    missing = [label for label in MEM_HARNESS_EXPECTED if label not in passed]
    assert not missing, \
        "UI-10 (driven): these assertions never ran — " + " || ".join(missing) \
        + (("\n" + proc.stderr.strip()[-2000:]) if proc.stderr.strip() else "")
    assert proc.returncode == 0, \
        f"UI-10: the harness itself must run to completion — rc {proc.returncode} " \
        f"{proc.stderr.strip().splitlines()[:2]}"


if __name__ == "__main__":
    sys.exit(main())
