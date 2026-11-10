import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import type { OperatorApi } from "./api";
import type { MachineInstance, MachineSpec } from "./machine";
import { MachineRunActions, MachineSendPanel } from "./MachineWorkspace";
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

// The documented contract (docs/state-machines.md, "accept, ignore, drop and defer"):
// a name in the current state's `defer=` is buffered and acknowledged as accepted.
const deferring: MachineSpec = {
  ...spec,
  states: {
    ...spec.states,
    review: { ...spec.states.review!, defer: ["merged", "ghost"] },
    ready: {
      ...spec.states.ready!,
      transitions: [
        { event: "back", target: "review", payload_schema: null },
        { event: "merged", target: "closed", payload_schema: null },
        { event: "closed_without_merge", target: "closed", payload_schema: null },
      ],
    },
  },
};

function deferringAt(state: string, accepts: string[]): MachineInstance {
  return { ...at(state, accepts), machine_spec: deferring };
}

function receiptFor(_runId: string, event: string) {
  return Promise.resolve({
    receipt_id: `receipt-${event}`,
    key: event,
    accepted_at: 1,
    status: "accepted" as const,
    duplicate: false,
  });
}

const options = () =>
  within(screen.getByLabelText<HTMLSelectElement>("Event"))
    .queryAllByRole("option")
    .map((o) => [(o as HTMLOptionElement).value, o.textContent]);
const sendButton = () =>
  within(screen.getByRole("form", { name: "Send event" })).getByRole("button", {
    name: "Send event",
  });

it("offers the events the current state defers, labelled, and sends them", async () => {
  const sendMachineEvent = vi.fn(receiptFor);
  const api = { sendMachineEvent } as unknown as OperatorApi;
  const view = render(panel(deferringAt("review", ["amended", "advance"]), api));
  // Accepted first, then deferred (`ghost` too: the host buffers it though no
  // state consumes it); `closed_without_merge`, neither accepted nor deferred
  // here, would be ignored by the host and is not offered.
  expect(options()).toEqual([
    ["amended", "amended"],
    ["advance", "advance"],
    ["merged", "merged (deferred)"],
    ["ghost", "ghost (deferred)"],
  ]);
  expect(screen.getByText(/Defers: merged, ghost/)).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Event"), { target: { value: "merged" } });
  fireEvent.click(sendButton());
  await waitFor(() => expect(sendMachineEvent).toHaveBeenCalledTimes(1));
  expect(sendMachineEvent.mock.calls[0]![1]).toBe("merged");
  expect(await screen.findByRole("status")).toHaveProperty(
    "textContent",
    "Receipt receipt-merged · merged · accepted",
  );

  // A later state accepts `merged`: it is offered as an ordinary event.
  view.rerender(panel(deferringAt("ready", ["back", "merged", "closed_without_merge"]), api));
  expect(options()).toEqual([
    ["back", "back"],
    ["merged", "merged"],
    ["closed_without_merge", "closed_without_merge"],
  ]);
});

it("keeps a deferred choice across reads and drops it when no longer deferred", () => {
  const api = { sendMachineEvent: vi.fn(receiptFor) } as unknown as OperatorApi;
  const view = render(panel(deferringAt("review", ["amended"]), api));
  fireEvent.change(screen.getByLabelText("Event"), { target: { value: "merged" } });
  view.rerender(panel(deferringAt("review", ["amended"]), api));
  expect(screen.getByLabelText<HTMLSelectElement>("Event").value).toBe("merged");
  // The run moved to a state that neither accepts nor defers `merged`.
  view.rerender(panel({ ...deferringAt("ready", ["back"]) }, api));
  expect(screen.getByLabelText<HTMLSelectElement>("Event").value).toBe("back");
});

it("enables Send event on a run whose state only defers events", () => {
  const api = { sendMachineEvent: vi.fn(receiptFor) } as unknown as OperatorApi;
  const actions = (instance: MachineInstance) => (
    <MachineRunActions
      api={api}
      runId="run-1"
      instance={instance}
      enabled
      onSend={() => undefined}
      onChanged={() => undefined}
    />
  );
  const view = render(actions(deferringAt("review", [])));
  expect(screen.getByRole("button", { name: "Send event" })).toHaveProperty("disabled", false);
  view.rerender(actions(at("review", [])));
  expect(screen.getByRole("button", { name: "Send event" })).toHaveProperty("disabled", true);
});
