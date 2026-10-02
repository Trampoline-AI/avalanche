import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MetricInputSummary } from "./MetricInputSummary";

describe("metric input disclosure", () => {
  it("keeps input paths collapsed until opened and hides them again when closed", () => {
    render(
      <MetricInputSummary
        inputs={[
          { source: "input", selector: "['packet']" },
          { source: "output", selector: "" },
          { source: "output", selector: "summary" },
          { source: "trace", selector: "" },
        ]}
      />,
    );
    const list = screen.getByRole("list", { name: "Metric sources", hidden: true });
    const summary = list.closest("details")!.querySelector("summary")!;
    expect(list).not.toBeVisible();
    fireEvent.click(summary);
    expect(list).toBeVisible();
    const paths = within(list);
    expect(paths.getByText("input.packet")).toBeVisible();
    expect(paths.getByText("output")).toBeVisible();
    expect(paths.getByText("output.summary")).toBeVisible();
    expect(paths.getByText("trace")).toBeVisible();
    fireEvent.click(summary);
    expect(list).not.toBeVisible();
  });

  it("preserves non-identifier keys, indexed fields, and opaque callable identities", () => {
    render(
      <MetricInputSummary
        inputs={[
          { source: "input", selector: '["customer"].request' },
          { source: "input", selector: "['customer.name']" },
          { source: "output", selector: "[0].summary" },
          { source: "trace", selector: "steps[0]" },
          { source: "custom", selector: "selectors.build_evidence" },
        ]}
      />,
    );
    const list = screen.getByRole("list", { name: "Metric sources", hidden: true });
    fireEvent.click(list.closest("details")!.querySelector("summary")!);
    const paths = within(list);
    expect(paths.getByText("input.customer.request")).toBeVisible();
    expect(paths.getByText("input['customer.name']")).toBeVisible();
    expect(paths.getByText("output[0].summary")).toBeVisible();
    expect(paths.getByText("trace.steps[0]")).toBeVisible();
    expect(paths.getByText("custom: selectors.build_evidence")).toBeVisible();
  });
});
