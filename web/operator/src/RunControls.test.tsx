import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { FlowInfoMsg, RunSnapshotMsg } from "./generated/operator";
import { parseRunInput, RunControls } from "./RunControls";

const workflow = {
  workflowId: "flows.py::orders",
  displayName: "Orders",
} as FlowInfoMsg;
const running = {
  summary: { runId: "run-1", status: "running" },
} as RunSnapshotMsg;

describe("RunControls", () => {
  it("starts without implicit input and cancels the authoritative active run", async () => {
    const onStart = vi.fn(async () => "run-2");
    const onCancel = vi.fn(async () => undefined);
    render(
      <RunControls
        workflow={workflow}
        run={running}
        onStart={onStart}
        onCancel={onCancel}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() =>
      expect(onStart).toHaveBeenCalledWith("flows.py::orders", undefined),
    );

    fireEvent.click(screen.getByRole("button", { name: "Cancel run" }));
    await waitFor(() => expect(onCancel).toHaveBeenCalledWith("run-1"));
  });

  it("keeps Run available while requests are pending", async () => {
    const onStart = vi.fn(async () => "run-2");
    render(
      <RunControls
        workflow={workflow}
        pending={{ kind: "start", target: workflow.workflowId }}
        onStart={onStart}
        onCancel={async () => undefined}
      />,
    );

    const runButton = screen.getByRole("button", { name: "Run" });
    expect(runButton).toBeEnabled();
    fireEvent.click(runButton);
    fireEvent.click(runButton);
    await waitFor(() => expect(onStart).toHaveBeenCalledTimes(2));
  });

  it("renders a return control for a historical run", () => {
    const onViewWorkflow = vi.fn();
    render(
      <RunControls
        run={running}
        onStart={async () => "run-2"}
        onCancel={async () => undefined}
        onViewWorkflow={onViewWorkflow}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Current workflow" }));
    expect(onViewWorkflow).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();
  });

  it("keeps the JSON editor closed by default and surfaces operator validation", async () => {
    const onStart = vi.fn(async () => {
      throw new Error("input.value is required");
    });
    render(
      <RunControls
        workflow={workflow}
        onStart={onStart}
        onCancel={async () => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    expect(await screen.findByText("input.value is required")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add JSON input" }));
    expect(
      screen.getByRole("textbox", { name: "Workflow input JSON" }),
    ).toBeInTheDocument();
  });

  it("accepts an explicit JSON object and rejects non-object run input", () => {
    expect(parseRunInput('{"value":7}')).toEqual({ value: 7 });
    expect(() => parseRunInput("[1,2,3]")).toThrow("Run input must be a JSON object");
  });
});
