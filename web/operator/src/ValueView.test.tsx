import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { ValueView } from "./ValueView";

it("bounds retained collections and long strings without losing values or interpreting them as HTML", () => {
  const hostile = '<img src="https://tracker.invalid" onerror="alert(1)">';
  const values = Array.from({ length: 205 }, (_, index) => `item-${index}`);
  values[0] = hostile;
  const view = render(<ValueView value={values} />);
  expect(screen.getByText(hostile)).toBeInTheDocument();
  expect(view.container.querySelector("img")).toBeNull();
  expect(screen.getByText("item-99")).toBeInTheDocument();
  expect(screen.queryByText("item-100")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Show 100 more items" }));
  expect(screen.getByText("item-199")).toBeInTheDocument();
  expect(screen.queryByText("item-200")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Show 5 more items" }));
  expect(screen.getByText("item-204")).toBeInTheDocument();

  const largeText = `${"retained ".repeat(1000)}END_OF_VALUE`;
  view.rerender(<ValueView value={largeText} />);
  expect(screen.queryByText(largeText)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Show full string" }));
  expect(screen.getByText(largeText)).toBeInTheDocument();
});

it("preserves arbitrary JSON objects resembling retained artifacts at every disclosure depth", () => {
  const artifactLike = {
    kind: "unavailable",
    reason: "An authored criterion",
    extra: {
      kind: "predict_rlm_file",
      path: "/a/rubric/not/a/retained/file",
      description: "This description must remain inspectable",
    },
  };
  const view = render(<ValueView value={artifactLike} />);
  expect(screen.getByText("Unavailable · An authored criterion")).toBeInTheDocument();
  expect(screen.queryByText("extra")).not.toBeInTheDocument();

  view.rerender(<ValueView value={artifactLike} jsonOnly />);
  expect(screen.getByText("kind")).toBeInTheDocument();
  expect(screen.getByText(JSON.stringify("An authored criterion"))).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Expand extra" }));
  expect(screen.getByText(JSON.stringify("/a/rubric/not/a/retained/file"))).toBeInTheDocument();
  expect(
    screen.getByText(JSON.stringify("This description must remain inspectable")),
  ).toBeInTheDocument();
  expect(screen.queryByText("PredictRLM file")).not.toBeInTheDocument();
});

it("distinguishes JSON strings from null and preserves escaping after expansion", () => {
  const long = `${'quote" and newline\n'.repeat(30)}END_OF_JSON_STRING`;
  render(<ValueView value={["", "null", null, long]} jsonOnly />);
  expect(screen.getByText('""')).toBeInTheDocument();
  expect(screen.getByText('"null"')).toBeInTheDocument();
  expect(screen.getByText("null")).toBeInTheDocument();
  expect(screen.queryByText(/END_OF_JSON_STRING/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Show full string" }));
  expect(JSON.parse(screen.getByText(/END_OF_JSON_STRING/).textContent ?? "")).toBe(long);
});
