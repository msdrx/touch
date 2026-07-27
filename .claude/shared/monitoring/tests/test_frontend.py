#!/usr/bin/env python3
"""Static-source regression guards for monitor.html (sp-frontend fixes).

monitor.html is HTML/JS and is not executed by python3, so these tests assert on
the source text: that the fixed pattern is present and the vulnerable/buggy
pattern is absent. Run: `python3 test_frontend.py` (exits non-zero on failure).

Covers FRONTEND-1..5 and PROTOCOL-6 (frontend half).
"""
import os
import re
import sys

HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "monitor.html")


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

    print("test_frontend.py: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
