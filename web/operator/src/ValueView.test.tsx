import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ValueView } from "./ValueView";

describe("ValueView", () => {
  it("renders PredictRLM file values as reported host paths", () => {
    render(
      <ValueView
        value={{
          document: { kind: "predict_rlm_file", path: "/tmp/reports/source.pdf" },
        }}
      />,
    );

    expect(screen.getByText("PredictRLM file")).toBeInTheDocument();
    expect(screen.getByText("/tmp/reports/source.pdf")).toBeInTheDocument();
    expect(screen.getByTitle(/contents are not copied/i)).toBeInTheDocument();
  });

  it("renders explicitly unavailable values without coercion", () => {
    render(
      <ValueView
        value={{ kind: "unavailable", reason: "unsupported value type: socket" }}
      />,
    );

    expect(screen.getByText(/Unavailable · unsupported value type: socket/)).toBeInTheDocument();
  });
});
