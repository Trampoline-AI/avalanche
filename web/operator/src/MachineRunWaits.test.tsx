import { render, screen, within } from "@testing-library/react";
import { expect, it } from "vitest";

import type { MachineInstance } from "./machine";
import { MachineRunWaits } from "./MachineWorkspace";

// A host that omits the optional deferred events (`pending_events`) and receipts.
function instance(accepts: string[], due: number | null = null): MachineInstance {
  return {
    machine: "review",
    key: ["acme/api", 42],
    run_id: "run-1",
    deployment_id: "",
    status: "waiting",
    state: "review",
    visit: 1,
    version: 1,
    accepts,
    due,
    finished: false,
  };
}

function waits(item: MachineInstance, settled: boolean | undefined) {
  return <MachineRunWaits instance={item} refresh={() => undefined} settled={settled} />;
}

const kinds = () =>
  [...document.querySelectorAll("[data-wait-kind]")].map((row) =>
    row.getAttribute("data-wait-kind"),
  );

it("says deferred events are unavailable on a live run whose host omits them", () => {
  render(waits(instance(["amended"]), false));
  const section = screen.getByRole("region", { name: "Machine waits" });
  expect(within(section).getByText("Deferred events unavailable")).toBeTruthy();
  expect(kinds()).toEqual(["deferred-unavailable", "accepts"]);
  expect(screen.queryByText(/not reported by this host/)).toBeNull();
  expect(screen.queryByText("No pending waits.")).toBeNull();
});

it("shows only 'No pending waits.' on a settled run, with no pending lines", () => {
  render(waits(instance(["amended"], Date.now() / 1000 + 600), true));
  expect(screen.getByText("No pending waits.")).toBeTruthy();
  expect(kinds()).toEqual(["none"]);
  expect(screen.queryByText("Deferred events unavailable")).toBeNull();
  expect(screen.queryByText("Waiting for event")).toBeNull();
});

it("claims nothing while the run status is unknown", () => {
  const view = render(waits(instance([]), undefined));
  expect(screen.queryByText("No pending waits.")).toBeNull();
  expect(screen.queryByText("Deferred events unavailable")).toBeNull();
  expect(kinds()).toEqual([]);
  // What the instance itself reports still shows.
  view.rerender(waits(instance(["amended"]), undefined));
  expect(kinds()).toEqual(["accepts"]);
});
