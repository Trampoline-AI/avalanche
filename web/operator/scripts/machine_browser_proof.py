"""Real workspace browser proof against a typed fake host (phase A).

Run from the web workspace after `pnpm install --frozen-lockfile`:
  uv run --no-project --python 3.13 --with playwright==1.55.0 python \
    operator/scripts/machine_browser_proof.py --evidence /path/to/evidence
Phase B reuses the browser scenario with a real operator URL/fixture after PR 2.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def wait_server(url: str, server: subprocess.Popen) -> None:
    for _ in range(200):
        if server.poll() is not None:
            raise RuntimeError("Browser harness server exited; see server.log")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Browser harness server did not become ready")


def start_run(
    page,
    pr: int,
    priority: int = 1,
    *,
    files: bool = False,
    evidence: Path | None = None,
) -> None:
    # The normal run form, opened from the graph's input dot like any flow.
    page.get_by_role("button", name="Inspect workflow input ReviewInput").click()
    inputs = page.get_by_role("complementary", name="Workflow input declaration")
    inputs.get_by_label("repo value", exact=True).fill("acme/api")
    inputs.get_by_label("pr value", exact=True).fill(str(pr))
    inputs.get_by_label("priority value", exact=True).fill(str(priority))
    if files:
        inputs.get_by_label("workspace value", exact=True).fill(
            '{"kind":"workspace","entries":[]}'
        )
        inputs.get_by_label("document file", exact=True).set_input_files(
            {"name": "proof.txt", "mimeType": "text/plain", "buffer": b"machine proof\n"}
        )
        expect(inputs.locator("[data-file-field]")).to_contain_text("proof.txt")
    if evidence is not None:
        page.screenshot(path=str(evidence / "00-run-input.png"))
    inputs.get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("button", name="Run", exact=True).click()


RERUN = re.compile(r"re-?run|run again|replay", re.IGNORECASE)

CARD_STYLE = """els => els.map(e => {
  const s = getComputedStyle(e);
  return [e.tagName, s.borderRadius, s.borderTopWidth, s.fontFamily, s.width].join('|');
})"""


def shared_canvas(page) -> dict:
    """Facts that must match between a flow and a machine: one canvas, one look."""
    shell = page.locator(".run-graph-shell")
    return {
        "viewport": shell.locator(".graph-viewport").count(),
        "background": shell.locator(".react-flow__background").count(),
        "navpanel": shell.locator(".navpanel").count(),
        "orientation": shell.get_by_role("group", name="Workflow orientation").count(),
        "cards": sorted(set(shell.locator(".node-card").evaluate_all(CARD_STYLE))),
        "input_dot": shell.locator(".node-input").count(),
        "timeline": page.locator(".workspace-timeline").count(),
        "log_pane": page.get_by_label("Run logs").count(),
    }


# Samples every rendered machine edge path (getPointAtLength, mapped to the screen)
# against every state card's DOMRect, and every arc label's rect against the cards.
ARC_GEOMETRY = """(tolerance) => {
  const shell = document.querySelector('.run-graph-shell');
  const boxes = [...shell.querySelectorAll('.react-flow__node-workflow')].map(node => ({
    id: node.getAttribute('data-id'), rect: node.getBoundingClientRect(),
  }));
  const inside = (x, y, r) => x > r.left + tolerance && x < r.right - tolerance
    && y > r.top + tolerance && y < r.bottom - tolerance;
  const overlap = (a, b) => a.left < b.right - tolerance && a.right > b.left + tolerance
    && a.top < b.bottom - tolerance && a.bottom > b.top + tolerance;
  const edges = [...shell.querySelectorAll('.react-flow__edge-machine')].map(edge => {
    const path = edge.querySelector('.react-flow__edge-path');
    const matrix = path.getScreenCTM();
    const length = path.getTotalLength();
    const points = [];
    for (let at = 0; at <= length; at += 1) {
      const p = path.getPointAtLength(at);
      points.push(new DOMPoint(p.x, p.y).matrixTransform(matrix));
    }
    return { id: edge.getAttribute('data-id'), points };
  });
  const arcBox = [];
  for (const edge of edges)
    for (const box of boxes)
      if (edge.points.some(p => inside(p.x, p.y, box.rect)))
        arcBox.push(`${edge.id} crosses ${box.id}`);
  const labels = [...shell.querySelectorAll('.machine-arc-label')].map(label => ({
    arc: label.getAttribute('data-arc-id'), text: label.textContent,
    rect: label.getBoundingClientRect(),
  }));
  const labelBox = [], labelLabel = [], labelArc = [];
  labels.forEach((label, index) => {
    for (const box of boxes)
      if (overlap(label.rect, box.rect)) labelBox.push(`label ${label.text} on ${box.id}`);
    for (const other of labels.slice(index + 1))
      if (overlap(label.rect, other.rect))
        labelLabel.push(`label ${label.text} on label ${other.text}`);
    // Another arc running through this label (its own arc passes under it by design).
    for (const edge of edges)
      if (edge.id !== label.arc && edge.points.some(p => inside(p.x, p.y, label.rect)))
        labelArc.push(`label ${label.text} on arc ${edge.id}`);
  });
  return { edges: edges.length, boxes: boxes.length, labels: labels.length,
           pairs: edges.length * boxes.length,
           label_pairs: labels.length * (labels.length - 1) / 2,
           arc_box: arcBox.length, label_box: labelBox.length,
           label_label: labelLabel.length, label_arc: labelArc.length,
           intersections: [...arcBox, ...labelBox, ...labelLabel, ...labelArc] };
}"""


def arc_geometry(page, name: str, expected_edges: int) -> dict:
    """Asserts no arc crosses a state card and no arc label touches a card, another
    label or another arc on the canvas."""
    graph = page.locator(".run-graph-shell")
    expect(graph.locator(".react-flow__edge-machine")).to_have_count(expected_edges)
    page.wait_for_timeout(300)
    result = page.evaluate(ARC_GEOMETRY, 1.5)
    assert result["boxes"] > 0 and result["edges"] == expected_edges, result
    assert result["intersections"] == [], (name, result)
    return {"view": name, **result}


def set_orientation(page, orientation: str) -> None:
    viewport = page.locator(".run-graph-shell .graph-viewport")
    if viewport.get_attribute("data-orientation") != orientation:
        page.get_by_role("button", name=f"Switch to {orientation} workflow").click()
    expect(viewport).to_have_attribute("data-orientation", orientation)


def arc_spacing(page, url: str, evidence: Path) -> list[dict]:
    """Both orientations of the harness machine, pr_lifecycle and the flow canvas."""
    checks = []
    page.set_viewport_size({"width": 1440, "height": 1100})
    # The harness machine (self-loop, back arc, timer) plus the input dot's edge.
    page.goto(url)
    for orientation in ("horizontal", "vertical"):
        set_orientation(page, orientation)
        checks.append(arc_geometry(page, f"harness-{orientation}", 5))
        page.screenshot(path=str(evidence / f"20-harness-machine-{orientation}.png"))
    # pr_lifecycle: a back arc out of a shared rank, three triggers into closed.
    page.goto(url + "?workflow=pr_lifecycle")
    expect(page.locator(".run-graph-shell .node-card")).to_have_count(4)
    for orientation in ("horizontal", "vertical"):
        set_orientation(page, orientation)
        checks.append(arc_geometry(page, f"pr_lifecycle-{orientation}", 7))
        labels = page.locator(".run-graph-shell .machine-arc-label").all_inner_texts()
        assert sorted(labels) == sorted(
            [
                "review_done_with_nits",
                "review_done",
                "amended",
                "merged",
                "closed_without_merge",
                "after 30d",
            ]
        ), labels
        page.screenshot(path=str(evidence / f"21-pr-lifecycle-{orientation}.png"))
    # The flow canvas in both orientations: its look is unchanged, and in the vertical
    # layout the input dot's label sits above the dot (its edge leaves below).
    page.goto(url)
    page.get_by_test_id("flow-nav").click()
    expect(page.locator(".run-graph-shell .node-card")).to_have_count(3)
    for orientation in ("horizontal", "vertical"):
        set_orientation(page, orientation)
        page.wait_for_timeout(300)
        dot = page.locator(".run-graph-shell .node-input-dot").bounding_box()
        label = page.locator(".run-graph-shell .node-input-label").bounding_box()
        if orientation == "vertical":
            assert label["y"] + label["height"] <= dot["y"], (label, dot)
        else:
            assert label["y"] >= dot["y"] + dot["height"], (label, dot)
        page.screenshot(path=str(evidence / f"22-flow-{orientation}.png"))
    page.get_by_test_id("machine-nav").click()
    set_orientation(page, "vertical")
    page.wait_for_timeout(300)
    dot = page.locator(".run-graph-shell .node-input-dot").bounding_box()
    label = page.locator(".run-graph-shell .node-input-label").bounding_box()
    assert label["y"] + label["height"] <= dot["y"], (label, dot)
    page.screenshot(path=str(evidence / "23-machine-vertical-input-label.png"))
    return checks


# Canvas items and controls under the floating timeline (or clipped by the canvas).
TIMELINE_COVER = """() => {
  const timeline = document.querySelector('.workspace-timeline').getBoundingClientRect();
  const shell = document.querySelector('.run-graph-shell');
  const hit = (r, o) => r.left < o.right && r.right > o.left && r.top < o.bottom
    && r.bottom > o.top;
  const items = [...shell.querySelectorAll('.react-flow__node, .machine-arc-label')];
  const covered = items.filter(el => hit(el.getBoundingClientRect(), timeline))
    .map(el => el.getAttribute('data-id') || el.textContent);
  // A control is covered when the timeline, not the control, receives its center.
  const controls = [...shell.querySelectorAll('.navpanel button, [role=group] button')]
    .filter(el => el.getBoundingClientRect().width > 0)
    .filter(el => {
      const r = el.getBoundingClientRect();
      const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return top && top.closest('.workspace-timeline');
    })
    .map(el => el.getAttribute('aria-label') || el.textContent);
  return { covered, controls };
}"""


def run_timeline(page):
    """The run's Timeline tab in the bottom pane (opened if Logs is showing)."""
    tab = page.get_by_role("tab", name="Timeline", exact=True)
    if tab.get_attribute("aria-selected") != "true":
        tab.click()
    timeline = page.get_by_role("region", name="Run timeline")
    expect(timeline).to_be_visible()
    return timeline


