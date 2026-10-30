import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { FlowInfoMsg } from "./model";
import { RunControls } from "./RunControls";
import { useInputDraft, type InputDraft } from "./useInputDraft";
import { workflow } from "./test/fixtures";

const nightly = FlowInfoMsg.create({
  ...workflow,
  workflowId: "nightly.py::nightly",
  cron: "0 2 * * *",
  inputSchemaJson: JSON.stringify({
    type: "object",
    properties: { region: { type: "string", default: "eu" }, limit: { type: "integer" } },
    required: ["limit"],
  }),
  inputDeclarationJson: JSON.stringify({
    type_name: "NightlyInput",
    json_schema: { type: "object" },
    consumer_node_ids: [],
  }),
});

interface HarnessProps {
  workflow: FlowInfoMsg;
  onStart: (selector: string, input?: Record<string, unknown>) => Promise<string>;
  onSetScheduleEnabled: (
    selector: string,
    enabled: boolean,
    input?: Record<string, unknown>,
  ) => Promise<void>;
  onEditInput: () => void;
  /** The latest draft, so a test can edit it the way the input panel does. */
  draft: { current?: InputDraft };
}

/** The workspace owns the input draft; the harness does the same. */
function Harness({
  workflow,
  onStart,
  onSetScheduleEnabled,
  onEditInput,
  draft,
}: HarnessProps) {
  const inputDraft = useInputDraft(workflow);
  draft.current = inputDraft;
  return (
    <RunControls
      workflow={workflow}
      inputDraft={inputDraft}
      onStart={onStart}
      onCancel={async () => undefined}
      onSetScheduleEnabled={onSetScheduleEnabled}
      onEditInput={onEditInput}
    />
  );
}

function renderControls(
  flow: FlowInfoMsg,
  onSetScheduleEnabled = vi.fn(
    async (_selector: string, _enabled: boolean, _input?: Record<string, unknown>) => undefined,
  ),
) {
  const onStart = vi.fn(async (_selector: string, _input?: Record<string, unknown>) => "run-1");
  const onEditInput = vi.fn();
  const draft: { current?: InputDraft } = {};
  const props = { onStart, onSetScheduleEnabled, onEditInput, draft };
  const view = render(<Harness workflow={flow} {...props} />);
  const edit = (value: unknown) => act(() => draft.current!.setText(JSON.stringify(value)));
  const rerender = (next: FlowInfoMsg) => view.rerender(<Harness workflow={next} {...props} />);
  return { ...props, edit, rerender };
}

it("starts Run and the schedule with the edited input", async () => {
  const { onStart, onSetScheduleEnabled, draft, edit } = renderControls(nightly);

  // Defaults stay out of the draft, so the run records them as defaults.
  expect(JSON.parse(draft.current!.text)).toEqual({});
  edit({ region: "us", limit: 10 });
  fireEvent.click(screen.getByRole("button", { name: "Run" }));
  await waitFor(() =>
    expect(onStart).toHaveBeenCalledWith(
      nightly.workflowId,
      { region: "us", limit: 10 },
      undefined,
    ),
  );
  fireEvent.click(screen.getByRole("switch", { name: "Run on schedule" }));
  await waitFor(() =>
    expect(onSetScheduleEnabled).toHaveBeenCalledWith(
      nightly.workflowId,
      true,
      { region: "us", limit: 10 },
      undefined,
    ),
  );
});

it("leaves the input to the operator while it is unedited", async () => {
  const { onStart, onSetScheduleEnabled } = renderControls(nightly);

  fireEvent.click(screen.getByRole("button", { name: "Run" }));
  fireEvent.click(screen.getByRole("switch", { name: "Run on schedule" }));

  await waitFor(() =>
    expect(onStart).toHaveBeenCalledWith(nightly.workflowId, undefined, undefined),
  );
  await waitFor(() =>
    expect(onSetScheduleEnabled).toHaveBeenCalledWith(
      nightly.workflowId,
      true,
      undefined,
      undefined,
    ),
  );
});

it("starts a manual run with the schedule's input the panel shows", async () => {
  const { onStart } = renderControls({
    ...nightly,
    scheduleEnabled: true,
    scheduleInput: { limit: 3 },
  });

  // Unedited, the draft holds the schedule's input, which a manual run does not inherit.
  fireEvent.click(screen.getByRole("button", { name: "Run" }));

  await waitFor(() =>
    expect(onStart).toHaveBeenCalledWith(nightly.workflowId, { limit: 3 }, undefined),
  );
});

