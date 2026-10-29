import { EditorView } from "@codemirror/view";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { FlowInfoMsg } from "./model";
import { RunControls } from "./RunControls";
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
});

function editor(): EditorView {
  return EditorView.findFromDOM(screen.getByRole("textbox", { name: "Workflow input JSON" }))!;
}

function replaceInput(value: unknown) {
  const view = editor();
  act(() => {
    view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: JSON.stringify(value) },
    });
  });
}

function renderControls(flow: FlowInfoMsg) {
  const onStart = vi.fn(async () => "run-1");
  const onSetScheduleEnabled = vi.fn(async () => undefined);
  const view = render(
    <RunControls
      workflow={flow}
      onStart={onStart}
      onCancel={async () => undefined}
      onSetScheduleEnabled={onSetScheduleEnabled}
    />,
  );
  return { onStart, onSetScheduleEnabled, view };
}

it("prefills the run input with defaults and uses it for Run and the schedule", async () => {
  const { onStart, onSetScheduleEnabled } = renderControls(nightly);

  fireEvent.click(screen.getByRole("button", { name: "Add JSON input" }));
  expect(JSON.parse(editor().state.doc.toString())).toEqual({ region: "eu", limit: null });
  replaceInput({ region: "us", limit: 10 });

  fireEvent.click(screen.getByRole("button", { name: "Run" }));
  await waitFor(() =>
    expect(onStart).toHaveBeenCalledWith(nightly.workflowId, { region: "us", limit: 10 }),
  );
  fireEvent.click(screen.getByRole("switch", { name: "Run on schedule" }));
  await waitFor(() =>
    expect(onSetScheduleEnabled).toHaveBeenCalledWith(nightly.workflowId, true, {
      region: "us",
      limit: 10,
    }),
  );
});

it("enables a schedule with its current input when the input editor is closed", async () => {
  const { onSetScheduleEnabled } = renderControls(nightly);

  fireEvent.click(screen.getByRole("switch", { name: "Run on schedule" }));

  await waitFor(() =>
    expect(onSetScheduleEnabled).toHaveBeenCalledWith(nightly.workflowId, true, undefined),
  );
});

it("offers to update an enabled schedule when the edited input differs", async () => {
  const { onSetScheduleEnabled } = renderControls({
    ...nightly,
    scheduleEnabled: true,
    scheduleInput: { limit: 3 },
  });

  fireEvent.click(screen.getByRole("button", { name: "Add JSON input" }));
  expect(JSON.parse(editor().state.doc.toString())).toEqual({ region: "eu", limit: 3 });
  expect(screen.queryByRole("button", { name: "Use for schedule" })).not.toBeInTheDocument();
  replaceInput({ region: "eu", limit: 4 });
  fireEvent.click(screen.getByRole("button", { name: "Use for schedule" }));

  await waitFor(() =>
    expect(onSetScheduleEnabled).toHaveBeenCalledWith(nightly.workflowId, true, {
      region: "eu",
      limit: 4,
    }),
  );
});

it("disables a schedule without sending input", async () => {
  const { onSetScheduleEnabled } = renderControls({ ...nightly, scheduleEnabled: true });

  fireEvent.click(screen.getByRole("button", { name: "Add JSON input" }));
  fireEvent.click(screen.getByRole("switch", { name: "Run on schedule" }));

  await waitFor(() =>
    expect(onSetScheduleEnabled).toHaveBeenCalledWith(nightly.workflowId, false, undefined),
  );
});

it("rejects input that is not an object before enabling", async () => {
  const { onSetScheduleEnabled } = renderControls(nightly);

  fireEvent.click(screen.getByRole("button", { name: "Add JSON input" }));
  replaceInput([1]);
  fireEvent.click(screen.getByRole("switch", { name: "Run on schedule" }));

  expect(await screen.findByText("Run input must be a JSON object")).toBeVisible();
  expect(onSetScheduleEnabled).not.toHaveBeenCalled();
});
