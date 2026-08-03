import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("./GraphCanvas", () => ({
  GraphCanvas: () => <div>Workflow graph</div>,
}));
vi.mock("./state", () => ({
  useOperatorProjection: () => ({
    state: {
      catalog: {
        operatorInstanceId: "operator-1",
        asOfSequence: "1",
        revision: "1",
        workflows: [
          {
            workflowId: "flow.py::demo",
            displayName: "demo",
            rootAlias: "examples",
            relativeFile: "flow.py",
            nodeIds: [],
            graph: {},
            nodeTypes: {},
            displayNames: {},
            agentNodeIds: [],
            agentMetadataJson: {},
          },
        ],
        scanTargets: [
          {
            alias: "examples",
            targetPath: "/workspace/examples",
            kind: "directory",
          },
        ],
        diagnostics: [],
      },
      runs: {},
      liveEvents: {},
      liveLogs: {},
      operatorInstanceId: "operator-1",
      sequence: "1",
      connection: "live",
    },
    startRun: vi.fn(async () => "run-1"),
    cancelRun: vi.fn(async () => undefined),
  }),
}));

import { App } from "./App";
import { GrpcWebOperatorApi } from "./api";

describe("App", () => {
  it("keeps Explorer available through the compact navigation toggle", () => {
    const { container } = render(
      <App api={new GrpcWebOperatorApi("http://localhost")} />,
    );
    const toggle = screen.getByRole("button", { name: "Explorer" });

    expect(toggle).toHaveAttribute("aria-controls", "operator-explorer");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("complementary", { name: "Explorer" })).toHaveAttribute(
      "id",
      "operator-explorer",
    );

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(container.querySelector(".app-shell")).toHaveClass("explorer-open");
  });
});
