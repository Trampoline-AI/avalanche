import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { Markdown } from "./Markdown";

it("blocks active content and remote images while disclosing large retained Markdown incrementally", () => {
  const source = [
    "# Safe",
    "[blocked](javascript:alert(1))",
    "![tracker](http://127.0.0.1:9/pixel)",
    "<script>alert('untrusted')</script>",
    "A".repeat(256),
    "SECOND_MARKER",
    "B".repeat(256),
    "THIRD_MARKER",
  ].join("\n\n");
  const view = render(<Markdown sourceCharacterBudget={256}>{source}</Markdown>);
  expect(view.container.querySelector("img, script")).toBeNull();
  expect(view.container.querySelector("a")?.getAttribute("href") ?? "").not.toContain(
    "javascript:",
  );
  expect(screen.queryByText("SECOND_MARKER")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Show more" }));
  expect(screen.getByText("SECOND_MARKER")).toBeInTheDocument();
  expect(screen.queryByText("THIRD_MARKER")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Show more" }));
  expect(screen.getByText("THIRD_MARKER")).toBeInTheDocument();

  view.rerender(
    <Markdown
      sourceCharacterBudget={256}
    >{`# Replacement\n\n${"R".repeat(256)}\n\nRESET_TAIL`}</Markdown>,
  );
  expect(screen.getByRole("heading", { name: "Replacement" })).toBeInTheDocument();
  expect(screen.queryByText("THIRD_MARKER")).not.toBeInTheDocument();
  expect(screen.queryByText("RESET_TAIL")).not.toBeInTheDocument();
});
