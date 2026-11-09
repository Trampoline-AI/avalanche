"""Real workspace browser proof against a typed fake host (phase A).

Run from the web workspace after `pnpm install --frozen-lockfile`:
  uv run --no-project --python 3.13 --with playwright==1.55.0 python \
    operator/scripts/machine_browser_proof.py --evidence /path/to/evidence
Phase B reuses the browser scenario with a real operator URL/fixture after PR 2.
"""

from __future__ import annotations

import argparse
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
    page.get_by_role("button", name="Edit inputs", exact=True).click()
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


def scenario(page, url: str, evidence: Path) -> None:
    page.goto(url)
    start_run(page, "proof-1", 2, files=True, evidence=evidence)
    expect(page.locator('[data-machine-run="machine-1"]')).to_be_visible()
    expect(page.locator(".run-list-row")).to_have_count(1)
    expect(page.locator(".run-list-row")).to_contain_text("proof-1")
    expect(page.locator('[data-current="true"]')).to_have_count(1)
    expect(page.locator(".react-flow__edge-machineArc")).to_have_count(4)
    paths = page.locator(".react-flow__edge-machineArc .react-flow__edge-path").evaluate_all(
        "els => els.map(e => e.getAttribute('d'))"
    )
    assert all("C" in path for path in paths), paths
    assert any(
        "review to review" in label
        for label in page.locator(".react-flow__edge").evaluate_all(
            "els => els.map(e => e.getAttribute('aria-label') || '')"
        )
    )
    # The measured machine viewport must not change after the first rendered frame.
    viewport = page.locator('[aria-label="Machine state graph"] .react-flow__viewport')
    first = viewport.get_attribute("style")
    page.wait_for_timeout(300)
    assert first == viewport.get_attribute("style"), "First-frame viewport moved"
    page.screenshot(path=str(evidence / "01-interrupted.png"), full_page=True)
    page.get_by_role("button", name="Retry visit", exact=True).click()
    expect(page.locator('[data-visit="2"]')).to_be_visible()
    visit = page.locator('[data-visit="1"]')
    visit.get_by_role("button", expanded=False).click()
    expect(visit.locator(".react-flow__node")).to_have_count(1)
    expect(visit.locator("[data-event-status]")).to_have_count(0)
    visit.get_by_role("button", name="Next facts").click()
    expect(visit.locator('[data-event-status="ignored"]')).to_be_visible()
    expect(visit.locator('[data-event-status="dropped"]')).to_be_visible()
    page.screenshot(path=str(evidence / "02-archived-visit.png"), full_page=True)
    # Send commits but loses its first response. The UI retry retains its idempotency key.
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
    page.screenshot(path=str(evidence / "03-send-receipt.png"), full_page=True)
    # This timer advances in the host without clicking a timer button.
    page.get_by_label("Event", exact=True).select_option("advance")
    page.get_by_role("form", name="Send event").get_by_role("button", name="Send event").click()
    expect(page.locator('[data-machine-state="ready"]')).to_be_visible()
    timer = page.locator("[data-timer-remaining]")
    before = int(timer.get_attribute("data-timer-remaining"))
    page.wait_for_timeout(1100)
    assert int(timer.get_attribute("data-timer-remaining")) < before
    page.locator("[data-machine-run]").evaluate("el => el.scrollTop = 0")
    page.screenshot(path=str(evidence / "04-timer.png"), full_page=True)
    expect(page.locator('[data-machine-state="closed"]')).to_be_visible(timeout=12000)
    expect(page.locator(".run-list-row")).to_have_count(1)
    expect(page.get_by_role("region", name="Machine actions")).to_have_count(0)
    page.screenshot(path=str(evidence / "05-final.png"), full_page=True)
    # Same-key start returns to the existing run; never creates a visit run-list entry.
    page.get_by_role("button", name="Current definition", exact=True).click()
    start_run(page, "proof-1", 2)
    expect(page.locator('[data-machine-run="machine-1"]')).to_be_visible()
    expect(page.locator(".run-list-row")).to_have_count(1)
    # Another instance exercises the explicit dev-only timer control.
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
    # Third instance exercises close. Confirming is deliberate, never automatic.
    page.get_by_role("button", name="Current definition", exact=True).click()
    start_run(page, "close-proof", 1)
    expect(page.locator('[data-machine-run="machine-3"]')).to_be_visible()
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Close machine", exact=True).click()
    expect(page.locator('[data-machine-state="closed"]')).to_be_visible()
    # A transient read failure is surfaced and recoverable, not a blank canvas.
    page.evaluate("window.machineHarness.failNextRead()")
    expect(page.get_by_role("alert")).to_be_visible(timeout=4000)
    page.get_by_role("button", name="Retry read", exact=True).click()
    page.evaluate("window.machineHarness.recoverRead()")
    expect(page.get_by_role("alert")).to_have_count(0)
    # Ordinary workflow path still uses its normal graph and controls.
    calls = page.evaluate("window.machineHarness.calls")
    assert not [call for call in calls if call["method"].startswith("flow-")], calls
    starts = [call for call in calls if call["method"] == "run-start"]
    assert len(starts) == 4, starts
    assert starts[0]["args"][1] == {
        "id": "proof-1", "priority": 2, "workspace": {"kind": "workspace", "entries": []}
    }, starts
    assert starts[0]["args"][2] == [{
        "field": "document", "name": "proof.txt", "bytes": list(b"machine proof\n")
    }], starts
    assert any(call["method"] == "baseline" for call in calls), calls
    (evidence / "calls.json").write_text(json.dumps(calls, indent=2) + "\n")
    page.get_by_test_id("flow-nav").click()
    expect(page.locator('[aria-label="Machine state graph"]')).to_have_count(0)
    expect(page.locator(".react-flow__node")).to_have_count(1)
    page.screenshot(path=str(evidence / "06-ordinary-flow.png"), full_page=True)
    # No optional methods means genuinely read-only; no mutation request is made.
    page.goto(url + "?readonly")
    expect(page.locator('[data-machine-run="machine-1"]')).to_be_visible()
    expect(page.locator('[data-current="true"]')).to_have_count(1)
    expect(page.get_by_role("region", name="Machine actions")).to_have_count(0)
    page.get_by_role("button", name="Current definition", exact=True).click()
    expect(page.get_by_role("button", name="Run", exact=True)).to_have_count(0)
    writes = page.evaluate(
        "window.machineHarness.calls.filter(c => "
        "['run-start','send','retry','close','fire','flow-start','flow-cancel'].includes(c.method))"
    )
    assert not writes, writes
    page.locator(".run-list-row").click()
    page.screenshot(path=str(evidence / "07-read-only.png"), full_page=True)
    # Deep-link failure is explicit, not a fallback to an unrelated run.
    page.goto(url + "?run=missing")
    expect(page.get_by_role("alert").last).to_be_visible()
    page.screenshot(path=str(evidence / "08-missing-run.png"), full_page=True)


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
                        scenario(page, url, evidence)
                        assert not errors, errors
                        results[name] = {
                            "status": "passed",
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
