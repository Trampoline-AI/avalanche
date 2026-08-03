import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ValueView } from "./ValueView";

describe("ValueView", () => {
  it("starts arrays and objects collapsed", () => {
    const { rerender } = render(<ValueView value={["hidden array value"]} />);

    expect(screen.getByRole("button", { name: "Array (1 item)" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByText("hidden array value")).not.toBeInTheDocument();

    rerender(<ValueView value={{ answer: "hidden object value" }} />);

    expect(screen.getByRole("button", { name: "Object (1 property)" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByText("hidden object value")).not.toBeInTheDocument();
  });

  it("reveals collection children in deterministic groups of at most 100", () => {
    const values = Array.from({ length: 205 }, (_, index) => `item-${index}`);
    const { container } = render(<ValueView value={values} />);

    fireEvent.click(screen.getByRole("button", { name: "Array (205 items)" }));

    expect(screen.getByText("item-0")).toBeInTheDocument();
    expect(screen.getByText("item-99")).toBeInTheDocument();
    expect(screen.queryByText("item-100")).not.toBeInTheDocument();
    expect(container.querySelectorAll(".value-list")).toHaveLength(1);
    expect(container.querySelector(".value-list")?.children).toHaveLength(100);

    fireEvent.click(screen.getByRole("button", { name: "Show 100 more items" }));

    expect(screen.getByText("item-100")).toBeInTheDocument();
    expect(screen.getByText("item-199")).toBeInTheDocument();
    expect(screen.queryByText("item-200")).not.toBeInTheDocument();
    expect(
      Array.from(container.querySelectorAll(".value-list"), (list) => list.children.length),
    ).toEqual([100, 100]);

    fireEvent.click(screen.getByRole("button", { name: "Show 5 more items" }));

    expect(screen.getByText("item-204")).toBeInTheDocument();
    expect(
      Array.from(container.querySelectorAll(".value-list"), (list) => list.children.length),
    ).toEqual([100, 100, 5]);
    expect(screen.queryByRole("button", { name: /Show .* more items/ })).not.toBeInTheDocument();
  });

  it("chunks object properties without enumerating them into one rendered group", () => {
    const value: Record<string, string> = {};
    for (let index = 0; index < 101; index += 1) {
      value[`key-${index}`] = `value-${index}`;
    }
    const { container } = render(<ValueView value={value} />);

    fireEvent.click(screen.getByRole("button", { name: "Object (101 properties)" }));

    expect(screen.getByText("key-99")).toBeInTheDocument();
    expect(screen.queryByText("key-100")).not.toBeInTheDocument();
    expect(container.querySelector(".value-object")?.children).toHaveLength(100);

    fireEvent.click(screen.getByRole("button", { name: "Show 1 more property" }));

    expect(screen.getByText("key-100")).toBeInTheDocument();
    expect(
      Array.from(container.querySelectorAll(".value-object"), (group) => group.children.length),
    ).toEqual([100, 1]);
  });

  it("requires nested collections to be expanded independently", () => {
    render(
      <ValueView
        value={{
          label: "outer value",
          nested: { secret: "nested value" },
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Object (2 properties)" }));

    expect(screen.getByText("outer value")).toBeInTheDocument();
    const nestedDisclosure = screen.getByRole("button", { name: "Object (1 property)" });
    expect(nestedDisclosure).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("nested value")).not.toBeInTheDocument();

    fireEvent.click(nestedDisclosure);

    expect(nestedDisclosure).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("nested value")).toBeInTheDocument();
  });

  it("stops nested collection disclosure at depth 12", () => {
    let value: unknown = { deeper: "must not be mounted" };
    for (let depth = 0; depth < 12; depth += 1) {
      value = { deeper: value };
    }
    render(<ValueView value={value} />);

    for (let depth = 0; depth < 12; depth += 1) {
      const disclosures = screen.getAllByRole("button", {
        name: "Object (1 property)",
      });
      fireEvent.click(disclosures[disclosures.length - 1]);
    }

    expect(screen.getAllByRole("button", { name: "Object (1 property)" })).toHaveLength(12);
    expect(
      screen.getByRole("note", {
        name: "Object (1 property). Deeper values are not shown (maximum depth 12).",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText("must not be mounted")).not.toBeInTheDocument();
  });

  it("preserves immediate value behavior at the depth boundary", () => {
    const longString = "x".repeat(300);
    const { rerender } = render(<ValueView value={longString} depth={12} />);

    fireEvent.click(screen.getByRole("button", { name: "Show full string" }));
    expect(screen.getByText(longString)).toBeInTheDocument();

    rerender(
      <ValueView
        value={{ kind: "predict_rlm_file", path: "/tmp/reports/deep.pdf" }}
        depth={12}
      />,
    );
    expect(screen.getByText("/tmp/reports/deep.pdf")).toBeInTheDocument();

    rerender(
      <ValueView
        value={{ kind: "unavailable", reason: "depth-independent failure" }}
        depth={12}
      />,
    );
    expect(screen.getByText("Unavailable · depth-independent failure")).toBeInTheDocument();

    rerender(<ValueView value={42} depth={12} />);
    expect(screen.getByText("42")).toHaveClass("value-scalar");

    rerender(<ValueView value={["hidden"]} depth={12} />);
    expect(
      screen.getByRole("note", {
        name: "Array (1 item). Deeper values are not shown (maximum depth 12).",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Array (1 item)" })).not.toBeInTheDocument();
  });

  it("truncates very long strings until explicitly expanded", () => {
    const value = "x".repeat(300);
    render(<ValueView value={value} />);

    expect(screen.getByText(`${"x".repeat(240)}…`)).toBeInTheDocument();
    expect(screen.queryByText(value)).not.toBeInTheDocument();
    const disclosure = screen.getByRole("button", { name: "Show full string" });
    expect(disclosure).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(disclosure);

    expect(screen.getByText(value)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show less" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("renders scalars immediately", () => {
    const { rerender } = render(<ValueView value={null} />);
    expect(screen.getByText("null")).toHaveClass("value-null");

    rerender(<ValueView value={42} />);
    expect(screen.getByText("42")).toHaveClass("value-scalar");

    rerender(<ValueView value={true} />);
    expect(screen.getByText("true")).toHaveClass("value-scalar");

    rerender(<ValueView value="short string" />);
    expect(screen.getByText("short string")).toHaveClass("value-string");
  });

  it("renders PredictRLM file values immediately as reported host paths", () => {
    render(<ValueView value={{ kind: "predict_rlm_file", path: "/tmp/reports/source.pdf" }} />);

    expect(screen.getByText("PredictRLM file")).toBeInTheDocument();
    expect(screen.getByText("/tmp/reports/source.pdf")).toBeInTheDocument();
    expect(screen.getByTitle(/contents are not copied/i)).toBeInTheDocument();
  });

  it("preserves explicit and generic unavailable values", () => {
    const { rerender } = render(
      <ValueView value={{ kind: "unavailable", reason: "unsupported value type: socket" }} />,
    );

    expect(screen.getByText(/Unavailable · unsupported value type: socket/)).toBeInTheDocument();

    rerender(<ValueView value={undefined} />);
    expect(screen.getByText("Unavailable")).toHaveClass("value-unavailable");
  });
});
