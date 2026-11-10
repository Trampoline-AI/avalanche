import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import type { OperatorApi } from "./api";
import type { MachineInstance, MachineSpec } from "./machine";
import { MachineSendPanel } from "./MachineWorkspace";
import { FlowInfoMsg } from "./model";

const spec: MachineSpec = {
  schema_version: "avalanche.machine/v1",
  name: "review",
  input_schema: { type: "object" },
  key_fields: ["repo", "pr"],
  initial: "review",
  states: {
    review: {
      work: "review_work",
      initial: true,
      final: false,
      defer: [],
      timers: [],
      transitions: [
        { event: "amended", target: "review", payload_schema: null },
        { event: "advance", target: "ready", payload_schema: null },
      ],
    },
    ready: {
      work: "ready_work",
      initial: false,
      final: false,
      defer: [],
      timers: [],
      transitions: [{ event: "back", target: "review", payload_schema: null }],
    },
    closed: {
      work: "cleanup",
      initial: false,
      final: true,
      defer: [],
      timers: [],
      transitions: [],
    },
  },
  work_graphs: {},
};
const workflow = FlowInfoMsg.create({
  name: "review",
  workflowId: "review",
  machineSpec: spec,
});

function at(state: string, accepts: string[], finished = false): MachineInstance {
  return {
    machine: "review",
    key: ["acme/api", 42],
    run_id: "run-1",
    deployment_id: "",
    status: finished ? "final" : "waiting",
    state,
    visit: 1,
    version: 1,
    accepts,
    due: null,
    finished,
    machine_spec: spec,
  };
}

function panel(instance: MachineInstance, api: OperatorApi) {
  return (
    <MachineSendPanel
      api={api}
      runId="run-1"
      workflow={workflow}
      spec={spec}
      instance={instance}
      onClose={() => undefined}
      onSent={() => undefined}
    />
  );
}

it("revalidates the chosen event against the events the run accepts after each transition", async () => {
  const sendMachineEvent = vi.fn(async (_runId: string, event: string) => ({
    receipt_id: `receipt-${event}`,
    key: event,
    accepted_at: 1,
    status: "accepted" as const,
    duplicate: false,
  }));
  const api = { sendMachineEvent } as unknown as OperatorApi;
  const view = render(panel(at("review", ["amended", "advance"]), api));
  const select = () => screen.getByLabelText<HTMLSelectElement>("Event");
  const options = () =>
    within(select())
      .queryAllByRole("option")
      .map((o) => o.textContent);
  const send = () =>
    within(screen.getByRole("form", { name: "Send event" })).getByRole("button", {
      name: "Send event",
    });

  fireEvent.change(select(), { target: { value: "advance" } });
  fireEvent.click(send());
  await waitFor(() => expect(sendMachineEvent).toHaveBeenCalledTimes(1));
  expect(sendMachineEvent.mock.calls[0]![1]).toBe("advance");

  // The run moved on: `advance` is no longer accepted, so the selection resets.
  // A second click must never send the stale `advance`.
  view.rerender(panel(at("ready", ["back"]), api));
  fireEvent.click(send());
  await waitFor(() => expect(sendMachineEvent).toHaveBeenCalledTimes(2));
  expect(sendMachineEvent.mock.calls[1]![1]).toBe("back");
  expect(select().value).toBe("back");
  // The menu offers only what the current state accepts.
  expect(options()).toEqual(["back"]);

  // A settled run accepts nothing: no event to choose and Send is disabled.
  view.rerender(panel(at("closed", [], true), api));
  expect(screen.queryByLabelText("Event")).toBeNull();
  expect(screen.getByText("This run accepts no events now.")).toBeTruthy();
  expect(send()).toHaveProperty("disabled", true);
  fireEvent.click(send());
  expect(sendMachineEvent).toHaveBeenCalledTimes(2);
});
