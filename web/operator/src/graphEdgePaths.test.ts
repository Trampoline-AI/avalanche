import { describe, expect, it } from "vitest";
import { branchEdgePath, branchJunction, outerTrunkPath } from "./graphEdgePaths";

describe.each(["vertical", "horizontal"] as const)("%s shared edge paths", (orientation) => {
  const source = { sourceX: 100, sourceY: 200 };
  const near = { ...source, targetX: 500, targetY: 600 };
  const far = { ...source, targetX: 1000, targetY: 1500 };

  it("shares the source segment and outer rail while ending at each target", () => {
    const paths = [near, far].map((points) =>
      outerTrunkPath(points, orientation, 1600, 0, branchJunction(250, 400), 400).split(" L "),
    );
    expect(paths[0].slice(0, 3)).toEqual(paths[1].slice(0, 3));
    const across = orientation === "vertical" ? 0 : 1;
    expect(paths[0][3].split(",")[across]).toBe(paths[1][3].split(",")[across]);
    expect(paths[0][5]).toBe("500,600");
    expect(paths[1][5]).toBe("1000,1500");
    const forward = orientation === "vertical" ? 1 : 0;
    expect(Number(paths[0][1].split(",")[forward])).toBeGreaterThan(250);
    expect(Number(paths[0][4].split(",")[forward])).toBeLessThan(400);
    expect(outerTrunkPath(near, orientation, 1600, 1, branchJunction(250, 400), 400)).not.toBe(
      paths[0].join(" L "),
    );
  });

  it("keeps local branches aligned despite different target coordinates", () => {
    const paths = [near, far].map((points) =>
      branchEdgePath(points, orientation, 325).split(" L "),
    );
    expect(paths[0].slice(0, 2)).toEqual(paths[1].slice(0, 2));
    const forward = orientation === "vertical" ? 1 : 0;
    expect(paths[0][2].split(",")[forward]).toBe(paths[1][2].split(",")[forward]);
    expect(paths[0][3]).toBe("500,600");
    expect(paths[1][3]).toBe("1000,1500");
  });

  it("keeps aligned neighbors straight and branches long connections at their midpoint", () => {
    const endpoints =
      orientation === "vertical"
        ? { sourceX: 100, sourceY: 200, targetX: 100, targetY: 300 }
        : { sourceX: 100, sourceY: 200, targetX: 200, targetY: 200 };
    const junction =
      orientation === "vertical" ? branchJunction(200, 300) : branchJunction(100, 200);
    const direct = branchEdgePath(endpoints, orientation, junction);
    expect(direct).toBe(
      `M ${endpoints.sourceX},${endpoints.sourceY} L ${endpoints.targetX},${endpoints.targetY}`,
    );
    const long = outerTrunkPath(far, orientation, 1600, 0, junction, 1400).split(" L ");
    expect(long[1]).toBe(orientation === "vertical" ? "100,250" : "150,200");
  });
});
