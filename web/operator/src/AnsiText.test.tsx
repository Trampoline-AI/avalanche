import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AnsiText } from "./AnsiText";

describe("AnsiText", () => {
  it("renders basic ANSI styles and resets them", () => {
    render(
      <pre data-testid="ansi-output">
        <AnsiText text={"before \u001b[1;32msuccess\u001b[0m after"} />
      </pre>,
    );

    expect(screen.getByText("success")).toHaveStyle({
      color: "rgb(22, 128, 93)",
      fontWeight: "700",
    });
    expect(screen.getByTestId("ansi-output").lastChild?.nodeType).toBe(Node.TEXT_NODE);
    expect(document.body.textContent).not.toContain("\u001b[");
  });

  it("supports xterm and true-color foreground sequences", () => {
    render(
      <pre>
        <AnsiText text={"\u001b[38;5;208morange\u001b[0m \u001b[38;2;1;2;3mrgb"} />
      </pre>,
    );

    expect(screen.getByText("orange")).toHaveStyle({ color: "rgb(255, 135, 0)" });
    expect(screen.getByText("rgb")).toHaveStyle({ color: "rgb(1, 2, 3)" });
  });
});
