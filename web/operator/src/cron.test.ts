import { describe, expect, it } from "vitest";

import { describeCron } from "./cron";

describe("describeCron", () => {
  it.each([
    ["* * * * *", "Every minute"],
    ["*/1 * * * *", "Every minute"],
    ["*/15 * * * *", "Every 15 minutes"],
    ["0 * * * *", "Every hour, on the hour"],
    ["5 * * * *", "Every hour at :05"],
    ["30 */6 * * *", "Every 6 hours at :30"],
    ["0 9 * * *", "Every day at 09:00"],
    ["15 8 * * 1-5", "Every weekday at 08:15"],
    ["0 18 * * 5", "Every Friday at 18:00"],
    ["0 0 * * 7", "Every Sunday at 00:00"],
    ["0 3 1 * *", "Monthly on day 1 at 03:00"],
    ["0 0 1 1 *", "Every year on January 1 at 00:00"],
  ])("describes %s", (expression, description) => {
    expect(describeCron(expression)).toBe(description);
  });

  it.each(["", "* * * *", "0 9 * * MON", "0-5 * * * *", "61 * * * *", "0 9 1 * 1", "@daily"])(
    "leaves %j undescribed",
    (expression) => {
      expect(describeCron(expression)).toBeUndefined();
    },
  );
});