it("offers to update an enabled schedule when the edited input differs", async () => {
  const { onSetScheduleEnabled, edit } = renderControls({
    ...nightly,
    scheduleEnabled: true,
    scheduleInput: { limit: 3 },
  });

  expect(screen.queryByRole("button", { name: "Use for schedule" })).not.toBeInTheDocument();
  edit({ region: "eu", limit: 4 });
  fireEvent.click(screen.getByRole("button", { name: "Use for schedule" }));

  await waitFor(() =>
    expect(onSetScheduleEnabled).toHaveBeenCalledWith(
      nightly.workflowId,
      true,
      { region: "eu", limit: 4 },
      undefined,
    ),
  );
});

it("disables a schedule without sending input", async () => {
  const { onSetScheduleEnabled, edit } = renderControls({ ...nightly, scheduleEnabled: true });

  edit({ limit: 4 });
  fireEvent.click(screen.getByRole("switch", { name: "Run on schedule" }));

  await waitFor(() =>
    expect(onSetScheduleEnabled).toHaveBeenCalledWith(
      nightly.workflowId,
      false,
      undefined,
      undefined,
    ),
  );
});

it.each([
  ["takes input", nightly, true],
  ["takes no input", { ...nightly, inputDeclarationJson: "" }, false],
])("links an error to the input panel when the workflow %s", async (_case, flow, linked) => {
  const rejected = vi.fn(async () => {
    throw new Error("Input is missing required fields: limit");
  });
  const { onEditInput } = renderControls(flow, rejected);

  fireEvent.click(screen.getByRole("switch", { name: "Run on schedule" }));
  const alert = await screen.findByRole("alert");
  const link = screen.queryByRole("button", { name: "Edit inputs" });

  expect(alert).toBeVisible();
  if (!linked) {
    expect(link).toBeNull();
    return;
  }
  fireEvent.click(link!);
  expect(onEditInput).toHaveBeenCalledOnce();
});

it("follows input stored elsewhere until the draft is edited", () => {
  const { draft, edit, rerender } = renderControls(nightly);

  rerender({ ...nightly, scheduleEnabled: true, scheduleInput: { region: "us", limit: 5 } });
  expect(JSON.parse(draft.current!.text)).toEqual({ region: "us", limit: 5 });
  expect(screen.queryByRole("button", { name: "Use for schedule" })).not.toBeInTheDocument();

  edit({ region: "us", limit: 7 });
  rerender({ ...nightly, scheduleEnabled: true, scheduleInput: { region: "ap", limit: 6 } });
  expect(JSON.parse(draft.current!.text)).toEqual({ region: "us", limit: 7 });
});

it("refuses to start while a field holds text that is not valid JSON", async () => {
  const { onStart, onSetScheduleEnabled, draft } = renderControls(nightly);

  act(() => draft.current!.setPending((current) => ({ ...current, limit: "[4," })));
  fireEvent.click(screen.getByRole("button", { name: "Run" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("limit");
  fireEvent.click(screen.getByRole("switch", { name: "Run on schedule" }));

  expect(onStart).not.toHaveBeenCalled();
  expect(onSetScheduleEnabled).not.toHaveBeenCalled();
});

it("refuses Use for schedule while a field holds text that is not valid JSON", async () => {
  const { onSetScheduleEnabled, draft, edit } = renderControls({
    ...nightly,
    scheduleEnabled: true,
    scheduleInput: { limit: 3 },
  });

  edit({ region: "eu", limit: 4 });
  act(() => draft.current!.setPending((current) => ({ ...current, region: "[" })));
  fireEvent.click(screen.getByRole("button", { name: "Use for schedule" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("region");
  expect(onSetScheduleEnabled).not.toHaveBeenCalled();
});

it("drops invalid typed text when the draft is replaced", () => {
  const { draft, rerender } = renderControls(nightly);

  act(() => draft.current!.setPending((current) => ({ ...current, limit: "[4," })));
  rerender({ ...nightly, workflowId: "other.py::other" });

  expect(draft.current!.pending).toEqual({});
});
