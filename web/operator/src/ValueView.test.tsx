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