def open_visit(page, visit: int) -> None:
    """Opens a visit's steps from its timeline entry (select, then Steps)."""
    row = run_timeline(page).locator(f'[data-entry-id="v{visit}:enter"]')
    expect(row).to_be_visible()
    if row.locator("button[aria-pressed=true]").count() == 0:
        row.locator("button[aria-pressed]").click()
    row.get_by_role("button", name="Steps", exact=True).click()
    expect(page.get_by_role("navigation", name="Machine visit")).to_contain_text(
        f"Visit {visit}"
    )


def current_state(page):
    return page.locator('.run-graph-shell .node-card[data-current="true"]')


def history_reads(page, run_id: str) -> int:
    return page.evaluate(
        "(id) => window.machineHarness.calls"
        ".filter(c => c.method === 'history' && c.args[0] === id).length",
        run_id,
    )


def host_neutral(page, url: str, evidence: Path) -> list[dict]:
    """A host with its own vocabulary and a slow first snapshot, at both acceptance sizes.

    The timeline mounts after the host's loading fallback, so its sizing must attach
    late; `admitted`/`parked` are live only because the host declares them; receipt
    ids are 64 hex characters; machine reads follow the update stream, and poll only
    while the run is live, with backoff.
    """
    checks = []
    for tag, size in (
        ("wide", {"width": 1600, "height": 1100}),
        ("narrow", {"width": 1024, "height": 768}),
    ):
        page.set_viewport_size(size)
        page.goto(url + "?host&run=machine-1")
        expect(page.get_by_test_id("host-loading")).to_be_visible()
        timeline = page.locator(".workspace-timeline")
        expect(timeline).to_be_visible()
        graph = page.locator(".run-graph-shell")
        expect(graph.locator(".react-flow__edge-machine")).to_have_count(4)
        # The late-mounted timeline still bounds its height and reserves the graph fit.
        expect(timeline).to_have_attribute("style", re.compile(r"max-height"))
        page.wait_for_timeout(600)
        cover = page.evaluate(TIMELINE_COVER)
        assert cover == {"covered": [], "controls": []}, (tag, cover)
        geometry = arc_geometry(page, f"host-{tag}-run-horizontal", 4)
        # `admitted` is live on this host: no snapshot banner, Cancel offered.
        banner = page.get_by_text("Viewing a run snapshot", exact=True)
        expect(banner).to_have_count(0)
        expect(page.get_by_role("button", name="Cancel run", exact=True)).to_be_visible()
        page.screenshot(path=str(evidence / f"30-host-{tag}-late-timeline-live.png"))
        # No stream change on a live run: backoff reads (1 s, 2 s, 4 s ...), not 1 Hz.
        before = history_reads(page, "machine-1")
        page.wait_for_timeout(4500)
        live_polls = history_reads(page, "machine-1") - before
        assert live_polls <= 3, (tag, live_polls)

        # The host omits deferred events and receipts: their entries are left out,
        # with one neutral note in user words and no developer text.
        waits = run_timeline(page)
        note = waits.locator("[data-note]")
        expect(note).to_have_count(1)
        expect(note).to_have_text(
            "Deferred events and send receipts aren't listed for this run."
        )
        live_note = note.inner_text()
        expect(waits.locator('[data-timeline-kind="receipt"]')).to_have_count(0)
        expect(waits.locator('[data-pending-kind="deferred"]')).to_have_count(0)
        expect(page.get_by_text("not reported by this host")).to_have_count(0)
        expect(page.get_by_text("unavailable", exact=False)).to_have_count(0)
        expect(page.locator("[data-terminal-status]")).to_have_count(0)

        # A 64-hex receipt wraps inside the side panel instead of widening it.
        page.get_by_role("group", name="Machine actions").get_by_role(
            "button", name="Send event", exact=True
        ).click()
        panel = page.get_by_role("complementary", name="Send event")
        event_menu = panel.get_by_label("Event", exact=True)
        # The menu offers what the current state accepts, then what it defers.
        menu_before = event_menu.locator("option").all_inner_texts()
        assert menu_before == ["amend", "advance", "later (deferred)"], (tag, menu_before)
        form = panel.get_by_role("form", name="Send event")
        receipt = panel.locator("[data-receipt-status]")
        # A deferred event is acknowledged as accepted and leaves the run where it is.
        event_menu.select_option("later")
        form.get_by_role("button", name="Send event").click()
        expect(receipt).to_have_attribute("data-receipt-status", "accepted")
        expect(receipt).to_contain_text("· later · accepted")
        deferred_receipt = receipt.inner_text()
        assert event_menu.locator("option").all_inner_texts() == menu_before
        event_menu.select_option("advance")
        form.get_by_role("button", name="Send event").click()
        expect(receipt).to_contain_text("· advance · accepted")
        expect(receipt).to_contain_text("c" * 40)
        # After the transition the stale `advance` is gone: only `back` is offered.
        expect(event_menu).to_have_value("back")
        menu_after = event_menu.locator("option").all_inner_texts()
        assert menu_after == ["back"], (tag, menu_after)
        fit = panel.evaluate(
            """(aside) => {
              const box = aside.getBoundingClientRect();
              const line = aside.querySelector('[data-receipt-status]').getBoundingClientRect();
              const scrollers = [aside, ...aside.querySelectorAll('*')]
                .filter(el => el.scrollWidth > el.clientWidth + 1
                  && getComputedStyle(el).overflowX !== 'visible');
              return { panel: Math.round(box.width), line_right: Math.round(line.right),
                       panel_right: Math.round(box.right), overflowing: scrollers.length,
                       lines: Math.round(line.height / 15) };
            }"""
        )
        assert fit["line_right"] <= fit["panel_right"] and fit["overflowing"] == 0, (tag, fit)
        # `parked` (the machine waiting between visits) is still live on this host.
        expect(current_state(page)).to_contain_text("ready")
        # The deferred event shows in the timeline from the host's event facts.
        expect(
            waits.locator('[data-timeline-kind="event"][data-status="deferred"]')
        ).to_have_count(1)
        status = page.locator(".run-list-row.active [data-status]")
        expect(status).to_have_attribute("data-status", "parked")
        expect(banner).to_have_count(0)
        expect(page.get_by_role("button", name="Cancel run", exact=True)).to_be_visible()
        page.screenshot(path=str(evidence / f"31-host-{tag}-long-receipt.png"))
        panel.get_by_role("button", name="Close", exact=True).click()

        # The closed run is settled: banner, no Cancel, and no polling at all.
        page.locator(".run-list-row", has_text="acme/api #9").click()
        expect(banner).to_have_count(1)
        expect(page.get_by_role("button", name="Cancel run", exact=True)).to_have_count(0)
        # No seal from this host: the outcome comes from the settled run status, and
        # a settled run shows no deferred-events line.
        outcome = page.locator("[data-terminal-status]")
        expect(outcome).to_have_text("Terminal: succeeded")
        # Settled: no deferred-events wording, at most the receipts note.
        waits = run_timeline(page)
        expect(waits.locator("[data-note]")).to_have_text(
            "Send receipts aren't listed for this run."
        )
        expect(waits.locator("[data-pending-kind]")).to_have_count(0)
        expect(page.get_by_text("not reported by this host")).to_have_count(0)
        page.wait_for_timeout(500)
        before = history_reads(page, "machine-2")
        page.wait_for_timeout(3000)
        settled_polls = history_reads(page, "machine-2") - before
        assert settled_polls == 0, (tag, settled_polls)
        # A change on the update stream re-reads at once.
        page.evaluate("window.machineHarness.touch('machine-2')")
        page.wait_for_timeout(700)
        stream_reads = history_reads(page, "machine-2") - before
        assert stream_reads == 1, (tag, stream_reads)
        cover_settled = page.evaluate(TIMELINE_COVER)
        assert cover_settled == {"covered": [], "controls": []}, (tag, cover_settled)
        page.screenshot(path=str(evidence / f"32-host-{tag}-settled.png"))
        checks.append(
            {
                "view": f"host-{tag}",
                "late_timeline_covered": len(cover["covered"]),
                "late_timeline_controls_covered": len(cover["controls"]),
                "settled_covered": len(cover_settled["covered"]),
                "live_banner": 0,
                "settled_banner": 1,
                "live_history_reads_4_5s_no_change": live_polls,
                "settled_history_reads_3s": settled_polls,
                "stream_change_reads": stream_reads,
                "receipt": fit,
                "send_menu_before": menu_before,
                "deferred_send_receipt": deferred_receipt,
                "send_menu_after_transition": menu_after,
                "live_omitted_note": live_note,
                "settled_outcome": outcome.inner_text(),
                "geometry": geometry,
            }
        )
    page.set_viewport_size({"width": 1440, "height": 1100})
    return checks


