import { render, screen, within } from "@testing-library/react";
import { expect, it } from "vitest";

import { SkillContent } from "./SkillContent";

it("renders a complete skill table with formatting, alignment, and safe links", () => {
  const tableSource = [
    "| Artifact | Method | Count |",
    "| :--- | :---: | ---: |",
    "| **PDF** | `read_pages` | 12 |",
    "| [Reference](https://example.com/docs) | [Blocked](javascript:alert) | 2 |",
  ].join("\n");
  const instructions = `# Source handling\n\n${"Context. ".repeat(600)}\n\n${tableSource}`;
  render(<SkillContent skill={{ name: "Sources", instructions, packages: [], modules: [] }} />);
  const table = screen.getByRole("table");
  expect(within(table).getAllByRole("columnheader")).toHaveLength(3);
  expect(within(table).getAllByRole("row")).toHaveLength(3);
  expect(
    within(table).getByRole("cell", { name: "PDF" }).querySelector("strong"),
  ).not.toBeNull();
  expect(
    within(table).getByRole("cell", { name: "read_pages" }).querySelector("code"),
  ).not.toBeNull();
  expect(within(table).getByRole("columnheader", { name: "Count" })).toHaveStyle({
    textAlign: "right",
  });
  expect(within(table).getByRole("link", { name: "Reference" })).toHaveAttribute(
    "href",
    "https://example.com/docs",
  );
  expect(within(table).getByText("Blocked").getAttribute("href") ?? "").not.toContain(
    "javascript:",
  );
});
