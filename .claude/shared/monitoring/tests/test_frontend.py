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

    # ---- ARTIFACTS-4: row placed after loop cards, before the orchestrator card ----
    place = _slice(src, "function placeArtifacts", "function renderArtifacts")
    assert "insertBefore(artifactsEl" in place, \
        "ARTIFACTS-4: placeArtifacts must insert the row before the orchestrator card"
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

    print("test_frontend.py: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
