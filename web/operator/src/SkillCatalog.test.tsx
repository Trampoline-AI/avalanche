import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SkillCatalog } from "./SkillCatalog";
import { FlowInfoMsg } from "./model";

function workflow(id: string, instructions = "Read every page.") {
  return FlowInfoMsg.create({
    workflowId: id,
    displayName: id,
    displayNames: { read: "Read sources", write: "Write article" },
    agentMetadataJson: {
      read: JSON.stringify({ skills: [{ name: "PDF", instructions, packages: ["pymupdf"] }] }),
      write: JSON.stringify({ skills: [{ name: "PDF", instructions, packages: ["pymupdf"] }] }),
    },
  });
}

describe("workflow skill catalog", () => {
  it("combines identical skills across workflows and opens their instructions and usage", () => {
    const openNode = vi.fn();
    render(
      <SkillCatalog
        workflows={[workflow("Research"), workflow("Content")]}
        onOpenNode={openNode}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Skills: 1" });
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog");
    const entry = within(dialog).getByRole("button", { name: "View PDF" });
    expect(within(dialog).getAllByRole("button", { name: "Read sources" })).toHaveLength(2);
    expect(dialog).not.toHaveTextContent("Research /");
    expect(dialog).not.toHaveTextContent("Content /");
    fireEvent.click(entry);
    expect(within(dialog).getByText("Read every page.")).toBeVisible();
    expect(within(dialog).getByRole("region", { name: "Packages" })).toHaveTextContent(
      "pymupdf",
    );
    expect(
      within(dialog).getByRole("region", { name: "Used by" }).querySelectorAll("li"),
    ).toHaveLength(4);
    const back = within(dialog).getByRole("button", { name: "Back to skills" });
    expect(back).toHaveFocus();
    fireEvent.click(back);
    expect(within(dialog).getByRole("button", { name: "View PDF" })).toBeVisible();
    fireEvent.click(within(dialog).getByRole("button", { name: "Close skills" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    fireEvent.click(trigger);
    fireEvent.click(
      within(screen.getByRole("dialog")).getAllByRole("button", { name: "Write article" })[1],
    );
    expect(openNode).toHaveBeenCalledWith("Content", "write");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps different definitions of the same skill separate and updates with the catalog", () => {
    const view = render(
      <SkillCatalog
        onOpenNode={vi.fn()}
        workflows={[workflow("Research"), workflow("Content", "Read captions too.")]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Skills: 2" }));
    fireEvent.click(screen.getByRole("button", { name: "View PDF variant 2" }));
    expect(screen.getByText("Read captions too.")).toBeVisible();
    expect(screen.getByRole("region", { name: "Used by" })).not.toHaveTextContent("Research");
    view.rerender(<SkillCatalog workflows={[]} onOpenNode={vi.fn()} />);
    expect(screen.getByText("No skills declared in the loaded workflows.")).toBeVisible();
    expect(screen.queryByText("Read captions too.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close skills" }));
    expect(screen.getByRole("button", { name: "Skills: 0" })).toHaveFocus();
  });
});
