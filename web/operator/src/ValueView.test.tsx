import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ValueView } from "./ValueView";

describe("ValueView", () => {
  it("shows root JSON content directly without generic object summary buttons", () => {
    render(<ValueView value={{ answer: "readable root", count: 3 }} />);

    expect(screen.getByText("answer")).toBeInTheDocument();
    expect(screen.getByText("readable root")).toBeInTheDocument();
    expect(screen.getByText("count")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Object/i })).not.toBeInTheDocument();
    expect(screen.getByRole("tree", { name: "JSON value" })).toBeInTheDocument();
  });

  it("uses key-named controls and expands nested values independently", () => {
    const onExpand = vi.fn();
    render(
      <ValueView
        value={{
          visible: "outer value",
          nested: { secret: "nested value" },
          items: ["first item"],
        }}
        onExpand={onExpand}
      />,
    );

    expect(screen.getByText("outer value")).toBeInTheDocument();
    expect(screen.queryByText("nested value")).not.toBeInTheDocument();
    const nested = screen.getByRole("button", { name: "Expand nested" });
    const items = screen.getByRole("button", { name: "Expand items" });
    expect(nested).toHaveAttribute("aria-expanded", "false");
    expect(items).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: /Object/i })).not.toBeInTheDocument();

    fireEvent.click(nested);

    expect(nested).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("secret")).toBeInTheDocument();
    expect(screen.getByText("nested value")).toBeInTheDocument();
    expect(screen.queryByText("first item")).not.toBeInTheDocument();
    expect(onExpand).toHaveBeenCalledWith(
      expect.objectContaining({ secret: "nested value" }),
      ["nested"],
    );
  });

  it("reveals root collection children in deterministic groups of at most 100", () => {
    const values = Array.from({ length: 205 }, (_, index) => `item-${index}`);
    const { container } = render(<ValueView value={values} />);

    expect(screen.getByText("item-0")).toBeInTheDocument();
    expect(screen.getByText("item-99")).toBeInTheDocument();
    expect(screen.queryByText("item-100")).not.toBeInTheDocument();
    expect(container.querySelector(".value-group")?.children).toHaveLength(100);

    fireEvent.click(screen.getByRole("button", { name: "Show 100 more items" }));
    expect(screen.getByText("item-100")).toBeInTheDocument();
    expect(screen.getByText("item-199")).toBeInTheDocument();
    expect(screen.queryByText("item-200")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show 5 more items" }));
    expect(screen.getByText("item-204")).toBeInTheDocument();
    expect(container.querySelector(".value-group")?.children).toHaveLength(205);
  });

  it("bounds object properties before enumerating an additional group", () => {
    const value: Record<string, string> = {};
    for (let index = 0; index < 101; index += 1) value[`key-${index}`] = `value-${index}`;
    render(<ValueView value={value} />);

    expect(screen.getByText("key-99")).toBeInTheDocument();
    expect(screen.queryByText("key-100")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show 1 more property" }));
    expect(screen.getByText("key-100")).toBeInTheDocument();
  });

  it("stops collection disclosure at the maximum depth", () => {
    render(<ValueView value={{ deeper: "hidden" }} depth={12} />);

    expect(
      screen.getByRole("note", {
        name: "1 property. Deeper values are not shown (maximum depth 12).",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText("hidden")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Expand/ })).not.toBeInTheDocument();
  });

  it("keeps deep rows in pane-local overflow classes instead of narrow recursive columns", () => {
    render(<ValueView value={{ deeply_nested_field_name: { next: { answer: "wide value" } } }} />);

    const tree = screen.getByRole("tree", { name: "JSON value" });
    expect(tree).toHaveClass("value-tree");
    expect(screen.getByText("deeply_nested_field_name").closest(".value-row")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Expand deeply_nested_field_name" }));
    const nextContent = screen.getByRole("button", { name: "Expand next" }).closest(".value-content");
    expect(nextContent).toBeInTheDocument();
    expect(tree.querySelector(".value-child-group")).toBeInTheDocument();
  });

  it("truncates long strings until explicitly expanded", () => {
    const value = "x".repeat(300);
    render(<ValueView value={value} />);

    expect(screen.getByText(`${"x".repeat(240)}…`)).toBeInTheDocument();
    expect(screen.queryByText(value)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show full string" }));
    expect(screen.getByText(value)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show less" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("renders scalar, file, and unavailable values immediately", () => {
    const { rerender } = render(<ValueView value={null} />);
    expect(screen.getByText("null")).toHaveClass("value-null");

    rerender(<ValueView value={{ kind: "predict_rlm_file", path: "/tmp/report.pdf" }} />);
    expect(screen.getByText("PredictRLM file")).toBeInTheDocument();
    expect(screen.getByText("/tmp/report.pdf")).toBeInTheDocument();
    expect(screen.getByTitle(/contents are not copied/i)).toBeInTheDocument();

    rerender(<ValueView value={{ kind: "unavailable", reason: "unsupported socket" }} />);
    expect(screen.getByText("Unavailable · unsupported socket")).toBeInTheDocument();
  });
});
