import type { JsonValue } from "./stepInterface";

/**
 * A readable run key: text parts joined by " / ", numbers as "#n"
 * (["acme/api", 44] is "acme/api #44"). Hosts use it for RunSummaryMsg.label of
 * any keyed run, machine or flow.
 */
export function runKeyLabel(key: readonly JsonValue[]): string {
  return key.reduce<string>((label, part, index) => {
    const text =
      typeof part === "number"
        ? `#${part}`
        : typeof part === "string"
          ? part
          : JSON.stringify(part);
    if (index === 0) return text;
    return `${label}${typeof part === "number" ? " " : " / "}${text}`;
  }, "");
}
