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
import os
import re
import sys

HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "monitor.html")
PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "monitor_server.py")


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


if __name__ == "__main__":
    sys.exit(main())
