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
    page, key: str, priority: int = 1, *, files: bool = False, evidence: Path | None = None
) -> None:
    # The normal run form, opened from the graph's input dot like any flow.
    page.get_by_role("button", name="Inspect workflow input ReviewInput").click()
    inputs = page.get_by_role("complementary", name="Workflow input declaration")
    inputs.get_by_label("id value", exact=True).fill(key)
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
        page.screenshot(path=str(evidence / "00-run-input.png"), full_page=True)
    inputs.get_by_role("button", name="Close", exact=True).click()
    page.get_by_role("button", name="Run", exact=True).click()


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
    }


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

    start_run(page, "proof-1", 2, files=True, evidence=evidence)
    expect(page.locator('[data-machine-run="machine-1"]')).to_be_visible()
    expect(page.locator(".run-list-row")).to_have_count(1)
    expect(page.locator(".run-list-row")).to_contain_text("proof-1")
    graph = page.locator(".run-graph-shell")
    expect(graph.locator('.node-card[data-current="true"]')).to_have_count(1)
    expect(graph.locator(".react-flow__edge-machine")).to_have_count(4)
    paths = graph.locator(".react-flow__edge-machine .react-flow__edge-path").evaluate_all(
        "els => els.map(e => e.getAttribute('d'))"
    )
    assert all(" C " in path for path in paths), paths
    labels = graph.locator(".react-flow__edge").evaluate_all(
        "els => els.map(e => e.getAttribute('aria-label') || '')"
    )
    assert any(label.startswith("review to review") for label in labels), labels
    assert any(label.startswith("ready to review") for label in labels), labels
    expect(graph.locator(".machine-arc--timer")).to_have_count(1)
    # The measured first frame is final: the viewport does not move after paint.
    viewport = graph.locator(".react-flow__viewport")
    first = viewport.get_attribute("style")
    page.wait_for_timeout(300)
    assert first == viewport.get_attribute("style"), "First-frame viewport moved"
    # Normal run controls stay on the machine run page.
    expect(page.get_by_role("button", name="Cancel run", exact=True)).to_be_visible()
    page.screenshot(path=str(evidence / "01-interrupted.png"))

    page.get_by_role("button", name="Retry visit", exact=True).click()
    expect(page.locator('[data-visit="2"]')).to_be_visible()
    # Visit 2 carries real, host-scoped step detail in the shared inspector.
    visit = page.locator('[data-visit="2"]')
    visit.get_by_role("button", expanded=False).click()
    workspace = visit.get_by_role("region", name="Visit 2 step workspace")
    expect(workspace.locator(".react-flow__node")).to_have_count(3)
    expect(workspace.get_by_label("Map items")).to_be_visible()
    expect(workspace.get_by_role("button", name="Inspect v2.fanout[0]")).to_be_visible()
    workspace.get_by_role("button", name="Inspect check", exact=True).click()
    expect(workspace.get_by_label("Execution attempts")).to_contain_text("3")
    workspace.get_by_role("tab", name="Run I/O").click()
    expect(workspace.get_by_role("complementary")).to_contain_text("score")
    expect(visit.get_by_text("visit 2 check finished")).to_be_visible()
    expect(visit.get_by_role("region", name="Run waits")).to_contain_text("Approve visit 2?")
    visit.scroll_into_view_if_needed()
    page.screenshot(path=str(evidence / "02-visit-steps.png"))
    # The archived first visit keeps its own graph and paged event facts.
    first_visit = page.locator('[data-visit="1"]')
    first_visit.get_by_role("button", expanded=False).click()
    expect(
        first_visit.get_by_role("region", name="Visit 1 step workspace").locator(
            ".react-flow__node"
        )
    ).to_have_count(1)
    expect(first_visit.locator("[data-event-status]")).to_have_count(0)
    first_visit.get_by_role("button", name="Next facts").click()
    expect(first_visit.locator('[data-event-status="ignored"]')).to_be_visible()
    expect(first_visit.locator('[data-event-status="dropped"]')).to_be_visible()
    # Clicking a state on the graph opens its latest visit.
    graph.get_by_role("button", name="Inspect review", exact=True).click()

    # Send commits but loses its first response. The UI retry keeps its idempotency key.
    page.locator("details summary").click()
    send = page.get_by_role("form", name="Send event")
    send.get_by_label("note", exact=False).fill("revise")
    send.get_by_label("score", exact=False).fill("3")
    send.get_by_label("approved", exact=False).select_option("true")
    send.get_by_role("button", name="Send event").click()
    expect(send.get_by_role("alert")).to_be_visible()
    send.get_by_role("button", name="Send event").click()
    expect(send.get_by_role("alert")).to_have_count(0)
    expect(page.locator('[data-visit="3"]')).to_be_visible()
    sends = page.evaluate("window.machineHarness.calls.filter(c => c.method === 'send')")
    assert len(sends) == 2 and sends[0]["args"][3] == sends[1]["args"][3], sends
    assert sends[0]["args"][2] == {"note": "revise", "score": 3, "approved": True}, sends
    expect(page.locator('[data-receipt-status="accepted"]')).to_be_visible()
    expect(page.locator('[data-receipt-duplicate="true"]')).to_have_count(1)
    # The visit that just ended shows its terminal decision audit.
    expect(visit.get_by_role("region", name="Decision audit")).to_be_visible()
    page.screenshot(path=str(evidence / "03-send-receipt.png"))

    # The timer advances in the host without a click; the arc counts down too.
    page.get_by_label("Event", exact=True).select_option("advance")
    page.get_by_role("form", name="Send event").get_by_role("button", name="Send event").click()
    expect(page.locator('[data-machine-state="ready"]')).to_be_visible()
    expect(graph.locator('.node-card[data-current="true"]')).to_contain_text("ready")
    timer = page.locator("[data-timer-remaining]")
    arc = graph.locator("[data-arc-remaining]")
    before = int(timer.get_attribute("data-timer-remaining"))
    arc_before = int(arc.get_attribute("data-arc-remaining"))
    page.wait_for_timeout(1100)
    assert int(timer.get_attribute("data-timer-remaining")) < before
    assert int(arc.get_attribute("data-arc-remaining")) < arc_before
    page.locator(".machine-run-sections").evaluate("el => el.scrollTop = 0")
    page.screenshot(path=str(evidence / "04-timer.png"))
    expect(page.locator('[data-machine-state="closed"]')).to_be_visible(timeout=12000)
    expect(page.locator(".run-list-row")).to_have_count(1)
    expect(page.get_by_role("region", name="Machine actions")).to_have_count(0)
    page.screenshot(path=str(evidence / "05-final.png"))

    # Same-key start returns the existing run; visits never become run-list entries.
    page.get_by_role("button", name="Current definition", exact=True).click()
    start_run(page, "proof-1", 2)
    expect(page.locator('[data-machine-run="machine-1"]')).to_be_visible()
    expect(page.locator(".run-list-row")).to_have_count(1)
    # Another instance exercises the dev-only timer control.
    page.get_by_role("button", name="Current definition", exact=True).click()
    start_run(page, "manual-timer", 1)
    expect(page.locator('[data-machine-run="machine-2"]')).to_be_visible()
    page.get_by_role("button", name="Retry visit", exact=True).click()
    page.locator("details summary").click()
    page.get_by_label("Event", exact=True).select_option("advance")
    page.get_by_role("form", name="Send event").get_by_role("button", name="Send event").click()
    expect(page.locator('[data-machine-state="ready"]')).to_be_visible()
    page.get_by_role("button", name="Fire next timer", exact=True).click()
    expect(page.locator('[data-machine-state="closed"]')).to_be_visible()
    # A third instance closes through the normal run cancel.
    page.get_by_role("button", name="Current definition", exact=True).click()
    start_run(page, "cancel-proof", 1)
    expect(page.locator('[data-machine-run="machine-3"]')).to_be_visible()
    page.get_by_role("button", name="Cancel run", exact=True).click()
    expect(page.locator('[data-machine-state="closed"]')).to_be_visible()
    expect(page.locator(".run-list-row").first).to_contain_text("cancelled", ignore_case=True)
    expect(page.get_by_role("button", name="Cancel run", exact=True)).to_have_count(0)
    page.screenshot(path=str(evidence / "06-cancelled.png"))
    # A transient read failure is surfaced and recoverable, not a blank canvas.
    page.evaluate("window.machineHarness.failNextRead()")
    expect(page.get_by_role("alert").first).to_be_visible(timeout=4000)
    page.get_by_role("button", name="Retry read", exact=True).first.click()
    page.evaluate("window.machineHarness.recoverRead()")
    expect(page.get_by_role("button", name="Retry read")).to_have_count(0, timeout=4000)
    calls = page.evaluate("window.machineHarness.calls")
    assert not [call for call in calls if call["method"].startswith("flow-")], calls
    assert [call["args"] for call in calls if call["method"] == "cancel"] == [["machine-3"]]
    starts = [call for call in calls if call["method"] == "run-start"]
    assert len(starts) == 4, starts
    assert starts[0]["args"][1] == {
        "id": "proof-1",
        "priority": 2,
        "workspace": {"kind": "workspace", "entries": []},
    }, starts
    assert starts[0]["args"][2] == [
        {"field": "document", "name": "proof.txt", "bytes": list(b"machine proof\n")}
    ], starts
    assert any(call["method"] == "baseline" for call in calls), calls
    (evidence / "calls.json").write_text(json.dumps(calls, indent=2) + "\n")
    # The ordinary flow keeps its normal DAG with no machine arcs.
    page.get_by_test_id("flow-nav").click()
    expect(page.locator(".react-flow__edge-machine")).to_have_count(0)
    expect(page.locator(".react-flow__node")).to_have_count(4)
    page.screenshot(path=str(evidence / "07-ordinary-flow.png"))
    # No optional methods means genuinely read-only; no mutation request is made.
    page.goto(url + "?readonly")
    expect(page.locator('[data-machine-run="machine-1"]')).to_be_visible()
    expect(page.locator('.node-card[data-current="true"]')).to_have_count(1)
    expect(page.get_by_role("region", name="Machine actions")).to_have_count(0)
    expect(page.get_by_role("button", name="Cancel run", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Current definition", exact=True).click()
    expect(page.get_by_role("button", name="Run", exact=True)).to_have_count(0)
    writes = page.evaluate(
        "window.machineHarness.calls.filter(c => "
        "['run-start','send','retry','cancel','fire','flow-start','flow-cancel'].includes(c.method))"
    )
    assert not writes, writes
    page.locator(".run-list-row").click()
    page.screenshot(path=str(evidence / "08-read-only.png"))
    # Deep-link failure is explicit, not a fallback to an unrelated run.
    page.goto(url + "?run=missing")
    expect(page.get_by_role("alert").last).to_be_visible()
    page.screenshot(path=str(evidence / "09-missing-run.png"))
    # One image, flow left and machine right, to compare the shared look.
    images = [
        base64.b64encode((evidence / name).read_bytes()).decode()
        for name in ("side-flow.png", "side-machine.png")
    ]
    page.set_viewport_size({"width": 2900, "height": 1140})
    page.set_content(
        "<body style='margin:0;display:flex;gap:20px;background:#222;padding:10px'>"
        + "".join(
            f"<figure style='margin:0'><img width=1440 src='data:image/png;base64,{data}'>"
            f"<figcaption style='color:#fff;font:14px sans-serif'>{caption}"
            "</figcaption></figure>"
            for data, caption in zip(images, ("Flow", "Machine"))
        )
        + "</body>"
    )
    page.screenshot(path=str(evidence / "10-flow-vs-machine.png"))
    return {"machine_canvas": machine_look, "flow_canvas": flow_look}


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
                server.terminate()
                server.wait(timeout=15)
    (args.evidence / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