def compose(page, evidence: Path, pair: tuple[str, str], out: str, captions: tuple) -> None:
    images = [base64.b64encode((evidence / name).read_bytes()).decode() for name in pair]
    page.set_viewport_size({"width": 2900, "height": 1140})
    page.set_content(
        "<body style='margin:0;display:flex;gap:20px;background:#222;padding:10px'>"
        + "".join(
            f"<figure style='margin:0'><img width=1440 src='data:image/png;base64,{data}'>"
            f"<figcaption style='color:#fff;font:14px sans-serif'>{caption}"
            "</figcaption></figure>"
            for data, caption in zip(images, captions)
        )
        + "</body>"
    )
    page.screenshot(path=str(evidence / out))


def scenario(page, url: str, evidence: Path) -> dict:
    page.goto(url)
    # The machine definition is drawn by the flow canvas: same cards, grid and controls.
    expect(page.locator(".run-graph-shell .node-card")).to_have_count(3)
    machine_look = shared_canvas(page)
    page.wait_for_timeout(300)
    page.screenshot(path=str(evidence / "side-machine.png"))
    page.get_by_test_id("flow-nav").click()
    expect(page.locator(".run-graph-shell .node-card")).to_have_count(3)
    flow_look = shared_canvas(page)
    page.wait_for_timeout(300)
    page.screenshot(path=str(evidence / "side-flow.png"))
    assert machine_look == flow_look, (machine_look, flow_look)
    expect(page.locator(".react-flow__edge-machine")).to_have_count(0)
    page.get_by_test_id("machine-nav").click()

    arcs = [arc_geometry(page, "definition-horizontal", 5)]
    start_run(page, 44, 2, files=True, evidence=evidence)
    actions = page.get_by_role("group", name="Machine actions")
    waits = run_timeline(page)
    selected_row = page.locator(".run-list-row.active")
    expect(waits).to_be_visible()
    # Visits and waits are no longer sections of the runs pane.
    expect(page.get_by_role("region", name="Machine visits")).to_have_count(0)
    expect(page.get_by_label("Run waits section")).to_have_count(0)
    expect(page.locator(".run-list-row")).to_have_count(1)
    # The key reads as fields, never raw JSON.
    expect(page.locator(".run-list-row")).to_contain_text("acme/api #44")
    expect(page.locator(".run-list-row")).not_to_contain_text("[")
    graph = page.locator(".run-graph-shell")
    expect(graph.locator('.node-card[data-current="true"]')).to_have_count(1)
    # One arc per trigger, never bundled.
    expect(graph.locator(".react-flow__edge-machine")).to_have_count(4)
    paths = graph.locator(".react-flow__edge-machine .react-flow__edge-path").evaluate_all(
        "els => els.map(e => e.getAttribute('d'))"
    )
    assert all(" C " in path for path in paths), paths
    labels = graph.locator(".machine-arc-label").all_inner_texts()
    assert sorted(label.split(" ·")[0].strip() for label in labels) == [
        "advance",
        "after 6s",
        "amend",
        "back",
    ], labels
    expect(graph.locator(".machine-arc--self")).to_have_count(1)
    expect(graph.locator(".machine-arc--back")).to_have_count(1)
    expect(graph.locator(".machine-arc--timer")).to_have_count(1)
    # The measured first frame is final: the viewport does not move after paint.
    viewport = graph.locator(".react-flow__viewport")
    first = viewport.get_attribute("style")
    page.wait_for_timeout(300)
    assert first == viewport.get_attribute("style"), "First-frame viewport moved"
    # Normal run controls stay on the machine run page; no instance table or refresh.
    expect(page.get_by_role("button", name="Cancel run", exact=True)).to_be_visible()
    # R3: no rerun of a machine run; retry is a new visit in the same run.
    expect(page.get_by_role("button", name=RERUN)).to_have_count(0)
    expect(page.get_by_role("table")).to_have_count(0)
    expect(page.get_by_role("button", name="Refresh instances")).to_have_count(0)
    # The deferred event, plus the events the current state accepts.
    expect(waits.locator('[data-wait-kind="event"]')).to_have_count(1)
    expect(waits.locator('[data-wait-kind="accepts"]')).to_have_count(1)
    arcs.append(arc_geometry(page, "run-horizontal", 4))
    page.screenshot(path=str(evidence / "01-machine-run.png"))

    actions.get_by_role("button", name="Retry visit", exact=True).click()
    expect(waits.locator('[data-entry-id="v2:enter"]')).to_be_visible()
    # Hovering an entry highlights its state on the graph.
    waits.locator('[data-entry-id="v2:enter"]').hover()
    expect(graph.locator(".react-flow__node.timeline-highlight")).to_have_count(1)
    page.screenshot(path=str(evidence / "01b-timeline-hover.png"))
    # A visit entry opens that visit's steps on the same canvas, like a flow run.
    open_visit(page, 2)
    expect(graph.locator(".react-flow__edge-machine")).to_have_count(0)
    expect(graph.locator(".react-flow__node")).to_have_count(3)
    expect(graph.get_by_label("Map items")).to_be_visible()
    expect(graph.get_by_role("button", name="Inspect v2.fanout[0]")).to_be_visible()
    detail = waits
    expect(detail.get_by_role("list", name="Pending")).to_contain_text("Approve visit 2?")
    page.screenshot(path=str(evidence / "02-visit-detail.png"))
    # Its steps open in the normal side inspector with real scoped detail and logs.
    graph.get_by_role("button", name="Inspect check", exact=True).click()
    inspector = page.get_by_role("complementary", name="Run inspector")
    expect(inspector.get_by_label("Execution attempts")).to_contain_text("3")
    expect(inspector).to_contain_text("score")
    # The step's logs are on the run pane's Logs tab, still scoped to the visit.
    page.get_by_role("tab", name="Logs").click()
    expect(page.get_by_text("visit 2 check finished")).to_be_visible()
    expect(page.get_by_role("navigation", name="Machine visit")).to_contain_text("Visit 2")
    run_look = {
        "timeline": page.locator(".workspace-timeline").count(),
        "graph": graph.locator(".graph-viewport").count(),
        "side_panel": page.locator(".workspace-inspector-pane").count(),
        "log_pane": page.get_by_label("Run logs").count(),
    }
    page.wait_for_timeout(300)
    page.screenshot(path=str(evidence / "side-machine-run.png"))
    page.screenshot(path=str(evidence / "03-visit-step.png"))
    inspector.get_by_role("button", name="Close", exact=True).click()
    # The first, interrupted visit keeps its own graph and paged event facts.
    run_timeline(page)
    open_visit(page, 1)
    expect(graph.locator(".react-flow__node")).to_have_count(1)
    detail.get_by_role("button", name="More events of visit 1").click()
    expect(detail.locator('[data-visit="1"][data-status="ignored"]')).to_be_visible()
    expect(detail.locator('[data-visit="1"][data-status="dropped"]')).to_be_visible()
    page.get_by_role("navigation", name="Machine visit").get_by_role(
        "button", name="State machine"
    ).click()
    expect(graph.locator(".react-flow__edge-machine")).to_have_count(4)

    # Send commits but loses its first response. The UI retry keeps its idempotency key.
    actions.get_by_role("button", name="Send event", exact=True).click()
    send_panel = page.get_by_role("complementary", name="Send event")
    send = send_panel.get_by_role("form", name="Send event")
    # R2: the run input form's own fields, File picker included, upload path included.
    send.get_by_label("note value", exact=True).fill("revise")
    send.get_by_label("score value", exact=True).fill("3")
    send.get_by_label("approved value", exact=True).fill("true")
    send.get_by_label("workspace value", exact=True).fill(
        '{"kind":"workspace","version":1,"reference":"proof"}'
    )
    send.get_by_label("evidence file", exact=True).set_input_files(
        {"name": "evidence.txt", "mimeType": "text/plain", "buffer": b"send proof\n"}
    )
    expect(send.locator('[data-file-status="staged"]')).to_contain_text("evidence.txt")
    page.screenshot(path=str(evidence / "13-send-form-file.png"))
    send.get_by_role("button", name="Send event").click()
    expect(send.get_by_role("alert")).to_be_visible()
    send.get_by_role("button", name="Send event").click()
    expect(send.get_by_role("alert")).to_have_count(0)
    expect(waits.locator('[data-entry-id="v3:enter"]')).to_be_visible()
    sends = page.evaluate("window.machineHarness.calls.filter(c => c.method === 'send')")
    assert len(sends) == 2 and sends[0]["args"][3] == sends[1]["args"][3], sends
    assert sends[0]["args"][2] == {
        "note": "revise",
        "score": 3,
        "approved": True,
        "workspace": {"kind": "workspace", "version": 1, "reference": "proof"},
    }, sends
    sent_files = [{"field": "evidence", "name": "evidence.txt", "bytes": list(b"send proof\n")}]
    assert sends[0]["args"][4] == sends[1]["args"][4] == sent_files, sends
    stages = page.evaluate("window.machineHarness.calls.filter(c => c.method === 'stage')")
    assert ["review", "evidence", "amend", "evidence.txt", "send proof\n"] in [
        stage["args"] for stage in stages
    ], stages
    expect(send_panel.locator('[data-receipt-status="accepted"]')).to_be_visible()
    expect(send_panel.locator('[data-receipt-duplicate="true"]')).to_have_count(1)
    expect(
        waits.locator('[data-timeline-kind="event"]', has_text=re.compile(r"receipt \S+ · key"))
    ).to_have_count(1)
    page.screenshot(path=str(evidence / "04-send-receipt.png"))
    # The visit that just ended keeps its terminal decision audit.
    expect(waits.locator('[data-timeline-kind="wait-resolved"][data-visit="2"]')).to_have_count(
        1
    )
    # Clicking a state on the machine graph filters the timeline to that state.
    graph.get_by_role("button", name="Inspect review", exact=True).click()
    expect(waits.locator(".run-timeline-filter")).to_contain_text("state review")
    states = waits.locator("[data-entry-id]").evaluate_all(
        "els => els.map(e => [e.dataset.state, e.dataset.entryId])"
    )
    assert states and all(
        state == "review" or "enter" in entry or "leave" in entry for state, entry in states
    ), states
    page.screenshot(path=str(evidence / "04b-state-filter.png"))
    waits.get_by_role("button", name="Show all", exact=True).click()

    # The timer advances in the host without a click; the arc counts down too.
    send_panel.get_by_label("Event", exact=True).select_option("advance")
    send.get_by_role("button", name="Send event").click()
    expect(current_state(page)).to_contain_text("ready")
    timer = waits.get_by_role("list", name="Pending").locator("[data-timer-remaining]")
    arc = graph.locator("[data-arc-remaining]")
    before = int(timer.get_attribute("data-timer-remaining"))
    arc_before = int(arc.get_attribute("data-arc-remaining"))
    page.wait_for_timeout(1100)
    assert int(timer.get_attribute("data-timer-remaining")) < before
    assert int(arc.get_attribute("data-arc-remaining")) < arc_before
    page.screenshot(path=str(evidence / "05-timer.png"))
    expect(current_state(page)).to_contain_text("closed", timeout=12000)
    expect(waits.locator('[data-timeline-kind="timer-fired"]')).to_have_count(1)
    expect(page.locator(".run-list-row")).to_have_count(1)
    expect(actions).to_have_count(0)
    page.screenshot(path=str(evidence / "06-final.png"))

    # Same-key start returns the existing run; visits never become run-list entries.
    page.get_by_role("button", name="Current definition", exact=True).click()
    start_run(page, 44, 2)
    expect(page.locator(".run-list-row")).to_have_count(1)
    expect(selected_row).to_contain_text("acme/api #44")
    # Another instance exercises the dev-only timer control.
    page.get_by_role("button", name="Current definition", exact=True).click()
    start_run(page, 45, 1)
    expect(selected_row).to_contain_text("acme/api #45")
    actions.get_by_role("button", name="Retry visit", exact=True).click()
    actions.get_by_role("button", name="Send event", exact=True).click()
    send_panel.get_by_label("Event", exact=True).select_option("advance")
    send.get_by_role("button", name="Send event").click()
    expect(current_state(page)).to_contain_text("ready")
    actions.get_by_role("button", name="Fire next timer", exact=True).click()
    expect(current_state(page)).to_contain_text("closed")
    # A third instance closes through the normal run cancel.
    page.get_by_role("button", name="Current definition", exact=True).click()
    start_run(page, 46, 1)
    expect(selected_row).to_contain_text("acme/api #46")
    page.get_by_role("button", name="Cancel run", exact=True).click()
    expect(current_state(page)).to_contain_text("closed")
    expect(page.locator(".run-list-row").first).to_contain_text("cancelled", ignore_case=True)
    expect(page.get_by_role("button", name="Cancel run", exact=True)).to_have_count(0)
    page.screenshot(path=str(evidence / "07-cancelled.png"))
    # A transient read failure is surfaced and recoverable, not a blank page.
    # The run is settled, so nothing polls; a change on the update stream re-reads.
    page.evaluate("window.machineHarness.failNextRead()")
    page.evaluate("window.machineHarness.touch('machine-3')")
    expect(waits.get_by_role("alert").first).to_be_visible(timeout=4000)
    page.evaluate("window.machineHarness.recoverRead()")
    waits.get_by_role("button", name="Retry read", exact=True).first.click()
    expect(waits.get_by_role("button", name="Retry read")).to_have_count(0, timeout=4000)
    calls = page.evaluate("window.machineHarness.calls")
    assert not [call for call in calls if call["method"].startswith("flow-")], calls
    assert [call["args"] for call in calls if call["method"] == "cancel"] == [["machine-3"]]
    starts = [call for call in calls if call["method"] == "run-start"]
    assert len(starts) == 4, starts
    assert starts[0]["args"][1] == {
        "repo": "acme/api",
        "pr": 44,
        "priority": 2,
        "workspace": {"kind": "workspace", "entries": []},
    }, starts
    assert starts[0]["args"][2] == [
        {"field": "document", "name": "proof.txt", "bytes": list(b"machine proof\n")}
    ], starts
    assert any(call["method"] == "baseline" for call in calls), calls
    (evidence / "calls.json").write_text(json.dumps(calls, indent=2) + "\n")

    # A keyed workflow run on the same page: same timeline, canvas, side inspector,
    # logs, and its key as the run label exactly like a machine run.
    page.get_by_test_id("flow-nav").click()
    page.get_by_role("button", name="Inspect workflow input DocumentInput").click()
    flow_inputs = page.get_by_role("complementary", name="Workflow input declaration")
    flow_inputs.get_by_label("id value", exact=True).fill("doc-9")
    flow_inputs.get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("button", name="Run", exact=True).click()
    expect(page.locator(".run-list-row")).to_have_count(1)
    expect(page.locator(".run-list-row")).to_contain_text("doc-9")
    expect(page.get_by_role("button", name=RERUN)).to_have_count(0)
    expect(page.locator(".react-flow__edge-machine")).to_have_count(0)
    expect(graph.locator(".node-card")).to_have_count(3)
    # The run pane keeps the tab last shown (Timeline): its steps are listed there too.
    expect(run_timeline(page).locator('[data-timeline-kind="step-started"]').first).to_be_visible()
    page.get_by_role("tab", name="Logs").click()
    expect(page.get_by_text("fetched document")).to_be_visible()
    graph.get_by_role("button", name="Inspect score", exact=True).click()
    expect(page.get_by_role("complementary", name="Run inspector")).to_contain_text("step")
    flow_run_look = {
        "timeline": page.locator(".workspace-timeline").count(),
        "graph": graph.locator(".graph-viewport").count(),
        "side_panel": page.locator(".workspace-inspector-pane").count(),
        "log_pane": page.get_by_label("Run logs").count(),
    }
    assert flow_run_look == run_look, (flow_run_look, run_look)
    page.wait_for_timeout(300)
    page.screenshot(path=str(evidence / "side-flow-run.png"))
    page.screenshot(path=str(evidence / "08-flow-run.png"))
    page.screenshot(path=str(evidence / "15-keyed-flow-run.png"))

    # No optional methods means genuinely read-only; no mutation request is made.
    page.goto(url + "?readonly")
    expect(page.get_by_role("tab", name="Timeline", exact=True)).to_be_visible()
    expect(page.locator('.node-card[data-current="true"]')).to_have_count(1)
    expect(page.get_by_role("group", name="Machine actions")).to_have_count(0)
    expect(page.get_by_role("button", name="Cancel run", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Current definition", exact=True).click()
    expect(page.get_by_role("button", name="Run", exact=True)).to_have_count(0)
    writes = page.evaluate(
        "window.machineHarness.calls.filter(c => "
        "['run-start','send','retry','cancel','fire','flow-start','flow-cancel']"
        ".includes(c.method))"
    )
    assert not writes, writes
    page.locator(".run-list-row").click()
    page.screenshot(path=str(evidence / "09-read-only.png"))
    # R5: expired history reads back `expired`; the run and its key label stay.
    page.goto(url + "?expired")
    row = page.locator(".run-list-row")
    expect(row).to_have_count(1)
    expect(row).to_have_attribute("data-run-history", "expired")
    expect(row).to_contain_text("acme/api #7")
    expect(row).to_contain_text("expired")
    badge = page.locator('.run-graph-shell [data-run-history="expired"]')
    expect(badge).to_be_visible()
    expect(page.get_by_role("group", name="Machine actions")).to_have_count(0)
    expect(page.get_by_role("button", name=RERUN)).to_have_count(0)
    # The definition graph is not history: the state machine still draws.
    expect(page.locator('.run-graph-shell .node-card[data-current="true"]')).to_contain_text(
        "closed"
    )
    page.wait_for_timeout(300)
    page.screenshot(path=str(evidence / "14-expired-machine-run.png"))
    open_visit(page, 1)
    expect(page.get_by_role("navigation", name="Machine visit")).to_contain_text("expired")
    # The whole run's history expired, so the badge names the run, not the visit.
    expect(badge).to_contain_text("This run's")
    expect(page.locator(".run-graph-shell .react-flow__edge-machine")).to_have_count(0)
    page.screenshot(path=str(evidence / "14b-expired-visit.png"))
    page.get_by_test_id("flow-nav").click()
    flow_row = page.locator('.run-list-row[data-run-history="expired"]')
    expect(flow_row).to_contain_text("doc-3")
    flow_row.click()
    expect(badge).to_be_visible()
    expect(page.locator(".run-list-row.active")).to_contain_text("doc-3")
    page.screenshot(path=str(evidence / "16-expired-keyed-flow-run.png"))
    expired_calls = page.evaluate("window.machineHarness.calls")
    assert not [
        call
        for call in expired_calls
        if call["method"] in ("waits", "facts", "logs") and call["args"][0] == "machine-1"
    ], expired_calls

    # Deep-link failure is explicit, not a fallback to an unrelated run.
    page.goto(url + "?run=missing")
    expect(page.get_by_role("alert").last).to_be_visible()
    page.screenshot(path=str(evidence / "10-missing-run.png"))
    compose(
        page,
        evidence,
        ("side-flow-run.png", "side-machine-run.png"),
        "11-flow-run-vs-machine-run.png",
        ("Workflow run", "Machine run"),
    )
    compose(
        page,
        evidence,
        ("side-flow.png", "side-machine.png"),
        "12-flow-vs-machine-definition.png",
        ("Workflow definition", "Machine definition"),
    )
    arcs += arc_spacing(page, url, evidence)
    host = host_neutral(page, url, evidence)
    return {
        "host_neutral": host,
        "machine_canvas": machine_look,
        "flow_canvas": flow_look,
        "run_page": run_look,
        "arc_geometry": arcs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--browser", choices=["chromium", "firefox", "all"], default="all")
    parser.add_argument("--url", help="Already-served harness URL; do not start a Vite server")
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = args.url or f"http://127.0.0.1:{port}/scripts/machine-harness.html"
    results = {}
    with (args.evidence / "server.log").open("w") as log:
        server = (
            None
            if args.url
            else subprocess.Popen(
                [
                    "pnpm",
                    "exec",
                    "vite",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--strictPort",
                ],
                cwd=root,
                stdout=log,
                stderr=subprocess.STDOUT,
                # Own process group: pnpm forks vite, and both must stop afterwards.
                start_new_session=True,
            )
        )
        try:
            if server is not None:
                wait_server(url, server)
            with sync_playwright() as playwright:
                for name in (
                    ["chromium", "firefox"] if args.browser == "all" else [args.browser]
                ):
                    evidence = args.evidence / name
                    evidence.mkdir(exist_ok=True)
                    browser = getattr(playwright, name).launch(headless=True)
                    page = browser.new_page(
                        viewport={"width": 1440, "height": 1100}, reduced_motion="reduce"
                    )
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    try:
                        canvas = scenario(page, url, evidence)
                        assert not errors, errors
                        results[name] = {
                            "status": "passed",
                            "shared_canvas": canvas,
                            "page_errors": errors,
                            "screenshots": sorted(
                                str(path) for path in evidence.glob("[0-9][0-9]-*.png")
                            ),
                        }
                        (args.evidence / "results.json").write_text(
                            json.dumps(results, indent=2) + "\n"
                        )
                    except Exception:
                        page.screenshot(path=str(evidence / "failure.png"), full_page=True)
                        (evidence / "page-errors.json").write_text(json.dumps(errors, indent=2))
                        raise
                    finally:
                        browser.close()
        finally:
            if server is not None:
                os.killpg(server.pid, signal.SIGTERM)
                server.wait(timeout=15)
    (args.evidence / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
